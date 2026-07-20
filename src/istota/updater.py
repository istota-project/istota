"""Local single-user self-update (`istota update`).

Only meaningful for the **standalone** shape installed via ``uv tool install``
(``install.sh --standalone``). The server shape is kept current by the
Ansible-managed ``{ns}-update.sh`` cron, so ``update`` refuses to run there
rather than contend with it.

Provenance is recorded by ``install.sh`` in ``install.json`` next to the config
(``source`` checkout dir, ``extras`` string, git ``ref``, ``method``). A
``uv tool``-installed package retains no pointer back to the checkout it was
built from, so without this record ``update`` cannot know where to pull from and
errors clearly.

Every external effect (git, uv, the web-asset build, migrations, the daemon
flock probe) is injected so the orchestration is testable without a real
checkout, uv, or npm.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .config import Config
from .scheduler import DAEMON_LOCK_PATH

# A subprocess runner: takes an argv list (+ optional cwd) and returns a
# CompletedProcess. Injected so tests can record/canned-respond.
Runner = Callable[..., "subprocess.CompletedProcess"]

_SUPPORTED_METHODS = ("checkout",)


class UpdateError(RuntimeError):
    """A user-actionable failure. cli.cmd_update prints the message and exits 1."""


def install_record_path(config_path: Path | None = None) -> Path:
    """Where ``install.json`` lives — sibling to an explicit ``-c`` config file,
    else the standard ``~/.config/istota/install.json`` (where install.sh writes
    it)."""
    if config_path is not None:
        return Path(config_path).expanduser().parent / "install.json"
    return Path.home() / ".config" / "istota" / "install.json"


def load_install_record(path: Path) -> dict:
    """Read + validate the install provenance file."""
    if not path.is_file():
        raise UpdateError(
            f"No install record at {path}. This install predates `istota update` "
            "(or was installed by hand). Re-run install.sh --standalone once to "
            "write it, then `istota update` will work."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Could not read install record {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError(f"Install record {path} is not a JSON object.")
    return data


def _default_run(cmd, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=False,
    )


def _daemon_running(lock_path: Path = DAEMON_LOCK_PATH) -> bool:
    """True if the scheduler daemon holds its singleton flock (so a live
    ``istota serve`` is running old code and needs a restart to pick up the
    update). Probe by trying the lock non-blocking; success means no daemon."""
    if not lock_path.exists():
        return False
    try:
        f = open(lock_path, "a")  # append: don't truncate the daemon's lock file
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        f.close()


def _run_fresh_migrations(config_path: Path | None, run: Runner) -> None:
    """Run DB migrations from the FRESHLY-INSTALLED code by shelling out to the
    reinstalled ``istota init`` console script.

    Running the in-process ``db.init_db`` would migrate with the *old* schema
    code still resident in memory — any migration shipped in the update itself
    would silently not apply (and nothing on the daemon/serve/web startup path
    runs framework migrations to cover for it). The server auto-update script
    spawns a fresh interpreter for exactly this reason.
    """
    cmd = ["istota"]
    if config_path is not None:
        cmd += ["-c", str(config_path)]
    cmd += ["init"]
    result = run(cmd)
    if result.returncode != 0:
        raise UpdateError(
            "Post-update database migrations failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
        )


def _build_web_assets(source: Path, run: Runner) -> None:
    """Best-effort web-UI asset rebuild — mirrors install.sh's maybe_build.
    A missing build script or npm is a skip; a build failure is a warning, not
    fatal (the REPL still works, the web UI is merely stale)."""
    script = source / "scripts" / "build-web-static.sh"
    if not script.is_file():
        return
    if shutil.which("npm") is None:
        print("npm not found — skipping web UI asset rebuild (web UI may be stale).")
        return
    print("Rebuilding web UI assets (npm — this can take a minute)…")
    result = run(["bash", str(script)])
    if result.returncode != 0:
        print(
            "Warning: web UI asset build failed; the web UI may be stale. "
            "The REPL ('istota repl') still works.",
            file=sys.stderr,
        )


def run_update(
    config: Config,
    *,
    record_path: Path,
    config_path: Path | None = None,
    force: bool = False,
    run: Runner | None = None,
    build_web: Callable[[Path], None] | None = None,
    migrate: Callable[[Path], None] | None = None,
    daemon_running: Callable[[], bool] | None = None,
) -> int:
    """Update a standalone install to the latest code. Returns a process exit
    code (0 = success or already-current). Raises ``UpdateError`` on any
    actionable failure."""
    if not config.is_standalone:
        raise UpdateError(
            "`istota update` only applies to a standalone (local) install. This "
            "instance looks like a server deploy — it is updated by the "
            "Ansible-managed auto-update cron; running update here would contend "
            "with it."
        )

    run = run or _default_run
    # Default: migrate from the freshly-installed code (a subprocess), not the
    # stale in-process db.init_db. db_path is ignored by the default (the fresh
    # `istota init` resolves it from config); tests inject a fake keyed on it.
    if migrate is None:
        migrate = lambda _db: _run_fresh_migrations(config_path, run)  # noqa: E731
    daemon_running = daemon_running or _daemon_running

    record = load_install_record(record_path)
    method = record.get("method", "checkout")
    if method not in _SUPPORTED_METHODS:
        if method == "pypi":
            raise UpdateError(
                "This install records method=pypi, which `istota update` does not "
                "support yet (no PyPI release). Update with `uv tool upgrade istota`."
            )
        raise UpdateError(f"Unknown install method {method!r} in {record_path}.")

    source = Path(record.get("source", "")).expanduser()
    extras = record.get("extras", "")
    ref = record.get("ref", "main")

    if not source.is_dir() or not (source / ".git").exists():
        raise UpdateError(
            f"Recorded install source {source} is not a git checkout. Re-run "
            "install.sh --standalone to refresh the install record."
        )

    if build_web is None:
        build_web = lambda src: _build_web_assets(src, run)  # noqa: E731

    # Refuse to clobber uncommitted work in the checkout unless forced. Scope
    # the check to tracked changes (`--untracked-files=no`): `git reset --hard`
    # only touches tracked files, so untracked scratch files shouldn't force the
    # user to pass --force (which would then also discard real tracked edits).
    status = run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"])
    if status.returncode != 0:
        raise UpdateError(f"git status failed in {source}: {status.stderr.strip()}")
    if status.stdout.strip() and not force:
        raise UpdateError(
            f"The install checkout {source} has uncommitted changes. `istota update` "
            "would discard them with `git reset --hard`. Commit/stash them, or "
            "re-run with --force to overwrite."
        )

    # Fetch the recorded ref and compare against FETCH_HEAD. An explicit
    # `fetch origin <ref>` always writes FETCH_HEAD, whereas the remote-tracking
    # `origin/<ref>` ref isn't reliably updated on a shallow single-branch clone
    # (what install.sh creates) — so FETCH_HEAD is the robust target.
    fetch = run(["git", "-C", str(source), "fetch", "origin", ref])
    if fetch.returncode != 0:
        raise UpdateError(
            f"git fetch origin {ref} failed in {source}: {fetch.stderr.strip()}"
        )

    head = run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    target = run(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"]).stdout.strip()

    if head and target and head == target:
        print(f"Already up to date ({head[:12]} on origin/{ref}).")
        return 0

    print(f"Updating {source} ({head[:12] or '?'} → {target[:12] or '?'} on origin/{ref})…")
    reset = run(["git", "-C", str(source), "reset", "--hard", "FETCH_HEAD"])
    if reset.returncode != 0:
        raise UpdateError(f"git reset --hard failed in {source}: {reset.stderr.strip()}")

    # From here the checkout is advanced to the new commit but the installed
    # package is still old. If the reinstall or migration fails we roll the
    # checkout BACK to the pre-update commit — otherwise the next run would see
    # HEAD == FETCH_HEAD, report "already up to date", and never retry the
    # reinstall, silently pinning the user to the old wheel while claiming success.
    try:
        build_web(source)

        spec = f"{source}[{extras}]" if extras else str(source)
        print(f"Reinstalling {spec}…")
        install = run(["uv", "tool", "install", "--force", "--reinstall", spec])
        if install.returncode != 0:
            raise UpdateError(
                f"uv tool install failed: {install.stderr.strip() or install.stdout.strip()}"
            )

        print("Running database migrations…")
        migrate(config.db_path)
    except UpdateError:
        if head:
            # Best-effort rollback so a retry re-detects the pending update.
            run(["git", "-C", str(source), "reset", "--hard", head])
        raise

    print("Update complete.")
    if daemon_running():
        print(
            "\nA scheduler/`istota serve` process is running old code in memory — "
            "restart it to pick up the update."
        )
    else:
        print("Start it with `istota serve` (or `istota repl`).")
    return 0
