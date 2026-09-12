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
from .providers._types import (
    SmsDeliveryEvent,
    SmsSendFailure,
    SmsSendRequest,
    SmsSendResult,
)
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
#: The same set as a stable sequence and its placeholder run, for the one place
#: it has to reach SQL. A `frozenset` has no order, so binding it directly would
#: produce a different parameter order per process.
_TERMINAL_STATUSES_ORDER = tuple(sorted(_TERMINAL_STATUSES))
_TERMINAL_PLACEHOLDERS = ", ".join("?" * len(_TERMINAL_STATUSES_ORDER))
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
        # The branch that writes the id every parked status is waiting on, so
        # the only one that can drain. A callback racing that write may have
        # taken the row straight to `failed` — whether it parked and was
        # replayed, or landed just after and applied itself — so `record` is
        # what the row says now rather than the provider's accept-time status.
        record = _settle_and_drain(
            config, logical_key, outcome.status,
            provider_message_id=outcome.provider_message_id,
            reported_segments=outcome.reported_segments,
        )
        if record.status in {"failed", "unknown"}:
            _alert_failure(config, record, user_id, task_id)
        return record
    if outcome.opted_out and number:
        set_opt_out(config, number, True)
    record = _settle_and_drain(
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
            # Carrying the id through, because losing it is the same permanent
            # unmatchability the parking exists to prevent: an attempt that
            # raised *after* the adapter accepted has an id, and settling
            # `unknown` without it leaves no later status able to find the row.
            return _set_outcome(
                config, logical_key, "unknown",
                provider_message_id=values.get("provider_message_id"),
            )
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
        changed = conn.execute(
            "UPDATE sent_sms SET status = ?, provider_message_id = COALESCE(?, provider_message_id), "
            "error_code = ?, reported_segments = COALESCE(?, reported_segments), updated_at = ? "
            f"WHERE logical_key = ? AND status NOT IN ({_TERMINAL_PLACEHOLDERS})",
            (status, provider_message_id, error_code, reported_segments, now,
             logical_key, *_TERMINAL_STATUSES_ORDER),
        ).rowcount
        row = conn.execute(
            "SELECT * FROM sent_sms WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        if row is None:
            # No row for this key at all. `_record` would raise on it a line
            # below; saying so here is the honest failure rather than a
            # TypeError out of a logging call.
            raise ValueError(f"no sent_sms row for logical key {logical_key!r}")
        if not changed:
            # Not a transition, so not a transition log line: the row is what
            # it already was. A replayed status can have taken it terminal
            # between the send and this write, and `unknown` overwriting a
            # `failed` the provider reported definitively is the one direction
            # an operator can never recover from.
            logger.info(
                "sms.outbound.settle_refused provider=%s status=%s current=%s",
                row["provider"], status, row["status"],
            )
        elif previous is not None and previous["status"] != status:
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
    """Write one durable alert and push it only through non-SMS destinations.

    The push itself is `transport._alerts.push_off_surface`, shared with the
    WhatsApp surface: the descriptor construction is the comma-list grammar
    `send_notification` parses, and two surfaces spelling it differently would
    route to the wrong place rather than fail. Only the row is written here,
    because only this module knows what an SMS failure means.
    """
    from .._alerts import push_off_surface

    with db.get_db(config.db_path) as conn:
        raised = _write_failure_alert(conn, record, user_id, task_id)
    push_off_surface(
        config, raised, exclude_surface="sms", reference_prefix="sms-failure",
    )


_ALERT_LABELS = {
    "blocked_opt_out": "blocked by opt-out",
    "unconfigured": "unconfigured",
    "unknown": "delivery unknown",
    "failed": "failed",
}


def _write_failure_alert(conn, record, user_id: str, task_id: int | None):
    """One durable alert row about an SMS that reached nobody.

    The row is `transport._alerts.write_delivery_failure`, shared with the
    WhatsApp surface the way `push_off_surface` already is: the two were the
    same sentence, the same `task #N` fallback and the same hashed dedup key,
    and only the label table genuinely differs. The body gains one word
    ("The SMS message for…" rather than "The SMS for…") so that one sentence
    reads on both surfaces; nothing asserts on it.
    """
    from .._alerts import write_delivery_failure

    return write_delivery_failure(
        conn,
        surface_label="SMS",
        dedup_prefix="sms",
        user_id=user_id,
        task_id=task_id,
        logical_key=record.logical_key,
        status=record.status,
        labels=_ALERT_LABELS,
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


#: How long a parked status may wait for the id it names. The race it covers is
#: the gap between the adapter's reply and the `provider_message_id` write one
#: statement later, so the real window is sub-second. What it is sized against
#: is the other end: a send killed between its claim and its settle leaves an
#: in-flight row nothing will ever clear, and while one exists every unmatched
#: status for that provider parks.
PARKED_STATUS_WINDOW_SECONDS = 15 * 60

#: The longest provider message id that may be stored. The inbound branch of
#: `handle_provider_event` has always bounded this field; the delivery branch
#: validated nothing, which did not matter while an unmatched id was discarded
#: and does once it becomes a durable row on a uniquely-indexed column. Named
#: here rather than left as a second literal so the two branches cannot drift.
MAX_PROVIDER_MESSAGE_ID = 255


def delivery_may_park(conn, event) -> bool:
    """Whether this callback could reach the park, and so needs the write lock.

    The cheap half of a double check, and it is about cost rather than
    correctness. Parking rests on a lookup that found no row, so that lookup
    and the park have to sit under one exclusive transaction or the outcome
    write can commit the id between them. Taking `BEGIN IMMEDIATE` for *every*
    callback would pay for that on the common ones too: a provider sends three
    or four statuses per message and all but the first find their row, and
    before this they took no write lock at all — a `delivery_stale` was one
    `SELECT` in autocommit. The route calls this synchronously on the event
    loop with a 30s busy timeout, so a lock held across the daemon's other
    writers is a stalled receiver rather than a slow query.

    So: ask without the lock, and take it only when the answer says a park is
    possible. The authoritative read is `apply_delivery_event`'s own, which
    then happens *under* the lock — a row appearing in between is found there
    and applied normally, and a row still absent is parked safely. The only
    cost of a stale `True` is an exclusive transaction that turns out not to
    need one.
    """
    return conn.execute(
        "SELECT 1 FROM sent_sms WHERE provider = ? AND provider_message_id = ?",
        (event.provider, event.provider_message_id),
    ).fetchone() is None


def _send_in_flight(conn, provider: str) -> bool:
    """Whether a send on this provider sits between its claim and its settle.

    That interval is what `status = 'pending'` with a claim stamped and no
    provider id means: the claim commits `claimed_at` before the adapter is
    called, and `_set_outcome` is what moves the row off `pending` and writes
    the id. An unclaimed `pending` row is not in flight and does not count.

    This is the whole bound on parking. A provider cannot produce a status for
    a message before we sent it, and the claim is committed before we send, so
    any status belonging to one of our unsettled sends finds a row here; one
    that finds none cannot be ours and is discarded as before. Scoped by
    provider, which this ledger can do and the WhatsApp one cannot — the event
    names its provider and the row's key is the pair.

    Sound only because the caller holds `BEGIN IMMEDIATE` across its own row
    lookup and the park, which is what stops `_set_outcome`'s write landing
    between the two.
    """
    return conn.execute(
        "SELECT 1 FROM sent_sms WHERE provider = ? AND status = 'pending' "
        "AND claimed_at IS NOT NULL AND provider_message_id IS NULL LIMIT 1",
        (provider,),
    ).fetchone() is not None


def _prune_parked_statuses(conn) -> int:
    """Drop every parked status past the window, and say how many there were.

    Called on both paths that touch the table — an attempted park and a drain
    — and on the attempt *before* its gate, so a quiet deployment still clears
    itself: a foreign status arriving with nothing in flight prunes and then
    declines to park. Behind the gate the only thing clearing the table would
    be a successful park, which is exactly the deployment that has gone quiet
    keeping its rows for good.

    A pruned row is a status istota held and could not place. Two causes, and
    they are logged apart because only one is actionable: a row whose id still
    matches no `sent_sms` was never ours, which is what a number shared with
    another application looks like, while a row whose id *does* match is a
    status we parked, failed to drain and have now lost — the defect this
    table exists to prevent, reappearing by another route.
    """
    window = (f"-{PARKED_STATUS_WINDOW_SECONDS} seconds",)
    matched = conn.execute(
        "SELECT count(*) FROM sms_parked_status AS p "
        "WHERE p.parked_at < datetime('now', ?) AND EXISTS ("
        "  SELECT 1 FROM sent_sms AS s WHERE s.provider = p.provider "
        "  AND s.provider_message_id = p.provider_message_id)",
        window,
    ).fetchone()[0]
    dropped = conn.execute(
        "DELETE FROM sms_parked_status WHERE parked_at < datetime('now', ?)",
        window,
    ).rowcount
    if matched:
        logger.warning(
            "sms.delivery.parked_lost count=%d: statuses whose row exists and "
            "which were never drained -- a delivery outcome has been lost",
            matched,
        )
    if dropped - matched > 0:
        logger.info(
            "sms.delivery.parked_expired count=%d: statuses for provider "
            "message ids no send of ours ever claimed",
            dropped - matched,
        )
    return dropped


def _park_status(conn, event) -> bool:
    """Hold one status whose provider message id has not been written yet.

    Returns whether it was parked. `False` means no send was in flight on this
    provider, so the id cannot become ours and the caller discards the status.

    The upsert merges rather than keeping the first write, matching what
    `apply_delivery_event` does to a row it can reach: a redelivered batch
    carries nothing new, but two genuine callbacks sharing a status — a
    `failed` seen first without its error code — are not a redelivery.
    `opted_out` is OR-ed rather than overwritten, since it is a request that
    was made and a later callback omitting it does not withdraw it.
    """
    # Housekeeping first, so a refusal below still clears the window.
    _prune_parked_statuses(conn)
    if (
        not event.provider_message_id
        or len(event.provider_message_id) > MAX_PROVIDER_MESSAGE_ID
    ):
        # An id this shape can match no `sent_sms` row — the column is NULL
        # there, never empty — so parking it would write a row that only ever
        # expires, keyed on a provider-chosen string of unbounded length.
        logger.warning(
            "sms.delivery.unparkable provider=%s reason=message_id_shape",
            event.provider,
        )
        return False
    if not _send_in_flight(conn, event.provider):
        return False
    conn.execute(
        "INSERT INTO sms_parked_status (provider, provider_message_id, "
        "provider_event_id, status, error_code, reported_segments, opted_out, "
        "parked_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(provider, provider_message_id, status) DO UPDATE SET "
        "  provider_event_id = COALESCE(excluded.provider_event_id, provider_event_id), "
        "  error_code = COALESCE(excluded.error_code, error_code), "
        "  reported_segments = COALESCE(excluded.reported_segments, reported_segments), "
        "  opted_out = CASE WHEN opted_out = 1 OR excluded.opted_out = 1 THEN 1 ELSE 0 END",
        (
            event.provider, event.provider_message_id, event.provider_event_id,
            event.status, event.error_code, event.reported_segments,
            1 if event.opted_out else 0,
        ),
    )
    logger.info(
        "sms.delivery.parked provider=%s provider_message_id=%s status=%s",
        event.provider, event.provider_message_id, event.status,
    )
    return True


def _parked_event(row) -> SmsDeliveryEvent:
    """One parked row as the event it was.

    A real `SmsDeliveryEvent` rather than a stand-in, because every field the
    dataclass carries is stored — there is nothing to invent.
    """
    return SmsDeliveryEvent(
        provider=row["provider"],
        provider_event_id=row["provider_event_id"],
        provider_message_id=row["provider_message_id"],
        status=row["status"],
        error_code=row["error_code"],
        reported_segments=row["reported_segments"],
        opted_out=bool(row["opted_out"]),
    )


def _drain_parked_statuses(config: Config, provider: str, provider_message_id: str):
    """Replay the statuses parked for an id that has just been written.

    Returns the record the last replayed status left, or `None` when nothing
    was parked, and the alerts they owe — buffered rather than pushed, since
    this holds a write transaction and `.claude/rules/notifications.md` gives
    the reason a push from inside one waits out its own lock.

    Its own transaction, opened here rather than shared with `_set_outcome`.
    See `_settle_and_drain` for the ordering that makes that safe and for what
    sharing one would cost.

    Replayed through `apply_delivery_event`, never written straight to the row,
    which is what keeps a parked status from resurrecting a settled one: the
    ladder refuses a row already in `_TERMINAL_STATUSES`, so a `failed` drained
    ahead of a `delivered` parked after it closes the row and the `delivered`
    is a duplicate. It is also what performs the opt-out a parked status may
    carry, which a direct write would drop.
    """
    with db.get_db(config.db_path) as conn:
        _prune_parked_statuses(conn)
        rows = conn.execute(
            "SELECT * FROM sms_parked_status WHERE provider = ? "
            "AND provider_message_id = ? ORDER BY id",
            (provider, provider_message_id),
        ).fetchall()
        if not rows:
            return None, ()
        conn.execute(
            "DELETE FROM sms_parked_status WHERE provider = ? "
            "AND provider_message_id = ?",
            (provider, provider_message_id),
        )
        record = None
        alerts = []
        for row in rows:
            _disposition, applied, raised = apply_delivery_event(
                conn, _parked_event(row),
            )
            if applied is not None:
                record = applied
            if raised is not None:
                alerts.append(raised)
        logger.info(
            "sms.delivery.drained provider=%s provider_message_id=%s statuses=%d",
            provider, provider_message_id, len(rows),
        )
    return record, tuple(alerts)


def _settle_and_drain(
    config: Config, logical_key: str, status: str, **values,
) -> SmsDeliveryRecord:
    """Write one row's outcome, then replay whatever was waiting on its id.

    Two transactions, in this order, and the order is the whole of it. The
    outcome commits on its own, so no failure in the replay can unwind the
    `provider_message_id` write; the replay then runs against a row that
    already holds the id, which is what lets it go through
    `apply_delivery_event` unchanged.

    Sharing one transaction reads better and is wrong. `db.get_db` commits only
    on a clean exit, so a raise out of any replay — a notification write, an
    opt-out insert, a parked table a half-upgraded host has not created yet —
    would unwind the id write with it, and a message the provider accepted
    whose id was never recorded can never be matched by a later status.

    Nothing between the two transactions can lose a status: a park happens only
    under the delivery branch's `BEGIN IMMEDIATE`, which holds the write lock
    across its own row lookup and the park, and the outcome write needs that
    same lock. So a callback either commits its park before the outcome, and
    this drains it, or does its lookup after and finds the row.

    The replay is never allowed to raise past here, for the reason
    `_alert_failure` never does: a delivery path must not fail over the notice
    about it.
    """
    record = _persist_outcome_or_unknown(config, logical_key, status, **values)
    if not record.provider_message_id:
        return record
    try:
        drained, alerts = _drain_parked_statuses(
            config, record.provider, record.provider_message_id,
        )
    except Exception:
        logger.warning("sms.outbound.drain_failed", exc_info=True)
        return record
    if alerts:
        _push_drained_alerts(config, alerts)
    if drained is not None:
        return drained
    # Nothing was parked, which does not mean nothing happened: on the other
    # interleaving the callback did its lookup *after* the outcome committed,
    # found the row and applied `failed` to it directly. `record` was read
    # before that and would report the provider's accept-time status, so the
    # caller would not raise its failure alert. One more read makes the
    # returned record current whichever order it was.
    current = _existing(config, logical_key)
    return _record(current) if current is not None else record


def _push_drained_alerts(config: Config, alerts) -> None:
    """Push the alerts a replayed status owed, once the outcome has committed.

    **Off the SMS route, which is not the same call `deliver_event_response`
    makes.** That one uses `deliver_pending` and so can deliver "your SMS
    failed" over SMS, to the number whose message just failed — the loop
    `transport/_alerts.py` exists to break, and `_raise_failure_alert` a few
    lines up already obeys. Copying it here would have been worse than
    inconsistent: for a drained failure this push is the *only* one that
    happens. `_deliver_sms_blocking` calls `_alert_failure` immediately
    afterwards with the same logical key, so `write_delivery_failure` bumps
    rather than inserts, `raised.deliver` is False, and the correctly-excluded
    push returns without sending. The webhook's copy is the same defect and is
    fixed with it.

    Never raises: the caller is on a delivery path whose contract is that a
    failed alert is not a failed send.
    """
    from .._alerts import push_off_surface

    for alert in alerts:
        try:
            push_off_surface(
                config, alert,
                exclude_surface="sms", reference_prefix="sms-failure",
            )
        except Exception:
            logger.warning("sms.delivery.alert_not_delivered", exc_info=True)


def apply_delivery_event(conn, event):
    """``(disposition, record, pending_alert)`` for one provider callback.

    The alert is *returned* rather than delivered: this runs inside the
    caller's open write transaction, and a push from in here would open a
    second connection against the lock this one holds. The caller sends it
    after the commit.

    Takes a replayed status as readily as a live one — `_parked_event` rebuilds
    the real `SmsDeliveryEvent`, so the replay is this code path rather than a
    second account of the rules below.
    """
    order = {"pending": 0, "accepted": 1, "queued": 2, "sent": 3, "delivered": 4}
    now = _now()
    entered_failed = False
    row = conn.execute(
        "SELECT * FROM sent_sms WHERE provider = ? AND provider_message_id = ?",
        (event.provider, event.provider_message_id),
    ).fetchone()
    if row is None:
        # The provider mints the message id in its reply to the send, so a
        # callback for one of our own messages can arrive before
        # `_set_outcome` has written it. Held rather than discarded while that
        # is possible, because a `failed` dropped here leaves the row reporting
        # whatever the provider said at accept time and raises no alert;
        # `_settle_and_drain` replays it the moment the id lands.
        #
        # Guarded, and the guard is the point rather than caution: this branch
        # used to be a bare return, so it could not fail, and it now writes to
        # a table `init_db` may not have created yet on a half-upgraded
        # deployment. The route turns any `sqlite3.Error` into a 503 the
        # provider retries into the same deterministic failure, so a park that
        # cannot happen falls through to the discard that was the only
        # behaviour before.
        try:
            parked = _park_status(conn, event)
        except sqlite3.Error:
            logger.warning("sms.delivery.park_failed", exc_info=True)
            parked = False
        if parked:
            return "delivery_parked", None, None
        # Acknowledged and bounded, and now said out loud: this branch was
        # silent, so a number carrying another application's traffic produced
        # no signal at all. The id is logged in full, as every other line in
        # this module logs it.
        logger.warning(
            "sms.delivery.unknown_message provider=%s provider_message_id=%s "
            "status=%s",
            event.provider, event.provider_message_id, event.status,
        )
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
