---
paths:
  - "web/src/lib/stores/chat.ts"
  - "web/src/lib/components/chat/**"
  - "src/istota/transport/web/**"
  - "src/istota/web_app.py"
---

# Web chat surface

The in-app chat surface. Inbound/outbound plumbing is in `.claude/rules/transport.md` (`WebTransport`); this file is the surface: rooms, composer, drafts, send durability, message actions, the room-event stream.

## Overview

Console at `/chat`. Rooms are per-user channel tokens in `web_chat_rooms`, each with `CHANNEL.md` and sleep-cycle handling. A send becomes a `source_type="web"` task with `output_target="web"`, a stream surface: no push, result and progress in `task_events`, tailed by `/api/chat/tasks/{id}/stream`. Web is also a delivery surface (ISSUE-121): alerts, the execution log and routed notifications become `role='system'` rows in `messages`. Knobs under `[web.chat]`; engine `web/src/lib/stores/chat.ts`, widgets `web/src/lib/components/chat/`.

### Endpoints and rooms

- **Room delete** (`DELETE /chat/rooms/{id}`): hard token-scoped cascade via `db.delete_web_chat_room`, 409 via `count_active_web_tasks`, best-effort `Channels/<token>/` removal; channel `memory_chunks` are a residual. Only for rooms with no Talk conversation: `web_app._is_talk_backed` (reads the `talk` binding, not `origin`, ISSUE-408, else a promoted room lost its `room_dismissals` and came back) sends Talk-backed rooms to the per-user hide (ISSUE-134). It never probes Nextcloud; "Reconnect to Talk" (ISSUE-401) repairs a dead conversation.
- **`GET /chat/files?path=`**: session-scoped file handover, since web has no outbound attachments and a share link is for other people. `_resolve_chat_file`: `resolve_scoped_path` (the skill CLI's rule) plus a `realpath` pass; `is_admin=False` always. Default `attachment` + `nosniff` (workspace HTML/SVG would run on the app origin). `image_sniff.sniff_raster` admits PNG/JPEG/GIF/WebP by signature, never extension, served `inline` with the sniffed type, `nosniff`, and `Content-Security-Policy: default-src 'none'; sandbox`. The head is read after the resolve, so refused paths are never opened. rclone → 503 naming share links. The model learns it from `config/guidelines/web.md` "Handing over a file".
- **Room settings** (`RoomSettings.svelte`): rename (token invariant), model/effort default, copy token, delete behind type-the-name.
- **Per-room model default** on the shared `rooms` registry (surface-independent). `transport.ingest.record_inbound` applies it absent an inline `!model` (which wins as a unit). Precedence: inline > room > `config.model`. Set via `!room model|effort` (`default` clears) or the modal (validated, `db.set_room_model_effort`). Room-global.
- **Room colour** (ISSUE-433): no `!room` subcommand, because Talk has no colour and the value is per-user on the `web_chat_rooms` handle. `PATCH /chat/rooms/{id}` is the only writer. Fixed named palette (`src/istota/room_colors.py`, `web/src/lib/roomColors.ts`, `tokens.css`) since every colour needs both themes and the linter cannot see DB hexes; the route refuses others; `tests/test_room_colors.py` holds the three in step.
- **Per-room brain** (`rooms.brain`): admin-only write (`config.is_admin`, keyed on presence of `brain` so a clear is gated; `room.user_id` is handle ownership, not the gate). Choices are `[brain] room_selectable`, control absent when empty, no failover for a pinned room. `selectable_brains` is filtered server-side.
- **Brain and model in one body** (ISSUE-417, reversing the original): `_chat_update_room` applies the brain first, runs `commands._clear_pin_across_namespaces` against the old pin, then writes the body's choices. The model is validated against the incoming brain; unrunnable is a 400 (a client bug). 400 on the combination was rejected. `cleared` reports losses minus what the body supplied (including `effort`); the store strips it and raises a notice; it covers a brain changed elsewhere mid-edit. See `.claude/rules/brain.md`.
- **Deep link** `/chat?room=<token>` (`selectRoomByToken`, silent fallback); `&task=<id>` runs `jumpToTask` (up to 5 older pages, scroll to `data-cid`, highlight).
- **`!search`**: `cmd_search` returns `command_data` `kind="search_results"` via `CommandContext.result_data`, rendered by `SearchResults.svelte`; Talk ignores `data`. `commands._search_memory` covers `channel:{token}` plus memory sources, classifies on `is_memory`, recovers rooms via `db.get_message_room_for_task`. A strict prefix pass, scope filter, then one OR retry only if the scoped result is empty. Executor recall and the skill CLI keep strict AND.
- **Commands**: `commands.dispatch(... surface=...)` pushes on Talk, returns `inline_result` on web. Rate limit counts `source_type='web'` tasks.

### `GET /chat/commands` and autocomplete

`!` opens a filtered dropdown; `!model ` completes aliases. Engine in `web/src/lib/components/chat/autocomplete/`, one `CompletionProvider` per trigger (`commandProvider`, `modelAliasProvider`). The endpoint serves `{commands, command_aliases, model_aliases}` from `commands.COMMANDS`, `commands._COMMAND_ALIASES` and the brain's `list_aliases()`, plus admin-only `selectable_brains`, `brain_namespaces` (every known kind, ISSUE-417) and `inherited_brain`. `room_id` scopes `model_aliases` to the room's brain; `brain` names a kind being considered (admin-only, bounded by `room_selectable`, unknown falls back). The composer's own `!model` completion asks unscoped, since the same response carries the routing command set and keying it by room would make routing depend on the last fetch.

### Composer

- `Composer.svelte`: `+`, field, Send, and the mic or Stop. On wrap the controls drop below (CSS `order`, `multiline`).
- **Two buttons, one meaning each.** Stop renders before Send inside `.tools` so Send never moves. This retired `MODE_FLIP_GUARD_MS`, `lastActivationAt`, `lastActivationMode`, `activatePrimary`. `showStop` = `busy && !!onCancel`; the mic is hidden while it shows because `singleRowWidth` subtracts `.tools`' width and a third button would wrap every turn. Residual: no `MediaRecorder` means one idle button. `Composer.sendButton.svelte.test.ts` fakes the mic.
- **Enter sends, Shift+Enter is newline, `Cmd/Ctrl+Enter` is an alias.** Hardware keyboard only (`usesSoftKeyboard()`); `enterkeyhint` stays `enter`. Checked after the autocomplete's `onKeydown` (Enter accepts a completion first); `useAutocomplete.onKeydown` declines modified Enter.
- **Every key path asks `wouldSend()` before consuming the key**, so a refused key writes its newline. Refusals: full queue (`MAX_QUEUED_PER_ROOM`, text stays), active recording, upload in flight, `canSend`. The button reads the same predicate. A running turn is not a refusal (ISSUE-238). `queueFull` is gated on `busy`.
- **IME**: `isImeComposing` (`$lib/platform/input`, shared with the room-name field) plus a `compositionend` stamp within `IME_COMMIT_GRACE_MS` (WebKit fires it before the confirming keydown); hoisted to the top of `onKeydown`.
- **Wrap is measured at the single-row width** (the live width oscillates): `wrapsAtSingleRowWidth` pins the wrapper's `flex-basis`; height after `await tick()`; re-evaluated on resize. Controls are `em`-sized with `font: inherit`.
- **Voice**: `useRecorder.svelte.ts` feeds the normal upload; `executor._pre_transcribe_attachments` appends a transcript (needs `whisper`). The mic hides without `navigator.mediaDevices`.

### Commands in a busy room

`runTurn` is non-re-entrant (owns `status`, the `pendingSend` slot, the cancel flag).

- A `!command` answered in the request goes out now via `sendInlineCommand` (ISSUE-300): no `status`, no `pendingSend`, no `cancelRequested` reset, no Retry. `!retry`/`!resume`/`!confirm` tasks arrive over the room stream and `pickUpStreamedTask` adopts them. Anything else queues (ISSUE-238).
- `isKnownCommand` (`providers.ts`) reads `commands` plus `command_aliases` (ISSUE-350); suggestion providers read `commands` only. Unknown `!word` queues (matching `!model <alias> <prompt>` client-side would copy a server rule). `init()` warms the catalogue (`void loadCommandNames().catch(() => {})`), since before it lands a command would queue. An attachment disqualifies a command.
- Durable command rows must be stamped: `cmd_steer` returns `{kind: 'steer_recorded', user_msg_id, body}` and `applyInlineResult` stamps it and adopts `body`.
- `unsettledSends` holds one entry per draft key, so `submit` writes the held text back when another send owns it, and `sendInlineCommand` calls `settleSendRow` (no `sendSettled` bump).

### Send queue (ISSUE-238, ISSUE-202)

- Per room, keyed by token (`web_chat_rooms.id` has no `AUTOINCREMENT`, rowids are reused), holding un-POSTed messages. Separate vocabulary from `streamQueue`: `sendQueue` / `enqueueSend` / `drainSendQueue`; user row "Waiting to send" vs placeholder `Queued…`. The entry is truth, the row a mirror. No server-side queue (it would commit before the user decides, and mirror to Talk).
- **Drain**: `canDrain` needs the room view, `online`, idle, no `activeStream`, empty `streamQueue`, no row `sending`, unheld head; else a silent no-op. The entry stays until the POST acks (ISSUE-202 reversed ISSUE-238): `unreachable`/`timeout` parks to `queued`, `rejected` fails and removes. Safe because `idempotencyKey` is minted at enqueue. Hands to `beginSend` (shared with `retrySend`, same cid).
- **Files upload first, one at a time**: `PendingAttachment` per file, bytes in the `blobs` IndexedDB store, entry re-persisted after each upload; every null-`path` chip has a `pendingBlobId`; `readEntry` refuses disagreeing halves. Gap parks; refusal fails without Retry and drops bytes; missing bytes fail the row. Room claimed `'sending'` during uploads; a cid claim set prevents double drains.
- **Byte bounds** (`offline/db.ts`): `MAX_PENDING_BLOB_BYTES` 10 MiB, `MAX_PENDING_BLOB_TOTAL` 50 MiB, 80% of `navigator.storage.estimate()`. Blobs are collected on `init()` when unreferenced (storage and memory) and older than an hour.
- **Triggers**: `onStreamSettled` on `done`; `loadHistory` (not `selectRoom`, since `recoverStream` halts the stream before rebuilding); `releaseQueued`; `sendTurn`'s inline return; end of `sendInlineCommand`.
- **Hold rule**: drains only if the turn it was written against finished normally. `error`, `cancelled`, a parked confirmation and a failed send hold every entry in the room ("Held — not sent"). Applied above the stream-queue advance in `onStreamSettled` (a room can have two live tasks). `failSend`'s hold is gated on `settleStatus`.
- **Verbs** (no-ops on a non-queued cid): `removeQueued`; `editQueued` (remove plus restore of text, attachments, citation via `sendReturned`; refuses unless active room); `releaseQueued` (only the head goes).
- **Client-only rows** (`isClientOnly = isStranded || isQueued`, in `stores/segments.ts` so page and store agree): skipped in All; `stopActive` leaves `sendQueue`; `forgetRoom` drops in-memory rows; stored copies go only via `dropRoomQueue` from `deleteRoom` and the page's `dropQueue`. They hold the transcript bottom (ISSUE-351) via `appendAboveClientOnly` at every append (`send`, `sendTurn`, `sendInlineCommand`, `appendStreamedRow`, `pickUpStreamedTask`, `feedAggregateView`, `loadHistory`'s resume placeholder). Append-time only: a stranded row can sit above a live turn until the next rebuild. They draw no day divider (`startsNewDay` is adjacency).
- **Persistence**: `stores/sendQueue.ts`, `localStorage` `chat.sendQueue`, key `<user>:room:<token>`. Every mutation writes; the clamped return is not adopted back. `forgetRoom` and `restoreQueues` do not write. Bounds: `QUEUE_TTL_MS` 7 days, `MAX_QUEUED_PER_ROOM` 10 (also at `enqueueSend`, since `prune` keeps the head), `MAX_QUEUE_ROOMS` 20, `MAX_QUEUE_CHARS` = `MAX_DRAFT_CHARS`, `MAX_QUEUE_TOTAL_CHARS` 256 KB serialized.
- **User id** from `GET /chat/config` `user_id`, stashed first in `init()`; memory-only until known. `web/vite-mock-api.ts` carries it.
- **Restore** (`holdOnRestore`): fresh cid (`idempotencyKey` is the durable identity). `busy` entries come back held; `offline` ones ready up to `OFFLINE_AUTO_SEND_MAX_AGE_MS`; a prior hold (`holdRoomQueue`) is kept. Skips live tokens and other users' keys; leaves gone rooms' keys.
- **`queueing` is captured at the top of `submit()`** because `send()` flips `status` synchronously. Ordinary sends hold the draft through ack; queued ones drop it. Accepted gaps: a mid-turn `!command` has its draft cleared; a refused enqueue has already cleared the field.
- **Queued bubble**: body at `opacity: 0.65`, status line full contrast. Send only on held rows (it would race the drain); Edit and Remove are `sm` `IconButton`s (Remove is an X with `danger`, not `Trash2`). `hasRowActions` excludes queued rows.

