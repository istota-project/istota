# Conversation context

The context module (`context.py`) selects which previous messages to include in the prompt. This keeps token usage reasonable while preserving relevant history.

## Selection algorithm

1. Fetch last `lookback_count` (25) messages for the conversation
2. Apply recency window: if `context_recency_hours` > 0, exclude messages older than the cutoff while always keeping at least `context_min_messages` (10)
3. If total <= `skip_selection_threshold` (3): include all, skip LLM selection
4. Most recent `always_include_recent` (5) messages are always included
5. Older messages are triaged by a fast model (Haiku via Claude CLI subprocess) that returns which message IDs are relevant to the current request
6. Selected older messages + guaranteed recent messages are combined in chronological order
7. On any error: fall back to guaranteed recent messages only

Reply-to messages are force-included regardless of selection. Actions taken (tool use descriptions) are appended after bot responses so Claude can see what it did previously.

## Talk context

Talk tasks use a poller-fed local cache (`talk_messages` table) populated by the talk poller. This avoids redundant API calls. Bot responses are captured in the cache via `:result` reference IDs.

Cache size is bounded per conversation (`talk_cache_max_per_conversation`, default 200). The Talk context limit (`talk_context_limit`, default 100) controls how many messages are fetched from the Talk API.

## Email context

Email tasks use DB-based context from completed tasks matching the conversation token.

## Configuration

All context settings live in the `[conversation]` section:

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Enable conversation context |
| `lookback_count` | 25 | Messages to consider |
| `skip_selection_threshold` | 3 | Include all if history is this small |
| `selection_model` | `"haiku"` | Model for relevance matching |
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
