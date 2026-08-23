"""The fire-and-forget class: notices with no object and no close condition.

Every other source watches something. A held task stops being held, a draft is
sent, a cron job is re-enabled, a token is reconnected — the object changes and
the resolver sees it. Nothing will ever happen to a "the model raised a security
alert" or a "your emailed request failed" notice. So this source is the one with
``auto_resolve_on_seen = True``: the row closes when the panel is opened with it
visible, and :func:`istota.notification_store.sweep_expired_alerts` is the
backstop for rows that fell below the render limit or belong to a user who never
opens the panel.

Five producers write here, each with its own key:

===================================  ==================================
``task:{task_id}:{alert_type}``      deferred alerts the model wrote from
                                     inside the sandbox
``throttle:{kind}``                  held-mail throttle notices
``expired:{task_id}``                a confirmation that timed out
``dmarc:{verdict}``                  the inbound-mail DMARC canary
``undelivered:{task_id}``            a task result that reached nobody
===================================  ==================================

Three rules hold across all five, and each exists because of a specific way this
class can go wrong.

**No ``link``, and no ``LINK`` action. Unconditionally.** The deferred-alert
producer reads a JSON file the model wrote *from inside the sandbox*. A `link`
is rendered into an anchor, where a text-node rule buys nothing and a
``javascript:`` or off-origin URL sails through. There is no branch here that
could emit one, so there is nothing for a future edit to get wrong: `resolve`
returns ``link=None`` and an empty action tuple for every row of this source.

**Every stored title and body is flattened**, through the same
``confirmations.flatten`` the gate's labels use. This class is the one that
carries model-authored text, ``send_notification`` puts that text into Talk, and
Talk renders markdown. Flattened on the way in *and* on the way out, because the
render path has no way to know which version of the producer wrote a stored row.
A body goes through :func:`flatten_block`, which flattens each line and keeps the
breaks: `_MARKUP_CHARS` maps a newline to a space because it was written for
one-line *labels*, and a body that collapsed several collapsed alerts onto one
run-on line would be the readability cost of a rule aimed at links.

**Every axis a key is built from is bounded.** ``alert_type`` arrives from the
model's own JSON and ``verdict`` from a parsed mail header; an unbounded axis
would mean one durable row per attacker-chosen value, each firing a push. The
alert type is narrowed to the two the producer actually distinguishes, and every
other component goes through :func:`_slug`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from ..config import Config
    from ..notification_sources import NotificationRow, NotificationView
    from ..notification_store import RaiseResult

logger = logging.getLogger(__name__)

SOURCE = "task_alert"

# The two alert types `_process_deferred_user_alerts` distinguishes. The model
# writes this field, so anything else collapses onto `security`: the producer's
# own branch is already binary, and honouring an arbitrary string would put a
# model-chosen value in a `dedup_key` — one durable row per value, per task.
ALERT_TYPE_SECURITY = "security"
ALERT_TYPE_ACTION_NEEDED = "action_needed"
ALERT_TYPES = (ALERT_TYPE_SECURITY, ALERT_TYPE_ACTION_NEEDED)

# The JSON array the model writes has no bound on entry count, and one row per
# entry would let a single task leave hundreds of durable rows. Entries collapse
# onto one row per `(task_id, alert_type)` and the count is capped — the same
# shape as `max_subtasks_per_task`, and for the same reason.
MAX_DEFERRED_ALERTS_PER_TASK = 20

# A `params` list is bounded for the reason the dedup key is. The DMARC canary
# fires on forged mail, so its senders are attacker-chosen: keying on one would
# be N rows, and appending every one to a JSON blob would be N entries in a
# single row instead. The *first* N are kept rather than the newest, so the
# stored value is stable across occurrences and the earliest evidence survives;
# the count of what was dropped rides alongside.
MAX_PARAM_ENTRIES = 20

# How much of a notice survives into the stored row. The full text of a rescued
# task result lives in `tasks.result`; this is the readable record of it.
MAX_ALERT_BODY_CHARS = 1000
MAX_ALERT_TITLE_CHARS = 160

# Why there are no buttons. `status_note` exists precisely so this is
# distinguishable from "no actions because nobody registered this source".
STATUS_NOTE = (
    "This notice has no in-app action. It clears itself once you have seen it."
)


def flatten(text: str | None) -> str:
    """The shared label flattener, applied to every stored field of this source.

    Imported function-locally: `confirmations` imports `db`, and every module in
    this package is written so a daemon hot path can import it without pulling
    the world in.
    """
    from ..confirmations import flatten as _flatten

    return _flatten(text or "")


def flatten_block(text: str | None) -> str:
    """Flatten every line of a body, keeping the line breaks between them.

    :func:`flatten` is the label rule and maps a newline to a space, which is
    right for a one-line title and wrong for a body listing several collapsed
    alerts. A newline is not a markdown-injection vector; the bracket and
    backtick characters are, and those still go.
    """
    lines = [flatten(line) for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _slug(value: object, *, limit: int = 32, fallback: str = "other") -> str:
    """A bounded, punctuation-free key component.

    Every caller here builds a `dedup_key` from a value it did not choose — a
    verdict parsed out of a mail header, a notice kind, a type the model wrote.
    Length and alphabet are both bounded so a hostile value cannot make the key
    unrecognisable or unbounded, and a `:` cannot be smuggled in to forge a
    different key's shape.
    """
    text = str(value or "").strip().lower()
    kept = "".join(c if (c.isascii() and (c.isalnum() or c in "_-")) else "-" for c in text)
    kept = kept.strip("-")[:limit].strip("-")
    return kept or fallback


def normalize_alert_type(value: object) -> str:
    """The model's `type` field, narrowed to the two the producer branches on."""
    text = str(value or "").strip().lower()
    return text if text in ALERT_TYPES else ALERT_TYPE_SECURITY


