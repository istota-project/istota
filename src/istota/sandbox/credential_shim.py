#!/usr/bin/env python3
"""The program a task runs to reach a credential without holding it.

Copied verbatim into ``{user_temp_dir}/.istota/istota-credential`` by
``task_env.build_task_runtime`` and put on the model's PATH. **stdlib only, no
relative imports, and nothing here may grow one**; the copy is the thing that
runs, in a process with no istota package on its path.

**A per-task file rather than a console script**, which is what it is worth
being exact about, since ``istota-skill`` speaks this same socket from inside
the sandbox as an entry point and so proves the package *is* reachable there.
Three reasons the entry point is wrong here and none of them is reachability.
The program has to sit in a directory that goes on the *model's* PATH and on
nothing else (``task_env`` states that rule at its application site), and a
venv entry point is on every PATH including the host-side skill CLIs'. It is
placed and removed with the task rather than with the installed package, so a
deployment that has not reinstalled still runs the current one. And it is the
program ``skills/developer`` used to generate as a string literal, promoted —
the shape it replaces, not a new one.

It replaces the socket client ``skills/developer.setup_env`` used to generate.
With the broker enabled, developer credential helpers use placeholders. The
legacy ``env`` verb still uses the same public socket and reveal policy.

Nine verbs::

    istota-credential list                       # shared credential names
    istota-credential placeholder <name>         # inert auth-header text
    istota-credential run VAR=name [...] -- cmd  # exec cmd with those set
    istota-credential run --stdin name -- cmd    # value on the child's stdin
    istota-credential get <name>                 # the value on stdout
    istota-credential env <VAR>                  # a manifest-declared var
    istota-credential new <slug> [options]        # generate and store a credential
    istota-credential otp-set <name>              # enrollment seed on stdin
    istota-credential recovery-set <name>         # recovery codes on stdin

``placeholder`` is the broker path: the value is added outside the sandbox.
``get`` and both forms of ``run`` ask for a value and, under reveal enforcement,
work only for vault entries tagged ``istota:reveal``. ``env`` reads manifest
variables, which have no reveal marker and are all refused under enforcement.
Before enforcement, the proxy audits the public reads it would refuse.

The shim is a convenience, not the gate. A hand-written client can speak the
same protocol; ``SkillProxy`` enforces reveal permission and the fetch cap.
Host-side skill CLIs use a private inherited fd, selected explicitly by
``_credref``, to resolve credentials without giving values to the model.

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

#: Fetches answer from the proxy's memory. A create may also write the KeePass
#: copy, which unlocks and saves a KDBX file, so it gets a longer wait below.
SOCKET_TIMEOUT_SECONDS = 30
CREATE_TIMEOUT_SECONDS = 120

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

#: Variables ``run`` refuses to set, whatever the caller asked for.
#:
#: Three groups. ``PATH`` and the socket path decide which program the exec
#: resolves and where the *next* fetch goes. The loader variables decide which
#: code the child loads before its own first line — ``BASH_ENV`` and ``ENV``
#: are here because the overwhelmingly common child is ``sh -c``, and bash
#: sources ``$BASH_ENV`` for a non-interactive shell, which would run the
#: credential value as a script (``executor.build_stripped_env`` strips that
#: same name for the same reason). ``IFS`` re-splits every unquoted expansion
#: in such a script.
#:
#: **Not a boundary, and the list is not a completeness claim.** The model can
#: set any of these in its own shell before calling this, and there are more
#: interpreters than this list names. What it stops is a *credential
#: injection* being the thing that does it — a refusal a reader can check,
#: rather than a guarantee nobody can keep.
RESERVED_VARS = frozenset({
    "PATH",
    "ISTOTA_SKILL_PROXY_SOCK",
    # Loaders and interpreter startup.
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "IFS",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
})

EXIT_REFUSED = 1
EXIT_NO_SOCKET = 2
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

USAGE = (
    "Usage:\n"
    "  istota-credential list\n"
    "  istota-credential placeholder NAME\n"
    "  istota-credential run VAR=NAME [VAR2=NAME2 ...] [--stdin NAME] -- CMD [ARGS...]\n"
    "  istota-credential get NAME\n"
    "  istota-credential env VAR\n"
    "  istota-credential otp-set NAME  # read enrollment secret from stdin\n"
    "  istota-credential recovery-set NAME  # read recovery codes from stdin\n"
    "  istota-credential new SLUG [--username USER] [--url URL] [--length N] [--no-symbols]\n"
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


def _request(
    payload: dict, *, timeout: int = SOCKET_TIMEOUT_SECONDS,
    credential_fd: str | None = None,
) -> dict:
    """One JSON line to the proxy, one JSON line back.

    Raises ``ProxyError`` for anything that is not a well-formed reply. The
    client does not retry a create, since the file may have been replaced before
    a response was lost.
    """
    sock_path = os.environ.get("ISTOTA_SKILL_PROXY_SOCK", "")
    if credential_fd is None and not sock_path:
        raise ProxyError("ISTOTA_SKILL_PROXY_SOCK is not set")

    # Duplicate the invocation's endpoint: closing this request must leave it
    # usable for the next stamped argument. Never fall back after an fd error.
    try:
        if credential_fd is None:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            conn = socket.fromfd(int(credential_fd), socket.AF_UNIX, socket.SOCK_STREAM)
    except (OSError, ValueError, OverflowError) as exc:
        raise ProxyError("the private credential channel is unavailable") from exc
    try:
        conn.settimeout(timeout)
        if credential_fd is None:
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


def fetch_credential(
    name: str, mode: str, *, binding: bool = False, credential_fd: str | None = None,
) -> str | tuple[str, list[str]]:
    """One shared credential, by name, under a declared mode.

    ``mode`` is a claim rather than a fact — the proxy sees a socket, not a
    process — and it is sent so the daemon's log can say which of the three
    paths asked. It is not a control and nothing here pretends otherwise.

    **Public because it has a second caller inside the package**:
    ``skills/_credref`` resolves a stamped argument through this same request,
    with ``mode="skill"`` and an explicit private ``credential_fd`` when
    spawned by the proxy. The model-facing shim never selects that fd from
    its environment. That is a *host-side* caller rather than a copy of
    this program — the shim runs in the sandbox with no istota package on its
    path, a skill CLI runs outside it with the package — and the two speaking
    one client is the point. Raises ``ProxyError``, which is the whole error
    surface either caller has to handle.
    """
    request = {"type": "vault_credential", "name": name, "mode": mode}
    if binding:
        request["binding"] = True
    reply = _request(request, credential_fd=credential_fd)
    value = reply.get("value")
    if not isinstance(value, str):
        raise ProxyError(f"no value for {name!r}")
    if binding:
        hosts = reply.get("bound_hosts", [])
        if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
            raise ProxyError("the credential proxy answered unparseably")
        return value, hosts
    return value


def fetch_otp(
    name: str, mode: str, *, credential_fd: str | None,
) -> tuple[str, int, tuple[str, ...]]:
    """A current code and its expiry, available only on the private channel."""
    if credential_fd is None:
        raise ProxyError("the private credential channel is unavailable")
    reply = _request({"type": "vault_otp", "name": name, "mode": mode},
                     credential_fd=credential_fd)
    code = reply.get("code")
    expires_at = reply.get("expires_at")
    hosts = reply.get("bound_hosts")
    if (not isinstance(code, str) or not code
            or type(expires_at) is not int
            or not isinstance(hosts, list) or not hosts
            or not all(isinstance(host, str) for host in hosts)):
        raise ProxyError("the credential proxy answered unparseably")
    return code, expires_at, tuple(hosts)


def fetch_entry(
    name: str, mode: str, *, credential_fd: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Every field of one vault entry, for one fetch: ``(fields, bound_hosts)``.

    ``fields`` maps ``password``, ``username``, ``url`` and each custom field
    to its value, holding only the fields the entry has. Same transport and
    error surface as ``fetch_credential``.
    """
    reply = _request({"type": "vault_entry", "name": name, "mode": mode},
                     credential_fd=credential_fd)
    fields = reply.get("fields")
    hosts = reply.get("bound_hosts", [])
    if (not isinstance(fields, dict) or not fields
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items())
            or not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts)):
        raise ProxyError("the credential proxy answered unparseably")
    return fields, hosts


