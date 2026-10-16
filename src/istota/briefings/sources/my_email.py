"""The user's own mail, from one connected account per source.

Personal only: the kind is in neither ``GROUP_SOURCE_KINDS`` nor
``ALLOWED_SHARED_SOURCE_KINDS``, so a group briefing or a shared block refuses
it at save and at gather. Two accounts exist, named by id rather than address:

- ``bot``: the user's mail in the bot mailbox, decided by the email skill's
  ``mine`` scope (plus-address, the user's own sender addresses, replies in
  their threads). The server-side criteria only narrow the fetch; the
  ownership filter decides what is returned. Fails closed without the
  framework DB, as the shared-pool ``email`` source does.
- ``google``: the user's Gmail inbox through their Google Workspace
  connection, read only with a ``gmail`` grant.

Mail is written by strangers, so every result is marked untrusted and fenced at
prompt assembly. Source config::

    {"account": "bot"|"google", "lookback_hours": 24}
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone

import httpx

from istota.briefings.sources import GatheredSource, SourceContext

logger = logging.getLogger(__name__)

KIND = "my_email"
ACCOUNTS = ("bot", "google")
MAX_MESSAGES = 50
DEFAULT_LOOKBACK_HOURS = 24
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"
_GMAIL_LEVELS = ("readonly", "full")


def _empty(title: str, provenance: str) -> GatheredSource:
    return GatheredSource(kind=KIND, title=title, provenance=provenance, ok=False)


def list_accounts(app_config, conn, user_id: str) -> list[dict]:
    """The accounts this user can pick for a ``my_email`` source.

    The bot address is always listed, unavailable only when the installation
    has no mailbox. Gmail is listed whenever Google Workspace is on, and is
    available once the user has granted Gmail read access.
    """
    from istota.mail.support import per_user_address

    address = per_user_address(app_config, user_id)
    imap_ready = bool(getattr(app_config.email, "imap_host", ""))
    bot_ready = bool(address) and imap_ready
    accounts = [{
        "id": "bot",
        "label": "Your bot address",
        "address": address,
        "available": bot_ready,
        "reason": "" if bot_ready else "Email is not set up on this installation.",
    }]
    gw = getattr(app_config, "google_workspace", None)
    if gw is not None and gw.enabled:
        level = _gmail_level(conn, user_id)
        if level in _GMAIL_LEVELS:
            reason = ""
        elif level is None:
            reason = "Connect Google in Settings, then allow Gmail access."
        else:
            reason = "Allow Gmail access for your Google connection in Settings."
        accounts.append({
            "id": "google",
            "label": "Gmail",
            "address": None,
            "available": level in _GMAIL_LEVELS,
            "reason": reason,
        })
    return accounts


def _gmail_level(conn, user_id: str) -> str | None:
    """None when Google is not connected, else the Gmail level ('off' if none)."""
    from istota import db
    from istota.credentials.google_scopes import LEVEL_OFF, levels_from_scopes

    scopes = db.get_google_scopes(conn, user_id)
    if scopes is None:
        return None
    return levels_from_scopes(scopes).get("gmail", LEVEL_OFF)


def resolve(config: dict, ctx: SourceContext) -> GatheredSource:
    account = config.get("account") or "bot"
    try:
        lookback_hours = float(config.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS)
    except (TypeError, ValueError):
        lookback_hours = float(DEFAULT_LOOKBACK_HOURS)
    if account not in ACCOUNTS:
        return _empty("My email", "(unknown email account)")
    if ctx.conn is None:
        return _empty("My email", "(email skipped: no database to check ownership)")
    if account == "bot":
        return _resolve_bot(ctx, lookback_hours)
    return _resolve_gmail(ctx, lookback_hours)


# ---------------------------------------------------------------------------
# The bot mailbox
# ---------------------------------------------------------------------------


def _resolve_bot(ctx: SourceContext, lookback_hours: float) -> GatheredSource:
    from istota.briefings.sources.email import _clean_body, _cutoff_dt, _env_before_cutoff, _since_date

    title = "My email"
    try:
        from imap_tools import AND

        from istota.mail.support import get_email_config
        from istota.skills.email import _mine_criteria, _scope_filter, fetch_emails_full, list_emails
    except Exception as e:  # noqa: BLE001
        logger.warning("my_email: imports unavailable: %s", e)
        return _empty(title, "(email unavailable: email dependencies missing)")

    email_config = get_email_config(ctx.app_config)
    if not email_config or not getattr(email_config, "imap_host", ""):
        return _empty(title, "(email unavailable: no mailbox configured)")
    mine = _mine_criteria(ctx.app_config, email_config, ctx.user_id, ctx.conn)
    if mine is None:
        return _empty(title, "(email unavailable: no address routes to you)")

    folder = getattr(ctx.app_config.email, "poll_folder", "INBOX") or "INBOX"
    cutoff = _cutoff_dt(lookback_hours, ctx.now)
    try:
        envelopes = list_emails(
            folder=folder, limit=None, config=email_config,
            criteria=AND(mine, date_gte=_since_date(lookback_hours, ctx.now)),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("my_email: envelope fetch failed: %s", e)
        return _empty(title, "(email fetch failed)")

    owned = _scope_filter(ctx.app_config, ctx.user_id, "mine", ctx.conn, envelopes)
    kept = [env for env in owned if not _env_before_cutoff(env, cutoff)]
    kept.sort(key=_envelope_sort_key, reverse=True)
    kept = kept[:MAX_MESSAGES]
    if not kept:
        return _empty(title, f"(no email for you in the last {int(lookback_hours)}h)")

    bodies: dict[str, str] = {}
    try:
        full = fetch_emails_full(
            folder=folder, limit=None, config=email_config,
            criteria=AND(uid=",".join(env.id for env in kept)),
        )
        bodies = {msg.id: (msg.body or "") for msg in full}
    except Exception as e:  # noqa: BLE001
        logger.warning("my_email: body fetch failed, using snippets: %s", e)

    max_chars = int(ctx.module_config.max_source_chars)
    items = []
    for env in kept:
        raw = bodies.get(env.id, "")
        body = _clean_body(raw) if raw else (env.snippet or "")
        items.append(_item(env.sender, env.subject, env.date, body, max_chars))
    return GatheredSource(
        kind=KIND, title=title, items=items, untrusted=True,
        provenance=f"{len(items)} messages to you (past {int(lookback_hours)}h)",
    )


def _envelope_sort_key(env) -> float:
    d = getattr(env, "date", None)
    if not isinstance(d, datetime):
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _item(sender, subject, date, body, max_chars: int) -> dict:
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + "\n[...truncated]"
    return {"sender": sender or "", "subject": subject or "", "date": date, "body": body}


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class _GmailError(Exception):
    pass


def _gmail_client() -> httpx.Client:
    return httpx.Client(timeout=20.0, follow_redirects=False)


def _resolve_gmail(ctx: SourceContext, lookback_hours: float) -> GatheredSource:
    from istota.briefings.sources.email import _cutoff_dt
    from istota.credentials import google_tokens

    title = "Gmail"
    gw = getattr(ctx.app_config, "google_workspace", None)
    if gw is None or not gw.enabled:
        return _empty(title, "(Gmail unavailable: Google is not set up on this installation)")
    if _gmail_level(ctx.conn, ctx.user_id) not in _GMAIL_LEVELS:
        return _empty(title, "(Gmail unavailable: Gmail access is not granted)")
    token = google_tokens.access_token_for(ctx.conn, ctx.app_config, ctx.user_id)
    if not token:
        return _empty(title, "(Gmail unavailable: reconnect Google in Settings)")

    cutoff = _cutoff_dt(lookback_hours, ctx.now)
    headers = {"Authorization": f"Bearer {token}"}
    max_chars = int(ctx.module_config.max_source_chars)
    try:
        with _gmail_client() as client:
            listing = _gmail_get(client, "messages", headers, params={
                "q": f"in:inbox after:{int(cutoff.timestamp())}",
                "maxResults": MAX_MESSAGES,
            })
            items = []
            for ref in (listing.get("messages") or [])[:MAX_MESSAGES]:
                message_id = str(ref.get("id") or "")
                if not message_id.isalnum():
                    continue
                message = _gmail_get(client, f"messages/{message_id}", headers, params={"format": "full"})
                item = _gmail_item(message, cutoff, max_chars)
                if item is not None:
                    items.append(item)
    except _GmailError as e:
        return _empty(title, f"(Gmail read failed: {e})")
    except httpx.HTTPError as e:
        logger.warning("my_email: Gmail unreachable: %s", type(e).__name__)
        return _empty(title, "(Gmail read failed: unreachable)")

    if not items:
        return _empty(title, f"(no Gmail in your inbox in the last {int(lookback_hours)}h)")
    items.sort(key=lambda item: item["date"], reverse=True)
    return GatheredSource(
        kind=KIND, title=title, items=items, untrusted=True,
        provenance=f"{len(items)} Gmail messages (past {int(lookback_hours)}h)",
    )


def _gmail_get(client: httpx.Client, path: str, headers: dict, *, params: dict) -> dict:
    response = client.get(GMAIL_BASE + path, headers=headers, params=params)
    if response.status_code == 401:
        raise _GmailError("Google rejected the connection; reconnect it in Settings")
    if response.status_code == 403:
        raise _GmailError("Google refused Gmail access")
    if not 200 <= response.status_code < 300:
        logger.warning("my_email: Gmail answered %s for %s", response.status_code, path.split("/")[0])
        raise _GmailError(f"Gmail answered {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise _GmailError("Gmail sent an unreadable answer") from None


def _gmail_item(message: dict, cutoff: datetime, max_chars: int) -> dict | None:
    from istota.briefings.sources.email import _clean_body

    try:
        received = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        received = None
    if received is not None and received < cutoff:
        return None
    payload = message.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers") or []}
    plain, html = _gmail_bodies(payload)
    if plain:
        body = plain
    elif html:
        body = _clean_body(html)
    else:
        body = message.get("snippet") or ""
    return _item(headers.get("from"), headers.get("subject"),
                 received or datetime.fromtimestamp(0, tz=timezone.utc), body, max_chars)


def _gmail_bodies(part: dict) -> tuple[str, str]:
    """The first text/plain and the first text/html body under ``part``."""
    plain = html = ""
    stack = [part]
    while stack:
        current = stack.pop(0)
        mime = (current.get("mimeType") or "").lower()
        data = (current.get("body") or {}).get("data")
        if data and mime == "text/plain" and not plain:
            plain = _b64decode(data)
        elif data and mime == "text/html" and not html:
            html = _b64decode(data)
        stack.extend(current.get("parts") or [])
    return plain, html


def _b64decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""
