"""One-time import of a bare-metal install into the container layout.

    istota migrate import-bare-metal --source DIR --ingress MODE [--dry-run | --list]
                                     [--rehearsal] [--emit-plan PLAN.json]

The source is what ``host/export-bare-metal.sh`` writes, unpacked: ``MANIFEST``,
``config/config.toml`` (the role's render) and ``config/admins``,
``secrets/secrets.env`` and ``secrets/web-secrets.env``, a ``db_backup`` snapshot
tree under ``db/``, and optionally ``repos/``, ``whatsapp-baileys-session/`` and
``logs/``. It runs inside the new image as uid 10001, in a one-shot container
with the config directory and the stack's ``secrets/`` writable, before the
stack's first start.

What it does, in order, stopping at the first failure:

1. Rewrites the config. Every state path the role rendered is set to the value
   ``istota setup`` writes for the container (``setup_wizard``'s constants are
   the one table), keys the container derives are dropped so they derive, keys
   this shape removed are dropped, and each change is reported. Everything else
   stays as the operator had it. Written through ``istota apply``'s planner, so
   it is validated by ``load_config`` and printed as a diff first.
2. Splits the two env files into the stack's secrets files (named by
   ``setup_wizard.SECRET_NAMES``), plus the master key and the web token key,
   which are carried byte for byte into ``/data``: every stored credential and
   every stored web token is encrypted under them. A name with no file in this
   shape is a refusal, never a drop.
3. Restores the databases with ``db_restore`` and runs ``quick_check`` on the
   framework database before and after.
4. Copies ``repos/`` and repairs the worktrees with ``repos_relocate``'s repair,
   since git records absolute paths in both directions.
5. Copies the Baileys session and the session transcripts, when present.
6. In one transaction: rebases the persisted absolute paths measured on the
   source (``tasks.attachments``, ``memory_chunks.source_id`` and
   ``metadata_json``, the ``note_resurface_*`` tables a user script keeps, the
   ``corpus``/``resurface`` history in ``istota_kv``), by prefix with a path
   boundary and inside JSON as decoded values, and writes the ``_migration``
   record. Columns that are history, or that a file sync rewrites, are scanned
   and reported, not rewritten.

Exit codes are ``repos_relocate``'s: 0 done (or already imported), 1 refused
with nothing touched, 2 partial. A partial leaves
``/data/.bare-metal-import.in-progress`` naming the export, and a re-run of the
same export resumes; the record is written last, so a second run after success
is a no-op.

``--rehearsal`` is for a copy that must not act as a second live bot. Every
scheduler loop and delivery path that can reach a person or another service is
switched off through a switch the daemon already reads, in the written config
(``REHEARSAL_DISABLED``, ``REHEARSAL_CONFIG``, the config-seeded briefings and
shared blocks) or in the imported database (``_rehearse_database``: scheduled
jobs, briefings, shared blocks, ntfy credentials, the old host's queue), every
transport credential is withheld, and the record says it was a rehearsal. The
full list, with what each switch stops, is in
``docs/deployment/migrating-from-bare-metal.md``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from istota import apply as apply_module
from istota import setup_wizard as wizard
from istota.lib import sqlite_util, toml_write

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PARTIAL = 2

MIGRATION_NAMESPACE = "_migration"
RECORD_KEY = "bare_metal_import"
PROGRESS_NAME = ".bare-metal-import.in-progress"
MANIFEST_FORMAT = "1"

INGRESS_MODES = tuple(wizard.INGRESS_HOPS)

#: Where the master key and the web token key live in the container: the
#: entrypoint reads both from the state volume.
KEY_FILES = {
    "ISTOTA_SECRET_KEY": ".secret_key",
    "ISTOTA_WEB_TOKEN_KEY": ".web_token_key",
}

#: Credentials a rehearsal never receives: each reaches a production service a
#: rehearsal must not touch. Matched by prefix on the variable name.
REHEARSAL_WITHHELD_PREFIXES = (
    "ISTOTA_NEXTCLOUD_",
    "ISTOTA_EMAIL_",
    "ISTOTA_SMS_",
    "ISTOTA_WHATSAPP_",
    "ISTOTA_CALDAV_",
    "ISTOTA_GOOGLE_WORKSPACE_",
    "ISTOTA_WEB_OAUTH2_",
)

REHEARSAL_DISABLED = ("talk", "email", "sms", "whatsapp", "sleep_cycle", "channel_sleep_cycle")

#: The pollers and senders with no ``enabled`` of their own, set to the value
#: that turns each off. An interval of 0 is off for these gates
#: (``scheduler.build_interval_gates``), not every tick.
REHEARSAL_CONFIG = (
    # Each user's HEARTBEAT.md: shell commands, URL probes, alerts.
    (("scheduler", "heartbeat_check_interval"), 0),
    # TASKS.md files and the briefing trigger files, which queue tasks.
    (("scheduler", "tasks_file_poll_interval"), 0),
    # Accepts and moves files shared with the bot on the Nextcloud.
    (("scheduler", "shared_file_check_interval"), 0),
    # The ntfy push when a long task finishes.
    (("scheduler", "push_notification_sources"), []),
    # Fetches every repository's remote before reaping a worktree.
    (("developer", "worktree_reap_enabled"), False),
    # Fetches avatars from the Nextcloud.
    (("web", "avatar_import_from_nextcloud"), False),
)

#: Task states the old host's daemon would still act on. A rehearsal cancels
#: them: they were the production queue at the moment of the online export.
LIVE_TASK_STATUSES = ("pending", "locked", "running", "pending_confirmation")

#: Keys the container leaves to their default or derivation, as `istota setup`
#: does, or that this shape removed. Dropped when present, and reported.
DROPPED_KEYS = (
    # The role's install name; nothing reads it (removed in Stage 9, S13).
    ("namespace",),
    ("module_data_dir",),
    ("skills_dir",),
    ("logging", "file"),
    ("scheduler", "host_pressure_docker_socket"),
    ("security", "allow_unsandboxed_multi_user_vaults"),
    ("devbox", "docker_cli"),
)

#: Dropped only when set: an empty value already derives.
DROPPED_WHEN_SET = (
    ("brain", "native", "session_log", "dir"),
    ("whatsapp", "baileys", "session_dir"),
    ("whatsapp", "baileys", "pairing_relay_path"),
    ("whatsapp", "baileys", "sidecar_command"),
    ("browser", "vnc_url"),
)

#: Rebased by prefix when set; dropped and reported when no prefix covers them.
REBASED_KEYS = (
    ("scheduler", "db_backup_dir"),
    ("security", "sandbox_cache_dir"),
)

#: Columns holding a path or JSON naming paths, measured on the source.
REBASE_COLUMNS = (
    ("tasks", "attachments", "json"),
    ("memory_chunks", "source_id", "path"),
    ("memory_chunks", "metadata_json", "json"),
    ("note_resurface_configs", "notes_path", "path"),
    ("note_resurface_history", "note_path", "path"),
)
REBASE_KV = (("corpus", "history"), ("resurface", "history"))

#: Scanned and reported, never rewritten: workspace-relative by design, or
#: rewritten from the user's own files by a sync.
REPORT_COLUMNS = (
    ("user_resources", "resource_path"),
    ("messages", "attachment_paths"),
    ("istota_file_tasks", "file_path"),
    ("scheduled_jobs", "command"),
    ("scheduled_jobs", "prompt"),
)


class Refusal(Exception):
    """Nothing was touched. The message names what an operator has to decide."""


# ---------------------------------------------------------------------------
# The config rewrite
# ---------------------------------------------------------------------------


@dataclass
class ConfigRewrite:
    document: dict
    changes: list[str] = field(default_factory=list)
    prefixes: list[tuple[str, str]] = field(default_factory=list)


def _get(doc: dict, path: tuple[str, ...]):
    node = doc
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _pop(doc: dict, path: tuple[str, ...]):
    node = doc
    for part in path[:-1]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict):
        return None
    return node.pop(path[-1], None)


def _set(doc: dict, path: tuple[str, ...], value) -> None:
    node = doc
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _parent(path: str) -> str:
    return str(Path(path).parent)


def path_prefixes(old: dict, new: dict) -> list[tuple[str, str]]:
    """Old state roots and where each is now, longest first."""
    pairs: list[tuple[str, str]] = []

    def add(old_value, new_value) -> None:
        if isinstance(old_value, str) and old_value.startswith("/") and old_value != new_value:
            pairs.append((old_value.rstrip("/") or "/", new_value))

    add(old.get("nextcloud_mount_path") or old.get("workspace_path"), new["workspace_path"])
    if old.get("workspace_path") != old.get("nextcloud_mount_path"):
        add(old.get("workspace_path"), new["workspace_path"])
    add(old.get("temp_dir"), new["temp_dir"])
    new_db_dir = _parent(new["db_path"])
    if isinstance(old.get("db_path"), str):
        add(_parent(old["db_path"]), new_db_dir)
    add(old.get("module_data_dir"), f"{new_db_dir}/modules")
    add(_get(old, ("developer", "repos_dir")) or None, wizard.CONTAINER_REPOS_DIR)
    add(_get(old, ("brain", "native", "session_log", "dir")) or None, f"{new_db_dir}/logs")
    seen: set[str] = set()
    unique = []
    for prefix, target in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if prefix not in seen:
            seen.add(prefix)
            unique.append((prefix, target))
    return unique


def rebase_path(value: str, prefixes: list[tuple[str, str]]) -> str | None:
    """``value`` under its new root, or None when no prefix covers it.

    A boundary match: the prefix itself or the prefix plus ``/``, never a
    sibling that merely starts with the same text.
    """
    for prefix, target in prefixes:
        if value == prefix:
            return target
        if value.startswith(prefix + "/"):
            return target + value[len(prefix):]
    return None


def rebase_json(value, prefixes: list[tuple[str, str]]):
    """``(rebased, count)`` over every string in a decoded JSON value."""
    if isinstance(value, str):
        new = rebase_path(value, prefixes)
        return (value, 0) if new is None else (new, 1)
    if isinstance(value, list):
        out, total = [], 0
        for item in value:
            new, count = rebase_json(item, prefixes)
            out.append(new)
            total += count
        return out, total
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            new, count = rebase_json(item, prefixes)
            out[key] = new
            total += count
        return out, total
    return value, 0


def _strings(value, where: str):
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{where}.{key}" if where else key)


def rewrite_config(
    old: dict, *, ingress: str, rehearsal: bool = False, old_home: str = "",
) -> ConfigRewrite:
    """The role's config, in the container layout. Pure."""
    if ingress not in wizard.INGRESS_HOPS:
        raise Refusal(f"--ingress must be one of {', '.join(INGRESS_MODES)}")
    new = copy.deepcopy(old)
    changes: list[str] = []
    uses_nextcloud = bool(_get(old, ("nextcloud", "url")))

    def assign(path: tuple[str, ...], value) -> None:
        before = _get(new, path)
        if before != value:
            _set(new, path, value)
            changes.append(f"{'.'.join(path)}: {before!r} -> {value!r}")

    def drop(path: tuple[str, ...]) -> None:
        if _get(new, path) is not None:
            changes.append(f"{'.'.join(path)}: dropped ({_pop(new, path)!r})")

    assign(("db_path",), wizard.CONTAINER_DB_PATH)
    assign(("temp_dir",), wizard.CONTAINER_TEMP_DIR)
    workspace = (
        wizard.CONTAINER_NEXTCLOUD_WORKSPACE if uses_nextcloud else wizard.CONTAINER_LOCAL_WORKSPACE
    )
    assign(("workspace_path",), workspace)
    if uses_nextcloud:
        assign(("nextcloud_mount_path",), wizard.CONTAINER_NEXTCLOUD_WORKSPACE)
    else:
        drop(("nextcloud_mount_path",))
        changes.append(
            f"workspace: local storage; copy the old workspace into {workspace} by hand"
        )

    for path in DROPPED_KEYS:
        drop(path)
    for path in DROPPED_WHEN_SET:
        if _get(new, path):
            drop(path)
    if _get(old, ("logging", "file")) and _get(new, ("logging", "output")) in ("file", "both"):
        assign(("logging", "output"), "console")

    developer = new.get("developer")
    if isinstance(developer, dict):
        if developer.get("repos_dir"):
            assign(("developer", "repos_dir"), wizard.CONTAINER_REPOS_DIR)
        for name in ("gh", "glab"):
            if f"{name}_bin_path" in developer:
                assign(("developer", f"{name}_bin_path"), f"{wizard.CONTAINER_FORGE_DIR}/{name}")
        if "devbox_proxy_socket_dir" in developer:
            assign(("developer", "devbox_proxy_socket_dir"), wizard.CONTAINER_DEVBOX_CRED_DIR)
        if _get(new, ("developer", "container", "exec_socket_dir")) is not None:
            assign(("developer", "container", "exec_socket_dir"), wizard.CONTAINER_DEVBOX_EXEC_DIR)
    if isinstance(new.get("browser"), dict) and "api_url" in new["browser"]:
        assign(("browser", "api_url"), wizard.CONTAINER_BROWSER_API_URL)
    assign(("web", "trusted_proxy_hops"), wizard.INGRESS_HOPS[ingress])

    prefixes = path_prefixes(old, new)
    for path in REBASED_KEYS:
        value = _get(new, path)
        if not value:
            continue
        rebased = rebase_path(value, prefixes)
        if rebased is None:
            drop(path)
        else:
            assign(path, rebased)

    if rehearsal:
        for section in REHEARSAL_DISABLED:
            assign((section, "enabled"), False)
        for path, value in REHEARSAL_CONFIG:
            assign(path, value)
        # Seeded into every user without a briefing of the same name, at each
        # start, and into `shared_block_configs` when a name has no row yet:
        # the database switches below would not reach either.
        if new.get("default_briefings"):
            changes.append(f"default_briefings: dropped ({len(new.pop('default_briefings'))} briefing(s))")
        for block in new.get("briefing_shared_blocks") or []:
            if isinstance(block, dict) and block.get("enabled", True) is not False:
                block["enabled"] = False
                changes.append(f"briefing_shared_blocks {block.get('name', '?')!r}: enabled -> False")

    scan = [prefix for prefix, _ in prefixes]
    if old_home:
        scan.append(old_home.rstrip("/"))
    for where, value in _strings(new, ""):
        if any(value == p or value.startswith(p + "/") for p in scan):
            changes.append(f"{where}: still names a host path ({value}); left as is")

    return ConfigRewrite(document=new, changes=changes, prefixes=prefixes)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def parse_env_file(text: str, name: str) -> dict[str, str]:
    """``KEY=value`` lines as systemd's ``EnvironmentFile=`` reads them."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise Refusal(f"{name} line {number} is not KEY=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass
class SecretsPlan:
    files: dict[str, str] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    withheld: list[str] = field(default_factory=list)


def map_secrets(env: dict[str, str], *, rehearsal: bool) -> SecretsPlan:
    """Each variable to the file the new shape reads it from. Refuses an unmapped one."""
    by_variable = {name.upper(): name for name in wizard.SECRET_NAMES}
    plan = SecretsPlan()
    unmapped = []
    for variable, value in env.items():
        if rehearsal and variable.startswith(REHEARSAL_WITHHELD_PREFIXES):
            plan.withheld.append(variable)
        elif variable in KEY_FILES:
            plan.keys[KEY_FILES[variable]] = value
        elif variable in by_variable:
            plan.files[by_variable[variable]] = value
        else:
            unmapped.append(variable)
    if unmapped:
        raise Refusal(
            "no secrets file in this shape for " + ", ".join(sorted(unmapped))
            + ". Nothing was written. The stack's compose file declares the set "
            "(setup_wizard.SECRET_NAMES); a credential is never dropped silently."
        )
    if ".secret_key" not in plan.keys:
        raise Refusal("secrets.env has no ISTOTA_SECRET_KEY; every stored credential needs it")
    return plan


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


@dataclass
class Source:
    root: Path
    manifest: dict[str, str]
    config: dict
    admins: str
    env: dict[str, str]
    snapshot: Path
    snapshot_dbs: list[str]

    @property
    def export_id(self) -> str:
        return f"{self.manifest.get('source_host', '')} {self.manifest.get('exported_at', '')}"


def _read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"the export has no readable {what} ({path}: {exc.strerror or exc})") from None


def read_source(root: Path) -> Source:
    import tomllib

    manifest: dict[str, str] = {}
    for line in _read_text(root / "MANIFEST", "MANIFEST").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            manifest[key.strip()] = value.strip()
    if manifest.get("format") != MANIFEST_FORMAT:
        raise Refusal(f"MANIFEST format {manifest.get('format')!r}; this release reads {MANIFEST_FORMAT}")
    try:
        config = tomllib.loads(_read_text(root / "config" / "config.toml", "config/config.toml"))
    except tomllib.TOMLDecodeError as exc:
        raise Refusal(f"config/config.toml does not parse: {exc}") from None
    env: dict[str, str] = {}
    for name in ("secrets.env", "web-secrets.env"):
        path = root / "secrets" / name
        if path.exists():
            env.update(parse_env_file(_read_text(path, f"secrets/{name}"), name))
    from istota.maintenance.db_backup import _dated_dirs

    dated = _dated_dirs(root / "db")
    if not dated:
        raise Refusal("the export holds no database snapshot under db/<YYYY-MM-DD>/")
    snapshot = dated[0]
    dbs = sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*.db"))
    if "framework/istota.db" not in dbs:
        raise Refusal(f"{snapshot} has no framework/istota.db")
    admins_path = root / "config" / "admins"
    admins = admins_path.read_text(encoding="utf-8") if admins_path.exists() else ""
    return Source(root, manifest, config, admins, env, snapshot, dbs)


# ---------------------------------------------------------------------------
# The database half
# ---------------------------------------------------------------------------


@dataclass
class DbReport:
    rebased: dict[str, int] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    report_only: list[str] = field(default_factory=list)
    rehearsal: list[str] = field(default_factory=list)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _absolute_strings(value) -> list[str]:
    return [s for _, s in _strings(value, "") if s.startswith("/")]


def rebase_database(
    conn: sqlite3.Connection, prefixes: list[tuple[str, str]], *,
    write: bool, rehearsal: bool, scan: list[str],
) -> DbReport:
    """Rebase the measured columns; report the rest. Writes only when ``write``."""
    report = DbReport()

    def note_unmapped(where: str, values: list[str]) -> None:
        for value in values:
            if rebase_path(value, prefixes) is None:
                report.unmapped.append(f"{where}: {value}")

    for table, column, kind in REBASE_COLUMNS:
        if column not in _columns(conn, table):
            continue
        count = 0
        rows = conn.execute(
            f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
        ).fetchall()
        for rowid, raw in rows:
            if kind == "path":
                if not isinstance(raw, str) or not raw.startswith("/"):
                    continue
                new = rebase_path(raw, prefixes)
                if new is None:
                    note_unmapped(f"{table}.{column} row {rowid}", [raw])
                    continue
                encoded = new
            else:
                try:
                    decoded = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                rebased, changed = rebase_json(decoded, prefixes)
                note_unmapped(f"{table}.{column} row {rowid}", _absolute_strings(decoded))
                if not changed:
                    continue
                encoded = json.dumps(rebased)
            count += 1
            if write:
                conn.execute(f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', (encoded, rowid))
        report.rebased[f"{table}.{column}"] = count

    kv_count = 0
    for namespace, key in REBASE_KV:
        for user_id, raw in conn.execute(
            "SELECT user_id, value FROM istota_kv WHERE namespace = ? AND key = ?", (namespace, key),
        ).fetchall():
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                continue
            rebased, changed = rebase_json(decoded, prefixes)
            if not changed:
                continue
            kv_count += 1
            if write:
                conn.execute(
                    "UPDATE istota_kv SET value = ? WHERE user_id = ? AND namespace = ? AND key = ?",
                    (json.dumps(rebased), user_id, namespace, key),
                )
    report.rebased["istota_kv corpus/resurface history"] = kv_count

    def mentions(value) -> bool:
        return isinstance(value, str) and any(p in value for p in scan)

    for table, column in REPORT_COLUMNS:
        if column not in _columns(conn, table):
            continue
        label = "user_id, name" if table == "scheduled_jobs" else "rowid"
        rows = conn.execute(f'SELECT {label}, "{column}" FROM "{table}"').fetchall()
        hits = [row for row in rows if mentions(row[-1])]
        if hits:
            names = ", ".join(
                f"{row[0]}/{row[1]}" if table == "scheduled_jobs" else f"row {row[0]}" for row in hits
            )
            report.report_only.append(f"{table}.{column}: {len(hits)} name a host path ({names})")
    skip = set(REBASE_KV)
    for user_id, namespace, key, value in conn.execute(
        "SELECT user_id, namespace, key, value FROM istota_kv"
    ).fetchall():
        if (namespace, key) not in skip and mentions(value):
            report.report_only.append(f"istota_kv {namespace}/{key} ({user_id}) names a host path")

    if rehearsal:
        report.rehearsal = _rehearse_database(conn, write=write)
    return report


_MODULE_JOB = "name LIKE '\\_module.%' ESCAPE '\\'"

#: ``(table, column, what it switches off, count query, update)``, in the order
#: they are reported. A table or column the source's schema lacks is skipped.
_REHEARSAL_ROWS = (
    (
        "scheduled_jobs", "auto_disabled_at", "scheduled job(s) suspended",
        "SELECT count(*) FROM scheduled_jobs WHERE auto_disabled_at IS NULL",
        # The daemon's suspension: the CRON.md sync rewrites `enabled` from each
        # user's file every tick but never writes this column.
        "UPDATE scheduled_jobs SET auto_disabled_at = datetime('now') "
        "WHERE auto_disabled_at IS NULL",
    ),
    (
        "scheduled_jobs", "disabled_at", "module job(s) switched off",
        f"SELECT count(*) FROM scheduled_jobs WHERE {_MODULE_JOB} "
        "AND NOT (enabled = 0 AND disabled_at IS NOT NULL)",
        # A `_module.*` row is in nobody's CRON.md, and the module sync lifts the
        # daemon's suspension after an hour; it keeps a user's `!cron disable`.
        "UPDATE scheduled_jobs SET enabled = 0, "
        "disabled_at = COALESCE(disabled_at, datetime('now')) "
        f"WHERE {_MODULE_JOB} AND NOT (enabled = 0 AND disabled_at IS NOT NULL)",
    ),
    (
        "briefing_configs", "enabled", "briefing(s) disabled",
        "SELECT count(*) FROM briefing_configs WHERE enabled != 0",
        "UPDATE briefing_configs SET enabled = 0 WHERE enabled != 0",
    ),
    (
        "shared_block_configs", "enabled", "shared block(s) disabled",
        "SELECT count(*) FROM shared_block_configs WHERE enabled != 0",
        "UPDATE shared_block_configs SET enabled = 0 WHERE enabled != 0",
    ),
    (
        # Per-user ntfy topics and tokens: a push to somebody's phone, from
        # heartbeats, job output, alerts and finished tasks alike.
        "secrets", "service", "ntfy credential(s) removed",
        "SELECT count(*) FROM secrets WHERE service = 'ntfy'",
        "DELETE FROM secrets WHERE service = 'ntfy'",
    ),
    (
        "tasks", "status", "queued task(s) cancelled",
        "SELECT count(*) FROM tasks WHERE status IN "
        f"({', '.join(repr(s) for s in LIVE_TASK_STATUSES)})",
        "UPDATE tasks SET status = 'cancelled', updated_at = datetime('now') "
        f"WHERE status IN ({', '.join(repr(s) for s in LIVE_TASK_STATUSES)})",
    ),
)


def _rehearse_database(conn: sqlite3.Connection, *, write: bool) -> list[str]:
    """Switch off, in the imported copy, what would act on its own."""
    lines = []
    for table, column, what, count_sql, update_sql in _REHEARSAL_ROWS:
        if column not in _columns(conn, table):
            continue
        count = conn.execute(count_sql).fetchone()[0]
        if write and count:
            conn.execute(update_sql)
        lines.append(f"{count} {what}")
    return lines


def read_record(db_path: Path) -> dict | None:
    """The ``_migration`` record, or None when this install was never imported.

    Raises ``sqlite3.Error`` for a database it cannot read and ``ValueError``
    for a record that is not a JSON object. ``doctor`` reads it through here.
    """
    conn = sqlite_util.connect_read_only(db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shared_kv'"
        ).fetchone() is None:
            return None
        row = conn.execute(
            "SELECT value FROM shared_kv WHERE namespace = ? AND key = ?",
            (MIGRATION_NAMESPACE, RECORD_KEY),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    record = json.loads(row[0])
    if not isinstance(record, dict):
        raise ValueError("the record is not a JSON object")
    return record


def _read_record(db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    try:
        return read_record(db_path)
    except sqlite3.Error:
        return None


def _task_count(db_path: Path) -> int:
    conn = sqlite_util.connect_read_only(db_path)
    try:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _quick_check(path: Path) -> list[str]:
    from istota.maintenance.db_health import quick_check

    conn = sqlite_util.connect_read_only(path)
    try:
        return quick_check(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Target:
    config_path: Path
    secrets_dir: Path
    data_dir: Path

    def on_disk(self, container_path: str) -> Path:
        """A container path, under ``data_dir`` (``/data`` in production)."""
        relative = Path(container_path).relative_to(wizard.CONTAINER_DATA_DIR)
        return self.data_dir / relative

    @property
    def db_path(self) -> Path:
        return self.on_disk(wizard.CONTAINER_DB_PATH)

    @property
    def progress(self) -> Path:
        return self.data_dir / PROGRESS_NAME


def _restore_config(target: Target, source: Source):
    """A ``Config`` whose framework and module paths are the target's, and whose
    backup root is the export's snapshot tree, for ``db_restore``."""
    from istota.config import load_config

    document = {
        "db_path": str(target.db_path),
        "scheduler": {"db_backup_dir": str(source.snapshot.parent)},
    }
    with tempfile.TemporaryDirectory(prefix="istota-import-") as scratch:
        path = Path(scratch) / "config.toml"
        path.write_text(toml_write.dumps(document), encoding="utf-8")
        return load_config(path)


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy into a sibling and rename, so a crash never leaves half a tree at ``dst``."""
    staging = dst.with_name(f".{dst.name}.importing")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging, symlinks=True)
    os.rename(staging, dst)


def _copy_entries(src: Path, dst: Path, out) -> None:
    """Every top-level entry of ``src`` into ``dst``; one already there is left."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if target.exists() or target.is_symlink():
            out(f"  {target} exists; left as it is")
            continue
        if entry.is_dir() and not entry.is_symlink():
            _copy_tree(entry, target)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


