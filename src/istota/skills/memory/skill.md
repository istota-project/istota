---
name: memory
description: Persistent memory writes — USER.md (behavioral) and the knowledge graph (facts).
always_include: true
cli: true
---

You have three persistent memory targets for each user:

- **USER.md** — behavioral instructions: how I should act, communication style, defaults, persistent preferences. Loaded automatically into your prompt as the "User memory" section.
- **Knowledge graph** — entity-relationship triples for facts: temporal events (date-stamped) and stable factual claims (identity, family, biography, medical). Loaded automatically as the "Known facts" section.
- **Per-skill overlays** — a behavioral rule scoped to one skill, appended to that skill's own instructions whenever it loads. Only reaches the prompt when the skill does. Written by the *user*, as an ordinary file — not through this CLI. See "Per-skill overlays" below.

Reading is automatic. You never need to `cat` USER.md or query the KG before writing. Write through the CLI commands below — never `echo >>`.

### Classify before writing

Before storing anything in memory, decide which of these branches it falls into:

**Temporal event** — something happened, a decision was made, an item was acquired or disposed of, status changed. Cue: you would naturally write `(noted YYYY-MM-DD)` or "on YYYY-MM-DD" next to it. Verbs: ordered, bought, returned, decided, started, stopped, moved, joined, left, became, finished. → Use `istota-skill memory_search add-fact` with `--from YYYY-MM-DD`.

**Stable factual claim** — a property of the user that is true regardless of date: identity, family, biography, languages spoken, places lived, employer, role. For medical: allergies and named chronic/serious conditions (`allergic_to shellfish`, `has_condition type_1_diabetes`) belong here. Quantitative health data does NOT — see "Health data" below. Cue: a noun phrase about the person, not a verb-headed instruction. Even without a date, these belong in the knowledge graph. → Use `istota-skill memory_search add-fact` (no `--from`).

**Behavioral instruction** — how I should act, communication style, defaults, persistent preferences for my own behavior. Cue: it would still be true a year from now without re-confirmation, AND it tells me what to do. Phrasings: "always", "never", "default to", "prefer", "treat X as Y", "draft as", "send as". → Use `istota-skill memory append`.

**Skill-specific instruction** — a behavioral rule that only bites while one particular skill is in use: a frontmatter convention for notes, a calendar routing rule, how test runs work on this deployment. → It belongs in that skill's overlay, which you edit as a file (see "Per-skill overlays" below), not through this CLI. Decide with this test first: **would it be *wrong* to ignore this rule on a task where the skill was not loaded? If yes → `USER.md`. If it is merely irrelevant there → skill overlay.** A note-naming convention is irrelevant on a task that writes no note. "Never write a new file to the base notes folder" is not — the task that most needs to hear it is the one that never recognized itself as a notes task — so that one stays in USER.md. A rule spanning three or more skills has failed the test; put it in USER.md. Ask the test about the **action**, not the topic: if the thing the rule governs can be done with that skill unloaded, the rule is not skill-specific however much it sounds like it. Editing `CRON.md` is the worked example — only `reminders` and `schedules` write it, so it reads as theirs, but it is an ordinary file any task can edit and a malformed edit silently unschedules every job, so its rules live in USER.md.

**Reusable task procedure** — "here is the multi-step way to do task X" distilled from a successful run (which skills/CLIs, in what order, with what gotchas). Cue: it's a *how-to*, not a fact about the user or a behavioral default. These are **learned playbooks**, stored as markdown files under `playbooks/` and recalled by relevance. In v1 they are generated only by the nightly sleep cycle from successful multi-step task trajectories — there is no runtime `playbooks add` write yet, so this branch is informational. Do not try to hand-write a playbook mid-task.

**Both** (rare) — write the behavioral rule to USER.md AND store the triggering event as a fact. Example: the user tells you they've switched to a new email client and from now on prefers shorter replies. The preference is behavioral; the switch event is a fact.

