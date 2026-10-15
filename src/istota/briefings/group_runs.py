"""Connect common editions to ordinary tasks, source gathering and synthesis."""

from datetime import datetime
from zoneinfo import ZoneInfo

from istota import db, modules
from istota.briefings import group_store
from istota.briefings.document import document_to_markdown
from istota.briefings.source_grants import validate_run_grants
from istota.browser.owner import BRIEFINGS_IDENTITY
from istota.groups.policy import GroupError
from istota.groups.service import _transaction

# Fixed wording: provider errors and private source names never reach the edition.
INCOMPLETE_NOTICE = "Incomplete sources: some selected sources were unavailable or empty."


def _current_run(conn, config, run_id, token, now):
    run = group_store._owned_run(conn, run_id, token, group_store._timestamp(now))
    definition = group_store.get_definition(conn, run["definition_id"])
    if (definition is None or db.get_group(conn, definition["group_id"]) is None
            or definition["revision"] != run["snapshot"]["definition"]["revision"]):
        raise GroupError(409, "definition_changed", "Briefing changed during research")
    validate_run_grants(conn, config, run)
    if not modules.module_available("briefings"):
        raise GroupError(503, "briefings_unavailable", "Briefing generation is unavailable")
    return run


def _research_run(conn, config, run_id, token, now):
    run = _current_run(conn, config, run_id, token, now)
    if run["state"] != "researching":
        raise GroupError(409, "research_conflict", "Run is not awaiting research")
    return run


def create_research_task(conn, config, run_id, token, *, now=None):
    """Idempotently admit one ordinary task for this run attempt."""
    with _transaction(conn):
        run = _research_run(conn, config, run_id, token, now)
        correlation = f"{run_id}:{token}"
        existing = conn.execute(
            "SELECT id FROM tasks WHERE user_id = ? AND source_type = 'group_briefing_research' "
            "AND briefing_name = ? ORDER BY id LIMIT 1", (BRIEFINGS_IDENTITY, correlation),
        ).fetchone()
        if existing:
            return db.get_task(conn, existing["id"])
        research = run["snapshot"]["definition"]["generation"].get("research", {})
        if not research.get("enabled") or not research.get("prompt") or research.get("allowed_domains"):
            raise GroupError(422, "research_settings_refused", "Research settings are unavailable")
        # No final prompt or gathered sources enter this task, including group inputs.
        task_id = db.create_task(
            conn, user_id=BRIEFINGS_IDENTITY, source_type="group_briefing_research",
            prompt=research["prompt"], briefing_name=correlation,
            queue="background", skip_log_channel=True,
        )
        return db.get_task(conn, task_id)


def capture_research_result(conn, config, run_id, token, task_id, *, now=None):
    """Save completed task output under the same attempt that requested it."""
    _research_run(conn, config, run_id, token, now)
    task = db.get_task(conn, task_id)
    if (task is None or task.user_id != BRIEFINGS_IDENTITY
            or task.source_type != "group_briefing_research"
            or task.briefing_name != f"{run_id}:{token}"):
        raise GroupError(409, "research_task_mismatch", "Research task does not belong to this claim")
    if task.status != "completed" or not task.result or not task.result.strip():
        raise GroupError(409, "research_task_incomplete", "Research task has no completed result")
    return group_store.save_research(conn, run_id, token, {
        "task_id": task.id, "task_attempt": task.attempt_count + 1, "text": task.result,
    }, now=now, config=config)


