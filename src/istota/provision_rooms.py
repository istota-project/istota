"""Provision a user's default Talk rooms (#general / #logs / #alerts).

Docker installs get these from ``docker/istota/entrypoint.sh``, which creates
the rooms and seeds their tokens into the rendered config. A bare-metal
(Ansible) deploy had no equivalent: the role only forwarded ``--log-channel`` /
``--alerts-channel`` to ``istota user ensure`` when the operator had already put
tokens in inventory, which presupposes the rooms exist and that the operator
knows their opaque tokens. Out of the box that left ``log_channel`` empty, so
``effective_log_destinations`` returned ``[]`` and the execution log was off
while the same install under Docker had it working (ISSUE-115).

This module is the shared implementation behind
``istota nextcloud provision-rooms``. Three properties make it safe to run on
every deploy:

- **Idempotent by participant-scoped lookup.** A room is reused when one of the
  same name already exists *with the target user in it*. The bot sits in every
  user's rooms, so a bare name match would hand one user another's ``#logs`` on
  a shared Nextcloud — participation is what keeps them apart.
- **Orphan adoption.** A room created here whose follow-up invite failed has the
  bot as its only human participant, so the participation lookup above would
  miss it and mint a duplicate on the next deploy — one room per deploy, for
  ever. Such a room is instead recognised (bot-only, right name) and the invite
  retried. That is what makes a failed invite self-healing rather than
  accumulating.
- **Seeding only on first provision.** A channel token is written into the
  profile only for a room this run actually made usable. An empty
  ``log_channel`` is a *user-chosen* state — ``effective_log_destinations``
  documents the execution log as opt-in, and clearing the web UI control is how
  you turn it off — so refilling every empty column on every deploy would
  re-enable a log the user switched off. That is the ISSUE-102 timezone clobber
  in a new place.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ROOMS: tuple[str, ...] = ("general", "logs", "alerts")

# Talk conversation types: 1 = one-to-one, 2 = group, 3 = public. Group, not
# public — a public room is joinable by anyone holding its token, and these
# rooms carry the execution log and the confirmation prompts.
GROUP_ROOM_TYPE = 2

# Which provisioned room seeds which user_profiles column. #general has no
# channel role; it is the room the user actually talks in.
CHANNEL_FIELDS: dict[str, str] = {
    "logs": "log_channel",
    "alerts": "alerts_channel",
}


class ProvisionError(RuntimeError):
    """Talk accepted the request but the response carried no usable room."""


@dataclass
class ProvisionedRoom:
    name: str
    token: str
    created: bool
    # True when this run put the target user into the room — either by creating
    # it or by adopting an orphan. False for a room the user was already in
    # (nothing to do) and for a room whose invite failed.
    invited: bool = True
    # Set when an earlier run created this room but its invite never landed.
    adopted: bool = False

    @property
    def seedable(self) -> bool:
        """Whether this run made the room usable for the user for the first time.

        Only such a room seeds a profile column. A room the user was already in
        must not, or a deploy would refill a channel the user cleared on
        purpose; a room whose invite failed must not either, since the user
        cannot read it.
        """
        return self.invited and (self.created or self.adopted)


def _is_users_actor(participant: dict) -> bool:
    """True for a Nextcloud-user participant, false for guests/groups/bots.

    ``actorType`` is absent on older Talk versions, where the participant list
    only carried users anyway.
    """
    actor_type = participant.get("actorType")
    return actor_type is None or actor_type == "users"


def _actor_id(participant: dict) -> str | None:
    return participant.get("actorId") or participant.get("userId")


def _is_user(participant: dict, user_id: str) -> bool:
    """True when this participant entry is the Nextcloud user ``user_id``."""
    return _is_users_actor(participant) and _actor_id(participant) == user_id


def _is_named_room(room: dict, name: str) -> bool:
    """True for a group room called ``name``.

    Talk puts the *other party's* user id in ``name`` for a one-to-one room, so
    a bare name match can adopt a private conversation with a user whose id
    happens to be ``logs``. Reject any room that reports a type other than
    group; a room with no ``type`` at all is accepted, since older Talk versions
    omit it.
    """
    if room.get("displayName") != name and room.get("name") != name:
        return False
    room_type = room.get("type")
    return room_type is None or room_type == GROUP_ROOM_TYPE


def _is_orphan(participants: "list[dict]", bot_user_id: str | None) -> bool:
    """True for a room whose only human participant is the bot.

    That is the signature of a room created by an earlier run whose invite
    failed. Another user's same-named room has *that* user in it, so this stays
    clear of the collision the participation scoping exists to prevent.
    Conservative on missing information: without a bot id, or with an empty
    participant list (more likely a failed read than a real room), nothing is
    adopted.
    """
    if not bot_user_id:
        return False
    humans = [p for p in participants if _is_users_actor(p)]
    if not humans:
        return False
    return all(_actor_id(p) == bot_user_id for p in humans)


async def find_room_for_user(
    client, name: str, user_id: str, rooms: "list[dict] | None" = None,
) -> str | None:
    """Token of an existing group room called ``name`` that ``user_id`` is in."""
    if rooms is None:
        rooms = await client.list_conversations()
    for room in rooms:
        if not _is_named_room(room, name):
            continue
        token = room.get("token") or ""
        if not token:
            continue
        participants = await client.get_participants(token)
        if any(_is_user(p, user_id) for p in participants):
            return token
    return None


async def _invite(client, token: str, name: str, user_id: str) -> bool:
    """Best-effort invite. Reports success rather than raising.

    The room exists whether or not this lands, and an invite is retried by the
    next run via orphan adoption; raising here would strand a created room
    behind a non-zero exit.
    """
    try:
        await client.add_participant(token, user_id)
        return True
    except Exception as e:
        logger.warning(
            "Talk room %s (%s) exists but inviting %s failed: %s", name, token, user_id, e,
        )
        return False


async def ensure_room(
    client,
    name: str,
    user_id: str,
    *,
    bot_user_id: str | None = None,
    rooms: "list[dict] | None" = None,
) -> ProvisionedRoom:
    """Reuse, adopt or create the group room ``name`` and put ``user_id`` in it.

    ``rooms`` lets a caller pass a room list it already fetched — provisioning
    three rooms otherwise costs three identical full-list GETs per user.
    """
    if rooms is None:
        rooms = await client.list_conversations()

    orphan: str | None = None
    for room in rooms:
        if not _is_named_room(room, name):
            continue
        token = room.get("token") or ""
        if not token:
            continue
        participants = await client.get_participants(token)
        if any(_is_user(p, user_id) for p in participants):
            logger.info("Talk room already provisioned: %s -> %s", name, token)
            return ProvisionedRoom(name=name, token=token, created=False, invited=False)
        if orphan is None and _is_orphan(participants, bot_user_id):
            orphan = token

    if orphan is not None:
        logger.info("Adopting Talk room %s (%s) left by a failed invite", name, orphan)
        invited = await _invite(client, orphan, name, user_id)
        return ProvisionedRoom(
            name=name, token=orphan, created=False, invited=invited, adopted=True,
        )

    data = await client.create_conversation(name, room_type=GROUP_ROOM_TYPE)
    token = (data or {}).get("token") or ""
    if not token:
        raise ProvisionError(f"Talk returned no token when creating room {name!r}")

    invited = await _invite(client, token, name, user_id)
    logger.info("Talk room created: %s -> %s", name, token)
    return ProvisionedRoom(name=name, token=token, created=True, invited=invited)


async def provision_rooms(
    client,
    user_id: str,
    names: "tuple[str, ...] | list[str]" = DEFAULT_ROOMS,
    *,
    bot_user_id: str | None = None,
) -> list[ProvisionedRoom]:
    """Ensure each room in ``names`` exists for ``user_id``, in order.

    The room list is fetched once and reused across the names. A room this call
    creates is absent from that snapshot, which is harmless: each name is looked
    up once.
    """
    rooms = await client.list_conversations()
    results = []
    for name in names:
        results.append(
            await ensure_room(
                client, name, user_id, bot_user_id=bot_user_id, rooms=rooms,
            )
        )
    return results


def provision_user_rooms(
    config, user_id: str, names: "tuple[str, ...] | list[str]" = DEFAULT_ROOMS,
) -> list[ProvisionedRoom]:
    """Synchronous entry point: open a bot-auth TalkClient and provision.

    One-shot, so it gets its own event loop rather than the daemon's persistent
    runtime loop — this runs from the CLI during a deploy, not inside the
    scheduler.
    """
    from .talk import TalkClient

    bot_user_id = config.nextcloud.username

    async def _run():
        client = TalkClient(config)
        try:
            return await provision_rooms(
                client, user_id, names, bot_user_id=bot_user_id,
            )
        finally:
            await client.aclose()

    return asyncio.run(_run())


def pending_channel_rooms(
    db_path: Path, user_id: str, names: "tuple[str, ...] | list[str]",
) -> tuple[str, ...]:
    """Drop channel rooms whose profile column is already set.

    An operator who pinned ``log_channel`` to a hand-made room does not want a
    second room called ``logs`` created beside it and then left unused. Rooms
    with no channel role (``general``) are always kept.
    """
    from . import user_profiles

    profile = user_profiles.get_profile(db_path, user_id)
    if profile is None:
        return tuple(names)

    keep = []
    for name in names:
        field = CHANNEL_FIELDS.get(name)
        if field and (getattr(profile, field, "") or ""):
            continue
        keep.append(name)
    return tuple(keep)


def seed_channel_profile(
    db_path: Path,
    user_id: str,
    rooms: "list[ProvisionedRoom]",
    *,
    force: bool = False,
) -> "tuple[dict[str, str], str]":
    """Write log/alerts tokens into the user's profile for newly-made rooms.

    Returns ``(written_fields, state)`` with the same ``created`` / ``updated``
    / ``noop`` state contract as ``user_profiles.update_profile_with_status``.

    Only a room this run made usable seeds a column, and only into a column that
    is empty. Both halves matter: the first keeps a redeploy from refilling a
    channel the user cleared on purpose (an empty ``log_channel`` means the
    opt-in execution log is off, not that it was never set up), the second keeps
    an inventory-pinned token — applied by ``user ensure`` before this runs —
    from being overwritten.

    ``force`` re-points a column from whatever rooms are passed in, ignoring
    both rules. That is the deliberate re-provisioning path, never the default.
    """
    from . import user_profiles

    profile = user_profiles.get_profile(db_path, user_id)
    updates: dict[str, str] = {}
    for room in rooms:
        field = CHANNEL_FIELDS.get(room.name)
        if not field or not room.token:
            continue
        if not force and not room.seedable:
            continue
        current = (getattr(profile, field, "") or "") if profile else ""
        if current and not force:
            continue
        if current == room.token:
            continue
        updates[field] = room.token

    if not updates:
        return {}, "noop"

    _, state = user_profiles.update_profile_with_status(db_path, user_id, **updates)
    return updates, state
