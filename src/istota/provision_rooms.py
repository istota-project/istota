"""Provision a user's default Talk rooms (`general` / `logs` / `alerts`).

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
  user's rooms, so a bare name match would hand one user another's ``logs`` on
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
- **Reuse by remembered token, name only on a first provision.** A display-name
  match cannot survive a rename, and the room's name belongs to the user: they
  renamed ``general`` to ``#general`` from the web UI, the rename propagated to
  Talk, and the next deploy found no group room called ``general`` and made one
  (ISSUE-342). So the token of every room a run resolves is written to the
  reserved ``_provisioned_rooms`` KV namespace, and a later run prefers that
  token over the name. The *writing* is the ``provision-rooms`` command's, not
  this module's — ``cmd_nextcloud_provision_rooms`` reads the record, threads it
  in, and records the result, because it is also the thing that decides which
  names are in scope for the run. ``general`` is what forced this: ``pending_channel_rooms``
  drops ``logs`` and ``alerts`` from the work list once their profile columns
  hold a token, and ``CHANNEL_FIELDS`` deliberately gives ``general`` no column,
  so it was the one name re-derived from Talk on every single deploy. The Docker
  entrypoint never had the bug because it persists ``GENERAL_TOKEN`` in its
  provisioning flag file and skips the lookup entirely; it reuses a token where
  this reused a name.
- **A retry needs a failure to retry.** Orphan adoption reads a bot-only room as
  a failed invite, and a user who walks out of their own ``general`` leaves
  exactly that state — so every deploy put them back in, for ever, and there was
  no state that expressed having left (ISSUE-408). The distinction is not in the
  participant list, but it is in the previous run: the invite outcome is
  recorded beside the token, and the remembered-token arm retries only where the
  last recorded invite *failed*. A room whose invite landed and that the user is
  now absent from is a room they left, and the correct action is the one the
  non-orphan arm already takes — log it and leave membership alone. The
  name-matching arm below is untouched: a room with no record at all has no
  outcome to contradict, and adopting it is still what stops one duplicate per
  deploy on a first provision.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ROOMS: tuple[str, ...] = ("general", "logs", "alerts")

# Talk conversation types: 1 = one-to-one, 2 = group, 3 = public. Group, not
# public — a public room is joinable by anyone holding its token, and these
# rooms carry the execution log and the confirmation prompts.
GROUP_ROOM_TYPE = 2

# Which provisioned room seeds which user_profiles column. `general` has no
# channel role; it is the room the user actually talks in.
CHANNEL_FIELDS: dict[str, str] = {
    "logs": "log_channel",
    "alerts": "alerts_channel",
}

# Where the token of each provisioned room is remembered, keyed by room name.
# The `istota_kv` store rather than a new table: this is framework bookkeeping
# that happens to be per-user key/value, which is exactly what the reserved
# namespace prefix exists for (`kv_namespaces.py`). The leading underscore is
# load-bearing — it is what stops a task reaching these rows through the `kv`
# skill and re-pointing a deploy at a room of its choosing.
PROVISIONED_NAMESPACE = "_provisioned_rooms"


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
    # Set when this run tried to put the user back into a room it had already
    # provisioned for them. Separate from `adopted` because it must not seed a
    # profile column, and separate from `invited` because on that path
    # `invited=False` would otherwise mean both "already in it, nothing to do"
    # and "tried and failed" — and the CLI reports one and not the other.
    reinvited: bool = False
    # Set when the remembered room exists and the user is simply not in it, and
    # this run deliberately did nothing about that. Usually a room they left, so
    # it is not a failure and must not fail the deploy — but it is also where a
    # room stranded by an invite that failed before the outcome was recorded
    # ends up, and that room is unreadable. Reporting it as `existing` would
    # hide the second case behind the first, so the CLI names it (ISSUE-408).
    absent: bool = False
    # What the *record* should carry when this run neither attempted an invite
    # nor saw the user in the room — i.e. it observed nothing that settles the
    # question. `None` means this run did settle it and `invite_failed` is the
    # answer. Carrying the previous value forward is what stops one transient
    # failure to read the participant list from erasing a recorded failure: the
    # run would otherwise write "no failure" having observed nothing at all, and
    # the retry that record exists to authorize would never fire again.
    carried_invite_failed: bool | None = None

    @property
    def seedable(self) -> bool:
        """Whether this run made the room usable for the user for the first time.

        Only such a room seeds a profile column. A room the user was already in
        must not, or a deploy would refill a channel the user cleared on
        purpose; a room whose invite failed must not either, since the user
        cannot read it.
        """
        return self.invited and (self.created or self.adopted)

    @property
    def invite_failed(self) -> bool:
        """Whether this run tried to put the user in the room and could not.

        The one fact that authorizes a later run to retry (ISSUE-408), and the
        predicate the CLI's stranded warning already used — it is here so the
        record and the warning cannot come to disagree about what a failure is.
        False for a room the user was already in, where ``invited=False`` means
        "nothing to do" rather than "tried and failed".
        """
        return not self.invited and (self.created or self.adopted or self.reinvited)

    @property
    def record_invite_failed(self) -> bool:
        """What `record_provisioned_rooms` persists about the invite.

        Deliberately not `invite_failed`, which answers a different question —
        "did *this run* try and fail", which is what the CLI's stranded warning
        and the Ansible `failed_when` are about. The record answers "is there an
        invite still outstanding", and a run that observed nothing must not
        overwrite that with its own silence. See `carried_invite_failed`.
        """
        if self.carried_invite_failed is None:
            return self.invite_failed
        return self.carried_invite_failed


@dataclass
class ProvisionedRecord:
    """What a previous run left about one room name.

    ``invite_failed`` defaults False, and that default is the whole
    compatibility story: every record written before ISSUE-408 carries no
    outcome, and reading the absence as "might have failed" would re-invite the
    reporter on their very next deploy — which is the bug. So an unknown outcome
    means "do not retry". ``--adopt`` records the same way: it never contacts
    Talk, so it has observed no failure to retry.

    What that costs is stated rather than implied, because it is a real
    regression for one shape: a room stranded by an invite that failed *before*
    the upgrade is no longer retried on its own, and it does not print
    ``invite FAILED``, so the Ansible ``failed_when`` no longer fires for it
    either. It is not silent — the room is reported ``user not a member``
    (``ProvisionedRoom.absent``), which is what separates it from ``existing``.
    Clearing the remembered token puts it back on the name-matching arm, which
    adopts it and retries; ``docs/reference/cli.md`` carries the command.
    """

    token: str
    invite_failed: bool = False


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


def _is_group_room(room: dict) -> bool:
    """True unless the room reports a type other than group.

    Talk puts the *other party's* user id in ``name`` for a one-to-one room, so
    a bare name match can adopt a private conversation with a user whose id
    happens to be ``logs``, and a remembered token could resurrect one as a
    channel. A room with no ``type`` at all is accepted, since older Talk
    versions omit it.
    """
    room_type = room.get("type")
    return room_type is None or room_type == GROUP_ROOM_TYPE


def _is_named_room(room: dict, name: str) -> bool:
    """True for a group room called ``name``."""
    if room.get("displayName") != name and room.get("name") != name:
        return False
    return _is_group_room(room)


#: Talk conversation types a remembered token must never resolve to: 1
#: one-to-one, 4 the "Talk updates" changelog room, 5 a former one-to-one, 6
#: note-to-self. Deliberately a reject list rather than the group-only test the
#: *name* path uses. The name match needs group-only because a one-to-one puts
#: the other party's user id in `name`, so `logs` can match a private
#: conversation; a token carries the identity outright and needs no such
#: protection. Requiring group there would instead reintroduce the bug: a user
#: who link-shares their room turns it into type 3, the token match would be
#: rejected, the name match already fails (the room was renamed — the whole
#: premise), and a duplicate is minted every deploy again.
_REMEMBERED_REJECT_TYPES = frozenset({1, 4, 5, 6})


def _find_remembered_room(rooms: "list[dict]", token: str) -> dict | None:
    """The bot's conversation with this exact token, or None.

    Whatever it is called now. That is the point: the name is the user's to
    change and the token is not, so this is the lookup that survives a rename
    (ISSUE-342). None also covers the room having been deleted in Nextcloud or
    the bot having been removed from it — either way it is absent from the
    conversation list and the caller falls back to the name.
    """
    if not token:
        return None
    for room in rooms:
        if room.get("token") != token:
            continue
        if room.get("type") in _REMEMBERED_REJECT_TYPES:
            return None
        return room
    return None


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
    known: ProvisionedRecord | None = None,
) -> ProvisionedRoom:
    """Reuse, adopt or create the group room ``name`` and put ``user_id`` in it.

    ``rooms`` lets a caller pass a room list it already fetched — provisioning
    three rooms otherwise costs three identical full-list GETs per user.

    ``known`` is what a previous run recorded for this name, from
    ``read_provisioned_records``: the token and whether that run's invite
    failed. The token is tried first and matched on the token alone, so a room
    the user has since renamed is still recognised as theirs (ISSUE-342). A
    stale token — the conversation deleted, or the bot removed from it — is
    absent from the room list, and the name path takes over unchanged.
    """
    if rooms is None:
        rooms = await client.list_conversations()

    known_token = known.token if known else ""
    remembered = _find_remembered_room(rooms, known_token)
    if remembered is not None:
        participants = await client.get_participants(known_token)
        if any(_is_user(p, user_id) for p in participants):
            logger.info(
                "Talk room already provisioned: %s -> %s", name, known_token,
            )
            # Seeing the user in the room settles it: whatever an earlier run
            # recorded, there is no invite outstanding now. This is the one arm
            # that clears a recorded failure without attempting anything.
            return ProvisionedRoom(
                name=name, token=known_token, created=False, invited=False,
                carried_invite_failed=False,
            )
        # The room is ours and still exists; the user is not in it. Three
        # reasons not to invite them back unconditionally.
        #
        # An empty participant list is more likely a failed read than a real
        # room, which is the judgement `_is_orphan` already makes — so the two
        # paths must not disagree about the same evidence.
        #
        # A room with other humans in it is one the user *left*. Dragging them
        # back on every deploy is the shape of the ISSUE-102 clobber this
        # module's own docstring warns about.
        #
        # And a bot-only room is ambiguous from the participant list alone — it
        # is either a failed invite this tool left behind or the user's own room
        # they stepped out of — so the list is not what decides it. The previous
        # run is: only a token whose recorded invite *failed* has anything to
        # retry, and a run that recorded success can never be looking at a
        # failure now (ISSUE-408). Without that gate a user who left their
        # `general` was put back on every deploy, with no state that could
        # express having left — unlike `logs` / `alerts` there is no profile
        # column to clear.
        if (
            not participants
            or not _is_orphan(participants, bot_user_id)
            or not (known and known.invite_failed)
        ):
            logger.info(
                "Provisioned Talk room %s (%s) exists but %s is not in it and "
                "no failed invite is recorded for it; leaving membership alone",
                name, known_token, user_id,
            )
            # Nothing was attempted and nothing about the invite was observed —
            # an empty list is a failed read, and a user missing from a room
            # says nothing about whether our last invite to it landed. So the
            # previous outcome is carried rather than overwritten: writing
            # "no failure" here would erase a recorded one and permanently
            # disable the retry it authorizes, which one transient Talk error
            # on the participant read is enough to trigger.
            return ProvisionedRoom(
                name=name, token=known_token, created=False, invited=False,
                absent=True,
                carried_invite_failed=bool(known and known.invite_failed),
            )
        # `adopted` stays False on purpose, so `seedable` is False and a
        # channel column the user cleared is not refilled behind them.
        # `reinvited` is what carries the fact that this run *attempted* an
        # invite, which `invited` alone cannot say: on this path `invited=False`
        # would otherwise mean both "already in it" and "tried and failed", and
        # the CLI's stranded warning and the Ansible `failed_when` both read it.
        logger.info(
            "Re-inviting %s to provisioned Talk room %s (%s)",
            user_id, name, known_token,
        )
        invited = await _invite(client, known_token, name, user_id)
        return ProvisionedRoom(
            name=name, token=known_token, created=False, invited=invited,
            reinvited=True,
        )

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
    known_records: "dict[str, ProvisionedRecord] | None" = None,
    resolved: "list[ProvisionedRoom] | None" = None,
) -> list[ProvisionedRoom]:
    """Ensure each room in ``names`` exists for ``user_id``, in order.

    The room list is fetched once and reused across the names. A room this call
    creates is absent from that snapshot, which is harmless: each name is looked
    up once.

    ``known_records`` maps a room name to what a previous run recorded for it —
    see ``read_provisioned_records``.

    ``resolved`` is an out-parameter: each room is appended as it resolves, so a
    caller can record what it got when a later name raises. Without it a Talk
    5xx on the third room throws away the two tokens already resolved, and the
    next deploy silently falls back to name matching for rooms that exist —
    which is the ISSUE-342 bug, arrived at through the error path.
    """
    rooms = await client.list_conversations()
    known = known_records or {}
    results = resolved if resolved is not None else []
    for name in names:
        results.append(
            await ensure_room(
                client, name, user_id, bot_user_id=bot_user_id, rooms=rooms,
                known=known.get(name),
            )
        )
    return list(results)


def provision_user_rooms(
    config, user_id: str, names: "tuple[str, ...] | list[str]" = DEFAULT_ROOMS,
    *, known_records: "dict[str, ProvisionedRecord] | None" = None,
    resolved: "list[ProvisionedRoom] | None" = None,
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
                known_records=known_records, resolved=resolved,
            )
        finally:
            await client.aclose()

    return asyncio.run(_run())


def _decode_record(raw: object) -> ProvisionedRecord | None:
    """One stored value -> what it says about the room, or None if nothing.

    Three encodings, all of which a deployment can have on disk. Values in
    `istota_kv` are JSON by convention — `cli.cmd_kv_get` does an unguarded
    `json.loads` on one, so a bare string there is a traceback rather than a
    value — but the first version of this record wrote bare strings anyway.
    ISSUE-342 wrote a JSON string, and ISSUE-408 needed a second field, so from
    here on it is a JSON object. Both older shapes carry no invite outcome and
    read as "no failure recorded", which is the safe direction: see
    `ProvisionedRecord`.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return ProvisionedRecord(token=raw)
    if isinstance(decoded, str):
        return ProvisionedRecord(token=decoded) if decoded else None
    if isinstance(decoded, dict):
        token = decoded.get("token")
        if not isinstance(token, str) or not token:
            return None
        return ProvisionedRecord(
            token=token, invite_failed=decoded.get("invite_failed") is True,
        )
    return None


