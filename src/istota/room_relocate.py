"""Offline database migration to minted room identities.

These are dispositions, not an instruction to UPDATE every value. Mixed and
structured columns need their named handler. The schema-walking tests keep
both lists complete for named token columns and room foreign keys; embedded
references require a manual audit when their producers change.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import db

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PARTIAL = 2

# Exact canonical references are rewritten only when they match a migrated
# room. Task and scheduling columns may also contain non-room thread ids.
REWRITE_COLUMNS: dict[tuple[str, str], str] = {
    ("rooms", "token"): "room_token",
    ("rooms", "side_of"): "room_token",
    ("room_bindings", "room_token"): "room_token",
    ("room_members", "room_token"): "room_token",
    ("room_participants", "room_token"): "room_token",
    ("room_read_state", "room_token"): "room_token",
    ("room_dismissals", "room_token"): "room_token",
    ("room_data_grants", "room_token"): "room_token",
    ("room_policy", "room_token"): "room_token",
    ("room_vetoes", "room_token"): "room_token",
    ("room_notices", "room_token"): "room_token",
    ("room_epochs", "room_token"): "room_token",
    ("speech_gate_decisions", "room_token"): "room_token",
    ("messages", "room_token"): "room_token",
    ("message_deletions", "room_token"): "room_token",
    ("web_chat_rooms", "token"): "room_token",
    ("web_chat_messages", "token"): "room_token",
    ("tasks", "conversation_token"): "room_token",
    ("briefing_configs", "conversation_token"): "room_token",
    ("scheduled_jobs", "conversation_token"): "room_token",
    ("channel_sleep_cycle_state", "conversation_token"): "room_token",
    ("credential_grant_rooms", "conversation_token"): "room_token",
    ("outbound_drafts", "room_token"): "room_token",
    ("notifications", "room_token"): "room_token",
    ("user_profiles", "default_room"): "room_token",
    # Parse the descriptor: room:/web: identities differ from talk: refs.
    ("tasks", "output_target"): "descriptor",
    ("scheduled_jobs", "output_target"): "descriptor",
    ("sent_emails", "origin_target"): "descriptor",
    ("outbound_drafts", "origin_target"): "descriptor",
    ("user_profiles", "default_destination"): "descriptor",
    ("user_profiles", "routing"): "routing_json",
    # Multiplayer's email threads store their canonical token here. Historical
    # sent_emails values are Talk refs; private-mail thread hashes stay intact.
    # See transport/email/threads.py::_token_by_stored_mail.
    ("processed_emails", "thread_id"): "email_thread",
    ("sent_emails", "conversation_token"): "email_thread",
    ("memory_chunks", "user_id"): "channel_namespace",
    ("memory_chunks", "source_id"): "channel_path",
    # index_file stores the same path in metadata_json.file_path.
    ("memory_chunks", "metadata_json"): "channel_path_json",
    # Origin: room_token/parent and web channel are canonical, Talk channel is
    # a surface ref. Destination: room_token/parent are canonical; talk_ref,
    # group refs and email thread refs stay intact. Never recursively replace
    # arbitrary strings. Recompute embedded fingerprints with the same helper
    # as their producer, together with the binding_fingerprint columns below.
    ("message_relays", "origin"): "origin_json",
    ("message_relays", "destination"): "destination_json",
    ("whatsapp_skill_requests", "origin"): "origin_json",
    ("whatsapp_skill_requests", "destination"): "destination_json",
    ("message_relays", "binding_fingerprint"): "destination_fingerprint",
    # Includes side_whisper's side_rooms._fingerprint(side, parent), room_post
    # and linked relay destinations; phone binding fingerprints stay intact.
    ("whatsapp_skill_requests", "binding_fingerprint"): "destination_fingerprint",
}

# Surface refs stay native, even where a web ref used to equal rooms.token.
# Credentials and the permanent mapping are included because they too have
# token-bearing column names. Nothing may blanket-rewrite these columns.
PRESERVE_COLUMNS: dict[tuple[str, str], str] = {
    ("room_bindings", "surface_ref"): "native surface reference",
    ("room_participants", "surface_ref"): "native actor identity",
    ("talk_poll_state", "conversation_token"): "Talk poll cursor",
    ("talk_messages", "conversation_token"): "Talk message cache",
    ("tasks", "talk_delivery_token"): "Talk delivery reference",
    ("sent_emails", "talk_delivery_token"): "Talk delivery reference",
    ("sent_emails", "thread_id"): "synthetic mail thread hash",
    ("user_profiles", "log_channel"): "configured Talk reference",
    ("user_profiles", "alerts_channel"): "configured Talk reference",
    # _provisioned_rooms holds Talk refs, not canonical identities. Arbitrary
    # user KV content is not a room-reference schema and must not be rewritten.
    ("istota_kv", "value"): "includes _provisioned_rooms native Talk refs",
    ("room_token_migration", "old_token"): "permanent forwarding source",
    ("room_token_migration", "new_token"): "already minted forwarding target",
    ("google_oauth_tokens", "access_token"): "credential ciphertext",
    ("google_oauth_tokens", "refresh_token"): "credential ciphertext",
    ("google_oauth_tokens", "token_expiry"): "credential expiry timestamp",
    ("web_user_tokens", "access_token"): "credential ciphertext",
    ("web_user_tokens", "refresh_token"): "credential ciphertext",
    ("web_auth_tokens", "token_hash"): "authentication token digest",
    ("task_usage", "billed_input_tokens"): "model usage count",
    ("task_usage", "cache_read_tokens"): "model usage count",
    ("task_usage", "cache_write_tokens"): "model usage count",
    ("task_usage", "initial_context_tokens"): "model usage count",
    ("task_usage", "output_tokens"): "model usage count",
    ("task_usage", "peak_context_tokens"): "model usage count",
    ("task_usage_models", "billed_input_tokens"): "model usage count",
    ("task_usage_models", "cache_read_tokens"): "model usage count",
    ("task_usage_models", "cache_write_tokens"): "model usage count",
    ("task_usage_models", "output_tokens"): "model usage count",
    # Approval covers the preview/body, never a re-rendered migration result.
    ("whatsapp_skill_requests", "preview_digest"): "approved content digest",
    ("whatsapp_skill_requests", "approved_digest"): "approved content digest",
    ("whatsapp_skill_requests", "content_hash"): "body digest",
    ("whatsapp_skill_requests", "service_hash"): "body digest",
    ("whatsapp_skill_requests", "template_hash"): "body digest",
}


_REFERENCE_NAMES = {
    "surface_ref", "side_of", "default_room", "thread_id", "output_target",
    "origin_target", "routing", "default_destination", "log_channel",
    "alerts_channel", "binding_fingerprint",
}


class MigrationRefusal(Exception):
    """A preflight condition that prevents all writes."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def check_inventory(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Reject unclassified token holders in the actual database, not a DDL copy."""
    known = set(REWRITE_COLUMNS) | set(PRESERVE_COLUMNS)
    if set(REWRITE_COLUMNS) & set(PRESERVE_COLUMNS):
        raise MigrationRefusal("overlapping_inventory")
    columns = set()
    candidates = set()
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        table = row[0]
        for column in conn.execute(f"PRAGMA table_info({_quote(table)})"):
            name = column[1]
            columns.add((table, name))
            if "token" in name or name in _REFERENCE_NAMES:
                candidates.add((table, name))
        for fk in conn.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            if fk[2] == "rooms":
                candidates.add((table, fk[3]))
    unknown = candidates - known
    if unknown:
        raise MigrationRefusal("unknown_columns: " + ", ".join(f"{t}.{c}" for t, c in sorted(unknown)))
    # The pre-migration installation may not have booted the code that adds
    # the mapping table. No other schema upgrade is this tool's job.
    missing = known - columns - {
        ("room_token_migration", "old_token"), ("room_token_migration", "new_token"),
    }
    if missing:
        raise MigrationRefusal("missing_columns: " + ", ".join(f"{t}.{c}" for t, c in sorted(missing)))
    return columns


