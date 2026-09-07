"""Running `git` against a repository whose config the model writes.

The `-c` overrides, the environment overlay and the subprocess wrapper that
applies both. `GIT_HARDENING` is the list; `GIT_SUBPROCESS_ENV` is the
environment overlay; :func:`run_git` applies both and is what daemon-side code
runs `git` through when the repository it runs against is one the model can
write — `worktree_reaper` and `repos_relocate` under `developer.repos_dir`.

Two callers here deliberately do not go through `run_git` and each says why at
its own definition: `git_remote_scrub._git_config`, which names a file rather
than a repository and must keep raising; and `code_review.engine._git_env`,
which builds an environment from nothing rather than overlaying `os.environ`.
Both take `GIT_SUBPROCESS_ENV`, which is the part that was written four times.

## The `-c` overrides

Repo-local `.git/config` cannot be switched off by environment variable.
`GIT_CONFIG_NOSYSTEM` and `GIT_CONFIG_GLOBAL` cover the system and user files
and leave the repository's own untouched — and under `developer.repos_dir` the
repository's own is the one the model can write, because that directory is
bound read-write into the sandbox. So any daemon-side `git` run against a
checkout there executes whatever `core.fsmonitor` names, as the daemon user,
with the daemon's environment (forge tokens included).

These are `-c` overrides rather than environment settings for that reason: a
later `-c` beats the repository's own value.

Every entry is a config key that either runs a command or reshapes output a
caller parses. `core.fsmonitor`, `diff.external` and the `gpg.*` programs are
the run-a-command ones — `gpg.program` is reached from a plain `git log`
whenever `log.showSignature` is on, which is itself just a repo-local boolean,
and that pair was a working escape past the first three. `color.ui` is not an
execution route but is just as load-bearing for a parser: with colour forced
on, output arrives wrapped in ANSI escapes that a matcher misses, and the
caller is handed what looks like an empty result with nothing reporting a loss.

Extracted from `skills/code_review/engine.py`, which paid for the list and
still re-exports it. It lives here because `istota.skills.__init__`
star-imports every skill (~190ms), so anything outside `skills/` that needs the
list — `worktree_reaper`, which runs `git status` inside exactly those
model-writable checkouts — cannot reach for it there. Same reason
`forge_bin.py` exists.

stdlib-only leaf: `os`, `subprocess` and `pathlib`, nothing from the package.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

GIT_HARDENING = (
    "-c",
    "core.fsmonitor=",
    "-c",
    "diff.external=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "log.showSignature=false",
    "-c",
    "gpg.program=/nonexistent",
    "-c",
    "gpg.openpgp.program=/nonexistent",
    "-c",
    "gpg.ssh.program=/nonexistent",
    "-c",
    "gpg.x509.program=/nonexistent",
    "-c",
    "color.ui=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "core.quotePath=false",
)

#: Environment overlay for a daemon-side `git`, applied over `os.environ`.
#:
#: The pair with the `-c` list above, not a substitute for it: these four cover
#: the system and user config, the credential prompt and the optional index
#: lock, and none of them touches the repository's own config, which is the
#: file the model can write.
#:
#: `GIT_CONFIG_NOSYSTEM` and `GIT_CONFIG_GLOBAL=/dev/null` keep a developer's
#: `~/.gitconfig` — aliases, `url.*.insteadOf` rewrites, a credential helper —
#: out of a daemon run, so the daemon answers the same question on a workstation
#: as on the host.
#:
#: `GIT_TERMINAL_PROMPT=0` makes an authenticating fetch fail rather than hang
#: on a prompt no one is at. The scheduler holds no git credential of its own,
#: so a private-repo fetch failing fast is the expected path, not an error case.
#:
#: `GIT_OPTIONAL_LOCKS=0` is load-bearing rather than tidiness: without it
#: `git status` rewrites the worktree's index, and that mtime is what
#: `worktree_reaper` reads as the idle clock — every sweep would reset the clock
#: of every worktree it examined and nothing would ever be reaped after the
#: first pass.
#:
#: A mapping proxy rather than a dict, for the same reason `GIT_HARDENING` is a
#: tuple: a caller that mutates a shared safety constant in place removes a
#: variable from every other caller with nothing reporting the loss.
GIT_SUBPROCESS_ENV: Mapping[str, str] = MappingProxyType(
    {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
)


def run_git(
    cwd: Path,
    *args: str,
    timeout: float = 30.0,
    merge_stderr: bool = False,
    on_error: str | None = "",
) -> tuple[int, str]:
    """``(exit_status, output)`` from ``git`` in ``cwd``. Never raises.

    ``GIT_HARDENING`` first, before ``-C``: the repository this runs against is
    model-writable, and a plain ``git`` there executes ``core.fsmonitor``,
    ``diff.external`` or a ``gpg.*`` program of the writer's choosing, as the
    daemon user, inheriting the daemon's environment. A later ``-c`` beats the
    repository's own value, which is why the overrides lead.

    Bytes are decoded, never rejected. ``text=True`` would raise
    ``UnicodeDecodeError`` — a ``ValueError``, caught by neither ``OSError`` nor
    ``SubprocessError`` — on a repository holding one non-UTF-8 path or commit
    message, and would abort a sweep from inside a helper every caller treats as
    total.

    ``merge_stderr`` appends stderr to stdout, for a caller that reports git's
    own diagnosis to an operator rather than parsing the output.

    ``on_error`` is the text returned beside exit 1 when git could not be run at
    all — a missing binary, a timeout. ``None`` means the exception's own
    message, which is what a caller that surfaces the output wants; the default
    ``""`` is for a caller that parses it and must not be handed prose.
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(cwd), *args],
            capture_output=True,
            timeout=timeout,
            env={**os.environ, **GIT_SUBPROCESS_ENV},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc) if on_error is None else on_error
    out = proc.stdout + proc.stderr if merge_stderr else proc.stdout
    return proc.returncode, out.decode("utf-8", "surrogateescape")