def read_provisioned_records(
    db_path: Path, user_id: str,
) -> "dict[str, ProvisionedRecord]":
    """Room name -> what a previous run recorded for it.

    Empty on anything that goes wrong, including a database that does not exist
    yet: an unreadable record means "provision by name", which is the behaviour
    every install had before the record existed. Never raises — the caller is a
    deploy step, and refusing to provision because bookkeeping was unreadable
    would be worse than the duplicate this exists to prevent.
    """
    from . import db

    try:
        if not Path(db_path).exists():
            return {}
        with db.get_db(db_path) as conn:
            rows = db.kv_list(conn, user_id, PROVISIONED_NAMESPACE)
    except Exception as e:
        logger.warning("could not read provisioned room tokens for %s: %s", user_id, e)
        return {}
    out: dict[str, ProvisionedRecord] = {}
    for row in rows:
        record = _decode_record(row.get("value"))
        if record is not None:
            out[row["key"]] = record
    return out


def record_provisioned_rooms(
    db_path: Path, user_id: str, rooms: "list[ProvisionedRoom]",
) -> bool:
    """Remember each room's token and whether an invite is still outstanding.

    Written for every room the run resolved, not only the ones it created: a
    room adopted or matched by name on this run is the one a future run must
    recognise after it is renamed. The outcome is what makes a later retry
    correct — see `ProvisionedRecord`. Returns whether the write landed.

    Never raises. The rooms exist on Talk by the time this runs, so failing the
    deploy over bookkeeping would be worse than what a lost record costs: the
    next run falls back to matching by name, which is what every install did
    before this record existed. The caller reports the failure rather than
    acting on it.
    """
    from . import db

    entries = [
        (r.name, {"token": r.token, "invite_failed": r.record_invite_failed})
        for r in rooms if r.name and r.token
    ]
    if not entries:
        return True
    try:
        if not Path(db_path).exists():
            raise FileNotFoundError(db_path)
        with db.get_db(db_path) as conn:
            for name, value in entries:
                db.kv_set(
                    conn, user_id, PROVISIONED_NAMESPACE, name, json.dumps(value),
                )
        return True
    except Exception as e:
        logger.warning(
            "could not record provisioned room tokens for %s: %s", user_id, e,
        )
        return False


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
