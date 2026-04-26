# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Web UI: **+ New place** action in the location sidebar — works on both Today and History. Click it, then click anywhere on the map to drop a place there.
- Web UI: **Discover** chip on the location History page overlays unknown recurring clusters and dismissed zones onto the same map, in the same spatial context as your pings and tracks. Click a yellow cluster to name it (or dismiss it); click a dimmer dismissed circle to restore.

### Changed
- Web UI: location Today now uses the same full-width bottom bar + collapsible details panel as History — current visit (place / duration / time-since / battery) is inlined, then ping/stop/transit/trip counts, then a single Show details toggle. The floating info card is gone.
- Web UI: the standalone Places page has been removed. Place creation, discovery, and dismissal are reachable from Today and History via the shared sidebar and the Discover chip — there is no longer a separate `/location/places` view.

### Fixed
- Web UI: location map no longer crashes on initialisation when discovered clusters are present. The previous `circle-stroke-dasharray` paint property is unsupported by MapLibre and was throwing a style-validation error that dropped the WebGL context as soon as the cluster source contained any features.
- Web UI: location Today info panel no longer covers the map zoom controls — moved to bottom-left to match the existing mobile layout.
- Local dev: `VITE_MOCK_API=1` mock backend now actually persists place creation/edit/delete and cluster dismiss/restore in-memory across requests, so the full flows can be exercised without a live FastAPI backend.

## [0.8.0] - 2026-04-26

### Changed
- Web UI top nav collapses into a hamburger menu on mobile (≤ 640 px) so the page links no longer wrap below the "Istota" title and there's headroom for more sections. Built on `bits-ui` `DropdownMenu` for keyboard/ARIA correctness; desktop layout unchanged.
- Sidebar toggle on mobile (≤ 768 px) is now a vertically-centered chevron tab affixed to the left edge, replacing the earlier bottom-left chip that clashed with bottom-anchored UI like the location day-summary card. Affects feeds (Sources), location (Places), money/transactions (Accounts), and money/accounts.
- Mobile sidebar can now be dismissed by tapping anywhere outside it. The toggle hides while the sidebar is open since the sidebar would obscure it.
- Money year selectors now show "All" instead of "All years" so they fit in their intrinsic-width state on mobile (transactions, reports, accounts).
- On the money transactions and accounts pages the year + filter inputs stay on a single row at all viewport sizes; the filter input grows to fill the space the year selector leaves.
- Money transactions list rows now align flush with the section header above them — previously they were inset by an extra ~0.5 rem due to nested padding.

### Security
- Deferred subtask creation now bounds prompt-injection blast radius. New `scheduler.max_subtask_depth` (default 3) refuses subtask creation when the parent chain is already at the cap — worst-case fan-out drops from unbounded to 10 + 100 + 1000. New `scheduler.max_subtask_prompt_chars` (default 8000) skips oversize prompts. The existing per-task cap of 10 is now exposed as `scheduler.max_subtasks_per_task`. INFO log on creation lists prompt prefixes for audit trail.
- Linux + bubblewrap is now documented as the only supported deployment configuration. Non-Linux / no-bwrap setups still run for development but provide no isolation guarantees. Scheduler now logs `SECURITY UNSUPPORTED CONFIGURATION` at WARNING level when sandbox is unavailable or explicitly disabled with multiple users configured (previously a softer informational message). Closes audit item M4.
- Skill proxy and network proxy Unix sockets are now created with `0o600` permissions immediately after `bind()`, so other local users on the same host can no longer connect during a task window. (Audit L2.)
- Web API place-creation and cluster-dismiss endpoints no longer return raw exception strings to the browser. The full exception is still logged server-side; the response body is a generic `failed to create place` / `failed to dismiss cluster`. (Audit L7.)
- `!status` system-wide running/queued counts are now hidden from non-admin users. The per-user task list is unchanged. Admins still see the full system view. (Audit L8.)
- ntfy `Title` and `Tags` headers are now CR/LF-stripped at the boundary, replacing any newlines with a single space. Prevents header injection if the input ever contains a stray newline (httpx already rejects them, so this avoids the `RequestError` rather than introducing exploitability). (Audit L10.)

### Changed
- Skill proxy credential authorization is now decoupled from skill selection. Any CLI skill whose mapped credentials are present in the user's task environment can request them at runtime — Pass 1 keyword matching and Pass 2 semantic routing only control which skill docs go into the prompt, not which credentials are accessible. Fixes the long-standing failure mode where a keyword miss would silently strand an agent without the credentials it needed.
- Pass 2 (semantic routing) prompt now includes the user's resource types so Haiku can reason "user has miniflux configured → feeds is plausible" without keyword overlap. Each skill line in the manifest also carries a `[needs resource: …]` hint when applicable.

