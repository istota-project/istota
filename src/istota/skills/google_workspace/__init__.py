"""Google Workspace operations with scoped, private copies of host input files.

The external CLI's argument grammar is not available here. The scanner keeps
its existing classification: absolute paths, traversal, existing relative
names and attached flag values are scoped as reads. Opaque Google identifiers
pass through. Each admitted regular file is copied through a confined fd,
then made read-only. Directory arguments are refused.

Relative input names are interpreted in the user's workspace. Execution uses
an empty, non-writable private cwd, so an unrecognized relative output cannot
write into that workspace. Local output files are unsupported; use stdout.
The parent skill launcher owns the staging directory and removes it after
exit or timeout. Keeping exec lets that parent terminate gws itself.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from istota.sandbox.host_paths import resolve_host_path, stage_resolved, user_workspace_root
from istota.skills._cli import fail

logger = logging.getLogger("istota.skills.google_workspace")


def setup_env(ctx) -> dict[str, str]:
    """Inject GOOGLE_WORKSPACE_CLI_TOKEN from DB, refreshing if expired."""
    from istota import db
    from istota.credentials.google_tokens import access_token_for

    gw_config = getattr(ctx.config, "google_workspace", None)
    if not gw_config or not gw_config.enabled:
        return {}
    with db.get_db(ctx.config.db_path) as conn:
        access_token = access_token_for(conn, ctx.config, ctx.task.user_id)
    if access_token is None:
        return {}

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


def _scoped(token: str, operation: str, stage=None) -> tuple[str | None, str | None]:
    """The token's resolution, or the refusal. Never the token unchanged."""
    resolved, error = resolve_host_path(
        Path(token), writable=False, operation=f"gws {operation}",
    )
    if error is not None:
        return None, error
    if stage is not None:
        try:
            resolved = stage(resolved)
        except OSError:
            return None, "Host file is not a safely readable regular file"
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


def _scan_token(token: str, stage=None) -> tuple[str | None, str | None]:
    """One token, rewritten to its resolution where it names a host path."""
    if _is_path_shaped(token):
        return _scoped(token, "argument", stage)
    for prefix, value, operation in _attached_values(token):
        if not _is_path_shaped(value):
            continue
        resolved, error = _scoped(value, operation, stage)
        if error is not None:
            return None, error
        return f"{prefix}{resolved}", None
    return token, None


def scan_argv(argv: list[str], *, stage=None) -> tuple[list[str], str | None]:
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
        rewritten, error = _scan_token(token, stage)
        if error is not None:
            return [], error
        out.append(rewritten)
    return out, None


def _enter_workspace() -> str | None:
    """Use the workspace to interpret relative inputs before staging them."""
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
    """Run gws with read-only input copies owned and reaped by the skill proxy."""
    host_files = os.environ.get("ISTOTA_HOST_FILES_DIR")
    if not host_files:
        fail("Google Workspace must run through istota-skill or a scheduled skill launcher", reason="host_path_refused")
    refusal = _enter_workspace()
    if refusal is not None:
        fail(refusal, reason="host_path_refused")
    scan_cwd = Path.cwd()
    with ExitStack() as stack:
        def stage(path):
            return stack.enter_context(stage_resolved(path, readonly=True))

        argv, refusal = scan_argv(sys.argv[1:], stage=stage)
        if refusal is not None:
            fail(refusal, reason="host_path_refused")
        directory = stack.enter_context(tempfile.TemporaryDirectory(dir=host_files, prefix="cwd-"))
        Path(directory).chmod(0o500)
        try:
            os.chdir(directory)
            # Keep exec: the proxy must kill and reap gws itself on timeout.
            # The proxy owns the copies' lifetime even after Python is gone.
            os.execvp("gws", ["gws"] + argv)
        except OSError as e:
            if isinstance(e, FileNotFoundError):
                fail("The gws binary is not installed on this deployment.", reason="gws_missing")
            fail(
                f"The gws binary could not be run ({e.strerror or e}).",
                reason="gws_exec_failed",
            )
        finally:
            os.chdir(scan_cwd)
            Path(directory).chmod(0o700)
