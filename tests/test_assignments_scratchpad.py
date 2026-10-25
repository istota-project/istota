"""`assignments/scratchpad.py`: snapshots around a wake, outside changes, repair.

Every case runs against a real assignment directory under a tmp workspace and
a real framework DB; nothing below the module is patched.
"""

from __future__ import annotations

import os

import pytest

from istota import db
from istota.assignments import charter, scratchpad, store

BUDGET = {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 20, "per_wake_turns": 60}


@pytest.fixture
def config(make_config, tmp_path):
    config = make_config(db_path=tmp_path / "istota.db")
    db.init_db(config.db_path)
    return config


@pytest.fixture
def conn(config):
    with db.get_db(config.db_path) as conn:
        yield conn


def _assignment(config, conn) -> store.Assignment:
    assignment_id = store.insert_assignment(
        conn, user_id="alice", slug="quotes", title="Quotes", goal="Get quotes",
        budget=BUDGET, dir_relpath=charter.dir_relpath("quotes"),
    )
    assignment = store.get_assignment(conn, assignment_id)
    assert charter.create_directory(config, conn, assignment)
    return assignment


def _wake(conn, assignment) -> store.Wake:
    wake_id = store.insert_wake(conn, assignment.id, fired_by="time", task_id=None)
    return store.get_wake(conn, wake_id)


def _path(config, assignment):
    return (
        config.workspace_root(assignment.user_id) / config.bot_dir_name
        / assignment.dir_relpath / charter.SCRATCHPAD_FILE
    )


def _events(conn, assignment, kind):
    return [e for e in store.list_events(conn, assignment.id) if e.kind == kind]


class TestBegin:
    def test_an_untouched_seed_is_no_edit_and_writes_the_seed_baseline(self, config, conn):
        assignment = _assignment(config, conn)
        result = scratchpad.begin(config, conn, assignment, _wake(conn, assignment))

        assert result.text == charter.SCRATCHPAD_SEED
        assert result.change_event_id is None and result.restored is None
        assert store.list_inbox(conn, assignment.id) == []
        seed = store.latest_snapshot(conn, assignment.id, phases=("seed",))
        assert seed is not None and seed.wake_id is None
        assert seed.sha256 == scratchpad.sha256(charter.SCRATCHPAD_SEED)
        before = store.latest_snapshot(conn, assignment.id, phases=("before",))
        assert before.content == charter.SCRATCHPAD_SEED

    def test_a_change_since_the_last_after_is_an_untrusted_change_with_a_diff(self, config, conn):
        """Any task of the user's can write the file, so a change is not the user's word."""
        assignment = _assignment(config, conn)
        first = _wake(conn, assignment)
        scratchpad.begin(config, conn, assignment, first)
        scratchpad.store_snapshot(
            conn, assignment.id, phase="after", text=charter.SCRATCHPAD_SEED, wake_id=first.id,
        )
        store.update_wake(conn, first.id, ended_at=db.iso_utc_now())
        edited = charter.SCRATCHPAD_SEED + "\nCall installer B after 5pm.\n"
        _path(config, assignment).write_text(edited)

        second = _wake(conn, assignment)
        result = scratchpad.begin(config, conn, assignment, second)

        assert result.text == edited
        [event] = store.list_inbox(conn, assignment.id)
        assert event.id == result.change_event_id
        assert event.kind == "scratchpad_changed" and not event.trusted
        assert "+Call installer B after 5pm." in event.payload["diff"]
        assert "SCRATCHPAD.md (after wake 1)" in event.payload["diff"]
        assert set(event.payload) == {"diff", "cut"}
        [audit] = _events(conn, assignment, "scratchpad_changed")
        assert audit.actor == "system"
        assert audit.payload["inbox_id"] == event.id
        assert _events(conn, assignment, "user_edit") == []

    def test_a_long_diff_is_cut_with_a_marker(self, config, conn):
        assignment = _assignment(config, conn)
        _path(config, assignment).write_text(charter.SCRATCHPAD_SEED + "x" * 20000 + "\n")
        scratchpad.begin(config, conn, assignment, _wake(conn, assignment))

        [event] = store.list_inbox(conn, assignment.id)
        assert event.payload["cut"] is True
        assert len(event.payload["diff"]) <= scratchpad.DIFF_MAX_CHARS + len(scratchpad.DIFF_CUT_MARKER)
        assert event.payload["diff"].endswith(scratchpad.DIFF_CUT_MARKER)

    @pytest.mark.parametrize(
        "breakage, reason",
        [
            ("missing", scratchpad.MISSING),
            ("empty", scratchpad.EMPTY),
            ("not_utf8", scratchpad.NOT_UTF8),
            ("too_large", scratchpad.TOO_LARGE),
            ("symlink", scratchpad.NOT_REGULAR),
        ],
    )
    def test_a_broken_file_is_restored_from_the_last_whole_snapshot(
        self, config, conn, tmp_path, breakage, reason,
    ):
        assignment = _assignment(config, conn)
        first = _wake(conn, assignment)
        good = charter.SCRATCHPAD_SEED + "\n- a finding\n"
        scratchpad.store_snapshot(conn, assignment.id, phase="after", text=good, wake_id=first.id)
        path = _path(config, assignment)
        if breakage == "missing":
            path.unlink()
        elif breakage == "empty":
            path.write_text("  \n")
        elif breakage == "not_utf8":
            path.write_bytes(b"\xff\xfe not text")
        elif breakage == "too_large":
            path.write_text("x" * (scratchpad.FILE_MAX_BYTES + 1))
        else:
            outside = tmp_path / "elsewhere.md"
            outside.write_text("someone else's file\n")
            path.unlink()
            os.symlink(outside, path)

        result = scratchpad.begin(config, conn, assignment, _wake(conn, assignment))

        assert result.restored == reason
        assert result.text == good
        assert not path.is_symlink()
        assert path.read_text() == good
        [event] = _events(conn, assignment, "scratchpad_restored")
        assert event.payload == {"reason": reason, "phase": "begin", "written": True}
        assert store.list_inbox(conn, assignment.id) == []
        if breakage == "symlink":
            assert (tmp_path / "elsewhere.md").read_text() == "someone else's file\n"
        if breakage == "empty":
            [emptied] = _events(conn, assignment, "scratchpad_changed")
            assert emptied.actor == "system" and emptied.payload == {"emptied": True}
        assert _events(conn, assignment, "user_edit") == []

    def test_a_truncated_snapshot_is_never_restored_from(self, config, conn):
        assignment = _assignment(config, conn)
        first = _wake(conn, assignment)
        huge = "y" * (scratchpad.SNAPSHOT_MAX_BYTES + 10)
        scratchpad.store_snapshot(conn, assignment.id, phase="after", text=huge, wake_id=first.id)
        _path(config, assignment).unlink()

        result = scratchpad.begin(config, conn, assignment, _wake(conn, assignment))

        assert result.text == charter.SCRATCHPAD_SEED


