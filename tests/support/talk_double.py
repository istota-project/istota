"""A Talk client double that refuses a token Nextcloud would refuse.

Every delivery test in the tree patches `get_talk_client` with a bare
`MagicMock` whose methods are `AsyncMock`s. Those accept any string, which is
exactly why ISSUE-400 was invisible: the double is *more permissive than
Nextcloud*, so a call handing a room's canonical `web-…` token to the Talk API
— the thing that 404s in production — looks identical to a call that resolved
the room's `talk` binding first. On an ordinary Talk room the two strings are
equal, so the mistake only shows on a promoted room, and no test built one.

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
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from istota import db


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
    never has to special-case them.
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
        # Seedable, so a test can drive the poller or name a channel whose
        # display name is not its room's registered name.
        self.display_names: dict[str, str] = {}
        self.messages: dict[str, list[dict]] = {}
        self.participants: dict[str, list[dict]] = {}
        self.conversations: list[dict] = []
        self.attachments: dict[str, bytes] = {}
        self._next_message_id = 1000

    # --- the rule ---------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        """`get_talk_client`'s singleton cache reads this on the real client."""
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
            with db.get_db(self.db_path) as conn:
                bound = conn.execute(
                    "SELECT 1 FROM room_bindings "
                    "WHERE surface = 'talk' AND surface_ref = ?",
                    (token,),
                ).fetchone() is not None
        if bound or not self.strict:
            self.calls.append(TalkCall(method, token, args))
            return
        self.calls.append(TalkCall(method, token, args, refused=True))
        raise UnknownTalkRoom(
            token,
            method=method,
            live_refs=self._live_talk_refs(),
            known_channels=sorted(self.known_channels),
        )

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
        return {"displayName": name or conversation_token}

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
        return list(self.messages.get(conversation_token, []))

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
        # A WebDAV path, not a conversation token — no room to check.
        self.calls.append(TalkCall("download_attachment", None, {
            "file_path": file_path, "local_path": local_path,
        }))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self.attachments.get(file_path, b""))

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
