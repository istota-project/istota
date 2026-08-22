"""Stage 3: ClaudeCodeBrain's usage capture off the stream.

`subprocess.Popen` is patched to replay a fixture, following the fake-process
helper the existing streaming tests use.

The gate in this file is `TestNoSurfaceLeak`. The executor fans progress events
out to live surfaces, so a token-accounting frame reaching `req.on_progress` or
`execution_trace` would put an accounting record in a user's chat.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.brain._types import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain


def _mock_process(stdout_lines, returncode=0):
    mock = MagicMock()
    mock.stdout = iter(stdout_lines)
    mock.stderr = iter([])
    mock.pid = 4242
    mock.returncode = None

    def _wait(*_a, **_kw):
        mock.returncode = returncode
        return returncode

    mock.wait.side_effect = _wait
    mock.kill = MagicMock()
    mock.stdin = MagicMock()
    return mock


def _init(model="claude-haiku-4-5-20251001", api_key_source="ANTHROPIC_API_KEY"):
    return json.dumps({
        "type": "system",
        "subtype": "init",
        "model": model,
        "apiKeySource": api_key_source,
        "claude_code_version": "2.1.227",
    })


def _message_delta(
    *, input_tokens, cache_read, cache_write, output,
    parent_tool_use_id=None, context_management=None,
):
    frame = {
        "type": "stream_event",
        "parent_tool_use_id": parent_tool_use_id,
        "model": "claude-haiku-4-5-20251001",
        "event": {
            "type": "message_delta",
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": output,
            },
        },
    }
    if context_management is not None:
        frame["event"]["context_management"] = context_management
    return json.dumps(frame)


_RESULT = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 2,
    "duration_ms": 2916,
    "duration_api_ms": 3655,
    "result": "Done.",
    "total_cost_usd": 0.0319275,
    "usage": {"input_tokens": 17, "output_tokens": 147, "service_tier": "standard"},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 550,
            "outputTokens": 161,
            "cacheReadInputTokens": 14425,
            "cacheCreationInputTokens": 14565,
            "costUSD": 0.0319275,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
        }
    },
})

# The two main-agent requests of the measured capture.
_REQ_1 = _message_delta(input_tokens=9, cache_read=0, cache_write=14425, output=119)
_REQ_2 = _message_delta(
    input_tokens=8, cache_read=14425, cache_write=140, output=28
)

_TOOL_USE = json.dumps({
    "type": "assistant",
    "message": {
        "id": "msg_1",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "ls"}}
        ],
    },
})

FULL_STREAM = [_init(), _REQ_1, _TOOL_USE, _REQ_2, _RESULT]


def _run(lines, *, on_progress=None, returncode=0, tmp_path=None):
    req = BrainRequest(
        prompt="hi",
        allowed_tools=["Bash"],
        cwd=tmp_path or Path("/tmp"),
        env={},
        timeout_seconds=60,
        streaming=True,
        on_progress=on_progress,
    )
    with patch(
        "istota.brain.claude_code.subprocess.Popen",
        return_value=_mock_process(lines, returncode=returncode),
    ):
        return ClaudeCodeBrain().execute(req)


class TestFullReplay:
    def test_usage_is_populated_from_model_usage(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        assert result.usage is not None
        # From modelUsage, not result.usage (17/147).
        assert result.usage.billed_input_tokens == 550
        assert result.usage.output_tokens == 161
        assert result.usage.cache_read_tokens == 14425
        assert result.usage.cache_write_tokens == 14565
        assert result.usage.has_totals is True
        assert result.usage.totals_source == "model_usage"

    def test_context_measures_come_from_the_request_frames(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        assert result.usage.initial_context_tokens == 14434
        assert result.usage.peak_context_tokens == 14573
        assert result.usage.model_requests == 2
        assert result.usage.context_window == 200000

    def test_cost_basis_comes_from_the_init_frame(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)
        assert result.usage.cost_basis == "api"

        subscription = [_init(api_key_source="none")] + FULL_STREAM[1:]
        assert _run(subscription, tmp_path=tmp_path).usage.cost_basis == "subscription"

    def test_brain_kind_is_set(self, tmp_path):
        assert _run(FULL_STREAM, tmp_path=tmp_path).brain_kind == "claude_code"

    def test_the_per_model_split_is_carried(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        assert [m.model for m in result.usage.models] == [
            "claude-haiku-4-5-20251001"
        ]
        assert result.usage.models[0].billed_input_tokens == 550


class TestNoSurfaceLeak:
    def test_accounting_frames_never_reach_on_progress(self, tmp_path):
        """The executor fans these out to live surfaces. An accounting frame in
        a user's chat is a bug."""
        seen = []
        rate_limit = json.dumps({
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
        })

        result = _run(
            [_init(), rate_limit, _REQ_1, _TOOL_USE, _REQ_2, _RESULT],
            on_progress=seen.append,
            tmp_path=tmp_path,
        )

        kinds = {type(e).__name__ for e in seen}
        assert "RequestUsageEvent" not in kinds
        assert "RateLimitEvent" not in kinds
        # And the tool event that shares the stream did get through.
        assert "ToolUseEvent" in kinds
        assert result.usage.model_requests == 2

    def test_accounting_frames_never_reach_the_execution_trace(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        trace = json.loads(result.execution_trace or "[]")
        assert all(entry.get("type") != "usage" for entry in trace)
        # The tool entry survives — it is what would have been consumed if usage
        # were emitted from the `assistant` branch.
        assert any(entry.get("type") == "tool" for entry in trace)

    def test_the_rate_limit_posture_is_still_captured(self, tmp_path):
        info = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 99}
        rate_limit = json.dumps({"type": "rate_limit_event", "rate_limit_info": info})

        result = _run([_init(), rate_limit, _REQ_1, _RESULT], tmp_path=tmp_path)

        assert result.usage.rate_limit == info