### Added
- Structured WARNING logs on every skill-proxy rejection, keyed by task and reason code (`proxy_rejected task_id=… type=skill|credential reason=unknown_skill|not_authorized_credential|credential_not_present`). Companion INFO logs from skill selection (`pass1_selection count=N: foo(always_include), bar(keyword='kw'), …` and `pass2_added` / `pass2_no_additions` / `pass2_timeout`) make it possible to count selection misses vs. real abuse attempts.
- Skill-proxy rejection responses now include `reason`, `name`/`skill`, and `authorized_skills` fields. The `istota-skill` client surfaces the authorized-skills list to the model via stderr so it can adapt rather than retry blindly.

### Added
- Five new `istota-skill location` subcommands matching the web UI: `discover` (find unknown recurring clusters), `dismiss-cluster` / `list-dismissed` / `restore-dismissed` (manage zones the discover view should skip), and `place-stats` (visit count, first/last/longest visit, total time spent — derived from pings).

### Changed
- Skill docs (`money`, `bookmarks`, `location`, `memory_search`, `feeds`) now point at `istota-skill <name> --help` for the live argument list, alongside the existing hand-enumerated examples.
- `money/skill.md` lists `run-scheduled` (previously omitted) and includes it in the mutation/concurrency rule.

### Fixed
- `istota-skill money run-scheduled` now works. The Click subcommand existed in the underlying `istota.money` CLI but was never wired into the `istota-skill` argparse wrapper, so the auto-seeded `_module.money.run_scheduled` cron job exited with usage help instead of running.
- Sidebar no longer side-scrolls when child content exceeds its width. Long place names truncate with an ellipsis instead of expanding the row.
- Place row hover background now reads symmetric top/bottom (explicit `line-height` + rebalanced padding) and left/right (matching gutter on both sides of the sidebar list).

### Changed
- Location places sidebar: removed the per-row radius badge and the hover-to-delete `×`. Rows now show the place name only.
- Place delete moved into the place edit modal as a left-aligned "Delete" link guarded by a confirmation prompt — no more accidental deletes from the sidebar.
- Place edit modal's category dropdown now lists the base categories *plus* every distinct category in use across the user's places (deduped, alphabetized), so a category created once stays available for the next place.

### Added
- Reusable web UI primitives in `web/src/lib/components/ui/`: `AppShell`, `ShellHeader`, `Sidebar`, `SidebarToggle`, `CategoryGroup`, `NavLink`, `Button`, `Select` (bits-ui Select wrapper), `Modal` (bits-ui Dialog wrapper). Replaces ~400 lines of duplicated shell/sidebar CSS across the four route layouts.
- `--chip-padding-x` and `--chip-gap` CSS variables in `app.css`; `.nav-hang` utility for hanging-pill alignment so chip text aligns with surrounding heading text.
- `CategoryGroup` supports a `collapsible` prop with caret toggle. Location places sidebar groups now collapse like the transactions account tree.
- Vite middleware mock (`web/vite-mock-api.ts`, gated on `VITE_MOCK_API=1`) lets `npm run dev` render the full UI with HMR without the FastAPI backend running.
- Logout link in the top nav is now a Lucide `LogOut` icon.
- Per-job `model` and `effort` overrides in `CRON.md`. Add `model = "claude-sonnet-4-6"` and/or `effort = "low"` to any `[[jobs]]` block to pin that one job to a specific Claude model and effort level. Per-task wins over `config.model` / `config.effort`; neither set = CLI default. Useful for downgrading volume "retrieve-and-render" jobs (briefings, transcription cron, feed digests) to Sonnet without touching the global default.
- Loose validation on `CRON.md` load: warns (never rejects) when `model` is missing the `claude-` prefix or contains whitespace, and when `effort` isn't in `{low, medium, high, xhigh, max}`.
- `!cron` listing now surfaces per-job `model: X` / `effort: Y` inline.
- Log channel finalize header now appends the resolved model + effort inline — e.g. `✅ Done (3 actions) - cli (claude-opus-4-7 high)` — so per-job overrides are visible at a glance without cross-referencing CRON.md.
- `effort` config field (top-level in `config.toml`, `istota_effort` in Ansible) wires Claude Code's `--effort` flag for adaptive reasoning. Accepts `low`, `medium`, `high`, `xhigh`, `max`. Supported on Opus 4.7, Opus 4.6, Sonnet 4.6. Empty = model default.
- `agents:` markdown frontmatter convention baked into the system prompt: per-file instructions (1–3 sentence string) travel with a file and are honored on reads from trusted paths, ignored on untrusted paths.
- In-tree `istota.money` subpackage (formerly the standalone moneyman service): accounting CLI, business logic, and SvelteKit pages folded into istota. Optional install: `pip install istota[money]`.
- Money web pages at `/istota/money/*` (Accounts, Transactions, Reports, Taxes, Business). Feature flag exposed via `/istota/api/me` as `features.money`; nav item appears when the user has a money resource.
- Money skill is in-process — no API key, no HTTP round-trip. Resource type accepts both `money` and legacy `moneyman`.
- Per-user money scheduled job `_module.money.run_scheduled` (daily 8 AM). Seeded under a reserved `_module.money.*` name prefix; auto-removed when a user's resource or feature config disappears. Folds in an opportunistic monarch sync (when `monarch_config` is set) followed by the invoice schedule check. Skipped entirely for ledger-only users. Workspace-mode users are seeded too — previously skipped.
- Workspace-mode money config loading: `INVOICING.md` / `TAX.md` / `MONARCH.md` files (TOML in fenced code blocks) in the user's workspace `config/` dir. Legacy `*.toml` files still accepted as a fallback.
- `EnvSpec.resource_types` — a declarative skill env spec can now match any of multiple resource types.
- `scripts/migrate_money_workspace_config.py` — one-shot migration from legacy `*.toml` to `*.md`.
- `Config.namespace` field — the install namespace (drives `/etc/{namespace}/`, etc.) is now a first-class config field, parsed from the TOML's top level and emitted by the ansible role.

