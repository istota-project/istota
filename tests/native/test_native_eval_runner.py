"""The native-brain eval runner, offline (stage 1 of the pi 1.x round).

The live suite in `tests/live/native_eval/` spends money and runs by hand, so
nothing would notice its harness rotting between runs. This drives
`run_case` with `ReplayProvider` over a scripted two-turn transcript, through
the real SSE parser, the real tool server and the real session log, and checks
every metric the report carries.
"""

import json
import subprocess
import sys

from istota.config import NativeBrainConfig
from istota.llm.replay import ReplayProvider, read_fixture, write_fixture
from istota.session.compaction import derive_reserve_tokens

from ..live.native_eval import cases as eval_cases
from ..live.native_eval import runner

MARKER = "REPLAY-MARKER-55c2"


def _usage(prompt, completion, cached, cost):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "cost": cost,
    }


def _tool_turn() -> list[str]:
    edit_args = json.dumps(
        {"file_path": "notes.txt", "old_string": "not in the file", "new_string": "x"}
    )
    read_args = json.dumps({"file_path": "notes.txt"})
    first = {
        "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_edit", "function": {"name": "Edit", "arguments": edit_args}},
        ]}}]
    }
    second = {
        "choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_read", "function": {"name": "Read", "arguments": read_args}},
        ]}}]
    }
    done = {
        "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
        "usage": _usage(1000, 50, 600, 0.0012),
    }
    return [
        f"data: {json.dumps(first)}",
        f"data: {json.dumps(second)}",
        f"data: {json.dumps(done)}",
        "data: [DONE]",
    ]


def _answer_turn() -> list[str]:
    text = {"choices": [{"delta": {"content": f"The marker is {MARKER}."}}]}
    done = {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": _usage(1200, 20, 800, 0.0008),
    }
    return [f"data: {json.dumps(text)}", f"data: {json.dumps(done)}", "data: [DONE]"]


class _Sequence:
    """One `ReplayProvider` per model call, in order."""

    def __init__(self, providers):
        self._providers = list(providers)
        self.calls = 0

    async def stream(self, system_prompt, messages, tools, **kwargs):
        provider = self._providers[self.calls]
        self.calls += 1
        async for event in provider.stream(system_prompt, messages, tools, **kwargs):
            yield event


def _replay(tmp_path):
    first = tmp_path / "sse" / "turn1.jsonl"
    second = tmp_path / "sse" / "turn2.jsonl"
    write_fixture(first, _tool_turn())
    write_fixture(second, _answer_turn())
    return _Sequence([ReplayProvider(first), ReplayProvider(second)])


def _case(tmp_path, **kw) -> runner.EvalCase:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "notes.txt").write_text(f"first line\n{MARKER}\nlast line\n")
    defaults = dict(
        name="replayed",
        fixture=fixture,
        prompt="Find the marker in notes.txt.",
        timeout=60,
        check_fn=lambda workdir, text: (MARKER in text, "marker in answer"),
        success_marker=MARKER,
    )
    defaults.update(kw)
    return runner.EvalCase(**defaults)


def _config() -> NativeBrainConfig:
    return NativeBrainConfig(model="replay/model", base_url="http://replay.invalid")


