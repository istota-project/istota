"""Emailed confirmations are bound to an identity, action and browser session."""
from dataclasses import replace
import pytest
from istota import db, user_profiles
from istota.webui import auth
from istota.config import Config

@pytest.fixture
def identity(db_path):
    user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
    auth.upsert_identity(db_path, "alice", "alice@example.com")
    return auth.policy_from_config(Config())

def test_binding_single_use_and_epoch(db_path, identity):
    request_id, code = auth.start_step_up(db_path, identity, "alice", "export", "session-a")
    for user, action, session in (("bob", "export", "session-a"), ("alice", "recovery_reveal", "session-a"), ("alice", "export", "session-b")):
        assert auth.redeem_step_up(db_path, request_id, user, action, session, code) == "dead"
    assert auth.redeem_step_up(db_path, request_id, "alice", "export", "session-a", code) == "ok"
    assert auth.redeem_step_up(db_path, request_id, "alice", "export", "session-a", code) == "dead"
    request_id, code = auth.start_step_up(db_path, identity, "alice", "export", "session-a")
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_identities SET credential_epoch=credential_epoch+1")
    assert auth.redeem_step_up(db_path, request_id, "alice", "export", "session-a", code) == "dead"

def test_expiry_replacement_and_mail_budget(db_path, identity):
    policy = replace(identity, mail_link_max_email=2)
    old, old_code = auth.start_step_up(db_path, policy, "alice", "export", "session-a")
    request_id, code = auth.start_step_up(db_path, policy, "alice", "export", "session-a")
    assert auth.redeem_step_up(db_path, old, "alice", "export", "session-a", old_code) == "dead"
    assert auth.start_step_up(db_path, policy, "alice", "export", "session-a") is None
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_step_ups SET expires_at=datetime('now', '-1 second')")
    assert auth.redeem_step_up(db_path, request_id, "alice", "export", "session-a", code) == "dead"
    assert auth.prune_step_ups(db_path) == 2
    assert auth.start_step_up(db_path, policy, "bob", "export", "session-a") is None

def test_failed_codes_are_capped_and_lock_is_audited_once(db_path, identity):
    from istota.credentials import audit
    policy = replace(identity, mail_link_max_email=100)
    for batch in range(4):
        request_id, code = auth.start_step_up(db_path, policy, "alice", "export", "session-a")
        wrong = "000000" if code != "000000" else "111111"
        for attempt in range(5):
            result = auth.redeem_step_up(db_path, request_id, "alice", "export", "session-a", wrong)
            assert result == ("dead" if attempt == 4 else "bad")
    request_id, code = auth.start_step_up(db_path, policy, "alice", "export", "session-a")
    assert auth.redeem_step_up(db_path, request_id, "alice", "export", "session-a", code) == "dead"
    with db.get_db(db_path) as conn:
        assert [r["action"] for r in audit.recent(conn, "alice")] == ["step_up_locked"]
        assert conn.execute("SELECT count(*) FROM notifications WHERE user_id='alice' AND dedup_key='step-up-locked'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM web_auth_attempts WHERE kind='step_up_code'").fetchone()[0] == 20
