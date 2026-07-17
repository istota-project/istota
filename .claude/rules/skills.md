# Skills System

## Skills Loader (`src/istota/skills/_loader.py`)

### `SkillMeta` Dataclass (`src/istota/skills/_types.py`)
```python
@dataclass
class SkillMeta:
    name: str
    description: str
    always_include: bool = False
    admin_only: bool = False
    keywords: list[str] = field(default_factory=list)
    resource_types: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    companion_skills: list[str] = field(default_factory=list)
    exclude_skills: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    env_specs: list[EnvSpec] = field(default_factory=list)
    cli: bool = False
    experimental: bool = False  # Operator must enable skill_<name> in [experimental] features
    exclude_memory: bool = False
    exclude_persona: bool = False
    exclude_resources: list[str] = field(default_factory=list)
    skill_dir: str = ""
```

### Functions
```python
load_skill_index(skills_dir: Path, bundled_dir: Path | None = None) -> dict[str, SkillMeta]
    # Load skill.md frontmatter (toml fallback). bundled_dir overrides _BUNDLED_SKILLS_DIR (for tests).
select_skills(prompt, source_type, user_resource_types, skill_index,
              is_admin=True, attachments=None, disabled_skills=None,
              sticky_skills=None,
              enabled_experimental_features=frozenset()) -> list[str]
eligible_skill_names(skill_index, exclude, disabled_skills=None, is_admin=True,
                     enabled_experimental_features=frozenset()) -> list[str]
    # Shared membership gate for the menu catalogue: sorted names excluding
    # already-selected / always_include / disabled / admin-gated / experimental-gated / missing-deps.
    # NO resource gate. No bundled skill declares resource_types now; the former
    # holdouts (notes/spec/todos) were doc-only conventions with defaults and dropped
    # the field. Mechanism kept for future skills. Unchanged by the single-axis switch.
expand_companions(names, skill_index, *, is_admin=True, disabled_skills=None,
                  enabled_experimental_features=frozenset()) -> list[str]
    # Shared, gate-filtered, one-level companion resolver. Returns the companions
    # declared by `names` that pass the standard gates (not disabled / admin-gated /
    # experimental-gated / deps present), excluding any name already in `names`.
    # Companions-of-companions are NOT expanded (a cycle is inert). A declared
    # companion missing from the index is logged at WARNING and skipped. Used by BOTH
    # select_skills (eager companion expansion) and the `skills show` CLI (pull-time
    # companion expansion) so the gate filter can't drift between the two paths.
compute_skills_fingerprint(skills_dir: Path) -> str               # SHA-256, first 12 hex chars
load_skills_changelog(skills_dir: Path) -> str | None             # CHANGELOG.md
load_skills(skills_dir: Path, skill_names: list[str], bot_name, bot_dir, skill_index=None, bundled_dir=None) -> str
    # Concatenate skill docs (strips frontmatter)
build_disclosure_index(menu_names, skill_index) -> str            # "" when menu empty
```

### Single axis: eager body vs. menu entry

There is one axis, not two. A skill is either **eager** (full body in the
prompt, because a deterministic rule in `select_skills` picked it) or in the
**menu** (a one-line "load on demand" entry the model pulls in full via
`istota-skill skills show <name>`). The old eager/lazy "progressive disclosure"
machinery — `SkillMeta.disclosure`, `resolve_disclosure_mode`,
`partition_skills_for_disclosure`, the `SkillsConfig`
`progressive_disclosure` / `auto_lazy_threshold_chars` / `always_eager` knobs —
is **gone**. The `disclosure: lazy` frontmatter was stripped from every skill.
There is no "off" switch and no per-skill body-deferral flag; the menu is
intrinsic.

"Selected ⇒ eager; everything else eligible ⇒ menu." `select_skills` produces
the eager set; `eligible_skill_names` produces the menu (the full eligible
catalogue minus the eager set and its `exclude_skills`). The two are
complementary partitions of the loadable catalogue.

`always_include` (per-skill frontmatter) = `files`, `sensitive_actions`,
`memory`, `scripts`, `memory_search`, `kv`, `skills` — "always **select** me"
(so these are always eager). `skills` is in this set because deferring the
loader's own body would be circular: the model needs the loader instructions to
pull any skill, itself included.

### The menu catalogue (replaced Pass 2)

The "Available skills (load on demand)" prompt section is the **full eligible
catalogue**, not a narrowed guess. The executor computes
`menu = eligible_skill_names(skill_index, exclude = selected ∪ ⋃
exclude_skills_of_selected)` — every loadable skill the model isn't already
given eager — and renders it via `build_disclosure_index(menu, skill_index)`.
The capable main model self-selects what to load from the menu. This replaced
the removed per-task `claude -p` Pass-2 pre-router (its cold-start cost
dominated and timed out in production). `eligible_skill_names` (`_loader.py`) is
the shared membership gate (excludes already-selected / `always_include` /
disabled / admin-gated / experimental-gated / missing-deps). The executor logs
`skills: eager=N menu=M` per task.