class TestSubagentAndCompaction:
    def test_a_subagent_frame_is_excluded_from_the_peak(self, tmp_path):
        """Hand-built, not captured: fan-out is denied in production, so a real
        capture cannot produce one. The sub-agent's prompt deliberately exceeds
        every main-agent frame, so a peak that included it would be visibly
        wrong."""
        subagent = _message_delta(
            input_tokens=5, cache_read=0, cache_write=90000, output=10,
            parent_tool_use_id="toolu_1",
        )

        result = _run(
            [_init(), _REQ_1, subagent, _REQ_2, _RESULT], tmp_path=tmp_path
        )

        assert result.usage.peak_context_tokens == 14573
        assert result.usage.peak_context_tokens != 90005
        assert result.usage.subagent_requests == 1
        assert result.usage.model_requests == 2

    def test_a_compacted_frame_is_counted_not_measured(self, tmp_path):
        """A compaction replays the previous response; counting it as a request
        would inflate model_requests with nothing behind it."""
        compacted = _message_delta(
            input_tokens=5, cache_read=0, cache_write=100, output=10,
            context_management={"applied_edits": [{"type": "clear_tool_uses_20250919"}]},
        )

        result = _run(
            [_init(), _REQ_1, compacted, _REQ_2, _RESULT], tmp_path=tmp_path
        )

        assert result.usage.compacted_requests == 1
        assert result.usage.model_requests == 2

    def test_subagent_requests_reads_zero_on_an_ordinary_run(self, tmp_path):
        """Fan-out is denied today, so a non-zero value is itself a signal."""
        result = _run(FULL_STREAM, tmp_path=tmp_path)
        assert result.usage.subagent_requests == 0


