"""A stand-in for the `claude` CLI on a tool-harness run, stdlib only.

It does what the pinned CLI does with the harness argv, as far as the brain
can see: it starts every MCP server named in `--mcp-config` with its own
environment, runs the handshake and `tools/list` on each, prints a
`system/init` frame listing the `--tools` built-ins and
`mcp__<server>__<name>` for each tool a server offered, then makes the planned tool calls through the server, printing
each as an assistant `tool_use` frame, and ends with a `result` frame holding
the call results.

Without `--mcp-config` it is a text-only run and answers with one `result`
frame. The old wrapped path, where the CLI ran tools itself, is gone.

What it does is read from a JSON plan named by `FAKE_CLAUDE_PLAN`, or else
`plan.json` beside this file (the smoke tier's case: the CLI's environment is
built from an allowlist and carries no variable of ours):

- `record`: a file to write the argv, cwd, pid and environment to.
- `init_extra_tools`, `init_drop_tools`, `init_server_status`,
  `init_drop_servers`, `init_extra_servers`: what to misreport in
  `system/init`.
- `calls`: `[{"name": "Bash", "args": {...}}]`, made through the first MCP
  server, or the one a call names with `"server"`.
- `host_probe`: a shell command the fake runs itself first, from the CLI's
  unwrapped view, its output put ahead of the call results.
- `run_in_cli`: on a harness run, make the planned `Bash` calls in the fake's
  own process rather than through the MCP server, as a CLI whose own Bash were
  left on would. The smoke witness's negative control
  (`scripts/test-image-negative-control.sh cli-witness`).

`{harness_dir}` in a call's arguments or the host probe is replaced with the
harness directory, read from the MCP config.
- `sleep_before_calls`, `sleep_before_result`, `sleep_after_result`: seconds.
- `exit`: the exit status, 0 by default.

Run as `python3 fake_claude_cli.py <argv>` from a `claude` shim on PATH.
"""

import json
import os
import subprocess
import sys
import time


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _flag(argv: list[str], name: str):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def _servers(config_path: str) -> dict:
    with open(config_path) as fh:
        return json.load(fh)["mcpServers"]


class _Mcp:
    def __init__(self, name: str, server: dict):
        self.name = name
        # The real CLI passes its whole environment, whatever `env` says.
        self.proc = subprocess.Popen(
            [server["command"], *server["args"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self._next = 0

    def request(self, method: str, params=None) -> dict:
        self._next += 1
        message = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"error": {"message": "server closed"}}
            reply = json.loads(line)
            if reply.get("id") == self._next:
                return reply

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def _plan_path() -> str:
    named = os.environ.get("FAKE_CLAUDE_PLAN")
    if named:
        return named
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.json")


def _without_mcp(argv: list[str], plan: dict) -> int:
    """A run with no `--mcp-config`: text-only.

    `--output-format json` (the non-streaming path) prints the frames as one
    array at the end, as the pinned CLI does with `--verbose`; otherwise one
    frame per line."""
    frames = [{"type": "result", "subtype": "success", "result": "done"}]
    if _flag(argv, "--output-format") == "json":
        sys.stdout.write(json.dumps(frames) + "\n")
        sys.stdout.flush()
    else:
        for frame in frames:
            _emit(frame)
    return 0


def main(argv: list[str]) -> int:
    with open(_plan_path()) as fh:
        plan = json.load(fh)
    sys.stdin.read()

    if plan.get("record"):
        with open(plan["record"], "w") as fh:
            json.dump({"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
                       "env": dict(os.environ)}, fh)

    tools_flag = _flag(argv, "--tools")
    builtins = [t for t in (tools_flag or "").split(",") if t]
    config_path = _flag(argv, "--mcp-config")
    if config_path is None:
        return _without_mcp(argv, plan)
    servers = [_Mcp(name, entry) for name, entry in _servers(config_path).items()]
    by_name = {mcp.name: mcp for mcp in servers}
    places = {"{harness_dir}": os.path.dirname(config_path)}

    def subst(value):
        if isinstance(value, str):
            for key, real in places.items():
                value = value.replace(key, real)
            return value
        if isinstance(value, dict):
            return {k: subst(v) for k, v in value.items()}
        if isinstance(value, list):
            return [subst(v) for v in value]
        return value
    tools = list(builtins)
    statuses = []
    for mcp in servers:
        status = "connected"
        init = mcp.request("initialize", {"protocolVersion": "2025-11-25"})
        if "result" in init:
            mcp.notify("notifications/initialized")
            listed = mcp.request("tools/list").get("result", {}).get("tools", [])
            tools += [f"mcp__{mcp.name}__{tool['name']}" for tool in listed]
        else:
            status = "failed"
        if mcp.name not in plan.get("init_drop_servers", []):
            statuses.append({"name": mcp.name, "status": plan.get("init_server_status", status),
                             "source": "dynamic"})
    for name in plan.get("init_extra_servers", []):
        statuses.append({"name": name, "status": "connected", "source": "dynamic"})

    tools += plan.get("init_extra_tools", [])
    tools = [t for t in tools if t not in plan.get("init_drop_tools", [])]
    _emit({
        "type": "system", "subtype": "init", "model": "claude-fake",
        "tools": tools,
        "mcp_servers": statuses,
    })
    time.sleep(plan.get("sleep_before_calls", 0))

    texts = []
    if plan.get("host_probe"):
        # The same probe the model's Bash runs, from the CLI's own (host) view.
        probe = subprocess.run(["/bin/sh", "-c", subst(plan["host_probe"])],
                               capture_output=True, text=True)
        texts.append("HOST:\n" + probe.stdout)
    for index, call in enumerate(plan.get("calls", [])):
        call_id = f"toolu_{index}"
        args = subst(call.get("args", {}))
        mcp = by_name[call["server"]] if "server" in call else servers[0]
        _emit({"type": "assistant", "message": {"id": f"msg_{index}", "content": [{
            "type": "tool_use", "id": call_id, "name": f"mcp__{mcp.name}__{call['name']}",
            "input": args,
        }]}})
        if plan.get("run_in_cli") and call["name"] == "Bash":
            ran = subprocess.run(["/bin/sh", "-c", args.get("command", "")],
                                 capture_output=True, text=True)
            texts.append(ran.stdout + ran.stderr)
            continue
        reply = mcp.request("tools/call", {"name": call["name"], "arguments": args})
        result = reply.get("result") or {}
        text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
        if result.get("isError") or "error" in reply:
            text = "ERROR: " + (text or json.dumps(reply.get("error")))
        texts.append(text)
    time.sleep(plan.get("sleep_before_result", 0))
    for mcp in servers:
        mcp.close()

    _emit({"type": "result", "subtype": "success",
           "result": plan.get("result", "\n".join(texts) or "done")})
    time.sleep(plan.get("sleep_after_result", 0))
    return int(plan.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
