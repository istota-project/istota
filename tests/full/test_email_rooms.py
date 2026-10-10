"""Email threads seen from the web app, on the full shape.

What the lean email files cannot reach, because the lean shape runs no web
app: testuser has no private room there, so every email note is the bell
fallback and every held draft waits for a route nobody can call. Here testuser
signs in through nginx as `test_email_login.py` does, creates three web rooms
(rooms are user-created, so the test does not insert one) and pins the first
as `default_room` through the settings route, which makes it the room
`private_replies.private_room_for` resolves for every email thread:

- **private**, testuser's only member and the pinned default: where notes,
  parked questions and linked turns go;
- **other**, testuser's too, but not the default, so not the private room for
  any thread (`about_room_link` refuses a send from it);
- **shared**, with alice added, the target of a `room post`.

The host's alert route is the provisioned Talk alerts room plus ntfy
(`tests/full/conftest.py`), so a push the product confines to ntfy and email
(`notifications.store.ROOM_FREE_SURFACES`) is visibly not a post in Talk.
Every mail's outcome goes through `email_flow.assert_outcome`, as on lean;
what only the web app shows (the history payload, the drafts list, the room
list) is read through its own routes.

Two cases differ from the spec's wording, and the product is what is
asserted:

- **The web confirm route's 409** has two arms, and both are driven. A
  relay-held task, one with `whatsapp_confirmation_request_id` (a relay
  question, a `room post`, a guest proposal), is refused from any room that
  does not show its preview (`webui.app._chat_confirm_task` asks
  `preview_rooms` only then): a `room post` into the shared room. A parked
  email-thread question has no request, and since #665 it is refused only
  from the thread room itself, which shows no question to answer:
  `TestThePrivatePark`.
- **"Default routing"** for the gate prompt is the user's alert route, which
  the seeding sets to the Talk alerts room plus ntfy rather than leaving
  unset; the Talk half is the provisioned alerts room either way
  (`notifications.delivery.resolve_destinations`).
"""

from __future__ import annotations

import re
import secrets
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from email.utils import parseaddr
from html import unescape

import httpx
import pytest

from istota.notifications.resolvers.confirmation import (
    PARK_BODY,
    PARK_UNDELIVERED_BODY,
    park_title,
)
from istota.notifications.resolvers.outbound_draft import dedup_key as draft_dedup_key
from istota.rooms.private_replies import NOTE_OUTCOMES
from istota.webui.app import (
    _CONFIRM_FROM_PRIVATE_CHAT,
    _CONFIRM_NOT_IN_THREAD,
    ABOUT_ROOM_REFUSED,
    EMAIL_THREAD_READ_ONLY,
)
from testbed.services import mail
from testbed.stack import CONTAINER_CONFIG

from ..support import email_flow as flow
from .conftest import alerts_token

pytestmark = pytest.mark.full

FULL = pytest.mark.profile("full")

API = "/istota/api"

#: Prints where the draft notice's Open action goes: the stored row rendered by
#: the source's own resolver, inside the container through the daemon's config.
_OPEN_HREF = (
    "import sys\n"
    "from pathlib import Path\n"
    "from istota import db\n"
    "from istota.config import load_config\n"
    "from istota.notifications import store\n"
    "from istota.notifications.resolvers.outbound_draft import RESOLVER, dedup_key\n"
    "config = load_config(Path('/data/config/config.toml'))\n"
    "with db.get_db(config.db_path) as conn:\n"
    "    raw = conn.execute('SELECT * FROM notifications WHERE user_id = ? "
    "AND dedup_key = ?', (sys.argv[2], dedup_key(int(sys.argv[1])))).fetchone()\n"
    "    view = RESOLVER.resolve(config, conn, store._row_to_notification(raw))\n"
    "    print(next((a.href for a in view.actions if a.id == 'open'), None)"
    " if view else None)\n"
)


# -- signing in, and the three rooms ---------------------------------------------


@dataclass(frozen=True)
class Rooms:
    """The web rooms testuser made, as `/chat/rooms` hands them out."""

    password: str = field(repr=False)
    origin: str
    private: dict
    other: dict
    shared: dict


@dataclass
class Web:
    """A signed-in client and testuser's rooms."""

    client: httpx.Client
    rooms: Rooms

    def get(self, path: str, **kw) -> httpx.Response:
        return self.client.get(API + path, **kw)

    def post(self, path: str, json=None) -> httpx.Response:
        return self.client.post(API + path, json=json)

    def patch(self, path: str, json) -> httpx.Response:
        return self.client.patch(API + path, json=json)

    def put(self, path: str, json) -> httpx.Response:
        return self.client.put(API + path, json=json)


def _form_fields(page: str, action: str) -> dict[str, str]:
    form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>',
                     page, re.S)
    assert form is not None, f"Missing form: {action}"
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])
    }


def _sign_in(stack, password: str, origin: str) -> httpx.Client:
    """A client signed in as testuser with the email identity's password.

    `Origin` names the configured site hostname, which `_verify_origin` checks
    on every state-changing API call.
    """
    port = stack.published_port("nginx", 80)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60,
                          trust_env=False, headers={"Origin": origin})
    page = client.get("/istota/login")
    assert page.status_code == 200, page.text
    fields = _form_fields(page.text, "/istota/login/email")
    signed_in = client.post("/istota/login/email", data={
        **fields, "email": flow.HOST_ADDRESS, "password": password,
    })
    assert signed_in.status_code == 302, signed_in.text
    me = client.get(API + "/me")
    assert me.status_code == 200 and me.json()["username"] == flow.HOST_ID, me.text
    return client


