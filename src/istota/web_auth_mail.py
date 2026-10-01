"""Auth-link messages and synchronous delivery for CLI and web callers."""

from __future__ import annotations

from html import escape
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


def _message(bot_name: str, display_name: str, link: str, action: str, detail: str) -> tuple[str, str, str]:
    subject = f"{bot_name}: {action}"
    greeting = f"Hello {display_name}," if display_name else "Hello,"
    plain = f"{greeting}\n\n{action} for {bot_name}:\n\n{link}\n\n{detail}\n"
    html = (f"<p>{escape(greeting)}</p><p>{escape(action)} for {escape(bot_name)}:</p>"
            f'<p><a href="{escape(link, quote=True)}">{escape(action)}</a></p>'
            f"<p>{escape(detail)}</p>")
    return subject, plain, html


def build_enrol_email(bot_name: str, display_name: str, link: str, ttl_hours: int) -> tuple[str, str, str]:
    return _message(bot_name, display_name, link, "Set your password",
                    f"This link expires in {ttl_hours} hours. If you did not expect this invitation, you can ignore it.")


def build_reset_email(bot_name: str, display_name: str, link: str, ttl_hours: int) -> tuple[str, str, str]:
    unit = "hour" if ttl_hours == 1 else "hours"
    return _message(bot_name, display_name, link, "Reset your password",
                    f"This link expires in {ttl_hours} {unit}. If you did not request it, you can ignore it.")


def build_login_link_email(bot_name: str, display_name: str, link: str, ttl_minutes: int) -> tuple[str, str, str]:
    return _message(bot_name, display_name, link, "Sign in",
                    f"This link expires in {ttl_minutes} minutes. Anyone who has this link can sign in as you. "
                    "If you did not request it, you can ignore it.")


def send_auth_email(config: Config, to: str, subject: str, plain: str, html: str) -> bool:
    """Send both parts; never log recipient, token or SMTP exception text."""
    if not config.email.enabled:
        logger.warning("Auth email is not configured")
        return False
    try:
        from .email_support import get_email_config
        from .skills.email import send_email

        send_email(to=to, subject=subject, body=plain, html_body=html,
                   config=get_email_config(config), from_addr=config.email.bot_email,
                   content_type="plain")
        return True
    except Exception:
        logger.warning("Auth email could not be sent")
        return False
