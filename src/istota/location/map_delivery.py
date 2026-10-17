"""A location share's map, delivered as a picture to Talk and WhatsApp.

Web draws the card itself from `messages.location_shares`; Talk and a Baileys
WhatsApp chat cannot, so they get the PNG `share_maps` renders from the card's
stored observation, at delivery time, after the text (Talk) or with it as its
caption (WhatsApp). The picture shows exactly what the text's map link
already discloses, so it is sent wherever that text went and nowhere else:
only cards attached to the message just delivered, only into the room they
were recorded for.

`location_share_cards.image_state` is the attempt's ledger, one per card:
`pending` once claimed (before the render), `sending` before the network
call, then `sent`, `failed`, `uncertain` when the outcome is unknown, or
`skipped` when no map was possible here (no browser service or render bundle,
a latitude Web Mercator cannot draw). A card is claimed only from `none` or
`failed`, so a repeated delivery never posts a second picture after `sent` or
`uncertain`, and a card one surface took is not sent by another.

A map never fails the text: every step here is caught, logged with the card
id, the surface and a stable reason, and recorded. Nothing logged carries a
coordinate, a path or a key. Each state write is its own short transaction,
and nothing here holds a database lock across a render or a network call.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from istota import db
from istota.location.share_maps import SKIP_REASONS, private_file, render_location_map

if TYPE_CHECKING:
    from istota.config import Config
    from istota.location.share_maps import RenderedMap
    from istota.transport.whatsapp._types import WhatsAppDeliveryRecord

logger = logging.getLogger(__name__)

#: The uploaded copy's name in the bot's Talk folder. It says nothing about
#: whose map it is or where.
MAP_FILENAME = "map.png"
_SCRATCH_DIR = ".share-maps"


@dataclass(frozen=True)
class Card:
    id: int
    actor_user_id: str
    room_token: str
    observation: dict


@dataclass(frozen=True)
class PreparedMap:
    """A WhatsApp map rendered and marked `sending`, waiting for its send's record."""

    card_id: int
    data: bytes


def attached_cards(conn, message_id: int) -> list[Card]:
    """The cards attached to one message, in recording order. Unreadable ones are left out."""
    rows = conn.execute(
        "SELECT id, actor_user_id, room_token, observation FROM location_share_cards "
        "WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    cards = []
    for row in rows:
        try:
            observation = json.loads(row["observation"])
        except ValueError:
            continue
        if isinstance(observation, dict):
            cards.append(Card(row["id"], row["actor_user_id"], row["room_token"], observation))
    return cards


def _claim(config: "Config", card_id: int, surface: str) -> bool:
    with db.get_db(config.db_path) as conn:
        claimed = conn.execute(
            "UPDATE location_share_cards SET image_state = 'pending', image_surface = ? "
            "WHERE id = ? AND image_state IN ('none', 'failed')",
            (surface, card_id),
        ).rowcount
    return claimed == 1


def _record(config: "Config", card_id: int, surface: str, state: str,
            reason: str | None = None) -> str:
    """Write a card's state and log the outcome. Never raises; returns ``state``."""
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE location_share_cards SET image_state = ? WHERE id = ?",
                (state, card_id),
            )
    except Exception as exc:
        logger.warning("location.share_map.state_unrecorded card=%s surface=%s state=%s (%s)",
                       card_id, surface, state, type(exc).__name__)
    if state in ("failed", "uncertain"):
        logger.warning("location.share_map.%s card=%s surface=%s reason=%s",
                       state, card_id, surface, reason)
    else:
        logger.info("location.share_map.%s card=%s surface=%s reason=%s",
                    state, card_id, surface, reason)
    return state


def _render(config: "Config", card: Card, surface: str) -> "RenderedMap | str":
    """The card's picture with the card at `sending`, or the state it was left in."""
    try:
        attempt = render_location_map(
            config, actor_user_id=card.actor_user_id, observation=card.observation,
        )
    except Exception as exc:
        # `render_location_map` never raises by contract; this is the backstop.
        return _record(config, card.id, surface, "failed", f"render_raised:{type(exc).__name__}")
    if attempt.rendered is None:
        state = "skipped" if attempt.reason in SKIP_REASONS else "failed"
        return _record(config, card.id, surface, state, attempt.reason)
    _record(config, card.id, surface, "sending")
    return attempt.rendered


