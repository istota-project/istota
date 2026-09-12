"""Thin client for the skill proxy.

Console script entry point ``istota-skill``. When ``ISTOTA_SKILL_PROXY_SOCK``
is set, connects to the proxy socket and delegates execution — the skill then
runs host-side, where the databases are. Otherwise it runs the skill module
directly via subprocess, which is right for the unsandboxed daemon callers
(cron ``command:`` rows, heartbeat shell-commands, an operator shell) and
refused inside the sandbox, where the databases are masked out.

Usage::

    istota-skill email send --to bob@example.com --subject "Hi" --body "Hello"
    istota-skill calendar list --date today
"""

import json
import os
import socket
import subprocess
import sys

#: How long this client waits on the proxy socket before giving up, when the
#: deployment says nothing else.
#:
#: This process runs *inside* the sandbox, where there is no config file to
#: read, so the operator's value arrives as ``ISTOTA_SKILL_CLIENT_WAIT`` —
#: exported by ``task_env`` from ``security.skill_client_wait_seconds`` — and
#: this constant is the fallback when the export is absent or unusable
#: (ISSUE-450). Every server-side timeout has to fit under the wait with room
#: for the proxy's own bookkeeping, or the client gives up first and the model
#: is told the proxy answered nothing: a worse failure than the short budget,
#: and one that looks like a wedged proxy rather than a timeout. The proxy
#: enforces that fit from the *config field*, never from the environment
#: variable — the variable lives in the model's own environment, so a task
#: that rewrites its copy changes only its own patience, not the bound the
#: operator set.
SKILL_CLIENT_WAIT_SECONDS = 600

#: The largest wait either end will honour: one day. Not a policy choice so
#: much as a robustness one — `socket.settimeout` raises `OverflowError` on a
#: large enough value (measured at 10**10 on CPython), which would replace the
#: call with a traceback, and the value is model-writable here. The proxy
#: clamps its configured wait to the same number so the two ends stay in
#: agreement.
MAX_CLIENT_WAIT_SECONDS = 86400


def client_wait_seconds(env=None) -> int:
    """The wait to arm on the proxy socket: the export, or the default.

    Never raises — this runs in front of every proxied skill call, and a
    malformed value must cost the override, not the call. Whole positive
    integer seconds only, clamped to `MAX_CLIENT_WAIT_SECONDS`; anything else
    falls back.
    """
    if env is None:
        env = os.environ
    raw = env.get("ISTOTA_SKILL_CLIENT_WAIT")
    if raw is None:
        return SKILL_CLIENT_WAIT_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return SKILL_CLIENT_WAIT_SECONDS
    if value <= 0:
        return SKILL_CLIENT_WAIT_SECONDS
    return min(value, MAX_CLIENT_WAIT_SECONDS)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: istota-skill <skill> [args...]", file=sys.stderr)
        sys.exit(1)

    skill = sys.argv[1]
    args = sys.argv[2:]

    sock_path = os.environ.get("ISTOTA_SKILL_PROXY_SOCK")

    if sock_path:
        _run_via_proxy(sock_path, skill, args)
    else:
        _run_direct(skill, args)


def _run_via_proxy(sock_path: str, skill: str, args: list[str]) -> None:
    """Send request to proxy socket, print result, exit with returncode."""
    request = json.dumps({"skill": skill, "args": args}) + "\n"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(client_wait_seconds())
        sock.connect(sock_path)
        sock.sendall(request.encode("utf-8"))

        # Read response until newline
        chunks = []
        while True:
            chunk = sock.recv(1048576)  # 1 MB chunks
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        sock.close()

        data = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not data:
            print("No response from skill proxy", file=sys.stderr)
            sys.exit(1)

        response = json.loads(data)
        if response.get("stdout"):
            print(response["stdout"], end="")
        # Credential lookups return {"error": ..., "reason": ...} with no stderr
        if response.get("error") and not response.get("stderr"):
            print(response["error"], file=sys.stderr)
        if response.get("stderr"):
            print(response["stderr"], end="", file=sys.stderr)
            # Append a trailing newline if stderr didn't end with one, so the
            # authorized-skills line (when present) doesn't run together.
            if not response["stderr"].endswith("\n"):
                print("", file=sys.stderr)
        sys.exit(response.get("returncode", 1))

    except FileNotFoundError:
        print(f"Skill proxy socket not found: {sock_path}", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Skill proxy not running at: {sock_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid response from skill proxy: {e}", file=sys.stderr)
        sys.exit(1)


def _run_direct(skill: str, args: list[str]) -> None:
    """Run the skill module in this process tree.

    Legitimate for the unsandboxed daemon-side callers — a CRON ``command:``
    row, a heartbeat shell-command, an operator at a terminal. Inside the
    sandbox it is not: the databases every skill CLI opens are masked out of
    the mount table, so a direct run reaches nothing and reports it as a
    missing table or an unopenable file rather than as the misconfiguration it
    is. ``ISTOTA_SANDBOXED`` (set by the executor only when bwrap is really in
    effect) makes that case fail closed and name the actual problem.
    """
    if os.environ.get("ISTOTA_SANDBOXED"):
        print(
            f"Cannot run skill {skill!r}: the skill proxy is unavailable "
            "(ISTOTA_SKILL_PROXY_SOCK is not set) and skills cannot run inside "
            "the sandbox — the databases they read are not mounted there. This "
            "is an operator misconfiguration: [security] skill_proxy_enabled "
            "must be true wherever sandbox_enabled is.",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = [sys.executable, "-m", f"istota.skills.{skill}"] + args
    try:
        result = subprocess.run(cmd)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"Python not found or skill module missing: istota.skills.{skill}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
