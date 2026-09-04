"""Per-room brain selection: the columns and the resolution order.

A room carries a standing brain default (`rooms.brain`); the answer for one
task is frozen on `tasks.brain` at creation so a mid-flight edit to the room
cannot change what a running task is. Resolution is

    tasks.brain  >  brain.source_type_overrides[source_type]  >  brain.kind

with the room override admitted only when it names a kind `make_brain` can
build *and* the operator listed it in `[brain] room_selectable`. Both refusals
log and fall through to the source-type layer, exactly as an unknown
`source_type_overrides` target already does — a routing typo must never wedge a
task.

An admitted override also turns availability failover off for that task
(`fallback` cleared). The room named a brain; a task that cannot run on it
fails with the real reason rather than answering from a different model. A
*refused* override leaves the config alone and keeps whatever failover it had,
which is what makes the switch track admission rather than the column merely
being populated.

Stage 1 of the per-room-brain-selection spec. There is no user-facing writer
yet — the column is set by SQL here, which is what these tests do.
"""

import io
import logging
from unittest.mock import patch

import pytest

from istota import db
from istota.brain import (
    KNOWN_BRAIN_KINDS,
    effective_fallback_kind,
    make_brain,
    reachable_brain_kinds,
    resolve_brain_kind,
    room_selectable_kinds,
)
from istota.brain.native import NativeBrain
from istota.config import (
    BrainConfig,
    Config,
    NativeBrainConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.brain._types import BrainRequest, BrainResult
from istota.transport.ingest import record_inbound


class _RecordingBrain:
    """Stands in for whatever `make_brain` would have returned.

    Same shape as the double in `tests/test_executor_brain_identity.py`: the
    executor only needs a brain that resolves names and answers, and building a
    real one would drag a provider into a routing test.
    """

    model_namespace = "anthropic"
    supports_steering = False

    def __init__(self):
        self.requests: list[BrainRequest] = []

    def resolve_model_name(self, name):
        return name

    def validate_alias_override(self, name, target):
        return []

    def execute(self, req: BrainRequest) -> BrainResult:
        self.requests.append(req)
        return BrainResult(success=True, result_text="ok", stop_reason="completed")


def _brain(kind="claude_code", overrides=None, selectable=None, fallback=""):
    return BrainConfig(
        kind=kind,
        native=NativeBrainConfig(model="claude-sonnet-4-6"),
        source_type_overrides=overrides or {},
        room_selectable=list(selectable or []),
        fallback=fallback,
    )


def _warnings(fn):
    """Run ``fn`` capturing WARNING output from the brain logger."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("istota.brain")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        result = fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return result, stream.getvalue()


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
    cfg.talk = TalkConfig(enabled=True, bot_username="istota")
    cfg.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="p",
    )
    cfg.scheduler = SchedulerConfig()
    cfg.users = {"alice": UserConfig()}
    cfg.brain = _brain("claude_code", selectable=["native", "tmux_claude"])
    return cfg


# =============================================================================
# The end-to-end claim: a room's brain reaches the task and resolves
# =============================================================================

class TestRoomBrainReachesTheTask:
    def test_room_brain_fills_the_task_and_resolves_to_that_kind(
        self, config, db_path,
    ):
        """The headline: a message in a room with `rooms.brain` set creates a
        task carrying that kind, and resolving from that task row yields it."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
            db.set_room_brain(conn, "room1", "native")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hi", source_type="web",
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)

        assert task.brain == "native"
        routed = resolve_brain_kind(
            task.source_type, config.brain, override=task.brain,
        )
        assert routed.kind == "native"
        assert isinstance(make_brain(routed), NativeBrain)

    def test_a_room_with_no_brain_leaves_the_task_null(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.add_room_binding(conn, "room1", "web", "room1")
        with db.get_db(db_path) as conn:
            _tok, task_id = record_inbound(
                conn, config, surface="web", surface_ref="room1",
                user_id="alice", text="hi", source_type="web",
            )
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
        assert task.brain is None
        assert resolve_brain_kind(
            task.source_type, config.brain, override=task.brain,
        ) is config.brain


# =============================================================================
# DB layer — the two columns
# =============================================================================

class TestBrainColumns:
    def test_new_room_has_no_brain(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            assert db.get_room(conn, "room1").brain is None

    def test_set_room_brain_roundtrip_and_clear(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_brain(conn, "room1", "native")
            assert db.get_room(conn, "room1").brain == "native"
            db.set_room_brain(conn, "room1", None)
            assert db.get_room(conn, "room1").brain is None

    def test_set_room_brain_leaves_the_model_pin_alone(self, db_path):
        """The brain column is its own knob at the DB layer. Clearing a model
        pin on a namespace change is a decision the *command* layer makes."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(conn, "room1", "claude-opus-4-8", "high")
            db.set_room_brain(conn, "room1", "native")
            room = db.get_room(conn, "room1")
        assert room.brain == "native"
        assert room.model == "claude-opus-4-8"
        assert room.effort == "high"

    def test_task_brain_roundtrips_through_create_and_read(self, db_path):
        """`_TASK_COLUMNS` / `_row_to_task` / `create_task` move together: a
        column named in the first but missing from a SELECT raises IndexError
        rather than reading as None."""
        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="p", user_id="alice", source_type="talk",
                conversation_token="room1", brain="native",
            )
            assert db.get_task(conn, tid).brain == "native"
            listed = [t for t in db.list_tasks(conn, user_id="alice")]
        assert [t.brain for t in listed] == ["native"]

    def test_task_brain_defaults_to_null(self, db_path):
        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="p", user_id="alice", source_type="talk",
            )
            assert db.get_task(conn, tid).brain is None


# =============================================================================
# resolve_brain_kind — the override layer
# =============================================================================

class TestOverrideAdmission:
    def test_override_beats_a_source_type_override(self):
        base = _brain(
            "claude_code",
            overrides={"talk": "tmux_claude"},
            selectable=["native"],
        )
        assert resolve_brain_kind("talk", base, override="native").kind == "native"

    def test_override_none_reproduces_the_source_type_answer(self):
        base = _brain("claude_code", overrides={"talk": "native"})
        assert resolve_brain_kind("talk", base, override=None).kind == "native"
        assert resolve_brain_kind("web", base, override=None) is base

    def test_blank_override_is_no_override(self):
        base = _brain("claude_code", overrides={"talk": "native"})
        assert resolve_brain_kind("talk", base, override="  ").kind == "native"

    def test_override_is_stripped(self):
        base = _brain("claude_code", selectable=["native"])
        assert resolve_brain_kind("talk", base, override="  native ").kind == "native"

    def test_override_equal_to_the_base_kind_is_still_admitted(self):
        """Pinning the deployment's own kind counts as pinning — the rule has
        no exception, which is what makes it explainable."""
        base = _brain("claude_code", selectable=["claude_code"], fallback="native")
        routed = resolve_brain_kind("talk", base, override="claude_code")
        assert routed is not base
        assert routed.kind == "claude_code"


class TestOverrideRefusals:
    def test_an_unknown_kind_falls_through_and_warns(self):
        base = _brain("claude_code", overrides={"talk": "native"}, selectable=["native"])
        routed, logged = _warnings(
            lambda: resolve_brain_kind("talk", base, override="bogus"),
        )
        assert routed.kind == "native"  # the source-type answer, not wedged
        assert "bogus" in logged

    def test_a_known_but_unallowlisted_kind_falls_through_and_warns(self):
        """A distinct branch from the unknown one: the kind is real, the
        operator has not offered it. This is what makes an operator shortening
        `room_selectable` take effect at the next dispatch with no sweep."""
        base = _brain("claude_code", overrides={"talk": "tmux_claude"}, selectable=[])
        routed, logged = _warnings(
            lambda: resolve_brain_kind("talk", base, override="native"),
        )
        assert routed.kind == "tmux_claude"  # the source-type answer
        assert "native" in logged

    def test_a_refused_override_returns_the_base_object_when_nothing_else_applies(self):
        base = _brain("claude_code", selectable=[])
        assert resolve_brain_kind("talk", base, override="native") is base


# =============================================================================
# D12 — an admitted override turns failover off
# =============================================================================

class TestPinnedRoomHasNoFailover:
    def test_admission_clears_a_configured_fallback(self):
        base = _brain("claude_code", selectable=["native"], fallback="tmux_claude")
        # The precondition: this deployment does have failover to lose.
        assert effective_fallback_kind(base) == "tmux_claude"
        routed = resolve_brain_kind("talk", base, override="native")
        assert routed.fallback == ""
        assert effective_fallback_kind(routed) is None

    def test_a_tmux_override_has_no_implicit_claude_code_fallback(self):
        """The regression guard for ISSUE-362 staying deleted. `tmux_claude`
        used to reach `claude_code` here with nothing configured, so clearing
        `fallback` alone would not have been enough."""
        base = _brain("claude_code", selectable=["tmux_claude"], fallback="native")
        routed = resolve_brain_kind("talk", base, override="tmux_claude")
        assert routed.kind == "tmux_claude"
        assert effective_fallback_kind(routed) is None

    def test_pinning_the_base_kind_also_disables_failover(self):
        base = _brain("claude_code", selectable=["claude_code"], fallback="native")
        routed = resolve_brain_kind("talk", base, override="claude_code")
        assert effective_fallback_kind(routed) is None

    def test_a_refused_override_keeps_its_failover(self):
        """The converse that proves the switch tracks *admission* rather than
        the column merely being populated."""
        base = _brain("claude_code", selectable=[], fallback="native")
        routed = resolve_brain_kind("talk", base, override="native")
        assert effective_fallback_kind(routed) == "native"

        unknown = resolve_brain_kind("talk", base, override="bogus")
        assert effective_fallback_kind(unknown) == "native"

    def test_the_source_type_layer_still_inherits_fallback(self):
        """Unchanged for an unpinned task: a `source_type_overrides` routing
        carries `fallback` through as it always has."""
        base = _brain("claude_code", overrides={"talk": "native"}, fallback="claude_code")
        routed = resolve_brain_kind("talk", base)
        assert effective_fallback_kind(routed) == "claude_code"


# =============================================================================
# room_selectable_kinds
# =============================================================================

class TestRoomSelectableKinds:
    def test_empty_by_default(self):
        assert room_selectable_kinds(_brain("claude_code")) == frozenset()

    def test_intersects_against_known_kinds(self):
        base = _brain("claude_code", selectable=["native", "bogus"])
        assert room_selectable_kinds(base) == frozenset({"native"})

    def test_it_does_not_drop_the_deployments_fallback_kind(self):
        """An earlier design excluded it. Under D12 a pinned room has no
        failover, so there is nothing for it to collide with."""
        base = _brain("claude_code", selectable=["native"], fallback="native")
        assert "native" in room_selectable_kinds(base)

    def test_entries_are_stripped_and_blanks_dropped(self):
        base = _brain("claude_code", selectable=["  native  ", "", "   "])
        assert room_selectable_kinds(base) == frozenset({"native"})

    def test_it_never_raises_on_a_malformed_value(self):
        base = _brain("claude_code")
        base.room_selectable = "native"  # not a list
        assert room_selectable_kinds(base) == frozenset()
        base.room_selectable = None
        assert room_selectable_kinds(base) == frozenset()

    def test_every_offered_kind_is_buildable(self):
        base = _brain("claude_code", selectable=sorted(KNOWN_BRAIN_KINDS))
        assert room_selectable_kinds(base) == frozenset(KNOWN_BRAIN_KINDS)


# =============================================================================
# reachable_brain_kinds
# =============================================================================

class TestReachableBrainKinds:
    """The set `doctor` asks instead of `brain.kind`.

    Three sources contribute directly — the base kind, the
    `source_type_overrides` targets and `room_selectable` — and the configured
    fallback is folded over the first two only, since an admitted room override
    clears `fallback`.
    """

    def test_a_default_config_reaches_only_its_own_kind(self):
        assert reachable_brain_kinds(_brain("claude_code")) == frozenset({"claude_code"})

    def test_a_source_type_override_target_is_reachable(self):
        """The case the spec's test strategy names, with the value the code
        actually yields.

        The spec asked for this pair on the grounds that `claude_code` would
        arrive "by way of the tmux back-compat default" and that a single fold
        over the base config would therefore miss it. ISSUE-362 deleted that
        default, so `claude_code` is in this set as the base kind and for no
        other reason, and with `fallback` unset the fold contributes nothing at
        all. The pair is kept because it is still the plainest statement that a
        routing target is reachable; the fold's own shape is pinned below,
        where it can actually fail.
        """
        base = _brain("claude_code", overrides={"scheduled": "tmux_claude"})
        assert reachable_brain_kinds(base) == frozenset({"claude_code", "tmux_claude"})

    def test_an_unknown_override_target_is_not_reachable(self):
        """`resolve_brain_kind` logs and ignores it, so no task runs it."""
        base = _brain("claude_code", overrides={"scheduled": "bogus"})
        assert reachable_brain_kinds(base) == frozenset({"claude_code"})

    def test_a_configured_fallback_is_reachable(self):
        base = _brain("claude_code", fallback="native")
        assert reachable_brain_kinds(base) == frozenset({"claude_code", "native"})

    def test_a_self_fallback_contributes_nothing(self):
        base = _brain("native", fallback="native")
        assert reachable_brain_kinds(base) == frozenset({"native"})

    def test_room_selectable_kinds_are_reachable(self):
        base = _brain("native", selectable=["claude_code"])
        assert reachable_brain_kinds(base) == frozenset({"native", "claude_code"})

    def test_an_unbuildable_room_selectable_entry_is_not_reachable(self):
        base = _brain("native", selectable=["bogus"])
        assert reachable_brain_kinds(base) == frozenset({"native"})

    def test_the_fallback_fold_runs_per_kind_and_skips_room_selectable(self):
        """Which kinds the fold is applied to, asserted directly.

        Deliberately white-box, because the two properties the stage names are
        both unobservable through the return value as the code stands. There is
        one configured `fallback` string, and `effective_fallback_kind` drops it
        exactly for the kind it equals — the kind that is already contributing
        itself — so a single evaluation against the base config and a per-kind
        fold produce the identical set for every possible config, and so does
        folding `room_selectable` in or leaving it out. That is arithmetic
        rather than design: a fallback rule that branches on kind again (the one
        ISSUE-362 removed did) breaks a single evaluation silently and leaves a
        `doctor` check SKIPped on a deployment that needed it. So the guard is
        on the call, which is the thing that would be rewritten.
        """
        import istota.brain as brain_pkg

        seen = []

        def _recording(brain_config):
            seen.append(getattr(brain_config, "kind", None))
            return None

        base = _brain(
            "claude_code",
            overrides={"scheduled": "native"},
            selectable=["tmux_claude"],
            fallback="native",
        )
        with patch.object(brain_pkg, "effective_fallback_kind", _recording):
            reachable_brain_kinds(base)
        assert sorted(seen) == ["claude_code", "native"]
        assert "tmux_claude" not in seen

    def test_it_never_raises_on_a_malformed_config(self):
        assert reachable_brain_kinds(None) == frozenset()

        base = _brain("claude_code")
        base.source_type_overrides = "scheduled=native"  # not a dict
        assert reachable_brain_kinds(base) == frozenset({"claude_code"})


# =============================================================================
# The executor call sites read the task's pin
# =============================================================================

class TestExecutorCallSites:
    """`_build_triage_completer` has its own coverage in
    `tests/native/test_triage_completer.py`; this covers the eager-skill probe,
    the third of the three sites."""

    def _task(self, brain=None, source_type="talk"):
        return db.Task(
            id=1, status="pending", source_type=source_type, user_id="alice",
            prompt="hi", brain=brain,
        )

    def test_a_native_pin_turns_the_web_fetch_probe_on(self, config):
        from istota.executor import _native_web_fetch_enabled

        assert config.brain.kind == "claude_code"
        assert _native_web_fetch_enabled(self._task(), config) is False
        assert _native_web_fetch_enabled(self._task(brain="native"), config) is True

    def test_execute_task_builds_the_pinned_brain_with_no_failover(self, tmp_path):
        """The third call site, and the one that decides which brain actually
        runs. Nothing else asserts on it: the two probes above are read-only
        helpers, and `dry_run` returns before this resolution happens.

        `make_brain` is patched to record the config it was handed, so the
        assertion is on the routing rather than on the brain object.
        """
        from istota.config import SecurityConfig

        cfg = Config(
            db_path=tmp_path / "exec.db",
            skills_dir=tmp_path / "cfg" / "skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            model="claude-sonnet-4-6",
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        cfg.skills_dir.mkdir(parents=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        db.init_db(cfg.db_path)
        # A deployment that does have failover, so clearing it is observable.
        cfg.brain = _brain(
            "claude_code", selectable=["native"], fallback="tmux_claude",
        )

        handed: list = []

        def _record(brain_config):
            handed.append(brain_config)
            return _RecordingBrain()

        with db.get_db(cfg.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="do the thing", user_id="alice",
                source_type="talk", conversation_token="a1b2c3d4",
                brain="native",
            )
            task = db.get_task(conn, task_id)
            with patch("istota.executor.make_brain", side_effect=_record):
                from istota.executor import execute_task

                execute_task(task, cfg, [], conn=conn)

        assert handed, "make_brain was never called"
        routed = handed[-1]
        assert routed.kind == "native"
        # D12: the pin took the deployment's failover with it.
        assert routed.fallback == ""
        assert effective_fallback_kind(routed) is None

    def test_execute_task_keeps_failover_for_an_unpinned_task(self, tmp_path):
        """The converse, on the same harness: without a pin the deployment's
        configured fallback survives to the executor."""
        from istota.config import SecurityConfig

        cfg = Config(
            db_path=tmp_path / "exec.db",
            skills_dir=tmp_path / "cfg" / "skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            model="claude-sonnet-4-6",
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        cfg.skills_dir.mkdir(parents=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        db.init_db(cfg.db_path)
        cfg.brain = _brain(
            "claude_code", selectable=["native"], fallback="tmux_claude",
        )

        handed: list = []

        def _record(brain_config):
            handed.append(brain_config)
            return _RecordingBrain()

        with db.get_db(cfg.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="do the thing", user_id="alice",
                source_type="talk", conversation_token="a1b2c3d4",
            )
            task = db.get_task(conn, task_id)
            with patch("istota.executor.make_brain", side_effect=_record):
                from istota.executor import execute_task

                execute_task(task, cfg, [], conn=conn)

        assert handed, "make_brain was never called"
        assert effective_fallback_kind(handed[-1]) == "tmux_claude"

    def test_a_claude_code_pin_turns_it_off_over_a_native_lane_rule(self, config):
        from istota.executor import _native_web_fetch_enabled

        config.brain = _brain(
            "claude_code",
            overrides={"talk": "native"},
            selectable=["claude_code"],
        )
        assert _native_web_fetch_enabled(self._task(), config) is True
        assert (
            _native_web_fetch_enabled(self._task(brain="claude_code"), config)
            is False
        )
