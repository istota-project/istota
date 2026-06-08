# Commands

Commands prefixed with `!` are intercepted before task creation and handled synchronously — no Claude Code invocation; they execute immediately. They are **surface-agnostic**: the same set works in Nextcloud Talk, web chat, and the CLI. `commands.dispatch(...)` runs each handler over a `CommandContext` and delivers the result via the resolved transport. On a push surface (Talk) the result is delivered as a new message; on a stream surface (web) it is returned inline and rendered as a text card.

## Available commands

| Command | Description |
|---|---|
| `!help` | List all available commands |
| `!stop` | Cancel the active task (sets `cancel_requested` flag + SIGTERM to worker) |
| `!status` | Show running/pending tasks and system stats |
| `!memory user` | Show USER.md contents |
| `!memory channel` | Show CHANNEL.md contents |
| `!memory facts` | Show knowledge graph facts |
| `!models` | List available model aliases and what they resolve to |
| `!cron` | List scheduled jobs with status |
| `!cron enable NAME` | Re-enable a disabled job |
| `!cron disable NAME` | Disable a job |
| `!check` | Run system health check (self-check heartbeat) |
| `!export [markdown\|text]` | Export conversation history to a file |
| `!skills` | List available skills (grouped: available, unavailable, disabled) |
| `!skills NAME` | Show details for a specific skill |
| `!more #TASK_ID` | Show execution trace for a completed task |
| `!search QUERY` | Search conversation history via memory index + Talk API |
| `!trust [EMAIL]` | List trusted email senders, or add one |
| `!untrust EMAIL` | Remove a runtime trusted email sender |

## Model override prefix

`!model <alias> <prompt>` is a per-task model override parsed before task creation on every surface (Talk and web alike). It is not a `!command` — it resolves the alias, sets the model (and optionally effort) on the task row, and passes the remaining text as the prompt. If the alias is unknown, it replies with usage help instead of creating a task.

Aliases include role names (`fast`, `general`, `smart`, plus any operator-defined custom roles from `[models.roles]`), provider aliases (`opus`, `opus-high`, `opus-xhigh`, `opus-max`, `opus-47`, `opus-47-high`, `opus-46`, `opus-46-high`, `sonnet`, `sonnet-high`, `haiku`), and `default`. Bare `opus` resolves to the current-latest Opus. Use `!models` to see the resolved alias table.

## Export

`!export` creates a conversation history file in `{bot_dir}/exports/conversations/`. First run exports all messages; subsequent runs incrementally append new messages.

Formats: `markdown` (default) or `text`.

## Trust

`!trust` manages runtime trusted email senders (stored in the database, checked alongside config-time `trusted_email_senders` patterns). See [email](../features/email.md) for the full confirmation gate flow.

## Search

`!search` queries the memory search index and Talk API for matching messages. Returns results with timestamps and conversation context.

Flags: `--since YYYY-MM-DD`, `--week`, `--memories` (memory files only).
