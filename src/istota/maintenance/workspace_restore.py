"""Rebuild a stack from the backups in its own workspace: the local VM's recovery.

`db_backup` writes two things into the workspace at every run: the database
snapshots (`Backups/db/snapshots/<date>/`) and, where the workspace is a
mount, `config.toml` and the admins file (`Backups/config/<date>/`). On the
local VM the workspace is a folder on the Mac, so both outlive the VM. The
master key does not: it comes on stdin, from the Mac's login Keychain.

`istota migrate restore-workspace [--date D] [--force]` puts the three back
on a new stack, in a one-shot container with the stack stopped
(`istota-stack restore`, which the Mac's `istota-stack restore` runs after
`create --no-setup`):

1. the master key into `/data/.secret_key` (0600);
2. `config.toml` and `admins` from `Backups/config/<date>/`, `<date>` being
   `--date` or the newest copy;
3. the framework database through `db_restore` (`--date`, or the newest good
   snapshot; an empty one refused without `--force`), then each user's module
   databases, for the users the restored database holds as well as the ones
   `config.toml` names;
4. with `--vm-dir`, the stack's `.env` and `host.env` as `istota-stack`
   copied them in (`stack.env`, `host.env`, the set in effect on `<date>`),
   handed back for `istota-stack restore` to install on the VM, which this
   container cannot reach.

Everything is checked before anything is written: the key's form, the config
copy (a plain file that parses), a restorable framework snapshot, and that
nothing already there differs. A different master key, a different config or
a framework database that holds tasks is a refusal, because a restore goes
onto a new VM and anything else there is somebody's work. What a first run
wrote is the same bytes on a second, so only the database refusal stops it.

`secrets/` is never in the backups (the local-VM spec's decision): the
deployment's credentials are entered again afterwards.

Exit 0 restored, 1 refused with nothing written, 2 partial.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tomllib
from pathlib import Path

from istota.lib.workspace_marker import MARKER_NAME, marker_present, marker_required

CONFIG_FILES = ("config.toml", "admins")
KEY_NAME = ".secret_key"
#: The container's layout (`setup_wizard`'s CONTAINER_DATA_DIR and the
#: workspace mount), restated so this module does not import the wizard.
DEFAULT_WORKSPACE = "/mnt/shared"
DEFAULT_DATA_DIR = "/data"
MAX_BYTES = 1 << 20
_KEY_RE = re.compile(r"[A-Za-z0-9._=+/-]+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Refusal(Exception):
    """Nothing was written."""


def read_key(text: str) -> str:
    from istota.credentials.store import _MIN_KEY_LEN

    key = text.strip()
    if len(key) < _MIN_KEY_LEN or not _KEY_RE.fullmatch(key):
        raise Refusal("stdin holds no usable master key; pass the key the old install used, on stdin")
    return key


def _read_plain(path: Path) -> bytes | None:
    """A regular file's bytes, read without following a link at the leaf; None
    when there is nothing there."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if os.path.islink(path):
            raise Refusal(f"{path} is not a plain file") from exc
        raise Refusal(f"{path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Refusal(f"{path} is not a plain file")
        data = fh.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Refusal(f"{path} is larger than {MAX_BYTES} bytes")
    return data


def config_copy_dir(workspace: Path, date: str | None) -> Path:
    from istota.maintenance.db_backup import _CONFIG_COPY_DIR, _dated_dirs

    root = workspace / _CONFIG_COPY_DIR
    candidates = [root / date] if date else _dated_dirs(root)
    for candidate in candidates:
        if os.path.lexists(candidate / "config.toml"):
            return candidate
    when = f"for {date} " if date else ""
    raise Refusal(f"no config copy {when}in {root} (Backups/config/<date>/config.toml)")


_ENV_LINE = re.compile(r"[A-Z_][A-Z0-9_]*=[^\x00-\x08\x0b-\x1f\x7f]*")


def stack_files(workspace: Path, config_date: str) -> tuple[str | None, dict[str, str]]:
    """The stack's `.env` and `host.env` copies to hand back, and their date.

    `istota-stack` copies them in only on update, setup and apply, so the set
    in effect on `config_date` is the newest at or before it; with none that
    old, the newest there is (db_backup keeps that one past its retention).
    """
    from istota.maintenance.db_backup import (
        _CONFIG_COPY_DIR, HOST_ENV_COPY, STACK_ENV_COPY, _dated_dirs,
    )

    dated = [d for d in _dated_dirs(workspace / _CONFIG_COPY_DIR)
             if os.path.lexists(d / STACK_ENV_COPY) or os.path.lexists(d / HOST_ENV_COPY)]
    if not dated:
        return None, {}
    chosen = next((d for d in dated if d.name <= config_date), dated[0])
    files: dict[str, str] = {}
    for name in (STACK_ENV_COPY, HOST_ENV_COPY):
        data = _read_plain(chosen / name)
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refusal(f"{chosen / name} is not text") from exc
        for line in text.splitlines():
            if line.strip() and not line.startswith("#") and not _ENV_LINE.fullmatch(line):
                raise Refusal(f"{chosen / name} is not an env file: a line is not KEY=VALUE")
        files[name] = text
    return chosen.name, files


def _task_count(db_path: Path) -> int:
    from istota.maintenance.bare_metal_import import _task_count as count

    return count(db_path)


def restore(workspace: Path, config_dir: Path, data_dir: Path, key: str, *,
            date: str | None, force: bool, load, out, vm_dir: Path | None = None) -> int:
    from istota.apply import write_private
    from istota.maintenance import db_restore
    from istota.maintenance.bare_metal_import import _quick_check
    from istota.maintenance.db_backup import MODULES

    # --- checks: nothing is written until every one has passed ---
    if marker_required() and not marker_present(workspace):
        raise Refusal(f"the workspace {workspace} has no {MARKER_NAME}: it is not the folder the backups were written to")
    if db_restore._daemon_running():
        raise Refusal("the scheduler daemon is running; stop the stack first")

    source = config_copy_dir(workspace, date)
    copies: dict[str, bytes] = {}
    for name in CONFIG_FILES:
        data = _read_plain(source / name)
        if data is not None:
            copies[name] = data
    if "config.toml" not in copies:
        raise Refusal(f"no config copy in {source}")
    try:
        tomllib.loads(copies["config.toml"].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"{source / 'config.toml'} is not a config: {exc}") from exc
    env_date, env_files = stack_files(workspace, source.name) if vm_dir is not None else (None, {})

    key_path = data_dir / KEY_NAME
    current_key = _read_plain(key_path)
    if current_key is not None and current_key.decode("utf-8", "replace").strip() != key:
        raise Refusal(f"{key_path} holds a different master key; a restore goes onto a new VM")
    for name, data in copies.items():
        current = _read_plain(config_dir / name)
        if current is not None and current != data:
            raise Refusal(f"{config_dir / name} is there and differs from the copy in {source}; a restore goes onto a new VM")

    try:
        config = load(source / "config.toml")
    except Exception as exc:  # noqa: BLE001 - any load failure is a refusal here
        raise Refusal(f"{source / 'config.toml'} does not load: {exc}") from exc
    db_path = Path(config.db_path)
    if db_path.exists() and _task_count(db_path):
        raise Refusal(f"{db_path} already holds tasks: this install has done work, and a restore replaces its database")
    planned = db_restore.restore_database(config, label="framework", date=date, dry_run=True, force=force)
    if planned["status"] == "refused_empty":
        raise Refusal(f"the framework snapshot of {planned['date']} holds no data; --force restores it anyway")
    if planned["status"] != "would_restore":
        when = f"of {date} " if date else ""
        raise Refusal(f"no framework database snapshot {when}to restore ({planned['status']})")

    # --- writes ---
    if current_key is None:
        write_private(key_path, key)
        out(f"wrote the master key to {key_path}")
    for name, data in copies.items():
        target = config_dir / name
        if not target.exists():
            write_private(target, data.decode("utf-8"))
            out(f"restored {target} from {source}")
    if vm_dir is not None:
        if not env_files:
            out("no stack.env or host.env copy in the backups: the stack's .env and host.env stay as they are")
        for name, text in env_files.items():
            write_private(vm_dir / name, text)
            out(f"handed back {name} of {env_date}")

    failed = 0
    installed = config_dir / "config.toml"
    config = load(installed)
    result = db_restore.restore_database(config, label="framework", date=date, force=force)
    if result["status"] != "restored":
        out(f"framework: {result['status']} {result.get('error', '')}".rstrip())
        return 2
    out(f"framework: restored ({result['date']}, {result['rows']} rows)")
    problems = _quick_check(db_path)
    if problems:
        out("framework: quick_check: " + "; ".join(problems[:5]))
        failed += 1

    # The users the restored database holds, which config.toml need not name.
    config = load(installed)
    for user_id in sorted(config.users):
        for module in MODULES:
            # An empty module database is a module the user never used; the
            # empty-snapshot refusal is for the framework database.
            result = db_restore.restore_database(config, user=user_id, module=module, date=date, force=True)
            status = result["status"]
            if status == "no_snapshot":
                continue
            if status == "restored":
                out(f"{module}:{user_id}: restored ({result['date']}, {result['rows']} rows)")
            else:
                out(f"{module}:{user_id}: {status} {result.get('error', '')}".rstrip())
                failed += 1

    out("secrets/ is not in the backups: enter the deployment's credentials again "
        "(istota-stack put-files, the names `istota setup` asks for).")
    return 2 if failed else 0


def main(argv: list[str], *, stdin=None, out=None, err=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out_stream = out if out is not None else sys.stdout
    err_stream = err if err is not None else sys.stderr

    def say(line: str) -> None:
        print(line, file=out_stream)

    def date_arg(value: str) -> str:
        if not _DATE_RE.fullmatch(value):
            raise argparse.ArgumentTypeError(f"{value!r} is not YYYY-MM-DD")
        return value

    parser = argparse.ArgumentParser(
        prog="istota migrate restore-workspace",
        description="Restore the master key (on stdin), the config and the databases "
                    "from the workspace's own backups onto a new stack.",
    )
    parser.add_argument("--date", type=date_arg, help="the backup of this day (default: the newest)")
    parser.add_argument("--force", action="store_true", help="restore an empty framework snapshot")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--config-dir", default=f"{DEFAULT_DATA_DIR}/config")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--vm-dir", help="where to hand back the stack's .env and host.env "
                                         "copies (stack.env, host.env) for istota-stack to install")
    args = parser.parse_args(argv)

    from istota.config import load_config

    try:
        key = read_key(stdin.read())
        return restore(
            Path(args.workspace), Path(args.config_dir), Path(args.data_dir), key,
            date=args.date, force=args.force, load=load_config, out=say,
            vm_dir=Path(args.vm_dir) if args.vm_dir else None,
        )
    except Refusal as exc:
        print(f"REFUSE: {exc}", file=err_stream)
        return 1
