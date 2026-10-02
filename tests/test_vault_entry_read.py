"""Reading a whole vault entry in one fetch (ISSUE-583).

An entry is one credential: its password, username, URL and custom fields. The
per-field read charges each of those against the attempt's fetch budget; the
entry read charges the entry once, and must refuse whatever the per-field path
would refuse for any one of its fields.
"""

import json
import pickle

import pytest

from istota import db, secrets_store
from istota.config import Config
from istota.credential_broker import grants
from istota.credential_broker.bindings import parse_binding
from istota.skills._credref import ENTRY, SecretEntry, SecretValue
from tests import test_skill_credential_fd as _credential_fd
from tests import test_vault_credential_fetch as _vault_fetch
from tests.test_credential_broker_reveal import granted_task
from tests.test_skill_credential_fd import start_proxy
from tests.test_vault_credential_fetch import proxy, request

skill_program = _credential_fd.skill_program
sock_path = _vault_fetch.sock_path

ACME = {
    "acme": "entryvalue-password-aaaaaaaa",
    "acme_username": "entryvalue-user-bbbbbbbb",
    "acme_url": "https://acme.example",
    "acme_api_key": "entryvalue-apikey-cccccccc",
}
OTHER = {"github_pat": "entryvalue-ghp-dddddddd"}
SNAPSHOT = {**ACME, **OTHER}


def _store(config, name, value, *, owner, url="https://acme.example", tags=()):
    binding = {**parse_binding(url, {}, list(tags)), "credential": owner}
    secrets_store.upsert_secret(
        config.db_path, "alice", "vault_entries", name, value, binding=binding,
    )


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "data.db")
    db.init_db(config.db_path)
    for name, value in ACME.items():
        _store(config, name, value, owner="acme")
    _store(config, "github_pat", OTHER["github_pat"], owner="github_pat",
           url="https://github.com")
    return config


def set_reveal(config, *, field_tags):
    """Re-store each acme field with its own reveal tag."""
    for name, value in ACME.items():
        _store(config, name, value, owner="acme",
               tags=["istota:reveal"] if field_tags.get(name, True) else [])


def entry_proxy(sock_path, config, **kwargs):
    kwargs.setdefault("vault_credentials", dict(SNAPSHOT))
    return proxy(sock_path, config=config, user_id="alice", **kwargs)


class TestThePublicRead:
    def test_one_request_returns_every_field_and_spends_one_fetch(self, config, sock_path):
        with entry_proxy(sock_path, config) as server:
            reply = request(sock_path, {"type": "vault_entry", "name": "acme"})
            assert server._vault_fetches == 1
        assert reply["fields"] == {
            "password": ACME["acme"],
            "username": ACME["acme_username"],
            "url": ACME["acme_url"],
            "api_key": ACME["acme_api_key"],
        }
        assert reply["bound_hosts"] == ["acme.example"]

    def test_a_field_outside_the_tasks_snapshot_is_not_returned(self, config, sock_path):
        snapshot = {k: v for k, v in SNAPSHOT.items() if k != "acme_api_key"}
        with entry_proxy(sock_path, config, vault_credentials=snapshot):
            reply = request(sock_path, {"type": "vault_entry", "name": "acme"})
        assert "api_key" not in reply["fields"]
        assert ACME["acme_api_key"] not in json.dumps(reply)

    def test_a_withheld_namespace_has_no_entries(self, config, sock_path):
        with entry_proxy(sock_path, config, vault_credentials={}) as server:
            reply = request(sock_path, {"type": "vault_entry", "name": "acme"})
            assert server._vault_fetches == 1
        assert reply["reason"] == "vault_credential_not_present"
        assert "fields" not in reply

    @pytest.mark.parametrize("name", ["absent", "acme_username", ""])
    def test_a_name_that_is_not_an_entry_is_refused(self, config, sock_path, name):
        with entry_proxy(sock_path, config) as server:
            reply = request(sock_path, {"type": "vault_entry", "name": name})
            assert server._vault_fetches == 1
        assert reply["reason"] == "vault_credential_not_present"
        for value in SNAPSHOT.values():
            assert value not in json.dumps(reply)

    def test_the_limit_refusal_is_identical_for_present_and_absent_entries(
        self, config, sock_path,
    ):
        with entry_proxy(sock_path, config, vault_fetch_limit=1):
            request(sock_path, {"type": "vault_entry", "name": "acme"})
            present = request(sock_path, {"type": "vault_entry", "name": "acme"})
            absent = request(sock_path, {"type": "vault_entry", "name": "absent"})
            field = request(sock_path, {"type": "vault_credential", "name": "acme"})
        assert present == absent == field
        assert present["reason"] == "vault_credential_limit"
        assert "name" not in present

    def test_entry_and_field_reads_share_one_budget(self, config, sock_path):
        with entry_proxy(sock_path, config, vault_fetch_limit=2):
            assert "value" in request(sock_path, {"type": "vault_credential", "name": "github_pat"})
            assert "fields" in request(sock_path, {"type": "vault_entry", "name": "acme"})
            reply = request(sock_path, {"type": "vault_credential", "name": "github_pat"})
        assert reply["reason"] == "vault_credential_limit"

    def test_no_value_reaches_the_log(self, config, sock_path, caplog):
        with entry_proxy(sock_path, config):
            request(sock_path, {"type": "vault_entry", "name": "acme", "mode": "read"})
        assert "vault_entry task_id=7 name=acme" in caplog.text
        for value in SNAPSHOT.values():
            assert value not in caplog.text


