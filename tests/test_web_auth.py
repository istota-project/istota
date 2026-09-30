"""Credential-store contracts, using the real framework database."""

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import sqlite3
from unittest.mock import patch

import pytest

from istota import db, user_profiles, web_auth as auth


PASSWORD = "a long example passphrase"


@pytest.fixture
def policy():
    return auth.Policy(12, 900, 10, 30, 604800, 3600, 900, 3)


@pytest.fixture
def identity(db_path):
    user_profiles.ensure_profile(db_path, "alice")
    return auth.upsert_identity(db_path, "alice", "Alice@Example.com")


def test_hash_roundtrip_and_parameter_upgrade():
    encoded = auth.hash_password(PASSWORD)
    assert encoded.startswith("scrypt$32768$8$1$")
    assert auth.hash_password(PASSWORD) != encoded
    assert auth.verify_password(PASSWORD, encoded) == (True, False)
    assert auth.verify_password("wrong", encoded) == (False, False)
    salt = b"example-salt-123"
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    old = "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    assert auth.verify_password(PASSWORD, old) == (True, True)


@pytest.mark.parametrize("encoded", ["", "garbage", "scrypt$999999999$8$1$a$b", "scrypt$32768$8$1$!$!"])
def test_malformed_hash_fails_closed(encoded):
    assert auth.verify_password(PASSWORD, encoded) == (False, False)


def test_email_normalization():
    assert auth.normalize_email(" Alice@EXAMPLE.com ") == "alice@example.com"
    assert auth.normalize_email("a+b@example.com") != auth.normalize_email("a@example.com")


@pytest.mark.parametrize("value", ["..", "/", "Alice", "-alice", "", "a" * 33, "a/b", "a\n"])
def test_new_user_id_rejects_unsafe_components(value):
    assert not auth.valid_new_user_id(value)


def test_new_user_id_accepts_safe_components():
    assert auth.valid_new_user_id("alice.1_test-user")


@pytest.mark.parametrize("password", ["short", "alice@example.com", "alice", "x" * 2000, "界" * 400])
def test_password_policy_rejects(password, policy):
    assert auth.password_policy_error(password, policy, email="alice@example.com", user_id="alice")


def test_password_policy_accepts_and_clamps(policy):
    assert auth.password_policy_error(PASSWORD, policy, email="alice@example.com", user_id="alice") is None
    assert auth.password_policy_error("short", replace(policy, min_password_length=1), email="a@x", user_id="a")
    with pytest.raises(ValueError):
        auth.password_policy_error(PASSWORD, replace(policy, min_password_length=129), email="a@x", user_id="a")


def test_identity_normalized_unique_and_idempotent(db_path, identity):
    assert auth.get_identity_by_email(db_path, " ALICE@example.com ") == identity
    assert auth.upsert_identity(db_path, "alice", identity.email) == identity
    assert auth.list_identities(db_path) == [identity]
    user_profiles.ensure_profile(db_path, "bob")
    with pytest.raises(sqlite3.IntegrityError):
        auth.upsert_identity(db_path, "bob", identity.email)
    with pytest.raises(ValueError):
        auth.upsert_identity(db_path, "ghost", "ghost@example.com")


def test_epochs_and_missing_identity(db_path, identity):
    epochs = [identity.credential_epoch]
    epochs.append(auth.set_password(db_path, "alice", PASSWORD))
    epochs.append(auth.set_disabled(db_path, "alice", True))
    epochs.append(auth.clear_password(db_path, "alice"))
    epochs.append(auth.bump_epoch(db_path, "alice"))
    assert all(a < b for a, b in zip(epochs, epochs[1:]))
    for operation in (auth.bump_epoch, lambda path, uid: auth.set_disabled(path, uid, True)):
        with pytest.raises(ValueError):
            operation(db_path, "nextcloud-only")


def test_identity_recreation_does_not_reuse_epoch(db_path, identity):
    auth.delete_identity(db_path, "alice")
    new = auth.upsert_identity(db_path, "alice", identity.email)
    assert new.credential_epoch != identity.credential_epoch


