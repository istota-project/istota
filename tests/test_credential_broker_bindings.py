"""Bindings follow the vault value and constrain browser destinations."""

import pytest

from istota import db
from istota.credentials import store as secrets_store
from istota.credentials import vault as secrets_vault
from tests.test_secrets_vault import _new_db, _read


@pytest.mark.parametrize("url,host", [
    ("portal.example.com", "portal.example.com"),
    ("Portal.Example.com:443", "portal.example.com"),
    ("portal.example.com:8443", "portal.example.com:8443"),
    ("[2001:db8::1]", "[2001:db8::1]"),
])
def test_vault_bare_authority_binds_for_https(url, host):
    from istota.credentials.broker.bindings import https_host, parse_binding
    assert parse_binding(url, {}, [])["hosts"] == [host]
    assert parse_binding(url, {}, [], source="local") == {
        **parse_binding(url, {}, []), "source": "local",
    }
    assert parse_binding(url, {}, [], source="config")["hosts"] == []
    with pytest.raises(ValueError):
        https_host(url)


def test_vault_metadata_is_not_a_credential(tmp_path):
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "portal", "alice", "fixture-password",
                         url="https://Portal.Example.com:443/login")
    entry.set_custom_property("istota_hosts", "api.example.com, portal.example.com:8443")
    entry.set_custom_property("istota_headers", "Authorization, X-API-Key")
    entry.set_custom_property("istota_future", "metadata")
    entry.tags = ["istota:reveal"]
    kp.save()
    read, _ = _read(path)
    assert not any("istota_" in name for name in read.services)
    assert read.bindings["portal"] == {
        "hosts": ["api.example.com", "portal.example.com", "portal.example.com:8443"],
        "headers": ["authorization", "x-api-key"], "revealable": True,
        "source": "vault", "credential": "portal",
    }
    assert read.bindings["portal_username"] == read.bindings["portal"]


@pytest.mark.parametrize("url,hosts", [
    ("http:portal.example.com", ""),
    ("//portal.example.com", ""),
    ("portal.example.com/path", ""),
    ("user@portal.example.com", ""),
    ("*.example.com", ""),
    ("portal.example.com:invalid", ""),
    ("portal.example.com\\evil", ""),
    ("portal.example.com\n", ""),
    ("https://portal.example.com", "*.example.com"),
    ("https://portal.example.com", "evil.example/path"),
    ("https://portal.example.com", "user@evil.example"),
])
def test_invalid_binding_fails_closed(url, hosts):
    from istota.credentials.broker.bindings import parse_binding
    binding = parse_binding(url, {"istota_hosts": hosts}, [])
    assert binding["hosts"] == []


def test_proxy_authorization_is_never_allowed():
    from istota.credentials.broker.bindings import parse_binding
    binding = parse_binding("https://example.com", {
        "istota_headers": "Proxy-Authorization, Authorization",
    }, [])
    assert binding["headers"] == ["authorization"]


def test_sync_updates_and_deletes_bindings_with_values(tmp_path, monkeypatch):
    from istota.credentials.broker.bindings import get_binding
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    database = tmp_path / "data.db"
    db.init_db(database)
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "portal", "", "fixture-password",
                         url="https://portal.example.com")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    with db.get_db(database) as conn:
        assert get_binding(conn, "alice", "portal")["hosts"] == ["portal.example.com"]
        assert get_binding(conn, "bob", "portal") is None
    entry.url = "https://other.example.com"
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    with db.get_db(database) as conn:
        assert get_binding(conn, "alice", "portal")["hosts"] == ["other.example.com"]
    secrets_store.delete_secret(database, "alice", "vault_entries", "portal")
    with db.get_db(database) as conn:
        assert get_binding(conn, "alice", "portal") is None


def test_forge_bindings_use_config_without_vault():
    from istota.config import DeveloperConfig
    from istota.credentials.broker.bindings import forge_bindings
    config = DeveloperConfig(gitlab_token="fixture-token", github_token="fixture-token")
    bindings = forge_bindings(config)
    assert bindings["forge.github"]["hosts"] == ["api.github.com", "github.com"]
    config.gitlab_url = "https://forge.example.com:8443"
    assert forge_bindings(config)["forge.gitlab"]["hosts"] == ["forge.example.com:8443"]
    config.github_token = ""
    assert "forge.github" not in forge_bindings(config)


