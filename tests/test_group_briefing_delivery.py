"""Saved editions traverse the real workers and provider boundaries."""

import smtplib
from datetime import timedelta

import pytest

from istota import db, scheduler
from istota.briefings import group_store as store
from istota.groups.policy import GroupError
from tests.test_group_briefing_generation import brain as _brain, clock as _clock
from tests.test_group_briefing_store import NOW, define, enqueue, env as _env, payload


brain = _brain
clock = _clock
env = _env


@pytest.fixture
def mail(env, monkeypatch):
    env.email.bot_email = "bot@example.com"
    for user in ("alice", "bob"):
        env.users[user].email_addresses = [f"{user}@example.com"]
    sent = []
    monkeypatch.setattr("istota.skills.email._send_smtp", lambda msg, config, **kw: sent.append(msg))
    return sent


def published(env, *, channel="email", body="Same **news** [article](https://example.com/article)"):
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, channel=channel)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        return store.publish_run(conn, env, run["id"], claim["attempt_token"],
                                 subject="News", body_md=body, now=NOW)


def deliver(env):
    from istota.briefings.group_delivery import queue_deliveries
    with db.get_db(env.db_path) as conn:
        tasks = queue_deliveries(conn, env, now=NOW)
    for task in tasks:
        assert scheduler.process_one_task(env, user_id="__briefings__") == (task, True)
    return tasks


def legs(env, run):
    with db.get_db(env.db_path) as conn:
        return {r["user_id"]: r for r in store.list_recipients(conn, run["id"]) if r["channel"] != "in_app"}


