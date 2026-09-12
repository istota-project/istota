"""Rendering, the service window, the outbound ledger and delivery state.

Everything between a finished answer and a Cloud API call, and the rule the
whole module is shaped by: **one Meta message per logical output, and no
automatic resend, ever**. The Cloud API has no application idempotency token,
so a timeout cannot be settled by asking — a resend would either duplicate a
private answer or duplicate a confirmation prompt, and both are worse than a
visible `unknown` an operator can read.

That gives the order of operations. A row is inserted `pending` and *committed*
before anything reaches the network, `claimed_at` is set by a conditional
update so two workers cannot both take one logical output, `attempted_at` is
stamped immediately before the call, and the outcome is written back. A crash
anywhere after the claim leaves a row that no later call will send again.

The gates in front of that are ordered cheapest-and-most-certain first, and the
order is not cosmetic: a user who opted out must be refused before a closed
window is reported about them, because the second is an operator alert and the
first is the user's own choice.

Two things are deliberately absent and belong to stage 4: the monthly service
attempt reservation (`quota_month` stays NULL here) and the pricing
observation that trips the billable circuit. The circuit is *read* here, since
"is this surface usable for this user" is a question this stage's notification
probe has to answer; only the write is stage 4's.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

from ... import db
from ...config import Config
from ._types import (
    LOCAL_TERMINAL_STATES,
    WhatsAppDeliveryEvent,
    WhatsAppDeliveryRecord,
    WhatsAppSendRequest,
    WhatsAppSendResult,
)

logger = logging.getLogger(__name__)

#: Meta's own cap on a text message body, in Unicode characters.
WHATSAPP_TEXT_LIMIT = 4096

#: What one body parameter of an approved utility template may carry. Well
#: under Meta's own limit, because the surrounding fixed template text counts
#: too and istota does not know how long the operator's wording is.
TEMPLATE_PARAMETER_LIMIT = 900

#: Meta allows a free-form service message within 24 hours of the user's last
#: message. Five minutes of that is given back: the stored timestamp is Meta's
#: clock rather than ours, the send happens some time after the decision, and a
#: send Meta refuses at the boundary is a wasted call with a scary error code.
SERVICE_WINDOW = timedelta(hours=23, minutes=55)

TRUNCATION_SUFFIX = "\n\n[Reply shortened. Send a narrower follow-up.]"

#: The row states no automatic path may leave. `LOCAL_TERMINAL_STATES` holds
#: the six istota decided on its own; `failed` is Meta's, and the four that
#: mean Meta took the message are here for the same reason — a row that reached
#: the provider must never be offered to the provider a second time.
_NO_RESEND = frozenset(LOCAL_TERMINAL_STATES) | {
    "failed", "accepted", "sent", "delivered", "read",
}

#: A row no status callback may move. The six local decisions plus `failed`:
#: each of them is a settled account of what happened, and a late callback
#: about a message istota never sent (or sent and saw fail) is a duplicate.
_TERMINAL_FOR_STATUS = frozenset(LOCAL_TERMINAL_STATES) | {"failed"}

#: Meta's four statuses as a total order. Out-of-order arrival is ordinary, and
#: comparing rank is what makes a `delivered` landing after a `read` a no-op
#: rather than a regression. There is no stored event timestamp to compare as
#: well: `sent_whatsapp` has no column for one, and over a total order of four
#: values rank answers the question the timestamp would.
_STATUS_RANK = {
    "pending": 0, "accepted": 1, "sent": 2, "delivered": 3, "read": 4,
}

_TASK_LOG_MESSAGES = {
    "accepted": "WhatsApp message accepted by Meta",
    "sent": "WhatsApp message sent",
    "delivered": "WhatsApp message delivered",
    "read": "WhatsApp message read",
    "failed": "WhatsApp delivery failed",
    "window_closed": "WhatsApp delivery blocked: service window closed",
    "budget_exhausted": "WhatsApp delivery blocked: monthly attempt cap reached",
    "billing_blocked": "WhatsApp delivery blocked: billable circuit open",
    "opted_out": "WhatsApp delivery blocked: recipient opted out",
    "unconfigured": "WhatsApp delivery unconfigured",
    "unknown": "WhatsApp delivery unknown",
}

_ALERT_LABELS = {
    "failed": "failed",
    "unknown": "delivery unknown",
    "window_closed": "blocked by a closed service window",
    "budget_exhausted": "blocked by the monthly attempt cap",
    "billing_blocked": "blocked by the billable circuit",
    "unconfigured": "unconfigured",
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_FENCE_LINE = re.compile(r"\s*`{3,}.*")
_TABLE_RULE = re.compile(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r" {2,}")


def _sanitize(text: str) -> str:
    """The character-level pass every rendering shares.

    NUL and lone surrogates become U+FFFD rather than being dropped: a
    dropped character silently changes the text, and a lone surrogate cannot
    be encoded to UTF-8 at all, so leaving one in would make the send raise
    inside httpx and be recorded as ambiguous — a boundary error reported as a
    network one.
    """
    return "".join(
        "�" if ch == "\x00" or 0xD800 <= ord(ch) <= 0xDFFF else ch
        for ch in str(text)
    )


def _clean_markdown(text: str) -> str:
    """Markdown markers WhatsApp does not render, removed with nothing else.

    Fenced-code markers and table rules go and their text stays, per the spec.
    Heading markers go with them: they are the same kind of thing — a Markdown
    marker WhatsApp shows as a literal `#` — and the regex requires the space
    after the hashes, so `#1 priority` and `issue #42` are untouched.

    `**strong**` becomes `*bold*` and `__strong__` becomes `_italic_`, because
    WhatsApp's emphasis vocabulary is the single-character one and a doubled
    marker renders as a bold word wearing two stray asterisks. Single `*` and
    `_` survive untouched — that is the vocabulary, and stripping it the way
    the SMS renderer does would take formatting the recipient actually sees.
    Inline backticks go the way the fences do: the marker is not rendered, the
    text it wrapped is.
    """
    lines: list[str] = []
    for line in text.splitlines():
        if _FENCE_LINE.fullmatch(line) or _TABLE_RULE.fullmatch(line):
            continue
        line = _HEADING.sub("", line)
        line = line.replace("**", "*").replace("__", "_").replace("`", "")
        lines.append(line)
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def _truncate(text: str, limit: int, suffix: str) -> str:
    """Cut to ``limit`` including ``suffix``, without splitting a grapheme.

    Python strings are code points, so an astral character is one index and
    cannot be halved — but a combining sequence can, and a cut that lands
    inside one leaves its marks stranded at the head of what was dropped and
    its base without them at the tail of what was kept. So the cut is walked
    back while the *first dropped* character is a combining mark, which lands
    it on that cluster's base and drops the cluster whole.
    """
    available = max(0, limit - len(suffix))
    cut = min(available, len(text))
    while 0 < cut < len(text) and unicodedata.combining(text[cut]) != 0:
        cut -= 1
    return text[:cut].rstrip() + suffix


def render_whatsapp(text: str, *, limit: int = WHATSAPP_TEXT_LIMIT) -> str:
    """One service message body, at most ``limit`` Unicode characters.

    The cap is applied here rather than by the generic splitter, and that is
    the reason `TransportCapabilities.max_message_length` is None for this
    surface: splitting one answer into three messages multiplies the October
    2026 per-message cost by three and gives the recipient three chances to
    read half an answer.
    """
    cleaned = _clean_markdown(_sanitize(text))
    if len(cleaned) <= limit:
        return cleaned
    return _truncate(cleaned, limit, TRUNCATION_SUFFIX)


def render_template_parameter(
    text: str, *, limit: int = TEMPLATE_PARAMETER_LIMIT
) -> str:
    """One body parameter of an approved utility template.

    Stricter than a service message on one axis that is not aesthetic: Meta
    refuses a template parameter containing a newline, a tab or four or more
    consecutive spaces, so the whole value is flattened to single spaces.
    Control characters go for the same reason. The wording around the parameter
    is the operator's approved template, which istota never sees — hence the
    conservative cap.
    """
    # Whitespace first, controls second, and the order matters: a tab is both,
    # and dropping it as a control would join the words either side of it.
    spaced = "".join(
        " " if ch.isspace() else ch for ch in _clean_markdown(_sanitize(text))
    )
    flattened = "".join(
        ch for ch in spaced
        if ch == " " or unicodedata.category(ch) not in ("Cc", "Cf")
    )
    flattened = _SPACE_RUN.sub(" ", flattened).strip()
    if len(flattened) <= limit:
        return flattened
    return _truncate(flattened, limit, TRUNCATION_SUFFIX.strip().replace("\n", " "))


# ---------------------------------------------------------------------------
# The customer service window
# ---------------------------------------------------------------------------


def _parse_sql_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def service_window_open(binding, *, now: datetime | None = None) -> bool:
    """Whether a free-form service message is allowed to this binding now.

    Local state only, and deliberately conservative: Meta remains authoritative
    and may refuse a send this allowed. The opposite direction is the one that
    costs — a send istota believed was inside the window and Meta priced as a
    template is a charge on a deployment configured not to make any.

    A binding that has never written in has no window at all. That is not the
    same as a closed one in meaning, but it is the same answer, and inventing a
    window from `enrolled_at` would open one for a person who has never
    consented to be messaged.
    """
    if binding is None:
        return False
    opened = _parse_sql_datetime(getattr(binding, "last_user_message_at", None))
    if opened is None:
        return False
    return (now or datetime.now(timezone.utc)) < opened + SERVICE_WINDOW


def _destination(binding) -> str:
    """Where a message to this binding goes, resolved at the last moment.

    The send id first — it is the opaque destination Meta itself handed us and
    the one that survives a username-only user with no `wa_id`. The bootstrap
    number is the fallback, which is what it is for: a binding whose send id
    collided with another user's row keeps working through it (see
    `db.touch_whatsapp_binding`), and so does one enrolled by number and not
    yet written in from.
    """
    if binding is None:
        return ""
    return (binding.send_id or "").strip() or (
        binding.bootstrap_phone_number or ""
    ).strip()


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _record(row) -> WhatsAppDeliveryRecord:
    return WhatsAppDeliveryRecord(
        logical_key=row["logical_key"],
        status=row["status"],
        send_kind=row["send_kind"],
        meta_message_id=row["meta_message_id"],
        error_code=row["error_code"],
        body_chars=row["body_chars"] or 0,
    )


def _gate(conn, config: Config, user_id: str, *, ignore_opt_out: bool) -> str:
    """The local terminal state that blocks this send, or ``"pending"``.

    Ordered by whose decision it is. A disabled or invalid transport is the
    deployment's; a missing binding is the operator's; an opt-out is the
    *user's* and outranks everything below it, because reporting a closed
    window about somebody who asked not to be messaged is an operator alert
    nobody should be woken by. The billing circuit is next, and the window last
    — it is the only one of the five that a later message from the user
    reopens on its own.
    """
    from ...config import whatsapp_config_errors

    if not config.whatsapp.enabled or whatsapp_config_errors(config):
        return "unconfigured"
    binding = db.get_whatsapp_binding(conn, user_id)
    if binding is None or not _destination(binding):
        return "unconfigured"
    if binding.opted_out_at is not None and not ignore_opt_out:
        return "opted_out"
    if (
        config.whatsapp.billing_policy == "free_guard"
        and db.whatsapp_billing_block(conn) is not None
    ):
        return "billing_blocked"
    if not service_window_open(binding):
        # A template is the only way past a closed window, and `free_guard`
        # forbids one outright — even inside the window — so there is nothing
        # to fall through to here until stage 4 wires the paid path.
        return "window_closed"
    return "pending"


def _claim(
    config: Config,
    *,
    logical_key: str,
    user_id: str,
    task_id: int | None,
    body: str,
    ignore_opt_out: bool,
) -> tuple[str, WhatsAppDeliveryRecord]:
    """Insert-or-reuse one ledger row and take it, in one transaction.

    Returns ``(outcome, record)`` where outcome is `claimed`, `blocked` or
    `settled`. `settled` means the row already exists in a state no automatic
    path may leave — a completed send, a local refusal, or a row another worker
    has claimed and not yet finished.

    `BEGIN IMMEDIATE` around the read and the write together is what makes the
    concurrent case safe: without it two workers both read "no row" and the
    second's INSERT raises on the unique index, which is recoverable, but two
    workers both reading an unclaimed `pending` and both updating it is not.
    """
    now = db.sql_datetime_now()
    digest = hashlib.sha256(body.encode()).hexdigest()
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        if row is not None and (
            row["status"] in _NO_RESEND or row["claimed_at"] is not None
        ):
            return "settled", _record(row)
        status = _gate(conn, config, user_id, ignore_opt_out=ignore_opt_out)
        claimed_at = now if status == "pending" else None
        if row is None:
            conn.execute(
                "INSERT INTO sent_whatsapp (logical_key, user_id, task_id, "
                "send_kind, status, body_chars, body_sha256, claimed_at, "
                "created_at, updated_at) VALUES (?, ?, ?, 'service', ?, ?, ?, ?, ?, ?)",
                (
                    logical_key, user_id, task_id, status, len(body), digest,
                    claimed_at, now, now,
                ),
            )
        else:
            conn.execute(
                "UPDATE sent_whatsapp SET status = ?, body_chars = ?, "
                "body_sha256 = ?, task_id = COALESCE(?, task_id), "
                "claimed_at = ?, updated_at = ? WHERE logical_key = ?",
                (status, len(body), digest, task_id, claimed_at, now, logical_key),
            )
        fresh = conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        record = _record(fresh)
        if status != "pending":
            _log_transition(conn, record, task_id)
    return ("claimed" if status == "pending" else "blocked"), record


def _stamp_attempt(config: Config, logical_key: str) -> None:
    now = db.sql_datetime_now()
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE sent_whatsapp SET attempted_at = ?, updated_at = ? "
            "WHERE logical_key = ?",
            (now, now, logical_key),
        )


def _settle(
    config: Config,
    logical_key: str,
    status: str,
    *,
    meta_message_id: str | None = None,
    error_code: str | None = None,
) -> WhatsAppDeliveryRecord:
    now = db.sql_datetime_now()
    with db.get_db(config.db_path) as conn:
        try:
            conn.execute(
                "UPDATE sent_whatsapp SET status = ?, "
                "meta_message_id = COALESCE(?, meta_message_id), "
                "error_code = COALESCE(?, error_code), updated_at = ? "
                "WHERE logical_key = ?",
                (status, meta_message_id, error_code, now, logical_key),
            )
        except sqlite3.IntegrityError:
            # `meta_message_id` is unique. Meta returning an id already on
            # another row is not something a resend could have caused — nothing
            # here resends — so it is a Meta or a clock anomaly, and the honest
            # record is that we do not know what happened to this one.
            logger.warning("whatsapp.outbound.unknown reason=duplicate_message_id")
            conn.execute(
                "UPDATE sent_whatsapp SET status = 'unknown', updated_at = ? "
                "WHERE logical_key = ?",
                (now, logical_key),
            )
        row = conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        record = _record(row)
        _log_transition(conn, record, row["task_id"])
    return record


def _log_transition(conn, record: WhatsAppDeliveryRecord, task_id) -> None:
    message = _TASK_LOG_MESSAGES.get(record.status)
    if task_id is not None and message is not None:
        db.log_task(
            conn, task_id,
            "error" if record.status in ("failed", "unknown") else "info",
            message,
        )
    logger.info(
        "whatsapp.outbound.%s task_id=%s message=%s error_code=%s",
        record.status, task_id, _message_fingerprint(record.meta_message_id),
        record.error_code,
    )


def _message_fingerprint(value: str | None) -> str:
    from ...user_profiles import short_fingerprint

    return short_fingerprint("istota-whatsapp-message-v1", value or "", length=12)


# ---------------------------------------------------------------------------
# Failure alerts
# ---------------------------------------------------------------------------


def _write_failure_alert(conn, record: WhatsAppDeliveryRecord, user_id, task_id):
    """One durable alert row about a WhatsApp send that reached nobody.

    Deduplicated on the logical key, so a repeated attempt at one logical
    output bumps a row rather than raising a push per try. The row is written
    here and pushed by the caller after its transaction closes — see
    `transport._alerts`.
    """
    from ...notification_resolvers import task_alert

    label = _ALERT_LABELS.get(record.status)
    if label is None:
        return None
    task_label = f"task #{task_id}" if task_id is not None else "a notification"
    dedup = hashlib.sha256(record.logical_key.encode()).hexdigest()[:24]
    return task_alert.write(
        conn, user_id,
        dedup_key=f"whatsapp:{dedup}",
        title=f"WhatsApp delivery {label} — {task_label}",
        body=(
            f"The WhatsApp message for {task_label} was not delivered. "
            f"Its state is {label}."
        ),
        params={"task_id": task_id, "status": record.status},
    )


def _alert_failure(
    config: Config, record: WhatsAppDeliveryRecord, user_id: str, task_id
) -> None:
    """Write and push the failure alert, with WhatsApp taken out of the route.

    Sync, and never raises: the caller is on a delivery path whose own contract
    is that a failed alert does not become a failed send. Reporting a WhatsApp
    failure over WhatsApp would be a notice addressed to exactly the person who
    has just been shown to be unreachable, and on a metered surface a second
    billed message chasing a first that failed.
    """
    from .._alerts import push_off_surface

    try:
        with db.get_db(config.db_path) as conn:
            raised = _write_failure_alert(conn, record, user_id, task_id)
        push_off_surface(
            config, raised,
            exclude_surface="whatsapp", reference_prefix="whatsapp-failure",
        )
    except Exception:
        logger.warning("whatsapp.outbound.alert_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def deliver_whatsapp(
    config: Config,
    *,
    logical_key: str,
    user_id: str,
    text: str,
    task_id: int | None = None,
    buttons: tuple[tuple[str, str], ...] = (),
    reply_to_message_id: str | None = None,
    ignore_opt_out: bool = False,
    client=None,
) -> WhatsAppDeliveryRecord:
    """Claim and perform one logical send. At most one Cloud API call.

    `ignore_opt_out` has exactly one caller and must keep exactly one: the
    acknowledgement of a STOP, which the spec makes a single best-effort
    message *after* the opt-out is stored. Anything else passing it would
    make the opt-out advisory.

    Every database step is synchronous SQLite and runs on a worker thread. Two
    callers submit this to the process-global runtime loop through `run_coro`
    — the loop the Talk poller runs on — so opening a connection inline would
    wait on the WAL write lock from inside the loop thread and stall the whole
    runtime until the busy timeout expired.
    """
    body = render_whatsapp(text)
    outcome, record = await asyncio.to_thread(
        _claim, config,
        logical_key=logical_key, user_id=user_id, task_id=task_id,
        body=body, ignore_opt_out=ignore_opt_out,
    )
    if outcome == "settled":
        return record
    if outcome == "blocked":
        await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
        return record

    # Resolved *after* the claim and immediately before the call, so a binding
    # the operator changed while the task ran is honoured and the old
    # destination never receives the answer.
    destination = await asyncio.to_thread(current_destination, config, user_id)
    if not destination:
        record = await asyncio.to_thread(_settle, config, logical_key, "unconfigured")
        await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
        return record

    owned = client is None
    if owned:
        from .client import make_client  # noqa: PLC0415

        client = make_client(config)
    request = WhatsAppSendRequest(
        to=destination, text=body, kind="service",
        reply_to_message_id=reply_to_message_id, buttons=tuple(buttons),
    )
    try:
        await asyncio.to_thread(_stamp_attempt, config, logical_key)
        result = await client.send(request)
    except Exception:
        logger.warning("whatsapp.outbound.unknown reason=deliver_raised")
        result = None
    finally:
        if owned:
            await client.aclose()

    if isinstance(result, WhatsAppSendResult):
        record = await asyncio.to_thread(
            _settle, config, logical_key, "accepted",
            meta_message_id=result.message_id,
        )
        return record
    definite = bool(getattr(result, "definite", False))
    record = await asyncio.to_thread(
        _settle, config, logical_key,
        "failed" if definite else "unknown",
        error_code=getattr(result, "error_code", None),
    )
    await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
    return record


def current_destination(config: Config, user_id: str) -> str:
    """The user's WhatsApp destination right now, or ``""``.

    Public because two callers outside the send path ask the same question for
    a different reason: `WhatsAppTransport.resolve_target` needs to know
    whether a destination exists at all before the planner keeps the leg, and
    it must not learn what the destination *is* — the answer it returns is the
    conversation token.
    """
    with db.get_db(config.db_path) as conn:
        return _destination(db.get_whatsapp_binding(conn, user_id))


# ---------------------------------------------------------------------------
# Status callbacks
# ---------------------------------------------------------------------------


def apply_delivery_event(conn, event: WhatsAppDeliveryEvent):
    """``(disposition, record, pending_alert)`` for one authenticated status.

    Monotonic: a status whose rank is no higher than the row's current one is
    stale and changes nothing, which is what makes a `delivered` arriving after
    a `read` a no-op rather than a regression. `failed` is the exception and is
    terminal from any non-terminal state, because Meta reporting a failure
    after a `sent` is new information rather than a late duplicate.

    The alert is *returned* rather than pushed: this runs inside the webhook's
    open write transaction, and a push from in here would open a second
    connection against the lock this one holds, wait out the full busy timeout
    and raise into a never-raises contract.

    Pricing is deliberately not read. Stage 4 owns the observation and the
    circuit it opens, and a half-implementation that stored `billable` without
    acting on it would look like a working guard.
    """
    row = conn.execute(
        "SELECT * FROM sent_whatsapp WHERE meta_message_id = ?", (event.message_id,),
    ).fetchone()
    if row is None:
        # Acknowledged and bounded. A message id istota did not send is
        # ordinary on a number that has ever been used by another application,
        # and the id is fingerprinted rather than logged: it names a private
        # conversation.
        logger.warning(
            "whatsapp.delivery.unknown_message message=%s status=%s",
            _message_fingerprint(event.message_id), event.status,
        )
        return "delivery_unknown", None, None

    current = row["status"]
    if current in _TERMINAL_FOR_STATUS:
        return "delivery_duplicate", _record(row), None
    if event.status != "failed" and (
        _STATUS_RANK.get(event.status, -1) <= _STATUS_RANK.get(current, -1)
    ):
        return "delivery_stale", _record(row), None

    now = db.sql_datetime_now()
    conn.execute(
        # COALESCE on the error code: a row that later takes any other write
        # must not lose the public code the task alert and the admin view read.
        "UPDATE sent_whatsapp SET status = ?, error_code = COALESCE(?, error_code), "
        "updated_at = ? WHERE id = ?",
        (event.status, event.error_code, now, row["id"]),
    )
    updated = conn.execute(
        "SELECT * FROM sent_whatsapp WHERE id = ?", (row["id"],),
    ).fetchone()
    record = _record(updated)
    _log_transition(conn, record, updated["task_id"])
    logger.info(
        "whatsapp.delivery.updated task_id=%s message=%s status=%s error_code=%s",
        updated["task_id"], _message_fingerprint(event.message_id),
        event.status, event.error_code,
    )
    pending_alert = None
    if event.status == "failed":
        pending_alert = _write_failure_alert(
            conn, record, updated["user_id"], updated["task_id"],
        )
    return "delivery_updated", record, pending_alert


# ---------------------------------------------------------------------------
# Configuration probe
# ---------------------------------------------------------------------------


def is_whatsapp_configured(config: Config, user_id: str) -> bool:
    """Whether a WhatsApp route can reach this user at all.

    **Enrollment, not policy.** The service window is deliberately absent: it
    reopens by itself the moment the user writes in, and answering False while
    it is shut would make `heartbeat.check_heartbeats` skip the alert outright
    rather than record `window_closed` and say so off-surface. The gates here
    are the ones that will still be true in an hour — the transport, the
    binding, the opt-out, and the billing circuit, which only an operator
    clears.
    """
    from ...config import whatsapp_config_errors

    if not config.whatsapp.enabled or whatsapp_config_errors(config):
        return False
    try:
        with db.get_db(config.db_path) as conn:
            binding = db.get_whatsapp_binding(conn, user_id)
            if binding is None or not _destination(binding):
                return False
            if binding.opted_out_at is not None:
                return False
            if (
                config.whatsapp.billing_policy == "free_guard"
                and db.whatsapp_billing_block(conn) is not None
            ):
                return False
    except sqlite3.Error:
        logger.warning("whatsapp.configured.unavailable", exc_info=True)
        return False
    return True


__all__ = [
    "SERVICE_WINDOW",
    "TEMPLATE_PARAMETER_LIMIT",
    "TRUNCATION_SUFFIX",
    "WHATSAPP_TEXT_LIMIT",
    "apply_delivery_event",
    "current_destination",
    "deliver_whatsapp",
    "is_whatsapp_configured",
    "render_template_parameter",
    "render_whatsapp",
    "service_window_open",
]