class TestTruncatedStream:
    def test_no_result_frame_yields_context_but_no_totals(self, tmp_path):
        """A timeout or kill still spent tokens. One rule: a row is written
        whenever anything was measured, and has_totals says whether the token
        columns mean anything."""
        result = _run([_init(), _REQ_1, _REQ_2], returncode=1, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.usage.has_totals is False
        assert result.usage.billed_input_tokens == 0
        # Context is measured independently.
        assert result.usage.initial_context_tokens == 14434
        assert result.usage.peak_context_tokens == 14573
        assert result.success is False

    def test_a_stream_with_nothing_measurable_still_returns_a_usage(self, tmp_path):
        result = _run([_init()], returncode=1, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.usage.has_totals is False
        assert result.usage.initial_context_tokens is None

    def test_the_init_model_is_the_fallback_when_model_usage_is_absent(
        self, tmp_path
    ):
        result = _run([_init(model="claude-sonnet-5"), _REQ_1], tmp_path=tmp_path)

        assert result.usage.model == "claude-sonnet-5"


class TestRetryWrapperKeepsUsage:
    """The retry wrapper builds two `BrainResult`s of its own. They used to
    carry no usage, so a run that streamed real requests and then exhausted its
    retries recorded nothing — and that is the worst case to lose, because
    `transient_api_error` is in the executor's default fallback trigger set: the
    primary's spend would vanish and the fallback's would be the only tokens the
    task ever recorded."""

    _TRANSIENT = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "result": "API Error: 529 Overloaded",
    })

    def test_exhausted_retries_still_carry_the_last_attempt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "istota.brain.claude_code._interruptible_sleep", lambda *a, **k: False
        )
        stream = [_init(), _REQ_1, _REQ_2, self._TRANSIENT]
        processes = [_mock_process(list(stream)) for _ in range(3)]

        with patch(
            "istota.brain.claude_code.subprocess.Popen",
            side_effect=processes,
        ):
            result = ClaudeCodeBrain().execute(
                BrainRequest(
                    prompt="hi", allowed_tools=["Bash"], cwd=tmp_path, env={},
                    timeout_seconds=60, streaming=True,
                )
            )

        assert result.stop_reason == "transient_api_error"
        assert result.usage is not None
        assert result.usage.model_requests == 2
        assert result.usage.initial_context_tokens == 14434

    def test_a_cancel_during_the_backoff_still_carries_usage(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "istota.brain.claude_code._interruptible_sleep", lambda *a, **k: True
        )
        stream = [_init(), _REQ_1, _REQ_2, self._TRANSIENT]

        with patch(
            "istota.brain.claude_code.subprocess.Popen",
            side_effect=[_mock_process(list(stream)) for _ in range(3)],
        ):
            result = ClaudeCodeBrain().execute(
                BrainRequest(
                    prompt="hi", allowed_tools=["Bash"], cwd=tmp_path, env={},
                    timeout_seconds=60, streaming=True,
                )
            )

        assert result.stop_reason == "cancelled"
        assert result.usage is not None
        assert result.usage.model_requests == 2


def _run_simple(stdout, *, returncode=0, tmp_path=None):
    """Drive the non-streaming path with `subprocess.run` patched to one stdout."""
    req = BrainRequest(
        prompt="hi", allowed_tools=[], cwd=tmp_path or Path("/tmp"), env={},
        timeout_seconds=60, streaming=False,
    )
    completed = MagicMock()
    completed.stdout = stdout
    completed.stderr = ""
    completed.returncode = returncode
    with patch("istota.brain.claude_code.subprocess.run", return_value=completed):
        return ClaudeCodeBrain().execute(req)


