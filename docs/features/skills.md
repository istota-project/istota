# Skills

Skills are self-contained directories under `src/istota/skills/`, each with a `skill.md` file containing YAML frontmatter for metadata and a markdown body for documentation. They provide reference docs loaded into the prompt so Claude knows how to use available tools and CLIs. Some skills also contain Python CLI modules.

## How skills work

Skills are not plugins or extensions. They are curated documentation and tooling that gets selectively loaded into Claude's prompt based on what's relevant to the current task. When a user asks about their calendar, the calendar skill docs are included so Claude knows how to use the CalDAV CLI. When they ask about email, the email skill docs are loaded instead.

## Selection: two-pass system

### Pass 1: keyword matching (deterministic, zero-cost)

A skill is selected if any of these match:

- `always_include = true` (files, sensitive_actions, memory, scripts, memory_search, kv)
- `source_types` matches the task's source type (e.g., `briefing` -> calendar, markets)
- Any `keywords` found in the prompt text (e.g., "email" -> email skill)
- Attachment file extensions match `file_types` (e.g., `.wav` -> whisper)
- `companion_skills` of already-selected skills are pulled in

If a skill declares both `keywords` and `resource_types`, the user must have at least one matching resource in addition to the keyword match.

Admin-only skills are filtered out for non-admin users. Skills with unmet `dependencies` are skipped. Skills listed in `disabled_skills` (instance or per-user) are excluded.

### Pass 2: semantic routing (LLM-based, additive)

When `semantic_routing` is enabled (default), a Haiku call sees the task prompt, a manifest of unselected skills, and the user's resource types (so it can reason "user has miniflux configured → feeds is plausibly relevant" without keyword overlap). It returns additional skills to load. Results are merged with Pass 1. On timeout or error, falls back to Pass 1 only.

### Selection observability

Pass 1 emits an INFO log per task with each selected skill annotated by the rule that fired:

```
pass1_selection count=5: files(always_include), markets(source_type=briefing), email(keyword='email'), …
```

Pass 2 emits `pass2_added skills=…` on additions, `pass2_no_additions` when nothing was added, and `pass2_timeout after=Xs` (WARNING) when the Haiku call exceeded its timeout. These logs make it easy to count selection misses against runtime credential-proxy rejections (see [security](../deployment/security.md#credential-proxy)).

### Pre-transcription

Audio attachments are transcribed before skill selection so keyword matching works on voice memos.

### Skill stickiness

Skills from recent conversation turns are automatically re-selected for follow-up messages in the same conversation. This covers up to 2 prior tasks within a 30-minute window. Skills from a direct reply parent are also carried forward. Sticky skills bypass keyword matching but still respect `disabled_skills` and dependency checks.

This means if you ask about your calendar and then say "also add that to my todos," the calendar skill stays loaded even though the follow-up message only triggers the todos skill.

### Exclude rules

Skills can exclude other skills via `exclude_skills` (e.g., the briefing skill excludes email to prevent delivery interference).

## Skill anatomy

Each skill directory contains:

```
src/istota/skills/calendar/
├── skill.md       # Frontmatter metadata + documentation (required)
├── __init__.py    # CLI module (optional)
└── __main__.py    # python -m support (optional)
```

### skill.md

All metadata lives in the YAML frontmatter. The markdown body is the documentation loaded into Claude's prompt.

```yaml
---
name: calendar
triggers: [calendar, event, meeting, schedule, appointment, caldav]
description: Calendar operations with CalDAV
cli: true
source_types: [briefing]
dependencies: [caldav, icalendar]
---

# Calendar Operations

Calendar operations use CalDAV...
```

Supported frontmatter fields: `name`, `triggers`, `description`, `always_include`, `admin_only`, `cli`, `resource_types`, `source_types`, `file_types`, `companion_skills`, `exclude_skills`, `dependencies`, `exclude_memory`, `exclude_persona`, `exclude_resources`, `env` (JSON-encoded array of env spec objects).

Operator overrides in `config/skills/` can use `skill.md` (or `skill.toml` for backward compatibility).

## Skill CLIs

Skills with Python modules expose CLIs invoked by Claude Code inside the sandbox via `python -m istota.skills.<name>`. The external entry point is `istota-skill <name>`, which routes through the credential proxy when enabled. Pattern: `build_parser()` + `main()`, JSON output, credentials via env vars.

When the skill proxy is enabled, CLI commands run through a Unix socket proxy that injects credentials server-side.

## Discovery layers

Skill discovery uses layered priority:

1. Bundled `skill.md` directories in `src/istota/skills/*/` (base)
2. Operator override directories in `config/skills/*/` (higher priority, `skill.md` or `skill.toml`)

Operator overrides can replace or extend bundled skills.

## Fingerprinting

Skills have a SHA-256 fingerprint (of all `skill.md` + `skill.toml` files). When the fingerprint changes between interactions, a "what's new" changelog is appended to the prompt for interactive tasks.

## Placeholder substitution

`{BOT_NAME}` and `{BOT_DIR}` in skill docs are replaced at load time, separating the technical identifier (`istota`) from the user-facing name.

## Creating new skills

See [adding skills](../development/adding-skills.md) for a step-by-step guide.

## Configuration

```toml
[skills]
semantic_routing = true           # Enable LLM-based Pass 2
semantic_routing_model = "haiku"  # Model for classification
semantic_routing_timeout = 3.0    # Seconds, falls back to Pass 1 on timeout
```

Instance-wide and per-user skill exclusion:

```toml
# config.toml (instance-wide)
disabled_skills = ["browse", "whisper"]

# config/users/alice.toml (per-user)
disabled_skills = ["markets"]
```
