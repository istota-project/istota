# WhatsApp

One WhatsApp number through one of two adapters behind a provider seam that mirrors SMS. `baileys` (default): a paired WhatsApp Web session held by a Node sidecar, unmetered, no window, no templates. `whatsapp_cloud`: Meta's Cloud API, direct, metered, window-bound. `config.whatsapp.provider` picks one; no failover.

A 1:1 chat is a user-routable push surface that owns one private room and is never a view of it (`room_view=None`): the first accepted inbound turn mints a room named WhatsApp; every later turn, answer, push, notification and `!command` reply is recorded there; web reads it and cannot write to it; nothing written there causes a send. The mechanism is shared with SMS and written down once in `transport.md` ("Phone rooms"). A Baileys **group** is a room of its own, registered by its JID rather than by the phone mint (see "Groups"). Inbound: text, images and audio (voice notes and audio files). Outbound: text. The `whatsapp-<user hash>` token, a pure function of `user_id`, is the private room's binding ref and a permanent alias for pre-mint history, so the room survives every number, username, send-id and adapter change; an identity reset leaves it alone, and a recycled number that resolves to a new user mints that user's own room. A bare `whatsapp` destination resolves the current binding just before send; `whatsapp:<anything>` is refused (no arbitrary-contact send). The `all` / `both` aliases do not include it.

Cloud-only: the 24-hour window, templates, monthly cap, `quota_month`, pricing, billing circuit, signed webhook, 1,024-char interactive body. Common: the one-send ledger, parked statuses, status monotonicity, identity, opt-out, media staging. Operator docs: `docs/features/whatsapp.md`.

## The seam

`transport/whatsapp/providers/` mirrors `transport/sms/providers/` in shape, not code. `WhatsAppProviderAdapter` is `name`, `caps`, `send`, plus `parse_webhook` / `verify_signature` (both `None` for an adapter with its own transport). `make_provider_registry` refuses: a name mismatch, a half-declared webhook pair, a synchronous `send` (it would fail inside claim-to-settle and settle `unknown`). Fatal on the active adapter; a non-active one is dropped at ERROR.

The active provider is built whenever nothing it declares is missing; a non-active one only if its block is also populated. (Baileys declares no credential fields, so SMS's populated-block gate would never build it.)

`WhatsAppProviderCaps` (`metered`, `has_service_window`, `supports_templates`, `delivery_receipts`, `address_field`, `service_body_limit`, `interactive_body_limit`) has **no defaults**. `delivery_receipts` is read by nothing. Drift guard: **`outbound.py` imports no provider module at run time** and gates read `adapter.caps`; Meta pricing/quota helpers still live in `outbound.py`. `identity.py` is the only module branching on provider name.

## Identity is the adapter-native identity, not the number

Cloud: BSUID. Baileys: JID `<number>@s.whatsapp.net`. `whatsapp_user_bindings` holds both plus nullable `provider` (NULL = `whatsapp_cloud`). The unique index on `jid` prevents a principal takeover. It is its own table because inbound reads and writes it inside the dedup claim's transaction (a `db_path` store would deadlock on the lock; see `notifications.md`).

Resolution (both adapters, in `identity.py`): match the adapter's identity column; else match an E.164 number (Meta `wa_id`, or the JID's number) to a bootstrap number whose identity **for this adapter** is empty, latching atomically. A bootstrap match whose stored identity for that adapter differs is **refused and alerted** (recycled line). Unknown senders: no task, nothing retained, fingerprint-only log.

Cross-adapter: events resolve only via their own column. A JID whose number matches a row with a different JID is refused and alerted (own dedup namespace). An adapter switch *does* let a number latch a row whose identity for the new adapter is unset (refusing would lock out a real migration); it raises a pushed `whatsapp-cross-adapter` alert.

**Two discards; collapsing them is a defect.** A **holder** change (`set_whatsapp_binding`, `reset_whatsapp_identity`) discards everything, `opted_out_at` and `last_user_message_at` included. An **adapter** change (latch onto a row owned by the other adapter) discards `last_user_message_at` and, on Baileys, `send_id`, and **keeps `opted_out_at`** (clearing it would revoke a STOP). Only a Cloud message writes `last_user_message_at`; Baileys never writes `send_id`. `--reset-whatsapp-identity` clears both adapters' identities and the provider stamp.

A non-active provider's inbound is refused `inactive_provider` before lookup and before the dedup claim; delivery events bypass that gate. `touch_whatsapp_binding` writes the unique `send_id` **conditionally** (correlated `EXISTS`): an unconditional write raised `IntegrityError` and Meta redelivered for ever. Now the touch lands, returns False, and the caller alerts.

