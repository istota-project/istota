"""Deferred-op file handlers for the scheduler.

When a task runs under the bubblewrap sandbox the DB is read-only, so
sandboxed Claude / skill CLI invocations write JSON to the always-RW user
temp dir instead of mutating the DB directly. After the task completes
(or before it retries) the scheduler — which runs unsandboxed — drains
those files via the handlers in this module.

File layout: ``{user_temp_dir}/task_{task_id}_{suffix}.json`` where the
suffix names the consumer (``subtasks``, ``kv_ops``, ``kg_ops``, etc.).
``_KNOWN_DEFERRED_SUFFIXES`` is the source of truth used by both
``_purge_deferred_files_for_retry`` (clear the slate before a retry) and
``_warn_unconsumed_deferred_files`` (catch hallucinated filenames that
would otherwise be silently dropped).

``_load_deferred_email_output`` lives in ``scheduler.py`` rather than
here — it returns parsed dict content for the email-delivery path, not
an op count, and its lifecycle is owned by the result-delivery code.
"""

import json
import logging
import sqlite3
from pathlib import Path

from . import db
from .config import Config

# Use the parent scheduler's logger name so log lines remain identical to
# pre-extraction output and any operator-side log routing keeps working.
logger = logging.getLogger("istota.scheduler")


# Recognized deferred-file suffixes — files matching task_{id}_{suffix}.json
# are consumed by their respective `_process_deferred_*` handlers. Anything
# else in the user temp dir that mentions the task id is unrecognized and
# was silently dropped. We log it so misnamed deferred writes (e.g. a
# hallucinated filename from the model) become visible.
_KNOWN_DEFERRED_SUFFIXES = (
    "subtasks",
    "tracked_transactions",
    "sent_emails",
    "kv_ops",
    "kg_ops",
    "user_alerts",
    "email_output",
    "health_ops",
)


def _load_deferred_json(
    user_temp_dir: Path,
    task_id: int,
    suffix: str,
    *,
    expected_type: type = list,
) -> tuple[Path, list | dict] | None:
    """Open ``task_<id>_<suffix>.json`` in ``user_temp_dir`` for a deferred-op handler.

    Returns ``(path, data)`` on success. Returns ``None`` for absent or
    malformed files; malformed files are unlinked and a WARN is logged. The
    path is returned so the caller can ``unlink`` after processing — keeping
    the lifecycle (and any task-specific invariants) explicit at the call-site.

    ``expected_type`` is checked with ``isinstance``; mismatches are treated
    as malformed (warned and unlinked).
    """
    path = user_temp_dir / f"task_{task_id}_{suffix}.json"
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Bad deferred %s file for task %d: %s", suffix, task_id, e)
        path.unlink(missing_ok=True)
        return None

    if not isinstance(data, expected_type):
        logger.warning(
            "Deferred %s for task %d is not a %s",
            suffix, task_id, expected_type.__name__,
        )
        path.unlink(missing_ok=True)
        return None

    return path, data


