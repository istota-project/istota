"""Complete settings from read transactions, verified again before publication."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
import tomllib

from istota import apply, storage, user_briefings, user_profiles
from istota.briefings import _migrate, db as bdb
from istota.config import UserConfig, _apply_user_briefings
from istota.config_editor import secrets as config_secrets
from istota.config_mapper import collect_problems
from istota.lib import toml_write
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan import briefings, files, profiles, resources, secrets, shared_briefings
from istota.settings_plan.engine import Context, ExecutionError, state_digest
from istota.settings_plan.paths import patch_document
from istota.settings_plan.schema import SchemaError, parse_plan


def _refuse_credentials(value, path):
    def refuse(leaf_path, leaf):
        if leaf not in ("", None):
            raise ExecutionError("embedded_briefing_credential", [*path, *leaf_path])
        return leaf

    config_secrets._walk(value, (), refuse)


def _blocks(config, user, schedules):
    database = config.module_db_path(user, "briefings")
    definitions, migrated = {}, set()
    try:
        try:
            database.stat()
        except FileNotFoundError:
            pass
        else:
            with closing(connect_read_only(database)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                for name in bdb.list_briefing_names(conn):
                    definitions[name] = briefings.snapshot_blocks(conn, name, user)
                migrated = {item["name"] for item in schedules
                            if bdb.meta_get(conn, _migrate._briefing_sentinel(item["name"])) == "1"}
    except (OSError, sqlite3.Error):
        raise ExecutionError("briefing_store_unavailable", ["users", user, "briefings"]) from None
    if definitions.keys() - {item["name"] for item in schedules}:
        raise ExecutionError("orphan_briefing_definitions", ["users", user, "briefings"])
    for index, schedule in enumerate(schedules):
        blocks = definitions.get(schedule["name"], [])
        for block_index, block in enumerate(blocks):
            path = ["users", user, "briefings", index, "blocks", block_index]
            _refuse_credentials(block["options"], [*path, "options"])
            for source_index, source in enumerate(block["sources"]):
                _refuse_credentials(source["config"], [*path, "sources", source_index, "config"])
        schedule["blocks"] = blocks
    for seed in config.users[user].briefings:
        if not definitions.get(seed.name) and seed.name not in migrated and _migrate.block_specs(seed):
            raise ExecutionError("briefing_initialization_required", ["users", user, "briefings"])


def _gather(config_path, selected):
    raw, refusal = storage.read_regular_file(config_path)
    if refusal or not raw:
        raise ExecutionError("config_unavailable", ["config"])
    try:
        document = tomllib.loads(raw)
        with collect_problems() as problems:
            config = profiles._load_config(parse_plan({"config": document}), Context(config_path))
        if any(problem["kind"] == "discarded" for problem in problems):
            raise ExecutionError("current_config_invalid", ["config"])
    except (ValueError, apply.PlanError):
        raise ExecutionError("current_config_invalid", ["config"]) from None
    result = {"meta": {"version": 1, "generated_by": "snapshot", "source": "snapshot"}}
    warnings, revisions = [], {}
    if not selected:
        result["config"] = document
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            identities = {row[0] for row in conn.execute("SELECT user_id FROM user_profiles")}
            wanted = sorted(set(selected) if selected else identities)
            legacy = (set(config.users) - identities) & set(selected or config.users)
            if legacy:
                raise ExecutionError("user_initialization_required_run_istota_init", ["users", sorted(legacy)[0]])
            for user in wanted:
                if user not in identities:
                    raise ExecutionError("unknown_user", ["users", user])
            try:
                stored = user_profiles.list_profiles(config.db_path, conn=conn, users=wanted, strict=True)
            except (ValueError, TypeError):
                raise ExecutionError("profile_store_invalid", ["users"]) from None
            # Validate IDs before module or workspace path construction.
            parse_plan({"users": {user: {} for user in wanted}})
            for user in wanted:
                user_profiles.merge_into_user_config(stored[user], config.users.setdefault(user, UserConfig()))
            rows = user_briefings.list_briefings(None, conn=conn)
            _apply_user_briefings(config, rows=rows)
            if not selected:
                shared = shared_briefings.snapshot_shared_blocks(conn)
                shared.sort(key=lambda item: item["name"])
                for index, definition in enumerate(shared):
                    for source_index, source in enumerate(definition["sources"]):
                        _refuse_credentials(source["config"],
                                            ["shared_briefing_blocks", index, "sources", source_index, "config"])
                result["shared_briefing_blocks"] = shared
            result["users"] = {}
            for user in wanted:
                definition = profiles.snapshot_profile(stored[user])
                definition["resources"] = resources.snapshot_resources(conn, user)
                schedules = briefings.snapshot_schedules(conn, user)
                briefings._check_schedule_seeds(config, user, {item["name"] for item in schedules})
                _blocks(config, user, schedules)
                definition["briefings"] = schedules
                jobs, cron_revision = files.snapshot_cron(conn, config, user)
                heartbeat, heartbeat_revision = files.snapshot_heartbeat(config, user)
                definition["scheduled_jobs"] = jobs
                definition["heartbeat"] = heartbeat
                definition["secrets"] = sorted(secrets.snapshot_secrets(conn, user, warnings),
                                                key=lambda item: item["service"])
                result["users"][user] = definition
                revisions[user] = [cron_revision, heartbeat_revision]
                if selected and any(source["kind"] == "shared_block" for schedule in schedules
                                    for block in schedule["blocks"] for source in block["sources"]):
                    warnings.append(f"users.{user}.briefings: shared references require definitions at the destination")
    except (OSError, sqlite3.Error):
        raise ExecutionError("framework_store_unavailable_run_istota_init", ["users"]) from None
    parse_plan(result)
    digest = state_digest(json.dumps([result, revisions, raw], ensure_ascii=False).encode())
    if "config" in result:
        result["config"] = config_secrets._walk(document, (),
                                                 lambda path, value: config_secrets.SENTINEL if value != "" else "")
    return result, warnings, digest


def gather(config_path: Path, *, users=None):
    """Retry the complete gather once; each comparison opens fresh transactions."""
    for _ in range(2):
        document, warnings, expected = _gather(config_path, users)
        _, _, current = _gather(config_path, users)
        if current == expected:
            return document, warnings
    raise ExecutionError("state_changed")


def run(config_path: Path, *, output=None, output_format="toml", users=None, no_meta=False,
        patches=None, out=print, err=print):
    try:
        document, warnings = gather(config_path, users=users)
        document, patch_warnings = patch_document(document, patches or [])
        warnings.extend(patch_warnings)
        parse_plan(document)
        if no_meta:
            document.pop("meta")
        # Insertion order retains operator-authored mappings and positional lists.
        text = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                if output_format == "json" else toml_write.dumps(document))
        if output and output != "-":
            destination = Path(output)
            try:
                mode = destination.lstat().st_mode
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(mode):
                    raise ExecutionError("output_requires_regular_file", ["output"])
            apply.write_private(destination, text)
        else:
            out(text.rstrip("\n"))
        for warning in warnings:
            err("istota snapshot: " + warning)
        return 0
    except ExecutionError as exc:
        suffix = " at " + config_secrets.label(exc.path) if exc.path else ""
        remedy = ("; remove the embedded credential or use the service credential store"
                  if exc.code == "embedded_briefing_credential" else "")
        if "run_istota_init" in exc.code:
            remedy = "; run istota init first"
        err(f"istota snapshot: {exc.code}{suffix}{remedy}; no output written.")
    except SchemaError as exc:
        err("istota snapshot: invalid_settings at " + config_secrets.label(exc.problem["path"]) + "; no output written.")
    except (OSError, ValueError, TypeError, apply.PlanError):
        err("istota snapshot: read_or_output_failed; no output written.")
    return 1
