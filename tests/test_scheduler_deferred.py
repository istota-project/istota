"""Deferred subtask creation: what a child inherits from its parent.

A subtask's `source_type` is `"subtask"`, so without an explicit inheritance it
would take `[brain.source_type_overrides]["subtask"]` and could silently run a
different brain from the parent that spawned it. `tasks.brain` is carried down
alongside `model` / `effort`, and this file asserts it against a config that
*does* set that override — otherwise the test cannot fail.

The `model` half carries a condition the `brain` half does not (ISSUE-421). A
stored model name is a bare string whose namespace comes from where it was
written, and `executor._pin_origin_namespace` reads an unpinned row's namespace
off that row's own lane — so copying the parent's name onto a `subtask` row with
a NULL `brain` hands the child a name it will read in the wrong namespace
wherever the two lanes route to different kinds.
"""

import json
import logging

import pytest

from istota import db
from istota.brain import model_namespace_for_kind, resolve_brain_kind
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


def _lane_namespace(config, source_type):
    return model_namespace_for_kind(
        resolve_brain_kind(source_type, config.brain).kind,
    )


class TestSubtaskModelInheritanceAcrossNamespaces:
    """A parent's `model` only travels where the child will read it right.

    The fixture's own override (`subtask` -> `tmux_claude`) is deliberately not
    a namespace change — `tmux_claude` and `claude_code` share `anthropic`,
    which ISSUE-417 settled — so each case below states the two lanes it needs
    and asserts they differ, or don't, before asserting on the row.
    """

    def _child_of(self, config, db_path, *, parent_kwargs, entries=None):
        with db.get_db(db_path) as conn:
            task = _parent(conn, **parent_kwargs)
        user_temp = _write_subtask_file(
            config, task, entries or [{"prompt": "follow up"}],
        )
        assert _process_deferred_subtasks(config, task, user_temp) == 1
        return _subtask(db_path)

    def test_the_model_is_dropped_where_the_child_would_read_another_namespace(
        self, config, db_path,
    ):
        """The reported defect: `talk` stays on claude_code, `subtask` goes native.

        `claude-opus-5` is what `commands.brain_for_room` writes for that parent.
        The child's row has a NULL `brain`, so the executor resolves its
        namespace from `subtask` — `openai_compat` — and passes an anthropic id
        to native's wire unchallenged. The parent's pin cannot carry, so it is
        dropped here and the child runs the routed brain's own default, which is
        what happened before ISSUE-419 and is the right outcome.
        """
        config.brain = BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model="z-ai/glm-5"),
            source_type_overrides={"subtask": "native"},
        )
        assert _lane_namespace(config, "talk") == "anthropic"
        assert _lane_namespace(config, "subtask") == "openai_compat"

        child = self._child_of(
            config, db_path, parent_kwargs={"model": "claude-opus-5"},
        )
        assert child.model is None
        assert child.brain is None

    def test_the_effort_survives_a_dropped_model(self, config, db_path):
        config.brain = BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model="z-ai/glm-5"),
            source_type_overrides={"subtask": "native"},
        )
        child = self._child_of(
            config, db_path,
            parent_kwargs={"model": "claude-opus-5", "effort": "high"},
        )
        assert child.model is None
        assert child.effort == "high"

    def test_the_model_carries_where_both_lanes_share_a_namespace(
        self, config, db_path,
    ):
        """The control against a blanket drop.

        The fixture routes `subtask` to `tmux_claude`, which is a different
        *kind* reading the same vocabulary, so the name is as valid on the child
        as on the parent and dropping it would discard a working pin.
        """
        assert _lane_namespace(config, "talk") == _lane_namespace(config, "subtask")
        child = self._child_of(
            config, db_path, parent_kwargs={"model": "claude-opus-5"},
        )
        assert child.model == "claude-opus-5"

    def test_a_pinned_parent_carries_its_model_down_with_its_brain(
        self, config, db_path,
    ):
        """`brain` travelling is what establishes the child's namespace.

        With the column set, the executor reads both rows' namespace off the
        same pinned kind, so there is no lane to disagree about and the name
        carries however the deployment routes `subtask`.
        """
        config.brain = BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model="z-ai/glm-5"),
            source_type_overrides={"subtask": "native"},
            room_selectable=["native"],
        )
        child = self._child_of(
            config, db_path,
            parent_kwargs={"brain": "native", "model": "z-ai/glm-5"},
        )
        assert child.brain == "native"
        assert child.model == "z-ai/glm-5"

    def test_a_routing_read_that_raises_drops_rather_than_carries(
        self, config, db_path, monkeypatch,
    ):
        """An origin that could not be established is not a match.

        The read is guarded because the drain calls its handlers in sequence
        with no guard between them, so a raise here would cost every later
        handler for this task. Its residue has to be the safe direction, which
        is the one `_resolve_crossing_model_effort` gives `None`: drop a pin
        whose portability could not be settled rather than send it to a wire
        that may not take it.
        """
        def _boom(*a, **k):
            raise RuntimeError("routing read failed")

        monkeypatch.setattr(
            "istota.scheduler_deferred.resolve_brain_kind", _boom,
        )
        child = self._child_of(
            config, db_path, parent_kwargs={"model": "claude-opus-5"},
        )
        assert child.model is None

    def test_an_explicit_model_in_the_deferred_json_is_untouched(
        self, config, db_path,
    ):
        """The JSON's own `model` is a raw name nobody has resolved yet.

        It is written in no namespace, so the executor resolving it against the
        child's own lane is already correct and there is nothing to drop.
        """
        config.brain = BrainConfig(
            kind="claude_code",
            native=NativeBrainConfig(model="z-ai/glm-5"),
            source_type_overrides={"subtask": "native"},
        )
        child = self._child_of(
            config, db_path,
            parent_kwargs={"model": "claude-opus-5"},
            entries=[{"prompt": "follow up", "model": "z-ai/glm-5"}],
        )
        assert child.model == "z-ai/glm-5"


