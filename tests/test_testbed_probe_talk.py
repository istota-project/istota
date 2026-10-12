"""The probe's Talk lookups, against a local database the real ingest wrote.

A Talk task stores its room's minted id (`rm_…`) in `tasks.conversation_token`,
not the Talk token a scenario holds (ISSUE-706). The probe restates the Talk
poller's resolution, since `testbed/` imports nothing from `istota`; these build
rows through `ingest_message` and require the probe to find them by the Talk
token the way the product does.
"""

from __future__ import annotations

import pytest

from istota import db
from istota.config import Config
from istota.transport import IncomingMessage, ingest_message
from testbed.probe import Probe

USER = "testuser"


@pytest.fixture
def config(db_path) -> Config:
    cfg = Config()
    cfg.db_path = db_path
    return cfg


@pytest.fixture
def probe(db_path) -> Probe:
    return Probe(local=db_path)


def _talk_turn(config, db_path, token: str, message_id: int) -> int:
    msg = IncomingMessage(
        user_id=USER, text="what does the script say?", source_type="talk",
        surface="talk", channel_token=token, platform_message_id=message_id,
    )
    with db.get_db(db_path) as conn:
        task_id = ingest_message(conn, config, msg)
    assert task_id is not None
    return task_id


def _product_room(db_path, token: str) -> str:
    """What `transport/talk/inbound.py` resolves a Talk token to."""
    with db.get_db(db_path) as conn:
        return db.resolve_room_token(conn, "talk", token) or db._canonical_room_token(
            conn, token, cross_surface=False
        )


class TestATalkTaskIsFoundByItsTalkToken:
    def test_tasks_finds_the_task_the_room_id_was_stored_on(self, config, db_path, probe):
        task_id = _talk_turn(config, db_path, "talkroom1", 101)

        rows = probe.tasks(talk_token="talkroom1")

        assert [row["id"] for row in rows] == [task_id]
        assert db.is_canonical_room_token(rows[0]["conversation_token"])

    def test_another_rooms_task_is_not_returned(self, config, db_path, probe):
        mine = _talk_turn(config, db_path, "roomaaaa", 101)
        _talk_turn(config, db_path, "roombbbb", 102)

        assert [row["id"] for row in probe.tasks(talk_token="roomaaaa")] == [mine]

    def test_wait_for_task_takes_the_talk_token(self, config, db_path, probe):
        task_id = _talk_turn(config, db_path, "roomwait", 101)
        with db.get_db(db_path) as conn:
            db.update_task_status(conn, task_id, "completed", result="done")

        task = probe.wait_for_task(status="completed", talk_token="roomwait", timeout=5)

        assert task["id"] == task_id

    def test_a_token_nothing_has_bound_yet_matches_nothing(self, config, db_path, probe):
        _talk_turn(config, db_path, "roomaaaa", 101)

        assert probe.tasks(talk_token="notyetseen") == []


class TestTalkRoomIsThePollersResolution:
    def test_a_minted_room_resolves_to_its_rooms_row(self, config, db_path, probe):
        _talk_turn(config, db_path, "talkroom1", 101)

        room = probe.talk_room("talkroom1")

        assert room == _product_room(db_path, "talkroom1")
        assert room != "talkroom1"
        assert probe.query("SELECT origin FROM rooms WHERE token = ?", [room]) == [
            {"origin": "talk"}
        ]
        assert [r["role"] for r in probe.room_messages(room)] == ["user"]

    def test_a_room_registered_under_its_talk_token_resolves_to_itself(
        self, config, db_path, probe
    ):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "legacy42", USER, origin="talk", name="#x")
        task_id = _talk_turn(config, db_path, "legacy42", 101)

        assert probe.talk_room("legacy42") == _product_room(db_path, "legacy42")
        assert [row["id"] for row in probe.tasks(talk_token="legacy42")] == [task_id]

    def test_an_unknown_token_stays_as_given(self, probe, db_path):
        assert probe.talk_room("nobodyhere") == _product_room(db_path, "nobodyhere")
        assert probe.talk_room("nobodyhere") == "nobodyhere"

    def test_an_alias_resolves_to_its_live_room_and_a_tombstone_stays(
        self, config, db_path, probe
    ):
        with db.get_db(db_path) as conn:
            live = db.register_room(conn, None, USER, origin="web", name="live").token
            gone = db.register_room(conn, None, USER, origin="web", name="gone").token
            conn.execute(
                "INSERT INTO room_token_migration (old_token, new_token, migrated_at) "
                "VALUES ('oldlive', ?, 'now'), ('oldgone', ?, 'now')",
                [live, gone],
            )
            conn.execute("DELETE FROM rooms WHERE token = ?", [gone])

        assert probe.talk_room("oldlive") == _product_room(db_path, "oldlive") == live
        assert probe.talk_room("oldgone") == _product_room(db_path, "oldgone") == "oldgone"
