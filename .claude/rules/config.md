---
paths:
  - "src/istota/config.py"
---

# Config module (`src/istota/config.py`)

This covers fields whose behaviour is not obvious from name and default: defaults that matter, security settings, retired keys, cross-field validation. The exhaustive field list is the dataclasses in `src/istota/config.py` and `config.example.toml`. `SchedulerConfig` is in `.claude/rules/scheduler.md`.

## Loading

Search order: `config/config.toml` → `~/src/config/config.toml` → `~/.config/istota/config.toml` → `/etc/istota/config.toml`.

1. Parse TOML; run `normalize_legacy_document` (WhatsApp flat-key migration) before the walk.
2. `config_mapper.apply_section` walks the dataclass tree: TOML key = field name, default = dataclass default (the only default), coercion by declared type. Unknown keys get one warning (`report_unknown`), never fatal, so a newer config loads on a rollback.
3. `_CONFIG_HOOKS` maps a dotted key to a parse beyond coercion (`web.auth` and other closed vocabularies, `security.sandbox_ro_paths`, `scheduler.email_task_queue`, `developer.container`, `health.max_document_bytes`). `_PARSED_BY_HAND` is what the walk skips; `_RETIRED` names dead keys so each gets its own migration warning. `tests/test_config_mapper.py` holds all three to real fields.
4. Hand-parsed: `[users.*]` (`_parse_user_data`), `[[default_briefings]]`, `[[briefing_shared_blocks]]`, `[models]`, `[experimental]` (against `KNOWN_FEATURES`), the `[email]` cross-field rules (after the walk, since `verify` needs `authserv_id`).
5. `load_admin_users()`: `ISTOTA_ADMINS_FILE` or `/etc/istota/admins`, one id per line, `#` comments. Missing file → empty set → everyone is admin.
6. Secret env overrides, `ISTOTA_<SECTION>_<FIELD>` (same names as compose): `ISTOTA_NEXTCLOUD_APP_PASSWORD`, `ISTOTA_EMAIL_IMAP_PASSWORD`, `ISTOTA_EMAIL_SMTP_PASSWORD`, `ISTOTA_DEVELOPER_GITLAB_TOKEN`, `ISTOTA_DEVELOPER_GITHUB_TOKEN`, `ISTOTA_GOOGLE_WORKSPACE_CLIENT_SECRET`, `ISTOTA_WEB_OAUTH2_CLIENT_SECRET`, `ISTOTA_WEB_SESSION_SECRET_KEY`, `ISTOTA_WEB_TOKEN_STORAGE` (value-validated), the three `ISTOTA_WHATSAPP_*` credentials (→ `whatsapp.cloud`). `ISTOTA_SECRET_KEY`, `ISTOTA_WEB_TOKEN_KEY` and runtime vars (`ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, ...) are not config overrides.
7. `_apply_user_profiles`: `user_profiles` overlays `config.users`. Scalars replace TOML when a row exists; lists (email_addresses, disabled_skills, trusted_email_senders) only when non-empty, so an auto-seeded blank row does not wipe templated lists. Best-effort.
8. `_apply_user_resources`: rows → `ResourceConfig`, dedup on `(type, path)`, DB wins.
9. `_migrate_obsolete_resources`: `secrets_store.import_from_user_configs` (absorbs karakeep `base_url`/`api_key` from `extra`, overland `ingest_token`, monarch creds), then `db.cleanup_obsolete_resources` deletes retired types (`feeds`, `money`, `monarch`, `moneyman`, `karakeep`, `overland`, `calendar`, `email_folder`, `notes_folder`). `todo_file`/`reminders_file` have no reader but are not auto-cleaned: deleting a user's rows is a data migration.
10. `_apply_user_briefings`: dedup on `name`, DB wins; `enabled=0` drops the TOML name (web mute). TOML `blocks` are captured and re-attached to the DB entry, since rows never carry `blocks`.

**A bad value never stops a process.** `load_config` runs in the scheduler, web app, webhook receiver and every proxied skill CLI. No raw value reaches bare `int()`/`float()` (TOML spells inf/nan). `coerce_int`/`coerce_float` turn a non-finite number, a bool in a numeric slot or a non-number into one WARNING and the default; a quoted number is read as the number.

## Adding a field

- Existing section: add it with a default; no loader change. Extra validation is a `_CONFIG_HOOKS` entry. An annotation the resolver cannot coerce fails `tests/test_config_mapper.py::test_every_declared_field_resolves_to_a_coercion` (once shipped silently for `dict`). The old hand loader silently ignored eleven settings, including `security.sandbox_ro_paths` and `scheduler.max_subtasks_per_task`.
- New section: dataclass + `Config` field; the walk recurses. Only lists of dataclasses, verbatim maps or cross-field rules need a hook or `_PARSED_BY_HAND`.
- Per-user: `UserConfig` field, `_parse_user_data` if non-trivial; if profile-shaped, plumb through `user_profiles` (row, web, `istota user ensure`). TOML profile keys are seed-only.
- Always: `config.example.toml`, Ansible `defaults/main.yml` + `templates/config.toml.j2`, Docker `render-config.sh` **and** `docker-compose.yml` (`test_render_config.py::TestTheEntrypointStillOwnsWhatItKept::test_every_var_the_render_reads_is_passed_by_compose`).

## `Config`

- `model`/`effort` at the root are deprecated (ISSUE-418): claude_code's defaults applied to every brain. Migrated with a warning onto `[brain.claude_code]` and `[brain.tmux]`, never `[brain.native]`. `advisor_model` uses the alias table (anthropic brains, no effort), dropped for a task with a model pin (`executor._resolve_advisor`).
- `module_data_dir` must be local (WAL `-shm` SIGBUSes on FUSE); explicit values under `nextcloud_mount_path` raise. `module_db_root()` = it or `{db_path.parent}/modules`, split out because the sandbox masks the root and `_validate_workspace_dir` refuses overlap (three derivations is how it once went unmasked). `module_db_path(user, module)` is the one enumerator for `db_health`, `db_backup`, `db_relocate`.
- `storage_is_nextcloud` = `bool(nextcloud.url)`, not `is_standalone` (which folds in web auth). Source of storage vocabulary; `storage_backend` and `storage_label` derive from it.
- `workspace_root(user_id=None)`: de-dups `workspace/"Users"/uid`, scoped via `user_scope.scoped_user_dir`; no I/O.
- `is_standalone`: blank `nextcloud.url` and `web.auth == ["none"]`. `local_user_id`: the sole user.
- `available_capabilities()`: `browser`, `devbox` flags. A skill whose `requires_capability` is absent joins `skills._loader.effective_disabled_skills`.
- `is_module_enabled(user, module)`: names from `modules.MODULE_NAMES` (`feeds`, `money`, `location`, `health`, `briefings`; unknown → False). Before the DB read: the `EXPERIMENTAL_MODULES` gate (empty) and the dependency gate (`MODULE_DEPENDENCIES`, `money → beancount`, `module_available` via `find_spec`), so a lean install hides a module everywhere. Then `user_profiles.disabled_modules` (cross-process without SIGHUP), else memory. Unknown user → True. `/settings/modules` and `_coerce_profile_value("disabled_modules", ...)` use the same gate.
- `is_trusted_email_sender(..., conn=None, *, include_own_addresses=True)`: own addresses, `trusted_email_senders` fnmatch, DB table with `conn`. `include_own_addresses=False` is for the sender-match gate (ISSUE-227), whose route is that match.

## Nextcloud

`dav_prefix`: the storage root inside the bot's DAV tree. Blank on bare metal; on Docker `/mnt/shared` is a `files_external` mount (compose `x-shared-mount-name`, `ISTOTA_NEXTCLOUD_DAV_PREFIX`). Applied by `nextcloud._http.to_remote_path`, inverted by `dav.href_to_path`. Not on `storage.BOT_USER_BASE` (on-disk paths) and not inside `resolve_scoped_path` (the confinement boundary speaks logical `/Users/{uid}`). Skill CLI gets `NC_DAV_PREFIX`.

`auto_share_bot_dir`: boot-time OCS share in `ensure_user_directories_v2`; false on Docker (compose literal) because `provision-nc.sh` already mounts the directory into the user's tree.

## Email

`confirm_sender_match` (`off`|`verify`|`gate`, ISSUE-249 Gap 3): what the own-address branch is worth. `off` trusts `From:`; `gate` holds every self-claim for a yes/no; `verify` trusts it only when `_authentication_verdict` is `pass` (our `authserv_id`, aligned `header.from`). One expression, `_own_address_claim_counts`. Fails closed, including `None` (thread route). `verify` without `authserv_id` raises: an unscoped verdict comes off the top header, which the sender writes. Legacy booleans and Ansible's `"true"`/`"false"` load; anything else raises. `_sender_match_policy` normalises direct `EmailConfig` builds (a stray `False` becoming `gate` would hold everything). `off` declares upstream DMARC already authenticated, still the better place. Ansible asserts value and the verify rule before templating.

`dmarc_canary` (on) only warns on a non-`pass` DMARC verdict for a self-claim; `dmarc=none` counts. `dmarc_canary_warn_on_missing` (off; a non-stamping path would warn on everything). `authserv_id` (blank) scopes which `Authentication-Results` are read; blank reads the top header, which inverts when the MTA stops stamping. Setting it asserts the MTA stamps, so `unstamped` warns on its own. See transport.md "Email confirmation gate", "The DMARC canary".

`outbound_approval_floor` (`untrusted`; `off`|`untrusted`|`all`): the weakest per-user policy allowed. `untrusted` holds unless every To/Cc/Bcc is trusted; `all` unless every recipient is the user's own; one bad recipient holds the message. Invalid raises (`_validate_outbound_approval_floor`): no safe fallback exists. Ansible `istota_email_outbound_approval_floor` and Docker `ISTOTA_EMAIL_OUTBOUND_APPROVAL_FLOOR` exist because the gate switches on at upgrade; the role asserts before templating since unquoted YAML `off` renders `"False"`.

The trusted allowlist is explicit only, never derived from correspondence (`sent_emails.to_addr`, `processed_emails.sender_email`): that once let one stranger's mail authorize mailing them for good. Guard: `tests/test_outbound_gate.py::TestLayerARegressionGuard`.

## SMS and WhatsApp

`[sms]` enabled requires a public `site.hostname`, E.164 numbers, a default sender from the list, bounded values and one complete active provider; any inactive block with a value must be complete, so an old adapter can keep authenticating callbacks across a switch. See sms.md.

`[whatsapp]` top level: `enabled`, `provider` (`baileys` default | `whatsapp_cloud`), `business_phone_number`. Meta settings are `[whatsapp.cloud]`; `[whatsapp.baileys]` is `session_dir`, `library_version` (self-resolving, not credentials). `graph_api_version` accepts `v25` and `v25.0`. `business_timezone` sets the cap's month boundary.

- `_migrate_whatsapp_flat` moves flat keys into `[whatsapp.cloud]` (nested wins). With no `provider`, a non-empty id or credential in the **resolved** cloud table selects `whatsapp_cloud`: values, not key presence (generators render every key), and resolved so moving keys under `[whatsapp.cloud]` does not flip a Cloud deployment to Baileys. No deprecation warning (it would fire per skill CLI call).
- ISSUE-058 split: `whatsapp_structural_config_errors` fails the load, active adapter only (`_WHATSAPP_PROVIDER_VALIDATORS`). `whatsapp_credential_errors` does not, because Ansible renders credentials empty under `istota_use_environment_file`; it reports only the active provider's fields (the unconditional form made `outbound._gate` record `unconfigured` for every Baileys send). `whatsapp_webhooks_enabled` has no callback-only arm. See whatsapp.md.

## ntfy

No `[ntfy]` block: per-user secrets (`secret_schema.CONNECTED_SERVICE_SCHEMA`); no topic → no-op; priority 3. Headers go through `ntfy_headers.encode_header_value` (RFC 2047): httpx sends ASCII headers and an em dash in a title lost the whole push (ISSUE-213). Markdown is opt-in (`DeliveryOptions.markdown`, `--markdown`), since ntfy renders it only in its web app. One-way.

## Devbox and the container transport

`DevboxConfig`: `container_prefix` (container `f"{prefix}{user_id}"`), `docker_cli` (`reset` and the devbox credential proxy's peer checks), `max_output_bytes`. `status` and `reset` speak Docker host-side with no `DOCKER_HOST`; everything else uses the exec transport.

- Retired, kept out by `tests/test_ansible_config_template.py`: `docker_socket`, `exec_timeout_seconds`, `api_proxy_enabled`, `api_proxy_socket_dir`, `api_proxy_exec_ttl_seconds`, `api_proxy_audit_log`. The Docker-API proxy is deleted (its only consumer bound a socket into every sandbox). No exec timeout: task budget, or `--timeout`.
- No `exec_socket_dir` here: everything uses `config.exec_socket_path`. Held by `tests/test_skills_devbox.py::TestTheSocketPathComesFromConfig::test_the_devbox_block_carries_no_second_spelling`.
- Only the Ansible role runs a devbox; compose ships none.

`ContainerConfig` (`[developer.container]`): `exec_socket_dir` (`/run/istota-exec`, socket `{dir}/{user_id}/exec.sock`), timeouts, `shim_commands` (`DEFAULT_SHIM_COMMANDS`: `npm npx pnpm yarn node uv uvx pip pip3 cargo rustc rustup go bundle gem`). Whether it is used is `container_backend(config)`: `[devbox] enabled` + `developer.enabled` + `repos_dir`. Deploy-time, so the host never consumes a container-built environment. Derived from config, never availability: a stopped devbox must fail (shims exit 120), not reroute builds to the host.

- `backend` is retired: it could contradict `[devbox] enabled` both ways. `_parse_container_block` warns (a `backend = "none"` operator's builds now move into the devbox); doctor `developer.container.backend` re-derives from the three inputs.
- `_UNSHIMMABLE_COMMANDS`/`_UNSHIMMABLE_RE` refuse interpreters (`python`, `python3`, `python3.12`, ...), shells, `env`, `git`, `gh`, `glab`, `istota-skill`: the network bridge, forge recipes and the exec client are Python. `make` is not shimmed by default; shimming a driver routes everything beneath it.

## Developer

`repos_dir` is a root of per-user subtrees: `{repos_dir}/{user_id}/{namespace}/{project}.git`, worktrees as siblings, cache at `.package-caches`. `config.repos_root(config, user_id)` serves every task-scoped consumer (bwrap bind, native write root, `DEVELOPER_REPOS_DIR` via `config_per_user`, `git_remote_scrub`, devbox mount). Isolation is structural, which retired the ISSUE-319 masks. The namespace level avoids basename collisions; depth 3 fits `git_remote_scrub._MAX_DEPTH`. Global-root consumers by design: `worktree_reaper` (no user) and `_protected_cache_parents` (global is stricter). Helpers: `repos_root`, `container_backend`, `devbox_container_backend`, `exec_socket_dir`, `exec_socket_path`.

- `DEVELOPER_REPOS_DIR` comes only from the developer skill's `setup_env` (`developer` and `code_review` manifests are `from: setup_env`), gated on `is_admin` like the bind. Residual: an override manifest at `config/skills/developer/skill.md` still saying `from: config` would hand out the shared root; the guard test reads bundled manifests only.
- `repos_relocate.py` (Ansible, before restart) assigns every namespace to the single admin and refuses on none or several; marker `{repos_dir}/.istota-layout` = `2`; the old shared `.package-caches` is left for manual removal.
- `gitlab_reviewer` → `GITLAB_REVIEWER` for `glab mr create --reviewer` (by username). `gitlab_reviewer_id` is read by nothing (ISSUE-289: ids in the consumed field left every MR unassigned). Doctor `developer.gitlab_reviewer` WARNs on an all-digit username or an id with no username.
- `worktree_reap_enabled`/`worktree_retention_hours` (24, 1h floor) drive `worktree_reaper.py` (ISSUE-288) from the scheduler (`scheduler.worktree_reap_interval`), never `setup_env`, which runs for every task including heartbeat `id=0`. Retention is what protects a running task.
- Retired: `gitlab_api_allowlist`, `github_api_allowlist`, `api_timeout_seconds` (unified-forge-cli-wrapper spec). Endpoint lists cannot describe real `gh` calls; denial is `sandbox/forge_cli.py`'s argv policy.
- `forge_cli_permit` turns a baseline guard off. `_validate_forge_clis` (spawns nothing; config-load path) warns on a permit entry matching nothing, a missing `gh_bin_path`/`glab_bin_path`, and tokens with `skill_proxy_enabled = false` (the token then sits in the model's shell env via `direct_token`).
- One instance per forge: each URL feeds the CONNECT allowlist (`executor._build_network_allowlist`), the per-host credential helper and `GITLAB_HOST`/`GH_HOST`. A second instance fails safe but opaquely; `extra_hosts` adds reachability, not credentials. `git` is unwrapped; force-push protection is forge-side.

## Security

`skill_proxy_enabled` is required wherever `sandbox_enabled` is true (ISSUE-393 warning): `_split_credential_env` strips sensitive vars only in the proxy branch, and DB dirs are masked so `skill_client._run_direct` refuses on `ISTOTA_SANDBOXED`. Both off (the `setup_wizard` pair) is not warned: no boundary exists.

`skill_proxy_timeouts` ships empty; the `code_review` ceiling is `skill_proxy.DEFAULT_SKILL_TIMEOUTS` (ISSUE-448). `skill_client_wait_seconds` (600) caps every skill budget at it minus 30s (ISSUE-450).

`sandbox_ro_paths` defaults to `[]` and is now parsed; it never was, so every deployment ran `["/srv/app"]`, exposing every DB to every task. DB masks are applied after it. `custom_system_prompt` no longer needs it: `build_bwrap_cmd` binds the single file (`custom_system_prompt_path`). `sandbox_admin_db_write` is removed; a stale key warns.

### Package caches

`resolve_sandbox_cache_dir`: with `developer.enabled` + `repos_dir`, `{repos_dir}/{user_id}/.package-caches` and `sandbox_cache_dir` is ignored; otherwise `{sandbox_cache_dir}/{user_id}`; empty keeps pre-ISSUE-305 behaviour (bwrap tmpfs, lands in `shmem_unaccounted`, discarded at exit).

- Per user always: a shared cache would be RW ground between non-admin and admin tasks, and uv runs cached wheels unverified.
- Derived for the mount: uv hardlinks and `link(2)` compares mounts, so elsewhere means EXDEV and a full copy (ISSUE-319). The repos bind is emitted after the cache bind and covers it. The path is returned as written so a symlinked `repos_dir` does not become two mounts.
- No masks: the covering exposes only the user's own subtree, so `_sandbox_cache_covering_targets`, `_sandbox_cache_is_covered`, `sandbox_cache_sibling_dirs`, `MAX_SANDBOX_CACHE_SIBLINGS`, `_BWRAP_BIND_VERBS` and the matching `native_fs_roots` denials are deleted. Instead the directory must resolve exactly to the layout path, mode set via an `O_NOFOLLOW` fd (the parent is task-writable). `_mask_dir` keeps its `bool` return.
- Open, ISSUE-320: without the `--disable-userns` precondition a concurrent same-user task could swap a symlink between check and `execve`; restoring it costs the EXDEV copy on old bwrap.
- All refusals live in `resolve_sandbox_cache_dir`, so bind, env and `native_fs_roots` drop together. It never raises (task path). Rejections fall open: relative, non-writable, under a DB dir, `_validate_workspace_dir`'s blocklist, or at/above a path the sandbox already mounts (`_sandbox_bind_targets`). That last check is one-directional; over-reading it hid ISSUE-319. Checks run on the parent, so a `repos_dir` overlapping protected paths loses its cache; move `repos_dir`.
- Planting a cache for another user (the ISSUE-319 residual) is closed by layout.
- `git_remote_scrub.find_git_dirs` and `scheduler.check_worktree_reap` skip the caches, which also stops the reaper fetching a model-written `remote.origin.url` unsandboxed. Cost: a repo under a cache is not swept.
- `UV_CACHE_DIR`, `XDG_CACHE_HOME`, `npm_config_cache`, `HF_HOME` (pinned for the RO model bind) are set after `proxy_base_env` is snapshotted, not in `build_clean_env`: host-side CLIs must not resolve caches from model-writable dirs.

Sweep (ISSUE-317): `sandbox_cache_sweep_enabled`, `sandbox_cache_max_gb` (10/user, 1 GiB floor), `[scheduler] sandbox_cache_sweep_interval`. `scheduler.sandbox_cache_sweep_root` copies the resolver's branch selection, not its refusals; user ids come from `config.users`, never from the tree. Full rules in maintenance.md (`sandbox_cache_sweeper.py`).

`CredentialBrokerConfig` (`[security.credential_broker]`): `enabled` (false), `enforce_reveal` (false), `scan_max_bytes` (1048576), `leaf_validity_hours` (24); bad integers warn and keep the default; enabled without a sandbox warns. `enforce_reveal` turns `credential_reveal action=would_refuse` audits into refusals for non-revealable entries, including manifest `env` lookups, and only applies with `enabled` (`reveal_enforced`). Turn it on after a week with no would-refuse events; it never self-activates.

## Web

`WebConfig` (`[web]`): `auth` (`["nextcloud"]`; `nextcloud`|`email`|`none`, env `ISTOTA_WEB_AUTH`). `normalize_auth_methods` drops unknowns, makes `none` exclusive, empty keeps the default; readers use `has_method`. `trusted_proxy_hops = 0` skips the IP dimension for proxied requests.

- `none` is local single-user mode, authorized only by `istota serve`'s in-process loopback marker after checking the bind. Direct uvicorn, Docker and Ansible refuse it; a SIGHUP trying it keeps the old config. `web_session_secret.resolve` is shared with doctor.
- `token_storage = "encrypted"` keeps the user OAuth pair in `web_user_tokens` under the web-only `ISTOTA_WEB_TOKEN_KEY` (≥32 chars, own salt; `web_tokens.py`). Missing key: one ERROR, ephemeral. Other values: warning, ephemeral.
- `max_avatar_kb` (4096; 0 off) is not nginx's `client_max_body_size`. Checked on `Content-Length` and again on the running total (the header is a claim); `len(await file.read())` would buffer whatever nginx passed.
- `avatar_import_from_nextcloud` (on, `[scheduler] avatar_import_interval`): imports only a **custom** avatar, told apart from Nextcloud's generated letter by one response header (absent → nothing imported). Records its result in `shared_kv` because doctor `web.avatar_import` opens no socket.

`WebMapConfig` (`[web.map]`, ISSUE-334): `provider` (`openfreemap`|`carto`|`osm`|`custom`), `api_key`, custom `dark_style`/`light_style`/`attribution`. `map_basemap.py` serves both `GET /istota/api/map/basemap` and doctor `web.basemap`. Never returns an unusable spec: unknown provider, bad custom URL or a keyed provider with no key falls back to `openfreemap` with `fell_back` (keyless templates plus a `needs_key` flag was the original bug; the flag is now the reason). `api_key` is public (it is in tile URLs). A user's stored key (`MODULE_SERVICE_SCHEMA["location"]["carto"]`) selects CARTO for them (`map_basemap.select_provider`), returned only inside the URL. **`web.basemap` opens no socket**: CARTO's watermarked tile is byte-identical to a keyed one, the daemon is not the browser, and a CDN blip would page the operator, so a keyed CARTO is "configured", never verified (`tests/test_doctor_basemap.py::TestItOpensNoSocket`).

`[web.chat] talk_read_sync_interval` (60; 0 off).

`SiteConfig` is only `hostname`. Sites were removed (per-user ISSUE-171, instance **ISSUE-194**): a RW-bound, publicly served dir made `cp` an egress channel the confirmation model read as a local write, and removing it beat teaching the model "is this path public". Gone: the bind, the `native_fs_roots` root, `WEBSITE_PATH`/`WEBSITE_URL`, the prompt section. Stale `enabled`/`base_path` warns. Ansible's static root (`istota_web_root_enabled`, `/srv/www/html`) is root-owned, in no sandbox, unwritable by istota, seeded once (`force: no`); disabled, the vhost keeps `location / { return 404; }`.

`[caldav]`: any field set overrides the NC-derived `caldav_*`; all blank = derivation.

## Rooms and speech gate

`[speech_gate] mode` (`mention`): an unknown mode fails closed (rung `failed`); DMs and addressed turns are answered before it is read. `[rooms]` is retired (in `_RETIRED`): `shared_room_data_policy` went with the grants (ISSUE-576) and a config still setting it gets a warning. Wired in `render-config.sh` + compose (`ISTOTA_SPEECH_GATE_*`) and Ansible. See transport.md "Multiplayer rooms".

## Brain

`[brain]`: `kind` (`claude_code`|`native`|`tmux_claude`), `source_type_overrides` (unknown targets ignored; `brain.resolve_brain_kind`), `room_selectable`, `fallback`, `fallback_on_transient` (ISSUE-212), `fallback_cooldown_seconds`.

- `fallback = ""` means none for every kind (ISSUE-362; tmux used to default to claude_code). `_validate_brain_fallback` neutralizes unknown kinds and a self-fallback unless a `source_type_overrides` entry routes elsewhere, and logs one INFO for `tmux_claude` without fallback. See brain.md "Brain fallback".
- `room_selectable` (empty) bounds kinds a room (`!brain`, web) or CRON.md job may pin (ISSUE-419), since `resolve_brain_kind` applies it regardless of provenance; a separate `job_selectable` was rejected (rename is a follow-up). `cron_loader.fj_brain_or_none` drops non-admin job pins (CRON.md is model-writable). Empty by default because a kind decides the loop's process, tools and sandbox profile. `_validate_room_selectable` warns on unbuildable names. A pin clears `fallback`; `brain.reachable_brain_kinds` widens doctor. Ansible `istota_brain_room_selectable`; Docker `ISTOTA_BRAIN_ROOM_SELECTABLE` (CSV through `toml_escape`). Neither renders the key at its default, since both regenerate `config.toml` (Docker each boot since ISSUE-368). `TestTheRoomSelectableAllowlist` in `tests/test_render_config.py` and `tests/test_ansible_config_template.py`.
- `[brain.tmux]`: own `model`/`effort` (ISSUE-418), trip/cooldown/timeouts, `cli_version_pin`, marker lists; `usage_limit_markers` are checked before `error_markers` → `stop_reason=usage_limit` → fallback.

`[brain.claude_code]`: own `model`/`effort` (ISSUE-418) and the subscription usage poll, read whatever `kind` is. `subscription_usage.get_snapshot` serves doctor `runtime.subscription_usage`, `/admin` and `!usage` from `{db_path.parent}/subscription_usage.json`; the credential is never written or refreshed.

- `subscription_usage` (true): false → doctor SKIP, no card. Real or quoted boolean only, warns otherwise, since `bool("false")` is True and this gates an outbound request.
- `subscription_usage_cache_ttl_seconds` (1800): freshness window and post-failure retry interval at once (a separate knob was rejected); failures are never cached as readings, a success clears the timer, stale readings are served meanwhile. 1800 because the endpoint rate-limits and its shortest window is five hours. A `Retry-After` overrides, capped by `MAX_RETRY_AFTER_SECONDS`.
- `warn_percent` (80) / `high_percent` (95) are ours, not the server's `severity`; never a FAIL. `stale_after_seconds` (3600): older readings SKIP.
- Only endpoint-produced failures are shared; "no credential" (`resolve_token` → `None`) describes the calling process and is rate-limited process-locally.
- `_validate_claude_code_brain` clamps percents to `[0,100]`, lowers `warn > high`, floors TTL and timeout at 1; non-finite → default (NaN would go amber forever, `inf` breaks `allow_nan=False` JSON). `stale_after_seconds` unfloored. No I/O.
- `subscription_usage.py` copies three defaults, pinned by `tests/test_config_claude_code_brain.py::TestOneSourceOfTruthForTheDefaults`. `deploy/ansible/files/validate_config.py` allowlists `claude_code`.

`[brain.native]` (brain.md "NativeBrain"):
- `model_overrides`: partial `ModelInfo` via `llm.catalog.set_model_overrides` (NB-4).
- `model_catalog_fetch`/`model_catalog_cache_ttl_hours`: OpenRouter `GET /models` when `base_url` has `openrouter.ai`, cached at `{db_path.parent}/openrouter_models.json` (ISSUE-182). Override > fetched > default; never fatal.
- `compaction_*_tokens`: 0 derives from the window, capped at 16k/20k (NB-14).
- `web_fetch`: on by default; all fields but `admin_only` map to `WebFetchPolicy`. `require_url_provenance` only fetches URLs from the prompt. `admin_only` (false since ISSUE-449) is read only by `executor.build_allowed_tools`.
- `session_log`: `retention_days` (14), `max_total_gb` (2.0 across all users, 0.5 floor; 0 drops it, hence the sweep's `or` gate); on the char caps 0 means no cap. `session_log.resolve_session_log_dir` maps `""` to `{db_path.parent}/logs` (masked on Ansible, only unbound on standalone and Docker). A set `dir` is literal; `/`, `.`, `..` or a null byte fall back, since the sweep deletes under it. Defaults pinned by `tests/test_config_native_session_log.py`.
- `bash_spill_full_output`: over-cap Bash output goes to a task-scoped file named in the result.
- `turn_budget_nudge*` (ISSUE-187): one notice at the early percent, one per remaining-steps level, on tool-bearing runs with `max_turns`; since ISSUE-373 also against a wall-clock turn estimate.
- `soft_deadline_percent` (90; 0 or ≥100 off, ISSUE-373): stop with `soft_timeout`, `success = True`, before `task_timeout_minutes` discards the work.
- Tiers resolve to `native.model` unless remapped (NB-3).

`[models.aliases]`: tiers and shortcuts in one table, raw. Flat string or per-namespace table (`anthropic`, `openai_compat`, `portable = true`). Floor: `brain.claude_code.DEFAULT_ALIASES`. `:effort` is a modifier. `brain._roles.set_alias_overrides` normalizes to `name -> namespace -> RoleTarget` (`"*"` flat), so an anthropic value never reaches the native wire. Warn-only. `[models.roles]` is no longer read (one-time WARNING).

## Memory and playbooks

A bare `[sleep_cycle]` header keeps `enabled = True` (the old loader made it false). `knowledge_graph_audit_retention_days` is independent of `memory_retention_days`. `[playbooks]` is off by default; `max_chars = 0` shares `max_memory_chars`; reuses `[sleep_cycle] extraction_model`.

## Skills, experimental, modules

`[skills]`/`SkillsConfig` are gone: a skill is eager or in the menu (`eligible_skill_names`). A stale block warns. See skills.md.

`[experimental] features`: operator-only, passed to subprocesses as `ISTOTA_EXPERIMENTAL_FEATURES`. Unknown names warn and are kept, so graduation is code-only. Gates: `@requires_feature` (JSON error envelope, reclassified as failure by `_execute_command_task`/`_execute_skill_task`), skills with `experimental: true` (selection, sticky, companions, `eligible_skill_names`), `EXPERIMENTAL_MODULES`, web routes and `/api/me`. Naming `module_<x>`, `skill_<x>`, free-form for CLI (`money_tax`, `money_wash_sales`). See `docs/EXPERIMENTAL.md`.

- **Resources**: only `folder` is declarable (operator-only, `istota resource ensure -t folder`); `shared_file` is organizer state. Calendars are CalDAV-discovered; todo/reminders/notes sources read their own `path`. Inert: `todo_file`, `reminders_file`. `ResourceConfig.base_url`/`api_key` live in `extra`.
- **Modules**: on by default, opt-out via `disabled_modules`, one gate `is_module_enabled`.
- **Connected services**: per-user credentials in `secrets` (Fernet over scrypt from `ISTOTA_SECRET_KEY`), schema in `credentials/schema.py`. `garmin` is cross-module: auth in `garmin_routes.py`, and `health.garmin.acquire_client` is the only sanctioned client (re-persists rotated tokens under a per-user lock).
- Settings: `_CONNECTED_SERVICE_SCHEMA` (`GET /settings/services`) and `_MODULE_SERVICE_SCHEMA` (`GET /settings/module-services/{module}`); `_all_known_services()` validates secret writes. `ServiceCard`'s generic OAuth branch is gone, so a new OAuth service needs its own card. `POST /settings/secrets/overland/ingest_token/generate` is the only endpoint returning a secret, once, and 409s with a blank `[site] hostname` (a relative webhook URL would fail the phone's decoder).

## Money and briefings

`[money] autoclass_lookup` (true): gates `portfolio_autoclass`'s third-party ticker lookup. Symbols are private and the call runs unsandboxed outside the CONNECT allowlist. Off reports `lookups_available: false`. Threaded via `money.cli.UserContext.autoclass_lookup`.

`[briefings] newsletter_max_links_per_source` (20; 0 unlimited): dropped anchors keep their text.

`BriefingConfig.blocks` is a raw passthrough read once by the seeder (`briefings/_migrate.normalize_block_specs` → `_seed_blocks`), `compare=False`, never persisted. Blocks are the sole content model: `components` is a migration-read carrier, TOML `components =` warns, `output` is a column. `[briefing_defaults]` and the legacy generator are gone. `[[default_briefings]]` is seeded into opted-in users (`UserConfig.default_briefings`, column default 1) before the DB overlay; a user briefing of the same name wins.

## Per-user profile fields

- `email_reply_routing` (`origin+thread`|`origin`|`thread`) via `Config.email_reply_routing_for`.
- `outbound_approval` (`''`|`off`|`untrusted`|`all`): `''` is unset, resolves to the floor. `outbound_policy.effective_policy` = `max(floor, user)`: users tighten, never loosen. An invalid row warns and counts as unset.
- `external_turn_display` (`collapsed`): `hidden` still shows the header row, since a bot answer with no question above it is the ISSUE-136 defect.
- `briefing_email_html` (true; unknown user → True): `multipart/alternative` via `render_briefing_html`.
- `google_scopes` is a JSON column, deliberately not a `UserConfig` field: the user's choice within `[google_workspace] scopes`, written only by `PUT /api/google/scopes`, no CLI or TOML, because a grant is the user's consent.

## Module DBs (local disk, WAL)

Framework DB: WAL set once in `init_db`, never per open (re-issuing takes a write lock that stalled the dispatch loop). `get_db` takes `busy_timeout_ms=` for read-only loop scans (`scheduler.main_loop_read_timeout_ms`, 2000). Module DBs moved from FUSE `DELETE` mode (ISSUE-157, ISSUE-156) to local WAL at `module_db_path`; only the `.db` moved. `python -m istota.db_relocate` migrates. Backups (`db_backup`, dated snapshots, collapse guard, mount-liveness guard, staleness alert), restore (`db_restore`) and the off-thread runs (ISSUE-144) are in maintenance.md and AGENTS.md.

## Standalone install

`auth = ["none"]`, blank `nextcloud.url`, `talk.enabled = false`, `workspace_path` `~/.istota`. Every default is the server behaviour. Spec `Specs/Done/local-single-user-install.md`; wizard and updater rules in install.md.

- `istota serve` runs the scheduler on a worker thread (`install_signal_handlers=False`) and uvicorn on main; `_DaemonAlreadyRunning` on flock contention (`scheduler.DAEMON_LOCK_PATH`); sources `istota.env` non-clobbering and sets `ISTOTA_CONFIG_PATH`.
- `istota update` is standalone-only (no contention with the Ansible cron). `install.json` records the channel; a record without one defaults to `main` so it is never reset backwards onto an older tag. Migrations use the freshly installed `istota init`, since nothing on startup runs them; failure rolls the checkout back.
- `schema.sql` is `force-include`d into the wheel (`db._resolve_schema_path`); `web_app._pick_static_dir` falls back to packaged `web_static`; `web_app._root_redirect` sends `/` to `/istota/`.
- "Sandbox disabled" logs INFO under `is_standalone`, WARNING otherwise.
- Storage vocabulary follows `storage_backend`, not `is_standalone`; Nextcloud prompts are unchanged.
