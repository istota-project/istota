"""Nightly sleep cycle — extract long-term memories from the day's interactions."""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from . import db
from .brain import BrainRequest, make_brain
from .config import Config
from .storage import (
    _get_mount_path,
    get_user_memories_path,
    get_user_memory_path,
    get_channel_memories_path,
    get_channel_memory_path,
    read_user_memory_v2,
    read_dated_memories,
    read_channel_memory,
    _DATED_MEMORY_PATTERN,
)

logger = logging.getLogger("istota.sleep_cycle")

# Maximum chars of day data to include in extraction prompt
MAX_DAY_DATA_CHARS = 50000

# Minimum per-task budget to avoid tiny fragments
_MIN_TASK_BUDGET = 500

# Source type classification for extraction quality
INTERACTIVE_SOURCE_TYPES = frozenset({"talk", "email", "cli"})
AUTOMATED_SOURCE_TYPES = frozenset({"cron", "briefing", "subtask"})

# Suggested predicates with usage hints, shown in the extraction prompt.
# Not enforced — freeform predicates are accepted and treated as multi-valued
# by the knowledge graph (only SINGLE_VALUED_PREDICATES trigger supersession).
SUGGESTED_PREDICATES = {
    # Single-valued (new value supersedes old — use only when one value is correct at a time)
    "works_at": "employer or organization (single-valued, supersedes)",
    "lives_in": "permanent residence city/country (single-valued, supersedes)",
    "has_role": "job title or role (single-valued, supersedes)",
    "has_status": "current life status like 'on sabbatical' or 'job hunting' (single-valued, supersedes)",
    # Temporary (coexist with permanent facts, auto-flagged)
    "staying_in": "temporary location like a trip or hotel (temporary, use valid_from/valid_until for dates)",
    "visiting": "short visit to a place (temporary, use valid_from/valid_until for dates)",
    # Multi-valued (concurrent facts allowed)
    "works_on": "project, product, or initiative",
    "uses_tech": "software, programming language, or digital tool (not physical objects)",
    "knows": "skill, language, person, or domain knowledge",
    "speaks": "spoken/written language",
    "prefers": "preference or habit (diet, communication style, tools, etc.)",
    "allergic_to": "allergy or intolerance",
    "owns": "vehicle, property, or significant possession",
    "relates_to": "relationship between entities (use when no specific predicate fits)",
    "decided": "explicit decision or commitment",
}

# Sentinel output from Claude indicating nothing worth saving
NO_NEW_MEMORIES = "NO_NEW_MEMORIES"

# Pattern for `ref:N` markers in dated memory bullets — used to attach
# per-task topics (from the extraction's TOPICS section) to the chunk that
# contains the bullet, rather than collapsing to a single dominant topic
# per file.
_REF_PATTERN = re.compile(r"\bref:(\d+)\b")


def _topics_per_chunk(
    chunks: list[str], extracted_topics: dict[str, str]
) -> list[str | None]:
    """Return per-chunk topics derived from ref:N markers inside each chunk.

    Each chunk inherits the topic of the first `ref:N` token it contains.
    Chunks without a ref get None — NULL-topic chunks are always returned
    in topic-filtered searches by design, so a missing ref doesn't hide
    content. If a chunk straddles two refs (long bullets crossing chunk
    boundaries), the first ref wins.
    """
    out: list[str | None] = []
    for chunk in chunks:
        topic: str | None = None
        for m in _REF_PATTERN.finditer(chunk):
            key = f"ref:{m.group(1)}"
            t = extracted_topics.get(key)
            if t:
                topic = t
                break
        out.append(topic)
    return out

# Sleep-cycle Claude calls run unsandboxed, with no tools, no streaming,
# no skill proxy. The prompt does all the work — model returns text/JSON.
_SLEEP_CYCLE_TIMEOUT_SECONDS = 120


def _run_sleep_cycle_brain(
    config: Config, prompt: str, model: str, label: str
) -> tuple[bool, str]:
    """Run a privileged text-only model call through the configured brain.

    Returns (success, output). On failure, output is an error description.

    The sleep cycle is privileged orchestration: no tools, no streaming, no
    sandbox, no progress callbacks, no PID tracking. The brain handles
    transient API retries; everything else stays text-only by construction
    (empty allowed_tools).
    """
    req = BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=Path(config.temp_dir) if config.temp_dir else Path("/tmp"),
        env=dict(os.environ),
        timeout_seconds=_SLEEP_CYCLE_TIMEOUT_SECONDS,
        model=model,
        streaming=False,
        on_progress=None,
        cancel_check=None,
        on_pid=None,
        sandbox_wrap=None,
        result_file=None,
    )
    try:
        result = make_brain(config.brain).execute(req)
    except FileNotFoundError:
        logger.error("Brain CLI not found for %s", label)
        return False, ""
    except Exception as e:
        logger.error("%s brain error: %s", label, e)
        return False, ""

    if result.stop_reason == "timeout":
        logger.error("%s timed out", label)
        return False, ""
    if result.stop_reason == "not_found":
        logger.error("Brain CLI not found for %s", label)
        return False, ""
    if not result.success:
        logger.error(
            "%s failed (stop_reason=%s): %s",
            label,
            result.stop_reason,
            (result.result_text or "")[:200],
        )
        return False, ""
    return True, (result.result_text or "").strip()


def _excerpt(text: str, budget: int) -> str:
    """Return head+tail excerpt of text within budget chars.

    Keeps the first 40% and last 60% when truncation is needed,
    since conclusions and outcomes tend to appear at the end.
    """
    if not text or len(text) <= budget:
        return text or ""
    marker = "\n...[truncated]...\n"
    usable = budget - len(marker)
    if usable < 40:
        return text[:budget]
    head_size = int(usable * 0.4)
    tail_size = usable - head_size
    return text[:head_size] + marker + text[-tail_size:]


