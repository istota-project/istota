"""`connect-link`: a one-use link that connects the user's Google account from chat (#994).

Runs host-side under `istota-skill`, like the rest of this skill, and issues a
`google_connect` secret link through `webui.secret_links`, the path the skill
proxy's `vault_link_request` takes: the same gates and the same delivery into
the user's private room. The task is read from `ISTOTA_TASK_ID`, which the
proxy sets from the task. The reply names the services and the expiry; the
link itself goes only into the queued message, never into the reply.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from istota.skills._cli import error_envelope

logger = logging.getLogger("istota.skills.google_workspace")


def _load_config():
    """The deployment's config, or None when it cannot be loaded."""
    try:
        from istota.config import load_config

        cfg_path = os.environ.get("ISTOTA_CONFIG_PATH", "")
        return load_config(Path(cfg_path) if cfg_path else None)
    except Exception:  # noqa: BLE001 — no config refuses
        return None


def _refusal(exc) -> dict:
    return error_envelope(exc.message, reason=exc.reason)


def _one_link_per_task(task_id: int):
    """The ``check`` for ``issue_and_queue``: a task sends at most one Google link (#999).

    Counts every ``google_connect`` row the task issued, spent or superseded
    included, so a model calling the verb in a loop sends the user one link.
    A credential link has the attempt's vault write budget and is not counted.
    """
    from istota.webui import secret_links

    def check(conn) -> None:
        sent = conn.execute(
            "SELECT 1 FROM secret_links WHERE task_id = ? AND intent = 'google_connect' LIMIT 1",
            (task_id,),
        ).fetchone()
        if sent is not None:
            raise secret_links.LinkRefused(
                "link_already_sent",
                "A link was already sent for this request; the user should use that one",
            )

    return check


def connect_link(argv: list[str]) -> dict:
    """Issue the link and queue it for the user's private room; the envelope for the model."""
    from istota import db
    from istota.credentials import google_scopes, google_tokens
    from istota.skills._turn_access import calling_task_id
    from istota.webui import secret_links
    from istota.webui.origin import external_origin

    if argv:
        return error_envelope("connect-link takes no arguments", reason="bad_request")
    config = _load_config()
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if config is None or not user_id:
        return error_envelope("This server's configuration could not be read", reason="not_configured")
    # The same condition the web app registers its Google client on.
    if not (config.google_workspace.enabled and config.google_workspace.client_id):
        return error_envelope("Google Workspace is not set up on this server; the operator has to "
                              "configure it first", reason="not_configured")
    task_id = calling_task_id()
    if task_id is None:
        return error_envelope("Only your own turn can ask for a link", reason="not_owner_turn")
    try:
        refused = secret_links.turn_refusal(config, user_id, task_id)
    except Exception as exc:
        logger.error("connect-link task_id=%s failed: %s", task_id, type(exc).__name__)
        return error_envelope("Could not create the link", reason="store_failed")
    if refused is None:
        refused = secret_links.origin_refusal(config)
    if refused is not None:
        return _refusal(refused)

    scopes = google_tokens.requested_scopes(config, user_id)
    if not scopes:
        host, scheme = external_origin(config)
        return error_envelope(
            "No Google services are selected. The user picks them in Settings, Connections, "
            "then asks for the link again", reason="no_scopes",
            settings_url=f"{scheme}://{host}/istota/settings/connections",
        )
    services = [f"{label} ({level.lower()})"
                for label, level in google_scopes.requested_services(scopes)]

    def compose(address: str, expires: str) -> str:
        return (f"Connect your Google account ({', '.join(services)}) here: {address}\n"
                f"It works once and expires at {expires}. I only send these when you ask for one.")

    try:
        with db.get_db(config.db_path) as conn:
            connected = db.has_google_token(conn, user_id)
        link, delivered_to = secret_links.issue_and_queue(
            config, user_id=user_id, task_id=task_id, intent="google_connect", params={},
            compose=compose, check=_one_link_per_task(task_id),
        )
    except secret_links.LinkRefused as exc:
        logger.info("connect-link refused task_id=%s reason=%s", task_id, exc.reason)
        if exc.reason == "link_already_sent":
            return error_envelope(exc.message, reason=exc.reason, connected=connected)
        return _refusal(exc)
    except Exception as exc:
        logger.error("connect-link task_id=%s failed: %s", task_id, type(exc).__name__)
        return error_envelope("Could not create the link", reason="store_failed")
    logger.info("connect-link task_id=%s link_id=%s delivered_to=%s", task_id, link.id, delivered_to)
    return {
        "status": "ok", "services": services, "connected": connected,
        "expires_at": secret_links.iso_expiry(link), "delivered_to": delivered_to,
    }
