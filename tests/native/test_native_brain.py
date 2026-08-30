"""NativeBrain — the Brain-protocol adapter over the three-layer stack."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from istota import db
from istota.brain import BrainRequest, make_brain
from istota.brain._events import TextEvent
from istota.brain.native import NativeBrain
from istota.config import BrainConfig, NativeBrainConfig
from istota.llm.provider import (
    StreamDone,
    StreamError,
    StreamStart,
    TextDelta,
    ThinkingDelta,
)
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    Usage,
)

from ._mock_provider import MockProvider


def _req(prompt: str, cwd: Path, tools: list[str] | None = None) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=tools if tools is not None else [],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model="claude-sonnet-4-6",
    )


def _brain(provider, **cfg) -> NativeBrain:
    config = NativeBrainConfig(model="claude-sonnet-4-6", **cfg)
    return NativeBrain(config, provider=provider)


class TestTextCompletion:
    def test_simple_completion(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="Hello there.")],
                    usage=Usage(input_tokens=100, output_tokens=10),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True
        assert result.result_text == "Hello there."
        assert result.stop_reason == "completed"

    def test_usage_accumulated(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=200, output_tokens=20),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("go", tmp_path))
        assert result.usage is not None
        # `BrainUsage.billed_input_tokens` excludes cache reads; this turn had
        # none, so it equals the provider's inclusive `prompt_tokens`.
        assert result.usage.billed_input_tokens == 200
        assert result.usage.output_tokens == 20
        assert result.usage.turns == 1

    def test_reported_cost_captured_when_turn_has_no_tokens(self, tmp_path):
        # A costed turn that reports zero token counts (some OpenRouter
        # free/BYOK responses) must still contribute its charge — the usage
        # gate accepts total_tokens>0 OR a reported cost.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(cost_usd=0.007),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("go", tmp_path))
        assert result.usage is not None
        assert result.usage.cost_usd == 0.007
        assert result.usage.turns == 1

    def test_max_tokens_truncation_marker(self, tmp_path):
        # NB-15: a final answer cut off at the output-token cap is delivered with
        # a visible marker, not as a clean completion.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="The answer is fo")],
                    usage=Usage(input_tokens=10, output_tokens=5),
                    stop_reason="max_tokens",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True
        assert "The answer is fo" in result.result_text
        assert "truncated" in result.result_text.lower()


    def test_content_filter_marker(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="partial")],
                    usage=Usage(input_tokens=10, output_tokens=2),
                    stop_reason="content_filter",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert "partial" in result.result_text
        assert "content filter" in result.result_text.lower()

    def test_max_tokens_empty_is_failure(self, tmp_path):
        # A truncation that produced *no* answer content — a reasoning model
        # that spent the whole output-token budget on thinking and emitted
        # nothing — must fail (not silently succeed with an empty result), so
        # the executor's retry path engages and the empty reply is never
        # delivered or archived as a completed task.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[ThinkingContent(thinking="(reasoning only, no answer)")],
                    usage=Usage(input_tokens=10, output_tokens=16384),
                    stop_reason="max_tokens",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"
        assert result.result_text.strip()  # non-empty, informative
        assert "truncated" in result.result_text.lower()
        assert "output token" in result.result_text.lower()

    def test_max_tokens_after_a_text_turn_keeps_the_partial(self, tmp_path):
        """NB-15 must survive the ISSUE-211 final-turn rule: the run produced
        an answer, the *final* turn was the empty truncated one. Failing here
        would discard the partial and burn all three attempts on a reasoning
        model that reliably exhausts its output budget."""
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        TextContent(text="Partial analysis so far."),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[ThinkingContent(thinking="(all budget on thinking)")],
                    usage=Usage(input_tokens=10, output_tokens=16384),
                    stop_reason="max_tokens",
                ),
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path, tools=["Write"]))
        assert result.success is True
        assert "Partial analysis so far." in result.result_text
        assert "truncated" in result.result_text.lower()

    def test_content_filter_empty_is_failure(self, tmp_path):
        # Same contract as max_tokens: a content-filter clip that yielded no
        # content is a failure, not an empty success.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[],
                    usage=Usage(input_tokens=10, output_tokens=0),
                    stop_reason="content_filter",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"
        assert "content filter" in result.result_text.lower()

    def test_clean_completion_has_no_marker(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="all good")], stop_reason="end_turn")]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.result_text == "all good"

    def test_system_prompt_from_custom_file(self, tmp_path):
        sysfile = tmp_path / "sys.md"
        sysfile.write_text("You are a test bot.")
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path)
        req.custom_system_prompt_path = sysfile
        _brain(provider).execute(req)
        # Text-only invocation (no tools): no coding block prepended.
        assert provider.calls[0]["system_prompt"] == "You are a test bot."


class TestCodingSystemPrompt:
    """Stage 2 — the native brain's coding-guidance system prompt."""

    def _run(self, tmp_path, req):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        _brain(provider).execute(req)
        return provider.calls[0]["system_prompt"]

    def test_tool_bearing_task_gets_coding_block(self, tmp_path):
        sp = self._run(tmp_path, _req("do a thing", tmp_path, tools=["Read", "Edit"]))
        assert "coding agent" in sp
        assert "Read a file before editing" in sp
        assert "edits[]" in sp

    def test_text_only_task_has_empty_prompt(self, tmp_path):
        sp = self._run(tmp_path, _req("summarize", tmp_path, tools=[]))
        assert sp == ""

    def test_custom_prompt_appended_after_coding_block(self, tmp_path):
        sysfile = tmp_path / "sys.md"
        sysfile.write_text("Operator override.")
        req = _req("do a thing", tmp_path, tools=["Read"])
        req.custom_system_prompt_path = sysfile
        sp = self._run(tmp_path, req)
        assert "coding agent" in sp
        assert sp.rstrip().endswith("Operator override.")
        # Coding block comes first, custom prompt after.
        assert sp.index("coding agent") < sp.index("Operator override.")