def _preflight(conn: sqlite3.Connection) -> None:
    try:
        live = db.get_users_with_live_tasks(conn)
        active = conn.execute(
            "SELECT status FROM tasks WHERE status IN ('locked','running','pending_confirmation') LIMIT 1",
        ).fetchone()
    except sqlite3.Error as exc:
        raise MigrationRefusal(f"task_table_unreadable: {exc}") from exc
    if live or active:
        reason = "pending_confirmation" if active and active[0] == "pending_confirmation" else "live_tasks"
        raise MigrationRefusal(reason)
    check_inventory(conn)
    ambiguous = conn.execute(
        "SELECT surface,surface_ref FROM room_bindings GROUP BY surface,surface_ref HAVING count(*)>1 LIMIT 1",
    ).fetchone()
    if ambiguous:
        raise MigrationRefusal(f"ambiguous_binding: {ambiguous[0]}:{ambiguous[1]}")


def _descriptor(value: str, old: str, new: str) -> str:
    # Same comma-separated leaf grammar as routing.parse_output_target. Keep
    # whitespace, aliases, unknown leaves and native refs byte-for-byte.
    leaves = value.split(",")
    for index, leaf in enumerate(leaves):
        surface, sep, channel = leaf.partition(":")
        if sep and surface.strip().lower() in {"room", "web"} and channel.strip() == old:
            start = len(channel) - len(channel.lstrip())
            leaves[index] = surface + sep + channel[:start] + new + channel[start + len(old):]
    return ",".join(leaves)


