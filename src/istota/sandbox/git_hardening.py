"""Running `git` against a repository whose config the model writes.

The `-c` overrides, the environment policy and the subprocess wrapper that
applies both. `GIT_HARDENING` is the list; `GIT_SUBPROCESS_ENV` and
`GIT_SUBPROCESS_ENV_UNSET` are the two halves of the environment policy and
:func:`git_env` is what applies them together; :func:`run_git` is what
daemon-side code runs `git` through when the repository it runs against is one
the model can write — `worktree_reaper` and `repos_relocate` under
`developer.repos_dir`.

Two callers here deliberately do not go through `run_git` and each says why at
its own definition: `git_remote_scrub._git_config`, which names a file rather
than a repository and must keep raising, and which takes `git_env`; and
`code_review.engine._git_env`, which builds an environment from nothing rather
than overlaying `os.environ` and so takes `GIT_SUBPROCESS_ENV` alone — it has
nothing inherited to remove, which is what made it the one caller immune to the
redirection `GIT_SUBPROCESS_ENV_UNSET` now closes for the others.

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

Every entry but the first is a config key that either runs a command or
reshapes output a caller parses; `--no-replace-objects` leads the list and says
at its own line why it is there. `core.fsmonitor`, `diff.external` and the
`gpg.*` programs are the run-a-command ones — `gpg.program` is reached from a
plain `git log` whenever `log.showSignature` is on, which is itself just a
repo-local boolean, and that pair was a working escape past the first three.
`color.ui` is not an execution route but is just as load-bearing for a parser:
with colour forced on, output arrives wrapped in ANSI escapes that a matcher
misses, and the caller is handed what looks like an empty result with nothing
reporting a loss.

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
    # Not a `-c` override, and the only entry here that is not a config key.
    # A `refs/replace/*` ref is repository *content*, so a model with write
    # access to a checkout can forge one, and it rewrites what `merge-base
    # --is-ancestor` and `rev-list --parents` answer — which is the whole of
    # `worktree_reaper`'s proof that a branch is merged and its worktree may be
    # removed. Measured on git 2.55: a replace ref pointing the main commit at
    # a forged parent turns "not merged" into "merged", and this flag turns it
    # back (ISSUE-457, found reviewing the environment half of the same hole).
    "--no-replace-objects",
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
#: Half the policy: what is *set*. The other half is `GIT_SUBPROCESS_ENV_UNSET`
#: below, and `git_env` is what applies both — reach for that rather than for
#: either constant, or a call site ends up with half the policy.
#:
#: A mapping proxy rather than a dict, for the same reason `GIT_HARDENING` is a
#: tuple: a caller that mutates a shared safety constant in place removes a
#: variable from every other caller with nothing reporting the loss.
GIT_SUBPROCESS_ENV: Mapping[str, str] = MappingProxyType(
    {
        "GIT_CONFIG_NOSYSTEM": "1",
        # `os.devnull`, not the literal, because `code_review.engine` wrote it
        # that way and folding it in here should not narrow the expression.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
)

#: Removed from a daemon-side `git` environment, whatever the daemon inherited.
#:
#: Setting is not enough, because these are read at a scope that outranks what
#: a caller passes on the command line. Measured on git 2.55: with `GIT_DIR`
#: set, `git -C <repo> rev-parse --absolute-git-dir` answers with the *other*
#: repository, and `GIT_WORK_TREE` sends `status` at another tree. The caller
#: that makes this matter is `worktree_reaper`, which runs `worktree remove`,
#: so a redirection there deletes from a repository nobody named (ISSUE-457).
#:
#: `GIT_COMMON_DIR` is the one to keep in mind, because the obvious sanity
#: check does not catch it: with it set, `rev-parse --absolute-git-dir` still
#: answers with *this* repository while the common dir — where `worktree list`
#: and `worktree remove` read and write `worktrees/<name>/` — points at the
#: other one.
#:
#: `GIT_GRAFT_FILE` is not a redirection and is the most direct of the lot. It
#: rewrites parentage, so it forges the answer to `merge-base --is-ancestor`
#: and `rev-list --parents` — which is exactly `worktree_reaper`'s proof that a
#: branch is merged and its worktree may be deleted. The `--no-replace-objects`
#: in `GIT_HARDENING` closes the same forgery from the repository side.
#:
#: `GIT_EXTERNAL_DIFF` is neither, and is here because it **beats** the
#: `-c diff.external=` in `GIT_HARDENING`, which that list names as one of its
#: three run-a-command defences; removing the variable is what makes the claim
#: true. No caller runs a diff today, so it is hardening ahead of one.
#:
#: What is deliberately *not* here, each measured rather than assumed:
#:
#: - `GIT_CONFIG_COUNT` with its numbered `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`
#:   pair, and `GIT_CONFIG_PARAMETERS`, all inject config — but a `-c` beats
#:   every one of them, so each key `GIT_HARDENING` names is already safe. A key
#:   it does not name is reachable through them, and equally through the
#:   repository's own config, which is the exposure this module is built around
#:   rather than a new one. Dropping the first would also change what the
#:   reaper's `fetch` can authenticate with, since the developer skill registers
#:   its credential helper that way.
#: - `GIT_CEILING_DIRECTORIES` can only *narrow* discovery: it turns an upward
#:   walk into a refusal, never into a different repository. Measured, it does
#:   not bite any caller here at all, since every one passes a path whose `.git`
#:   is found without walking up. Removing it would delete the one containment
#:   lever an operator can still set from the environment — the same one
#:   `code_review.engine._git_env` sets deliberately — and buy nothing.
#: - `GIT_TRACE` and the `GIT_TRACE2*` family write to stderr only, so they
#:   reach no caller that parses stdout. They would land in `repos_relocate`'s
#:   `merge_stderr=True` text, which goes to an operator rather than a parser,
#:   and they are that operator's way of debugging a failing sweep.
#: - `GIT_SSH_COMMAND` and `GIT_ASKPASS` name a program, but only on the network
#:   path this module's one fetching caller expects to fail anyway, and they are
#:   a deployment's legitimate way to give that fetch an identity.
GIT_SUBPROCESS_ENV_UNSET: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_GRAFT_FILE",
    "GIT_EXTERNAL_DIFF",
)


def git_env() -> dict[str, str]:
    """The environment for a daemon-side `git`: `os.environ`, both halves applied.

    A fresh dict every call, never `os.environ` itself, so a caller adding a
    variable of its own cannot edit the daemon's environment.

    Removals happen before the overlay rather than after, which matters only if
    a name ever appears in both — the overlay would then win, which is the
    reading a caller expects of something called an overlay.
    """
    env = dict(os.environ)
    for name in GIT_SUBPROCESS_ENV_UNSET:
        env.pop(name, None)
    env.update(GIT_SUBPROCESS_ENV)
    return env


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
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc) if on_error is None else on_error
    out = proc.stdout + proc.stderr if merge_stderr else proc.stdout
    return proc.returncode, out.decode("utf-8", "surrogateescape")
