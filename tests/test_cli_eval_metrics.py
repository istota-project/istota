"""`tests/live/cli_eval/runner.metrics_from_stream`, in the default suite.

The live eval is the only thing that produces a real transcript and it costs
quota, so the counting it rests on is held here against hand-written
stream-json, in the shapes the pinned CLI emits for each arm.
"""

from __future__ import annotations

import json

import pytest

from tests.live.cli_eval import runner


def _write(tmp_path, frames: list) -> object:
    path = tmp_path / "stream.jsonl"
    lines = [f if isinstance(f, str) else json.dumps(f) for f in frames]
    path.write_text("\n".join(lines) + "\n")
    return path


def _init(tools, servers=()):
    return {"type": "system", "subtype": "init", "tools": list(tools),
            "mcp_servers": [{"name": n, "status": s, "source": "dynamic"} for n, s in servers]}


def _use(call_id, name, **args):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": args}]}}


def _result(call_id, content, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}]}}


def _final(text="done", turns=4, cost=0.12, is_error=False):
    return {"type": "result", "subtype": "success", "is_error": is_error, "num_turns": turns,
            "result": text, "total_cost_usd": cost,
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 300, "cache_creation_input_tokens": 40}}


def test_an_mcp_run_counts_like_a_builtin_one(tmp_path):
    path = _write(tmp_path, [
        _init(["WebSearch", "mcp__spike__Bash", "mcp__spike__Edit"], [("spike", "connected")]),
        "not json",
        _use("a", "mcp__spike__Bash", command="bash sources/fetch_rate.sh XYZ"),
        # The tool server's Bash leaves is_error off on a non-zero exit.
        _result("a", [{"type": "text", "text": "connection refused\n[exit code: 7]"}]),
        _use("b", "mcp__spike__Edit", file_path="x.py"),
        _result("b", [{"type": "text", "text": "old_string not found"}], is_error=True),
        _use("c", "mcp__spike__Bash", command="cat data/rates_cache.json"),
        _result("c", [{"type": "text", "text": '{"XYZ": "0.73519"}'}]),
        {"type": "system", "subtype": "compact_boundary"},
        _final(),
    ])
    m = runner.metrics_from_stream(path, success_marker="0.73519")
    assert m["init_tools"] == ["WebSearch", "mcp__spike__Bash", "mcp__spike__Edit"]
    assert m["init_mcp_servers"] == [{"name": "spike", "status": "connected"}]
    assert m["tool_calls"] == 3
    assert m["tool_errors"] == 2
    assert m["edit_failures"] == 1
    assert m["tool_errors_before_success"] == 2
    assert m["tools_used"] == {"Bash": 2, "Edit": 1}
    assert m["raw_tool_names"] == ["mcp__spike__Bash", "mcp__spike__Edit"]
    assert m["compactions"] == 1
    assert (m["turns"], m["stop_reason"], m["is_error"]) == (4, "success", False)
    assert m["cost_usd"] == pytest.approx(0.12)
    assert (m["cache_read_tokens"], m["cache_write_tokens"]) == (300, 40)


def test_builtin_string_results_images_and_persisted_output(tmp_path):
    path = _write(tmp_path, [
        _init(["Bash", "Read"]),
        _use("a", "Read", file_path="sign.png"),
        _result("a", [{"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                      {"type": "text", "text": "[Image: source: /cfg/projects/p/tool-results/b.png]"}]),
        _use("b", "Read", file_path="/cfg/projects/p/tool-results/b.png"),
        _result("b", "<persisted-output>Output too large (49.7KB)</persisted-output>"),
        _use("c", "Bash", command="false"),
        _result("c", "Exit code 1", is_error=True),
        _final("OSPREY 318", turns=3),
    ])
    m = runner.metrics_from_stream(path)
    assert m["init_mcp_servers"] == []
    assert m["image_results"] == 1
    assert m["config_dir_reads"] == 1
    assert m["persisted_outputs"] == 1
    assert m["tool_errors"] == 1
    assert m["tool_errors_before_success"] is None
    assert m["result_text"] == "OSPREY 318"


def test_a_stream_with_no_result_frame_reads_as_no_stop(tmp_path):
    m = runner.metrics_from_stream(_write(tmp_path, [_init([])]))
    assert m["stop_reason"] == ""
    assert m["is_error"] is None
    assert m["tool_calls"] == 0


@pytest.mark.parametrize("name,expected", [
    ("mcp__spike__Bash", "Bash"),
    ("mcp__istota__Read", "Read"),
    ("mcp__my_server__Glob", "Glob"),
    ("Bash", "Bash"),
])
def test_canonical_tool_name(name, expected):
    assert runner.canonical_tool_name(name) == expected


@pytest.mark.parametrize("base_url", ["https://openrouter.ai/api", "http://localhost:8080"])
def test_auth_refuses_a_third_party_endpoint(base_url):
    with pytest.raises(runner.AuthError):
        runner.anthropic_auth({"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": "k"})


def test_auth_prefers_the_subscription_token_and_never_forwards_an_auth_token():
    mode, env = runner.anthropic_auth({
        "CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    })
    assert mode == "subscription"
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
    with pytest.raises(runner.AuthError):
        runner.anthropic_auth({"ANTHROPIC_AUTH_TOKEN": "x"})


def test_the_spike_argv_widens_the_deny_list_and_restricts_builtins(tmp_path):
    system = tmp_path / "system.md"
    system.write_text("x")
    req = runner.base_request(system, "claude-x", None, 60)
    argv = runner.build_argv("spike", "claude", req, mcp_config=tmp_path / "mcp.json")
    deny = argv[argv.index("--disallowedTools") + 1:argv.index("--tools")]
    assert set(deny) == set(runner.HARNESS_DENIED_TOOLS)
    assert "WebSearch" not in deny
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert "--strict-mcp-config" in argv
    assert argv.count("--disallowedTools") == 1
    builtin = runner.build_argv("builtin", "claude", req)
    assert builtin[builtin.index("--disallowedTools") + 1:][:2] == ["Agent", "Workflow"]
    assert "--tools" not in builtin
