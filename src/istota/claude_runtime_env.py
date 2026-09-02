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

**What this is not.** It is not a general credential filter. The skill
credentials — `NC_PASS`, the mail passwords, the forge tokens — are taken out of
the model's environment by `_split_credential_env`, gated on
`security.skill_proxy_enabled`, which defaults on. This set exists because the
Claude token is the one name that gating never reaches: `build_clean_env` sets
it unconditionally and no skill manifest declares it, so `derive_credential_set`
cannot see it and it survived on the *sandboxed* production shape where every
other credential was already gone. That is the whole of the gap, and it is why
ISSUE-390 was live where it was.

Two neighbouring shapes are deliberately not this module's problem. Where the
proxy is off, `setup_wizard` also writes `sandbox_enabled = false`, so nothing
is confined and there is no boundary for an environment variable to cross — the
task runs as the user and can read the config file. Where the proxy is off *and*
a sandbox is on, credentials do reach inside a real boundary; `load_config`
already warns on that pairing (ISSUE-393 is about what that warning says).

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