### Changed
- Web UI: secondary navbars across feeds / location / money standardized — same chip styling, font size (`--text-sm`), padding, gap, line-height. App nav background bumped to `#1a1a1a` to differentiate from the page bg `#111`. Sidebar default width unified to 220px.
- `routes/location/+layout.svelte`, `routes/feeds/+layout.svelte`, `routes/money/+layout.svelte`, `routes/money/transactions/+layout.svelte` migrated onto the new shell/sidebar primitives. `lib/components/location/PlaceForm.svelte` uses `Modal` + `Select` + `Button` instead of hand-rolled overlay/backdrop/select.
- Three raw `<select>` elements (ledger picker, transactions year picker, place category) replaced with the bits-ui-backed `Select` primitive.
- Custom system prompt (`config/system-prompt.md`, used when `custom_system_prompt = true`) gained an "Executing actions with care" section covering reversibility, risky-op examples, investigate-before-destroy, and scoped authorization. Sleep guidance split into two specific rules. Synced against Claude Code 2.1.120's extracted prompts; pieces that duplicate `emissaries.md` / `persona.md` were intentionally left out.
- Documentation now recommends pinning the `model` config to a full version ID (e.g. `claude-opus-4-7`) rather than an alias (`opus`), so a Claude Code update can't silently swap the model out from under us. Aliases still work but float to whatever Anthropic ships next.
- Money is now `src/istota/money/` instead of a top-level `src/money/` package; the standalone-extract scaffolding is gone. Web routes, skill, and scheduler all call the same in-process `istota.money.resolve_for_user(user_id, istota_config)`.
- Money skill no longer marshals env vars for workspace mode; it resolves the user's `UserContext` in-process and injects it into Click directly. The standalone `money` CLI keeps file-based config support (`MONEY_CONFIG=...` or `-c <path>`) for terminal use.
- Money scheduled jobs invoke `istota-skill money <cmd>` with `MONEY_USER` set, instead of `MONEY_CONFIG=… money --user X <cmd>`. `MONEY_SECRETS_FILE` is no longer exported by seeded jobs — the skill reads credentials in-process.
- `run-scheduled` now bundles an opportunistic monarch sync (when `monarch_config` is set) before the invoice check, with a new `--skip-monarch` flag for opt-out. Replaces the previously separate `monarch_sync` auto-seeded job — users who want a narrated/observable monarch sync layer their own prompt-based job in `CRON.md` on top.
- Monarch credentials (`monarch_session_token` / `monarch_email` / `monarch_password`) now live on the user's `[[resources]] type = "money"` entry, matching the karakeep / miniflux / overland convention. The previous per-user `/etc/{namespace}/secrets/{user_id}/money.toml` file is removed.
- The `[[resources]] type = "moneyman"` rendering now emits `type = "money"` (the loader still accepts both forms).
- Ansible: `[moneyman]` block removed from `config.toml.j2`; the moneyman nginx include is dropped; standalone moneyman cron entries are no longer used (the istota scheduler runs them per-user).

