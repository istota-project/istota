"""Email source resolver — the shared/unowned mail pool (zero-config newsletters).

Default ``mode="shared"``: a server-side ``date_gte`` INBOX fetch within
``lookback_hours`` (no fixed message cap — the prior 100-message truncation is
gone), each message's owner resolved via
:func:`istota.email_ownership.resolve_email_owner`, keeping only *unowned*
(shared-pool) mail. ``mode="senders"`` additionally narrows the shared pool to
an fnmatch sender allowlist (for noisy shared mailboxes).

**Fail closed**: if the framework DB can't be opened for ownership resolution,
the source is dropped (empty) rather than risk summarizing owned mail. Bodies
are read with ``mark_seen=False`` (a briefing never marks a user's mail read).

Source config shape::

    {"mode": "shared"|"senders", "senders": ["*@semafor.com", ...],
     "lookback_hours": 12}
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch

from istota.briefings.sources import GatheredSource, SourceContext


logger = logging.getLogger(__name__)


def _since_date(lookback_hours: float, now: datetime | None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=lookback_hours)).date()


def _cutoff_dt(lookback_hours: float, now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now - timedelta(hours=lookback_hours)


def _env_before_cutoff(env, cutoff: datetime) -> bool:
    """Whether an envelope predates the exact hour-level cutoff.

    IMAP ``date_gte`` is date-granular, so the server fetch is over-inclusive
    (up to ~a day wider than ``lookback_hours``). This trims the surplus to the
    exact window client-side so the ``past Nh`` provenance stays honest. A
    missing or non-datetime date is kept — never drop on absent metadata.
    """
    d = getattr(env, "date", None)
    if not isinstance(d, datetime):
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d < cutoff


def _sender_matches(sender: str, patterns: list[str]) -> bool:
    lo = (sender or "").lower()
    return any(fnmatch(lo, p.lower()) for p in patterns)


def resolve(config: dict, ctx: SourceContext) -> GatheredSource:
    mode = config.get("mode", "shared")
    senders = [s for s in (config.get("senders") or []) if s]
    lookback_hours = float(
        config.get("lookback_hours", ctx.module_config.default_lookback_hours)
    )
    max_chars = int(ctx.module_config.max_source_chars)
    title = "Newsletters" if mode == "shared" else "Newsletters (selected senders)"

    # Fail closed: ownership resolution needs the framework DB. Without it we
    # cannot tell shared mail from a user's own mail, so drop the source.
    if ctx.conn is None:
        return GatheredSource(
            kind="email", title=title,
            provenance="(email source skipped — no DB for ownership resolution)",
            ok=False,
        )

    try:
        from imap_tools import AND

        from istota.email_ownership import resolve_email_owner
        from istota.email_support import get_email_config
        from istota.skills.email import fetch_emails_full, list_emails
    except Exception as e:  # noqa: BLE001
        logger.warning("email source: imports unavailable: %s", e)
        return GatheredSource(
            kind="email", title=title,
            provenance="(email source unavailable — email deps missing)", ok=False,
        )

    email_config = get_email_config(ctx.app_config)
    if not email_config or not getattr(email_config, "imap_host", ""):
        return GatheredSource(
            kind="email", title=title,
            provenance="(email source unavailable — IMAP not configured)", ok=False,
        )

    folder = getattr(ctx.app_config.email, "poll_folder", "INBOX") or "INBOX"
    since = _since_date(lookback_hours, ctx.now)
    cutoff = _cutoff_dt(lookback_hours, ctx.now)

    # Pass 1: windowed envelope fetch (envelopes carry to/cc/references so
    # ownership resolution is complete — an Email lacks to/cc). limit=None so
    # the date window, not an arbitrary cap, bounds the result.
    try:
        envelopes = list_emails(
            folder=folder, limit=None, config=email_config,
            criteria=AND(date_gte=since),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("email source: envelope fetch failed: %s", e)
        return GatheredSource(
            kind="email", title=title, provenance="(email fetch failed)", ok=False,
        )

    kept = []
    for env in envelopes:
        owner = resolve_email_owner(ctx.app_config, ctx.conn, env)
        if owner is not None:
            continue  # owned by a configured user — never in the shared pool
        if mode == "senders" and not _sender_matches(env.sender, senders):
            continue
        if _env_before_cutoff(env, cutoff):
            continue  # trim the day-granular server surplus to the exact window
        kept.append(env)

    if not kept:
        return GatheredSource(
            kind="email", title=title,
            provenance=f"(no shared newsletters in the last {int(lookback_hours)}h)",
            ok=False,
        )

    # Pass 2: read the bodies of the kept messages in one session.
    bodies: dict[str, str] = {}
    try:
        uidset = ",".join(env.id for env in kept)
        full = fetch_emails_full(
            folder=folder, limit=None, config=email_config,
            criteria=AND(uid=uidset),
        )
        bodies = {msg.id: (msg.body or "") for msg in full}
    except Exception as e:  # noqa: BLE001
        logger.warning("email source: body fetch failed, using snippets: %s", e)

    items: list[dict] = []
    for env in kept:
        raw = bodies.get(env.id, "")
        body = _clean_body(raw) if raw else (env.snippet or "")
        if max_chars and len(body) > max_chars:
            body = body[:max_chars] + "\n[...truncated]"
        items.append({
            "sender": env.sender,
            "subject": env.subject,
            "date": env.date,
            "body": body,
        })

    return GatheredSource(
        kind="email", title=title, items=items,
        provenance=f"{len(items)} newsletters (past {int(lookback_hours)}h)",
    )


def _clean_body(body: str) -> str:
    """Strip HTML from a newsletter body when it looks like HTML."""
    lo = body.lower()
    if "<html" in lo or "<body" in lo or "<div" in lo or "<table" in lo:
        try:
            from istota.skills.briefing import _strip_html
            return _strip_html(body)
        except Exception:  # noqa: BLE001
            return body
    return body