@pytest.mark.parametrize("case", ["unknown", "passwordless", "disabled", "wrong", "correct"])
def test_authentication_always_verifies_admitted_attempt(db_path, policy, identity, case):
    if case not in ("unknown", "passwordless"):
        auth.set_password(db_path, "alice", PASSWORD)
    if case == "disabled":
        auth.set_disabled(db_path, "alice", True)
    email = "unknown@example.com" if case == "unknown" else identity.email
    with patch.object(auth, "verify_password", wraps=auth.verify_password) as verify:
        status, result = auth.authenticate(db_path, policy, email, "wrong" if case == "wrong" else PASSWORD, ip=None)
    verify.assert_called_once()
    if case == "correct":
        assert status == "ok" and result.user_id == "alice"
        assert auth.get_identity(db_path, "alice").last_login_at
    else:
        assert (status, result) == ("bad", None)


def test_budget_counts_success_and_refusal_does_not_extend_window(db_path, policy, identity):
    auth.set_password(db_path, "alice", PASSWORD)
    policy = replace(policy, throttle_max_email=1)
    assert auth.authenticate(db_path, policy, identity.email, PASSWORD, ip=None)[0] == "ok"
    with patch.object(auth, "verify_password") as verify:
        assert auth.authenticate(db_path, policy, identity.email, PASSWORD, ip=None) == ("throttled", None)
    verify.assert_not_called()
    assert auth.attempts_in_window(db_path, "email", identity.email, 900) == 1
    with db.get_db(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_auth_attempts WHERE kind='ip'").fetchone()[0] == 0
        conn.execute("UPDATE web_auth_attempts SET at = datetime('now', '-901 seconds')")
    assert auth.check_and_record(db_path, policy, email=identity.email, ip=None)


def test_budget_serializes_concurrent_reservations(db_path, policy):
    policy = replace(policy, throttle_max_email=3)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: auth.check_and_record(db_path, policy, email="alice@example.com", ip=None), range(16)))
    assert sum(results) == 3
    assert auth.attempts_in_window(db_path, "email", "alice@example.com", 900) == 3


def test_ip_budget_and_pruning(db_path, policy):
    policy = replace(policy, throttle_max_ip=1)
    assert auth.check_and_record(db_path, policy, email="a@example.com", ip="192.0.2.1")
    assert not auth.check_and_record(db_path, policy, email="b@example.com", ip="192.0.2.1")
    assert auth.attempts_in_window(db_path, "email", "b@example.com", 900) == 0
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_attempts SET at=datetime('now', '-2 days')")
    assert auth.prune_attempts(db_path, 3600) == 2


def test_reads_propagate_database_failure_and_authenticate_fails_closed(db_path, policy, caplog):
    with db.get_db(db_path) as conn:
        conn.execute("DROP TABLE web_auth_identities")
    with pytest.raises(sqlite3.Error):
        auth.get_identity(db_path, "alice")
    assert auth.authenticate(db_path, policy, "alice@example.com", PASSWORD, ip=None) == ("bad", None)
    assert "authentication failed" in caplog.text.lower()
    assert PASSWORD not in caplog.text and "alice@example.com" not in caplog.text


@pytest.mark.parametrize("change", ["password", "epoch", "disabled", "removed", "profile"])
def test_authentication_rechecks_state_after_verification(db_path, policy, identity, change):
    auth.set_password(db_path, "alice", PASSWORD)
    original = auth.verify_password

    def verify(password, encoded):
        result = original(password, encoded)
        if change == "password":
            auth.set_password(db_path, "alice", "another example passphrase")
        elif change == "epoch":
            auth.bump_epoch(db_path, "alice")
        elif change == "disabled":
            auth.set_disabled(db_path, "alice", True)
        elif change == "removed":
            auth.delete_identity(db_path, "alice")
        else:
            user_profiles.delete_profile(db_path, "alice")
        return result

    with patch.object(auth, "verify_password", side_effect=verify):
        assert auth.authenticate(db_path, policy, identity.email, PASSWORD, ip=None) == ("bad", None)


