"""Post-template-render validation for istota config.toml.

Exits non-zero (with a human-readable error on stderr) when:
1. The TOML doesn't parse.
2. Top-level config keys leaked under the `[brain]` table — the
   ISSUE-058 failure mode where inserting a `[table]` header above
   existing root keys silently captures them under the table.
3. The fields the scheduler actually depends on
   (`db_path`, `temp_dir`) don't resolve to the values the operator
   passed in. Catches the same nesting bug from a different angle: when
   keys leak under a table, the dataclass defaults silently win and the
   scheduler comes up against `data/istota.db` rather than the deployed
   path.

Usage:
  validate_config.py CONFIG_PATH PACKAGE EXPECTED_DB_PATH EXPECTED_TEMP_DIR

Run via Ansible's `script` module against the deployed config; gate the
scheduler restart handler on this passing.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: validate_config.py CONFIG_PATH PACKAGE EXPECTED_DB_PATH EXPECTED_TEMP_DIR",
            file=sys.stderr,
        )
        return 2

    cfg_path_str, package, expected_db, expected_tmp = sys.argv[1:5]
    cfg_path = Path(cfg_path_str)

    try:
        import tomli
    except ImportError:
        print("validate_config: tomli not available in venv", file=sys.stderr)
        return 2

    try:
        with cfg_path.open("rb") as f:
            raw = tomli.load(f)
    except FileNotFoundError:
        print(f"validate_config: {cfg_path} does not exist", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"validate_config: TOML parse error in {cfg_path}: {e}", file=sys.stderr)
        return 1

    # Allowlist for the [brain] table. Update when BrainConfig grows
    # legitimate fields (see .claude/rules/brain.md).
    brain_allowlist = {"kind"}
    brain = raw.get("brain", {})
    leaked = sorted(k for k in brain if k not in brain_allowlist)
    if leaked:
        print(
            "validate_config: keys leaked under [brain] table: "
            + ", ".join(leaked)
            + " — likely a [table] header in config.toml.j2 above root keys",
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(cfg_path.parent.parent / "src"))
    try:
        mod = __import__(f"{package}.config", fromlist=["load_config"])
        load_config = mod.load_config
    except Exception as e:
        print(f"validate_config: cannot import {package}.config: {e}", file=sys.stderr)
        return 2

    try:
        c = load_config(cfg_path)
    except Exception as e:
        print(f"validate_config: load_config raised: {e}", file=sys.stderr)
        return 1

    actual_db = str(c.db_path)
    if actual_db != expected_db:
        print(
            f"validate_config: db_path={actual_db!r} expected={expected_db!r} "
            "(field likely fell back to dataclass default — keys nested under wrong table)",
            file=sys.stderr,
        )
        return 1

    actual_tmp = str(c.temp_dir)
    if actual_tmp != expected_tmp:
        print(
            f"validate_config: temp_dir={actual_tmp!r} expected={expected_tmp!r}",
            file=sys.stderr,
        )
        return 1

    print(f"validate_config: ok ({cfg_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
