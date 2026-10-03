# Leaf modules

Single-purpose modules whose reasoning does not fit anywhere else. Each is a leaf: paths and policy are parameters, most import nothing from the package, and most never raise.

## config_mapper.py

The mechanical half of `load_config`: walks `dataclasses.fields` over the `Config` tree and maps a parsed TOML document onto it, instead of restating the schema as hand-written `if "x" in data` lines. That second copy carried three defect classes, each a thing a duplicated schema does:

- **A field the loader never read.** Declared, rendered by Ansible or Docker, documented, and silently defaulted: `security.sandbox_ro_paths` and `scheduler.max_subtasks_per_task` (a cap on prompt-injection blast radius) among them.
- **Two defaults for one field.** The dataclass said `True` and the loader's `.get(key, default)` said `False`, so a bare `[sleep_cycle]` header switched nightly memory extraction off. Here the dataclass default is the only default.
- **A typo that did nothing.** `[breifings]` or `max_subtask_dept` was discarded silently. The walk knows the whole schema and reports unknown keys as a warning, not a refusal, since refusing to boot turns a forward-compatible config into a hard failure on rollback.

It does not own judgement: anything that validates, migrates a legacy key, reads two fields to decide one, or builds something that is not a plain field stays hand-written in `config.py` and is registered here as a hook against its dotted key.

## executor_stream.py

`TaskStreamAdapter`: the brain's `StreamEvent` stream adapted to `TaskEvent`s, one instance per task. It holds separate coalescing buffers for answer text and reasoning (they render to different places on a stream surface), the narration gate, and the delta-vs-whole-turn dedupe. `on_event` goes on `BrainRequest.on_progress`; `execute_task` calls `flush_thinking` / `settle_at_tool_boundary` / `finish` at the reroute boundary and the end of the run. Stateful and single-threaded: events arrive serialized, so nothing locks. `task_is_stream_surface` is imported inside `__init__` for a test reason, not a cycle: two suites patch the name through `istota.transport.registry`, and a module-scope binding would make both patches inert while the tests still passed.

## rooms/surfaces.py

What role each surface plays in the room model, in one table, replacing hardcoded surface-name lists scattered across five files. Three different questions shared those literals, and the table answers two:

- `room_role`: whether a surface creates and owns rooms. `member` for talk, web, sms and whatsapp (the two phone surfaces each own one private room per user that they are not a view of); `guest` for email, which joins an existing room's transcript and never mints one (ISSUE-136's "existence, never creation" rule).
- `room_view`: whether the surface has a view of the room, so a turn written into the room is already in front of its users. `canonical` where the view renders from our `messages` store, `external` where the store is somebody else's.

The third question, whether a surface may deposit a `role='user'` row in a room, deliberately stays a literal in `db.py`: it admits email, and converting it to the ownership predicate would have stopped an email `!confirm` recording what its docstring calls a durable authorization record.

**The durable-place test** makes email's `None` principled: is there a durable, addressable place a person opens to read the whole conversation, that we can write into? A Talk room yes; an email thread no. Bidirectional sync is not a field: it is the conjunction of the first two, and a flag would allow states no surface can implement.

**Static on purpose.** `routing._room_view` reads the same fact through a config-built registry and collapses "not a room view" with "surface not resolvable": safe for a delivery planner branching only on `canonical`, unsafe here, where with `talk.enabled = false` `webui.app._user_row_display` would render every historic Talk turn as external and the confirmation gate would put the prompt on the mirror Talk leg. Each docstring names the other.

`origin_surface_for_source_type` is likewise not `registry._surface_for_source_type`, which answers "where do I deliver this" and maps every non-surface source type to `talk`. Used for "where did this originate", the negated confirmation gate would park cron, briefing and heartbeat tasks with the question delivered nowhere until `expire_stale_confirmations`. `tests/test_room_target_no_origin.py` pins the two apart.

Each transport still declares its fields on `TransportCapabilities`, where somebody adding a surface looks; a test holds the two in step, and another requires a record for every surface `make_registry` can produce. Every reader takes `object` (values off database rows) and answers "not a room surface" for anything unrecognised, which is safe at every site except `webui.app._user_row_display`, where it renders a genuine Talk turn as external. Full reference in `.claude/rules/transport.md`. stdlib-only leaf, imports nothing: `transport` imports `db` at module level, so a table both read has to sit below them.

## webui/shutdown.py