class TestNonStreaming:
    """Stage 4: the path the daemon's task-less model calls take.

    CLI 2.1.227 emits `--output-format json` as an array of the same frames the
    streaming path produces, so the totals are readable. There are no
    `message_delta` frames, so these runs carry NULL context — totals only. The
    single-object form newer CLIs emit is covered by
    `TestNonStreamingSingleObject`.
    """

    def _json_array(self):
        return json.dumps([
            json.loads(_init()),
            json.loads(_RESULT),
        ])

    def test_the_array_parses_to_the_same_totals(self, tmp_path):
        result = _run_simple(self._json_array(), tmp_path=tmp_path)

        assert result.success is True
        assert result.result_text == "Done."
        assert result.usage is not None
        assert result.usage.billed_input_tokens == 550
        assert result.usage.output_tokens == 161
        assert result.usage.has_totals is True
        assert result.usage.cost_basis == "api"

    def test_context_columns_are_null_with_no_message_delta(self, tmp_path):
        result = _run_simple(self._json_array(), tmp_path=tmp_path)

        assert result.usage.initial_context_tokens is None
        assert result.usage.peak_context_tokens is None
        assert result.usage.model_requests == 0

    @pytest.mark.parametrize(
        "stdout",
        [
            "Here is the answer.",
            "",
            "  ",
            '{"not": "an array"}',
            "[unclosed",
            "[]",
            '["a string, not a frame"]',
            "[1, 2, 3]",
        ],
    )
    def test_anything_that_is_not_a_frame_array_falls_back_to_answer_text(
        self, stdout, tmp_path
    ):
        """The fallback is the point, not a nicety: roughly ninety tests across
        six files patch `subprocess.run` with plain-text stdout, and a CLI that
        ignores the flag behaves the same way. New behaviour is confined to the
        case where the array really parses."""
        result = _run_simple(stdout, tmp_path=tmp_path)

        if stdout.strip():
            assert result.result_text == stdout.strip()
        assert result.usage is None

    def test_plain_text_stdout_is_still_the_answer(self, tmp_path):
        result = _run_simple("The answer is 42.", tmp_path=tmp_path)

        assert result.success is True
        assert result.result_text == "The answer is 42."

    def test_an_error_frame_keeps_its_text_for_classification(self, tmp_path):
        """An `error_during_execution` frame carries no `result`. Blanking the
        answer there would skip the caller's `returncode == 0 and output` guard,
        so the classifiers would never see the text — and a provider failure
        that used to be read off stdout would come back as "produced no output"
        with a generic `error`, which is in neither the fallback trigger set nor
        the breaker's cooldown set."""
        stdout = json.dumps([
            json.loads(_init()),
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": "API Error: 529 Overloaded",
                "total_cost_usd": 0.01,
                "modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.01}},
            },
        ])

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.stop_reason == "transient_api_error"
        assert "529" in result.result_text
        # And the spend is still recorded.
        assert result.usage is not None
        assert result.usage.billed_input_tokens == 5

    def test_a_result_frame_with_no_text_at_all_keeps_raw_stdout(self, tmp_path):
        stdout = json.dumps([
            {"type": "result", "subtype": "success", "total_cost_usd": 0.01},
        ])

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.result_text == stdout
        assert result.usage is not None

    def test_an_array_with_no_result_frame_falls_back(self, tmp_path):
        stdout = json.dumps([json.loads(_init())])

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.usage is None
        assert result.result_text == stdout

    def test_the_command_asks_for_json_not_stream_json(self):
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd=Path("/tmp"), env={},
            timeout_seconds=60, streaming=False,
        )
        cmd = ClaudeCodeBrain._build_command(req)

        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "stream-json" not in cmd
        assert "--include-partial-messages" not in cmd

    def test_the_command_asks_for_verbose_so_the_init_frame_arrives(self):
        """`--verbose` is what makes the CLI emit the `system` init frame.

        Without it, 2.1.239 answers `--output-format json` with the bare
        terminal frame — no `apiKeySource`, so every daemon-side call records
        `cost_basis = "unknown"`. Measured against both deployed versions:
        2.1.227 and 2.1.239 each emit the frame array with the flag, and
        `_parse_simple_json_output` has always read that shape. The flag is how
        the CLI gets asked to report its own credential, rather than the daemon
        inferring one from config.
        """
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd=Path("/tmp"), env={},
            timeout_seconds=60, streaming=False,
        )
        cmd = ClaudeCodeBrain._build_command(req)

        assert "--verbose" in cmd

    def test_a_subscription_init_frame_is_read_as_subscription(self, tmp_path):
        """The whole point of asking for the init frame.

        `apiKeySource: "none"` is what a `/login` deployment reports, and it
        must land on `subscription` — not `unknown`, which is where every
        `sleep_cycle`, `code_review` and `shared_blocks` row went while the
        frame was missing.
        """
        stdout = json.dumps([
            json.loads(_init(api_key_source="none")),
            json.loads(_RESULT),
        ])

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.usage.cost_basis == "subscription"