**Don't store** — anything already on the calendar / in files; transient state ("meeting tomorrow"); information about other users; sensitive data (passwords, tokens, financial account numbers); quantitative health data (see below).

**Health data.** The `health` skill owns body stats, biomarkers, and labs in a separate per-user DB. Never write any of the following to USER.md, the knowledge graph, dated memories, or KV: numerical measurements (weight, BP, HR, body temp, SpO2, body-fat %), biomarker / lab values, medication doses or schedules, dates of specific labs or procedures, current symptoms or transient illnesses. When another skill needs a current value, it should call `istota-skill health latest` or `health trend NAME`, not memorize it. What stays in the KG: allergies (`allergic_to`), named chronic conditions (`has_condition`), and the existence of major surgical history at the diagnosis level — identity-shaped facts that affect general reasoning, not measurements.

**Calendar owns dates for scheduled events.** Never write a KG fact that carries the date of an appointment, meeting, or other calendar-managed event. The calendar is the single source of truth for when things happen — duplicating the date in a KG fact creates two independent stores that can (and do) disagree, with nothing to break the tie. Date-less metadata about a scheduled event is fine: `has_scheduled_procedure mri`, `has_medical_workup "riverside clinic, 4hr fast"`. On these facts, `--from` = when we learned the fact, never the event date. If the event date is the only thing worth recording, write nothing — the calendar already has it.

### Writing facts

```bash
# Temporal events — always include --from
istota-skill memory_search add-fact alice acquired "arc desk lamp" --from 2026-05-03
istota-skill memory_search add-fact alice decided "standardize on clamp-mount lamps with warm dimmable bulbs" --from 2026-05-04
istota-skill memory_search add-fact alice disposed_of "arc desk lamp" --from 2026-05-04

# Stable factual claims — no --from
istota-skill memory_search add-fact alice allergic_to shellfish
istota-skill memory_search add-fact alice has_family_member "wife: Clara"
istota-skill memory_search add-fact alice speaks portuguese
istota-skill memory_search add-fact alice grew_up_in Lisbon
```

**Predicate guidance.** Short, lowercase, snake_case. Prefer the existing vocabulary:

- Single-valued (auto-supersedes the previous value): `works_at`, `lives_in`, `has_role`, `has_status`.
- Temporary (coexists with permanent facts): `staying_in`, `visiting`, `traveled_to`.
- Multi-valued: `acquired`, `disposed_of`, `decided`, `completed`, `owns`, `uses_tech`, `knows`, `speaks`, `prefers`, `allergic_to`, `has_family_member`, `interested_in`, `grew_up_in`, `born_in`, `relates_to`.

**Reverting an `acquired` fact.** When the user gets rid of something they previously acquired, record `disposed_of` with the **same object string**. Both facts live side by side; the timeline is reconstructable from `--from` dates. The fuzzy-dedup engine compares objects only when predicates match, so `acquired` and `disposed_of` for the same object will not collide.

**Other knowledge-graph commands.** `istota-skill memory_search invalidate <fact_id> [--ended YYYY-MM-DD]` marks a fact as no longer valid. `istota-skill memory_search delete-fact <fact_id>` hard-deletes a fact. `istota-skill memory_search facts --subject alice` lists current facts. `istota-skill memory_search timeline alice` lists the historical record for an entity.

### Writing behavioral instructions

```bash
istota-skill memory headings                          # see what's already there
istota-skill memory show --heading "Communication style"   # inspect a section's current content
istota-skill memory append --heading "Communication style" --line "Keep replies under 3 sentences with alice's family"
istota-skill memory append --heading "Communication style" --subheading "Email" --line "Plain text, no HTML"
istota-skill memory add-heading --heading "Travel" --line "Default vehicle is the motorcycle — say 'rode' not 'drove'"
istota-skill memory remove --heading "Preferences" --match "morning meetings"
istota-skill memory replace --heading "Preferences" --match "prefers tea" --line "Prefers black coffee"
istota-skill memory remove-heading --heading "Old Project"
istota-skill memory remove-subheading --heading "Notes" --subheading "Old workflow"
```

