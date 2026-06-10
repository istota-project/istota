# Skills

Skills are self-contained directories under `src/istota/skills/`, each with a `skill.md` file containing YAML frontmatter for metadata and a markdown body for documentation. They provide reference docs loaded into the prompt so Claude knows how to use available tools and CLIs. Some skills also contain Python CLI modules.

## How skills work

Skills are not plugins or extensions. They are curated documentation and tooling that gets selectively loaded into Claude's prompt based on what's relevant to the current task. When a user asks about their calendar, the calendar skill docs are included so Claude knows how to use the CalDAV CLI. When they ask about email, the email skill docs are loaded instead.

## Selection: keyword matching

A single deterministic pass selects skills (the former LLM "Pass 2 semantic routing" was removed — see [Progressive disclosure](#progressive-disclosure) for what replaced it). A skill is selected if any of these match:

- `always_include = true` (files, sensitive_actions, memory, scripts, memory_search, kv)
- `source_types` matches the task's source type (e.g., `briefing` -> calendar, markets)
- Any `keywords` found in the prompt text (e.g., "email" -> email skill)
- Attachment file extensions match `file_types` (e.g., `.wav` -> whisper)
- `companion_skills` of already-selected skills are pulled in

If a skill declares both `keywords` and `resource_types`, the user must have at least one matching resource in addition to the keyword match.

Admin-only skills are filtered out for non-admin users. Skills with unmet `dependencies` are skipped. Skills listed in `disabled_skills` (instance or per-user) are excluded.

### Selection observability

Selection emits an INFO log per task with each selected skill annotated by the rule that fired:

```
pass1_selection count=5: files(always_include), markets(source_type=briefing), email(keyword='email'), …
```

The executor also logs `disclosure: eager=N lazy=M catalogue=K` (see [Progressive disclosure](#progressive-disclosure)). These logs make it easy to count selection misses against runtime credential-proxy rejections (see [security](../deployment/security.md#credential-proxy)).

### Pre-transcription

Audio attachments are transcribed before skill selection so keyword matching works on voice memos.

### Skill stickiness

Skills from recent conversation turns are automatically re-selected for follow-up messages in the same conversation. This applies to the interactive surfaces (`talk`, `email`, `repl`, `web`) with a `conversation_token`, and covers up to 2 prior tasks within a 30-minute window. Skills from a direct reply parent are also carried forward. Sticky skills bypass keyword matching but still respect `disabled_skills` and dependency checks.

This means if you ask about your calendar and then say "also add that to my todos," the calendar skill stays loaded even though the follow-up message only triggers the todos skill.

### Exclude rules

Skills can exclude other skills via `exclude_skills` (e.g., the briefing skill excludes email to prevent delivery interference).

## Progressive disclosure

On by default (`skills.progressive_disclosure`). A selected skill is rendered either **eager** (full instructions inline) or **lazy** (a one-line entry in an "Available skills (load on demand)" section). For a lazy skill the model loads the full body on demand with `istota-skill skills show <name>`. Per-skill mode: frontmatter `disclosure: eager|lazy` wins, else a size threshold (`auto_lazy_threshold_chars`, CLI skills only), else eager; `always_eager` skills (the behavioral/safety set) are never deferred.

The on-demand index is **widened to the full eligible catalogue** — every loadable skill that isn't already eager (excluding always-included, disabled, admin-gated, experimental-gated, missing-dependency, and excluded skills). So the model can reach for any relevant tool even when keyword matching didn't surface it, while the prompt stays small. This replaced an earlier LLM "semantic routing" pre-pass that ran a separate model call per task; the cold-start cost dominated and timed out in production, and the widened catalogue gives the main model the full menu for free. Set `progressive_disclosure = false` for legacy all-eager rendering with no index.

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

Supported frontmatter fields: `name`, `triggers`, `description`, `always_include`, `admin_only`, `cli`, `experimental` (requires `skill_<name>` in `[experimental] features`), `resource_types`, `source_types`, `file_types`, `companion_skills`, `exclude_skills`, `dependencies`, `exclude_memory`, `exclude_persona`, `exclude_resources`, `env` (JSON-encoded array of env spec objects).

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
progressive_disclosure = true     # default; defer lazy bodies + widen the on-demand catalogue index
auto_lazy_threshold_chars = 0     # >0: a CLI skill over N chars defaults to lazy (0 = explicit frontmatter only)
# always_eager = ["sensitive_actions", "untrusted_input", "files", "scripts", "memory"]  # never deferred
```

Instance-wide and per-user skill exclusion:

```toml
# config.toml (instance-wide)
disabled_skills = ["browse", "whisper"]

# [users.alice] block in config.toml (per-user — DB row from `istota user ensure --disabled-skill markets` wins)
disabled_skills = ["markets"]
```
