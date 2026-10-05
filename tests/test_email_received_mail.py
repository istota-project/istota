"""Hidden email threads, stage 3: the thread is a mail view.

Incoming mail is stored with its metadata at intake (`messages.received_mail`,
and `processed_emails.mail_meta` for the held path), built by
`mail_card.received_mail_meta` and read back by `mail_card.stored_received_mail`.
The web reads it through `_user_row_display` as `received_mail`, the
incoming-mail card's data.

Driven through `poll_emails` and the history endpoint, as
`test_email_thread_rooms.py` does.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import confirmations, db
from istota.rooms import private_replies
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email import email_conversation_token, threads
from istota.transport.email.inbound import poll_emails
from istota.transport.email.mail_card import (
    MAIL_META_LIST_CAP, MAIL_META_STRING_CAP, SENDER_CHECKS, received_mail_meta,
    stored_received_mail,
)

from . import test_email_thread_rooms as _base
from .test_email_thread_rooms import ALICE, BOB, BOT, HOST, HOST_ADDR, ROOT, _rows

config = _base.config
db_path = _base.db_path

_UID = [5000]
AUTHSERV = "mx.test.com"


def _poll(config, *, sender, to=(BOT,), cc=(), message_id, references=None,
          subject="Dinner plans", body="hello", manifest=(), downloaded=(),
          uploaded=None, auth=(), display_names=None):
    """Poll one message; return the created task ids."""
    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False,
    )
    email = Email(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000",
        body=body, attachments=[m["filename"] for m in manifest],
        message_id=message_id, references=references, to=tuple(to), cc=tuple(cc),
        attachment_manifest=list(manifest),
        authentication_results=auth[0] if auth else None,
        authentication_results_all=tuple(auth),
        display_names=dict(display_names or {}),
    )
    uploads = dict(uploaded or {})
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments",
              return_value=list(downloaded)),
        patch("istota.transport.email.inbound.ensure_user_directories_v2"),
        patch("istota.transport.email.inbound.upload_file_to_inbox_v2",
              side_effect=lambda cfg, user, local, remote: uploads.get(local.name)),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        return poll_emails(config)


def _thread_token(config):
    with db.get_db(config.db_path) as conn:
        return db.resolve_room_token(conn, "email", ROOT)


def _received(db_path, room_token):
    rows = _rows(db_path, "SELECT received_mail FROM messages WHERE room_token=? "
                 "AND role='user' ORDER BY id", (room_token,))
    return [json.loads(r["received_mail"]) if r["received_mail"] else None for r in rows]


def _start(config, **kw):
    """The host mails the bot and two friends: a thread room."""
    return _poll(config, sender=HOST_ADDR, to=(BOT, ALICE), cc=(BOB,),
                 message_id=ROOT, **kw)


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


class TestIntakeStoresTheMetadata:
    def test_a_thread_turn_stores_to_and_cc_as_sent(self, config, db_path):
        _start(config, display_names={ALICE: "Alice Ash"})
        _poll(config, sender=ALICE, to=(BOT, "Carol <carol@test.com>"), cc=(BOB,),
              message_id="<a2@ext.example>", references=ROOT, subject="Re: Dinner plans",
              body="Zorg, Thursday?", display_names={ALICE: "Alice Ash"})

        first, second = _received(db_path, _thread_token(config))
        assert first["from"] == {"name": "", "address": HOST_ADDR}
        assert second["from"] == {"name": "Alice Ash", "address": ALICE}
        assert second["to"] == [{"name": "", "address": BOT},
                                {"name": "Carol", "address": "carol@test.com"}]
        assert second["cc"] == [{"name": "", "address": BOB}]
        assert second["subject"] == "Re: Dinner plans"
        assert second["message_id"] == "<a2@ext.example>"
        assert second["date"] == "Mon, 01 Jan 2026 12:00:00 +0000"
        assert "bcc" not in second
        assert second["attachments"] == []

    def test_a_private_email_room_turn_stores_it(self, config, db_path):
        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>",
              subject="Note to self")
        with db.get_db(db_path) as conn:
            private = db.resolve_room_token(conn, "email", email_conversation_token(HOST))
        (meta,) = _received(db_path, private)
        assert meta["from"]["address"] == HOST_ADDR
        assert meta["subject"] == "Note to self"

    def test_the_attachment_manifest_carries_the_inbox_path_when_written(
        self, config, db_path, tmp_path,
    ):
        local = tmp_path / "report.pdf"
        local.write_bytes(b"%PDF-1.4")
        _start(config, manifest=[
            {"filename": "report.pdf", "size": 1234, "content_type": "application/pdf"},
            {"filename": "huge.zip", "size": 9_000_000, "content_type": "application/zip"},
        ], downloaded=[local], uploaded={"report.pdf": "/Users/carol/inbox/ab12_report.pdf"})

        (meta,) = _received(db_path, _thread_token(config))
        assert meta["attachments"] == [
            {"filename": "report.pdf", "size": 1234, "content_type": "application/pdf",
             "path": "/Users/carol/inbox/ab12_report.pdf"},
            {"filename": "huge.zip", "size": 9_000_000, "content_type": "application/zip"},
        ]

    def test_trusted_marks_a_sender_on_the_list(self, config, db_path):
        _start(config)
        _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,), message_id="<a2@ext.example>",
              references=ROOT, body="Zorg, Thursday?")
        host, alice = _received(db_path, _thread_token(config))
        # The host's own address is no trust grant: `trusted` is the list.
        assert host["trusted"] is False
        assert alice["trusted"] is True


class TestTheSenderCheck:
    """`sender_check` is the verdict the gate computes, read through the same
    helper: verified only for an aligned pass under our own authserv-id."""

    def _alice(self, config, auth):
        _start(config)
        _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,), message_id="<a2@ext.example>",
              references=ROOT, body="Zorg, Thursday?", auth=auth)
        return _received(config.db_path, _thread_token(config))[-1]["sender_check"]

    def test_an_aligned_pass_under_our_authserv_id_is_verified(self, config):
        config.email.authserv_id = AUTHSERV
        assert self._alice(config, (f"{AUTHSERV}; dmarc=pass header.from=ext.example",)) \
            == "verified"

    def test_a_fail_is_failed(self, config):
        config.email.authserv_id = AUTHSERV
        assert self._alice(config, (f"{AUTHSERV}; dmarc=fail header.from=ext.example",)) \
            == "failed"

    def test_no_header_is_none(self, config):
        config.email.authserv_id = AUTHSERV
        assert self._alice(config, ()) == "none"

    def test_a_pass_read_off_an_unscoped_header_is_not_verified(self, config):
        """Without `authserv_id` the verdict is read off the topmost header,
        which the sender can write, so a pass there proves nothing."""
        assert self._alice(config, ("mx.any; dmarc=pass header.from=ext.example",)) \
            == "none"

    def test_the_hosts_own_verdict_is_the_gates(self, config, db_path):
        config.email.authserv_id = AUTHSERV
        _start(config, auth=(f"{AUTHSERV}; dmarc=pass header.from=test.com",))
        (meta,) = _received(db_path, _thread_token(config))
        assert meta["sender_check"] == "verified"


class TestHeldThenApproved:
    def test_the_ledger_carries_it_and_approval_copies_it_onto_the_row(
        self, config, db_path,
    ):
        config.users[HOST].trusted_email_senders = []
        _start(config)
        outsider = "dave@else.example"
        (held,) = _poll(config, sender=outsider, to=(BOT,), cc=(HOST_ADDR,),
                        message_id="<d1@else.example>", references=ROOT,
                        subject="Re: Dinner plans", body="Can I come?",
                        display_names={outsider: "Dave"})

        (ledger,) = _rows(db_path, "SELECT mail_meta FROM processed_emails WHERE task_id=?",
                          (held,))
        stored = json.loads(ledger["mail_meta"])
        assert stored["from"] == {"name": "Dave", "address": outsider}
        assert stored["cc"] == [{"name": "", "address": HOST_ADDR}]
        assert stored["trusted"] is False
        # Not in the room until approved.
        assert len(_received(db_path, _thread_token(config))) == 1

        with db.get_db(db_path) as conn:
            confirmations.approve(conn, db.get_task(conn, held), config=config)
        assert _received(db_path, _thread_token(config))[-1] == stored

    def test_a_held_private_mail_is_copied_too(self, config, db_path):
        config.email.confirm_sender_match = "gate"
        (held,) = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p9@test.com>",
                        subject="Self")
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, held)
            assert task.status == "pending_confirmation"
            confirmations.approve(conn, task, config=config)
            private = db.resolve_room_token(conn, "email", email_conversation_token(HOST))
        (meta,) = _received(db_path, private)
        assert meta["subject"] == "Self"


class TestTheCaps:
    def test_lists_and_strings_are_bounded(self):
        long_local = "x" * 400
        email = Email(
            id="1", subject="s" * 500, sender=f"{long_local}@ext.example",
            date="d", body="", attachments=[], message_id="<m@x>",
            to=(BOT,), cc=tuple(f"p{i}@ext.example" for i in range(60)),
        )
        meta = received_mail_meta(email, sender_check="none", trusted=False)
        assert len(meta["cc"]) == 50
        assert len(meta["from"]["address"]) == 320
        assert len(meta["subject"]) == 320

    def test_an_attachment_list_is_bounded(self):
        email = Email(
            id="1", subject="s", sender=ALICE, date="d", body="", attachments=[],
            attachment_manifest=[{"filename": f"f{i}.txt", "size": 1,
                                  "content_type": "text/plain"} for i in range(60)],
        )
        meta = received_mail_meta(email, sender_check="none", trusted=False)
        assert len(meta["attachments"]) == 50


class TestTheStoredSchema:
    """One schema, both sides of the column (ISSUE-642)."""

    def _meta(self):
        email = Email(
            id="1", subject="Dinner", sender="Alice Ash <alice@ext.example>",
            date="Mon, 1 Sep 2026 10:00:00 +0000", body="", attachments=[],
            message_id="<m@x>", in_reply_to="<r@x>", to=(BOT,), cc=(BOB,),
            attachment_manifest=[{"filename": "a.pdf", "size": 3,
                                  "content_type": "application/pdf"}],
        )
        return received_mail_meta(email, sender_check="verified", trusted=True,
                                  stored_paths={"a.pdf": "/Users/host/inbox/a.pdf"})

    def test_what_the_builder_writes_reads_back_unchanged(self):
        meta = self._meta()
        assert meta["attachments"][0]["content_type"] == "application/pdf"
        assert stored_received_mail(json.dumps(meta)) == meta

    def test_a_string_over_the_cap_is_cut_on_read(self):
        meta = self._meta()
        meta["subject"] = "s" * (MAIL_META_STRING_CAP + 10)
        meta["from"]["address"] = "a" * (MAIL_META_STRING_CAP + 10)
        stored = stored_received_mail(json.dumps(meta))
        assert len(stored["subject"]) == MAIL_META_STRING_CAP
        assert len(stored["from"]["address"]) == MAIL_META_STRING_CAP

    def test_lists_over_the_cap_are_cut_on_read(self):
        meta = self._meta()
        meta["cc"] = [{"name": "", "address": f"p{i}@x"} for i in range(60)]
        meta["attachments"] = [{"filename": f"f{i}"} for i in range(60)]
        stored = stored_received_mail(json.dumps(meta))
        assert len(stored["cc"]) == MAIL_META_LIST_CAP
        assert len(stored["attachments"]) == MAIL_META_LIST_CAP

    def test_an_unknown_sender_check_reads_as_none(self):
        meta = self._meta()
        meta["sender_check"] = "trust-me"
        assert stored_received_mail(json.dumps(meta))["sender_check"] == "none"
        assert set(SENDER_CHECKS) == {"verified", "failed", "none"}

    def test_malformed_entries_are_dropped(self):
        meta = self._meta()
        meta["to"] = [{"name": "x"}, "bob@x", {"address": 5}, {"address": "c@x"}]
        meta["attachments"] = [{"filename": ""}, {"size": 1}, {"filename": "ok",
                               "size": "big", "path": 7}]
        meta["trusted"] = "yes"
        stored = stored_received_mail(json.dumps(meta))
        assert stored["to"] == [{"name": "", "address": "c@x"}]
        assert stored["attachments"] == [{"filename": "ok"}]
        assert stored["trusted"] is False

    @pytest.mark.parametrize("raw", [None, "", "not json", "[1]", "3", b"\xff"])
    def test_anything_but_a_json_object_is_none(self, raw):
        assert stored_received_mail(raw) is None


class TestSplitNewText:
    def test_the_quoted_history_and_the_signature_are_the_rest(self):
        body = "Thursday works.\n-- \nAlice\n\nOn Mon, Carol wrote:\n> Dinner?"
        assert threads.split_new_text(body) == (
            "Thursday works.", "-- \nAlice\n\nOn Mon, Carol wrote:\n> Dinner?",
        )

    def test_an_inline_reply_keeps_the_lines_it_answers(self):
        """`new_text` drops them for intake; the card must not, or it shows
        answers to nothing."""
        body = "> Can you come?\nYes.\n> Bring wine?\nSure."
        assert threads.new_text(body) == "Yes.\nSure."
        assert threads.split_new_text(body) == (body, "")


# ---------------------------------------------------------------------------
# The columns
# ---------------------------------------------------------------------------


def _schema_db(path):
    schema = (Path(db.__file__).parent.parent.parent / "schema.sql").read_text()
    raw = sqlite3.connect(path)
    raw.executescript(schema)
    raw.close()


def _column(path, table, name):
    with db.get_db(path) as conn:
        return [tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({table})")
                if r[1] == name]


class TestTheColumns:
    @pytest.mark.parametrize("table,name", [
        ("messages", "received_mail"),
        ("processed_emails", "mail_meta"),
    ])
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path, table, name):
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
        raw.commit()
        db._run_migrations(raw)
        raw.commit()
        raw.close()
        declared = tmp_path / "declared.db"
        _schema_db(declared)

        assert _column(declared, table, name) == _column(old, table, name) \
            == _column(fresh, table, name) == [(name, "TEXT", 0, None, 0)]


# ---------------------------------------------------------------------------
# The web read
# ---------------------------------------------------------------------------


def _page(config, token, viewer=HOST):
    pytest.importorskip("fastapi")
    from istota.webui import app as web_app

    prev = web_app._config
    web_app._config = config
    try:
        return web_app._chat_room_messages(viewer, token, 50)
    finally:
        web_app._config = prev


QUOTED = "Thursday works.\n\nOn Mon, Carol wrote:\n> Dinner?\n> Soon"


class TestTheWebRead:
    def test_a_thread_row_carries_received_mail_with_new_text_and_rest(
        self, config, db_path,
    ):
        _start(config)
        _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
              message_id="<a2@ext.example>", references=ROOT, subject="Re: Dinner plans",
              body=QUOTED, display_names={ALICE: "Alice Ash"})

        rows = [m for m in _page(config, _thread_token(config))["messages"]
                if m["role"] == "user"]
        mail = rows[-1]["received_mail"]
        assert mail["from"] == {"name": "Alice Ash", "address": ALICE}
        assert mail["new_text"] == "Thursday works."
        assert mail["rest"].startswith("On Mon, Carol wrote:")
        assert "> Soon" in mail["rest"]
        assert mail["sender_check"] == "none" and mail["trusted"] is True
        # The viewer's own address and the bot's are named for the card.
        assert mail["labels"] == {HOST_ADDR: "you", BOT: "Zorg"}
        assert "fallback" not in mail

    def test_an_attachment_chip_keeps_only_a_path_in_the_viewers_workspace(
        self, config, db_path,
    ):
        _start(config)
        token = _thread_token(config)
        (stored,) = _received(db_path, token)
        stored["attachments"] = [
            {"filename": "a.pdf", "size": 3, "content_type": "application/pdf",
             "path": f"/Users/{HOST}/inbox/a.pdf"},
            {"filename": "b.pdf", "path": "/Users/someoneelse/inbox/b.pdf"},
            {"filename": "c.pdf", "path": f"/Users/{HOST}/../x/c.pdf"},
        ]
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE messages SET received_mail = ? WHERE room_token = ? "
                         "AND role = 'user'", (json.dumps(stored), token))
        (row,) = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        assert row["received_mail"]["attachments"] == [
            {"filename": "a.pdf", "size": 3, "path": f"/Users/{HOST}/inbox/a.pdf"},
            {"filename": "b.pdf"},
            {"filename": "c.pdf"},
        ]

    def test_a_pre_change_row_gets_the_wrapper_only_fallback(self, config, db_path):
        _start(config)
        token = _thread_token(config)
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE messages SET received_mail = NULL WHERE room_token = ?",
                         (token,))
        (row,) = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        mail = row["received_mail"]
        assert mail["fallback"] is True
        assert mail["from"] == {"name": "", "address": HOST_ADDR}
        assert mail["subject"] == "Dinner plans"
        assert mail["date"] == "Mon, 01 Jan 2026 12:00:00 +0000"
        assert mail["to"] == [] and mail["cc"] == [] and mail["attachments"] == []
        assert "sender_check" not in mail and "message_id" not in mail

    def test_a_user_row_in_any_other_room_has_none(self, config, db_path):
        """Only a mail room's rows are mail: an email-wrapped row anywhere else
        keeps today's rendering."""
        with db.get_db(db_path) as conn:
            token = db.create_web_chat_room(conn, HOST, "Plain").token
            db.add_message(conn, token, role="user", origin_surface="email",
                           author_label=ALICE,
                           body="<email_metadata>\nFrom: x@y\nSubject: s\nDate: d\n\n"
                                "</email_metadata>\n\n<email_content>\nhi\n"
                                "</email_content>\n\nThe text within")
        rows = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        assert rows and all("received_mail" not in m for m in rows)

    def test_the_cards_link_to_the_viewers_private_room_with_the_task(
        self, config, db_path,
    ):
        """"Discuss in private chat" and a held card's link open the viewer's
        private room with the turn's task, until stage 4 writes the note."""
        with db.get_db(db_path) as conn:
            private = db.create_web_chat_room(conn, HOST, "General").token
        _start(config)
        _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,), message_id="<a2@ext.example>",
              references=ROOT, body="Zorg, Thursday?")

        rows = [m for m in _page(config, _thread_token(config))["messages"]
                if m["role"] == "user"]
        task_id = rows[-1]["task_id"]
        assert rows[-1]["received_mail"]["note_path"] == f"/chat/r/{private}/t/{task_id}"

    def test_the_cards_link_to_the_room_holding_the_note(self, config, db_path):
        """Once the note is written (stage 4) the link follows it, wherever it
        landed, rather than the viewer's current private room."""
        with db.get_db(db_path) as conn:
            db.create_web_chat_room(conn, HOST, "General")
            noted = db.create_web_chat_room(conn, HOST, "Earlier").token
        _start(config)
        _poll(config, sender=ALICE, to=(BOT,), cc=(BOB,), message_id="<a2@ext.example>",
              references=ROOT, body="Zorg, Thursday?")
        token = _thread_token(config)
        task_id = [m for m in _page(config, token)["messages"] if m["role"] == "user"][-1]["task_id"]
        with db.get_db(db_path) as conn:
            db.add_message(conn, noted, role="system", body="note", origin_surface="web",
                           about_room_token=token,
                           delivery_reference=f"private-pass_on:{task_id}:pass-on")
            current = private_replies.private_room_for(conn, config, HOST, token)
        assert current.room_token != noted

        rows = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        assert rows[-1]["received_mail"]["note_path"] == f"/chat/r/{noted}/t/{task_id}"

    def test_with_no_private_room_there_is_no_link(self, config, db_path):
        _start(config)
        (row,) = [m for m in _page(config, _thread_token(config))["messages"]
                  if m["role"] == "user"]
        assert "note_path" not in row["received_mail"]