Whether the web process is stopping, in one place the three SSE generators can see (`/chat/stream`, the task stream, the admin log tail). They poll until the client goes away, so shutdown ran out uvicorn's graceful window and then cancelled them, logging `CancelledError` as `ERROR: Exception in ASGI application`. uvicorn gives a generator nothing to observe (`request.is_disconnected()` stays False; the lifespan shutdown event fires after the connection wait), so the signal is made here. `install_signal_hook` wraps the SIGINT/SIGTERM handler uvicorn installed, and `sleep_unless_shutdown` wakes every sleeping stream at once so each returns and completes normally.

- **Called from the web app's lifespan, not `serve.py`**, because that is the one startup path shared by `istota serve` and plain `uvicorn istota.webui.app:app`, and it runs after `capture_signals` installed the handler being wrapped.
- **A non-callable handler (`SIG_DFL`/`SIG_IGN`) is left alone**: the default SIGINT disposition is what ends the process, and replacing it would break Ctrl-C.
- **A notice, not a boundary.** `serve.py`'s bounded `timeout_graceful_shutdown` and its force-quit abort remain the backstops. Nothing raises; a hook that cannot be installed is a debug line.
- **Nothing on the notice path may take a lock.** A signal handler runs on the main thread between bytecodes, and that thread is the streams' event loop, so a lock could deadlock against the interrupted code and swallow the stop signal. State is a plain flag and list (atomic under the GIL); the register/signal race is closed by ordering (flag published before the waiter list is copied, sleeper re-reads it after its append). The wake is `call_soon_threadsafe` on each waiter's own loop, the one signal-safe loop method. Delegation to the wrapped handler is guarded.
- **Starting a server resets the latch.** uvicorn restores pre-server handlers on return, but the latch would persist, and a second server in one process would answer every stream at its first check.
- **Signal-driven, with one exception**: `serve.py`'s supervisor sets `should_exit` when the scheduler thread dies with no signal, so it raises the notice itself.

stdlib-only leaf, imports nothing from the package.

## webui/router_stubs.py

The auth and CSRF stubs every module router declares so it stays mountable on its own, plus the user-context factory three of them share. `briefings/routes.py`, `feeds/routes.py`, `webui/garmin_routes.py`, `health/routes.py` and `money/routes.py` each declared byte-identical `require_auth` and `verify_origin`. They exist so a router can be included in a bare `FastAPI()` and tested with no session middleware; `webui/app.py` replaces both through `app.dependency_overrides` at mount time. `verify_origin` returning `None` is the seam the host fills, not "CSRF is off", which is why `webui/app.py` sets that override on the same line it includes the router.

**Sharing the function object matters.** `dependency_overrides` is keyed by the callable, so the five keys (all set to `_require_api_auth` and `_verify_origin`) collapse to two, and no caller can override one router's auth and leave another's on the same app. A router needing its own gate declares one (`briefings.require_admin`). `tests/test_web_router_stubs.py` asserts the override values, since the collapse stops being safe the day one router needs a different gate.

`make_get_user_context` covers the three routers whose `get_user_context` differed only in the module's resolver, its `UserNotFoundError`, the `app.state` cache attribute, and whether `ensure_initialised` takes the config. Each call returns a **distinct** function object, unlike the stubs, because route suites override one module's context per app and must not reach the others; only `tests/test_web_router_stubs.py` would catch a cached closure. `money/routes.py` (a `get_user_config`) and `webui/garmin_routes.py` (no per-user resolver) take only the two stubs. FastAPI only, no config and no DB, so a router imports this before anything of its module's.

## usage/telemetry.py

Normalized per-attempt token/cost telemetry (`BrainUsage`, `ModelUsage`, `from_cli_result`, `from_task_usage`). The one place each brain's reporting shape is converted to one vocabulary, so the schema and read surfaces never learn which brain produced a row. Pure: no DB, no config, no brain imports, and neither adapter raises, since they sit on the brain's return path.

## usage/render.py

The cost render rule for token-usage surfaces, in one Python place: `COST_PLACEHOLDER`, `render_cost`, `fmt_money`, `fmt_int`, `fmt_context`. A currency figure appears only where `cost_basis = 'api'`, and nothing is summed across bases: a subscription's list-price equivalent and a catalog estimate both read as spend at a glance. It is here rather than in `cli.py` because there are two Python importers: `cli.py` has a heavy import graph, and `commands.py` (`!usage`) is on the Talk polling path. `web/src/lib/usageFormat.ts` states the same rule in TypeScript; `tests/test_cli_render_cost.py` and `usageFormat.parity.test.ts` hold the two languages together, and a third Python copy would make that harder. stdlib-only leaf.

## usage/subscription.py

The Claude Code plan's rate-limit windows, from `GET https://api.anthropic.com/api/oauth/usage`. One fetch, one parser and one deployment-wide disk cache (`{db_path.parent}/subscription_usage.json`) shared by three surfaces: the `runtime.subscription_usage` doctor check, the `/admin` card and `!usage`. On a subscription deployment the cost column is blank (a plan-equivalent list price is not spend), so these percentages are the real budget.