Rules:

- The heading must already exist for `append`; on `heading_missing` the CLI returns the list of available headings — pick the closest match or use `add-heading`.
- `add-heading` is for genuinely new topic areas only. Don't proliferate near-duplicates ("Notes", "Memory", "Stuff").
- `remove` requires a substring unique to one bullet under the heading. The match spans the whole section — top region **and** any `### subsections`. If the substring matches multiple bullets, the CLI returns `multiple_matches`; narrow the substring. `### subheading` lines themselves are never matched.
- To **reword** a stale bullet, use `replace --match <substr> --line <new text>` — one in-place op instead of `remove` then `append`. To drop an entire stale section, use `remove-heading`; to drop one `### ` subsection of a section, use `remove-subheading`. Those two are the only way to remove a heading line, or prose and numbered items — `remove` only ever takes a bullet.
- To append under a `### subsection`, pass `--subheading "Name"` to `append`. Without it, `append` targets the section's top region (above the first `###`).

### Per-skill overlays

A skill's instructions can carry a per-user addition at `config/skills/<skill-name>.md`, appended to that skill's body whenever the skill loads, under a heading saying it takes precedence over the skill's own text. It is an addition, never a replacement.

**This CLI does not write them.** An overlay is the user's own document — the `developer` one is a twelve-stage workflow with prose and fenced code blocks in it — and the bullet ops here reach almost none of that. Edit the file directly with your file tools, then check it took:

```bash
istota-skill skills overlays          # what is customized, and does each one load
istota-skill skills overlay notes     # print one overlay
```

Rules:

- Run the classification test above before writing one. A rule that must hold on tasks where the skill did not load belongs in USER.md instead — an overlay only reaches the prompt when its skill is selected.
- After editing one, run `istota-skill skills overlays` and check `binds: true`. That is the only thing that decides whether the file reaches a prompt, and a file can look right and load into nothing: a misspelled skill name, a skill switched off, a file holding nothing but frontmatter, or one over the 32 KB cap. The `reason` field says which. Nothing else will tell you.
- An overlay is flat: no `## ` sections. A level-2 heading escapes the block the overlay is injected into; the loader demotes one it finds, but write `### ` and below.
- `sensitive_actions` and `untrusted_input` accept no overlay.
- Keep it short. Past 24 KB you are near the cap; past 32 KB the file stops loading entirely.
- A rule that applies to two skills goes in both files; there is no include mechanism. Check first that it is skill-specific at all — two skills being the only ones that write some file does not make that file's rules skill-specific, because a third task can edit the file with neither loaded.

### Don't bypass the CLI

Never write to USER.md or CHANNEL.md with `echo >>`, `cat >>`, `tee -a`, or direct file edits. Those bypass section routing, dedup, and the audit log, and for USER.md the nightly bypass detector will flag them as legacy writes. Use `istota-skill memory` exclusively for those two files.

This does not apply to a per-skill overlay. That file has no CLI write path and direct editing is how it is meant to be changed.

### Channel memory

Each Talk room has a `CHANNEL.md` loaded as the "Channel memory" section of your prompt.

```bash
istota-skill memory append --heading "Decisions" --line "Use PostgreSQL" --channel room123
istota-skill memory headings --channel room123
```

The `--channel` flag must match the active conversation token. Cross-channel writes are refused.

**Channel vs user memory.** Channel memory is for things relevant to everyone in the room (project decisions, shared conventions). User memory is for personal preferences and personal context. When unsure, prefer user memory — it won't leak personal context to other room participants.

### Bot-managed directory layout

Each user has a bot-managed workspace area:

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
istota-skill memory_search search "desk lamp" --limit 5
istota-skill memory_search search "Project Alpha" --since 2026-04-01
```

Do not write to dated memory files directly. They are managed by the sleep cycle.
