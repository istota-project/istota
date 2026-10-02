"""Auth-link and sign-in code messages and synchronous delivery for CLI and web callers."""

from __future__ import annotations

from html import escape
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config

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


def build_sign_in_code_email(bot_name: str, display_name: str, code: str, ttl_minutes: int) -> tuple[str, str, str]:
    """A code, never a link: it opens wherever the mail is read and is useless there (ISSUE-574).

    The code sits in the subject and alone on its own line, which is what lets
    Apple's one-time-code autofill offer it from Mail.
    """
    subject = f"{code} is your {bot_name} sign-in code"
    greeting = f"Hello {display_name}," if display_name else "Hello,"
    detail = (f"Enter it on the {bot_name} sign-in screen where you asked for it. It expires in "
              f"{ttl_minutes} minutes and works only there. Never type it into a page you did not "
              "open yourself. If you did not request it, you can ignore this email.")
    plain = f"{greeting}\n\nYour {bot_name} sign-in code is:\n\n{code}\n\n{detail}\n"
    html = (f"<p>{escape(greeting)}</p><p>Your {escape(bot_name)} sign-in code is:</p>"
            f'<p style="font-size:1.5em;letter-spacing:0.2em"><strong>{escape(code)}</strong></p>'
            f"<p>{escape(detail)}</p>")
    return subject, plain, html


def send_auth_email(config: Config, to: str, subject: str, plain: str, html: str) -> bool:
    """Send both parts; never log recipient, token or SMTP exception text."""
    if not config.email.enabled:
        logger.warning("Auth email is not configured")
        return False
    try:
        from istota.mail.support import get_email_config
        from istota.skills.email import send_email

        send_email(to=to, subject=subject, body=plain, html_body=html,
                   config=get_email_config(config), from_addr=config.email.bot_email,
                   content_type="plain")
        return True
    except Exception:
        logger.warning("Auth email could not be sent")
        return False
