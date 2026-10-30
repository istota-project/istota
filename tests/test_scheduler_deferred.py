"""Deferred-op handlers: subtask inheritance, the health source-path guard,
and one bad op not costing the rest of the drain.

Deferred subtask creation: what a child inherits from its parent.

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
from pathlib import Path

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
        # And that the inheritance is what decided it: the `subtask` lane
        # would have sent this task somewhere else entirely.
        assert resolve_brain_kind("subtask", config.brain).kind == "claude_code"
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

    The fixture routes `subtask` nowhere of its own, so both lanes are the
    base kind's `anthropic`; each case below states the two lanes it needs and
    asserts they differ, or don't, before asserting on the row.
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

        The fixture's `subtask` lane reads the same vocabulary as `talk`, so
        the name is as valid on the child as on the parent and dropping it
        would discard a working pin.
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


class _HealthOpsReplay:
    """Replay a deferred health op file the way the daemon does."""

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

        # `ops=None` means the caller wrote the file itself — the way to get
        # JSON that `json.dumps` cannot produce, such as a bare `Infinity`.
        if ops is not None:
            (deferred / f"task_{task_id}_health_ops.json").write_text(
                json.dumps(ops), encoding="utf-8",
            )

        import istota.health as _health

        # A real Config, not a stand-in: the guard derives its roots from
        # `workspace_path` and the task's user id, so a fake answering
        # one hand-written question would not exercise the derivation the
        # deployment runs.
        config = Config(workspace_path=ctx.workspace_root.parent.parent.parent)

        original = _health.resolve_for_user
        try:
            _health.resolve_for_user = lambda uid, cfg: ctx
            task = core_db.Task(
                id=task_id, status="completed", source_type="cli",
                user_id="alice", prompt="",
            )
            return _process_deferred_health_ops(config, task, deferred)
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


class TestDeferredRegisterUploadTarget(_HealthOpsReplay):
    """Local-workspace spec, stage 3: the uploads tree is in the user's
    workspace, so the panel directory and the copy follow no link."""

    def test_an_upload_is_copied_under_the_panel(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "scan.pdf"
        src.write_bytes(b"%PDF-1.4 scan")

        count = self._replay(ctx, deferred, [
            {"op": "register_upload", "source_path": str(src), "drawn_at": "2026-05-08"},
        ])

        assert count == 1
        (pid,) = self._panel_ids(ctx)
        assert (ctx.uploads_dir / str(pid) / "original.pdf").read_bytes() == b"%PDF-1.4 scan"

    def test_a_symlinked_panel_dir_is_not_written_through(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "scan.pdf"
        src.write_bytes(b"%PDF-1.4 scan")
        outside = tmp_path / "outside"
        outside.mkdir()
        ctx.uploads_dir.mkdir(parents=True, exist_ok=True)
        (ctx.uploads_dir / "1").symlink_to(outside, target_is_directory=True)

        count = self._replay(ctx, deferred, [
            {"op": "register_upload", "source_path": str(src), "drawn_at": "2026-05-08"},
        ])

        assert count == 0
        assert list(outside.iterdir()) == []


class TestDeferredImportCsvSourcePath(_HealthOpsReplay):
    """`import_csv` replays a path written from inside the sandbox.

    The daemon doing the replay is never sandboxed, so an unscoped
    `source_path` files any host file the daemon can read into the user's
    health database — from where it comes back through the health UI and the
    health skill. Its two neighbours (`register_upload`, `attach_document`)
    already resolve through `_resolved_source_path`; this arm did not.
    """

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

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            count = self._replay(ctx, deferred, [
                {"op": "import_csv", "source_path": str(outside)},
            ], task_id=2)

        assert count == 0
        assert self._panel_ids(ctx) == mark
        assert any("import_csv skipped" in r.getMessage() for r in caplog.records)

    def test_a_deferred_dir_source_is_imported(self, tmp_path):
        """The route a sandboxed task actually takes.

        The workspace case above is the email-attachment shape. A task with
        no mount write of its own writes into `$ISTOTA_DEFERRED_DIR`, which
        is the guard's other root, and a narrowing that dropped it would
        leave the case above green while breaking the sandbox's own path.
        """
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = self._write(deferred / "labs.csv")

        count = self._replay(ctx, deferred, [
            {"op": "import_csv", "source_path": str(src)},
        ])

        assert count == 1
        assert len(self._panel_ids(ctx)) == 1

    def test_a_symlink_out_of_the_workspace_is_skipped(self, tmp_path, caplog):
        """Resolution comes first, so a link inside the roots is caught too."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()

        outside = self._write(tmp_path / "elsewhere" / "stolen.csv")
        link = ctx.workspace_root.parent / "inbox" / "labs.csv"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            count = self._replay(ctx, deferred, [
                {"op": "import_csv", "source_path": str(link)},
            ])

        assert count == 0
        assert self._panel_ids(ctx) == set()
        # Pin the *reason*: count == 0 alone is equally true of a parse
        # failure or an op that raised and was recorded in `failures`.
        assert any("import_csv skipped" in r.getMessage() for r in caplog.records)

    def test_the_roots_come_from_a_real_config(self, tmp_path):
        """The two derivations of one root have to agree, id by id.

        `_source_path_allowed` no longer *calls* `Config.workspace_root` — it
        re-derives `{mount}/Users/{uid}` through `workspace_roots`. Two
        derivations of one boundary is exactly how the four copies this
        replaced drifted apart, so what holds them together has to be an
        equality over a table rather than one shared happy case: asserting
        each separately on `"alice"` stays green through any divergence that
        does not happen to involve `"alice"`, which is the shape of every
        real one.
        """
        from istota.sandbox.host_paths import path_under_roots
        from istota.scheduler_deferred import _source_path_allowed

        config = Config(workspace_path=tmp_path / "mount")
        assert config.workspace_root("alice") == tmp_path / "mount" / "Users" / "alice"

        deferred = tmp_path / "deferred"
        deferred.mkdir()
        mine = self._write(tmp_path / "mount" / "Users" / "alice" / "inbox" / "labs.csv")
        theirs = self._write(
            tmp_path / "mount" / "Users" / "bob" / "inbox" / "labs.csv",
        )

        assert _source_path_allowed(mine, deferred, config, "alice")
        assert not _source_path_allowed(theirs, deferred, config, "alice")

        # An empty user id is the one case where the two must *not* agree, and
        # it is excluded from the table below rather than passed: on
        # `Config.workspace_root` a falsy id means "no user given, hand back
        # the bare mount root", which is a different question from "this user
        # id does not scope". The guard must never adopt that answer — the
        # mount root is every user's directory at once.
        assert config.workspace_root("") == tmp_path / "mount"
        assert not _source_path_allowed(mine, deferred, config, "")

        # The equality, over ids that do name a user. Neither file is under
        # the deferred dir, so the guard's answer is entirely the own root's,
        # which is what makes it comparable to the config's answer at all.
        for user_id in (
            "alice", " alice", "alice ", ".", "..", "/etc", "../bob", "a/b",
        ):
            root = config.workspace_root(user_id)
            for candidate in (mine, theirs):
                via_config = root is not None and path_under_roots(
                    candidate.resolve(), [Path(root).resolve()],
                )
                via_guard = _source_path_allowed(
                    candidate, deferred, config, user_id,
                )
                assert via_config == via_guard, (user_id, candidate)


