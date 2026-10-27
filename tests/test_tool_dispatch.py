"""`ToolDispatcher`: the per-call policy, over in-test `AgentTool`s.

No relay, no tool server: the dispatcher is handed tools and called. That is
what lets the unified tool server (#862) build it over its real tools and keep
this file.
"""

import ast
import asyncio
import subprocess
import sys
import time

import pytest

from istota.agent.tools import AgentTool, ToolResult
from istota.llm.types import ImageContent, TextContent, ToolParameter, ToolSchema
from istota.session.tools import build_remote_tools
from istota.session.tools import dispatch as dispatch_mod
from istota.session.tools.dispatch import HARNESS_TEXT_CAP_CHARS, ToolDispatcher
from tests.support.drift import source_of


def _tool(name, *, mode="sequential", params=None, execute=None, prepare=None):
    async def _default(call_id, args, on_update, abort):
        return ToolResult(content=[TextContent(text=f"{name} ok {args}")])

    return AgentTool(
        schema=ToolSchema(name=name, description=f"the {name} tool", parameters=params or []),
        execute=execute or _default,
        execution_mode=mode,
        prepare_arguments=prepare,
    )


def _text(result: ToolResult) -> str:
    return "".join(b.text for b in result.content if isinstance(b, TextContent))


class _Timeline:
    """Fixture tools that record when each call ran, to read overlap from."""

    def __init__(self):
        self.spans: dict[str, tuple[float, float]] = {}

    def tool(self, name, mode, hold=0.05):
        async def _execute(call_id, args, on_update, abort):
            start = time.monotonic()
            await asyncio.sleep(hold)
            self.spans[call_id] = (start, time.monotonic())
            return ToolResult(content=[TextContent(text=call_id)])

        return _tool(name, mode=mode, execute=_execute)

    def overlap(self, a, b) -> bool:
        a0, a1 = self.spans[a]
        b0, b1 = self.spans[b]
        return a0 < b1 and b0 < a1


class TestTheSubset:
    async def test_a_name_not_offered_never_reaches_execute(self):
        ran = []

        async def _execute(call_id, args, on_update, abort):
            ran.append(call_id)
            return ToolResult(content=[TextContent(text="ran")])

        tools = {"Read": _tool("Read", mode="parallel"), "Bash": _tool("Bash", execute=_execute)}
        d = ToolDispatcher(tools, offered=["Read"], text_cap=None)
        result = await d.call("c1", "Bash", {"command": "id"}, None, asyncio.Event())
        assert result.is_error is True
        assert "Bash" in _text(result)
        assert ran == []

    async def test_an_unknown_name_is_an_error_result(self):
        d = ToolDispatcher({"Read": _tool("Read")}, offered=["Read", "Nope"], text_cap=None)
        result = await d.call("c1", "Nope", {}, None, asyncio.Event())
        assert result.is_error is True
        assert d.offered == ("Read",)

    async def test_an_offered_name_runs(self):
        d = ToolDispatcher({"Read": _tool("Read")}, offered=["Read"], text_cap=None)
        result = await d.call("c1", "Read", {}, None, asyncio.Event())
        assert result.is_error is False
        assert _text(result).startswith("Read ok")