CSV = (
    ",,MORPHOLOGY,,\n"
    "Date,Lab,WBC (th/mm3),Hgb (g/dL)\n"
    ",,4.8-10.5,12.7-16.7\n"
    '2026-07-27,"Example Lab",6.4,14.5\n'
)


class TestDeferredImportCsvSourcePath:
    """`import_csv` replays a path written from inside the sandbox.

    The daemon doing the replay is never sandboxed, so an unscoped
    `source_path` files any host file the daemon can read into the user's
    health database — from where it comes back through the health UI and the
    health skill. Its two neighbours (`register_upload`, `attach_document`)
    already resolve through `_resolved_source_path`; this arm did not.
    """

    def _ctx(self, tmp_path):
        """Production's shape: the bot workspace sits inside the user's dir."""
        from istota.health._migrate import ensure_initialised
        from istota.health.workspace import synthesize_health_context

        ctx = synthesize_health_context(
            "alice", tmp_path / "Users" / "alice" / "istota",
        )
        ensure_initialised(ctx)
        return ctx

    def _replay(self, ctx, deferred, ops, *, task_id=99):
        from istota import db as core_db
        from istota.scheduler_deferred import _process_deferred_health_ops

        (deferred / f"task_{task_id}_health_ops.json").write_text(
            json.dumps(ops), encoding="utf-8",
        )

        import istota.health as _health

        class _FakeConfig:
            # Mirrors Config.workspace_root(user_id) -> {mount}/Users/{uid}.
            @staticmethod
            def workspace_root(user_id=None):
                return ctx.workspace_root.parent

        original = _health.resolve_for_user
        try:
            _health.resolve_for_user = lambda uid, cfg: ctx
            task = core_db.Task(
                id=task_id, status="completed", source_type="cli",
                user_id="alice", prompt="",
            )
            return _process_deferred_health_ops(_FakeConfig(), task, deferred)
        finally:
            _health.resolve_for_user = original

    def _panel_ids(self, ctx):
        from istota.health import db as health_db

        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(conn, include_drafts=True)
        return {p.id for p in panels}

    def _write(self, path, text=CSV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_workspace_source_is_imported(self, tmp_path):
        """The acceptance half: without it, every refusal below is vacuous."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = self._write(ctx.workspace_root.parent / "inbox" / "labs.csv")

        count = self._replay(ctx, deferred, [
            {"op": "import_csv", "source_path": str(src)},
        ])

        assert count == 1
        assert len(self._panel_ids(ctx)) == 1

    def test_a_source_outside_the_workspace_is_skipped(self, tmp_path, caplog):
        """Watermark plus a discriminating column: no *new* panel appears."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()

        inside = self._write(ctx.workspace_root.parent / "inbox" / "labs.csv")
        assert self._replay(ctx, deferred, [
            {"op": "import_csv", "source_path": str(inside)},
        ], task_id=1) == 1
        mark = self._panel_ids(ctx)
        assert mark

        outside = self._write(
            tmp_path / "elsewhere" / "stolen.csv",
            ",,MORPHOLOGY,,\n"
            "Date,Lab,WBC (th/mm3),Hgb (g/dL)\n"
            ",,4.8-10.5,12.7-16.7\n"
            '2026-08-01,"Other Lab",7.7,13.3\n',
        )

        with caplog.at_level(logging.WARNING, logger="istota.scheduler_deferred"):
            count = self._replay(ctx, deferred, [
                {"op": "import_csv", "source_path": str(outside)},
            ], task_id=2)

        assert count == 0
        assert self._panel_ids(ctx) == mark
        assert any("import_csv skipped" in r.getMessage() for r in caplog.records)

    def test_a_symlink_out_of_the_workspace_is_skipped(self, tmp_path):
        """Resolution comes first, so a link inside the roots is caught too."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()

        outside = self._write(tmp_path / "elsewhere" / "stolen.csv")
        link = ctx.workspace_root.parent / "inbox" / "labs.csv"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        count = self._replay(ctx, deferred, [
            {"op": "import_csv", "source_path": str(link)},
        ])

        assert count == 0
        assert self._panel_ids(ctx) == set()