- **A fourth reader is not a surface.** `cached_reset_seconds` / `soonest_reset_seconds` tell the brain-availability breaker when a quota comes back (ISSUE-374). They read the disk cache alone, no credential and no socket, because they run on a task's failure path; any cache age answers, since `resets_at` is absolute. They return `int | None`, the one exception to the snapshot rule below, and never raise.
- **Prefers the payload's `limits[]` array** over the top-level window keys: the array carries a display-ready scope name, while the top-level namespace is shared with unreleased codenames. The fallback renders an allowlist and drops the rest, so an unshipped feature name never reaches a public project's dashboard.
- **The credential is read, never written or refreshed.** The server's `setup-token` has a string sentinel expiry where the keychain blob holds epoch milliseconds, it has no refresh token, and a daemon rewriting `~/.claude/.credentials.json` would race the `claude` subprocesses it spawns.
- **Nothing raises.** Every other entry point returns a `UsageSnapshot`, and a failure is a snapshot with a non-empty `error`, because one caller is the daemon's boot sequence. A failure is recorded beside the cache so the TTL is also the retry interval. Only failures the endpoint produced go in that shared file; "no credential here" is a fact about the calling process and is bounded process-locally.

stdlib-only leaf, paths are parameters.

## lib/image_sniff.py

Which bytes are images, asked of the bytes and never of a name. Two predicates, each for its own caller:

- `sniff_raster`: what `/chat/files` will serve `inline` on the app's own origin. PNG, JPEG, GIF and WebP by signature, never by extension, because the name is caller-supplied on a file the model wrote and an SVG named `.png` is the case that decides it. Everything else stays `attachment`, the default and the security position, since the workspace holds user- and model-authored HTML and SVG. A hit is served with the sniffed type sent explicitly plus `nosniff`, so a file that is both valid PNG and valid HTML stays an image. HEIF is left out though `webui.avatars.ACCEPTED_FORMATS` admits it: browser support is not universal, and an inline type that does not draw is worse than an attachment.
- `sniff_decodable`: what the image pipeline can decode, which adds HEIF for inbound WhatsApp media. Widening `sniff_raster` instead would have changed `/chat/files` silently. See `.claude/rules/whatsapp.md`.

**No Pillow.** A magic-number test must not decode; Pillow's peak memory is why `webui/app.py` serializes avatar decodes on one worker, and a download route must not join that queue. A leaf so the skill side shares the predicate (`browse screenshot` uses it) instead of a second table. stdlib-only leaf, imports nothing, never raises.

## lib/audio_sniff.py

Which bytes are audio the transcription pipeline decodes, asked of the bytes and never of a name or a declared mimetype: `sniff_audio`, plus `EXTENSION_BY_MEDIA_TYPE` for naming the copy. The caller stages a file off a messaging surface (a WhatsApp voice note), and the executor's pre-transcription pass screens by suffix, so a copy named with a suffix outside `AUDIO_EXTENSIONS` is skipped in silence. Ogg, MP3 (frame sync or `ID3`), ADTS AAC, MP4 by an audio-capable major brand, WAV, FLAC and WebM.

- **The sniff confirms, the message type chooses.** An `isom` file is as much video as audio, so the surface's declared type decides which pipeline a file may enter and this module only says whether the bytes match it. The `ftyp` arm reads the major brand alone for the same reason.
- **MP3 and ADTS share the `0xFFF` prefix** and are told apart by the layer bits (`01` layer III, `00` ADTS). MPEG 2.5 has eleven sync bits, so the MP3 test reads three ones, not four. Layers I and II are not admitted.
- **AMR is not admitted**: it is in neither `AUDIO_EXTENSIONS` nor whisper's `file_types`, and the deployed decoder is unverified.
- **`VOICE_TRANSCRIPT_LABEL` lives here too** (ISSUE-613), for the same import reason: the executor writes it into the prompt, the scheduler into the WhatsApp room's rewritten turn, and `transport` must not import `executor`. `Message.svelte` keys its italics on a copy, held equal by `tests/test_scheduler_attachments.py`.
- **`AUDIO_EXTENSIONS` lives here**, re-exported as `executor._AUDIO_EXTENSIONS` (which `webui/app.py` imports), because `transport` needs the set and must not import `executor`. `tests/test_audio_sniff.py` holds it equal to the whisper skill's `file_types` and every `EXTENSION_BY_MEDIA_TYPE` value inside it, since this module imports nothing to say so itself.

No decode. stdlib-only leaf, imports nothing, never raises.

## webui/map_basemap.py

