"""What a task environment carries only because the outer process is `claude`.

`executor.build_clean_env` copies `CLAUDE_CODE_OAUTH_TOKEN` out of the daemon's
environment into **every** task's env, for every brain, because two of the three
brains authenticate with it: `ClaudeCodeBrain`'s outer process is the `claude`
CLI and `TmuxClaudeBrain`'s is the same binary in a pane. `NativeBrain` is not —
it authenticates from its own configured provider key (`NativeBrainConfig`) and
reads nothing here — so on that path the variable has no reader at all, only a
route out: `echo "$CLAUDE_CODE_OAUTH_TOKEN"` in a Bash call comes back as a
`ToolResultMessage` addressed to whatever provider native is pointed at, which
is a provider boundary wherever that is not the credential's own issuer
(ISSUE-390).

This is the environment counterpart of the Claude-only *mount* block. ISSUE-389
split those, putting `~/.claude/.credentials.json` and its neighbours behind
`SandboxProfile.CLAUDE`; mounts and environment are separate mechanisms and the
mount split never addressed the second one.

**A name list rather than a `CLAUDE_*` prefix rule.** A prefix would also
swallow an operator's own `security.passthrough_env_vars` entry, and what an
operator put in that list on purpose is their decision, not this module's. The
drift a name list buys is covered mechanically instead:
`tests/test_security.py::TestBuildCleanEnv` requires every Claude-family or
credential-shaped key `build_clean_env` puts in a task env to be named here.

**What this is not.** It is not "no credential crosses into a native tool". With
`security.skill_proxy_enabled` off — the shipped standalone/local install —
`_split_credential_env` never runs, so a task env still holds `NC_PASS`, the
SMTP and IMAP passwords, the forge tokens and every other configured service
credential, and the argument above applies to each of them word for word. That
is a wider decision than this one (those credentials are in that env so the
skill CLIs can use them on a deployment with no proxy to inject them), and it is
deliberately not made here.

A stdlib-only leaf that imports nothing from the package, so `brain/native.py`
can reach the rule at module scope: `executor` imports `.brain`, so the reverse
import would be a cycle, and the alternative — a function-local import on the
task path — makes every direct brain caller pay `executor`'s whole import graph
inside the agent loop. Same reasoning as `git_hardening.py` and `shell_exec.py`.
"""

from __future__ import annotations

#: Names a task env carries only for the `claude` CLI's benefit.
#:
#: One entry today. A second variable hand-set beside the first in
#: `build_clean_env` belongs here too, unless a native tool subprocess has
#: business reading it.
CLAUDE_RUNTIME_ENV_VARS = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})


def without_claude_runtime_env(
    env: dict[str, str] | None,
) -> dict[str, str] | None:
    """``env`` minus :data:`CLAUDE_RUNTIME_ENV_VARS`, as a new mapping.

    A copy, never a mutation, and that is load-bearing rather than tidy. The
    mapping handed in is ``req.env``, which ``ClaudeCodeBrain`` gives to the
    ``claude`` CLI — and writes to in place (``brain/claude_code.py`` sets
    ``IS_SANDBOX`` and ``CLAUDE_CODE_DISABLE_ADVISOR_TOOL`` on it) — and which
    ``_run_fallback`` carries across a reroute with ``dataclasses.replace``
    without rebuilding. Stripping in place would take the credential away from
    the brain that needs it on the ``native -> claude_code`` path, on the
    deployment shape where that token is the only credential there is.

    ``None`` in, ``None`` out, and an empty mapping stays an empty mapping:
    ``ToolEnv.subprocess_env`` reads ``None`` as "inherit the parent
    environment" and ``{}`` as "an empty environment", which on this path are
    opposite instructions — the parent is the daemon, whose environment is
    where the token came from. A caller collapsing one to the other after
    calling this undoes the strip; see the call site in ``NativeBrain``.
    """
    if env is None:
        return None
    return {k: v for k, v in env.items() if k not in CLAUDE_RUNTIME_ENV_VARS}
