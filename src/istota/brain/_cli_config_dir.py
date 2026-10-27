"""Building a per-task ``CLAUDE_CONFIG_DIR`` for the ``claude`` CLI.

The three helpers came out of the retired tmux brain unchanged: a config dir
the daemon creates fresh each task needs its files written without following a
planted link, its onboarding state pre-seeded so the CLI does not stop at the
first-run dialogs, and a way to the subscription token, which the CLI reads
from ``$CLAUDE_CONFIG_DIR`` rather than the home copy once that is set.

``make_cli_config_dir`` is the CLI tool harness's use of them: the config dir
of one harness, which keeps the daemon's own ``~/.claude`` out of a CLI that
runs outside the sandbox.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("istota.brain.cli_config_dir")


def _write_new_file(path: Path, text: str) -> None:
    """Create ``path`` 0600 and write it; refuse anything already there.

    ``O_EXCL`` refuses an existing file and a symlink alike, so a link planted
    between the daemon's ``mkdir`` and this write fails the attempt instead of
    being written through. The descriptor is closed by hand only where
    ``os.fdopen`` itself raises, as in ``executor._write_control_file``.
    """
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
    )
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        handle.write(text)


def _seed_onboarding(config_dir: Path, launch_cwd: Path) -> None:
    """Pre-seed ``<CLAUDE_CONFIG_DIR>/.claude.json`` so the CLI skips first-run
    onboarding — the theme picker, the workspace-trust dialog, and the
    Bypass-Permissions warning.

    A per-task config dir is empty each time, so without this the CLI re-runs
    onboarding every time. These keys (verified present in the CLI binary)
    mark the install and this project as already onboarded.

    Not best-effort about the write itself: a failure here means something
    is already at that name in a directory the daemon just created, and a
    ``.claude.json`` the CLI then reads is not one to start a session on."""
    cfg = {
        "theme": "dark",
        "hasCompletedOnboarding": True,
        "bypassPermissionsModeAccepted": True,
        "projects": {
            str(launch_cwd): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }
        },
    }
    _write_new_file(config_dir / ".claude.json", json.dumps(cfg))


def _link_credentials(config_dir: Path, home: str) -> None:
    """Point a per-task `CLAUDE_CONFIG_DIR` at the subscription token.

    The CLI reads `$CLAUDE_CONFIG_DIR/.credentials.json`, not
    `~/.claude/.credentials.json`, once `CLAUDE_CONFIG_DIR` is set, so with
    the token off the environment the session needs a way to the file. A link
    at that name to the home copy, which the CLAUDE wrap binds read-only at
    the same path under "subscription". Nothing when there is no file (a Mac
    checkout, where the CLI keeps its login in the keychain).
    """
    if not home:
        return
    target = Path(home) / ".claude" / ".credentials.json"
    if not target.is_file():
        return
    try:
        os.symlink(target, Path(config_dir) / ".credentials.json")
    except OSError as e:
        logger.warning("cli_config_dir: could not link the credentials file: %s", type(e).__name__)


def make_cli_config_dir(
    harness_dir: Path, auth_mode: str | None, *, home: str, launch_cwd: Path,
) -> Path:
    """Create ``{harness_dir}/config`` 0700 and fill it for one CLI run.

    The directory is created with no ``exist_ok``: anything already at the
    name fails the harness rather than being adopted. It holds an empty
    ``settings.json`` (``--settings`` carries istota's document, so nothing of
    the operator's own settings applies), a seeded ``.claude.json`` for
    ``launch_cwd``, and under "subscription" a link to the daemon's
    ``{home}/.claude/.credentials.json``. Nothing else of ``{home}/.claude``
    is read or linked. Raises ``OSError``; the caller reports it.
    """
    config_dir = Path(harness_dir) / "config"
    config_dir.mkdir(mode=0o700)
    _write_new_file(config_dir / "settings.json", "{}")
    _seed_onboarding(config_dir, launch_cwd)
    if auth_mode == "subscription":
        _link_credentials(config_dir, home)
    return config_dir