def fetch_card(purchase_id: int, *, credential_fd: str | None) -> tuple[dict[str, str], list[str]]:
    """Claim one purchase fill over the host-side invocation's private channel."""
    if credential_fd is None:
        raise ProxyError("wallet_channel_unavailable")
    try:
        reply = _request({"type": "wallet_card", "purchase_id": purchase_id}, credential_fd=credential_fd)
    except ProxyError:
        raise ProxyError("wallet_channel_unavailable") from None
    if reply.get("ok") is False:
        reason = reply.get("reason")
        allowed = {"purchase_not_found", "purchase_not_authorized", "purchase_expired",
                   "purchase_fill_limit", "wallet_unavailable", "wallet_error", "wallet_channel_unavailable"}
        raise ProxyError(reason if isinstance(reason, str) and reason in allowed else "wallet_error")
    fields, hosts = reply.get("fields"), reply.get("bound_hosts")
    if (not isinstance(fields, dict) or set(fields) != {"number", "cvc", "exp_month", "exp_year", "name"}
            or not all(isinstance(value, str) for value in fields.values())
            or not isinstance(hosts, list) or not hosts or not all(isinstance(host, str) for host in hosts)):
        raise ProxyError("wallet_error")
    return fields, hosts


def list_entries() -> list[str]:
    """The vault entry names this task can read, with no values. Not charged.

    Raises ``ProxyError`` when the proxy cannot say, rather than answering an
    empty list that would read as "no entries".
    """
    reply = _request({"type": "vault_list"})
    entries = reply.get("entries")
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise ProxyError("the credential proxy did not list vault entries")
    return entries