class TestDeferredHealthOpBatchIsolation(_HealthOpsReplay):
    """One malformed op must not take the rest of the batch with it.

    Every field in a deferred op file is model-authored JSON written from
    inside the sandbox, so any of the loop's coercions can be handed the
    wrong type: `Path(123)`, `float([1])` and `int({})` all raise
    `TypeError`. That was outside the per-op `except` tuple, so instead of
    the intended "log it, record it, carry on" the exception unwound the
    whole drain and every later op in the file was silently never applied —
    on records that are not idempotent and cannot be replayed by hand
    (ISSUE-451).
    """

    def _stats(self, ctx):
        from istota.health import db as health_db

        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_stats(conn)

    def test_a_non_string_source_path_leaves_the_batch_running(
        self, tmp_path, caplog,
    ):
        """The reported shape: a bad path arm ahead of a good op."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = self._write(ctx.workspace_root.parent / "inbox" / "labs.csv")

        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            count = self._replay(ctx, deferred, [
                {"op": "import_csv", "source_path": 123},
                {"op": "import_csv", "source_path": str(src)},
            ], task_id=7)

        assert count == 1
        assert len(self._panel_ids(ctx)) == 1
        assert any(
            "Failed to process health op" in r.getMessage()
            for r in caplog.records
        )

        # The failing op has to be recoverable, not merely survived: without
        # the sidecar the user has a health record that never arrived and no
        # way to tell which one.
        sidecar = deferred / "task_7_health_op_failures.json"
        recorded = json.loads(sidecar.read_text(encoding="utf-8"))
        assert len(recorded) == 1
        assert recorded[0]["op"]["source_path"] == 123
        assert recorded[0]["error"].startswith("TypeError:")

    def test_failure_sidecar_link_cannot_overwrite_private_file(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        canary = tmp_path / "private.json"
        canary.write_text('[{"private": true}]')
        (deferred / "task_7_health_op_failures.json").symlink_to(canary)

        assert self._replay(ctx, deferred, [
            {"op": "import_csv", "source_path": 123},
        ], task_id=7) == 0

        assert json.loads(canary.read_text()) == [{"private": True}]
        assert not (deferred / "task_7_health_ops.json").exists()

    def test_a_non_numeric_value_leaves_the_batch_running(self, tmp_path):
        """The same failure class away from the path arms.

        `float(entry["value"])` is one of twenty-odd coercions over the same
        untrusted JSON, so a fix confined to `_resolved_source_path` would
        leave this one aborting the drain.
        """
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()

        count = self._replay(ctx, deferred, [
            {"op": "insert_stat", "metric": "weight", "value": [72], "unit": "kg"},
            {"op": "insert_stat", "metric": "weight", "value": 72.5, "unit": "kg"},
        ], task_id=8)

        assert count == 1
        assert [s.value for s in self._stats(ctx)] == [72.5]

    def test_an_infinite_id_leaves_the_batch_running(self, tmp_path):
        """Why the guard stopped enumerating types.

        `json.loads` accepts a bare `Infinity`, and `int(float("inf"))` raises
        `OverflowError` — neither the original tuple nor the same tuple plus
        `TypeError` would have caught it, and the drain would still have been
        one model-written token away from aborting.
        """
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        (deferred / "task_10_health_ops.json").write_text(
            '[{"op": "delete_diagnosis", "diagnosis_id": Infinity},'
            ' {"op": "insert_stat", "metric": "weight", "value": 71.0,'
            ' "unit": "kg"}]',
            encoding="utf-8",
        )

        count = self._replay(ctx, deferred, None, task_id=10)

        assert count == 1
        assert [s.value for s in self._stats(ctx)] == [71.0]


class TestDeferredDrainSurvivesANonDictEntry:
    """The same failure class in the drain's other two loops.

    `_load_deferred_json` type-checks the outer list only. The health and KG
    loops skip a non-dict entry; the KV and sent-email loops read `.get` off
    it before their own `try`, so one bare string in a model-written file
    raised an AttributeError that escaped the handler — costing the rest of
    that file, and (until ISSUE-469 guarded the sequence) every handler after
    it as well.
    """

    def _task(self, db_path, user_id="alice"):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="t", user_id=user_id)
            return db.get_task(conn, task_id)

    def _user_temp(self, tmp_path):
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        return user_temp

    def test_kv_ops_skips_it_and_applies_the_rest(self, db_path, tmp_path):
        from istota.scheduler_deferred import _process_deferred_kv_ops

        config = Config(db_path=db_path, temp_dir=tmp_path / "temp")
        task = self._task(db_path)
        user_temp = self._user_temp(tmp_path)
        (user_temp / f"task_{task.id}_kv_ops.json").write_text(
            json.dumps([
                "not an op",
                {"op": "set", "namespace": "notes", "key": "k", "value": "v"},
            ]),
            encoding="utf-8",
        )

        assert _process_deferred_kv_ops(config, task, user_temp) == 1
        with db.get_db(db_path) as conn:
            row = db.kv_get(conn, "alice", "notes", "k")
        assert row is not None
        assert row["value"] == "v"

    def test_sent_emails_skips_it_and_applies_the_rest(self, db_path, tmp_path):
        from istota.scheduler_deferred import _process_deferred_sent_emails

        config = Config(db_path=db_path, temp_dir=tmp_path / "temp")
        task = self._task(db_path)
        user_temp = self._user_temp(tmp_path)
        (user_temp / f"task_{task.id}_sent_emails.json").write_text(
            json.dumps([
                ["not", "an", "entry"],
                {"message_id": "<m1@example.com>", "to_addr": "her@example.com"},
            ]),
            encoding="utf-8",
        )

        assert _process_deferred_sent_emails(config, task, user_temp) == 1


class TestDrainSurvivesOneHandlersBadOp(_HealthOpsReplay):
    """The seam, not the handler: what a bad health op used to cost.

    An exception escaping the health loop did not stop at the end of that
    file — the Garmin import, the user alerts and the deferred email output
    for that task were all skipped behind it. The handler-level tests above
    cannot see that; this one runs the real drain. It passes because the
    health guard catches, which is why ISSUE-469 needed the class below: a
    control for the *sequence* has to defeat the handler's own guard.
    """

    def test_a_bad_health_op_no_longer_costs_the_later_handlers(
        self, db_path, tmp_path,
    ):
        import istota.health as _health
        from istota.scheduler import _drain_deferred_ops

        ctx = self._ctx(tmp_path)
        config = Config(
            db_path=db_path,
            temp_dir=tmp_path / "temp",
            workspace_path=ctx.workspace_root.parent.parent.parent,
        )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        with db.get_db(db_path) as conn:
            task = db.get_task(
                conn, db.create_task(conn, prompt="t", user_id="alice"),
            )

        (user_temp / f"task_{task.id}_health_ops.json").write_text(
            json.dumps([
                {"op": "insert_stat", "metric": "weight",
                 "value": [72], "unit": "kg"},
                {"op": "insert_stat", "metric": "weight",
                 "value": 72.5, "unit": "kg"},
            ]),
            encoding="utf-8",
        )
        (user_temp / f"task_{task.id}_user_alerts.json").write_text(
            json.dumps([{"type": "note", "message": "something looked odd"}]),
            encoding="utf-8",
        )

        original = _health.resolve_for_user
        try:
            _health.resolve_for_user = lambda uid, cfg: ctx
            _drain_deferred_ops(config, task, "done")
        finally:
            _health.resolve_for_user = original

        from istota.health import db as health_db

        with health_db.connect(ctx.db_path) as conn:
            assert [s.value for s in health_db.list_stats(conn)] == [72.5]

        # The handler that runs *after* the health one still ran.
        with db.get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
                ("alice",),
            ).fetchone()
        assert rows[0] == 1


class TestLoaderSurvivesUnparseableJson:
    """`_load_deferred_json` is the other place one file kills the drain.

    Its catch covers the file the model wrote, so it has to cover what
    parsing that file can raise — a decode error, and deeply nested JSON,
    which raises `RecursionError` rather than a `JSONDecodeError`.
    """

    def test_a_recursion_error_is_contained(self, tmp_path, monkeypatch, caplog):
        from istota.scheduler_deferred import _load_deferred_json

        path = tmp_path / "task_3_health_ops.json"
        path.write_text("[]", encoding="utf-8")

        # The depth at which the parser gives up is the interpreter's, not
        # ours; raising it here keeps the test about containment rather than
        # about a stack size that could change under it.
        def _raise(*a, **kw):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(json, "loads", _raise)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            assert _load_deferred_json(tmp_path, 3, "health_ops") is None

        assert not path.exists()
        assert any(
            "Bad deferred health_ops file" in r.getMessage()
            for r in caplog.records
        )


class TestDrainContainsAHandlerThatRaises:
    """The sequence itself, not any one handler (ISSUE-469).

    `_drain_deferred_ops` used to call its nine handlers in a bare sequence,
    so anything escaping one of them skipped the eight after it — and in
    `process_one_task`, where the drain sits ahead of result delivery, also
    the briefing archive, the log-channel finalization and the whole Talk /
    email / web delivery of an answer whose row already said `completed`.

    Two issues were closed by hardening one handler and leaving the sequence
    alone (ISSUE-135, ISSUE-451), so the control here has to defeat a
    handler's own guard: these patch the handler to raise outright rather
    than feeding it a bad op file it might now catch.
    """

    def _setup(self, db_path, tmp_path):
        config = Config(db_path=db_path, temp_dir=tmp_path / "temp")
        with db.get_db(db_path) as conn:
            task = db.get_task(
                conn, db.create_task(conn, prompt="t", user_id="alice"),
            )
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        return config, task, user_temp

    def _alerts(self, user_temp, task):
        (user_temp / f"task_{task.id}_user_alerts.json").write_text(
            json.dumps([{"type": "note", "message": "something looked odd"}]),
            encoding="utf-8",
        )

    def _notification_count(self, db_path):
        with db.get_db(db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
                ("alice",),
            ).fetchone()[0]

    @staticmethod
    def _boom(*args, **kwargs):
        raise RuntimeError("no guard catches this")

    def test_the_handlers_after_it_still_run(self, db_path, tmp_path, monkeypatch):
        from istota import scheduler

        config, task, user_temp = self._setup(db_path, tmp_path)
        self._alerts(user_temp, task)

        # KG runs fifth in the sequence; user alerts eighth.
        monkeypatch.setattr(scheduler, "_process_deferred_kg_ops", self._boom)
        scheduler._drain_deferred_ops(config, task, "done")

        assert self._notification_count(db_path) == 1

    def test_the_first_handler_raising_still_leaves_the_rest(
        self, db_path, tmp_path, monkeypatch,
    ):
        """The retired-file sweep runs first by design, so its escape is the
        one that used to cost every other handler."""
        from istota import scheduler

        config, task, user_temp = self._setup(db_path, tmp_path)
        self._alerts(user_temp, task)

        monkeypatch.setattr(scheduler, "_process_retired_deferred_files", self._boom)
        scheduler._drain_deferred_ops(config, task, "done")

        assert self._notification_count(db_path) == 1

    def test_the_failure_is_recorded_against_the_task(
        self, db_path, tmp_path, monkeypatch, caplog,
    ):
        from istota import scheduler

        config, task, _ = self._setup(db_path, tmp_path)

        monkeypatch.setattr(scheduler, "_process_deferred_kg_ops", self._boom)
        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            scheduler._drain_deferred_ops(config, task, "done")

        # With a stack, because the worker's own catch logs one line without
        # `exc_info` and that was the only trace an escape left anywhere.
        record = next(
            r for r in caplog.records
            if "kg_ops" in r.getMessage() and r.levelno >= logging.ERROR
        )
        assert record.exc_info is not None

        # And on the row, so the failure is visible to anything reading the
        # task rather than only in the daemon's journal.
        with db.get_db(db_path) as conn:
            logs = db.get_task_logs(conn, task.id)
        assert any(
            entry["level"] == "error" and "kg_ops" in entry["message"]
            for entry in logs
        )

    def test_the_unconsumed_scan_is_guarded_too(
        self, db_path, tmp_path, monkeypatch,
    ):
        """The last step sits outside the handler table — it takes no
        `config` — so it carries its own `try` and needs its own control."""
        from istota import scheduler

        config, task, _ = self._setup(db_path, tmp_path)

        monkeypatch.setattr(scheduler, "_warn_unconsumed_deferred_files", self._boom)
        scheduler._drain_deferred_ops(config, task, "done")

        with db.get_db(db_path) as conn:
            logs = db.get_task_logs(conn, task.id)
        assert any("unconsumed_scan" in entry["message"] for entry in logs)

    def test_the_recorder_itself_never_raises(
        self, db_path, tmp_path, monkeypatch, caplog,
    ):
        """The recorder runs inside the drain's own except, where the drain's
        guard is no second layer: an exception from *it* would escape and skip
        delivery, which is the original bug arrived at from the other side."""
        from istota import scheduler

        config, task, user_temp = self._setup(db_path, tmp_path)
        self._alerts(user_temp, task)

        monkeypatch.setattr(scheduler, "_process_deferred_kg_ops", self._boom)
        monkeypatch.setattr(
            scheduler.db, "log_task",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db is locked")),
        )

        with caplog.at_level(logging.DEBUG, logger="istota.scheduler"):
            scheduler._drain_deferred_ops(config, task, "done")

        # The drain ran on past both failures.
        assert self._notification_count(db_path) == 1
        assert any(
            "Could not record the deferred kg_ops failure" in r.getMessage()
            for r in caplog.records
        )

    def test_a_failing_handler_does_not_cost_the_delivery(
        self, db_path, tmp_path, monkeypatch,
    ):
        """The seam that matters: `run_task_inline` drains before it returns
        and `process_one_task` drains before it delivers, so neither may see
        an exception come out of the drain."""
        from istota import scheduler

        config, task, _ = self._setup(db_path, tmp_path)

        monkeypatch.setattr(scheduler, "_process_deferred_kv_ops", self._boom)
        monkeypatch.setattr(
            scheduler, "execute_task",
            lambda *a, **kw: (True, "the answer", None, None),
        )

        success, result = scheduler.run_task_inline(config, task)

        # The return is what the drain sits in front of; the status is written
        # before the drain runs, so asserting on it here would pass either way.
        assert (success, result) == (True, "the answer")

    def test_a_cancelled_drain_is_recorded_and_re_raised(
        self, db_path, tmp_path, monkeypatch,
    ):
        """`CancelledError` is a `BaseException`, and it is the one that
        actually reaches here: two handlers notify through `run_coro`, and
        `AsyncRuntime.stop` cancels in-flight coroutines on shutdown.

        Swallowing it would keep the drain running against a cancelled
        runtime, so it propagates — but the breadcrumb is written first.
        """
        import asyncio

        from istota import scheduler

        config, task, _ = self._setup(db_path, tmp_path)

        def _cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(scheduler, "_process_deferred_kg_ops", _cancelled)

        with pytest.raises(asyncio.CancelledError):
            scheduler._drain_deferred_ops(config, task, "done")

        with db.get_db(db_path) as conn:
            logs = db.get_task_logs(conn, task.id)
        assert any(
            entry["level"] == "error" and "CancelledError" in entry["message"]
            for entry in logs
        )



class TestSubtaskAudienceInheritance:
    """A subtask in the parent's room is read by the parent's audience, so the
    row fields the reach gate and the group set read travel with the token."""

    def test_the_audience_fields_are_inherited(self, config, db_path):
        with db.get_db(db_path) as conn:
            task = _parent(conn, is_group_chat=True, audience="mixed",
                           guest_participant_id=3)
        user_temp = _write_subtask_file(config, task, [{"prompt": "follow up"}])
        assert _process_deferred_subtasks(config, task, user_temp) == 1
        child = _subtask(db_path)
        assert child.is_group_chat is True
        assert child.audience == "mixed"
        assert child.guest_participant_id == 3

    def test_a_group_chat_subtask_resolves_no_group(self, config, db_path):
        from istota.rooms.scopes import task_group_ids

        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk", name="r")
            db.create_group(conn, "fam", kind="family", display_name="Fam",
                            created_by="operator")
            db.add_group_member(conn, "fam", "alice", added_by="operator")
            task = _parent(conn, is_group_chat=True)
            assert task_group_ids(conn, task) == []
        user_temp = _write_subtask_file(config, task, [{"prompt": "follow up"}])
        assert _process_deferred_subtasks(config, task, user_temp) == 1
        with db.get_db(db_path) as conn:
            assert task_group_ids(conn, _subtask(db_path)) == []


# ---------------------------------------------------------------------------
# The replay allowlist on a correspondent's turn
# ---------------------------------------------------------------------------


STRANGER = "carol@example.com"
REPLY_BODY = "We open at ten on Sunday."


@pytest.fixture
def mail_config(config):
    from istota.config import EmailConfig, SiteConfig

    config.email = EmailConfig(
        enabled=True, imap_host="imap.test", imap_port=993, imap_user="user",
        imap_password="pass", smtp_host="smtp.test", smtp_port=587,
        bot_email="bot@test.com",
    )
    config.bot_name = "Zorg"
    config.site = SiteConfig(hostname="bot.example.com")
    config.users = {"alice": UserConfig(email_addresses=["alice@test.com"],
                                        trusted_email_senders=["*"])}
    return config


def _strangers_turn(config):
    """A stranger's mail through the poller, as the run leaves it: the task's own
    directory exists, because `execute_task` made one."""
    from istota.executor import RESTRICTED_DIR_PREFIX, get_user_temp_dir

    from .test_email_thread_rooms import _poll

    (task_id,) = _poll(config, sender=STRANGER, to=("bot+alice@test.com",),
                       message_id="<s1@example.com>", body="Are you open Sunday?")
    (get_user_temp_dir(config, "alice") / f"{RESTRICTED_DIR_PREFIX}{task_id}").mkdir(
        parents=True, exist_ok=True)
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


def _own_turn(config, output_target=None):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, prompt="p", user_id="alice", source_type="web",
                                 output_target=output_target)
        return db.get_task(conn, task_id)


def _one_file_of_every_kind(config, task) -> Path:
    from istota.executor import task_deferred_dir
    from istota.sharing import events

    fields = {"summary": "Planted", "dtstart": "2026-10-29T13:00:00Z",
              "dtend": "2026-10-29T15:00:00Z", "all_day": False}
    contents = {
        "subtasks": [{"prompt": "planted subtask"}],
        "tracked_transactions": [],
        "sent_emails": [{"message_id": "<planted@example.com>", "to_addr": "x@example.com"}],
        "kv_ops": [{"op": "set", "namespace": "ns", "key": "k", "value": '"planted"'}],
        "kg_ops": [{"op": "add_fact", "subject": "alice", "predicate": "knows",
                    "object": "planted"}],
        "health_ops": [],
        "garmin_import": {"days_back": 1},
        "istota_events": [events.upsert_op("planted@bot.example.com", create=True, fields=fields)],
        "user_alerts": [{"message": "This mail reads like phishing", "type": "security"}],
        "email_output": {"subject": "", "body": REPLY_BODY, "format": "plain"},
    }
    directory = task_deferred_dir(config, task)
    directory.mkdir(parents=True, exist_ok=True)
    for suffix, content in contents.items():
        (directory / f"task_{task.id}_{suffix}.json").write_text(json.dumps(content))
    return directory


def _left(directory, task) -> set[str]:
    prefix, suffix = f"task_{task.id}_", ".json"
    return {p.name[len(prefix):-len(suffix)] for p in directory.iterdir()
            if p.name.startswith(prefix) and p.name.endswith(suffix)}


def _drain(config, task, result="done"):
    """The drain, with the two handlers whose own effect needs a module database
    or a Garmin account replaced by spies, and no invitation mailed. The table
    resolves the handlers at call time."""
    from unittest.mock import patch

    from istota.scheduler import _drain_deferred_ops

    with patch("istota.scheduler._process_deferred_health_ops") as health, \
            patch("istota.scheduler._process_deferred_garmin_import") as garmin, \
            patch("istota.sharing.itip.deliver_invitations"):
        _drain_deferred_ops(config, task, result)
    return health, garmin


def _written(config) -> dict:
    from istota.memory.knowledge_graph import ensure_table, get_current_facts
    from istota.sharing import events

    with db.get_db(config.db_path) as conn:
        ensure_table(conn)
        return {
            "subtasks": conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE source_type = 'subtask' "
                "AND prompt = 'planted subtask'").fetchone()[0],
            "sent_emails": conn.execute(
                "SELECT COUNT(*) FROM sent_emails WHERE message_id = '<planted@example.com>'",
            ).fetchone()[0],
            "kv": db.kv_get(conn, "alice", "ns", "k"),
            "facts": len(get_current_facts(conn, "alice")),
            "events": len(events.events_for_user(conn, "alice", include_cancelled=True)),
        }


def _alerts(config) -> list[str]:
    with db.get_db(config.db_path) as conn:
        return [r["body"] for r in conn.execute(
            "SELECT body FROM notifications WHERE source = 'task_alert'")]


class TestACorrespondentsDrain:
    def test_it_keeps_the_reply_and_writes_nothing(self, mail_config):
        task = _strangers_turn(mail_config)
        directory = _one_file_of_every_kind(mail_config, task)
        assert directory.name.startswith("room-task-")

        health, garmin = _drain(mail_config, task)

        assert _left(directory, task) == {"email_output"}
        health.assert_not_called()
        garmin.assert_not_called()
        assert _written(mail_config) == {
            "subtasks": 0, "sent_emails": 0, "kv": None, "facts": 0, "events": 0,
        }

    def test_a_user_alert_reaches_the_host(self, mail_config):
        task = _strangers_turn(mail_config)
        _one_file_of_every_kind(mail_config, task)
        _drain(mail_config, task)
        assert ["phishing" in body for body in _alerts(mail_config)] == [True]

    @pytest.mark.asyncio
    async def test_the_reply_is_sent_after_the_drain_with_no_draft(self, mail_config):
        from unittest.mock import patch

        from istota.scheduler import post_result_to_email

        task = _strangers_turn(mail_config)
        directory = _one_file_of_every_kind(mail_config, task)
        _drain(mail_config, task)

        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            assert await post_result_to_email(mail_config, task, "the model's report") is True

        assert reply.call_args.kwargs["to_addr"] == STRANGER
        assert reply.call_args.kwargs["body"] == REPLY_BODY
        assert _left(directory, task) == set()
        with db.get_db(mail_config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM outbound_drafts").fetchone()[0] == 0

    def test_calendar_events_apply_when_calendar_is_allowed(self, mail_config, monkeypatch):
        from istota.rooms import scopes

        real = scopes.correspondent_scopes

        def _widened(*args, **kwargs):
            allowance = real(*args, **kwargs)
            return scopes.CorrespondentAllowance(scopes=allowance.scopes | {"calendar"})

        monkeypatch.setattr(scopes, "correspondent_scopes", _widened)
        task = _strangers_turn(mail_config)
        directory = _one_file_of_every_kind(mail_config, task)

        _drain(mail_config, task)

        assert _left(directory, task) == {"email_output"}
        written = _written(mail_config)
        assert written["events"] == 1
        assert written["kv"] is None and written["subtasks"] == 0

    def test_a_subtask_of_the_turn_is_held_to_the_same_list(self, mail_config):
        parent = _strangers_turn(mail_config)
        with db.get_db(mail_config.db_path) as conn:
            child = db.get_task(conn, db.create_task(
                conn, prompt="t", user_id="alice", source_type="subtask",
                parent_task_id=parent.id, output_target="email",
            ))
        directory = _one_file_of_every_kind(mail_config, child)

        _drain(mail_config, child)

        assert _left(directory, child) == {"email_output"}
        assert _written(mail_config)["kv"] is None

    def test_a_file_that_will_not_unlink_is_still_not_replayed(self, mail_config):
        from unittest.mock import patch

        from istota.sandbox import deferred

        task = _strangers_turn(mail_config)
        directory = _one_file_of_every_kind(mail_config, task)
        real_unlink = deferred.unlink

        def _refuses_kv(path, **kwargs):
            if path.name.endswith("_kv_ops.json"):
                raise OSError("read-only")
            return real_unlink(path, **kwargs)

        with patch("istota.scheduler_deferred.deferred.unlink", _refuses_kv):
            _drain(mail_config, task)

        assert "kv_ops" in _left(directory, task)
        assert _written(mail_config)["kv"] is None

    def test_a_turn_that_cannot_be_read_replays_nothing_but_keeps_the_reply(
        self, mail_config, monkeypatch,
    ):
        from istota.rooms import scopes

        def _boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(scopes, "turn_author", _boom)
        task = _own_turn(mail_config, output_target="email")
        directory = _one_file_of_every_kind(mail_config, task)

        _drain(mail_config, task)

        assert _left(directory, task) == {"email_output"}
        assert _written(mail_config)["kv"] is None
        assert _written(mail_config)["subtasks"] == 0
        assert len(_alerts(mail_config)) == 1

    def test_every_handler_reads_a_suffix_the_purge_knows(self):
        import re

        from istota.scheduler import _drain_deferred_ops
        from istota.scheduler_deferred import _KNOWN_DEFERRED_SUFFIXES

        from .support.drift import source_of

        named = set(re.findall(r'\("(\w+)", _\w+\),', source_of(_drain_deferred_ops)))
        assert "subtasks" in named
        assert named - {"retired_files"} <= set(_KNOWN_DEFERRED_SUFFIXES)

    def test_the_kept_map_names_only_known_suffixes(self):
        from istota.scheduler import CORRESPONDENT_KEPT_SUFFIXES
        from istota.scheduler_deferred import _KNOWN_DEFERRED_SUFFIXES

        assert set(CORRESPONDENT_KEPT_SUFFIXES) <= set(_KNOWN_DEFERRED_SUFFIXES)
        assert CORRESPONDENT_KEPT_SUFFIXES == {
            "email_output": None, "user_alerts": None, "istota_events": "calendar",
        }


class TestControlOtherTurnsDrainAsBefore:
    def test_an_own_turn_replays_every_handler(self, mail_config):
        task = _own_turn(mail_config)
        directory = _one_file_of_every_kind(mail_config, task)
        assert not directory.name.startswith("room-task-")

        health, garmin = _drain(mail_config, task)

        health.assert_called_once()
        garmin.assert_called_once()
        written = _written(mail_config)
        assert written["subtasks"] == 1
        assert written["sent_emails"] == 1
        assert written["kv"]["value"] == '"planted"'
        assert written["facts"] == 1
        assert written["events"] == 1
        assert len(_alerts(mail_config)) == 1
        # A web task has no email leg, so the orphan is removed as it always was.
        assert "email_output" not in _left(directory, task)

    def test_a_guests_drain_purges_all_of_it(self, mail_config):
        with db.get_db(mail_config.db_path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="alice", source_type="web")
            conn.execute("UPDATE tasks SET guest_participant_id = 1 WHERE id = ?", (task_id,))
            task = db.get_task(conn, task_id)
        directory = _one_file_of_every_kind(mail_config, task)

        health, garmin = _drain(mail_config, task)

        assert _left(directory, task) == set()
        health.assert_not_called()
        assert _alerts(mail_config) == []
        assert _written(mail_config)["kv"] is None


class TestThePurgesKeepList:
    def _task(self):
        return db.Task(id=7, status="running", source_type="email", user_id="alice", prompt="x")

    def test_keep_spares_only_what_it_names(self, tmp_path):
        from istota.scheduler_deferred import _purge_deferred_files_for_retry

        for suffix in ("email_output", "kv_ops", "user_alerts"):
            (tmp_path / f"task_7_{suffix}.json").write_text("[]")
        _purge_deferred_files_for_retry(self._task(), tmp_path, keep={"email_output"})
        assert _left(tmp_path, self._task()) == {"email_output"}

    def test_a_retry_keeps_nothing(self, tmp_path):
        from istota.scheduler_deferred import _purge_deferred_files_for_retry

        for suffix in ("email_output", "kv_ops"):
            (tmp_path / f"task_7_{suffix}.json").write_text("[]")
        _purge_deferred_files_for_retry(self._task(), tmp_path)
        assert _left(tmp_path, self._task()) == set()

    def test_the_log_names_the_task_and_the_kinds(self, tmp_path, caplog):
        from istota.scheduler_deferred import _purge_deferred_files_for_retry

        (tmp_path / "task_7_kv_ops.json").write_text("[]")
        with caplog.at_level(logging.INFO, logger="istota.scheduler"):
            _purge_deferred_files_for_retry(self._task(), tmp_path)
        lines = [r.getMessage() for r in caplog.records if "kv_ops" in r.getMessage()]
        assert len(lines) == 1
        assert "task 7" in lines[0]