### Removed
- Standalone money REST API (`istota.money.api` package) and the `money serve` CLI subcommand. The SvelteKit pages consume `istota.money.routes` (session-auth router mounted by the istota web app), and the skill calls money in-process — no separate HTTP service needed.
- Per-user money secrets file at `/etc/{namespace}/secrets/{user_id}/money.toml` and the `money-secrets.toml.j2` ansible template. Replaced by colocated credentials on the money resource entry.
- `money.config` module (TTL cache, `set_loader`, mtime invalidation) — replaced by direct `resolve_for_user` calls.
- `MONEY_WORKSPACE` / `MONEY_DATA_DIR` / `MONEY_CONFIG_DIR` / `MONEY_LEDGERS` / `MONEY_DB_PATH` environment variables and the `setup_env` hook on the money skill — no longer needed once the resolver runs in-process.
- `web_app._install_money_loader` and the SIGHUP loader-reinstall step — replaced by setting `app.state.istota_config` after each config load.
- Public-extract tooling for moneyman (`scripts/extract_money_to_standalone.py`, `scripts/check_money_isolation.sh`, `tests/test_money_extract.py`).

### Deprecated
- `istota_moneyman_*` ansible vars (`api_url`, `api_key`, `cli_path`, `config_path`) — kept as no-ops for inventory compatibility but no longer rendered into config. Use the per-user `[[resources]] type = "money"` entry instead.

### Fixed
- Prompt now carries a single, consistent answer to "what's today" in the user's local zone. The header emits `Current time` / `Today's date` / `User timezone`, conversation-context timestamps (Talk and DB) render in `user_tz` instead of UTC, and the rules section explicitly tells the model to ignore the auto-memory `currentDate` (which Claude Code injects in UTC). Closes ISSUE-056.
- Money skill in sandboxed task runs no longer fails with "Unknown user" for workspace-mode users (resource entry without `config_path`). The unified resolver handles workspace and legacy modes uniformly across web, skill, and scheduler call sites.
- Monarch sync no longer fails with "No Monarch credentials configured" on instances whose namespace differs from `"istota"`. The hardcoded fallback in `_loader.load_user_secrets` and `scheduler._sync_money_module_jobs` was reading from the wrong `/etc/...` path; now uses `Config.namespace` directly. Also obsoleted by the unified credential storage on the resource entry.
- Monarch `sync-monarch` recategorization for income postings: removing a `#business` tag from an income transaction in Monarch produced a malformed ledger entry (double-credited the income account, introduced a phantom personal-expense debit, never reversed the original contra leg). The formatter now branches by account type — true reversal for income postings, category swap for expenses — and `monarch_synced_transactions` tracks `contra_account` so the reversal has the second leg available. Income→income category changes flip signs symmetrically. Income recats for rows synced before this change are skipped and surfaced in the sync result for manual reversal.

## [0.7.0] - 2026-04-24

### Added
- Temporal knowledge graph: entity-relationship triples with validity windows, queryable via `!memory facts` and the memory_search CLI, surfaced into prompts as relevance-filtered "Known facts."
- Topic and entity metadata on memory chunks, with filtered search via `--topic` and `--entity`.
- Place categories for medical, hotel, and transit; notes field on places (web + CLI); UI flow to create a place from a map click; dismissable cluster zones on the location places page.
- Asymmetric place-visit detection: accuracy gate, dwell-based exit, and a periodic batch reconciler that re-derives closed visits from recent pings.
- Speed-gradient path coloring (extended through magenta/white for rail), transit-run heuristic, browser-timezone day grouping, dwell-weighted heatmap, and great-circle arcs for long gap edges in the location web UI.
- Service manifest spec tiers (T1–T4) and per-service install field for the upcoming installer wizard (ISSUE-032).
- `log_channel_show_skills` config to include selected skills in log channel entries.