def test_scheduler_publication_queues_saved_mail_and_retry_never_generates(env, clock, brain, mail, monkeypatch):
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="UTC", generation={"version": 1, "prompt": "Write news",
            "research": {"enabled": True, "prompt": "Read public articles"}})
    research = scheduler.check_group_briefings(env)
    with db.get_db(env.db_path) as conn:
        db.update_task_status(conn, research[0], "completed", result="[Article](https://example.com/article)")
    generation = scheduler.check_group_briefings(env)
    assert scheduler.process_one_task(env, user_id="__briefings__") == (generation[0], True)
    attempts = []
    def smtp(msg, config, **kwargs):
        attempts.append(msg)
        if msg["To"] == "bob@example.com" and len(attempts) == 2:
            raise smtplib.SMTPRecipientsRefused({"bob@example.com": (450, b"PRIVATE ERROR")})
        mail.append(msg)
    monkeypatch.setattr("istota.skills.email._send_smtp", smtp)
    queued = scheduler.check_group_briefings(env)
    assert len(queued) == 2
    for task in queued:
        assert scheduler.process_one_task(env, user_id="__briefings__") == (task, True)
    with db.get_db(env.db_path) as conn:
        run = store.get_run(conn, conn.execute("SELECT id FROM group_briefing_runs").fetchone()[0])
        assert store.retry_deliveries(conn, env, run["id"], actor_id="alice", now=NOW) == 1
    deliver(env)
    assert [m["To"] for m in mail] == ["alice@example.com", "bob@example.com"]
    assert all(m.get_body(preferencelist=("plain",)).get_content().strip() == "Common edition" for m in mail)
    assert attempts[1]["Message-ID"] == attempts[2]["Message-ID"]
    assert all(r["state"] == "sent" for r in legs(env, run).values())
    assert brain.call_count == 1
    with db.get_db(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type='group_briefing_research'").fetchone()[0] == 1


def test_uncertain_is_held_requires_owner_ack_and_never_moves_destination(env, clock, mail, monkeypatch):
    run = published(env)
    def ambiguous(msg, config, **kwargs):
        if msg["To"] == "bob@example.com":
            raise TimeoutError("PRIVATE PROVIDER DETAIL")
        mail.append(msg)
    monkeypatch.setattr("istota.skills.email._send_smtp", ambiguous)
    deliver(env)
    assert legs(env, run)["bob"]["state"] == "uncertain"
    assert deliver(env) == []
    with db.get_db(env.db_path) as conn:
        assert store.retry_deliveries(conn, env, run["id"], actor_id="alice", now=NOW) == 0
        for actor, admin in (("bob", False), ("admin", True)):
            with pytest.raises(GroupError):
                store.retry_deliveries(conn, env, run["id"], actor_id=actor, is_admin=admin,
                                       acknowledge_duplicates=True, now=NOW)
        assert store.retry_deliveries(conn, env, run["id"], actor_id="alice",
                                     acknowledge_duplicates=True, now=NOW) == 1
    env.users["bob"].email_addresses = ["new@example.com"]
    deliver(env)
    assert legs(env, run)["bob"]["error_code"] == "destination_changed"
    assert [m["To"] for m in mail] == ["alice@example.com"]


@pytest.mark.parametrize("change,reason", [("module", "module_disabled"), ("leave_rejoin", "membership_ended"),
    ("account", "membership_ended"), ("mute", "muted"), ("group_mute", "muted"), ("missing", "missing_email")])
def test_live_recipient_checks_before_send(env, clock, mail, change, reason):
    run = published(env)
    with db.get_db(env.db_path) as conn:
        if change == "leave_rejoin":
            db.end_group_membership(conn, "team", "bob", ended_by="alice")
            db.add_group_member(conn, "team", "bob", added_by="alice")
        elif change in ("mute", "group_mute"):
            store.set_delivery_preferences(conn, env, "team", actor_id="bob", outbound_muted=True,
                in_app_muted=False, definition_id=run["definition_id"] if change == "mute" else None)
        elif change == "module":
            env.users["bob"].disabled_modules = ["briefings"]
        elif change == "account":
            del env.users["bob"]
        else:
            env.users["bob"].email_addresses = []
        db.add_group_member(conn, "team", "carol", added_by="alice")
    deliver(env)
    assert legs(env, run)["bob"]["error_code"] == reason
    assert "carol" not in legs(env, run)
    assert [m["To"] for m in mail] == ["alice@example.com"]


def test_html_preference_preserves_common_content(env, clock, mail):
    env.users["bob"].briefing_email_html = False
    run = published(env)
    deliver(env)
    assert mail[0].is_multipart() and not mail[1].is_multipart()
    assert mail[0].get_body(preferencelist=("plain",)).get_content() == mail[1].get_content()
    assert "https://example.com/article" in mail[0].get_body(preferencelist=("html",)).get_content()
    assert all(r["state"] == "sent" for r in legs(env, run).values())


def test_claim_restart_and_stale_worker_are_held(env, clock, mail):
    run = published(env)
    with db.get_db(env.db_path) as conn:
        recipient = legs(env, run)["alice"]
        claim = store.claim_recipient(conn, env, recipient["id"], now=NOW)
        assert store.claim_recipient(conn, env, recipient["id"], now=NOW) is None
        store.prepare_delivery(conn, recipient["id"], claim["dispatch_token"], {"to": "alice@example.com"}, [run["body_md"]], now=NOW)
        store.start_delivery_part(conn, env, recipient["id"], claim["dispatch_token"], 0, now=NOW)
    with db.get_db(env.db_path) as conn:
        store.expire_delivery_claims(conn, now=NOW + timedelta(minutes=6))
        assert store.get_recipient(conn, recipient["id"])["state"] == "uncertain"
        with pytest.raises(GroupError):
            store.finish_delivery_part(conn, recipient["id"], claim["dispatch_token"], 0, "sent", now=NOW)
    deliver(env)
    assert [m["To"] for m in mail] == ["bob@example.com"]


def test_talk_retry_preserves_confirmed_parts_and_reuses_reference(env, clock, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    env.talk.enabled = True
    env.nextcloud.url = "https://cloud.example.com"
    for name in ("alice", "bob"):
        env.users[name].alerts_channel = name + "-private"
    run = published(env, channel="talk", body="Common news. " * 4000)
    sent = []
    refused = False
    async def send(token, text, **kwargs):
        nonlocal refused
        sent.append((token, text, kwargs["reference_id"]))
        if token == "alice-private" and "(2/" in text and not refused:
            refused = True
            raise httpx.HTTPStatusError("private detail", request=httpx.Request("POST", "https://cloud.example.com"),
                                        response=httpx.Response(403))
        return {"id": len(sent)}
    client = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    monkeypatch.setattr("istota.transport.talk.get_talk_client", lambda config: client)
    deliver(env)
    assert legs(env, run)["alice"]["state"] == "failed"
    with db.get_db(env.db_path) as conn:
        assert store.retry_deliveries(conn, env, run["id"], actor_id="alice", now=NOW) == 1
    deliver(env)
    first = [p for p in sent if p[0] == "alice-private" and "(1/" in p[1]]
    second = [p for p in sent if p[0] == "alice-private" and "(2/" in p[1]]
    assert len(first) == 1 and len(second) == 2 and second[0][2] == second[1][2]
    assert all(r["state"] == "sent" for r in legs(env, run).values())


def test_ntfy_unicode_parts_use_existing_sender_and_hold_ambiguous_response(env, clock, monkeypatch):
    import httpx
    run = published(env, channel="ntfy", body="\U0001f30d" * 1400)
    monkeypatch.setattr("istota.transport.ntfy.ntfy_settings", lambda config, user: {
        "server_url": "https://ntfy.example.com", "topic": user, "token": "", "username": "", "password": ""})
    sent = []
    def post(url, *, content, **kwargs):
        sent.append((url, content))
        if url.endswith("/bob"):
            raise httpx.ReadTimeout("private error")
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr("istota.transport.ntfy.httpx.post", post)
    deliver(env)
    assert legs(env, run)["alice"]["state"] == "sent"
    assert legs(env, run)["bob"]["state"] == "uncertain"
    assert all(len(body.encode()) <= 4096 for _, body in sent)
    assert len(sent) == 3


def test_disable_then_enable_does_not_revive_queued_edition(env, clock, mail):
    run = published(env)
    with db.get_db(env.db_path) as conn:
        definition = store.get_definition(conn, run["definition_id"])
        for enabled in (False, True):
            definition = store.save_definition(conn, env, "team", payload(enabled=enabled),
                actor_id="alice", expected_revision=definition["revision"], definition_id=definition["id"])
    assert deliver(env) == []
    assert not mail
    assert all(r["state"] == "skipped" for r in legs(env, run).values())


def test_owner_succession_controls_retry_and_mutes_do_not_grant_management(env, clock, mail, monkeypatch):
    from istota.groups.service import change_membership
    run = published(env)
    monkeypatch.setattr("istota.skills.email._send_smtp", lambda *a, **kw: (_ for _ in ()).throw(
        smtplib.SMTPRecipientsRefused({"recipient": (450, b"rejected")})))
    deliver(env)
    with db.get_db(env.db_path) as conn:
        store.set_delivery_preferences(conn, env, "team", actor_id="bob", outbound_muted=False, in_app_muted=True)
        change_membership(conn, env, "team", "bob", actor_id="alice", expected_revision=0, role="owner")
        change_membership(conn, env, "team", "alice", actor_id="bob", expected_revision=1, role="member")
        with pytest.raises(GroupError) as exc:
            store.retry_deliveries(conn, env, run["id"], actor_id="alice", now=NOW)
        assert exc.value.status_code == 403
        assert store.retry_deliveries(conn, env, run["id"], actor_id="bob", now=NOW) == 2
        assert store.get_run(conn, run["id"])["snapshot"]["definition"]["created_by"] == "alice"


@pytest.mark.parametrize("during", [False, True])
def test_grant_revocation_stops_pending_sends_and_keeps_edition(env, clock, mail, monkeypatch, tmp_path, during):
    from tests.test_briefing_source_grants import define as contribution, approve, enable
    from istota.briefings.source_grants import revoke_source
    env.workspace_path = tmp_path / "workspace"
    root = env.workspace_root("bob")
    root.mkdir(parents=True)
    (root / "notes.md").write_text("Approved contribution")
    with db.get_db(env.db_path) as conn:
        definition = contribution(conn, env)
        grant = approve(conn, env, definition)
        definition = enable(conn, env, definition)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        run = store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News",
                                body_md="Approved common content", now=NOW)
    def revoke():
        with db.get_db(env.db_path) as conn:
            revoke_source(conn, env, definition["id"], "selected", actor_id="bob",
                          expected_grant_revision=grant["revision"], now=NOW)
    if during:
        def smtp(msg, config, **kwargs):
            mail.append(msg)
            revoke()
        monkeypatch.setattr("istota.skills.email._send_smtp", smtp)
    else:
        revoke()
    deliver(env)
    assert len(mail) == int(during)
    assert legs(env, run)["bob"]["state"] == "skipped"
    with db.get_db(env.db_path) as conn:
        assert store.get_run(conn, run["id"])["body_md"] == "Approved common content"


def test_talk_private_room_gate_is_rechecked_between_parts(env, clock, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    env.talk.enabled = True
    env.nextcloud.url = "https://cloud.example.com"
    env.users["alice"].alerts_channel = "alice-private"
    env.users["bob"].alerts_channel = "bob-private"
    with db.get_db(env.db_path) as conn:
        db.register_room(conn, "room-a", "alice", origin="talk")
        db.add_room_binding(conn, "room-a", "talk", "alice-private")
    run = published(env, channel="talk", body="Common news. " * 4000)
    sent = []
    async def send(token, text, **kwargs):
        sent.append(token)
        if token == "alice-private":
            with db.get_db(env.db_path) as conn:
                db.add_room_member(conn, "room-a", "carol")
        return {"id": len(sent)}
    monkeypatch.setattr("istota.transport.talk.get_talk_client", lambda config: SimpleNamespace(send_message=AsyncMock(side_effect=send)))
    deliver(env)
    assert sent.count("alice-private") == 1
    assert legs(env, run)["alice"]["error_code"] == "shared_destination"
    assert legs(env, run)["bob"]["state"] == "sent"


def test_delivery_queue_and_claim_have_one_winner(env, clock, mail):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from istota.briefings.group_delivery import queue_deliveries
    run = published(env)
    barrier = Barrier(2)
    def queue(_):
        with db.get_db(env.db_path) as conn:
            barrier.wait(timeout=5)
            return queue_deliveries(conn, env, now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = list(pool.map(queue, range(2)))
    assert sum(len(tasks) for tasks in queued) == 2
    recipient_id = legs(env, run)["alice"]["id"]
    def claim(_):
        with db.get_db(env.db_path) as conn:
            barrier.wait(timeout=5)
            return store.claim_recipient(conn, env, recipient_id, now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert sum(c is not None for c in claims) == 1


def test_failed_email_reresolves_destination_without_repeating_confirmed_recipient(env, clock, mail, monkeypatch):
    run = published(env)
    def smtp(msg, config, **kwargs):
        if msg["To"] == "bob@example.com":
            raise smtplib.SMTPRecipientsRefused({"recipient": (450, b"rejected")})
        mail.append(msg)
    monkeypatch.setattr("istota.skills.email._send_smtp", smtp)
    deliver(env)
    env.users["bob"].email_addresses = ["updated@example.com"]
    with db.get_db(env.db_path) as conn:
        assert store.retry_deliveries(conn, env, run["id"], actor_id="alice", now=NOW) == 1
    deliver(env)
    assert [m["To"] for m in mail] == ["alice@example.com", "updated@example.com"]


def test_delivery_columns_upgrade_an_existing_ledger(env):
    with db.get_db(env.db_path) as conn:
        for column in ("dispatch_token", "lease_expires_at", "destination_json", "destination_locked"):
            conn.execute(f"ALTER TABLE group_briefing_recipients DROP COLUMN {column}")
        conn.commit()
        db._migrate_group_briefings(conn)
        db._migrate_group_briefings(conn)
        assert {r["name"] for r in conn.execute("PRAGMA table_info(group_briefing_recipients)")} >= {
            "dispatch_token", "lease_expires_at", "destination_json", "destination_locked"}