class TestArgumentPreparation:
    async def test_nulls_are_dropped_and_strings_coerced(self):
        seen = []

        async def _execute(call_id, args, on_update, abort):
            seen.append(args)
            return ToolResult(content=[TextContent(text="ok")])

        tool = _tool("Read", execute=_execute, params=[
            ToolParameter(name="file_path", type="string"),
            ToolParameter(name="limit", type="integer", required=False),
            ToolParameter(name="offset", type="integer", required=False),
        ])
        d = ToolDispatcher({"Read": tool}, offered=["Read"], text_cap=None)
        await d.call("c1", "Read", {"file_path": "/a", "limit": None, "offset": "5"}, None, asyncio.Event())
        assert seen == [{"file_path": "/a", "offset": 5}]

    async def test_a_missing_required_argument_is_an_error_result(self):
        ran = []

        async def _execute(call_id, args, on_update, abort):
            ran.append(args)
            return ToolResult(content=[TextContent(text="ok")])

        tool = _tool("Read", execute=_execute, params=[ToolParameter(name="file_path", type="string")])
        d = ToolDispatcher({"Read": tool}, offered=["Read"], text_cap=None)
        result = await d.call("c1", "Read", {"file_path": None}, None, asyncio.Event())
        assert result.is_error is True
        assert "Missing required argument(s): file_path" in _text(result)
        assert ran == []

    async def test_a_prepare_arguments_that_raises_is_an_error_result(self):
        def _boom(args):
            raise ValueError("bad edits")

        d = ToolDispatcher({"Edit": _tool("Edit", prepare=_boom)}, offered=["Edit"], text_cap=None)
        result = await d.call("c1", "Edit", {}, None, asyncio.Event())
        assert result.is_error is True
        assert "Argument preparation failed: bad edits" in _text(result)

    async def test_an_execute_that_raises_is_an_error_result(self):
        async def _execute(call_id, args, on_update, abort):
            raise RuntimeError("server gone")

        d = ToolDispatcher({"Bash": _tool("Bash", execute=_execute)}, offered=["Bash"], text_cap=None)
        result = await d.call("c1", "Bash", {}, None, asyncio.Event())
        assert result.is_error is True
        assert "server gone" in _text(result)

    async def test_arguments_on_update_and_abort_reach_execute(self):
        seen = []

        async def _execute(call_id, args, on_update, abort):
            seen.append((call_id, on_update, abort))
            return ToolResult(content=[])

        abort = asyncio.Event()

        async def on_update(text):
            return None

        d = ToolDispatcher({"Bash": _tool("Bash", execute=_execute)}, offered=["Bash"], text_cap=None)
        await d.call("c9", "Bash", {}, on_update, abort)
        assert seen == [("c9", on_update, abort)]