def test_tokens_peek_replacement_and_delete(db_path, identity):
    tokens = {purpose: auth.issue_token(db_path, "alice", purpose, 3600) for purpose in ("enrol", "reset", "login")}
    assert auth.peek_token(db_path, tokens["reset"]).purpose == "reset"
    assert auth.peek_token(db_path, tokens["reset"], "login") is None
    replacement = auth.issue_token(db_path, "alice", "reset", 3600)
    assert auth.peek_token(db_path, tokens["reset"]) is None
    assert auth.peek_token(db_path, tokens["login"]) is not None
    with db.get_db(db_path) as conn:
        stored = conn.execute("SELECT token_hash FROM web_auth_tokens").fetchall()
    assert all(row[0] not in tokens.values() and row[0] != replacement for row in stored)
    assert auth.delete_identity(db_path, "alice")
    auth.upsert_identity(db_path, "alice", identity.email)
    assert all(auth.peek_token(db_path, token) is None for token in [*tokens.values(), replacement])


@pytest.mark.parametrize("purpose", ["enrol", "reset", "login"])
@pytest.mark.parametrize("invalid", ["expired", "disabled", "email", "orphan", "recreated"])
def test_invalid_tokens_do_not_authenticate_or_change_password(db_path, identity, policy, purpose, invalid):
    token = auth.issue_token(db_path, "alice", purpose, 3600)
    if invalid == "expired":
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE web_auth_tokens SET expires_at=datetime('now', '-1 second')")
    elif invalid == "disabled":
        auth.set_disabled(db_path, "alice", True)
    elif invalid == "email":
        auth.upsert_identity(db_path, "alice", "new@example.com")
    elif invalid == "orphan":
        user_profiles.delete_profile(db_path, "alice")
    else:
        auth.delete_identity(db_path, "alice")
        auth.upsert_identity(db_path, "alice", identity.email)
    if purpose == "login":
        result = auth.consume_login_token(db_path, token)
    else:
        result = auth.consume_and_set_password(db_path, token, purpose, PASSWORD, policy)
    assert result is None
    assert auth.get_identity(db_path, "alice").password_hash == ""


def test_atomic_password_token_and_rollback(db_path, identity, policy):
    token = auth.issue_token(db_path, "alice", "enrol", 3600)
    other = auth.issue_token(db_path, "alice", "login", 3600)
    assert auth.peek_token(db_path, token) is not None
    with db.get_db(db_path) as conn:
        conn.execute("CREATE TRIGGER reject_password BEFORE UPDATE OF password_hash ON web_auth_identities BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.Error):
        auth.consume_and_set_password(db_path, token, "enrol", PASSWORD, policy)
    assert auth.peek_token(db_path, token) is not None
    assert auth.peek_token(db_path, other) is not None
    with db.get_db(db_path) as conn:
        conn.execute("DROP TRIGGER reject_password")
    result = auth.consume_and_set_password(db_path, token, "enrol", PASSWORD, policy)
    assert result[:2] == ("alice", identity.email)
    assert result[2] > identity.credential_epoch
    assert auth.consume_and_set_password(db_path, token, "enrol", PASSWORD, policy) is None
    assert auth.peek_token(db_path, other) is None
    assert auth.verify_password(PASSWORD, auth.get_identity(db_path, "alice").password_hash)[0]


def test_login_token_is_atomic_single_use_without_epoch_change(db_path, identity):
    token = auth.issue_token(db_path, "alice", "login", 3600)
    reset = auth.issue_token(db_path, "alice", "reset", 3600)
    assert auth.peek_token(db_path, token) is not None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth.consume_login_token(db_path, token), range(4)))
    assert results.count(("alice", identity.email, identity.credential_epoch)) == 1
    assert results.count(None) == 3
    assert auth.peek_token(db_path, reset) is not None
    assert auth.get_identity(db_path, "alice").last_login_at


