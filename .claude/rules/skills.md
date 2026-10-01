---
paths:
  - "src/istota/skills/**"
---

# Skills System

## Skills Loader (`src/istota/skills/_loader.py`)

`SkillMeta` (`_types.py`): `name`, `description`, `always_include`, `admin_only`, `keywords`, `resource_types`, `source_types`, `file_types`, `companion_skills`, `exclude_skills`, `dependencies`, `requires_capability`, `env_specs`, `cli`, `experimental` (needs `skill_<name>` in `[experimental] features`), `exclude_memory`, `exclude_persona`, `exclude_resources`, `skill_dir`.

### Functions
- `load_skill_index(skills_dir, bundled_dir=None)`; `select_skills(prompt, source_type, user_resource_types, skill_index, is_admin=True, attachments=None, disabled_skills=None, sticky_skills=None, enabled_experimental_features=frozenset())` returns the sorted eager set.
- `advertised_cli_skills(skill_index, *, is_admin, disabled_skills)`: the one derivation behind the "Skill CLI tools" list and `executor.room_identity_line`'s `istota-skill rooms list` clause, so the prompt cannot name a verb it withholds (ISSUE-513). Gates keyword-only with no default, since a forgotten one silently widens the prompt. Deliberately a subset of executable: the proxy's `allowed_skills` is every `cli: true` skill. Not the menu's gate: unenabled experimental and missing-deps skills (`whisper`, `markets`, `transcribe` on a lean install) are still advertised. Closing that needs `enabled_experimental_features` at the call site; recorded, not closed.
- `eligible_skill_names(...)`: the menu's membership gate (excludes selected, `always_include`, disabled, admin-gated, experimental-gated, missing-deps). The `resource_types` gate survives here but no bundled skill declares the field.
- `expand_companions(names, skill_index, *, is_admin=True, disabled_skills=None, enabled_experimental_features=frozenset())`: gate-filtered, **one level**. Shared by `select_skills` and `skills show` so the filter cannot drift.
- `load_skills(..., user_overlay_dir=None)`, `build_disclosure_index(menu, index)` (`""` when empty).
- Overlay primitives `contained_overlay_dir`, `open_overlay_dir`, `read_overlay_bytes`, `inspect_overlay`, `overlay_effective_body`. `inspect_overlay(...).binds` is the single bind predicate for `skills overlays` and `doctor`; neither re-derives it.

### Single axis: eager body vs. menu entry

A skill is **eager** (body in the prompt because a rule in `select_skills` picked it) or in the **menu** (a one-line entry pulled via `istota-skill skills show <name>`). The eager/lazy disclosure machinery (`SkillMeta.disclosure`, `resolve_disclosure_mode`, `partition_skills_for_disclosure`, `progressive_disclosure`, `auto_lazy_threshold_chars`, `always_eager`) is gone; there is no off switch.

