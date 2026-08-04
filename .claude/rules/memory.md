---
paths:
  - "src/istota/memory/**"
  - "src/istota/context.py"
---

# Memory system

## Memory System

- `USER.md` — auto-loaded, optional nightly op-based curation. Runtime writes go through the `memory` skill CLI (`istota-skill memory append|add-heading|remove|replace|remove-heading|show|headings`) — never `echo >>`. `remove`/`replace` reach bullets anywhere in a section (top region **and** `### subsections`); `replace` rewrites in place; `remove-heading` drops a whole section; `append --subheading` targets a subsection. The CLI shares the curation `apply_ops` engine, takes a per-file flock (anchor under the per-user deferred dir, not on the Nextcloud mount, so flock stays reliable and the config dir stays clean), and writes a `source="runtime"` audit entry per call.
- `CHANNEL.md` — loaded with `conversation_token`. Same CLI with `--channel TOKEN` (token must match `ISTOTA_CONVERSATION_TOKEN`). Channel writes are not audited (no per-channel audit infrastructure yet) and do not update `USER.md.last_seen.json`; the audit/curation pipeline is USER.md-only.
- `memories/YYYY-MM-DD.md` — last N days auto-loaded (`auto_load_dated_days`).
- Knowledge graph (`knowledge_facts`) — temporal subject/predicate/object triples, freeform predicates, fuzzy dedup (predicate-equality gated), audited. Sandboxed runtime writes via `istota-skill memory_search add-fact|invalidate|delete-fact` are deferred as `task_<id>_kg_ops.json` and applied by the scheduler post-task.
- Classification gate in `memory/skill.md`: temporal events and stable factual claims → KG; behavioral instructions → USER.md; reusable task procedures → playbooks (sleep-cycle-generated in v1).
- Learned playbooks (`playbooks.enabled`, off by default) — per-user markdown task procedures distilled by the sleep cycle from successful multi-step tasks, stored under the user's bot `playbooks/` dir, indexed into `memory_chunks` as `source_type="playbook"`, and recalled by relevance into a "## Learned Playbooks" prompt section. Markdown-only, never executed; excluded from briefings. Lifecycle (ISSUE-174): the extraction prompt distils the _verified_ command from the trace's per-tool `raw` field (the literal Bash invocation, captured by all three brains) rather than a paraphrase, and writes a thin router instead of re-narrating a single-script task; a `pinned: true` file survives re-derivation (kept on disk, re-indexed so the correction reaches recall); retention (`retention_days`, default 90) prunes on **last-use** mtime (stamped on recall), deletes the pruned file's chunks, never prunes a pinned file, and grandfathers existing files on first upgrade. See `.claude/rules/scheduler.md` (generation) + `.claude/rules/executor.md` (recall).
- Nightly curator self-heals: bypass-write detection (`USER.md.last_seen.json` sidecar), sha256 re-read after the LLM call, agents-header migration, Phase-A lint pass logs date-stamped USER.md bullets without migrating them.
- Memory recall (BM25 + vector) — opt-in via `auto_recall`.
- Briefings exclude all personal memory.
- Subsystem lives under `src/istota/memory/`; `memory/sleep_cycle.py` orchestrates.

## Sleep Cycle

Nightly extraction goes through the configured Brain (no streaming, no sandbox). Per-feature model overrides via `[sleep_cycle]` and `[channel_sleep_cycle]`. Writes dated memory files with `ref:TASK_ID`, inserts KG facts, optionally curates `USER.md` op-by-op. **Degraded-brain skip (ISSUE-181):** the sleep cycle calls the primary brain _directly_ (not the executor's fallback-wrapped path), so it consults the shared availability breaker (`brain/_fallback.primary_brain_unavailable`) before each call and feeds its own failures back (`report_brain_result`). When the primary is in a `usage_limit`/`not_found` state the whole pass is skipped — no per-user/channel extraction, no curation, no playbook distillation — and the first failure opens the breaker so the remaining channels skip in-pass (stops the N-identical-errors pattern). One operator alert fires on the closed→open transition; the breaker cooldown gates the next scheduled run so it doesn't re-attempt every cycle while the primary stays down. Shared-block synthesis follows the same policy (keeps last-known-good content; structured blocks still generate — no brain call). See `.claude/rules/brain.md` "Direct-caller availability" + the posture registry (`brain/_postures.py`).