class TestTheLock:
    async def test_parallel_calls_run_together(self):
        tl = _Timeline()
        d = ToolDispatcher({"Read": tl.tool("Read", "parallel")}, offered=["Read"], text_cap=None)
        await asyncio.gather(
            d.call("r1", "Read", {}, None, asyncio.Event()),
            d.call("r2", "Read", {}, None, asyncio.Event()),
        )
        assert tl.overlap("r1", "r2")

    async def test_a_sequential_call_blocks_a_concurrent_parallel_one(self):
        tl = _Timeline()
        tools = {"Read": tl.tool("Read", "parallel"), "Write": tl.tool("Write", "sequential", hold=0.1)}
        d = ToolDispatcher(tools, offered=["Read", "Write"], text_cap=None)

        async def read_later():
            await asyncio.sleep(0.02)
            return await d.call("r1", "Read", {}, None, asyncio.Event())

        await asyncio.gather(d.call("w1", "Write", {}, None, asyncio.Event()), read_later())
        assert not tl.overlap("w1", "r1")
        assert tl.spans["r1"][0] >= tl.spans["w1"][1]

    async def test_a_sequential_call_waits_for_running_parallel_ones(self):
        tl = _Timeline()
        tools = {"Read": tl.tool("Read", "parallel", hold=0.1), "Write": tl.tool("Write", "sequential")}
        d = ToolDispatcher(tools, offered=["Read", "Write"], text_cap=None)

        async def write_later():
            await asyncio.sleep(0.02)
            return await d.call("w1", "Write", {}, None, asyncio.Event())

        await asyncio.gather(d.call("r1", "Read", {}, None, asyncio.Event()), write_later())
        assert not tl.overlap("r1", "w1")

    async def test_a_waiting_writer_holds_back_later_readers(self):
        tl = _Timeline()
        tools = {"Read": tl.tool("Read", "parallel", hold=0.1), "Write": tl.tool("Write", "sequential", hold=0.05)}
        d = ToolDispatcher(tools, offered=["Read", "Write"], text_cap=None)

        async def after(delay, call_id, name):
            await asyncio.sleep(delay)
            return await d.call(call_id, name, {}, None, asyncio.Event())

        await asyncio.gather(
            d.call("r1", "Read", {}, None, asyncio.Event()),
            after(0.02, "w1", "Write"),
            after(0.04, "r2", "Read"),
        )
        # r2 arrived while w1 was waiting on r1, so it runs after w1.
        assert tl.spans["w1"][0] >= tl.spans["r1"][1]
        assert tl.spans["r2"][0] >= tl.spans["w1"][1]

    async def test_two_sequential_calls_do_not_overlap(self):
        tl = _Timeline()
        d = ToolDispatcher({"Bash": tl.tool("Bash", "sequential")}, offered=["Bash"], text_cap=None)
        await asyncio.gather(
            d.call("b1", "Bash", {}, None, asyncio.Event()),
            d.call("b2", "Bash", {}, None, asyncio.Event()),
        )
        assert not tl.overlap("b1", "b2")

    async def test_a_cancelled_waiter_does_not_wedge_the_lock(self):
        tl = _Timeline()
        tools = {"Read": tl.tool("Read", "parallel", hold=0.1), "Write": tl.tool("Write", "sequential")}
        d = ToolDispatcher(tools, offered=["Read", "Write"], text_cap=None)
        reader = asyncio.create_task(d.call("r1", "Read", {}, None, asyncio.Event()))
        await asyncio.sleep(0.02)
        writer = asyncio.create_task(d.call("w1", "Write", {}, None, asyncio.Event()))
        await asyncio.sleep(0.02)
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        await reader
        result = await asyncio.wait_for(d.call("w2", "Write", {}, None, asyncio.Event()), 2)
        assert result.is_error is False
        result = await asyncio.wait_for(d.call("r2", "Read", {}, None, asyncio.Event()), 2)
        assert result.is_error is False


class TestTheTextCap:
    async def test_text_over_the_cap_is_cut_with_the_recovery_line(self):
        body = "x" * 120

        async def _execute(call_id, args, on_update, abort):
            return ToolResult(content=[TextContent(text=body)])

        d = ToolDispatcher({"Read": _tool("Read", execute=_execute)}, offered=["Read"], text_cap=100)
        result = await d.call("c1", "Read", {}, None, asyncio.Event())
        assert result.content[0].text == "x" * 100
        recovery = result.content[-1].text
        assert "120" in recovery and "100" in recovery
        assert "offset" in recovery
        assert result.is_error is False

    async def test_the_cap_counts_every_text_block_and_leaves_images_alone(self):
        image = ImageContent(media_type="image/png", data="A" * 500, display_name="a.png")

        async def _execute(call_id, args, on_update, abort):
            return ToolResult(content=[
                TextContent(text="a" * 60), image, TextContent(text="b" * 60), TextContent(text="c" * 10),
            ])

        d = ToolDispatcher({"Read": _tool("Read", execute=_execute)}, offered=["Read"], text_cap=100)
        result = await d.call("c1", "Read", {}, None, asyncio.Event())
        assert result.content[0].text == "a" * 60
        assert result.content[1] is image
        assert result.content[2].text == "b" * 40
        assert len(result.content) == 4
        assert "130" in result.content[3].text

    async def test_the_bash_recovery_line_names_the_spill_file(self):
        async def _execute(call_id, args, on_update, abort):
            return ToolResult(content=[TextContent(text="y" * 200)])

        d = ToolDispatcher({"Bash": _tool("Bash", execute=_execute)}, offered=["Bash"], text_cap=100)
        result = await d.call("c1", "Bash", {}, None, asyncio.Event())
        assert "full output" in result.content[-1].text

    async def test_text_at_the_cap_is_untouched(self):
        async def _execute(call_id, args, on_update, abort):
            return ToolResult(content=[TextContent(text="z" * 100)])

        d = ToolDispatcher({"Read": _tool("Read", execute=_execute)}, offered=["Read"], text_cap=100)
        result = await d.call("c1", "Read", {}, None, asyncio.Event())
        assert [b.text for b in result.content] == ["z" * 100]

    async def test_no_cap_leaves_text_alone(self):
        async def _execute(call_id, args, on_update, abort):
            return ToolResult(content=[TextContent(text="q" * 100_000)])

        d = ToolDispatcher({"Read": _tool("Read", execute=_execute)}, offered=["Read"], text_cap=None)
        result = await d.call("c1", "Read", {}, None, asyncio.Event())
        assert result.content[0].text == "q" * 100_000

    def test_the_cap_sits_below_the_clis_persistence_threshold(self):
        # Stage 1 measured the CLI's first threshold at 50,000 characters;
        # above it the model gets a preview of a file it cannot read.
        assert HARNESS_TEXT_CAP_CHARS == 45_000


