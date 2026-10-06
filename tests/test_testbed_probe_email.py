"""The probe's email helpers, against a local database the real producers wrote.

`testbed/probe.py` imports nothing from `istota`, so the rules it restates (the
private email room's ref, `threads.find_thread_room`'s order, the canonical
token) can drift from the product without anything failing. These build rows
with the product's own functions and require the probe to read them back the
way the product does.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from istota import db
from istota.notifications.resolvers import task_alert
from istota.transport.email import threads
from istota.transport.email.private_room import email_conversation_token
from testbed import probe as probe_support
from testbed.probe import Probe

USER = "testuser"


@pytest.fixture
def probe(db_path) -> Probe:
    return Probe(local=db_path)


def _mail(message_id: str) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, references=None, in_reply_to=None)


def _thread(conn, config, root: str) -> str:
    room = threads._mint(conn, config, owner_user_id=USER, root=root,
                         subject="a thread", people=[("pal@ext.test", "Pal")])
    assert room is not None
    return room.token


def _private(conn) -> str:
    room = db.register_bound_room(
        conn, USER, origin="email", name="Email", surface="email",
        surface_ref=email_conversation_token(USER),
    )
    assert room is not None
    return room.token


class TestThePrivateRefIsTheProducts:
    @pytest.mark.parametrize("user_id", ["testuser", "alice", "a.b@c", ""])
    def test_the_restated_digest_equals_email_conversation_token(self, user_id):
        assert probe_support.private_email_ref(user_id) == email_conversation_token(user_id)


class TestTheNoteStringsAreTheProducts:
    """A "no note" claim reads these two strings and finds nothing either way,
    so a rename in the product would leave it passing without testing
    anything. Held equal here instead."""

    def test_the_note_reference_is_the_one_deliver_email_note_writes(self):
        from istota.rooms import private_replies

        assert probe_support.email_note_reference(42) == (
            f"{private_replies.NOTE_PREFIX}42{private_replies._NOTE_SUFFIX}"
        )

    def test_the_note_log_line_is_the_one_the_scheduler_writes(self):
        from istota import scheduler

        from .support.drift import source_of
        from .support.email_flow import NOTE_LOG_LINE

        assert f'"{NOTE_LOG_LINE}{{outcome}}"' in source_of(scheduler._write_email_note)


class TestEmailRoomAgreesWithFindThreadRoom:
    """Three shapes, each asked of both: the probe answers what the product does."""

    def test_a_thread_room_by_its_bound_root(self, db_path, probe, make_config):
        config = make_config(db_path=db_path)
        with db.get_db(db_path) as conn:
            token = _thread(conn, config, "<root@ext.test>")
            found = threads.find_thread_room(conn, config, _mail("<root@ext.test>"))

        assert found is not None and found.token == token
        assert probe.email_room("<root@ext.test>") == token
        assert probe.email_room("root@ext.test") == token

    def test_a_private_email_room_is_no_thread(self, db_path, probe, make_config):
        """Mail filed in the private room is refused as a thread by both, and
        `private_email_room` names it."""
        config = make_config(db_path=db_path)
        with db.get_db(db_path) as conn:
            token = _private(conn)
            db.mark_email_processed(conn, email_id="1", sender_email="me@ext.test",
                                    thread_id=token, message_id="<own@ext.test>",
                                    user_id=USER)
            found = threads.find_thread_room(conn, config, _mail("<own@ext.test>"))

        assert found is None
        assert probe.email_room("<own@ext.test>") is None
        assert probe.private_email_room(USER) == token
        assert probe.private_email_room("nobody") is None

    def test_a_mail_filed_under_an_aliased_token(self, db_path, probe, make_config):
        """`processed_emails.thread_id` naming a permanent alias resolves to
        the room the alias points at, through the stored-mail arm."""
        config = make_config(db_path=db_path)
        with db.get_db(db_path) as conn:
            token = _thread(conn, config, "<root2@ext.test>")
            conn.execute(
                "INSERT INTO room_token_migration (old_token, new_token, migrated_at) "
                "VALUES (?, ?, datetime('now'))",
                ("email-thread-legacy", token),
            )
            db.mark_email_processed(conn, email_id="2", sender_email="pal@ext.test",
                                    thread_id="email-thread-legacy",
                                    message_id="<reply@ext.test>", user_id=USER)
            found = threads.find_thread_room(conn, config, _mail("<reply@ext.test>"))

        assert found is not None and found.token == token
        assert probe.email_room("<reply@ext.test>") == token

    def test_an_unknown_mail_is_in_no_room(self, db_path, probe, make_config):
        config = make_config(db_path=db_path)
        with db.get_db(db_path) as conn:
            found = threads.find_thread_room(conn, config, _mail("<none@ext.test>"))

        assert found is None
        assert probe.email_room("<none@ext.test>") is None


class TestTheRowReaders:
    def test_processed_decodes_mail_meta_and_takes_either_bracket_form(self, db_path, probe):
        with db.get_db(db_path) as conn:
            db.mark_email_processed(
                conn, email_id="9", sender_email="pal@ext.test",
                message_id="<m9@ext.test>", user_id=USER, routing_method="plus_address",
                mail_meta={"sender_check": "verified", "to": []},
            )

        row = probe.processed("m9@ext.test")
        assert row["routing_method"] == "plus_address"
        assert row["mail_meta"]["sender_check"] == "verified"
        assert probe.processed("<other@ext.test>") is None

    def test_participants_lists_present_and_departed(self, db_path, probe, make_config):
        config = make_config(db_path=db_path)
        with db.get_db(db_path) as conn:
            token = _thread(conn, config, "<p@ext.test>")
            conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                         "WHERE room_token = ?", (token,))
            db.upsert_room_participant(conn, room_token=token, surface="email",
                                       surface_ref="new@ext.test", kind="guest")

        rows = probe.participants(token)
        assert [(r["surface_ref"], r["left_at"] is None) for r in rows] == [
            ("pal@ext.test", False), ("new@ext.test", True),
        ]

    def test_room_messages_decodes_the_cards_and_honours_id_above(self, db_path, probe):
        with db.get_db(db_path) as conn:
            token = _private(conn)
            first = db.add_message(conn, token, role="user", body="in",
                                   origin_surface="email")
            db.set_received_mail(conn, first, {"to": [], "sender_check": "none"})
            second = db.add_message(conn, token, role="assistant", body="out",
                                    origin_surface="email")
            db.set_outgoing_mail(conn, second, {"state": "sent"})

        rows = probe.room_messages(token)
        assert rows[0]["received_mail"]["sender_check"] == "none"
        assert rows[1]["outgoing_mail"]["state"] == "sent"
        assert [r["id"] for r in probe.room_messages(token, id_above=first)] == [second]

    def test_email_note_finds_the_row_by_its_reference(self, db_path, probe):
        with db.get_db(db_path) as conn:
            token = _private(conn)
            note = db.add_message(conn, token, role="system", body="the note",
                                  origin_surface="web",
                                  delivery_reference="private-pass_on:42:pass-on")
            db.set_email_note(conn, note, {"header": "h", "outcome": "Replied.",
                                           "remark": ""})

        found = probe.email_note(42)
        assert found["body"] == "the note"
        assert found["email_note"]["outcome"] == "Replied."
        assert probe.email_note(43) is None

    def test_notifications_filter_by_source_key_and_watermark(self, db_path, probe):
        with db.get_db(db_path) as conn:
            task_alert.write(conn, USER, dedup_key="private-note:7", title="t",
                             body="b", params={"task_id": 7})
            task_alert.write(conn, USER, dedup_key="private-note:8", title="t", body="b")
        mark = probe.watermark()
        with db.get_db(db_path) as conn:
            task_alert.write(conn, USER, dedup_key="private-note:9", title="t", body="b")

        keyed = probe.notifications(USER, dedup_key="private-note:7")
        assert [(n["source"], n["params"]["task_id"]) for n in keyed] == [("task_alert", 7)]
        assert [n["dedup_key"] for n in probe.notifications(
            USER, source="task_alert", id_above=mark["notifications"])] == ["private-note:9"]
        assert probe.notifications("nobody") == []

    def test_drafts_decode_the_address_lists(self, db_path, probe):
        with db.get_db(db_path) as conn:
            conn.execute(
                "INSERT INTO outbound_drafts (user_id, to_addrs, cc_addrs) VALUES (?, ?, ?)",
                (USER, json.dumps(["a@ext.test"]), json.dumps(["b@ext.test"])),
            )

        (draft,) = probe.drafts(USER)
        assert draft["to_addrs"] == ["a@ext.test"]
        assert draft["cc_addrs"] == ["b@ext.test"]
        assert probe.drafts(USER, id_above=draft["id"]) == []

    def test_task_log_matches_literally(self, db_path, probe):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id=USER)
            db.log_task(conn, task_id, "info", "Private note to the host: sent")
            db.log_task(conn, task_id, "info", "100% done")

        assert [r["message"] for r in probe.task_log(
            task_id, contains="Private note to the host: ")] == [
                "Private note to the host: sent"]
        assert probe.task_log(task_id, contains="1_0%") == []


class TestTheWatermarkCoversTheNewTables:
    def test_each_new_table_has_a_mark(self, probe):
        mark = probe.watermark()
        for table in ("notifications", "outbound_drafts", "room_participants"):
            assert mark[table] == 0

    def test_rows_above_accepts_them(self, db_path, probe):
        mark = probe.watermark()
        with db.get_db(db_path) as conn:
            task_alert.write(conn, USER, dedup_key="k", title="t")

        assert len(probe.rows_above("notifications", mark, user_id=USER)) == 1