def _cmd_list() -> int:
    reply = _request({"type": "vault_list"})
    names = reply.get("names")
    # Type-checked like every other field read off this socket. The proxy is
    # ours, but a `str` here would print one character per line and a `dict`
    # would print its keys, and neither failure says anything about what went
    # wrong.
    if not isinstance(names, list):
        raise ProxyError("the credential proxy answered unparseably")
    credentials = reply.get("credentials")
    if isinstance(credentials, list):
        print("NAME\tBOUND HOSTS\tREVEALABLE\tGRANT\tOTP")
        for item in credentials:
            print("\t".join((item["name"], ",".join(item["bound_hosts"]) or "unbound",
                             "yes" if item["revealable"] else "no", item["grant"],
                             "yes" if item.get("kind", "value") == "totp" else "no")))
    else:
        for name in names:
            print(name)
    return 0


def _cmd_placeholder(args: list[str]) -> int:
    """Print inert text; discover binding metadata without fetching a value."""
    if len(args) != 1 or re.fullmatch(r"[A-Za-z0-9_-]+|forge\.(?:gitlab|github)", args[0]) is None:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    name = args[0]
    reply = _request({"type": "vault_list"})
    for item in reply.get("credentials", []):
        if item.get("name") == name:
            if item.get("kind") == "recovery":
                raise ProxyError("credential_is_recovery: only the user can read recovery codes")
            if item.get("kind", "value") != "value":
                raise ProxyError("credential_is_otp_seed: use browse --fill-otp")
            hosts = item.get("bound_hosts", [])
            print("Bound hosts: " + (", ".join(hosts) or "unbound"), file=sys.stderr)
            print("{{cred:" + name + "}}", end="")
            return 0
    raise ProxyError("credential name is unavailable")


def _cmd_get(args: list[str]) -> int:
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    # `end=""`: a caller substituting this into a header or a git credential
    # line wants the bytes verbatim, and a trailing newline is not in them.
    print(fetch_credential(args[0], "read"), end="")
    return 0


def _cmd_new(args: list[str]) -> int:
    if not args or args[0].startswith("--"):
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    payload: dict = {"type": "vault_create", "slug": args[0]}
    index = 1
    while index < len(args):
        flag = args[index]
        if flag == "--no-symbols":
            payload["symbols"] = False
            index += 1
            continue
        if flag not in ("--username", "--url", "--length") or index + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return EXIT_REFUSED
        value = args[index + 1]
        if flag == "--length":
            try:
                value = int(value)
            except ValueError:
                print("istota-credential: length must be an integer", file=sys.stderr)
                return EXIT_REFUSED
        payload[flag[2:]] = value
        index += 2
    reply = _request(payload, timeout=CREATE_TIMEOUT_SECONDS)
    for field in ("name", "username_name", "url_name", "username"):
        if not isinstance(reply.get(field), str):
            raise ProxyError("the credential proxy answered unparseably")
    print(json.dumps({field: reply[field] for field in (
        "name", "username_name", "url_name", "username",
    )}))
    return 0


