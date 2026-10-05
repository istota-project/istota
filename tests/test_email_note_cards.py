"""Hidden email threads, stage 5: the cards under the private note.

An email note is a `role='system'` row in the host's private room, keyed
``private-pass_on:<task>:pass-on``. History and the room stream give it the
note's `task_id`, so the web attaches the held draft under it and a deep link
to the task lands on it, and the thread row's mail card, read live so a draft
released later shows `sent` here too.
"""
import pytest

from istota import db
from istota.rooms import private_replies

from .test_private_park_card import _mod, _private, client, config  # noqa: F401
from .test_web_chat import _login, _needs_web_deps

MAILED = "Friday works for us. See you at seven."


def _note_ref(ident):
    return f"private-pass_on:{ident}:pass-on"


def _noted_turn(conn, *, state="held", body=MAILED, mail_body=None):
    """A completed thread turn whose mail is in ``state``, and its note in
    Alice's private room. Returns ``(task id, private room, note id)``."""
    thread = db.create_web_chat_room(conn, "alice", "Dinner plans").token
    private = _private(conn)
    ident = db.create_task(conn, user_id="alice", source_type="email", prompt="mail",
                           conversation_token=thread)
    db.update_task_status(conn, ident, "completed")
    row = db.add_message(conn, thread, role="assistant", body=body, origin_surface="email",
                         task_id=ident)
    mail = {"to": ["ana@example.com"], "cc": ["alice@example.com"],
            "subject": "Re: Dinner", "state": state, "draft_id": 7}
    if mail_body is not None:
        mail["body"] = mail_body
    db.set_outgoing_mail(conn, row, mail)
    note = db.add_message(
        conn, private.token, role="system", body="Ana wrote on Dinner plans:\n\n> Friday?",
        origin_surface="web", about_room_token=thread, delivery_reference=_note_ref(ident),
    )
    return ident, private, note, thread


class TestNotedTaskForReference:
    def test_names_the_task_whatever_its_status(self, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            ident, _private_room, _note, _thread = _noted_turn(conn)
            ref = _note_ref(ident)
            assert private_replies.noted_task_for_reference(conn, ref, "alice") == ident
            assert private_replies.noted_task_for_reference(conn, ref, "bob") is None

    @pytest.mark.parametrize("ref", [
        None, "", "private-pass_on:x1:pass-on", "private-pass_on:5:other",
        "private-pass_on:5", "private-whisper:5:pass-on", "private-confirmation:5:abc",
        "private-pass_on:" + "9" * 19 + ":pass-on",
    ])
    def test_a_malformed_reference_names_nothing(self, config, ref):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, user_id="alice", source_type="email", prompt="x")
            assert private_replies.noted_task_for_reference(conn, ref, "alice") is None


