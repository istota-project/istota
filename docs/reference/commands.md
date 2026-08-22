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
| `!usage` | Show token usage; adds a by-brain split and the Claude Code plan's rate-limit windows for admins (hidden alias: `!limits`) |
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
| `!trust [EMAIL]` | List trusted email addresses, or add one — trust runs in both directions (see below) |
| `!untrust EMAIL` | Remove a runtime trusted email address, in both directions |
| `!drafts` | List outbound mail held for your approval, with ids |
| `!drafts send ID` | Release one held draft (id optional when exactly one is waiting) |
| `!drafts discard ID` | Bin one held draft |

## Steering a running task

`!steer <text>` (alias `!inject`) is the additive counterpart to `!stop`: instead of cancelling, the note reaches the model at its next loop boundary as an extra user turn, so it adjusts course without discarding work in progress. The steer is written to the room transcript (display-only) and to the running task's live event log, so a reconnecting web client sees that it landed.

It is room-scoped — it targets your most recent `running`/`locked` task in the room you send it from. A task awaiting confirmation is not steerable (reply normally instead). Steering needs a brain that can accept a mid-run turn: only the **native** brain is wired for it today, and `!steer` refuses with an explanation for `claude_code` (its stdin is closed once the prompt is sent) and `tmux_claude`. At most 10 pending steers may queue on one task.

## Retrying a failed task

`!retry` re-runs the most recent `failed` or `cancelled` task in the room from scratch; `!resume` re-runs it with the failed attempt's captured execution trace (tool calls and intermediate text) prepended, framed as "continue from where you left off". Both accept an explicit `!retry #1234` / `!resume #1234`. Own tasks only, unless you are an admin.

Each creates a **new** task rather than re-queueing the old row, so the failed attempt stays intact in history and out of the automatic backoff. Delivery fields (`output_target`, model, effort, skill) are copied; attachments are not. A task that is still running is rejected (`!stop` it first), as is one that already completed. `!resume` degrades to `!retry` with a note when no usable trace was captured.

## Usage and plan limits

`!usage` reports what the deployment has been consuming, in up to three blocks. Everyone gets a **Token usage** block, headed `fleet` for an admin and with your own user id for anyone else, carrying rows, tokens, cache hit rate and cost over the last 24 hours and the last 30 days. The other two blocks are admin-only: **By brain**, the 30-day split that answers which brain is spending, and **Claude Code subscription**, the plan's rate-limit windows drawn as 20-character bars with their reset times. A non-admin's Token usage block is filtered to their own rows and the other two are simply absent, with nothing saying they were withheld, the same way `!status` omits its `**System:**` block. The subscription is one credential for the whole deployment, so on a multi-user install the fleet totals and the plan are the operator's business.

The plan block is the answer to the dash in the cost column above it. A currency figure appears only where the cost is real money (see [cost basis](../features/usage.md#cost-basis)), so on a subscription deployment `!usage` reports tokens and no dollars, and the windows underneath supply the budget that column cannot. They render from the same cached reading as the `/admin` card and the `runtime.subscription_usage` doctor check — the handler issues no request of its own, and within the cache TTL no request happens at all. Reset times are absolute and in your own timezone, resolved live from your profile rather than from the zone the daemon booted with; a missing or unreadable timezone falls back to UTC and the line says so. An `Extra usage:` line appears only when pay-as-you-go credits are enabled on the account, and a `_Reading is … old._` footer only when the number is not current.

That block appears when a reading is available, not when `brain.kind` is `claude_code`. A `native` deployment with a `claude_code` fallback burns the same plan and is the case that most needs the number, and a `source_type_overrides` entry makes the configured brain a poor proxy in the other direction too. With no reading the block is omitted silently: a chat reply is not where an unreachable diagnostic endpoint gets reported, and `istota doctor` says so properly. `!limits` is a hidden alias for the whole command rather than for that one block — it runs the same handler and filters nothing, because the token totals directly above the windows are half the answer to "how much headroom is left".

## Room model default

`!room` shows the room's standing model/effort default; `!room model <alias>` and `!room effort <level>` set it, and `default` clears it. The default lives on the shared room registry, so it applies to every message in that room on both Talk and web, and to every participant. Precedence: an inline `!model` prefix wins over the room default, which wins over the instance `model` config.

## Model override prefix

`!model <alias> <prompt>` is a per-task model override parsed before task creation on every surface (Talk and web alike). It is not a `!command` — it resolves the alias, sets the model (and optionally effort) on the task row, and passes the remaining text as the prompt. If the alias is unknown, it replies with usage help instead of creating a task.

Aliases are base names: role tiers (`fast`, `general`, `smart`, plus any operator-defined custom aliases from `[models.aliases]`), provider shortcuts (`opus`, `sonnet`, `haiku`), and `default`. Bare `opus` resolves to the current-latest Opus. Effort is an orthogonal `:effort` modifier appended to any name — `opus:high`, `smart:low`, `claude-opus-5:xhigh`, with `:effort` ∈ `low|medium|high|xhigh|max`. A prior-version pin is the canonical id plus the modifier (`claude-opus-4-7:high`). Use `!models` to see the resolved alias table.

## Export

`!export` creates a conversation history file in `{bot_dir}/exports/conversations/`. First run exports all messages; subsequent runs incrementally append new messages.

Formats: `markdown` (default) or `text`.

## Trust

`!trust` manages runtime trusted email addresses (stored in the database, checked alongside config-time `trusted_email_senders` patterns). One list, two meanings: mail **from** a trusted address is processed without asking, and mail **to** it is sent without waiting for your approval under the `untrusted` outbound policy. `!untrust` reverses both. See [email](../features/email.md) for the full confirmation gate flow and [what trusting a sender means](../features/email.md#what-trusting-a-sender-means).

## Drafts

`!drafts` is the composer-surface half of the [outbound approval gate](../features/email.md#the-outbound-approval-gate) — mail to a recipient you have not authorized is held as an editable draft rather than sent. `!drafts` lists what is waiting with ids and recipients (Bcc by count, not by address, since the reply may land in a shared room), `!drafts send <id>` releases one and `!drafts discard <id>` bins it. With exactly one draft pending the id may be omitted; with several it is required.

The command lists `pending` rows only. A draft left marked `sending` — the process died between claiming it and recording the send — is terminal and appears on the web surface alone, shown with no action offered because nobody can tell from the outside whether the mail went out. Web chat shows the same drafts as cards; see [answering a held draft](../features/email.md#answering-a-held-draft).

## Search

`!search` queries the memory search index and Talk API for matching messages. Returns results with timestamps and conversation context.

Flags: `--since YYYY-MM-DD`, `--week`, `--memories` (memory files only), `--all` (search every room, not just this one), `--room <token>` (scope to one room; a leading `#` is stripped). Anything else is treated as query text.
