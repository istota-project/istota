"""One background sweeper for inbox alerts, delayed chat and badge repair.

No write transaction spans APNs I/O. Flags are acknowledged after attempts,
including failures; chat watermarks stop at the delay cutoff. A process killed
between a send and its acknowledgement can repeat that alert on restart.
"""

import copy
import importlib.util
import json
import logging
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from istota import db, user_profiles
from istota.config import Config
from istota.lib.markdown_plain import to_plain_text
from istota.notifications import sources, store
from istota.push import apns, devices, preferences

logger = logging.getLogger(__name__)
_ALERT_TITLE_MAX = 80
_ALERT_BODY_MAX = 180
# Enough source for 180 characters of preview, and a bound on the flattening work.
_PREVIEW_SOURCE_MAX = 4000
_MAX_PAYLOAD_BYTES = 4096
_warned_unavailable = False
_dispatch_lock = threading.Lock()


def _sanitize(text: str, limit: int) -> str:
    # Keep whitespace as separators before removing other control characters.
    clean = "".join(" " if char.isspace() else char for char in text
                    if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    return " ".join(clean.split())[:limit]


def _fit_payload(payload: dict) -> dict:
    """Keep routing data intact; refuse an envelope that cannot fit alone."""
    def size():
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    alert = payload["aps"]["alert"]
    if size() > _MAX_PAYLOAD_BYTES:
        alert["body"] = ""
    while size() > _MAX_PAYLOAD_BYTES and alert["title"]:
        alert["title"] = alert["title"][:-1]
    if size() > _MAX_PAYLOAD_BYTES:
        raise ValueError("Push routing envelope exceeds APNs limit")
    return payload


def _alert_payload(title: str, body: str, badge: int, thread: str, target: dict) -> dict:
    return _fit_payload({
        "aps": {"alert": {"title": _sanitize(title, _ALERT_TITLE_MAX),
                          "body": _sanitize(body, _ALERT_BODY_MAX)},
                "badge": badge, "sound": "default", "thread-id": thread,
                "interruption-level": "active"},
        "istota": target,
    })


def _for_device(payload: dict, device: dict) -> dict:
    """Name the enrolling connection in a generic-shell registration's routing.

    The app refuses a tap whose binding is not its current connection, since a
    room ID alone is not unique across servers. Legacy rows get the payload as is.
    """
    binding = device.get("connection_binding")
    if binding is None or "istota" not in payload:
        return payload
    routed = copy.deepcopy(payload)
    routed["istota"]["connection_binding"] = binding
    routed["istota"]["connection_generation"] = device["connection_generation"]
    return _fit_payload(routed)


def _notification_pushes(rows: list[dict], badge: int, preview: bool, oldest: float):
    for row in rows:
        if row["state"] != "open" or _timestamp(row["updated_at"]) < oldest:
            continue
        target = {"kind": "notification", "notification_id": row["id"]}
        if sources.is_safe_path(row["link"]):
            target["link"] = row["link"]
        title = row["title"] if preview else "Something needs your attention"
        body = row["body"] if preview else ""
        yield _alert_payload(title, body, badge, "notif:" + row["source"], target), None


def _chat_pushes(rows: list[dict], badge: int, preview: bool, oldest: float):
    rooms = {}
    for row in rows:
        if _timestamp(row["created_at"]) >= oldest:
            rooms.setdefault(row["room_token"], []).append(row)
    for token, messages in rooms.items():
        newest = messages[-1]
        name = newest["room_name"] or "Chat"
        title = name if preview else "New message in " + name
        # Stripped before _sanitize truncates, so markup cannot spend the budget.
        body = to_plain_text(newest["body"][:_PREVIEW_SOURCE_MAX]) if preview else ""
        if preview and len(messages) > 1:
            body = f"{len(messages)} new messages: {body}"
        target = {"kind": "chat", "room_token": token}
        if newest["room_id"] is not None:
            target["room_id"] = newest["room_id"]
        collapse = "room:" + token
        yield _alert_payload(title, body, badge, collapse, target), collapse


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _tick_ceiling(conn, delay_s: int, *, now: float | None = None) -> int:
    cutoff = datetime.fromtimestamp(
        (time.time() if now is None else now) - delay_s, timezone.utc,
    ).isoformat()
    return int(conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM messages WHERE julianday(created_at) <= julianday(?)",
        (cutoff,),
    ).fetchone()[0])


