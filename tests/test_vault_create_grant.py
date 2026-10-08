"""An entry a task creates is granted to that task's conversation (ISSUE-684).

Later tasks in the same conversation may use it; other conversations,
scheduled tasks and a grant the user narrowed or revoked stay as they were.
"""

import base64

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.credentials import store, vault
from istota.credentials.broker import bindings, grants
from istota.sandbox.skill_proxy import SkillProxy
from tests.support.kdbx import create_database
from tests import test_skill_proxy_vault_create as _vault_create
from tests.test_skill_proxy_otp import ask
from tests.test_skill_proxy_vault_create import _request

sock = _vault_create.sock

SEED = base64.b32encode(b"01234567890123456789").decode()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "vault.kdbx"
    create_database(str(path), password="test-passphrase")
    config = Config(db_path=tmp_path / "test.db", users={"alice": UserConfig(vault_path=str(path))})
    config.security.credential_broker.enabled = True
    db.init_db(config.db_path)
    store.upsert_secret(config.db_path, "alice", "vault", "passphrase", "test-passphrase")
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    return config, path


def _task(config, room, source_type="talk"):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, user_id="alice", prompt="test", source_type=source_type,
                                 conversation_token=room)
    with db.get_db(config.db_path) as conn:
        grants.ensure_credential_grants(conn, task_id, "alice")
    return task_id


def _create(config, sock, task_id, slug="acme", url="https://acme.example"):
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=task_id,
                    vault_write_limit=1):
        return _request(sock, {"type": "vault_create", "slug": slug, "username": "alice", "url": url})


def _use(config, task_id, name="generated_acme"):
    with db.get_db(config.db_path) as conn:
        return grants.check_credential_use(conn, task_id, "alice", name)


def _namespace(config):
    return store.get_service_secrets(config.db_path, "alice", vault.VAULT_ENTRY_SERVICE)


def _notice(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT body FROM notifications WHERE user_id='alice' "
                            "AND dedup_key LIKE 'vault-created:%'").fetchone()["body"]


def _sync(config, path):
    vault.apply_vault(config.db_path, "alice", vault.parse_vault(path.read_bytes(), "test-passphrase"))


def test_a_later_task_in_the_same_conversation_can_fill_the_entry(configured, sock):
    config, _ = configured
    assert _create(config, sock, _task(config, "room-a"))["name"] == "generated_acme"
    later = _task(config, "room-a")
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=later,
                    vault_credentials=_namespace(config)) as server:
        for name in ("generated_acme", "generated_acme_username"):
            reply = ask(server, sock, {"type": "vault_credential", "name": name}, True)
            assert "value" in reply, reply
    with db.get_db(config.db_path) as conn:
        grant = grants.get_grant(conn, "alice", "generated_acme")
    assert (grant["scope_mode"], grant["rooms"], grant["allow_scheduled"], grant["allow_http"]) == (
        "rooms", ["room-a"], False, False,
    )
    assert "Later tasks in the conversation that created it can use it" in _notice(config)


def test_another_conversation_and_a_scheduled_task_are_refused(configured, sock):
    config, _ = configured
    _create(config, sock, _task(config, "room-a"))
    assert _use(config, _task(config, "room-b")) == "credential_not_granted"
    assert _use(config, _task(config, "room-a", source_type="scheduled")) == "credential_not_granted"


def test_a_task_with_no_conversation_grants_nothing(configured, sock):
    config, _ = configured
    _create(config, sock, _task(config, None))
    with db.get_db(config.db_path) as conn:
        assert grants.get_grant(conn, "alice", "generated_acme") is None
    assert "Only the task that created it can use it" in _notice(config)


def test_a_revoked_or_narrowed_grant_survives_the_next_sync(configured, sock):
    config, path = configured
    _create(config, sock, _task(config, "room-a"))
    _create(config, sock, _task(config, "room-a"), slug="other")
    with db.get_db(config.db_path) as conn:
        grants.delete_grant(conn, "alice", "generated_acme")
        grants.put_grant(conn, "alice", "generated_other", scope_mode="rooms", rooms=["room-c"])
    _sync(config, path)
    with db.get_db(config.db_path) as conn:
        assert grants.get_grant(conn, "alice", "generated_acme") is None
        assert grants.get_grant(conn, "alice", "generated_other")["rooms"] == ["room-c"]
        assert grants.auto_grant_marker(conn, "alice", "generated_acme") == grants.AUTO_GRANT_DONE


def test_otp_set_in_one_task_then_fill_otp_in_the_next(configured, sock):
    config, _ = configured
    _create(config, sock, _task(config, "room-a"))
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=_task(config, "room-a"),
                    vault_write_limit=1):
        assert _request(sock, {"type": "vault_otp_set", "name": "generated_acme", "otp": SEED}) == {
            "name": "generated_acme", "otp": True,
        }
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=_task(config, "room-a"),
                    vault_credentials=_namespace(config)) as server:
        assert "code" in ask(server, sock, {"type": "vault_otp", "name": "generated_acme"}, True)


def test_an_entry_written_under_generated_by_hand_is_still_declined(configured, sock):
    config, path = configured
    _create(config, sock, _task(config, "room-a"))
    from pykeepass import PyKeePass
    kp = PyKeePass(str(path), password="test-passphrase")
    group = kp.find_groups(name="generated", first=True)
    kp.add_entry(group, "handmade", "alice", "fixture-password", url="https://handmade.example")
    kp.save()
    _sync(config, path)
    with db.get_db(config.db_path) as conn:
        # Imported as a generated credential (ISSUE-686), and never granted by the sync.
        assert bindings.get_binding(conn, "alice", "generated_handmade")["source"] == "generated"
        assert grants.get_grant(conn, "alice", "generated_handmade") is None
        assert grants.get_grant(conn, "alice", "generated_acme")["rooms"] == ["room-a"]


def test_a_retry_of_the_creating_task_can_still_fill_the_entry(configured, sock):
    config, _ = configured
    creator = _task(config, "room-a")
    _create(config, sock, creator)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=creator,
                    vault_credentials=_namespace(config)) as retry:
        assert "value" in ask(retry, sock, {"type": "vault_credential", "name": "generated_acme"}, True)


def test_a_scheduled_creator_is_told_its_own_runs_are_outside_the_grant(configured, sock):
    config, _ = configured
    creator = _task(config, "room-a", source_type="scheduled")
    _create(config, sock, creator)
    assert _use(config, creator) == "credential_not_granted"
    assert _use(config, _task(config, "room-a")) is None
    assert "scheduled runs, including the job that created it, cannot" in _notice(config)
