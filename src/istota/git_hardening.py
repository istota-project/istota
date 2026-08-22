"""`-c` overrides that stop a repository's own config running a program.

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

stdlib-only leaf. No imports at all, in fact: it is data.
"""

from __future__ import annotations

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