class TestSnapshots:
    def test_text_over_the_cap_is_stored_truncated_with_the_whole_texts_hash(self, conn, config):
        assignment = _assignment(config, conn)
        text = "z" * (scratchpad.SNAPSHOT_MAX_BYTES + 100)
        scratchpad.store_snapshot(conn, assignment.id, phase="after", text=text)

        snapshot = store.latest_snapshot(conn, assignment.id, phases=("after",))
        assert snapshot.truncated
        assert len(snapshot.content.encode()) == scratchpad.SNAPSHOT_MAX_BYTES
        assert snapshot.sha256 == scratchpad.sha256(text)

    def test_a_truncated_baseline_still_compares_exactly(self, conn, config):
        assignment = _assignment(config, conn)
        first = _wake(conn, assignment)
        text = "line\n" * (scratchpad.SNAPSHOT_MAX_BYTES // 5 + 50)
        scratchpad.store_snapshot(conn, assignment.id, phase="after", text=text, wake_id=first.id)
        _path(config, assignment).write_text(text)

        result = scratchpad.begin(config, conn, assignment, _wake(conn, assignment))

        assert result.change_event_id is None


class TestEnd:
    def _changed_wake(self, config, conn):
        """A wake whose begin read an outside change adding two lines."""
        assignment = _assignment(config, conn)
        edited = charter.SCRATCHPAD_SEED + "\nUSER: the budget is firm.\nUSER: no gas units.\n"
        _path(config, assignment).write_text(edited)
        wake = _wake(conn, assignment)
        scratchpad.begin(config, conn, assignment, wake)
        store.consume_inbox(conn, assignment.id, wake.id)
        return assignment, wake, edited

    def test_lines_from_an_outside_change_the_wake_removed_stay_removed(self, config, conn):
        assignment, wake, _edited = self._changed_wake(config, conn)
        written = charter.SCRATCHPAD_SEED + "\nUSER: no gas units.\nWake's own note.\n"
        _path(config, assignment).write_text(written)

        result = scratchpad.end(config, conn, assignment, wake)

        assert result.text == written
        assert _path(config, assignment).read_text() == written
        assert store.wake_snapshot(conn, wake.id, "after").content == written
        assert _events(conn, assignment, "user_lines_kept") == []

    def test_a_file_the_wake_broke_goes_back_to_its_before_text(self, config, conn):
        assignment, wake, edited = self._changed_wake(config, conn)
        _path(config, assignment).write_text("")

        result = scratchpad.end(config, conn, assignment, wake)

        assert result.restored == scratchpad.EMPTY
        assert _path(config, assignment).read_text() == edited
        assert store.wake_snapshot(conn, wake.id, "after").content == edited

    def test_the_next_begin_sees_no_change_after_end(self, config, conn):
        assignment, wake, _edited = self._changed_wake(config, conn)
        _path(config, assignment).write_text(charter.SCRATCHPAD_SEED + "\nWake's own note.\n")
        scratchpad.end(config, conn, assignment, wake)
        store.update_wake(conn, wake.id, ended_at=db.iso_utc_now())

        result = scratchpad.begin(config, conn, assignment, _wake(conn, assignment))

        assert result.change_event_id is None