def advance_runs(conn, config, *, now=None):
    """Queue phase tasks and capture finished research; perform no remote I/O."""
    created = []
    rows = conn.execute("SELECT id FROM group_briefing_runs WHERE state IN "
                        "('queued', 'researching', 'generating') ORDER BY due_at, id").fetchall()
    for row in rows:
        run = group_store.get_run(conn, row["id"])
        at = group_store._timestamp(now)
        if run["state"] == "queued" or (run["lease_expires_at"] and run["lease_expires_at"] <= at):
            run = group_store.claim_run(conn, run_id=run["id"], now=now)
        if not run or run["state"] not in ("researching", "generating"):
            continue
        token = run["attempt_token"]
        try:
            _current_run(conn, config, run["id"], token, now)
            group_store.renew_run_lease(conn, run["id"], token, now=now)
            correlation = f"{run['id']}:{token}"
            phase = "group_briefing_research" if run["state"] == "researching" else "group_briefing_generation"
            row = conn.execute("SELECT id FROM tasks WHERE user_id = ? AND source_type = ? "
                               "AND briefing_name = ? ORDER BY id LIMIT 1",
                               (BRIEFINGS_IDENTITY, phase, correlation)).fetchone()
            task = db.get_task(conn, row["id"]) if row else None
            if task is None and run["state"] == "researching":
                task = create_research_task(conn, config, run["id"], token, now=now)
                created.append(task.id)
            if task is not None:
                if task.status not in ("completed", "failed", "cancelled"):
                    continue
                if run["state"] == "researching":
                    if (task.status == "cancelled" or (task.error or "").startswith("briefing_research_")):
                        group_store.finish_run(conn, run["id"], token, error_code="research_unavailable", now=now)
                        continue
                    if task.status == "completed" and task.result and task.result.strip():
                        run = capture_research_result(conn, config, run["id"], token, task.id, now=now)
                    else:
                        # An external research outage may omit this source,
                        # but never turn an all-empty gather into fresh news.
                        run = group_store.save_research(conn, run["id"], token, {
                            "task_id": task.id, "task_attempt": task.attempt_count + 1,
                            "error_code": "research_unavailable", "text": "",
                        }, now=now, config=config)
                else:
                    group_store.finish_run(conn, run["id"], token, error_code="generation_failed", now=now)
                    continue
            if run["state"] == "generating":
                with _transaction(conn):
                    _current_run(conn, config, run["id"], token, now)
                    existing = conn.execute("SELECT id FROM tasks WHERE user_id = ? "
                        "AND source_type = 'group_briefing_generation' AND briefing_name = ?",
                        (BRIEFINGS_IDENTITY, correlation)).fetchone()
                    if existing is None:
                        task_id = db.create_task(conn, user_id=BRIEFINGS_IDENTITY,
                            source_type="group_briefing_generation", prompt="Generate the common edition.",
                            briefing_name=correlation, queue="background", skip_log_channel=True)
                        created.append(task_id)
        except GroupError as exc:
            if exc.code in ("research_conflict", "lease_lost"):
                continue
            # A concurrent edit may already have superseded this claim.
            try:
                group_store.finish_run(conn, run["id"], token, error_code=exc.code, now=now)
            except GroupError:
                pass
    return created


