"""Purchase holds through the scheduler, the bell and digest-bound approval."""

from datetime import datetime
from unittest.mock import patch

import pytest

from istota import confirmations, db
from istota.config import UserConfig
from istota.notifications import store
from istota.notifications.resolvers import confirmation
from istota.relay.requests import RequestError, text_hash
from istota.wallet import purchases
from .support.wallet import request, wallet_fixture  # noqa: F401 -- shared fixture


def park(env, *, shared=False):
    path, config, _, task_id = env
    config.db_path = path
    config.users = {"alice": UserConfig()}
    with db.get_db(path) as conn:
        room = db.create_web_chat_room(conn, "alice", "general").token
        if shared:
            db.add_room_member(conn, room, "bob")
        conn.execute("UPDATE tasks SET status='pending', source_type='web', conversation_token=?, output_target='room' WHERE id=?", (room, task_id))

    def execute(*args, **kwargs):
        with db.get_db(path) as conn:
            result = request(conn, env, amount_cents=6000, request_key="filter")
            assert result.status == "held"
        return True, "Waiting for purchase approval.", None, None

    with patch("istota.scheduler.execute_task", side_effect=execute), patch("istota.notifications.delivery.send_notification", return_value=True):
        from istota.scheduler import process_one_task
        process_one_task(config)
    with db.get_db(path) as conn:
        task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        assert task.whatsapp_confirmation_request_id
        purchase = purchases.list_purchases(conn, "alice")[0]
        assert purchase["request_id"] == task.whatsapp_confirmation_request_id
        assert f"Purchase #{purchase['id']}." in task.confirmation_prompt
        return task, purchase


@pytest.mark.parametrize("shared", [False, True])
def test_scheduler_park_and_digest_approval(wallet_env, shared):
    wallet_env[1].security.wallet_authorization_minutes = 45
    wallet_env[1].security.wallet_fills_per_purchase = 2
    task, purchase = park(wallet_env, shared=shared)
    path, config, _, _ = wallet_env
    with db.get_db(path) as conn:
        assert "up to 2 times within 45 minutes" in task.confirmation_prompt
        assert confirmations.describe_title(conn, task) == confirmation.PURCHASE_TITLE
        views, _ = store.list_open(config, conn, "alice")
        assert views[0].title == confirmation.PURCHASE_TITLE
        assert "For this purchase." in views[0].body
        from istota.rooms.private_replies import preview_rooms
        preview = preview_rooms(conn, task)
        if shared:
            assert task.conversation_token not in preview
            shared_messages = conn.execute("SELECT body FROM messages WHERE room_token=?", (task.conversation_token,)).fetchall()
            assert all("60.00" not in row["body"] and "shop.example" not in row["body"] for row in shared_messages)
        else:
            assert task.conversation_token in preview
        with pytest.raises(RequestError, match="confirmation_unavailable"):
            confirmations.approve(conn, task, config=config, preview_digest="wrong")
        assert purchases.get_purchase(conn, "alice", purchase["id"])["state"] == "held"
        digest = text_hash(task.confirmation_prompt)
        confirmations.approve(conn, task, config=config, preview_digest=digest)
        updated = purchases.get_purchase(conn, "alice", purchase["id"])
        assert updated["state"] == "authorized" and updated["approval"] == "user"
        assert updated["approved_digest"] == digest
        assert (datetime.fromisoformat(updated["expires_at"]) - datetime.fromisoformat(updated["authorized_at"])).seconds == 45 * 60
        rerun = db.get_task(conn, task.id)
        assert rerun.status == "pending"
        assert rerun.confirmation_prompt == task.confirmation_prompt
        assert conn.execute("SELECT state FROM whatsapp_skill_requests").fetchone()[0] == "approved"
        assert store.list_open(config, conn, "alice") == ([], 0)


@pytest.mark.parametrize("action,state", [("decline", "declined"), ("expire", "expired"), ("cancel", "cancelled")])
def test_hold_closes_with_task(wallet_env, action, state):
    task, purchase = park(wallet_env)
    with db.get_db(wallet_env[0]) as conn:
        if action == "decline":
            confirmations.decline(conn, task)
        elif action == "expire":
            conn.execute("UPDATE tasks SET updated_at=datetime('now','-3 hours') WHERE id=?", (task.id,))
            db.expire_stale_confirmations(conn, 120)
        else:
            db.cancel_task(conn, task.id)
        assert purchases.get_purchase(conn, "alice", purchase["id"])["state"] == state
        assert db.get_task(conn, task.id).status == "cancelled"


def test_scheduler_expires_authorizations(wallet_env):
    path, config, _, _ = wallet_env
    config.db_path = path
    with db.get_db(path) as conn:
        result = request(conn, wallet_env)
        conn.execute("UPDATE wallet_purchases SET expires_at='2000-01-01 00:00:00'")
    from istota.scheduler import run_cleanup_checks
    run_cleanup_checks(config)
    with db.get_db(path) as conn:
        assert purchases.get_purchase(conn, "alice", result.purchase_id)["state"] == "expired"


