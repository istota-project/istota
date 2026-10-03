# Conversation context

The context module (`context.py`) triages which previous messages to include in the prompt. This keeps token usage reasonable while preserving relevant history. One step of the algorithm below lives elsewhere: the recency window (step 2) is applied by `_apply_recency_window_talk` / `_apply_recency_window_db` in `executor.py`, before `context.py` sees the list.

## Selection algorithm

1. Fetch last `lookback_count` (25) messages for the conversation
2. Apply recency window: if `context_recency_hours` > 0, exclude messages older than the cutoff while always keeping at least `context_min_messages` (10)
3. If total <= `skip_selection_threshold` (3): include all, skip LLM selection
4. Most recent `always_include_recent` (5) messages are always included
5. Older messages are triaged by a fast model that returns which message IDs are relevant to the current request. Triage runs through the **task's own brain**, not a hardcoded `claude` CLI call: a `claude_code` task triages with Haiku via the CLI subprocess, a `native` task triages through its own provider's fast completer. If a completer can't be built (e.g. a native task with no API key), triage fails open — all older messages are kept rather than dropped
6. Selected older messages + guaranteed recent messages are combined in chronological order
7. On any triage error: **fail open** — keep every older message rather than dropping it. Losing context silently is the worse failure, so an unavailable triage costs tokens instead of history

Reply-to messages are force-included regardless of selection. Actions taken (tool use descriptions) are appended after bot responses so Claude can see what it did previously.

## Talk context

Talk tasks use a poller-fed local cache (`talk_messages` table) populated by the talk poller. This avoids redundant API calls. Bot responses are captured in the cache via `:result` reference IDs.

Cache size is bounded per conversation (`talk_cache_max_per_conversation`, default 200). The Talk context limit (`talk_context_limit`, default 100) controls how many messages are fetched from the Talk API.

## Email context

Email tasks use DB-based context from completed tasks matching the conversation token.

## Shared rooms

In a room more than one person reads, three things change. The full model is in [rooms and multi-user chat](rooms.md).

- **Unanswered turns are history too.** Every turn is recorded whether or not the speech gate answered it, so the history holds turns nobody addressed to the bot. An unanswered turn has no bot response after it.
- **Other people's turns are fenced.** Both history formatters take the task's user as the principal and wrap every other participant's turn as `ROOM PARTICIPANT MESSAGE`, including a guest or an outside correspondent stored with a label and no user id. The bot's own earlier answers are not fenced. In a room several people have written in, the `Replying to` quote is fenced as `QUOTED MESSAGE`.
- **History starts at the latest join.** When someone joins a room others already read, `db.front_stage_cutoff` returns the boundary, and every history read for that room starts after it: the DB and Talk fetches, the reply-to parent (a parent from before the boundary is not force-included), and recall over past turns. A member's side room reads the parent room's transcript whole.

## Configuration

All context settings live in the `[conversation]` section:

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Enable conversation context |
| `lookback_count` | 25 | Messages to consider |
| `skip_selection_threshold` | 3 | Include all if history is this small |
| `selection_model` | `"fast"` | Role alias for relevance matching, resolves to Haiku by default (resolved through the active brain; under `native` the brain's own fast completer is used) |
| `selection_timeout` | 30.0 | Timeout for LLM selection |
| `use_selection` | `true` | Use LLM selection (false = include all) |
| `always_include_recent` | 5 | Always include this many recent messages |
| `context_truncation` | 0 | Max chars per bot response (0 = no limit) |
| `context_recency_hours` | 0 | Exclude messages older than this (0 = disabled) |
| `context_min_messages` | 10 | Minimum messages to keep when recency filtering |
| `previous_tasks_count` | 3 | Recent unfiltered tasks to inject |
| `talk_context_limit` | 100 | Messages to fetch from Talk API |

## Background task exclusion

Scheduled and briefing task results are excluded from interactive conversation context. This prevents cron job output from cluttering a user's chat history. The `previous_tasks_count` setting ensures scheduled/briefing messages aren't completely orphaned when a user replies to them.