def test_binding_write_rolls_back_value_on_failure(tmp_path, monkeypatch):
    database = tmp_path / "data.db"
    db.init_db(database)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    from istota.credentials.broker import bindings
    def fail(*args):
        raise RuntimeError("binding write failed")
    monkeypatch.setattr(bindings, "put_binding", fail)
    with pytest.raises(RuntimeError, match="binding write failed"):
        secrets_store.upsert_secret(database, "alice", "vault_entries", "portal",
                                    "fixture-password", binding={})
    assert secrets_store.get_secret(database, "alice", "vault_entries", "portal") is None


def test_sync_sweep_removes_binding_but_truncation_holds_it(tmp_path, monkeypatch):
    from istota.credentials.broker.bindings import get_binding, parse_binding
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    database = tmp_path / "data.db"
    db.init_db(database)
    secrets_store.upsert_secret(database, "alice", "vault_entries", "portal", "fixture-password",
                               binding=parse_binding("https://portal.example", {}, []))
    read = secrets_vault.VaultRead("digest", {}, frozenset(), "entry", True)
    secrets_vault.apply_vault(database, "alice", read)
    with db.get_db(database) as conn:
        assert get_binding(conn, "alice", "portal") is not None
    from dataclasses import replace
    secrets_vault.apply_vault(database, "alice", replace(read, truncated=""))
    with db.get_db(database) as conn:
        assert get_binding(conn, "alice", "portal") is None


def test_upgrade_creates_binding_table_idempotently(tmp_path):
    database = tmp_path / "data.db"
    db.init_db(database)
    with db.get_db(database) as conn:
        conn.execute("DROP TABLE credential_bindings")
    db.init_db(database)
    db.init_db(database)
    with db.get_db(database) as conn:
        assert conn.execute("SELECT count(*) FROM credential_bindings").fetchone()[0] == 0


def test_proxy_resolves_live_value_and_hosts_together(tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path
    from istota.config import Config
    from istota.credentials.broker.bindings import parse_binding
    from istota.sandbox.skill_proxy import SkillProxy
    from istota.skills._credref import _resolve_name
    from tests.test_vault_credential_fetch import request
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    database = tmp_path / "data.db"
    db.init_db(database)
    binding = parse_binding("https://portal.example", {}, ["istota:reveal"])
    with tempfile.TemporaryDirectory(prefix="binding_", dir="/tmp") as directory:
        socket = Path(directory) / "s"
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(socket))
        with SkillProxy(socket, {}, {}, config=Config(db_path=database), user_id="alice",
                        vault_credentials={"portal": "stale-password"}):
            secrets_store.upsert_secret(database, "alice", "vault_entries", "portal",
                                        "current-password", binding=binding)
            secret, error = _resolve_name("portal", "test")
            assert error is None
            assert secret.reveal() == "current-password"
            assert secret.bound_hosts == ("portal.example",)
            reply = request(socket, {"type": "vault_list"})
            assert reply["credentials"] == [{"name": "portal", "bound_hosts": ["portal.example"],
                                              "revealable": True, "grant": "ungranted"}]
            secrets_store.delete_secret(database, "alice", "vault_entries", "portal")
            secret, error = _resolve_name("portal", "test")
            assert secret is None
            assert "no longer available" in error


def test_vault_list_names_whole_entries_in_the_snapshot(tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path
    from istota.config import Config
    from istota.sandbox.credential_shim import ProxyError, list_entries
    from istota.sandbox.skill_proxy import SkillProxy
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    database = tmp_path / "data.db"
    db.init_db(database)
    # `blogfield` belongs to wordpress_blog by the parser's record alone: a
    # grouping by name suffix would list it as an entry of its own.
    fields = {"wordpress_blog": None, "wordpress_blog_username": "wordpress_blog",
              "blogfield": "wordpress_blog", "wordpress_nopw_url": "wordpress_nopw",
              "hidden": None}
    for key, entry in fields.items():
        secrets_store.upsert_secret(database, "alice", "vault_entries", key, "fixture")
        if entry:
            with db.get_db(database) as conn:
                db.kv_set(conn, "alice", "_credential_fields", key, entry)
    snapshot = {key: "fixture" for key in fields if key != "hidden"}
    with tempfile.TemporaryDirectory(prefix="entries_", dir="/tmp") as directory:
        socket = Path(directory) / "s"
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(socket))
        with SkillProxy(socket, {}, {}, config=Config(db_path=database), user_id="alice",
                        vault_credentials=snapshot):
            # Grouped by the parser's record, not by suffix; an entry with no
            # password is still an entry; one outside the snapshot is not listed.
            assert list_entries() == ["wordpress_blog", "wordpress_nopw"]
        with SkillProxy(socket, {}, {}, config=Config(db_path=database), user_id="alice",
                        vault_credentials={}):
            assert list_entries() == []
        # A proxy that cannot group says nothing, and the shim refuses rather
        # than answering "no entries".
        with SkillProxy(socket, {}, {}, vault_credentials=snapshot):
            with pytest.raises(ProxyError):
                list_entries()


