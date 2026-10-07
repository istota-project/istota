"""Credentials created in Istota: `source="local"` rows in `vault_entries`.

Every test runs against a real temp database through the real Fernet layer,
because what is asserted is the state of rows after a write and a refusal, and
the refusal half is only meaningful if a write that should not have happened
could have.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from istota import db
from istota.credentials import local as local_credentials
from istota.credentials import store as secrets_store
from istota.credentials.broker import bindings, grants
from istota.credentials.local import LocalCredential, LocalCredentialError
from istota.credentials.vault import VAULT_ENTRY_SERVICE, VAULT_MAX_VALUE_BYTES

VALUE = "lc-fixture-value-not-real"
NEW_VALUE = "lc-fixture-value-rotated"
USERNAME = "lc-fixture-user"

ALL_ROOMS = {"scope_mode": "all", "rooms": [], "allow_scheduled": False, "allow_http": False}


@pytest.fixture(autouse=True)
def secret_key_env():
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


def _create(db_path, cred, *, access=None, user="alice"):
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return local_credentials.create(conn, user, cred, access=access)


def _update(db_path, name, *, user="alice", **fields):
    defaults = {"value": None, "username": "", "url": "", "extra_hosts": "",
                "headers": "", "revealable": False}
    defaults.update(fields)
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return local_credentials.update(conn, user, name, **defaults)


def _entry(db_path, name, user="alice"):
    return secrets_store.get_secret(db_path, user, VAULT_ENTRY_SERVICE, name)


def _counts(db_path):
    with db.get_db(db_path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM credential_bindings").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM credential_grants").fetchone()[0],
        )


def _binding(db_path, name, user="alice"):
    with db.get_db(db_path) as conn:
        return bindings.get_binding(conn, user, name)


def _grant(db_path, name, user="alice"):
    with db.get_db(db_path) as conn:
        return grants.get_grant(conn, user, name)


class TestCreate:
    def test_value_only_writes_one_row_with_a_local_binding(self, db_path):
        result = _create(db_path, LocalCredential(name="api_key", value=VALUE))

        assert result == {"name": "api_key", "username_name": None,
                          "url_name": None, "grant": None}
        assert _entry(db_path, "api_key") == VALUE
        assert _entry(db_path, "api_key_username") is None
        assert _entry(db_path, "api_key_url") is None
        binding = _binding(db_path, "api_key")
        assert binding["source"] == "local"
        assert binding["hosts"] == []
        assert binding["headers"] == bindings.DEFAULT_HEADERS
        assert VALUE not in repr(result)

    def test_every_field_is_one_group_with_the_site_bound(self, db_path):
        result = _create(db_path, LocalCredential(
            name="openrouter_key", value=VALUE, username=USERNAME,
            url="openrouter.ai", extra_hosts="api.openrouter.ai",
            headers="x-api-key", revealable=True,
        ))

        assert result["username_name"] == "openrouter_key_username"
        assert result["url_name"] == "openrouter_key_url"
        assert _entry(db_path, "openrouter_key_username") == USERNAME
        assert _entry(db_path, "openrouter_key_url") == "openrouter.ai"
        with db.get_db(db_path) as conn:
            groups = bindings.credential_groups(conn, "alice")
            entry = bindings.get_entry_binding(conn, "alice", "openrouter_key")
            for field in ("openrouter_key", "openrouter_key_username", "openrouter_key_url"):
                assert bindings.credential_name(conn, "alice", field) == "openrouter_key"
                assert bindings.get_binding(conn, "alice", field)["source"] == "local"
        assert sorted(groups["openrouter_key"]) == [
            "openrouter_key", "openrouter_key_url", "openrouter_key_username",
        ]
        assert entry == {"hosts": ["api.openrouter.ai", "openrouter.ai"],
                         "headers": ["x-api-key"], "revealable": True, "source": "local", "kind": "value"}

    def test_http_site_is_kept_with_its_scheme(self, db_path):
        _create(db_path, LocalCredential(name="lan", value=VALUE, url="http://192.0.2.10:8080"))
        assert _binding(db_path, "lan")["hosts"] == ["http://192.0.2.10:8080"]

    def test_access_writes_the_grant_in_the_same_transaction(self, db_path):
        result = _create(db_path, LocalCredential(name="api_key", value=VALUE,
                                                  url="api.example.com"),
                         access=ALL_ROOMS)

        assert result["grant"]["scope_mode"] == "all"
        assert _grant(db_path, "api_key")["scope_mode"] == "all"

    def test_access_without_a_site_writes_nothing(self, db_path):
        before = _counts(db_path)
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE,
                                             username=USERNAME), access=ALL_ROOMS)
        assert exc.value.field == "access"
        assert _counts(db_path) == before

    def test_a_bad_access_policy_rolls_back_every_row(self, db_path):
        before = _counts(db_path)
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE,
                                             username=USERNAME, url="api.example.com"),
                    access={"scope_mode": "everywhere"})
        assert exc.value.field == "access"
        assert _counts(db_path) == before

    @pytest.mark.parametrize("name", [
        "OpenRouter Key", "9key", "a__b_", "forge.gitlab", "generated_thing",
        "", "x" * 65, "x" * 60,
    ])
    def test_a_name_that_is_not_canonical_is_refused(self, db_path, name):
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name=name, value=VALUE))
        assert exc.value.field == "name"
        assert _counts(db_path)[0] == 0

    def test_a_name_already_used_by_any_source_is_refused(self, db_path):
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "api_key", "from-the-file",
            binding=bindings.parse_binding("api.example.com", {}, []),
        )
        with pytest.raises(LocalCredentialError, match="already exists") as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE))
        assert exc.value.field == "name"
        assert _entry(db_path, "api_key") == "from-the-file"

    def test_a_derived_name_clash_is_named(self, db_path):
        _create(db_path, LocalCredential(name="api_url", value=VALUE))
        with pytest.raises(LocalCredentialError, match="api_url") as exc:
            _create(db_path, LocalCredential(name="api", value=VALUE))
        assert exc.value.field == "name"

    def test_a_group_owner_name_is_refused(self, db_path):
        # A file entry with only a username: its owner name holds no row.
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "portal_login", "someone",
            binding={**bindings.parse_binding("", {}, []), "credential": "portal"},
        )
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="portal", value=VALUE))
        assert exc.value.field == "name"

    def test_a_binding_name_with_no_value_row_is_refused(self, db_path):
        with db.get_db(db_path) as conn:
            bindings.put_binding(conn, "alice", "orphan",
                                 bindings.parse_binding("", {}, [], source="config"))
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="orphan", value=VALUE))
        assert exc.value.field == "name"

    @pytest.mark.parametrize("value", [
        "", "   ", f" {VALUE}", f"{VALUE}\n", "x" * (VAULT_MAX_VALUE_BYTES + 1), 42, None,
    ])
    def test_a_bad_value_is_refused(self, db_path, value):
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=value))
        assert exc.value.field == "value"
        assert _counts(db_path)[0] == 0
        if isinstance(value, str) and value.strip():
            assert value.strip() not in str(exc.value)

    def test_a_value_at_the_cap_is_accepted(self, db_path):
        _create(db_path, LocalCredential(name="api_key", value="x" * VAULT_MAX_VALUE_BYTES))
        assert _entry(db_path, "api_key") == "x" * VAULT_MAX_VALUE_BYTES

    def test_a_username_with_surrounding_whitespace_is_refused(self, db_path):
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE, username=" bob"))
        assert exc.value.field == "username"

    @pytest.mark.parametrize("url", [
        "https://user@host.example", "ftp://x.example", "host.example/path", "not a host",
        "https://host.example/v1", "https://host.example/?key=abc", "host.example?key=abc",
        "https://host.example#frag", "http://host.example:8080/login",
    ])
    def test_an_invalid_site_is_refused(self, db_path, url):
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE, url=url))
        assert exc.value.field == "url"
        assert _counts(db_path)[0] == 0

    @pytest.mark.parametrize("url", [
        "host.example", "host.example:8443", "https://host.example", "https://host.example/",
        "http://192.0.2.10:8080",
    ])
    def test_a_site_without_path_or_query_is_accepted(self, db_path, url):
        _create(db_path, LocalCredential(name="api_key", value=VALUE, url=url))
        assert _counts(db_path)[0] == 2

    @pytest.mark.parametrize("hosts", ["bad/host", "ok.example, http://x.example", "@x"])
    def test_invalid_extra_hosts_are_refused(self, db_path, hosts):
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE,
                                             url="api.example.com", extra_hosts=hosts))
        assert exc.value.field == "extra_hosts"

    @pytest.mark.parametrize("headers", ["x api", "proxy-authorization"])
    def test_invalid_headers_are_refused(self, db_path, headers):
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="api_key", value=VALUE, headers=headers))
        assert exc.value.field == "headers"


class TestUpdate:
    def _seed(self, db_path, **extra):
        fields = {"name": "api_key", "value": VALUE, "username": USERNAME,
                  "url": "api.example.com"}
        fields.update(extra)
        _create(db_path, LocalCredential(**fields))

    def test_value_none_keeps_the_stored_value(self, db_path):
        self._seed(db_path)
        _update(db_path, "api_key", value=None, username=USERNAME, url="api.example.com")
        assert _entry(db_path, "api_key") == VALUE

    def test_a_new_value_replaces_it(self, db_path):
        self._seed(db_path)
        result = _update(db_path, "api_key", value=NEW_VALUE, username=USERNAME,
                         url="api.example.com")
        assert _entry(db_path, "api_key") == NEW_VALUE
        assert NEW_VALUE not in repr(result)

    def test_an_empty_username_deletes_that_row(self, db_path):
        self._seed(db_path)
        result = _update(db_path, "api_key", username="", url="api.example.com")
        assert _entry(db_path, "api_key_username") is None
        assert _binding(db_path, "api_key_username") is None
        assert result["username_name"] is None
        with db.get_db(db_path) as conn:
            assert sorted(bindings.credential_groups(conn, "alice")["api_key"]) == [
                "api_key", "api_key_url",
            ]

    def test_every_fields_binding_is_rewritten(self, db_path):
        self._seed(db_path)
        _update(db_path, "api_key", username=USERNAME, url="new.example.com",
                extra_hosts="other.example.com", headers="x-api-key", revealable=True)
        for field in ("api_key", "api_key_username", "api_key_url"):
            assert _binding(db_path, field) == {
                "hosts": ["new.example.com", "other.example.com"],
                "headers": ["x-api-key"], "revealable": True, "source": "local", "kind": "value",
            }
        assert _entry(db_path, "api_key_url") == "new.example.com"

    def test_a_username_can_be_added(self, db_path):
        self._seed(db_path, username="")
        _update(db_path, "api_key", username=USERNAME, url="api.example.com")
        assert _entry(db_path, "api_key_username") == USERNAME
        with db.get_db(db_path) as conn:
            assert bindings.credential_name(conn, "alice", "api_key_username") == "api_key"

    def test_a_vault_sourced_name_is_refused(self, db_path):
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "file_key", "from-the-file",
            binding=bindings.parse_binding("api.example.com", {}, []),
        )
        with pytest.raises(LocalCredentialError, match="KeePassXC") as exc:
            _update(db_path, "file_key", value=NEW_VALUE)
        assert exc.value.field == "name"
        assert _entry(db_path, "file_key") == "from-the-file"

    def test_an_unknown_name_is_refused(self, db_path):
        with pytest.raises(LocalCredentialError) as exc:
            _update(db_path, "nothing_here", value=NEW_VALUE)
        assert exc.value.field == "name"
        assert _counts(db_path)[0] == 0

    def test_clearing_the_site_of_a_granted_credential_is_refused(self, db_path):
        _create(db_path, LocalCredential(name="api_key", value=VALUE,
                                         url="api.example.com"), access=ALL_ROOMS)
        with pytest.raises(LocalCredentialError) as exc:
            _update(db_path, "api_key", value=NEW_VALUE, url="")
        assert exc.value.field == "url"
        assert _entry(db_path, "api_key") == VALUE
        assert _entry(db_path, "api_key_url") == "api.example.com"
        assert _grant(db_path, "api_key") is not None

    def test_clearing_the_site_without_a_grant_unbinds(self, db_path):
        self._seed(db_path)
        _update(db_path, "api_key", username=USERNAME, url="")
        assert _entry(db_path, "api_key_url") is None
        assert _binding(db_path, "api_key")["hosts"] == []

    def test_a_field_name_another_local_credential_owns_is_never_taken_over(self, db_path):
        """`foo` was created with no site, so `foo_url` was free to be a
        credential of its own. Giving `foo` a site must not overwrite it."""
        _create(db_path, LocalCredential(name="foo", value=VALUE))
        with pytest.raises(LocalCredentialError) as exc:
            _create(db_path, LocalCredential(name="foo_url", value=NEW_VALUE,
                                             url="api.example.com"), access=ALL_ROOMS)
        assert exc.value.field == "name"

    @pytest.mark.parametrize("site", ["other.example.com", ""])
    def test_update_refuses_a_field_row_owned_by_a_file_entry(self, db_path, site):
        """A KeePassXC entry titled `foo url` is a credential named `foo_url`.
        Editing a local `foo` must neither overwrite nor delete it."""
        _create(db_path, LocalCredential(name="foo", value=VALUE))
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "foo_url", "from-the-file",
            binding=bindings.parse_binding("files.example.com", {}, []),
        )
        with pytest.raises(LocalCredentialError) as exc:
            _update(db_path, "foo", url=site)
        assert exc.value.field == "url"
        assert _entry(db_path, "foo_url") == "from-the-file"
        with db.get_db(db_path) as conn:
            assert bindings.credential_name(conn, "alice", "foo_url") == "foo_url"
            assert bindings.get_binding(conn, "alice", "foo_url")["source"] == "vault"

    def test_update_refuses_a_username_row_owned_by_another_credential(self, db_path):
        _create(db_path, LocalCredential(name="foo", value=VALUE))
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "foo_username", "from-the-file",
            binding=bindings.parse_binding("", {}, []),
        )
        with pytest.raises(LocalCredentialError) as exc:
            _update(db_path, "foo", username=USERNAME)
        assert exc.value.field == "username"
        assert _entry(db_path, "foo_username") == "from-the-file"

    def test_a_bad_new_value_changes_nothing(self, db_path):
        self._seed(db_path)
        with pytest.raises(LocalCredentialError) as exc:
            _update(db_path, "api_key", value=" padded ", username="",
                    url="other.example.com")
        assert exc.value.field == "value"
        assert _entry(db_path, "api_key") == VALUE
        assert _entry(db_path, "api_key_username") == USERNAME
        assert _binding(db_path, "api_key")["hosts"] == ["api.example.com"]


class TestDelete:
    def test_delete_removes_every_row_binding_and_grant(self, db_path):
        _create(db_path, LocalCredential(name="api_key", value=VALUE, username=USERNAME,
                                         url="api.example.com"), access=ALL_ROOMS)

        assert local_credentials.delete(db_path, "alice", "api_key") is True

        assert _counts(db_path) == (0, 0, 0)
        assert _grant(db_path, "api_key") is None
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM istota_kv WHERE namespace='_credential_fields'"
            ).fetchone()[0] == 0

    def test_delete_refuses_a_vault_sourced_name(self, db_path):
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "file_key", "from-the-file",
            binding=bindings.parse_binding("api.example.com", {}, []),
        )
        with pytest.raises(LocalCredentialError):
            local_credentials.delete(db_path, "alice", "file_key")
        assert _entry(db_path, "file_key") == "from-the-file"

    def test_delete_of_a_missing_name_is_false(self, db_path):
        assert local_credentials.delete(db_path, "alice", "nothing_here") is False

    def test_delete_is_per_user(self, db_path):
        _create(db_path, LocalCredential(name="api_key", value=VALUE), user="alice")
        _create(db_path, LocalCredential(name="api_key", value=NEW_VALUE), user="bob")
        local_credentials.delete(db_path, "alice", "api_key")
        assert _entry(db_path, "api_key", user="bob") == NEW_VALUE


class TestIsLocal:
    def test_is_local_reads_the_binding_source(self, db_path):
        _create(db_path, LocalCredential(name="api_key", value=VALUE, username=USERNAME))
        secrets_store.set_secret(
            db_path, "alice", VAULT_ENTRY_SERVICE, "file_key", "from-the-file",
            binding=bindings.parse_binding("", {}, []),
        )
        with db.get_db(db_path) as conn:
            assert local_credentials.is_local(conn, "alice", "api_key") is True
            assert local_credentials.is_local(conn, "alice", "file_key") is False
            assert local_credentials.is_local(conn, "alice", "missing") is False
            assert local_credentials.is_local(conn, "bob", "api_key") is False


class TestSetSecretOnACallersConnection:
    def test_the_write_rolls_back_with_the_callers_transaction(self, db_path):
        with pytest.raises(RuntimeError):
            with db.get_db(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                secrets_store.set_secret(db_path, "alice", "svc", "k", VALUE, connection=conn)
                raise RuntimeError("abort")
        assert secrets_store.get_secret(db_path, "alice", "svc", "k") is None

    def test_the_write_commits_with_the_callers_transaction(self, db_path):
        with db.get_db(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            secrets_store.set_secret(db_path, "alice", "svc", "k", VALUE, connection=conn)
        assert secrets_store.get_secret(db_path, "alice", "svc", "k") == VALUE
