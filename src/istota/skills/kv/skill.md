---
name: kv
description: Key-value store for persistent runtime state
---
# KV Store

Persistent key-value store scoped by user and namespace. Use this to store and retrieve runtime state (small JSON blobs).

## CLI

```bash
istota-skill kv get <namespace> <key>                  # Get a value
istota-skill kv set <namespace> <key> '<json_value>'   # Set a value (JSON)
istota-skill kv list <namespace>                       # List all keys in namespace
istota-skill kv delete <namespace> <key>               # Delete a key
istota-skill kv namespaces                             # List all namespaces
```

## Environment variables

| Variable | Description |
|---|---|
| `ISTOTA_DB_PATH` | Path to SQLite database (set automatically) |
| `ISTOTA_USER_ID` | Current user ID (set automatically) |
| `ISTOTA_DEFERRED_DIR` | Directory for deferred writes from sandbox |
| `ISTOTA_TASK_ID` | Current task ID (for deferred file naming) |

## Sandbox constraints

- **Reads** (`get`, `list`, `namespaces`) work directly — the DB is read-only accessible.
- **Writes** (`set`, `delete`) are deferred when running in the sandbox: the CLI writes a JSON file to `$ISTOTA_DEFERRED_DIR` and the scheduler processes it after task completion.

The CLI handles this automatically — use `set` and `delete` normally and they will be deferred transparently when `ISTOTA_DEFERRED_DIR` is set.

## Output format

All commands return JSON with a `status` field (`ok`, `not_found`, or `error`).

```json
{"status": "ok", "value": {"last_place": "Home"}}
{"status": "not_found"}
{"status": "ok", "count": 3, "entries": [...]}
{"status": "ok", "namespaces": ["briefing", "location"]}
{"status": "ok", "deferred": true}
```

## Notes

- Values must be valid JSON (strings, numbers, objects, arrays, booleans, null)
- KV store is the standard way to persist runtime state — prefer it over JSON files in `data/`