class TestModelUsed:
    def test_reports_requested_model(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="ok")],
                    usage=Usage(input_tokens=10, output_tokens=2),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.model_used == "claude-sonnet-4-6"

    def test_falls_back_to_config_model_when_request_empty(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="ok")],
                    stop_reason="end_turn",
                )
            ]
        )
        req = _req("hi", tmp_path)
        req.model = ""  # no per-task model → brain falls back to its config model
        result = _brain(provider).execute(req)
        assert result.model_used == "claude-sonnet-4-6"


class TestAdvisorIgnored:
    """advisor-model spec: the advisor tool is an Anthropic Messages beta tool
    with no wire over openai_compat. NativeBrain reads req.model/req.effort
    only — BrainRequest.advisor is present (BrainRequest is shared across
    brains) but never consulted, so setting it changes nothing."""

    def test_advisor_set_does_not_affect_result(self, tmp_path):
        def _run(advisor):
            provider = MockProvider(
                [
                    AssistantMessage(
                        content=[TextContent(text="ok")],
                        usage=Usage(input_tokens=10, output_tokens=2),
                        stop_reason="end_turn",
                    )
                ]
            )
            req = _req("hi", tmp_path)
            req.advisor = advisor
            return _brain(provider).execute(req)

        without = _run("")
        with_advisor = _run("claude-opus-5")
        assert with_advisor.success == without.success
        assert with_advisor.result_text == without.result_text
        assert with_advisor.model_used == without.model_used


class TestToolUse:
    def test_write_tool_then_completion(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Wrote the file.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        req = _req("write a file", tmp_path, tools=["Write", "Read"])
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "Wrote the file."
        assert (tmp_path / "out.txt").read_text() == "hi"

    def test_trace_and_actions(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Done.")], stop_reason="end_turn"
                ),
            ]
        )
        req = _req("write", tmp_path, tools=["Write"])
        result = _brain(provider).execute(req)
        trace = json.loads(result.execution_trace)
        assert any(e["type"] == "tool" for e in trace)
        assert any(e["type"] == "text" for e in trace)
        actions = json.loads(result.actions_taken)
        assert any("out.txt" in a for a in actions)

    def test_only_allowed_tools_exposed(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path, tools=["Read"])
        _brain(provider).execute(req)
        tool_names = {t.name for t in provider.calls[0]["tools"]}
        assert tool_names == {"Read"}