The on-demand loader is the `skills` core skill (`always_include`, `cli: true`):
`istota-skill skills show <name>` renders a skill's full body (same frontmatter
strip + `{BOT_NAME}`/`{BOT_DIR}`/`{scripts_dir}`/`{user_id}` substitution as
`load_skills`), re-applying the disabled / `admin_only` / experimental /
missing-deps guards from the loaded config + `ISTOTA_USER_ID` so a pulled body
can't bypass them; unknown / disallowed → `{"status":"error",...}` + exit 1.
`istota-skill skills list` enumerates the loadable (guard-filtered) skills. (No
resource gate — the `resource_types` skills are doc-only conventions with
defaults, matching the catalogue.) It runs server-side via the skill proxy
(unsandboxed), so `load_config()` and the admins file are reachable.

**`skills show` appends companion bodies.** After rendering `<name>`'s body,
`show` resolves its companions via `expand_companions` and appends each
companion's rendered body under a delimiter `\n\n---\n<!-- companion: <comp>
-->\n\n<body>`. A gated-off / missing / unreadable companion instead appends
`<!-- companion <comp>: unavailable -->` and logs a WARNING — a missing safety
companion is a config error, never silently dropped. This is the safety-critical
guarantee: pulling an ingest skill from the menu (e.g. `browse`) also delivers
`untrusted_input` in the **same response**, so its inbound-handling guardrails
are never optional or at the model's discretion. `expand_companions` is the
shared resolver, so the menu-pull path applies the identical gate filter as
selection-time companion expansion.

### Skill Selection

**Deterministic matching** (`select_skills`) — fast, zero-cost. The only selection pass; the eager set falls out of it (the former LLM Pass 2 was removed — see below).

Filters applied to every candidate before any rule fires:
- `admin_only=True` skipped when `is_admin=False`
- `experimental=True` skipped unless `skill_<name>` is in `enabled_experimental_features` — the gate fires on the main loop, the sticky path, the companion pull-in (`expand_companions`), and the menu filter (`eligible_skill_names`) so an unenabled experimental skill cannot leak into selection or the menu via any path
- Unmet `dependencies` (missing Python packages) skipped via `_check_dependencies()`
- Names in `disabled_skills` (instance-level + per-user, merged) skipped

Eager selectors (priority order, with `continue` short-circuits):
1. `meta.always_include == True`
2. `source_type in meta.source_types`
3. Any `meta.file_types` match attachment extensions

Keyword (`triggers`/`keywords`) and `resource_types` matching are **no longer selectors** — every non-eager eligible skill is in the menu, so a keyword guess is redundant. The `triggers`/`keywords` frontmatter is **kept deliberately** (not removed): it's surfaced by the `!skills` command as documentation, but it does not drive selection. `resource_types` survives only as a menu-membership gate inside `eligible_skill_names`. (`prompt` / `user_resource_types` stay in the `select_skills` signature for call-site compatibility; they no longer drive selection.)

After the main loop:
4. **Sticky skills** — names supplied via `sticky_skills` are added eager (filtered by disabled/admin_only/deps). Always-include skills are not re-added.
5. **Companion skills** — companions of already-selected skills are pulled in eager via `expand_companions` (gate-filtered, one level), so e.g. `untrusted_input` rides along with a source/file/sticky-selected ingest skill.
6. **Exclude pass** — `meta.exclude_skills` of selected skills are removed from the final set (e.g., briefing excludes email).

**Sticky skills source** (`executor.py:1761-1789`): for `talk` and `email` tasks with a `conversation_token`, the executor populates `sticky_skills` from:
- `db.get_recent_conversation_skills(conversation_token, max_age_minutes=30, limit=2)` — skills from the last two tasks in the same conversation within the last 30 minutes
- `parent.selected_skills` from `db.get_reply_parent_task()` when `task.reply_to_talk_id` is set (no time limit)

After execution, the resolved skill set is persisted via `db.save_task_selected_skills()` so future tasks in the conversation can carry it forward.

**Pre-transcription**: before skill selection, `_pre_transcribe_attachments()` transcribes audio attachments and enriches `task.prompt` with the spoken text. Selection no longer keyword-matches the prompt, but the enriched prompt still flows into the menu-driven flow and is available to the model.

**Pass 2 (LLM semantic routing) was removed.** It ran a per-task `claude -p`
subprocess to pre-guess extra skills; the cold-start cost dominated and timed out
on every production task. The full-catalogue menu (above) replaces it — every
eligible skill is in the menu and the main model self-loads, no pre-router.
`classify_skills` / `build_skill_manifest` / the `semantic_routing*` config knobs
are gone; `eligible_skill_names` is the surviving shared gate.

Returns sorted list of skill names (the eager set).

**Selection observability**: `select_skills` emits a single INFO log per task with each eager skill annotated by the rule that fired (`pass1_selection count=N: foo(always_include), bar(source_type=briefing), …`); the executor emits `skills: eager=N menu=M`. Use these to reconcile selection misses against runtime proxy rejections (see executor.md).

### Skill Metadata (YAML frontmatter)
All metadata lives in YAML frontmatter at the top of each `skill.md` file:
- `name`, `triggers` (keyword list — `!skills` documentation only, not a selector), `description` (shown in the menu catalogue and `!skills`)
- `always_include`, `admin_only`, `cli` (booleans)
- `resource_types`, `source_types`, `file_types`, `companion_skills`, `exclude_skills`, `dependencies`, `exclude_resources` (lists)
- `exclude_memory`, `exclude_persona` (booleans)
- `env` (JSON-encoded array of env spec objects)

Operator overrides in `config/skills/` can still use `skill.toml` as a fallback.

### Skill Discovery (three layers, merged)
1. Bundled skill directories in `src/istota/skills/*/skill.md`
2. Operator override directories in `config/skills/*/` (skill.md or skill.toml)
3. Legacy `_index.toml` (lowest priority, deprecated)

## Skill Index (from skill.md frontmatter)

| Skill | always_include | keywords | resource_types | source_types |
|---|---|---|---|---|
| `files` | yes | — | — | — |
| `sensitive_actions` | yes | — | — | — |
| `memory` | yes | — | — | — |
| `scripts` | yes | — | — | — |
| `memory_search` | yes | — | — | — |
| `kv` | yes | — | — | — |
| `skills` | yes | — | — | — |
| `devbox` | — | devbox, install package, pip install, apt install, dig, nslookup, traceroute, whois, ping, nmap, ... | — | — |
| `email` | — | email, mail, send, inbox, reply, message | — | email |
| `calendar` | — | calendar, event, meeting, schedule, appointment, caldav | — | briefing |
| `todos` | — | todo, task, checklist, reminder, done, complete | — | — |
| `tasks` | — | subtask, queue, background, later | — | — | admin_only |
| `markets` | — | market, stock, stocks, ticker, index, indices, futures, ... | — | briefing |
| `reminders` | — | remind, reminder, remind me, alert me, notify me, don't forget, ... | — | — |
| `schedules` | — | schedule, recurring, cron, daily, weekly, ... | — | — |
| `nextcloud` | — | share, sharing, nextcloud, permission, access | — | — |
| `browse` | — | browse, website, scrape, screenshot, url, http, ... | — | — |
| `briefing` | — | — | — | briefing |
| `briefings_config` | — | briefing config, briefing schedule, ... | — | — |
| `heartbeat` | — | heartbeat, monitoring, health check, alert, ... | — | — |
| `transcribe` | — | transcribe, ocr, screenshot, scan, image, ... | — | — |
| `whisper` | — | transcribe, whisper, audio, voice, speech, dictation, ... | — | — |
| `notes` | — | note, save, write, markdown | — | — |
| `developer` | — | git, gitlab, repo, repository, commit, branch, MR, ... | — | — |
| `location` | — | location, gps, where, place, tracking, ... | — | — |
| `bookmarks` | — | bookmark, karakeep, save, read later, ... | — | — |
| `website` | — | website, site, publish, blog, ... | — | — |
| `feeds` | — | feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe, opml | — | — |
| `google_workspace` | — | google drive, google docs, google sheets, google calendar, google chat, google workspace, gmail, spreadsheet, gws | — | — |
| `money` | — | accounting, ledger, beancount, invoice, invoicing, expense, transaction, ... | — | — |
| `health` | — | health, weight, bloodwork, labs, biomarker, panel, blood pressure, ... | — | — |
| `untrusted_input` | — | — | — | — |

Note: `money` is the sole accounting skill. It runs in-process via the vendored `money` package (no subprocess, no HTTP).

**Module-shaped skills (`feeds`, `money`, `bookmarks`, `location`)** dropped their `resource_types` fields with Phase 1 of the modules / connected services refactor. They have no eager selector and live in the menu (pulled on demand); the credential / module gate enforced by the proxy + the in-process loader (`feeds.resolve_for_user`, `money.resolve_for_user`) decides whether the skill can actually do anything. The bookmarks `env` block reads both `KARAKEEP_BASE_URL` and `KARAKEEP_API_KEY` from the encrypted `secrets` table via the new `from: "secret"` env-spec source.

**No bundled skill declares `resource_types` anymore.** The last holdouts — the doc-only convention skills `notes`, `spec`, `todos` — dropped the field too: they're pure instruction docs with sensible defaults (`notes` writes to `{BOT_DIR}/notes/` when no `notes_folder` is declared; `spec`/`todos` similar). With keyword selection gone, none of them is eager unless source/file/sticky-selected — they live in the menu and the model pulls them on demand (`spec`'s ~7KB body in particular is a menu entry, never inlined eagerly). The `resource_types` gate survives only inside `eligible_skill_names` (menu membership) for any future resource-backed skill; no bundled skill exercises it.

`untrusted_input` is a doc-only companion skill — no triggers, no source_types, never selected directly. It loads via `companion_skills` declarations on the seven ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`) so its rules ride along whenever a task is processing content from outside the trust boundary — both when an ingest skill is selected eager (`select_skills` → `expand_companions`) and when one is pulled from the menu (`skills show` appends companion bodies via the same `expand_companions`). Paired with `sensitive_actions`: outbound rules in that one, inbound-reading rules here, per-action authorization principle stated in both.