def _process_deferred_subtasks(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred subtask creation requests from JSON file.

    Returns count of subtasks created.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "subtasks")
    if loaded is None:
        return 0
    path, data = loaded

    # Admin-only: non-admin users cannot create subtasks
    if not config.is_admin(task.user_id):
        logger.warning(
            "Non-admin user %s attempted deferred subtask creation (task %d), ignoring",
            task.user_id, task.id,
        )
        path.unlink(missing_ok=True)
        return 0

    max_subtasks = config.scheduler.max_subtasks_per_task
    max_depth = config.scheduler.max_subtask_depth
    max_chars = config.scheduler.max_subtask_prompt_chars
    count = 0
    with db.get_db(config.db_path) as conn:
        # Depth gate: refuse to extend a chain that's already at or past the cap.
        # parent_depth >= max_depth means a new child would land at depth+1 > max.
        if max_depth > 0:
            parent_depth = db.get_subtask_depth(conn, task.id)
            if parent_depth >= max_depth:
                logger.warning(
                    "Task %d at subtask depth %d >= max_subtask_depth %d, "
                    "refusing %d deferred subtask(s)",
                    task.id, parent_depth, max_depth, len(data),
                )
                path.unlink(missing_ok=True)
                return 0

        for entry in data:
            if count >= max_subtasks:
                logger.warning(
                    "Task %d hit deferred subtask limit (%d), ignoring remaining entries",
                    task.id, max_subtasks,
                )
                break
            prompt = entry.get("prompt", "")
            if not prompt:
                continue
            if max_chars > 0 and len(prompt) > max_chars:
                logger.warning(
                    "Task %d deferred subtask prompt too long (%d > %d chars), skipping",
                    task.id, len(prompt), max_chars,
                )
                continue
            # Pin conversation_token to parent task — deferred JSON cannot
            # override this to prevent prompt-injection-driven routing.
            conv_token = task.conversation_token
            output_target = entry.get("output_target")
            if not output_target and conv_token:
                output_target = "talk"
            db.create_task(
                conn,
                prompt=prompt,
                user_id=task.user_id,
                source_type="subtask",
                parent_task_id=task.id,
                conversation_token=conv_token,
                priority=entry.get("priority", 5),
                queue=task.queue,
                output_target=output_target,
                talk_delivery_token=task.talk_delivery_token,
                # Inherit parent's model / effort overrides — a task spawned
                # via `!model opus-46-high` should run its children at the
                # same level unless the deferred JSON explicitly overrides.
                model=entry.get("model") or task.model,
                effort=entry.get("effort") or task.effort,
            )
            count += 1

    if count:
        logger.info(
            "Created %d deferred subtasks for task %d (prompts: %s)",
            count, task.id,
            ", ".join(repr(e.get("prompt", "")[:80]) for e in data[:count]),
        )
    path.unlink(missing_ok=True)
    return count


