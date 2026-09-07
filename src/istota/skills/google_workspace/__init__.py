"""Google Workspace skill — setup_env hook and CLI passthrough.

**The passthrough is the one skill CLI whose arguments nothing can enumerate,
so its boundary is on argv.** `main` execs `gws`, a program that is not in
this tree and is not installed on the development machine, and its own
`skill.md` documents `drive +upload /path/to/file.pdf` — a host read that
lands in Google Drive. Every other skill declares its path arguments and
`tests/test_skill_host_paths_coverage.py` walks them; here there is no parser
to walk, so `scan_argv` classifies each token instead and a path-shaped one is
resolved through the shared allowlist before the exec (ISSUE-447 Layer 4).

Coarser than a declaration, and chosen over the alternative deliberately: a
per-verb policy table for gws would be a hand-maintained description of a
program we do not ship, which is the staleness the rest of that work removes.
The costs are stated rather than hidden.

- **A path this scan cannot see passes through unscoped.** A token that is
  neither absolute, nor `~`-prefixed, nor holding a `..` component, nor naming
  something that exists relative to the cwd is not path-shaped — which is
  exactly a *relative destination that does not exist yet*. That residual is
  carried by the cwd rather than by another layer: `main` stands in the user's
  own workspace before the exec, so such a path lands in-roots by
  construction rather than beside the daemon's working directory.
- **One rule for reads and uploads alike.** A token is resolved with
  `writable=False`, so it must exist and it is admitted from the task's whole
  working context — `{mount}/Talk` included. An upload is egress and the
  narrower `OWN` set would fit it better, but telling an upload from a read
  means knowing gws's verbs, which is the table above. The scan applies the
  read rule and says so here.
- **False positives are refusals, not silent passes.** A `~`-prefixed token
  and a destination that does not exist yet are both refused where they are
  path-shaped, which is the direction to be wrong in.
- **A workspace file named like a gws verb rewrites the verb.** The cwd is the
  workspace, so `drive` is path-shaped when a file of that name sits there and
  the token becomes an absolute path where gws wants a service name. `docs`,
  `calendar`, `chat` and `list` are all plausible filenames in a directory the
  model writes to. The command then fails in gws rather than doing something
  unintended, which is why the existence signal stays: dropping it for
  separator-less tokens would let a bare `secret.pdf` beside a cwd that is
  *not* the workspace through unscanned, trading a loud failure for a quiet
  read.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from istota.skill_host_paths import resolve_host_path, user_workspace_root
from istota.skills._cli import fail

logger = logging.getLogger("istota.skills.google_workspace")


def _is_expired(token_expiry: str) -> bool:
    """Check if the token expiry (ISO 8601 UTC) is in the past."""
    try:
        expiry = datetime.fromisoformat(token_expiry).replace(tzinfo=timezone.utc)
        # Refresh 60s before actual expiry to avoid race conditions
        return datetime.now(timezone.utc) >= expiry
    except (ValueError, TypeError):
        return True


def _refresh_token(refresh_token: str, client_id: str, client_secret: str) -> dict | None:
    """Refresh a Google OAuth access token.

    Returns dict with access_token, expires_in on success, None on failure.
    """
    import httpx

    try:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("Google token refresh failed: %s %s", resp.status_code, resp.text)
            return None
        return resp.json()
    except Exception as e:
        logger.warning("Google token refresh error: %s", e)
        return None


def setup_env(ctx) -> dict[str, str]:
    """Inject GOOGLE_WORKSPACE_CLI_TOKEN from DB, refreshing if expired."""
    config = ctx.config
    gw_config = getattr(config, "google_workspace", None)
    if not gw_config or not gw_config.enabled:
        return {}

    user_id = ctx.task.user_id

    from istota import db as _db

    with _db.get_db(config.db_path) as conn:
        token_data = _db.get_google_token(conn, user_id)

    if not token_data:
        return {}

    access_token = token_data["access_token"]

    if _is_expired(token_data["token_expiry"]):
        refreshed = _refresh_token(
            token_data["refresh_token"],
            gw_config.client_id,
            gw_config.client_secret,
        )
        if not refreshed or "access_token" not in refreshed:
            logger.warning("Could not refresh Google token for user %s", user_id)
            return {}

        access_token = refreshed["access_token"]
        expires_in = refreshed.get("expires_in", 3600)
        new_expiry = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta
        new_expiry = (new_expiry + timedelta(seconds=expires_in)).isoformat()

        # Google may return a new refresh token; use it if present
        new_refresh = refreshed.get("refresh_token", token_data["refresh_token"])

        with _db.get_db(config.db_path) as conn:
            _db.upsert_google_token(
                conn, user_id, access_token, new_refresh,
                new_expiry, token_data["scopes"],
            )
        logger.debug("Refreshed Google token for user %s", user_id)

    env = {"GOOGLE_WORKSPACE_CLI_TOKEN": access_token}

    # Point gws config/cache to the writable temp dir (sandbox HOME is read-only)
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(ctx.user_temp_dir / "gws_cache")

    return env


def _is_path_shaped(token: str) -> bool:
    """Whether this token could be naming a host path.

    Four signals, and the last is the only one that touches the filesystem:
    an absolute path, a `~` prefix, a `..` component, or a name that exists
    relative to the process cwd. Deliberately wide in the first three — a
    `..` token is scanned whether or not anything is there today, since it
    names something above the cwd either way.

    Never raises: `token` comes off the model's command line, and a value
    `Path` refuses (an embedded null byte) is not a path this program can be
    handed either.
    """
    if not token:
        return False
    if token.startswith(("/", "~")):
        return True
    try:
        if ".." in Path(token).parts:
            return True
        return Path(token).exists()
    except (OSError, ValueError):
        return False


def _scoped(token: str, operation: str) -> tuple[str | None, str | None]:
    """The token's resolution, or the refusal. Never the token unchanged."""
    resolved, error = resolve_host_path(
        Path(token), writable=False, operation=f"gws {operation}",
    )
    if error is not None:
        return None, error
    return str(resolved), None