## Skill CLI Modules (`src/istota/skills/`)

### `devbox/` - Persistent dev container
**Subcommands**: `exec <command> [--timeout N]`, `exec-file <path> [--interpreter X] [--timeout N]`, `cp-in <src> <dest>`, `cp-out <src> <dest>`, `status`, `reset --yes`
**Env vars**: `ISTOTA_USER_ID`, `ISTOTA_DEVBOX_CONTAINER` (default `devbox-<user_id>`), `ISTOTA_DEVBOX_DOCKER_CLI`, `ISTOTA_DEVBOX_DOCKER_SOCKET`, `ISTOTA_DEVBOX_EXEC_TIMEOUT`, `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES`
**Note**: Plain menu skill; **not** `always_include`. The `exclude_skills: [devbox]` exclusions on the seven ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`) are **gone** — co-selection with ingest tasks is safe now because the boundary moved off the socket. The Docker-API allowlist proxy (`src/istota/docker_proxy.py`) is bound into the sandbox at `/var/run/docker.sock` **unconditionally** (whenever `config.devbox.enabled and config.devbox.api_proxy_enabled` and the per-user proxy socket exists), with no `"devbox" in selected_skills` gate; the raw root-equivalent socket is never bound. So even an untrusted-content task that reaches the socket directly (`curl --unix-socket`) sees only the allowlist — exec/cp/inspect/restart on its own `devbox-<user_id>` container, and 403 on create/run/build/privileged/host-mount. The executor folds `devbox` into the effective `disabled_skills` when `config.devbox.enabled = False`, so it appears in neither eager nor menu. Container name is validated against `^[a-zA-Z0-9_.-]+$` before every `docker exec/cp/inspect/restart`. Each container carries a `com.istota.user_id=<user_id>` label and `_running()` verifies the label matches `ISTOTA_USER_ID` before any operation — defence-in-depth against stale containers from a prior tenant. `cp-in` / `cp-out` host paths are validated to stay under `ISTOTA_DEFERRED_DIR` or the user's `NEXTCLOUD_MOUNT_PATH` subtree (and rejects host-side symlinks). `args.command` is capped at 32 KB and refuses NUL bytes. Stdout/stderr capped at `max_output_bytes` per stream with a `[truncated: N more bytes]` marker. Image (`istota-devbox:latest`) built from `docker/devbox/Dockerfile`; production deploys via Ansible (one container per `istota_devbox_users` entry, isolated on `devbox-net` with `DOCKER-USER` iptables drops for `169.254.169.254/32` + RFC1918). The former residual trade-off (anything with the raw socket bound could launch a privileged host-mounting container) is **resolved** by the proxy: the socket inside the sandbox is the allowlist, and container creation is refused outright, so root-in-an-unprivileged-no-host-mount container is not host root.

**Docker-API allowlist proxy** (`src/istota/docker_proxy.py`): per-user asyncio reverse proxy in front of the host Docker socket, safe to bind into the sandbox unconditionally. `DockerApiProxy` listens on `{config.devbox.api_proxy_socket_dir}/{user_id}.sock` and forwards a tightly-scoped allowlist against the user's own `devbox-<user_id>` container; the pure `classify_request(method, path, body, *, container_name, tracked_exec_ids) -> (allowed, reason)` is the decision core. Allowed: `GET /_ping|/version`, `GET /containers/json`, `GET /containers/{name}/json`, exec-create `POST /containers/{name}/exec` (owned; body must not set `Privileged`/`HostConfig`), exec-start `POST /exec/{id}/start` + exec-inspect `GET /exec/{id}/json` (tracked id only), cp `HEAD|GET|PUT /containers/{name}/archive`, `POST /containers/{name}/restart` — all scoped to the owned container. Everything else → 403. exec-create is the one fully-mediated op (parse request body for the privilege check, parse the response body for the issued exec `Id`); all other allowed ops splice the client socket full-duplex to the real socket without interpreting the stream. exec-ids are tracked (evicted on start, TTL-swept by `api_proxy_exec_ttl_seconds`). Audit logger `istota.docker_proxy.audit` emits one `docker_proxy user=… method=… path=… result=… reason=… dur_ms=…` line per request; optional file fan-out via `config.devbox.api_proxy_audit_log`. Daemon entry point `python -m istota.docker_proxy --user <id>`. Ansible: `istota-docker-proxy@.service.j2` systemd instance unit + `istota-docker-proxy.tmpfiles.j2` + `istota_devbox_api_proxy_enabled` / `istota_docker_proxy_socket_dir` defaults; `config.toml.j2` maps `[devbox] api_proxy_*`.

**Credential proxy** (`src/istota/devbox_proxy.py` + `docker/devbox/scripts/*` + `docker/devbox/lib/istota_devbox_client.py`): per-user asyncio daemon on the host, listens on `/var/run/{namespace}/<user>/sock` (mode 0o660, owned by `istota:istota`). The compose template bind-mounts the per-user directory `/var/run/{namespace}/<user>/` into the container at `/run/istota-cred/` so daemon restarts can unlink + recreate the socket inode without stranding the container against a dead bind-mount target. The container's `dev` user gains access via the compose `group_add:` entry that grants the host's `istota` gid as a supplementary group; the per-user directory also enforces cross-tenant isolation (container alice's bind mount contains only alice's socket). Container-side shims (`git-credential-istota`, `gitlab-api`, `github-api`, `gh`, `glab`) frame JSON requests over the socket; the daemon injects GitHub/GitLab tokens server-side. Tokens never enter the container's env or filesystem. Protocol in `devbox_proxy_protocol.py` — single-line JSON, 16 MiB cap, structured error envelope with stable `ERR_*` codes (`no_token`, `not_allowed`, `upstream_error`, `bad_request`, `unknown_action`, `internal`). Allowlist enforcement reuses `developer.{gitlab,github}_api_allowlist`. Audit logger `istota.devbox_proxy.audit` emits one key-value line per action (`user=, action=, result=, dur_ms=, method=, endpoint=, status=`) to the journal, plus an optional file fan-out via `developer.devbox_proxy_audit_log`. Cross-host `git_credential get` attempts (e.g. `bitbucket.org`) emit a `result=no_token` audit line — the only signal we have that the agent reached for a third-party host. The daemon starts cleanly with no tokens; per-action `no_token` errors are the normal mode for partial-provider configurations. Systemd instance template at `deploy/ansible/templates/istota-devbox-proxy@.service.j2`; deployed as `{namespace}-devbox-proxy@<user>.service`, one instance per `istota_devbox_users` entry. Tmpfiles snippet creates the socket directory at boot. Compose template's per-user `volumes:` entry pins the socket into each container, gated on `istota_devbox_proxy_enabled` (default true when devbox is on). The Dockerfile-checksum task in `tasks/main.yml` was generalized to hash the whole `docker/devbox/{Dockerfile,lib,scripts,etc}` tree so any shim edit triggers an image rebuild via the existing `restart istota-devbox` handler.

### `kv/` - Key-Value Store
**Subcommands**: `get NAMESPACE KEY`, `set NAMESPACE KEY '<json>'`, `list NAMESPACE`, `delete NAMESPACE KEY`, `namespaces`, `set-contains NS KEY MEMBER`, `set-size NS KEY`, `set-members NS KEY [--limit N] [--offset N]`, `set-add NS KEY MEMBER [MEMBER...]`, `set-remove NS KEY MEMBER [MEMBER...]`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_DEFERRED_DIR`, `ISTOTA_TASK_ID`
**Note**: `always_include` core skill. Persistent per-user, namespaced JSON store. Writes go through deferred-DB pattern under sandbox. Set ops (`set-add`/`set-remove`/`set-contains`/`set-size`/`set-members`) operate on a JSON-array value at `<ns>/<key>` with plain-string members — added so membership-tracking patterns (seen IDs, processed hashes) don't have to round-trip the full array through `get`. Deferred `set-add`/`set-remove` carry only the member list; the scheduler re-reads the current value at apply time so concurrent ops compose correctly.

### `email/` - IMAP/SMTP (two-way client)
**Read subcommands**: `list` (+`--since`/`--from`/`--unread`, snippet + has_attachments), `read` (headers, plain **and** html, attachment manifest), `search` (raw IMAP SEARCH string, verbatim — errors, never silent subject-match), `thread` (real References/In-Reply-To walk), `attachments <id> --dest`, `from-senders --senders` (server-side SEARCH, no 100-truncation — the digest/batching path), `newsletters --sources` (required). Every read verb takes `--scope {mine,shared,all}` (default `all`).
**Write subcommands**: `send` (+`--cc`/`--bcc`/`--attach`(repeatable)/`--reply-to`; Bcc never transmitted), `reply`/`reply-all <id>` (threaded from a fetched message), `mark <id> {read,unread,flagged}` + `delete <id>` (destructive — refuse without `--confirmed`), `output` (deferred structured reply).
**Read scoping**: shared `istota.email_ownership` module resolves who owns an inbound message (plus-address → sender-match → thread-match); the inbound poll (`transport/email/inbound.py`) and the read-scope filter agree exactly, so an unscoped read can't leak one user's mail to another. `shared`/`all` fail closed if the framework DB (thread arm) is unavailable. `--scope mine` pushes `TO bot+<user>@ OR FROM <addrs>` down to the server. Fetched bodies/snippets are wrapped in an untrusted-content delimiter; the whole payload carries an `untrusted: true` notice.
**Env vars**: `IMAP_HOST/PORT/USER/PASSWORD`, `IMAP_TIMEOUT`, `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM`, `ISTOTA_USER_ID`, `ISTOTA_DB_PATH`, `ISTOTA_TASK_ID`, `ISTOTA_DEFERRED_DIR`. Read verbs `load_config()` (via `ISTOTA_CONFIG_PATH`) for the user table + DB (scoping); IMAP/SMTP creds come from the proxy-injected env.
**Key fns**: `list_emails()`, `read_email()`, `fetch_emails_full()`, `send_email()`, `reply_to_email()`, `mark_email()`, `search_emails()`, `get_newsletters()`, `delete_email()`, `cmd_output()`; `email_ownership.resolve_email_owner/owner_in_scope`.

### `calendar/` - CalDAV
**Subcommands**: `list` (`--date`, `--week`), `create`, `update` (`--clear-location`, `--clear-description`), `delete`
**Env vars**: `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
**Key fns**: `get_caldav_client()`, `get_calendars_for_user()`, `get_events()`, `get_event_by_uid()`, `create_event()`, `update_event()`, `delete_event()`

### `markets/` - Market Data CLI
**Subcommands**: `quote`, `summary`, `finviz`
**Env vars**: `BROWSER_API_URL` (finviz only)
**Key fns**: `get_quotes()`, `get_futures_quotes()`, `get_index_quotes()`, `format_market_summary()`, `fetch_finviz_data()`, `format_finviz_briefing()`

### `browse/` - Headless Browser
**Subcommands**: `get`, `screenshot`, `extract`, `interact`, `close`
**Env vars**: `BROWSER_API_URL`

### `transcribe/` - OCR
**Subcommands**: `ocr`
**Env vars**: None
**Deps**: `pytesseract`, `PIL`

### `memory_search/` - Memory Search CLI
**Subcommands**: `search`, `index` (sub: `conversation`, `file`), `reindex`, `stats`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `NEXTCLOUD_MOUNT_PATH`, `ISTOTA_CONVERSATION_TOKEN`

### `whisper/` - Audio Transcription (package)
**Subcommands**: `transcribe`, `models`, `download`
**Env vars**: None (reads audio files from paths accessible via mount)
**Key fns**: `transcribe_audio()`, `select_model()`, `format_srt()`, `format_vtt()`
**Optional deps**: `faster-whisper>=1.1.0`, `psutil>=5.9.0` (in `whisper` extra group)

### `nextcloud/` - Nextcloud Sharing CLI
**Subcommands**: `share list` (`--path`), `share create` (`--path`, `--type user|link|email`, `--permissions`), `share delete SHARE_ID`, `share search QUERY`
**Env vars**: `NC_URL`, `NC_USER`, `NC_PASS`
**Key fns**: Uses `nextcloud_client.py` (OCS + WebDAV)

### `location/` - GPS Location + Calendar Attendance
**Subcommands**: `current`, `history`, `places`, `learn`, `update`, `delete`, `attendance`, `reverse-geocode`, `day-summary`, `discover`, `dismiss-cluster`, `list-dismissed`, `restore-dismissed`, `place-stats`, `import-garmin-tracks`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
**Optional deps**: `caldav` (in `calendar` extra group)
**Shared logic**: cluster discovery, dismiss-zone management, and per-place visit stats live in `istota.location_logic` (pure SQL + `geo.haversine`). Both the FastAPI web routes and this skill import the same `_location_*` helpers — the web UI's "discovered clusters", "dismissed clusters", and place-detail visit stats are now reachable from CLI parity.
**`import-garmin-tracks`**: imports Garmin watch GPS tracks into `location.db` via the shared `istota.location.garmin_import.import_tracks` (also driving the web "Import GPS tracks" button and the cron script). Direct/delegated split like health `garmin-sync`: with `ISTOTA_SECRET_KEY` in env it runs inline; sandboxed it writes a `task_<id>_garmin_import.json` deferred op that `scheduler_deferred._process_deferred_garmin_import` runs in-process post-task (where the key lives) and notifies the user. Unlike `garmin-sync`'s enqueue path, the deferred-op path works from the sandbox — `location.db` is the user's writable workspace; only the token-decrypt key is stripped.

### `bookmarks/` - Karakeep Bookmark Management
**Subcommands**: `search`, `list`, `get`, `add`, `tags`, `tag`, `untag`, `lists`, `list-bookmarks`, `summarize`, `stats`, `highlights`
**Env vars**: `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY`
**Note**: `highlights [--bookmark ID] [--limit N]` reads Karakeep highlights (read-only; `--limit` defaults to `0` = all). `_paginate` injects `includeContent=False` only for the bookmarks key, so the tags/highlights endpoints never receive it.

### `feeds/` - Native RSS / Atom / Tumblr / Are.na (in-process)
**Subcommands**: `list`, `categories`, `entries`, `add`, `remove`, `refresh`, `poll`, `run-scheduled`, `import-opml`, `export-opml`, `star`, `starred`, `mark-read`
**Env vars**: `FEEDS_USER` (set by executor); `TUMBLR_API_KEY` optional fallback
**Note**: In-process facade — resolves the user's `FeedsContext` via `istota.feeds.resolve_for_user` and invokes `istota.feeds.cli` through `CliRunner`. No subprocess, no HTTP. The `feeds.toml` round-trip is gone (commit 24b5f3a) — per-user SQLite at `{workspace}/feeds/data/feeds.db` is the sole source of truth (subscriptions, categories, entries, read state, plus the global default poll interval in `schema_meta`). Pre-existing `feeds.toml` files are auto-imported on first touch by `istota.feeds._migrate.migrate_legacy_toml` (idempotent, gated on a `schema_meta` sentinel) and then ignored. Scheduler auto-seeds `_module.feeds.run_scheduled` (`*/5 * * * *`, `skip_log_channel=1`) for users where `Config.is_module_enabled(user_id, "feeds")` is True; rows are deleted when the module is opted out via `disabled_modules`. Same pattern for `_module.money.run_scheduled` (`0 8 * * *`).

### `google_workspace/` - Google Workspace CLI Passthrough
**Subcommands**: Passes all arguments through to `gws` binary (Drive, Gmail, Calendar, Sheets, Docs, Chat)
**Env vars**: `GOOGLE_WORKSPACE_CLI_TOKEN` (injected via `setup_env` hook from DB OAuth tokens), `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` (writable cache dir)
**Note**: CLI wrapper around the standalone `gws` binary. Credentials injected via skill proxy. OAuth tokens stored in `google_oauth_tokens` DB table, refreshed automatically. Scopes configurable via `[google_workspace]` config section (default: read-only).

### `money/` - Accounting (in-process)
**Subcommands**: `list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `add-transaction`, `edit-transaction`, `backfill-ids`, `sync-monarch`, `debug-monarch`, `import-csv`, `invoice` (sub: `generate`, `list`, `paid`, `create`, `void`), `work` (sub: `list`, `add`, `update`, `remove`)
**Env vars**: `MONEY_USER` (the istota user_id; config resolved from the per-user money DB via `resolve_for_user`)
**Note**: In-process facade — resolves the user's `UserContext` via `istota.money.resolve_for_user` and invokes the `istota.money` Click CLI via `CliRunner` with the `Context` injected. No subprocess, no HTTP. Money is fully istota-native: **no standalone `money` binary**, no `MONEY_CONFIG`/`config.toml`/`load_context`, and no TOML config-read fallback — config (invoicing/monarch/tax) lives only in the per-user money DB (`config_store`), seeded from legacy TOML once by `_migrate` on first touch. The same operations are operator-reachable as `istota money <op> …` (`cli_money.py`): the CLI resolves the user the istota way (`-u USER` → DB) and forwards to the same Click tree. argparse can't capture a leading option through `REMAINDER`, so `main()` peels `money <operational-cmd>` off before `parse_args` and routes via `cli_money.dispatch_operational`; config-management commands (`config|client|company|service|tax|monarch`) stay native argparse. `lots` and `wash-sales` are `@requires_feature`-gated (`money_tax` / `money_wash_sales`); gated-off calls return the standard error envelope. Transactions carry a stable `id:` metadata line (backfilled once via `backfill-ids`, auto-run from `ensure_initialised`; stamped by every writer, plus `monarch-id:` on synced entries). `edit-transaction` locates by id and rewrites the directive in place under a ledger flock with `bean-check` + rollback (`core/edit.py`); `edited:`-marked entries are left alone by Monarch sync's reconciler.

