"""Native push delivery through the real inbox and room stores."""

import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from istota import db
from istota.notifications import store
from istota.config import Config, PushConfig
from istota.push import apns, devices, dispatch


def test_pending_flag_tracks_insert_bump_and_reopen(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        def write(title):
            return store.write_notification(conn, "alice", source="confirmation",
                                            dedup_key="task:1", title=title)
        first = write("Waiting")
        assert first.deliver
        row = conn.execute("SELECT * FROM notifications").fetchone()
        assert row["push_pending"] == 1
        assert not write("Still waiting").deliver
        assert conn.execute("SELECT push_pending FROM notifications").fetchone()[0] == 1
        conn.execute("UPDATE notifications SET push_pending=0")
        assert not write("Still open").deliver
        assert conn.execute("SELECT push_pending FROM notifications").fetchone()[0] == 0
        conn.execute("UPDATE notifications SET state='resolved'")
        assert write("Waiting again").deliver
        assert conn.execute("SELECT push_pending FROM notifications").fetchone()[0] == 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    key = tmp_path / "test-key.p8"
    key.write_text("unused by mocked APNs client")
    config = Config(db_path=tmp_path / "test.db", push=PushConfig(enabled=True, apns_key_path=str(key)))
    db.init_db(config.db_path)
    now = [2_000_000_000.0]
    monkeypatch.setattr(dispatch.time, "time", lambda: now[0])
    monkeypatch.setattr(dispatch, "_warned_unavailable", False)
    sent = []

    def send(push_config, device, payload, **kwargs):
        # A second connection can write during the network call: no transaction
        # from the sweep is allowed to hold the DB writer lock here.
        with db.get_db(config.db_path, busy_timeout_ms=10) as conn:
            conn.execute("UPDATE push_state SET updated_at=updated_at")
        sent.append((device["user_id"], device["id"], copy.deepcopy(payload), kwargs))
        return apns.ApnsResult(status=200, badge=payload["aps"]["badge"],
                               device_created_at=device["created_at"])

    monkeypatch.setattr(apns, "send", send)
    return config, now, sent


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def register(conn, user="alice", token="ab" * 32):
    return devices.register_device(conn, user, token, "sandbox", auth_method="nextcloud", auth_epoch=0)


def message(conn, room, when, body="Reply", role="assistant"):
    db.add_message(conn, room.token, role=role, body=body, origin_surface="web")
    message_id = db.room_max_message_id(conn, room.token)
    conn.execute("UPDATE messages SET created_at=? WHERE id=?", (stamp(when), message_id))
    return message_id


def notification(conn, when, *, user="alice", key="task:1"):
    result = store.write_notification(conn, user, source="confirmation", dedup_key=key,
                                     title="Approval needed", body="Review the held task", link="/chat")
    conn.execute("UPDATE notifications SET updated_at=? WHERE id=?", (stamp(when), result.notification_id))
    return result.notification_id


def test_full_sweep_two_users_three_rooms_and_held_task(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        alice = register(conn)
        bob = register(conn, "bob", "cd" * 32)
        work = db.create_web_chat_room(conn, "alice", "Work")
        home = db.create_web_chat_room(conn, "alice", "Home")
        shared = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_web_room_member(conn, shared.token, "alice")
        held = db.create_task(conn, user_id="alice", prompt="Approve a test request", source_type="web")
        conn.execute("UPDATE tasks SET status='pending_confirmation' WHERE id=?", (held,))
        notification(conn, now[0], key=f"task:{held}")
        message(conn, work, now[0] - 20, "First")
        message(conn, work, now[0] - 19, "Newest")
        message(conn, home, now[0] - 18, "Home reply")
        message(conn, shared, now[0] - 17, "Shared reply")
    dispatch.run_push_dispatch(config)
    assert [(user, device, p["istota"]["kind"], p["aps"]["badge"], p["aps"]["alert"]["body"])
            for user, device, p, _ in sent] == [
        ("alice", alice, "notification", 5, "Review the held task"),
        ("alice", alice, "chat", 5, "2 new messages: Newest"),
        ("alice", alice, "chat", 5, "Home reply"),
        ("alice", alice, "chat", 5, "Shared reply"),
        ("bob", bob, "chat", 1, "Shared reply"),
    ]
    assert sent[1][2]["istota"]["room_id"] == work.id
    assert sent[1][3]["collapse_id"] == "room:" + work.token
    with db.get_db(config.db_path) as conn:
        alice_handle = db.get_web_chat_room(conn, sent[3][2]["istota"]["room_id"])
        assert alice_handle.user_id == "alice" and alice_handle.token == shared.token
        assert sent[4][2]["istota"]["room_id"] == shared.id
    assert all(item[3]["expiration"] == 0 for item in sent)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT push_pending FROM notifications").fetchone()[0] == 0
    dispatch.run_push_dispatch(config)
    assert len(sent) == 5


def test_delay_watermark_keeps_young_message_for_next_tick(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        room = db.create_web_chat_room(conn, "alice", "Work")
        old = message(conn, room, now[0] - 20, "Old enough")
        young = message(conn, room, now[0] - 2, "Wait for me")
    dispatch.run_push_dispatch(config)
    assert [s[2]["aps"]["alert"]["body"] for s in sent] == ["Old enough"]
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT last_pushed_message_id FROM push_state").fetchone()[0] == old
    now[0] += 20
    dispatch.run_push_dispatch(config)
    assert [s[2]["aps"]["alert"]["body"] for s in sent] == ["Old enough", "Wait for me"]
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT last_pushed_message_id FROM push_state").fetchone()[0] == young


def test_read_archived_dismissed_hidden_and_own_turns_do_not_alert(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        for name in ("read", "archive", "dismiss", "hidden", "own"):
            room = db.create_web_chat_room(conn, "alice", name)
            mid = message(conn, room, now[0] - 20, role="user" if name == "own" else "assistant")
            if name == "read":
                db.set_room_read_state(conn, room.token, "web", mid, "alice")
            elif name == "archive":
                db.set_room_archived(conn, room.token, True)
            elif name == "dismiss":
                db.dismiss_room(conn, room.token, "alice")
            elif name == "hidden":
                conn.execute("DELETE FROM room_bindings WHERE room_token=?", (room.token,))
                db.add_room_binding(conn, room.token, "email", "thread:private")
    dispatch.run_push_dispatch(config)
    assert len(sent) == 1
    assert sent[0][2] == {"aps": {"badge": 0}}
    assert sent[0][3] == {"collapse_id": "badge", "priority": 5, "expiration": 0}


@pytest.mark.parametrize("setting,expected", [("badge", 2), ("off", 1)])
def test_chat_preferences_preserve_inbox_alerts(setup, setting, expected):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        conn.execute("INSERT INTO user_profiles (user_id, push_chat_alerts) VALUES (?, ?)", ("alice", setting))
        room = db.create_web_chat_room(conn, "alice", "Work")
        message(conn, room, now[0] - 20)
        notification(conn, now[0])
    dispatch.run_push_dispatch(config)
    assert len(sent) == 1
    assert sent[0][2]["istota"]["kind"] == "notification"
    assert sent[0][2]["aps"]["badge"] == expected


def test_browser_read_repairs_badge_once_per_device(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        register(conn, token="ef" * 32)
        room = db.create_web_chat_room(conn, "alice", "Work")
        mid = message(conn, room, now[0] - 20)
    dispatch.run_push_dispatch(config)
    assert len(sent) == 2
    with db.get_db(config.db_path) as conn:
        db.set_room_read_state(conn, room.token, "web", mid, "alice")
    dispatch.run_push_dispatch(config)
    assert [s[2] for s in sent[2:]] == [{"aps": {"badge": 0}}, {"aps": {"badge": 0}}]
    dispatch.run_push_dispatch(config)
    assert len(sent) == 4


def test_old_and_closed_alerts_are_drained_without_banners(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0] - 3600)
        closed = notification(conn, now[0], key="task:2")
        conn.execute("UPDATE notifications SET state='resolved' WHERE id=?", (closed,))
        room = db.create_web_chat_room(conn, "alice", "Work")
        last = message(conn, room, now[0] - 3600)
    dispatch.run_push_dispatch(config)
    assert [s[2] for s in sent] == [{"aps": {"badge": 2}}]
    with db.get_db(config.db_path) as conn:
        assert not store.take_pending_pushes(conn)
        assert conn.execute("SELECT last_pushed_message_id FROM push_state").fetchone()[0] == last


def test_no_devices_short_circuits_before_per_user_or_candidate_queries(setup, monkeypatch):
    config, now, sent = setup
    original = db.get_db
    queries = []
    @contextmanager
    def recording(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            conn.set_trace_callback(queries.append)
            yield conn
    monkeypatch.setattr(db, "get_db", recording)
    dispatch.run_push_dispatch(config)
    selects = [q for q in queries if q.startswith("SELECT")]
    assert selects == ["SELECT 1 FROM push_devices LIMIT 1"]
    assert not sent


def test_failed_alert_not_retried_but_badge_repaired_next_tick(setup, monkeypatch):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0])
        room = db.create_web_chat_room(conn, "alice", "Work")
        message(conn, room, now[0] - 20)
    successful = apns.send
    attempts = []
    def fail(push_config, device, payload, **kwargs):
        attempts.append(payload)
        return apns.ApnsResult(status=503, reason="ServiceUnavailable", device_created_at=device["created_at"])
    monkeypatch.setattr(apns, "send", fail)
    dispatch.run_push_dispatch(config)
    assert len(attempts) == 2
    with db.get_db(config.db_path) as conn:
        assert not store.take_pending_pushes(conn)
        assert devices.devices_for_user(conn, "alice")[0]["last_error"] == "503 ServiceUnavailable"
    monkeypatch.setattr(apns, "send", successful)
    dispatch.run_push_dispatch(config)
    assert [s[2] for s in sent] == [{"aps": {"badge": 2}}]


def test_broken_user_does_not_stop_next_user(setup, monkeypatch):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        register(conn, "bob", "cd" * 32)
        conn.execute("UPDATE push_devices SET token='malformed' WHERE user_id='alice'")
        notification(conn, now[0])
        notification(conn, now[0], user="bob")
    successful = apns.send
    def send(push_config, device, payload, **kwargs):
        if device["token"] == "malformed":
            raise ValueError("broken boundary")
        return successful(push_config, device, payload, **kwargs)
    monkeypatch.setattr(apns, "send", send)
    dispatch.run_push_dispatch(config)
    assert [s[0] for s in sent] == ["bob"]


def test_unavailable_configuration_warns_once_and_never_drains(setup, caplog):
    config, now, sent = setup
    config.push.enabled = False
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0])
    dispatch.run_push_dispatch(config)
    dispatch.run_push_dispatch(config)
    assert caplog.text.count("Native push unavailable") == 1
    with db.get_db(config.db_path) as conn:
        assert len(store.take_pending_pushes(conn)) == 1
    assert not sent


def test_scheduler_gate_is_background_single_inflight_and_respects_interval(setup, monkeypatch):
    from istota import scheduler
    config, _, _ = setup
    config.push.dispatch_interval_seconds = 7
    started = threading.Event()
    release = threading.Event()
    calls = []
    def slow(config):
        calls.append(config)
        started.set()
        assert release.wait(3)
    monkeypatch.setattr("istota.push.run_push_dispatch", slow)
    gate = next(g for g in scheduler.build_interval_gates(config) if g.name == "push-dispatch")
    assert gate.background and gate.interval(config) == 7
    clocks = {gate.name: 100.0}
    inflight = {}
    try:
        scheduler._tick_interval_gates([gate], clocks, config, 106, inflight)
        assert not started.is_set()
        scheduler._tick_interval_gates([gate], clocks, config, 107, inflight)
        assert started.wait(1)
        scheduler._tick_interval_gates([gate], clocks, config, 114, inflight)
        assert calls == [config]
    finally:
        release.set()
        for thread in inflight.values():
            thread.join(1)
    config.push.dispatch_interval_seconds = 0
    assert not gate.enabled(config)


def test_pending_batch_limit_and_concurrent_refresh_survives_ack(setup, monkeypatch):
    config, now, sent = setup
    config.push.alert_batch_limit = 1
    with db.get_db(config.db_path) as conn:
        register(conn)
        first = notification(conn, now[0])
        second = notification(conn, now[0], key="task:2")
    original = apns.send
    def reopen(push_config, device, payload, **kwargs):
        result = original(push_config, device, payload, **kwargs)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE notifications SET state='resolved' WHERE id=?", (first,))
            notification(conn, now[0], key="task:1")
        return result
    monkeypatch.setattr(apns, "send", reopen)
    dispatch.run_push_dispatch(config)
    assert len(sent) == 1
    with db.get_db(config.db_path) as conn:
        assert [r["id"] for r in store.take_pending_pushes(conn)] == [first, second]
    monkeypatch.setattr(apns, "send", original)
    dispatch.run_push_dispatch(config)
    dispatch.run_push_dispatch(config)
    assert [s[2]["istota"]["notification_id"] for s in sent] == [first, first, second]


def test_device_move_between_alerts_stops_old_account_sends(setup, monkeypatch):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0])
        notification(conn, now[0], key="task:2")
    original = apns.send
    def move(push_config, device, payload, **kwargs):
        result = original(push_config, device, payload, **kwargs)
        with db.get_db(config.db_path) as conn:
            register(conn, "bob")
        return result
    monkeypatch.setattr(apns, "send", move)
    dispatch.run_push_dispatch(config)
    assert len(sent) == 1
    with db.get_db(config.db_path) as conn:
        assert devices.devices_for_user(conn, "bob")[0]["last_badge"] == -1