Where the map's background tiles come from, decided in one place (ISSUE-334). `LocationMap.svelte` hardcoded `basemaps.cartocdn.com`, which now watermarks unauthenticated tiles. A provider name plus a few `[web.map]` strings resolve to the concrete URLs the browser fetches; adding a provider is a row in `PROVIDERS`.

- **Two consumers that must agree**: `GET /istota/api/map/basemap` and doctor's `web.basemap`; a second copy of the URL shapes would let doctor pass on a blank map.
- **Never raises and never returns an unusable spec.** An unknown provider, a `custom` with no URL or a non-http(s) one, and a keyed provider with no key all fall back to `openfreemap` and set `fell_back`. Returning the keyless CARTO templates with a `needs_key` flag would be the original bug with a label, since a browser cannot act on a flag. The flag survives on the fallback spec as the reason, so doctor can say "carto, with no key".
- **A user's own stored CARTO key** (`MODULE_SERVICE_SCHEMA["location"]["carto"]`) selects CARTO for that user, overriding `provider`, because otherwise pasting a key would do nothing visible. The endpoint returns it only embedded in the tile URL, never as a field.
- `api_key` **is not a secret**: MapLibre puts it in the tile URL, so every browser gets it.

stdlib-only leaf.

## rooms/provision.py

Default Talk rooms (general/logs/alerts) for a user: reuse by remembered token first, participant-scoped name lookup only on a first provision, group (not public) rooms, and seeding `log_channel`/`alerts_channel` only where empty. Behind `istota nextcloud provision-rooms`, called by the Ansible role on every deploy; the bare-metal counterpart to the Docker entrypoint, which persists `GENERAL_TOKEN` in its provisioning flag file and never had the bug below.

**The token record is the ISSUE-342 fix**, in the reserved `_provisioned_rooms` KV namespace. A display-name match cannot survive a user's rename, so the next deploy minted a second room; `general` was the usual victim because `CHANNEL_FIELDS` gives it no column. Reusing a remembered room never counts as `seedable`, so a channel column the user cleared on purpose is not refilled.

**The record carries the invite outcome too, and the orphan-adoption retry is gated on it** (ISSUE-408). A bot-only room is equally what a failed invite leaves and what a user leaves by walking out of their own `general`; reading it as the first meant every deploy re-invited them, with no opt-out. Only a token whose last invite is recorded as failed is retried; the name-matching arm is untouched. Three consequences, each a decision:

- A pre-ISSUE-408 record has no outcome and reads as "no failure". Leaving works from the first deploy after the upgrade; a genuinely stranded room is not auto-retried and reports `user not a member` rather than `existing`. Clearing its KV key returns it to the name arm.
- A run that observed nothing (`_is_orphan` treats an empty participant list as a failed read) carries the previous outcome forward, or one transient Talk error would erase a recorded failure for good. So `record_invite_failed` ("is an invite outstanding", persisted) differs from `invite_failed` ("did this run try and fail", read by the CLI warning and the Ansible `failed_when`).
- `--adopt` records no failure, since it never contacts Talk.

## lib/rclone_client.py

The rclone API `storage.py` and `skills/files/__init__.py` each had a copy of: `rclone_run` plus the `mkdir` / `path_exists` / `cat` / `rcat` wrappers. `subprocess.run` raises on a missing binary, so a `FileNotFoundError` escaped every helper documented to return `None` or `False`, reachable wherever the no-mount fallback runs without rclone installed; the fix had to land on both sides, so the pair was merged. `setdefault` rather than fixed keywords, so a caller passing `text=False` gets its own value rather than "multiple values for keyword argument".

- **A leaf rather than an import of `storage`** because `skills/files` runs in a skill subprocess and `storage` pulls in the package. `storage.py` keeps the private names as aliases so its callers and tests are unchanged.
- **Only shared code lives here.** `rclone_list`, `rclone_move`, `rclone_download`, `rclone_upload` and `_rclone_run_or_raise` stay in the skill, which is their only caller.
- **The pin is a source scan, not a mock.** `istota.storage.subprocess` and `istota.lib.rclone_client.subprocess` are the same module object, so patching `lib.rclone_client.subprocess.run` cannot tell a reintroduced local copy apart. `tests/test_rclone_client.py` patches `lib.rclone_client.rclone_run`, which a local copy never calls, and asserts neither converted module contains `subprocess.run(`.

stdlib-only leaf: `subprocess` and `logging`.

## lib/sqlite_util.py