def _make_room(web: Web, name: str) -> dict:
    made = web.post("/chat/rooms", {"name": name})
    assert made.status_code == 200, made.text
    return made.json()


def _set_up(stack, email_people) -> Web:
    """testuser's email login, then the three rooms, once per stack.

    One signed-in client for the whole stack: the login route throttles
    attempts per address (`webui.auth`, `throttle_max_email`), and a sign-in
    per test reached it within this one file.
    """
    password = secrets.token_urlsafe(24)
    added = subprocess.run(
        stack.args + ["exec", "-T", "istota", "istota-drop", "uv", "run", "istota", "-c",
                      CONTAINER_CONFIG, "auth", "add", email_people.host_id,
                      "--email", email_people.host_address, "--password-stdin"],
        input=password + "\n", text=True, capture_output=True, timeout=120,
    )
    assert added.returncode == 0, added.stderr
    rendered = stack.exec(["cat", CONTAINER_CONFIG])
    hostname = tomllib.loads(rendered.stdout)["site"]["hostname"]
    origin = f"http://{hostname}"

    bare = Web(_sign_in(stack, password, origin), Rooms(password, origin, {}, {}, {}))
    private = _make_room(bare, "private notes")
    other = _make_room(bare, "another room")
    shared = _make_room(bare, "shared with alice")
    pinned = bare.put("/settings/profile", {"default_room": private["token"]})
    assert pinned.status_code == 200, pinned.text
    # The web process learns of alice through its own profile refresh.
    deadline = time.monotonic() + 30
    while True:
        added = bare.post(f"/chat/rooms/{shared['id']}/members",
                          {"user_id": email_people.alice_id,
                           "acknowledge_history": True})
        if added.status_code in (200, 201) or time.monotonic() >= deadline:
            break
        time.sleep(2)
    assert added.status_code in (200, 201), added.text
    # The setup's writes must not land inside the first test's window.
    stack.mark = stack.reset(list(stack.endpoint.turns))
    return Web(bare.client, Rooms(password, origin, private, other, shared))


@pytest.fixture
def web(stack, email_people) -> Web:
    signed_in = getattr(stack, "_email_web", None)
    if signed_in is None:
        signed_in = _set_up(stack, email_people)
        stack._email_web = signed_in
    return signed_in


# -- reading the stack -----------------------------------------------------------


def _alerts(stack) -> str:
    token = getattr(stack, "_email_alerts_token", None)
    if token is None:
        token = alerts_token(stack)
        stack._email_alerts_token = token
    return token


def _alerts_posts(stack, above: int) -> list[dict]:
    """Posts in the Talk alerts room above message id `above`, system
    messages (joins, renames) left out."""
    nextcloud = stack.service("nextcloud")
    return [m for m in nextcloud.messages(_alerts(stack), limit=100)
            if int(m.get("id", 0)) > above and m.get("actorType") == "users"
            and not m.get("systemMessage")]


def _alerts_mark(stack) -> int:
    nextcloud = stack.service("nextcloud")
    return max((int(m.get("id", 0)) for m in
                nextcloud.messages(_alerts(stack), limit=20)), default=0)


def _history(web: Web, room: dict) -> list[dict]:
    answer = web.get(f"/chat/rooms/{room['id']}/messages", params={"limit": 200})
    assert answer.status_code == 200, answer.text
    return answer.json()["messages"]


def _history_row(web: Web, room: dict, msg_id: int) -> dict:
    rows = [r for r in _history(web, room) if r.get("msg_id") == msg_id]
    assert len(rows) == 1, (msg_id, _history(web, room))
    return rows[0]


def _listed(web: Web, token: str) -> dict:
    answer = web.get("/chat/rooms")
    assert answer.status_code == 200, answer.text
    rooms = [r for r in answer.json()["rooms"] if r["token"] == token]
    assert len(rooms) == 1, (token, answer.json()["rooms"])
    return rooms[0]