def _format_task_section(
    tasks: list,
    per_task_budget: int,
) -> list[str]:
    """Format a group of tasks with conversation grouping and budget control."""
    from collections import defaultdict

    groups: dict[str | None, list] = defaultdict(list)
    for task in tasks:
        groups[task.conversation_token].append(task)

    parts = []
    for conv_token, conv_tasks in groups.items():
        if conv_token and len(conv_tasks) > 1:
            parts.append(
                f"=== Conversation {conv_token} ({len(conv_tasks)} messages) ==="
            )
        for task in conv_tasks:
            prompt_budget = int(per_task_budget * 0.4)
            result_budget = per_task_budget - prompt_budget
            prompt_text = _excerpt(task.prompt or "", prompt_budget)
            result_text = _excerpt(task.result or "", result_budget)
            parts.append(
                f"--- Task {task.id} ({task.source_type}, {task.created_at or 'unknown'}) ---\n"
                f"User: {prompt_text}\n"
                f"Bot: {result_text}\n"
            )
    return parts


def gather_day_data(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    lookback_hours: int,
    after_task_id: int | None,
) -> str:
    """
    Gather the day's interaction data for memory extraction.

    Separates interactive conversations from automated/scheduled output
    with clear section headers so the extraction LLM can distinguish
    user-stated facts from bot-generated content.

    Uses dynamic per-task budget allocation and tail-biased truncation
    to preserve conclusions and decisions. Interactive tasks get 80% of
    the budget when automated tasks are also present.
    """
    since = datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=lookback_hours)
    # DB stores naive UTC timestamps, so strip tzinfo for comparison
    since_str = since.replace(tzinfo=None).isoformat()

    tasks = db.get_completed_tasks_since(conn, user_id, since_str, after_task_id)

    if not tasks:
        return ""

    # Partition by source type
    interactive = [t for t in tasks if t.source_type in INTERACTIVE_SOURCE_TYPES]
    automated = [t for t in tasks if t.source_type not in INTERACTIVE_SOURCE_TYPES]

    # Budget split: 80/20 when both exist, full budget for single section
    if interactive and automated:
        interactive_budget = int(MAX_DAY_DATA_CHARS * 0.8)
        automated_budget = MAX_DAY_DATA_CHARS - interactive_budget
    elif interactive:
        interactive_budget = MAX_DAY_DATA_CHARS
        automated_budget = 0
    else:
        interactive_budget = 0
        automated_budget = MAX_DAY_DATA_CHARS

    parts = []

    if interactive:
        interactive_per_task = max(_MIN_TASK_BUDGET, interactive_budget // len(interactive))
        parts.append(
            "======== INTERACTIVE CONVERSATIONS ========\n"
            "(User spoke directly — primary source for fact extraction)\n"
        )
        parts.extend(_format_task_section(interactive, interactive_per_task))

    if automated:
        automated_per_task = max(_MIN_TASK_BUDGET, automated_budget // len(automated))
        parts.append(
            "\n======== AUTOMATED/SCHEDULED OUTPUT ========\n"
            "(Bot-generated — do not attribute facts to people merely mentioned here)\n"
        )
        parts.extend(_format_task_section(automated, automated_per_task))

    combined = "\n".join(parts)
    if len(combined) > MAX_DAY_DATA_CHARS:
        combined = _excerpt(combined, MAX_DAY_DATA_CHARS)

    return combined


def build_memory_extraction_prompt(
    user_id: str,
    day_data: str,
    existing_memory: str | None,
    date_str: str,
) -> str:
    """
    Build the prompt that instructs Claude to extract memories from the day's interactions.

    Args:
        user_id: The user ID
        day_data: Concatenated interaction data from the day
        existing_memory: Current contents of memory.md (to avoid duplication)
        date_str: Date string for the memory file (e.g. "2026-01-28")
    """
    existing_section = ""
    if existing_memory:
        existing_section = f"""
## Existing long-term memory (memory.md)

The following information is already stored in the user's permanent memory file.
Do NOT repeat any of this information in your output.

{existing_memory}
"""

    predicates_str = "\n".join(
        f"  - {pred}: {hint}" for pred, hint in SUGGESTED_PREDICATES.items()
    )

    return f"""You are extracting important memories from a day of interactions with user '{user_id}'.

Date: {date_str}
{existing_section}
## Today's interactions

{day_data}

## Instructions

Review the interactions above and extract information worth remembering long-term.
Write each item with enough context to be self-contained and useful months from now
without access to the original conversation.

What to extract:
- Facts about the user: preferences, projects, people they work with, habits, goals
- Decisions made or plans discussed, including the reasoning when given
- Corrections the user made to the bot's understanding
- Outcomes of tasks: what was sent, created, configured, or changed
- Recurring patterns or workflows observed across conversations

What to skip:
- Information already in the existing memory above
- Greetings, acknowledgments, and small talk
- Temporary states no longer relevant (e.g., "waiting for a response" when the response already came)
- Raw data dumps or lengthy command output

## Source type guidance

The interactions above may be split into two sections:

INTERACTIVE CONVERSATIONS: Direct exchanges where the user stated facts, made decisions,
or gave instructions. This is the primary source for extracting memories and facts.

AUTOMATED/SCHEDULED OUTPUT: Bot-generated content (briefings, cron jobs, scheduled tasks).
The bot produced this content — the user did not state it. For this section:
- DO extract: outcomes of automated tasks (e.g., "morning briefing was sent successfully")
- DO extract: notable information the bot surfaced that the user would want to remember
- DO NOT extract facts about people merely mentioned in bot-generated content
- DO NOT attribute preferences or behaviors to anyone based on automated output

Write bullet points that are self-contained.
Bad: "Discussed project Alpha."
Good: "Project Alpha is migrating from Django to FastAPI; target completion is Q2 (ref:1234)."

Format: dated bullet points with task references.
- Project Alpha migrating from Django to FastAPI, targeting Q2 completion ({date_str}, ref:1234)
- Prefers email summaries limited to 5 bullet points, not full reports ({date_str}, ref:1235)
- Sent introduction email to Dana (dana@example.com) about consulting engagement ({date_str}, ref:1236)

When the day had few interactions, still extract what is there. A single substantive
memory is better than {NO_NEW_MEMORIES}.

If there is genuinely nothing new worth remembering (e.g., only greetings or repeated
questions with no new information), respond with exactly: {NO_NEW_MEMORIES}

## Output format

Output three sections in this exact order. Each section starts with its marker on its own line.

MEMORIES:
(bullet points as described above — this is the primary output)

Important: if a piece of information is a personal attribute, relationship, or preference
that reduces cleanly to a (subject, predicate, object) triple, put it in FACTS only —
do NOT also create a MEMORY bullet for it. MEMORIES are for events, decisions, outcomes,
and situational context that don't reduce to a simple triple.

Examples:
- "Stefan is allergic to tree nuts" → FACT only (stefan | allergic_to | tree_nuts)
- "Felix has an egg allergy" → FACT only (felix | allergic_to | eggs)
- "Sent intro email to Dana about consulting" → MEMORY (event/outcome, not a triple)
- "Project Alpha migrating to FastAPI, targeting Q2" → MEMORY (situational context)

FACTS:
(JSON array of entity-relationship triples extracted from the interactions)
Each triple: {{"subject": "entity", "predicate": "relationship", "object": "value"}}
Optional temporal fields: "valid_from" and "valid_until" (YYYY-MM-DD) for time-bounded facts.

Suggested predicates (with usage guidance):
{predicates_str}
You may use other predicates when none of the above fit — prefer short, lowercase, snake_case verbs.
Choose predicates carefully: use uses_tech for software/tools only (not physical objects like vehicles),
use has_status for life situations only (not dietary choices — use prefers for those).

Temporal facts: for trips, visits, and time-bounded states, put dates in valid_from/valid_until
fields — NOT in the object string. Example:
  {{"subject": "felix", "predicate": "visiting", "object": "japan", "valid_from": "2026-04-14", "valid_until": "2026-04-24"}}
NOT: {{"subject": "felix", "predicate": "visiting", "object": "japan, april 14-24 2026"}}

Normalize entity names to lowercase. Keep object values concise — a few words, not a sentence.

Subject constraints:
- For user preferences, habits, and decisions, the subject should be "{user_id}"
- Only create facts about other people if {user_id} explicitly stated something about them
  in an interactive conversation — not because they appeared in bot-generated output
- Facts about projects, tools, or organizations are fine when clearly discussed

Bad fact examples (do NOT produce facts like these):
- {{"subject": "max", "predicate": "prefers", "object": "morning briefings"}}
  (Max was mentioned in a briefing the bot generated — {user_id} never said this about Max)
- {{"subject": "dana", "predicate": "works_at", "object": "acme corp"}}
  (Dana appeared in an automated email summary — {user_id} didn't state this)

Good fact examples:
- {{"subject": "{user_id}", "predicate": "prefers", "object": "morning briefings"}}
  ({user_id} explicitly said they prefer morning briefings in conversation)
- {{"subject": "{user_id}", "predicate": "works_on", "object": "project alpha"}}
  ({user_id} discussed working on this project)
- {{"subject": "dana", "predicate": "works_at", "object": "acme corp"}}
  ({user_id} explicitly told the bot that Dana works at Acme in an interactive conversation)

If no facts to extract, output an empty array: []

TOPICS:
(JSON object mapping each task ref to a topic category)
Topics: work, tech, personal, finance, admin, learning, meta
Example: {{"ref:1234": "tech", "ref:1235": "personal"}}
If no topics to classify, output: {{}}

If you cannot produce the structured sections, output only the bullet points (the MEMORIES section).
Do not include any preamble or explanation outside these sections."""


def _validate_fact(fact: dict) -> bool:
    """Validate an extracted fact has required fields with non-empty values.

    Predicates are freeform — any non-empty string is accepted. The knowledge
    graph handles unknown predicates as multi-valued by default.
    """
    if not isinstance(fact, dict):
        return False
    if not all(k in fact for k in ("subject", "predicate", "object")):
        return False
    if not fact["subject"].strip() or not fact["predicate"].strip() or not fact["object"].strip():
        return False
    return True


def _normalize_fact(fact: dict) -> dict:
    """Normalize fact values to lowercase/stripped."""
    fact["subject"] = fact["subject"].strip().lower()
    fact["predicate"] = fact["predicate"].strip().lower()
    fact["object"] = fact["object"].strip().lower()
    return fact


def _parse_structured_extraction(output: str) -> tuple[str, list[dict], dict[str, str]]:
    """Parse structured extraction output into components.

    Returns (memories_text, facts_list, topics_dict).
    Falls back gracefully: if structured sections are missing, treats entire
    output as memories with empty facts/topics.
    """
    facts: list[dict] = []
    topics: dict[str, str] = {}

    # Try to find MEMORIES: section (allow optional newline after marker)
    memories_match = re.search(r"(?:^|\n)MEMORIES:\s*\n?", output)
    facts_match = re.search(r"(?:^|\n)FACTS:\s*\n?", output)
    topics_match = re.search(r"(?:^|\n)TOPICS:\s*\n?", output)

    if not memories_match:
        # No structured format — treat entire output as memories
        return output.strip(), facts, topics

    # Extract memories text (from MEMORIES: to FACTS: or end)
    mem_start = memories_match.end()
    mem_end = facts_match.start() if facts_match else (topics_match.start() if topics_match else len(output))
    memories_text = output[mem_start:mem_end].strip()

    # Extract facts JSON
    if facts_match:
        facts_start = facts_match.end()
        facts_end = topics_match.start() if topics_match else len(output)
        facts_raw = output[facts_start:facts_end].strip()
        try:
            parsed = json.loads(facts_raw)
            if isinstance(parsed, list):
                facts = [_normalize_fact(f) for f in parsed if _validate_fact(f)]
        except (json.JSONDecodeError, ValueError):
            logger.debug("Failed to parse FACTS section: %s", facts_raw[:200])

    # Extract topics JSON
    if topics_match:
        topics_raw = output[topics_match.end():].strip()
        try:
            parsed = json.loads(topics_raw)
            if isinstance(parsed, dict):
                topics = {k: v for k, v in parsed.items() if isinstance(v, str)}
        except (json.JSONDecodeError, ValueError):
            logger.debug("Failed to parse TOPICS section: %s", topics_raw[:200])

    return memories_text, facts, topics


def process_user_sleep_cycle(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
) -> bool:
    """
    Run the sleep cycle for one user: gather data, extract memories, write file.

    Returns True if a memory file was written.
    """
    sleep_config = config.sleep_cycle

    # Get last run state
    last_run_at, last_task_id = db.get_sleep_cycle_last_run(conn, user_id)

    # Gather day data
    day_data = gather_day_data(
        config, conn, user_id, sleep_config.lookback_hours, last_task_id
    )

    if not day_data.strip():
        logger.info("Sleep cycle for %s: no new interactions, skipping", user_id)
        db.set_sleep_cycle_last_run(conn, user_id, last_task_id)
        return False

    # Load existing memory to avoid duplication
    existing_memory = read_user_memory_v2(config, user_id)

    # Build extraction prompt — date_str follows the user's timezone so
    # filenames and bullet timestamps match the user's calendar day, not the
    # server's. Falls back to UTC for unknown tz strings.
    user_cfg = config.users.get(user_id)
    tz_name = user_cfg.timezone if user_cfg and user_cfg.timezone else "UTC"
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("UTC")
    date_str = datetime.now(user_tz).strftime("%Y-%m-%d")
    prompt = build_memory_extraction_prompt(
        user_id, day_data, existing_memory, date_str
    )

    ok, output = _run_sleep_cycle_brain(
        config, prompt,
        model=sleep_config.extraction_model,
        label=f"Sleep cycle extraction for {user_id}",
    )
    if not ok:
        return False

    # Check for sentinel
    if output == NO_NEW_MEMORIES:
        logger.info("Sleep cycle for %s: no new memories to save", user_id)
        # Still update state so we don't reprocess these tasks
        _update_state(config, conn, user_id, last_task_id)
        return False

    # Parse structured output (memories + facts + topics)
    memories_text, extracted_facts, extracted_topics = _parse_structured_extraction(output)

    if not memories_text or memories_text == NO_NEW_MEMORIES:
        logger.info("Sleep cycle for %s: no memories after parsing", user_id)
        _update_state(config, conn, user_id, last_task_id)
        return False

    # Write dated memory file (only the human-readable memories)
    if not config.use_mount:
        logger.warning("Sleep cycle requires mount mode, skipping file write for %s", user_id)
        _update_state(config, conn, user_id, last_task_id)
        return False

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    context_dir.mkdir(parents=True, exist_ok=True)

    memory_file = context_dir / f"{date_str}.md"
    memory_file.write_text(memories_text + "\n")
    logger.info("Wrote dated memory file for %s: %s (%d chars)", user_id, memory_file.name, len(memories_text))

    # Insert extracted facts into knowledge graph (non-critical)
    if extracted_facts:
        try:
            from .memory.knowledge_graph import ensure_table, add_fact
            ensure_table(conn)
            inserted = 0
            for fact in extracted_facts:
                fact_id = add_fact(
                    conn, user_id,
                    subject=fact["subject"],
                    predicate=fact["predicate"],
                    object_val=fact["object"],
                    valid_from=fact.get("valid_from"),
                    valid_until=fact.get("valid_until"),
                    source_type="extracted",
                )
                if fact_id is not None:
                    inserted += 1
            if inserted:
                logger.info("Inserted %d knowledge facts for %s", inserted, user_id)
        except Exception as e:
            logger.debug("Knowledge graph insertion failed for %s: %s", user_id, e)

    # Index memory file for semantic search (non-critical). Per-chunk topics
    # are derived from the bullets each chunk contains: each bullet's
    # `ref:N` marker maps via `extracted_topics` to a topic, and the chunk
    # inherits the topic of its first ref. Bullets without a ref leave the
    # chunk's topic NULL — NULL-topic chunks are always returned in
    # topic-filtered searches by design.
    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .memory.search import (
                chunk_text as _chunk_text,
                index_file as _index_file,
            )
            chunks = _chunk_text(memories_text)
            topic_per_chunk = (
                _topics_per_chunk(chunks, extracted_topics)
                if extracted_topics else None
            )
            _index_file(
                conn, user_id, str(memory_file), memories_text, "memory_file",
                topic_per_chunk=topic_per_chunk,
            )
        except Exception as e:
            logger.debug("Memory search indexing failed for %s: %s", memory_file.name, e)

    # Update state
    _update_state(config, conn, user_id, last_task_id)

    # Clean up old memory files
    cleanup_old_memory_files(config, user_id, sleep_config.memory_retention_days)

    # Clean up old ephemeral memory_chunks (conversation, memory_file,
    # channel_memory). Reuses the same retention setting as the file cleanup
    # so users have a single knob.
    if sleep_config.memory_retention_days > 0:
        try:
            from .memory.search import cleanup_old_chunks
            n = cleanup_old_chunks(conn, user_id, sleep_config.memory_retention_days)
            if n:
                logger.info("Pruned %d ephemeral memory chunks for %s", n, user_id)
        except Exception as e:
            logger.debug("memory_chunks cleanup failed for %s: %s", user_id, e)

    # Knowledge graph audit pruning runs on its own knob, independent of
    # memory_retention_days. Audit rows are tiny but accumulate several
    # per night per user; tying them to the (often-unset) memory knob
    # would let the table grow unbounded by default.
    if sleep_config.knowledge_graph_audit_retention_days > 0:
        try:
            from .memory.knowledge_graph import cleanup_old_audit_rows, ensure_table
            ensure_table(conn)
            n = cleanup_old_audit_rows(
                conn, user_id, sleep_config.knowledge_graph_audit_retention_days
            )
            if n:
                logger.info("Pruned %d KG audit rows for %s", n, user_id)
        except Exception as e:
            logger.debug("KG audit cleanup failed for %s: %s", user_id, e)

    # Curate USER.md if enabled
    if sleep_config.curate_user_memory:
        try:
            curate_user_memory(config, user_id, conn=conn)
        except Exception as e:
            logger.error("USER.md curation failed for %s: %s", user_id, e)

    return True


def _load_kg_facts_text(
    config: Config, conn: "db.sqlite3.Connection | None", user_id: str
) -> str | None:
    """Load formatted knowledge graph facts for the curation prompt.

    Returns None on any failure (graceful degradation). The KG section is
    optional — the model still gets the dated memories and current USER.md
    structure even when this returns None.
    """
    try:
        from .memory.knowledge_graph import (
            ensure_table,
            format_facts_for_prompt,
            get_current_facts,
        )
        if conn is not None:
            ensure_table(conn)
            facts = get_current_facts(conn, user_id)
            return format_facts_for_prompt(facts) if facts else None
        with db.get_db(config.db_path) as temp_conn:
            ensure_table(temp_conn)
            facts = get_current_facts(temp_conn, user_id)
            return format_facts_for_prompt(facts) if facts else None
    except Exception:
        return None


# Phase A observability for USER.md growth. Warning fires once the file
# crosses ~8 KB — somewhere around where it starts pushing other prompt
# sections out under typical max_memory_chars budgets. Tunable via config
# in phase B; for now it's a hard-coded soft threshold.
USER_MEMORY_SOFT_WARN_BYTES = 8 * 1024


def _maybe_warn_usermd_size(config: Config, user_id: str, size_bytes: int) -> None:
    """Post a one-line warning to log_channel when USER.md crosses 8 KB.

    Phase A: visibility only. No truncation, no failure. The audit log
    captures the size on every curation run regardless; this is the
    in-the-moment notice so users see the growth before it bites.
    """
    if size_bytes < USER_MEMORY_SOFT_WARN_BYTES:
        return
    user_cfg = config.users.get(user_id)
    log_channel = getattr(user_cfg, "log_channel", "") if user_cfg else ""
    if not log_channel:
        logger.info(
            "USER.md size warning for %s: %d bytes (no log_channel configured)",
            user_id, size_bytes,
        )
        return
    msg = (
        f"USER.md is {size_bytes:,} bytes — over the {USER_MEMORY_SOFT_WARN_BYTES:,} byte "
        "soft threshold. Consider reviewing for stale entries before it crowds out "
        "recall and KG facts in prompts."
    )
    try:
        from .notifications import send_notification
        send_notification(
            config,
            user_id,
            msg,
            surface="talk",
            conversation_token=log_channel,
        )
    except Exception as e:
        logger.debug("USER.md size warning post failed for %s: %s", user_id, e)


def _post_curation_summary(
    config: Config, user_id: str, applied: list[dict], rejected: list[dict]
) -> None:
    """Post a one-line summary of an applied curation run to the user's log channel.

    No-op when no log channel is configured or when nothing was applied.
    """
    if not applied:
        return
    user_cfg = config.users.get(user_id)
    log_channel = getattr(user_cfg, "log_channel", "") if user_cfg else ""
    if not log_channel:
        return

    n_appended = sum(
        1
        for a in applied
        if a.get("op", {}).get("op") == "append" and a.get("outcome") == "applied"
    )
    n_removed = sum(
        1
        for a in applied
        if a.get("op", {}).get("op") == "remove" and a.get("outcome") == "applied"
    )
    n_headings = sum(
        1
        for a in applied
        if a.get("op", {}).get("op") == "add_heading" and a.get("outcome") == "applied"
    )
    msg = (
        f"USER.md curated: +{n_appended} appended, -{n_removed} removed, "
        f"+{n_headings} new headings"
    )
    try:
        from .notifications import send_notification  # late import to avoid cycles in tests
        send_notification(
            config,
            user_id,
            msg,
            surface="talk",
            conversation_token=log_channel,
        )
    except Exception as e:
        logger.debug("curation summary post failed for %s: %s", user_id, e)


def curate_user_memory(
    config: Config, user_id: str, conn: "db.sqlite3.Connection | None" = None
) -> bool:
    """Op-based USER.md curation.

    Reads current USER.md + last 3 days of dated memories + KG facts, asks
    Sonnet to emit a JSON list of small ops (append / add_heading / remove),
    validates and applies them against a parsed `SectionedDoc`, writes the
    result back, and audit-logs the run.

    Returns True if USER.md was updated.
    """
    import json

    from .memory.curation import (
        apply_ops,
        build_op_curation_prompt,
        parse_sectioned_doc,
        serialize_sectioned_doc,
        strip_json_fences,
        write_audit_log,
    )

    current = read_user_memory_v2(config, user_id) or ""
    dated = read_dated_memories(config, user_id, max_days=3, max_chars=8000)
    if not dated:
        return False

    if not config.use_mount:
        logger.warning("USER.md curation requires mount mode, skipping for %s", user_id)
        return False

    doc = parse_sectioned_doc(current)
    kg_text = _load_kg_facts_text(config, conn, user_id)
    prompt = build_op_curation_prompt(user_id, doc, dated, kg_text)

    ok, output = _run_sleep_cycle_brain(
        config, prompt,
        model=config.sleep_cycle.curation_model,
        label=f"USER.md curation for {user_id}",
    )
    if not ok:
        return False

    raw = strip_json_fences(output)
    try:
        payload = json.loads(raw)
        ops = payload.get("ops", [])
        if not isinstance(ops, list):
            raise ValueError("ops must be a list")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(
            "USER.md curation JSON parse failed for %s: %s; raw=%r",
            user_id,
            e,
            raw[:200],
        )
        return False

    new_doc, applied, rejected = apply_ops(doc, ops)
    if not applied and not rejected:
        return False  # truly nothing happened — no audit, no write
    if not applied:
        write_audit_log(
            config, user_id, applied=[], rejected=rejected,
            user_md_size_bytes=len(current.encode("utf-8")),
        )
        return False

    # Skip the write if every applied op was a no-op (dedup, no_match). Decide
    # this from outcomes rather than text comparison — comparing serialized
    # output against `current` is brittle when USER.md has formatting drift
    # (trailing whitespace on headings, missing trailing newline, CRLF) that
    # the round-trip normalizes away, leading to spurious nightly rewrites.
    real_changes = any(a.get("outcome") == "applied" for a in applied)
    if not real_changes:
        write_audit_log(
            config, user_id, applied=applied, rejected=rejected,
            user_md_size_bytes=len(current.encode("utf-8")),
        )
        return False

    new_text = serialize_sectioned_doc(new_doc)
    memory_path = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(new_text)
    logger.info(
        "Updated USER.md for %s (+%d ops, %d rejected)",
        user_id,
        len(applied),
        len(rejected),
    )
    new_size_bytes = len(new_text.encode("utf-8"))
    write_audit_log(
        config, user_id, applied=applied, rejected=rejected,
        user_md_size_bytes=new_size_bytes,
    )

    # Phase A — observability only. Warn to log_channel when USER.md crosses
    # a soft threshold so growth is visible before it starts displacing
    # recall and KG facts in interactive prompts. Active size pressure is
    # phase B, deferred until we have data.
    _maybe_warn_usermd_size(config, user_id, new_size_bytes)

    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .memory.search import index_file as _index_file
            if conn is not None:
                _index_file(conn, user_id, str(memory_path), new_text, "user_memory")
            else:
                with db.get_db(config.db_path) as temp_conn:
                    _index_file(temp_conn, user_id, str(memory_path), new_text, "user_memory")
        except Exception as e:
            logger.debug("USER.md re-index failed for %s: %s", user_id, e)

    if getattr(config.sleep_cycle, "curation_log_summary", True):
        _post_curation_summary(config, user_id, applied, rejected)

    return True


def _update_state(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    previous_last_task_id: int | None,
) -> None:
    """Update sleep cycle state with the latest completed task ID."""
    # Find the latest completed task ID for this user
    since = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=48)).replace(tzinfo=None).isoformat()
    tasks = db.get_completed_tasks_since(conn, user_id, since, previous_last_task_id)
    if tasks:
        latest_id = tasks[-1].id
    else:
        latest_id = previous_last_task_id
    db.set_sleep_cycle_last_run(conn, user_id, latest_id)