class TestTheMcpToolList:
    def test_the_proxied_six_render_with_read_only_on_the_parallel_ones(self):
        tools = {t.schema.name: t for t in build_remote_tools(None)}
        d = ToolDispatcher(tools, offered=["Read", "Grep", "Glob", "Write", "Edit", "Bash"], text_cap=None)
        listed = {entry["name"]: entry for entry in d.mcp_tool_list()}
        assert set(listed) == {"Read", "Grep", "Glob", "Write", "Edit", "Bash"}
        assert {n for n, e in listed.items() if e["read_only"]} == {"Read", "Grep", "Glob"}
        assert listed["Read"]["description"] == tools["Read"].schema.description

    def test_only_offered_tools_are_listed(self):
        tools = {t.schema.name: t for t in build_remote_tools(None)}
        d = ToolDispatcher(tools, offered=["Read", "Grep"], text_cap=None)
        assert [e["name"] for e in d.mcp_tool_list()] == ["Read", "Grep"]

    def test_bash_is_listed_without_exclude_from_context(self):
        tools = {t.schema.name: t for t in build_remote_tools(None)}
        d = ToolDispatcher(tools, offered=["Bash"], text_cap=None)
        (bash,) = d.mcp_tool_list()
        assert "exclude_from_context" not in bash["input_schema"]["properties"]
        assert "command" in bash["input_schema"]["properties"]
        # The shared schema object is not edited in place.
        assert any(p.name == "exclude_from_context" for p in tools["Bash"].schema.parameters)


#: Modules the dispatcher may never load: anything brain-side or daemon-side,
#: which the tool server (where the unified spec moves this) cannot import.
_FORBIDDEN_PREFIXES = (
    "istota.brain",
    "istota.executor",
    "istota.scheduler",
    "istota.db",
    "istota.config",
    "istota.transport",
    "istota.webui",
    "istota.skills",
    "istota.llm.openai_compat",
)


def test_the_dispatcher_imports_nothing_brain_or_daemon_side():
    probe = (
        "import sys\n"
        "import istota.session.tools.dispatch\n"
        "print(','.join(sorted(m for m in sys.modules if m.startswith('istota'))))\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    loaded = [m for m in tail.split(",") if m]
    assert "istota.session.tools.dispatch" in loaded
    bad = [m for m in loaded if m.startswith(_FORBIDDEN_PREFIXES)]
    assert bad == [], bad


def test_the_dispatchers_own_imports_are_agent_and_llm_types():
    """The direct imports, read from the module: `istota.agent`, `istota.llm.types`
    and the schema leaf. Anything wider is a dependency the tool server takes on."""
    tree = ast.parse(source_of(dispatch_mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    istota_imports = {m for m in imported if m.startswith("istota")}
    assert istota_imports <= {
        "istota.agent.coercion",
        "istota.agent.tools",
        "istota.llm.types",
        "istota.llm.tool_json_schema",
    }, istota_imports
