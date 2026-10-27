"""A stand-in for `istota.sandbox.mcp_adapter`, run by the CLI inside the image.

Stdlib only and self-contained, because it is bind-mounted into the container
and run with `python3 -I` from a directory that holds nothing else. It answers
the handshake and `tools/list` with the six names the bridge offers and runs no
tool: `tests/image/test_cli_harness_init.py` reads only `system/init`, which
the CLI emits before any model request. Stage 6 of the CLI-brain spec replaces
it with the real adapter and bridge.
"""

import json
import sys

TOOLS = ("Read", "Write", "Edit", "Bash", "Grep", "Glob")
READ_ONLY = ("Read", "Grep", "Glob")


def _tool(name: str) -> dict:
    return {
        "name": name,
        "description": f"Stub {name}.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": name in READ_ONLY},
    }


def _reply(msg_id, result=None, error=None) -> None:
    body = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _reply(msg_id, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "istota", "version": "0"},
            })
        elif method == "tools/list":
            _reply(msg_id, {"tools": [_tool(name) for name in TOOLS]})
        elif method == "ping":
            _reply(msg_id, {})
        elif msg_id is not None:
            # `server/discover`, which 2.1.296 sends before `initialize`, lands
            # here; the CLI falls back to `initialize` on this error.
            _reply(msg_id, error={"code": -32601, "message": "method not found"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
