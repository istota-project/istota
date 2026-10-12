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


# A password step-up: the confirmation on an install that cannot mail a code.
PASSWORD = "a long example passphrase"


@pytest.fixture
def with_password(db_path, identity, monkeypatch):
    # The shipped scrypt cost is ~0.1s a hash; nothing here is about the cost.
    monkeypatch.setattr(auth, "_N", 1024)
    monkeypatch.setattr(auth, "DUMMY_HASH", auth.hash_password("unused"))
    auth.set_password(db_path, "alice", PASSWORD)
    return replace(identity, throttle_max_email=100, throttle_max_ip=100)


def test_password_request_spends_no_mail_budget_and_needs_a_password(db_path, with_password):
    request_id, _ = auth.start_password_step_up(db_path, with_password, "alice", "export", "session-a")
    assert request_id
    assert auth.attempts_in_window(db_path, "mail_link", "alice@example.com", 3600) == 0
    with db.get_db(db_path) as conn:
        assert conn.execute("SELECT method FROM web_auth_step_ups WHERE request_id=?", (request_id,)).fetchone()[0] == "password"
    auth.clear_password(db_path, "alice")
    assert auth.start_password_step_up(db_path, with_password, "alice", "export", "session-a") is None
    assert auth.start_password_step_up(db_path, with_password, "bob", "export", "session-a") is None


def test_password_redeems_once_bound_to_session_action_and_epoch(db_path, with_password):
    policy = with_password
    request_id, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    for user, action, session in (("bob", "export", "session-a"), ("alice", "recovery_reveal", "session-a"),
                                  ("alice", "export", "session-b")):
        assert auth.redeem_password_step_up(db_path, policy, request_id, user, action, session, PASSWORD, ip=None) == "dead"
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a", PASSWORD, ip=None) == "ok"
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a", PASSWORD, ip=None) == "dead"
    request_id, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    auth.bump_epoch(db_path, "alice")
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a", PASSWORD, ip=None) == "dead"


def test_an_expired_password_request_is_refused(db_path, with_password):
    request_id, _ = auth.start_password_step_up(db_path, with_password, "alice", "export", "session-a")
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_step_ups SET expires_at=datetime('now', '-1 second')")
    assert auth.redeem_password_step_up(db_path, with_password, request_id, "alice", "export", "session-a",
                                        PASSWORD, ip=None) == "dead"


def test_a_code_and_a_password_only_redeem_their_own_method(db_path, with_password):
    policy = with_password
    password_request, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_step_ups SET code_hash=? WHERE request_id=?",
                     (auth._code_digest(password_request, "123456"), password_request))
    assert auth.redeem_step_up(db_path, password_request, "alice", "export", "session-a", "123456") == "dead"
    request_id, code = auth.start_step_up(db_path, policy, "alice", "history_purge", "session-a")
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "history_purge", "session-a",
                                        PASSWORD, ip=None) == "dead"
    assert auth.redeem_step_up(db_path, request_id, "alice", "history_purge", "session-a", code) == "ok"


def test_wrong_passwords_spend_the_request_and_the_login_budget(db_path, with_password):
    policy = replace(with_password, throttle_max_email=3)
    request_id, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                        "not it", ip="192.0.2.1") == "bad"
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                        "not it either", ip="192.0.2.1") == "bad"
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                        "still not it", ip="192.0.2.1") == "bad"
    # The login form's own budget is spent: the right password is refused too.
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                        PASSWORD, ip="192.0.2.1") == "throttled"
    assert auth.authenticate(db_path, policy, "alice@example.com", PASSWORD, ip="192.0.2.1") == ("throttled", None)
    with db.get_db(db_path) as conn:
        assert conn.execute("SELECT attempts FROM web_auth_step_ups WHERE request_id=?", (request_id,)).fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM web_auth_attempts WHERE kind='step_up_code'").fetchone()[0] == 3


def test_five_wrong_passwords_kill_the_request(db_path, with_password):
    policy = with_password
    request_id, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    results = [auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                            f"wrong {n}", ip=None) for n in range(5)]
    assert results == ["bad", "bad", "bad", "bad", "dead"]
    assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                        PASSWORD, ip=None) == "dead"


def test_correct_passwords_cost_no_sign_in_budget(db_path, with_password):
    policy = replace(with_password, throttle_max_email=3, throttle_max_ip=3)
    for _ in range(12):
        request_id, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
        assert auth.redeem_password_step_up(db_path, policy, request_id, "alice", "export", "session-a",
                                            PASSWORD, ip="192.0.2.1") == "ok"
    assert auth.authenticate(db_path, policy, "alice@example.com", PASSWORD, ip="192.0.2.1")[0] == "ok"


def test_a_live_password_request_is_reused_until_used_expired_or_stale(db_path, with_password):
    policy = with_password
    first = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    assert auth.start_password_step_up(db_path, policy, "alice", "export", "session-a") == first
    other_action, _ = auth.start_password_step_up(db_path, policy, "alice", "history_purge", "session-a")
    assert other_action != first[0]
    with db.get_db(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_auth_step_ups").fetchone()[0] == 2
    assert auth.redeem_password_step_up(db_path, policy, first[0], "alice", "export", "session-a",
                                        PASSWORD, ip=None) == "ok"
    second, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    assert second != first[0]
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_step_ups SET expires_at=datetime('now', '-1 second') WHERE request_id=?",
                     (second,))
    third, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    assert third != second
    auth.bump_epoch(db_path, "alice")
    fourth, _ = auth.start_password_step_up(db_path, policy, "alice", "export", "session-a")
    assert fourth != third
    assert auth.redeem_password_step_up(db_path, policy, fourth, "alice", "export", "session-a",
                                        PASSWORD, ip=None) == "ok"