class TestFailurePathsStillMeasure:
    @pytest.mark.parametrize("returncode", [0, 1, -9])
    def test_usage_is_attached_regardless_of_outcome(self, returncode, tmp_path):
        """Tokens are spent whether or not the run succeeded."""
        result = _run(FULL_STREAM, returncode=returncode, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.brain_kind == "claude_code"


# A genuine `--output-format json` envelope from CLI 2.1.238, which emits the
# terminal frame as one object rather than wrapping it in an array. Session and
# uuid values are placeholders; the shape is otherwise as captured. The array
# form 2.1.227 emits is covered by `TestNonStreaming` above — both shapes are
# real, and the adapter has to read either.
_SINGLE_OBJECT_RESULT = json.dumps({
    "is_error": False,
    "duration_api_ms": 27854,
    "num_turns": 2,
    "stop_reason": "end_turn",
    "session_id": "00000000-0000-0000-0000-000000000000",
    "total_cost_usd": 0.0319275,
    "usage": {"input_tokens": 17, "output_tokens": 147, "service_tier": "standard"},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 550,
            "outputTokens": 161,
            "cacheReadInputTokens": 14425,
            "cacheCreationInputTokens": 14565,
            "costUSD": 0.0319275,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
        }
    },
    "permission_denials": [],
    "subtype": "success",
    "api_error_status": None,
    "result": "Done.",
    "type": "result",
    "duration_ms": 28104,
    "uuid": "00000000-0000-0000-0000-000000000001",
})


class TestNonStreamingSingleObject:
    """ISSUE-271: `--output-format json` emits one object, not an array.

    The CLI's own help has always called it "json (single result)". 2.1.227
    nonetheless emits an array; 2.1.238, the deployed version, emits the bare
    terminal frame. Reading only the array form means every daemon-side model
    call — seven origins, none of them `task` — returns the JSON envelope where
    the answer should be and records no usage row at all.
    """

    def test_the_single_object_form_yields_the_answer(self, tmp_path):
        result = _run_simple(_SINGLE_OBJECT_RESULT, tmp_path=tmp_path)

        assert result.success is True
        assert result.result_text == "Done."

    def test_the_single_object_form_yields_a_usage_row(self, tmp_path):
        result = _run_simple(_SINGLE_OBJECT_RESULT, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.usage.billed_input_tokens == 550
        assert result.usage.output_tokens == 161
        assert result.usage.cache_read_tokens == 14425
        assert result.usage.cache_write_tokens == 14565
        assert result.usage.cost_usd == pytest.approx(0.0319275)
        assert result.usage.has_totals is True
        assert result.usage.model == "claude-haiku-4-5-20251001"

    def test_cost_basis_degrades_to_unknown_with_no_init_frame(self, tmp_path):
        """The single-object form carries no `system` init frame, so there is no
        `apiKeySource` to read. `unknown` is the honest answer — inferring the
        basis from config would be the guess `cost_basis_from_api_key_source`
        exists to refuse.

        `--verbose` means a current CLI no longer answers in this shape, so this
        is now the degradation path rather than the deployed one. It is kept
        because the flag's effect belongs to the CLI: a version that ignores it
        must still land on `unknown` rather than on a guess."""
        result = _run_simple(_SINGLE_OBJECT_RESULT, tmp_path=tmp_path)

        assert result.usage.cost_basis == "unknown"

    def test_context_columns_are_null(self, tmp_path):
        result = _run_simple(_SINGLE_OBJECT_RESULT, tmp_path=tmp_path)

        assert result.usage.initial_context_tokens is None
        assert result.usage.peak_context_tokens is None
        assert result.usage.model_requests == 0

    def test_a_single_object_error_frame_keeps_its_text(self, tmp_path):
        """Same reasoning as the array form: an `error_during_execution` frame
        carries no `result`, and blanking the answer would skip the classifiers
        so a provider failure comes back as a generic `error`."""
        stdout = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "error": "API Error: 529 Overloaded",
            "total_cost_usd": 0.01,
            "modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.01}},
        })

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.stop_reason == "transient_api_error"
        assert "529" in result.result_text
        assert result.usage is not None
        assert result.usage.billed_input_tokens == 5

    @pytest.mark.parametrize(
        "stdout",
        [
            '{"not": "an array"}',
            '{"type": "system", "subtype": "init"}',
            '{"type": "assistant"}',
            "{unclosed",
            "{}",
        ],
    )
    def test_an_object_that_is_not_a_result_frame_still_falls_back(
        self, stdout, tmp_path
    ):
        """Only `type == "result"` makes an object the terminal frame. A bare
        JSON object in a plain-text answer must not be mistaken for one."""
        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.result_text == stdout.strip()
        assert result.usage is None

    def test_a_json_object_answer_is_left_alone(self, tmp_path):
        """The daemon asks several callers for JSON answers. One that happens to
        carry a `type` key must not be swallowed as an envelope."""
        stdout = json.dumps({"type": "summary", "text": "the model's answer"})

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.result_text == stdout
        assert result.usage is None

    def test_an_error_frame_with_an_empty_result_still_classifies(self, tmp_path):
        """`result` present-and-empty, not merely absent.

        The extraction used to test `isinstance(answer, str)`, and `""` is a
        `str` — so the `error` fallback was never consulted and the run came
        back as "produced no output" with a generic `error`, which is in
        neither the fallback trigger set nor the breaker's cooldown set. That
        is verbatim the outcome the comment beside it says it prevents.
        """
        stdout = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "",
            "error": "API Error: 529 Overloaded",
            "total_cost_usd": 0.01,
            "modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.01}},
        })

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.stop_reason == "transient_api_error"
        assert "529" in result.result_text

    def test_an_empty_answer_is_not_replaced_by_the_envelope(self, tmp_path):
        """A degenerate-but-successful answer is not a provider failure. Falling
        through to raw stdout here would put JSON where the answer goes."""
        stdout = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "total_cost_usd": 0.01,
            "modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.01}},
        })

        result = _run_simple(stdout, tmp_path=tmp_path)

        assert result.result_text != stdout
        assert "modelUsage" not in result.result_text


