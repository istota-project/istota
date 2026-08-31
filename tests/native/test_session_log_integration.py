"""Stage 3 — the session log written through ``NativeBrain.execute``.

``tests/native/test_session_log.py`` covers the writer in isolation. This file
covers the *wiring*: that a real run of the native brain produces a file whose
records are the run, in the order the loop produced them.

The assertion the whole spec exists for is
:meth:`TestTheRecordSequence.test_the_tool_results_are_in_the_file_with_their_output`.
``tasks.execution_trace`` records tool *labels* and no output at all, so what a
tool returned was exactly the thing a finished native task could not be asked
about. That assertion was demonstrated red against pre-change code before the
wiring landed — with the feature enabled in the config, so the failure is "the
brain writes nothing" and not "the writer is off":

    $ uv run pytest tests/native/test_session_log_integration.py \\
          -k tool_results_are_in_the_file -x -q
    E   AssertionError: no session log file was written under .../logs/alice
    1 failed

The negative-control discipline this follows is ``AGENTS.md``'s: on a test
asserting against an artifact, reading the test tells you almost nothing about
whether it can fail.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from istota.brain import BrainRequest
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig, SessionLogConfig
from istota.llm.provider import StreamDone, StreamError, StreamStart, TextDelta
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    Usage,
)

from ._mock_provider import MockProvider

MARKER = "MARKER-TOOL-OUTPUT-7f3a91"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _req(
    prompt: str,
    cwd: Path,
    *,
    tools: list[str] | None = None,
    task_id: int = 4471,
    attempt: int = 1,
    user_id: str = "alice",
    timeout: int = 30,
    **kw,
) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=tools if tools is not None else [],
        cwd=cwd,
        env={},
        timeout_seconds=timeout,
        model="claude-sonnet-4-6",
        task_id=task_id,
        attempt=attempt,
        user_id=user_id,
        source_type="talk",
        conversation_token="a1b2c3d4",
        **kw,
    )


def _brain(provider, root: Path, **cfg) -> NativeBrain:
    """A brain whose session log is *enabled* and points at ``root``.

    Enabled deliberately: a test that only proves a disabled writer writes
    nothing proves nothing about the wiring.
    """
    session_log = cfg.pop("session_log", None) or SessionLogConfig(
        enabled=True, dir=str(root)
    )
    config = NativeBrainConfig(
        model="claude-sonnet-4-6", session_log=session_log, **cfg
    )
    return NativeBrain(config, provider=provider)


def _files(root: Path, user_id: str = "alice") -> list[Path]:
    return sorted((root / user_id).glob("*.jsonl"))


def _read(root: Path, user_id: str = "alice") -> list[dict]:
    files = _files(root, user_id)
    assert files, f"no session log file was written under {root / user_id}"
    assert len(files) == 1, f"expected one session log, found {files}"
    return _records(files[0])


def _records(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _kinds(records: list[dict]) -> list[str]:
    """Record types, with a ``message`` collapsed onto the role it carries."""
    out = []
    for rec in records:
        if rec["type"] == "message":
            out.append(f"message:{rec['message']['role']}")
        else:
            out.append(rec["type"])
    return out


def _text_turn(text: str, **kw) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        stop_reason=kw.pop("stop_reason", "end_turn"),
        usage=kw.pop("usage", Usage(input_tokens=100, output_tokens=10)),
        **kw,
    )


def _read_call(call_id: str, path: Path) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCallContent(id=call_id, name="Read", arguments={"file_path": str(path)})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=200, output_tokens=20),
    )


# --------------------------------------------------------------------------- #
# The record sequence
# --------------------------------------------------------------------------- #


class TestTheRecordSequence:
    def _run(self, tmp_path) -> list[dict]:
        target = tmp_path / "notes.txt"
        target.write_text(f"line one\n{MARKER}\nline three\n")
        root = tmp_path / "logs"
        provider = MockProvider(
            [
                _read_call("c1", target),
                _read_call("c2", target),
                _text_turn("All read."),
            ]
        )
        result = _brain(provider, root).execute(
            _req("summarize the notes", tmp_path, tools=["Read"])
        )
        assert result.success is True
        return _read(root)

    def test_the_records_are_the_run_in_order(self, tmp_path):
        records = self._run(tmp_path)
        assert _kinds(records) == [
            "session",
            "context",
            "message:user",
            "message:assistant",
            "message:tool_result",
            "message:assistant",
            "message:tool_result",
            "message:assistant",
            "result",
        ]

    def test_the_tool_results_are_in_the_file_with_their_output(self, tmp_path):
        """The assertion the spec exists for. Demonstrated red pre-change."""
        records = self._run(tmp_path)
        results = [
            r["message"] for r in records
            if r["type"] == "message" and r["message"]["role"] == "tool_result"
        ]
        assert len(results) == 2
        for msg in results:
            assert msg["tool_name"] == "Read"
            assert msg["is_error"] is False
            body = "".join(
                b.get("text", "") for b in msg["content"] if b["type"] == "text"
            )
            # Not "a tool result record exists" — the tool's *output* is in it.
            assert MARKER in body, body[:400]
        assert {m["tool_call_id"] for m in results} == {"c1", "c2"}

    def test_line_one_is_the_session_header(self, tmp_path):
        header = self._run(tmp_path)[0]
        assert header["type"] == "session"
        assert header["v"] == 1
        assert header["task_id"] == 4471
        assert header["attempt"] == 1
        assert header["user_id"] == "alice"
        assert header["source_type"] == "talk"
        assert header["conversation_token"] == "a1b2c3d4"
        assert header["brain"] == "native"
        assert header["model"] == "claude-sonnet-4-6"
        # The host, never the URL: an operator can put a token in the path.
        assert header["base_url_host"] == "api.anthropic.com"
        assert "base_url" not in header
        assert "api_key" not in header
        assert "extra_headers" not in header

    def test_line_two_is_the_context_record(self, tmp_path):
        record = self._run(tmp_path)[1]
        assert record["type"] == "context"
        assert record["tools"] == ["Read"]
        assert len(record["tools_schema_sha256"]) == 64
        assert record["system_prompt"]
        assert record["system_prompt_source"] == "builtin"

    def test_the_first_user_message_is_the_assembled_prompt(self, tmp_path):
        records = self._run(tmp_path)
        first = next(r for r in records if r["type"] == "message")
        assert first["message"]["role"] == "user"
        text = "".join(
            b.get("text", "") for b in first["message"]["content"] if b["type"] == "text"
        )
        assert text == "summarize the notes"

    def test_the_last_line_is_the_result(self, tmp_path):
        records = self._run(tmp_path)
        assert records[-1]["type"] == "result"
        assert records[-1]["success"] is True
        assert records[-1]["stop_reason"] == "completed"
        assert records[-1]["result_text"] == "All read."
        assert records[-1]["model_used"] == "claude-sonnet-4-6"
        assert records[-1]["turns"] == 3
        assert records[-1]["duration_ms"] >= 0


class TestUsage:
    def test_the_assistant_record_carries_the_turn_usage(self, tmp_path):
        root = tmp_path / "logs"
        provider = MockProvider(
            [
                _text_turn(
                    "one and done",
                    usage=Usage(
                        input_tokens=1234,
                        output_tokens=56,
                        cache_read_tokens=7,
                        cache_write_tokens=8,
                    ),
                )
            ]
        )
        result = _brain(provider, root).execute(_req("hi", tmp_path))
        records = _read(root)
        assistant = [
            r["message"] for r in records
            if r["type"] == "message" and r["message"]["role"] == "assistant"
        ][-1]
        # A single-turn run, so the turn's usage *is* the attempt's usage.
        assert assistant["usage"]["output_tokens"] == 56
        assert assistant["usage"]["cache_read_tokens"] == 7
        assert result.usage is not None
        assert records[-1]["usage"]["output_tokens"] == result.usage.output_tokens
        assert (
            records[-1]["usage"]["cache_read_tokens"] == result.usage.cache_read_tokens
        )

    def test_thinking_is_recorded(self, tmp_path):
        root = tmp_path / "logs"
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ThinkingContent(thinking="weighing the options"),
                        TextContent(text="answer"),
                    ],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=10, output_tokens=5),
                )
            ]
        )
        _brain(provider, root).execute(_req("hi", tmp_path))
        blocks = [
            b
            for r in _read(root)
            if r["type"] == "message" and r["message"]["role"] == "assistant"
            for b in r["message"]["content"]
        ]
        assert any(b["type"] == "thinking" for b in blocks)


# --------------------------------------------------------------------------- #
# Compaction, steering, nudges
# --------------------------------------------------------------------------- #


class TestCompaction:
    def test_a_proactive_compaction_is_recorded_between_the_messages(self, tmp_path):
        target = tmp_path / "notes.txt"
        # Big enough that `find_cut_point` has something to cut: it walks back
        # from the newest accumulating estimated tokens, so a three-message
        # conversation of short strings never reaches any budget and returns 0.
        target.write_text((MARKER + "\n") * 200)
        root = tmp_path / "logs"
        provider = MockProvider(
            [
                # Turn 1 reports a near-full window, so prepare_next_turn
                # compacts after its tool result.
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1", name="Read", arguments={"file_path": str(target)}
                        )
                    ],
                    stop_reason="tool_use",
                    usage=Usage(input_tokens=1800, output_tokens=20),
                ),
                _text_turn("SUMMARY OF THE RUN"),  # the summarizer's own call
                _text_turn("Done."),
            ]
        )
        brain = _brain(
            provider,
            root,
            context_window=2000,
            compaction_reserve_tokens=500,
            compaction_keep_recent_tokens=50,
        )
        brain.execute(_req("go", tmp_path, tools=["Read"]))
        records = _read(root)
        kinds = _kinds(records)
        assert "compaction" in kinds
        compaction = next(r for r in records if r["type"] == "compaction")
        assert compaction["trigger"] == "proactive"
        assert compaction["recovery_index"] is None
        assert compaction["summary"] == "SUMMARY OF THE RUN"
        assert compaction["tokens_before"] > 0
        assert compaction["cut_index"] >= 1
        assert compaction["messages_dropped"] >= 1
        assert compaction["image_pinned"] is False
        # A dataclass, so the wiring has to convert it — left as one it would
        # land as a `serialization_error` line instead.
        assert set(compaction["details"]) == {"read_files", "modified_files"}
        # Between the tool result it followed and the next assistant turn.
        i = kinds.index("compaction")
        assert kinds[i - 1] == "message:tool_result"
        assert kinds[i + 1] == "message:assistant"


class _ScriptedProvider:
    """One behaviour per ``stream`` call: overflow error, or a completion."""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)

    async def stream(
        self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
    ) -> AsyncIterator:
        kind, payload = self.behaviors.pop(0) if self.behaviors else ("done", "")
        yield StreamStart()
        if kind != "done":
            yield StreamError(
                message=AssistantMessage(stop_reason="error", error_message=payload)
            )
        else:
            yield TextDelta(text=payload)
            yield StreamDone(
                message=AssistantMessage(
                    content=[TextContent(text=payload)],
                    usage=Usage(input_tokens=10, output_tokens=5),
                    stop_reason="end_turn",
                )
            )


_OVERFLOW = ("overflow", "HTTP 400: context length exceeded")


class TestOverflowRecovery:
    def test_the_recovery_writes_an_overflow_compaction(self, tmp_path):
        root = tmp_path / "logs"
        provider = _ScriptedProvider(
            [_OVERFLOW, ("done", "SUMMARY"), ("done", "Recovered answer.")]
        )
        result = _brain(provider, root).execute(_req("hi", tmp_path))
        assert result.result_text == "Recovered answer."
        records = _read(root)
        compaction = next(r for r in records if r["type"] == "compaction")
        assert compaction["trigger"] == "overflow"
        assert compaction["recovery_index"] == 1
        assert compaction["summary"] == "SUMMARY"

    def test_the_continue_pass_still_records_its_messages(self, tmp_path):
        """Stage 3's first task, pinned as a test.

        ``run_agent_loop_continue`` shares ``_run_loop`` with ``run_agent_loop``,
        so every assistant message and tool result it produces emits
        ``message_end`` — measured before the wiring was written, which is why
        the ``tool_execution_end`` fallback the spec held in reserve was not
        needed. What the continue pass does *not* re-emit is the recovery
        context's own pre-existing messages, and that is correct: the tail was
        already written by the first pass, and the synthetic compaction summary
        is what the ``overflow`` record above stands for.
        """
        root = tmp_path / "logs"
        provider = _ScriptedProvider(
            [_OVERFLOW, ("done", "SUMMARY"), ("done", "Recovered answer.")]
        )
        _brain(provider, root).execute(_req("hi", tmp_path))
        records = _read(root)
        kinds = _kinds(records)
        after = kinds[kinds.index("compaction") + 1 :]
        assert "message:assistant" in after
        texts = [
            "".join(
                b.get("text", "")
                for b in r["message"]["content"]
                if b["type"] == "text"
            )
            for r in records
            if r["type"] == "message" and r["message"]["role"] == "assistant"
        ]
        assert "Recovered answer." in texts


class TestSteering:
    def test_a_steer_record_precedes_the_injected_user_message(self, tmp_path):
        root = tmp_path / "logs"
        target = tmp_path / "notes.txt"
        target.write_text(MARKER)
        pending = [["check the staging branch first"]]

        def _poll_steers():
            return pending.pop(0) if pending else []

        provider = MockProvider(
            [_read_call("c1", target), _text_turn("Checked.")]
        )
        _brain(provider, root).execute(
            _req("go", tmp_path, tools=["Read"], poll_steers=_poll_steers)
        )
        records = _read(root)
        kinds = _kinds(records)
        assert "steer" in kinds
        steer = next(r for r in records if r["type"] == "steer")
        # The raw text the user sent, not the frame the loop wraps it in.
        assert steer["text"] == "check the staging branch first"
        i = kinds.index("steer")
        assert kinds[i + 1] == "message:user"
        injected = records[i + 1]["message"]
        body = "".join(
            b.get("text", "") for b in injected["content"] if b["type"] == "text"
        )
        assert "check the staging branch first" in body


class TestNudge:
    def test_a_turn_budget_nudge_is_recorded(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text(MARKER)
        root = tmp_path / "logs"
        provider = MockProvider(
            [_read_call("c1", target), _read_call("c2", target), _text_turn("Done.")]
        )
        brain = _brain(
            provider,
            root,
            max_turns=2,
            turn_budget_nudge=True,
            turn_budget_nudge_early_percent=50,
            turn_budget_nudge_remaining=[1],
        )
        brain.execute(_req("go", tmp_path, tools=["Read"]))
        records = _read(root)
        nudges = [r for r in records if r["type"] == "nudge"]
        assert nudges, _kinds(records)
        assert nudges[0]["max_turns"] == 2
        assert nudges[0]["phase"] in ("early", "late")
        assert nudges[0]["turns"] >= 1


# --------------------------------------------------------------------------- #
# The abnormal paths
# --------------------------------------------------------------------------- #


class TestTimeout:
    def test_the_timeout_writes_a_result_and_the_file_ends_in_a_newline(
        self, tmp_path
    ):
        root = tmp_path / "logs"

        class _SlowProvider:
            async def stream(self, *a, **k):
                yield StreamStart()
                await asyncio.sleep(30)
                yield StreamDone(message=_text_turn("never"))

        result = _brain(_SlowProvider(), root).execute(
            _req("hi", tmp_path, timeout=1)
        )
        assert result.stop_reason == "timeout"
        path = _files(root)[0]
        raw = path.read_text(encoding="utf-8")
        assert raw.endswith("\n")
        records = _records(path)
        assert records[-1]["type"] == "result"
        assert records[-1]["stop_reason"] == "timeout"
        assert records[-1]["success"] is False


class TestExceptionEscapingTheLoop:
    def test_error_then_result(self, tmp_path):
        root = tmp_path / "logs"

        class _BoomProvider:
            async def stream(self, *a, **k):
                raise RuntimeError("provider exploded")
                yield  # pragma: no cover — makes this an async generator

        result = _brain(_BoomProvider(), root).execute(_req("hi", tmp_path))
        assert result.success is False
        records = _read(root)
        assert [r["type"] for r in records[-2:]] == ["error", "result"]
        assert records[-2]["kind"] == "RuntimeError"
        assert "provider exploded" in records[-2]["message"]
        assert "Traceback" in records[-2]["traceback"]
        assert records[-1]["stop_reason"] == "error"


class TestAttempts:
    def test_two_attempts_are_two_files(self, tmp_path):
        root = tmp_path / "logs"
        for attempt in (1, 2):
            provider = MockProvider([_text_turn(f"attempt {attempt}")])
            _brain(provider, root).execute(
                _req("hi", tmp_path, attempt=attempt)
            )
        files = _files(root)
        assert len(files) == 2
        headers = [_records(p)[0] for p in files]
        assert sorted(h["attempt"] for h in headers) == [1, 2]
        assert len({h["session_id"] for h in headers}) == 2
        # Neither truncated the other.
        for path in files:
            assert _records(path)[-1]["type"] == "result"


# --------------------------------------------------------------------------- #
# Off, and taskless
# --------------------------------------------------------------------------- #


class TestTheOffSwitch:
    def test_disabled_writes_nothing_and_creates_no_directory(self, tmp_path):
        root = tmp_path / "logs"
        provider = MockProvider([_text_turn("ok")])
        brain = _brain(
            provider,
            root,
            session_log=SessionLogConfig(enabled=False, dir=str(root)),
        )
        result = brain.execute(_req("hi", tmp_path))
        assert result.success is True
        assert not root.exists()

    def test_a_call_with_no_task_identity_writes_nothing(self, tmp_path):
        """The sleep cycle and the REPL call the brain with no task behind it.

        There is no attempt to name a file after, so there is no file — rather
        than a ``task-0-0`` colliding across every such call.
        """
        root = tmp_path / "logs"
        provider = MockProvider([_text_turn("ok")])
        req = BrainRequest(
            prompt="hi",
            allowed_tools=[],
            cwd=tmp_path,
            env={},
            timeout_seconds=30,
            model="claude-sonnet-4-6",
        )
        assert _brain(provider, root).execute(req).success is True
        assert not root.exists()


class TestTheWriterNeverFailsTheTask:
    """The never-raises contract, exercised through the brain rather than the
    writer: `tests/native/test_session_log.py` proves the writer swallows it,
    this proves the task still finishes when it does."""


    def test_an_unwritable_root_still_completes_the_task(self, tmp_path):
        # A file where the root directory should be: every mkdir under it fails.
        root = tmp_path / "logs"
        root.write_text("not a directory")
        provider = MockProvider([_text_turn("still fine")])
        result = _brain(provider, root).execute(_req("hi", tmp_path))
        assert result.success is True
        assert result.result_text == "still fine"