class TestRunCaseMetrics:
    def _run(self, tmp_path, **case_kw):
        provider = _replay(tmp_path)
        result = runner.run_case(
            _case(tmp_path, **case_kw),
            _config(),
            tmp_path / "repo",
            provider=provider,
        )
        assert provider.calls == 2
        return result

    def test_every_metric_is_filled_from_the_run(self, tmp_path):
        result = self._run(tmp_path)
        assert result.case == "replayed"
        assert result.passed is True
        assert result.check_detail == "marker in answer"
        assert result.stop_reason == "completed"
        assert result.success is True
        assert MARKER in result.result_text
        assert result.turns == 2
        assert result.tool_calls == 2
        assert result.tool_errors == 1
        assert result.edit_failures == 1
        assert result.tool_errors_before_success == 1
        assert result.compactions == 0
        # prompt_tokens includes cached reads; the report's input is the rest.
        assert result.input_tokens == (1000 - 600) + (1200 - 800)
        assert result.output_tokens == 70
        assert result.cache_read_tokens == 1400
        assert result.cache_write_tokens == 0
        assert abs(result.cache_hit_rate - 1400 / 2200) < 1e-9
        assert abs(result.cost_usd - 0.002) < 1e-9
        assert result.wall_seconds > 0
        assert result.session_log and result.session_log.endswith(".jsonl")
        assert result.error == ""

    def test_the_result_serializes_to_the_report_shape(self, tmp_path):
        row = self._run(tmp_path).to_dict()
        json.dumps(row)
        for key in runner.METRIC_FIELDS:
            assert key in row, key

    def test_a_failed_check_is_a_metric_not_an_exception(self, tmp_path):
        result = self._run(
            tmp_path, check_fn=lambda workdir, text: (False, "wrong answer")
        )
        assert result.passed is False
        assert result.check_detail == "wrong answer"

    def test_too_few_compactions_fail_the_case(self, tmp_path):
        result = self._run(tmp_path, min_compactions=1)
        assert result.passed is False
        assert "compactions" in result.check_detail

    def test_a_check_argv_runs_in_the_workdir(self, tmp_path):
        argv = [sys.executable, "-c", "import pathlib, sys; "
                "sys.exit(0 if pathlib.Path('notes.txt').exists() else 3)"]
        result = self._run(tmp_path, check_fn=None, check_argv=argv)
        assert result.passed is True

    def test_a_failed_bash_command_counts_as_a_tool_error(self, tmp_path):
        bash_args = json.dumps({"command": "echo nope; exit 1"})
        call = {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_bash", "function": {"name": "Bash", "arguments": bash_args}},
        ]}}]}
        done = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": _usage(1000, 50, 0, 0.001)}
        first = tmp_path / "sse" / "bash.jsonl"
        second = tmp_path / "sse" / "answer.jsonl"
        write_fixture(first, [f"data: {json.dumps(call)}", f"data: {json.dumps(done)}", "data: [DONE]"])
        write_fixture(second, _answer_turn())
        provider = _Sequence([ReplayProvider(first), ReplayProvider(second)])
        result = runner.run_case(_case(tmp_path), _config(), tmp_path / "repo", provider=provider)
        assert result.tool_calls == 1
        assert result.tool_errors == 1
        assert result.edit_failures == 0

    def test_the_model_never_sees_this_checkout(self, tmp_path):
        checkout = str(runner.Path(runner.__file__).resolve().parents[3])
        assert checkout not in runner._prompt_text(f"run {runner.PYTHON_PLACEHOLDER} -m pytest")
        workdir = tmp_path / "repo"
        runner.prepare_workdir(_case(tmp_path), workdir)
        env = runner.task_env(workdir)
        assert checkout not in env["PATH"]
        out = subprocess.run(
            ["python", "-c", "import pytest; print('ok')"],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        assert out.stdout.strip() == "ok", out.stderr

    def test_the_sse_tee_records_every_call_replayably(self, tmp_path):
        provider = _replay(tmp_path)
        out = tmp_path / "recorded.jsonl"
        for sub in provider._providers:
            runner.tee_sse(sub._parser, out)
        runner.run_case(_case(tmp_path), _config(), tmp_path / "repo", provider=provider)
        assert read_fixture(out) == _tool_turn() + _answer_turn()


class TestTheFiveCases:
    def test_there_are_five_with_distinct_names(self):
        names = [case.name for case in eval_cases.CASES]
        assert names == [
            "fix_failing_test",
            "multi_site_edit",
            "answer_from_code",
            "long_context",
            "recover_from_failure",
        ]

    def test_each_prepares_a_workdir(self, tmp_path):
        for case in eval_cases.CASES:
            workdir = tmp_path / case.name
            runner.prepare_workdir(case, workdir)
            assert any(workdir.iterdir()), case.name

    def test_the_crlf_file_is_crlf_after_preparation(self, tmp_path):
        case = eval_cases.by_name("multi_site_edit")
        runner.prepare_workdir(case, tmp_path / "w")
        for name in case.crlf_files:
            data = (tmp_path / "w" / name).read_bytes()
            assert b"\r\n" in data
            assert data.count(b"\n") == data.count(b"\r\n")

    def test_long_context_crosses_the_forced_threshold(self, tmp_path):
        case = eval_cases.by_name("long_context")
        window = case.config_overrides["context_window"]
        runner.prepare_workdir(case, tmp_path / "w")
        chars = sum(p.stat().st_size for p in (tmp_path / "w").rglob("*.txt"))
        assert chars // 4 > window - derive_reserve_tokens(window)
        assert case.min_compactions >= 1

    def test_the_unmodified_fixtures_fail_their_checks(self, tmp_path):
        """A check that passes on the untouched fixture measures nothing."""
        for case in eval_cases.CASES:
            workdir = tmp_path / case.name
            runner.prepare_workdir(case, workdir)
            passed, detail = runner.run_check(case, workdir, "", env=runner.task_env(workdir))
            assert passed is False, (case.name, detail)

    def test_the_tool_grant_is_the_six_core_tools(self):
        assert runner.core_tool_names() == ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


class TestBudget:
    def test_the_first_case_always_runs(self):
        assert runner.budget_allows({"results": []}, 5.0)[0] is True

    def test_a_case_that_would_cross_the_cap_is_refused(self):
        report = {"results": [{"cost_usd": 2.0}, {"cost_usd": 2.5}]}
        allowed, reason = runner.budget_allows(report, 5.0)
        # 4.5 spent, and the dearest case so far was 2.5.
        assert allowed is False
        assert "4.5" in reason

    def test_room_for_the_dearest_case_runs(self):
        report = {"results": [{"cost_usd": 0.1}, {"cost_usd": 0.2}]}
        assert runner.budget_allows(report, 5.0)[0] is True

    def test_a_missing_cost_counts_as_zero(self):
        report = {"results": [{"cost_usd": None}, {}]}
        assert runner.report_cost(report) == 0.0


class TestEnvFile:
    def test_key_value_lines(self, tmp_path):
        path = tmp_path / "eval.env"
        path.write_text(
            "# a comment\n\nISTOTA_EVAL_MODEL=some/model\n"
            "export ISTOTA_BRAIN_NATIVE_API_KEY = 'sk-test'\nnot a pair\n"
        )
        assert runner.load_env_file(path) == {
            "ISTOTA_EVAL_MODEL": "some/model",
            "ISTOTA_BRAIN_NATIVE_API_KEY": "sk-test",
        }

    def test_the_task_env_carries_no_key(self, tmp_path):
        env = runner.task_env(tmp_path)
        assert "PATH" in env
        assert not any("KEY" in name or "TOKEN" in name for name in env)