def _send(config: Config, device: dict, payload: dict, *, collapse_id=None, priority=10):
    # A removal or account switch during an earlier send must apply before the
    # next alert. APNs itself cannot participate in the registration transaction.
    with db.get_db(config.db_path) as conn:
        current = conn.execute(
            "SELECT 1 FROM push_devices WHERE id = ? AND user_id = ? AND created_at = ?",
            (device["id"], device["user_id"], device["created_at"]),
        ).fetchone()
    if current is None or not devices.registration_authorized(config, device):
        return
    result = apns.send(config.push, device, payload, collapse_id=collapse_id,
                       priority=priority, expiration=0)
    with db.get_db(config.db_path) as conn:
        devices.record_result(conn, device["id"], result)


def _dispatch_user(config: Config, user_id: str, pending: list[dict], ceiling: int, now: float):
    with db.get_db(config.db_path) as conn:
        registered = devices.devices_for_user(conn, user_id)
        if not registered:
            store.clear_push_pending(conn, [], snapshots=pending)
            return
        profile = user_profiles.get_profile(config.db_path, user_id, conn=conn)
        fallback = config.users.get(user_id)
        setting = getattr(profile or fallback, "push_chat_alerts", "alerts")
        preview = getattr(profile or fallback, "push_preview", True)
        state = conn.execute(
            "SELECT last_pushed_message_id FROM push_state WHERE user_id = ?", (user_id,),
        ).fetchone()
        floor = int(state[0]) if state else ceiling
        badge = preferences.push_badge_count(conn, user_id, profile=profile or fallback)
        categories = preferences.stored_inbox_categories(getattr(profile or fallback, "push_inbox_categories", {}))
        chat = [row for row in db.unread_rooms_since(conn, user_id, "web", floor, ceiling)
                if preferences.effective_room_mode(setting, row["push_notifications"]) == "alerts"]
        if chat:
            handles = {}
            for row in chat:
                if row["room_id"] is None and _timestamp(row["created_at"]) >= now - config.push.max_age_seconds:
                    token = row["room_token"]
                    if token not in handles:
                        handles[token] = db.ensure_web_chat_handle(
                            conn, user_id, token, row["room_name"] or "Chat",
                        ).id
                    row["room_id"] = handles[token]
    oldest = now - config.push.max_age_seconds
    # Materialize before sending: malformed content must not partly send a user
    # and then repeat its successful alerts on every subsequent tick.
    enabled = [row for row in pending if categories.get(row["source"], True)]
    alerts = list(_notification_pushes(enabled, badge, preview, oldest))
    if chat:
        alerts.extend(_chat_pushes(chat, badge, preview, oldest))
    for device in registered:
        for payload, collapse in alerts:
            _send(config, device, _for_device(payload, device), collapse_id=collapse)
        if not alerts and device["last_badge"] != badge:
            _send(config, device, {"aps": {"badge": badge}}, collapse_id="badge", priority=5)
    with db.get_db(config.db_path) as conn:
        store.clear_push_pending(conn, [], snapshots=pending)
        conn.execute(
            "INSERT INTO push_state (user_id, last_pushed_message_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_pushed_message_id = "
            "MAX(push_state.last_pushed_message_id, excluded.last_pushed_message_id), "
            "updated_at = excluded.updated_at",
            (user_id, max(floor, ceiling), db.iso_utc_now()),
        )


def run_push_dispatch(config: Config) -> None:
    """Best-effort sweep; a broken user cannot stop another user's delivery."""
    global _warned_unavailable
    if not _dispatch_lock.acquire(blocking=False):
        return
    try:
        if (not config.push.enabled or importlib.util.find_spec("h2") is None
                or not config.push.apns_key_path or not Path(config.push.apns_key_path).is_file()):
            if not _warned_unavailable:
                logger.warning("Native push unavailable: enable push, install h2 and configure the APNs key")
                _warned_unavailable = True
            return
        with db.get_db(config.db_path) as conn:
            if not devices.any_devices(conn):
                return
            now = time.time()
            ceiling = _tick_ceiling(conn, config.push.chat_delay_seconds, now=now)
            pending = store.take_pending_pushes(conn, config.push.alert_batch_limit)
            users = {row[0] for row in conn.execute("SELECT DISTINCT user_id FROM push_devices")}
        grouped = {}
        for row in pending:
            grouped.setdefault(row["user_id"], []).append(row)
        for user_id in sorted(users | grouped.keys()):
            try:
                _dispatch_user(config, user_id, grouped.get(user_id, []), ceiling, now)
            except Exception as exc:
                logger.warning("Native push user sweep failed user=%s: %s", user_id, type(exc).__name__)
    except Exception as exc:
        logger.warning("Native push sweep failed: %s", type(exc).__name__)
    finally:
        _dispatch_lock.release()