class TestUnreadableEnvelopeWarns:
    """The silent fallback is how ISSUE-271 stayed invisible for three weeks.

    The warning has to fire for the shape-change class that actually happened —
    array to object — which means an unreadable *object* must warn, not just an
    array with no terminal frame. It must equally stay quiet when the model
    simply answered with JSON of its own, since a warning that fires on every
    structured answer trains an operator to ignore the real one.
    """

    def test_an_array_with_no_terminal_frame_warns(self, tmp_path, caplog):
        stdout = json.dumps([json.loads(_init())])

        with caplog.at_level("WARNING", logger="istota.brain.claude_code"):
            _run_simple(stdout, tmp_path=tmp_path)

        assert "envelope has probably changed" in caplog.text

    def test_a_renamed_terminal_object_warns(self, tmp_path, caplog):
        """The next envelope change, in the direction the last one moved."""
        stdout = json.dumps({
            "type": "final_result",
            "subtype": "success",
            "result": "Done.",
            "total_cost_usd": 0.01,
            "modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.01}},
        })

        with caplog.at_level("WARNING", logger="istota.brain.claude_code"):
            result = _run_simple(stdout, tmp_path=tmp_path)

        assert "envelope has probably changed" in caplog.text
        assert result.usage is None

    def test_a_bare_init_object_warns(self, tmp_path, caplog):
        """No envelope-only keys, but `system` is a type only the CLI emits."""
        stdout = json.dumps({"type": "system", "subtype": "init"})

        with caplog.at_level("WARNING", logger="istota.brain.claude_code"):
            _run_simple(stdout, tmp_path=tmp_path)

        assert "envelope has probably changed" in caplog.text

    @pytest.mark.parametrize(
        "stdout",
        [
            '["first", "second"]',
            '[{"type": "bug", "file": "x.py"}, {"type": "nit", "file": "y.py"}]',
            '{"type": "summary", "text": "the model\'s answer"}',
            '{"findings": [], "verdict": "ok"}',
            "{}",
            "[]",
        ],
    )
    def test_a_model_authored_json_answer_does_not_warn(
        self, stdout, tmp_path, caplog
    ):
        with caplog.at_level("WARNING", logger="istota.brain.claude_code"):
            _run_simple(stdout, tmp_path=tmp_path)

        assert "envelope has probably changed" not in caplog.text