def test_second_hold_rolls_back_purchase_and_does_not_replace_preview(wallet_env):
    with db.get_db(wallet_env[0]) as conn:
        first = request(conn, wallet_env, amount_cents=6000, request_key="first")
        with pytest.raises(RequestError, match="confirmation_pending"):
            request(conn, wallet_env, amount_cents=7000, request_key="second")
        assert [p["id"] for p in purchases.list_purchases(conn, "alice")] == [first.purchase_id]
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 1


def test_cleanup_continues_when_wallet_expiry_fails(wallet_env):
    config = wallet_env[1]
    config.db_path = wallet_env[0]
    from istota.scheduler import run_cleanup_checks
    with patch("istota.wallet.purchases.expire", side_effect=RuntimeError("expiry unavailable")):
        run_cleanup_checks(config)


@pytest.mark.parametrize("surface", ["sms", "whatsapp"])
def test_phone_purchase_refuses_a_truncated_preview(wallet_env, surface):
    path, config, _, task_id = wallet_env
    config.sms.max_segments = 1
    with db.get_db(path) as conn:
        conn.execute("UPDATE tasks SET source_type=? WHERE id=?", (surface, task_id))
        with pytest.raises(RequestError, match="invalid_preview"):
            request(conn, wallet_env, amount_cents=6000,
                    extra_hosts=[f"frame{i}.example" for i in range(30)], description="buy this filter " * 25)
        assert purchases.list_purchases(conn, "alice") == []
        assert conn.execute("SELECT count(*) FROM whatsapp_skill_requests").fetchone()[0] == 0


def test_approval_cannot_expand_the_preview_limits(wallet_env):
    config = wallet_env[1]
    config.security.wallet_authorization_minutes = 5
    config.security.wallet_fills_per_purchase = 1
    task, purchase = park(wallet_env)
    config.security.wallet_authorization_minutes = 240
    config.security.wallet_fills_per_purchase = 3
    with db.get_db(wallet_env[0]) as conn:
        confirmations.approve(conn, task, config=config, preview_digest=text_hash(task.confirmation_prompt))
        updated = purchases.get_purchase(conn, "alice", purchase["id"])
        assert (datetime.fromisoformat(updated["expires_at"]) - datetime.fromisoformat(updated["authorized_at"])).seconds == 5 * 60
        purchases.claim_fill(conn, config, user_id="alice", task_id=task.id, purchase_id=purchase["id"])
        with pytest.raises(purchases.WalletRefusal, match="purchase_fill_limit"):
            purchases.claim_fill(conn, config, user_id="alice", task_id=task.id, purchase_id=purchase["id"])


def test_declined_turn_closes_its_purchase(wallet_env):
    path, config, _, task_id = wallet_env
    config.db_path = path
    config.users = {"alice": UserConfig()}
    with db.get_db(path) as conn:
        room = db.create_web_chat_room(conn, "alice", "shared").token
        db.add_room_member(conn, room, "bob")
        conn.execute("UPDATE tasks SET status='pending', source_type='web', declinable=1, conversation_token=? WHERE id=?", (room, task_id))

    def execute(*args, **kwargs):
        with db.get_db(path) as conn:
            request(conn, wallet_env, amount_cents=6000)
        return True, "NO_ACTION: This turn is not for me.", None, None

    from istota.scheduler import process_one_task
    with patch("istota.scheduler.execute_task", side_effect=execute):
        process_one_task(config)
    with db.get_db(path) as conn:
        assert db.get_task(conn, task_id).status == "completed"
        assert purchases.list_purchases(conn, "alice")[0]["state"] == "cancelled"
        assert conn.execute("SELECT state FROM whatsapp_skill_requests").fetchone()[0] == "cancelled"


@pytest.mark.parametrize("surface", ["sms", "whatsapp"])
def test_phone_purchase_preview_reaches_delivery_intact(wallet_env, surface):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from istota.scheduler import process_one_task
    from istota.transport.sms.outbound import render_sms
    from istota.transport.whatsapp.outbound import render_whatsapp

    path, config, _, task_id = wallet_env
    config.db_path = path
    config.sms.enabled = True
    config.sms.max_segments = 10
    config.whatsapp.enabled = True
    config.users = {"alice": UserConfig(sms_phone_number="+15551234567")}
    with db.get_db(path) as conn:
        conn.execute("UPDATE tasks SET status='pending', source_type=?, output_target=? WHERE id=?", (surface, surface, task_id))

    def execute(*args, **kwargs):
        with db.get_db(path) as conn:
            request(conn, wallet_env, amount_cents=6000)
        return True, "Waiting for purchase approval.", None, None

    transport = "SmsTransport" if surface == "sms" else "WhatsAppTransport"
    send = AsyncMock(return_value=SimpleNamespace(status="accepted"))
    with patch("istota.scheduler.execute_task", side_effect=execute), patch(f"istota.transport.{surface}.{transport}.send_record", send):
        process_one_task(config)
    with db.get_db(path) as conn:
        task = db.get_task(conn, task_id)
        assert task.status == "pending_confirmation"
        assert send.call_count == 1
        body = send.call_args.args[1]
        assert task.confirmation_prompt in body
        rendered = render_sms(body, config.sms.max_segments).text if surface == "sms" else render_whatsapp(body)
        assert rendered == body