def _plan_config(rewrite: ConfigRewrite, target: Target):
    try:
        return apply_module.plan_config(rewrite.document, target.config_path)
    except apply_module.PlanError as exc:
        raise Refusal(f"the rewritten config does not validate: {exc}") from None


def _print_rewrite(rewrite: ConfigRewrite, section, out) -> None:
    out("config:")
    for line in rewrite.changes:
        out(f"  {line}")
    for warning in section.warnings:
        out(f"  warning: {warning}")
    for line in section.diff:
        out(line)


def _print_secrets(plan: SecretsPlan, out) -> None:
    out("secrets:")
    for name in sorted(plan.files):
        out(f"  secrets/{name}")
    for name in sorted(plan.keys):
        out(f"  /data/{name} (carried byte for byte)")
    for variable in sorted(plan.withheld):
        out(f"  withheld for the rehearsal: {variable}")


def _print_db_report(report: DbReport, out) -> None:
    out("database:")
    for column, count in report.rebased.items():
        out(f"  {column}: {count} row(s) rebased")
    for line in report.unmapped:
        out(f"  under no mapped prefix, left as is: {line}")
    for line in report.report_only:
        out(f"  report only: {line}")
    for line in report.rehearsal:
        out(f"  rehearsal: {line}")


def _scan_prefixes(source: Source, rewrite: ConfigRewrite) -> list[str]:
    scan = [prefix for prefix, _ in rewrite.prefixes]
    home = source.manifest.get("istota_home", "").rstrip("/")
    if home:
        scan.append(home)
    return scan


