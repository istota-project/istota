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

The cost controls sit in the same two places. **The monthly attempt cap is
reserved by the claim**, inside the same immediate transaction, so two workers
cannot both take the last free slot; and the **billable circuit** is tripped by
a status callback, which is evidence after the fact rather than authorization
before it — the message that revealed the charge has already been sent.
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
from ...config import WHATSAPP_FREE_GUARD_MAX_ATTEMPTS, Config
from . import message_fingerprint
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

#: The cap on an **interactive** message's body, which is a quarter of the
#: plain-text one. A message carrying quick-reply buttons is a different Cloud
#: API object with a different limit, and rendering a confirmation prompt
#: against the text limit means Meta refuses every question longer than this
#: with a 4xx — a definite failure, so the row reads `failed`, the question is
#: never asked, and the task sits parked until it expires. The model's answer
#: is unbounded, so that is the ordinary case rather than an edge one.
WHATSAPP_INTERACTIVE_BODY_LIMIT = 1024

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

#: Every state that means a message reached nobody, and the words the alert
#: uses for it. `opted_out` is in the list on the spec's instruction rather
#: than by analogy — `## Design > Notifications` names the opt-out among the
#: five things that must alert: a confirmation prompt blocked here has to reach
#: the user some other way, or the task parks until `expire_stale_confirmations`
#: kills it two hours later. Deduplicated on the logical key, so it is one
#: alert per logical output rather than one per attempt.
_ALERT_LABELS = {
    "failed": "failed",
    "unknown": "delivery unknown",
    "window_closed": "blocked by a closed service window",
    "budget_exhausted": "blocked by the monthly attempt cap",
    "billing_blocked": "blocked by the billable circuit",
    "opted_out": "blocked by an opt-out",
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

    **`combining()` is narrower than "grapheme".** It is zero for a
    zero-width joiner, a variation selector, a skin-tone modifier and a
    regional indicator, so an emoji sequence can still be cut in half — the
    halves render as separate emoji rather than as a replacement character, so
    the cost is cosmetic and bounded. Widening it means a real
    grapheme-cluster segmenter, which is a dependency for a cosmetic gain.

    A `limit` below the suffix's own length would otherwise return a string
    *longer* than the limit, which is the one thing both callers' contracts
    promise it does not; the suffix is cut too rather than dropped, so the
    reader still sees that something was removed.
    """
    if limit <= len(suffix):
        return suffix[:max(0, limit)]
    available = limit - len(suffix)
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
# The WABA month and the local attempt cap
# ---------------------------------------------------------------------------


def quota_month(config: Config, *, now: datetime | None = None) -> str:
    """The WABA calendar month a claim counts against, as ``YYYY-MM``.

    Meta's announced allowance is per business phone number and per calendar
    month **on the business account**, which is why the zone is configured
    rather than assumed. A WABA at UTC+14 turns over fourteen hours before the
    daemon's own clock does, so counting in UTC would hand back a fresh
    allowance early there and keep spending an exhausted one at UTC-11.

    Never raises. `load_config` refuses an unresolvable zone, so the only way
    to reach the fallback is a daemon whose tzdata differs from the one that
    validated the file — and this is called from inside the claim transaction,
    where an exception would abort a send rather than bound it. UTC is the
    fallback because it is the zone the stored timestamps are already in.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # noqa: PLC0415

    moment = now or datetime.now(timezone.utc)
    try:
        zone = ZoneInfo(config.whatsapp.business_timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning(
            "whatsapp.quota.timezone_unresolved: counting the month in UTC",
        )
        zone = timezone.utc
    return moment.astimezone(zone).strftime("%Y-%m")


def _attempt_limit(config: Config) -> int:
    """The service-attempt ceiling for this month, or ``0`` for unlimited.

    ``0`` means opposite things under the two policies, and picking the wrong
    one costs money in exactly the mode that exists to avoid it. Under
    ``allow_paid`` it is the operator saying "no local bound"; under
    ``free_guard`` it must never be read that way, so the value is clamped
    into the range `load_config` already enforces. The clamp is defence in
    depth against a `WhatsAppConfig` built in code rather than parsed.
    """
    limit = config.whatsapp.monthly_service_attempt_limit
    if config.whatsapp.billing_policy == "allow_paid":
        return max(0, limit)
    return min(max(limit, 1), WHATSAPP_FREE_GUARD_MAX_ATTEMPTS)


def _service_attempts_used(conn, month: str) -> int:
    """Claimed service rows in ``month``. The count the cap is applied to.

    **Claims, not deliveries.** An attempt that failed may still have been
    counted by Meta and an ambiguous one may have arrived, so handing either
    slot back would make the bound overshoot by exactly the population a
    deployment in trouble has most of. A row blocked locally never reached
    Meta and has no `claimed_at`, so it is not counted and consumes nothing.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM sent_whatsapp WHERE send_kind = 'service' "
        "AND quota_month = ? AND claimed_at IS NOT NULL",
        (month,),
    ).fetchone()[0]


def _service_budget_exhausted(conn, config: Config, month: str) -> bool:
    limit = _attempt_limit(config)
    return bool(limit) and _service_attempts_used(conn, month) >= limit


def template_available(config: Config) -> bool:
    """Whether a closed window has a paid template to fall through to.

    The policy is re-checked here and not merely at config load. `load_config`
    refuses ``proactive_template.enabled`` under ``free_guard``, but one
    validator standing between a free-biased deployment and a paid message is
    one place for the rule to be edited out of — and the whole point of
    ``free_guard`` is that no code path can spend money.
    """
    template = config.whatsapp.proactive_template
    return bool(
        config.whatsapp.billing_policy == "allow_paid"
        and template.enabled
        and template.name.strip()
        and template.language.strip()
    )


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


def _gate(
    conn, config: Config, user_id: str, *, ignore_opt_out: bool, month: str,
) -> tuple[str, str]:
    """``(status, send_kind)``: the state that blocks this send, or ``pending``.

    Ordered by whose decision it is. A disabled or invalid transport is the
    deployment's; a missing binding is the operator's; an opt-out is the
    *user's* and outranks everything below it, because reporting a closed
    window about somebody who asked not to be messaged is an operator alert
    nobody should be woken by. The billing circuit is next, then the window —
    which is the one of these a later message from the user reopens on its own
    — and the monthly cap last, because it is the only one that depends on
    which kind of message the window just chose.

    The kind is decided here rather than by the caller, and it has to be: the
    window read and the budget count both belong inside the claim's own
    transaction, and the kind is what says which of the two rendered bodies
    goes on the row.
    """
    from ...config import whatsapp_config_errors

    if not config.whatsapp.enabled or whatsapp_config_errors(config):
        return "unconfigured", "service"
    binding = db.get_whatsapp_binding(conn, user_id)
    if binding is None or not _destination(binding):
        return "unconfigured", "service"
    if binding.opted_out_at is not None and not ignore_opt_out:
        return "opted_out", "service"
    if (
        config.whatsapp.billing_policy == "free_guard"
        and db.whatsapp_billing_block(conn) is not None
    ):
        return "billing_blocked", "service"
    send_kind = "service"
    if not service_window_open(binding):
        # A template is the only way past a closed window, and `free_guard`
        # forbids one outright — even inside the window — so a free-biased
        # deployment has nothing to fall through to.
        if not template_available(config):
            return "window_closed", "service"
        send_kind = "template"
    if send_kind == "service" and _service_budget_exhausted(conn, config, month):
        # Named for service attempts and counting only those. A template is
        # reachable only under `allow_paid`, which is the operator's explicit
        # acceptance of billing, so this number does not bound it. Pinned by
        # `test_a_template_attempt_does_not_consume_the_service_cap`, and the
        # setup guide has to say so where an operator sets the number.
        return "budget_exhausted", "service"
    return "pending", send_kind


def _claim(
    config: Config,
    *,
    logical_key: str,
    user_id: str,
    task_id: int | None,
    bodies: dict[str, str],
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
    **The monthly reservation rides on that same lock**, which is the whole of
    why the cap is concurrency-safe: the count and the row that increments it
    are one write transaction, so the final slot cannot be read as free twice.

    `bodies` carries both renderings because the gate is what picks between
    them. `quota_month` is stamped on every row, blocked ones included — it
    records which month the decision was made in — and only a *claimed* row
    counts against the allowance.
    """
    now = db.sql_datetime_now()
    month = quota_month(config)
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        if row is not None and (
            row["status"] in _NO_RESEND or row["claimed_at"] is not None
        ):
            return "settled", _record(row)
        status, send_kind = _gate(
            conn, config, user_id, ignore_opt_out=ignore_opt_out, month=month,
        )
        body = bodies[send_kind]
        digest = hashlib.sha256(body.encode()).hexdigest()
        claimed_at = now if status == "pending" else None
        if row is None:
            conn.execute(
                "INSERT INTO sent_whatsapp (logical_key, user_id, task_id, "
                "send_kind, status, body_chars, body_sha256, quota_month, "
                "claimed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    logical_key, user_id, task_id, send_kind, status, len(body),
                    digest, month, claimed_at, now, now,
                ),
            )
        else:
            conn.execute(
                "UPDATE sent_whatsapp SET send_kind = ?, status = ?, "
                "body_chars = ?, body_sha256 = ?, task_id = COALESCE(?, task_id), "
                "quota_month = ?, claimed_at = ?, updated_at = ? "
                "WHERE logical_key = ?",
                (
                    send_kind, status, len(body), digest, task_id, month,
                    claimed_at, now, logical_key,
                ),
            )
        fresh = conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()
        record = _record(fresh)
        if status != "pending":
            # The row's task id, not the caller's. The UPDATE above is
            # `COALESCE(?, task_id)`, so a pre-existing row keeps its own when
            # the caller passes None — and `db.log_task` on None writes
            # nothing, so the blocked state would never reach that task's log.
            _log_transition(conn, record, fresh["task_id"])
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
            #
            # The accepted id is lost from the row, which is the cost of the
            # unique index and is why it is fingerprinted into the log instead:
            # a message Meta took and billed for would otherwise be traceable
            # from nowhere, and no later status callback can match it either.
            logger.warning(
                "whatsapp.outbound.unknown reason=duplicate_message_id message=%s",
                message_fingerprint(meta_message_id),
            )
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
        record.status, task_id, message_fingerprint(record.meta_message_id),
        record.error_code,
    )