def _scratch_dir(config: "Config") -> Path:
    """A private directory for the rendered file an upload needs.

    Beside `.control` under the temp root, which no task binds: the per-user
    temp directories are bound, the root is not.
    """
    directory = Path(config.temp_dir) / _SCRATCH_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _share_failure_state(exc: BaseException) -> str:
    """`failed` for an answer that says nothing was shared, else `uncertain`.

    A 4xx from the share endpoint is a refusal. A timeout, a dropped
    connection, a 5xx or a 2xx with an unreadable body may have posted it.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
        return "failed"
    return "uncertain"


def _post_to_talk(config: "Config", card: Card, rendered: "RenderedMap", talk_token: str) -> str:
    from istota.async_runtime import run_coro
    from istota.nextcloud.talk_files import upload_copy
    from istota.transport.talk import TalkTransport

    try:
        with private_file(rendered, _scratch_dir(config)) as path:
            remote = upload_copy(config, talk_token, path, MAP_FILENAME)
    except Exception as exc:
        return _record(config, card.id, "talk", "failed", f"upload_failed:{type(exc).__name__}")
    try:
        run_coro(TalkTransport(config).share_file(talk_token, remote))
    except Exception as exc:
        state = _share_failure_state(exc)
        reason = "upload_failed" if state == "failed" else "send_uncertain"
        return _record(config, card.id, "talk", state, f"{reason}:{type(exc).__name__}")
    return _record(config, card.id, "talk", "sent")


def deliver_talk_maps(config: "Config", *, message_id: int, talk_token: str) -> list[str]:
    """Share each map attached to ``message_id`` into the Talk conversation ``talk_token``.

    For after the message's text has been posted there. Only cards recorded
    for the room that conversation is bound to; returns each attempted card's
    final state, in order. Never raises.

    Runs `run_coro` for the share, so call it from a thread that is not the
    runtime loop's (the scheduler's, or `asyncio.to_thread`).
    """
    try:
        with db.get_db(config.db_path) as conn:
            cards = attached_cards(conn, message_id)
            room = db.resolve_room_token(conn, "talk", talk_token) or talk_token
    except Exception as exc:
        logger.warning("location.share_map.cards_unreadable message=%s surface=talk (%s)",
                       message_id, type(exc).__name__)
        return []
    states = []
    for card in cards:
        if card.room_token != room:
            continue
        try:
            if not _claim(config, card.id, "talk"):
                continue
        except Exception as exc:
            logger.warning("location.share_map.claim_failed card=%s surface=talk (%s)",
                           card.id, type(exc).__name__)
            continue
        rendered = _render(config, card, "talk")
        if isinstance(rendered, str):
            states.append(rendered)
            continue
        states.append(_post_to_talk(config, card, rendered, talk_token))
    return states


def map_missing(config: "Config", *, message_id: int) -> bool:
    """Whether a map attached to ``message_id`` ended `failed` or `skipped`.

    Read from the ledger after every surface had its attempt, so a render that
    never reached a send counts as much as a send that failed. Never raises.
    """
    try:
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM location_share_cards WHERE message_id = ? "
                "AND image_state IN ('failed', 'skipped') LIMIT 1", (message_id,),
            ).fetchone()
    except Exception as exc:
        logger.warning("location.share_map.state_unreadable message=%s (%s)",
                       message_id, type(exc).__name__)
        return False
    return row is not None


def _whatsapp_can_send_media(config: "Config") -> bool:
    from istota.transport.whatsapp import outbound

    adapter = outbound.active_adapter(config)
    return bool(adapter is not None and adapter.caps.outbound_media)


def prepare_whatsapp_map(config: "Config", *, message_id: int, room_token: str) -> PreparedMap | None:
    """Render the first map attached to ``message_id`` for a WhatsApp send into ``room_token``.

    One image per send, so only the first card in the room goes; the rest keep
    `none`. Nothing is claimed where the adapter cannot send media (Meta's
    Cloud API). Never raises. Pass the result's bytes as the send's
    `generated_image`, then hand its record to `settle_whatsapp_map`.
    """
    from istota.rooms.scopes import canonical_token

    try:
        if not _whatsapp_can_send_media(config):
            return None
        with db.get_db(config.db_path) as conn:
            room = canonical_token(conn, room_token) or room_token
            cards = [card for card in attached_cards(conn, message_id) if card.room_token == room]
        if not cards or not _claim(config, cards[0].id, "whatsapp"):
            return None
    except Exception as exc:
        logger.warning("location.share_map.prepare_failed message=%s surface=whatsapp (%s)",
                       message_id, type(exc).__name__)
        return None
    card = cards[0]
    rendered = _render(config, card, "whatsapp")
    if isinstance(rendered, str):
        return None
    return PreparedMap(card.id, rendered.data)


def settle_whatsapp_map(
    config: "Config", prepared: PreparedMap | None, record: "WhatsAppDeliveryRecord | None",
    *, send_raised: bool = False,
) -> str | None:
    """Record what became of a prepared map once its send has an answer. Never raises.

    ``send_raised`` is a send that raised instead of answering: past the claim
    it may have gone out, so the map is `uncertain`, never retried.
    """
    if prepared is None:
        return None
    from istota.transport.whatsapp._types import REACHED_META

    if send_raised:
        return _record(config, prepared.card_id, "whatsapp", "uncertain", "send_uncertain")
    if record is None:
        return _record(config, prepared.card_id, "whatsapp", "failed", "not_sent")
    if not record.media_staged:
        return _record(config, prepared.card_id, "whatsapp", "failed", "media_refused")
    if record.status in REACHED_META:
        return _record(config, prepared.card_id, "whatsapp", "sent")
    if record.status == "unknown":
        return _record(config, prepared.card_id, "whatsapp", "uncertain", "send_uncertain")
    return _record(config, prepared.card_id, "whatsapp", "failed", f"send_{record.status}")


__all__ = [
    "Card", "MAP_FILENAME", "PreparedMap", "attached_cards", "deliver_talk_maps",
    "map_missing", "prepare_whatsapp_map", "settle_whatsapp_map",
]