class TestSimpleRetryKeepsUsage:
    """The non-streaming mirror of `TestRetryWrapperKeepsUsage`.

    That class patches `Popen` and runs `streaming=True`, so it covered only
    the streaming loop — `_execute_simple`'s two self-built results dropped the
    usage of every attempt. Inert before ISSUE-271, because on the deployed CLI
    there was never any usage on this path to lose.
    """

    @staticmethod
    def _error_envelope():
        return json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "error": "API Error: 500 Internal server error",
            "total_cost_usd": 0.01,
            "modelUsage": {"m": {"inputTokens": 5, "outputTokens": 3, "costUSD": 0.01}},
        })

    def test_exhausted_retries_still_record_the_spend(self, tmp_path):
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd=tmp_path, env={},
            timeout_seconds=60, streaming=False,
        )
        completed = MagicMock()
        completed.stdout = self._error_envelope()
        completed.stderr = ""
        completed.returncode = 1

        with patch(
            "istota.brain.claude_code.subprocess.run", return_value=completed
        ), patch("istota.brain.claude_code._interruptible_sleep", return_value=False):
            result = ClaudeCodeBrain().execute(req)

        assert result.success is False
        assert result.stop_reason == "transient_api_error"
        assert result.usage is not None
        assert result.usage.billed_input_tokens == 5

    def test_cancelling_during_the_backoff_still_records_the_spend(self, tmp_path):
        req = BrainRequest(
            prompt="hi", allowed_tools=[], cwd=tmp_path, env={},
            timeout_seconds=60, streaming=False,
        )
        completed = MagicMock()
        completed.stdout = self._error_envelope()
        completed.stderr = ""
        completed.returncode = 1

        with patch(
            "istota.brain.claude_code.subprocess.run", return_value=completed
        ), patch("istota.brain.claude_code._interruptible_sleep", return_value=True):
            result = ClaudeCodeBrain().execute(req)

        assert result.stop_reason == "cancelled"
        assert result.usage is not None
        assert result.usage.billed_input_tokens == 5


class TestSingleObjectReachesTheUsageTable:
    """The end of the chain ISSUE-271 broke, through the real writer.

    `_parse_simple_json_output` returning `(None, None)` meant `result.usage`
    was `None`, and `persist_brain_usage` returns on `if usage is None` before
    even its log line — so the seven non-task origins recorded nothing at all.
    Asserting on the brain's return value alone would not have caught that the
    row never lands.
    """

    def test_a_non_task_origin_records_a_row(self, tmp_path):
        from istota import db
        from istota.executor import persist_brain_usage

        dbp = tmp_path / "istota.db"
        db.init_db(dbp)

        result = _run_simple(_SINGLE_OBJECT_RESULT, tmp_path=tmp_path)

        with db.get_db(dbp) as conn:
            persist_brain_usage(
                _CfgWithDb(dbp),
                conn,
                usage=result.usage,
                origin="code_review",
                user_id="alice",
                brain_kind="claude_code",
                success=True,
            )
            rows = list(
                conn.execute(
                    "SELECT origin, output_tokens, cost_basis FROM task_usage"
                ).fetchall()
            )

        parents = [r for r in rows if r["origin"] == "code_review"]
        assert len(parents) == 1
        assert parents[0]["output_tokens"] == 161
        assert parents[0]["cost_basis"] == "unknown"


class _CfgWithDb:
    def __init__(self, dbp):
        self.db_path = dbp