def _open_href(stack, draft_id: int) -> str:
    result = stack.exec(
        ["uv", "run", "python", "-c", _OPEN_HREF, str(draft_id), flow.HOST_ID],
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def _note_parts(note: dict) -> dict:
    parts = note.get("email_note")
    assert isinstance(parts, dict), note
    return parts


def _header(call, name: str) -> str | None:
    for key, value in call.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _bot_address(message: mail.ReceivedMessage) -> str:
    return parseaddr(message.sender)[1].lower()


def _wait(read, timeout: float = flow.DEFAULT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while True:
        found = read()
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(flow.POLL_INTERVAL)


def _web_task(stack, web: Web, room: dict, marker: str, text: str,
              **extra) -> int:
    """Send `text` with `marker` into `room` from the web app; the task id."""
    flow.sent_markers(stack).add(flow.marker_text(marker))
    sent = web.post(f"/chat/rooms/{room['id']}/messages",
                    {"text": f"{text} {flow.marker_text(marker)}", **extra})
    assert sent.status_code == 200, sent.text
    task_id = sent.json().get("task_id")
    assert task_id, sent.json()
    return int(task_id)


def _finished(stack, task_id: int, status: str = "completed") -> dict:
    task = stack.probe.wait_for_task(status=status, task_id=task_id,
                                     timeout=flow.DEFAULT_TIMEOUT)
    assert task["status"] == status, task
    if status == "completed":
        assert flow.worker_done(stack, task_id), f"task {task_id} never finished"
    return task


def _notification(stack, key: str) -> dict:
    rows = stack.probe.notifications(flow.HOST_ID, dedup_key=key)
    assert len(rows) == 1, (key, rows)
    return rows[0]


def _thread_bot_row(stack, room: str, task_id: int) -> dict:
    rows = [r for r in stack.probe.room_messages(room)
            if r["role"] == "assistant" and r.get("task_id") == task_id]
    assert len(rows) == 1, rows
    return rows[0]


# -- mail onto a thread --------------------------------------------------------------


def _trusted_thread(stack, email_people, nonce: str, turns: list[dict],
                    *, cc_kind: str | None = None):
    """A trusted correspondent mails the host's plus-address, with someone
    of `cc_kind` on Cc when given. The sender is trusted by pattern, so the
    mail is not held and mints a thread room with everyone but the host as a
    guest; the host is not on it, so a note is due whatever the outcome."""
    stack.script([flow.route(nonce, turns)])
    sender = flow.person("trusted", nonce)
    other = flow.person(cc_kind, f"cc-{nonce}") if cc_kind else None
    plus = mail.tagged(email_people.host_id)
    sent = flow.send(
        stack, sender, to=[plus], cc=[other.address] if other else [],
        subject=f"rooms {nonce}", text=f"please confirm the delivery date {nonce}",
        marker=nonce,
    )
    return sent, sender, other, plus


def _thread_expected(email_people, sender, other, plus, *, bot, reply, note,
                     notices, status: str = "completed") -> flow.Expected:
    guests = {(sender.address, "guest", None)}
    if other is not None:
        guests.add((other.address, "guest", None))
    return flow.Expected(
        processed=flow.Processed(
            routing_method="plus_address", user_id=email_people.host_id,
            host_asked=False, sender_check="verified",
        ),
        task=flow.TaskState(status=status, host_absent=True),
        room="thread",
        participants=frozenset(guests),
        transcript=flow.Transcript(
            incoming=flow.Incoming(to=(plus,), cc=(other.address,) if other else (),
                                   sender_check="verified", trusted=True),
            bot=bot,
        ),
        reply=reply,
        note=note,
        notices=notices,
    )


def _sent_thread(stack, email_people, nonce: str):
    """A trusted correspondent's mail answered on the thread, unheld: the
    note is `Replied.` in the private room."""
    answer = f"Delivery is on Tuesday {nonce}."
    sent, sender, other, plus = _trusted_thread(
        stack, email_people, nonce, [flow.email_answer(answer)])
    seen = flow.assert_outcome(stack, sent, _thread_expected(
        email_people, sender, other, plus,
        bot=flow.BotRow(body=answer, mail_state="sent"),
        reply=flow.Reply(to=(sender.address,), cc=()),
        note=flow.Note(outcome="sent", without_you=True, remark=None),
        notices=flow.Notices(
            rows=frozenset({("task_alert", "private-note:{task}")}),
            pushes=(flow.PRIVATE_NOTE_POINTER,), alert_mails=0,
        ),
    ))
    return sent, sender, answer, seen


# -- the note ------------------------------------------------------------------------


@FULL
class TestTheNoteInThePrivateRoom:
    def test_a_correspondents_mail_is_noted_in_the_private_web_room(
        self, stack, email_people, web,
    ):
        """The note is a system row in the private room, tagged with the
        thread, carrying its parts (`deliver_private`, kind `pass_on`). A
        web-only room is pushed nothing itself, so `_send` writes one bell row
        and delivers it room-free: the pointer reaches ntfy and the Talk alerts
        room hears nothing. The history gives the note the task and the thread
        row's live mail card (`_noted_rows`)."""
        talk_mark = _alerts_mark(stack)
        nonce = flow.new_nonce()
        sent, _sender, _answer, seen = _sent_thread(stack, email_people, nonce)
        task_id = seen.task["id"]

        note = stack.probe.email_note(task_id)
        assert note is not None
        assert (note["room_token"], note["role"], note["about_room_token"]) == (
            web.rooms.private["token"], "system", seen.room,
        ), note
        parts = _note_parts(note)
        assert set(parts) == {"header", "outcome", "remark"}, parts
        assert parts["outcome"] == NOTE_OUTCOMES["sent"], parts
        assert "without you on the message" in parts["header"], parts
        assert parts["remark"] == "", parts

        assert _alerts_posts(stack, talk_mark) == []

        row = _history_row(web, web.rooms.private, note["id"])
        assert row.get("task_id") == task_id, row
        assert row.get("mail", {}).get("state") == "sent", row
        assert row.get("email_note", {}).get("outcome") == NOTE_OUTCOMES["sent"], row


@FULL
class TestTheThreadRoomInTheWebApp:
    def test_the_thread_room_is_a_hidden_read_only_mail_view(
        self, stack, email_people, web,
    ):
        """`/chat/rooms` lists the thread room hidden and read-only
        (`_room_phone_fields`, the email-thread arm); a send into it is
        refused before anything is recorded; `listed` can be switched on for a
        thread and is refused on any other room (`_RoomNotListable`). Its
        history renders the incoming mail's card from `received_mail` and the
        bot's row as the mailed body with an outgoing card that carries no
        second copy of it."""
        nonce = flow.new_nonce()
        sent, sender, answer, seen = _sent_thread(stack, email_people, nonce)
        thread = seen.room

        listed = _listed(web, thread)
        assert (listed["email_thread"], listed["listed"], listed["read_only"]) == (
            True, False, True,
        ), listed

        since = stack.probe.watermark()
        refused = web.post(f"/chat/rooms/{listed['id']}/messages",
                           {"text": f"a reply typed in web {nonce}"})
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"] == EMAIL_THREAD_READ_ONLY
        assert stack.probe.rows_above("tasks", since,
                                      conversation_token=thread) == []
        assert [r for r in stack.probe.room_messages(thread, id_above=since["messages"])
                if nonce in (r.get("body") or "")] == []

        shown = web.patch(f"/chat/rooms/{listed['id']}", {"listed": True})
        assert shown.status_code == 200, shown.text
        assert _listed(web, thread)["listed"] is True
        not_a_thread = web.patch(f"/chat/rooms/{web.rooms.private['id']}",
                                 {"listed": True})
        assert not_a_thread.status_code == 400, not_a_thread.text

        rows = _history(web, listed)
        incoming = [r for r in rows if r["role"] == "user"
                    and r.get("task_id") == seen.task["id"]]
        assert len(incoming) == 1, rows
        card = incoming[0].get("received_mail") or {}
        assert (
            [p["address"] for p in card.get("to", [])],
            [p["address"] for p in card.get("cc", [])],
            card.get("sender_check"), card.get("trusted"),
        ) == ([mail.tagged(email_people.host_id)], [], "verified", True), card
        assert card["from"]["address"] == sender.address, card
        bot = [r for r in rows if r["role"] == "assistant"
               and r.get("task_id") == seen.task["id"]]
        assert len(bot) == 1, rows
        assert bot[0]["text"].strip() == answer, bot[0]
        assert bot[0]["mail"]["state"] == "sent", bot[0]
        assert "body" not in bot[0]["mail"], bot[0]


# -- held drafts, decided from the web ---------------------------------------------


def _held_thread_reply(stack, email_people, web, nonce: str):
    """A thread reply the outbound gate holds: the reply-all has a stranger
    on Cc (`recipients_require_hold`, the `untrusted` branch). The draft
    notice goes out on the whole alert route, Talk included; the note is
    `Reply waiting for your approval.` in the private room, pushed room-free."""
    talk_mark = _alerts_mark(stack)
    answer = f"Delivery is on Tuesday {nonce}."
    sent, sender, other, plus = _trusted_thread(
        stack, email_people, nonce, [flow.email_answer(answer)], cc_kind="stranger")
    task_id = flow.filed_row(stack, sent)["task_id"]
    assert task_id is not None, sent
    task = _finished(stack, task_id)
    since = flow.Checkpoint(mark=stack.mark, pushes=0)
    draft, key, push = flow.held_draft(stack, task["id"], since)
    seen = flow.assert_outcome(stack, sent, _thread_expected(
        email_people, sender, other, plus,
        bot=flow.BotRow(body=answer, mail_state="held"),
        reply=None,
        note=flow.Note(outcome="held", without_you=True, remark=None),
        notices=flow.Notices(
            rows=frozenset({("outbound_draft", key),
                            ("task_alert", "private-note:{task}")}),
            pushes=(push, flow.PRIVATE_NOTE_POINTER), alert_mails=0,
        ),
    ))
    posts = _wait(lambda: _alerts_posts(stack, talk_mark))
    assert [f"!drafts send {draft['id']}" in p["message"] for p in posts] == [True], posts

    note = stack.probe.email_note(task["id"])
    assert note is not None and note["room_token"] == web.rooms.private["token"], note
    row = _history_row(web, web.rooms.private, note["id"])
    assert (row.get("task_id"), row.get("mail", {}).get("state")) == (
        task["id"], "held"), row

    listed = web.get("/chat/drafts")
    assert listed.status_code == 200, listed.text
    assert [(d["id"], d["status"]) for d in listed.json()["drafts"]
            if d["task_id"] == task["id"]] == [(draft["id"], "pending")], listed.json()
    assert _open_href(stack, draft["id"]) == (
        f"/chat/r/{web.rooms.private['token']}/t/{task['id']}"
    )
    return sent, sender, other, draft, key, note, seen


@FULL
class TestAHeldThreadReply:
    def test_released_from_the_web_it_is_mailed_and_the_card_follows(
        self, stack, email_people, web,
    ):
        """Approving the draft sends the stored bytes (`drafts.release`), moves
        the thread row's card to `sent` (`db.settle_draft_mail`) and resolves
        the `outbound_draft` row. The note is not rewritten: its card reads the
        thread row live, so the same note now shows `sent`."""
        nonce = flow.new_nonce()
        sent, sender, other, draft, key, note, seen = _held_thread_reply(
            stack, email_people, web, nonce)

        approved = web.post(f"/chat/drafts/{draft['id']}/approve")
        assert approved.status_code == 200, approved.text
        reply = _wait(lambda: flow.reply_to(stack, sent))
        assert reply is not None, "the released draft never reached the wire"
        assert (flow.recipients(reply, "To"), flow.recipients(reply, "Cc")) == (
            (sender.address,), (other.address,))

        card = _thread_bot_row(stack, seen.room, seen.task["id"])["outgoing_mail"]
        assert (card["state"], card.get("draft_id")) == ("sent", draft["id"]), card
        assert [d["status"] for d in stack.probe.drafts(flow.HOST_ID)
                if d["id"] == draft["id"]] == ["sent"]
        assert _notification(stack, key)["state"] == "resolved"

        row = _history_row(web, web.rooms.private, note["id"])
        assert row.get("mail", {}).get("state") == "sent", row
        assert _note_parts(stack.probe.email_note(seen.task["id"]))["outcome"] == (
            NOTE_OUTCOMES["held"])

    def test_discarded_from_the_web_nothing_is_mailed(self, stack, email_people, web):
        """Discarding the draft sends nothing, moves the thread row's card to
        `discarded` (`drafts.discard` through `settle_draft_mail`) and
        resolves the `outbound_draft` row."""
        nonce = flow.new_nonce()
        sent, _sender, _other, draft, key, note, seen = _held_thread_reply(
            stack, email_people, web, nonce)

        discarded = web.post(f"/chat/drafts/{draft['id']}/discard")
        assert discarded.status_code == 200, discarded.text
        card = _thread_bot_row(stack, seen.room, seen.task["id"])["outgoing_mail"]
        assert card["state"] == "discarded", card
        assert [d["status"] for d in stack.probe.drafts(flow.HOST_ID)
                if d["id"] == draft["id"]] == ["discarded"]
        assert _notification(stack, key)["state"] == "resolved"
        assert _wait(lambda: flow.reply_to(stack, sent),
                     timeout=flow.NEGATIVE_SETTLE) is None
        row = _history_row(web, web.rooms.private, note["id"])
        assert row.get("mail", {}).get("state") == "discarded", row


def _held_send(stack, web, nonce: str):
    """A task in the private room runs `email send` to a stranger. The
    outbound gate holds it as a draft (`skills/email._outbound_gate`), and a
    draft mints no thread until it is released."""
    stranger = flow.person("stranger", nonce)
    subject = f"Introduction {nonce}"
    body = f"Hello, we met at the fair {nonce}."
    command = (f"istota-skill email send --to {stranger.address} "
               f"--subject '{subject}' --body '{body}'")
    stack.script([
        flow.route(nonce, [
            {"tool_calls": [{"id": f"call-send-{nonce}", "name": "Bash",
                             "arguments": {"command": command}}]},
            {"text": f"drafted it {nonce}"},
        ]),
        flow.route(f"r-{nonce}", [{"text": "NO_ACTION:"}]),
    ])
    task = _finished(stack, _web_task(
        stack, web, web.rooms.private, nonce, "please write to the person from the fair"))
    drafts = [d for d in stack.probe.drafts(flow.HOST_ID, id_above=stack.mark["outbound_drafts"])
              if d.get("task_id") == task["id"]]
    assert [(d["status"], d["to_addrs"]) for d in drafts] == [
        ("pending", [stranger.address])], drafts
    draft = drafts[0]
    assert stack.probe.rows_above("sent_emails", stack.mark, subject=subject) == []
    key = draft_dedup_key(draft["id"])
    assert _notification(stack, key)["state"] == "open"
    return stranger, subject, body, draft, key


@FULL
class TestAnEmailSendToAStranger:
    def test_released_it_mints_the_thread_and_the_strangers_reply_is_not_held(
        self, stack, email_people, web,
    ):
        """Released from the web, the mail goes out and `drafts.release`
        registers the thread (`threads.register_sent_thread`) with the
        stranger on it. Their reply on that thread is admitted without a hold,
        since they are present, and runs as the host; the scripted answer is a
        bare `NO_ACTION:`, so the note in the private room is `No reply
        sent.`."""
        nonce = flow.new_nonce()
        outbox = flow.outbox_uid(stack)
        stranger, subject, body, draft, key = _held_send(stack, web, nonce)

        approved = web.post(f"/chat/drafts/{draft['id']}/approve")
        assert approved.status_code == 200, approved.text
        out = flow.wait_for_reply(stack, after_uid=outbox, subject=subject)
        assert flow.recipients(out, "To") == (stranger.address,)
        thread = _wait(lambda: stack.probe.email_room(out.message_id))
        assert thread is not None, f"the release minted no thread: {out.message_id}"
        assert {(p["surface_ref"], p["kind"], p["user_id"])
                for p in stack.probe.participants(thread) if p["left_at"] is None} == {
            (stranger.address, "guest", None)}
        assert [d["status"] for d in stack.probe.drafts(flow.HOST_ID)
                if d["id"] == draft["id"]] == ["sent"]
        assert _notification(stack, key)["state"] == "resolved"

        since = flow.checkpoint(stack)
        bot = _bot_address(out)
        reply = flow.send(
            stack, stranger, to=[bot], subject=f"Re: {subject}",
            text=f"good to hear from you {nonce}", marker=f"r-{nonce}",
            reply_to_msg=out,
        )
        flow.assert_outcome(stack, reply, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(stranger.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(bot,), cc=(), sender_check="verified",
                                       trusted=False),
                bot=None,
            ),
            reply=None,
            note=flow.Note(outcome="none", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("task_alert", "private-note:{task}")}),
                pushes=(flow.PRIVATE_NOTE_POINTER,), alert_mails=0,
            ),
        ), since=since)

    def test_discarded_it_sends_and_mints_nothing(self, stack, email_people, web):
        """Discarded from the web, the draft sends nothing, and the stranger
        is on no thread: no participant row names their address, which is
        unique to this test's nonce."""
        nonce = flow.new_nonce()
        stranger, subject, _body, draft, key = _held_send(stack, web, nonce)

        discarded = web.post(f"/chat/drafts/{draft['id']}/discard")
        assert discarded.status_code == 200, discarded.text
        assert [d["status"] for d in stack.probe.drafts(flow.HOST_ID)
                if d["id"] == draft["id"]] == ["discarded"]
        assert _notification(stack, key)["state"] == "resolved"
        time.sleep(flow.NEGATIVE_SETTLE)
        assert stack.probe.rows_above("sent_emails", stack.mark, subject=subject) == []
        assert [m for m in flow.bot_mail_since(stack, 0) if m.subject == subject] == []
        assert stack.probe.rows_above("room_participants", stack.mark,
                                      surface_ref=stranger.address) == []


