"""`briefing:` and `tasks-file` are no longer subjects, and an upgrade leaves nothing
of them behind: no grant, no refusal, and no bell row `store.counts` would go on
counting, since that read is plain SQL and only a panel open sweeps a stale row.
"""

from __future__ import annotations

import sqlite3

import pytest

from istota import db
from istota.config import Config
from istota.executor import file_egress_refusals
from istota.notifications import store
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress

USER = "alice"
RETIRED = ["tasks-file", "briefing:morning"]
#: Includes a job whose name looks like a retired subject.
KEPT = ["cron:digest", "cron:briefing:morning", "cron:tasks-file", "assignment:7"]
HOST = "canary-host.example.com"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _seed(conn, subject, user=USER):
    conn.execute(
        "INSERT INTO egress_grants (user_id, subject, entry, decided_via) "
        "VALUES (?, ?, 'news.example.com', 'web')",
        (user, subject),
    )
    conn.execute(
        "INSERT INTO egress_refused (user_id, subject, entry) VALUES (?, ?, ?)",
        (user, subject, HOST),
    )


def _briefing(conn):
    conn.execute(
        "INSERT INTO briefing_configs "
        "(user_id, name, cron_expression, conversation_token, output, components) "
        "VALUES (?, 'morning', '0 7 * * *', '', 'email', '{}')",
        (USER,),
    )


def _subjects(conn, table):
    return sorted(row[0] for row in conn.execute(f"SELECT subject FROM {table}"))


def _bell_states(conn):
    return {
        row["object_id"]: row["state"]
        for row in conn.execute(
            "SELECT object_id, state FROM notifications WHERE source = ?", (egress_refused.SOURCE,),
        )
    }


class TestTheMigration:
    def test_it_deletes_both_kinds_and_keeps_the_rest(self, conn):
        for subject in [*RETIRED, *KEPT]:
            _seed(conn, subject)
        db._migrate_egress_retire_subjects(conn)
        assert _subjects(conn, "egress_grants") == sorted(KEPT)
        assert _subjects(conn, "egress_refused") == sorted(KEPT)

    def test_it_covers_every_user(self, conn):
        _seed(conn, "tasks-file", user="bob")
        db._migrate_egress_retire_subjects(conn)
        assert _subjects(conn, "egress_grants") == []

    def test_it_runs_on_every_start(self, conn):
        for subject in [*RETIRED, *KEPT]:
            _seed(conn, subject)
        db._migrate_egress_retire_subjects(conn)
        db._migrate_egress_retire_subjects(conn)
        assert _subjects(conn, "egress_grants") == sorted(KEPT)

    @pytest.mark.parametrize("tables", [
        (),
        ("egress_grants",),  # what the seed leaves on an upgrade: schema.sql runs later
        ("egress_refused",),
    ])
    def test_it_tolerates_a_missing_table(self, tables):
        bare = sqlite3.connect(":memory:")
        for table in tables:
            bare.execute(f"CREATE TABLE {table} (user_id TEXT, subject TEXT, entry TEXT)")
            bare.execute(f"INSERT INTO {table} VALUES ('alice', 'tasks-file', 'x')")
        db._migrate_egress_retire_subjects(bare)
        for table in tables:
            assert bare.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    def test_it_runs_from_init_db_and_commits(self, db_path):
        with db.get_db(db_path) as conn:
            _seed(conn, "briefing:morning")
            egress_refused.write(conn, USER, "briefing:morning")
            conn.commit()
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            assert _subjects(conn, "egress_grants") == []
            assert store.counts(conn, USER) == {"open": 0, "actionable": 0}

    def test_it_logs_counts_and_no_name(self, conn, caplog):
        _seed(conn, "briefing:canary-name")
        _seed(conn, "tasks-file")
        egress_refused.write(conn, USER, "briefing:canary-name")
        with caplog.at_level("INFO", logger="istota.db"):
            db._migrate_egress_retire_subjects(conn)
        assert "egress_subjects_retired grants=2 refused=2 bell=1" in caplog.text
        assert "canary" not in caplog.text


class TestTheBellCount:
    def test_an_open_row_on_a_retired_subject_stops_counting(self, conn):
        for subject in RETIRED:
            egress_refused.write(conn, USER, subject)
        assert store.counts(conn, USER)["open"] == 2
        db._migrate_egress_retire_subjects(conn)
        assert store.counts(conn, USER) == {"open": 0, "actionable": 0}

    def test_it_closes_as_the_store_closes_a_row(self, conn):
        egress_refused.write(conn, USER, "tasks-file")
        db._migrate_egress_retire_subjects(conn)
        row = conn.execute(
            "SELECT state, resolved_at, resolved_by, updated_at FROM notifications",
        ).fetchone()
        assert (row["state"], row["resolved_by"]) == ("resolved", "system")
        assert row["resolved_at"] == row["updated_at"]
        assert row["resolved_at"]

    def test_a_row_on_a_kept_subject_stays_open(self, conn):
        for subject in KEPT:
            egress_refused.write(conn, USER, subject)
        db._migrate_egress_retire_subjects(conn)
        assert set(_bell_states(conn).values()) == {"open"}

    def test_another_sources_row_with_that_object_stays_open(self, conn):
        store.write_notification(
            conn, USER, source="task_alert", dedup_key="k", title="t", body="b",
            object_type=egress_refused.OBJECT_TYPE, object_id="tasks-file",
        )
        db._migrate_egress_retire_subjects(conn)
        assert store.counts(conn, USER)["open"] == 1

    def test_a_closed_row_keeps_its_own_close(self, conn):
        egress_refused.write(conn, USER, "tasks-file")
        store.resolve_by_object(
            conn, USER, egress_refused.SOURCE, egress_refused.OBJECT_TYPE, "tasks-file", by="web",
        )
        db._migrate_egress_retire_subjects(conn)
        row = conn.execute("SELECT resolved_by FROM notifications").fetchone()
        assert row["resolved_by"] == "web"


class TestNothingHangsOnThem:
    def _task(self, conn, source_type, **columns):
        task_id = db.create_task(
            conn, prompt="p", user_id=USER, source_type=source_type, **columns,
        )
        return db.get_task(conn, task_id)

    def test_neither_source_type_has_a_subject(self, conn):
        _briefing(conn)
        assert egress.subject_for(conn, self._task(conn, "briefing", briefing_name="morning")) is None
        assert egress.subject_for(conn, self._task(conn, "istota_file")) is None

    @pytest.mark.parametrize("subject", RETIRED)
    def test_neither_exists(self, conn, subject):
        _briefing(conn)
        assert egress.subject_exists(conn, USER, subject) is False

    def test_a_briefing_and_a_stale_row_are_not_listed(self, conn):
        _briefing(conn)
        for subject in RETIRED:
            _seed(conn, subject)
        assert egress.list_subject_names(conn, USER) == []

    @pytest.mark.parametrize("source_type,columns", [
        ("briefing", {"briefing_name": "morning"}),
        ("istota_file", {}),
    ])
    def test_a_refusal_is_not_recorded(self, tmp_path, db_path, source_type, columns):
        config = Config(db_path=db_path, temp_dir=tmp_path / "temp")
        with db.get_db(db_path) as conn:
            _briefing(conn)
            task = self._task(conn, source_type, **columns)
            conn.commit()
        file_egress_refusals(config, task, [("web_fetch", HOST)])
        with db.get_db(db_path) as conn:
            assert _subjects(conn, "egress_refused") == []
            assert _bell_states(conn) == {}