def cleanup_old_memory_files(
    config: Config,
    user_id: str,
    retention_days: int,
) -> int:
    """
    Delete dated memory files older than retention_days.

    Returns number of files deleted. If retention_days <= 0, cleanup is
    skipped (unlimited retention).
    """
    if retention_days <= 0:
        return 0

    if not config.use_mount:
        return 0

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    if not context_dir.exists():
        return 0

    # Cutoff in the user's timezone — filenames are user-local
    # YYYY-MM-DD, so a server-tz cutoff can be off by a day either way.
    user_cfg = config.users.get(user_id)
    tz_name = user_cfg.timezone if user_cfg and user_cfg.timezone else "UTC"
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("UTC")
    cutoff = datetime.now(user_tz) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted = 0
    for path in context_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem
            if date_str < cutoff_str:
                path.unlink()
                deleted += 1
                logger.debug("Deleted old memory file: %s", path.name)

    if deleted:
        logger.info("Cleaned up %d old memory file(s) for %s", deleted, user_id)

    return deleted


def check_sleep_cycles(conn: "db.sqlite3.Connection", config: Config) -> list[str]:
    """
    Evaluate sleep cycle cron for all users, process when due.

    Returns list of user_ids that were processed.
    """
    if not config.sleep_cycle.enabled:
        return []

    sleep_config = config.sleep_cycle
    processed = []

    for user_id, user_config in config.users.items():
        # Evaluate cron in user's timezone
        try:
            user_tz = ZoneInfo(user_config.timezone)
        except Exception:
            user_tz = ZoneInfo("UTC")

        now = datetime.now(user_tz)

        should_run = False
        last_run_at, _ = db.get_sleep_cycle_last_run(conn, user_id)

        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
            cron = croniter(sleep_config.cron, last_run.astimezone(user_tz))
            next_run = cron.get_next(datetime)
            should_run = now >= next_run
        else:
            # Never run — check if we're past first scheduled time today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cron = croniter(sleep_config.cron, today_start)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run

        if should_run:
            logger.info("Running sleep cycle for user %s", user_id)
            try:
                wrote = process_user_sleep_cycle(config, conn, user_id)
                if wrote:
                    processed.append(user_id)
            except Exception as e:
                logger.error("Sleep cycle failed for %s: %s", user_id, e)

    return processed