def test_badge_read_failure_never_publishes_zero(setup, monkeypatch):
    import sqlite3
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0])
    def broken(conn, user_id, *, profile=None):
        raise sqlite3.OperationalError("busy")
    monkeypatch.setattr(dispatch.preferences, "push_badge_count", broken)
    dispatch.run_push_dispatch(config)
    assert not sent
    with db.get_db(config.db_path) as conn:
        assert len(store.take_pending_pushes(conn)) == 1


def test_badge_only_setting_sends_chat_count_without_banner(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        conn.execute("INSERT INTO user_profiles (user_id, push_chat_alerts) VALUES ('alice', 'badge')")
        room = db.create_web_chat_room(conn, "alice", "Work")
        message(conn, room, now[0] - 20)
    dispatch.run_push_dispatch(config)
    assert [s[2] for s in sent] == [{"aps": {"badge": 1}}]


def test_draining_unregistered_user_does_not_seed_first_registration(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn, "bob", "cd" * 32)
        notification(conn, now[0])
    dispatch.run_push_dispatch(config)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT 1 FROM push_state WHERE user_id='alice'").fetchone() is None
        room = db.create_web_chat_room(conn, "alice", "Work")
        last = message(conn, room, now[0] - 20)
        register(conn)
        assert conn.execute("SELECT last_pushed_message_id FROM push_state WHERE user_id='alice'").fetchone()[0] == last
    dispatch.run_push_dispatch(config)
    assert all("alert" not in s[2]["aps"] for s in sent)


@pytest.mark.parametrize("unavailable", ["key", "h2"])
def test_missing_prerequisite_leaves_pending_work(setup, monkeypatch, unavailable, caplog):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0])
    if unavailable == "key":
        config.push.apns_key_path = str(config.db_path.parent / "absent.p8")
    else:
        monkeypatch.setattr(dispatch.importlib.util, "find_spec", lambda name: None)
    dispatch.run_push_dispatch(config)
    dispatch.run_push_dispatch(config)
    assert not sent
    assert caplog.text.count("Native push unavailable") == 1
    with db.get_db(config.db_path) as conn:
        assert len(store.take_pending_pushes(conn)) == 1