### Changed
- GPS outlier detection: lookahead + perpendicular test catches chained and off-axis bad fixes; single-ping outliers dropped before path rendering; transit-stop pings kept in path while true dwells are dropped.
- Path runs split at dwell boundaries to quiet up the month view; long gap edges rendered as great-circle arcs.
- Sleep cycle routes personal attributes to FACTS only, uses annotated suggested predicates with temporal-field guidance, and de-duplicates via word-bigram Jaccard; USER.md curation cross-references the KG to avoid restating facts.
- Behavioral instructions split out of user memory into the relevant skill prompts so USER.md only carries durable user-specific facts.
- Adaptive KNN `k` in vector memory search to survive post-filter starvation; UNIQUE constraint on current knowledge-fact triples (legacy rows de-duplicated on migration).
- Feeds page paginates with a `before` cursor under the unread filter, fixing infinite-scroll stalls.
- Persona and Talk guideline tightened: emoji defaults to none, work-effort puffery banned.

### Fixed
- Inbound tasks that trip the Anthropic policy filter no longer retry silently three times — they fail immediately and post a named alert to the user (ISSUE-033).
- Indoor GPS gaps no longer drop intermediate stops from the day summary (ISSUE-043).
- Location stop discovery no longer fragments a single dwell into multiple stops; `duration_minutes` surfaced.
- `!stop` cancelled tasks no longer get retried by the scheduler.
- Channel-level single-foreground-task gate now enforced at task claim time, preventing two workers running concurrently in the same channel.
- `selected_skills` missing from `get_task` SELECT and from log-channel entries for tasks with no tool calls.
- Heartbeat interval-elapsed test uses UTC to match production behavior.

## [0.6.1] - 2026-04-06

### Added
- Google Workspace skill: OAuth web-UI authentication, Drive/Gmail/Calendar/Sheets/Docs/Chat via the standalone `gws` binary, configurable scopes (read-only by default), credentials injected via the skill proxy.
- Email confirmation gate: plus-addressed mail from untrusted senders is held in `pending_confirmation` until the user approves via Talk; trusted-sender list editable at runtime from Talk.
- Suspicious-email user alerts: deferred `user_alerts.json` posts to the alerts channel for prompt injection / exfil attempts; alerts channel also notified after confirmed email tasks complete.
- Skill stickiness for conversation follow-ups: skills from the last 2 conversation tasks (within 30 min) and the explicit reply parent are added to Pass 1.

### Changed
- Skill-proxy credential allowlist derived from the skill index instead of hardcoded; all CLI-capable skills get their credentials regardless of which were selected for the task.

### Fixed
- Email replies to emissary tasks were silently dropped (ISSUE-031).
- Day summary merged Home stops across separate trips, hiding short away-from-home segments.

### Security
- High- and medium-severity findings from the codebase audit fixed across the skill proxy, deferred-file handling, web auth, and sandbox env handling.

## [0.6.0] - 2026-04-04

### Added
- Per-user plus-addressed email ingest (`bot+user_id@domain`) so external contacts can email a specific user's agent directly.
- Place management for the location skill: full CRUD via CLI and web UI, drag-to-reposition, ping-based visit stats, geofence circle interpolation through zoom 22, and ping reassignment when a place moves.
- `custom_system_prompt` config toggle replaces Claude Code's default prompt with a minimal one focused on tool use.
- Viewport-based read tracking on the feeds web page, with a "New" filter chip and unread count badge.
- Two-pass skill selection: deterministic Pass 1 plus a Haiku-based semantic-routing Pass 2, configurable via `[skills]`.
- Moneyman/Fava integration in the web UI: Services page (later replaced), per-user Fava reverse proxy under nginx.
- `git-cliff` configuration and changelog generation in the release workflow.

### Changed
- All bundled skill metadata consolidated into `skill.md` YAML frontmatter; `skill.toml` files removed from bundled skills (operator overrides may still use TOML).
- Network allowlist scoped to the current task's user (M-2); CSRF Origin checks added to all state-changing web endpoints (M-3); OIDC session rotated on login (M-4); deferred `sent_emails` no longer trust user_id from JSON (M-1).
- Location config moved from `LOCATION.md` to per-user `[[resources]]` of type `overland`; `LOCATION.md` removed.
- Stationary pings rendered as dots instead of connected lines; path segmentation only breaks on real spatial/time gaps, not activity changes.
- Moneyman service config moved from per-user resource to instance-level `[moneyman]`; per-user API key derived for HTTP calls.

### Removed
- `LOCATION.md` (replaced by DB-backed places + per-user TOML config).

### Fixed
- Context-management mid-response no longer causes duplicate delivery — the executor segments by CM boundaries and uses the last substantial segment (ISSUE-026).
- `SMTP_FROM` plus-addressed sender that some mail servers rejected.
- Stop-detection centroid drift; visit splitting now uses elsewhere-based detection instead of a fixed time gap.
- Location date display rendering in UTC instead of local time (ISSUE-029).
- Feeds infinite scroll not loading when filters hid most entries.
- Geofence radius display + map reset on place drag.