# --- keys ----------------------------------------------------------------
#
# Spelled once each. Idempotency comes from `UNIQUE (user_id, source,
# dedup_key)`, so a producer and a close path that disagree by one character
# means two rows for one thing, with only one of them closable.


def deferred_key(task_id: int | str, alert_type: str) -> str:
    return f"task:{_slug(task_id, limit=24, fallback='0')}:{normalize_alert_type(alert_type)}"


def throttle_key(kind: str) -> str:
    return f"throttle:{_slug(kind)}"


def expired_key(task_id: int | str) -> str:
    return f"expired:{_slug(task_id, limit=24, fallback='0')}"


def dmarc_key(verdict: str) -> str:
    return f"dmarc:{_slug(verdict)}"


def undelivered_key(task_id: int | str) -> str:
    return f"undelivered:{_slug(task_id, limit=24, fallback='0')}"


# --- the write path ------------------------------------------------------


def write(
    conn: "sqlite3.Connection",
    user_id: str,
    *,
    dedup_key: str,
    title: str,
    body: str = "",
    severity: str = "warning",
    actionable: bool = False,
    params: dict | None = None,
    room_token: str | None = None,
) -> "RaiseResult | None":
    """Write one fire-and-forget row, on the caller's connection.

    Deliberately narrower than :func:`istota.notification_store.write_notification`:
    there is no `link`, no `object_type` and no `object_id` parameter, because
    this source has no object to point at and must never emit a URL. A producer
    cannot pass one by mistake.

    Returns the :class:`RaiseResult` so a producer inside a write transaction can
    buffer it. **Most producers of this source do not deliver through it** —
    their delivery gate is an in-process window (the DMARC canary's 24-hour key,
    the mail throttle's per-window notice) or a send that carries routing the
    store does not model. Those call ``send_notification`` themselves and record
    the outcome with :func:`istota.notification_store.mark_delivered`; see the
    spec's "the row is the durable record" rule.
    """
    from ..notification_store import write_notification

    return write_notification(
        conn, user_id,
        source=SOURCE,
        dedup_key=dedup_key,
        title=flatten(title)[:MAX_ALERT_TITLE_CHARS] or "Notice",
        body=flatten_block(body)[:MAX_ALERT_BODY_CHARS],
        severity=severity,
        actionable=actionable,
        params=params or {},
        room_token=room_token,
        purpose="alert",
    )