def test_password_token_concurrent_single_use(db_path, identity, policy):
    token = auth.issue_token(db_path, "alice", "reset", 3600)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth.consume_and_set_password(db_path, token, "reset", PASSWORD, policy), range(2)))
    assert sum(result is not None for result in results) == 1


def test_shared_mail_budget_preserves_last_link_when_refused(db_path, identity, policy):
    assert auth.issue_mail_link_if_allowed(db_path, policy, "unknown@example.com", "login") is None
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "reset")
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "reset")
    token, found = auth.issue_mail_link_if_allowed(db_path, policy, " ALICE@example.com ", "login")
    assert found == identity
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "login") is None
    assert auth.peek_token(db_path, token, "login") is not None
    assert auth.attempts_in_window(db_path, "mail_link", identity.email, 3600) == 3
    assert auth.attempts_in_window(db_path, "mail_link", "unknown@example.com", 3600) == 0


def test_shared_mail_budget_concurrency(db_path, identity, policy):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "login" if n % 2 else "reset"), range(12)))
    assert sum(result is not None for result in results) == 3


def test_authentication_rehashes_without_changing_epoch(db_path, identity, policy):
    salt = b"example-salt-123"
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    old = "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_identities SET password_hash = ?", (old,))
    status, result = auth.authenticate(db_path, policy, identity.email, PASSWORD, ip=None)
    assert status == "ok"
    assert result.credential_epoch == identity.credential_epoch
    assert result.password_hash.startswith("scrypt$32768$")
    assert auth.verify_password(PASSWORD, result.password_hash) == (True, False)


@pytest.mark.parametrize("token", ["", "unknown", "x" * 257, "\ud800"])
def test_malformed_token_is_absent(db_path, token):
    assert auth.peek_token(db_path, token) is None
    assert auth.consume_login_token(db_path, token) is None


def test_password_token_rechecks_epoch_after_hashing(db_path, identity, policy):
    token = auth.issue_token(db_path, "alice", "reset", 3600)
    original = auth.hash_password

    def hash_and_revoke(password):
        encoded = original(password)
        auth.bump_epoch(db_path, "alice")
        return encoded

    with patch.object(auth, "hash_password", side_effect=hash_and_revoke):
        assert auth.consume_and_set_password(db_path, token, "reset", PASSWORD, policy) is None
    assert auth.get_identity(db_path, "alice").password_hash == ""
    assert auth.peek_token(db_path, token) is not None


def test_login_token_rolls_back_if_login_stamp_fails(db_path, identity):
    token = auth.issue_token(db_path, "alice", "login", 3600)
    with db.get_db(db_path) as conn:
        conn.execute("CREATE TRIGGER reject_login BEFORE UPDATE OF last_login_at ON web_auth_identities BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.Error):
        auth.consume_login_token(db_path, token)
    assert auth.peek_token(db_path, token) is not None


def test_mail_budget_ignores_disabled_orphan_and_expired_reservations(db_path, identity, policy):
    auth.set_disabled(db_path, "alice", True)
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "login") is None
    auth.set_disabled(db_path, "alice", False)
    for _ in range(3):
        assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "login")
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_attempts SET at=datetime('now', '-3601 seconds')")
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "reset")
    user_profiles.delete_profile(db_path, "alice")
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "login") is None


def test_schema_upgrade_is_additive_and_idempotent(db_path, identity):
    with db.get_db(db_path) as conn:
        conn.execute("DROP TABLE web_auth_attempts")
        conn.execute("DROP TABLE web_auth_tokens")
    db.init_db(db_path)
    db.init_db(db_path)
    assert auth.get_identity(db_path, "alice") == identity
    assert auth.peek_token(db_path, auth.issue_token(db_path, "alice", "login", 900))


def test_authentication_error_logs_no_exception_payload(db_path, policy, caplog):
    with patch.object(auth, "check_and_record", side_effect=RuntimeError(PASSWORD)):
        assert auth.authenticate(db_path, policy, "alice@example.com", PASSWORD, ip=None) == ("bad", None)
    assert "RuntimeError" in caplog.text
    assert PASSWORD not in caplog.text
