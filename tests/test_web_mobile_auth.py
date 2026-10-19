"""Proof-bound handoffs use the real DB and the ordinary credential authority."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import secrets
import sqlite3
from threading import Barrier

import pytest

from istota import db, user_profiles
from istota.config import Config, UserConfig, WebConfig
from istota.webui import auth, mobile_auth as mobile


@pytest.fixture
def config(db_path):
    user_profiles.ensure_profile(db_path, "alice")
    return Config(db_path=db_path, users={"alice": UserConfig()}, web=WebConfig(auth=["email", "nextcloud", "local_code"]))


@pytest.fixture
def identity(db_path):
    user_profiles.ensure_profile(db_path, "alice")
    return auth.upsert_identity(db_path, "alice", "alice@example.com")


def attempt(db_path, source="192.0.2.1"):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    request = mobile.create_request(db_path, challenge=challenge, state=state, source=source)
    return request, verifier, state


def approved(config, identity, source="192.0.2.1"):
    request, verifier, state = attempt(config.db_path, source)
    approval = mobile.approve_request(config, request.request_id, user_id="alice", method="email", epoch=identity.credential_epoch)
    return request, verifier, approval


def test_email_handoff_preserves_state_and_never_stores_proofs(config, identity):
    request, verifier, approval = approved(config, identity)
    assert approval.state == request.state
    assert mobile.get_request(config.db_path, request.request_id) == request
    with db.get_db(config.db_path) as conn:
        row = dict(conn.execute("SELECT * FROM web_mobile_auth_requests").fetchone())
        assert approval.code not in row.values() and verifier not in row.values()
        assert row["code_digest"] == hashlib.sha256(approval.code.encode()).hexdigest()
    result = mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)
    assert (result.user_id, result.method, result.epoch) == ("alice", "email", identity.credential_epoch)
    with pytest.raises(mobile.HandoffError, match="replay"):
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)


@pytest.mark.parametrize("field", ["code", "verifier", "request_id"])
def test_bad_proof_does_not_consume(config, identity, field):
    request, verifier, approval = approved(config, identity)
    proof = dict(request_id=request.request_id, code=approval.code, verifier=verifier)
    proof[field] = secrets.token_urlsafe(32)
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.redeem_request(config, **proof)
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).user_id == "alice"


@pytest.mark.parametrize("field", ["challenge", "state"])
@pytest.mark.parametrize("bad", [None, 3, "", "x" * 42, "!" * 43, "x" * 44, "é" * 43])
def test_create_refuses_malformed_proof(db_path, field, bad):
    values = dict(challenge=secrets.token_urlsafe(32), state=secrets.token_urlsafe(32), source="192.0.2.1")
    values[field] = bad
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.create_request(db_path, **values)


def test_concurrent_redemption_and_lost_response_are_once_only(config, identity):
    request, verifier, approval = approved(config, identity)
    barrier = Barrier(2)
    def redeem():
        barrier.wait()
        try:
            mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)
            return "ok"
        except mobile.HandoffError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: redeem(), range(2)))
    assert sorted(results) == ["ok", "replay"]
    # A caller losing the successful response cannot reuse its proof on retry.
    with pytest.raises(mobile.HandoffError, match="replay"):
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)


def test_failed_consume_rolls_back(config, identity):
    request, verifier, approval = approved(config, identity)
    with db.get_db(config.db_path) as conn:
        conn.execute("CREATE TRIGGER refuse_consume BEFORE UPDATE OF consumed_at ON web_mobile_auth_requests BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT consumed_at FROM web_mobile_auth_requests").fetchone()[0] is None
        conn.execute("DROP TRIGGER refuse_consume")
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).user_id == "alice"


@pytest.mark.parametrize("change", ["epoch", "disabled", "deleted", "profile", "method"])
def test_live_authority_rechecked_at_redemption(config, identity, change):
    request, verifier, approval = approved(config, identity)
    if change == "epoch":
        auth.bump_epoch(config.db_path, "alice")
    elif change == "disabled":
        auth.set_disabled(config.db_path, "alice", True)
    elif change == "deleted":
        auth.delete_identity(config.db_path, "alice")
    elif change == "profile":
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM user_profiles WHERE user_id='alice'")
    else:
        config.web.auth = ["nextcloud"]
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)


def test_approval_revalidates_browser_epoch(config, identity):
    request, _, _ = attempt(config.db_path)
    auth.bump_epoch(config.db_path, "alice")
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.approve_request(config, request.request_id, user_id="alice", method="email", epoch=identity.credential_epoch)


def test_nextcloud_needs_no_email_identity_but_honors_retired_epoch(config):
    request, verifier, _ = attempt(config.db_path)
    approval = mobile.approve_request(config, request.request_id, user_id="alice", method="nextcloud", epoch=0)
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).method == "nextcloud"
    request, verifier, _ = attempt(config.db_path)
    approval = mobile.approve_request(config, request.request_id, user_id="alice", method="nextcloud", epoch=0)
    auth.upsert_identity(config.db_path, "alice", "alice@example.com")
    auth.delete_identity(config.db_path, "alice")
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)


def test_none_requires_explicit_approval_for_configured_user(config):
    config.web.auth = ["none"]
    request, verifier, _ = attempt(config.db_path)
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.redeem_request(config, request.request_id, code=secrets.token_urlsafe(32), verifier=verifier)
    with pytest.raises(mobile.HandoffError, match="invalid"):
        mobile.approve_request(config, request.request_id, user_id="bob", method="none", epoch=0)
    approval = mobile.approve_request(config, request.request_id, user_id="alice", method="none", epoch=0)
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).method == "none"


@pytest.mark.parametrize("column", ["expires_at", "code_expires_at"])
def test_expired_proof_never_redeems(config, identity, column):
    request, verifier, approval = approved(config, identity)
    with db.get_db(config.db_path) as conn:
        conn.execute(f"UPDATE web_mobile_auth_requests SET {column}=datetime('now','-1 second')")
    with pytest.raises(mobile.HandoffError, match="expired"):
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)


def test_pending_and_creation_limits_reuse_durable_attempts(config, identity):
    for _ in range(5):
        attempt(config.db_path)
    with pytest.raises(mobile.HandoffError, match="throttled"):
        attempt(config.db_path)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE web_mobile_auth_requests SET expires_at=datetime('now','-1 second')")
    for _ in range(5):
        request, verifier, approval = approved(config, identity)
        mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier)
    with pytest.raises(mobile.HandoffError, match="throttled"):
        attempt(config.db_path)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_auth_attempts WHERE kind='mobile_create'").fetchone()[0] == 10
        conn.execute("UPDATE web_auth_attempts SET at=datetime('now','-601 seconds')")
    assert attempt(config.db_path)[0].request_id


def test_global_cap_preserves_approved_attempt(config, identity):
    request, verifier, approval = approved(config, identity)
    with db.get_db(config.db_path) as conn:
        conn.executemany(
            "INSERT INTO web_mobile_auth_requests(request_id,challenge,state,source,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            [(str(i), "challenge", "state", "other", auth._timestamp(), auth._timestamp(600)) for i in range(9999)],
        )
    with pytest.raises(mobile.HandoffError, match="throttled"):
        attempt(config.db_path, "192.0.2.3")
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).user_id == "alice"


def test_expiry_pruning_is_bounded_and_runs_from_maintenance(config):
    for _ in range(3):
        attempt(config.db_path)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE web_mobile_auth_requests SET expires_at=datetime('now','-1 second')")
    assert mobile.prune_requests(config.db_path, limit=1) == 1
    from istota.scheduler import build_interval_gates
    gate = next(g for g in build_interval_gates(config) if g.name == "credential-maintenance")
    gate.run(0)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_mobile_auth_requests").fetchone()[0] == 0


def test_approval_cannot_reissue_or_change_identity(config, identity):
    request, verifier, approval = approved(config, identity)
    with pytest.raises(mobile.HandoffError, match="replay"):
        mobile.approve_request(config, request.request_id, user_id="alice", method="email", epoch=identity.credential_epoch)
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).user_id == "alice"


def test_approval_cannot_extend_attempt_lifetime(config, identity):
    request, _, _ = attempt(config.db_path)
    expiry = auth._timestamp(5)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE web_mobile_auth_requests SET expires_at=?", (expiry,))
    mobile.approve_request(config, request.request_id, user_id="alice", method="email", epoch=identity.credential_epoch)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT code_expires_at FROM web_mobile_auth_requests").fetchone()[0] == expiry
        conn.execute("UPDATE web_mobile_auth_requests SET expires_at=?", (auth._timestamp(),))
    with pytest.raises(mobile.HandoffError, match="expired"):
        mobile.get_request(config.db_path, request.request_id)


def test_concurrent_creation_cannot_exceed_pending_cap(config):
    for _ in range(4):
        attempt(config.db_path)
    barrier = Barrier(2)
    def create():
        barrier.wait()
        try:
            attempt(config.db_path)
            return "ok"
        except mobile.HandoffError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sorted(results) == ["ok", "throttled"]


def test_shared_validation_reads_callers_transaction(config, identity):
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE web_auth_identities SET disabled=1 WHERE user_id='alice'")
        with pytest.raises(ValueError, match="disabled"):
            auth.validate_session_credential(config.db_path, "alice", "email", identity.credential_epoch, config.web, conn=conn)
        conn.rollback()
    assert auth.validate_session_credential(config.db_path, "alice", "email", identity.credential_epoch, config.web) == identity


def test_request_path_prunes_without_evicting_live_approval(config, identity):
    request, verifier, approval = approved(config, identity)
    with db.get_db(config.db_path) as conn:
        conn.executemany(
            "INSERT INTO web_mobile_auth_requests(request_id,challenge,state,source,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            [(str(i), "challenge", "state", "other", auth._timestamp(-700), auth._timestamp(-1)) for i in range(101)],
        )
    attempt(config.db_path)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_mobile_auth_requests WHERE expires_at<=?", (auth._timestamp(),)).fetchone()[0] == 1
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).user_id == "alice"


def test_local_code_handoff_uses_email_authority(config, identity):
    request, verifier, _ = attempt(config.db_path)
    approval = mobile.approve_request(config, request.request_id, user_id="alice", method="local_code", epoch=identity.credential_epoch)
    assert mobile.redeem_request(config, request.request_id, code=approval.code, verifier=verifier).method == "local_code"
