"""Tool names from the CLI tool harness's MCP server, read back as the built-ins.

With the harness on, the CLI calls our tools as `mcp__istota__Bash` and so on.
Every name match downstream (progress labels, the trace's `raw`, the relay
clean-turn flag, the private relay redaction, the image audit) is written
against `Bash` / `Read`, so the parse sites canonicalise the name once, before
building the `ToolUseEvent`. The guard below is what keeps a parse site from
handing a helper the unstripped name: missed, the private relay redaction
would write relay, room and wallet arguments into the trace.
"""

import json
from types import SimpleNamespace

import pytest

from istota.agent import events as agent_events
from istota.agent.events import (
    HARNESS_MCP_SERVER,
    PRIVATE_RELAY_TOOL_DESCRIPTION,
    WEB_FETCH_MCP_SERVER,
    READ_DESCRIPTION_PREFIX,
    canonical_tool_name,
)
from istota.brain import _events as stream_events
from istota.brain._events import parse_stream_line

PREFIX = f"mcp__{HARNESS_MCP_SERVER}__"
WEB_PREFIX = f"mcp__{WEB_FETCH_MCP_SERVER}__"
WEB_CALL = ("WebFetch", {"url": "https://example.org/page"})

RELAY = {"command": "istota-skill relay ask bob --request-key q 'What time?'"}
WALLET = {"command": "istota-skill wallet request --amount 20 --merchant shop"}

CALLS = [
    ("Bash", RELAY),
    ("Bash", WALLET),
    ("Bash", {"command": "ls -la /tmp"}),
    ("Read", {"file_path": "/tmp/photo.png"}),
    ("Write", {"file_path": "/tmp/out.txt", "content": "hi"}),
    ("Edit", {"file_path": "/tmp/out.txt", "old_string": "a", "new_string": "b"}),
    ("Grep", {"pattern": "TODO"}),
    ("Glob", {"pattern": "**/*.py"}),
]


class TestCanonicalToolName:
    def test_the_server_name_is_the_harness_constant(self):
        assert HARNESS_MCP_SERVER == "istota"
        assert WEB_FETCH_MCP_SERVER == "istota_web"

    def test_the_webfetch_servers_prefix_is_stripped(self):
        assert canonical_tool_name("mcp__istota_web__WebFetch") == "WebFetch"

    @pytest.mark.parametrize("name", ["Bash", "Read", "Write", "Edit", "Grep", "Glob"])
    def test_our_server_prefix_is_stripped(self, name):
        assert canonical_tool_name(f"mcp__istota__{name}") == name

    @pytest.mark.parametrize("name", [
        "Bash",
        "WebSearch",
        "mcp__other__Bash",  # another server's tool is not ours
        "mcp__istota_x__Bash",
        "MCP__istota__Bash",
        "mcp__istota__",  # no tool name at all
        "mcp__istota_web__",
        "mcp__istota_webx__WebFetch",
        "",
    ])
    def test_any_other_name_is_unchanged(self, name):
        assert canonical_tool_name(name) == name

    def test_stripped_once(self):
        assert canonical_tool_name("mcp__istota__mcp__istota__Bash") == "mcp__istota__Bash"

    def test_a_non_string_is_returned_as_is(self):
        assert canonical_tool_name(None) is None
        assert canonical_tool_name(7) == 7


def _frame(name, args, call_id="t1"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": args}]}}


def _via_stream(name, args):
    return parse_stream_line(json.dumps(_frame(name, args)))


SITES = {
    "stream": lambda name, args, tmp_path: _via_stream(name, args),
}


class TestTheParseSites:
    @pytest.mark.parametrize("site", sorted(SITES))
    @pytest.mark.parametrize("name,args", CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)])
    def test_an_mcp_name_reads_as_the_builtin(self, site, name, args, tmp_path):
        parse = SITES[site]
        mcp = parse(PREFIX + name, args, tmp_path)
        plain = parse(name, args, tmp_path)
        assert mcp.tool_name == name
        assert mcp == plain

    @pytest.mark.parametrize("site", sorted(SITES))
    def test_the_webfetch_server_name_reads_as_the_builtin(self, site, tmp_path):
        name, args = WEB_CALL
        mcp = SITES[site](WEB_PREFIX + name, args, tmp_path)
        assert mcp.tool_name == name
        assert mcp == SITES[site](name, args, tmp_path)

    @pytest.mark.parametrize("site", sorted(SITES))
    @pytest.mark.parametrize("args", [RELAY, WALLET], ids=["relay", "wallet"])
    def test_the_private_relay_redaction_holds(self, site, args, tmp_path):
        event = SITES[site](PREFIX + "Bash", args, tmp_path)
        assert event.description == PRIVATE_RELAY_TOOL_DESCRIPTION
        assert event.invocation == ""

    @pytest.mark.parametrize("site", sorted(SITES))
    def test_a_lone_relay_ask_is_flagged(self, site, tmp_path):
        assert SITES[site](PREFIX + "Bash", RELAY, tmp_path).lone_relay_ask is True

    @pytest.mark.parametrize("site", sorted(SITES))
    def test_another_servers_bash_is_not_ours(self, site, tmp_path):
        event = SITES[site]("mcp__other__Bash", RELAY, tmp_path)
        assert event.tool_name == "mcp__other__Bash"
        assert event.lone_relay_ask is False

    def test_the_image_audit_counts_an_mcp_read(self):
        from istota.executor import unread_images

        event = _via_stream(PREFIX + "Read", {"file_path": "/work/photo.png"})
        assert event.description.startswith(READ_DESCRIPTION_PREFIX)
        trace = json.dumps([{"type": "tool", "text": event.description}])
        images = [SimpleNamespace(path="/work/photo.png", display_name="photo.png")]
        assert unread_images(images, trace) == []


class TestTheGuard:
    """No helper ever sees `mcp__istota__…` from a parse site."""

    def test_no_helper_receives_an_unstripped_name(self, monkeypatch, tmp_path):
        received = []

        def recording(helper_name, real):
            def wrapper(name, input_data):
                received.append((helper_name, name))
                return real(name, input_data)
            return wrapper

        for helper in ("_describe_tool_use", "_tool_invocation", "_lone_relay_ask"):
            real = getattr(agent_events, helper)
            monkeypatch.setattr(stream_events, helper, recording(helper, real))
        monkeypatch.setattr(agent_events, "_private_relay_tool",
                            recording("_private_relay_tool", agent_events._private_relay_tool))

        for site in SITES.values():
            for name, args in CALLS:
                site(PREFIX + name, args, tmp_path)
            site(WEB_PREFIX + WEB_CALL[0], WEB_CALL[1], tmp_path)

        helpers = {helper for helper, _ in received}
        # The guard is vacuous unless every helper was reached through the sites.
        assert helpers == {"_describe_tool_use", "_tool_invocation", "_lone_relay_ask", "_private_relay_tool"}
        assert [name for _, name in received if name.startswith("mcp__")] == []
        assert ("_describe_tool_use", "WebFetch") in received
