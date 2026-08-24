"""Developer skill — setup_env hook.

Creates the task's own subtree of ``repos_dir`` (``{repos_dir}/{user_id}`` —
the only part of that tree the sandbox binds) and sweeps it for credentials
embedded in remote URLs, stripping them (:mod:`istota.git_remote_scrub`,
ISSUE-270) before generating anything. Then generates, inside the task's user
temp directory:

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
from urllib.parse import urlsplit

# The forge-binary resolution rule lives in a stdlib-only leaf so `doctor` can
# reach it without importing `istota.skills` (whose __init__ star-imports every
# skill, ~190ms) on the `load_config` path. Re-exported under the old private
# names because this module's own call sites and the tests use them.
from istota.forge_bin import FALLBACK_BIN as _FALLBACK_BIN  # noqa: F401 - re-export
from istota.forge_bin import IMAGE_BIN as _IMAGE_BIN  # noqa: F401 - re-export
from istota.forge_bin import resolve_real_bin as _resolve_real_bin
from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, build_policy
from istota.git_remote_scrub import scrub_and_report

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


def _plain_http_host_entry(forge_url: str) -> str:
    """glab's config for a forge reached over plain HTTP, or "" for anything else.

    glab discards the scheme inside ``GITLAB_HOST`` and keeps the port, so a
    deployment configured against ``http://gitlab.internal:8080`` forces https
    and every call dies with "tls: first record does not look like a TLS
    handshake". Measured on glab 1.114.0, the version ``docker/devbox`` pins.
    ``GITLAB_API_PROTOCOL`` is not read either; the one lever glab offers is a
    per-host ``api_protocol`` in its own config file.

    That file is the one ``_seed_cli_config_dir`` truncates on every task, so
    the entry has to be written by the same code that empties it — a caller
    cannot seed it and have it survive.

    Returns "" for https, for an unset or unparseable URL, and for one carrying
    userinfo (see below) — which keeps the file empty wherever it does not have
    to carry something. Whatever is in it is honoured by both CLIs before
    dispatch, so it is not a surface to grow idly.

    The key is the lowercased host, its port if non-default, and the URL path.
    All three are measured rather than assumed: glab lowercases its lookup key,
    and it derives that key from the whole of ``GITLAB_HOST``, which
    ``build_invocation`` sets to the whole URL because a sub-path install is a
    supported shape.
    """
    if not forge_url:
        return ""
    try:
        parts = urlsplit(forge_url if "://" in forge_url else f"https://{forge_url}")
    except ValueError:
        # An unparseable URL is the operator's problem and doctor's to report.
        # This is a setup path; raising here would take the whole hook's return
        # value with it (`dispatch_setup_env_hooks` keeps only what it returned),
        # leaving a task that looks fine and cannot authenticate.
        return ""
    if parts.scheme != "http":
        return ""

    # A URL carrying userinfo gets nothing, deliberately. Measured on glab
    # 1.114.0: its lookup key *includes* the userinfo, so an entry that actually
    # matched `http://user:token@host` would have to carry the password — and
    # this file lives under `.developer`, which is bound readable into the
    # sandbox. That would hand the model a credential in order to support a
    # shape that should not exist: the token belongs in `gitlab_token`, and
    # `git_remote_scrub` exists to strip exactly this out of URLs. Better to
    # leave the call failing the way it already did and let
    # `doctor.check_forge_transport` say why.
    if "@" in (parts.netloc or ""):
        return ""

    # `hostname`, not `netloc`: `hostname` is already lowercased and free of
    # userinfo, and glab's lookup key is lowercased too — measured, an entry
    # filed under `LOCALHOST:8080` is never found and the call forces https.
    host = parts.hostname or ""
    if not host:
        return ""
    if parts.port:
        host = f"{host}:{parts.port}"
    # The path belongs in the key. `build_invocation` puts the whole URL in
    # GITLAB_HOST because a subpath install is a supported shape, and glab
    # derives its key from that — so an entry under the bare netloc is never
    # consulted for `http://forge.internal/gitlab`.
    path = (parts.path or "").rstrip("/")
    if path:
        host = f"{host}{path}"

    # Quoted, because a key carrying a port or a path is a YAML mapping key
    # containing a colon or a slash — unquoted, that is not the key it looks
    # like.
    return (
        "hosts:\n"
        f'  "{host}":\n'
        "    api_protocol: http\n"
        f'    api_host: "{host}"\n'
    )


def _seed_cli_config_dir(
    dev_bin: Path, name: str, *, forge: str = "", forge_url: str = ""
) -> Path:
    """A pre-seeded CLI config directory, at the modes each CLI will accept.

    ``config.yml`` is mode 0600, not 0400, because glab refuses to start on
    anything else ("has the permissions 400, but glab requires 600"); gh is
    happy with either. The file being owner-writable does not matter: the
    model reaches this path only through the sandbox, where ``.developer`` is
    a read-only bind, and that is what actually holds it down.

    Seeding an empty file rather than leaving the directory bare is
    deliberate. gh expands ``aliases`` from ``config.yml`` *before* command
    dispatch, so an absent file is one the model could otherwise supply.

    ``forge`` and ``forge_url`` together decide whether anything is written at
    all — see :func:`_plain_http_host_entry`. **Only glab gets an entry**, and
    the rule lives here rather than at the call site because it is not merely
    useless for gh, it is harmful: on finding a ``hosts:`` block gh runs its
    multi-account migration and writes a ``hosts.yml`` beside the config. (It
    would also buy nothing. The entry exists to reach a forge over plain HTTP,
    and gh refuses a scheme in ``GH_HOST`` outright. The *port* half is a
    different question and is handled — ``forge_cli._gh_host`` keeps a
    non-default one, ISSUE-279.) This function truncates ``config.yml`` and
    nothing else, and ``user_temp_dir`` persists across tasks — so that file
    would survive every later run, in a directory whose whole design is that
    nothing does.
    """
    cfg = dev_bin / name
    cfg.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_yml = cfg / "config.yml"
    # Rewritten on every run, not seeded once, and *replaced* rather than
    # appended to. user_temp_dir persists across tasks, so a "write it only if
    # absent" guard would make the seed a one-time act: anything that got an
    # alias table in there once — a deployment without bwrap, or a host-side
    # write — would have it honoured by every later task. The wrapper and the
    # policy are both rewritten unconditionally; this is the file that most
    # needs to be. Everything written here is derived from config, so a
    # non-empty body is as reproducible as the empty one it replaced.
    entry = _plain_http_host_entry(forge_url) if forge == FORGE_GITLAB else ""
    _atomic_write(config_yml, entry, 0o600)
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
            section["config_dir"] = str(
                _seed_cli_config_dir(
                    dev_bin, f"{forge}-config", forge=forge, forge_url=url
                )
            )
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
                _resolve_real_bin(getattr(dev, "gh_bin_path", ""), "gh"),
            ),
            FORGE_GITLAB: _section(
                FORGE_GITLAB,
                dev.gitlab_url,
                _resolve_real_bin(getattr(dev, "glab_bin_path", ""), "glab"),
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

    # ISSUE-270: strip any credential embedded in a git config under repos_dir
    # before the model can read one. `repos_dir` is bound read-write into the
    # sandbox a few steps from here, every worktree inherits its bare clone's
    # remotes, and `git remote -v` prints a URL in full — so a token in one
    # reaches the model's context as a matter of routine, around the helper
    # registered above. Nothing here writes such a config; this catches one
    # that arrived by hand.
    #
    # Last, and after `env` is complete, deliberately. `dispatch_setup_env_hooks`
    # wraps each hook in `try/except` and keeps only what it returned, so an
    # exception raised here would not fail the task — it would silently discard
    # the credential helper, GIT_CONFIG_COUNT and the forge-CLI wiring, leaving
    # a task that looks fine and cannot authenticate. `scrub_and_report` holds a
    # never-raises contract of its own; this ordering is the second guard.
    # The package caches sit inside repos_dir by default (ISSUE-319) and hold
    # one directory per unpacked wheel. Pruned from the walk: none of them is a
    # repository, and this runs on every task.
    cache_root = config.security.sandbox_cache_dir
    repos_root = _user_repos_dir(dev, ctx)
    if repos_root is not None:
        scrub_and_report(repos_root, skip=[cache_root] if cache_root else [])

    return env


def _user_repos_dir(dev, ctx) -> Path | None:
    """Create and return ``{repos_dir}/{user_id}``, or None.

    The layout rule is ``executor.get_user_repos_dir``; this is the same rule
    written a second time because a skill module cannot import the executor
    that imports it. ``tests/test_sandbox.py::TestPerUserReposDir`` holds the
    two equal, so a change to either without the other goes red.

    Created here, at 0700, because ``build_bwrap_cmd``'s ``_bind`` skips a path
    that does not exist. Without it a user's first developer task binds nothing
    at all, the model's first ``mkdir -p`` under ``$DEVELOPER_REPOS_DIR`` lands
    on bwrap's root tmpfs, and the clone it then spends minutes on disappears
    when the task ends — a working first task and a confusing one differ by
    this directory. Same shape as ``resolve_sandbox_cache_dir``'s creation of
    the per-user cache directory, deliberately: ``mkdir`` is umask-dependent,
    so the mode is set explicitly afterwards.

    Never raises. This runs late in a hook whose exceptions
    ``dispatch_setup_env_hooks`` swallows along with everything the hook
    returned, so a failure here has to be reported rather than thrown — a task
    that cannot clone is better than one that silently cannot authenticate.
    """
    user_id = getattr(getattr(ctx, "task", None), "user_id", "") or ""
    if not user_id:
        # The fallback would be the shared root, which is the cross-user reach
        # the per-user layout exists to remove. Fail closed and say so.
        logger.warning(
            "developer: no user id on the task; not creating or scrubbing a "
            "repos subtree under %s", dev.repos_dir,
        )
        return None
    repos_root = Path(dev.repos_dir) / user_id
    try:
        repos_root.mkdir(parents=True, exist_ok=True)
        os.chmod(repos_root, 0o700)
    except OSError as exc:
        logger.warning(
            "developer: could not prepare the repos subtree %s (%s); the task "
            "will have no writable repos directory inside the sandbox",
            repos_root, exc,
        )
        return None
    return repos_root