### `health/` - Body Stats, Bloodwork, Biomarker Trends
**Subcommands**: `log`, `stats`, `latest`, `panels`, `panel`, `add-panel`, `add-biomarker`, `trend`, `upload`, `summary`, `settings`, `set`, plus `garmin-status` / `garmin-sync` / `garmin-disconnect`.
**Env vars**: `HEALTH_DB_PATH` (injected via `setup_env` hook from `istota.health.resolve_for_user(user_id, config).db_path`); the OCR/explainer paths additionally use the active brain for structured extraction.
**Note**: Standard module — on by default; per-user opt-out via `disabled_modules`. All values stored metric. Writes flow through deferred ops (`task_<id>_health_ops.json`) under sandbox; `scheduler_deferred._process_deferred_health_ops` replays them post-task. The web UI ships pre-written explainer payloads in the mock API for development.

**`garmin-sync` direct/delegated routing.** Garmin OAuth tokens live in the encrypted secrets table; the engine decrypts and re-encrypts rotated tokens mid-run. Subprocess callers don't have `ISTOTA_SECRET_KEY` by design, so `cmd_garmin_sync` checks `secrets_store.secret_key_available()` and dispatches: **direct** (operator shell with the EnvironmentFile sourced) runs the engine inline; **delegated** (sandboxed LLM Bash, hand-written CRON `command:` rows, dev shells without the env file) enqueues a `skill="health"` task with `max_attempts=1` and polls every 0.5s up to 60s, then surfaces the engine's JSON payload. The scheduler's `_run_garmin_sync_inprocess` short-circuit (see scheduler.md) makes the delegated path execute on the daemon thread where the key lives. Sandboxed callers see `sqlite3.OperationalError` on the enqueue (bwrap mounts the DB read-only) → fail-loud with `/garmin/sync` hint. Project note `Skill proxy execution model and the master-key boundary.md` covers why this isn't auto-injected via the skill proxy.