# ============================================================================
# Channel sleep cycle (shared channel memory extraction)
# ============================================================================


def gather_channel_data(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
    lookback_hours: int,
    after_task_id: int | None,
) -> str:
    """
    Gather channel interaction data for memory extraction.

    Like gather_day_data but filters by conversation_token and includes
    user_id attribution per task.
    """
    since = datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=lookback_hours)
    since_str = since.replace(tzinfo=None).isoformat()

    tasks = db.get_completed_channel_tasks_since(
        conn, conversation_token, since_str, after_task_id
    )

    if not tasks:
        return ""

    per_task_budget = max(_MIN_TASK_BUDGET, MAX_DAY_DATA_CHARS // len(tasks))

    parts = []
    for task in tasks:
        prompt_budget = int(per_task_budget * 0.4)
        result_budget = per_task_budget - prompt_budget
        prompt_text = _excerpt(task.prompt or "", prompt_budget)
        result_text = _excerpt(task.result or "", result_budget)
        parts.append(
            f"--- Task {task.id} (user: {task.user_id}, {task.source_type}, {task.created_at or 'unknown'}) ---\n"
            f"User: {prompt_text}\n"
            f"Bot: {result_text}\n"
        )

    combined = "\n".join(parts)
    if len(combined) > MAX_DAY_DATA_CHARS:
        combined = _excerpt(combined, MAX_DAY_DATA_CHARS)

    return combined


def build_channel_memory_extraction_prompt(
    conversation_token: str,
    day_data: str,
    existing_memory: str | None,
    date_str: str,
) -> str:
    """
    Build prompt for extracting shared memories from channel conversations.

    Focuses on shared context (decisions, agreements, project status) rather
    than personal information.
    """
    existing_section = ""
    if existing_memory:
        existing_section = f"""
## Existing channel memory (CHANNEL.md)

The following information is already stored in this channel's memory file.
Do NOT repeat any of this information. Respect the existing structure —
produce new items that could be appended under appropriate headings.

{existing_memory}
"""

    return f"""You are extracting shared memories from a day of conversations in channel '{conversation_token}'.

Date: {date_str}
{existing_section}
## Today's channel interactions

{day_data}

## Instructions

Review the interactions above and extract information worth remembering as shared channel context.
Focus on:
- Decisions made or agreements reached by the group
- Project status updates, milestones, or blockers
- Action items assigned to specific people
- Technical decisions or architecture choices
- Important context that would help anyone in the channel
- Links between topics discussed here and other projects

Do NOT include:
- Information already in the existing channel memory above
- Personal/private information about individual users
- Trivial exchanges (greetings, acknowledgments, small talk)
- Temporary states that are no longer relevant
- Raw data or lengthy outputs

Format your output as concise bullet points with dates, attribution, and task references, like:
- Decided to migrate API to GraphQL (alice, 2026-01-28, ref:1234)
- Blocked on infrastructure approval for prod deploy (bob, 2026-01-28, ref:1235)

If there is genuinely nothing new worth remembering, respond with exactly: {NO_NEW_MEMORIES}

Output ONLY the bullet points (or {NO_NEW_MEMORIES}). No preamble, no explanation."""


def process_channel_sleep_cycle(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
) -> bool:
    """
    Run the channel sleep cycle: gather data, extract memories, write file.

    Returns True if a memory file was written.
    """
    csc = config.channel_sleep_cycle

    # Get last run state
    last_run_at, last_task_id = db.get_channel_sleep_cycle_last_run(
        conn, conversation_token
    )

    # Gather channel data
    day_data = gather_channel_data(
        config, conn, conversation_token, csc.lookback_hours, last_task_id
    )

    if not day_data.strip():
        logger.info(
            "Channel sleep cycle for %s: no new interactions, skipping",
            conversation_token,
        )
        db.set_channel_sleep_cycle_last_run(conn, conversation_token, last_task_id)
        return False

    # Load existing channel memory to avoid duplication
    existing_memory = read_channel_memory(config, conversation_token)

    # Build extraction prompt — channel sleep cycle stays UTC because
    # channels span timezones. (`datetime.now()` is server-local, so go
    # via ZoneInfo("UTC") explicitly.)
    date_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
    prompt = build_channel_memory_extraction_prompt(
        conversation_token, day_data, existing_memory, date_str
    )

    ok, output = _run_sleep_cycle_brain(
        config, prompt,
        model=csc.extraction_model,
        label=f"Channel sleep cycle extraction for {conversation_token}",
    )
    if not ok:
        return False

    # Check for sentinel
    if output == NO_NEW_MEMORIES:
        logger.info(
            "Channel sleep cycle for %s: no new memories to save",
            conversation_token,
        )
        _update_channel_state(config, conn, conversation_token, last_task_id)
        return False

    # Write dated memory file
    if not config.use_mount:
        logger.warning(
            "Channel sleep cycle requires mount mode, skipping file write for %s",
            conversation_token,
        )
        _update_channel_state(config, conn, conversation_token, last_task_id)
        return False

    memories_dir = _get_mount_path(config, get_channel_memories_path(conversation_token))
    memories_dir.mkdir(parents=True, exist_ok=True)

    memory_file = memories_dir / f"{date_str}.md"
    memory_file.write_text(output + "\n")
    logger.info(
        "Wrote channel memory file for %s: %s (%d chars)",
        conversation_token,
        memory_file.name,
        len(output),
    )

    # Index memory file for semantic search (non-critical)
    channel_user_id = f"channel:{conversation_token}"
    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .memory.search import index_file as _index_file

            _index_file(
                conn,
                channel_user_id,
                str(memory_file),
                output,
                "channel_memory",
            )
        except Exception as e:
            logger.debug(
                "Memory search indexing failed for channel %s: %s",
                conversation_token,
                e,
            )

    # Re-index CHANNEL.md as durable channel memory. Done unconditionally each
    # channel sleep cycle (the file is small, embedding is fast) which also
    # covers the case where humans edit CHANNEL.md directly via Nextcloud.
    _reindex_channel_durable(config, conn, conversation_token)

    # Update state
    _update_channel_state(config, conn, conversation_token, last_task_id)

    # Clean up old memory files
    cleanup_old_channel_memory_files(
        config, conversation_token, csc.memory_retention_days
    )

    # Clean up old ephemeral memory_chunks for this channel (channel_memory
    # source_type only). channel_memory_durable (CHANNEL.md) is intentionally
    # excluded — it refreshes on edit, like USER.md.
    if csc.memory_retention_days > 0:
        try:
            from .memory.search import cleanup_old_chunks
            channel_user_id = f"channel:{conversation_token}"
            n = cleanup_old_chunks(
                conn,
                channel_user_id,
                csc.memory_retention_days,
                source_types=("channel_memory",),
            )
            if n:
                logger.info(
                    "Pruned %d channel memory chunks for %s", n, conversation_token
                )
        except Exception as e:
            logger.debug(
                "channel memory_chunks cleanup failed for %s: %s", conversation_token, e
            )

    return True


def _reindex_channel_durable(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
) -> None:
    """Re-index CHANNEL.md under source_type=channel_memory_durable.

    Like USER.md indexing — durable, refreshed on file edit (or here, on each
    channel sleep cycle). Excluded from EPHEMERAL_SOURCE_TYPES so retention
    never deletes it. No-op if the file doesn't exist or memory search is
    disabled.
    """
    if not (config.memory_search.enabled and config.memory_search.auto_index_memory_files):
        return
    if not config.use_mount:
        return
    try:
        channel_md = _get_mount_path(config, get_channel_memory_path(conversation_token))
    except Exception:
        return
    if not channel_md.is_file():
        return
    try:
        content = channel_md.read_text()
    except Exception:
        return
    if not content.strip():
        return
    try:
        from .memory.search import index_file as _index_file
        _index_file(
            conn,
            f"channel:{conversation_token}",
            str(channel_md),
            content,
            "channel_memory_durable",
        )
    except Exception as e:
        logger.debug(
            "CHANNEL.md durable indexing failed for %s: %s", conversation_token, e
        )


def _update_channel_state(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
    previous_last_task_id: int | None,
) -> None:
    """Update channel sleep cycle state with the latest completed task ID."""
    since = (
        (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=48))
        .replace(tzinfo=None)
        .isoformat()
    )
    tasks = db.get_completed_channel_tasks_since(
        conn, conversation_token, since, previous_last_task_id
    )
    if tasks:
        latest_id = tasks[-1].id
    else:
        latest_id = previous_last_task_id
    db.set_channel_sleep_cycle_last_run(conn, conversation_token, latest_id)