class TestEmailNoteTaskForReference:
    def test_names_a_note_and_a_parked_question(self, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            ident, _private_room, _note, _thread = _noted_turn(conn)
            find = private_replies.email_note_task_for_reference
            assert find(conn, _note_ref(ident), "alice") == ident
            assert find(conn, f"private-confirmation:{ident}:ab", "alice") == ident
            assert find(conn, _note_ref(ident), "bob") is None
            assert find(conn, f"private-confirmation:{ident}:ab", "bob") is None

    @pytest.mark.parametrize("ref", [
        None, "", "private-pass_on:x1:pass-on", "private-pass_on:5:other",
        "private-whisper:5:x", "private-proposal:5:x", "private-confirmation:x:ab",
    ])
    def test_anything_else_names_nothing(self, config, ref):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            db.create_task(conn, user_id="alice", source_type="email", prompt="x")
            assert private_replies.email_note_task_for_reference(conn, ref, "alice") is None

    def test_parts_are_written_once(self, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            _ident, _private_room, note, _thread = _noted_turn(conn)
            db.set_email_note(conn, note, {"remark": "first"})
            db.set_email_note(conn, note, {"remark": "second"})
            stored = conn.execute("SELECT email_note FROM messages WHERE id = ?",
                                  (note,)).fetchone()["email_note"]
        assert '"first"' in stored


def _system_rows(payload):
    return [m for m in payload["messages"] if m["role"] == "system"]


@_needs_web_deps
class TestTheNoteCarriesItsTurn:
    async def test_history_gives_the_note_its_task_and_a_live_mail(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            ident, private, _note, thread = _noted_turn(conn)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert note["task_id"] == ident
        assert "confirmation" not in note
        assert note["mail"]["state"] == "held"
        assert note["mail"]["subject"] == "Re: Dinner"
        # The record carries no body when it equals the row; the note's card
        # still shows what was mailed.
        assert note["mail"]["body"] == MAILED
        assert note["mail"]["labels"] == {}
        assert "draft_id" not in note["mail"]
        with db.get_db(_mod()._config.db_path) as conn:
            db.settle_draft_mail(conn, thread, 7, state="sent")
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert note["mail"]["state"] == "sent"

    async def test_an_edited_draft_shows_the_body_that_went(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            _ident, private, _note, _thread = _noted_turn(conn, mail_body="Edited text.")
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert note["mail"]["body"] == "Edited text."

    async def test_the_room_stream_carries_the_same_fields(self, client):  # noqa: F811
        await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            ident, _private_room, note_id, thread = _noted_turn(conn)
        (row,) = [e for e in _mod()._room_events_batch("alice", 0)["events"]
                  if e["msg_id"] == note_id]
        assert row["task_id"] == ident and row["mail"]["state"] == "held"
        with db.get_db(_mod()._config.db_path) as conn:
            db.settle_draft_mail(conn, thread, 7, state="sent")
        (row,) = [e for e in _mod()._room_events_batch("alice", 0)["events"]
                  if e["msg_id"] == note_id]
        assert row["mail"]["state"] == "sent"

    async def test_a_turn_that_mailed_nothing_has_a_task_and_no_card(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            thread = db.create_web_chat_room(conn, "alice", "Dinner plans").token
            private = _private(conn)
            ident = db.create_task(conn, user_id="alice", source_type="email", prompt="x",
                                   conversation_token=thread)
            db.add_message(conn, private.token, role="system", body="No reply sent.",
                           origin_surface="web", about_room_token=thread,
                           delivery_reference=_note_ref(ident))
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert note["task_id"] == ident and "mail" not in note

    async def test_a_malformed_reference_has_neither(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            thread = db.create_web_chat_room(conn, "alice", "Dinner plans").token
            private = _private(conn)
            db.add_message(conn, private.token, role="system", body="Note",
                           origin_surface="web", about_room_token=thread,
                           delivery_reference="private-pass_on:abc:pass-on")
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert "task_id" not in note and "mail" not in note

    async def test_another_users_note_reference_names_nothing(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            thread = db.create_web_chat_room(conn, "bob", "Bob's thread").token
            private = _private(conn)
            ident = db.create_task(conn, user_id="bob", source_type="email", prompt="x",
                                   conversation_token=thread)
            db.add_message(conn, private.token, role="system", body="Note",
                           origin_surface="web", delivery_reference=_note_ref(ident))
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert "task_id" not in note and "mail" not in note


PARTS = {"header": "ana@example.com wrote on Dinner plans, without you on the message",
         "outcome": "Replied.", "remark": "Your calendar is free on Friday."}
RECEIVED = {"from": {"name": "", "address": "ana@example.com"},
            "to": [{"name": "", "address": "bot@example.com"}], "cc": [],
            "date": "Sun, 04 Oct 2026 10:00:00 +0000", "subject": "Dinner",
            "attachments": [], "sender_check": "verified", "trusted": True}


def _with_parts(conn, ident, thread, note, *, parts=PARTS):
    """Give ``note`` its stored parts, and the thread its incoming mail."""
    user_row = db.add_message(conn, thread, role="user",
                              body="Friday?\n\nSent from my phone", origin_surface="email",
                              task_id=ident, author_label="ana@example.com")
    db.set_received_mail(conn, user_row, RECEIVED)
    if parts is not None:
        db.set_email_note(conn, note, parts)


@_needs_web_deps
class TestTheNoteParts:
    """ISSUE-644: the web renders an email note from its stored parts, with
    the incoming mail as a card rather than a quote."""

    async def test_history_carries_the_parts_and_the_incoming_mail(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            ident, private, note_id, thread = _noted_turn(conn, state="sent")
            _with_parts(conn, ident, thread, note_id)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert note["email_note"] == PARTS
        received = note["received_mail"]
        assert received["from"]["address"] == "ana@example.com"
        assert received["subject"] == "Dinner"
        assert received["new_text"].startswith("Friday?")
        # Already in the note's room: no link back to it.
        assert "note_path" not in received and "discuss" not in received

    async def test_the_room_stream_carries_the_same_fields(self, client):  # noqa: F811
        await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            ident, _private_room, note_id, thread = _noted_turn(conn, state="sent")
            _with_parts(conn, ident, thread, note_id)
        (row,) = [e for e in _mod()._room_events_batch("alice", 0)["events"]
                  if e["msg_id"] == note_id]
        assert row["email_note"] == PARTS
        assert row["received_mail"]["subject"] == "Dinner"

    async def test_a_note_from_before_the_parts_renders_as_it_did(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            ident, private, note_id, thread = _noted_turn(conn, state="sent")
            _with_parts(conn, ident, thread, note_id, parts=None)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert "email_note" not in note and "received_mail" not in note
        assert note["mail"]["state"] == "sent"

    async def test_a_parked_note_carries_its_parts_and_mail(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            thread = db.create_web_chat_room(conn, "alice", "Dinner plans").token
            private = _private(conn)
            ident = db.create_task(conn, user_id="alice", source_type="email", prompt="m",
                                   conversation_token=thread)
            note_id = db.add_message(
                conn, private.token, role="system", body="note", origin_surface="web",
                about_room_token=thread, delivery_reference=f"private-confirmation:{ident}:ab",
            )
            parts = dict(PARTS, outcome="Question for you.")
            _with_parts(conn, ident, thread, note_id, parts=parts)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert note["email_note"] == parts
        assert note["received_mail"]["subject"] == "Dinner"

    async def test_parts_whose_mail_is_gone_render_the_body(self, client):  # noqa: F811
        # The parts leave out the quote, so without the mail they would lose it.
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            _ident, private, note_id, _thread = _noted_turn(conn, state="sent")
            db.set_email_note(conn, note_id, PARTS)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert "email_note" not in note and "received_mail" not in note
        assert "> Friday?" in note["text"]

    async def test_another_users_parked_note_carries_nothing(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            thread = db.create_web_chat_room(conn, "bob", "Bob's thread").token
            private = _private(conn)
            ident = db.create_task(conn, user_id="bob", source_type="email", prompt="m",
                                   conversation_token=thread)
            note_id = db.add_message(
                conn, private.token, role="system", body="note", origin_surface="web",
                about_room_token=thread, delivery_reference=f"private-confirmation:{ident}:ab",
            )
            _with_parts(conn, ident, thread, note_id)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert "email_note" not in note and "received_mail" not in note

    async def test_parts_on_a_row_that_is_not_a_note_are_ignored(self, client):  # noqa: F811
        cookies = await _login(client, "alice")
        with db.get_db(_mod()._config.db_path) as conn:
            thread = db.create_web_chat_room(conn, "alice", "Dinner plans").token
            private = _private(conn)
            note_id = db.add_message(conn, private.token, role="system", body="whisper",
                                     origin_surface="web", about_room_token=thread,
                                     delivery_reference="private-whisper:5:x")
            db.set_email_note(conn, note_id, PARTS)
        url = f"/istota/api/chat/rooms/{private.id}/messages"
        (note,) = _system_rows((await client.get(url, cookies=cookies)).json())
        assert "email_note" not in note
