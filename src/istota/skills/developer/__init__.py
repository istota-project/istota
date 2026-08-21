"""Developer skill — setup_env hook.

Generates, inside the task's user temp directory:

- the credential-fetch helper and the per-platform git-credential-helper
  scripts, plus the ``GIT_CONFIG_*`` vars that point git at them;
- ``gh`` and ``glab``, copies of :mod:`istota.forge_cli` that wrap the real
  binaries, and the policy file they read;
- a seeded, read-only config directory per CLI.

Everything lands in ``{user_temp_dir}/.developer``, which ``build_bwrap_cmd``
re-binds read-only inside the sandbox (and which ``native_fs_roots`` excludes
from the native brain's write roots). That is what makes the wrapper, the
policy and the alias table unwritable by the model — none of it would mean
anything in a directory the model could edit.

Static env vars (DEVELOPER_REPOS_DIR, GITLAB_URL, GITHUB_URL, the optional
namespace/owner/reviewer/credit knobs, GITLAB_TOKEN, GITHUB_TOKEN) come
from the manifest's ``env:`` block — this hook only handles the parts
that aren't expressible as static EnvSpecs.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, build_policy

logger = logging.getLogger("istota.skills.developer")

# Where the canonical wrapper lives, for copying into the task's .developer.
_FORGE_CLI_SOURCE = Path(__file__).resolve().parents[2] / "forge_cli.py"


def _write_forge_cli(dev_bin: Path, name: str) -> Path:
    """Install the wrapper under one of the names it dispatches on.

    A copy rather than a symlink: ``forge_from_argv0`` reads ``argv[0]``, and
    the sandbox's view of a symlink's target is one more thing to get wrong
    for no benefit.
    """
    dest = dev_bin / name
    shutil.copyfile(_FORGE_CLI_SOURCE, dest)
    dest.chmod(0o700)
    return dest


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
    cfg.mkdir(parents=True, exist_ok=True)
    config_yml = cfg / "config.yml"
    if not config_yml.exists():
        config_yml.write_text("")
    config_yml.chmod(0o600)
    return cfg


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
        policy = {
            FORGE_GITHUB: build_policy(
                FORGE_GITHUB,
                extra_denied=list(getattr(dev, "forge_cli_extra_denied", [])),
                permit=list(getattr(dev, "forge_cli_permit", [])),
            ),
            FORGE_GITLAB: build_policy(
                FORGE_GITLAB,
                extra_denied=list(getattr(dev, "forge_cli_extra_denied", [])),
                permit=list(getattr(dev, "forge_cli_permit", [])),
            ),
        }
        policy_path = dev_bin / "forge-policy.json"
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True))
        policy_path.chmod(0o600)

        _write_forge_cli(dev_bin, "gh")
        _write_forge_cli(dev_bin, "glab")
        # The retired names, so a cached habit or an old CRON.md job gets the
        # one-line explanation rather than "command not found".
        _write_forge_cli(dev_bin, "github-api")
        _write_forge_cli(dev_bin, "gitlab-api")

        # Writable scratch for the CLIs' own state — gh drops a device id
        # under $XDG_STATE_HOME on every run, and that must not land in the
        # read-only config dir or in a possibly read-only HOME.
        state_dir = user_temp_dir / ".forge-state"
        state_dir.mkdir(parents=True, exist_ok=True)

        env["ISTOTA_FORGE_POLICY"] = str(policy_path)
        env["ISTOTA_GH_REAL"] = getattr(dev, "gh_bin_path", "") or "/usr/local/bin/gh"
        env["ISTOTA_GLAB_REAL"] = (
            getattr(dev, "glab_bin_path", "") or "/usr/local/bin/glab"
        )
        env["ISTOTA_GH_CONFIG_DIR"] = str(_seed_cli_config_dir(dev_bin, "gh-config"))
        env["ISTOTA_GLAB_CONFIG_DIR"] = str(
            _seed_cli_config_dir(dev_bin, "glab-config")
        )
        env["ISTOTA_FORGE_STATE_DIR"] = str(state_dir)
        env["ISTOTA_GH_URL"] = dev.github_url
        env["ISTOTA_GITLAB_URL"] = dev.gitlab_url
        # Reserved key: the executor prepends this to the *model's* PATH only,
        # after snapshotting the environment it gives host-side skill CLIs.
        # See executor.HOOK_PATH_PREPEND_KEY — the ordering is a security
        # property, not housekeeping.
        env["ISTOTA_PATH_PREPEND"] = str(dev_bin)

    return env