def test_inbox_reaches_each_device_with_saved_preview_preference(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        first = register(conn)
        second = register(conn, token="ef" * 32)
        conn.execute("INSERT INTO user_profiles (user_id, push_preview) VALUES ('alice', 0)")
        notification(conn, now[0])
    dispatch.run_push_dispatch(config)
    assert [s[1] for s in sent] == [first, second]
    assert all(s[2]["aps"]["alert"] == {"title": "Something needs your attention", "body": ""}
               and s[2]["aps"]["badge"] == 1 for s in sent)
    with db.get_db(config.db_path) as conn:
        assert not store.take_pending_pushes(conn)


def test_disabled_category_is_acknowledged_without_replay(setup):
    from istota import user_profiles
    config, now, sent = setup
    user_profiles.ensure_profile(config.db_path, "alice")
    user_profiles.update_profile(config.db_path, "alice", push_inbox_categories={"confirmation": False})
    with db.get_db(config.db_path) as conn:
        register(conn)
        notification(conn, now[0])
    dispatch.run_push_dispatch(config)
    assert [item[2] for item in sent] == [{"aps": {"badge": 0}}]
    with db.get_db(config.db_path) as conn:
        assert not store.take_pending_pushes(conn)
        assert store.counts(conn, "alice")["open"] == 1
    user_profiles.update_profile(config.db_path, "alice", push_inbox_categories={})
    dispatch.run_push_dispatch(config)
    assert sent[-1][2] == {"aps": {"badge": 1}}
    assert len(sent) == 2


def test_enabling_room_does_not_replay_old_messages(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        register(conn)
        room = db.create_web_chat_room(conn, "alice", "Work")
        db.update_web_chat_room(conn, room.id, push_notifications="off")
        message(conn, room, now[0] - 20)
    dispatch.run_push_dispatch(config)
    with db.get_db(config.db_path) as conn:
        db.update_web_chat_room(conn, room.id, push_notifications="alerts")
    dispatch.run_push_dispatch(config)
    assert [s[2] for s in sent] == [{"aps": {"badge": 0}}, {"aps": {"badge": 1}}]


@pytest.mark.parametrize("revocation", ["legacy", "disabled", "epoch", "deleted", "method"])
def test_dispatch_refuses_revoked_enrollment(setup, revocation):
    from istota import user_profiles
    from istota.webui import auth
    config, now, sent = setup
    user_profiles.ensure_profile(config.db_path, "alice")
    identity = auth.upsert_identity(config.db_path, "alice", "alice@example.com")
    with db.get_db(config.db_path) as conn:
        devices.register_device(conn, "alice", "ab" * 32, "sandbox", auth_method="nextcloud", auth_epoch=identity.credential_epoch)
        notification(conn, now[0])
        if revocation == "legacy":
            conn.execute("UPDATE push_devices SET auth_method=NULL, auth_epoch=NULL")
        elif revocation == "disabled":
            conn.execute("UPDATE web_auth_identities SET disabled=1")
        elif revocation == "epoch":
            conn.execute("UPDATE web_auth_identities SET credential_epoch=credential_epoch+1")
        elif revocation == "deleted":
            conn.execute("DELETE FROM user_profiles WHERE user_id='alice'")
            conn.execute("UPDATE push_devices SET auth_method='email'")
    if revocation == "method":
        config.web.auth = ["email"]
    dispatch.run_push_dispatch(config)
    assert not sent


def test_revocation_during_send_stops_remaining_alerts(setup, monkeypatch):
    from istota import user_profiles
    from istota.webui import auth
    config, now, sent = setup
    user_profiles.ensure_profile(config.db_path, "alice")
    identity = auth.upsert_identity(config.db_path, "alice", "alice@example.com")
    with db.get_db(config.db_path) as conn:
        devices.register_device(conn, "alice", "ab" * 32, "sandbox", auth_method="nextcloud", auth_epoch=identity.credential_epoch)
        notification(conn, now[0], key="task:1")
        notification(conn, now[0], key="task:2")
    send = apns.send
    def revoke_after_first(*args, **kwargs):
        result = send(*args, **kwargs)
        auth.bump_epoch(config.db_path, "alice")
        return result
    monkeypatch.setattr(apns, "send", revoke_after_first)
    dispatch.run_push_dispatch(config)
    assert len(sent) == 1


BINDING = "0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51"


def test_generic_shell_routing_names_its_own_binding_and_legacy_payloads_are_unchanged(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        legacy = register(conn)
        bound = devices.register_device(conn, "alice", "ef" * 32, "sandbox", "ios", "0.12.0",
                                        auth_method="nextcloud", auth_epoch=0,
                                        connection_binding=BINDING, connection_generation=2)
        room = db.create_web_chat_room(conn, "alice", "Work")
        mid = message(conn, room, now[0] - 20)
        notification(conn, now[0])
    dispatch.run_push_dispatch(config)
    routed = {(device, p["istota"]["kind"]): p["istota"] for _, device, p, _ in sent}
    assert routed[(legacy, "chat")] == {"kind": "chat", "room_token": room.token, "room_id": room.id}
    assert set(routed[(legacy, "notification")]) == {"kind", "notification_id", "link"}
    for kind in ("chat", "notification"):
        target = routed[(bound, kind)]
        assert target["connection_binding"] == BINDING and target["connection_generation"] == 2
        assert {k: v for k, v in target.items() if not k.startswith("connection_")} == routed[(legacy, kind)]
    with db.get_db(config.db_path) as conn:
        db.set_room_read_state(conn, room.token, "web", mid, "alice")
        store.dismiss(conn, routed[(legacy, "notification")]["notification_id"], "alice", by="web")
    dispatch.run_push_dispatch(config)
    # A badge-only update has no routing and so no binding: the OS applies it before any app code runs.
    assert [s[2] for s in sent[4:]] == [{"aps": {"badge": 0}}, {"aps": {"badge": 0}}]


def test_bound_routing_survives_the_payload_bound(setup):
    config, now, sent = setup
    with db.get_db(config.db_path) as conn:
        devices.register_device(conn, "alice", TOKEN_BOUND, "sandbox", "ios", "0.12.0",
                                auth_method="nextcloud", auth_epoch=0,
                                connection_binding=BINDING, connection_generation=2**53 - 1)
        room = db.create_web_chat_room(conn, "alice", "W" * 5000)
        message(conn, room, now[0] - 20, "x" * 5000)
    dispatch.run_push_dispatch(config)
    (_, _, payload, _), = sent
    assert payload["istota"]["connection_binding"] == BINDING
    assert payload["istota"]["connection_generation"] == 2**53 - 1
    assert len(dispatch.json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 4096


TOKEN_BOUND = "ef" * 32