def _process_deferred_tracking(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred transaction tracking requests from JSON file.

    Returns count of items processed.
    """
    loaded = _load_deferred_json(
        user_temp_dir, task.id, "tracked_transactions", expected_type=dict,
    )
    if loaded is None:
        return 0
    path, data = loaded

    count = 0
    with db.get_db(config.db_path) as conn:
        monarch_synced = data.get("monarch_synced", [])
        if monarch_synced:
            count += db.track_monarch_transactions_batch(conn, task.user_id, monarch_synced)

        csv_imported = data.get("csv_imported", [])
        if csv_imported:
            hashes = [e["content_hash"] for e in csv_imported if "content_hash" in e]
            source_file = csv_imported[0].get("source_file") if csv_imported else None
            count += db.track_csv_transactions_batch(conn, task.user_id, hashes, source_file)

        for txn_id in data.get("monarch_recategorized", []):
            if db.mark_monarch_transaction_recategorized(conn, task.user_id, txn_id):
                count += 1

        for update in data.get("monarch_category_updates", []):
            if db.update_monarch_transaction_posted_account(
                conn, task.user_id,
                update["monarch_transaction_id"],
                update["posted_account"],
            ):
                count += 1

    if count:
        logger.info("Processed %d deferred tracking entries for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_sent_emails(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred sent email records from JSON file.

    When Claude sends emails via `email send` inside the sandbox, the skill
    writes a deferred file with message metadata. The scheduler processes it
    here to record outbound emails for emissary thread matching.

    Returns count of sent emails recorded.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "sent_emails")
    if loaded is None:
        return 0
    path, data = loaded

    count = 0
    with db.get_db(config.db_path) as conn:
        for entry in data:
            message_id = entry.get("message_id", "")
            to_addr = entry.get("to_addr", "")
            if not message_id or not to_addr:
                continue
            try:
                db.record_sent_email(
                    conn,
                    user_id=task.user_id,
                    message_id=message_id,
                    to_addr=to_addr,
                    subject=entry.get("subject"),
                    task_id=task.id,
                    conversation_token=task.conversation_token,
                    talk_delivery_token=task.talk_delivery_token,
                )
                count += 1
            except Exception as e:
                logger.warning(
                    "Failed to record sent email for task %d: %s", task.id, e,
                )

    if count:
        logger.info("Recorded %d deferred sent emails for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_kv_ops(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred KV store operations from JSON file.

    When Claude runs `istota-skill kv set|delete|set-add|set-remove` inside
    the sandbox, the skill CLI writes operations to a deferred file. The
    scheduler processes them here. `set-add` / `set-remove` re-read the
    current value from the DB before applying the diff so concurrent ops
    across tasks compose correctly.

    Returns count of operations processed.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "kv_ops")
    if loaded is None:
        return 0
    path, data = loaded

    count = 0
    with db.get_db(config.db_path) as conn:
        for entry in data:
            op = entry.get("op")
            namespace = entry.get("namespace", "")
            key = entry.get("key", "")
            if not namespace or not key:
                continue
            try:
                if op == "set":
                    value = entry.get("value", "")
                    db.kv_set(conn, task.user_id, namespace, key, value)
                    count += 1
                elif op == "delete":
                    db.kv_delete(conn, task.user_id, namespace, key)
                    count += 1
                elif op in ("set-add", "set-remove"):
                    members = entry.get("members") or []
                    if not isinstance(members, list):
                        logger.warning(
                            "Bad %s op for task %d: members not a list", op, task.id,
                        )
                        continue
                    row = db.kv_get(conn, task.user_id, namespace, key)
                    current: list = []
                    if row is not None:
                        try:
                            parsed = json.loads(row["value"])
                        except json.JSONDecodeError:
                            logger.warning(
                                "Skipping %s for task %d: %s/%s is not valid JSON",
                                op, task.id, namespace, key,
                            )
                            continue
                        if not isinstance(parsed, list):
                            logger.warning(
                                "Skipping %s for task %d: %s/%s is not a JSON array",
                                op, task.id, namespace, key,
                            )
                            continue
                        current = parsed
                    if op == "set-add":
                        seen = set(current)
                        new_members = list(current)
                        for m in members:
                            if m not in seen:
                                new_members.append(m)
                                seen.add(m)
                    else:
                        to_remove = set(members)
                        new_members = [m for m in current if m not in to_remove]
                    db.kv_set(
                        conn, task.user_id, namespace, key, json.dumps(new_members),
                    )
                    count += 1
                else:
                    logger.warning(
                        "Unknown KV op %r in deferred file for task %d", op, task.id,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to process KV op for task %d: %s", task.id, e,
                )

    if count:
        logger.info("Processed %d deferred KV ops for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_user_alerts(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred user alert requests from JSON file.

    When the agent detects suspicious inbound content (social engineering,
    prompt injection, exfiltration attempts), it writes alerts to a deferred
    file. The scheduler posts them to the user's alerts channel after task
    completion.

    Returns count of alerts posted.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "user_alerts")
    if loaded is None:
        return 0
    path, data = loaded

    count = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message", "").strip()
        if not message:
            continue

        alert_type = entry.get("type", "security")
        if alert_type == "action_needed":
            formatted = f"**Action needed** (task #{task.id})\n\n{message}"
        else:
            formatted = f"⚠️ **Security alert** (task #{task.id})\n\n{message}"

        from .notifications import send_notification
        if send_notification(config, task.user_id, formatted, surface="talk"):
            count += 1

    if count:
        logger.info("Posted %d deferred user alerts for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_kg_ops(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred knowledge-graph operations from JSON file.

    `istota-skill memory_search add-fact / invalidate / delete-fact` write
    a JSON op here when the DB is read-only inside the sandbox; we apply
    them post-task with task.user_id always wins over any user_id in the
    file (defense-in-depth).

    Returns count of operations processed.
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "kg_ops")
    if loaded is None:
        return 0
    path, data = loaded

    from .memory.knowledge_graph import (
        add_fact as kg_add_fact,
        delete_fact as kg_delete_fact,
        ensure_table as kg_ensure_table,
        invalidate_fact as kg_invalidate_fact,
    )

    count = 0
    with db.get_db(config.db_path) as conn:
        kg_ensure_table(conn)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            op = entry.get("op")
            try:
                if op == "add_fact":
                    subject = entry.get("subject", "")
                    predicate = entry.get("predicate", "")
                    object_val = entry.get("object", "")
                    if not (subject and predicate and object_val):
                        continue
                    kg_add_fact(
                        conn, task.user_id, subject, predicate, object_val,
                        valid_from=entry.get("valid_from"),
                        source_task_id=task.id,
                        source_type=entry.get("source_type", "user_stated"),
                    )
                    count += 1
                elif op == "invalidate":
                    fact_id = entry.get("fact_id")
                    if fact_id is None:
                        continue
                    kg_invalidate_fact(conn, int(fact_id), ended=entry.get("ended"))
                    count += 1
                elif op == "delete":
                    fact_id = entry.get("fact_id")
                    if fact_id is None:
                        continue
                    kg_delete_fact(conn, int(fact_id))
                    count += 1
                else:
                    logger.warning(
                        "Unknown KG op %r in deferred file for task %d", op, task.id,
                    )
                    continue
                # Per-op commit (ISSUE-074): a failure later in the loop must
                # not roll back ops we've already accepted. `delete` and
                # `invalidate` are not idempotent, so a partial replay would
                # otherwise re-apply work the next time this file was read.
                conn.commit()
            except Exception as e:
                logger.warning(
                    "Failed to process KG op for task %d: %s", task.id, e,
                )

    if count:
        logger.info("Processed %d deferred KG ops for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_health_ops(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Apply deferred ``health`` skill operations to the user's health DB.

    Resolves the user's :class:`HealthContext` and replays insert / update
    ops written by sandboxed ``istota-skill health`` invocations. The
    user id always comes from the task (defense-in-depth).
    """
    loaded = _load_deferred_json(user_temp_dir, task.id, "health_ops")
    if loaded is None:
        return 0
    path, data = loaded

    try:
        from . import health as _health
        from .health import db as health_db
    except ImportError as e:
        logger.warning(
            "Health module unavailable for deferred ops on task %d: %s",
            task.id, e,
        )
        path.unlink(missing_ok=True)
        return 0

    try:
        ctx = _health.resolve_for_user(task.user_id, config)
    except _health.UserNotFoundError as e:
        logger.warning(
            "Skipping health ops for task %d: %s", task.id, e,
        )
        path.unlink(missing_ok=True)
        return 0

    _health.ensure_initialised(ctx)

    count = 0
    with health_db.connect(ctx.db_path) as conn:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            op = entry.get("op")
            try:
                if op == "insert_stat":
                    health_db.insert_stat(
                        conn,
                        metric=entry["metric"],
                        value=float(entry["value"]),
                        unit=entry["unit"],
                        measured_at=entry.get("measured_at"),
                        source=entry.get("source", "manual"),
                        notes=entry.get("notes"),
                    )
                    count += 1
                elif op == "insert_panel":
                    health_db.insert_panel(
                        conn,
                        drawn_at=entry["drawn_at"],
                        lab_name=entry.get("lab_name"),
                        panel_type=entry.get("panel_type"),
                        notes=entry.get("notes"),
                    )
                    count += 1
                elif op == "insert_biomarker":
                    health_db.insert_biomarker(
                        conn,
                        panel_id=int(entry["panel_id"]),
                        name=entry["name"],
                        value=float(entry["value"]),
                        unit=entry["unit"],
                        ref_range_low=entry.get("ref_range_low"),
                        ref_range_high=entry.get("ref_range_high"),
                        flag=entry.get("flag"),
                    )
                    count += 1
                elif op == "register_upload":
                    # Copy the source file into the uploads dir under the
                    # new panel id, then record the panel row.
                    import mimetypes
                    import shutil as _shutil

                    src = Path(entry["source_path"])
                    if not src.is_file():
                        logger.warning(
                            "register_upload skipped for task %d: source missing %s",
                            task.id, src,
                        )
                        continue
                    mime = (
                        mimetypes.guess_type(src.name)[0]
                        or "application/octet-stream"
                    )
                    pid = health_db.insert_panel(
                        conn,
                        drawn_at=entry["drawn_at"],
                        lab_name=entry.get("lab_name"),
                        source_mime=mime,
                        draft=True,
                    )
                    target_dir = ctx.uploads_dir / str(pid)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / f"original{src.suffix}"
                    try:
                        _shutil.copyfile(src, target)
                    except OSError as e:
                        logger.warning(
                            "register_upload copy failed for task %d: %s",
                            task.id, e,
                        )
                        continue
                    rel = str(target.relative_to(ctx.uploads_dir))
                    conn.execute(
                        "UPDATE panels SET source_file = ? WHERE id = ?",
                        (rel, pid),
                    )
                    count += 1
                elif op == "import_csv":
                    from .health import csv_io as _csv_io

                    src = Path(entry["source_path"])
                    if not src.is_file():
                        logger.warning(
                            "import_csv skipped for task %d: source missing %s",
                            task.id, src,
                        )
                        continue
                    csv_text = src.read_text(encoding="utf-8-sig", errors="replace")
                    on_collision = entry.get("on_collision") or "skip"
                    try:
                        summary = _csv_io.import_csv(
                            conn, csv_text, on_collision=on_collision,
                        )
                    except ValueError as e:
                        logger.warning(
                            "import_csv invalid args for task %d: %s",
                            task.id, e,
                        )
                        continue
                    logger.info(
                        "Imported CSV for task %d: created=%d replaced=%d skipped=%d biomarkers=%d",
                        task.id, summary.panels_created, summary.panels_replaced,
                        summary.panels_skipped, summary.biomarkers_created,
                    )
                    count += 1
                elif op == "set_setting":
                    key = entry["key"]
                    value = entry.get("value")
                    if key == "display_units_merge":
                        existing = health_db.get_settings(conn).get(
                            "display_units"
                        ) or {}
                        if isinstance(value, dict):
                            existing.update(value)
                            health_db.set_setting(conn, "display_units", existing)
                            count += 1
                    else:
                        health_db.set_setting(conn, key, value)
                        count += 1
                else:
                    logger.warning(
                        "Unknown health op %r in deferred file for task %d",
                        op, task.id,
                    )
                    continue
                conn.commit()
            except (KeyError, ValueError, sqlite3.Error) as e:
                logger.warning(
                    "Failed to process health op for task %d: %s",
                    task.id, e,
                )

    if count:
        logger.info("Processed %d deferred health ops for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _purge_deferred_files_for_retry(task: db.Task, user_temp_dir: Path) -> None:
    """Delete the task's accumulated deferred-op files before a retry.

    ISSUE-074: producers like ``_defer_kg_op`` *append* to ``task_{id}_*.json``;
    on retry the same task.id is reused, so a previously-failed attempt's ops
    would replay alongside the new attempt's. Non-idempotent ops (``invalidate``,
    ``delete`` for KG; subtask creation; outbound emails; user alerts) make
    replays harmful, not just redundant. Clear the slate on every retry.

    Result and prompt files are left in place — they're scoped per-task, not
    per-attempt, and the executor overwrites them.
    """
    if not user_temp_dir.is_dir():
        return
    purged: list[str] = []
    for suffix in _KNOWN_DEFERRED_SUFFIXES:
        path = user_temp_dir / f"task_{task.id}_{suffix}.json"
        if path.exists():
            try:
                path.unlink()
                purged.append(suffix)
            except OSError as e:
                logger.warning(
                    "Could not purge deferred %s for task %d retry: %s",
                    suffix, task.id, e,
                )
    if purged:
        logger.info(
            "Purged deferred files for task %d retry: %s",
            task.id, ", ".join(purged),
        )


def _warn_unconsumed_deferred_files(task: db.Task, user_temp_dir: Path) -> None:
    """Log a WARN for any task-scoped file in user_temp_dir that doesn't
    match a recognized deferred-file name.

    Catches two failure shapes:
    - Hallucinated names that drop the ``task_`` prefix (e.g.
      ``{id}_skip_log.json``) — would never match the consumers' exact
      filename lookup.
    - Canonical ``task_{id}_<unknown>.json`` shapes for handlers that don't
      exist — also silently ignored by the dispatch.
    """
    if not user_temp_dir.is_dir():
        return
    known_filenames = {
        f"task_{task.id}_{suffix}.json" for suffix in _KNOWN_DEFERRED_SUFFIXES
    }
    # Static task-scoped files written by the executor itself.
    known_filenames.add(f"task_{task.id}_prompt.txt")
    known_filenames.add(f"task_{task.id}_result.txt")

    suspicious: list[Path] = []
    # Shape 1: missing the ``task_`` prefix entirely.
    suspicious.extend(user_temp_dir.glob(f"{task.id}_*"))
    # Shape 2: canonical prefix but unknown suffix.
    for path in user_temp_dir.glob(f"task_{task.id}_*"):
        if path.name not in known_filenames:
            suspicious.append(path)

    for path in suspicious:
        logger.warning(
            "Unrecognized deferred file for task %d: %s "
            "(expected name: task_%d_<%s>.json)",
            task.id, path.name, task.id,
            "|".join(_KNOWN_DEFERRED_SUFFIXES),
        )
