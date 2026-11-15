"""The filter `istota-stack logs` puts every line through before it is printed (#968).

The Mac app hands what the verb prints to whoever a problem is being reported
to, so a line leaves the VM only after two passes: every value the stack holds
in its secret files is replaced wherever it appears, then every web address
goes through `lib/url_redact.redact_web_addresses` (userinfo, query, fragment).

**It fails closed.** The secret files are read before the first line is, and a
file that cannot be read, or is anything but a regular file, refuses the run
with `EXIT_UNREADABLE` and nothing on stdout: a value it could not read is a
value it could not redact. A missing directory or extra file holds nothing.

It runs on the VM's host as root, with the host's python3 and the checkout's
`src/` put on the path by hand (no istota is installed there), so it imports the
standard library and `lib/url_redact` and nothing else;
`tests/test_log_redact.py` holds that.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys

from istota.lib.url_redact import REDACTED, redact_web_addresses

#: The code `istota-secrets` refuses an unreadable secret file with.
EXIT_UNREADABLE = 78
#: A line of a multi-line value, or a string inside a JSON one, is redacted on
#: its own only from this length: an extra-headers file's `{` or `"1"` would
#: otherwise take every brace and digit out of the logs. The whole value is
#: always redacted, however short.
MIN_PIECE_LEN = 8
MAX_SECRET_BYTES = 1 << 20


class Unreadable(Exception):
    """A secret file that could not be read. The message names the path only."""


def _read(path: str, dir_fd: int | None = None) -> str | None:
    """The text of a regular file, None when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Unreadable(f"{path} ({type(exc).__name__})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unreadable(f"{path} (not a regular file)")
        data = os.read(fd, MAX_SECRET_BYTES + 1)
    except OSError as exc:
        raise Unreadable(f"{path} ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    if len(data) > MAX_SECRET_BYTES:
        raise Unreadable(f"{path} (larger than {MAX_SECRET_BYTES} bytes)")
    return data.decode("utf-8", "surrogateescape")


def _json_strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        node = list(node.values())
    if isinstance(node, list):
        return [leaf for item in node for leaf in _json_strings(item)]
    return []


def _pieces(text: str) -> set[str]:
    """What of one file's text is redacted: the value as `istota-secrets`
    exports it (trailing newlines dropped), and its longer lines and JSON strings."""
    value = text.rstrip("\r\n")
    if not value.strip():
        return set()
    pieces = {value, value.strip()}
    parts = [line.strip() for line in value.splitlines()]
    try:
        parts += [leaf.strip() for leaf in _json_strings(json.loads(value))]
    except ValueError:
        pass
    pieces.update(part for part in parts if len(part) >= MIN_PIECE_LEN)
    return pieces


#: A `KEY=VALUE` line in an env file holds a credential when its name ends so:
#: the signaling secrets in `.env`, `RCLONE_RC_PASS` in `rclone-rc.env`.
#: `ISTOTA_SECRETS_DIR` names a path and does not count.
_SECRET_NAME = re.compile(r"[A-Z0-9_]*(?:SECRET|PASS|PASSWORD|TOKEN|KEY)")


def _env_pieces(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if not sep or not _SECRET_NAME.fullmatch(name.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found.update(_pieces(value))
    return found


def secret_values(directory: str, files: list[str], env_files: list[str] = ()) -> list[str]:
    """Every value to redact: each file in `directory`, each of `files`, and the
    credential lines of each of `env_files`.

    Raises `Unreadable` for a file that cannot be read or is not a regular
    file, a link included, and for a `directory` that exists and cannot be listed.
    """
    found: set[str] = set()
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except FileNotFoundError:
        dir_fd = None
    except OSError as exc:
        raise Unreadable(f"{directory} ({type(exc).__name__})") from None
    if dir_fd is not None:
        try:
            for name in sorted(os.listdir(dir_fd)):
                text = _read(name, dir_fd=dir_fd)
                if text is None:
                    # Gone between the listing and the open: a put-files rename.
                    continue
                found.update(_pieces(text))
        except Unreadable as exc:
            raise Unreadable(f"{directory}/{exc}") from None
        except OSError as exc:
            raise Unreadable(f"{directory} ({type(exc).__name__})") from None
        finally:
            os.close(dir_fd)
    for path in files:
        text = _read(path)
        if text is not None:
            found.update(_pieces(text))
    for path in env_files:
        text = _read(path)
        if text is not None:
            found.update(_env_pieces(text))
    return sorted(found)


def values_pattern(values: list[str]) -> re.Pattern | None:
    """One alternation over `values`, longest first, so a value that holds a
    shorter one is replaced whole."""
    if not values:
        return None
    ordered = sorted(set(values), key=lambda v: (-len(v), v))
    return re.compile("|".join(re.escape(v) for v in ordered))


#: What a value is replaced with until the web addresses are done: a bracket in
#: a netloc stops it parsing, so `https://bot:[redacted]@host` would lose its host.
_HOLD = ""


def redact_line(line: str, pattern: re.Pattern | None) -> str:
    """Values first, so one inside an address goes whole, then the addresses."""
    if pattern is not None:
        line = pattern.sub(_HOLD, line)
    return redact_web_addresses(line).replace(_HOLD, REDACTED)


def _arguments(argv: list[str]) -> tuple[str, list[str], list[str]] | None:
    """`secrets_dir [--file PATH]... [--env PATH]...`, or None."""
    if not argv:
        return None
    files: list[str] = []
    env_files: list[str] = []
    rest = list(argv[1:])
    while rest:
        flag = rest.pop(0)
        if flag not in ("--file", "--env") or not rest:
            return None
        (files if flag == "--file" else env_files).append(rest.pop(0))
    return argv[0], files, env_files


def main(argv: list[str]) -> int:
    """`main([secrets_dir, "--file", path, "--env", path, ...])`: stdin to
    stdout, every line redacted."""
    arguments = _arguments(argv)
    if arguments is None:
        print("[istota-stack] usage: log filter SECRETS_DIR [--file PATH]... [--env PATH]...", file=sys.stderr)
        return 64
    try:
        pattern = values_pattern(secret_values(*arguments))
    except Unreadable as exc:
        print(
            f"[istota-stack] REFUSE: cannot read the secret file {exc}, so a line holding "
            "its value could not be redacted; nothing was printed.",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE
    out = sys.stdout.buffer
    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", "surrogateescape")
            end = "\n" if line.endswith("\n") else ""
            out.write((redact_line(line.rstrip("\n"), pattern) + end).encode("utf-8", "surrogateescape"))
        out.flush()
    except BrokenPipeError:
        # The reader went away (`| head`); nothing is left to protect.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0
