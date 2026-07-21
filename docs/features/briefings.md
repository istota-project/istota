# Briefings

Scheduled summaries delivered to Talk and/or email. A morning briefing might include today's calendar events, market futures, headline news, and pending todos.

## Content model: blocks

Blocks are the sole content model. A briefing is an ordered list of **blocks**, each with a title, an optional synthesis directive, a render mode (`synthesis` / `structured`), and 1..N **sources**. Each block is gathered and synthesized into one section at generation time.

Source kinds (`kind = …`):

| Kind | What it gathers |
|---|---|
| `rss` | A Feeds subscription or category (needs the Feeds module) |
| `email` | Shared-pool newsletters, windowed and owner-filtered |
| `browse` | A bundled preset frontpage or an arbitrary URL (needs the browser) |
| `markets` | Futures / indices via yfinance |
| `calendar` | Today's / tomorrow's events |
| `todos` | Pending todo items from a workspace file |
| `reminders` | A random reminder |
| `notes` | Recent notes |

> The legacy boolean-`components` content model (and its `[briefing_defaults]` expansion) is retired. Existing component-based briefings migrate once into blocks on first touch of the briefings module DB.

## Configuration sources

Schedule + delivery are loaded from (higher precedence wins):

1. `briefing_configs` DB table — provisioned via `istota briefings schedule ensure` (Ansible) or the web UI (briefings tab → settings); `enabled=0` mutes a row without scheduling.
2. `[[users.alice.briefings]]` in main `config/config.toml` (name/cron/output; DB rows win by name).

Content **blocks** are seeded once into the per-user briefings module DB from config-authored `[[users.X.briefings.blocks]]` and are then edited in the web block editor.

## Default briefings

A canonical shared set defined once in the top-level `[[default_briefings]]` section is seeded by name into each opted-in user (per-user `default_briefings` flag, default on; an explicit user briefing of the same name wins). Seeding is one-time — later web edits win and re-runs never clobber them. Opt a user out with `istota user ensure --no-default-briefings`.

## Memory isolation

Briefing prompts intentionally exclude USER.md and dated memories. This prevents private context from leaking into what may be shared or newsletter-style output.

## Example config (TOML)

```toml
[[users.alice.briefings]]
name = "morning"
cron = "0 6 * * *"               # 6am in user's timezone
conversation_token = "room123"    # Talk room to post to
output = "both"                   # "talk", "email", or "both"

  [[users.alice.briefings.blocks]]
  title = "Today"
  render_mode = "structured"

    [[users.alice.briefings.blocks.sources]]
    kind = "calendar"
    config = {}

    [[users.alice.briefings.blocks.sources]]
    kind = "todos"
    config = {}

  [[users.alice.briefings.blocks]]
  title = "World News"
  render_mode = "synthesis"

    [[users.alice.briefings.blocks.sources]]
    kind = "browse"
    config = { preset = "ap" }
```

## Output format

Claude returns structured JSON (`{"subject": "...", "body": "..."}`). The scheduler parses it and handles delivery deterministically. The email skill is excluded from briefing tasks via `exclude_skills` to prevent the model from sending emails directly.

## Scheduling

Cron expressions are evaluated in each user's timezone. The scheduler checks for due briefings every `briefing_check_interval` (60s) and tracks the last run per briefing in the `briefing_state` table.
