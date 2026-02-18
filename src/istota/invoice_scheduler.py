"""Scheduled invoice generation and reminders.

Checks for clients with schedule = "monthly" in INVOICING.md and
auto-generates invoices on the configured day. Sends reminders N days
before generation. Notifications go via Talk and/or email.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import db
from .notifications import resolve_conversation_token as _resolve_conversation_token
from .notifications import send_notification as _send_notification_impl
from .storage import get_user_invoicing_path

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.invoice_scheduler")


def _resolve_invoicing_path(config: "Config", user_id: str) -> Path | None:
    """Resolve the INVOICING.md path for a user.

    Always looks in the user's bot config folder.
    Returns None if mount is not configured or file doesn't exist.
    """
    if not config.nextcloud_mount_path:
        return None

    if user_id not in config.users:
        return None

    path = config.nextcloud_mount_path / get_user_invoicing_path(user_id, config.bot_dir_name).lstrip("/")
    if path.exists():
        return path
    return None


def _resolve_ledger_path(config: "Config", user_id: str) -> str | None:
    """Resolve the primary ledger path for a user (for income postings)."""
    if not config.nextcloud_mount_path:
        return None

    user_config = config.users.get(user_id)
    if not user_config:
        return None

    for r in user_config.resources:
        if r.type == "ledger":
            path = config.nextcloud_mount_path / r.path.lstrip("/")
            if path.exists():
                return str(path)

    return None


def _resolve_notification_surface(
    client_notifications: str,
    config_notifications: str,
    user_notifications: str,
) -> str:
    """Resolve notification surface from the chain:
    client > invoicing config > user config > default "talk".
    """
    return client_notifications or config_notifications or user_notifications or "talk"


def _send_notification(
    config: "Config", user_id: str, message: str, subject: str, surface: str,
) -> bool:
    """Send a notification via the specified surface. Delegates to notifications module."""
    return _send_notification_impl(config, user_id, message, surface=surface, title=subject)


def _already_ran_this_month(
    last_timestamp: str | None,
    now: datetime,
) -> bool:
    """Check if an action already ran in the current month (user timezone)."""
    if not last_timestamp:
        return False

    last_run = datetime.fromisoformat(last_timestamp)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
    last_run_local = last_run.astimezone(now.tzinfo)

    return last_run_local.year == now.year and last_run_local.month == now.month


def _is_due_this_month(
    last_timestamp: str | None,
    target_day: int,
    now: datetime,
) -> bool:
    """Check if an action is due: target_day has passed and hasn't run this month.

    Args:
        last_timestamp: ISO timestamp of last run (UTC from DB), or None.
        target_day: Day of month the action is due.
        now: Current time in user's timezone.
    """
    if now.day < target_day:
        return False

    if not last_timestamp:
        return True

    last_run = datetime.fromisoformat(last_timestamp)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
    last_run_local = last_run.astimezone(now.tzinfo)

    # Already ran this month?
    return not (last_run_local.year == now.year and last_run_local.month == now.month)


def _resolve_overdue_days(client_config, invoicing_config) -> int:
    """Resolve overdue threshold: client override > global. 0 = disabled."""
    if client_config.days_until_overdue > 0:
        return client_config.days_until_overdue
    return invoicing_config.days_until_overdue


def _check_overdue_invoices(
    conn, app_config, user_id, user_config,
    invoicing_config, work_entries, now,
) -> int:
    """Check for overdue unpaid invoices and send consolidated notification.

    Returns count of newly-detected overdue invoices.
    """
    from .skills.accounting.invoicing import build_line_items

    already_notified = db.get_notified_overdue_invoices(conn, user_id)

    # Find outstanding entries: invoiced but not paid
    outstanding_by_invoice: dict[str, list] = {}
    for entry in work_entries:
        if entry.invoice and not entry.paid_date:
            outstanding_by_invoice.setdefault(entry.invoice, []).append(entry)

    newly_overdue = []

    for invoice_number, entries in outstanding_by_invoice.items():
        if invoice_number in already_notified:
            continue

        # Find the client config for this invoice group
        client_key = entries[0].client
        client_config = invoicing_config.clients.get(client_key)
        if not client_config:
            continue

        overdue_days = _resolve_overdue_days(client_config, invoicing_config)
        if overdue_days <= 0:
            continue

        # Invoice date = max date among entries sharing this invoice number
        invoice_date = max(e.date for e in entries)
        days_overdue = (now.date() - invoice_date).days - overdue_days
        if days_overdue <= 0:
            continue

        # Compute total via line items
        items = build_line_items(entries, invoicing_config.services)
        total = sum(item.amount for item in items)

        newly_overdue.append({
            "invoice_number": invoice_number,
            "client_name": client_config.name,
            "total": total,
            "days_overdue": days_overdue,
            "invoice_date": invoice_date,
        })

    if not newly_overdue:
        return 0

    # Build consolidated notification
    newly_overdue.sort(key=lambda x: x["days_overdue"], reverse=True)
    lines = ["**Overdue Invoices**\n"]
    for inv in newly_overdue:
        lines.append(
            f"- **{inv['invoice_number']}** ({inv['client_name']}): "
            f"${inv['total']:,.2f} — {inv['days_overdue']} days overdue "
            f"(issued {inv['invoice_date'].strftime('%b %d, %Y')})"
        )
    message = "\n".join(lines)

    surface = _resolve_notification_surface(
        "",  # no client-level override for consolidated message
        invoicing_config.notifications,
        user_config.invoicing_notifications,
    )
    _send_notification(
        app_config, user_id, message,
        "Overdue invoices", surface,
    )

    # Mark all as notified
    for inv in newly_overdue:
        db.mark_invoice_overdue_notified(conn, user_id, inv["invoice_number"])

    logger.info(
        "Detected %d overdue invoice(s) for %s",
        len(newly_overdue), user_id,
    )

    return len(newly_overdue)


def check_scheduled_invoices(conn, app_config: "Config") -> dict:
    """Check for invoice reminders and generations that are due.

    Returns dict with 'reminders_sent' and 'invoices_generated' counts.
    """
    from .skills.accounting.invoicing import parse_invoicing_config

    results = {"reminders_sent": 0, "invoices_generated": 0, "overdue_detected": 0}

    for user_id, user_config in app_config.users.items():
        invoicing_path = _resolve_invoicing_path(app_config, user_id)
        if not invoicing_path:
            continue

        try:
            invoicing_config = parse_invoicing_config(invoicing_path)
        except Exception as e:
            logger.error("Failed to parse INVOICING.md for %s: %s", user_id, e)
            continue

        # Get user timezone
        try:
            user_tz = ZoneInfo(user_config.timezone)
        except Exception:
            user_tz = ZoneInfo("UTC")
        now = datetime.now(user_tz)

        for client_key, client_config in invoicing_config.clients.items():
            if client_config.schedule != "monthly":
                continue

            schedule_day = client_config.schedule_day
            reminder_days = client_config.reminder_days

            state = db.get_invoice_schedule_state(conn, user_id, client_key)
            last_reminder_at = state.last_reminder_at if state else None
            last_generation_at = state.last_generation_at if state else None

            surface = _resolve_notification_surface(
                client_config.notifications,
                invoicing_config.notifications,
                user_config.invoicing_notifications,
            )

            # Check reminder
            if reminder_days > 0:
                reminder_day = schedule_day - reminder_days
                if reminder_day < 1:
                    reminder_day = 1

                if _is_due_this_month(last_reminder_at, reminder_day, now):
                    # Don't send reminder if generation already happened this month
                    if not _already_ran_this_month(last_generation_at, now):
                        gen_date = now.replace(day=schedule_day)
                        message = (
                            f"**Invoice Reminder**\n\n"
                            f"Invoices for **{client_config.name}** will be "
                            f"auto-generated on {gen_date.strftime('%B %d')}."
                        )
                        _send_notification(
                            app_config, user_id, message,
                            f"Invoice reminder: {client_config.name}", surface,
                        )
                        db.set_invoice_schedule_reminder(conn, user_id, client_key)
                        results["reminders_sent"] += 1
                        logger.info(
                            "Sent invoice reminder for %s/%s (day %d)",
                            user_id, client_key, reminder_day,
                        )

            # Check generation
            if _is_due_this_month(last_generation_at, schedule_day, now):
                # Compute period as current month (YYYY-MM)
                period = now.strftime("%Y-%m")

                # Set env vars needed by invoicing module
                ledger_path = _resolve_ledger_path(app_config, user_id)
                old_env = {}
                env_vars = {
                    "INVOICING_CONFIG": str(invoicing_path),
                }
                if ledger_path:
                    env_vars["LEDGER_PATH"] = ledger_path
                if app_config.nextcloud_mount_path:
                    env_vars["NEXTCLOUD_MOUNT_PATH"] = str(app_config.nextcloud_mount_path)

                for k, v in env_vars.items():
                    old_env[k] = os.environ.get(k)
                    os.environ[k] = v

                try:
                    invoices = parse_invoicing_config(invoicing_path)
                    from .skills.accounting.invoicing import generate_invoices_for_period
                    generated = generate_invoices_for_period(
                        config=invoices,
                        config_path=invoicing_path,
                        period=period,
                        client_filter=client_key,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to generate invoices for %s/%s: %s",
                        user_id, client_key, e,
                    )
                    generated = []
                finally:
                    # Restore env
                    for k, v in old_env.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v

                if generated:
                    total = sum(inv.get("total", 0) for inv in generated)
                    invoice_nums = ", ".join(inv.get("invoice_number", "") for inv in generated)
                    message = (
                        f"**Invoices Generated**\n\n"
                        f"Generated **{len(generated)}** invoice(s) for "
                        f"**{client_config.name}**: {invoice_nums}\n"
                        f"Total: ${total:,.2f}"
                    )
                    _send_notification(
                        app_config, user_id, message,
                        f"Invoices generated: {client_config.name}", surface,
                    )
                    results["invoices_generated"] += len(generated)
                    logger.info(
                        "Generated %d invoice(s) for %s/%s, total $%.2f",
                        len(generated), user_id, client_key, total,
                    )
                else:
                    logger.info(
                        "No uninvoiced entries for %s/%s in %s",
                        user_id, client_key, period,
                    )

                db.set_invoice_schedule_generation(conn, user_id, client_key)

        # Check for overdue invoices (runs regardless of schedule config)
        if invoicing_config.days_until_overdue > 0 or any(
            c.days_until_overdue > 0 for c in invoicing_config.clients.values()
        ):
            try:
                from .skills.accounting.invoicing import parse_work_log

                work_log_path = (
                    app_config.nextcloud_mount_path
                    / invoicing_config.work_log.lstrip("/")
                )
                if work_log_path.exists():
                    work_entries = parse_work_log(work_log_path)
                    overdue_count = _check_overdue_invoices(
                        conn, app_config, user_id, user_config,
                        invoicing_config, work_entries, now,
                    )
                    results["overdue_detected"] += overdue_count
            except Exception as e:
                logger.error("Error checking overdue invoices for %s: %s", user_id, e)

    return results
