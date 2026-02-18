# Briefing Schedule Configuration

Users can control their own briefing schedules by creating a `BRIEFINGS.md` file in their `{BOT_DIR}/config/` directory.

## File location

```
/Users/{user_id}/{BOT_DIR}/config/BRIEFINGS.md
```

## Format

The file is a Markdown document with TOML configuration in a fenced code block:

```markdown
# Briefing Schedule

Your description here...

## Settings

` ` `toml
[[briefings]]
name = "morning"
cron = "0 6 * * *"           # cron expression in user's timezone
conversation_token = "abc123" # Talk room to post to
output = "talk"               # "talk", "email", or "both"

[briefings.components]
calendar = true
todos = true
markets = true    # morning → futures, evening → indices (automatic)
news = true       # expands using admin defaults (sources, lookback)
reminders = true

[[briefings]]
name = "evening"
cron = "0 18 * * *"
conversation_token = "abc123"
output = "talk"

[briefings.components]
calendar = true
markets = true
` ` `
```

(Remove spaces from the backticks above — they are shown with spaces to prevent parsing issues.)

## Component values

- **Boolean `true`**: Enables the component. `markets = true` automatically shows futures for morning briefings (pre-market) and index closes for evening briefings. `news = true` expands using admin-configured defaults (sources, lookback). Simple components (`calendar`, `todos`, `reminders`) stay as-is.
- **Dict**: Pass through unchanged, overriding defaults entirely.

Example with explicit dict (overrides defaults):
```toml
[briefings.components]
markets = { enabled = true, futures = ["ES=F", "NQ=F"] }  # force specific symbols
news = { enabled = true, lookback_hours = 6, sources = [
    { type = "domain", value = "example.com" }
]}
```

## Precedence

User BRIEFINGS.md overrides admin config at the briefing name level:
- If user config defines a briefing named "morning", it replaces the admin "morning" briefing
- Admin briefings not overridden by the user are preserved
- New briefings in user config are added

## Creating the file

Use standard file write operations to create or edit the file at the `{BOT_DIR}/config/` path. The scheduler reads it on each check cycle (typically every 60 seconds).
