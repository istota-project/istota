"""The delivery seam: one logical alert, three content modes, one sender.

`golden/push/legacy_wire.json` is the wire output of a full legacy sweep, taken
before the seam existed. Regenerate it only for an intended change to legacy
delivery: `uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_push_delivery.py -n0`.
"""

import json
from pathlib import Path

import httpx
import pytest

from istota import db
from istota.config import Config, PushConfig
from istota.push import apns, devices, dispatch
from tests.test_prompt_golden import updating

SNAPSHOT = Path(__file__).parent / "golden" / "push" / "legacy_wire.json"
UPDATE_CMD = "uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_push_delivery.py -n0"
NOW = 2_000_000_000.0
BINDING = "0f8e4b0c-2a6d-4c4e-9a43-6d2f1b8e7c51"
_WIRE_HEADERS = ("apns-topic", "apns-push-type", "apns-priority", "apns-expiration", "apns-collapse-id")


def _stamp(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _message(conn, room, when, body):
    db.add_message(conn, room.token, role="assistant", body=body, origin_surface="web")
    message_id = db.room_max_message_id(conn, room.token)
    conn.execute("UPDATE messages SET created_at=? WHERE id=?", (_stamp(when), message_id))
    return message_id


def _notification(conn, user, key, *, title, body, link):
    from istota.notifications import store
    result = store.write_notification(conn, user, source="confirmation", dedup_key=key,
                                     title=title, body=body, link=link)
    conn.execute("UPDATE notifications SET updated_at=? WHERE id=?", (_stamp(NOW), result.notification_id))


@pytest.fixture
def wire(tmp_path, monkeypatch):
    """A push deployment whose APNs is an in-memory transport recording each request."""
    key = tmp_path / "apns.p8"
    key.write_text("unused: the provider token is stubbed")
    push = PushConfig(enabled=True, apns_key_path=str(key), apns_key_id="KEYEXAMPLE",
                      apns_team_id="TEAMEXAMPL", apns_topic="com.example.app")
    config = Config(db_path=tmp_path / "test.db", push=push)
    db.init_db(config.db_path)
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch, "_warned_unavailable", False)
    monkeypatch.setattr(apns, "provider_token", lambda config, force_refresh=False: "jwt")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(apns, "_client", lambda: client)
    yield config, requests
    client.close()


def _recorded(requests, replacements):
    def scrub(text):
        for value, placeholder in replacements.items():
            text = text.replace(value, placeholder)
        return text

    return [{
        "path": request.url.path,
        "headers": {name: scrub(request.headers[name]) for name in _WIRE_HEADERS if name in request.headers},
        "body": scrub(request.content.decode("utf-8")),
    } for request in requests]


def _legacy_sweeps(config, requests):
    """Two users, a bound and an unbound device, previews on and off, then a badge repair."""
    with db.get_db(config.db_path) as conn:
        devices.register_device(conn, "alice", "ab" * 32, "sandbox", auth_method="nextcloud", auth_epoch=0)
        devices.register_device(conn, "alice", "ef" * 32, "production", "ios", "0.12.0",
                                auth_method="nextcloud", auth_epoch=0,
                                connection_binding=BINDING, connection_generation=7)
        devices.register_device(conn, "bob", "cd" * 32, "sandbox", auth_method="nextcloud", auth_epoch=0)
        conn.execute("INSERT INTO user_profiles (user_id, push_preview) VALUES ('bob', 0)")
        work = db.create_web_chat_room(conn, "alice", "Work")
        cafe = db.create_web_chat_room(conn, "alice", "Café ☕ " + "ü" * 120)
        shared = db.create_web_chat_room(conn, "bob", "Shared")
        db.add_web_room_member(conn, shared.token, "alice")
        _notification(conn, "alice", "task:1", title="Approval needed", body="Review the held task", link="/chat")
        _notification(conn, "alice", "task:2", title="🚀" * 100 + "\x00‮", body="z\n\t" * 3000,
                      link="https://example.com/elsewhere")
        _notification(conn, "bob", "task:3", title="Secret subject", body="Secret text", link="/chat")
        _message(conn, work, NOW - 30, "First")
        _message(conn, work, NOW - 29, "## Done\n\n**Deployed** the [fix](https://example.com/run_42).")
        last = _message(conn, cafe, NOW - 28, "Grüße, " + "é" * 400)
        _message(conn, shared, NOW - 27, "Shared reply")
    dispatch.run_push_dispatch(config)
    with db.get_db(config.db_path) as conn:
        db.set_room_read_state(conn, cafe.token, "web", last, "alice")
    dispatch.run_push_dispatch(config)
    return {work.token: "<work>", cafe.token: "<cafe>", shared.token: "<shared>"}


def test_legacy_wire_output_matches_the_snapshot(wire):
    config, requests = wire
    replacements = _legacy_sweeps(config, requests)
    recorded = _recorded(requests, replacements)
    if updating(command=UPDATE_CMD):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert recorded == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
