"""Provider-neutral SMS rendering and ledger-backed outbound delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import sqlite3

from ... import db
from ...config import Config
from ...timestamps import iso_now as _now
from ._types import RenderedSms, SmsDeliveryRecord
from .providers._types import SmsSendFailure, SmsSendRequest, SmsSendResult
from .providers.registry import SmsProviderRegistry

logger = logging.getLogger(__name__)

_TRUNCATION_SUFFIX = "\n\n[Reply shortened. Send a narrower follow-up.]"
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")
_TERMINAL_STATUSES = frozenset({
    "delivered", "delivery_unconfirmed", "failed", "blocked_opt_out",
    "unconfigured", "unknown",
})
_TASK_LOG_MESSAGES = {
    "accepted": "SMS accepted by provider",
    "queued": "SMS queued by provider",
    "sent": "SMS sent by provider",
    "delivered": "SMS delivered",
    "delivery_unconfirmed": "SMS delivery unconfirmed",
    "failed": "SMS delivery failed",
    "blocked_opt_out": "SMS blocked by opt-out",
    "unconfigured": "SMS delivery unconfigured",
    "unknown": "SMS delivery unknown",
}


def _clean_text(text: str) -> str:
    safe = "".join(
        "�" if ch == "\x00" or 0xD800 <= ord(ch) <= 0xDFFF else ch
        for ch in str(text)
    )
    lines: list[str] = []
    for line in safe.splitlines():
        if re.fullmatch(r"\s*`{3,}.*", line):
            continue
        if re.fullmatch(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*", line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = line.replace("**", "").replace("__", "")
        line = line.replace("`", "").replace("*", "")
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _gsm7_units(text: str) -> int | None:
    units = 0
    for char in text:
        if char in _GSM7_BASIC:
            units += 1
        elif char in _GSM7_EXTENSION:
            units += 2
        else:
            return None
    return units


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _units(text: str, encoding: str) -> int:
    if encoding == "gsm7":
        value = _gsm7_units(text)
        if value is None:
            raise ValueError("text no longer fits GSM-7")
        return value
    return _utf16_units(text)


def _segment_count(units: int, encoding: str) -> int:
    single = 160 if encoding == "gsm7" else 70
    joined = 153 if encoding == "gsm7" else 67
    return 1 if units <= single else math.ceil(units / joined)


def _truncate(text: str, encoding: str, budget: int) -> str:
    available = max(0, budget - _units(_TRUNCATION_SUFFIX, encoding))
    kept: list[str] = []
    used = 0
    for char in text:
        char_units = _units(char, encoding)
        if used + char_units > available:
            break
        kept.append(char)
        used += char_units
    return "".join(kept).rstrip() + _TRUNCATION_SUFFIX


def render_sms(text: str, max_segments: int) -> RenderedSms:
    """Sanitize one body and cap it to the configured carrier segment budget."""
    if not 1 <= max_segments <= 10:
        raise ValueError("max_segments must be between 1 and 10")
    cleaned = _clean_text(text)
    gsm_units = _gsm7_units(cleaned)
    encoding = "gsm7" if gsm_units is not None else "ucs2"
    units = gsm_units if gsm_units is not None else _utf16_units(cleaned)
    single = 160 if encoding == "gsm7" else 70
    joined = 153 if encoding == "gsm7" else 67
    budget = single if max_segments == 1 else joined * max_segments
    if units > budget:
        cleaned = _truncate(cleaned, encoding, budget)
        units = _units(cleaned, encoding)
    return RenderedSms(cleaned, encoding, _segment_count(units, encoding))

def _record(row) -> SmsDeliveryRecord:
    return SmsDeliveryRecord(
        logical_key=row["logical_key"], provider=row["provider"],
        status=row["status"], provider_message_id=row["provider_message_id"],
        error_code=row["error_code"], estimated_segments=row["estimated_segments"],
        reported_segments=row["reported_segments"],
    )


def _existing(config: Config, logical_key: str):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sent_sms WHERE logical_key = ?", (logical_key,),
        ).fetchone()
    return row


async def deliver_sms(
    config: Config,
    providers: SmsProviderRegistry,
    *,
    logical_key: str,
    user_id: str,
    text: str,
    task_id: int | None = None,
    preferred_from_number: str | None = None,
) -> SmsDeliveryRecord:
    """Claim and perform one logical send, off whatever loop awaits it.

    Every step below is synchronous SQLite plus one blocking provider call, and
    two callers (`scheduler.process_one_task` and `notifications._dispatch`)
    submit this to the process-global runtime loop through ``run_coro`` — the
    same loop the Talk poller runs on. Opening a connection there would wait on
    the WAL write lock from inside the loop thread, so the coroutine holding
    that lock could never be resumed and the whole runtime would stall until
    the 30s busy timeout expired. `WebTransport.deliver` hands its write to an
    executor for this reason; this does the same for all of them at once.
    """
    return await asyncio.to_thread(
        _deliver_sms_blocking, config, providers,
        logical_key=logical_key, user_id=user_id, text=text, task_id=task_id,
        preferred_from_number=preferred_from_number,
    )


def _deliver_sms_blocking(
    config: Config,
    providers: SmsProviderRegistry,
    *,
    logical_key: str,
    user_id: str,
    text: str,
    task_id: int | None = None,
    preferred_from_number: str | None = None,
) -> SmsDeliveryRecord:
    """The body of :func:`deliver_sms`, on a worker thread. Never on the loop."""
    previous = _existing(config, logical_key)
    if previous is not None and (
        previous["status"] != "pending"
        or previous["claimed_at"] is not None
        or previous["attempted_at"] is not None
    ):
        return _record(previous)

    rendered = render_sms(text, config.sms.max_segments)
    number = config.sms_phone_number_for(user_id)
    sender = (
        preferred_from_number
        if preferred_from_number in config.sms.service_numbers
        else config.sms.default_sender_number
    )
    status = "pending"
    if not number:
        status = "unconfigured"
    elif is_opted_out(config, number):
        status = "blocked_opt_out"

    now = _now()
    active = providers.active()
    provider_name = active.name if active is not None else config.sms.provider
    with db.get_db(config.db_path) as conn:
        if previous is None:
            try:
                conn.execute(
                    "INSERT INTO sent_sms (logical_key, provider, user_id, task_id, "
                    "to_number, from_number, status, estimated_segments, body_chars, "
                    "body_sha256, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        logical_key, provider_name, user_id, task_id, number or "", sender,
                        status, rendered.estimated_segments, len(rendered.text),
                        hashlib.sha256(rendered.text.encode()).hexdigest(), now, now,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM sent_sms WHERE logical_key = ?", (logical_key,),
                ).fetchone()
                return _record(row)
        else:
            conn.execute(
                "UPDATE sent_sms SET to_number = ?, from_number = ?, status = ?, "
                "updated_at = ? WHERE logical_key = ? AND status = 'pending' "
                "AND claimed_at IS NULL AND attempted_at IS NULL",
                (number or "", sender, status, now, logical_key),
            )
        if status != "pending":
            row = conn.execute(
                "SELECT * FROM sent_sms WHERE logical_key = ?", (logical_key,),
            ).fetchone()
            blocked_record = _record(row)
        else:
            blocked_record = None
    if blocked_record is not None:
        _log_transition(config, blocked_record, task_id)
        _alert_failure(config, blocked_record, user_id, task_id)
        return blocked_record

    with db.get_db(config.db_path) as conn:
        claimed = conn.execute(
            "UPDATE sent_sms SET provider = ?, to_number = ?, from_number = ?, "
            "claimed_at = ?, updated_at = ? "
            "WHERE logical_key = ? AND status = 'pending' "
            "AND claimed_at IS NULL AND attempted_at IS NULL",
            (provider_name, number, sender, now, now, logical_key),
        ).rowcount
        row = conn.execute(
            "SELECT * FROM sent_sms WHERE logical_key = ?", (logical_key,),
        ).fetchone()
    if not claimed:
        return _record(row)

    adapter = providers.get(row["provider"])
    if adapter is None:
        record = _set_outcome(config, logical_key, "unknown")
        _alert_failure(config, record, user_id, task_id)
        return record
    number = config.sms_phone_number_for(user_id)
    if not number:
        record = _set_outcome(config, logical_key, "unconfigured")
        _alert_failure(config, record, user_id, task_id)
        return record
    if is_opted_out(config, number):
        record = _set_outcome(config, logical_key, "blocked_opt_out")
        _alert_failure(config, record, user_id, task_id)
        return record
    sender = (
        preferred_from_number
        if preferred_from_number in config.sms.service_numbers
        else config.sms.default_sender_number
    )
    attempted = _now()
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE sent_sms SET to_number = ?, from_number = ?, attempted_at = ?, "
            "updated_at = ? WHERE logical_key = ?",
            (number, sender, attempted, attempted, logical_key),
        )
    request = SmsSendRequest(
        to_number=number,
        preferred_from_number=sender,
        text=rendered.text,
        encoding=rendered.encoding,
        status_callback_url=f"https://{config.site.hostname}/webhooks/sms/{adapter.name}",
    )
    try:
        outcome = adapter.send(request)
    except Exception:
        outcome = SmsSendFailure(False, None, False, "delivery outcome unknown")
    if isinstance(outcome, SmsSendResult):
        record = _persist_outcome_or_unknown(
            config, logical_key, outcome.status,
            provider_message_id=outcome.provider_message_id,
            reported_segments=outcome.reported_segments,
        )
        if record.status in {"failed", "unknown"}:
            _alert_failure(config, record, user_id, task_id)
        return record
    if outcome.opted_out and number:
        set_opt_out(config, number, True)
    record = _persist_outcome_or_unknown(
        config, logical_key, "failed" if outcome.definite else "unknown",
        error_code=outcome.error_code,
    )
    _alert_failure(config, record, user_id, task_id)
    return record


def _alert_failure(
    config: Config,
    record: SmsDeliveryRecord,
    user_id: str,
    task_id: int | None,
) -> None:
    """Raise the failure alert. Sync: its only caller already runs off-loop."""
    try:
        _raise_failure_alert(config, record, user_id, task_id)
    except Exception:
        logger.warning(
            "sms.outbound.alert_failed logical_key_hash=%s task_id=%s",
            hashlib.sha256(record.logical_key.encode()).hexdigest()[:16], task_id,
            exc_info=True,
        )


def _persist_outcome_or_unknown(
    config: Config,
    logical_key: str,
    status: str,
    **values,
) -> SmsDeliveryRecord:
    try:
        return _set_outcome(config, logical_key, status, **values)
    except Exception:
        logger.warning(
            "sms.outbound.unknown logical_key_hash=%s",
            hashlib.sha256(logical_key.encode()).hexdigest()[:16],
            exc_info=True,
        )
        try:
            return _set_outcome(config, logical_key, "unknown")
        except Exception:
            previous = _existing(config, logical_key)
            if previous is not None:
                return SmsDeliveryRecord(
                    logical_key=logical_key, provider=previous["provider"],
                    status="unknown",
                    provider_message_id=previous["provider_message_id"],
                    error_code=previous["error_code"],
                    estimated_segments=previous["estimated_segments"],
                    reported_segments=previous["reported_segments"],
                )
            raise


def _set_outcome(
    config: Config,
    logical_key: str,
    status: str,
    *,
    provider_message_id: str | None = None,
    error_code: str | None = None,
    reported_segments: int | None = None,
) -> SmsDeliveryRecord:
    now = _now()
    with db.get_db(config.db_path) as conn:
        previous = conn.execute(
            "SELECT status, task_id FROM sent_sms WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        conn.execute(
            "UPDATE sent_sms SET status = ?, provider_message_id = COALESCE(?, provider_message_id), "
            "error_code = ?, reported_segments = COALESCE(?, reported_segments), updated_at = ? "
            "WHERE logical_key = ?",
            (status, provider_message_id, error_code, reported_segments, now, logical_key),
        )
        row = conn.execute(
            "SELECT * FROM sent_sms WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        if previous is not None and previous["status"] != status:
            _log_transition_on_connection(conn, _record(row), previous["task_id"])
    return _record(row)


def _log_transition_on_connection(conn, record: SmsDeliveryRecord, task_id) -> None:
    message = _TASK_LOG_MESSAGES.get(record.status)
    if task_id is not None and message is not None:
        db.log_task(conn, task_id, "error" if record.status == "failed" else "info", message)
    logger.info(
        "sms.outbound.%s provider=%s task_id=%s provider_message_id=%s error_code=%s",
        record.status, record.provider, task_id,
        record.provider_message_id, record.error_code,
    )


def _log_transition(config: Config, record: SmsDeliveryRecord, task_id) -> None:
    with db.get_db(config.db_path) as conn:
        _log_transition_on_connection(conn, record, task_id)


def is_opted_out(config: Config, phone_number: str) -> bool:
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sms_opt_outs WHERE phone_number = ?", (phone_number,),
        ).fetchone()
    return row is not None


def set_opt_out(config: Config, phone_number: str, opted_out: bool) -> None:
    now = _now()
    with db.get_db(config.db_path) as conn:
        if opted_out:
            conn.execute(
                "INSERT INTO sms_opt_outs (phone_number, opted_out_at, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(phone_number) DO UPDATE SET updated_at = excluded.updated_at",
                (phone_number, now, now),
            )
        else:
            conn.execute("DELETE FROM sms_opt_outs WHERE phone_number = ?", (phone_number,))


def _raise_failure_alert(
    config: Config,
    record: SmsDeliveryRecord,
    user_id: str,
    task_id: int | None,
) -> None:
    """Write one durable alert and push it only through non-SMS destinations."""
    from ... import notifications
    from ...notification_store import mark_delivered

    with db.get_db(config.db_path) as conn:
        raised = _write_failure_alert(conn, record, user_id, task_id)
    if raised is None or not raised.deliver:
        return
    dests = [
        dest for dest in notifications.resolve_destinations(config, user_id, "alert")
        if dest.surface != "sms"
    ]
    descriptor = ",".join(
        dest.surface if dest.channel is None else f"{dest.surface}:{dest.channel}"
        for dest in dests
    )
    if not descriptor:
        return
    if notifications.send_notification(
        config, user_id, raised.text, surface=descriptor, title=raised.title,
        reference_id=f"sms-failure:{raised.notification_id}",
    ):
        with db.get_db(config.db_path) as conn:
            mark_delivered(conn, [raised.notification_id])


def _write_failure_alert(conn, record, user_id: str, task_id: int | None):
    from ...notification_resolvers import task_alert

    label = {
        "blocked_opt_out": "blocked by opt-out",
        "unconfigured": "unconfigured",
        "unknown": "delivery unknown",
        "failed": "failed",
    }.get(record.status)
    if label is None:
        return None
    task_label = f"task #{task_id}" if task_id is not None else "a notification"
    title = f"SMS delivery {label} — {task_label}"
    body = f"The SMS for {task_label} was not delivered. Its state is {label}."
    dedup_key = f"sms:{hashlib.sha256(record.logical_key.encode()).hexdigest()[:24]}"
    return task_alert.write(
        conn, user_id, dedup_key=dedup_key, title=title, body=body,
        params={"task_id": task_id, "status": record.status},
    )


def is_sms_configured(
    config: Config,
    user_id: str,
    providers: SmsProviderRegistry | None = None,
) -> bool:
    if not config.sms.enabled:
        return False
    if providers is None:
        try:
            from .providers.registry import make_provider_registry
            providers = make_provider_registry(config)
        except (ImportError, ValueError):
            return False
    if providers.active() is None:
        return False
    number = config.sms_phone_number_for(user_id)
    return bool(number and not is_opted_out(config, number))


def apply_delivery_event(conn, event):
    """``(disposition, record, pending_alert)`` for one provider callback.

    The alert is *returned* rather than delivered: this runs inside the
    caller's open write transaction, and a push from in here would open a
    second connection against the lock this one holds. The caller sends it
    after the commit.
    """
    """Apply one canonical callback without regressing or reopening a row."""
    order = {"pending": 0, "accepted": 1, "queued": 2, "sent": 3, "delivered": 4}
    now = _now()
    entered_failed = False
    row = conn.execute(
        "SELECT * FROM sent_sms WHERE provider = ? AND provider_message_id = ?",
        (event.provider, event.provider_message_id),
    ).fetchone()
    if row is None:
        return "delivery_unknown", None, None
    current = row["status"]
    if current in _TERMINAL_STATUSES:
        return "delivery_duplicate", _record(row), None
    terminal_next = event.status in {"failed", "delivery_unconfirmed"}
    if not terminal_next and order.get(event.status, -1) <= order.get(current, -1):
        return "delivery_stale", _record(row), None
    conn.execute(
        # COALESCE like its neighbours: a `failed` row that later takes any
        # other write would otherwise lose the public error code the task alert
        # and the admin view read.
        "UPDATE sent_sms SET status = ?, error_code = COALESCE(?, error_code), "
        "reported_segments = COALESCE(?, reported_segments), "
        "provider_event_id = COALESCE(?, provider_event_id), updated_at = ? "
        "WHERE id = ?",
        (
            event.status, event.error_code, event.reported_segments,
            event.provider_event_id, now, row["id"],
        ),
    )
    updated = conn.execute("SELECT * FROM sent_sms WHERE id = ?", (row["id"],)).fetchone()
    entered_failed = event.status == "failed"
    if event.opted_out:
        conn.execute(
            "INSERT INTO sms_opt_outs (phone_number, opted_out_at, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(phone_number) DO UPDATE SET updated_at = excluded.updated_at",
            (updated["to_number"], now, now),
        )
    record = _record(updated)
    _log_transition_on_connection(conn, record, updated["task_id"])
    logger.info(
        "sms.delivery.updated provider=%s task_id=%s provider_message_id=%s "
        "provider_event_id=%s status=%s error_code=%s",
        event.provider, updated["task_id"], event.provider_message_id,
        event.provider_event_id, event.status, event.error_code,
    )
    pending_alert = None
    if entered_failed:
        pending_alert = _write_failure_alert(
            conn, record, updated["user_id"], updated["task_id"],
        )
    return "delivery_updated", record, pending_alert
