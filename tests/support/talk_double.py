"""A Talk client double that refuses a token Nextcloud would refuse.

Every delivery test in the tree used to patch `get_talk_client` with a bare
`MagicMock` whose methods are `AsyncMock`s, and several non-delivery tests
still do. Those accept any string, which is exactly why ISSUE-400 was
invisible: such a double is *more permissive than Nextcloud*, so a call handing
a room's canonical `web-…` token to the Talk API — the thing that 404s in
production — looks identical to a call that resolved the room's `talk` binding
first. On an ordinary Talk room the two strings are equal, so the mistake only
shows on a promoted room, and no test built one.

One rule, applied on every method taking a conversation token: accept it if it
is a `talk` `surface_ref` in `room_bindings`, or if it is in `known_channels`.
Anything else raises `UnknownTalkRoom`. That is what Nextcloud does, and it
turns a misroute into a red test with no assertion written — which is the point,
because a delivery path added later is then covered by whichever existing test
walks it.

**A test asserting "no exception was raised" proves nothing here.** The product
swallows: `scheduler.edit_talk_message` catches everything and returns False,
`TalkTransport.deliver` catches and returns None, `inbound._post_ack` catches
and logs. `UnknownTalkRoom` is swallowed by all three exactly as a real 404
would be. So a converted test asserts on `calls` or on the return value. The
refusal is recorded in `calls` with `refused=True` precisely so it is reachable
after the product has eaten the exception.

**Why the lookup is raw SQL rather than `db.resolve_room_token`.** The double
exists to catch a call that resolved the binding wrongly or not at all; routing
its own decision through the same function the product calls would make a bug in
that function invisible to the instrument built to catch it. Same reasoning as
the spec's rejection of resolving `known_channels` through
`notifications.resolve_conversation_token`.

**The lookup runs per call, not at construction**, because a test may promote a
room half way through a scenario and the next call must see the new binding.
`db_path`, `strict` and `known_channels` are plain public attributes for the
same reason: a test repoints or extends them mid-scenario.

**The web process is reached by a second patch, not by this module's first one.**
`web_app.py` constructs `TalkClient(...)` directly in seven places — including
`_chat_promote_to_talk`, which *creates* the promoted shape, and `_post_as_user`,
which posts a web turn to the room's Talk ref — and each takes a per-user OAuth
bearer token, so there is no factory to patch. `talk_client_factory` stands in at
`istota.talk.TalkClient` for exactly those, and the `fake_talk_web` fixture
installs it. `fake_talk` alone does not: a web test using only that fixture still
reaches the real client. The two *function-local* `get_talk_client` imports
(`web_app._delete_from_talk`'s bot fallback, `commands`' `!search`) are *reached*
by `fake_talk` patching `async_runtime.get_talk_client` itself, since a
function-local import resolves the name at call time — reached, not covered:
`!search` calls `search_messages`, which is on no seam and not on this double,
so such a call gets an `AttributeError` rather than an answer.

**One of the seven web sites is reached and still cannot be driven**, which is
worth naming rather than leaving to be discovered. `_talk_conversation_verdict`
tells a deleted conversation from one the bot was merely removed from by
branching on the *bot's* own 404, and only then constructs the user client in
`_talk_conversation_seen_by_user`. This double's second failure mode is keyed on
the bearer token and the bot carries none, so an unknown token there comes back
as `UnknownTalkRoom`, lands in the generic `except Exception` and reads as
`unknown` — which leaves the `gone` and `bot_removed` verdicts, and the rebind
branch behind them, unreachable under this fixture. Expressing it wants a
token-keyed rejection map beside the bearer-keyed one, and the nesting hazard
noted on `FakeTalkClient` has to be closed first.

A bearer client brings a second way to fail, and flattening it into the first
would destroy behaviour the product has: `_post_as_user` and `_mark_read_as_user`
each retry once on a **401** after force-refreshing the token, and a double whose
only unhappy answer is `UnknownTalkRoom` makes both attempts look like a
misroute — the retry becomes untestable, and a converted test would read a stale
credential as a routing bug. `bearer_rejections` maps a bearer token to the
status the server answers with, and the answer is recorded as `TalkCall.status`
rather than as `refused`, so `refusals == []` keeps meaning "nothing was
misrouted" in the thirty-odd tests that already assert it.

Also not covered: ISSUE-401, a binding whose Talk conversation has been deleted.
It is indistinguishable from a live binding at the database level, so the double
accepts it, exactly as this module's rule says it should.

**Reads and writes are not connected**, deliberately. `send_message` mints an id
and returns it but appends nothing to `self.messages`, so `get_latest_message_id`
after a send still answers from the seed alone. Wiring them would mean inventing
an `actorId` the double has no way to know and would overwrite what a poller test
seeded. A test that needs post-then-read consistency seeds both halves.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from istota import db


class BrokenTalkDouble(BaseException):
    """The double itself could not answer — not a refusal, and never swallowed.

    A `BaseException` on purpose. Everything this double raises normally is
    caught by the product's `except Exception` handlers, which is the whole
    design; but a database with no `room_bindings` table is the *instrument*
    being broken, and letting that arrive at `TalkTransport.deliver` as a
    swallowed `sqlite3.OperationalError` makes a misconfigured test
    indistinguishable from a 404. Reached most easily through the `fake_talk`
    fixture's documented `db_path` reassignment, since `db_path` is the only
    fixture that runs `db.init_db`.
    """


class UnknownTalkAttachment(Exception):
    """The 404 a WebDAV GET gets for a file path naming nothing.

    Ordinary `Exception`, like `UnknownTalkRoom`: `TalkTransport.download_attachment`
    does not catch, so this propagates exactly as the real `raise_for_status`
    would.
    """


class UnknownTalkRoom(Exception):
    """Shaped like the 404 Nextcloud returns for a token naming no conversation.

    A plain `Exception` subclass on purpose: the product's handlers are
    `except Exception`, and a double whose refusal escaped them would be
    testing a control path the real 404 never takes.
    """

    def __init__(self, token: object, *, method: str, live_refs: Sequence[str],
                 known_channels: Sequence[str]) -> None:
        self.token = token
        self.method = method
        self.live_refs = list(live_refs)
        self.known_channels = list(known_channels)
        known = ", ".join(repr(c) for c in self.known_channels) or "(none)"
        refs = ", ".join(repr(r) for r in self.live_refs) or "(none)"
        super().__init__(
            f"talk.{method}: {token!r} names no Talk conversation. "
            f"Live `talk` surface_refs: {refs}. known_channels: {known}. "
            "A canonical room token here is ISSUE-400 — resolve the room's "
            "`talk` binding. A legitimately unbound operator channel belongs "
            "in known_channels."
        )


@dataclass(frozen=True)
class TalkCall:
    """One attempted call, in order, refused or not.

    `token` is None for the two methods that take no conversation token
    (`list_conversations`, `download_attachment`), so a test filtering by token
    never has to special-case them. It is annotated `str | None` but carries
    whatever the caller passed — the product hands tokens off database rows and
    a test may pass junk deliberately, so nothing coerces it.

    `refused` means *the token named no conversation*, which is the misroute
    this whole instrument exists to catch. Under `strict=False` nothing is
    refused, so `refused` is False on every row and `refusals` says nothing at
    all; that is a second reason to reach for `known_channels` instead.

    `status` is the HTTP status of a **credential** answer — a bearer token in
    `bearer_rejections`, which today means the 401 that `_post_as_user` and
    `_mark_read_as_user` recover from by force-refreshing. It is deliberately
    not folded into `refused`: a stale token is not a misroute, and the many
    existing `assert fake_talk.refusals == []` lines mean the second thing.
    Both raise, so both are calls that did not reach a room.

    `bearer_token` is what the client carried when the call was made — None for
    the bot (basic auth), a string for a user-scoped OAuth client. It is on the
    call rather than left to be inferred from `constructions` because the one
    assertion that needs it, "the retry used the *fresh* token", is otherwise a
    guess about which construction went with which call.

    `sent_id` is the id an accepted `send_message` minted, and it is on the call
    for a reason worth stating: `sent_id_for` used to walk `calls` and
    `sent_ids` as parallel arrays, keeping its own index of "accepted sends so
    far". A credential rejection is recorded with `refused=False` and mints no
    id, so the two lists desynchronised the moment a 401 retry happened — which
    is precisely the path this double gained for `_post_as_user` — and the
    helper then returned another post's id, or raised `IndexError` out of a
    test helper. Reading the id off the call it belongs to removes the
    correspondence rather than correcting it.

    Frozen but **not hashable** — `args` is a dict, so `set(client.calls)` and
    `TalkCall(...) in {…}` raise `TypeError`. Compare the list, or compare
    fields.
    """

    method: str
    token: str | None
    args: dict[str, Any] = field(default_factory=dict)
    refused: bool = False
    status: int | None = None
    bearer_token: str | None = None
    sent_id: int | None = None


@dataclass(frozen=True)
class TalkConstruction:
    """One `TalkClient(...)` the product built through `talk_client_factory`.

    Six of `web_app`'s seven sites construct a client per attempt, so the count
    is the attempt count — which is how a test tells "retried once" from
    "retried in a loop" without reaching into the product.

    Two exclusions, both found in review and both easy to trip over:

    - **A client fetched through `get_talk_client` is not counted here.**
      `talk_bot_client` hands back the same instance without recording a
      construction, so `_delete_from_talk`'s two attempts — one bearer, one bot
      — produce two `calls` and one construction. Read `calls` on any path with
      a bot leg.
    - **`_delete_from_talk` never closes its user client**, alone among the
      seven, so `closes` is not the construction count either. That is a
      product defect rather than a modelling choice; `tests/test_web_talk_seams.py`
      pins the current behaviour so a fix turns it red instead of passing
      unnoticed.
    """

    bearer_token: str | None
    timeout: float | None


class FakeTalkClient:
    """Stands in for `istota.talk.TalkClient` at both of its seams.

    Only the methods the patched seams actually call are implemented — the two
    `get_talk_client` importers (`transport/talk/__init__.py` and
    `transport/talk/inbound.py`) and `web_app`'s direct constructions.
    `tests/test_support_talk_double.py` pins that list against all three modules
    and pins each signature against the real client, so a method added to a
    seam, or a parameter renamed on the real client, turns a test red rather
    than leaving the double quietly lying.

    **One instance serves every construction.** `talk_client_factory` hands the
    same object back each time, because a per-construction instance would split
    `calls` across the two attempts of a 401 retry and leave a test unable to
    say the retry happened at all. `bearer_token` and `timeout` are therefore
    rebound at each construction and are the *current* credential, not a
    history — `constructions` is the history, and `TalkCall.bearer_token` is
    what pins a call to one.

    **Which bounds what a test on this may do: one Talk-calling coroutine at a
    time.** Two in flight share the credential field, so `constructions`
    interleaves and a call records whichever construction ran last rather than
    the one that owns it. `web_app` has both shapes — `chat_read_all_rooms`
    fires `_push_read_to_talk` once per moved room through `_fire_and_forget`,
    and `_chat_promote_to_talk` holds a bot client across a call that would
    construct a *user* client inside it, so the outer one would come back
    carrying the user's bearer. Neither is reachable today: the tests drive
    these functions directly rather than through an endpoint, and the nested
    case additionally needs the bot 404 that is inexpressible below. An
    endpoint-driven test wants a per-construction facade over a shared ledger
    first; do not read `constructions` positionally from one.

    **`timeout` is a double-only observation field**, like `constructions`. The
    real client stores it as `_timeout` and exposes nothing public, so nothing
    in the product reads this and no pin covers it — attributes are outside the
    signature walk, which only sees callables.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        strict: bool = True,
        known_channels: Iterable[str] = (),
    ) -> None:
        self.db_path = Path(db_path)
        # `strict=False` accepts anything and is for the genuinely unmodellable
        # case only — `talk_channel_for_task` rung 3 can hand back an email
        # thread hash that names nothing anywhere. Every *operator-configured*
        # channel (alerts_channel, log_channel, the first briefing token,
        # default_destination, an auto-detected DM, a provisioned room the
        # poller has not seen) is data and belongs in known_channels: with only
        # an on/off switch the opt-out becomes routine, and a routine opt-out is
        # not a guard. A test setting this states its reason on the line.
        self.strict = strict
        self.known_channels: set[str] = set(known_channels)
        self.calls: list[TalkCall] = []
        # The ids `send_message` handed back, in order, one per *accepted*
        # send. `TalkCall` records what went into a call and not what came out
        # of it, and several converted tests assert that an edit addresses the
        # message a send created rather than the ack — so without this each of
        # them wraps `send_message` itself, which is four spellings of one
        # thing and invisible to the signature pin in
        # `tests/test_support_talk_double.py`. A refused send raises before
        # minting, so this list is shorter than the send calls; `sent_id_for`
        # is what walks the two together.
        #
        # Inherits the same blind spot the wrappers had, for the same reason: a
        # test that assigns `client.send_message = ...` shadows the class
        # method, so nothing appends here and `sent_id_for` answers None with
        # no error. Read the double, do not replace its methods.
        self.sent_ids: list[int] = []
        # Seedable, so a test can drive the poller or name a channel whose
        # display name is not its room's registered name.
        self.display_names: dict[str, str] = {}
        self.conversation_info: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.participants: dict[str, list[dict]] = {}
        self.conversations: list[dict] = []
        self.attachments: dict[str, bytes] = {}
        self._next_message_id = 1000
        # What the real client's constructor takes, rebound by
        # `talk_client_factory` at each construction. None is the bot's
        # basic-auth mode, which is what every `get_talk_client` seam uses.
        self.bearer_token: str | None = None
        self.timeout: float | None = None
        self.constructions: list[TalkConstruction] = []
        # `aclose` is idempotent on the real client and a no-op here (one
        # instance outlives every construction), so this is the only evidence
        # that the product closed what it opened.
        self.closes = 0
        # bearer token -> the status the server answers with, for every call
        # that client makes. 401 is the case with product behaviour behind it;
        # 403 and 404 are here because `_mark_read_as_user` and
        # `_delete_from_talk` branch on *not* being 401.
        self.bearer_rejections: dict[str, int] = {}
        # The tokens `create_conversation` minted, in order. Nothing binds
        # them — `_chat_promote_to_talk` writes the binding itself, which is
        # exactly the step the double is there to check, since every later call
        # in that function is refused if it names anything else.
        self.created_tokens: list[str] = []
        self._next_created = 1

    # --- the rule ---------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        """Always False, and that is a divergence worth stating.

        `get_talk_client`'s singleton cache reads this on the real client, and
        the real one answers True after `aclose()`. Here one instance outlives
        every construction, so answering True after the first `finally` would
        make the second attempt of a 401 retry read as a closed client. Nothing
        in the product asks the double this today; a caller that starts to has
        to decide what it means first.
        """
        return False

    def _record(
        self, method: str, token: object, args: dict[str, Any],
        *, refused: bool = False, status: int | None = None,
    ) -> None:
        self.calls.append(TalkCall(
            method, token, args, refused=refused, status=status,
            bearer_token=self.bearer_token,
        ))

    def _check_credential(
        self, method: str, token: object, args: dict[str, Any],
    ) -> None:
        """The server's answer about *who is calling*, before any about a room.

        Nextcloud authenticates before it routes, so a rejected bearer token
        answers 401 whatever the conversation token names, and this runs first
        for that reason. The consequence a test should know about: with a stale
        credential *and* a misroute, the first attempt reports the credential
        and only the retry — with a fresh token — reaches the room check. Both
        attempts are in `calls`, which is where that reads correctly.
        """
        if not isinstance(self.bearer_token, str):
            return
        status = self.bearer_rejections.get(self.bearer_token)
        if not status:
            return
        self._record(method, token, args, status=status)
        request = httpx.Request("POST", "https://nextcloud.invalid/ocs")
        raise httpx.HTTPStatusError(
            f"talk.{method}: bearer token {self.bearer_token!r} rejected with "
            f"HTTP {status}",
            request=request,
            response=httpx.Response(status, request=request),
        )

    def _live_talk_refs(self) -> list[str]:
        with db.get_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT surface_ref FROM room_bindings WHERE surface = 'talk' "
                "ORDER BY surface_ref"
            ).fetchall()
        return [row["surface_ref"] for row in rows]

    def _check(self, method: str, token: object, args: dict[str, Any]) -> None:
        """Record the call, and raise if the token names no conversation."""
        self._check_credential(method, token, args)
        if isinstance(token, str) and token in self.known_channels:
            self._record(method, token, args)
            return
        bound = False
        if isinstance(token, str) and token:
            # An archived room's binding row still exists and is accepted: the
            # Nextcloud conversation outlives our archive flag, so refusing it
            # would be the double being stricter than the thing it stands in
            # for.
            bound = self._is_bound(method, token, args)
        if bound or not self.strict:
            self._record(method, token, args)
            return
        self._record(method, token, args, refused=True)
        raise UnknownTalkRoom(
            token,
            method=method,
            live_refs=self._live_talk_refs(),
            # `key=str` because a caller may put a non-`str` in the set, and a
            # bare `sorted` would then raise `TypeError` *on the refusal path*
            # — swallowed by the product, with the refusal lost.
            known_channels=sorted(self.known_channels, key=str),
        )

    def _is_bound(self, method: str, token: str, args: dict[str, Any]) -> bool:
        """The lookup, with a database failure kept out of the product's reach.

        The call is recorded before the raise so the transcript is complete
        even on this path, but the raise is what matters: a `sqlite3.Error`
        escaping as-is is caught by `TalkTransport.deliver` and reported as a
        Talk failure, so a test pointed at an uninitialised database would read
        as a clean refusal and record nothing.
        """
        try:
            with db.get_db(self.db_path) as conn:
                return conn.execute(
                    "SELECT 1 FROM room_bindings "
                    "WHERE surface = 'talk' AND surface_ref = ?",
                    (token,),
                ).fetchone() is not None
        except sqlite3.Error as exc:
            self._record(method, token, args, refused=True)
            raise BrokenTalkDouble(
                f"talk.{method}: could not read room_bindings from "
                f"{self.db_path}: {exc!r}. This is the double failing, not a "
                "refusal — point `db_path` at a database `db.init_db` has run "
                "against."
            ) from exc

    # --- the methods the two patched seams call ---------------------------

    async def send_message(
        self,
        conversation_token: str,
        message: str,
        reply_to: int | None = None,
        reference_id: str | None = None,
    ) -> dict:
        self._check("send_message", conversation_token, {
            "message": message,
            "reply_to": reply_to,
            "reference_id": reference_id,
        })
        self._next_message_id += 1
        self.sent_ids.append(self._next_message_id)
        # Onto the call `_check` just recorded, so `sent_id_for` never has to
        # walk two lists in step. `_check` returned, so that call is the last
        # one and it was accepted.
        self.calls[-1] = replace(self.calls[-1], sent_id=self._next_message_id)
        # The shape `TalkTransport.deliver` unwraps.
        return {"ocs": {"data": {"id": self._next_message_id}}}

    async def edit_message(
        self, conversation_token: str, message_id: int, message: str,
    ) -> dict:
        self._check("edit_message", conversation_token, {
            "message_id": message_id, "message": message,
        })
        return {"ocs": {"data": {"id": message_id}}}

    async def get_conversation_info(self, conversation_token: str) -> dict:
        self._check("get_conversation_info", conversation_token, {})
        name = self.display_names.get(conversation_token)
        if name is None:
            name = self._registered_name(conversation_token)
        # The real client returns the whole `ocs.data` object, and
        # `inbound._get_participants` branches on `type`. Returning
        # `displayName` alone would give a caller reading `type` a `KeyError`
        # that the product swallows and reports as a Talk failure. A test that
        # cares about the room type seeds the whole dict here.
        seeded = self.conversation_info.get(conversation_token, {})
        return {
            "token": conversation_token,
            "type": 2,  # group; Talk's ROOM_TYPE_GROUP
            "displayName": name or conversation_token,
            **seeded,
        }

    async def poll_messages(
        self,
        conversation_token: str,
        last_known_message_id: int | None = None,
        timeout: int = 30,
        limit: int = 50,
    ) -> list[dict]:
        self._check("poll_messages", conversation_token, {
            "last_known_message_id": last_known_message_id,
            "timeout": timeout, "limit": limit,
        })
        seeded = list(self.messages.get(conversation_token, []))
        if not last_known_message_id:
            # `lookIntoFuture=0` — recent history, oldest first.
            return seeded[-limit:]
        # `lookIntoFuture=1` — only what is newer, and `[]` on the real
        # client's 304. Without this the poller re-ingests the same turns on
        # every tick, which is a false pass in the direction that matters.
        return [
            m for m in seeded if (m.get("id") or 0) > last_known_message_id
        ][:limit]

    async def fetch_chat_history(
        self, conversation_token: str, limit: int = 100,
    ) -> list[dict]:
        self._check("fetch_chat_history", conversation_token, {"limit": limit})
        return list(self.messages.get(conversation_token, []))[-limit:]

    async def get_latest_message_id(self, conversation_token: str) -> int | None:
        self._check("get_latest_message_id", conversation_token, {})
        seeded = self.messages.get(conversation_token) or []
        return seeded[-1].get("id") if seeded else None

    async def get_participants(self, conversation_token: str) -> list[dict]:
        self._check("get_participants", conversation_token, {})
        return list(self.participants.get(conversation_token, []))

    async def list_conversations(self) -> list[dict]:
        # No conversation token, so no room to check — but there is still a
        # credential, and `_talk_read_pull` calls this with a user's bearer.
        self._check_credential("list_conversations", None, {})
        self._record("list_conversations", None, {})
        return list(self.conversations)

    async def download_attachment(self, file_path: str, local_path: str) -> None:
        # A WebDAV path, not a conversation token — no room to check, but the
        # same rule in spirit: the real client GETs and calls
        # `raise_for_status`, so an unseeded path must fail rather than leave a
        # zero-byte file behind for a test to assert exists. Seed `b""` to ask
        # for an empty body on purpose.
        known = file_path in self.attachments
        self._record("download_attachment", None, {
            "file_path": file_path, "local_path": local_path,
        }, refused=not known)
        if not known:
            raise UnknownTalkAttachment(
                f"talk.download_attachment: {file_path!r} is in no test's "
                f"`attachments`. Seed it (b'' for an empty body); Nextcloud "
                "would answer 404."
            )
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self.attachments[file_path])

    # --- the methods web_app's own constructions call ---------------------

    async def aclose(self) -> None:
        """Counted, not honoured. One instance outlives every construction.

        The real client is idempotent here and refuses use afterwards; see
        `is_closed` for why the double cannot follow it that far.
        """
        self.closes += 1

    async def create_conversation(self, name: str, room_type: int = 2) -> dict:
        """Mint a Talk token, bound to nothing.

        Deliberately unbound: `_chat_promote_to_talk` writes the binding itself
        between this call and the `add_participant` / `send_message` that
        follow, so every later call in that function is refused unless the
        product persisted the token it was handed. That is the whole promote
        path checked by the rule rather than by an assertion.
        """
        self._check_credential("create_conversation", None, {
            "name": name, "room_type": room_type,
        })
        token = f"created{self._next_created}"
        self._next_created += 1
        self.created_tokens.append(token)
        self._record("create_conversation", None, {
            "name": name, "room_type": room_type, "token": token,
        })
        # The real client returns `ocs.data`, already unwrapped.
        return {"token": token, "displayName": name, "type": room_type}

    async def add_participant(
        self, conversation_token: str, participant: str, source: str = "users",
    ) -> dict:
        self._check("add_participant", conversation_token, {
            "participant": participant, "source": source,
        })
        return {"type": 3}

    async def rename_conversation(
        self, conversation_token: str, name: str,
    ) -> None:
        self._check("rename_conversation", conversation_token, {"name": name})

    async def delete_conversation(self, conversation_token: str) -> None:
        self._check("delete_conversation", conversation_token, {})

    async def delete_message(
        self, conversation_token: str, message_id: int,
    ) -> dict:
        self._check("delete_message", conversation_token, {
            "message_id": message_id,
        })
        return {"ocs": {"data": {"id": message_id}}}

    async def mark_conversation_read(
        self, conversation_token: str, *, raise_on_error: bool = False,
    ) -> bool:
        """Swallows by default, exactly as the real client does.

        `_mark_read_as_user` passes `raise_on_error=True` because it acts on a
        401, and every other caller takes the bool. Reproducing the swallow is
        what keeps a refusal *invisible as an exception* on the default path and
        visible in `calls` — the same property the rest of this module rests on.
        `BrokenTalkDouble` is a `BaseException` and so escapes this, which is
        the point of it being one.
        """
        try:
            self._check("mark_conversation_read", conversation_token, {})
        except Exception:
            if raise_on_error:
                raise
            return False
        return True

    # --- helpers ----------------------------------------------------------

    def _registered_name(self, token: str) -> str | None:
        with db.get_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT r.name FROM rooms r JOIN room_bindings b "
                "ON b.room_token = r.token "
                "WHERE b.surface = 'talk' AND b.surface_ref = ?",
                (token,),
            ).fetchone()
        return row["name"] if row else None

    def calls_to(self, token: str, *, method: str | None = None) -> list[TalkCall]:
        """Every attempt against `token`, refused ones included."""
        return [
            c for c in self.calls
            if c.token == token and (method is None or c.method == method)
        ]

    def sent_id_for(self, reference_id: str) -> int | None:
        """The id handed back for the **last** accepted send carrying `reference_id`.

        The scheduler labels every post it makes (`istota:task:<id>:ack`,
        `:prompt`, `:result`, `istota:log:<id>`), so a test naming one of those
        says which post it means instead of indexing into `calls` and hoping
        the order holds.

        **Last, not first, and that is the product's rule rather than a
        preference.** `TalkTransport.deliver` splits a long message and passes
        the *same* `reference_id` to every part, then returns the id of the
        final one — which is what the caller stores in
        `messages.external_ids`. Matching the first part would agree with the
        product on every short message and disagree on exactly the long ones,
        which is the worst possible place for a test helper to differ.

        `None` when no send that *minted an id* carried it. That covers both
        unhappy answers — a refused send (a misroute) and a
        credential-rejected one (a 401) — and neither is a case a test should
        read as "the post did not happen for the reason I expected": pair this
        with `refusals` and `auth_failures` when the distinction matters.
        """
        # An unlabelled send records `reference_id: None`, so a `None` argument
        # would match every one of them and hand back a real id. No caller
        # passes one today; this is what stops the first that does from getting
        # a confident wrong answer.
        if not reference_id:
            return None
        found = None
        for call in self.calls:
            if call.method != "send_message" or call.sent_id is None:
                continue
            if call.args.get("reference_id") == reference_id:
                found = call.sent_id
        return found

    @property
    def refusals(self) -> list[TalkCall]:
        return [c for c in self.calls if c.refused]

    @property
    def auth_failures(self) -> list[TalkCall]:
        """Calls the double answered with a status rather than a room.

        Separate from `refusals` on purpose — see `TalkCall`. A test driving the
        401 retry asserts on this *and* on `refusals == []`, which together say
        "the credential failed and nothing was misrouted".
        """
        return [c for c in self.calls if c.status is not None]


def talk_bot_client(client: FakeTalkClient):
    """A stand-in for `get_talk_client`, which hands back the *bot's* client.

    A plain `return_value=client` would do, were it not that one object now
    serves both seams: `talk_client_factory` leaves the last web construction's
    bearer token on it, and `_delete_from_talk` asks for the bot immediately
    after a user-scoped attempt was refused. Without the reset the bot inherits
    the credential that just failed, and the fallback the product has — try as
    the user, then as the bot — reads as a second failure of the same kind.
    The real `get_talk_client` returns a basic-auth client, so None is what it
    means.
    """
    def _get(config) -> FakeTalkClient:
        client.bearer_token = None
        client.timeout = None
        return client

    return _get


def talk_client_factory(client: FakeTalkClient):
    """A stand-in for `istota.talk.TalkClient` at its construction sites.

    `web_app` builds a bearer client per attempt (`TalkClient(_config,
    bearer_token=access, timeout=5)`) and closes it in a `finally`, so there is
    no factory to patch and no singleton to replace — the class itself is the
    seam. This returns `client` every time and records what the product asked
    for, which is how a test says the 401 retry constructed a *second* client
    carrying the *refreshed* token.

    `config` is accepted and ignored: the double reads rooms from its own
    `db_path`, and taking the config from the caller would let a test point the
    two at different databases and get refusals it could not explain.
    """
    def _construct(config, bearer_token=None, timeout=None) -> FakeTalkClient:
        client.bearer_token = bearer_token
        client.timeout = timeout
        client.constructions.append(
            TalkConstruction(bearer_token=bearer_token, timeout=timeout)
        )
        return client

    return _construct


def talk_refs_in(db_path: str | Path) -> list[str]:
    """Every live `talk` surface_ref, for a test that wants to assert on them.

    Same query the refusal message uses, exposed so a test does not reach into
    a private method to build the expectation it is checking against.
    """
    with db.get_db(Path(db_path)) as conn:
        rows = conn.execute(
            "SELECT surface_ref FROM room_bindings WHERE surface = 'talk' "
            "ORDER BY surface_ref"
        ).fetchall()
    return [row["surface_ref"] for row in rows]