### Security
- Four medium-severity findings from the codebase audit fixed; warning when web session secret uses the insecure default.

## [0.5.0] - 2026-03-22

### Added
- `!search` command for searching Talk conversation history across the memory index, the Talk unified search API, and exported conversations.
- New `feeds` skill with Miniflux CLI (list, add, remove, categories, entries, refresh) and `miniflux` resource type.
- Authenticated web interface: SvelteKit frontend with Nextcloud OIDC login, dashboard, feeds page reading directly from Miniflux.
- Moneyman skill: dual-mode (CLI subprocess preferred, HTTP fallback) accounting client for ledgers, transactions, invoicing, and work log.
- `!more #<task_id>` command and `actions_taken` / `execution_trace` columns to surface task internals.

### Changed
- Replaced built-in feed polling with Miniflux as the RSS aggregator; non-RSS sources bridged via a separate `rss-bridger` service.
- `install.sh` rewritten as a thin Ansible bootstrap; the 1765-line script is gone, the wizard delegates to the bundled role.
- Briefing delivery is now deterministic: Claude returns structured JSON, the scheduler handles delivery, the email skill is excluded from briefing tasks.
- Stream parser deduplicates by tool/text block ID instead of `stop_reason`, so tool calls and interrupted responses are no longer dropped (ISSUE-024 follow-up, ISSUE-025).

### Fixed
- Malformed model output (raw tool-call XML under context pressure) is now detected and routed through retry instead of delivered as a "successful" empty response (ISSUE-019, partial).
- Browse skill no longer flags small passive reCAPTCHA badges as captchas.
- Five Debian 13 install bugs caught via Docker-based testing (pipx ensurepath, missing unzip/cron, Ansible v12 yaml callback, rclone password override, rclone obscure invocation).

### Removed
- `!usage` command — Anthropic blocks non-official clients from `/api/oauth/usage`.
- `garmin` skill — Garmin's SSO change broke `garth`; data access moves to the browse skill.
- Direct `accounting` skill, invoice scheduler, and the `accounting` extras group — all accounting flows through Moneyman now.

## [0.4.1] - 2026-03-18

### Added
- Emissary draft-approve-send flow: confirmed tasks get the bot's previous output injected as `confirmation_context` so it executes instead of re-drafting (ISSUE-016 Phase 2).
- Emissary email thread tracking: outbound mail recorded in `sent_emails`; replies from external contacts route back to the originating Talk conversation (ISSUE-016 Phase 1).
- Headlines briefing component: pre-fetches frontpages from AP, Reuters, Guardian, FT, Al Jazeera, Le Monde, Der Spiegel via the browser API.
- Briefing digest persistence — the previous briefing's body is included in the next prompt to reduce repetition.
- `prompt_file` field for CRON.md jobs so long prompts can live in separate files; `--tz` flag on calendar create/update for timezone-aware events.
- Per-user scripts directory under the bot dir, plus Garmin and Monarch credentials configurable as `[[resources]]` entries.

### Changed
- Sleep cycle memory extraction reworked: tail-biased excerpts, dynamic per-task budgets, conversation grouping, tightened prompt with examples (ISSUE-018).
- External email default sender identity is the bot, not the user, unless explicitly asked (ISSUE-017).
- Empty `all_descriptions` no longer leaves the ack stuck on "Riffing…"; reruns post a fresh ack so edit-in-place works.

### Fixed
- Briefing pipeline could leak one user's calendar events into another user's output; the unscoped fallback is gone and CalDAV credentials only flow when the user has discovered calendars (ISSUE-015).
- `location history --date` was filtering by naive UTC and capping at 20 pings; both fixed, `--tz` flag added.
- FinViz fetch now retries up to 3 times before giving up; previously a single transient failure stripped market data from the briefing.

## [0.4.0] - 2026-03-13

### Added
- Network isolation for the bwrap sandbox: each task runs in its own network namespace and reaches the outside world only through a CONNECT proxy with a host:port allowlist; defaults cover the Anthropic API and PyPI.
- Credential-isolated developer tokens: `GITLAB_TOKEN` and `GITHUB_TOKEN` go through a `credential-fetch` helper instead of the subprocess env when the skill proxy is enabled.
- Docker Compose stack: postgres, redis, Nextcloud, and Istota in four containers with auto-provisioning, optional browser and webhooks profiles.