def test_forge_sync_replaces_and_removes_bindings(tmp_path):
    from istota.config import DeveloperConfig
    from istota.credentials.broker.bindings import get_binding, sync_forge_bindings
    database = tmp_path / "data.db"
    db.init_db(database)
    config = DeveloperConfig(gitlab_token="fixture-token")
    with db.get_db(database) as conn:
        sync_forge_bindings(conn, "alice", config)
        assert get_binding(conn, "alice", "forge.gitlab")["hosts"] == ["gitlab.com"]
        config.gitlab_token = ""
        sync_forge_bindings(conn, "alice", config)
        assert get_binding(conn, "alice", "forge.gitlab") is None


def test_empty_value_still_revokes_hosts_and_reveal(tmp_path, monkeypatch):
    from istota.credentials.broker.bindings import get_binding
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    database = tmp_path / "data.db"
    db.init_db(database)
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "portal", "", "fixture-password", url="https://portal.example")
    entry.tags = ["istota:reveal"]
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    entry.password = ""
    entry.url = ""
    entry.tags = []
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    assert secrets_store.get_secret(database, "alice", "vault_entries", "portal") == "fixture-password"
    with db.get_db(database) as conn:
        binding = get_binding(conn, "alice", "portal")
        assert binding["hosts"] == []
        assert binding["revealable"] is False


def test_list_columns_include_unbound_and_forge(tmp_path, monkeypatch, capsys):
    import tempfile
    from pathlib import Path
    from istota.sandbox import credential_shim
    from istota.config import Config, DeveloperConfig
    from istota.sandbox.skill_proxy import SkillProxy
    database = tmp_path / "data.db"
    db.init_db(database)
    config = Config(db_path=database, developer=DeveloperConfig(github_token="fixture-token"))
    with tempfile.TemporaryDirectory(prefix="binding_", dir="/tmp") as directory:
        socket = Path(directory) / "s"
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(socket))
        with SkillProxy(socket, {"GITHUB_TOKEN": "fixture-token"}, {}, config=config,
                        user_id="alice", vault_credentials={"portal": "fixture-password"}):
            assert credential_shim._cmd_list() == 0
    output = capsys.readouterr().out
    assert "NAME\tBOUND HOSTS\tREVEALABLE\tGRANT" in output
    assert "portal\tunbound\tno\tungranted" in output
    assert "forge.github\tapi.github.com,github.com" in output
    assert "fixture-password" not in output
    assert "fixture-token" not in output


def test_empty_custom_metadata_roundtrips_as_none(tmp_path):
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "portal", "", "fixture-password", url="https://portal.example")
    entry.set_custom_property("istota_hosts", "")
    entry.set_custom_property("istota_headers", "")
    kp.save()
    read, _ = _read(path)
    assert read.bindings["portal"]["hosts"] == ["portal.example"]
    assert "authorization" in read.bindings["portal"]["headers"]
    assert not any("istota_" in name for name in read.services)


@pytest.mark.parametrize("url,host", [
    ("http://192.0.2.10:8080/login", "http://192.0.2.10:8080"),
    ("http://portal.example:80", "http://portal.example"),
    ("http://[2001:db8::1]:8080/", "http://[2001:db8::1]:8080"),
])
def test_http_vault_metadata_keeps_scheme(url, host):
    from istota.credentials.broker.bindings import https_host, parse_binding
    assert parse_binding(url, {}, [])["hosts"] == [host]
    assert parse_binding(url, {}, [], source="local")["hosts"] == [host]
    assert parse_binding(url, {}, [], source="config")["hosts"] == []
    with pytest.raises(ValueError):
        https_host(url)


def test_removing_custom_field_preserves_entry_grant(tmp_path, monkeypatch):
    from istota.credentials.broker import grants
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    database = tmp_path / "data.db"
    db.init_db(database)
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "portal", "alice", "fixture-password",
                         url="https://portal.example")
    entry.set_custom_property("note", "fixture-note")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    with db.get_db(database) as conn:
        granted = grants.put_grant(conn, "alice", "portal")
    entry.delete_custom_property("note")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    with db.get_db(database) as conn:
        assert grants.get_grant(conn, "alice", "portal") == granted
    assert not secrets_store.secret_exists(database, "alice", "vault_entries", "portal_note")
    kp.delete_entry(entry)
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(database, "alice", read)
    with db.get_db(database) as conn:
        assert grants.get_grant(conn, "alice", "portal") is None