One SQLite open, with each caller's pragma set as parameters, replacing many helpers that each issued a subset of the same four pragmas. Three entry points for three caller shapes: `open_db` (a context manager), `connect` (bare, for `money/cli._get_db_conn` and `money/routes._portfolio_conn`, which hand a live connection on, and `room_relocate`'s migration, which passes `create=False` so a wrong path raises rather than becoming an empty database), and `connect_read_only` (`doctor`, `storage.channel_memory_tokens`, `room_mount_reconcile` and `room_relocate`'s `--dry-run` / `--list`).

- **There is no `journal_mode` parameter, and adding one would be a defect.** WAL is persistent in the file header, so each store's `init_db` issues it once. Re-issuing it per open takes a write lock that races sibling readers, the recorded cause of a dispatch-loop stall (argued in `money/config_store.init`). `tests/test_sqlite_util.py` asserts the parameter's absence. Three `init_db` bodies keep their own `sqlite3.connect` for this reason: they are the only place `journal_mode=WAL` is issued.
- **`timeout` already is a busy timeout.** `sqlite3.connect(timeout=T)` sets `busy_timeout` to `T * 1000`, so `busy_timeout_ms` is an override, `None` is not "no busy timeout", and asserting `busy_timeout == 30000` on a `timeout=30.0` connection proves nothing about the pragma. That is why the pin is a matrix over every caller: `foreign_keys`, `synchronous`, `row_factory` and `busy_timeout` split the callers differently, and no single default can move all four.
- `rollback_on_error` catches `Exception`, not `BaseException`: on `KeyboardInterrupt` the `finally`'s `close()` rolls back implicitly, and an explicit `rollback()` there can raise `ProgrammingError` over the in-flight exception.

**`connect_read_only` chooses its mode per database** (ISSUE-458), because a diagnostic may assume nothing of a database another process is using. A database with a hot journal (`_has_hot_journal`) is opened `mode=ro`; one with none is opened `mode=rw` with `PRAGMA query_only`.

- `mode=ro` alone leaves the WAL sidecars behind (deleting them on close is a write); under `sudo istota doctor` they were root-owned and locked the daemon out of its own database.
- `mode=rw` alone checkpoints an un-checkpointed WAL into the main file on last close (`query_only` does not stop that), rewriting the file doctor is diagnosing. A hot database already has its sidecars, so the `ro` branch strands nothing new.
- `immutable=1` ignores an un-checkpointed WAL and reports `no such table`, and doctor's ordinary case is a live database.
- `query_only` is a guard against a mistake, not a boundary (reversible by SQL). It must stay header-free: it is the first statement run, and a page-touching pragma would move corrupt-database reports from `check_framework_db`'s body branch to its open branch.
- `rw`, not `rwc`, so a missing database raises instead of becoming a zero-byte file that later reads as corruption.

Residuals: against a 0444 file `mode=rw` falls back to read-only and leaves sidecars; a `SIGKILL` between open and close strands them; the branch rests on a filesystem check that can be stale. None is worse than before.

**The URI path is percent-encoded** (ISSUE-461). Raw interpolation let a `?` or `#` end the path early and drop the mode, so the open landed read-write on a path nobody named and created it. `os.fsencode` so undecodable bytes round-trip, `safe="/"` to keep separators.

stdlib-only leaf: `sqlite3`, `pathlib`, `contextlib`, `os`, `urllib.parse`.

## lib/du.py

Du-style tree measurement and the first-level directory scan beneath it, shared by callers that each had a copy. `iter_tree` is the walk (`os.walk(followlinks=False)` + `os.lstat`), `entry_bytes` the arithmetic, `tree_bytes` the sum, `first_level_dirs` the sorted, symlink-skipping, non-directory-skipping scan.

- **Blocks rather than apparent size** (`st_blocks * 512`), because a volume fills by blocks. `dedupe_inodes` counts each `(st_dev, st_ino)` once, because uv's cache hardlinks a wheel into every venv and counting per link reports an overage no reclaim can clear.
- **`include_dirs` defaults off and is the one axis callers disagree on.** `sandbox_cache_sweeper` passes `True` (uv's `archive-v0` is a directory per wheel, real occupancy); `session_log`'s sweep passes `False` (a per-user directory is overhead no eviction can reclaim, and counting it would leave a many-user deployment permanently over its ceiling). A directory reports `st_blocks == 0` on APFS, so a byte assertion about `include_dirs` is vacuous on macOS; tests carry the property in the entry set instead.
- **Nothing raises**; an unreadable root is nought bytes and no directories. `ValueError` (a null byte) is caught beside `OSError` everywhere, including a guard on the `os.walk` iteration itself, since CPython wraps only `OSError` around the root `scandir`. `on_error` follows `os.walk`'s convention and skips the `ValueError` arms.
- One scan is deliberately not converted: `maintenance.sandbox_cache_sweeper._sweepable_entries` must yield a symlinked entry (its `ACTION_OUTSIDE` planted-symlink detector), which `first_level_dirs` skips by construction.

stdlib-only leaf: `os`, `pathlib`.

## sandbox/net_guard.py

Whether an address is a routable public one: `ip_is_public`, the blocklist and operator CIDR parsing, lifted out of `session/tools/web_fetch.py` when the `wordpress` skill became the second daemon-network caller fetching a URL somebody else chose. A leaf rather than an import of `web_fetch`, because importing that from a skill pulls in the native tool package (about fifty modules), and `web_fetch` runs in the tool server, which may not import `istota.skills`. `web_fetch` keeps `_ip_is_public` as an import alias. URL validation is not shared: `web_fetch._validate_url` is shaped by `WebFetchPolicy`, while the skill's URL rule is the credential binding (`skills/wordpress/sites.check_bound`). stdlib-only leaf, never raises.

## lib/untrusted.py

One fence around content somebody else wrote: `frame_untrusted(text, label)` puts `text` between markers naming the source, and **redacts both markers out of `text` first**. That redaction is the whole point: four modules had their own versions and did not agree on it. `skills/nextcloud` did not redact, so a Talk room renamed to `[END UNTRUSTED NEXTCLOUD CONTENT]` closed the fence from inside (ISSUE-509); `skills/email` and `session/tools/web_fetch` did not either, wrapping the most attacker-controlled content in the tree, and were converted (ISSUE-512). Which skills use it, and with what label, is in `.claude/rules/skills.md`.

- **Redaction is looser than the emitted markers**, because a reader takes extra spaces, a tab, lowercase or an ASCII `-` for the em dash as the fence closing. `_redaction_patterns` joins label words with `\s+` and allows inner space and any opener tail, anchored on the literal `UNTRUSTED` and the label.
- `label` is stripped to letters, digits and spaces and capped, so it cannot forge a marker either.
- `text` is typed `object` and coerced, because callers pass `dict.get(...)` off someone else's JSON or a MIME parse, and `re.sub` on a non-string would turn one odd field into an error envelope for the whole verb. Falsy input returns `""`, keeping `-> str` honest; a fence around nothing is noise.

**It sits at the package root, not under `skills/`, for import cost.** Importing any `istota.skills` submodule runs the package `__init__`, which star-imports `calendar`, `email` and `files`. The tool server spawns once per task attempt and reaches this module through `session/tools/web_fetch`, so it would pay that every attempt (a function-scope import only moves it into the agent loop). Same reason as `sandbox/git_hardening.py` and `sandbox/forge_bin.py`; no re-export shim, since it had three private importers. The boundary is pinned as a module set, not a duration: `tests/test_tool_server_env.py::TestTheServerDoesNotImportTheSkillsPackage` imports `istota.sandbox.tool_server` in a subprocess, builds the default tools with a `WebFetchPolicy` so `make_web_fetch_tool`'s body runs, and requires no `sys.modules` key starting `istota.skills`.

**The fence does not cover a line outside it.** `web_fetch`'s `Fetched: <url> (HTTP <status>, <mime>)` header precedes the opening marker, so the model reads it as the daemon's words while the remote end chooses both scalars (`final_url` is raw `urljoin` over a server-written `Location`, untouched by httpx's request-target encoding). `urljoin` and h11 strip LF and CR but not U+0085, which `str.splitlines()` treats as a line break, so a redirect could forge a daemon line. `_header_scalar` collapses every line break Python recognises to a space and caps the length (collapse, not refuse: a mangled URL beats a forged line or a failed fetch). The non-text branch writes a second model-facing line, `[non-text content: …]`, so `_header_mime` bounds the mime for both; its test is parametrized over where the break is injected. VT, FF and other C0 separators are refused by `httpx.URL` incidentally and bounded anyway. Markers are not redacted from the header: it precedes the opener, and redacting would misreport the URL fetched.

**Two `_header_scalar` functions, deliberately of different strength.** The one in `session/tools/web_fetch.py` is authoritative for the web-fetch provenance header and collapses every `str.splitlines()` boundary. `executor._header_scalar` / `_one_line` is authoritative for system-prompt headers and collapses only `\r` and `\n`. `web_fetch` cannot reuse the executor's, since it is a leaf in the tool server's import graph, so the copy is forced. Whether the executor's narrower set is a live exposure is unsettled: it would need a `rooms.name` or `room_bindings.surface_ref` carrying U+0085 and a model that reads NEL as a line break. Widening it moves the prompt goldens, so it is its own change.

stdlib-only leaf: imports nothing, never raises.

## lib/toml_fence.py

Where a ```toml fence starts and ends, for the four modules that parse one from a user-written markdown file: `cron_loader` (CRON.md), `heartbeat` (HEARTBEAT.md), `user_briefings` (BRIEFINGS.md) and `money._config_io`. All four copied one expression with one defect (ISSUE-386): neither marker was line-anchored, so the block ended at the first backtick run after the opener, in a comment or a string. In `cron_loader`, which drives an orphan sweep, a truncation on a table boundary produced valid TOML holding a subset of the jobs and the sweep deleted the rest silently.

- **Every bound is loose on purpose.** The old expression had no `^` and accepted any prefix, so almost any bound is a narrowing that breaks a working file. The indent is unbounded rather than CommonMark's three spaces, a marker is `` `{3,} `` on either side, the trailing class `[^\S\n]` carries a CRLF's `\r` and a pasted non-breaking space, and a leading BOM is named separately because it is not `\s`. The fix is only that a marker must be alone on its line.
- **Two searches, not one expression**: `open(.*?)close` is quadratic with no closer, on user-writable files parsed on the scheduler tick with no timeout.
- **What a caller does with no block is not owned here.** `cron_loader` uses the markers directly (`FENCE_OPEN_RE`, `FENCE_CLOSE_RE`) and keeps its own hold guard on `BACKTICK_RUN_RE`, because a file it cannot resolve must never read as "the user has authored no jobs": that verdict is `is_template`, and it authorizes `_sync_cron_files` to rewrite the document from the table. The other three take `find_toml_block`.

stdlib-only leaf.

## lib/filenames.py

The one rule for turning a name somebody else chose into a filename: `safe_filename` and `filename_parts`. Before it, four callers had their own `[^A-Za-z0-9._-]` allowlist (health documents, web chat, the wordpress upload header, image renditions), three health upload routes took the client's suffix raw, and the email attachment path had no rule at all. That last one is ISSUE-593: a MIME filename carrying a carriage return was written into the inbox as given, Nextcloud answered the upload with a 404, and rclone retried it for a day while the file existed only in the VFS cache.

- **Two modes, one rule.** Readable (the default) keeps Unicode and spaces, since inbox names are read by people: control and invisible formatting characters become a space and whitespace runs collapse, `\ / : * ? " < > |` become `_`, leading and trailing dots and spaces go. `ascii_only` keeps `[A-Za-z0-9._-]`, for a name that goes back out in a `Content-Disposition` header (health documents, wordpress) or a store that wants it plain. An extension counts only if it starts with a letter or digit and is at most 16 characters; otherwise the whole name is the stem, so `a.pd\rf` has no extension rather than a hostile one. `.part` and `.filepart` are folded into the stem (`x_part`), because Nextcloud refuses them as partial-upload names, which is the same stuck upload. A name that is only an extension (`.png`) is an empty stem with that extension, so the caller's fallback names it (`upload.png`). The invisible-character table is built from code points, so the source carries no bidi override (a test holds the file to ASCII).
- **Idempotent by construction.** Callers layer it (the inbound poll names the file, `upload_file_to_inbox_v2` sanitises again), and splitting the extension after cleaning is not idempotent on its own, so `filename_parts` reapplies the rule until it stops changing the name, bounded at eight passes.
- **A filename rule, not a containment check.** The result is one component that is never empty, `.` or `..`, but `download_attachments` still refuses a traversing name on the raw string first, `..` spelled with backslashes included, and sanitises each component only after containment passed. Sanitising first would turn ISSUE-447's refusal into a silent rename.
- **Sanitising makes names collide, so a writer numbers them.** `a:b.pdf` and `a?b.pdf` are both `a_b.pdf`, and `write_resolved` truncates. `skills/email._unclaimed_name` gives the second one written by the same download `a_b (2).pdf`; a file already in the directory is still overwritten, since an `attachments --dest` re-run replaces what it wrote before.
- **Both sides of a diff go through it.** The inbound poll and `email attachments` compare declared attachment names with written ones; `skills/email.attachment_leaf_name` is the declared side, or every renamed attachment reads as not retrieved.
- `upload_file_to_inbox_v2` and `upload_file_to_inbox` apply it through `storage._inbox_name`, so every inbox writer is covered whoever calls them. There only the byte limit binds, not the stem cap: the poll prefixes `<id>_` to a name already capped, and capping again cut two names that differ in their tails to one. Residual: a leaf near 255 bytes of multi-byte text still loses its tail to the prefix.

Not converted, on purpose: `cron_loader._prompt_file_name` and `memory/sleep_cycle._playbook_slug` turn a label we own (a job name, a playbook title) into a slug for a file we name, and moving them would rename existing prompt and playbook files on disk. The Garmin importers' user-id sanitising is a different rule. Not covered: Windows reserved device names (`CON`, `NUL`). Display names rendered into a prompt are a different rule (`image_attachments._display_name`), since they are text rather than a path. stdlib-only leaf, never raises.

## lib/date_parse.py

Loose date parsing for text a model or a person typed (`parse_loose_date`), for the three health modules that each had a copy: `health/parser.py` (an EHR paste), `health/encounter_ocr.py` and `health/immunization_ocr.py` (a model's JSON). Two validated the ISO branch with `date.fromisoformat` and one did not, so `2026-02-31` was returned verbatim by `parser.py`. The strict copy wins, per the rule that where copies disagree on a safety property the strict one survives.

This costs nothing new: every caller already handles the same answer from the always-validated `M/D/YYYY` branch. `parse_paste` sets `date_given=None` and drops the row to `medium` confidence, which its consumers already expect (`POST /immunizations/bulk` 400s those strings; the skill's `--confirm` refuses a dateless row). Nothing back-fills.

- `raw` is typed `object`, widening the spec's signature, because two callers pass a value straight off `json.loads` and both already coerced with `str()`.
- `is_future_date` came along as a fourth duplicate. It takes `today` as a parameter so tests need no clock, and answers `False` for an unparseable string, since its job is to drop a row and "cannot parse" is not "in the future".
- Two-digit year pivot: 70.

stdlib-only leaf, never raises.

## lib/llm_json.py

Where a markdown code fence starts and ends in **model** output: `lib/toml_fence.py`'s question about a string the model wrote. The expressions it replaced had two different defects, each in its own file:

- `context._parse_relevant_ids` had a bare backtick-run closer, so a run inside the JSON truncated the block.
- The three health OCR modules were line-anchored on the closer but quadratic on repeated openers with no closer, on input whose size the model chooses. Anchoring narrows them (a decorated closer, a line-leading run inside the body), bounded because valid JSON cannot carry a raw newline in a string.

**The opener is anchored only where a wrong answer would be returned rather than tried**, which is where this departs from `toml_fence`. `find_fenced_block` keeps the anchor, since `context.py` and `transport/email/outbound.py` take its answer or fall through. `candidate_json_blocks` discards a candidate that will not parse, so it runs three arms in order, each pinned by a control that removes it:

1. Strict.
2. Relaxed opener, because prose carrying a `{` before the fence makes the widest-`{...}` span invalid and the widest-`[...]` span then answers with an inner array; `ocr._parse_llm_response` used to accept that through its bare-list branch and return a panel silently missing `drawn_at`, `lab_name` and `panel_type`.
3. `iter_opener_delimited_blocks`, opener to next opener, for a block whose closer the model forgot before starting another.

The strict arm is not redundant and the order matters: a backtick run mentioned in the prose ("wrap it in ``` when you paste it back") steals the relaxed opener, and the block then swallows the real opener line.

**The two widest-substring arms are marked, not trusted or dropped** (ISSUE-455). `candidate_json_blocks` returns `JsonCandidate(text, whole)`, and all three OCR modules take a bare list only from a `whole` candidate; a dict they take from any arm, since its key (`biomarkers`, `immunizations`, `encounters`) says it is the answer. The flag is positional, not per arm: a delimited arm is whole by construction, a bracket scan only when no opener of the other kind sits outside its span (a `{` outside the widest `[...]` signals an envelope the scan could not parse; a truncated one overlaps rather than nests, so a containment test would miss it). Marking every scan a fragment would silently refuse `"Here are the biomarkers:\n[{...}]"`, a shape all three read. Residuals, both ending at an empty payload that reaches the user as a warning: where prose after a decorated closer carries a `{` or `[`, no arm yields a parseable candidate; and a genuine bare array whose prose carries a brace is refused with the fragments. Reaching the first means changing the fence rule, not the fallbacks.

`strip_fences` replaced `health/explainer._strip_fences` and `memory.curation.prompt.strip_json_fences` (which keeps its name and delegates). Its tail is looser than its head: it drops a trailing backtick run wherever it sits and returns the body of an unclosed opener, since truncated model output is ordinary and an end-of-string closer truncates nothing.

`session/result._CODE_FENCE_PATTERN` is not converted: it removes every fenced block to find leaked tool-call XML, and line-anchoring would flag an inline ```` ```<invoke``` ```` as malformed. It is not quadratic; its comment says both.

Test traps: a fence case whose caller has a `{...}` fallback cannot fail, since the fallback rescues a well-formed fence, so each has a sibling whose prose carries a bracket. The timing fixture is `"```json\n" * N` (many openers, no closer); `"```json\nx" * N` holds one anchored opener and lets a quadratic expression pass.

stdlib-only leaf, never raises.