# -- linking, parking, posting -------------------------------------------------------


def _host_thread(stack, email_people, nonce: str, routes: list[dict]) -> str:
    """The host mails a trusted correspondent with the bot in To; the thread
    room's token. Two humans are on it (the host a principal, the
    correspondent a guest), so it is a shared room. Answered unheld, and with
    the host on the mail no note is written. `routes` are installed in the
    same script, so the opener's route stays in place for the rest of the
    test (a channel's memory pass can quote the opener later)."""
    answer = f"Thursday works for everyone {nonce}."
    stack.script([flow.route(nonce, [flow.email_answer(answer)]), *routes])
    friend = flow.person("trusted", nonce)
    sent = flow.send(
        stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS, friend.address],
        subject=f"plans {nonce}", text=f"can we meet this week {nonce}",
        marker=nonce,
    )
    seen = flow.assert_outcome(stack, sent, flow.Expected(
        processed=flow.Processed(
            routing_method="sender_match", user_id=email_people.host_id,
            host_asked=True, sender_check="verified",
        ),
        task=flow.TaskState(status="completed", host_absent=False),
        room="thread",
        participants=frozenset({
            (email_people.host_address, "principal", email_people.host_id),
            (friend.address, "guest", None),
        }),
        transcript=flow.Transcript(
            incoming=flow.Incoming(to=(mail.BOT_ADDRESS, friend.address), cc=(),
                                   sender_check="verified", trusted=False),
            bot=flow.BotRow(body=answer, mail_state="sent"),
        ),
        reply=flow.Reply(to=(email_people.host_address,), cc=(friend.address,)),
        note=None,
        notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
    ))
    return seen.room


