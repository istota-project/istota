"""A notification raised in the web process is delivered by the daemon (ISSUE-702).

The web process holds no WhatsApp bridge, so a push it sent itself over a
Baileys route was refused definitely and left a `failed` ledger row under the
notification's key, which refuses every later send under that key. The web
route now leaves the delivery to the daemon: a request in the reserved
`_notification_delivery` namespace, written in the producer's own
transaction, which the daemon's `notification-handoff` gate takes and sends.
"""

from __future__ import annotations

import threading

import pytest

from istota import db, scheduler, user_profiles
from istota.config import Config, UserConfig
from istota.notifications import store


def _config(tmp_path) -> Config:
    cfg = Config(db_path=tmp_path / "istota.db", users={"bob": UserConfig(timezone="UTC")})
    db.init_db(cfg.db_path)
    return cfg


def _write(cfg, *, key="k1", title="Title", body="Body", **kwargs):
    with db.get_db(cfg.db_path) as conn:
        result = store.write_notification(
            conn, "bob", source="task_alert", dedup_key=key, title=title, body=body,
            **kwargs,
        )
        store.hand_to_daemon(conn, [result])
    return result


@pytest.fixture
def sends(monkeypatch):
    calls: list[dict] = []

    def fake(config, user_id, message, **kwargs):
        calls.append({"user_id": user_id, "message": message, **kwargs})
        return True

    monkeypatch.setattr("istota.notifications.delivery.send_notification", fake)
    return calls


