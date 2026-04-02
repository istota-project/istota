#!/usr/bin/env python3
"""One-time migration: JSON data files -> KV store.

Usage:
    uv run python scripts/migrate_json_to_kv.py [--user USER] [--dry-run]

Reads JSON files from data/ and writes their contents to the KV store.
After verification, renames originals to .json.bak.
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from istota import db
from istota.config import load_config

MIGRATIONS = [
    ("data/corpus_history.json", "corpus", "history"),
    ("data/resurface_history.json", "resurface", "history"),
    ("data/stefan_location_state.json", "location", "state"),
    ("data/warsaw_apartments_seen.json", "warsaw", "seen_apartments"),
]


def migrate(user_id: str, dry_run: bool = False):
    config = load_config()
    project_root = Path(__file__).resolve().parent.parent
    migrated = 0
    skipped = 0

    for rel_path, namespace, key in MIGRATIONS:
        path = project_root / rel_path
        if not path.exists():
            print(f"  SKIP {rel_path} (not found)")
            skipped += 1
            continue

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ERROR {rel_path}: {e}")
            continue

        value = json.dumps(data)
        print(f"  {rel_path} -> kv:{namespace}/{key} ({len(value)} bytes)")

        if dry_run:
            print(f"    (dry run, skipping write)")
            continue

        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, user_id, namespace, key, value)

        # Verify
        with db.get_db(config.db_path) as conn:
            result = db.kv_get(conn, user_id, namespace, key)
        if result is None:
            print(f"    VERIFY FAILED — value not found after write")
            continue

        stored = json.loads(result["value"])
        if stored != data:
            print(f"    VERIFY FAILED — stored value differs")
            continue

        print(f"    verified OK")

        # Rename original
        bak = path.with_suffix(".json.bak")
        path.rename(bak)
        print(f"    renamed to {bak.name}")
        migrated += 1

    print(f"\nDone: {migrated} migrated, {skipped} skipped")


def main():
    parser = argparse.ArgumentParser(description="Migrate JSON data files to KV store")
    parser.add_argument("--user", default="stefan", help="User ID (default: stefan)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    print(f"Migrating JSON files to KV store for user '{args.user}'")
    if args.dry_run:
        print("(dry run mode)\n")
    else:
        print()

    migrate(args.user, args.dry_run)


if __name__ == "__main__":
    main()