@FULL
class TestDiscussingAThread:
    def test_a_send_from_the_private_room_links_to_a_thread_with_no_note(
        self, stack, email_people, web,
    ):
        """The host mails a trusted correspondent with the bot in To: the
        thread is minted, the reply-all goes out unheld, and with the host on
        the mail no note is written. A send from the private room naming the
        thread (`about_room`) makes a task linked to it; the same send from a
        room that is not the private room for that thread is refused
        (`private_replies.about_room_link`) and makes no task."""
        nonce = flow.new_nonce()
        thread = _host_thread(stack, email_people, nonce, [
            flow.route(f"d-{nonce}", [{"text": f"they asked about Thursday {nonce}"}]),
        ])

        linked = _finished(stack, _web_task(
            stack, web, web.rooms.private, f"d-{nonce}", "what do they want",
            about_room=thread))
        assert linked["about_room_token"] == thread, linked
        assert linked["conversation_token"] == web.rooms.private["token"], linked

        since = stack.probe.watermark()
        refused = web.post(f"/chat/rooms/{web.rooms.other['id']}/messages",
                           {"text": f"and from here {nonce}", "about_room": thread})
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"] == ABOUT_ROOM_REFUSED
        assert stack.probe.rows_above(
            "tasks", since, conversation_token=web.rooms.other["token"]) == []