### Attachments

- Stored names: `_attachment_stem` plus random suffix. Chip names persist on `messages.attachments` (via `transport.display_attachment_names`), since retention deletes `tasks`. The client sends `attachment_names` (positional; dropped on count mismatch).
- **Chips link to the file** (ISSUE-206): `transport.ingest.workspace_attachment_paths` stores `messages.attachment_paths` at ingest (`null` = not servable). Reads re-scope to the caller's workspace, so a co-member gets an inert chip. `POST /chat/attachments` returns `workspace_path`.
- **Intake**: five paths into one `upload()`. Limits from `GET /chat/config` (`max_attachment_mb`, `attachment_extensions`), checked fail-open; the 413 is the backstop. App default 25 MB, Ansible deploys 100 and renders nginx `client_max_body_size` from the same variable. `heic` accepted. Native picks go via `IstotaUploader`, else 3 MiB windows into a blob-backed `File`.
- A text-less send with attachments stores a `_describe_attachment_only_message` descriptor.

### Drafts (ISSUE-205, ISSUE-216)

- Client-only in `stores/drafts.ts` (`chat.drafts`), key `<user>:room:<token>`. `switchDraft` saves the outgoing room before restoring and clears chips.
- Debounced (`DRAFT_SAVE_DEBOUNCE_MS`), flushed on key change, unmount, `pagehide`, `visibilitychange` → hidden (iOS's only callback).
- Text typed before the room list loads carries into the first room unless a stored draft exists; a key going null later clears the field (one-shot "has ever had a key" flag).
- Bounds: 30-day TTL on read and write, 50 entries; `MAX_DRAFT_CHARS` 64 KB on read and write; `MAX_DRAFTS_CHARS` 256 KB in `prune`, which puts the written key first. One oversized entry used to fail every write silently through `persisted.ts`.
- **`writeDraft` returns what was stored; callers keep that**, since `settleDraft` and `switchDraft` compare against it.

### Send lifecycle and durability (ISSUE-200)

- `sendChatMessage` classifies: `SendFailure` = `unreachable | timeout | auth | rate_limit | rejected | reply_target_gone`, bounded by `SEND_TIMEOUT_MS`. `sendState` absent means settled. `failSend` removes the placeholder and sets `'idle'` only if its room is on screen.
- **Retry reuses the cid** (folds the echo via `(role, task_id)` dedup), needs `sendPayload`'s host paths, requires idle, stays out of the queue. Withheld for expired sessions and 4xx verdicts (409, 400, 413). Classified failures raise no `notify()`.
- **Adoption fallback**: `appendStreamedRow` adopts an echo matching a failed task-less row on `(role, text)`, but refuses a row attributed to someone else. `client_msg_id` is the primary mechanism.
- **One indicator per turn**: `sendTurn` appends the placeholder after `settleSend`, room-guarded.
- **Stranded sends**: failed rows (no `msgId`) go to `strandedSends` in `stopActive` and come back via `carryClientOnlyRows`; carried into All, not Unread/Starred.
- **The draft is dropped on ack**, signalled by `sendSettled` (counter plus room) from `settleSend`. `submit` writes the text immediately; `flushDraft` holds it. `unsettledSends` is keyed by draft key; `switchDraft` refuses to restore an unacked message; the drop is withheld if something was typed since.
- **`client_msg_id`**: nullable on `messages`, partial unique on `(room_token, client_msg_id)`, empty → `NULL`. Minted with `crypto.randomUUID`, reused on retry. `record_inbound` checks it before creating anything; `_mirror_web_turn_as_user` uses the stamp as guard. The lookup returns the sender; a co-member's colliding key makes the send drop idempotency. Over-long keys are rejected. The replay ignores task status. Gap: `!commands` never consult it, so Retry is withheld for `!`-prefixed bodies.
- `uploadEpoch` drops an upload result that crossed a room switch. `runTurn` holds its own `pendingSend` buffer; `stopActive` abandons it.

### Room memory pane (ISSUE-248)

- `GET`/`PUT /chat/rooms/{room_id}/memory` behind a Memory kebab entry. `RoomMemory.svelte`, keyed `{#if memoryRoom}`, reused across rooms (clears its buffer, gates save on `loadedRoomId`). Whole-file editing.
- **Confinement**: `_chat_memory_room` uses `_chat_owned_room`, `storage.validate_conversation_token`, then `db.is_room_member` explicitly, since a handle outlives membership. All refusals 404.
- **Revision**: `PUT` re-reads under `memory_md_lock` and 409s `conflict`, because the lock anchor is per-user. The revision hashes what `read_channel_memory` returns, not the bytes written.
- **Busy** via `count_active_room_tasks` (all members). Client switches on `code`.
- `storage.write_channel_memory` stages via `tempfile.mkstemp` (as the skill CLI's `_atomic_write`), UTF-8 pinned. rclone is not atomic; the revision check makes it safe. `OSError` → 500 `code: "failed"`. `_CHANNEL_MEMORY_MAX_BYTES` 256 KiB → 413. Empty → `exists: false` and the server-served `CHANNEL_MEMORY_TEMPLATE`. `USER.md` is an open decision.

### Message actions (ISSUE-210)

- Copy, star, reply, delete; layout box kept when hidden. Copy is `messageCopyText` markdown, withheld mid-stream.
- **Delete** (`DELETE /chat/messages/{message_id}` → `db.delete_message`) is room-wide, authorized by membership (stars are per-user). Unknown and non-member both 404; in-flight 409. Hard delete (a tombstone would still hold its unique-index slot). Touches `messages` and `message_stars` only. Pessimistic client removal.
- **`message_deletions` ledger** is the stream's second cursor: `message_deleted` has no SSE `id:`, cursor in the payload, sent back as `since_deletion_id`; advances to the max scanned; pruned at 30 days (`_MESSAGE_DELETION_RETENTION_DAYS`). Talk propagation tries the user's token then the bot's; a Talk failure never fails the delete.
- **System rows** carry the action row inside `.content` (a third child of `.cmd-row` would sit beside the card). Search-result rows have no copy.

### Reply to a message

- Parent is the canonical `messages.id` (`reply_to_msg_id`), stored on `tasks.reply_to_message_id` and `messages.reply_to_message_id`, no foreign key so a deleted parent can be shown.
- **Not `reply_to_talk_id`**: canonical and Talk ids collide silently in a bound room. `record_inbound` takes `reply_to_canonical_id` beside surface-native `reply_to_message_id`; do not merge them. `_ensure_reply_parent_in_history` tries `db.get_reply_parent_task_by_message_id` first.
- **The snapshot is server-derived** (1000 chars from `messages.body`); a client quote would be forgeable. Unknown or foreign parent → 404, no task.
- `build_prompt` renders the snapshot as a request blockquote always; `_ensure_reply_parent_in_history` force-includes the parent turn. Snapshot-only: system rows, retention-deleted, unfinished turns.
- `reply_to` (`{msg_id, role, excerpt, deleted}`) from all three producers via a live `LEFT JOIN`; excerpt 200 chars, same as the chip; `buildHistoryMessage` is the one builder.
- **`reply_target_gone`**: the row is removed and text returns to the composer, the one exception to ISSUE-200, since Retry cannot work.
- Talk: a web reply uses `db.get_message_external_id`; inbound maps via room-scoped `db.find_message_by_external_id`.
- Client: no Reply in aggregate panes; Escape clears the chip after the popover declines; `replyTo` rides the draft; `!commands` clear it.

### Offline (ISSUE-202, ISSUE-354, ISSUE-355)

- **Service worker** (`src/service-worker.ts`): `kit.serviceWorker.register: false`, registered by `routes/+layout.svelte` behind `shellAtLeast('0.10.0')`. Precache `build` plus a `files` allowlist all-or-nothing, `prerendered` tolerantly. No `skipWaiting`. Needs `WKAppBoundDomains` and `limitsNavigationsToAppBoundDomains` (unsupported WebKit behaviour).
- **Routing** (`routeFor`, pure and tested): `${base}/api/` never cached; `text/event-stream` passes through; `_app/immutable/` cache-first; navigation network-first, 3s timeout only with a cached fallback, trying trailing-slash spellings and nothing wider (else `login`/OAuth would get the shell); else network with 503 (so `version.json` still prompts). Cached documents are re-wrapped in a fresh `Response`.
- **Cold-launch pointer** `offline/lastUser.ts` (`chat.lastUserId`), written by `init()` and `getMe()`, read only under `isNativeShell()`; `forgetLastUserId` on both 401 paths and `LogoutButton`. While a guess stands, `cacheUserId()` is null and `canDrain` refuses. `settleSeededUser` (in `init`, once from `onBackOnline`) handles a wrong guess: drop transcript, rooms and selection, split the queue via `restoredCids`. It compares against the id `init()` captured.
- **Cached identity** (ISSUE-354): `readUser`/`writeUser` in `offline/db.ts` under `<user>:me` in the `config` store (no `DB_VERSION` bump); each reader refuses the other's shape; `readUser` validates `username` and `features`; `canReadCachedUser()` gates both. Published via `lib/userContext.ts` (ISSUE-355).
- **Layout catch**: `AuthError` redirects first; a connectivity gap uses the cached identity, writes nothing, starts no poll; else the error page. Retry waits on `live`, listens on `online` and `visibilitychange`; `loadUser` has a generation counter; `getMe()` is bounded by `ME_TIMEOUT_MS`.
- **"Clear offline data"** (`offline/clear.ts`): unregisters workers under the base path, deletes `istota-` caches, `clearOffline()` (all stores, all users, reports failure), reloads. Gated on `shellAtLeast('0.10.0')`, outside `{#if profile}`. Keeps the send queue, drops blobs.
- **Offline notice** is `sticky` in `NoticeDrawer`'s absolute band (a `NoticeBanner` pushed the transcript down). `sticky` (`notices.ts`) is exempt from `clearNotices()`, `PINNED_HANDOVER_MS` and `MAX_QUEUE`, but yields the slot to events (`notices.sticky.test.ts`). Tests: `offlineBanner.svelte.test.ts`, `AppShell.notices.svelte.test.ts`. The chat page owns its lifecycle; it stays chat-scoped.

### App shell, render model, unread, order

- `_app/immutable/` immutable for a year, the rest `no-cache`; a new build shows a Reload toast. The Ansible unit passes `--timeout-graceful-shutdown` (Docker has no equivalent); SSE generators also observe `istota.web_shutdown`.
- **Segments** (`segments.ts`): `text`/`tool`/`thinking`/`notice`/`gate`. `renderGroups` keeps prose ≥ `SUBSTANTIAL_TEXT_CHARS`, coalesces tools into one `ActivityTrace`, drops short narration; live and `execution_trace` turns build the same groups. `notice` (`brain_fallback`, ISSUE-278) is a `.banner warn` group, excluded from copy and from `Message.svelte`'s streaming cue, live-only.
- **`gate`** (ISSUE-592) is the question a task parked on, always rendered and copied, with `outcome` (`approved`/`declined`) once answered. The live `confirmation` event turns the trailing open text block into it; a replayed `confirmed` event (approve relabels the row in place) renders it approved with no card. Approving keeps the turn: `confirm` marks the gate and reopens the stream past `gateSeq`, the seq the question arrived at, and only a turn drawn from history (no `gateSeq`) is emptied and replayed from seq 0. Reloads match because the park stores the attempt's trace ending in a `gate` entry and `confirmations.trace_with_gate` keeps it ahead of the re-run's trace; `web_app._settle_gates` derives each gate's outcome from position and task status.
- **Unread**: `db.count_unread_messages` past `room_read_state`, excluding `role='user'`; `db.initialize_room_read_state` seeds; `_chat_mark_room_read`; active room held at 0.
- **Order**: `last_activity` from `db.list_member_rooms` (`ORDER BY id DESC LIMIT 1`, room creation fallback), via `_iso_utc`. `stores/roomOrder.ts`; `applyRoomEvent` touches every frame; never moves backwards; the 30s poll re-sorts with the newer stamp; the `room` frame carries none; PATCH and create are merged.

### Confirmations (ISSUE-241, ISSUE-243)

- **They live in the notification bell** (`confirmation` source); `GET /chat/confirmations` and `PendingConfirmations.svelte` are gone. A gated email's mirror is withheld (`suppress_transcript_mirror`), which also keeps it out of `_AUX_ROOM_SCOPE` (admitting it would publish `tasks.prompt`).
- A room confirmation shows in the bell and the `ConfirmationCard`; the park writes the row always. **Known rough edge**: `m.confirmation = false` is set only by the store's `confirm`/`reject`, so the bell or `!confirm`/`!yes`/`!no` (the `confirmation_answered` branch only stamps ids) leaves a stale card; pressing it is harmless. ISSUE-350 widened this to busy rooms. Re-adoption by `pickUpStreamedTask` is unverified (needs a `full`-profile run).
- **Bare "yes"** (ISSUE-243): `confirmations.py` (`parse_answer` / `resolve` / `apply_answer` / `ambiguity_listing`) is shared by Talk, web and `!confirm`. Only a parked question suppresses task creation (`_chat_answer_confirmation` returns `None`). Exact word lists, strip and casefold only. Skipped with attachments. Ordered after the `!command` block and before `_chat_create_web_task` (which cancels confirmations).
- **Durable exchange**: `confirmations.record_exchange` writes `role='user'` and `role='system'` with `task_id=NULL`. Not posted to Talk (inherited asymmetry). Not recorded: usage errors, the ambiguity listing (no rate limit), surfaces outside `_TRANSCRIPT_SURFACES` (`TRANSCRIPT_SURFACE_FILTER` renders user rows only for web, talk, email, sms and whatsapp, while every system row renders).
- The response's `{kind: 'confirmation_answered', user_msg_id, system_msg_id}` (also from `cmd_confirm`) must be stamped. A retry is answered by `db.find_confirmation_exchange` first (else a 500, or approving a second question). Path B resolves to the canonical token first; Path A works via the mirrored row's `external_ids` and `db.get_message_external_id`, room-checked.

### Talk notification mirror (ISSUE-242)

- `notifications.mirror_talk_to_room` writes a `role='system'` row after a Talk delivery from `_dispatch`, gated on room existence after `db.resolve_room_token`. Best-effort, not idempotent. The double-write guard compares canonical rooms.
- **`_MIRROR_LOCK_WAIT_MS` (250ms)**: callers may hold a write transaction (the sleep cycle), so a dropped mirror beats a 30s stall. `run_cleanup_checks`' ancient-pending notice moved out of its transaction.
- Direct `client.send_message` in `transport/talk/inbound.py` bypasses it; confirmation acks there use `record_exchange`; the `!model` usage error stays Talk-only.

### Failed-turn gap-fill

`_chat_room_messages` merges failed/cancelled `tasks` so error bubbles render, banded on `_AUX_TURN_TS` (user spine row's `created_at`, else the task's), since the task stamp can precede its row by a second. Sargable companions `_AUX_TS_ABOVE` / `_AUX_TS_BELOW` (one hour slack) keep the index. A turn tying the floor second can still split pages.

### External turns

- `origin_surface` and `subject` are in both `db._CROSS_ROOM_COLUMNS` and `web_app._SPINE_COLUMNS`, published by `_user_row_display`; subject capped by `_SUBJECT_MAX_CHARS`; an empty parsed body publishes empty `text`.
- `author_id` only under the label's own condition; none for external senders or the viewer (D13 of the profile-icons spec).
- **`origin`** needs `not surfaces.is_room_member(origin_surface)` and an `author_label` (`transport.ingest.resolve_author`; `UNATTRIBUTED_SENDER`), so your own mail is not external. talk, web, sms and whatsapp are all room members, so it resolves to email today; the client label is derived (`External email` / `External message`) and nothing enforces that coupling. See `.claude/rules/transport.md` "The room model".
- **ISSUE-274**: `email_support.parse_email_prompt` tolerates `[ \t]*` before closing tags and headers (stored rows are indented); the builder writes flush left; `tests/test_email_prompt_wrapper_render.py` pairs them. `email_support.flatten_prompt_header` collapses whitespace in header values and attachment names; headers are first-wins; `body` is greedy, `meta` lazy.
- **`external_turn_display`** (`full | collapsed | hidden`, default `collapsed`) governs the body only (`hidden` keeps the row, ISSUE-136); the gate tests the mode; one-line preview by code point; neutral tint; expansion keyed on `message.cid` (rebuilds re-collapse, accepted). A `user_profiles` column read live by `GET /chat/config`, not the `_config.users` snapshot. `outbound_approval` there still reads the snapshot; nothing edits it yet. The PUT rejects unknown values; client normalizes in `stores/externalTurns.ts` (not `$lib/api`, which tests mock).

### Default room and Talk state (ISSUE-342, ISSUE-477, ISSUE-478, ISSUE-479)

- `_chat_list_rooms` emits `talk_token` (`db.talk_refs_for_member`), as does the PATCH, since promoted rooms keep `origin='web'`. `db.ensure_default_web_chat_room` runs only when `list_member_rooms` is empty. `_default_room_candidates` excludes shared rooms and `log_channel`/`alerts_channel` rooms.
- **`user_profiles.default_room`** (ISSUE-477): `db.configured_default_room` raw; `db.configured_delivery_room(conn, user_id, surface)` is read by `db.default_web_room` and `notifications.resolve_conversation_token`. Above the web heuristic, below an explicit Talk route and `alerts_channel`. A gone/archived/foreign room falls back; a hidden or handle-less one does not (`ensure_default_web_chat_room` consults `configured_default_room` first and un-hides). `_migrate_default_room` (`default_room_v1`) backfills, skipping Talk-bound rooms.
- **`db._live_room`** (ISSUE-479) is the shared core; dismissal and membership stay out. Controls in `tests/test_delivery_room_predicates.py`.
- **Dead pins** (ISSUE-478): `web_app._ignored_default_room_pin` marks only `configured_default_room`'s terminal arms, as `ignored_default_room`, worded `(ignored)` not `(unavailable)`. No membership gate (own value), unlike `_unavailable_web_room_pins`.

### Stale Talk binding repair (ISSUE-401)

- `db.add_room_binding` is `INSERT OR IGNORE`, so a deleted conversation left a dead ref. `_chat_promote_to_talk` with a binding probes and replaces only when gone; the control shows when `talk_token` is set.
- `_talk_conversation_verdict`: the bot's 404 also means "bot removed", so it re-asks as the user: visible → `bot_removed`; 404 → `gone`; else `unknown`, refused. Minting on a removed bot would fork a live conversation.
- `db.replace_room_binding` is a compare-and-set; the race loser deletes its conversation. Same transaction: `db.clear_room_external_ids` and `db.clear_stale_talk_delivery_token` (non-terminal tasks holding the old ref).
- Returns `{status, room}`. No explicit unbind, deliberately.

### Live room-event stream

- `GET /chat/stream`: one session-lived connection tailing `messages` for every member room. An unsettled streamed `user` row opens a task stream. Cursor is `messages.id`.
- Server polls ~1s (`id > :cursor` behind a `MAX(id)` gate). `db.list_room_events_since` shares SQL fragments with `db.list_messages_across_rooms` and emits `_cross_room_message_dict`.
- `message` and `gap` carry `id:`; `room`, `message_deleted` and `: ping` must not.
- Gap: server on cost (`room_stream_max_batch`, `room_stream_max_bytes`), client on ~60s silence. Recovery reloads rooms and the open room, adopts the max scanned cursor, re-applies buffered frames.
- The rooms poll is a 30s reconciler kept for the Talk→web read-state pull. Knobs `[web.chat] room_stream_*`; `GET /chat/events` is the polling fallback; `_admin_chat_section` reports connections (the metric for a deferred per-user broker).

### Held outbound drafts

- `DraftCard.svelte`: recipients, subject, whole body as a `pre-wrap` text node; approving sends exactly that. Inline under the composing assistant row; otherwise in the bell. Actions summary read-only.
- Kept: `ConfirmationCard.svelte`, inline `DraftCard`, `GET /chat/drafts`, the `drafts` frame, `draftsByTask`. Removed: the `PendingConfirmations` and `looseDrafts` strips.
- `sending` rows are listed (`outbound_drafts.open_for_user`) with no action. Unparseable rows are ids in `DraftListing.unreadable`, discardable via `outbound_drafts.identity`. The frame is byte-budgeted (`_DRAFT_FRAME_MAX_BYTES`): overflow rides as stubs (`_draft_stub`, with `task_id`), first draft always whole.
- **A 409 is read against the action**: already-discarded settles a discard, refuses a send with a notice. Every 409 carries `state`; missing `state` keeps the card. Permanent `DraftError` → `retryable: false`: retry is withheld where it cannot succeed.
- Optimistic removal is suppressed for `ANSWERED_SUPPRESS_MS` only on acceptance. Seeded on entry, refreshed by `recoverStream`; `refreshDrafts` coalesces callers. The full-row latch resets when no longer truncated.
- `placement: 'turn' | 'banner'`: banner is `compact` (whitespace-collapsed CSS-clipped peek; Send/Edit/Discard kept; not while `editing` or `unreadable`/`truncated`/`sending`). Send and Edit use the stored `body`, never `shownBody`/`peek`.
- `--chat-body-max` lives in `Message.svelte` on `.content > :global(.draft-card)`, pinned against source in `DraftCard.svelte.test.ts` (that file's margin regex reads comments too).

## Phone rooms are read-only

A private SMS or WhatsApp room is the transcript of a phone thread (`.claude/rules/transport.md`, "Phone rooms"), and web reads it without writing to it (decided 2026-10-01). The alternatives were a web turn the phone never sees, so the thread goes dark on a conversation continued in web, or texting the user their own web words back, at a price and inside a window a web-only exchange never opens.

**The server is the gate, not the missing composer.** `POST /chat/rooms/{id}/messages` answers 409 with `{error, read_only: true}` when `routing.phone_room` names the room, a WhatsApp group included (ISSUE-585), ahead of `!commands` and confirmation answers. `/chat/tasks/{id}/confirm` and the decline arm of `/cancel` refuse a phone task's parked question with the same body, since that question is answered by text; stopping a running phone task stays allowed, because it is not an answer. Those two use the narrower `routing.phone_transcript_surface`, so a WhatsApp group task's parked question is still answered from web as well as by `!confirm <id> yes|no` in the member's 1:1 view; the two are both answers by that member, and keeping the buttons costs nothing the group can see. `POST /chat/rooms/{id}/members` refuses an add to a private phone room with 409 and the same `read_only: true`, since a second member would make it a shared room the phone side no longer answers or records into; `RoomMembers.svelte` shows a caption instead of the add control there. A room shared before that refusal stays read-only, and removing the member is what restores it (`.claude/rules/transport.md`, "Phone rooms").

**The listing and the room-stream snapshot carry three fields.** `phone_surface` is any sms or whatsapp binding and draws `PhoneSurfaceIcon` in the sidebar and the header: a phone for SMS, a green bubble for a private WhatsApp chat and a green pair of bubbles for a WhatsApp group, which outranks the shared glyph there and keeps the shared fact in its title (ISSUE-584); `read_only` is true for every such room, and `phone_group` marks the WhatsApp group among them, both answered by `_room_phone_fields` from the binding the listing already fetched. The rooms poll and the snapshot merge copy all three, since a key the merge omits reads as absent. A read-only room renders a one-line notice in the composer dock instead of the composer (worded for a group when `phone_group`) and stages no replies; a private thread's confirmation card says to reply by SMS or WhatsApp in place of Confirm and Cancel, while a group's keeps its buttons and its members control. Room settings show the binding as a read-only "Connected to" line with no unbind control, the binding being identity, and stop offering "Also open in Talk", which the server already refused for a non-web origin. The bell's confirmation item for a phone task renders with no actions.

**A texted turn gets no external treatment.** Every user row whose `origin_surface` is sms or whatsapp carries `via`, a WhatsApp group member's or guest's included, and `Message.svelte` draws a muted "Sent by SMS" or "Sent on WhatsApp" line with the surface icon above the body. The `origin` treatment below collapses a stranger's body by default, which in a private phone room would hide the reader's own words; sms and whatsapp are room members, so `_user_row_display` never sets `origin` for them anyway. This is a recorded divergence from the spec, which said external.

**A phone room is never the implicit default room.** `db._usable_as_delivery_default` excludes any room with a phone binding, which covers `default_web_room`, `_default_room_candidates`, side rooms and the relay default, and `ensure_default_web_chat_room` mints a private `general` for a user whose only private room is a phone room. A pinned `default_room` naming one is honoured, a pin being the user's choice, so bare web deliveries then land in the transcript as `role='system'` rows. `relay_destinations._room` refuses that pin as `recipient_has_no_private_room`, because a relay question there could only be answered by a web send the server refuses.

**A streamed row older than what is on screen is inserted, not appended.** The phone backfill writes a room's earlier history with new ids and old stamps, so a room open during the pass received that history over the room stream below the conversation it came before. `insertStreamedRow` places a row ahead of the first on-screen server row with a later `created_at`, comparing server stamps only (`_iso_utc`'s second-precision `…Z`) so a client clock never decides an order; any other row still goes through `appendAboveClientOnly`.

Residual: the aggregate views (All, Unread, Starred) do not know a row's room is read-only, so a phone task's confirmation card there may still show buttons, which the server refuses with 409.

## Shared rooms

A web room can hold several members (multiplayer Stages 8, 17). Surface-neutral rules are in `.claude/rules/transport.md` ("Multiplayer rooms").

**Membership.** `GET /chat/users` (id and display name only). `GET /chat/rooms/{id}/members` (members, `can_manage`, `message_count`). `POST` takes `{user_id, acknowledge_history: true}` (literally `true`). `DELETE /chat/rooms/{id}/members/{user_id}`. Order: 404 non-member, 409 Talk-backed (`_is_talk_backed`), 403 non-creator, 400 bad user or flag. Only the creator manages; members may leave; the creator cannot (409); a target with a live task is 409. Removal deletes the member's handle (routes authorize on it). Adding clears the dismissal and upserts a `principal` participant (`db.add_web_room_member`). A co-member's delete leaves; archive is per-user; the creator's delete waits on any member's task and deletes every member's tasks.

**Host, group.** The grants endpoints and `RoomShareScopes.svelte` are gone (ISSUE-576). `POST .../host` is `room_policy.claim_host`. `GET`/`PUT .../group` via `room_policy.group_link_refusal` (`RoomGroupLink.svelte`).

**Listing state.** `shared` (`db.room_is_shared`), `policy` (`{host, is_host, guest_reply, settings_refusal}`), `side_of`, `off` (`room_veto.switched_off` via `web_app._room_off`; shown as a banner). Reading `policy` may record a lost host (D14). Name, model, effort, brain and promote are the host's (`room_policy.settings_refusal`, 403); colour and hide are per member. `guest_reply` via `room_policy.guest_reply_refusal`.

**Transcript.** A recorded-only turn returns `{task_id: null, message_id, status: "recorded"}`. `deletable: false` from `_message_owners` (shared with delete). A switched-off room 409s sends. A guest turn's Talk ack is deleted on confirm or cancel.

**Side rooms.** Private room with `side_of`, indented. Ephemeral bubble in the open parent (no `msgId`/`taskId`, "Only you can see this"), gone on reload. No Talk promotion, no second member.

**Settings refuse a shared room.** Routes, briefing token, `alerts_channel`, `log_channel`, `default_room` via `_refuse_shared_destination`; unchanged values are not re-judged; pickers disable shared options but keep a current pin.

**Dev mock.** `web/vite-mock-api.ts` serves the membership, host and group endpoints and `shared`/`policy`/`off`, seeded with a carol/dave room and a guest-switched-off Talk room.
