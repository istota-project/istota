#!/usr/bin/env python3
"""Migrate a user's legacy money TOML configs into UPPERCASE.md format.

Reads ``invoicing.toml`` / ``tax.toml`` / ``monarch.toml`` from a source
directory and writes ``INVOICING.md`` / ``TAX.md`` / ``MONARCH.md`` into a
destination directory (typically the user's istota workspace ``config/``
subdir). Each output file embeds the original TOML verbatim inside a fenced
code block, preceded by a brief preamble.

Idempotent: running again on already-migrated files re-extracts the TOML
block, formats it the same way, and rewrites the file. Source TOML files
are not deleted — that is left to the operator.

Usage:
    migrate_money_workspace_config.py \\
        --source /path/to/old/configs \\
        --dest   /nextcloud/Users/alice/.istota/config

Pass --dry-run to print what would be written without touching disk.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


_PREAMBLES = {
    "INVOICING.md": (
        "# Invoicing config\n\n"
        "Companies, clients, services, rates, and invoice schedule. "
        "Edit the TOML block below; comments are preserved.\n"
    ),
    "TAX.md": (
        "# Tax config\n\n"
        "Filing status, prior-year tax, W-2 defaults, and any rate "
        "overrides. The Money web UI persists user-edited inputs separately.\n"
    ),
    "MONARCH.md": (
        "# Monarch Money sync config\n\n"
        "Account mapping, category overrides, and tag filters for the daily "
        "Monarch sync. Credentials live in the istota secrets file, not here.\n"
    ),
}


_PAIRS = (
    ("invoicing.toml", "INVOICING.md"),
    ("tax.toml", "TAX.md"),
    ("monarch.toml", "MONARCH.md"),
)


def _extract_toml(text: str) -> str:
    """If text already contains a fenced toml block, return its body; else
    return text as-is (assumed plain TOML)."""
    match = _TOML_BLOCK_RE.search(text)
    if match:
        return match.group(1).rstrip() + "\n"
    return text.rstrip() + "\n"


def _render(filename: str, toml_body: str) -> str:
    preamble = _PREAMBLES[filename]
    return f"{preamble}\n```toml\n{toml_body}```\n"


def migrate(source: Path, dest: Path, dry_run: bool = False) -> int:
    """Migrate all known *.toml files from source into UPPERCASE.md in dest.

    Returns the number of files written.
    """
    if not source.is_dir():
        print(f"error: source dir does not exist: {source}", file=sys.stderr)
        return 0
    if not dest.exists():
        if dry_run:
            print(f"[dry-run] would mkdir -p {dest}")
        else:
            dest.mkdir(parents=True, exist_ok=True)

    written = 0
    for src_name, dest_name in _PAIRS:
        src_path = source / src_name
        if not src_path.exists():
            print(f"skip: {src_path} (not present)")
            continue
        toml_body = _extract_toml(src_path.read_text())
        rendered = _render(dest_name, toml_body)
        dest_path = dest / dest_name
        if dry_run:
            print(f"[dry-run] would write {dest_path} ({len(rendered)} bytes)")
        else:
            dest_path.write_text(rendered)
            print(f"wrote {dest_path}")
            written += 1
    return written


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, type=Path, help="Directory containing legacy *.toml")
    p.add_argument("--dest", required=True, type=Path, help="Destination directory for *.md files")
    p.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    n = migrate(args.source, args.dest, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"\nMigrated {n} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