@FULL
class TestThePrivatePark:
    def test_a_thread_question_is_asked_in_the_private_room(
        self, stack, email_people, web,
    ):
        """The model's answer asks for confirmation, so the task parks
        (`scheduler.asks_for_confirmation`) and the question goes to the
        private room as a `private-confirmation:` row in the note's shape
        (`email_note(..., outcome="parked")`). Nothing reaches the thread, and
        the `confirmation` row's push is room-free: ntfy only on this route,
        titled `park_title`, and no Talk post.

        The push carries `PARK_BODY`, not `PARK_UNDELIVERED_BODY`: a web-only
        private room has no surface to push to, so its row is the delivery and
        the park is not owed a re-push (`PrivateDestination.pushes`, #661).
        """
        talk_mark = _alerts_mark(stack)
        nonce = flow.new_nonce()
        question = (f"I can cancel the standing order for them {nonce}.\n\n"
                    "Should I proceed?")
        sent, sender, other, plus = _trusted_thread(
            stack, email_people, nonce, [{"text": question}])
        task_id = flow.filed_row(stack, sent)["task_id"]
        assert task_id is not None, sent
        _finished(stack, task_id, status="pending_confirmation")
        assert flow.worker_done(stack, task_id), f"task {task_id} never finished"
        thread = stack.probe.email_room(sent.message_id)

        rows = [r for r in stack.probe.room_messages(web.rooms.private["token"])
                if (r.get("delivery_reference") or "").startswith(
                    f"private-confirmation:{task_id}:")]
        assert len(rows) == 1, rows
        park = rows[0]
        assert (park["role"], park["about_room_token"]) == ("system", thread), park
        outcome, without_you, remark = flow.parse_note(park["body"])
        assert (outcome, without_you) == ("parked", True), park["body"]
        assert remark and "Should I proceed?" in remark, park["body"]
        assert _note_parts(park)["outcome"] == NOTE_OUTCOMES["parked"]

        ntfy = stack.service("ntfy")
        pushes = _wait(ntfy.pushes)
        assert pushes, "the park pushed nothing"
        assert _header(pushes[0], "Title") == park_title(task_id), pushes[0].headers
        assert question not in " ".join(pushes[0].headers.values())
        assert _alerts_posts(stack, talk_mark) == []

        flow.assert_outcome(stack, sent, _thread_expected(
            email_people, sender, other, plus, status="pending_confirmation",
            bot=None, reply=None, note=None,
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}")}),
                pushes=(PARK_BODY,), alert_mails=0,
            ),
        ))
        assert PARK_UNDELIVERED_BODY not in " ".join(
            " ".join(p.headers.values()) + p.body.decode("utf-8", "replace")
            for p in ntfy.pushes()), pushes

    def test_the_thread_room_hides_the_question_and_refuses_its_confirm(
        self, stack, email_people, web,
    ):
        """A thread task's parked question belongs to the host's private room
        (#665). The thread room's history keeps the mail as a turn and renders
        no answer and no `active_tasks` entry for the task, while the private
        room shows the question. `POST /chat/tasks/{id}/confirm` naming the
        thread room is refused 409 (`_CONFIRM_NOT_IN_THREAD`) and changes
        nothing; naming the private room approves it, and the re-run, scripted
        afresh, answers on the thread.
        """
        nonce = flow.new_nonce()
        question = (f"I can move the delivery to Friday {nonce}.\n\n"
                    "Should I proceed?")
        sent, sender, _other, _plus = _trusted_thread(
            stack, email_people, nonce, [{"text": question}])
        task_id = flow.filed_row(stack, sent)["task_id"]
        assert task_id is not None, sent
        _finished(stack, task_id, status="pending_confirmation")
        assert flow.worker_done(stack, task_id), f"task {task_id} never finished"
        thread = _listed(web, stack.probe.email_room(sent.message_id))

        answer = web.get(f"/chat/rooms/{thread['id']}/messages", params={"limit": 200})
        assert answer.status_code == 200, answer.text
        payload = answer.json()
        mine = [r for r in payload["messages"] if r.get("task_id") == task_id]
        assert [r["role"] for r in mine] == ["user"], mine
        assert [t for t in payload["active_tasks"] if t["id"] == task_id] == [], payload
        park = _wait(lambda: [
            r for r in stack.probe.room_messages(web.rooms.private["token"])
            if (r.get("delivery_reference") or "").startswith(
                f"private-confirmation:{task_id}:")])
        assert park and len(park) == 1, park
        shown = _history_row(web, web.rooms.private, park[0]["id"])
        assert "Should I proceed?" in (shown.get("text") or ""), shown

        refused = web.post(f"/chat/tasks/{task_id}/confirm", {"room": thread["token"]})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == _CONFIRM_NOT_IN_THREAD
        assert stack.probe.tasks(task_id=task_id)[0]["status"] == "pending_confirmation"

        # The re-run carries the same marker and starts at turn 0 again, so
        # its route is replaced before the approval that queues it.
        done = f"Moved to Friday {nonce}."
        stack.script([
            *[item for item in stack.endpoint.turns
              if not (isinstance(item, dict)
                      and item.get("when") == flow.marker_text(nonce))],
            flow.route(nonce, [flow.email_answer(done)]),
        ])
        confirmed = web.post(f"/chat/tasks/{task_id}/confirm",
                             {"room": web.rooms.private["token"]})
        assert confirmed.status_code == 200, confirmed.text
        # The park attempt already logged its worker line, so `_finished`
        # alone would return at once; the re-run's last step is its note.
        _finished(stack, task_id)
        decision = flow.note_step(stack, task_id)
        assert decision is not None and decision.startswith("note written ("), decision
        reply = _wait(lambda: flow.reply_to(stack, sent))
        assert reply is not None and flow.recipients(reply, "To") == (
            sender.address,), reply


