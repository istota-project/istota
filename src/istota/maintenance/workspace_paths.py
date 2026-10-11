"""Rewrite a migrated install's old host paths in its users' workspace files.

The bare-metal import rebases the paths the database stores, but the files in
each user's bot directory (CRON.md, the scripts and prompt files its jobs run,
playbooks) are the user's and still name the old host: `/srv/mount/...`, the
old temp directory, the old venv's binaries. This rewrites them with the same
prefix map the import used, keeping a `<name>.pre-migration` copy of each file
it changes, and lists any old-home path it had no mapping for.

`istota migrate workspace-paths` runs it on an imported install (the map comes
from the import's `_migration` record, or `--map OLD=NEW` and `--old-home`);
`istota migrate import-bare-metal --rewrite-workspace` runs it straight after
an import.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

BACKUP_SUFFIX = ".pre-migration"
TEXT_SUFFIXES = frozenset({".md", ".txt", ".py", ".sh", ".toml", ".json", ".yaml", ".yml"})
MAX_BYTES = 1 << 20
# Characters that end a path in prose, shell and code.
_PATH_END = r"(?=[/\s'\"`)\]},;:]|$)"


@dataclass
class Result:
    changed: list[tuple[Path, int]] = field(default_factory=list)
    leftovers: list[tuple[Path, int, str]] = field(default_factory=list)


def compile_rules(prefixes: list[tuple[str, str]], old_home: str = "") -> list[tuple[re.Pattern, str]]:
    """The substitutions, in the order they apply: the old venv's binaries
    first (a bare command in the container), then prefixes longest first."""
    rules: list[tuple[re.Pattern, str]] = []
    home = old_home.rstrip("/")
    if home:
        rules.append((re.compile(re.escape(home) + r"(?:/[^\s'\"`]*?)?/\.venv/bin/"), ""))
    for old, new in sorted(prefixes, key=lambda p: len(p[0]), reverse=True):
        old = old.rstrip("/")
        if old:
            rules.append((re.compile(re.escape(old) + _PATH_END), new.rstrip("/") or "/"))
    return rules


def rewrite_text(text: str, rules: list[tuple[re.Pattern, str]]) -> tuple[str, int]:
    total = 0
    for pattern, replacement in rules:
        text, count = pattern.subn(lambda _m, r=replacement: r, text)
        total += count
    return text, total


def candidate_files(workspace_root: Path, bot_dir: str) -> list[Path]:
    """Text files under every `Users/<user>/<bot_dir>/`, no links, no backups.

    The bot directory is bound read-write into its owner's sandbox, so a link
    in it is model-plantable: the walk follows none, and the reads and writes
    below open with O_NOFOLLOW."""
    files: list[Path] = []
    users = workspace_root / "Users"
    if not users.is_dir():
        return files
    for user_dir in sorted(users.iterdir()):
        base = user_dir / bot_dir
        if user_dir.is_symlink() or base.is_symlink() or not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name.endswith(BACKUP_SUFFIX) or path.suffix not in TEXT_SUFFIXES:
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES:
                    files.append(path)
    return files


def _read(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def _write(path: Path, text: str, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def run(
    workspace_root: Path, bot_dir: str, prefixes: list[tuple[str, str]], old_home: str = "",
    *, dry_run: bool = False,
) -> Result:
    rules = compile_rules(prefixes, old_home)
    home = old_home.rstrip("/")
    result = Result()
    for path in candidate_files(workspace_root, bot_dir):
        try:
            text = _read(path)
        except (UnicodeDecodeError, OSError):
            continue
        new, count = rewrite_text(text, rules)
        if count:
            result.changed.append((path, count))
            if not dry_run:
                backup = path.with_name(path.name + BACKUP_SUFFIX)
                if not backup.exists():
                    _write(backup, text, exclusive=True)
                _write(path, new)
        if home:
            for number, line in enumerate(new.splitlines(), start=1):
                if re.search(re.escape(home) + _PATH_END, line):
                    result.leftovers.append((path, number, line.strip()[:160]))
    return result


def report(result: Result, workspace_root: Path, *, dry_run: bool, out=print) -> None:
    verb = "would rewrite" if dry_run else "rewrote"
    for path, count in result.changed:
        out(f"  {verb} {path.relative_to(workspace_root)}: {count} path(s)")
    for path, number, line in result.leftovers:
        out(f"  still names the old host: {path.relative_to(workspace_root)}:{number}: {line}")
    out(f"workspace paths: {verb} {len(result.changed)} file(s)"
        f"{' (dry run, nothing written)' if dry_run else ''}; {len(result.leftovers)} line(s) left for a person")


def _parse_map(values: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        old, sep, new = value.partition("=")
        if not sep or not old.startswith("/") or not new.startswith("/"):
            raise SystemExit(f"--map takes OLD=NEW, both absolute paths: {value!r}")
        pairs.append((old, new))
    return pairs


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="istota migrate workspace-paths",
        description="Rewrite the old host's paths in each user's bot directory, "
                    "keeping a .pre-migration copy of every file it changes.",
    )
    parser.add_argument("-c", "--config", help="config.toml to load (default: the usual search)")
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                        help="a prefix to rewrite, in place of or beside the import record's")
    parser.add_argument("--old-home", default=None,
                        help="the old install's home, whose venv binaries become bare commands")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from istota.config import load_config
    from istota.maintenance import bare_metal_import

    config = load_config(Path(args.config) if args.config else None)
    record = bare_metal_import._read_record(Path(config.db_path)) or {}
    prefixes = [tuple(pair) for pair in record.get("prefixes") or []] + _parse_map(args.map)
    old_home = args.old_home if args.old_home is not None else record.get("istota_home", "")
    if not prefixes and not old_home:
        print("no import record with a prefix map, and no --map: nothing to rewrite", file=sys.stderr)
        return 1
    root = Path(config.nextcloud_mount_path or config.workspace_path)
    result = run(root, config.bot_dir_name, prefixes, old_home, dry_run=args.dry_run)
    report(result, root, dry_run=args.dry_run)
    return 0
