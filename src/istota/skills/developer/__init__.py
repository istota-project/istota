"""Developer skill — setup_env hook.

Generates, inside the task's user temp directory:

- the credential-fetch helper and the per-platform git-credential-helper
  scripts, plus the ``GIT_CONFIG_*`` vars that point git at them;
- ``gh`` and ``glab``, copies of :mod:`istota.forge_cli` that wrap the real
  binaries, and the policy file they read;
- a seeded, read-only config directory per CLI.

Everything lands in ``{user_temp_dir}/.developer``, which ``build_bwrap_cmd``
re-binds read-only inside the sandbox and which ``native_fs_roots`` excludes
from the native brain's write roots. Those two together are what stop the
model's *own file tools* rewriting the wrapper, the policy or gh's alias
table, which is the level of protection an accident guard needs.

It is not an absolute. ``user_temp_dir`` is also the deferred directory, which
``skill_host_paths`` admits as a host-side write root, so a determined model
has paths to that directory that neither the bind nor the deny root covers.
Same posture as the policy itself: it stops a mistake, not a decision. The
boundary that does the real work is the forge token's own scope.

Static env vars (DEVELOPER_REPOS_DIR, GITLAB_URL, GITHUB_URL, the optional
namespace/owner/reviewer/credit knobs, GITLAB_TOKEN, GITHUB_TOKEN) come
from the manifest's ``env:`` block — this hook only handles the parts
that aren't expressible as static EnvSpecs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, build_policy

logger = logging.getLogger("istota.skills.developer")

# Where the canonical wrapper lives, for copying into the task's .developer.
_FORGE_CLI_SOURCE = Path(__file__).resolve().parents[2] / "forge_cli.py"


def _atomic_write(dest: Path, data: str, mode: int) -> Path:
    """Write via a temp file in the same directory, then rename.

    Tasks for one user share ``user_temp_dir`` and the worker pool runs them
    concurrently, so a plain truncate-then-write can be read half-finished by
    a wrapper another task is running right now. ``os.replace`` is atomic and
    leaves an already-open process on its own inode.
    """
    tmp = dest.with_name(f".{dest.name}.tmp{os.getpid()}")
    tmp.write_text(data)
    tmp.chmod(mode)
    os.replace(tmp, dest)
    return dest


def _write_forge_cli(dev_bin: Path, name: str) -> Path:
    """Install the wrapper under one of the names it dispatches on.

    A copy rather than a symlink: ``forge_from_argv0`` reads ``argv[0]``, and
    the sandbox's view of a symlink's target is one more thing to get wrong
    for no benefit.
    """
    return _atomic_write(
        dev_bin / name, _FORGE_CLI_SOURCE.read_text(), 0o700,
    )


def _seed_cli_config_dir(dev_bin: Path, name: str) -> Path:
    """A pre-seeded CLI config directory, at the modes each CLI will accept.

    ``config.yml`` is mode 0600, not 0400, because glab refuses to start on
    anything else ("has the permissions 400, but glab requires 600"); gh is
    happy with either. The file being owner-writable does not matter: the
    model reaches this path only through the sandbox, where ``.developer`` is
    a read-only bind, and that is what actually holds it down.

    Seeding an empty file rather than leaving the directory bare is
    deliberate. gh expands ``aliases`` from ``config.yml`` *before* command
    dispatch, so an absent file is one the model could otherwise supply.
    """
    cfg = dev_bin / name
    cfg.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_yml = cfg / "config.yml"
    # Truncated on every run, not seeded once. user_temp_dir persists across
    # tasks, so a "write it only if absent" guard would make the seed a
    # one-time act: anything that got an alias table in there once — a
    # deployment without bwrap, or a host-side write — would have it honoured
    # by every later task. The wrapper and the policy are both rewritten
    # unconditionally; this is the file that most needs to be.
    _atomic_write(config_yml, "", 0o600)
    return cfg


def _pinned_data_dir(dev_bin: Path, name: str) -> Path:
    """An empty directory to point XDG_DATA_HOME at.

    gh dispatches an unknown first argument to ``gh-<name>`` in
    ``$XDG_DATA_HOME/gh/extensions``, and the argv rules cannot see that — the
    argv is ``gh <name>`` and matches nothing. Left unset, gh derives the path
    from HOME, whose ``.local/share`` is writable inside the sandbox. Verified
    against gh 2.98: a planted extension runs, and pinning the variable at an
    empty directory stops it.
    """
    data = dev_bin / name
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    return data


def setup_env(ctx) -> dict[str, str]:
    """Write helper scripts and return the GIT_CONFIG_* / forge-CLI env vars.

    Self-gates on ``config.developer.enabled`` and a non-empty
    ``repos_dir`` — the hook is invoked for every skill in the index, so
    skills must opt themselves out when their config isn't ready.
    """
    config = ctx.config
    dev = getattr(config, "developer", None)
    if dev is None or not dev.enabled or not dev.repos_dir:
        return {}

    env: dict[str, str] = {}
    user_temp_dir = Path(ctx.user_temp_dir)
    dev_bin = user_temp_dir / ".developer"
    dev_bin.mkdir(parents=True, exist_ok=True)

    use_proxy = config.security.skill_proxy_enabled
    cred_fetch_cmd = ""
    if use_proxy:
        cred_fetch = dev_bin / "credential-fetch"
        cred_fetch.write_text(
            "#!/usr/bin/env python3\n"
            "import json, socket, sys\n"
            "import os\n"
            "sock_path = os.environ.get('ISTOTA_SKILL_PROXY_SOCK', '')\n"
            "if not sock_path:\n"
            "    print('ISTOTA_SKILL_PROXY_SOCK not set', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.connect(sock_path)\n"
            "s.sendall(json.dumps({'type': 'credential', 'name': sys.argv[1]}).encode() + b'\\n')\n"
            "d = b''\n"
            "while b'\\n' not in d:\n"
            "    c = s.recv(4096)\n"
            "    if not c: break\n"
            "    d += c\n"
            "s.close()\n"
            "r = json.loads(d)\n"
            "if 'error' in r:\n"
            "    print(r['error'], file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print(r.get('value', ''), end='')\n"
        )
        cred_fetch.chmod(0o700)
        cred_fetch_cmd = str(cred_fetch)

    def _token_expr(var_name: str) -> str:
        # Quoted: git's credential protocol wants the value verbatim, and an
        # unquoted expansion is word-split by sh and rejoined by echo on
        # single spaces. No PAT format has whitespace today; this costs two
        # characters and removes a way for a future one to fail unreadably.
        if use_proxy:
            return f'"$({cred_fetch_cmd} {var_name})"'
        return f'"${var_name}"'

    git_config_index = 0

    if dev.gitlab_token:
        gitlab_host = dev.gitlab_url.rstrip("/")

        git_cred = dev_bin / "git-credential-helper"
        git_cred.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "get" ] || exit 0\n'
            f"echo username={dev.gitlab_username}\n"
            f"echo password={_token_expr('GITLAB_TOKEN')}\n"
        )
        git_cred.chmod(0o700)
        env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{gitlab_host}.helper"
        env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(git_cred)
        git_config_index += 1

    if dev.github_token:
        github_host = dev.github_url.rstrip("/")
        gh_username = dev.github_username or "x-access-token"

        gh_cred = dev_bin / "git-credential-helper-github"
        gh_cred.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "get" ] || exit 0\n'
            f"echo username={gh_username}\n"
            f"echo password={_token_expr('GITHUB_TOKEN')}\n"
        )
        gh_cred.chmod(0o700)
        env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{github_host}.helper"
        env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(gh_cred)
        git_config_index += 1

    if git_config_index > 0:
        env["GIT_CONFIG_COUNT"] = str(git_config_index)

    # --- Forge CLIs -------------------------------------------------------
    #
    # Installed whenever either token is configured. Both names go on PATH
    # regardless: `glab` with no GitLab token exits 5 with the proxy's own
    # message, which is a clearer failure than "command not found" leading
    # the model to the real binary and an unauthenticated call.
    if dev.gitlab_token or dev.github_token:
        state_dir = user_temp_dir / ".forge-state"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        def _section(forge: str, url: str, real_bin: str) -> dict:
            section = build_policy(
                forge,
                extra_denied=list(getattr(dev, "forge_cli_extra_denied", [])),
                permit=list(getattr(dev, "forge_cli_permit", [])),
            )
            section["url"] = url
            section["real_bin"] = real_bin
            section["config_dir"] = str(_seed_cli_config_dir(dev_bin, f"{forge}-config"))
            section["data_dir"] = str(_pinned_data_dir(dev_bin, f"{forge}-data"))
            section["state_dir"] = str(state_dir)
            # With the skill proxy off there is no socket to ask, and the
            # token is legitimately in the environment rather than having
            # escaped a stripping step. Saying so here rather than in an env
            # var matters: this file is the one input the model cannot
            # redirect, so it is the only safe place to grant that permission.
            section["direct_token"] = not use_proxy
            return section

        policy = {
            FORGE_GITHUB: _section(
                FORGE_GITHUB,
                dev.github_url,
                getattr(dev, "gh_bin_path", "") or "/usr/local/bin/gh",
            ),
            FORGE_GITLAB: _section(
                FORGE_GITLAB,
                dev.gitlab_url,
                getattr(dev, "glab_bin_path", "") or "/usr/local/bin/glab",
            ),
        }
        _atomic_write(
            dev_bin / "forge-policy.json",
            json.dumps(policy, indent=2, sort_keys=True),
            0o600,
        )

        _write_forge_cli(dev_bin, "gh")
        _write_forge_cli(dev_bin, "glab")
        # The retired names, so a cached habit or an old CRON.md job gets the
        # one-line explanation rather than "command not found".
        _write_forge_cli(dev_bin, "github-api")
        _write_forge_cli(dev_bin, "gitlab-api")

        # The only var the wrapper needs from the environment. Everything else
        # it might have read from there — the policy, the real binary, the
        # config and data dirs, the forge URL — now travels in the policy file,
        # because the wrapper runs as a child of the model's own shell and an
        # env-supplied path is a path the model chooses.
        #
        # Reserved key: the executor prepends this to the *model's* PATH only,
        # after snapshotting the environment it gives host-side skill CLIs.
        # See executor.HOOK_PATH_PREPEND_KEY — that ordering is a security
        # property, not housekeeping.
        env["ISTOTA_PATH_PREPEND"] = str(dev_bin)

    return env
