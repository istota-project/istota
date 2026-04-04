# Talk commands

Commands prefixed with `!` are intercepted in the Talk poller before task creation and handled synchronously. No Claude Code invocation -- they execute immediately.

## Available commands

| Command | Description |
|---|---|
| `!help` | List all available commands |
| `!stop` | Cancel the active task (sets `cancel_requested` flag + SIGTERM to worker) |
| `!status` | Show running/pending tasks and system stats |
| `!memory user` | Show USER.md contents |
| `!memory channel` | Show CHANNEL.md contents |
| `!cron` | List scheduled jobs with status |
| `!cron enable NAME` | Re-enable a disabled job |
| `!cron disable NAME` | Disable a job |
| `!check` | Run system health check (self-check heartbeat) |
| `!export [markdown\|text]` | Export conversation history to a file |
| `!skills` | List available skills (grouped: available, unavailable, disabled) |
| `!skills NAME` | Show details for a specific skill |
| `!more #TASK_ID` | Show execution trace for a completed task |
| `!search QUERY` | Search conversation history via memory index + Talk API |

## Export

`!export` creates a conversation history file in `{bot_dir}/exports/conversations/`. First run exports all messages; subsequent runs incrementally append new messages.

Formats: `markdown` (default) or `text`.

## Search

`!search` queries the memory search index and Talk API for matching messages. Returns results with timestamps and conversation context.