class TestRevealEnforcement:
    @pytest.fixture(autouse=True)
    def enforce(self, config):
        config.security.credential_broker.enabled = True
        config.security.credential_broker.enforce_reveal = True

    def test_a_fully_revealable_entry_is_returned(self, config, sock_path):
        set_reveal(config, field_tags={})
        with entry_proxy(sock_path, config):
            reply = request(sock_path, {"type": "vault_entry", "name": "acme"})
        assert reply["fields"]["password"] == ACME["acme"]
        assert reply["fields"]["username"] == ACME["acme_username"]

    @pytest.mark.parametrize("hidden", ["acme", "acme_username", "acme_api_key"])
    def test_one_unrevealable_field_refuses_the_whole_entry(self, config, sock_path, hidden):
        set_reveal(config, field_tags={hidden: False})
        with entry_proxy(sock_path, config):
            reply = request(sock_path, {"type": "vault_entry", "name": "acme"})
            single = request(sock_path, {"type": "vault_credential", "name": hidden})
        assert single["reason"] == "credential_brokered"
        assert reply["reason"] == "credential_brokered"
        assert "fields" not in reply
        for value in ACME.values():
            if value != ACME["acme_url"]:
                assert value not in json.dumps(reply)

    def test_audit_mode_returns_and_logs_would_refuse(self, config, sock_path, caplog):
        config.security.credential_broker.enforce_reveal = False
        with entry_proxy(sock_path, config):
            reply = request(sock_path, {"type": "vault_entry", "name": "acme"})
        assert reply["fields"]["password"] == ACME["acme"]
        assert "type=vault_entry name=acme" in caplog.text
        assert "action=would_refuse" in caplog.text


ENTRY_SKILL = """
    import argparse, hashlib, json
    from istota.skills._cli import parse_and_resolve
    from istota.skills._credref import ENTRY, credential_ref
    parser = argparse.ArgumentParser()
    credential_ref(parser, "--login", form=ENTRY)
    args = parse_and_resolve(parser, ["--login", "acme"])
    login = args.login
    print(json.dumps({
        "repr": repr(args),
        "password": hashlib.sha256(login.password.reveal().encode()).hexdigest(),
        "username": login.username.reveal(),
        "url": login.url.reveal(),
        "custom": sorted(login.fields),
        "hosts": list(login.bound_hosts),
    }))
"""