### Changed
- Per-skill credential scoping: the proxy only returns secrets needed by the task's selected skills, and each skill CLI subprocess only sees its own env vars.
- Admin Nextcloud mounts now match non-admin scoping — own user dir + channel dir + explicit resources, not the whole content tree.
- Intermediate text blocks accumulated during streaming are prepended to the final result so tool-interleaved status updates aren't lost.

### Fixed
- CONNECT proxy no longer kills streaming API responses — tunnel timeouts are cleared after CONNECT and TCP keepalive is enabled.
- Skill proxy socket bind-mounted into the bwrap sandbox so it's actually visible at `/tmp/istota-proxy-{task_id}.sock`.
- `_warn_orphaned_email_output` no longer deletes legitimate deferred email files for briefings.
- Talk poller `list_conversations` cached with a 60s TTL and 15s timeout so transient ReadTimeouts don't abort poll cycles.

## [0.3.1] - 2026-03-13

### Fixed
- Skill proxy Unix socket was invisible inside the bwrap sandbox; now bind-mounted at `/tmp/istota-proxy-{task_id}.sock`.

## [0.3.0] - 2026-03-13

### Added
- Credential isolation via Unix-socket skill proxy: secret env vars stripped from Claude's environment, skill CLIs run through a server-side proxy that injects credentials.
- GPS location tracking via Overland webhook receiver with hysteresis-based place transitions, calendar attendance correlation, day summaries, and reverse geocoding.
- DST-safe scheduling: cron evaluation now uses naive local wall-clock times so spring-forward doesn't double-fire jobs and briefings.
- Per-user log channel for verbose tool-by-tool execution traces, with configurable `progress_style` (`replace` / `full` / `legacy` / `none`).
- `!export` command exports a Talk channel's full conversation history to a file in the user's workspace (markdown or text, incremental on repeat).
- Multi-user Talk room support: bot only responds when @mentioned in rooms with 3+ participants; reply threading and @mentions on the final response in group chats.
- Memory recall with BM25 search, dated-memory auto-load (`auto_load_dated_days`), `max_memory_chars` cap, and optional nightly USER.md curation.
- Heavy optional deps moved to extras groups; `!skills` shows availability with install hints; `dependencies` declared per-skill in `skill.toml`.

### Changed
- Skills restructured into self-contained directory packages under `src/istota/skills/` with `skill.toml` manifests and declarative env-var wiring.
- Conversation context now reads from a poller-fed local cache (`talk_messages`) instead of per-task Talk API calls; recency window (`context_recency_hours`) added.
- Briefing system consolidated into `skills/briefing/`; legacy `briefing.py`, `briefing_loader.py`, and `skills_loader.py` shims removed.
- One-time CRON.md jobs (`once = true`) are auto-removed from both DB and file after success; reminders skill template updated to match.

### Fixed
- Bot replies were absent from conversation context after the cache migration — root cause was a multi-thread race between poller and scheduler when re-tagging `:progress` to `:result`. Fixed with direct upsert and `ON CONFLICT DO UPDATE` preserving result tags.
- Production crash when `check_briefings()` held a write transaction across slow network I/O; split into read → prefetch → write phases.
- Per-channel gate after `!stop` no longer rejects new messages from cancelled tasks still in `running`.

## [0.2.0] - 2026-03-01

### Added
- Per-user filesystem sandbox via bubblewrap (`bwrap`): Claude Code subprocess runs inside a mount namespace, non-admins see only their own subtree.
- Deferred DB operations pattern: with the sandbox mounting the DB read-only, Claude and skill CLIs write JSON request files to a per-user temp dir for the scheduler to process.
- Three-tier worker concurrency: separate fg/bg instance caps, per-user limits, and a per-channel gate that queues duplicate-channel messages instead of discarding them.
- `!command` dispatch (`!help`, `!stop`, `!status`, `!memory`, `!cron`, `!check`, `!skills`) intercepted in the Talk poller before task creation.
- Heartbeat monitoring system with five check types (file-watch, shell-command, url-health, calendar-conflicts, task-deadline, plus self-check), cooldowns, and quiet hours.
- Hybrid BM25 + vector memory search (sqlite-vec + sentence-transformers), with channel sleep cycle and channel-namespace indexing.
- Whisper audio transcription skill with RAM-aware model selection; pre-transcription before skill selection so voice memos hit keyword rules.
- ntfy push notifications and a centralized notifications dispatcher; per-user `ntfy_topic` override.
- Webhook receiver service for GPS pings (Overland), separate from the scheduler.

