---
name: kv
description: Key-value store for persistent runtime state
always_include: true
cli: true
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

# Set ops — operate on a JSON-array value at <ns>/<key>, members are plain strings:
istota-skill kv set-contains <ns> <key> <member>            # {"contains": bool}
istota-skill kv set-size     <ns> <key>                     # {"size": N}
istota-skill kv set-members  <ns> <key> [--limit N] [--offset N]  # paginated slice
istota-skill kv set-add      <ns> <key> <member> [<member>...]    # bootstraps [] if missing
istota-skill kv set-remove   <ns> <key> <member> [<member>...]
```

Use the set ops for membership-tracking patterns (seen IDs, processed hashes,
etc.) instead of round-tripping the full array through `get` — a 40 KB blob
choked one task in production. `set-add` / `set-remove` accept multiple
members in a single call. The deferred apply re-reads the current value, so
concurrent set-adds across tasks compose correctly.

## Shared (cross-user) store — `--shared`

The per-user store above is private to you. A separate **shared** store lets one
identity publish content that *other* users read — used for curated briefing
content (world headlines, a markets summary, a newsletter digest) that would
otherwise be regenerated per user.

```bash
istota-skill kv get       <ns> <key> --shared   # open to any user
istota-skill kv list      <ns> --shared
istota-skill kv namespaces --shared
istota-skill kv set       <ns> <key> '<json>' --shared   # admin-only
istota-skill kv delete    <ns> <key> --shared            # admin-only

istota-skill kv shared-status   # can I write shared KV on this deployment?
```

- **Reads are open** to any user. **Writes are admin-only** — content flows into
  other users' prompts, so it must come from a trusted identity. A non-admin
  write returns `{"status":"error","error":"shared KV writes require admin"}`
  and exits non-zero. On a deployment with a *blank* admins file no one can
  write (fail-closed).
- **Check before you wire.** Whether *you* may write shared content is
  deployment-specific (it depends on the admins allowlist, which differs per
  install). Run `istota-skill kv shared-status` to find out — don't infer it
  from being an admin generally. It returns
  `{"status":"ok","user_id":…,"can_write_shared":true|false,"admins_configured":true|false}`.
  The gate is deliberately *not* the same as admin status: a blank admins file
  makes everyone an admin but authorizes **nobody** to write shared KV
  (fail-closed). Use this before adding a `publish_shared_kv` scheduled job or a
  `--shared` write, so a job that can never publish isn't wired up.
- **Set-ops (`set-add`/`set-remove`/…) reject `--shared`** — shared content is
  written as a whole value, not incremental set membership.
- **Value shape controls briefing granularity** when a briefing `kv` source
  reads the entry:
  - `{"items": [{"title","summary","url"}, …]}` (or a bare JSON list) → each
    reader's briefing **synthesizes** the items (share the fetch, not the prose).
  - `{"text": "…"}` (or a bare JSON string) → the section text is **spliced**
    near-verbatim into a `structured` block (share the synthesis too).
  Prefer `{"text": …}` for a finished section, `{"items": […]}` for raw material.

## Environment variables

| Variable | Description |
|---|---|
| `ISTOTA_DB_PATH` | Path to SQLite database (set automatically) |
| `ISTOTA_USER_ID` | Current user ID (set automatically) |
| `ISTOTA_DEFERRED_DIR` | Directory for deferred writes from sandbox |
| `ISTOTA_TASK_ID` | Current task ID (for deferred file naming) |

## Sandbox constraints

- **Reads** (`get`, `list`, `namespaces`, `set-contains`, `set-size`, `set-members`) work directly — the DB is read-only accessible. `--shared` reads work the same way.
- **Writes** (`set`, `delete`, `set-add`, `set-remove`) are deferred when running in the sandbox: the CLI writes a JSON file to `$ISTOTA_DEFERRED_DIR` and the scheduler processes it after task completion. A `--shared` write carries the shared scope in the deferred op; the scheduler applies it only if your task's identity is an admin (fail-closed).

The CLI handles this automatically — use the write commands normally and they will be deferred transparently when `ISTOTA_DEFERRED_DIR` is set.

## Output format

All commands return JSON with a `status` field (`ok`, `not_found`, or `error`).

```json
{"status": "ok", "value": {"last_place": "Home"}}
{"status": "not_found"}
{"status": "ok", "count": 3, "entries": [...]}
{"status": "ok", "namespaces": ["briefing", "location"]}
{"status": "ok", "deferred": true}
{"status": "ok", "contains": true}
{"status": "ok", "size": 1417}
{"status": "ok", "total": 1417, "offset": 0, "members": ["id-1", "id-2", ...]}
{"status": "ok", "added": 2, "deferred": true}
{"status": "ok", "removed": 1, "deferred": true}
```

## Notes

- Values must be valid JSON (strings, numbers, objects, arrays, booleans, null)
- KV store is the standard way to persist runtime state — prefer it over JSON files in `data/`
- Do not store secrets (passwords, tokens, API keys) here — use the encrypted `secrets` table via `istota secret`.
- Do not store quantitative health data (measurements, biomarker / lab values, medication doses, current symptoms). That belongs in the `health` module's per-user DB; query it on demand via `istota-skill health latest` / `health trend`.
