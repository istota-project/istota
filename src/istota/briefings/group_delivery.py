"""Connect the saved-edition ledger to existing transports and task workers."""

import smtplib
from types import SimpleNamespace

import httpx

from istota import db
from istota.briefings import group_store as store
from istota.groups.policy import GroupError
from istota.groups.service import _transaction


def queue_deliveries(conn, config, *, now=None):
    """Queue external work and atomically record in-app notifications."""
    store.expire_delivery_claims(conn, now=now)
    created = []
    with _transaction(conn):
        rows = conn.execute("SELECT r.* FROM group_briefing_recipients r JOIN group_briefing_runs e ON e.id=r.run_id "
                            "WHERE e.state='published' AND r.state='pending' ORDER BY r.id").fetchall()
        for row in rows:
            refusal = store.delivery_refusal(conn, config, row)
            if refusal:
                conn.execute("UPDATE group_briefing_recipients SET state='skipped', error_code=? WHERE id=?",
                             (refusal, row["id"]))
                continue
            if row["channel"] == "in_app":
                from istota.notifications.resolvers.group_briefing import write_edition
                if write_edition(conn, row) is not None:
                    conn.execute("UPDATE group_briefing_recipients SET state='sent', updated_at=? WHERE id=?",
                                 (store._timestamp(now), row["id"]))
                continue
            correlation = f"{row['id']}:{row['attempts']}"
            active = conn.execute("SELECT 1 FROM tasks WHERE source_type='group_briefing_delivery' AND briefing_name=? "
                                  "AND status NOT IN ('completed', 'failed', 'cancelled')", (correlation,)).fetchone()
            if not active:
                created.append(db.create_task(conn, user_id="__briefings__", source_type="group_briefing_delivery",
                    prompt="Deliver the saved edition.", briefing_name=correlation, queue="background", skip_log_channel=True))
    return created


def _destination(config, recipient):
    """Resolve only the selected surface using its existing destination rules."""
    from istota.notifications.delivery import resolve_conversation_token
    from istota.transport.ntfy import ntfy_settings
    from istota.transport.routing import Destination, refuse_shared_rooms
    user = recipient["user_id"]
    channel = recipient["channel"]
    if channel == "email":
        addresses = config.users[user].email_addresses
        if not addresses:
            raise GroupError(409, "missing_email", "No email destination")
        return {"to": addresses[0]}
    if channel == "talk":
        token = resolve_conversation_token(config, user)
        if not token:
            raise GroupError(409, "missing_talk", "No Talk destination")
        if not refuse_shared_rooms(config, user, [Destination("talk", token)], purpose="group_briefing"):
            raise GroupError(409, "shared_destination", "A private destination is required")
        return {"token": token}
    settings = ntfy_settings(config, user)
    if not settings:
        raise GroupError(409, "missing_ntfy", "No ntfy destination")
    # Never persist bearer credentials in the ledger.
    return {"server_url": settings["server_url"], "topic": settings["topic"]}


def _send_part(config, recipient, run, destination, part):
    from istota.async_runtime import run_coro
    from istota.mail.support import get_email_config
    from istota.skills.email import send_email
    from istota.transport.email.outbound import _briefing_email_bodies
    from istota.transport.ntfy import _post_ntfy_blocking, ntfy_settings
    from istota.transport.talk import TalkTransport, get_talk_client
    from istota.transport import DeliveryOptions
    channel = recipient["channel"]
    if channel == "email":
        task = SimpleNamespace(user_id=recipient["user_id"], source_type="briefing")
        # A split edition is mailed part by part as Markdown; a whole one keeps its sections.
        document = run.get("document") if part["body"] == run["body_md"] else None
        plain, html, fmt = _briefing_email_bodies(config, task, part["body"], "plain",
                                                  briefing_document=document)
        return send_email(to=destination["to"], subject=run["subject"], body=plain,
                          html_body=html, content_type=fmt, config=get_email_config(config),
                          from_addr=config.email.bot_email,
                          message_id=f"<{part['send_identity']}@istota.invalid>")
    if channel == "talk":
        return run_coro(TalkTransport(config)._post_part(get_talk_client(config), destination["token"], part["body"],
            reply_to=None, reference_id=part["send_identity"], readback_allowed=True, task=None, report_errors=False))
    settings = ntfy_settings(config, recipient["user_id"])
    if not settings or any(settings[key] != value for key, value in destination.items()):
        raise GroupError(409, "destination_changed", "Destination changed before send")
    _post_ntfy_blocking(settings, part["body"], DeliveryOptions(title=run["subject"], markdown=True), raise_errors=True)
    return None


def _failure_state(exc):
    # Connect failures and explicit rejection prove no acceptance. A timeout,
    # disconnect or malformed success response cannot prove that.
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                        ConnectionRefusedError, smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused)):
        return "failed"
    if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
        return "failed"
    return "uncertain"


def deliver_task(task, config, *, now=None):
    """Only transport work. A completed task may contain failed recipient legs."""
    from istota.nextcloud.talk import split_message
    from istota.transport.talk import TalkTransport
    recipient_id = int(task.briefing_name.split(":", 1)[0])
    with db.get_db(config.db_path) as conn:
        recipient = store.claim_recipient(conn, config, recipient_id, now=now)
        if not recipient:
            return True, "delivery_settled"
        token = recipient["dispatch_token"]
        run = store.get_run(conn, recipient["run_id"])
    state, error = "sent", None
    try:
        destination = _destination(config, recipient)
        text = run["body_md"]
        if recipient["channel"] == "talk":
            parts = split_message(text, TalkTransport.capabilities.max_message_length or 4000)
        elif recipient["channel"] == "ntfy":
            # 1,000 Unicode codepoints fit ntfy's 4 KiB UTF-8 limit.
            parts = split_message(text, 1000)
        else:
            parts = [text]
        with db.get_db(config.db_path) as conn:
            saved = store.prepare_delivery(conn, recipient_id, token, destination, parts, now=now)
        for part in saved:
            if part["state"] == "sent":
                continue
            # Re-resolve before each part and stop if a private room became shared.
            current = _destination(config, recipient)
            if current != destination:
                raise GroupError(409, "destination_changed", "Destination changed during delivery")
            with db.get_db(config.db_path) as conn:
                if not store.start_delivery_part(conn, config, recipient_id, token, part["part_index"], now=now):
                    raise GroupError(409, "delivery_claim_lost", "Part is no longer pending")
            reference = None
            try:
                reference = _send_part(config, recipient, run, destination, part)
                if recipient["channel"] == "talk" and reference is None:
                    state, error = "uncertain", "acceptance_unknown"
            except GroupError:
                raise
            except Exception as exc:
                state = _failure_state(exc)
                error = "delivery_failed" if state == "failed" else "acceptance_unknown"
            with db.get_db(config.db_path) as conn:
                store.finish_delivery_part(conn, recipient_id, token, part["part_index"], state,
                                           reference=str(reference) if reference is not None else None, now=now)
            if state != "sent":
                break
    except GroupError as exc:
        if exc.code == "delivery_claim_lost":
            return True, "delivery_claim_lost"
        state, error = "skipped", exc.code
    except Exception:
        state, error = "uncertain", "acceptance_unknown"
    with db.get_db(config.db_path) as conn:
        try:
            store.finish_delivery(conn, recipient_id, token, state, error_code=error, now=now)
        except GroupError:
            return True, "delivery_claim_lost"
    return True, state