def _channel_path(value: str, old: str, new: str) -> str:
    # Rewrite a path component, never a basename or arbitrary text containing
    # the old token. Both absolute index paths and Channels/... paths exist.
    parts = value.split("/")
    for index in range(1, len(parts)):
        if parts[index - 1] == "Channels" and parts[index] == old:
            parts[index] = new
    return "/".join(parts)


def _json_object(value: str) -> dict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected a stored JSON object")
    return parsed


def _origin(value: dict, old: str, new: str) -> dict:
    result = dict(value)
    for key in ("room_token", "parent"):
        if result.get(key) == old:
            result[key] = new
    if result.get("surface") == "web" and result.get("channel") == old:
        result["channel"] = new
    return result


def _translated(value: str, handler: str, old: str, new: str) -> str:
    if handler == "descriptor":
        return _descriptor(value, old, new)
    if handler == "channel_namespace":
        return f"channel:{new}" if value == f"channel:{old}" else value
    if handler == "channel_path":
        return _channel_path(value, old, new)
    parsed = _json_object(value)
    updated = dict(parsed)
    if handler == "routing_json":
        for key, descriptor in parsed.items():
            if not isinstance(descriptor, str):
                raise ValueError("routing value is not a descriptor")
            updated[key] = _descriptor(descriptor, old, new)
    elif handler == "channel_path_json":
        if isinstance(parsed.get("file_path"), str):
            updated["file_path"] = _channel_path(parsed["file_path"], old, new)
    elif handler == "origin_json":
        updated = _origin(parsed, old, new)
    else:
        raise ValueError(f"unknown handler: {handler}")
    return json.dumps(updated, ensure_ascii=True) if updated != parsed else value


def _destination_hash(destination: dict) -> str:
    if destination.get("kind") == "side_room":
        from .side_rooms import _fingerprint
        return _fingerprint(destination["room_token"], destination["parent"])
    from .relay_destinations import destination_fingerprint
    return destination_fingerprint(destination)