### Changed
- Talk progress edits the initial ack message in-place instead of posting up to 5 separate messages; final message shows "Done — N actions (Xs)".
- Scheduled jobs gain isolation: excluded from interactive context, prioritized below interactive in dispatch, `silent_unless_action` mode, auto-disable after N consecutive failures.
- `[security]` clean-env subprocess + `--allowedTools` whitelist + credential stripping for heartbeat/cron commands; `EnvironmentFile=` support in systemd.
- Scheduled job definitions moved from sqlite-only to user-editable `CRON.md` files with TOML `[[jobs]]` blocks.
- Per-user directory structure: `workspace/` renamed to `{bot_dir}/`, config files moved into `{bot_dir}/config/`, with auto-migration.

### Fixed
- Bubblewrap on Debian 13: re-enabled unprivileged user namespaces, fixed merged-usr symlink resolution and dest-path handling for `/etc/resolv.conf` so DNS works inside the sandbox.
- Email header newline injection in `In-Reply-To`/`References` no longer causes outbound delivery failures.
- Briefings excluded from auto-loading user/dated memory to prevent private context leaking into newsletter output.

## [0.1.1] - 2026-02-21

### Fixed
- E2BIG on large prompts — prompt is now passed via stdin instead of `argv`, bypassing the 128 KB execve limit.
- `claude -p` requires the prompt as a positional arg; restored after the stdin migration.

## [0.1.0] - 2026-02-21

### Added
- Initial public release of Istota — Claude Code-powered assistant with Nextcloud Talk interface, forked from Zorg.
- Talk integration via long-polling (user API, not bot API), email input/output via IMAP/SMTP, and TASKS.md file polling.
- Per-user concurrent task queues with atomic locking, retry with exponential backoff, and stale-task cleanup.
- Streaming task execution: `subprocess.Popen` with `--output-format stream-json`, real-time tool-use progress posted to Talk.
- Skills system with selective loading by keyword/resource/source type; bundled skills cover files, email, calendar, todos, memory, markets, browse, accounting, developer (Git/GitLab/GitHub), nextcloud, and more.
- Sleep cycle: nightly memory extraction writes dated `YYYY-MM-DD.md` files; multi-tiered memory model (USER.md, CHANNEL.md, dated memories).
- Briefings: cron-based, components for calendar/todos/email/markets/news/notes/reminders, BRIEFINGS.md user config.
- Scheduled jobs (DB-driven), invoicing system with PDF export and beancount A/R, Monarch Money sync, Fava per-user systemd service.
- OCS API skill, OCR transcription skill, web browsing skill via Dockerized Playwright with VNC captcha fallback.
- Admin/non-admin user isolation via root-owned `/etc/istota/admins`; admin-only skills filtered for non-admin users.
- Emissaries (constitutional principles) layered before persona; per-user `PERSONA.md` overrides global persona.
- Interactive install wizard (`deploy/install.sh`) with Nextcloud connectivity validation, rclone obscure auto-generation, and `--dry-run` mode.
- Tag-based release deployment via `repo_tag` setting (`"latest"` resolves highest `v*` tag).

- MIT license, README rewritten with security model and origin story.
- Hybrid context selection: recent N messages always included, older messages triaged by Haiku/Sonnet.
- Native `imap-tools` + `smtplib` email backend with RFC 5322 References-header threading (replacing the pre-fork himalaya CLI).

[Unreleased]: https://gitlab.com/cynium/istota/-/compare/v0.8.0...main
[0.8.0]: https://gitlab.com/cynium/istota/-/releases/v0.8.0
[0.7.0]: https://gitlab.com/cynium/istota/-/releases/v0.7.0
[0.6.1]: https://gitlab.com/cynium/istota/-/releases/v0.6.1
[0.6.0]: https://gitlab.com/cynium/istota/-/releases/v0.6.0
[0.5.0]: https://gitlab.com/cynium/istota/-/releases/v0.5.0
[0.4.1]: https://gitlab.com/cynium/istota/-/releases/v0.4.1
[0.4.0]: https://gitlab.com/cynium/istota/-/releases/v0.4.0
[0.3.1]: https://gitlab.com/cynium/istota/-/releases/v0.3.1
[0.3.0]: https://gitlab.com/cynium/istota/-/releases/v0.3.0
[0.2.0]: https://gitlab.com/cynium/istota/-/releases/v0.2.0
[0.1.1]: https://gitlab.com/cynium/istota/-/releases/v0.1.1
[0.1.0]: https://gitlab.com/cynium/istota/-/releases/v0.1.0
