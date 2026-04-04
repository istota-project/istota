# Briefings

Scheduled summaries delivered to Talk and/or email. A morning briefing might include today's calendar events, market futures, headline news, and pending todos.

## Configuration sources

Briefing config is loaded from three sources (higher precedence wins):

1. User workspace `BRIEFINGS.md` (Nextcloud file, user self-service)
2. Per-user config TOML (`config/users/alice.toml`)
3. Main config (`config/config.toml` under `[[users.alice.briefings]]`)

Merging happens at the briefing name level.

## Components

| Component | What it includes |
|---|---|
| `calendar` | Today's events (morning) or tomorrow's events (evening) |
| `todos` | Pending TODO items from todo_file resources |
| `email` | General unread email summary / newsletter content |
| `markets` | Pre-fetched futures (morning) and indices (evening) via yfinance |
| `headlines` | Pre-fetched frontpages from major news outlets |
| `news` | Newsletter summaries from configured email sources |
| `notes` | Recent notes summary |
| `reminders` | Random item from user's REMINDERS file |

Market data and newsletter IDs are pre-fetched before Claude execution so the model doesn't need API access for data collection.

## Memory isolation

Briefing prompts intentionally exclude USER.md and dated memories. This prevents private context from leaking into what may be shared or newsletter-style output.

## Example config (TOML)

```toml
[[users.alice.briefings]]
name = "morning"
cron = "0 6 * * *"               # 6am in user's timezone
conversation_token = "room123"    # Talk room to post to
output = "both"                   # "talk", "email", or "both"

[users.alice.briefings.components]
calendar = true
todos = true
markets = true                    # auto-selects futures (morning) or indices (evening)
news = true                       # expands using [briefing_defaults.news] sources
reminders = { enabled = true }
```

## Boolean expansion

Setting `markets = true` or `news = true` in BRIEFINGS.md expands using admin-configured `[briefing_defaults]`:

```toml
[briefing_defaults.news]
lookback_hours = 12
sources = [
    { type = "domain", value = "semafor.com" },
    { type = "email", value = "briefing@nytimes.com" },
]

[briefing_defaults.headlines]
sources = ["ap", "reuters", "guardian", "ft", "aljazeera", "lemonde", "spiegel"]
```

## Output format

Claude returns structured JSON (`{"subject": "...", "body": "..."}`). The scheduler parses it and handles delivery deterministically. The email skill is excluded from briefing tasks via `exclude_skills` to prevent the model from sending emails directly.

## Scheduling

Cron expressions are evaluated in each user's timezone. The scheduler checks for due briefings every `briefing_check_interval` (60s) and tracks the last run per briefing in the `briefing_state` table.
