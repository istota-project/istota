"""Consent, reservations and exact-action associations use real SQLite."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from istota import db
from .test_whatsapp_requests import store, task


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return path


def hold(conn, task_id, recipient="bob", actor="alice", **kwargs):
    from istota.message_relays import set_permission
    set_permission(conn, actor_user_id=recipient, asker_user_id=actor)
    return store(conn, task_id, actor_user_id=actor, kind="relay_question",
                 recipient_user_id=recipient, preview="Exact preview",
                 relay_snapshot=dict(origin={"surface": "web", "room": "private"},
                                     audience=[actor], asker_display=actor), **kwargs)


def test_permission_is_directional_and_revocation_closes_only_unanswered(path):
    from istota.message_relays import has_permission, revoke_permission, get_relay
    with db.get_db(path) as conn:
        req = hold(conn, task(conn))
        assert has_permission(conn, actor_user_id="bob", asker_user_id="alice")
        assert not has_permission(conn, actor_user_id="alice", asker_user_id="bob")
        assert get_relay(conn, actor_user_id="eve", relay_id=req["relay_id"]) is None
        assert get_relay(conn, actor_user_id="bob", relay_id=req["relay_id"])["question"] == "hello"
        revoke_permission(conn, actor_user_id="bob", asker_user_id="alice")
        assert get_relay(conn, actor_user_id="alice", relay_id=req["relay_id"])["state"] == "cancelled"
        assert conn.execute("SELECT state FROM whatsapp_skill_requests").fetchone()[0] == "cancelled"
        assert not has_permission(conn, actor_user_id="bob", asker_user_id="alice")


def test_consent_absent_leaves_no_request(path):
    from istota.whatsapp_requests import RequestError
    with db.get_db(path) as conn:
        with pytest.raises(RequestError, match="recipient_unavailable"):
            store(conn, task(conn), kind="relay_question", recipient_user_id="bob",
                  preview="preview", relay_snapshot=dict(origin={}, audience=["alice"], asker_display="alice"))
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 0


def test_pair_and_task_reservations_are_atomic(path):
    from istota.whatsapp_requests import RequestError
    with db.get_db(path) as conn:
        ident = task(conn)
        req = hold(conn, ident)
        assert hold(conn, ident)["id"] == req["id"]
        with pytest.raises(RequestError, match="relay_already_open"):
            hold(conn, task(conn))
        with pytest.raises(RequestError, match="confirmation_pending"):
            hold(conn, ident, recipient="carol", request_key="two")
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 1


def test_scoped_held_preview_association(path):
    from istota.whatsapp_requests import RequestError, associate_confirmation
    with db.get_db(path) as conn:
        ident = task(conn)
        req = hold(conn, ident)
        with pytest.raises(RequestError, match="confirmation_unavailable"):
            associate_confirmation(conn, actor_user_id="alice", task_id=ident, request_id=req["id"], preview_digest=req["preview_digest"])
        conn.execute("UPDATE tasks SET status='pending_confirmation' WHERE id=?", (ident,))
        for actor, digest in (("bob", req["preview_digest"]), ("alice", "forged")):
            with pytest.raises(RequestError, match="confirmation_unavailable"):
                associate_confirmation(conn, actor_user_id=actor, task_id=ident, request_id=req["id"], preview_digest=digest)
        associate_confirmation(conn, actor_user_id="alice", task_id=ident, request_id=req["id"], preview_digest=req["preview_digest"])
        assert conn.execute("SELECT whatsapp_confirmation_request_id FROM tasks WHERE id=?", (ident,)).fetchone()[0] == req["id"]


@pytest.mark.parametrize("side,cap", [("asker", 10), ("recipient", 20)])
def test_open_count_caps(path, side, cap):
    from istota.whatsapp_requests import RequestError
    with db.get_db(path) as conn:
        for n in range(cap):
            actor = "alice" if side == "asker" else f"asker{n}"
            recipient = f"recipient{n}" if side == "asker" else "bob"
            hold(conn, task(conn, actor), recipient=recipient, actor=actor)
        actor = "alice" if side == "asker" else "overflow"
        recipient = "overflow" if side == "asker" else "bob"
        with pytest.raises(RequestError, match="relay_limit"):
            hold(conn, task(conn, actor), recipient=recipient, actor=actor)
        assert conn.execute("SELECT count(*) FROM message_relays").fetchone()[0] == cap


def test_concurrent_pair_has_one_winner(path):
    from istota.message_relays import set_permission
    from istota.whatsapp_requests import RequestError
    with db.get_db(path) as conn:
        ids = [task(conn), task(conn)]
        set_permission(conn, actor_user_id="bob", asker_user_id="alice")
    barrier = Barrier(2)
    def insert(ident):
        with db.get_db(path) as conn:
            barrier.wait()
            try:
                return store(conn, ident, kind="relay_question", recipient_user_id="bob", preview="preview",
                             relay_snapshot=dict(origin={}, audience=["alice"], asker_display="alice"))["id"]
            except RequestError as exc:
                return str(exc)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(insert, ids))
    assert results.count("relay_already_open") == 1


def test_delete_task_cancels_held_and_retains_tombstone(path):
    with db.get_db(path) as conn:
        ident = task(conn)
        hold(conn, ident)
        conn.execute("DELETE FROM tasks WHERE id=?", (ident,))
        row = conn.execute("SELECT * FROM whatsapp_skill_requests").fetchone()
        assert row["origin_task_id"] is None and row["state"] == "cancelled"
        assert conn.execute("SELECT state FROM message_relays").fetchone()[0] == "cancelled"


def test_undelivered_answer_retention_is_bounded(path):
    from istota.whatsapp_requests import cleanup_content
    from istota.message_relays import get_relay
    with db.get_db(path) as conn:
        req = hold(conn, task(conn))
        conn.execute("UPDATE message_relays SET state='answered', answer_text='  exact answer  ', answered_at=datetime('now','-31 days'), content_expires_at=datetime('now','-1 days'), return_state='blocked' WHERE id=?", (req["relay_id"],))
        cleanup_content(conn, limit=5)
        relay = get_relay(conn, actor_user_id="alice", relay_id=req["relay_id"])
        assert relay["question"] is None and relay["answer_text"] is None
        assert relay["return_state"] == "expired"


def test_candidate_limits_raw_text_and_restart(path):
    from istota.message_relays import store_reply_candidate
    with db.get_db(path) as conn:
        req = hold(conn, task(conn))
        args = dict(actor_user_id="bob", provider="baileys", quoted_id="provider-question", text="  yes\n")
        assert store_reply_candidate(conn, inbound_id="ordinary", **args) == "no_inflight"
        conn.execute("UPDATE message_relays SET state='sending' WHERE id=?", (req["relay_id"],))
        for n in range(20):
            assert store_reply_candidate(conn, inbound_id=f"answer-{n}", **args) == "pending"
        assert store_reply_candidate(conn, inbound_id="answer-0", **args) == "pending"
        assert store_reply_candidate(conn, inbound_id="overflow", **args) == "overflow"
        assert store_reply_candidate(conn, inbound_id="foreign", **dict(args, actor_user_id="eve")) == "no_inflight"
    with db.get_db(path) as conn:
        row = conn.execute("SELECT * FROM relay_reply_candidates WHERE inbound_id='answer-0'").fetchone()
        assert row["answer_text"] == "  yes\n"
        assert conn.execute("SELECT unixepoch(expires_at)-unixepoch(received_at) FROM relay_reply_candidates LIMIT 1").fetchone()[0] == 600
        assert conn.execute("SELECT count(*) FROM relay_reply_candidates").fetchone()[0] == 20


def test_answered_relay_survives_revocation_and_releases_pair(path):
    from istota.message_relays import revoke_permission
    with db.get_db(path) as conn:
        req = hold(conn, task(conn))
        conn.execute("UPDATE message_relays SET state='answered',answer_text='answer',return_state='pending' WHERE id=?", (req["relay_id"],))
        conn.execute("UPDATE whatsapp_skill_requests SET state='sent' WHERE id=?", (req["id"],))
        assert revoke_permission(conn, actor_user_id="bob", asker_user_id="alice") == 0
        second = hold(conn, task(conn))
        assert second["relay_id"] != req["relay_id"]
        assert conn.execute("SELECT answer_text FROM message_relays WHERE id=?", (req["relay_id"],)).fetchone()[0] == "answer"


def test_cancel_is_scoped_and_does_not_commit(path):
    from istota.message_relays import cancel_relay, get_relay
    from istota.whatsapp_requests import RequestError
    with db.get_db(path) as conn:
        req = hold(conn, task(conn))
    with db.get_db(path) as conn:
        with pytest.raises(RequestError, match="relay_unavailable"):
            cancel_relay(conn, actor_user_id="eve", relay_id=req["relay_id"])
        assert cancel_relay(conn, actor_user_id="bob", relay_id=req["relay_id"])
        conn.rollback()
        assert get_relay(conn, actor_user_id="bob", relay_id=req["relay_id"])["state"] == "held"
