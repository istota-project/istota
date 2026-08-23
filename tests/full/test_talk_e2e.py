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

#: The three group rooms `entrypoint.sh` provisions, by display name. The same
#: three `provision_rooms.DEFAULT_ROOMS` names, which is the point of the test
#: that drives it.
GROUP_ROOMS = ("general", "logs", "alerts")


def _room_name() -> str:
    """A name no other test and no boot-provisioned room can collide with.

    `find_room_by_name` in `entrypoint.sh` and `find_room_for_user` in
    `provision_rooms.py` both match on the display name, so a scenario naming
    its room `general` would be handing the recovery paths a decoy.
    """
    return f"testbed-{uuid.uuid4().hex[:8]}"


def _delivered(stack, task_id: int, timeout: float = 60) -> dict:
    """The task row, re-read until the ids delivery writes are actually on it.

    `wait_for_task` returns the moment the row reaches a terminal status
    (`db.update_task_status` in `scheduler.py`), and the Talk post and
    `db.update_talk_response_id` both run *after* that, outside that
    transaction. So a scenario that asserts on the column — or on the room's
    contents — straight off the waited-for row is reading it mid-write, which
    passes on a quiet machine and fails on a busy one. Measured the hard way:
    the first run of this file failed exactly there, with the reply visible in
    the room and the column still NULL.

    Filtered by task id rather than by room token, because a room can hold more
    than one task and polling the newest would wait out the timeout on a row
    that is never going to carry a response id — reporting "the reply was never
    recorded" about a task that did nothing of the sort.

    Polling rather than sleeping, and it returns the row so the caller's own
    assertions still name what they compared.
    """
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        rows = stack.probe.tasks(task_id=task_id)
        latest = rows[0] if rows else {}
        if latest.get("talk_response_id"):
            return latest
        time.sleep(1.0)
    raise AssertionError(
        f"task {task_id} completed but never recorded the id of its Talk reply "
        f"(talk_response_id stayed NULL for {timeout:.0f}s): {latest}\n"
        + stack.diagnostics(latest)
    )


def _assistant_row(stack, token: str, task_id: int, timeout: float = 60) -> dict:
    """This task's assistant turn, re-read until its Talk id is stamped.

    Same race, one table over: the scheduler writes the assistant row when it
    delivers and stamps `external_ids` afterwards, in a third transaction. Also
    scoped to the task, for the same reason `_delivered` is.
    """
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = [
            row for row in stack.probe.query(
                "SELECT * FROM messages WHERE room_token = ? AND task_id = ? "
                "ORDER BY id", [token, task_id]
            )
            if row["role"] == "assistant"
        ]
        if rows and _talk_id(rows[0]):
            return rows[0]
        time.sleep(1.0)
    raise AssertionError(
        f"no assistant turn for task {task_id} in {token} carried a Talk id "
        f"within {timeout:.0f}s: {rows}"
    )


def _second_task(stack, token: str, *, after: int, timeout: float = TALK_TIMEOUT) -> dict:
    """The room's next task after `after`, once it reaches a terminal status.

    The room is already known to the daemon by the time this is called, so this
    waits a poll interval rather than a conversation-cache window — but the
    same timeout is used, because a machine slow enough to need the first one is
    slow enough to need the second.
    """
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while time.monotonic() < deadline:
        seen = [
            row for row in stack.probe.tasks(conversation_token=token)
            if row["id"] > after
        ]
        terminal = [
            row for row in seen
            if row.get("status") in ("completed", "failed", "cancelled",
                                     "pending_confirmation")
        ]
        if terminal:
            return terminal[0]
        time.sleep(1.0)
    raise AssertionError(
        f"the reply to task {after} in {token} produced no finished task "
        f"within {timeout:.0f}s; saw "
        f"{[(row.get('id'), row.get('status')) for row in seen]}"
    )


def _bot_messages(nextcloud, token: str, *, user: str = "") -> list[dict]:
    return [
        row
        for row in nextcloud.messages(token, user=user or nextcloud.bot_user)
        if row.get("actorId") == nextcloud.bot_user
    ]


#: The same answer several times over, and the repetition is insurance rather
#: than need. `Stack.script` guards the *install* instant three ways — the
#: barrier across the swap, a served-count of zero, and a re-read of the task
#: table — and nothing constrains the daemon afterwards. These scenarios then
#: wait out a conversation-cache window before their own request arrives, which
#: is a far wider gap than any lean scenario opens, and a poller's task landing
#: in it would take turn 0 and leave the Talk task with the endpoint's
#: exhausted-script frame. That failure presents as a broken Talk transport.
#: Spare turns cost nothing.
SCRIPT = [{"text": ANSWER}] * 4


