"""Effort clamping: an effort is checked against what the model supports.

`brain._resolve.clamp_effort_level` and `clamp_effort` are the rule (pi's):
`low`, `medium` and `high` are supported by every reasoning model, `xhigh` and
`max` only where the model's `effort_map` names them, and a level mapped to
`None` is not supported. An unsupported level moves up to the nearest supported
one first, then down (D8). A model without reasoning gets no effort. The wire
value is the map's entry when it has one, else the level name.

Then the seams: the CLI brains pass the clamped level to `--effort`, the native
brain sends the clamped wire value as `reasoning_effort`, both report what was
asked and what ran, and the executor stores the requested level while the
trace records both when they differ.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import db
from istota.brain import BrainRequest, BrainResult, make_brain
from istota.brain._aliases import EFFORT_LEVELS
from istota.brain._resolve import EFFORT_ORDER, clamp_effort, clamp_effort_level
from istota.config import BrainConfig, ClaudeCodeBrainConfig, Config, SecurityConfig
from istota.llm.catalog import ModelInfo
from istota.usage.telemetry import BrainUsage


def _info(reasoning=True, effort_map=None):
    return ModelInfo(
        id="m", context_window=1000, max_output_tokens=100, provider="p",
        reasoning=reasoning, effort_map=effort_map or {},
    )


TOPS_AT_HIGH = _info()
WITH_XHIGH_AND_MAX = _info(effort_map={"xhigh": "xhigh", "max": "max"})
WITH_MAX_ONLY = _info(effort_map={"max": "max"})
MEDIUM_FLOOR = _info(effort_map={"low": None})
NO_REASONING = _info(reasoning=False)


class TestTheLevel:
    def test_the_order_walked_is_every_effort_level(self):
        assert set(EFFORT_ORDER) == set(EFFORT_LEVELS)

    @pytest.mark.parametrize("level", ["low", "medium", "high"])
    def test_the_three_base_levels_are_supported_by_every_reasoning_model(self, level):
        assert clamp_effort_level(TOPS_AT_HIGH, level) == level

    def test_xhigh_on_a_model_that_tops_out_at_high_gives_high(self):
        assert clamp_effort_level(TOPS_AT_HIGH, "xhigh") == "high"

    def test_max_on_a_model_that_tops_out_at_high_gives_high(self):
        assert clamp_effort_level(TOPS_AT_HIGH, "max") == "high"

    def test_xhigh_and_max_pass_where_the_map_names_them(self):
        assert clamp_effort_level(WITH_XHIGH_AND_MAX, "xhigh") == "xhigh"
        assert clamp_effort_level(WITH_XHIGH_AND_MAX, "max") == "max"

    def test_an_unsupported_level_moves_up_before_down(self):
        # `xhigh` is not named, `max` is: up first gives `max`, not `high`.
        assert clamp_effort_level(WITH_MAX_ONLY, "xhigh") == "max"

    def test_a_level_mapped_to_none_clamps_up_to_the_floor(self):
        assert clamp_effort_level(MEDIUM_FLOOR, "low") == "medium"

    def test_a_none_entry_removes_a_named_top_level(self):
        info = _info(effort_map={"xhigh": None, "max": "max"})
        assert clamp_effort_level(info, "xhigh") == "max"

    def test_down_when_nothing_above_is_supported(self):
        info = _info(effort_map={"high": None})
        assert clamp_effort_level(info, "high") == "medium"

    def test_no_supported_level_at_all_gives_none(self):
        info = _info(effort_map={"low": None, "medium": None, "high": None})
        assert clamp_effort_level(info, "medium") is None

    def test_a_model_without_reasoning_gets_no_effort(self):
        assert clamp_effort_level(NO_REASONING, "high") is None

    @pytest.mark.parametrize("requested", ["", None])
    def test_nothing_requested_is_nothing(self, requested):
        assert clamp_effort_level(TOPS_AT_HIGH, requested) is None

    def test_a_level_is_read_case_insensitively(self):
        assert clamp_effort_level(TOPS_AT_HIGH, "XHigh") == "high"

    def test_an_unknown_level_is_not_sent(self):
        assert clamp_effort_level(TOPS_AT_HIGH, "turbo") is None

    def test_an_unknown_model_is_not_checked(self):
        # Nothing to check against: the brain sends what was asked, as before.
        assert clamp_effort_level(None, "xhigh") == "xhigh"


class TestTheWireValue:
    def test_the_level_name_when_the_map_has_no_entry(self):
        assert clamp_effort(TOPS_AT_HIGH, "medium") == "medium"

    def test_the_map_entry_when_it_has_one(self):
        info = _info(effort_map={"high": "high-thinking", "max": "budget-max"})
        assert clamp_effort(info, "high") == "high-thinking"
        assert clamp_effort(info, "max") == "budget-max"

    def test_the_wire_value_of_the_clamped_level(self):
        info = _info(effort_map={"high": "H"})
        assert clamp_effort(info, "xhigh") == "H"

    def test_no_effort_for_a_model_without_reasoning(self):
        assert clamp_effort(NO_REASONING, "low") is None


def _req(**kwargs) -> BrainRequest:
    fields = dict(
        prompt="hi", allowed_tools=[], cwd="/tmp", env={}, timeout_seconds=60,
        streaming=False,
    )
    fields.update(kwargs)
    return BrainRequest(**fields)


def _cli_argv(brain, req):
    with patch("istota.brain.claude_code.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        result = brain.execute(req)
    argv = run.call_args.args[0]
    return argv, result


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


class TestTheCliBrains:
    """The shipped catalogue: `claude-opus-4-5` names neither `xhigh` nor `max`."""

    def test_the_clamped_level_reaches_the_effort_flag(self):
        brain = make_brain(BrainConfig(kind="claude_code"))
        argv, result = _cli_argv(brain, _req(model="claude-opus-4-5", effort="xhigh"))
        assert _flag(argv, "--effort") == "high"
        assert result.effort_used == "high"
        assert result.effort_requested == "xhigh"

    def test_a_supported_level_passes_unchanged(self):
        brain = make_brain(BrainConfig(kind="claude_code"))
        argv, result = _cli_argv(brain, _req(model="claude-opus-4-7", effort="xhigh"))
        assert _flag(argv, "--effort") == "xhigh"
        assert result.effort_used == result.effort_requested == "xhigh"

    def test_the_brains_own_default_effort_is_clamped_too(self):
        brain = make_brain(
            BrainConfig(
                kind="claude_code",
                claude_code=ClaudeCodeBrainConfig(model="claude-opus-4-5", effort="max"),
            )
        )
        argv, result = _cli_argv(brain, _req())
        assert _flag(argv, "--effort") == "high"
        assert result.effort_requested == "max"

    def test_a_model_the_catalogue_does_not_know_is_sent_as_asked(self):
        brain = make_brain(BrainConfig(kind="claude_code"))
        argv, _ = _cli_argv(brain, _req(model="claude-opus-9-20990101", effort="xhigh"))
        assert _flag(argv, "--effort") == "xhigh"

    def test_the_tmux_brain_clamps_through_the_same_rule(self):
        from istota.brain.claude_code import ClaudeCodeBrain

        cli = ClaudeCodeBrain()
        clamped = cli.with_clamped_effort(_req(model="claude-opus-4-5", effort="max"))
        assert clamped.effort == "high"


class TestTheNativeBrain:
    def teardown_method(self):
        from istota.llm.catalog import set_model_overrides

        set_model_overrides({})

    def _run(self, tmp_path, model, effort, overrides):
        from istota.brain.native import NativeBrain
        from istota.config import NativeBrainConfig
        from istota.llm.catalog import set_model_overrides
        from istota.llm.types import AssistantMessage, TextContent

        from .native._mock_provider import MockProvider

        set_model_overrides(overrides)
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        brain = NativeBrain(NativeBrainConfig(model=model), provider=provider)
        result = brain.execute(_req(cwd=tmp_path, model=model, effort=effort))
        return provider.calls[0]["reasoning_effort"], result

    def test_xhigh_on_a_model_without_it_sends_high(self, tmp_path):
        wire, result = self._run(
            tmp_path, "z-ai/glm-5", "xhigh", {"z-ai/glm-5": {"reasoning": True}},
        )
        assert wire == "high"
        assert result.effort_used == "high"
        assert result.effort_requested == "xhigh"

    def test_the_map_value_is_what_reaches_the_wire(self, tmp_path):
        overrides = {"z-ai/glm-5": {"reasoning": True, "effort_map": {"max": "max"}}}
        wire, result = self._run(tmp_path, "z-ai/glm-5", "max", overrides)
        assert wire == "max"
        assert result.effort_used == "max"

    def test_a_model_without_reasoning_sends_none_and_reports_none(self, tmp_path):
        wire, result = self._run(tmp_path, "qwen-local", "high", {})
        assert wire is None
        assert result.effort_used == ""
        assert result.effort_requested == "high"


class _ClampingBrain:
    """A brain that ran `high` when `xhigh` was asked, as a clamp would."""

    model_namespace = "anthropic"
    kind = "claude_code"
    supports_steering = False
    default_model = ""
    default_effort = ""

    def __init__(self):
        self.requests: list[BrainRequest] = []

    def resolve_model_name(self, name):
        return name

    def resolve_alias(self, alias):
        return None

    def validate_alias_override(self, name, target):
        return []

    def execute(self, req: BrainRequest) -> BrainResult:
        self.requests.append(req)
        return BrainResult(
            success=True, result_text="ok", stop_reason="completed",
            execution_trace=json.dumps([{"type": "text", "text": "ok"}]),
            effort_requested=req.effort, effort_used="high", usage=BrainUsage(),
        )


def _execute(tmp_path, brain, **task_kwargs):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    skills_dir = tmp_path / "config" / "skills"
    skills_dir.mkdir(parents=True)
    config = Config(
        db_path=db_path,
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty_bundled",
        temp_dir=tmp_path / "temp",
        workspace_path=tmp_path / "workspace",
        security=SecurityConfig(skill_proxy_enabled=False),
    )
    (tmp_path / "temp" / "alice").mkdir(parents=True, exist_ok=True)
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn, prompt="do the thing", user_id="alice", source_type="talk",
            conversation_token="a1b2c3d4", **task_kwargs,
        )
        task = db.get_task(conn, task_id)
        with patch("istota.executor.make_brain", return_value=brain):
            from istota.executor import execute_task

            _ok, _result, _actions, trace = execute_task(task, config, [], conn=conn)
        stored = db.get_task(conn, task_id)
        usage = conn.execute(
            "SELECT effort FROM task_usage WHERE task_id = ?", (task_id,)
        ).fetchall()
    return stored, json.loads(trace), [row[0] for row in usage]


class TestTheExecutor:
    def test_the_requested_level_is_stored_and_the_clamped_one_ran(self, tmp_path):
        brain = _ClampingBrain()
        stored, trace, usage = _execute(
            tmp_path, brain, model="claude-opus-4-5", effort="xhigh",
        )
        assert brain.requests[0].effort == "xhigh"
        assert stored.effort == "xhigh"
        assert usage == ["high"]
        notes = [e for e in trace if e.get("type") == "effort"]
        assert notes == [
            {
                "type": "effort",
                "requested": "xhigh",
                "used": "high",
                "text": "Effort xhigh was clamped to high for this model.",
            }
        ]
        # First, so the composer's walk over the final turn never meets it.
        assert trace[0]["type"] == "effort"

    def test_no_note_when_the_level_ran_as_asked(self, tmp_path):
        class _Unclamped(_ClampingBrain):
            def execute(self, req):
                return BrainResult(
                    success=True, result_text="ok", stop_reason="completed",
                    execution_trace=json.dumps([{"type": "text", "text": "ok"}]),
                    effort_requested="high", effort_used="high",
                )

        _stored, trace, _usage = _execute(
            tmp_path, _Unclamped(), model="claude-opus-4-5", effort="high",
        )
        assert [e for e in trace if e.get("type") == "effort"] == []

    def test_a_dropped_effort_is_noted_with_the_level_asked(self, tmp_path):
        class _Dropped(_ClampingBrain):
            def execute(self, req):
                return BrainResult(
                    success=True, result_text="ok", stop_reason="completed",
                    execution_trace=None,
                    effort_requested="high", effort_used="",
                )

        _stored, trace, _usage = _execute(
            tmp_path, _Dropped(), model="qwen-local", effort="high",
        )
        assert trace[0] == {
            "type": "effort",
            "requested": "high",
            "used": "",
            "text": "Effort high was dropped: this model takes no effort.",
        }
