---
name: memory
description: Persistent memory writes — USER.md (behavioral) and the knowledge graph (facts).
always_include: true
cli: true
---

You have two persistent memory targets for each user:

- **USER.md** — behavioral instructions: how I should act, communication style, defaults, persistent preferences. Loaded automatically into your prompt as the "User memory" section.
- **Knowledge graph** — entity-relationship triples for facts: temporal events (date-stamped) and stable factual claims (identity, family, biography, medical). Loaded automatically as the "Known facts" section.

Reading is automatic. You never need to `cat` USER.md or query the KG before writing. Write through the CLI commands below — never `echo >>`.

### Classify before writing

Before storing anything in memory, decide which of these three branches it falls into:

**Temporal event** — something happened, a decision was made, an item was acquired or disposed of, status changed. Cue: you would naturally write `(noted YYYY-MM-DD)` or "on YYYY-MM-DD" next to it. Verbs: ordered, bought, returned, decided, started, stopped, moved, joined, left, became, finished. → Use `istota-skill memory_search add-fact` with `--from YYYY-MM-DD`.

**Stable factual claim** — a property of the user that is true regardless of date: identity, family, biography, medical, allergies, languages spoken, places lived, employer, role. Cue: a noun phrase about the person, not a verb-headed instruction. Even without a date, these belong in the knowledge graph. → Use `istota-skill memory_search add-fact` (no `--from`).

**Behavioral instruction** — how I should act, communication style, defaults, persistent preferences for my own behavior. Cue: it would still be true a year from now without re-confirmation, AND it tells me what to do. Phrasings: "always", "never", "default to", "prefer", "treat X as Y", "draft as", "send as". → Use `istota-skill memory append`.

**Both** (rare) — write the behavioral rule to USER.md AND store the triggering event as a fact. Example: the user tells you they've switched to a new email client and from now on prefers shorter replies. The preference is behavioral; the switch event is a fact.

**Don't store** — anything already on the calendar / in files; transient state ("meeting tomorrow"); information about other users; sensitive data (passwords, tokens, financial account numbers).

### Writing facts

```bash
# Temporal events — always include --from
istota-skill memory_search add-fact stefan acquired "pilot prera fountain pen" --from 2026-05-03
istota-skill memory_search add-fact stefan decided "standardize on short international cartridge pens with syringe-fill" --from 2026-05-04
istota-skill memory_search add-fact stefan disposed_of "pilot prera fountain pen" --from 2026-05-04

# Stable factual claims — no --from
istota-skill memory_search add-fact stefan allergic_to penicillin
istota-skill memory_search add-fact stefan has_family_member "wife: Marta"
istota-skill memory_search add-fact stefan speaks polish
istota-skill memory_search add-fact stefan grew_up_in Warsaw
```

**Predicate guidance.** Short, lowercase, snake_case. Prefer the existing vocabulary:

- Single-valued (auto-supersedes the previous value): `works_at`, `lives_in`, `has_role`, `has_status`.
- Temporary (coexists with permanent facts): `staying_in`, `visiting`, `traveled_to`.
- Multi-valued: `acquired`, `disposed_of`, `decided`, `completed`, `owns`, `uses_tech`, `knows`, `speaks`, `prefers`, `allergic_to`, `has_family_member`, `interested_in`, `grew_up_in`, `born_in`, `relates_to`.

**Reverting an `acquired` fact.** When the user gets rid of something they previously acquired, record `disposed_of` with the **same object string**. Both facts live side by side; the timeline is reconstructable from `--from` dates. The fuzzy-dedup engine compares objects only when predicates match, so `acquired` and `disposed_of` for the same object will not collide.

**Other knowledge-graph commands.** `istota-skill memory_search invalidate <fact_id> [--ended YYYY-MM-DD]` marks a fact as no longer valid. `istota-skill memory_search delete-fact <fact_id>` hard-deletes a fact. `istota-skill memory_search facts --subject stefan` lists current facts. `istota-skill memory_search timeline stefan` lists the historical record for an entity.

### Writing behavioral instructions

```bash
istota-skill memory headings                          # see what's already there
istota-skill memory show --heading "Communication style"   # inspect a section's current content
istota-skill memory append --heading "Communication style" --line "Keep replies under 3 sentences with stefan's family"
istota-skill memory add-heading --heading "Travel" --line "Default vehicle is the BMW /5 motorcycle — say 'rode' not 'drove'"
istota-skill memory remove --heading "Preferences" --match "morning meetings"
```

Rules:

- The heading must already exist for `append`; on `heading_missing` the CLI returns the list of available headings — pick the closest match or use `add-heading`.
- `add-heading` is for genuinely new topic areas only. Don't proliferate near-duplicates ("Notes", "Memory", "Stuff").
- `remove` requires a substring unique to one bullet under the top region of the heading. If the substring matches multiple bullets, the CLI returns `multiple_matches`; narrow the substring.
- Subsections (`### …`) are opaque to the CLI. To edit content under a subsection, restructure the surrounding section first via `add-heading`/`remove` at the `## ` level.

### Don't bypass the CLI

Never write to USER.md or CHANNEL.md with `echo >>`, `cat >>`, `tee -a`, or direct file edits. Those bypass section routing, dedup, and the audit log, and the nightly bypass detector will flag them as legacy writes. Use `istota-skill memory` exclusively.

### Channel memory

Each Talk room has a `CHANNEL.md` loaded as the "Channel memory" section of your prompt.

```bash
istota-skill memory append --heading "Decisions" --line "Use PostgreSQL" --channel room123
istota-skill memory headings --channel room123
```

The `--channel` flag must match the active conversation token. Cross-channel writes are refused.

**Channel vs user memory.** Channel memory is for things relevant to everyone in the room (project decisions, shared conventions). User memory is for personal preferences and personal context. When unsure, prefer user memory — it won't leak personal context to other room participants.

### Bot-managed directory layout

Each user has a bot-managed area under their Nextcloud:

```
/Users/{user_id}/
├── {BOT_DIR}/      # Shared collaboration space
│   ├── config/
│   │   ├── USER.md     # Persistent memory file (this skill writes here)
│   │   ├── TASKS.md    # User's task file
│   │   └── ...
│   ├── exports/    # Files I generate for the user
│   └── ...
├── inbox/          # Files the user wants me to process
├── memories/       # Auto-generated dated memory files (read-only — written by sleep cycle)
├── shared/         # Auto-organized files shared by user
└── scripts/        # Reusable Python scripts
```

### Dated memory files (search-only)

The nightly sleep cycle writes summaries to `/Users/{user_id}/memories/YYYY-MM-DD.md`. These are NOT auto-loaded — search them on demand:

```bash
istota-skill memory_search search "fountain pen" --limit 5
istota-skill memory_search search "Project Alpha" --since 2026-04-01
```

Do not write to dated memory files directly. They are managed by the sleep cycle.