def merge_param_list(
    conn: "sqlite3.Connection",
    user_id: str,
    dedup_key: str,
    key: str,
    value: str,
) -> dict:
    """Append `value` to a bounded list in the open row's `params`, and read it back.

    `write_notification` replaces `params` wholesale, which is right for a row
    whose params describe the latest occurrence and wrong for one that is
    accumulating evidence across them. The DMARC canary is the case: one row
    covers every forged sender that produced the same verdict, and the senders
    are what makes the row worth reading.

    Bounded at :data:`MAX_PARAM_ENTRIES`, keeping the first entries seen and
    counting the rest under ``{key}_omitted``. Never raises — a params read that
    failed must not cost the row.
    """
    kept: list[str] = []
    omitted = 0
    try:
        import json

        row = conn.execute(
            "SELECT params FROM notifications "
            "WHERE user_id = ? AND source = ? AND dedup_key = ?",
            (user_id, SOURCE, dedup_key),
        ).fetchone()
        if row is not None:
            stored = json.loads(row[0] or "{}")
            if isinstance(stored, dict):
                existing = stored.get(key)
                if isinstance(existing, list):
                    kept = [str(v) for v in existing][:MAX_PARAM_ENTRIES]
                try:
                    omitted = max(0, int(stored.get(f"{key}_omitted") or 0))
                except (TypeError, ValueError):
                    omitted = 0
    except Exception:
        logger.debug("could not read params for %r, starting fresh", dedup_key, exc_info=True)
        kept, omitted = [], 0

    flat = flatten(value)[:MAX_ALERT_TITLE_CHARS]
    if flat and flat not in kept:
        if len(kept) < MAX_PARAM_ENTRIES:
            kept.append(flat)
        else:
            omitted += 1

    out: dict = {key: kept}
    if omitted:
        out[f"{key}_omitted"] = omitted
    return out


# --- the read path -------------------------------------------------------


def body_for(row: "NotificationRow") -> str:
    """What the panel shows under the title.

    Prefers the individual messages in `params` over the stored `body`, because
    the deferred-alert producer collapses several of them onto one row and the
    stored body is only the first line of the summary. Flattened again on the way
    out: the render path cannot know which version of a producer wrote a stored
    row, and this is the source whose text the model authored.
    """
    params = row.params if isinstance(row.params, dict) else {}
    messages = params.get("messages")
    if isinstance(messages, list) and messages:
        lines = [flatten(str(m)) for m in messages[:MAX_DEFERRED_ALERTS_PER_TASK]]  # one line each
        rendered = "\n".join(f"- {line}" for line in lines if line)
        if rendered:
            return rendered[:MAX_ALERT_BODY_CHARS]
    return flatten_block(row.body)[:MAX_ALERT_BODY_CHARS]


class TaskAlertResolver:
    source = SOURCE
    # The whole point of this source. Nothing outside the table will ever close
    # one of these rows, so being read is what closes it.
    auto_resolve_on_seen = True

    def resolve(
        self, config: "Config", conn: "sqlite3.Connection", row: "NotificationRow",
    ) -> "NotificationView | None":
        """Always a view, never ``None``.

        `None` means "the object is gone", and `list_open` marks those rows
        `stale` and omits them. A row of this source has no object, so returning
        `None` on any path would close a notice the user has not read — silently,
        and for good, since nothing raises it a second time.

        No `link` and no actions, on every path. See the module docstring.
        """
        from ..notification_sources import NotificationView

        return NotificationView(
            title=flatten(row.title)[:MAX_ALERT_TITLE_CHARS] or "Notice",
            body=body_for(row),
            severity=row.severity,
            actions=(),
            link=None,
            status_note=STATUS_NOTE,
        )


RESOLVER = TaskAlertResolver()