class TestTheDiscussTarget:
    """Section 0c: with no note, "Discuss in private chat" opens the viewer's
    private room with the composer linked to the thread, so the card names
    both rooms. A note outranks it; a non-thread mail room never has one."""

    def test_a_thread_card_with_no_note_names_the_private_room_and_the_thread(
        self, config, db_path,
    ):
        with db.get_db(db_path) as conn:
            private = db.create_web_chat_room(conn, HOST, "General").token
        _start(config)
        token = _thread_token(config)
        (row,) = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        assert row["received_mail"]["discuss"] == {"room": private, "about": token}

    def test_a_note_outranks_it(self, config, db_path):
        with db.get_db(db_path) as conn:
            private = db.create_web_chat_room(conn, HOST, "General").token
        _start(config)
        token = _thread_token(config)
        (row,) = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        with db.get_db(db_path) as conn:
            db.add_message(conn, private, role="system", body="note", origin_surface="web",
                           about_room_token=token,
                           delivery_reference=f"private-pass_on:{row['task_id']}:pass-on")
        (row,) = [m for m in _page(config, token)["messages"] if m["role"] == "user"]
        assert "discuss" not in row["received_mail"]
        assert row["received_mail"]["note_path"] == f"/chat/r/{private}/t/{row['task_id']}"

    def test_with_no_private_room_there_is_none(self, config, db_path):
        _start(config)
        (row,) = [m for m in _page(config, _thread_token(config))["messages"]
                  if m["role"] == "user"]
        assert "discuss" not in row["received_mail"]

    def test_the_private_email_room_has_none(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.create_web_chat_room(conn, HOST, "General")
        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>",
              subject="Note to self")
        with db.get_db(db_path) as conn:
            private = db.resolve_room_token(conn, "email", email_conversation_token(HOST))
        (row,) = [m for m in _page(config, private)["messages"] if m["role"] == "user"]
        assert "discuss" not in row["received_mail"]