def _verified_destination(conn, destination: dict, fingerprint: str) -> dict:
    """Recover omitted group refs only when the old hash proves they are exact.

    Old room_post snapshots omit email/WhatsApp refs. Using today's bindings
    without verifying the old hash would silently approve a changed audience.
    A snapshot whose original binding cannot be proved stops this room.
    """
    if _destination_hash(destination) == fingerprint:
        return destination
    if destination.get("kind") == "room":
        candidate = dict(destination)
        for surface in ("whatsapp", "email"):
            key = surface + "_ref"
            if key not in candidate:
                binding = db.get_room_binding(conn, destination["room_token"], surface)
                if binding:
                    candidate[key] = binding.surface_ref
        if _destination_hash(candidate) == fingerprint:
            return candidate
    raise ValueError("destination fingerprint cannot be verified")


def _rewrite_destinations(conn, old: str, new: str) -> None:
    for table in ("message_relays", "whatsapp_skill_requests"):
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE destination IS NOT NULL AND destination != ''",
        ).fetchall()
        for row in rows:
            destination = _json_object(row["destination"])
            if not any(destination.get(key) == old for key in ("room_token", "parent")):
                continue
            if destination.get("kind") not in {"room", "side_room"}:
                raise ValueError("unknown destination kind with a room reference")
            verified = _verified_destination(conn, destination, row["binding_fingerprint"])
            if "fingerprint" in destination and destination["fingerprint"] != row["binding_fingerprint"]:
                raise ValueError("embedded destination fingerprint differs")
            updated = _origin(destination, old, new)
            new_hash = _destination_hash(_origin(verified, old, new))
            if "fingerprint" in updated:
                updated["fingerprint"] = new_hash
            if table == "message_relays":
                request = conn.execute(
                    "SELECT binding_fingerprint FROM whatsapp_skill_requests WHERE id=?",
                    (row["request_id"],),
                ).fetchone()
                if request is None or request[0] != row["binding_fingerprint"]:
                    raise ValueError("relay request fingerprint differs")
                conn.execute("UPDATE whatsapp_skill_requests SET binding_fingerprint=? WHERE id=?",
                             (new_hash, row["request_id"]))
            conn.execute(f"UPDATE {table} SET destination=?,binding_fingerprint=? WHERE id=?",
                         (json.dumps(updated, ensure_ascii=True), new_hash, row["id"]))