@FULL
class TestTheWebConfirmRoute:
    def test_a_held_room_post_is_approved_only_from_its_preview_room(
        self, stack, email_people, web,
    ):
        """A task in the private room asks to post into the shared room. The
        post is held for approval (`hold_room_post`; not a clean turn, since
        the text is not the member's own words) and the task parks with a
        request. `POST /chat/tasks/{id}/confirm` naming a room that does not
        show its preview is refused 409 (`_chat_confirm_task`, #624), and
        naming the private room it was asked in releases the post."""
        nonce = flow.new_nonce()
        posted = f"The delivery moved to Tuesday {nonce}."
        command = (f"istota-skill room post --request-key k-{nonce} "
                   f"--room {web.rooms.shared['token']} '{posted}'")
        stack.script([flow.route(nonce, [
            {"tool_calls": [{"id": f"call-post-{nonce}", "name": "Bash",
                             "arguments": {"command": command}}]},
            {"text": f"asked to post it {nonce}"},
        ])])
        task_id = _web_task(stack, web, web.rooms.private, nonce,
                            "let the shared room know about the delivery")
        task = _finished(stack, task_id, status="pending_confirmation")
        assert task["whatsapp_confirmation_request_id"], task

        for room in (web.rooms.shared, web.rooms.other):
            refused = web.post(f"/chat/tasks/{task_id}/confirm", {"room": room["token"]})
            assert refused.status_code == 409, (room["name"], refused.text)
            assert refused.json()["detail"] == _CONFIRM_FROM_PRIVATE_CHAT
        assert stack.probe.tasks(task_id=task_id)[0]["status"] == "pending_confirmation"
        assert [r for r in stack.probe.room_messages(web.rooms.shared["token"])
                if posted in (r.get("body") or "")] == []

        confirmed = web.post(f"/chat/tasks/{task_id}/confirm",
                             {"room": web.rooms.private["token"]})
        assert confirmed.status_code == 200, confirmed.text
        assert _wait(lambda: [
            r for r in stack.probe.room_messages(web.rooms.shared["token"])
            if posted in (r.get("body") or "")])
        # The re-run replays the same request key; a second copy would be a
        # second post, so the count is read after the task settles.
        _finished(stack, task_id)
        delivered = [r for r in stack.probe.room_messages(web.rooms.shared["token"])
                     if posted in (r.get("body") or "")]
        assert len(delivered) == 1, delivered