### Module-skill facade exit-code contract

The feeds and money skill facades (`src/istota/skills/feeds/__init__.py`, `src/istota/skills/money/__init__.py`) emit `{"status":"error","error":"…"}` envelopes from `_output()` whenever `_run()` catches an error (`UserNotFoundError`, missing env, exception, non-zero CliRunner exit, JSON decode failure). `_output()` calls `sys.exit(1)` when it sees an error envelope, so the subprocess returncode reflects reality. The scheduler's `_execute_command_task()` also detects the envelope shape on stdout as a defense-in-depth fallback (see `.claude/rules/scheduler.md`). New module-skill facades must follow this convention.

### Library-Only Modules (no CLI)
- `files/` - Nextcloud file ops (mount-aware, rclone fallback)
- `markets/finviz.py` - FinViz scraping for market data (internal helper for `markets`)

## How to Add a New Skill

### 1. Create the skill directory
Create `src/istota/skills/<name>/` with:
- `skill.md` — YAML frontmatter for metadata + markdown body for instructions (required)

### 2. Define metadata in `skill.md` frontmatter
```yaml
---
name: my_skill
triggers: [trigger, words]
description: What it does
resource_types: [my_resource]
source_types: [briefing]
cli: true
dependencies: [some-package]
env: [{"var":"MY_VAR","from":"user_resource_config","resource_type":"my_resource","field":"path"}]
---

# My Skill

Instructions for Claude follow here...
```

### 3. (Optional) Create CLI module
Create `src/istota/skills/<name>/__init__.py` (plus `__main__.py` for `python -m` support):
```python
import argparse, json, sys

def build_parser():
    parser = argparse.ArgumentParser(description="My skill")
    sub = parser.add_subparsers(dest="command")
    cmd = sub.add_parser("my-command")
    cmd.add_argument("--flag")
    return parser

def cmd_my_command(args):
    result = {"status": "ok"}
    print(json.dumps(result))

def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "my-command":
        cmd_my_command(args)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### 4. (Optional) Add env vars in executor.py
In `execute_task()` L643-725, add env var mapping for the new resource type:
```python
# After existing resource mappings
my_resources = [r for r in user_resources if r.resource_type == "my_resource"]
if my_resources:
    env["MY_RESOURCE_PATH"] = str(config.nextcloud_mount_path / my_resources[0].resource_path.lstrip("/"))
```

### 5. (Optional) Add resource type
- Add to `ResourceConfig.type` validation (if any)
- Document in skill md file
- Users add via `uv run istota resource add -u USER -t my_resource -p /path`
