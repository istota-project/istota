"""Provider-neutral inbound and delivery event handling."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3

from ... import commands, confirmations
from ...config import Config
from .._types import IncomingMessage
from ..ingest import ingest_message
from ..registry import TransportRegistry
from . import sms_conversation_token
from ._types import SmsEventResult
from .outbound import apply_delivery_event, deliver_sms
from .providers._types import InboundSmsEvent, SmsDeliveryEvent
from .providers.registry import SmsProviderRegistry

_MMS_REPLY = "MMS is not supported. Please resend the request as text."
_E164_RE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
logger = logging.getLogger(__name__)


def _number_fingerprint(number: str) -> str:
    value = f"istota-sms-number-v1\0{number}".encode()
    return hashlib.sha256(value).hexdigest()[:16]


def _claim_inbound(conn, event: InboundSmsEvent, user_id: str) -> bool:
    try:
        conn.execute(
            "INSERT INTO processed_sms (provider, provider_message_id, provider_event_id, "
            "user_id, from_number, to_number, disposition, opt_out_action, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'received', ?, datetime('now'))",
            (
                event.provider, event.provider_message_id, event.provider_event_id,
                user_id, event.from_number, event.to_number, event.opt_out_action,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _set_disposition(conn, event: InboundSmsEvent, disposition: str, task_id=None) -> None:
    conn.execute(
        "UPDATE processed_sms SET disposition = ?, task_id = ? "
        "WHERE provider = ? AND provider_message_id = ?",
        (disposition, task_id, event.provider, event.provider_message_id),
    )


def handle_provider_event(
    conn,
    config: Config,
    event: InboundSmsEvent | SmsDeliveryEvent,
    *,
    active_provider_ready: bool = True,
) -> SmsEventResult:
    """Apply one normalized event inside the caller's database transaction."""
    if isinstance(event, SmsDeliveryEvent):
        disposition, delivery = apply_delivery_event(conn, event)
        return SmsEventResult(disposition=disposition, delivery=delivery)

    if not config.sms.enabled or not active_provider_ready:
        return SmsEventResult("unconfigured_provider")
    if event.provider != config.sms.provider:
        return SmsEventResult("inactive_provider")
    if not event.provider_message_id or len(event.provider_message_id) > 255:
        return SmsEventResult("invalid_message_id")
    if _E164_RE.fullmatch(event.from_number) is None:
        return SmsEventResult("invalid_sender")
    user_id = config.find_user_by_sms_number(event.from_number)
    if user_id is None:
        logger.info(
            "sms.inbound.rejected provider=%s reason=unknown_sender "
            "number_fingerprint=%s number_suffix=%s",
            event.provider, _number_fingerprint(event.from_number), event.from_number[-4:],
        )
        return SmsEventResult("unknown_sender")
    token = sms_conversation_token(user_id)
    conn.execute("BEGIN IMMEDIATE")
    if not _claim_inbound(conn, event, user_id):
        return SmsEventResult("duplicate")
    if event.opt_out_action:
        disposition = event.opt_out_action
        if disposition == "stop":
            conn.execute(
                "INSERT INTO sms_opt_outs (phone_number, opted_out_at, updated_at) "
                "VALUES (?, datetime('now'), datetime('now')) "
                "ON CONFLICT(phone_number) DO UPDATE SET updated_at = datetime('now')",
                (event.from_number,),
            )
        elif disposition == "start":
            conn.execute(
                "DELETE FROM sms_opt_outs WHERE phone_number = ?", (event.from_number,),
            )
        _set_disposition(conn, event, disposition)
        return SmsEventResult(disposition)
    if event.media_count > 0:
        _set_disposition(conn, event, "unsupported_media")
        return SmsEventResult(
            "unsupported_media", user_id=user_id, response_text=_MMS_REPLY,
            response_logical_key=(
                f"unsupported-media:{event.provider}:{event.provider_message_id}"
            ),
            preferred_from_number=event.to_number,
        )
    if not event.text.strip():
        _set_disposition(conn, event, "empty")
        return SmsEventResult("empty")
    answer = confirmations.parse_answer(event.text)
    if answer is not None:
        resolution = confirmations.resolve(
            conn, user_id, conversation_token=token,
        )
        if resolution.ambiguous:
            response = confirmations.ambiguity_listing(conn, resolution.ambiguous)
            _set_disposition(conn, event, "confirmation_ambiguous")
            return SmsEventResult(
                "confirmation_ambiguous", user_id=user_id, response_text=response,
                response_logical_key=(
                    f"confirmation-answer:{event.provider}:{event.provider_message_id}"
                ),
                preferred_from_number=event.to_number,
            )
        if resolution.task is not None:
            response = confirmations.apply_answer(
                conn, resolution.task, answer, config, by="sms",
            )
            _set_disposition(conn, event, "confirmation_answer")
            return SmsEventResult(
                "confirmation_answer", user_id=user_id, response_text=response,
                response_logical_key=(
                    f"confirmation-answer:{event.provider}:{event.provider_message_id}"
                ),
                preferred_from_number=event.to_number,
            )
    if event.text.startswith("!"):
        _set_disposition(conn, event, "command")
        return SmsEventResult(
            "command", user_id=user_id, command_text=event.text,
            response_logical_key=f"command:{event.provider}:{event.provider_message_id}",
            preferred_from_number=event.to_number,
        )
    confirmations.cancel_for_conversation(conn, token, user_id, by="sms")
    task_id = ingest_message(
        conn, config,
        IncomingMessage(
            user_id=user_id, text=event.text.strip(), source_type="sms",
            surface="sms", channel_token=token, output_target="sms",
            mirror_to_room=False, queue="foreground",
        ),
    )
    _set_disposition(conn, event, "task", task_id)
    return SmsEventResult("task", user_id=user_id, task_id=task_id)


async def deliver_event_response(
    config: Config,
    providers: SmsProviderRegistry,
    result: SmsEventResult,
) -> None:
    """Run command work and idempotent replies after the inbound commit."""
    response = result.response_text
    if result.command_text:
        if result.user_id is None:
            return
        command = await commands.dispatch(
            config, result.user_id, sms_conversation_token(result.user_id),
            result.command_text,
            surface="sms", registry=TransportRegistry({}),
        )
        response = command.text or ""
    if not response or not result.response_logical_key or not result.user_id:
        return
    await deliver_sms(
        config, providers, logical_key=result.response_logical_key,
        user_id=result.user_id, text=response,
        preferred_from_number=result.preferred_from_number,
    )