@FULL
class TestRoomPostIntoAThread:
    def test_a_post_aimed_at_an_email_thread_is_refused(self, stack, email_people, web):
        """From the private web room the origin check passes, so the refusal
        is the destination's: an email thread room takes no post
        (`private_replies._post_destination`, `email_thread`). On lean the
        origin check refuses first (`unsupported_origin`), which is why this
        case is here.

        The thread has the host and a correspondent on it, since `room post`
        resolves `--room` among the shared rooms only (`rooms.lookup`); a
        plus-address thread with one correspondent is not shared and is
        refused earlier, `parent_unavailable`.
        """
        nonce = flow.new_nonce()
        thread = _host_thread(stack, email_people, nonce, [])
        posted = f"Posting into the mail thread {nonce}."
        command = (f"istota-skill room post --request-key k-{nonce} "
                   f"--room {thread} '{posted}'")
        call = f"call-post-{nonce}"
        # The opener's route is kept beside the new one (see `_host_thread`).
        stack.script([*stack.endpoint.turns, flow.route(f"p-{nonce}", [
            {"tool_calls": [{"id": call, "name": "Bash",
                             "arguments": {"command": command}}]},
            {"text": f"could not post it {nonce}"},
        ])])
        since = stack.probe.watermark()
        outbox = flow.outbox_uid(stack)
        task_id = _web_task(stack, web, web.rooms.private, f"p-{nonce}",
                            "tell the people on that mail")
        _finished(stack, task_id)

        result = stack.endpoint.tool_results_by_id().get(call, "")
        assert "email_thread" in result, result
        assert [r for r in stack.probe.room_messages(thread, id_above=since["messages"])
                if posted in (r.get("body") or "")] == []
        assert stack.probe.query(
            "SELECT id FROM whatsapp_skill_requests WHERE kind = 'room_post' "
            "AND service_body = ?", [posted]) == []
        time.sleep(flow.NEGATIVE_SETTLE)
        assert [m for m in flow.bot_mail_since(stack, outbox)
                if posted in m.body_text] == []


@FULL
class TestTheGatePromptInTalk:
    def test_a_strangers_held_mail_is_asked_about_in_the_alerts_room(
        self, stack, email_people, web,
    ):
        """A stranger at the plus-address is held at intake, and the prompt
        goes to every destination of the alert route
        (`inbound._deliver_confirmation_prompts`): the provisioned Talk alerts
        room and ntfy. The `confirmation` row is written and not pushed
        besides, because the prompt reached someone."""
        talk_mark = _alerts_mark(stack)
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.person("stranger", nonce), to=[mail.tagged(email_people.host_id)],
            subject=f"stranger {nonce}", text=f"a stranger's question {nonce}",
            marker=nonce,
        )
        task = flow.held_task(stack, sent)
        flow.assert_unknown_sender_prompt(task, sent.sender)
        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="pending_confirmation", host_absent=False),
            room="none", participants=None,
            transcript=flow.Transcript(incoming=None, bot=None),
            reply=None, note=None,
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}")}),
                pushes=(flow.prompt_push(task),), alert_mails=0,
            ),
        ))
        posts = _wait(lambda: _alerts_posts(stack, talk_mark))
        assert len(posts) == 1, posts
        assert flow.confirm_command_from(posts[0]["message"]) == f"!confirm {task['id']}"
        assert sent.text not in posts[0]["message"]