class TestTheHandOff:
    def test_the_daemon_sends_what_the_producer_left(self, tmp_path, sends):
        cfg = _config(tmp_path)
        result = _write(cfg)
        assert sends == []
        assert store.handoffs_pending(cfg)

        store.deliver_handed_off(cfg)

        assert len(sends) == 1
        sent = sends[0]
        assert sent["user_id"] == "bob"
        assert sent["purpose"] == "alert"
        assert sent["title"] == "Title"
        assert sent["reference_id"] == f"notification:{result.notification_id}"
        assert not store.handoffs_pending(cfg)
        with db.get_db(cfg.db_path) as conn:
            row = conn.execute(
                "SELECT last_delivered_at FROM notifications WHERE id = ?",
                (result.notification_id,),
            ).fetchone()
        assert row["last_delivered_at"] is not None

    def test_a_request_is_taken_once(self, tmp_path, sends):
        cfg = _config(tmp_path)
        _write(cfg)
        store.deliver_handed_off(cfg)
        store.deliver_handed_off(cfg)
        assert len(sends) == 1

    def test_a_rolled_back_producer_leaves_no_request(self, tmp_path, sends):
        cfg = _config(tmp_path)
        with pytest.raises(RuntimeError):
            with db.get_db(cfg.db_path) as conn:
                result = store.write_notification(
                    conn, "bob", source="task_alert", dedup_key="k", title="T",
                )
                store.hand_to_daemon(conn, [result])
                raise RuntimeError("the producer failed after the write")
        assert not store.handoffs_pending(cfg)
        store.deliver_handed_off(cfg)
        assert sends == []

    def test_a_bump_hands_nothing_over(self, tmp_path, sends):
        cfg = _config(tmp_path)
        _write(cfg)
        store.deliver_handed_off(cfg)
        _write(cfg)  # the same key, still open: a bump, which never delivers
        assert not store.handoffs_pending(cfg)

    def test_a_row_closed_before_the_daemon_got_to_it_is_not_pushed(self, tmp_path, sends):
        cfg = _config(tmp_path)
        result = _write(cfg)
        with db.get_db(cfg.db_path) as conn:
            conn.execute(
                "UPDATE notifications SET state = 'dismissed' WHERE id = ?",
                (result.notification_id,),
            )
        store.deliver_handed_off(cfg)
        assert sends == []
        assert not store.handoffs_pending(cfg)

    def test_room_free_and_skipped_surfaces_survive_the_hand_off(self, tmp_path, sends):
        from dataclasses import replace

        cfg = _config(tmp_path)
        with db.get_db(cfg.db_path) as conn:
            result = store.write_notification(
                conn, "bob", source="task_alert", dedup_key="k", title="T",
            )
            store.hand_to_daemon(
                conn, [replace(result, room_free=True, skip_surfaces=("whatsapp",))],
            )
        store.deliver_handed_off(cfg)
        assert sends[0]["only_surfaces"] == store.ROOM_FREE_SURFACES
        assert tuple(sends[0]["skip_surfaces"]) == ("whatsapp",)

    def test_the_user_comes_from_the_row_not_the_request(self, tmp_path, sends):
        # The request names a notification id; whose routing it goes through
        # is read off the row, so a request cannot point one user's row at
        # another user's destinations.
        import json

        cfg = _config(tmp_path)
        result = _write(cfg)
        with db.get_db(cfg.db_path) as conn:
            key = f"request:{result.notification_id}"
            stored = json.loads(db.shared_kv_get(conn, store.HANDOFF_NAMESPACE, key)["value"])
            stored["user_id"] = "mallory"
            db.shared_kv_set(conn, store.HANDOFF_NAMESPACE, key, json.dumps(stored), "test")
        store.deliver_handed_off(cfg)
        assert [s["user_id"] for s in sends] == ["bob"]

    def test_a_cut_push_text_survives_the_hand_off(self, tmp_path, sends):
        # `_common.pushing_only` pushes less than the row holds (#638); the
        # daemon must send what the producer chose, not the stored body.
        from dataclasses import replace

        cfg = _config(tmp_path)
        with db.get_db(cfg.db_path) as conn:
            result = store.write_notification(
                conn, "bob", source="task_alert", dedup_key="k", title="T",
                body="somebody's private words",
            )
            store.hand_to_daemon(conn, [replace(result, text="T: see the bell")])
        store.deliver_handed_off(cfg)
        assert sends[0]["message"] == "T: see the bell"

    def test_a_malformed_request_does_not_block_the_queue(self, tmp_path, sends):
        cfg = _config(tmp_path)
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(conn, store.HANDOFF_NAMESPACE, "request:0", "[]", "test")
        _write(cfg)
        store.deliver_handed_off(cfg)
        assert len(sends) == 1
        assert not store.handoffs_pending(cfg)

    def test_a_raising_send_still_takes_the_request(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("down")

        monkeypatch.setattr("istota.notifications.delivery.send_notification", boom)
        _write(cfg)
        store.deliver_handed_off(cfg)
        assert not store.handoffs_pending(cfg)

    def test_no_database_is_nothing_pending(self, tmp_path):
        cfg = Config(db_path=tmp_path / "absent.db")
        assert store.handoffs_pending(cfg) is False
        store.deliver_handed_off(cfg)
        assert not (tmp_path / "absent.db").exists()

    def test_the_namespace_is_reserved_from_tasks(self):
        from istota.sandbox.kv_namespaces import is_reserved_namespace

        assert is_reserved_namespace(store.HANDOFF_NAMESPACE)


class TestTheGate:
    def test_the_gate_spawns_only_when_a_request_waits(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        spawned: list[str] = []

        def fake_spawn(name, fn, inflight, **kwargs):
            spawned.append(name)
            fn()
            return True

        monkeypatch.setattr(scheduler, "_spawn_background_check", fake_spawn)
        monkeypatch.setattr(
            "istota.notifications.delivery.send_notification", lambda *a, **k: True,
        )
        scheduler.poll_notification_handoffs(cfg, {})
        assert spawned == []
        _write(cfg)
        scheduler.poll_notification_handoffs(cfg, {})
        assert spawned == ["notification-handoff"]
        assert not store.handoffs_pending(cfg)

    def test_the_gate_is_built(self, tmp_path):
        cfg = _config(tmp_path)
        names = [gate.name for gate in scheduler.build_interval_gates(cfg)]
        assert "notification-handoff" in names


class TestTheAdminRouteDoesNotSendFromTheWebProcess:
    """The reported path, end to end through the real route and the real
    delivery layer, with WhatsApp on Baileys and no bridge in this process."""

    @pytest.fixture
    def web(self, db_path, monkeypatch):
        pytest.importorskip("fastapi")
        pytest.importorskip("authlib")
        from istota.config import SiteConfig, WebConfig
        from istota.webui import app as mod
        from istota.webui import auth as web_auth

        config = Config(
            db_path=db_path, site=SiteConfig(hostname="example.com"),
            web=WebConfig(auth=["email", "nextcloud"]), admin_users={"alice"},
            users={"bob": UserConfig(timezone="UTC", routing={"alert": "whatsapp"})},
        )
        config.whatsapp.enabled = True
        config.whatsapp.provider = "baileys"
        monkeypatch.setattr(mod, "_config", config)
        for name in ("alice", "bob"):
            user_profiles.ensure_profile(db_path, name, display_name=name.title())
        web_auth.upsert_identity(db_path, "alice", "alice@example.com")
        return mod

    def _patch(self, mod):
        admin = {"username": "alice", "display_name": "Alice"}
        return mod._write_admin_profile(
            mod._config, admin, "bob", {"whatsapp_number": "+15550100013"},
        )

    def _ledger(self, db_path):
        with db.get_db(db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sent_whatsapp").fetchall()]

    def test_the_web_route_writes_no_ledger_row(self, web, db_path):
        self._patch(web)
        assert self._ledger(db_path) == []
        assert store.handoffs_pending(web._config)

    def test_the_daemon_sends_it_through_the_bridge(self, web, db_path, monkeypatch):
        sent: list = []

        class Bridge:
            async def send(self, request):
                from istota.transport.whatsapp import baileys_protocol as proto

                sent.append(request)
                return proto.local_failure("test bridge", definite=True)

        self._patch(web)
        monkeypatch.setattr(
            "istota.transport.whatsapp.baileys_bridge.active_bridge", lambda: Bridge(),
        )
        store.deliver_handed_off(web._config)
        assert len(sent) == 1
        assert "WhatsApp number" in sent[0].text


class TestTheConnectedServiceProducerHandsOff:
    """`raise_for_service` runs in the web process too (the Nextcloud token
    refresh, the Health page's Garmin Sync now): same defect, same fix."""

    def test_it_sends_nothing_itself(self, tmp_path, sends):
        from istota.notifications.resolvers import connected_service

        cfg = _config(tmp_path)
        notification_id = connected_service.raise_for_service(
            cfg, "bob", "garmin", "token_expired",
        )
        assert notification_id is not None
        assert sends == []
        assert store.handoffs_pending(cfg)
        store.deliver_handed_off(cfg)
        assert [s["reference_id"] for s in sends] == [f"notification:{notification_id}"]

    def test_the_web_token_loss_writes_no_ledger_row(self, tmp_path):
        from istota.webui import tokens as web_tokens

        cfg = Config(
            db_path=tmp_path / "istota.db",
            users={"bob": UserConfig(timezone="UTC", routing={"alert": "whatsapp"})},
        )
        db.init_db(cfg.db_path)
        cfg.whatsapp.enabled = True
        cfg.whatsapp.provider = "baileys"
        with db.get_db(cfg.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15550100013")
        web_tokens.note_credential_lost(cfg, "bob", "revoked")
        with db.get_db(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sent_whatsapp").fetchone()[0] == 0
        assert store.handoffs_pending(cfg)


def test_two_daemon_passes_racing_take_each_request_once(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    count = []
    lock = threading.Lock()

    def fake(*args, **kwargs):
        with lock:
            count.append(1)
        return True

    monkeypatch.setattr("istota.notifications.delivery.send_notification", fake)
    for i in range(5):
        _write(cfg, key=f"k{i}")
    threads = [threading.Thread(target=store.deliver_handed_off, args=(cfg,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(count) == 5