class TestProviderLifecycle:
    """NB-17: a self-built provider's HTTP client is closed per task; an
    injected provider is left alone (the caller owns it)."""

    def test_injected_provider_not_closed(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        brain = _brain(provider)
        assert brain._owns_provider is False
        brain.execute(_req("hi", tmp_path))  # must not raise / close the mock

    def test_self_built_provider_is_closed(self, tmp_path):
        closed = {"v": False}

        class _Prov:
            async def stream(self, *a, **k):
                from istota.llm.provider import StreamDone

                yield StreamDone(
                    message=AssistantMessage(
                        content=[TextContent(text="ok")], stop_reason="end_turn"
                    )
                )

            async def aclose(self):
                closed["v"] = True

        brain = _brain(MockProvider([]))
        # Simulate a self-built provider.
        brain._provider = _Prov()
        brain._owns_provider = True
        brain.execute(_req("hi", tmp_path))
        assert closed["v"] is True


class TestFsConfinement:
    """NB-1: fs_read_roots/fs_write_roots thread into the file tools' ToolEnv."""

    def _tool(self, brain, req, name):
        return next(t for t in brain._build_tools(req) if t.schema.name == name)

    def test_roots_confine_read_tool(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "in.txt").write_text("inside\n")
        secret = tmp_path / "secret.txt"
        secret.write_text("classified\n")

        req = _req("hi", tmp_path, tools=["Read"])
        req.fs_read_roots = [ws]
        req.fs_write_roots = [ws]
        read = self._tool(_brain(MockProvider([])), req, "Read")

        ok = asyncio.run(read.execute("c", {"file_path": str(ws / "in.txt")}, None, None))
        assert "inside" in ok.content[0].text
        blocked = asyncio.run(read.execute("c", {"file_path": str(secret)}, None, None))
        assert "classified" not in blocked.content[0].text
        assert "workspace" in blocked.content[0].text.lower()

    def test_write_denied_roots_thread_through_to_the_tools(self, tmp_path):
        """The seam the executor actually uses: BrainRequest → _build_tools →
        ToolEnv. Dropping either plumbing line leaves the ToolEnv-level tests
        green, so this is where the carve-out is proven to arrive."""
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        (carve / "credential-fetch").write_text("original\n")

        req = _req("hi", tmp_path, tools=["Write"])
        req.fs_read_roots = [ws]
        req.fs_write_roots = [ws]
        req.fs_write_denied_roots = [carve]
        write = self._tool(_brain(MockProvider([])), req, "Write")

        blocked = asyncio.run(write.execute(
            "c", {"file_path": str(carve / "credential-fetch"), "content": "x"}, None, None,
        ))
        assert (carve / "credential-fetch").read_text() == "original\n"
        assert "read-only" in blocked.content[0].text.lower()

        ok = asyncio.run(write.execute(
            "c", {"file_path": str(ws / "notes.txt"), "content": "fine\n"}, None, None,
        ))
        assert (ws / "notes.txt").read_text() == "fine\n"
        assert "Created" in ok.content[0].text

    def test_no_roots_means_unconfined(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("readable\n")
        req = _req("hi", tmp_path, tools=["Read"])  # no fs roots
        read = self._tool(_brain(MockProvider([])), req, "Read")
        out = asyncio.run(read.execute("c", {"file_path": str(secret)}, None, None))
        assert "readable" in out.content[0].text

    def test_relative_paths_resolve_under_write_root(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        req = _req("hi", tmp_path, tools=["Write"])
        req.fs_read_roots = [ws]
        req.fs_write_roots = [ws]
        write = self._tool(_brain(MockProvider([])), req, "Write")
        asyncio.run(write.execute("c", {"file_path": "rel.txt", "content": "x"}, None, None))
        assert (ws / "rel.txt").read_text() == "x"


class TestErrorAndStops:
    def test_error_stop(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="")],
                    stop_reason="error",
                    error_message="HTTP 400: bad",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"

    def test_max_turns_stops(self, tmp_path):
        # Model keeps calling a tool forever; max_turns caps it.
        turns = [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id=f"c{i}", name="Read", arguments={"file_path": "README"}
                    )
                ],
                stop_reason="tool_use",
            )
            for i in range(20)
        ]
        provider = MockProvider(turns)
        req = _req("loop", tmp_path, tools=["Read"])
        result = _brain(provider, max_turns=3).execute(req)
        # A capped run surfaces its real stop_reason ("max_turns") rather than
        # being masked as "completed" — so a truncated-by-cap task stays visible
        # to stop_reason-keyed dispatch and the done event instead of reading as
        # a natural completion.
        assert result.stop_reason == "max_turns"
        # …with an informative marker rather than an empty success.
        assert "maximum number of steps" in result.result_text
        # Only ran up to the cap, not all 20 scripted turns.
        assert len(provider.calls) <= 4

    def test_max_turns_keeps_the_narration_it_was_capped_mid(self, tmp_path):
        """ISSUE-187 defects 1-2 must survive the ISSUE-211 final-turn rule.

        A cap lands on whatever turn the model was on, routinely a tool-only
        one. The marker labels the text as incomplete, so delivering the last
        thing the model said alongside it is honest — dropping it would leave
        the user a bare "reached the maximum number of steps".
        """
        turns = [
            AssistantMessage(
                content=[
                    TextContent(text="Let me move to Otodom and OLX next."),
                    ToolCallContent(
                        id="c0", name="Read", arguments={"file_path": "README"}
                    ),
                ],
                stop_reason="tool_use",
            ),
        ] + [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id=f"c{i}", name="Read", arguments={"file_path": "README"}
                    )
                ],
                stop_reason="tool_use",
            )
            for i in range(1, 20)
        ]
        result = _brain(MockProvider(turns), max_turns=2).execute(
            _req("search", tmp_path, tools=["Read"])
        )
        assert result.stop_reason == "max_turns"
        assert "Let me move to Otodom and OLX next." in result.result_text
        assert "maximum number of steps" in result.result_text

    def test_cancellation(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="first")], stop_reason="end_turn"
                ),
            ]
        )

        cancelled = {"v": False}

        def cancel_check():
            return cancelled["v"]

        req = _req("hi", tmp_path)
        req.cancel_check = cancel_check
        cancelled["v"] = True  # cancelled before the run starts
        result = _brain(provider).execute(req)
        assert result.stop_reason == "cancelled"
        assert result.success is False

    def test_cancellation_emits_scheduler_magic_string(self, tmp_path):
        # The executor drops stop_reason; the scheduler routes cancellation by
        # matching result_text == "Cancelled by user" exactly. NativeBrain must
        # emit that string or a cancelled task gets retried.
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="x")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path)
        req.cancel_check = lambda: True
        result = _brain(provider).execute(req)
        assert result.result_text == "Cancelled by user"

    def test_cancel_poll_runs_off_the_event_loop(self, tmp_path):
        # NB-9: cancel_check is a synchronous DB read; the poller must run it via
        # asyncio.to_thread so SQLite lock contention can't freeze the loop.
        import contextlib
        import threading

        seen = {}

        async def _drive():
            loop_thread = threading.get_ident()

            def check():
                seen["thread"] = threading.get_ident()
                seen["loop_thread"] = loop_thread
                return False

            abort = asyncio.Event()
            task = asyncio.create_task(NativeBrain._poll_cancel(check, abort))
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_drive())
        assert seen.get("thread") is not None
        assert seen["thread"] != seen["loop_thread"]

    def test_error_surfaces_message_into_result_text(self, tmp_path):
        # The scheduler classifies policy refusals / errors from result_text; an
        # empty string reads as a generic failure. The provider's error_message
        # must reach result_text.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="")],
                    stop_reason="error",
                    error_message="API Error: 400 content policy refused",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"
        assert "content policy refused" in result.result_text

    def test_cancel_check_exception_does_not_crash_run(self, tmp_path):
        # A transient cancel_check failure (e.g. SQLite lock) must not abort the
        # run or crash the brain — it's treated as "not cancelled".
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="done")], stop_reason="end_turn")]
        )

        def boom():
            raise RuntimeError("db locked")

        req = _req("hi", tmp_path)
        req.cancel_check = boom
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "done"


