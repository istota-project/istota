"""Deferred subtask creation: what a child inherits from its parent.

A subtask's `source_type` is `"subtask"`, so without an explicit inheritance it
would take `[brain.source_type_overrides]["subtask"]` and could silently run a
different brain from the parent that spawned it. `tasks.brain` is carried down
alongside `model` / `effort`, and this file asserts it against a config that
*does* set that override — otherwise the test cannot fail.
"""

import json

import pytest

from istota import db
from istota.brain import resolve_brain_kind
from istota.config import (
    BrainConfig,
    Config,
    NativeBrainConfig,
    SchedulerConfig,
    UserConfig,
)
from istota.scheduler_deferred import _process_deferred_subtasks


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    cfg = Config()
    cfg.db_path = db_path
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir(exist_ok=True)
    cfg.users = {"alice": UserConfig()}
    cfg.admin_users = {"alice"}
    cfg.scheduler = SchedulerConfig()
    cfg.brain = BrainConfig(
        kind="claude_code",
        native=NativeBrainConfig(model="claude-sonnet-4-6"),
        # The thing the inheritance has to beat.
        source_type_overrides={"subtask": "tmux_claude"},
        room_selectable=["native"],
    )
    return cfg


def _parent(conn, **kwargs):
    tid = db.create_task(
        conn, prompt="parent", user_id="alice", source_type="talk",
        conversation_token="room1", **kwargs,
    )
    return db.get_task(conn, tid)


def _write_subtask_file(config, task, entries):
    user_temp = config.temp_dir / "alice"
    user_temp.mkdir(parents=True, exist_ok=True)
    (user_temp / f"task_{task.id}_subtasks.json").write_text(
        json.dumps(entries), encoding="utf-8",
    )
    return user_temp


def _subtask(db_path):
    with db.get_db(db_path) as conn:
        rows = [
            t for t in db.list_tasks(conn, user_id="alice")
            if t.source_type == "subtask"
        ]
    assert len(rows) == 1
    return rows[0]


class TestSubtaskBrainInheritance:
    def test_a_subtask_inherits_the_parents_brain(self, config, db_path):
        with db.get_db(db_path) as conn:
            task = _parent(conn, brain="native")
        user_temp = _write_subtask_file(config, task, [{"prompt": "follow up"}])

        assert _process_deferred_subtasks(config, task, user_temp) == 1

        child = _subtask(db_path)
        assert child.brain == "native"
        # And that the inheritance is what decided it: the source-type layer
        # would have sent this task somewhere else entirely.
        assert resolve_brain_kind("subtask", config.brain).kind == "tmux_claude"
        assert resolve_brain_kind(
            child.source_type, config.brain, override=child.brain,
        ).kind == "native"

    def test_a_parent_with_no_brain_leaves_the_subtask_null(
        self, config, db_path,
    ):
        with db.get_db(db_path) as conn:
            task = _parent(conn)
        user_temp = _write_subtask_file(config, task, [{"prompt": "follow up"}])

        assert _process_deferred_subtasks(config, task, user_temp) == 1
        assert _subtask(db_path).brain is None
