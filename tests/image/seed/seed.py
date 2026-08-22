"""Place a legacy on-mount module database, the way an old release left one.

Run inside the container by `tests/image/test_upgrade.py`, with
`ISTOTA_CONFIG_PATH` pointing at the captured release config. Prints the path it
wrote, as the last line of stdout.

The location comes from `db_relocate.legacy_db_path`, not from a path spelled
out in the test. The migrator is the thing under test, and a source placed by a
second, independent idea of where legacy databases live would drift from it —
at which point `relocate_module` returns `no_source`, the migrator does nothing,
and the test that was meant to prove relocation works passes because there was
nothing to relocate.

The schema is deliberately not the feeds schema. `_perform_migration` copies the
file and then runs the module's own init over the copy, so the source only has
to be a valid SQLite database holding rows that `_data_row_count` will see —
and using a made-up table makes it unambiguous that the rows came from here.
"""

import sqlite3
import sys

from istota.config import load_config
from istota.db_relocate import legacy_db_path

MODULE = "feeds"


def main() -> int:
    config = load_config()
    users = list(getattr(config, "users", {}) or {})
    if not users:
        print("the captured config declares no users", file=sys.stderr)
        return 1
    user = users[0]

    path = legacy_db_path(config, user, MODULE)
    if path is None:
        print(
            "legacy_db_path returned None: the captured config has no "
            "nextcloud_mount_path, so there is no legacy location to seed",
            file=sys.stderr,
        )
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seeded_before_relocation (note TEXT)")
        conn.execute(
            "INSERT INTO seeded_before_relocation (note) VALUES (?)",
            ("written to the mount by a release that predates db_relocate",),
        )

    print(f"user={user}", file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