@FULL
@pytest.mark.script(SCRIPT)
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

        # Before reading the room, not after: `completed` is written before the
        # post is made, so a Talk read taken here would sometimes find nothing
        # and fail saying the reply went to the wrong room — the single most
        # expensive misdiagnosis this file could produce. `talk_response_id`
        # being set is exactly the proof the post landed.
        task = _delivered(stack, task["id"])
        replies = [
            row for row in _bot_messages(nextcloud, token)
            if ANSWER in (row.get("message") or "")
        ]
        assert len(replies) == 1, (
            f"the scripted answer is not in {token!r}; the bot's other rooms "
            f"hold it at {_where_the_answer_went(nextcloud, token)}\n"
            + stack.diagnostics(task)
        )
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

        Two claims against one round trip, and they are in one test because the
        second needs the first's assistant turn to reply *to*. The provisioning
        suite makes the same trade for the same reason.

        **The mirror.** Web chat renders a Talk-bound room out of the canonical
        tables, so a turn that reached Talk and not those tables is a room whose
        transcript is empty in one surface and full in the other. Three rows
        have to line up, each written by a different piece of code: `rooms` by
        the poller's lazy registration, the user row by `transport/ingest.py`,
        and the assistant row by the scheduler after delivery.

        **The reply citation**, which is the linkage `.claude/rules/web-chat.md`
        names and the one with a trap in it. A reply's parent is addressed by
        *canonical* `messages.id` on `tasks.reply_to_message_id` and
        `messages.reply_to_message_id`, while the surface-native Talk id goes to
        `tasks.reply_to_talk_id`. Both are small integers in the same room, so
        a Talk id stored in the canonical slot resolves to whichever turn
        happens to share the number and nothing at any layer notices — the rule
        file says so in as many words. `record_inbound` resolves the canonical
        id through the `external_ids` ledger, which is why the mirror above has
        to be right for this to be right, and why the assertion checks the two
        ids are *different* numbers before comparing: a scenario where they
        happened to coincide would pass under the bug.
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

        assistant = _assistant_row(stack, token, task["id"])
        task = _delivered(stack, task["id"])
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

        # --- the reply citation, against the turn the bot just posted
        bot_talk_id = task["talk_response_id"]
        assert assistant["id"] != bot_talk_id, (
            "the canonical id and the Talk id of the same turn are the same "
            "number here, so the assertion below could not tell them apart; "
            f"canonical={assistant['id']} talk={bot_talk_id}"
        )
        nextcloud.post_message(
            token, message="and this replies to it", reply_to=bot_talk_id
        )

        second = _second_task(stack, token, after=task["id"])
        assert second["status"] == "completed", stack.diagnostics(second)
        assert second["reply_to_talk_id"] == bot_talk_id, stack.diagnostics(second)
        assert second["reply_to_message_id"] == assistant["id"], (
            "the canonical reply parent is not the assistant turn's canonical "
            "id; a Talk id stored here resolves to whichever turn shares the "
            "number\n" + stack.diagnostics(second)
        )
        reply_rows = [
            row for row in stack.probe.query(
                "SELECT * FROM messages WHERE room_token = ? AND role = 'user' "
                "ORDER BY id", [token]
            )
        ]
        assert len(reply_rows) == 2, stack.diagnostics(second)
        assert reply_rows[1]["reply_to_message_id"] == assistant["id"], (
            "the transcript row carries no citation, so the turn would render "
            "as an ordinary message after retention deletes the task\n"
            + stack.diagnostics(second)
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
        """Scoped to the rooms provisioning could have touched, not to a count.

        Two things this test is careful not to do. It does not compare the
        bot's whole room list before and after: that list holds the four rooms
        the boot made and the two Talk adds per account, and anything appearing
        there for an unrelated reason would fail this test while naming the
        provisioning code — the discipline `NextcloudService.reset`'s docstring
        argues for, applied here. And it does not let an empty `rooms` list pass
        as idempotence: every per-room claim is inside a loop, so a run that
        reported nothing at all would satisfy all of them.
        """
        nextcloud = stack.service("nextcloud")
        before = {
            room["token"]: room.get("displayName", "")
            for room in nextcloud.rooms()
        }
        before_defaults = sorted(
            name for name in before.values() if name in GROUP_ROOMS
        )

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
        reported = payload["rooms"]
        # Not `== 3`: the CLI drops a name whose channel column is already
        # seeded, so `logs` and `alerts` are filtered out by
        # `pending_channel_rooms` and only `general` — which seeds no column —
        # comes back on every run.
        assert reported, "the CLI reported no rooms at all, so nothing below ran"
        assert "general" in [room["name"] for room in reported], reported
        assert {room["name"] for room in reported} <= set(GROUP_ROOMS), reported
        for room in reported:
            assert room["created"] is False, room
            assert before.get(room["token"]) == room["name"], (room, before)

        after = {
            room["token"]: room.get("displayName", "")
            for room in nextcloud.rooms()
        }
        assert sorted(
            name for name in after.values() if name in GROUP_ROOMS
        ) == before_defaults, (
            "a provisioning run over existing rooms created or lost a default "
            f"room: before={before_defaults} after={sorted(after.values())}"
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