def _migrate_room(conn: sqlite3.Connection, old: str) -> str:
    """Caller holds BEGIN IMMEDIATE; all canonical holders and mapping commit together."""
    # Task retention deliberately leaves email provenance pointing at pruned
    # tasks. Keep those rows; only a violation introduced here is a failure.
    existing_violations = {tuple(row) for row in conn.execute("PRAGMA foreign_key_check")}
    conn.execute("PRAGMA defer_foreign_keys=ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS room_token_migration (
        old_token TEXT PRIMARY KEY, new_token TEXT NOT NULL, migrated_at TEXT NOT NULL)""")
    if conn.execute("SELECT 1 FROM room_token_migration WHERE old_token=?", (old,)).fetchone():
        raise ValueError("old room already has a permanent mapping")
    new = db.mint_room_token()
    if (conn.execute("SELECT 1 FROM rooms WHERE token=?", (new,)).fetchone()
            or conn.execute("SELECT 1 FROM room_token_migration WHERE old_token=? OR new_token=?", (new, new)).fetchone()
            or conn.execute("SELECT 1 FROM room_bindings WHERE surface_ref=?", (new,)).fetchone()):
        raise ValueError("minted token collision")
    email_room = conn.execute(
        "SELECT 1 FROM rooms WHERE token=? AND origin='email' UNION ALL "
        "SELECT 1 FROM room_bindings WHERE room_token=? AND surface='email'", (old, old),
    ).fetchone() is not None
    _rewrite_destinations(conn, old, new)
    for (table, column), handler in REWRITE_COLUMNS.items():
        if handler in {"destination_json", "destination_fingerprint"}:
            continue  # The two values are checked and updated together above.
        table_sql, column_sql = _quote(table), _quote(column)
        if handler in {"room_token", "email_thread"}:
            if handler == "email_thread" and not email_room:
                continue
            conn.execute(f"UPDATE {table_sql} SET {column_sql}=? WHERE {column_sql}=?", (new, old))
            continue
        for row in conn.execute(
            f"SELECT rowid,{column_sql} FROM {table_sql} WHERE {column_sql} IS NOT NULL AND {column_sql} != ''",
        ).fetchall():
            updated = _translated(row[1], handler, old, new)
            if updated != row[1]:
                conn.execute(f"UPDATE {table_sql} SET {column_sql}=? WHERE rowid=?", (updated, row[0]))
    conn.execute("INSERT INTO room_token_migration VALUES (?, ?, datetime('now'))", (old, new))
    violations = {tuple(row) for row in conn.execute("PRAGMA foreign_key_check")}
    if violations - existing_violations:
        raise ValueError("new foreign key violation after room rewrite")
    return new


def _refusal(reason: str) -> None:
    print(f"refusal: {reason}", file=sys.stderr)


def migrate_database(db_path: Path, *, dry_run: bool = False, list_only: bool = False) -> int:
    """Migrate each legacy room atomically; a failed room remains resumable.

    The service units must be stopped by the caller. A writer lock closes the
    gap between each task guard and its writes, and guards run again after
    each commit. Inspection opens read-only and never initializes the schema.
    """
    conn = None
    moved = 0
    failures = 0
    try:
        mode = "ro" if dry_run or list_only else "rw"
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + f"?mode={mode}", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_chunks_vec'").fetchone():
            from .memory.search import enable_vec_extension
            # The loader opens the installed package's extension, never a DB-
            # supplied path. Restrict extension loading again immediately.
            conn.enable_load_extension(True)
            try:
                if not enable_vec_extension(conn):
                    raise MigrationRefusal("vector_extension_unavailable")
            finally:
                conn.enable_load_extension(False)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN" if dry_run or list_only else "BEGIN IMMEDIATE")
        _preflight(conn)
        rooms = [row[0] for row in conn.execute("SELECT token FROM rooms ORDER BY token")]
        legacy = [token for token in rooms if not db.is_canonical_room_token(token)]
        if list_only or dry_run:
            for token in rooms:
                status = "pending" if token in legacy else "already-migrated"
                print(f"{status}: {token}")
            return EXIT_OK
        for old in legacy:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
                _preflight(conn)
            try:
                new = _migrate_room(conn, old)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                failures += 1
                print(f"failed: {old}: {exc}", file=sys.stderr)
                continue
            moved += 1
            print(f"migrated: {old} -> {new}")
        print(f"database: {moved} migrated, {len(rooms) - len(legacy)} already-migrated, {failures} failed")
        return EXIT_PARTIAL if failures else EXIT_OK
    except Exception as exc:
        _refusal(str(exc))
        return EXIT_PARTIAL if moved or failures else EXIT_REFUSED
    finally:
        if conn is not None:
            conn.close()


def reconcile_mount(config, *, dry_run: bool = False, list_only: bool = False) -> int:
    """The resumable filesystem half, driven only by committed mappings."""
    from .room_mount_reconcile import reconcile
    return reconcile(config, dry_run=dry_run or list_only)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate database room identities. Stop all service units first.")
    parser.add_argument("--db-path", type=Path, help="Database path; defaults to the configured database.")
    parser.add_argument("--reconcile-mount", action="store_true", help="Reconcile channel directories and workspace files from the mapping table.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report pending rooms without writing.")
    mode.add_argument("--list", action="store_true", dest="list_only", help="List current room identities.")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            pass
    try:
        if args.reconcile_mount:
            from .config import load_config
            config = load_config()
            if args.db_path is not None:
                config.db_path = args.db_path
            return reconcile_mount(config, dry_run=args.dry_run, list_only=args.list_only)
        path = args.db_path
        if path is None:
            from .config import load_config
            path = load_config().db_path
        return migrate_database(path, dry_run=args.dry_run, list_only=args.list_only)
    except Exception as exc:
        _refusal(f"config_unreadable: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