class TestLiveCancellation:
    class _BlockedProvider:
        """A provider waiting for its first byte, as a slow HTTP stream does."""

        def __init__(self):
            self.started = threading.Event()
            self._loop = None
            self._release = None

        async def stream(self, *args, **kwargs):
            self._loop = asyncio.get_running_loop()
            self._release = asyncio.Event()
            self.started.set()
            await self._release.wait()
            if False:
                yield StreamStart()

        def release(self):
            if self._loop is not None and self._release is not None:
                self._loop.call_soon_threadsafe(self._release.set)

    def test_cancel_interrupts_provider_wait_before_first_event(self, tmp_path):
        provider = self._BlockedProvider()
        cancelled = threading.Event()
        req = _req("wait forever", tmp_path)
        req.cancel_check = cancelled.is_set

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_brain(provider).execute, req)
            assert provider.started.wait(timeout=1)
            cancelled.set()
            try:
                result = future.result(timeout=2)
            except FutureTimeoutError:
                provider.release()
                future.result(timeout=1)
                raise

        assert result.stop_reason == "cancelled"
        assert result.result_text == "Cancelled by user"

    @pytest.mark.parametrize("control", ["command", "web"])
    def test_chat_cancel_controls_interrupt_native_provider_wait(
        self, control, make_config, monkeypatch, tmp_path,
    ):
        from istota.commands import CommandContext, cmd_stop

        config = make_config()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn,
                prompt="wait forever",
                user_id="alice",
                source_type="web" if control == "web" else "talk",
                conversation_token="room1",
            )
            db.update_task_status(conn, task_id, "running")

        provider = self._BlockedProvider()
        req = _req("wait forever", tmp_path)

        def cancelled():
            with db.get_db(config.db_path) as conn:
                return db.is_task_cancelled(conn, task_id)

        req.cancel_check = cancelled
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_brain(provider).execute, req)
            assert provider.started.wait(timeout=1)

            if control == "command":
                with db.get_db(config.db_path) as conn:
                    asyncio.run(cmd_stop(CommandContext(
                        config=config,
                        conn=conn,
                        user_id="alice",
                        conversation_token="room1",
                        args="",
                    )))
            else:
                import istota.web_app as web_app

                monkeypatch.setattr(web_app, "_config", config)
                web_app._chat_cancel_task(task_id)

            try:
                result = future.result(timeout=2)
            except FutureTimeoutError:
                provider.release()
                future.result(timeout=1)
                raise

        assert result.stop_reason == "cancelled"
        assert result.result_text == "Cancelled by user"