def _print_listing(source: Source, target: Target, out) -> None:
    out(f"export: {source.root}")
    for key, value in source.manifest.items():
        out(f"  {key}: {value}")
    out(f"  snapshot: {source.snapshot.name}")
    for name in source.snapshot_dbs:
        out(f"    {name}")
    for name in ("repos", "whatsapp-baileys-session", "logs", "browser-profile"):
        out(f"  {name}/: {'present' if (source.root / name).is_dir() else 'absent'}")
    out(f"  secrets: {', '.join(sorted(source.env))}")
    record = _read_record(target.db_path)
    if record is not None:
        state = f"imported ({record.get('source_host')}, {record.get('imported_at')})"
    elif target.progress.exists():
        state = "a partial import is in progress"
    elif target.db_path.exists() and _task_count(target.db_path):
        state = "holds work; an import would refuse"
    else:
        state = "fresh"
    out(f"target: {target.data_dir} ({state})")


def _preflight(source: Source, target: Target) -> bool:
    """True when this run resumes a partial import of the same export."""
    resuming = False
    if target.progress.exists():
        recorded = target.progress.read_text(encoding="utf-8").strip()
        if recorded != source.export_id:
            raise Refusal(
                f"{target.progress} names another export ({recorded}); finish or "
                "remove that import first"
            )
        resuming = True
    if not resuming and target.db_path.exists() and _task_count(target.db_path):
        raise Refusal(
            f"{target.db_path} already holds tasks: this install has done work, and "
            "an import replaces its database and master key"
        )
    from istota.maintenance.db_restore import _daemon_running

    if _daemon_running():
        raise Refusal("the scheduler daemon is running; stop the stack first")
    problems = _quick_check(source.snapshot / "framework" / "istota.db")
    if problems:
        raise Refusal("the exported framework database fails quick_check: " + "; ".join(problems[:5]))
    return resuming


