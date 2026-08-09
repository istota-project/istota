# Commands

Commands prefixed with `!` are intercepted before task creation and handled synchronously — no Claude Code invocation; they execute immediately. They are **surface-agnostic**: the same set works in Nextcloud Talk, web chat, and the CLI. `commands.dispatch(...)` runs each handler over a `CommandContext` and delivers the result via the resolved transport. On a push surface (Talk) the result is delivered as a new message; on a stream surface (web) it is returned inline and rendered as a text card.

## Available commands

| Command | Description |
|---|---|
| `!help` | List all available commands |
| `!stop` | Cancel the active task (sets `cancel_requested` flag + SIGTERM to worker) |
| `!steer TEXT` | Send a note to the running task without restarting it (alias: `!inject`) |
| `!retry [#ID]` | Re-run a failed or cancelled task from scratch |
| `!resume [#ID]` | Re-run a failed or cancelled task, continuing from its captured progress |
| `!status` | Show running/pending tasks and system stats |
| `!room` | Show this room's standing model/effort default; `!room model ALIAS` / `!room effort LEVEL` set it, `default` clears |
| `!memory user` | Show USER.md contents |
| `!memory channel` | Show CHANNEL.md contents |
| `!memory facts [ENTITY]` | Show knowledge graph facts, optionally filtered to one subject |
| `!models` | List available model aliases and what they resolve to |
| `!cron` | List scheduled jobs with status |
| `!cron enable NAME` | Re-enable a disabled job |
| `!cron disable NAME` | Disable a job |
| `!check` | Run an inline system health check: model binary, bwrap, DB connectivity, recent-task stats, and a live sandboxed execution test |
| `!export [markdown\|text]` | Export conversation history to a file |
| `!skills` | List available skills (grouped: available, unavailable, disabled) |
| `!skills NAME` | Show details for a specific skill |
| `!more #TASK_ID` | Show execution trace for a completed task |
| `!search QUERY` | Search conversation history via memory index + Talk API |
| `!trust [EMAIL]` | List trusted email senders, or add one |
| `!untrust EMAIL` | Remove a runtime trusted email sender |

## Steering a running task

`!steer <text>` (alias `!inject`) is the additive counterpart to `!stop`: instead of cancelling, the note reaches the model at its next loop boundary as an extra user turn, so it adjusts course without discarding work in progress. The steer is written to the room transcript (display-only) and to the running task's live event log, so a reconnecting web client sees that it landed.

It is room-scoped — it targets your most recent `running`/`locked` task in the room you send it from. A task awaiting confirmation is not steerable (reply normally instead). Steering needs a brain that can accept a mid-run turn: only the **native** brain is wired for it today, and `!steer` refuses with an explanation for `claude_code` (its stdin is closed once the prompt is sent) and `tmux_claude`. At most 10 pending steers may queue on one task.

## Retrying a failed task

`!retry` re-runs the most recent `failed` or `cancelled` task in the room from scratch; `!resume` re-runs it with the failed attempt's captured execution trace (tool calls and intermediate text) prepended, framed as "continue from where you left off". Both accept an explicit `!retry #1234` / `!resume #1234`. Own tasks only, unless you are an admin.

Each creates a **new** task rather than re-queueing the old row, so the failed attempt stays intact in history and out of the automatic backoff. Delivery fields (`output_target`, model, effort, skill) are copied; attachments are not. A task that is still running is rejected (`!stop` it first), as is one that already completed. `!resume` degrades to `!retry` with a note when no usable trace was captured.

## Room model default

`!room` shows the room's standing model/effort default; `!room model <alias>` and `!room effort <level>` set it, and `default` clears it. The default lives on the shared room registry, so it applies to every message in that room on both Talk and web, and to every participant. Precedence: an inline `!model` prefix wins over the room default, which wins over the instance `model` config.

## Model override prefix

`!model <alias> <prompt>` is a per-task model override parsed before task creation on every surface (Talk and web alike). It is not a `!command` — it resolves the alias, sets the model (and optionally effort) on the task row, and passes the remaining text as the prompt. If the alias is unknown, it replies with usage help instead of creating a task.

Aliases are base names: role tiers (`fast`, `general`, `smart`, plus any operator-defined custom aliases from `[models.aliases]`), provider shortcuts (`opus`, `sonnet`, `haiku`), and `default`. Bare `opus` resolves to the current-latest Opus. Effort is an orthogonal `:effort` modifier appended to any name — `opus:high`, `smart:low`, `claude-opus-5:xhigh`, with `:effort` ∈ `low|medium|high|xhigh|max`. A prior-version pin is the canonical id plus the modifier (`claude-opus-4-7:high`). Use `!models` to see the resolved alias table.

## Export

`!export` creates a conversation history file in `{bot_dir}/exports/conversations/`. First run exports all messages; subsequent runs incrementally append new messages.

Formats: `markdown` (default) or `text`.

## Trust

`!trust` manages runtime trusted email senders (stored in the database, checked alongside config-time `trusted_email_senders` patterns). See [email](../features/email.md) for the full confirmation gate flow.

## Search

`!search` queries the memory search index and Talk API for matching messages. Returns results with timestamps and conversation context.

Flags: `--since YYYY-MM-DD`, `--week`, `--memories` (memory files only), `--all` (search every room, not just this one). A bare `#TOKEN` argument scopes the search to that room.