def cleanup_old_channel_memory_files(
    config: Config,
    conversation_token: str,
    retention_days: int,
) -> int:
    """
    Delete dated channel memory files older than retention_days.

    Returns number of files deleted. If retention_days <= 0, cleanup is
    skipped (unlimited retention).
    """
    if retention_days <= 0:
        return 0

    if not config.use_mount:
        return 0

    memories_dir = _get_mount_path(
        config, get_channel_memories_path(conversation_token)
    )
    if not memories_dir.exists():
        return 0

    # Channel filenames are written in UTC (channels span timezones); cutoff
    # follows the same convention to stay aligned.
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted = 0
    for path in memories_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem
            if date_str < cutoff_str:
                path.unlink()
                deleted += 1
                logger.debug("Deleted old channel memory file: %s", path.name)

    if deleted:
        logger.info(
            "Cleaned up %d old channel memory file(s) for %s",
            deleted,
            conversation_token,
        )

    return deleted


def check_channel_sleep_cycles(
    conn: "db.sqlite3.Connection",
    config: Config,
) -> list[str]:
    """
    Evaluate channel sleep cycle cron, auto-discover active channels, process when due.

    Returns list of conversation_tokens that were processed.
    """
    if not config.channel_sleep_cycle.enabled:
        return []

    csc = config.channel_sleep_cycle
    processed = []

    # Auto-discover active channels from recent completed tasks
    since = (
        (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=csc.lookback_hours))
        .replace(tzinfo=None)
        .isoformat()
    )
    active_tokens = db.get_active_channel_tokens(conn, since)

    if not active_tokens:
        return []

    # Evaluate cron in UTC (channels span users in different timezones)
    now = datetime.now(ZoneInfo("UTC"))

    for token in active_tokens:
        should_run = False
        last_run_at, _ = db.get_channel_sleep_cycle_last_run(conn, token)

        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
            cron = croniter(csc.cron, last_run)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run
        else:
            # Never run — check if we're past first scheduled time today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cron = croniter(csc.cron, today_start)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run

        if should_run:
            logger.info("Running channel sleep cycle for %s", token)
            try:
                wrote = process_channel_sleep_cycle(config, conn, token)
                if wrote:
                    processed.append(token)
            except Exception as e:
                logger.error(
                    "Channel sleep cycle failed for %s: %s", token, e
                )

    return processed