# ---------------------------------------------------------------------------
# Failure alerts
# ---------------------------------------------------------------------------


def _write_failure_alert(conn, record: WhatsAppDeliveryRecord, user_id, task_id):
    """One durable alert row about a WhatsApp send that reached nobody.

    The row itself is `transport._alerts.write_delivery_failure`, shared with
    the SMS surface for the reason that module's docstring gives — the two
    were the same sentence, the same `task #N` fallback and the same hashed
    dedup key on both sides, and only the label table genuinely differs.
    Deduplicated on the logical key, so a repeated attempt at one logical
    output bumps a row rather than raising a push per try; the caller pushes
    it after its transaction closes.
    """
    from .._alerts import write_delivery_failure

    return write_delivery_failure(
        conn,
        surface_label="WhatsApp",
        dedup_prefix="whatsapp",
        user_id=user_id,
        task_id=task_id,
        logical_key=record.logical_key,
        status=record.status,
        labels=_ALERT_LABELS,
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

    **Nothing may escape between the claim and the settle.** The claim commits
    `claimed_at`, and from that instant every later call for this logical key
    reads the row as settled and returns without sending — so a raise in
    between leaves a row that is never sent, never settled and never alerted,
    with the answer lost and nothing saying so. `_send_claimed` is therefore
    wrapped whole, and an escape lands on `unknown` for the same reason a
    timeout does: past `_stamp_attempt` the request may have gone out, and
    from out here there is no way to tell which side of it the failure fell.
    """
    # Both renderings, because only the claim's own transaction can read the
    # service window and therefore decide which one this send is. Both are
    # pure functions of `text`, so computing the unused one costs two regex
    # passes and buys the kind decision staying where the lock is.
    bodies = {
        "service": render_whatsapp(
            text,
            # A message carrying buttons is an interactive object with a
            # quarter of the plain-text body limit. Rendering it at 4096 means
            # Meta refuses every confirmation question longer than 1024 and the
            # row reads `failed`, so the question is asked nowhere.
            limit=WHATSAPP_INTERACTIVE_BODY_LIMIT if buttons else WHATSAPP_TEXT_LIMIT,
        ),
        "template": render_template_parameter(text),
    }
    outcome, record = await asyncio.to_thread(
        _claim, config,
        logical_key=logical_key, user_id=user_id, task_id=task_id,
        bodies=bodies, ignore_opt_out=ignore_opt_out,
    )
    if outcome == "settled":
        return record
    if outcome == "blocked":
        await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
        return record

    try:
        return await _send_claimed(
            config, logical_key=logical_key, user_id=user_id,
            body=bodies[record.send_kind], send_kind=record.send_kind,
            task_id=task_id, buttons=buttons,
            reply_to_message_id=reply_to_message_id, client=client,
        )
    except BaseException:
        # `BaseException`, not `Exception`: this runs as a FastAPI background
        # task and inside `run_coro`, so a shutdown delivers `CancelledError`
        # — which would otherwise leave exactly the stuck claimed row this
        # wrapper exists to prevent, on the one path where the send may
        # already be on the wire.
        logger.warning("whatsapp.outbound.unknown reason=deliver_escaped")
        try:
            record = await asyncio.to_thread(
                _settle, config, logical_key, "unknown",
            )
            await asyncio.to_thread(
                _alert_failure, config, record, user_id, task_id,
            )
        except Exception:
            # The row could not be settled either. Nothing further to try; the
            # original failure is what matters and is re-raised below.
            logger.warning("whatsapp.outbound.settle_failed", exc_info=True)
        raise


async def _send_claimed(
    config: Config,
    *,
    logical_key: str,
    user_id: str,
    body: str,
    send_kind: str,
    task_id: int | None,
    buttons: tuple[tuple[str, str], ...],
    reply_to_message_id: str | None,
    client,
) -> WhatsAppDeliveryRecord:
    """The body of :func:`deliver_whatsapp` from a claimed row onwards.

    Split in two at the one line that matters: everything before
    `client.send` is provably a message that never left, and everything from
    it onwards may have. A failure in the first half settles **`failed`**, not
    `unknown` — `unknown` says the request may have reached Meta and is the
    one state an operator can never resolve, so spending it on a case whose
    answer is known makes the ledger less useful, not more careful.
    """
    owned = False
    try:
        # Resolved *after* the claim and immediately before the call, so a
        # binding the operator changed while the task ran is honoured and the
        # old destination never receives the answer.
        destination = await asyncio.to_thread(current_destination, config, user_id)
        if not destination:
            record = await asyncio.to_thread(
                _settle, config, logical_key, "unconfigured",
            )
            await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
            return record
        request = _request(config, destination, body, send_kind, buttons,
                           reply_to_message_id)
        await asyncio.to_thread(_stamp_attempt, config, logical_key)
        if client is None:
            from .client import make_client  # noqa: PLC0415

            client = make_client(config)
            owned = True
    except Exception:
        # Nothing has reached the network: the binding read, the attempt stamp
        # and the client construction all run before the first byte. No
        # `exc_info` on the message itself would carry the body — a traceback
        # prints frames, not locals — so it is kept, since the cause here is
        # a database or a configuration fault an operator has to see.
        logger.warning(
            "whatsapp.outbound.failed reason=presend_error", exc_info=True,
        )
        record = await asyncio.to_thread(_settle, config, logical_key, "failed")
        await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
        return record

    try:
        result = await client.send(request)
    except Exception:
        # `client.send` has its own never-raises contract; this is the backstop
        # for a double that does not, and for anything the adapter's own
        # handler missed. From here the request may have gone out.
        logger.warning("whatsapp.outbound.unknown reason=send_raised")
        result = None
    finally:
        if owned:
            await client.aclose()

    if isinstance(result, WhatsAppSendResult):
        return await asyncio.to_thread(
            _settle, config, logical_key, "accepted",
            meta_message_id=result.message_id,
        )
    definite = bool(getattr(result, "definite", False))
    record = await asyncio.to_thread(
        _settle, config, logical_key,
        "failed" if definite else "unknown",
        error_code=getattr(result, "error_code", None),
    )
    await asyncio.to_thread(_alert_failure, config, record, user_id, task_id)
    return record


def _request(
    config: Config,
    destination: str,
    body: str,
    send_kind: str,
    buttons: tuple[tuple[str, str], ...],
    reply_to_message_id: str | None,
) -> WhatsAppSendRequest:
    """One Cloud API call described, for whichever kind the gate chose.

    A template carries **neither the buttons nor the reply id**, and both
    omissions are decisions. An approved template's buttons are Meta account
    state istota neither creates nor can map onto a confirmation id, so a
    question sent this way is answered by a typed `YES` or by `!confirm <id>`
    — which is why the scheduler keeps that sentence inside the smaller of the
    two budgets. And the message a reply id would point at is by definition
    more than twenty-four hours old, since a template is only reachable once
    the window has shut.
    """
    if send_kind == "template":
        template = config.whatsapp.proactive_template
        return WhatsAppSendRequest(
            to=destination, text=body, kind="template",
            template_name=template.name.strip(),
            template_language=template.language.strip(),
        )
    return WhatsAppSendRequest(
        to=destination, text=body, kind="service",
        reply_to_message_id=reply_to_message_id, buttons=tuple(buttons),
    )


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


def _observe_pricing(conn, config: Config, row, event: WhatsAppDeliveryEvent):
    """Store what Meta said this message cost, and trip the circuit if it did.

    Returns the buffered billing alert, or `None`.

    **Read, never derived.** The normalizer takes `pricing.billable` off the
    payload and refuses to infer it from anything else (PyWa's own
    `Pricing.from_dict` guesses it from `type == "regular"`), so `None` here
    means Meta said nothing — which is neither free nor paid, and is stored as
    NULL rather than resolved to either.

    **A `true` is never downgraded.** Two statuses about one message can
    disagree, and a row reading `free` beside a circuit the first of them
    opened would contradict the evidence an operator is being asked to take to
    Meta billing. Every other field takes the latest non-NULL observation.

    The circuit itself is `free_guard`'s alone: under `allow_paid` the operator
    has accepted billing and the pricing is recorded as an observation and
    nothing more.
    """
    updates: dict[str, object] = {}
    if event.billable is not None:
        updates["billable"] = 1 if (event.billable or row["billable"] == 1) else 0
    for column, value in (
        ("pricing_model", event.pricing_model),
        ("pricing_category", event.pricing_category),
        ("pricing_type", event.pricing_type),
    ):
        if value is not None:
            updates[column] = value
    if updates:
        # The column names are this function's own literals — the keys are set
        # four lines above from a fixed tuple — so the interpolation carries no
        # caller value; every observed value is a bound parameter.
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE sent_whatsapp SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), db.sql_datetime_now(), row["id"]),
        )
        logger.info(
            "whatsapp.delivery.priced message=%s billable=%s category=%s",
            message_fingerprint(event.message_id), event.billable,
            event.pricing_category,
        )

    if not event.billable or config.whatsapp.billing_policy != "free_guard":
        return None
    if not db.block_whatsapp_billing(conn, event.message_id):
        # The circuit was already open. One alert per outage, not one per
        # status: every later billable message is the same operator decision.
        return None
    logger.warning(
        "whatsapp.billing.blocked message=%s category=%s: every later WhatsApp "
        "send is refused until `istota whatsapp billing-unblock`",
        message_fingerprint(event.message_id), event.pricing_category,
    )
    return _write_billing_alert(conn, row["user_id"], row["task_id"])


def _write_billing_alert(conn, user_id: str, task_id):
    """The durable row behind an open billable circuit.

    Not `write_delivery_failure`: that one is about a message that reached
    nobody, and this is the opposite — a message that arrived and was charged
    for. The alert has to say so, because the charge is already made and no
    action an operator takes will unmake it; what the circuit buys is that the
    *next* one is not.

    A fixed dedup key, since the circuit is a singleton and only an operator
    reopens it. The Meta message id is not in the row at any length: a
    notification body renders on the web panel and through every alert route,
    and the id names a private conversation. `istota whatsapp billing-unblock`
    prints it in full, which is the private operator surface for it.
    """
    from ...notification_resolvers import task_alert  # noqa: PLC0415

    return task_alert.write(
        conn, user_id,
        dedup_key="whatsapp:billing-blocked",
        title="WhatsApp billing blocked — Meta reported a billable message",
        body=(
            "Meta's delivery callback reported that a WhatsApp message istota "
            "sent was billable, so every later WhatsApp send is refused. That "
            "message may already have been charged — the circuit stops the "
            "next one, not this one. Check the Meta billing page, then run "
            "`istota whatsapp billing-unblock` or switch "
            '`[whatsapp] billing_policy` to "allow_paid".'
        ),
        params={"task_id": task_id, "status": "billing_blocked"},
    )


def apply_delivery_event(conn, config: Config, event: WhatsAppDeliveryEvent):
    """``(disposition, record, alerts)`` for one authenticated status.

    Monotonic: a status whose rank is no higher than the row's current one is
    stale and changes nothing, which is what makes a `delivered` arriving after
    a `read` a no-op rather than a regression. `failed` is the exception and is
    terminal from any non-terminal state, because Meta reporting a failure
    after a `sent` is new information rather than a late duplicate.

    **Pricing is observed before that ladder and independently of it**, which
    is the one ordering here that is not obvious. Meta may put the pricing on a
    `delivered` that arrives after a `read`, or on a status about a row already
    `failed`; both are correctly refused as state transitions, and reading the
    pricing only on an applied one would throw the evidence away in exactly the
    out-of-order case the ladder exists for. A billable `failed` row is the
    loudest evidence there is — money spent on a message nobody received.

    Alerts are *returned* rather than pushed: this runs inside the webhook's
    open write transaction, and a push from in here would open a second
    connection against the lock this one holds, wait out the full busy timeout
    and raise into a never-raises contract. A tuple because one status can owe
    two — a failure notice and a circuit notice are different things with
    different dedup keys, and a status can carry both.
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
            message_fingerprint(event.message_id), event.status,
        )
        return "delivery_unknown", None, ()

    billing_alert = _observe_pricing(conn, config, row, event)
    alerts = () if billing_alert is None else (billing_alert,)

    current = row["status"]
    if current in _TERMINAL_FOR_STATUS:
        return "delivery_duplicate", _record(row), alerts
    if event.status != "failed" and (
        _STATUS_RANK.get(event.status, -1) <= _STATUS_RANK.get(current, -1)
    ):
        return "delivery_stale", _record(row), alerts

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
        updated["task_id"], message_fingerprint(event.message_id),
        event.status, event.error_code,
    )
    if event.status == "failed":
        failure_alert = _write_failure_alert(
            conn, record, updated["user_id"], updated["task_id"],
        )
        if failure_alert is not None:
            alerts = (*alerts, failure_alert)
    return "delivery_updated", record, alerts


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
    binding, the opt-out, the billing circuit, which only an operator clears,
    and the monthly cap, which only the calendar clears.

    The cap is read *against the template*, which is the one subtlety: on a
    paid deployment with an approved template a closed window still has a
    route, so an exhausted service allowance is not the end of the surface.
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
            if not template_available(config) and _service_budget_exhausted(
                conn, config, quota_month(config)
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
    "WHATSAPP_INTERACTIVE_BODY_LIMIT",
    "WHATSAPP_TEXT_LIMIT",
    "apply_delivery_event",
    "current_destination",
    "deliver_whatsapp",
    "is_whatsapp_configured",
    "quota_month",
    "render_template_parameter",
    "render_whatsapp",
    "service_window_open",
    "template_available",
]
