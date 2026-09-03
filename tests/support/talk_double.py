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

Not covered, stated so a green suite is not misread: `web_app.py` constructs
`TalkClient(...)` directly in seven places, including `_chat_promote_to_talk`
(which *creates* the promoted shape) and `_post_as_user` (which posts a web turn
to the room's Talk ref). Those take a per-user OAuth token, so they need a
construction-site patch rather than a factory patch. Two further function-local
`get_talk_client` imports live in `web_app` and `commands`.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

    `refused` means *this double raised*, not *this token was unbound*. Under
    `strict=False` nothing raises, so `refused` is False on every row and
    `refusals` says nothing at all; that is a second reason to reach for
    `known_channels` instead.

    Frozen but **not hashable** — `args` is a dict, so `set(client.calls)` and
    `TalkCall(...) in {…}` raise `TypeError`. Compare the list, or compare
    fields.
    """

    method: str
    token: str | None
    args: dict[str, Any] = field(default_factory=dict)
    refused: bool = False


class FakeTalkClient:
    """Stands in for `istota.talk.TalkClient` at the `get_talk_client` seam.

    Only the methods the two patched seams actually call are implemented —
    `transport/talk/__init__.py` and `transport/talk/inbound.py`, nine between
    them. `tests/test_support_talk_double.py` pins that list against those two
    modules and pins each signature against the real client, so a method added
    to a seam, or a parameter renamed on the real client, turns a test red
    rather than leaving the double quietly lying.
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

    # --- the rule ---------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        """Kept so a patch at the *construction* site also works.

        `get_talk_client`'s singleton cache reads this on the real client, but
        the `fake_talk` fixture replaces the whole factory, so nothing consults
        it today — Stage 10's construction-site patch for `web_app` is what
        would. Not evidence of a live coupling.
        """
        return False

    def _live_talk_refs(self) -> list[str]:
        with db.get_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT surface_ref FROM room_bindings WHERE surface = 'talk' "
                "ORDER BY surface_ref"
            ).fetchall()
        return [row["surface_ref"] for row in rows]

    def _check(self, method: str, token: object, args: dict[str, Any]) -> None:
        """Record the call, and raise if the token names no conversation."""
        if isinstance(token, str) and token in self.known_channels:
            self.calls.append(TalkCall(method, token, args))
            return
        bound = False
        if isinstance(token, str) and token:
            # An archived room's binding row still exists and is accepted: the
            # Nextcloud conversation outlives our archive flag, so refusing it
            # would be the double being stricter than the thing it stands in
            # for.
            bound = self._is_bound(method, token, args)
        if bound or not self.strict:
            self.calls.append(TalkCall(method, token, args))
            return
        self.calls.append(TalkCall(method, token, args, refused=True))
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
            self.calls.append(TalkCall(method, token, args, refused=True))
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
        self.calls.append(TalkCall("list_conversations", None))
        return list(self.conversations)

    async def download_attachment(self, file_path: str, local_path: str) -> None:
        # A WebDAV path, not a conversation token — no room to check, but the
        # same rule in spirit: the real client GETs and calls
        # `raise_for_status`, so an unseeded path must fail rather than leave a
        # zero-byte file behind for a test to assert exists. Seed `b""` to ask
        # for an empty body on purpose.
        known = file_path in self.attachments
        self.calls.append(TalkCall("download_attachment", None, {
            "file_path": file_path, "local_path": local_path,
        }, refused=not known))
        if not known:
            raise UnknownTalkAttachment(
                f"talk.download_attachment: {file_path!r} is in no test's "
                f"`attachments`. Seed it (b'' for an empty body); Nextcloud "
                "would answer 404."
            )
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self.attachments[file_path])

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

        `None` when no accepted send carried it — including when the send was
        *refused*, which is the case a test must not read as "the post did not
        happen for the reason I expected": pair this with `refusals` when the
        distinction matters.
        """
        # An unlabelled send records `reference_id: None`, so a `None` argument
        # would match every one of them and hand back a real id. No caller
        # passes one today; this is what stops the first that does from getting
        # a confident wrong answer.
        if not reference_id:
            return None
        found = None
        accepted = 0
        for call in self.calls:
            if call.method != "send_message" or call.refused:
                continue
            if call.args.get("reference_id") == reference_id:
                found = self.sent_ids[accepted]
            accepted += 1
        return found

    @property
    def refusals(self) -> list[TalkCall]:
        return [c for c in self.calls if c.refused]


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
