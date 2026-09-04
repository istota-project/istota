"""Talk conversation polling and task creation — the TalkTransport inbound body.

Owns every Talk-protocol-specific inbound step and the module-global
conversation/participant/DM caches. ``poll_talk_conversations`` self-creates its
tasks (see its atomicity note); ``TalkTransport.poll`` delegates here.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ... import confirmations, db
from ...async_runtime import get_talk_client
from ...config import Config
from ...talk import TalkClient, clean_message_content
from .._types import WEBMIRROR_REF_PREFIX, IncomingMessage
from ..ingest import ingest_message

logger = logging.getLogger("istota.transport.talk.inbound")

# Dedicated logger so a multi-day series can be pulled out of the journal whole
# (`journalctl … | grep talk_poll_txn`), the same reasoning as the scheduler's
# health line and the host-pressure breadcrumb.
_POLL_TXN_LOGGER = logging.getLogger("istota.transport.talk.txn")

# A transaction open this long is an operator's problem rather than a data
# point. Well under `db.get_db`'s 30s lock wait, which is what another writer
# would spend behind it, and far under the 180s main-loop stall threshold —
# this is meant to fire before either of those does.
_TXN_HOLD_WARN_SECONDS = 1.0

# Below this, an "await" was a cache hit rather than a round trip.
# `_get_participants` is awaited whether or not it reaches the network, so
# counting entries would put a line on every cycle of every busy room.
_TXN_AWAIT_FLOOR_SECONDS = 0.005


# Participant cache: token -> (participants list, timestamp)
_participant_cache: dict[str, tuple[list[dict], float]] = {}
_PARTICIPANT_CACHE_TTL = 300  # 5 minutes

# Conversation list cache: avoids calling list_conversations() every poll cycle.
# Rooms change rarely; refreshing every 60s is sufficient.
_conversation_cache: tuple[list[dict], float] | None = None
_CONVERSATION_CACHE_TTL = 60  # seconds

# 1:1 DM token cache: user_id -> conversation_token (populated from list_conversations)
_dm_token_cache: dict[str, str] = {}

# When every room was last polled regardless of the `lastMessage` gate below.
# `None` means "not this process yet", so the first cycle after a restart is a
# full sweep — the safe direction, since the gate has no history to reason from.
#
# Mutated without a lock, like the caches above it. Nothing overlaps today:
# `run_coro` blocks its submitting thread, `_talk_poll_loop` is the only daemon
# caller and is single-threaded, and `TalkTransport.poll` has no live caller. A
# registry-driven inbound driver — the shape `.claude/rules/transport.md`
# describes as intended — would be the first thing to break that, and would
# race `_conversation_cache` in the same breath.
_last_full_sweep: float | None = None


def _gate_enabled(config: Config) -> bool:
    """Whether this deployment gates the per-room fetch at all.

    One knob rather than a boolean beside it: `talk_poll_full_sweep_interval`
    at `0` means every cycle is a full sweep, which is exactly the ungated
    behaviour, so an operator can switch the gate off without the codebase
    carrying two polling paths for ever.
    """
    return config.scheduler.talk_poll_full_sweep_interval > 0


def _has_news(conv: dict, last_known_id: int) -> bool:
    """Whether the room list says this room holds a message we have not seen.

    Reads `lastMessage` out of the `/api/v4/room` payload the poll cycle has
    already fetched, so answering it costs no extra request.

    **Fails toward fetching.** A missing `lastMessage`, a shape that is not a
    dict, or an id that is not an integer all read as "poll it". The two wrong
    answers are not symmetric: a needless poll costs one request that the gate
    exists to save, while a wrongly skipped room loses its message until the
    next full sweep — and the behaviour this replaces re-fetched a cycle later
    rather than dropping anything.

    **The accepted trade is latency, and it is a floor rather than a spike.** A
    quiet room used to have a long-poll open for most of every cycle, so the
    server pushed a message the moment it arrived. A gated room has no
    connection open at all, so a new message waits for the next room listing —
    up to `talk_poll_interval` plus a round trip, every time. `talk_poll_interval`
    is therefore the responsiveness knob now, where it used to be a backoff.

    **What is not established anywhere is that a real `lastMessage.id` tracks a
    real cursor.** Every test here builds both sides by hand. Talk is reported
    to skip `Room::setLastMessage` for some message classes and to return an
    empty `lastMessage` for oversized ones; the first would gate a room whose
    chat endpoint does have something to return. The sweep is what bounds that,
    and its interval is the recovery time nobody has measured against a live
    Nextcloud.
    """
    last_message = conv.get("lastMessage")
    if not isinstance(last_message, dict):
        return True
    newest = last_message.get("id")
    # A bool is an int in Python and would compare as 0 or 1 against a real
    # cursor, so it is refused here rather than read as an id.
    if not isinstance(newest, int) or isinstance(newest, bool):
        return True
    return newest > last_known_id


def get_dm_token(user_id: str) -> str | None:
    """Get the 1:1 DM conversation token for a user, if known.

    Populated automatically during Talk polling. Returns None if the
    poller hasn't run yet or the user has no 1:1 conversation with the bot.
    """
    return _dm_token_cache.get(user_id)


def extract_attachments(message: dict) -> list[str]:
    """
    Extract file attachment paths from a Talk message.

    When files are shared in Talk, they appear in the bot user's Talk folder.
    The message contains {file0}, {file1} placeholders that we replace with
    actual filenames from the message parameters.

    Returns list of relative paths like "Talk/filename.jpg".
    """
    attachments = []

    # Check for file parameters in message
    # Note: messageParameters is a dict when present, but can be an empty list when empty
    message_params = message.get("messageParameters", {})
    if not isinstance(message_params, dict):
        return attachments
    for key, value in message_params.items():
        if key.startswith("file") and isinstance(value, dict):
            # File shared in conversation
            filename = value.get("name", "")
            if filename:
                # Strip directory components to prevent path traversal
                safe_name = Path(filename).name
                if safe_name and safe_name != ".." and safe_name != ".":
                    # Files shared in Talk are accessible in the bot's Talk folder
                    attachments.append(f"Talk/{safe_name}")

    return attachments


def is_bot_mentioned(message: dict, bot_username: str) -> bool:
    """Check if the bot is directly @mentioned in a Talk message.

    Checks messageParameters for mention-user or mention-federated-user entries
    matching the bot username. Excludes mention-call (@all) to avoid responding
    to every broadcast.
    """
    message_params = message.get("messageParameters", {})
    if not isinstance(message_params, dict):
        return False

    for key, value in message_params.items():
        if not isinstance(value, dict):
            continue
        if key.startswith("mention-user") or key.startswith("mention-federated-user"):
            if value.get("id") == bot_username:
                return True
    return False


async def _get_participants(
    client: TalkClient,
    conversation_token: str,
    conv_type: int | None,
) -> list[dict]:
    """Get participants for a conversation, with TTL cache.

    Type 1 (DM) returns empty list (no lookup needed).
    Returns cached or fresh participant list from API.
    Falls back to empty list on API errors.
    """
    if conv_type == 1:
        return []

    now = time.monotonic()
    cached = _participant_cache.get(conversation_token)
    if cached is not None:
        participants, ts = cached
        if now - ts < _PARTICIPANT_CACHE_TTL:
            return participants

    try:
        participants = await client.get_participants(conversation_token)
        _participant_cache[conversation_token] = (participants, now)
        logger.debug(
            "Room %s (type=%s) has %d participants → %s",
            conversation_token, conv_type, len(participants),
            "multi-user" if len(participants) >= 3 else "DM-like",
        )
        return participants
    except Exception as e:
        logger.warning(
            "Error getting participants for %s (type=%s): %s: %s — treating as DM",
            conversation_token, conv_type, type(e).__name__, e,
        )
        return []


@dataclass
class _TxnHold:
    """One poll transaction: how long it was held, and how much of that it spent
    waiting on Nextcloud (ISSUE-406).

    `held` runs from just before `db.get_db` opens the connection to just after
    it commits, so it includes `sqlite3.connect`, the `PRAGMA synchronous` write
    and any wait for the lock itself. That is deliberate — the wait for the lock
    is the cost another writer pays, and it is the number an investigator wants.

    `awaits` counts only the awaits that took at least `_TXN_AWAIT_FLOOR_SECONDS`
    and `await_seconds` sums only those. The floor is applied per await rather
    than to the total because `_get_participants` is awaited once per message
    whether or not it reaches the network: a few hundred cache hits sum past any
    floor worth setting, and would then be reported as a round trip.
    """

    label: str
    opened: float
    awaits: int = 0
    await_seconds: float = 0.0


@contextlib.contextmanager
def _timed_poll_txn(label: str):
    """Measure one ``db.get_db`` block in ``poll_talk_conversations``.

    The poller awaits the network with a synchronous `sqlite3` connection open.
    `db.get_db` commits at the end of the `with`, and SQLite's deferred
    transaction begins at the first write — so a write anywhere in the block
    takes a WAL write lock that is then held across every later await, and every
    other writer in the daemon queues behind it for the length of a round trip.

    Whether that costs anything in production was inference when ISSUE-406 was
    filed. This is the measurement that decides it, and it is deliberately the
    whole of what that issue got: restructuring the daemon's busiest poll path
    on a hypothesis is the trade the issue itself declined.

    **Both blocks await on a recurring basis, not only on first sight**, which
    is worth stating because the opposite reading is the natural one and it is
    wrong. `get_latest_message_id` persists a cursor only when the room has a
    message, and `fetch_chat_history` caches only when it returns something —
    so an *empty* group room fails both guards on every cycle for ever, and does
    two round trips inside the write transaction each time. The comment at the
    cursor write records the same fact from the gate's point of view. The room
    loop's participant fetch recurs too, for any room where
    `_istota_members_for_conversation` comes back empty, since nothing is then
    registered and the next cycle takes the same branch.

    The line is emitted at **close**, so the window it describes is the
    `held_ms` before its own timestamp — which is what makes it possible to line
    a hold up against the `ReadTimeout` records the issue is trying to explain.
    """
    hold = _TxnHold(label=label, opened=time.monotonic())
    try:
        yield hold
    finally:
        _report_poll_txn(hold, time.monotonic() - hold.opened)


async def _await_in_txn(hold: "_TxnHold", coro):
    """Await ``coro`` and charge its wall time to an open transaction.

    Charged only if it took at least ``_TXN_AWAIT_FLOOR_SECONDS`` — see
    ``_TxnHold`` for why the floor is per await and not on the total.
    """
    start = time.monotonic()
    try:
        return await coro
    finally:
        elapsed = time.monotonic() - start
        if elapsed >= _TXN_AWAIT_FLOOR_SECONDS:
            hold.awaits += 1
            hold.await_seconds += elapsed


def _report_poll_txn(hold: "_TxnHold", held_seconds: float) -> None:
    """Emit the `talk_poll_txn` line, or nothing.

    **A data format, not chatter.** Fixed field order, `key=value`,
    space-separated, matching `scheduler_stats` and the host-pressure
    breadcrumb; a rename is a breaking change for whoever is grepping a journal.

    Nothing is said about a transaction that neither waited on the network nor
    ran long, which is most of them on a settled deployment.

    **INFO, not DEBUG**, matching `scheduler_stats` and the host-pressure
    breadcrumb. The distribution this exists to collect is the sub-second
    population, and `setup_logging` puts the `istota` logger and both its
    handlers at the configured level, which defaults to INFO — so at DEBUG the
    only lines that survive are the ones already past the warn threshold, and
    the instrument ships unable to measure the thing it was built for. The
    alternative is asking an operator to run the whole tree at DEBUG for days.
    The floor and the caches are what keep the volume down instead of the level.

    Never raises. The caller is a `finally`, where an exception here would
    replace whatever the block was already propagating, and it runs on the
    daemon's busiest loop.
    """
    try:
        slow = held_seconds >= _TXN_HOLD_WARN_SECONDS
        if not hold.awaits and not slow:
            return
        line = (
            f"talk_poll_txn phase={hold.label} "
            f"held_ms={held_seconds * 1000:.0f} "
            f"awaits={hold.awaits} "
            f"await_ms={hold.await_seconds * 1000:.0f}"
        )
        if slow:
            _POLL_TXN_LOGGER.warning(line)
        else:
            _POLL_TXN_LOGGER.info(line)
    except Exception:  # noqa: BLE001 — instrumentation must not fail the poll
        pass


def _is_multi_user(participants: list[dict]) -> bool:
    """Return True if 3+ participants (requires @mention)."""
    return len(participants) >= 3


def _participant_names(participants: list[dict], exclude: str | None = None) -> list[str]:
    """Extract display names from participant list, excluding a specific actor."""
    names = []
    for p in participants:
        actor_id = p.get("actorId", "")
        if exclude and actor_id == exclude:
            continue
        name = p.get("displayName") or actor_id
        if name:
            names.append(name)
    return names


def _istota_members_for_conversation(
    conv: dict, participants: list[dict], config: Config,
) -> list[str]:
    """The istota user_ids who are human participants of a Talk conversation,
    for seeding `room_members` when the room is first registered from a poll.

    Uses the same actor→user gate as message processing (`actor_id in
    config.users`, bot excluded), so membership matches who could actually
    drive a task in the room. For a DM (type 1) the participant list is empty,
    so the other party comes from `conv['name']`. Returned sorted for a
    deterministic room owner. Empty when no istota human participates (a
    bot-only or all-guest room) — the caller then skips registration."""
    bot = config.talk.bot_username
    members: set[str] = set()
    if conv.get("type") == 1:
        other = conv.get("name", "")
        if other and other != bot and other in config.users:
            members.add(other)
    else:
        for p in participants:
            # Mirror the message-processing gate: only real users (not guests /
            # federated / bots), and only those mapped to an istota user.
            if p.get("actorType", "users") != "users":
                continue
            actor_id = p.get("actorId", "")
            if actor_id and actor_id != bot and actor_id in config.users:
                members.add(actor_id)
    return sorted(members)


async def _poll_single_conversation(
    client: TalkClient,
    conversation_token: str,
    last_message_id: int | None,
    timeout: int,
) -> tuple[str, list[dict]]:
    """
    Poll a single conversation for new messages.

    Returns (conversation_token, messages) tuple.
    """
    try:
        messages = await client.poll_messages(
            conversation_token,
            last_known_message_id=last_message_id,
            timeout=timeout,
        )
        return (conversation_token, messages)
    except Exception as e:
        # The type is part of the message because several of the shapes seen in
        # production stringify to nothing at all (httpx.ReadError and friends),
        # which logged as "Error polling conversation X: " with no cause.
        logger.error(
            "Error polling conversation %s: %s: %s",
            conversation_token, type(e).__name__, e,
        )
        return (conversation_token, [])


def _reconcile_webmirror_stamp(
    conn, config: Config, conversation_token: str, reference_id: str,
    msg: dict,
) -> None:
    """Repair a post-as-user mirror whose send-time stamp never landed, from
    the Talk echo of the message itself (ISSUE-287). Best-effort, never raises.

    Nothing here may raise, and that is the whole shape of this function. The
    caller's `conn` is the poll batch's single transaction — it spans every
    conversation in the batch, the poll-cursor advance and every
    `ingest_message` — and `db.get_db` commits only on the non-exception path.
    An exception escaping here therefore discards the whole batch, and since
    the cursor never advances the same message is re-polled and re-raises on
    every tick: one message would stop all Talk inbound, permanently. That
    matters because every input is attacker-chosen. `referenceId` is free text
    on Talk's chat API, and this block deliberately runs *ahead* of the bot,
    actor-type and known-user filters (a genuine echo is authored by a user and
    must be skipped before any of them), so the sender need not even be a
    configured user.

    Hence a strict parse rather than a permissive one: `str.isdigit()` is true
    for characters `int()` refuses outright (`'²'`), and true for non-ASCII
    decimal digits that `int()` silently folds onto an ASCII value (`'١٢٣'` →
    123) — a row id the producer could never have written. The producer emits
    ASCII decimal digits, so that is the whole accepted alphabet, and the
    `int()` still runs under `except` because a long-enough run of them
    overflows SQLite's INTEGER on binding rather than on conversion.
    """
    try:
        mirrored = reference_id[len(WEBMIRROR_REF_PREFIX):]
        talk_id = msg.get("id")
        actor_id = msg.get("actorId") or ""
        if not talk_id or not mirrored.isascii() or not mirrored.isdecimal():
            return
        # A deleted mirror keeps its referenceId, so reconciling from it would
        # stamp an id that no longer resolves — and the stamp suppresses the
        # repost, leaving the question absent from Talk entirely. Cheap to
        # refuse, and correct whichever way Talk reports a deletion.
        if msg.get("messageType") == "comment_deleted" or msg.get("deleted"):
            return
        # The echo's author is the evidence, so the actor has to be a real,
        # known user before it is worth comparing (`stamp_webmirror_echo`
        # makes the comparison itself). A guest or a foreign actor carrying a
        # forged reference stops here.
        if msg.get("actorType") != "users" or actor_id not in config.users:
            return
        canonical_token = (
            db.resolve_room_token(conn, "talk", conversation_token)
            or conversation_token
        )
        if db.stamp_webmirror_echo(
            conn, canonical_token, int(mirrored), str(talk_id), actor_id,
        ):
            logger.info(
                "Reconciled unstamped web-mirror turn %s in room %s from its "
                "Talk echo (talk id %s)",
                mirrored, canonical_token, talk_id,
            )
    except Exception as e:  # noqa: BLE001 — must never abort the poll batch
        logger.warning(
            "web-mirror stamp reconciliation failed in %s (ref=%s): %s: %s",
            conversation_token, reference_id, type(e).__name__, e,
        )


@dataclass
class _RoomPlan:
    """One room's work for a poll cycle, carried across the three phases of the
    room pass (ISSUE-406).

    The read phase fills everything down to `needs_backfill` from the registry,
    the fetch phase fills the three network fields with no connection open, and
    the write phase applies the result. Nothing here is authoritative by the
    time the write phase reads it — see `_apply_room_pass`, which re-reads every
    condition it acts on.
    """

    conv: dict
    token: str
    conv_type: int | None
    display_name: str | None
    canonical: str
    # The cursor as the read phase found it. `None` means the gate has nothing
    # to compare against and must not hold the room back.
    known_cursor: int | None
    last_message_id: int | None
    needs_participants: bool
    needs_cursor_init: bool
    needs_backfill: bool
    # Filled by the fetch phase.
    participants: list[dict] | None = None
    latest_id: int | None = None
    backfill_msgs: list[dict] | None = None
    cursor_init_failed: bool = False


def _plan_room_pass(
    conn,
    config: Config,
    conversations: list[dict],
    conv_types: dict[str, int],
    conv_names: dict[str, str],
) -> list[_RoomPlan]:
    """Read what the registry says about each conversation. Reads only.

    Not one write, which is the property that matters rather than a style
    preference: under WAL a transaction that never writes never becomes a
    writer, so this block blocks nobody however long it takes. `db.get_db`'s
    `PRAGMA synchronous` is per-connection and takes no lock either.
    """
    plans: list[_RoomPlan] = []
    for conv in conversations:
        conversation_token = conv.get("token")
        if not conversation_token:
            continue

        # Conversation types: 1=one-to-one (DM), 2=group, 3=public, 4=changelog
        conv_type = conv.get("type")
        conv_types[conversation_token] = conv_type
        display_name = conv.get("displayName") or conv.get("name")
        if display_name:
            conv_names[conversation_token] = display_name

        # Resolve the canonical token FIRST: a *promoted* web room's canonical
        # token is its web token (the Talk token lives only in a binding), so
        # keying by the raw Talk token would create a phantom duplicate
        # origin='talk' row.
        canonical = (
            db.resolve_room_token(conn, "talk", conversation_token)
            or conversation_token
        )
        # Only a genuinely new room needs the participant fetch. Type 4 is the
        # "Talk updates" changelog room, which shouldn't surface in web chat.
        needs_participants = (
            db.get_room(conn, canonical) is None and conv_type != 4
        )

        # Cache 1:1 DM tokens by user ID (for notification fallback)
        if conv_type == 1:
            other_user = conv.get("name", "")
            if other_user and other_user in config.users:
                _dm_token_cache[other_user] = conversation_token

        last_message_id = db.get_talk_poll_state(conn, conversation_token)

        # The gate needs a cursor we have actually seen. A room being polled for
        # the first time has none, and the write phase invents one from the
        # server's own latest id — comparing the room list against that would
        # skip the very message the initialisation is there to catch.
        known_cursor = last_message_id

        needs_cursor_init = False
        if last_message_id is None:
            if conv_type == 1:
                # DM: fetch recent messages — the DM is initiated by messaging
                # the bot, so there's no historical spam risk. 0 triggers a
                # history fetch.
                last_message_id = 0
                logger.debug(
                    "First poll for DM %s - fetching message history",
                    conversation_token,
                )
            else:
                needs_cursor_init = True

        plans.append(_RoomPlan(
            conv=conv,
            token=conversation_token,
            conv_type=conv_type,
            display_name=display_name,
            canonical=canonical,
            known_cursor=known_cursor,
            last_message_id=last_message_id,
            needs_participants=needs_participants,
            needs_cursor_init=needs_cursor_init,
            needs_backfill=not db.has_cached_talk_messages(
                conn, conversation_token,
            ),
        ))
    return plans


async def _fetch_room_pass(
    client: TalkClient, config: Config, plans: list[_RoomPlan],
) -> None:
    """Ask Nextcloud for what the write phase needs, with nothing open.

    Deliberately **sequential**, one room after another, which is what the
    single-transaction version did. Running these concurrently is a real
    latency win and a separate change with its own risk: ISSUE-399 was about
    the cost of the number of connections this poller opens at once, and
    widening that is not something to do in passing while fixing a lock.
    """
    for plan in plans:
        if plan.needs_participants:
            # Never raises — `_get_participants` treats a failure as a DM.
            plan.participants = await _get_participants(
                client, plan.token, plan.conv_type,
            )

        if plan.needs_cursor_init:
            try:
                plan.latest_id = await client.get_latest_message_id(plan.token)
            except Exception as e:
                logger.error(
                    "Error initializing poll state for %s: %s", plan.token, e,
                )
                # Pass over this room for the cycle, exactly as the
                # single-transaction version's `continue` did — including
                # skipping the backfill below it.
                plan.cursor_init_failed = True
                continue

        if plan.needs_backfill:
            try:
                plan.backfill_msgs = await client.fetch_chat_history(
                    plan.token, limit=config.conversation.talk_context_limit,
                )
            except Exception as e:
                logger.warning(
                    "Backfill failed for %s: %s — context will build from polling",
                    plan.token, e,
                )


def _apply_room_pass(
    conn,
    config: Config,
    client: TalkClient,
    plans: list[_RoomPlan],
    *,
    full_sweep: bool,
) -> tuple[list, int]:
    """Apply the room pass and return the polls to open plus the gated count.

    Writes only, and no await — this is the block that takes the write lock, and
    the whole point of the split is that nothing in it waits on a socket.

    **Every condition is re-read here rather than trusted from the plan.** The
    lock was free while Nextcloud answered, which is the fix, and that is
    exactly the window in which another writer can register the room or advance
    its cursor. `register_room` / `add_room_binding` / `add_room_member` are all
    `INSERT OR IGNORE` and need no help, but `set_talk_poll_state` is an
    unconditional upsert: writing `latest_id - 1` over a cursor somebody
    advanced would rewind the room and re-poll messages already read.
    """
    poll_tasks = []
    gated = 0

    for plan in plans:
        if plan.cursor_init_failed:
            continue

        existing_room = db.get_room(conn, plan.canonical)
        if existing_room is not None:
            # Backfill the registry title from Talk's displayName (migrated
            # rooms were folded in with NULL names; without this they'd show the
            # generic "Talk room" until their next message). Talk-origin only —
            # a web-origin (incl. promoted) room's user-set name wins.
            if (
                existing_room.origin == "talk"
                and plan.display_name
                and existing_room.name != plan.display_name
            ):
                db.rename_room(conn, plan.canonical, plan.display_name)
        elif plan.conv_type != 4 and plan.participants is not None:
            # Register the Talk room in the unified registry on first sight so
            # it surfaces in web chat even when no one has messaged the bot in
            # it yet — the task-keyed unified-rooms migration and the live
            # record_inbound path both miss a room the bot merely lurks in
            # (polled + history-cached, but never addressed): the #sysadmin
            # case. Seed membership from the human participants mapped to istota
            # users (bot excluded). A user who later hides the room is kept out
            # by their dismissal tombstone.
            #
            # `participants is not None` covers the room the read phase found
            # present and this one finds gone: nothing fetched a participant
            # list for it, and `_istota_members_for_conversation` iterates that
            # list for anything but a DM, so the alternative is a `TypeError`
            # out of the poll. It registers on the next cycle instead. Slightly
            # conservative for a DM, which reads its member off `conv['name']`
            # and would have registered — one cycle, against a second branch.
            member_ids = _istota_members_for_conversation(
                plan.conv, plan.participants, config,
            )
            if member_ids:
                db.register_room(
                    conn, plan.canonical, member_ids[0],
                    origin="talk", name=plan.display_name,
                )
                db.add_room_binding(conn, plan.canonical, "talk", plan.token)
                for uid in member_ids[1:]:
                    db.add_room_member(conn, plan.canonical, uid)

        last_message_id = plan.last_message_id
        if plan.needs_cursor_init:
            if plan.latest_id:
                last_message_id = plan.latest_id - 1
                # Persist it. The only other writer is the message loop, which
                # fires solely for a message a poll actually returned — so a
                # room whose polls keep coming back empty never acquired a
                # cursor at all, and `known_cursor is None` bypassed the gate on
                # every cycle for ever. `latest_id - 1` is behind the newest
                # message by construction, so the next poll still returns it
                # (ISSUE-399 review).
                #
                # Only when the cursor is *still* absent: see the re-read note
                # in this function's docstring.
                if db.get_talk_poll_state(conn, plan.token) is None:
                    db.set_talk_poll_state(conn, plan.token, last_message_id)
                logger.debug(
                    "First poll for room %s - starting from message %d",
                    plan.token, last_message_id,
                )
            else:
                last_message_id = 0
                logger.debug(
                    "First poll for room %s - no messages yet", plan.token,
                )

        if plan.needs_backfill and plan.backfill_msgs:
            db.upsert_talk_messages(conn, plan.token, plan.backfill_msgs)
            logger.info(
                "Backfilled %d messages for conversation %s",
                len(plan.backfill_msgs), plan.token,
            )

        # The gate: on an ordinary cycle, skip a room the room list says holds
        # nothing we have not already read. This is what stops the cycle opening
        # one long-poll per room around the clock — 97% of which were being
        # abandoned client-side having carried nothing (ISSUE-399). A full sweep
        # ignores it.
        if (
            not full_sweep
            and plan.known_cursor is not None
            and not _has_news(plan.conv, plan.known_cursor)
        ):
            gated += 1
            continue

        poll_tasks.append(
            _poll_single_conversation(
                client,
                plan.token,
                last_message_id,
                config.scheduler.talk_poll_timeout,
            )
        )

    return poll_tasks, gated


async def poll_talk_conversations(config: Config) -> list[int]:
    """
    Poll all Talk conversations concurrently for new messages and create tasks.

    This is the Talk transport's inbound body (``TalkTransport.poll`` delegates
    here). It owns every Talk-protocol-specific step — conversation listing +
    cache, per-room long-poll, system/own/unknown-user/unmentioned filtering,
    ``!model`` prefix parsing, ``!command`` dispatch, confirmation-reply
    handling, the per-channel active-task gate, attachment extraction, and
    cancelling superseded confirmations.

    Task creation (via ``ingest_message``) happens in the **same** ``db.get_db``
    transaction as ``set_talk_poll_state`` / ``cancel_pending_confirmations`` /
    ``!command`` dispatch, so a ``create_task`` failure rolls the whole batch
    back and the messages are re-polled next cycle rather than silently lost
    (the poll-cursor advance is never committed without the task). This is why
    Talk — like email — self-creates inside ``poll`` rather than handing
    un-ingested ``IncomingMessage``s back to a driver across a transaction
    boundary.

    Uses asyncio.wait() with a timeout so fast rooms are processed immediately
    without waiting for slow (quiet) rooms to finish their long-poll.

    Returns the list of created task IDs.
    """
    if not config.talk.enabled:
        return []

    if not config.nextcloud.url:
        return []

    global _conversation_cache, _last_full_sweep

    client = get_talk_client(config)
    created: list[int] = []

    now = time.monotonic()

    # A cycle is either gated or a full sweep. Gated, only a room whose
    # `lastMessage` is newer than our cursor is long-polled; on a sweep every
    # room is, which is what bounds the cost of a gate that reads wrongly.
    sweep_interval = config.scheduler.talk_poll_full_sweep_interval
    full_sweep = (
        sweep_interval <= 0
        or _last_full_sweep is None
        or now - _last_full_sweep >= sweep_interval
    )

    # Get all conversations, using cache to avoid blocking every cycle.
    #
    # The gate reads `lastMessage` out of this payload, so on a gated cycle the
    # cache would be a stale gate: at a 60s TTL and a 10s poll interval six
    # consecutive cycles read one snapshot, and a message arriving just after a
    # refresh would be held for the rest of the minute — worse inbound latency
    # than the long-poll the gate replaces. So a gated cycle always refetches,
    # and the cache degrades to what it is still needed for: the last known
    # room list for a cycle where Nextcloud did not answer. One short request
    # per cycle against N held ones is the trade (ISSUE-399).
    cache_valid = (
        not _gate_enabled(config)
        and _conversation_cache is not None
        and now - _conversation_cache[1] < _CONVERSATION_CACHE_TTL
    )

    if cache_valid:
        conversations = _conversation_cache[0]
    else:
        try:
            conversations = await client.list_conversations()
            _conversation_cache = (conversations, now)
        except Exception as e:
            if _conversation_cache is not None:
                logger.debug(
                    "list_conversations failed (%s: %s), using cached list (%d rooms)",
                    type(e).__name__, e, len(_conversation_cache[0]),
                )
                conversations = _conversation_cache[0]
                # A list the server did not just hand us says which rooms exist
                # and nothing about what is new in them. Its `lastMessage` is
                # frozen at the last successful fetch while the cursors keep
                # advancing from whatever the polls return, so once a cursor
                # reaches that frozen id the room gates shut on every cycle for
                # as long as the listing keeps failing — an outage that used to
                # cost nothing would instead cut inbound to one sweep per
                # `talk_poll_full_sweep_interval`. So a stale list ungates the
                # cycle rather than gating it (ISSUE-399 review).
                full_sweep = True
            else:
                logger.warning("Error listing Talk conversations: %s: %s", type(e).__name__, e)
                return []

    # Build list of conversations to poll and initialize new ones.
    #
    # Three phases, and the split is the fix rather than a tidy-up (ISSUE-406).
    # This used to be one `db.get_db` block that read the registry, wrote to it,
    # awaited Nextcloud and wrote again. `db.get_db` commits at the end of the
    # `with` and SQLite's deferred transaction becomes a *writer* at the first
    # write, so every await after that first write was a WAL write lock held
    # across a round trip — and every other writer in the daemon queued behind
    # it, each for up to `db.get_db`'s 30s wait. Readers were unaffected, which
    # is why it went unseen.
    #
    # It also recurred rather than being first-encounter work: a group room
    # nobody has written in fails both the cursor guard and the history-cache
    # guard on every cycle for ever, and paid two round trips each time.
    #
    # So: read what the registry says, close, ask Nextcloud, reopen to write.
    # The results block below cannot be split this way — see the atomicity note
    # in this function's docstring — and keeps its instrumented awaits.
    conv_types: dict[str, int] = {}  # token -> conversation type
    conv_names: dict[str, str] = {}  # token -> display name (lazy room registration)

    with db.get_db(config.db_path) as conn:
        plans = _plan_room_pass(conn, config, conversations, conv_types, conv_names)

    await _fetch_room_pass(client, config, plans)

    with _timed_poll_txn("rooms"), db.get_db(config.db_path) as conn:
        poll_tasks, gated = _apply_room_pass(
            conn, config, client, plans, full_sweep=full_sweep,
        )

        # Reconcile the unified registry against Nextcloud: a Talk room the bot
        # is no longer in (deleted in NC, or bot removed) drops out of the
        # conversation list, so archive its registry row — otherwise it keeps
        # surfacing in the web room list forever. `conversations` is the bot's
        # *complete* room list; only reconcile when it's non-empty so a transient
        # empty/failed fetch can't mass-archive every room.
        #
        # Only on a full sweep. Gating made a quiet cycle return without
        # awaiting anything, so cycles now come round roughly every
        # `talk_poll_interval` instead of every `talk_poll_timeout +
        # talk_poll_interval` — and this is a mass-archive whose only guard is
        # that the token set is non-empty, so a listing that came back
        # truncated for any reason archives every room missing from it. Room
        # membership changes on a human timescale and does not need re-deciding
        # every ten seconds; running it on the sweep keeps the number of draws
        # against that guard where it was (ISSUE-399 review).
        if full_sweep:
            live_talk_tokens = {
                c.get("token") for c in conversations if c.get("token")
            }
            if live_talk_tokens:
                n = db.archive_orphaned_talk_rooms(conn, live_talk_tokens)
                if n:
                    logger.info("Archived %d Talk room(s) no longer in Nextcloud", n)

    # Stamped here rather than beside the decision above, so the timestamp
    # records a sweep that happened. Stamped early, a cycle that died on the
    # listing fetch or anywhere in the room loop still spent the credit, and
    # the next sweep was deferred a full interval — which matters because the
    # sweep is the gate's only safety net (ISSUE-399 review).
    if full_sweep:
        _last_full_sweep = now

    if gated or full_sweep:
        logger.debug(
            "Talk poll: %d room(s) polled, %d held by the lastMessage gate%s",
            len(poll_tasks), gated, " (full sweep)" if full_sweep else "",
        )

    if not poll_tasks:
        return []

    # Poll all conversations concurrently using long-poll for responsiveness.
    # FIRST_COMPLETED preserves instant detection (server responds immediately
    # when a message arrives) while not blocking on quiet rooms.  Once any room
    # responds, give remaining rooms a brief grace period then move on.
    #
    # The gate has to outlast the server's own long-poll rather than equal it.
    # `talk_poll_timeout` is sent to Nextcloud as the `timeout` parameter, so it
    # is how long the *server* holds the request; the answer to a request held
    # for N seconds cannot arrive before N seconds have passed. A gate of
    # exactly N therefore expires just as every room is about to reply, and
    # since only `done` is read below, their messages are dropped and refetched
    # a poll interval later. `talk_poll_wait` is the slack, the same allowance
    # already used for stragglers (ISSUE-399: a deployment set the timeout to 1
    # to stop holding a PHP worker per room, at which point the skew was the
    # whole window and every cycle opened a full round of connections and
    # abandoned them).
    gate = config.scheduler.talk_poll_timeout + config.scheduler.talk_poll_wait
    tasks = [asyncio.create_task(t) for t in poll_tasks]
    done, pending = await asyncio.wait(
        tasks,
        timeout=gate,
        return_when=asyncio.FIRST_COMPLETED,
    )

    # If a room responded and others are still long-polling, give them a
    # short window to return before cancelling (covers near-simultaneous msgs)
    if done and pending:
        more_done, pending = await asyncio.wait(
            pending, timeout=config.scheduler.talk_poll_wait,
        )
        done |= more_done

    for t in pending:
        t.cancel()
    # Suppress CancelledError from cancelled tasks
    await asyncio.gather(*pending, return_exceptions=True)
    results = [t.result() for t in done]

    # Process results
    with _timed_poll_txn("results") as hold, db.get_db(config.db_path) as conn:
        for conversation_token, messages in results:
            if not messages:
                continue

            # Store all messages in cache (system, bot, user — context builder filters)
            db.upsert_talk_messages(conn, conversation_token, messages)

            # Process messages in order (oldest first)
            for msg in messages:
                message_id = msg.get("id")
                actor_id = msg.get("actorId", "")  # Nextcloud username
                actor_type = msg.get("actorType", "")
                message_type = msg.get("messageType", "")

                # Update poll state to this message
                if message_id:
                    db.set_talk_poll_state(conn, conversation_token, message_id)

                # Skip system messages
                if message_type == "system":
                    continue

                # Skip the Talk echo of a web-origin user turn the web process
                # posted *as the user* (post-as-user mirroring). The marker
                # travels inside the message payload, so this works even when
                # the long-poll delivers the echo before the external-id stamp
                # lands in the DB. The message is user-authored (the bot-own
                # filter below can't catch it); the poll cursor has already
                # advanced and the talk_messages cache upsert above kept it —
                # the turn is legitimately part of the conversation context.
                #
                # The echo is also Nextcloud's own confirmation that the
                # message exists, so it is where a stamp that never arrived at
                # all gets repaired: `_post_as_user` gives up at 5 s on a post
                # the server went on to store, and the unstamped turn then drew
                # the scheduler's legacy attributed repost on top of it
                # (ISSUE-287). Handling a stamp that lands *late* was never
                # enough — one that never lands needs reconciling from here.
                reference_id = msg.get("referenceId") or ""
                if isinstance(reference_id, str) and reference_id.startswith(
                    WEBMIRROR_REF_PREFIX
                ):
                    _reconcile_webmirror_stamp(
                        conn, config, conversation_token, reference_id, msg,
                    )
                    logger.debug(
                        "Skipping web-mirror echo in %s (ref=%s)",
                        conversation_token, reference_id,
                    )
                    continue

                # Skip bot's own messages
                if actor_id == config.talk.bot_username:
                    continue

                # Only process messages from users (not guests, bots, etc.)
                if actor_type != "users":
                    continue

                # Check if sender is a configured user
                if actor_id not in config.users:
                    # Unknown user - skip silently
                    continue

                # Re-engagement un-hides: any message the user posts in a room
                # they'd hidden clears their dismissal tombstone (and re-adds
                # their membership), so it resurfaces in their web list — even
                # in a multi-user room where the message is dropped just below
                # for lacking an @mention (so record_inbound is never reached).
                # Resolve to the canonical token so a promoted web room works.
                reengaged_token = (
                    db.resolve_room_token(conn, "talk", conversation_token)
                    or conversation_token
                )
                if db.get_room(conn, reengaged_token) is not None:
                    db.add_room_member(conn, reengaged_token, actor_id)
                    db.undismiss_room(conn, reengaged_token, actor_id)

                # In multi-user rooms, only respond when @mentioned
                conv_type = conv_types.get(conversation_token, 1)
                participants = await _await_in_txn(
                    hold, _get_participants(client, conversation_token, conv_type),
                )
                is_multi_user = _is_multi_user(participants)
                if is_multi_user and not is_bot_mentioned(msg, config.talk.bot_username):
                    logger.debug(
                        "Skipping message from %s in multi-user room %s (no @mention)",
                        actor_id, conversation_token,
                    )
                    continue

                # Extract message content and attachments
                # In multi-user rooms, strip bot mention from prompt and resolve other mentions
                content = clean_message_content(
                    msg,
                    bot_username=config.talk.bot_username if is_multi_user else None,
                )
                attachments = extract_attachments(msg)

                # !model prefix — strip and capture per-task overrides before
                # dispatching commands or creating the task. Unknown alias →
                # post usage and stop; valid alias → carry overrides forward.
                # The active brain owns the alias namespace.
                model_override: str | None = None
                effort_override: str | None = None
                from ...brain import make_brain
                from ...commands import (
                    ModelPrefixOutcome,
                    brain_for_room,
                    dispatch as dispatch_command,
                    is_model_prefix,
                    resolve_model_prefix,
                )

                # !model prefix — strip and capture per-task overrides via the
                # shared cross-surface helper. Empty remainder is only an error
                # when there's nothing to do at all; with attachments present
                # "!model opus" is a valid "process this attachment" intent.
                #
                # The alias namespace belongs to the brain this *room* runs, not
                # to the deployment default, so a room pinned to another
                # namespace can never be handed an id it cannot resolve — nor
                # offered one in the unknown-alias usage line, which
                # `resolve_model_prefix` builds from the same instance.
                # `conversation_token` here is the Talk-native ref (canonical
                # resolution happens later, inside `record_inbound`), so it is
                # mapped first; `conn` is the poll batch's transaction and spans
                # this point.
                # Gated on the prefix actually being there. The brain is now the
                # *room's*, and building one constructs a provider client that
                # nothing closes — per message, for every message, on a room
                # pinned to native. `is_model_prefix` is the same test
                # `parse_model_prefix` makes first, so nothing that used to
                # match stops matching.
                if is_model_prefix(content):
                    prefix = resolve_model_prefix(
                        content,
                        make_brain(brain_for_room(
                            config,
                            conn,
                            db.resolve_room_token(conn, "talk", conversation_token)
                            or conversation_token,
                            "talk",
                        )),
                        has_attachments=bool(attachments),
                    )
                else:
                    prefix = ModelPrefixOutcome(matched=False, content=content)
                if prefix.usage is not None:
                    await _await_in_txn(
                        hold,
                        client.send_message(conversation_token, prefix.usage),
                    )
                    continue
                if prefix.matched:
                    model_override = prefix.model
                    effort_override = prefix.effort
                    content = prefix.content

                # !command dispatch — intercept before task creation
                if content.strip().startswith("!"):
                    result = await _await_in_txn(
                        hold,
                        dispatch_command(
                            config, actor_id, conversation_token, content,
                            surface="talk", conn=conn,
                        ),
                    )
                    if result.handled:
                        continue

                # Extract reply metadata (before confirmation check so we can
                # match reply-to-specific confirmation prompts)
                reply_to_talk_id = None
                reply_to_content = None
                parent = msg.get("parent")
                if isinstance(parent, dict) and parent.get("id") and not parent.get("deleted"):
                    reply_to_talk_id = parent["id"]
                    # Store parent message content as fallback
                    parent_content = parent.get("message", "")
                    if parent_content:
                        reply_to_content = parent_content[:1000]

                # Check if this is a confirmation reply before creating a new task
                handled = await _await_in_txn(
                    hold,
                    handle_confirmation_reply(
                        conn, config, actor_id, content, conversation_token,
                        reply_to_talk_id=reply_to_talk_id,
                    ),
                )
                if handled:
                    continue

                # Per-channel gate: notify user if there's already an active fg task
                # but still queue the message (fall through to task creation)
                if db.has_active_foreground_task_for_channel(conn, conversation_token):
                    logger.debug(
                        "Channel gate: active fg task in %s, queuing message from %s",
                        conversation_token, actor_id,
                    )
                    try:
                        await _await_in_txn(
                            hold,
                            client.send_message(
                                conversation_token,
                                "Still working on a previous request — I'll be with you shortly.",
                            ),
                        )
                    except Exception as e:
                        logger.debug("Failed to send channel gate message: %s", e)

                # Skip empty messages (file-only shares have empty content)
                if not content.strip() and not attachments:
                    continue

                # Build prompt
                prompt = content.strip() if content.strip() else "Process the attached file(s)"

                # For group chats, prepend participant context so the bot
                # knows who else is in the room
                if is_multi_user and participants:
                    other_names = _participant_names(participants, exclude=config.talk.bot_username)
                    if other_names:
                        prompt = f"[Room participants: {', '.join(other_names)}]\n{prompt}"

                # Cancel any pending confirmations in this conversation —
                # the user has moved on by sending a new message
                cancelled = confirmations.cancel_for_conversation(
                    conn, conversation_token, actor_id, by="talk",
                )
                if cancelled:
                    logger.info(
                        "Cancelled %d pending confirmation(s) in %s for %s (new message)",
                        cancelled, conversation_token, actor_id,
                    )

                # Normalize into an IncomingMessage and create the task in the
                # SAME transaction as the poll-state advance above — see the
                # docstring's atomicity note.
                task_id = ingest_message(conn, config, IncomingMessage(
                    user_id=actor_id,
                    text=prompt,
                    source_type="talk",
                    surface="talk",
                    channel_token=conversation_token,
                    channel_name=conv_names.get(conversation_token),
                    is_group_chat=is_multi_user,
                    attachments=attachments if attachments else [],
                    platform_message_id=message_id,
                    reply_to_message_id=reply_to_talk_id,
                    reply_to_content=reply_to_content,
                    model=model_override,
                    effort=effort_override,
                    model_prefix_used=prefix.matched,
                ))
                if task_id is not None:
                    created.append(task_id)

    return created


async def handle_confirmation_reply(
    conn,
    config: Config,
    actor_id: str,
    content: str,
    conversation_token: str,
    reply_to_talk_id: int | None = None,
) -> bool:
    """
    Check if a message is a confirmation reply to a pending task.

    Talk-specific half only. The word lists, the three-path lookup and the ack
    text live in ``confirmations`` (ISSUE-243) so the web composer answers
    identically; what stays here is reading the reply's Talk parent id, posting
    the ack to the room, and recording the exchange in that room's canonical
    transcript so the web view of the same room shows it too (ISSUE-242).

    Returns True if the message was handled as a confirmation. False means it
    was not an answer — a bare "yes" with nothing parked is an ordinary message
    and must fall through to task creation.
    """
    answer = confirmations.parse_answer(content)
    if answer is None:
        return False

    # A per-surface ref maps to the canonical room — a promoted web room's Talk
    # token is not its own token — and a task parks under the *canonical* one.
    # Resolving before the lookup rather than only before the transcript write
    # is what keeps Path B working there: unresolved, a same-room answer misses
    # and falls to Path C, which with a second question open answers neither.
    room_token = (
        db.resolve_room_token(conn, "talk", conversation_token)
        or conversation_token
    )

    res = confirmations.resolve(
        conn, actor_id,
        conversation_token=room_token,
        talk_response_id=reply_to_talk_id,
    )
    if res.ambiguous:
        # Nothing decided, so nothing recorded — see the web sibling.
        await _post_ack(
            config, conversation_token,
            confirmations.ambiguity_listing(conn, res.ambiguous),
        )
        return True
    if res.task is None:
        return False

    ack = confirmations.apply_answer(conn, res.task, answer, config, by="talk")
    await _post_ack(config, conversation_token, ack)
    confirmations.record_exchange(
        conn, room_token, answer_text=content, ack=ack, origin_surface="talk",
        answered_by=actor_id,
    )
    return True


async def _post_ack(config: Config, conversation_token: str, ack: str) -> None:
    """Post a confirmation ack to Talk. Best-effort.

    The answer has already been recorded and is what the user asked for, so a
    failed post must not report the reply as unhandled — that would fall
    through to task creation and turn "yes" into a prompt.
    """
    try:
        client = get_talk_client(config)
        await client.send_message(conversation_token, ack)
    except Exception:
        logger.warning(
            "Could not post the confirmation ack to %s", conversation_token,
            exc_info=True,
        )