def _attached_values(token: str) -> list[tuple[str, str, str]]:
    """`(prefix, candidate value, operation)` for each way a token can hold one.

    A path does not have to *be* the token to reach gws. Three spellings put
    one inside it, and a scan that reads only whole tokens passes all three:

    - `--file=/abs/path`, one token that starts with a dash, holds no `..` and
      names nothing relative to the cwd, so gws splits it and we would not.
      Applied to every token rather than only a dashed one, since the split
      costs nothing on a token with no `=` and some CLIs take `key=value`
      positionally.
    - `-f/abs/path`, the short-flag spelling of the same thing, which
      argparse, Click, pflag and cobra all accept. Whether gws does is
      unverified — it is not in this tree — which is the reason to scan it
      rather than the reason not to.
    - `@/abs/path`, the response-file convention. Also unverified for gws, and
      this codebase has its own precedent in the `--paste @PATH` form ISSUE-447
      removed from `health`.

    Order is widest-first and every candidate is *tested* before it is used, so
    a token holding no path is returned untouched however many candidates it
    produced.
    """
    out: list[tuple[str, str, str]] = []
    flag, sep, value = token.partition("=")
    if sep:
        out.append((f"{flag}=", value, flag or "argument"))
    if len(token) > 2 and token[0] == "-" and token[1] != "-":
        out.append((token[:2], token[2:], token[:2]))
    if len(token) > 1 and token[0] == "@":
        out.append(("@", token[1:], "@argument"))
    return out


def _scan_token(token: str) -> tuple[str | None, str | None]:
    """One token, rewritten to its resolution where it names a host path."""
    if _is_path_shaped(token):
        return _scoped(token, "argument")
    for prefix, value, operation in _attached_values(token):
        if not _is_path_shaped(value):
            continue
        resolved, error = _scoped(value, operation)
        if error is not None:
            return None, error
        return f"{prefix}{resolved}", None
    return token, None


def scan_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """The argv to exec, or `([], refusal)`.

    Each token is passed through untouched or replaced by its resolution.
    Untouched is the common case and has to be: Google Workspace addresses
    its own objects by opaque id — `--fileId`, `--parents FOLDER_ID`,
    spreadsheet ids — and a query may legitimately contain a slash
    (`--query "name contains 'a/b'"`), none of which is path-shaped.

    **There is no `--` arm, and its absence is the rule rather than an
    omission.** Everything after a separator is scanned exactly as everything
    before it is, which is what "gws takes no positional passthrough today, so
    treating the tail as opaque would be a bypass by one character" means. An
    earlier version did have one, and it read `--` as a switch to whole-token
    testing — which is *narrower* than the attached-value scan, not wider, so
    `-- --file=/etc/passwd` went through untouched. Scanning both readings of
    every token is the wide answer; making one of them conditional on a token
    gws may or may not honour is how the bypass got back in.
    """
    out: list[str] = []
    for token in argv:
        rewritten, error = _scan_token(token)
        if error is not None:
            return [], error
        out.append(rewritten)
    return out, None


def _enter_workspace() -> str | None:
    """Stand in the user's own workspace. The refusal, or None.

    This is what carries the residual `scan_argv` cannot classify: a relative
    token naming something that does not exist yet is not path-shaped, passes
    through, and is resolved by gws against *this* process's cwd — which for a
    proxied skill is the daemon's working directory. Standing in the workspace
    puts it in-roots by construction.

    **A workspace that resolves and cannot be entered is a refusal, not a
    warning.** `user_workspace_root` does not check that the directory exists,
    so a deployment whose workspace has not been created yet gets a path and an
    `ENOENT` from `chdir` — and carrying on there would leave the residual
    resolving against the daemon's working directory, which is the one thing
    this function exists to prevent, with only a log line saying so.

    A deployment where no workspace *resolves at all* is a different state and
    is left where it is: the allowlist is empty or nearly so, every path-shaped
    token is refused by it, and refusing the invocation outright would break
    the opaque-id verbs that have nothing to do with host paths. The residual
    is uncarried there and is the narrower one — a relative path naming
    something that does not exist.
    """
    root = user_workspace_root()
    if root is None:
        return None
    try:
        os.chdir(root)
    except OSError as e:
        logger.warning("Could not enter the workspace before gws: %s", e)
        return (
            "Could not enter the workspace before running gws "
            f"({e.strerror or e}). Refusing rather than resolving relative "
            "paths against the daemon's working directory."
        )
    return None


def main() -> None:
    """Scan the argv for host paths, then pass through to the gws binary."""
    refusal = _enter_workspace()
    if refusal is None:
        argv, refusal = scan_argv(sys.argv[1:])
    if refusal is not None:
        fail(refusal, reason="host_path_refused")
    os.execvp("gws", ["gws"] + argv)