def _cmd_otp_set(args: list[str]) -> int:
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    otp = sys.stdin.read(65537).strip()
    if not otp or len(otp) > 65536:
        raise ProxyError("invalid_otp: provide an OTP URI or base32 secret on stdin")
    reply = _request({"type": "vault_otp_set", "name": args[0], "otp": otp},
                     timeout=CREATE_TIMEOUT_SECONDS)
    if not isinstance(reply.get("name"), str) or reply.get("otp") is not True:
        raise ProxyError("the credential proxy answered unparseably")
    print(json.dumps({"name": reply["name"], "otp": True}))
    return 0


def store_recovery(name: str, text: str, *, credential_fd: str | None = None) -> tuple[int, bool]:
    """Save a generated credential's recovery codes: ``(line count, replaced)``.

    The codes go one way, to the daemon; the reply carries neither them nor
    anything derived from them beyond the count.
    """
    reply = _request({"type": "vault_recovery_set", "name": name, "text": text},
                     timeout=CREATE_TIMEOUT_SECONDS, credential_fd=credential_fd)
    count, replaced = reply.get("count"), reply.get("replaced")
    if type(count) is not int or type(replaced) is not bool:
        raise ProxyError("the credential proxy answered unparseably")
    return count, replaced


def fetch_recovery_target(name: str, *, credential_fd: str | None) -> tuple[str, ...]:
    """The hosts ``browse --save-recovery`` may read ``name``'s codes on. Private channel only."""
    if credential_fd is None:
        raise ProxyError("the private credential channel is unavailable")
    reply = _request({"type": "vault_recovery_target", "name": name}, credential_fd=credential_fd)
    hosts = reply.get("bound_hosts")
    if (not isinstance(hosts, list) or not hosts
            or not all(isinstance(host, str) for host in hosts)):
        raise ProxyError("the credential proxy answered unparseably")
    return tuple(hosts)


def _cmd_recovery_set(args: list[str]) -> int:
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    text = sys.stdin.read(65537)
    if not text.strip() or len(text) > 65536:
        raise ProxyError("recovery_empty: provide the recovery codes on stdin")
    count, replaced = store_recovery(args[0], text)
    print(json.dumps({"name": args[0], "saved": count, "replaced": replaced}))
    return 0


def _cmd_env(args: list[str]) -> int:
    """A manifest-declared variable fetch, refused under reveal enforcement.

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
        env[var] = fetch_credential(name, "inject")
    payload = b""
    if stdin_name is not None:
        payload = fetch_credential(stdin_name, "inject").encode("utf-8")
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
        # `read_fd` can *be* 0 where this process was started with stdin
        # closed, since `os.pipe` takes the lowest free descriptor. `dup2` is
        # then a no-op and closing the source would hand the child no stdin at
        # all — and the fd still needs `set_inheritable`, because `os.pipe`
        # returns close-on-exec descriptors while `dup2` clears that flag on
        # its target. Without the second line the child's `sys.stdin` is
        # `None` rather than the value, which is the bug this branch exists to
        # avoid wearing a different mask.
        if read_fd == 0:
            os.set_inheritable(read_fd, True)
        else:
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
        "placeholder": lambda: _cmd_placeholder(rest),
        "run": lambda: _cmd_run(rest),
        "get": lambda: _cmd_get(rest),
        "env": lambda: _cmd_env(rest),
        "new": lambda: _cmd_new(rest),
        "otp-set": lambda: _cmd_otp_set(rest),
        "recovery-set": lambda: _cmd_recovery_set(rest),
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
    except (OSError, ValueError) as exc:
        # The pipe, the dup and the encode, which are outside `_request`'s own
        # contract. A traceback here would print the credential's surroundings
        # into the model's tool output and exit 1 anyway; a named line exits 1
        # and says which thing failed.
        print(f"istota-credential: {verb}: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