`always_include`: `files`, `sensitive_actions`, `memory`, `scripts`, `memory_search`, `kv`, `skills` (deferring the loader's own body would be circular).

### The menu catalogue (replaced Pass 2)

`menu = eligible_skill_names(skill_index, exclude = selected ∪ exclude_skills_of_selected)`, rendered by `build_disclosure_index`; the main model self-selects. It replaced the per-task `claude -p` Pass-2 pre-router, whose cold start timed out in production (`classify_skills`, `build_skill_manifest`, `semantic_routing*` are gone).

`skills show` renders a body with `load_skills`' strip and substitutions (`{BOT_NAME}`, `{BOT_DIR}`, `{scripts_dir}`, `{user_id}`, `{workspace}` = `/Users/{user}` via `_workspace_dir`, `{storage}` = `config.storage_label`), and re-applies disabled / `admin_only` / experimental / missing-deps guards from config and `ISTOTA_USER_ID`; refusals are an error envelope, exit 1. Runs host-side through the proxy.

**`skills show` appends companion bodies** under `\n\n---\n<!-- companion: <comp> -->\n\n<body>`; an unavailable one appends `<!-- companion <comp>: unavailable -->` and logs WARNING. This guarantees an ingest skill pulled from the menu (e.g. `browse`) arrives with `untrusted_input` in the same response.

### Skill Selection

Filters on every candidate: `admin_only` when not admin; `experimental` unless enabled (on every path: main loop, sticky, companions, menu); unmet `dependencies` (`_check_dependencies()`); `disabled_skills`.

**Capability gate.** `disabled_skills` is the effective set from `skills._loader.effective_disabled_skills(config, user_id, skill_index)`: instance + per-user disabled, plus `capability_disabled_skills(...)` for any skill whose `requires_capability` is not within `Config.available_capabilities()`. That function is the single map from capability to flag (`browser`, `devbox`, `whatsapp` from their `enabled`; `nextcloud` from `nextcloud.url`), so `browse`/`devbox` vanish from eager and menu on the standalone install. Executor, `skills` CLI and `!skills` share the helper. A new service-backed skill is frontmatter plus one line there.

Eager rules, in order: (1) `always_include`; (2) `source_type in source_types`; (3) `file_types` match on attachments; then (4) sticky skills; (5) companions via `expand_companions`; (6) remove `exclude_skills` of selected skills (briefing excludes email). Keywords and `resource_types` are **not selectors**; triggers stay as `!skills` documentation, and `prompt` / `user_resource_types` stay in the signature for compatibility.

**Sticky source**: for `talk` and `email` tasks with a token, `db.get_recent_conversation_skills(token, max_age_minutes=30, limit=2)` plus `parent.selected_skills` from `db.get_reply_parent_task()` when `reply_to_talk_id` is set. `db.save_task_selected_skills()` persists the set.

**Pre-transcription.** `_pre_transcribe_attachments()` spawns the whisper CLI per file (`skills/whisper/out_of_process.py`) because `faster_whisper` strands memory in the daemon (ISSUE-273). Never raises. `executor._PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS` is shared across one send's audio (`task_timeout_minutes` does not cover it). A timed-out child is killed by process group and its output still parsed.

**Automatic image OCR.** `prepare_image_attachments()` OCRs each decodable image via `skills/transcribe/out_of_process.py` on a long-edge-bounded rendition (Tesseract degrades below ~150 dpi), rendered under `## Image attachment OCR (untrusted text)` with one outcome block per image. One 60s deadline, 48,000-char total, `min(12_000, 48_000 // len(images))` per image. **Automatic OCR is not a transcription request**: `transcribe/skill.md` saves notes only when asked. `transcribe` stays eager on image `file_types` (incl. `heif`) and pulls `untrusted_input`.

**Selection observability.** One INFO line per task, `pass1_selection count=N: foo(always_include), bar(source_type=briefing)`, plus `skills: eager=N menu=M`. Reconcile against proxy rejections (executor.md).

### The untrusted fence: `istota/untrusted.py`

Reasoning in `.claude/rules/leaf-modules.md`. Users: `skills/nextcloud` (`NEXTCLOUD CONTENT`), `skills/rooms` (`ROOM NAME`), `skills/email` (`EMAIL CONTENT`); Room labels differ on purpose: a registry name typed in web chat is not Nextcloud content, and `tests/test_storage_identity.py` forbids "Nextcloud" in bodies other than `files` and `nextcloud`.

**Residual:** `email` leaves `subject` and `from` unfenced on `list`, `search` and `newsletters` (`signup-inbox` fences them). Predates ISSUE-512; fencing changes every listing's shape, a surface decision.

### Skill metadata and discovery

Frontmatter keys mirror `SkillMeta` (`triggers` is docs only; `env` is a JSON array). Discovery merges bundled `src/istota/skills/*/skill.md`, operator `config/skills/*/` (skill.md or skill.toml), then legacy `_index.toml`.

### Per-skill user overlays

An **additive** source: `{mount}/Users/{uid}/{bot_dir}/config/skills/<skill-name>.md`, appended to the winning body on every load. A forked whole skill doc filed here gives two contradictory bodies, which the size cap catches.

- **Injected inside `load_skills`**, so the eager path and `cmd_show` cannot drift. Both call `storage.open_user_skill_overlays(config, user_id)`, which decides `has_workspace` and containment and returns the directory **with an open descriptor** (ISSUE-344): the tree is model-writable, so re-resolving per read leaves a symlink-swap window into the prompt. `load_skills` reads through `user_overlay_dir_fd`; the opener closes it.
- **Shape**: body, `#### {user_id}'s configuration for this skill`, `OVERLAY_PREAMBLE` scoped to this skill, overlay. The preamble never claims precedence over what is above, since `sensitive_actions` sorts early in the eager set.
- **Gates** in `_load_user_overlay`: `OVERLAY_DENYLIST` (`sensitive_actions`, `untrusted_input`, `-`/`_`-normalized), read refusals, UTF-8, emptiness, `OVERLAY_MAX_BYTES` 32 KB. Index membership and disabled checks are upstream in `select_skills`; `inspect_overlay` re-derives them for reporting. `OVERLAY_WARN_BYTES` 24 KB is report-only (raised from 8 KB when ISSUE-337 moved the developer workflow into `developer.md`) and logs at `debug` on load, since it persists and `skills show` would print it into tool output; `doctor`'s `config.skill_overlays` reports it once. Level-1/2 headings (ATX, setext) are demoted to `#### `.
- **Reads** via `read_overlay_bytes`: `O_NOFOLLOW | O_NONBLOCK`, `S_ISREG` and `fstat` size first, since every entry is model-plantable and a FIFO would block prompt assembly.
- `compute_skills_fingerprint` ignores the user tree so an overlay edit does not fire the changelog notice (`TestSkillOverlays::test_fingerprint_is_unchanged_across_an_overlay_write`).
- **Companion overlays differ by path, deliberately**: eager companions get theirs (`test_a_companion_in_the_eager_set_does_get_its_overlay`); `_render_companion_body` applies none (`TestShowOverlays::test_a_companion_body_carries_no_overlay`, control `test_the_same_overlay_applies_when_that_skill_is_the_primary`). Harmless for safety companions, which are denylisted. The overlay precedes companion bodies so it never follows a safety companion (`test_the_overlay_never_follows_a_safety_companion`).
- **No CLI write path** (ISSUE-343; see memory.md). Read via `skills overlay <name>`, inventoried by `skills overlays`, both through `open_user_skill_overlays` and `open_overlay_dir`. A symlinked directory is refused everywhere (reversing ISSUE-343's relaxation); `doctor` reports `dir_not_openable`. Indexed as `source_type="skill_overlay"` by `memory/search.py::reindex_skill_overlays` from `reindex_all` and `scheduler.check_skill_overlay_reindex` (`skill_overlay_reindex_interval`, 6h), not the sleep cycle, which is gated on `sleep_cycle.enabled` and the brain breaker. User docs: `docs/configuration/per-user.md`.

## Skill index

- `always_include`: the seven above. `source_types`: `email` (email, signup); `calendar`, `markets`, `briefing` (briefing). `admin_only` + `cli`: `tasks`, `code_review`. Everything else is a menu skill; keywords are documentation only.
- `untrusted_input`: doc-only companion on the ingest skills and `developer`/`code_review`, never selected directly. `sensitive_actions` holds the outbound rules, it the inbound ones.
- `money` is the sole accounting skill. Module-shaped skills (`feeds`, `money`, `bookmarks`, `location`) and the convention skills (`notes`, `spec`, `todos`) dropped `resource_types`; they live in the menu, and the proxy plus in-process loaders decide whether they can act.

**`developer` declares `companion_skills: [commit, code_review, untrusted_input]`.** Three documents with one subject each: `developer` (layout, job lifecycle, deferring the workflow half to `USER.md` and `CHANNEL.md` but not the mechanics, change tiers, verification budget, abort path, report shape), `commit`, `code_review`. `untrusted_input` is declared directly as well because `expand_companions` is one level; `code_review/skill.md` also states the rule itself.

The three rendered bodies are held under a 776-line budget, enforced by `TestLoadBudget`. Raise it here first, then in the test; `TestLoadBudget.test_the_rules_file_states_the_same_number` enforces the order. Each raise paid for a failure the model could not read from its own error: ISSUE-264 (exit status through a pipe; ISSUE-264/-267/-268/-269/-270 in one week), host-robustness Track D (worker caps, detach past the 600s tool call), ISSUE-291 (`core.hooksPath`), ISSUE-288 (worktree retention, `update-ref` not `branch -d`, ISSUE-125), ISSUE-304 and ISSUE-318 (only npm/PyPI/crates.io reachable; silent npm postinstall failure, `EAI_AGAIN`), and the devbox section. ISSUE-337 held 776 by removing repeated mandates.

## Skill CLI modules (`src/istota/skills/`)

### `devbox/` - persistent dev container
Env `ISTOTA_USER_ID`, `ISTOTA_DEVBOX_CONTAINER`, `ISTOTA_DEVBOX_DOCKER_CLI`, `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES`. **No socket path is ever exported** (ISSUE-284): the env is the model's, so a path there is one it can replace. The CLI reads the socket from config host-side. No default timeout; `--timeout` is the kill.

- Menu skill, `requires_capability: [devbox]`. Every verb uses the exec transport on `{[developer.container] exec_socket_dir}/{user_id}/exec.sock`; `status` adds `docker inspect` and `reset` is pure Docker, so `_run_docker` and `_check_owned` survive for those. `_transport_settings` refuses by name when `container_backend(config)` is not `devbox` (now reached only when `developer.enabled` or `repos_dir` is off), rather than misreporting a down container.
- **Containment is the server's** `realpath` under its root list; the daemon-side mount-table guesses and `/workspace` are deleted (ISSUE-306, ISSUE-312). Host paths for `cp-in`/`cp-out` still go through `skill_host_paths.py`.
- `exec` runs `bash -o pipefail -c` (ISSUE-307; 141 carries a `note`, and a reporting non-final stage colours the pipeline, named in `skill.md`) with `cwd: null`; a named `cwd` must be under the repos root. `exec-file` checks its staging write before running. `cp-out` checks byte count and the terminal frame before touching host disk. `status` halves are independent.
- Ceilings: the transport has none; this process drops past `MAX_BUFFERED_OUTPUT_BYTES` (64 MiB, since the proxy is in the daemon) as an error envelope, never a status; each stream caps at `max_output_bytes` (100 KB) with a marker; `security.skill_proxy_timeout` sits above, so pass `--timeout` below it. `args.command` capped at 32 KB, no NUL.
- `devbox-net` has `DOCKER-USER` drops for link-local, `168.63.129.16/32`, RFC1918, CGNAT. `CAP_NET_RAW` is removed (ISSUE-299: it bypasses `-s`-scoped drops), so `traceroute`/`mtr`/`tcpdump` fail.

**The Docker-API allowlist proxy is retired** (`docker_proxy.py`, `[devbox] api_proxy_*`): it could not return an exec status. No task reaches `cp`/`restart`/`inspect` now, but inside the devbox nothing narrows (`dev` has sudo), hence the `developer` gate on the exec socket.

**Credential proxy** (`devbox_proxy.py`): per-user host socket `/var/run/{namespace}/<user>/sock`; the directory (not the inode) is bind-mounted at `/run/istota-cred/` so restarts survive. `docker/docker-compose.yml` ships no devbox (ISSUE-282), held by `tests/test_devbox_deployment_shape.py`. Actions `ping`, `git_credential` (server-side injection), `forge_token` (to the `gh`/`glab` wrapper, a copy of `forge_cli.py`). It replaced the `gitlab_api`/`github_api` endpoint allowlists, which could not describe real `gh` calls; the deny list is the wrapper's argv policy. Audit logger `istota.devbox_proxy.audit`, values escaped by `_audit_value` so a newline cannot forge a line; a cross-host credential get logs `result=no_token`.

### `kv/` - key-value store
- `always_include`; writes deferred under sandbox. Deferred set ops carry only members or the count, and the scheduler re-reads at apply time so they compose.
- **No store limit, but an argv element is capped** (ISSUE-239, `MAX_ARG_STRLEN`, 128 KiB): a value grown by `set-add` reads fine but cannot be rewritten by `kv set`. Use `--value-file` or `set-trim` (count, not age).
- **`--value-file` is scoped** because the CLI is host-side and `kv get` returns the bytes. Roots mirror the user's binds: deferred dir, `Users/{uid}`, the task's `Channels/{token}`, `Talk` read-only; never the workspace root. Deliberately wider than `scheduler_deferred._source_path_allowed` (deferred dir and user workspace), whose content outlives the task; unifying them would widen a boundary. Use the **resolved** path; check destinations before `mkdir`. Rule in `skill_host_paths.py`.
- `set-contains` with two or more members returns a map, with one the scalar; every response has `"batched"`. `list` truncates values at 2048 chars (`truncated`, `value_chars`, `truncated_count`); `get` and `set-members` never do. Operator `istota kv` shows whole values and has an unscoped `--value-file`.

### `tasks/` - task state read surface
`status <id>`, `recent [--since] [--parent] [--status] [--source-type] [--limit]`, `transcript <id> [--attempt N] [--turns|--turn N|--tools|--grep TEXT] [--thinking]`. Env `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_TASK_ID`, `ISTOTA_TASK_ATTEMPT` (in `executor._EXECUTOR_PROXY_ONLY_VARS`), `ISTOTA_SESSION_LOG_DIR` (`proxy_only`, from this skill's `setup_env` via `session_log.resolve_session_log_dir`).

- `admin_only` + `cli` (ISSUE-237). `db.get_task_state_for_user` / `db.list_recent_tasks_for_user` take `user_id` as a mandatory ownership predicate, and "missing" and "not yours" both answer `not_found`. Results are capped with an untrusted `notice` (hence `untrusted_input`); `--since` is parsed and bounded; rows carry `conversation_token`.
- **`transcript`** reads native session logs via `session/session_log_read.py`, scoped by `find_logs(root, ISTOTA_USER_ID)` (the header check is defence in depth). The **current attempt is excluded**, from `ISTOTA_TASK_ATTEMPT` in the process env, never the row, which reapers bump (ISSUE-377); a missing attempt excludes the whole task. Residual: an attempt below the floor may still be live if a reaper was wrong; closing it needs a write-recency window. `--grep` is literal (`re.escape`), never a regex. Tool results are fenced `[UNTRUSTED TRANSCRIPT CONTENT …]`; unavailable logs answer `{"available": false, "reason"}` at exit 0.

**`admin_only` does not gate execution**, only selection, companions, the menu and `skills show`. Paths around it: the proxy's `allowed_skills` (every `cli: true` skill in the reach index, `task_env.build_task_runtime`); and a CRON.md `command: istota-skill …` row run by `scheduler._execute_skill_task`. A CLI needing a boundary carries its own; `tasks` scopes by `ISTOTA_USER_ID`.

### `code_review/` - branch-diff review
`run --worktree PATH [--base REF] [--range A..B] [--intent TEXT] [--agents both|conformance|bughunt] [--timeout N]`. Env `DEVELOPER_REPOS_DIR` (caller's own subtree, `from: setup_env` via the developer hook), `ISTOTA_BRAIN_NATIVE_API_KEY`, plus DB/user/task ids for the call counter.

- `admin_only` + `cli`, host-side, modelled on `memory/sleep_cycle.py:_run_sleep_cycle_brain` (text-only, `allowed_tools=[]`, timeout, breaker). Conformance and bughunt reviewers, both above `both_agents_threshold_lines` (150) or on `boundary_patterns`. Findings merged on `(file, line)`, `low`/preference dropped. `engine.py` is model-free; `run_review` calls the brain.
- **The key is `sensitive`, never `proxy_only`**: the proxy-only split runs first and lands in `proxy_base_env`, which every skill CLI gets; `sensitive` alone is scoped to `code_review` by `derive_skill_credential_map`. On `claude_code` it uses `CLAUDE_CODE_OAUTH_TOKEN` (see `SKILL_MODEL_CALLERS` in sandbox.md).
- **Git hardening**, since the worktree is model-writable (`tests/test_code_review_engine.py`, `engine.py` docstring): config-driven execution (`-c` overrides, `--no-ext-diff --no-textconv`); upward search (discovery ceiling); relocation via `.git` file or `commondir` (`git_dir` checks both dirs); option injection (`--end-of-options`, and for `git grep` a full hex id via `_require_object_id`).
- **Content comes from the object store** (`git show <rev>:<path>`), never a worktree path. Validation is not atomic with use. `_git` caps stdout (`MAX_GIT_OUTPUT_BYTES`), closes stdin, has a deadline.
- **`status`**: `ok` acts; `error` (bad range, path outside roots) blocks the push; `skipped` never blocks (`brain_unavailable`, `brain_unsupported`, `call_cap`, `repos_root_unavailable`, `review_disabled`, `review_failed`, `malformed_output`, the last moved by ISSUE-266). Without the proxy there is no envelope (`skill_client._run_direct` exits 1); `code_review/skill.md` reads that as review unavailable. Cleanliness fields: `empty`, `partial`, `agents_failed` (a list; blocking on it was declined as policy, as in ISSUE-292), `dropped_findings`, `need_files_note`, `round_trip_refused`, `agent_timeout_clamped`.
- **Timeouts** (ISSUE-448, ISSUE-450): `skill_proxy.resolve_skill_timeout` resolves `security.skill_proxy_timeouts`, then `DEFAULT_SKILL_TIMEOUTS` (540 for `code_review`), then the global, clamped to `skill_client_wait_seconds` minus 30. The 540 is in code because a dict config field replaces its default; both config defaults are `{}`. `describe_skill_timeouts` runs once at proxy construction. The proxy reads config, never the client's `ISTOTA_SKILL_CLIENT_WAIT`. The agent budget is clamped under the ceiling minus `RESERVED_SECONDS` (`ASSEMBLY_ALLOWANCE_SECONDS` 20 plus `JOIN_SLACK_SECONDS`), floored at `MIN_AGENT_TIMEOUT_SECONDS`, and only lowers; `ReviewConfig.timeout_seconds` defaults to 480. `agent_timeout_*` fields report it (the warning is only in the daemon log); `--timeout` overrides one run (`agent_timeout_override`); `overhead_seconds` measures non-model time.
- Reviewer calls stream, so a timeout still records usage; `partial_text` is logged, not returned.
- **Call cap** `code_review_calls`, `max_calls_per_task` (8), counts invocations that returned, not successes, so prose-answering reviewers cannot run unbounded. At most two rounds; `<= 0` permits nothing; advisory under concurrency.
- **`need_files` is one round trip**, bounded by `MAX_NEED_FILE_REQUESTS`, `max_need_files`, `MAX_NEED_FILE_BYTES` (size and type checked first); a drop in findings goes in `need_files_note`. Admitted on **measured cost** (ISSUE-292): the slowest invocation times `ROUND_TRIP_COST_MULTIPLIER`, with `MIN_RETRY_SECONDS` only a lower bound. `round_trip_refused` is set on every asked-but-not-run path, including `serve is None`.
- Weaker than they read: `ON DELETE CASCADE` is decorative (`foreign_keys` off); `allowed_tools=[]` binds only on native.

### `email/` - IMAP/SMTP
Read: `list`, `read`, `search`, `thread`, `attachments --dest`, `from-senders`, `newsletters`, `signup-inbox`, all with `--scope {mine,shared,all}`. Write: `send` (Bcc never transmitted), `reply`/`reply-all`, `mark`/`delete` (`--confirmed`), `output` (deferred).

- **Approval gate**: `send`/`reply`/`reply-all` check every recipient via `outbound_policy.recipients_require_hold`. A hold writes `outbound_drafts` and returns `"status": "held"` at **exit 0**, since non-zero invites a retry; a gate that cannot run errors with exit 1, never sends. No `--confirmed`. Not placed in `send_email` (briefings and `outbound_drafts.release` use it), the proxy or the scheduler.
- **`output` is not exempt** (ISSUE-246): `_hold_if_unapproved` runs in `transport/email/outbound.deliver_email_result` before all three send sites; a hold returns `True`, an unrunnable check `False` and sends nothing; `_announce_hold` alerts. A plain `yes` or a thread match writes no trust row (ISSUE-234), so replies are held unless the recipient is authorized. Replies snapshot threading headers so `release` sends from the row.
- **`--attach` is `EGRESS`** (ISSUE-447): root `{mount}/Users/{uid}` only, resolved at parse by `skills/_hostpath.resolve_parsed`, replacing the split `_scoped_attachments` / `_holdable_attachments`. Talk, channel and deferred files must be copied into the workspace first.
- `effective_policy` resolves before opening the DB. `!drafts` answers drafts; `nag_stale_outbound_drafts` alerts at 24h, stamping `nagged_at` after delivery; `release` raises `DraftSentButUnrecorded` when bookkeeping fails after SMTP.
- **Read scoping**: `istota.email_ownership` is shared with the inbound poll; `shared`/`all` fail closed without the DB; `--scope mine` pushes all arms server-side, the thread arm from `_MINE_THREAD_MAX_IDS` sends (ISSUE-252), and the client filter stays authoritative.
- `delete_emails_before` is library-only retention (scheduler.md). `html_body` (`_set_body`) is used only by briefing email.

### `browse/` - headless browser
`get`, `render`, `screenshot`, `extract`, `interact`, `links`, `close`; env `BROWSER_API_URL`; `requires_capability: [browser]`.

- `render` first for structured pages (keeps link URLs, ISSUE-192).
- **Visual mode**: the model names points only in the delivered picture's pixels. The container measures the PNG and records the frame (`X-Browse-Capture`); one `delivered_size` (rounding down) shrinks captures under `image_attachments.MAX_EDGE` / `MAX_AREA_PIXELS`. Input via xdotool, since CDP clicks fail Cloudflare. Stale captures are refused (`stale_capture`, `viewport_changed`, `full_page_capture`, `no_capture`, `out_of_picture`).
- **Scroll** (ISSUE-528): `--scroll-at X,Y` is a distinct `scroll_at` wheel action at a point (an old container answers `unknown`); bare `--scroll` sends Page keys via `key_native` (XTest) and refuses `window_not_focused`; no JS evaluation. `--scroll-amount` is retired but still declared, and the container refuses it (`retired_argument`). `--scroll-zoom` holds an allowlisted modifier with the keydown inside `modifier_held`'s `try`, and requires an explicit direction.
- `--fill-credential` checks frame origin against the vault binding, requires HTTPS, fills in one CDP evaluation, needs capability `credential_origin_check`, and is selector-only.
- Untrusted pixels cannot be fenced; `untrusted.IMAGE_NOTICE` is a notice, not a control, and the 8-round bound is advisory.

### `transcribe/` - OCR
`cmd_ocr` runs Tesseract once (`image_to_data`). `ocr_image_out_of_process` spawns `python -P -m istota.ocr_leaf ocr` (process group, no stdin, `--` before the path, argv from `child_argv`); the leaf imports nothing from `istota` because the skills package star-imports every skill (`TestTheChildImportSurface`). Never raises. Concurrency `image_attachments.OCR_MAX_CONCURRENCY` (4), per call. The child is for memory isolation and a real timeout; `health/ocr._ocr_image` remains an in-process second implementation.

### `whisper/`
`transcribe`, `models`, `download`; extra `whisper`. Daemon callers use `transcribe_audio_out_of_process()`, never `transcribe_audio` (ISSUE-273). It must pass the task identity (ISSUE-447), or `env_host_roots()` is empty and every path refused. Same for any daemon-side skill CLI spawn; a `--trusted-caller` flag was rejected (the model could pass it).

### `nextcloud/` - Nextcloud control plane
Groups over `src/istota/nextcloud/`: `capabilities` (`--check`), `user`/`group`, `share` (incl. `share link`, ISSUE-193), `files` (WebDAV metadata, SEARCH, versions, trash, quota, chunked upload; no read/write/rm/mv), `talk` (control, not delivery), `notify`/`activity`. `requires_capability: [nextcloud]`. Outbound actions confirmation-gated, destructive verbs need `--confirmed`, non-admin paths scoped to `/Users/<caller>/`. Env `NC_URL`, `NC_USER`, `NC_PASS`, `NC_DAV_PREFIX` (must be in the manifest because the CLI has no daemon Config; it is applied and inverted in the request layer so paths stay logical). `tests/test_nextcloud_skill_live.py` is the `integration` tier; `TestLiveCoverage` in the default suite fails when a verb lacks a live test. Server facts: SEARCH href `/files/<user>`; streams `/activity/<filter>`; `talk send` unwraps OCS; Talk search `from` excludes that room (`--token` filters client-side); link expiry is UTC.

### `location/`
The query pipeline is `istota.location_logic`, shared with the web routes; `tests/test_location_surface_parity.py` requires equal payloads. Surfaces differ only in envelope, limit default and dated `history` sort order. `assign_pings_to_place` (ISSUE-491) reassigns pings on place create/move; **deliberate divergence**: web always, skill only with `--backfill`, since a model may choose a radius that swallows a neighbour. `import-garmin-tracks` runs inline with `ISTOTA_SECRET_KEY`, else defers to `scheduler_deferred._process_deferred_garmin_import`.

### Other CLIs
- `calendar/`, `markets/`, `memory_search/`: plain CLIs.
- `bookmarks/`: Karakeep; `_paginate` sends `includeContent=False` only for bookmarks.
- `feeds/`: in-process facade over `istota.feeds.cli` via `CliRunner`. Per-user `{workspace}/feeds/data/feeds.db` is the sole source of truth; legacy `feeds.toml` imported once by `migrate_legacy_toml`. The scheduler seeds `_module.feeds.run_scheduled` (`*/5 * * * *`) and `_module.money.run_scheduled` (`0 8 * * *`) for enabled users.
- `google_workspace/`: `gws` passthrough with `GOOGLE_WORKSPACE_CLI_TOKEN` from `setup_env`. `[google_workspace] scopes` is a ceiling; users pick `{service: off|readonly|full}` on `user_profiles.google_scopes` (empty means the ceiling; non-empty is authoritative, so widening the ceiling never widens a request), resolved by `istota.google_scopes`. Unmapped ceiling scopes are appended and reported as `unoffered_scopes`.
- Library-only: `files/`, `markets/finviz.py`.

### `money/` - accounting (in-process)
Ledger, `invoice`, `work` and `portfolio` verb groups. Env `MONEY_USER`. Tax config is operator-only (`istota money tax`, see money.md).

- **Facade**: Click via `CliRunner`; config only in `config_store`. `istota money <op>` routes via `cli_money.dispatch_operational`. `edit-transaction` rewrites by `id:` under a flock with `bean-check` and rollback; `edited:` entries are skipped by Monarch sync.
- **Invoice auto-matching** (`core/invoice_matching.py`): a credit settles an invoice only when exactly one open invoice matches to the cent (or `--tolerance`) and was not issued after it; partly paid, partly unrecognised and paid invoices are excluded. `work.invoice_issue_date` is the date rule (stored `invoice_date`, else `max(entry.date)` permanently, ISSUE-256). Marks paid via `record_invoice_payment` (no double post). Ambiguity goes to `invoice_matching.review` (`_demote_contested`), as does a zero-row write. Runs once across profiles, in integer cents, best-effort in one `try`. `--no-match-invoices`; `invoice unpaid` undoes.
- **Work entries**: stable `uid` alongside the display `id`. Programmatic callers use `update_work_entry_by_uid` / `remove_work_entry_by_uid` (resolve inside `_work_lock`, return `WorkMutationResult`) with `entry_etag` as `expect_etag`. `generate_invoices_for_period` renders before stamping, so it uses `assign_invoice_number_by_uids`, backfills first and logs `invoice_stamp_incomplete`. Year files keep unknown keys (`WorkEntry.extra`), not comments; `_save_year` parses its own output before replacing. Unreadable rows quarantine the year (`_QUARANTINED_YEARS`), and content-changing writes to it raise `WorkFileQuarantined`.
- **Invoicing config** (`/config/*` routes and CLI). Behaviour-changing invariants live in `config_store` and raise `ValueError` on every surface (enum `type`/`schedule`, finite rates, `terms >= 0`, Unicode-aware `_is_account`, `logo` inside the accounting folder, slug keys, lowercase client keys since entries store `client.lower()`). Only changed fields are validated (`unchanged_fields`) so legacy rows stay editable. Shape checks are route-side (`_coerce_*_fields`, in step with `_reject_unknown`). `save_invoicing` sanitizes with `money_config_sanitized` rather than raising. `POST` creates (409 via `KeyExistsError` in-transaction); `PUT ?create=false` 404s. Keys are immutable.
- **Delete guards** (`money/config_refs.py`, shared by CLI and web): a service used by any work entry is refused; an entity is refused while a client or a work entry names it, or it is the stored or effective default (`cfg.company.key`); a client delete is allowed and reports its entry count. A quarantined year makes strict deletes refuse.
- **Portfolio** (`money/portfolio.py`): Fidelity Positions or fina history snapshots via `kind="positions"` importers, content-hash dedup. Classification resolves at read time. Auto-classification (`portfolio_autoclass.py`): yfinance, then heuristics gated on fund markers or bond shape; commodities only from the category, since fund names carry metal words. Auto rows are `source='auto'` via `INSERT OR IGNORE` (`insert_classification_if_absent`), so they never replace a row. Bounded by `MAX_LOOKUPS_PER_RUN`, `LOOKUP_BUDGET_SECONDS`, `LOOKUP_TIMEOUT_SECONDS`; `[money] autoclass_lookup` gates egress. Never writes the ledgers.

### `health/`
Body stats, bloodwork, history, immunizations, Garmin, documents. Env `HEALTH_DB_PATH` from `setup_env`. Sandboxed writes go to `task_<id>_health_ops.json` (`_process_deferred_health_ops`). `link-encounter` / `unlink-encounter` maintain `diagnosis_encounters`; `@ref` names an encounter from the same batch; link rather than duplicate a condition. `attach-document` resolves the real `HealthContext` via `load_config` (uploads are on the mount, the DB is local); deletion is web-only. `garmin-sync` runs inline when `secrets_store.secret_key_available()`, else enqueues a `skill="health"` task (`max_attempts=1`, polled up to 60s) run by `_run_garmin_sync_inprocess`.

### Module-skill facade exit-code contract

Feeds and money facades emit `{"status":"error","error":"…"}` via `_output()` (= `skills/_cli.py`'s `emit`), which exits 1 on an error envelope; `_execute_command_task()` also detects the envelope (scheduler.md). New facades dispatch through `run_skill_cli`; `tests/test_skill_cli_facade.py` pins it, with `EPILOGUE_EXEMPT` naming the six exceptions.

## How to add a new skill

1. `src/istota/skills/<name>/skill.md`: frontmatter plus body.
2. Optional CLI: `__init__.py` (`build_parser`, `main` printing JSON) and `__main__.py`, dispatched through `run_skill_cli`.
3. **Env vars only in the manifest** `env:` block; `build_skill_env()` resolves each `EnvSpec`.

e.g. `{"var":"MY_API_KEY","from":"secret","service":"my_service","key":"api_key","sensitive":true}`.

Sources: `config`, `secret`, `setup_env`, `template_file`, `user_id` (the resource sources went with the Resources sunset). `sensitive: true`: stripped from the model, injected only into declaring skills, fetchable via `istota-credential env <VAR>`, an auto-authorization signal. `proxy_only: true`: non-secret values the model must not hold (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`; `ISTOTA_DB_PATH` via `_EXECUTOR_PROXY_ONLY_VARS`), handed to every skill CLI. Never both: the proxy-only split runs first.

**Credential references**: `skills/_credref.credential_ref(parser, …)` stamps an argument that `skills/_cli.parse_and_resolve` resolves before dispatch from the user's shared namespace, over the per-invocation `ISTOTA_CRED_FD` socketpair (same fetch budget, live binding; marked close-on-exec before skills load). Without an fd the model-facing socket is used, subject to the reveal policy in sandbox.md; an invalid fd refuses. Handlers get a `SecretValue` (plaintext only via `reveal()`); unknown names refuse with `vault_credential_refused`. `browse interact --fill-credential` is the consumer; prefer it over `--fill`. Argparse only.

## WhatsApp and relay task skills

`whatsapp` (capability-gated, companions `sensitive_actions`, `untrusted_input`) queues self-sends only, with no address or approval argument; `status` refuses a relay row. `relay` (`ask`, `status`, `list`) has no capability gate; the daemon picks the destination, decides approval and sends. `whatsapp ask`/`relays` are gone. `agent/events._private_relay_tool` matches `relay ask` and `whatsapp ask`. Rules in `.claude/rules/relay.md`.