def _write_secrets(plan: SecretsPlan, target: Target) -> None:
    for name, value in plan.files.items():
        wizard._write_secret_file(target.secrets_dir / name, value)
    for name, value in plan.keys.items():
        apply_module.write_private(target.data_dir / name, value)


def _restore_databases(source: Source, target: Target, out) -> None:
    from istota.maintenance.db_restore import restore_database

    config = _restore_config(target, source)
    for name in source.snapshot_dbs:
        if name == "framework/istota.db":
            result = restore_database(config, label="framework", date=source.snapshot.name)
        else:
            user, module_file = name.split("/", 1)
            result = restore_database(
                config, user=user, module=Path(module_file).stem,
                date=source.snapshot.name, force=True,
            )
        if result["status"] != "restored":
            raise RuntimeError(f"{name}: {result['status']} {result.get('error', '')}".strip())
        out(f"  restored {name} -> {result['dest']}")
    problems = _quick_check(target.db_path)
    if problems:
        raise RuntimeError("the restored framework database fails quick_check: " + "; ".join(problems[:5]))


def _import_repos(source: Source, target: Target, out) -> None:
    from istota.maintenance.repos_relocate import repair_after_copy

    src = source.root / "repos"
    old_root = _get(source.config, ("developer", "repos_dir"))
    if not src.is_dir():
        return
    if not old_root:
        out("  repos/: the old config has no developer.repos_dir; not copied")
        return
    new_root = target.on_disk(wizard.CONTAINER_REPOS_DIR)
    _copy_entries(src, new_root, out)
    report = repair_after_copy(old_root, new_root)
    for worktree in report.repaired:
        out(f"  repaired {worktree}")
    for note in report.notes:
        out(f"  note: {note}")
    if report.unrepaired or report.failed:
        raise RuntimeError(
            "worktrees not repaired: " + ", ".join([*report.unrepaired, *report.failed])
        )