## Outbound: one send, one row, no resend

`sent_whatsapp` claims a `logical_key` and commits **before** the network call. The claimed region catches `BaseException` and splits at the send: pre-send failure is `failed`, anything after is `unknown`, the one state an operator can never resolve. Key namespaces and the park-twice defect are shared with SMS (`sms.md`).

**An adapter reports one bit: definite or not.** Cloud: 4xx refused, 5xx may have applied (PyWa's `is_transient` ignored). Baileys: the line is `writer.write`; `definite` comes from `send_result`. **No failure reason is built from exception text** (it carries recipient, token path, JIDs, Boom requests); reasons come from a fixed table plus a numeric code.

Gate order: transport, binding, opt-out, billing circuit, window, budget. Circuit and budget need `metered`, window needs `has_service_window`, template fall-through needs `supports_templates`. Opt-out outranks the window. The destination is the column named by `caps.address_field`; `identity.address_for_binding` sits beside the JID parser.

**A confirmation is an interactive message, capped at 1,024 by Meta.** The scheduler sizes it via `outbound.confirmation_body_budget` so `Task #N. Reply YES or NO.` survives. Baileys' two budgets are equal; a stale Cloud template block does not narrow them.

`_TERMINAL_FOR_STATUS` (rows a status may not move) and `_NO_RESEND` (rows no send may retry) differ on the four "taken" states. Monotonicity is by rank. Pricing is observed **outside** the ladder (out-of-order and `failed` statuses still count); `billable` never goes true→false.

### Parked statuses (ISSUE-490)

A status can arrive before `_settle` writes its id; dropping a `failed` one is silently wrong. It is held in `whatsapp_parked_status` and replayed by `_settle_async`.

- **Replay is a second transaction after the settle commits**: inside it, a raising replay would roll back `meta_message_id`. The replay never raises.
- Parking happens only under `handle_whatsapp_batch`'s `BEGIN IMMEDIATE`, and the settle needs the same lock, so nothing is lost between the two. `test_a_status_racing_the_id_write_is_never_lost` goes red with a deferred `BEGIN`.
- Replay goes through `apply_delivery_event` (keeps the ladder). `_settle`'s UPDATE has `_TERMINAL_FOR_STATUS` as a `WHERE` so a late cancellation cannot overwrite a settled row with `unknown`.
- Parked rows key on the **message fingerprint**, never the id (the gate is deployment-wide and sees other apps' ids).
- Two bounds: park only while some send is mid-region (`pending`, claimed, no id); prune past `PARKED_STATUS_WINDOW` on every touch, before the gate. Pruned counts log a warning.
- A failing park falls through to the discard; it never 503s the batch.

SMS has a port of this in `sms_parked_status` (`tests/test_delivery_parking_parity.py`). WhatsApp is the authoritative copy.

## Inbound media: images and audio

`WhatsAppInboundMedia.kind` is `image` or `audio`, taken from the declared message type (Baileys `imageMessage` / `audioMessage`, Cloud `type`), never from the sniff; the sniff only confirms. `ptt` / `voice` decide nothing. An image becomes a task attachment handled by `image_attachments.prepare_image_attachments` like any other. A caption is ordinary text through every gate (STOP, `!` commands, confirmation answers); no caption gets `MEDIA_ONLY_PROMPT`. Audio has no text, on either adapter (the decoder drops any): the prompt is `ingest.describe_attachment_only_message`, the web voice memo's string, and the executor's existing `_pre_transcribe_attachments` transcribes it. **Spoken words drive no intake gate** (opt-out, confirmation, command): ASR is a guess. Reasons and the failed-fetch reply are per kind (`media.reason(kind, key)`, `MEDIA_FAILED_AUDIO_REPLY`). Group media of either kind stays refused; video, documents, stickers and AMR keep `unsupported_type`. Inbox paths reach the executor localized by the scheduler (`localize_workspace_attachments`), as email's do.

**Bytes land on disk before the transaction; the transaction sees only a path.** A fetch under `handle_whatsapp_batch`'s `BEGIN IMMEDIATE` stalls the receiver and web UI (`TestNothingStagesUnderTheWriteLock` asserts on elapsed time). The sidecar fetches on Baileys, the daemon's webhook route on Cloud; from `media.precheck` on, one path. The pre-check supplies the `user_id` the inbox copy needs and reads opt-out. It is a **pre-filter**: `WhatsAppInboundMedia.attached_for_user` is compared with the authoritative resolution in the transaction, and a mismatch drops the media and logs the stray copy.

Bounds: per-file cap is the fetcher's (sidecar aborts; Cloud checks `MediaURL.file_size` then the stream); staging ceiling and orphan sweep are the daemon's, swept on every touch.

**Type comes from bytes.** Images: `lib.image_sniff.sniff_decodable` (what the pipeline decodes) vs `sniff_raster` (what `/chat/files` serves inline; HEIC stays an attachment). HEIF is a **major-brand allowlist**, never bare `ftyp`. Audio: `lib.audio_sniff.sniff_audio`, whose extensions are all in `AUDIO_EXTENSIONS` (the pre-transcription screen). The sidecar's advisory `MEDIA_EXTENSIONS` equals the two sniffers' tables merged, and its `MEDIA_KINDS` the daemon's (`tests/test_whatsapp_sidecar_vendoring.py`). The inbox copy is named from its own sniff, since a wrong suffix is skipped silently downstream.

### The staging directory

`{db_path.parent}/whatsapp-media`, 0700, files 0600, `O_NOFOLLOW | O_EXCL`, no override. There because (1) the sidecar's unit can write it (`ReadWritePaths={istota_home}/data`, deliberately narrow), (2) compose already shares `/data`, (3) it is inside the database mask.

`ISTOTA_BAILEYS_MEDIA_DIR` is an **override, not a requirement** (ISSUE-508): requiring it crash-looped the sidecar on a cron deploy that could not re-render the unit. **Do not require a variable whose value another variable in the same process determines**; socket and session paths stay required. `deriveMediaDir` resolves the path, refuses relative or `/`, and refuses a result equal to or inside the session directory (a credential; `dir_holds_a_session` would count a photo as a session). The cron guard reports, never refuses. Deployment literals are compared to `media.default_media_dir` and driven through `deriveMediaDir`; a test checks the path is inside the unit's `ReadWritePaths=`.

Fallback when the WebDAV copy fails: `{temp_dir}/whatsapp-media/`, owned by `cleanup_old_temp_files` (the staging sweep and the sandbox mask would otherwise lose it). Standalone residual: no mask there, so a workspace-root resource reaches staged files briefly. `doctor.whatsapp.media_staging` (gated on `enabled` only) reports mode, owner and orphans; absent is OK; survey only, since its repair deletes.

## The Cloud adapter

### The signed webhook

`/webhooks/whatsapp`: **authenticate then read** (bounds, content type, secret, HMAC over raw bytes, decode); **normalize the whole batch before `BEGIN IMMEDIATE`**; **one transaction per batch**, DB failure 503s. `verify_signature` refuses an absent secret (PyWa would HMAC under `b''`). Oversize is 413; `hub.challenge` must be ASCII digits. `pricing.billable` is read, never derived. Messages pair with contacts by sender id, never positionally (PyWa's `contacts[0]` is a takeover).

### Cost controls

The monthly cap is **reserved in the claim's own `BEGIN IMMEDIATE`**; only claimed rows count. `monthly_service_attempt_limit` does **not** bound templates: with the cap spent a closed window still bills a template under `allow_paid` (doctor shows template counts; a template limit needs a spec). The **circuit breaker** is persistent and deployment-wide: under `free_guard` the first `billable = true` status sets `billing_blocked_at` and blocks all sends, alerting admins and the recipient. `template_available` re-checks `allow_paid`. `doctor.whatsapp.billing` SKIPs when not metered.

### What a switch away from Cloud costs

`whatsapp_webhooks_enabled` = `enabled` and the **active** provider in `WHATSAPP_WEBHOOK_PROVIDERS` (not `callback_only_names()`). After a flip to Baileys the handlers 404: **Meta's queued retries are lost** and outstanding rows keep their state. Same predicate in `serve._maybe_mount_webhooks`, the role (which **refuses `baileys`** rather than requiring `whatsapp_cloud`, so an unnamed provider keeps a receiver), and compose profiles (`whatsapp` receiver, `whatsapp-baileys` sidecar).

The verify token is in the query string: nginx has `access_log off` on the route, and both standalone receivers run `--no-access-log` (`serve.build_uvicorn_server` passes `access_log=False`); a test pins all three. nginx's 512k body limit sits above istota's 256 KiB.

## The Baileys adapter

Unsanctioned outside the Business API: ban risk, protocol breakage, unlink after inactivity. That is why Cloud stays.

### The sidecar and the wire

`docker/whatsapp-baileys/`, its own image (too heavy to charge every deployment). **The daemon listens, the sidecar dials** (the daemon owns the socket's lifetime and mode). One JSON object per line over `AF_UNIX`, 256 KiB cap both ends, versioned `hello` refused on mismatch. **No HMAC**: the 0600 socket is the boundary. Pinned behaviourally in `tests/test_whatsapp_sidecar_vendoring.py`: entries classified, field names driven through daemon normalizers, pure functions **executed via `node`** (`loadBaileys` is lazy). No tier makes a real connection.

### The session directory is a full-account credential

0700 asserted on an `O_NOFOLLOW` fd that also refuses a foreign uid. The sidecar sets `umask 0o077` on itself (systemd defaults 0022, compose cannot set one); the unit also has `UMask=0077`; `harden_session_files` narrows at bridge start. Sidecar stdio is discarded (Baileys logs JIDs and bodies); it writes `sidecar.log` in the directory with bounded labels. No contents or `qr` payload is ever logged. Its env is an **allowlist** (`PATH`, `HOME`, locale, `TZ`, `NODE_ENV`, socket, session dir). Bound into no sandbox.

**Atomic credential writes (ISSUE-554).** Not `useMultiFileAuthState` (in-place writes left a 0-byte `creds.json`). `useAtomicAuthState`: temp file `O_EXCL | O_NOFOLLOW` 0600, fsync, rename, fsync dir — fsync for the credential only (812 pre-keys would block the loop). Each save also writes `creds.json.bak`; a bad `creds.json` at start restores from it; a *missing* one is a fresh pairing. Refuses without `BufferJSON`. Library helpers are injected for testing.

**Replaced connections (ISSUE-553).** A 440 means another client holds the credential. Drop the socket (sends: definite `not_connected`), reconnect after 30 s; 5 within 10 min latches: no reconnects, three probes at 15 min, 1 h, 1 h; a 440 within 5 min of a probe's open re-latches; after the third the run gives up with a permanent `fatal` and holds on the credential watch. The outage ends on a *stable* open. Persisted in `connection-replaced.json` (in `_SIDECAR_OWN_FILES`), with three rules: a restart holds (probe counted before anything opens); only a `creds.json` stamp we did not cause ends the run (`refreshReplacedStamp` after our saves, explicit baseline in `watchCredential`); alerts fire once per outage because `announced` is persisted after the frame is sent and the bridge alerts on `announce`, not arrival.

Bridge side: `_handle_replaced`, transient while latched, permanent only with `permanent: true`, through the same latch as logout (so every re-pair route works); never `_on_fatal`. `connection_replaced_latched` makes `_send` refuse definitely until `ready`. Alert keys `whatsapp:baileys-replaced` and `…-gave-up`. Give-up body, doctor and card share `REPLACED_GIVE_UP_REMEDY` (re-pair, and unlink an unknown device on the phone first, since re-pairing does not revoke the copy). A run is ended or outranked by: a re-pair (`_clear_fatal_latch`), a logout, any other permanent fatal, `restore_session_archive` (drops the archive's run file), an unreadable credential (checked before resuming a hold), a probe that cannot start (`holdOrGiveUp`), and `probe_opened_at` resumes a probe's stable window across restarts.

**Unreadable credential (ISSUE-552).** `open_` asks `storedCredentialVerdict` before loading the library. Unreadable (no usable main or backup; any errno but `ENOENT`) is a permanent `fatal` `credential_unreadable`, latched like logout, with its own alert key, doctor arm, card label and `CREDENTIAL_UNREADABLE_REMEDY` (fix owner/mode and restart first; re-pair only for a corrupt file with no backup). It opens nothing (never overwrites the file), re-announces on reconnect, waits up to an hour on the credential watch. **A `qr` frame sets `_session_unpaired`** on the bridge, since `start()` reads disk only once.

### The bridge

**Definite line is `writer.write`**: before it `failed`, from it `unknown` (decided by a mark set at the write). Waits are bounded: the answer by `SEND_TIMEOUT_SECONDS`, `drain()` by a smaller bound (it holds the write lock). The writer is adopted after `hello`. Request ids match answers.

**The read loop only dispatches** (a reply awaits a `send_result` the loop must deliver). Inbound goes to a bounded queue with **one serial worker**; overflow drops loudly. The worker calls `handle_whatsapp_batch(..., provider="baileys")` inside one `asyncio.to_thread`. Malformed lines: count and drop, never attributed. DB failures: bounded retry, then drop with a count (`failed_events`), a stated deviation from the spec so the serial queue cannot starve.

**A permanent `fatal` ends the respawn loop**: sidecar stopped, sends refuse definitely, only `ready` clears it. `sidecar_argv=()` (both deployment shapes) listens and supervises nothing. Unlink alerts once per transition, admin-addressed, pushed off-surface, reason via `task_alert._slug`. `whatsapp.baileys_session` surveys, never repairs.

**The bridge starts from scheduler boot (`baileys_runtime.py`), not `serve.py`**: on Ansible the web unit never sends, and the bridge's asyncio primitives must live on the runtime loop `deliver_whatsapp` uses. `whatsapp.baileys_bridge` SKIPs outside that process (after its `library_version` arm).

### Pairing

**`istota whatsapp pair` picks its mode by whether a bridge answers the socket.** None: spawn a sidecar, draw the QR, wait for `ready`. Bridge: **attach** — write the durable pairing request and follow the relay file. The session directory is single-writer; attach is safe because the bridge does the destructive half. The probe cannot see an external unit's sidecar while the daemon is stopped, so remedies name both.

**`--reset` (ISSUE-496).** After a logout, `creds.json` blocks any QR. `BaileysBridge.reset_session` moves the directory only after its supervised child exited. `SessionResetRefused` (nothing touched): no permanent fatal; no `sidecar_argv`; stopping; already used; **a supervisor that outlived the wait** (`_await_child_or_fatal` nulls `_process`, so the liveness check cannot see a loop that would wake and spawn). Waits bounded here; gates **re-read after the waits**; link closed afterward. A post-rename failure renames back; if that fails, `SessionResetIncomplete.moved_to` names the only copy. **One reset per bridge** (`_reset_used`). Archives are **timestamped siblings**, one `rename(2)`, never deleted, name chosen by bounded `lexists` probing (the test freezes the clock). Refusal tests use `match=`.

**`restore-session` (ISSUE-504)**: validates the archive with `ensure_session_dir` first, parks the live dir via `archive_destination` (or removes it if it holds only `sidecar.log`), renames in, unwinds on failure, clears the pairing row. Newest = newest `dir_holds_a_session` accepts; `--date`, `--list`. Refuses with a live bridge; always warns about an external sidecar; displacing a live session needs the typed unlink phrase.

`--reset` needs no phrase in own-sidecar mode (permanent fault only) but does in attach mode (`pairing_force` can disconnect a working session; TTY only, phrase asserted against the Svelte source). Remedies name `--reset` where plain `pair` would do nothing. The QR is drawn in-process by `segno`, so the payload reaches no argv. The daemon uses only `sidecar_command`; `in_tree_sidecar_argv` is `pair`'s alone (a daemon fallback would spawn a second sidecar).

### Re-pairing through the running bridge

Admin requests from Admin, Connections; the scheduler's poll hands it to the bridge; the bridge sends a `shutdown` frame, moves the directory after the drop it caused, and opens a TTL window in which `_handle_qr` relays codes.

- **Two channels**: request/state in the `whatsapp_runtime` row; the code in a 0600 relay file, **never the DB** (backups go to the mount). Standalone puts the relay under the per-user temp root; the route refuses a relay path inside a sandbox-bound root.
- **The frame is the evidence.** Waiting for `connected == False` is wrong: a new sidecar binds `saveCreds` before its link connects.
- One restart is spent at the write; aborts after it name that and leave the directory. `sidecar_absent` (no sidecar within `restart_interval_seconds + 30.0`) costs nothing.
- **`force`** accepts disconnecting a working session; the web also needs `confirm_disconnect`; neither defaults server-side (409). Carried in `pairing_force` because the poll calls `repair_session`. Never derived from live state.
- **`_move_session_aside` is shared with `reset_session`**. On the spawned shape `repair_session` delegates to `reset_session` (the child respawns within a second); no forced case there. `RESET_COOLDOWN` limits the daemon.
- **Send gate is three-state**: `_repairing`, open window, or `_session_unpaired`, cleared by `ready`. Tests assert the row settles `failed`, not `unknown`.
- **`_session_unpaired` producers (ISSUE-506)**: `repair_session`, `reset_session`, `adopt_pairing_window`, a `qr` frame, and `start()` (an expired window across restart, or a never-paired host; the sidecar's `not_connected` guard passes before the connection opens). `start()` reads **`session_is_registered`**, which fails toward *unregistered*, not `dir_holds_a_session`, which fails toward True. `BridgeStatus.session_unpaired` lets doctor tell "pair it" from "logged out".
- **Poll every tick, inline**: `IntervalGate` with `fixed_interval=0`, not `background` or `one_shot`; claims to `servicing` in one `BEGIN IMMEDIATE`, then `async_runtime.spawn_task`; a refused spawn reverts the claim.
- **Two expiry arms**: deadline (any non-terminal row past `pairing_expires_at` → `expired`) and orphan (window states only, only in the bridge process). A single "no window behind it" rule would kill a fresh `requested` row. Re-pairing an emptied directory re-opens rather than re-archives.
- **Adoption (ISSUE-504)**: after a scheduler restart mid-window, `adopt_pairing_window` rebuilds the window from the row, **writing nothing**: monotonic deadline from the row, `opened_at` now, **`qr_seq` seeded when the row says `awaiting_scan`** (else a post-scan `ready` would fail the row and prompt a re-pair that archives the new credential). Refuses: window open, stopping, deadline passed, session `ready`. Sets `_session_unpaired`. Arm order in `_expire_stale_pairing`: missing id, deadline, no-bridge/not-window, live window, `last_pairing_outcome`, adopt, close (only for an unparseable deadline or refused spawn, naming `restore-session`).
- **Web**: five admin routes plus the index; SSE at 1 Hz via `webui.shutdown.sleep_unless_shutdown`; `qr.svg` server-rendered, `no-store`, `seq` a cache-buster echoed in `X-Pairing-Qr-Seq`. `pairing_enabled = false` 404s the routes only; the index and the poll stay. The card offers one-click re-pair when latched or unreadable from this process (the unforced start refuses as `session_live`).
- **Doctor**: `baileys_bridge` reports an open window before `connected`/`ready`; `pairing_relay` `lstat`s the relay file; `baileys_session` walks archives too (`session_archives`, sharing `ARCHIVE_STAMP_FORMAT`). Survey only.

## Config, credentials and deployment

Structural errors fail the load; **missing credentials do not** (Ansible renders them empty under `istota_use_environment_file`; ISSUE-058). Both are per active provider. `graph_api_version` takes `v25` or `v25.0`. `business_phone_number`: required under Cloud, format-checked under Baileys (lets `address_for_binding` message a number-enrolled user first).

Cloud settings live in `[whatsapp.cloud]`; the flat spelling loads through `_migrate_whatsapp_flat` (`normalize_legacy_document`), a pre-walk step, not a `config_mapper` hook (a hook would replace the section walk and cannot set `provider`). **The provider signal comes from the resolved `cloud` table**: any Cloud id or credential there selects `whatsapp_cloud` when `provider` is absent.

The transport registers on `enabled` alone. `is_whatsapp_configured` checks transport, binding, opt-out, and when metered the circuit and cap, **not** the window. Generators render `provider` **only when named**; empty is refused.

The sidecar is its own systemd unit / compose service; `sidecar_command` stays empty, and the role refuses both together. `ISTOTA_BAILEYS_SOCKET` and `ISTOTA_BAILEYS_SESSION_DIR` are required and compared to `default_socket_path` / `default_session_dir`. Unit: `Wants=` not `Requires=`; `RestartSec={{ istota_whatsapp_baileys_restart_sec }}` (30), the same variable rendering `restart_interval_seconds`; `ReadWritePaths` narrowed to data.

### Logout backoff (ISSUE-498, ISSUE-501)

`RestartSec` does not bound logins against an unlinked number (~2,400/day, a ban risk), and `MAX_START_FAILURES` misses the logout branch. The sidecar waits before exiting: none, 30 s, 5 m, 15 m, 30 m, 1 h (clamped), 500 ms floor for the frame.

- `logout-backoff.json` (0600) is written **before** the wait; an `open` deletes it. Bad reads degrade to run 1.
- If the state cannot be recorded (write failed, or read failed with non-`ENOENT`), the run is `unrecorded` and floored at `LOGOUT_UNKNOWN_RUN`; a failed write never shortens the rung read. A second `fatal` with `run_unrecorded` reports it (`BridgeStatus.fatal_run_unrecorded`, doctor's unlink arm).
- The wait watches `creds.json` (mtime and size, removal counts) so a re-pair is prompt. **Baseline at the first poll, not at scheduling**, or our own late `creds.update` ends every wait. `creds.update` is behind `mine()`; the socket is nulled. Control: `test_a_write_landing_before_the_first_poll_does_not_end_the_wait`.
- The logged-out branch is one-shot; the dropped socket makes sends definite; the daemon link keeps reconnecting and re-sends the fatal.

### Updates (ISSUE-497)

Both update paths diff `docker/whatsapp-baileys/` and run `npm ci` (unit stopped, under the update lock) when the lockfile moved or `node_modules` is missing. **Neither restarts unconditionally** (restarts churn a watched link): the cron restarts only on a non-empty diff, else `start`s; the play restarts the unit from the checkout task. One webhook receiver serves location, SMS and WhatsApp on every shape.

## Residuals

- A never-writable session dir sits on `LOGOUT_UNKNOWN_RUN` (~290 logins/day); doctor reports it.
- The backoff keeps a de-paired sidecar alive continuously (wider two-client window); with the daemon down it appends to an unrotated `sidecar.log`.
- `writeFileSync` for backoff state follows symlinks, like the `sidecar.log` append; fix both or neither.
- **No advisory lock on the session directory**: `pair` cannot see an external unit's sidecar, and `repair_session` cannot see a rejected second sidecar (doctor WARNs on refused connections). The fix is a Node-side lock.
- Re-pairing has no cancel for `servicing`, no reclaim for a dead claimant (the deadline arm covers it), and must not become a casual restart or grow retries.
- A `pair` beside an own-sidecar `pair` writes a request nothing services; the deadline arm clears it.
- An unlistable state root reads as "no archives"; the archive walk grows with re-pairs.
- `qr_seq` can lead the relay file; tests wait on the file.
- The `shutdown` frame is unreachable on daemon shutdown; spawned sidecars are SIGTERM'd.
- Nothing reaps a stale claim on SMS or WhatsApp.
- A failing `npm ci` leaves the sidecar stopped and the marker stuck (`set -e`, no `|| true`).
- A refused adoption leaves the old relay file until the deadline (≤300 s, rotated code). Archives are never swept.
- An unpaired host burns a `logical_key` per message; `is_whatsapp_configured` still says True.
- `session_is_registered` reads an unverified Baileys field; both misreadings are bounded.
- `_status.ready` is cleared by a transient fatal with no later `ready`, so adoption can set a latch nothing lifts.
- A first pairing cannot happen inside compose (no Node in the istota image); re-pair can.
- **Baileys pinned to `7.0.0-rc14` on purpose**: chats are LID-addressed and 6.7.x has no PN→LID mapping; it also fixes GHSA-qvv5-jq5g-4cgg and drops delivery ACKs. Pinned exactly because `6.17.16` outranks every 6.7.x. ESM-only, loaded with `await import` so the sidecar stays CommonJS and testable without `node_modules`; Node 20+.
- No tier connects to WhatsApp, runs the sidecar image, or executes its unit.
- `istota_update_only` plus `istota_web_only` skips the `/usr/bin/node` guard (ISSUE-494).
- A hand-written unit moving the session dir without `ISTOTA_BAILEYS_MEDIA_DIR` stages where the daemon never reads.
- A LID contact with no usable `senderPn` cannot use the surface (`@lid` refused).
- Retries after a sidecar restart cannot be served: `getMessage` uses the last 256 bodies, in memory only.
- `CIPHERTEXT` stubs are withheld (forwarding would claim the id); a never-redelivered message leaves only a log line.
- The reply direction is tested by hand only.
- The staging ceiling cannot be enforced on Baileys; bounded by per-file cap times queue depth.
- An uncommitted batch is re-staged, leaving extra inbox copies.
- The sniff-then-stage tail is duplicated in both adapters; extracting it to `media.py` is a follow-up.
- PyWa logs Graph bodies and media URLs at DEBUG.
- Cloud staging resolves identity before the inactive-provider gate (narrow).
- The ceiling is checked against the per-file cap, not the declared size.
- The Cloud fetch's 60 s deadline is a hang bound; Meta publishes no tolerance.
- Whether HEIC ever arrives as an `imageMessage` is unsettled; one iPhone photo settles it.
- `doctor.whatsapp.media_staging`'s census is unbounded.

## Groups

A group the bot is in becomes a room on Baileys (multiplayer Stage 18, D6); general rules in `transport.md`. Cloud `group_id` messages are refused.

- **A container, not a surface**: `IncomingMessage.room_container` makes `is_room_member_for` return `member`. Since phone rooms `SURFACES` records whatsapp as `member` anyway, so the binding separates a group from the private chat: a group is bound by its JID and registered by `groups.py`, the private room by the user's hash ref, which `is_group_task` and `routing.phone_room` both test for. **Neither has a web composer** (ISSUE-585). The group kept one at first, for no recorded reason, and it did nothing useful: a web send is a `source_type="web"` task, `is_group_task` requires `whatsapp`, so neither the member's words (we cannot post as them) nor the answer reached the group, and the speech gate left an unaddressed turn unanswered. The send route refuses it with 409 and the group wording. What the group keeps that the private room does not: web still confirms or declines its parked questions, and it still takes members, since both refusals are the narrower private-thread test (`routing.phone_transcript_surface`). The refusal runs ahead of `!<bot> off|on` as it does for a private room, so the D8 veto is used in the group on WhatsApp; a member added from web who is not in the group can read and leave the room but not veto it. That is deliberate: an exemption would need a composer for one command, and a turn from outside the group is the side channel this closed.
- **Token** `whatsapp-group-<sha256[:24]>` of the JID, never the JID (old JIDs embed a number); `surface_ref` holds the JID.
- **The roster registers the room**: `group_roster` before the first message and on `group-participants.update` / `groups.upsert`; `groups.apply_roster`. Host: whoever added the bot (`author`, else `owner`), else the first principal. No istota user: dropped (`group_unregistered`). First roster is the epoch baseline. A user whose refs all left is removed; nobody is judged gone while a LID-only entry remains.
- **Identity is read-only**: `identity.group_member_user` never latches, enrolls or alerts. An unmapped LID is a guest, relabelled when mapped.
- **Addressing**: sidecar's `mentions_bot`; a quote of a stanza id in `sent_whatsapp.meta_message_id`; the bot's name as first word.
- **Sends**: only the group's own turn sends to the group JID (re-resolved, non-metered, no window); other tasks' legs are dropped, never redirected. No per-user opt-out. Approved `room post`s and guest proposals go in. Archived groups get nothing.
- **The 1:1 is where a group's private replies go** (ISSUE-608): `rooms.private_replies.private_room_for` picks the member's private WhatsApp room first for a group (never minted for a push; a member who never messaged the bot falls to web, Talk or the bell). `send_private` sends `re: <room>` under `private-reply:<messages.id>`, so a quote resolves back to the tagged row and links the turn (`quoted_private_reply`, run only when the relay matcher did not claim the event); a quote of a linked turn's answer resolves through `task-result:`. Confirmations carry `!confirm <id> yes|no`; a bare YES there runs Path B only and does not reach them. With no private room the group gets the fixed notice under `private-notice:<task>:<hash>`, never the key the question's bell push claims.
- **D14**: host leaves → `rooms.policy.lose_host`, archive, `leave_group` after commit (retried on next roster); a bot-removal archive reverses on re-add.
- **Veto**: `!<bot name> off` precedes member commands and the guest path. Removing the bot switches the room off (`rooms.veto.switch_off_by_removal`) until a member's `on`. Vetoed messages claim their id only (`group_vetoed`).
- **Wire**: `group_roster` up, `leave_group` down, inbound `sender_jid`, `sender_lid`, `mentions_bot`, `mentions`; version unchanged, sender-less frames refused, group media refused.
- **Mentions (ISSUE-601)**: WhatsApp writes `@<LID or number>` into the body and the phone renders the name, so the sidecar sends `mentions` (`[{token, jid, lid, bot}]`, only tokens the body carries, capped at 32) and `groups.render_mentions` rewrites each token before the turn is recorded, classified or gated: the bot to `@<bot_name>`, a participant of this room with a `user_id` to that user's display name, any other participant to the name they last posted under, else `@member`. **Names come from the room's own `room_participants` rows, never from `group_member_user`**: the list is whatever the sender's client put in `mentionedJid`, so resolving through the bindings would let anyone in the group learn whether an arbitrary number belongs to a user of this installation and what they are called. The bot's entry carries `bot: true` and no ids; its token is the user part the body already held. A malformed entry is dropped rather than refusing the turn, and keeps its token. Outbound mentions (`mentionedJid` on a send) are not built.
- **Classifier mode**: `groups.classify_group_event` decides before `BEGIN IMMEDIATE`, passed as `classified=` to `record_inbound`.
- **Owed before merge**: a real group with a guest number, and three unverified fields (`participant` / `participantAlt` / `participantPn`; `groupMetadata` ids under LID; which event fires on add, with which author).

## Task sends

`relay/requests.py` owns task intent. The `whatsapp` skill persists a self-send under the proxy's trusted identity; it never calls an adapter and takes no address. `drain_requests` delivers through the ledger and its gates. Self-sends survive task failure; keys are immutable and user/task-scoped; retained hashes stop resurrection; receipts update request status in the ledger transaction. `whatsapp status` refuses relay rows. Relay questions share the table and, for WhatsApp destinations, this ledger; rules in `relay.md`.
