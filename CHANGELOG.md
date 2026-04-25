# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://gitlab.com/cynium/istota/-/compare/v0.7.0...main
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