class TestProgressStreaming:
    """ISSUE-111: in-progress updates must reach the on_progress sink.

    The scheduler's Talk-edit callback calls ``asyncio.run()`` internally. The
    native brain drives ``on_progress`` from inside its own ``asyncio.run`` event
    loop, so calling the callback directly would invoke ``asyncio.run()`` from a
    running loop → RuntimeError, silently dropping every update. The brain must
    invoke the sync callback off the loop so its ``asyncio.run`` works.
    """

    def _scheduler_like_callback(self, log):
        """Mimic the scheduler progress callback: it calls asyncio.run()."""

        def callback(event):
            log.append(("received", type(event).__name__))

            async def _edit():
                return True

            asyncio.run(_edit())  # exactly what edit_talk_message does
            log.append(("edited", type(event).__name__))

        return callback

    def test_tool_and_text_progress_reach_sink(self, tmp_path):
        provider = MockProvider(
            [
                # Turn 1: intermediate text + a tool call.
                AssistantMessage(
                    content=[
                        TextContent(text="Working on it."),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                # Turn 2: final answer — its text becomes result_text.
                AssistantMessage(
                    content=[TextContent(text="Wrote the file.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        log: list[tuple[str, str]] = []
        req = _req("write a file", tmp_path, tools=["Write"])
        req.on_progress = self._scheduler_like_callback(log)
        result = _brain(provider).execute(req)

        assert result.success is True
        assert result.result_text == "Wrote the file."
        # Each received event must also be fully processed by the callback —
        # i.e. its internal asyncio.run() completed, not swallowed as a
        # RuntimeError ("asyncio.run() cannot be called from a running loop").
        received = [name for kind, name in log if kind == "received"]
        edited = [name for kind, name in log if kind == "edited"]
        assert "ToolUseEvent" in received
        assert "ToolEndEvent" in received  # NativeBrain emits tool completion
        # "Working on it." and "Wrote the file." both stream as TextDeltaEvents
        # (token-level answer streaming). The brain also flushes intermediate-turn
        # text as a whole-turn TextEvent so push surfaces (Talk) still get
        # narration — here turn 1's "Working on it." Only the *final* turn's
        # TextEvent is suppressed (its text becomes result_text); the executor
        # dedupes deltas-vs-TextEvent per surface downstream.
        assert "TextDeltaEvent" in received
        assert "TextEvent" in received
        assert edited == received  # every callback ran to completion

    def test_streaming_suppresses_whole_turn_text_event(self, tmp_path):
        # With delta streaming the answer arrives incrementally as
        # TextDeltaEvents; the whole-turn TextEvent flush must be suppressed so a
        # stream surface doesn't render the answer twice. result_text is intact.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="The answer is 42.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        log: list[tuple[str, str]] = []
        req = _req("question", tmp_path)
        req.on_progress = self._scheduler_like_callback(log)
        result = _brain(provider).execute(req)

        assert result.success is True
        assert result.result_text == "The answer is 42."
        received = [name for kind, name in log if kind == "received"]
        assert "TextDeltaEvent" in received  # answer streamed as deltas
        assert "TextEvent" not in received   # whole-turn flush suppressed

    def test_progress_callback_exception_does_not_crash_run(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="done")], stop_reason="end_turn")]
        )

        def boom(event):
            raise RuntimeError("talk server down")

        req = _req("hi", tmp_path)
        req.on_progress = boom
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "done"


class TestAnswerStreaming:
    """Stage 3 — provider TextDeltas are forwarded as ordered TextDeltaEvents."""

    def test_text_deltas_forwarded_in_order(self, tmp_path):
        from istota.brain import TextDeltaEvent, TextEvent

        class _DeltaProvider:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                yield StreamStart()
                for frag in ["Hel", "lo, ", "world"]:
                    yield TextDelta(text=frag)
                yield StreamDone(
                    message=AssistantMessage(
                        content=[TextContent(text="Hello, world")],
                        stop_reason="end_turn",
                    )
                )

        captured: list = []
        req = _req("hi", tmp_path)
        req.on_progress = lambda ev: captured.append(ev)
        result = _brain(_DeltaProvider()).execute(req)

        deltas = [e.text for e in captured if isinstance(e, TextDeltaEvent)]
        assert deltas == ["Hel", "lo, ", "world"]  # forwarded in arrival order
        # No whole-turn TextEvent — the deltas carried it (no double-render).
        assert not any(isinstance(e, TextEvent) for e in captured)
        # The canonical result still equals the assembled final-turn text.
        assert result.result_text == "Hello, world"


class TestThinkingStreaming:
    """Stage 4 — provider reasoning deltas surface as ThinkingDeltaEvents and stay
    out of result_text."""

    def test_thinking_deltas_forwarded_and_excluded_from_result(self, tmp_path):
        from istota.brain import TextDeltaEvent, ThinkingDeltaEvent

        class _ThinkingProvider:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                yield StreamStart()
                # Reasoning streams first (Anthropic-compat reasoning_content),
                # then the answer content.
                for frag in ["Let me ", "think… "]:
                    yield ThinkingDelta(thinking=frag)
                for frag in ["The ", "answer."]:
                    yield TextDelta(text=frag)
                yield StreamDone(
                    message=AssistantMessage(
                        content=[TextContent(text="The answer.")],
                        stop_reason="end_turn",
                    )
                )

        captured: list = []
        req = _req("hi", tmp_path)
        req.on_progress = lambda ev: captured.append(ev)
        result = _brain(_ThinkingProvider()).execute(req)

        thinking = [e.thinking for e in captured if isinstance(e, ThinkingDeltaEvent)]
        assert thinking == ["Let me ", "think… "]
        # Reasoning never lands in the answer stream or the canonical result.
        deltas = [e.text for e in captured if isinstance(e, TextDeltaEvent)]
        assert deltas == ["The ", "answer."]
        assert result.result_text == "The answer."
        assert "Let me" not in result.result_text


class TestFinalTurnIsTheAnswer:
    """ISSUE-211 — the durable answer is the *final* turn's text, never an
    earlier turn's between-tool-calls narration."""

    def test_empty_final_turn_does_not_promote_earlier_narration(self, tmp_path):
        provider = MockProvider(
            [
                # Turn 1: narration, then a tool call — mid-flight by construction.
                AssistantMessage(
                    content=[
                        TextContent(text="Let me check the calendar."),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                # Turn 2: the model stops with nothing to say.
                AssistantMessage(content=[], stop_reason="end_turn"),
            ]
        )
        result = _brain(provider).execute(_req("what's on today", tmp_path, tools=["Write"]))
        assert result.success is True
        assert "Let me check the calendar." not in result.result_text
        assert result.result_text.strip() == ""

    def test_orphaned_narration_still_reaches_progress(self, tmp_path):
        """The held block is no longer the answer, so it must be released as a
        progress event rather than suppressed into nothing."""
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        TextContent(text="Let me check the calendar."),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content=[], stop_reason="end_turn"),
            ]
        )
        captured: list = []
        req = _req("what's on today", tmp_path, tools=["Write"])
        req.on_progress = lambda ev: captured.append(ev)
        _brain(provider).execute(req)
        # Must be the whole-turn TextEvent, not the TextDeltaEvent that streams
        # regardless — asserting on `.text` alone would pass with the release
        # branch deleted, since both event types carry that attribute.
        assert any(
            isinstance(e, TextEvent) and e.text == "Let me check the calendar."
            for e in captured
        )

    def test_trace_keeps_document_order_through_composition(self, tmp_path):
        """End-to-end: the brain's own trace, composed the way the executor
        composes it, must not hand back narration.

        The agent loop executes a turn's tools before emitting its turn_end, so
        appending tool entries as they fire recorded them *ahead* of the text
        the model wrote first. The finality rule reads "text after the last tool
        call" as the final message, so an inverted trace made narration look
        like the answer. Asserting only at the brain boundary misses this.
        """
        from istota.session.result import _compose_full_result

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        TextContent(text="Let me check the calendar. " * 30),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content=[], stop_reason="end_turn"),
            ]
        )
        result = _brain(provider).execute(_req("what's on today", tmp_path, tools=["Write"]))
        trace = json.loads(result.execution_trace)
        kinds = [e["type"] for e in trace]
        assert kinds == ["text", "tool"], kinds

        class _T:
            id, source_type = 1, "talk"
            heartbeat_silent, scheduled_job_id = False, None

        composed = _compose_full_result(result.result_text, trace, task=_T())
        assert not composed.startswith("Let me check the calendar.")
        assert "without a final response" in composed

    def test_final_turn_text_still_wins_normally(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        TextContent(text="Let me check the calendar."),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Your meeting is at 3pm.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        result = _brain(provider).execute(_req("what's on today", tmp_path, tools=["Write"]))
        assert result.result_text == "Your meeting is at 3pm."


class TestTimeout:
    def test_wall_clock_timeout_aborts(self, tmp_path):
        # A provider that streams forever must be stopped at the task deadline,
        # tagged stop_reason="timeout" — not run unbounded (which would let the
        # scheduler reclaim and double-run the task).
        class _ForeverProvider:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                yield StreamStart()
                for _ in range(100000):
                    yield TextDelta(text="x")
                    await asyncio.sleep(0.02)

        req = _req("hang", tmp_path)
        req.timeout_seconds = 1
        result = _brain(_ForeverProvider()).execute(req)
        assert result.success is False
        assert result.stop_reason == "timeout"
        assert "timed out" in result.result_text


class TestRetryProvider:
    def test_streamstart_before_error_is_still_retried(self, tmp_path, monkeypatch):
        # _RetryingProvider must not treat StreamStart as a committed turn: a
        # transient error after StreamStart (but before any real delta) is
        # retryable. Regression for the StreamStart-commits-the-turn bug.
        from istota.brain import native as native_mod

        monkeypatch.setattr(native_mod, "_API_RETRY_BASE_DELAY", 0.0)

        calls = {"n": 0}

        class _Flaky:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                calls["n"] += 1
                yield StreamStart()
                if calls["n"] == 1:
                    yield StreamError(
                        message=AssistantMessage(
                            stop_reason="error", error_message="HTTP 503: overloaded"
                        )
                    )
                else:
                    yield StreamDone(
                        message=AssistantMessage(
                            content=[TextContent(text="recovered")],
                            stop_reason="end_turn",
                        )
                    )

        result = _brain(_Flaky()).execute(_req("hi", tmp_path))
        assert calls["n"] == 2  # retried despite the leading StreamStart
        assert result.success is True
        assert result.result_text == "recovered"


class TestCacheTelemetry:
    def test_hit_rate_logged_at_task_end(self, tmp_path, caplog):
        import logging

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(
                        input_tokens=100,
                        output_tokens=10,
                        cache_read_tokens=40,
                        cache_write_tokens=25,
                    ),
                    stop_reason="end_turn",
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            _brain(provider).execute(_req("hi", tmp_path))
        line = next((r.message for r in caplog.records if "cache" in r.message), None)
        assert line is not None
        assert "hit_rate=" in line
        assert "read=40" in line
        assert "input=100" in line
        # The documented cache_creation → cache_write_tokens mapping reaches the
        # task-end footer (closes the end-to-end loop, not just the SSE parse).
        assert "write=25" in line

    def test_hit_rate_clamped_at_100(self, tmp_path, caplog):
        # A non-conforming provider can report cache reads outside prompt_tokens;
        # the footer must not show a >100% rate.
        import logging

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(
                        input_tokens=10, output_tokens=5, cache_read_tokens=40
                    ),
                    stop_reason="end_turn",
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            _brain(provider).execute(_req("hi", tmp_path))
        line = next((r.message for r in caplog.records if "cache" in r.message), None)
        assert line is not None
        assert "hit_rate=100.0%" in line

    def test_zero_input_no_divide_by_zero(self, tmp_path, caplog):
        import logging

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=0, output_tokens=0),
                    stop_reason="end_turn",
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True  # did not crash on divide-by-zero
        line = next((r.message for r in caplog.records if "cache" in r.message), None)
        assert line is not None
        assert "hit_rate=0" in line


class TestFactory:
    def test_make_brain_native(self):
        cfg = BrainConfig(kind="native", native=NativeBrainConfig(model="claude-sonnet-4-6"))
        brain = make_brain(cfg)
        assert isinstance(brain, NativeBrain)

    def test_openai_compat_provider_does_not_translate_aliases(self):
        # A non-Anthropic endpoint gets explicit ids, not translated Anthropic
        # aliases (see test_native_resolution.py).
        brain = NativeBrain(NativeBrainConfig(model="qwen-x"), provider=object())
        assert brain.resolve_model_name("opus") == "opus"
