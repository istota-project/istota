"""What the email surface writes to the bell, and what it pushes, on the lean stack.

One row per producer. Since #638 a push on the `alert` purpose is a title plus
text a producer fixed or the system composed, never somebody's words: the alert
route can be a room, and the bell row, which only the user reads, is where the
words go. testuser's alert route is `email,ntfy` (`email_people`), so every
push is read twice, off the ntfy stub and out of the catch-all, and
`email_flow.assert_outcome` checks the sender's words are in neither.

Five of the six producers are already driven end to end, with their source,
dedup key, push decision and push text, by the file whose subject they are.
They are named here rather than run twice:

| Producer | Row | Pushed | Driven by |
|---|---|---|---|
| gate hold, prompt delivered | `confirmation`, `task:<id>` | no: the one push is the prompt (`flatten_body` of the composed question), and the row is not delivered because the prompt reached someone (`inbound._deliver_confirmation_prompts`) | `test_email_senders.py::TestCorrespondents::test_a_stranger_at_the_plus_address_is_held` |
| gate hold, prompt undeliverable | `confirmation`, `task:<id>` | yes, on the whole alert route (`in_room=False`, `14cec6c2`) | `tests/testbed/test_email_prompt_push.py`, on the wire tier, since it needs a routing that fails once |
| outbound hold | `outbound_draft`, `outbound_draft:<id>` | yes, the stored notice | `test_email_outbound_gate.py::TestTheUntrustedFloor::test_one_stranger_among_the_recipients_holds_the_reply` |
| DMARC canary on a failing self-claim | `task_alert`, `dmarc:fail` | yes, the canary's composed alert | `test_email_senders.py::TestTheHostsOwnAddress::test_a_failing_stamp_on_the_hosts_address_is_held` |
| email note, no private room | `task_alert`, `private-note:<task>` | yes, `PRIVATE_NOTE_POINTER`, the note body in the row only | `test_email_senders.py::TestCorrespondents::test_a_trusted_sender_mints_a_thread_and_is_answered` and every note case in `test_email_notes.py` |
| scheduler park on a thread room, no private room | `confirmation`, `task:<id>` | yes, on the whole alert route, `park_title` and `PARK_BELL_BODY` | this file |

**The park.** A thread task whose answer ends asking for confirmation
(`scheduler.asks_for_confirmation`) parks, and `private_replies.park_about`
names the thread room, so the question is the principal's to answer privately.
`deliver_private` finds no private room on lean and, for kind `confirmation`,
writes nothing (only `whisper` and `pass_on` get a bell note there), so the
`confirmation` row is the only place the question is. It is written with the
fixed `park_title` and `PARK_BELL_BODY` and `in_room=False`, and pushed at once
on the whole alert route.

**What the bell shows for that row** is the model's question, not the note
shape. `ConfirmationResolver.resolve` renders `body_for(task.confirmation_prompt)`,
and `process_one_task` stores the model's answer there
(`db.set_task_confirmation(conn, task_id, result, ...)`). The note-shaped body
(`email_note(..., outcome="parked")`, `Question for you.`) is built only for
`deliver_private`, which has no room to put it in here. On the full shape it is
the private room's row instead. The bell's title is the push's: the resolver
renders the row's stored title, `park_title` ("Task #N is waiting for your
approval"), rather than recomputing the gate's label from the task (#663).

**The `14cec6c2` control cannot turn this file red.** With the alert route
`email,ntfy`, the whole route and the room-free cut (`ROOM_FREE_SURFACES`) are
the same two surfaces, so `in_room` changes nothing observable on lean. The
wire file above drives the same flag with a Talk route, where it does; the
scheduler's half is in the default suite (`tests/test_private_channel_push.py`).
"""

from __future__ import annotations

import json

import pytest

from istota.notifications.resolvers.confirmation import (
    PARK_BELL_BODY,
    body_for,
    park_title,
)
from istota.rooms.private_replies import NOTE_OUTCOMES
from testbed.services import mail

from ..support import email_flow as flow

pytestmark = [pytest.mark.smoke, pytest.mark.profile("email")]


