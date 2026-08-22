"""The Talk transport, end to end, against the Nextcloud the deployment ships.

Everything between a person typing in a room and the answer appearing under it
is asserted here in one piece: the poller's room discovery, the participant and
actor filters, ingest into the canonical `rooms` / `messages` tables, the task,
the scripted model, delivery back through `transport/talk/`, and the ids that
tie the two halves together.

**Why this file rather than more unit tests.** Every one of those pieces has
unit coverage and none of it can tell you they are wired to each other, because
the seam they meet at is a real Talk server. `tests/test_talk_integration.py`
does run against one — a hand-configured external Nextcloud, with a hardcoded
room token, reading the developer's own `config/config.toml`. That is
developer-local rather than absent, which is the same thing for a tier's
purposes. `provision_rooms.py`, the Ansible path's implementation of the room
provisioning this file's last test drives, is asserted against `MagicMock`.

**Read `tests/full/test_provisioning.py` first if everything here fails.** Every
scenario needs the boot to have made the bot user, enabled `spreed` and left a
daemon that polls; none of these failures would name any of that.

**Why the timeouts are minutes and not seconds.** A room is only polled once the
daemon has *seen* it, and `transport/talk/inbound.py` caches the conversation
list for 60 seconds (`_CONVERSATION_CACHE_TTL`) — so a room created by a test is
invisible for up to that long, plus one 10-second poll interval, before anything
happens at all. That is the product's real behaviour rather than a harness
delay, so the tests wait it out rather than reaching into the daemon to shorten
it.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

pytestmark = pytest.mark.full

FULL = pytest.mark.profile("full")

#: The scripted answer, distinctive enough to find among the daemon's own posts:
#: a Talk task also produces an acknowledgement message that is later edited in
#: place, so "a message from the bot" is not the same as "the reply".
ANSWER = "the answer this scenario scripted"

#: One conversation-cache window (60s), one poll interval (10s), the task, and
#: enough slack that a loaded machine does not turn a pass into a flake. Long by
#: design; see the module docstring.
TALK_TIMEOUT = 240

CONTAINER_CONFIG = "/data/config/config.toml"


def _room_name() -> str:
    """A name no other test and no boot-provisioned room can collide with.

    `find_room_by_name` in `entrypoint.sh` and `find_room_for_user` in
    `provision_rooms.py` both match on the display name, so a scenario naming
    its room `general` would be handing the recovery paths a decoy.
    """
    return f"testbed-{uuid.uuid4().hex[:8]}"


def _delivered(stack, token: str, timeout: float = 60) -> dict:
    """The task row, re-read until the ids delivery writes are actually on it.

    `wait_for_task` returns the moment the row reaches a terminal status, and
    `talk_response_id` is written *after* that by `db.update_talk_response_id`
    — a separate transaction on the far side of the Talk post. So a scenario
    that asserts on the column straight off the waited-for row is reading it
    mid-write, which passes on a quiet machine and fails on a busy one. Measured
    the hard way: the first run of this file failed exactly there, with the
    reply visible in the room and the column still NULL.

    Polling rather than sleeping, and it returns the row so the caller's own
    assertions still name what they compared.
    """
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        rows = stack.probe.tasks(conversation_token=token)
        latest = rows[-1] if rows else {}
        if latest.get("talk_response_id"):
            return latest
        time.sleep(1.0)
    raise AssertionError(
        "the task completed but never recorded the id of its Talk reply "
        f"(talk_response_id stayed NULL for {timeout:.0f}s): {latest}\n"
        + stack.diagnostics(latest)
    )


def _assistant_row(stack, token: str, timeout: float = 60) -> dict:
    """The room's assistant turn, re-read until its Talk id is stamped.

    Same race, one table over: the scheduler writes the assistant row when it
    delivers and stamps `external_ids` afterwards, in a third transaction.
    """
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = [
            row for row in stack.probe.query(
                "SELECT * FROM messages WHERE room_token = ? ORDER BY id", [token]
            )
            if row["role"] == "assistant"
        ]
        if rows and _talk_id(rows[0]):
            return rows[0]
        time.sleep(1.0)
    raise AssertionError(
        f"no assistant turn in {token} carried a Talk id within {timeout:.0f}s: "
        f"{rows}"
    )


def _bot_messages(nextcloud, token: str, *, user: str = "") -> list[dict]:
    return [
        row
        for row in nextcloud.messages(token, user=user or nextcloud.bot_user)
        if row.get("actorId") == nextcloud.bot_user
    ]


@FULL
@pytest.mark.script([{"text": ANSWER}])
class TestATalkRoundTrip:
    """A message arrives, a task runs, the answer comes back to the same room."""

    def test_the_reply_is_posted_into_the_room_the_message_arrived_in(self, stack):
        """The whole transport seam, and the assertion the negative control bites.

        Four separate claims, and the third is the one with no witness anywhere
        else: the task exists, it is Talk-sourced and carries the *inbound*
        message id, the reply landed in **this** room rather than in some other
        room the bot is also in, and the task row records the id of that reply.
        The daemon is in four rooms the boot made — a 1:1, `#general`, `#logs`
        and `#alerts` — so "a reply was posted somewhere" is a much weaker fact
        than it looks, and `talk_channel_for_task`'s ladder is what decides.
        """
        nextcloud = stack.service("nextcloud")
        token = nextcloud.create_room(
            name=_room_name(), participants=[nextcloud.bot_user]
        )

        inbound = nextcloud.post_message(token, message="what does the script say?")

        task = stack.probe.wait_for_task(
            status="completed", conversation_token=token, timeout=TALK_TIMEOUT
        )
        assert task["status"] == "completed", stack.diagnostics(task)
        assert task["source_type"] == "talk", stack.diagnostics(task)
        assert task["talk_message_id"] == inbound, stack.diagnostics(task)

        replies = [
            row for row in _bot_messages(nextcloud, token)
            if ANSWER in (row.get("message") or "")
        ]
        assert len(replies) == 1, (
            f"the scripted answer is not in {token!r}; the bot's other rooms "
            f"hold it at {_where_the_answer_went(nextcloud, token)}\n"
            + stack.diagnostics(task)
        )
        task = _delivered(stack, token)
        assert task["talk_response_id"] == replies[0]["id"], (
            "the task recorded a different message id than the one this room "
            "holds, so the reply went somewhere else\n" + stack.diagnostics(task)
        )

    def test_a_message_in_a_room_the_bot_is_not_in_produces_nothing(self, stack):
        """The poller's candidate set is the bot's own conversation list.

        A negative assertion needs something to prove the poller ran at all,
        or it passes on a daemon that is simply asleep — the failure mode the
        watermark discipline exists for. So two rooms are made and posted to:
        the joined one produces a task, and *that* is what makes the unjoined
        one's silence mean something.
        """
        nextcloud = stack.service("nextcloud")
        unjoined = nextcloud.create_room(name=_room_name())
        joined = nextcloud.create_room(
            name=_room_name(), participants=[nextcloud.bot_user]
        )

        nextcloud.post_message(unjoined, message="the bot cannot see this")
        nextcloud.post_message(joined, message="the bot can see this")

        task = stack.probe.wait_for_task(
            status="completed", conversation_token=joined, timeout=TALK_TIMEOUT
        )
        assert task["status"] == "completed", stack.diagnostics(task)

        assert not stack.probe.rows_above(
            "tasks", stack.mark, conversation_token=unjoined
        ), stack.diagnostics(task)
        assert not _bot_messages(
            nextcloud, unjoined, user=nextcloud.test_user
        ), stack.diagnostics(task)

    def test_the_turn_carries_the_linkage_the_web_chat_surface_reads(self, stack):
        """A Talk exchange has to be a room in `messages`, not only in Talk.

        Web chat renders a Talk-bound room out of the canonical tables, so a
        turn that reached Talk and not those tables is a room whose transcript
        is empty in one surface and full in the other. Three rows have to line
        up for that to work, and each is written by a different piece of code:
        `rooms` by the poller's lazy registration, the user row by
        `transport/ingest.py`, and the assistant row by the scheduler after
        delivery. The `external_ids` ledger is the join between a canonical id
        and a Talk one — the two namespaces `db.py` warns never to assign
        across.
        """
        nextcloud = stack.service("nextcloud")
        token = nextcloud.create_room(
            name=_room_name(), participants=[nextcloud.bot_user]
        )

        inbound = nextcloud.post_message(token, message="mirror this into the room")

        task = stack.probe.wait_for_task(
            status="completed", conversation_token=token, timeout=TALK_TIMEOUT
        )
        assert task["status"] == "completed", stack.diagnostics(task)

        rooms = stack.probe.query(
            "SELECT * FROM rooms WHERE token = ?", [token]
        )
        assert rooms, "the poller registered no room for a room it polled"
        assert rooms[0]["origin"] == "talk", rooms[0]

        assistant = _assistant_row(stack, token)
        task = _delivered(stack, token)
        rows = stack.probe.query(
            "SELECT * FROM messages WHERE room_token = ? ORDER BY id", [token]
        )
        inbound_rows = [row for row in rows if row["role"] == "user"]
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert len(inbound_rows) == 1, stack.diagnostics(task)
        assert len(assistant_rows) == 1, stack.diagnostics(task)
        assert _talk_id(inbound_rows[0]) == str(inbound), inbound_rows[0]
        assert assistant["task_id"] == task["id"], assistant
        assert _talk_id(assistant) == str(task["talk_response_id"]), (
            "the assistant turn's Talk id is not the one the task recorded, so "
            "the web surface's read cap would advance to the wrong message\n"
            + stack.diagnostics(task)
        )


@FULL
class TestProvisionRoomsAgainstARealServer:
    """`provision_rooms.py` — the Ansible path — run against Talk, not a mock.

    No room is created here and none should be: the point is that a second
    provisioning run over rooms `entrypoint.sh` already made reuses them. That
    is `find_room_for_user`'s participant-scoped lookup, which has never run
    against a server that could disagree with it.
    """

    def test_a_second_provisioning_run_reuses_the_rooms_the_boot_made(self, stack):
        nextcloud = stack.service("nextcloud")
        before = {
            room["token"]: room.get("displayName", "")
            for room in nextcloud.rooms()
        }

        result = stack.exec(
            [
                "uv", "run", "istota", "-c", CONTAINER_CONFIG,
                "nextcloud", "provision-rooms", "--user", "testuser", "--json",
            ],
            timeout=180,
        )

        assert result.returncode == 0, result.stderr
        payload = _first_json_object(result.stdout)
        assert payload["state"] == "noop", payload
        for room in payload["rooms"]:
            assert room["created"] is False, room
            assert before.get(room["token"]) == room["name"], (room, before)
        after = {
            room["token"]: room.get("displayName", "")
            for room in nextcloud.rooms()
        }
        assert after == before, (
            "a provisioning run over existing rooms created or lost one"
        )


def _where_the_answer_went(nextcloud, expected: str) -> list[tuple[str, str]]:
    """Every *other* room of the bot's that holds the scripted answer.

    Only ever called from a failure message, and it is what makes that message
    worth reading: "the answer is not in this room" is true of a task that never
    ran, a model that answered nothing and a reply posted into the wrong room,
    and only the third is a routing defect. `talk_channel_for_task` has four
    rungs and three of them can hand back a room the bot is in for reasons that
    have nothing to do with this scenario — the alerts channel above all — so
    the useful thing to report is the token by value.
    """
    found = []
    for room in nextcloud.rooms():
        token = room.get("token", "")
        if not token or token == expected:
            continue
        try:
            rows = nextcloud.messages(token, limit=20)
        except Exception:  # pragma: no cover - diagnostic
            continue
        if any(ANSWER in (row.get("message") or "") for row in rows):
            found.append((room.get("displayName", ""), token))
    return found


def _talk_id(message_row: dict) -> str:
    """The Talk message id out of a `messages` row's `external_ids` ledger.

    A JSON blob of `{surface: external_id}` rather than a column, because a room
    can be bound to more than one surface and each has its own id space.
    """
    return json.loads(message_row.get("external_ids") or "{}").get("talk", "")


def _first_json_object(text: str) -> dict:
    """The CLI prints a JSON object and then a `STATE:` line for Ansible."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    if start < 0:
        raise AssertionError(f"no JSON object in:\n{text}")
    return decoder.raw_decode(text[start:])[0]
