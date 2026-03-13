"""Thin client for the skill proxy.

Console script entry point ``istota-skill``. When ``ISTOTA_SKILL_PROXY_SOCK``
is set, connects to the proxy socket and delegates execution. Otherwise falls
back to running the skill module directly via subprocess.

Usage::

    istota-skill email send --to bob@example.com --subject "Hi" --body "Hello"
    istota-skill calendar list --date today
"""

import json
import os
import socket
import subprocess
import sys


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
        sock.settimeout(600)  # 10 min max wait
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
        if response.get("stderr"):
            print(response["stderr"], end="", file=sys.stderr)
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
    """Fall back to running the skill module directly."""
    cmd = [sys.executable, "-m", f"istota.skills.{skill}"] + args
    try:
        result = subprocess.run(cmd)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"Python not found or skill module missing: istota.skills.{skill}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