#: Renders one notification row through its source's resolver, read only,
#: inside the container: the view before `store.list_open` applies its
#: liveness pass and URL allowlist. argv[1] is the row id.
_RESOLVE_ROW = (
    "import json, sqlite3, sys\n"
    "from pathlib import Path\n"
    "from istota.config import load_config\n"
    "from istota.lib.sqlite_util import connect_read_only\n"
    "from istota.notifications import sources, store\n"
    "config = load_config(Path('/data/config/config.toml'))\n"
    "conn = connect_read_only(config.db_path)\n"
    "conn.row_factory = sqlite3.Row\n"
    "raw = conn.execute('SELECT * FROM notifications WHERE id = ?',"
    " (int(sys.argv[1]),)).fetchone()\n"
    "row = store._row_to_notification(raw)\n"
    "view = sources.get_resolver(row.source).resolve(config, conn, row)\n"
    "print(json.dumps(None if view is None else {'title': view.title,"
    " 'body': view.body, 'actions': [a.id for a in view.actions]}))\n"
)


def _header(call, name: str) -> str | None:
    for key, value in call.headers.items():
        if key.lower() == name.lower():
            return value
    return None


class TestTheSchedulerPark:
    def test_a_question_on_a_thread_with_no_private_room_is_the_bells(
        self, stack, email_people,
    ):
        """A trusted correspondent mails the host's plus-address and the
        model's answer asks for confirmation. The task parks; nothing goes to
        the thread, by mail or in its transcript; no note is written; the
        `confirmation` row carries fixed text and is pushed on both alert
        surfaces; the bell renders the question from the live task."""
        nonce = flow.new_nonce()
        question = (f"I can cancel the standing order for them {nonce}.\n\n"
                    "Should I proceed?")
        stack.script([flow.route(nonce, [{"text": question}])])
        sender = flow.person("trusted", nonce)
        plus = mail.tagged(email_people.host_id)
        sent = flow.send(
            stack, sender, to=[plus], subject=f"park {nonce}",
            text=f"please cancel the standing order {nonce}", marker=nonce,
        )

        seen = flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="pending_confirmation", host_absent=True),
            room="thread",
            participants=frozenset({(sender.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus,), cc=(), sender_check="verified",
                                       trusted=True),
                bot=None,
            ),
            reply=None,
            # A parked task's note is its private confirmation, and with no
            # private room that is the `confirmation` row: no `private-note:`.
            note=None,
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}")}),
                pushes=(PARK_BELL_BODY,),
                alert_mails=1,
            ),
        ))
        task = seen.task
        assert task["confirmation_prompt"] == question, task

        # The push is the stored title and fixed body on both surfaces.
        title = park_title(task["id"])
        assert [_header(call, "Title") for call in seen.pushes] == [title]
        alerts = [
            m for m in flow.bot_mail_since(stack, sent.outbox_uid)
            if flow.user_id_address(email_people.host_id) in flow.recipients(m, "To")
        ]
        assert [(m.subject, m.body_text.strip()) for m in alerts] == [
            (title, PARK_BELL_BODY),
        ], [(m.subject, m.body_text) for m in alerts]

        # The question went nowhere but the bell: not into a push, not into
        # an alert mail, not onto the thread's transcript.
        asked = "I can cancel the standing order for them"
        for call in seen.pushes:
            carried = call.body.decode("utf-8", "replace") + " " + " ".join(
                call.headers.values())
            assert asked not in carried, carried
        for message in alerts:
            assert asked not in message.body_text, message.body_text
        for row in stack.probe.room_messages(seen.room):
            assert asked not in (row.get("body") or ""), (row["role"], row.get("body"))

        rows = stack.probe.notifications(email_people.host_id,
                                         dedup_key=f"task:{task['id']}")
        assert len(rows) == 1, rows
        assert (rows[0]["source"], rows[0]["title"], rows[0]["body"]) == (
            "confirmation", title, PARK_BELL_BODY,
        ), rows[0]
        assert rows[0]["last_delivered_at"] is not None, rows[0]

        # The bell renders the model's question, flattened by `body_for`, with
        # Confirm and Discard; not the note shape `deliver_private` was given.
        result = stack.exec(
            ["uv", "run", "python", "-c", _RESOLVE_ROW, str(rows[0]["id"])],
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        view = json.loads(result.stdout.strip().splitlines()[-1])
        assert view is not None, "the resolver called a live park's row stale"
        assert view["body"] == body_for(question), view
        assert view["title"] == title, view
        assert NOTE_OUTCOMES["parked"] not in view["body"], view
        assert view["actions"] == ["confirm", "discard"], view
