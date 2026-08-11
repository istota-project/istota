"""`notifications.mirror_talk_to_room` — a Talk-delivered notification reaching
the web view of the room it landed in (ISSUE-242).

The *result*-side decision this file used to cover (`_talk_result_mirror_body`)
moved to `tests/test_email_routed_room.py` when ISSUE-247 narrowed it: an email
task's answer is now an ordinary assistant turn in the room its exchange was
routed to, so the mirror is left covering only the case it cannot reach — a task
delivered to a Talk room that is not its own.
"""

from istota import db
from istota.config import Config

import pytest


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def conn(config):
    with db.get_db(config.db_path) as c:
        yield c


class TestMirrorWrite:
    def test_writes_a_system_row_carrying_the_talk_id(self, config, conn):
        from istota.notifications import mirror_talk_to_room

        db.register_room(conn, "dmtoken1", "alice", origin="talk")
        conn.commit()
        mirror_talk_to_room(config, "dmtoken1", "the reply", talk_message_id=414)
        with db.get_db(config.db_path) as c:
            msgs = db.get_messages(c, "dmtoken1")
        assert [(m.role, m.body) for m in msgs] == [("system", "the reply")]
        # The stamp is what lets a cited reply walk back to the Talk post, and
        # what caps the Talk→web read-sync cursor.
        assert msgs[0].external_ids == {"talk": "414"}

    def test_unregistered_room_is_a_noop(self, config):
        from istota.notifications import mirror_talk_to_room

        mirror_talk_to_room(config, "ghosttoken", "x", talk_message_id=1)
        with db.get_db(config.db_path) as c:
            assert db.get_messages(c, "ghosttoken") == []