def generate_task(task, config, *, now=None):
    """Generate on an ordinary worker; return only a bounded task outcome."""
    from istota.briefings.generate import assemble_group_input, default_briefing_title, format_briefing_title
    from istota.briefings.shared_blocks import _run_section_brain
    from istota.briefings.sources import GatheredSource
    from istota.briefings.sources.group import GroupSourceContext, gather_group_sources
    from istota.lib.llm_json import candidate_json_blocks
    import json

    if task.user_id != BRIEFINGS_IDENTITY or task.source_type != "group_briefing_generation":
        return False, "generation_task_refused"
    try:
        run_id, token = task.briefing_name.split(":", 1)
        run_id = int(run_id)
    except (AttributeError, TypeError, ValueError):
        return False, "generation_task_refused"
    try:
        with db.get_db(config.db_path) as conn:
            run = group_store.get_run(conn, run_id)
            if run and run["state"] in ("published", "preview_ready"):
                return True, run["state"]  # publication committed before task completion
            run = _current_run(conn, config, run_id, token, now)
            if run["state"] != "generating":
                raise GroupError(409, "research_incomplete", "Research is incomplete")
        def cancelled():
            with db.get_db(config.db_path) as conn:
                current = db.get_task(conn, task.id)
                if (current is None or current.status in ("cancelled", "failed")
                        or current.attempt_count != task.attempt_count):
                    return True
                try:
                    _current_run(conn, config, run_id, token, now)
                except GroupError:
                    return True
            return False

        if cancelled():
            raise GroupError(409, "generation_cancelled", "Generation task is no longer current")
        definition = run["snapshot"]["definition"]
        due = datetime.fromisoformat(run["due_at"].replace("Z", "+00:00"))
        gathered = gather_group_sources(GroupSourceContext(
            definition["group_id"], definition["id"], config, due,
            tuple(run["snapshot"]["grants"]), definition["revision"],
        ))
        if run["research"] is not None:
            research = run["research"]
            gathered.append(GatheredSource("research", "Public research", text=research.get("text", ""),
                untrusted=True, ok=bool(research.get("text", "").strip()),
                failure_kind="unavailable" if research.get("error_code") else None,
                error_code=research.get("error_code"), provenance=f"research task {research['task_id']}"))
        if gathered and not any(source.ok and not source.is_empty for source in gathered):
            raise GroupError(422, "sources_empty", "No selected source has usable content")
        prompt, sections = assemble_group_input(definition, gathered)
        usage = {}
        if prompt:
            ok, text = _run_section_brain(config, prompt, f"group-run-{run_id}",
                                          task_id=task.id, usage_out=usage, cancel_check=cancelled)
            if not ok or not text:
                return False, "synthesis_unavailable"
            if definition["generation_mode"] == "prompt":
                body = text
            else:
                synthesized = None
                for candidate in candidate_json_blocks(text):
                    try:
                        value = json.loads(candidate.text)
                    except (ValueError, TypeError):
                        continue
                    expected = {str(index) for index, section in enumerate(sections) if section["body_md"] is None}
                    if (isinstance(value, dict) and set(value) == expected
                            and all(isinstance(v, str) and v.strip() for v in value.values())):
                        synthesized = value
                        break
                if synthesized is None:
                    return False, "synthesis_invalid"
                for index, section in enumerate(sections):
                    if section["body_md"] is None:
                        section["body_md"] = synthesized[str(index)].strip()
        if cancelled():
            raise GroupError(409, "generation_cancelled", "Generation task is no longer current")
        subject = format_briefing_title(definition["title"] or default_briefing_title(definition["name"]),
                                        due.astimezone(ZoneInfo(definition["timezone"])))
        incomplete = any(not s.ok or s.is_empty for s in gathered)
        document = None
        if definition["generation_mode"] == "blocks":
            if incomplete:
                sections[0]["body_md"] = INCOMPLETE_NOTICE + "\n\n" + sections[0]["body_md"]
            document = {"version": 1, "title": subject, "blocks": sections}
            body = document_to_markdown(document)
        elif incomplete:
            body = INCOMPLETE_NOTICE + "\n\n" + body
        provenance = {"incomplete_sources": incomplete,
                      "sources": [{"kind": s.kind, "ok": s.ok, "provenance": s.provenance,
                                   "error_code": s.error_code} for s in gathered]}
        with db.get_db(config.db_path) as conn:
            result = group_store.publish_run(conn, config, run_id, token, subject=subject,
                body_md=body, provenance=provenance, usage=usage, document=document, now=now)
        return True, result["state"]
    except GroupError as exc:
        with db.get_db(config.db_path) as conn:
            try:
                group_store.finish_run(conn, run_id, token, error_code=exc.code, now=now)
            except GroupError:
                pass
        return True, exc.code  # terminal authorization/claim failure; do not retry
    except Exception:
        # Provider/local errors cannot put contributed text in task logs.
        return False, "generation_unavailable"
