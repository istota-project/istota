"""What a task environment carries only because the outer process is `claude`.

Since cli-brain-tools part 2, Stage 5, no task env carries the CLI's
credential: `executor.build_clean_env` names the mode only, and the CLI's own
env builders add the credential through `llm.providers.cli_credential_env`.
The CLI runs outside the sandbox on the tool harness, and the strips below stay
as defence in depth for a task. A direct caller's request env is
`build_model_cli_env`, which holds the credential, so for it they are the
boundary (#867). The history they answer follows.

`executor.build_clean_env` copied `CLAUDE_CODE_OAUTH_TOKEN` out of the daemon's
environment into **every** task's env, for every brain, because
`ClaudeCodeBrain` authenticates with it: its outer process is the `claude`
CLI. `NativeBrain` is not —
it authenticates from its own configured provider key (`NativeBrainConfig`) and
reads nothing here — so on that path the variable has no reader at all, only a
route out: `echo "$CLAUDE_CODE_OAUTH_TOKEN"` in a Bash call comes back as a
`ToolResultMessage` addressed to whatever provider native is pointed at, which
is a provider boundary wherever that is not the credential's own issuer
(ISSUE-390).

This is the environment counterpart of the Claude-only *mount* block that
ISSUE-389 split out and the CLI harness removed; mounts and environment are
separate mechanisms and the mount split never addressed the second one.

**A name list rather than a `CLAUDE_*` prefix rule.** A prefix would also
swallow an operator's own `security.passthrough_env_vars` entry, and what an
operator put in that list on purpose is their decision, not this module's. The
drift a name list buys is covered mechanically instead:
`tests/test_security.py::TestBuildCleanEnv` requires `build_clean_env` to put
no Claude-family or credential-shaped key in a task env, and every such key
`build_model_cli_env` adds to be named here.

**What this is not.** It is not a general credential filter. The skill
credentials — `NC_PASS`, the mail passwords, the forge tokens — are taken out of
the model's environment by `_split_credential_env`, gated on
`security.skill_proxy_enabled`, which defaults on. This set exists because the
Claude token was the one name that gating never reached: `build_clean_env` set
it unconditionally and no skill manifest declares it, so `derive_credential_set`
could not see it and it survived on the *sandboxed* production shape where every
other credential was already gone. That was the whole of the gap, and it is why
ISSUE-390 was live where it was.

Two neighbouring shapes are deliberately not this module's problem. Where the
proxy is off and `sandbox_enabled = false` too, nothing is confined and there is no boundary for an environment variable to cross — the
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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Names a task env carries only for the `claude` CLI's benefit.
#:
#: Everything `llm.providers.cli_auth_env` can put in a task env, for either
#: auth mode: the OAuth token under "subscription", the `anthropic` provider's
#: key and base URL under "api_key", and the bearer name nothing sets but a
#: CLI would read. A variable `build_clean_env` sets later belongs here too,
#: unless a native tool subprocess has business reading it.
CLAUDE_RUNTIME_ENV_VARS = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
})


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


# --- The OAuth token as a file ------------------------------------------------
#
# The `claude` CLI reads its subscription credential from
# `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude`). On the stack
# `HOME` is on the data volume `web` mounts too, so the file is shared by every
# process in both containers: it follows the auth mode, written only under
# "subscription" with a token and removed otherwise, by one verb
# (`istota cli-auth sync-credentials-file`) the entrypoint runs at boot.

#: Where the file sits under a home directory.
CREDENTIALS_FILE = (".claude", ".credentials.json")

#: The scope the CLI requires of a stored token before it counts as logged in.
#: Without it the file reads as "Not logged in" (measured against the pinned
#: 2.1.296 in the Stage 3 probe; a file of only `accessToken` and `expiresAt`,
#: the shape the entrypoint used to write, never authenticated on its own).
_INFERENCE_SCOPE = "user:inference"


def credentials_file_path(home: str | None = None) -> Path:
    from pathlib import Path
    import os

    return Path(home if home is not None else os.environ.get("HOME", "/tmp")).joinpath(*CREDENTIALS_FILE)


def credentials_document(token: str) -> str:
    """The file's contents for a long-lived setup token: never expiring, no
    refresh token, the inference scope."""
    import json

    return json.dumps({"claudeAiOauth": {
        "accessToken": token,
        "refreshToken": "",
        "expiresAt": 253402300799000,
        "scopes": [_INFERENCE_SCOPE],
    }})


def sync_credentials_file(mode: str | None, token: str, home: str | None = None) -> str:
    """Make the file agree with the mode: written (0600) under "subscription"
    with a token, removed otherwise. Returns what it did: ``written``,
    ``unchanged``, ``removed`` or ``absent``. Follows no link at the file."""
    import os

    path = credentials_file_path(home)
    if mode == "subscription" and token:
        wanted = credentials_document(token)
        try:
            with open(path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as fh:
                if fh.read().decode("utf-8", "replace") == wanted:
                    return "unchanged"
        except OSError:
            pass
        from istota.lib.atomic_write import write_text_atomic

        path.parent.mkdir(mode=0o700, exist_ok=True)
        write_text_atomic(path, wanted, mode=0o600, fsync=True)
        return "written"
    return "removed" if remove_stale_credentials_file(home) else "absent"


def remove_stale_credentials_file(home: str | None = None) -> bool:
    """Remove the file when it is there (a link at the name too). True if it was."""
    path = credentials_file_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