def _import_side_trees(source: Source, target: Target, out) -> None:
    db_dir = target.db_path.parent
    for name in ("whatsapp-baileys-session", "logs"):
        src = source.root / name
        if not src.is_dir():
            continue
        dst = db_dir / name
        if dst.exists():
            out(f"  {name}/: {dst} exists; left as it is")
            continue
        _copy_tree(src, dst)
        out(f"  copied {name}/ -> {dst}")
    if (source.root / "browser-profile").is_dir():
        out(
            "  browser-profile/: not placed; the browser service's volume is not "
            "mounted here (see the runbook)"
        )


def _record(source: Source, rehearsal: bool) -> str:
    return json.dumps({
        "source_host": source.manifest.get("source_host", ""),
        "source_release": source.manifest.get("source_release", ""),
        "exported_at": source.manifest.get("exported_at", ""),
        "export_mode": source.manifest.get("mode", ""),
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rehearsal": rehearsal,
    })


def _finish_database(source, target, rewrite, rehearsal: bool) -> DbReport:
    from istota import db

    conn = sqlite3.connect(target.db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        report = rebase_database(
            conn, rewrite.prefixes, write=True, rehearsal=rehearsal,
            scan=_scan_prefixes(source, rewrite),
        )
        db.shared_kv_set(conn, MIGRATION_NAMESPACE, RECORD_KEY, _record(source, rehearsal), "import-bare-metal")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return report


def run(args, out, err) -> int:
    source_root = Path(args.source)
    target = Target(Path(args.config), Path(args.secrets_dir), Path(args.data_dir))
    try:
        source = read_source(source_root)
        if args.list_only:
            _print_listing(source, target, out)
            return EXIT_OK
        record = _read_record(target.db_path)
        if record is not None and not args.emit_plan:
            out(
                f"already imported from {record.get('source_host')} at "
                f"{record.get('imported_at')}; nothing to do"
            )
            return EXIT_OK
        rewrite = rewrite_config(
            source.config, ingress=args.ingress, rehearsal=args.rehearsal,
            old_home=source.manifest.get("istota_home", ""),
        )
        secrets = map_secrets(source.env, rehearsal=args.rehearsal)
        if args.emit_plan:
            plan_text = json.dumps(
                {"meta": {"version": apply_module.PLAN_VERSION}, "config": rewrite.document},
                indent=2,
            ) + "\n"
            apply_module.write_private(Path(args.emit_plan), plan_text)
            out(f"plan: wrote {args.emit_plan}")
        if record is not None:
            out(f"already imported from {record.get('source_host')}; nothing else to do")
            return EXIT_OK
        section = _plan_config(rewrite, target)
        if args.dry_run:
            _print_rewrite(rewrite, section, out)
            _print_secrets(secrets, out)
            conn = sqlite_util.connect_read_only(source.snapshot / "framework" / "istota.db")
            try:
                report = rebase_database(
                    conn, rewrite.prefixes, write=False, rehearsal=args.rehearsal,
                    scan=_scan_prefixes(source, rewrite),
                )
            finally:
                conn.close()
            _print_db_report(report, out)
            out("--dry-run: nothing was written.")
            return EXIT_OK
        _preflight(source, target)
    except Refusal as exc:
        err(f"import-bare-metal: refused: {exc}")
        return EXIT_REFUSED

    try:
        target.data_dir.mkdir(parents=True, exist_ok=True)
        target.progress.write_text(source.export_id + "\n", encoding="utf-8")
        _print_rewrite(rewrite, section, out)
        if section.changed:
            section.write()
        apply_module.write_private(target.config_path.parent / "admins", source.admins)
        _print_secrets(secrets, out)
        _write_secrets(secrets, target)
        out("databases:")
        _restore_databases(source, target, out)
        out("files:")
        _import_repos(source, target, out)
        _import_side_trees(source, target, out)
        report = _finish_database(source, target, rewrite, args.rehearsal)
        _print_db_report(report, out)
        target.progress.unlink()
    except Exception as exc:  # noqa: BLE001 - a partial is reported, never raised
        err(f"import-bare-metal: PARTIAL: {exc}")
        err(f"import-bare-metal: {target.progress} is kept; re-run with the same export to resume.")
        return EXIT_PARTIAL
    out(f"imported {source.manifest.get('source_host')} ({'rehearsal' if args.rehearsal else 'live'}).")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="istota migrate import-bare-metal",
        description="Import a bare-metal install's export into this container's layout.",
    )
    parser.add_argument("--source", required=True, help="The unpacked export directory.")
    parser.add_argument(
        "--ingress", choices=INGRESS_MODES, required=True,
        help="The stack's ingress mode; sets web.trusted_proxy_hops.",
    )
    parser.add_argument(
        "--config", default=os.environ.get("ISTOTA_CONFIG_PATH") or "/data/config/config.toml",
        help="The config.toml to write (default: ISTOTA_CONFIG_PATH, else /data/config/config.toml).",
    )
    parser.add_argument(
        "--secrets-dir", default="/srv/istota/secrets",
        help="The stack's secrets directory, mounted read-write.",
    )
    parser.add_argument("--data-dir", default=str(wizard.CONTAINER_DATA_DIR), help=argparse.SUPPRESS)
    parser.add_argument("--rehearsal", action="store_true", help="Import as an inert copy.")
    parser.add_argument(
        "--emit-plan", metavar="PATH",
        help="Also write the rewritten config as an `istota apply` plan (JSON).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the plan and write nothing.")
    mode.add_argument("--list", action="store_true", dest="list_only", help="Describe the export and the target.")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass
    return run(args, out=print, err=lambda line: print(line, file=sys.stderr))


if __name__ == "__main__":
    raise SystemExit(main())
