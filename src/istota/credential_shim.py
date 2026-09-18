#!/usr/bin/env python3
"""The program a task runs to reach a credential without holding it.

Copied verbatim into ``{user_temp_dir}/.istota/istota-credential`` by
``task_env.build_task_runtime`` and put on the model's PATH — the pattern
``skills/developer`` already uses for ``devbox_exec_client.py``, and for the
same reason: the istota package is not importable from inside the sandbox, so
the program has to travel as a file rather than as an entry point. **stdlib
only, no relative imports, and nothing here may grow one**; the copy is the
thing that runs.

It replaces the five-line socket client ``skills/developer.setup_env`` used to
generate as a string literal. Two socket clients for one protocol is the
duplication ``AGENTS.md`` opens with, so the developer skill's git credential
helper now shells out to the ``env`` verb here, which is that generated
program's behaviour byte for byte.

Four verbs::

    istota-credential list                       # shared credential names
    istota-credential run VAR=name [...] -- cmd  # exec cmd with those set
    istota-credential run --stdin name -- cmd    # value on the child's stdin
    istota-credential get <name>                 # the value on stdout
    istota-credential env <VAR>                  # a manifest-declared var

``run`` is the verb this exists for. It resolves each name over the proxy
socket and ``execvpe``s the given argv with those variables added to its own
environment, so the ordinary path — a script, a ``curl``, a CLI that wants a
token — puts the credential in front of the program that needs it and nowhere
else: not in the model's context, not in the transcript, not in the argv of
anything. ``get`` is kept because removing it would be theatre
(``run X=n -- sh -c 'echo "$X"'`` is the same thing in one more step) and it is
what a skill CLI or a person on a host shell wants; it is demoted instead —
absent from the prompt, and sent with ``mode: read`` so the proxy logs it at
WARNING while an injection logs at INFO.

**This program is not a boundary and must not be read as one.** It sits in a
directory bound read-write into the sandbox, so the model can overwrite it, and
the socket answers a hand-rolled five-line client just as readily. Every rule
that matters — which names exist, how many fetches an attempt may make — is
enforced in ``SkillProxy``. What this buys is that the ordinary path leaves no
copy of the value anywhere.

Exit codes: ``1`` for a refusal, an absent name or a usage error; ``2`` when
``ISTOTA_SKILL_PROXY_SOCK`` is unset, which is what a deployment with the skill
proxy off looks like from in here. ``run`` otherwise exits with the child's own
status, and reports a failed exec as 127 (not found) or 126 (found and not
executable) the way a shell does. A child killed by a signal needs no handling
at all: ``execvpe`` replaced this process, so the caller's shell sees the
signal itself rather than a number this program invented.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys

#: Where ``task_env`` writes this file, relative to ``user_temp_dir``. Here
#: rather than in ``task_env`` because two other callers need the same spelling
#: and neither may import that module: ``skills/developer.setup_env`` runs
#: *before* the shim is written and builds the path from the same rule, and the
#: copied-out program itself carries these names harmlessly.
SHIM_DIR_NAME = ".istota"
SHIM_PROGRAM_NAME = "istota-credential"

#: Owner-only, and the directory with it. The file is reachable and writable by
#: the model either way (``user_temp_dir`` is bound read-write), so this is
#: about other local users rather than about the task.
SHIM_MODE = 0o700

#: The socket-level wait. Every request this program makes is answered from a
#: dict in the daemon's memory — no subprocess, no file, no network — so the
#: long ``ISTOTA_SKILL_CLIENT_WAIT`` budget ``skill_client`` arms for a skill
#: *command* is the wrong number here. The generated client this replaces armed
#: none at all and would hang for ever against a wedged proxy.
SOCKET_TIMEOUT_SECONDS = 30

#: Cap on a value delivered through ``--stdin``. The bytes are written into a
#: pipe *before* the exec, so a payload past the kernel's pipe buffer would
#: block for ever with nothing on the other end to drain it. 8192 matches
#: ``secrets_vault.VAULT_MAX_VALUE_BYTES``, which is what bounds every value in
#: the namespace, and sits under the smallest pipe buffer either supported
#: platform ships (16 KiB on macOS, 64 KiB on Linux). Restated rather than
#: imported, because this file runs with no istota package on its path.
STDIN_MAX_BYTES = 8192

#: A POSIX-portable environment variable name.
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Variables ``run`` refuses to set, whatever the caller asked for. The first
#: three are the model's own route back into this process's behaviour —
#: rewriting ``PATH`` or either loader variable changes which program the exec
#: resolves and which code it loads, and rewriting the socket path points the
#: *next* fetch at something the model wrote. ``IFS`` is here because the
#: overwhelmingly common child is ``sh -c``, where it re-splits every unquoted
#: expansion in the script. None of this is a boundary — the model can set any
#: of them in its own shell before calling this — it stops a credential
#: injection from being the thing that does it.
RESERVED_VARS = frozenset({
    "PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "ISTOTA_SKILL_PROXY_SOCK",
    "IFS",
})

EXIT_REFUSED = 1
EXIT_NO_SOCKET = 2
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

USAGE = (
    "Usage:\n"
    "  istota-credential list\n"
    "  istota-credential run VAR=NAME [VAR2=NAME2 ...] [--stdin NAME] -- CMD [ARGS...]\n"
    "  istota-credential get NAME\n"
    "  istota-credential env VAR\n"
)


def shim_path(user_temp_dir):
    """Where this program lives for one task. One spelling, three callers."""
    import pathlib

    return pathlib.Path(user_temp_dir) / SHIM_DIR_NAME / SHIM_PROGRAM_NAME


class ProxyError(Exception):
    """A refusal from the proxy, or a socket that would not answer.

    Carries the message to print and nothing else — the caller decides the exit
    code, because a refused name and an unreachable socket are different
    answers to the operator.
    """


def _request(payload: dict) -> dict:
    """One JSON line to the proxy, one JSON line back.

    Raises ``ProxyError`` for anything that is not a well-formed reply. Nothing
    here retries: every request is answered from memory, so a failure is the
    proxy being gone rather than busy.
    """
    sock_path = os.environ.get("ISTOTA_SKILL_PROXY_SOCK", "")
    if not sock_path:
        raise ProxyError("ISTOTA_SKILL_PROXY_SOCK is not set")

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(SOCKET_TIMEOUT_SECONDS)
    try:
        conn.connect(sock_path)
        conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    except OSError as exc:
        raise ProxyError(f"could not reach the credential proxy: {exc}") from exc
    finally:
        try:
            conn.close()
        except OSError:
            pass

    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        raise ProxyError("the credential proxy closed without answering")
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise ProxyError(f"the credential proxy answered unparseably: {exc}") from exc
    if not isinstance(reply, dict):
        raise ProxyError("the credential proxy answered unparseably")
    if "error" in reply:
        raise ProxyError(str(reply["error"]))
    return reply


def _fetch(name: str, mode: str) -> str:
    """One shared credential, by name, under a declared mode.

    ``mode`` is a claim rather than a fact — the proxy sees a socket, not a
    process — and it is sent so the daemon's log can say which of the three
    paths asked. It is not a control and nothing here pretends otherwise.
    """
    reply = _request({"type": "vault_credential", "name": name, "mode": mode})
    value = reply.get("value")
    if not isinstance(value, str):
        raise ProxyError(f"no value for {name!r}")
    return value


def _cmd_list() -> int:
    reply = _request({"type": "vault_list"})
    names = reply.get("names") or []
    for name in names:
        print(name)
    return 0


def _cmd_get(args: list[str]) -> int:
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    # `end=""`: a caller substituting this into a header or a git credential
    # line wants the bytes verbatim, and a trailing newline is not in them.
    print(_fetch(args[0], "read"), end="")
    return 0


def _cmd_env(args: list[str]) -> int:
    """The manifest-declared variable fetch, byte for byte as it was.

    A different namespace from the three verbs above — ``derive_lookup_allowlist``
    over skill manifests, not the user's vault — reached through the proxy's
    pre-existing ``credential`` branch, and deliberately not folded into it.
    """
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    reply = _request({"type": "credential", "name": args[0]})
    print(reply.get("value", ""), end="")
    return 0


def _parse_run(args: list[str]):
    """``(assignments, stdin_name, argv)`` for a ``run``, or a usage message.

    Hand-parsed rather than argparse: everything past ``--`` is the child's own
    command line, flags included, and argparse would claim them.
    """
    try:
        split = args.index("--")
    except ValueError:
        return None, "run needs a `--` before the command to execute"
    spec, argv = args[:split], args[split + 1:]
    if not argv:
        return None, "run needs a command after the `--`"

    assignments: list[tuple[str, str]] = []
    stdin_name: str | None = None
    index = 0
    while index < len(spec):
        token = spec[index]
        if token == "--stdin":
            if index + 1 >= len(spec):
                return None, "--stdin needs a credential name"
            if stdin_name is not None:
                return None, "--stdin may be given once"
            stdin_name = spec[index + 1]
            index += 2
            continue
        var, sep, name = token.partition("=")
        if not sep:
            return None, f"expected VAR=NAME or --stdin NAME, got {token!r}"
        if not _VAR_NAME_RE.match(var):
            return None, f"{var!r} is not a usable environment variable name"
        if var in RESERVED_VARS:
            return None, f"{var!r} may not be set by run"
        if not name:
            return None, f"{var}= needs a credential name"
        assignments.append((var, name))
        index += 1

    if not assignments and stdin_name is None:
        return None, "run needs at least one VAR=NAME or --stdin NAME"
    return (assignments, stdin_name, argv), None


def _cmd_run(args: list[str]) -> int:
    parsed, problem = _parse_run(args)
    if parsed is None:
        print(f"istota-credential: {problem}\n\n{USAGE}", file=sys.stderr)
        return EXIT_REFUSED
    assignments, stdin_name, argv = parsed

    # Every name resolved before anything is executed. A command that ran with
    # one variable missing is the failure this verb exists to avoid: a `curl`
    # with an empty bearer token gets a 401 the model then debugs in the wrong
    # place.
    env = dict(os.environ)
    for var, name in assignments:
        env[var] = _fetch(name, "inject")
    payload = b""
    if stdin_name is not None:
        payload = _fetch(stdin_name, "inject").encode("utf-8")
        if len(payload) > STDIN_MAX_BYTES:
            print(
                f"istota-credential: the value is larger than the "
                f"{STDIN_MAX_BYTES} bytes --stdin can deliver",
                file=sys.stderr,
            )
            return EXIT_REFUSED

    if stdin_name is not None:
        # Written into the pipe and the write end closed *before* the exec, so
        # the child reads the value and then EOF. A pipe rather than a temp
        # file: an unlinked file still puts the plaintext on a filesystem, and
        # this way the bytes never leave the two processes' memory.
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, payload)
        finally:
            os.close(write_fd)
        os.dup2(read_fd, 0)
        os.close(read_fd)

    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        print(f"istota-credential: {argv[0]}: not found", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as exc:
        print(f"istota-credential: {argv[0]}: {exc}", file=sys.stderr)
        return EXIT_NOT_EXECUTABLE
    return EXIT_NOT_EXECUTABLE  # pragma: no cover - execvpe does not return


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED

    verb, rest = args[0], args[1:]
    handlers = {
        "list": lambda: _cmd_list(),
        "run": lambda: _cmd_run(rest),
        "get": lambda: _cmd_get(rest),
        "env": lambda: _cmd_env(rest),
    }
    handler = handlers.get(verb)
    if handler is None:
        print(f"istota-credential: unknown verb {verb!r}\n\n{USAGE}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        return handler()
    except ProxyError as exc:
        print(f"istota-credential: {exc}", file=sys.stderr)
        # An unset socket is the deployment shape rather than a refusal, and it
        # is the one thing the caller can act on differently.
        if not os.environ.get("ISTOTA_SKILL_PROXY_SOCK", ""):
            return EXIT_NO_SOCKET
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
