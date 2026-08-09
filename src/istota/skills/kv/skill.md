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
istota-skill kv set <ns> <key> --value-file <path>     # Set from a file (large values)
istota-skill kv list <namespace> [--keys-only] [--max-value-chars N]
istota-skill kv delete <namespace> <key>               # Delete a key
istota-skill kv namespaces                             # List all namespaces

# Set ops — operate on a JSON-array value at <ns>/<key>, members are plain strings:
istota-skill kv set-contains <ns> <key> <member> [<member>...]    # batched — see below
istota-skill kv set-size     <ns> <key>                     # {"size": N}
istota-skill kv set-members  <ns> <key> [--limit N] [--offset N]  # paginated slice
istota-skill kv set-add      <ns> <key> <member> [<member>...]    # bootstraps [] if missing
istota-skill kv set-remove   <ns> <key> <member> [<member>...]
istota-skill kv set-trim     <ns> <key> --keep-newest N     # drop all but the newest N
```

### Value size: the store is unlimited, a command argument is not

`istota_kv.value` is plain `TEXT` with no constraint, so a value is bounded
only by SQLite (about 10⁹ bytes). The real ceiling is the command line: Linux
caps a **single argv element** at 32 × page size — **128 KiB** on an ordinary
4 KiB-page host — and `execve` fails with `E2BIG` above it. A `kv set <ns>
<key> '<blob>'` passes the whole value as one element, so an oversized write
dies before any code runs, at a size nothing reports to you.

That cap applies only to writes made *through an argument*. `get` returns the
value on stdout, which has no such limit, and `set-add` appends without ever
passing the accumulated array. So a value grown past 128 KiB by repeated
`set-add` reads back fine but can no longer be rewritten wholesale by a plain
`kv set`.

What follows from this:

- **A collection that grows without bound is maintained with the set ops**, not
  with `get` + `set`. That is the rule, not a performance suggestion.
- **For a large whole value** — a document, a generated report — use `kv set
  --value-file <path>`. Write the file into your own workspace, the current
  channel's directory or `$ISTOTA_DEFERRED_DIR` first; anywhere else on the
  host is refused, symlinks included, and the file is capped at 8 MiB.
- **Bound the collection** with `set-trim --keep-newest N` if it only needs
  recent history. Members are ordered oldest-first, so "newest N" is the tail.
  There is no TTL: the store keeps no per-member timestamp, so trimming is by
  count. If you need age-based expiry, store the timestamp in the member
  string yourself.

### Batch your membership checks

`set-contains` takes many members at once, and you should give it all of them:

```bash
istota-skill kv set-contains warsaw seen_ids id-a id-b id-c
# {"status":"ok","contains":{"id-a":true,...},"batched":true,"present":1,"missing":2}
```

One member keeps the scalar form (`{"contains": true, "batched": false}`); two
or more return the map above. Read `batched` rather than guessing from how many
members you passed — a list built from a variable-length collection gives you
the scalar exactly when it happens to hold one item. Checking 50 items one at a time means 50 process spawns and 50 full
parses of the same array — the parse is the cost, not the storage. `set-add`
and `set-remove` batch the same way, and `set-add` is already idempotent, so
the common check-then-add loop usually collapses into a single `set-add`: pass
every candidate and read `added` to learn how many were new.

The deferred apply re-reads the current value, so concurrent set ops across
tasks compose correctly.

### `list` previews values by default

`list` is the natural command for orienting in a namespace, so it does not dump
every value in full: each is truncated to 2048 characters with `"truncated":
true` and a `value_chars` count of the real length. Use `--keys-only` to see
just the keys and their sizes, `--max-value-chars 0` to get them whole, or
`get` for a specific entry — `get` and `set-members` are never truncated.

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
- **Set-ops (`set-add`/`set-remove`/`set-trim`/…) reject `--shared`** — shared
  content is written as a whole value, not incremental set membership.
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
| `ISTOTA_DB_PATH` | Path to the framework database. Set automatically **for the CLI only** — it is withheld from the task environment, because the file it names is not in the sandbox |
| `ISTOTA_USER_ID` | Current user ID (set automatically) |
| `ISTOTA_DEFERRED_DIR` | Directory for deferred writes from sandbox |
| `ISTOTA_TASK_ID` | Current task ID (for deferred file naming) |

## Sandbox constraints

- **Reads** (`get`, `list`, `namespaces`, `set-contains`, `set-size`, `set-members`) return a value on the spot, `--shared` reads included, and they work for every user — the CLI runs outside the sandbox and scopes each query to you. There is no file to fall back to: the database directories are masked out of the sandbox, so `sqlite3` and Python's `sqlite3` have nothing to open.
- **Writes** (`set`, `delete`, `set-add`, `set-remove`, `set-trim`) are deferred when running in the sandbox: the CLI writes a JSON file to `$ISTOTA_DEFERRED_DIR` and the scheduler processes it after task completion. A `--shared` write carries the shared scope in the deferred op; the scheduler applies it only if your task's identity is an admin (fail-closed).
- **`--value-file` reads the file host-side**, where this CLI runs — not inside the sandbox. Both places see the same paths, so write the file into your workspace, the current channel's directory or `$ISTOTA_DEFERRED_DIR` and pass that path. Anywhere else on the host is refused — including another user's workspace, which the host side can see and the sandbox cannot — and so is a symlink.

The CLI handles this automatically — use the write commands normally and they will be deferred transparently when `ISTOTA_DEFERRED_DIR` is set.

## Output format

All commands return JSON with a `status` field (`ok`, `not_found`, or `error`).

```json
{"status": "ok", "value": {"last_place": "Home"}}
{"status": "not_found"}
{"status": "ok", "count": 3, "truncated_count": 1, "entries": [...]}
{"status": "ok", "namespaces": ["briefing", "location"]}
{"status": "ok", "deferred": true}
{"status": "ok", "contains": true, "batched": false}
{"status": "ok", "contains": {"id-a": true, "id-b": false}, "batched": true, "present": 1, "missing": 1}
{"status": "ok", "size": 1417}
{"status": "ok", "total": 1417, "offset": 0, "members": ["id-1", "id-2", ...]}
{"status": "ok", "added": 2, "deferred": true}
{"status": "ok", "removed": 1, "deferred": true}
{"status": "ok", "removed": 900, "size": 100, "deferred": true}
```

A `list` entry carries `value_chars` (the value's real length) and, when it was
shortened, `"truncated": true` with `value` as a string preview rather than the
decoded JSON. With `--keys-only` the `value` field is absent entirely.

## Notes

- Values must be valid JSON (strings, numbers, objects, arrays, booleans, null)
- KV store is the standard way to persist runtime state — prefer it over JSON files in `data/`
- Do not store secrets (passwords, tokens, API keys) here — use the encrypted `secrets` table via `istota secret`.
- Do not store quantitative health data (measurements, biomarker / lab values, medication doses, current symptoms). That belongs in the `health` module's per-user DB; query it on demand via `istota-skill health latest` / `health trend`.