class TestThePrivateSkillRead:
    @pytest.fixture(autouse=True)
    def broker(self, config):
        config.security.credential_broker.enabled = True

    def test_a_granted_entry_resolves_in_one_fetch(self, config, sock_path, skill_program):
        import hashlib
        skill_program(ENTRY_SKILL)
        with db.get_db(config.db_path) as conn:
            grants.put_grant(conn, "alice", "acme", scope_mode="all")
        task_id = granted_task(config)
        with start_proxy(sock_path, config=config, user_id="alice", task_id=task_id) as server:
            server.vault_credentials = dict(SNAPSHOT)
            response = request(sock_path, {"skill": "probe", "args": []})
            assert response["returncode"] == 0, response["stderr"]
            assert server._vault_fetches == 1
        out = json.loads(response["stdout"])
        assert out["password"] == hashlib.sha256(ACME["acme"].encode()).hexdigest()
        assert out["username"] == ACME["acme_username"]
        assert out["url"] == ACME["acme_url"]
        assert out["custom"] == ["api_key", "password", "url", "username"]
        assert out["hosts"] == ["acme.example"]
        assert ACME["acme"] not in out["repr"]

    def test_an_ungranted_entry_is_refused(self, config, sock_path, skill_program):
        skill_program(ENTRY_SKILL)
        task_id = granted_task(config)
        with start_proxy(sock_path, config=config, user_id="alice", task_id=task_id) as server:
            server.vault_credentials = dict(SNAPSHOT)
            response = request(sock_path, {"skill": "probe", "args": []})
        assert response["returncode"] != 0
        assert "credential_not_granted" in response["stdout"] + response["stderr"]
        for value in ACME.values():
            if value != ACME["acme_url"]:
                assert value not in json.dumps(response)


    def test_an_entry_created_this_attempt_needs_no_grant(self, config, sock_path, skill_program):
        skill_program(ENTRY_SKILL)
        task_id = granted_task(config)
        with start_proxy(sock_path, config=config, user_id="alice", task_id=task_id) as server:
            server.vault_credentials = dict(SNAPSHOT)
            server._created_names.update(ACME)
            response = request(sock_path, {"skill": "probe", "args": []})
        assert response["returncode"] == 0, response["stderr"]

    def test_one_created_member_does_not_exempt_the_rest(self, config, sock_path, skill_program):
        skill_program(ENTRY_SKILL)
        task_id = granted_task(config)
        with start_proxy(sock_path, config=config, user_id="alice", task_id=task_id) as server:
            server.vault_credentials = dict(SNAPSHOT)
            server._created_names.add("acme")
            response = request(sock_path, {"skill": "probe", "args": []})
        assert response["returncode"] != 0
        assert "credential_not_granted" in response["stdout"] + response["stderr"]


class TestALocalCredential:
    def test_a_credential_added_in_istota_reads_as_one_entry(self, config, sock_path):
        from istota import local_credentials
        with db.get_db(config.db_path) as conn:
            local_credentials.create(conn, "alice", local_credentials.LocalCredential(
                name="shop", value="entryvalue-local-pw", username="entryvalue-local-user",
                url="https://shop.example",
            ))
        snapshot = {**SNAPSHOT, "shop": "entryvalue-local-pw",
                    "shop_username": "entryvalue-local-user", "shop_url": "https://shop.example"}
        with entry_proxy(sock_path, config, vault_credentials=snapshot) as server:
            reply = request(sock_path, {"type": "vault_entry", "name": "shop"})
            assert server._vault_fetches == 1
        assert reply["fields"] == {"password": "entryvalue-local-pw",
                                   "username": "entryvalue-local-user",
                                   "url": "https://shop.example"}
        assert reply["bound_hosts"] == ["shop.example"]


class TestTheBox:
    def entry(self):
        return SecretEntry("acme", {
            "password": SecretValue("acme", "pw-secret"),
            "username": SecretValue("acme_username", "user-secret"),
        }, ("acme.example",))

    def test_it_renders_redacted(self):
        entry = self.entry()
        for text in (repr(entry), str(entry), f"{entry}", f"{entry:>40}"):
            assert "pw-secret" not in text
            assert "user-secret" not in text
            assert "acme" in text

    def test_it_refuses_to_pickle(self):
        with pytest.raises(TypeError):
            pickle.dumps(self.entry())

    def test_an_absent_standard_field_is_none(self):
        assert self.entry().url is None

    def test_the_form_is_registered(self):
        from istota.skills._credref import FORMS
        assert ENTRY in FORMS


class TestTheFieldKeys:
    def test_members_map_to_field_keys(self):
        from istota.sandbox.skill_proxy import entry_fields
        assert entry_fields("acme", {"acme": "p", "acme_username": "u", "acme_url": "w",
                                     "acme_pin": "1"}) == {
            "password": "p", "username": "u", "url": "w", "pin": "1"}

    def test_a_custom_field_named_password_keeps_its_full_name(self):
        from istota.sandbox.skill_proxy import entry_fields
        assert entry_fields("acme", {"acme_password": "custom"}) == {"acme_password": "custom"}
        assert entry_fields("acme", {"acme": "p", "acme_password": "custom"}) == {
            "password": "p", "acme_password": "custom"}

    def test_a_fallback_name_never_overwrites_another_field(self):
        from istota.sandbox.skill_proxy import entry_fields
        fields = entry_fields("acme", {"acme": "p", "acme_acme_password": "A",
                                       "acme_password": "B"})
        assert sorted(fields.values()) == ["A", "B", "p"]
        assert fields["password"] == "p"
