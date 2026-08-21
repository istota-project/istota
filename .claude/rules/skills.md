---
paths:
  - "src/istota/skills/**"
---

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
    requires_capability: list[str] = field(default_factory=list)  # backing-service gate (browser/devbox)
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
strip + `{BOT_NAME}`/`{BOT_DIR}`/`{scripts_dir}`/`{user_id}`/`{workspace}`/`{storage}`
substitution as `load_skills`; `{workspace}` resolves to the base user root
(`/Users/{user}`) and `{storage}` to `config.storage_label` — storage-neutral
prose so a pulled body doesn't hardcode Nextcloud framing, via a `_workspace_dir`
helper), re-applying the disabled / `admin_only` / experimental /
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

**Capability gate.** `disabled_skills` passed to selection is the *effective* set from `skills._loader.effective_disabled_skills(config, user_id, skill_index)` — instance + per-user disabled, unioned with `capability_disabled_skills(...)`: any skill whose `requires_capability` list isn't a subset of `Config.available_capabilities()`. That map is the single home wiring a capability name to its config flag (`browser`→`config.browser.enabled`, `devbox`→`config.devbox.enabled`), both off by default (notably in the standalone install, which deploys neither the headless browser nor the devbox container). So `browse` (`requires_capability: [browser]`) and `devbox` (`requires_capability: [devbox]`) drop from eager selection **and** the on-demand menu when their service is undeployed, with no wasted menu pull or confusing CLI failure. The same helper is called by the executor, the `skills` CLI (`skills list`/`show`), and the `!skills` command, so all three agree. Adding a new service-backed skill = declare `requires_capability` in its frontmatter + one line in `available_capabilities()`; this replaced the old hardcoded `if not config.devbox.enabled` fold that had to be mirrored across the executor and the CLI.

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
- `resource_types`, `source_types`, `file_types`, `companion_skills`, `exclude_skills`, `dependencies` (lists)
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
| `tasks` (admin_only, cli) | — | subtask, queue, background, later, task status | — | — |
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
| `commit` | — | commit, commit message, changelog, git commit, staging | — | — |
| `code_review` (admin_only, cli) | — | review, code review, review the diff, review my changes, ... | — | — |
| `location` | — | location, gps, where, place, tracking, ... | — | — |
| `bookmarks` | — | bookmark, karakeep, save, read later, ... | — | — |
| `feeds` | — | feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe, opml | — | — |
| `google_workspace` | — | google drive, google docs, google sheets, google calendar, google chat, google workspace, gmail, spreadsheet, gws | — | — |
| `money` | — | accounting, ledger, beancount, invoice, invoicing, expense, transaction, ... | — | — |
| `health` | — | health, weight, bloodwork, labs, biomarker, panel, blood pressure, ... | — | — |
| `untrusted_input` | — | — | — | — |

Note: `money` is the sole accounting skill. It runs in-process via the vendored `money` package (no subprocess, no HTTP).

**Module-shaped skills (`feeds`, `money`, `bookmarks`, `location`)** dropped their `resource_types` fields with Phase 1 of the modules / connected services refactor. They have no eager selector and live in the menu (pulled on demand); the credential / module gate enforced by the proxy + the in-process loader (`feeds.resolve_for_user`, `money.resolve_for_user`) decides whether the skill can actually do anything. The bookmarks `env` block reads both `KARAKEEP_BASE_URL` and `KARAKEEP_API_KEY` from the encrypted `secrets` table via the new `from: "secret"` env-spec source.

**No bundled skill declares `resource_types` anymore.** The last holdouts — the doc-only convention skills `notes`, `spec`, `todos` — dropped the field too: they're pure instruction docs with sensible defaults (`notes` writes to `{BOT_DIR}/notes/` when no `notes_folder` is declared; `spec`/`todos` similar). With keyword selection gone, none of them is eager unless source/file/sticky-selected — they live in the menu and the model pulls them on demand (`spec`'s ~7KB body in particular is a menu entry, never inlined eagerly). The `resource_types` gate survives only inside `eligible_skill_names` (menu membership) for any future resource-backed skill; no bundled skill exercises it.

**`developer` declares companions, and the split is the point.** The workflow is three documents rather than one: `developer` (the spine — repository layout, the job lifecycle, change tiers, the verification budget, the abort path, the report shape), `commit` (message format, what lands alongside a commit, the private-data scrub table) and `code_review` (running a review, reading the envelope, what to do with each severity). `developer` declares `companion_skills: [commit, code_review, untrusted_input]`, so pulling it from the menu delivers all three bodies in one response — the same guarantee that rides `untrusted_input` along with `browse`. Each document has one subject and a life outside the workflow: "review this diff" pulls `code_review` alone, "commit what I changed" pulls `commit` alone. The three rendered bodies are held under a 715-line budget, enforced by `TestLoadBudget`. It was 700 until ISSUE-264 added the rule that a test runner's exit status is not readable through a pipe; five recipe fixes in one week (ISSUE-264, -267, -268, -269, -270) is the signal that the figure was set below what the recipes cost, not that any one fix was too expensive. Raise it here before raising it in the test, never the other way round.

`developer` declares `untrusted_input` **directly** even though `code_review` declares it too, and that duplication is load-bearing. `expand_companions` is one level (`_loader.py:405`), so pulling `developer` resolves *developer's* companions and stops — `code_review`'s own companion is never reached on the path the happy path actually takes. Two declarations cost nothing (`expand_companions` de-duplicates against `seen`); the alternative is a safety companion declared everywhere and delivered nowhere. `code_review/skill.md` also states the rule in full itself rather than deferring to a document that may not arrive with it, since review findings are model text about a diff that may be an outside contributor's.

`untrusted_input` is a doc-only companion skill — no triggers, no source_types, never selected directly. It loads via `companion_skills` declarations on the ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`, `briefings`, `nextcloud`, `tasks`) and on the two developer-workflow skills that read model output about outside content (`developer`, `code_review`) so its rules ride along whenever a task is processing content from outside the trust boundary — both when an ingest skill is selected eager (`select_skills` → `expand_companions`) and when one is pulled from the menu (`skills show` appends companion bodies via the same `expand_companions`). Paired with `sensitive_actions`: outbound rules in that one, inbound-reading rules here, per-action authorization principle stated in both.

## Skill CLI Modules (`src/istota/skills/`)

### `devbox/` - Persistent dev container
**Subcommands**: `exec <command> [--timeout N]`, `exec-file <path> [--interpreter X] [--timeout N]`, `cp-in <src> <dest>`, `cp-out <src> <dest>`, `status`, `reset --yes`
**Env vars**: `ISTOTA_USER_ID`, `ISTOTA_DEVBOX_CONTAINER` (default `devbox-<user_id>`), `ISTOTA_DEVBOX_DOCKER_CLI`, `ISTOTA_DEVBOX_DOCKER_SOCKET`, `ISTOTA_DEVBOX_EXEC_TIMEOUT`, `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES`
**Note**: Plain menu skill; **not** `always_include`. The `exclude_skills: [devbox]` exclusions on the seven ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`) are **gone** — co-selection with ingest tasks is safe now because the boundary moved off the socket. The Docker-API allowlist proxy (`src/istota/docker_proxy.py`) is bound into the sandbox at `/var/run/docker.sock` **unconditionally** (whenever `config.devbox.enabled and config.devbox.api_proxy_enabled` and the per-user proxy socket exists), with no `"devbox" in selected_skills` gate; the raw root-equivalent socket is never bound. So even an untrusted-content task that reaches the socket directly (`curl --unix-socket`) sees only the allowlist — exec/cp/inspect/restart on its own `devbox-<user_id>` container, and 403 on create/run/build/privileged/host-mount. The `devbox` skill declares `requires_capability: [devbox]`, so the shared capability gate (`skills._loader.effective_disabled_skills` + `Config.available_capabilities()`) folds it into the effective `disabled_skills` when `config.devbox.enabled = False` — it appears in neither eager nor menu (and shows as disabled in `!skills`). This is the general "a skill whose backing service isn't deployed is dropped" mechanism (see "Capability gate" below), not a devbox special-case; `browse` uses the same field keyed on `config.browser.enabled`. Container name is validated against `^[a-zA-Z0-9_.-]+$` before every `docker exec/cp/inspect/restart`. Each container carries a `com.istota.user_id=<user_id>` label and `_running()` verifies the label matches `ISTOTA_USER_ID` before any operation — defence-in-depth against stale containers from a prior tenant. `cp-in` / `cp-out` host paths go through the shared allowlist in `src/istota/skill_host_paths.py` (see the `kv --value-file` note below for the rule and why it exists): `$ISTOTA_DEFERRED_DIR`, `{mount}/Users/{ISTOTA_USER_ID}`, the task's own `{mount}/Channels/{ISTOTA_CONVERSATION_TOKEN}`, and `{mount}/Talk` for reads only. devbox held the only copy of that check before, and it took `NEXTCLOUD_MOUNT_PATH` whole — the shared mount root for every user, so the allowlist admitted every other user's workspace. Host-side symlinks are rejected, callers operate on the resolved path the validator hands back rather than reopening the one they passed in, and a `cp-out` destination is checked before its parent directories are created. `args.command` is capped at 32 KB and refuses NUL bytes. Stdout/stderr capped at `max_output_bytes` per stream with a `[truncated: N more bytes]` marker. Image (`istota-devbox:latest`) built from `docker/devbox/Dockerfile`; production deploys via Ansible (one container per `istota_devbox_users` entry, isolated on `devbox-net` with `DOCKER-USER` iptables drops for `169.254.169.254/32` + RFC1918). The former residual trade-off (anything with the raw socket bound could launch a privileged host-mounting container) is **resolved** by the proxy: the socket inside the sandbox is the allowlist, and container creation is refused outright, so root-in-an-unprivileged-no-host-mount container is not host root.

**Docker-API allowlist proxy** (`src/istota/docker_proxy.py`): per-user asyncio reverse proxy in front of the host Docker socket, safe to bind into the sandbox unconditionally. `DockerApiProxy` listens on `{config.devbox.api_proxy_socket_dir}/{user_id}.sock` and forwards a tightly-scoped allowlist against the user's own `devbox-<user_id>` container; the pure `classify_request(method, path, body, *, container_name, tracked_exec_ids) -> (allowed, reason)` is the decision core. Allowed: `GET /_ping|/version`, `GET /containers/json`, `GET /containers/{name}/json`, exec-create `POST /containers/{name}/exec` (owned; body must not set `Privileged`/`HostConfig`), exec-start `POST /exec/{id}/start` + exec-inspect `GET /exec/{id}/json` (tracked id only), cp `HEAD|GET|PUT /containers/{name}/archive`, `POST /containers/{name}/restart` — all scoped to the owned container. Everything else → 403. exec-create is the one fully-mediated op (parse request body for the privilege check, parse the response body for the issued exec `Id`); all other allowed ops splice the client socket full-duplex to the real socket without interpreting the stream. exec-ids are tracked (evicted on start, TTL-swept by `api_proxy_exec_ttl_seconds`). Audit logger `istota.docker_proxy.audit` emits one `docker_proxy user=… method=… path=… result=… reason=… dur_ms=…` line per request; optional file fan-out via `config.devbox.api_proxy_audit_log`. Daemon entry point `python -m istota.docker_proxy --user <id>`. Ansible: `istota-docker-proxy@.service.j2` systemd instance unit + `istota-docker-proxy.tmpfiles.j2` + `istota_devbox_api_proxy_enabled` / `istota_docker_proxy_socket_dir` defaults; `config.toml.j2` maps `[devbox] api_proxy_*`.

**Credential proxy** (`src/istota/devbox_proxy.py` + `docker/devbox/scripts/*` + `docker/devbox/lib/{istota_devbox_client,istota_forge_cli}.py`): per-user asyncio daemon on the host, listens on `/var/run/{namespace}/<user>/sock` (mode 0o660, owned by `istota:istota`). The compose template bind-mounts the per-user directory `/var/run/{namespace}/<user>/` into the container at `/run/istota-cred/` so daemon restarts can unlink + recreate the socket inode without stranding the container against a dead bind-mount target. The container's `dev` user gains access via the compose `group_add:` entry that grants the host's `istota` gid as a supplementary group; the per-user directory also enforces cross-tenant isolation (container alice's bind mount contains only alice's socket). Three actions: `ping`, `git_credential` and `forge_token`. `git-credential-istota` frames `git_credential` over the socket and the daemon injects the token server-side, so git never holds one; `ping` is a host-side liveness check with no in-container client. `forge_token` hands the token to the container's `gh`/`glab` wrapper (`docker/devbox/lib/istota_forge_cli.py`, a byte-identical copy of `src/istota/forge_cli.py`), which needs it in the real CLI's own environment — so the forge token *does* enter the container, as it already did on every `git push`. The configured forge URL rides along with it, because one image serves every user and cannot bake one in. `forge_token` replaced the `gitlab_api` / `github_api` actions and the `developer.{gitlab,github}_api_allowlist` fields they enforced: an endpoint allowlist cannot describe what a real `gh` invocation does (`gh pr create` is several calls, `gh pr checks` paginates), so the deny list moved into the wrapper's argv policy. The daemon now makes no outbound requests at all. Protocol in `devbox_proxy_protocol.py` — single-line JSON, 16 MiB cap, structured error envelope with stable `ERR_*` codes (`no_token`, `unknown_provider`, `bad_request`, `unknown_action`, `internal`). Audit logger `istota.devbox_proxy.audit` emits one key-value line per action (`user=, action=, result=, dur_ms=, op=, host=, provider=`) to the journal, plus an optional file fan-out via `developer.devbox_proxy_audit_log`; caller-controlled values are quoted, escaped and truncated by `_audit_value`, because a newline in one forges a whole second line. Cross-host `git_credential get` attempts (e.g. `bitbucket.org`) emit a `result=no_token` audit line — the only signal we have that the agent reached for a third-party host. The daemon starts cleanly with no tokens; per-action `no_token` errors are the normal mode for partial-provider configurations. Systemd instance template at `deploy/ansible/templates/istota-devbox-proxy@.service.j2`; deployed as `{namespace}-devbox-proxy@<user>.service`, one instance per `istota_devbox_users` entry. Tmpfiles snippet creates the socket directory at boot. Compose template's per-user `volumes:` entry pins the socket into each container, gated on `istota_devbox_proxy_enabled` (default true when devbox is on). The Dockerfile-checksum task in `tasks/main.yml` was generalized to hash the whole `docker/devbox/{Dockerfile,lib,scripts,etc}` tree so any shim edit triggers an image rebuild via the existing `restart istota-devbox` handler.

### `kv/` - Key-Value Store
**Subcommands**: `get NAMESPACE KEY`, `set NAMESPACE KEY ('<json>' | --value-file PATH)`, `list NAMESPACE [--keys-only] [--max-value-chars N]`, `delete NAMESPACE KEY`, `namespaces`, `set-contains NS KEY MEMBER [MEMBER...]`, `set-size NS KEY`, `set-members NS KEY [--limit N] [--offset N]`, `set-add NS KEY MEMBER [MEMBER...]`, `set-remove NS KEY MEMBER [MEMBER...]`, `set-trim NS KEY --keep-newest N`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_DEFERRED_DIR`, `ISTOTA_TASK_ID`, `NEXTCLOUD_MOUNT_PATH`, `ISTOTA_CONVERSATION_TOKEN` (`ISTOTA_DEFERRED_DIR` + `NEXTCLOUD_MOUNT_PATH`/`ISTOTA_USER_ID`/`ISTOTA_CONVERSATION_TOKEN` are what bound `--value-file`)
**Note**: `always_include` core skill. Persistent per-user, namespaced JSON store. Writes go through deferred-DB pattern under sandbox. Set ops (`set-add`/`set-remove`/`set-trim`/`set-contains`/`set-size`/`set-members`) operate on a JSON-array value at `<ns>/<key>` with plain-string members — added so membership-tracking patterns (seen IDs, processed hashes) don't have to round-trip the full array through `get`. Deferred `set-add`/`set-remove` carry only the member list and `set-trim` only the count; the scheduler re-reads the current value at apply time so concurrent ops compose correctly.

**The store has no size limit; a command argument does** (ISSUE-239). `istota_kv.value` is bare `TEXT` with no CHECK, bounded only by `SQLITE_MAX_LENGTH` (~10⁹). The real ceiling is `MAX_ARG_STRLEN` — Linux caps one argv element at 32 × page size (**128 KiB** on a 4 KiB-page host) and `execve` returns `E2BIG` above it — and there are two argv hops subject to it, the sandboxed `istota-skill` invocation and the proxy's own `subprocess.run`. The socket protocol between them is newline-delimited JSON with no cap of its own, so it is not the constraint. The cap applies **only to argv writes**: `get` returns on stdout and `set-add` never passes the accumulated array, so a value grown past 128 KiB by repeated `set-add` reads back fine but can never again be rewritten wholesale by `kv set`. `--value-file` is the escape hatch; `set-trim --keep-newest N` bounds the collection instead (by count, not age — the store keeps no per-member timestamp, and `updated_at` is per-key).

**`--value-file` is scoped, because the CLI runs host-side.** The proxy spawns skill CLIs outside the sandbox with the daemon's filesystem view, so an unscoped path argument would be an arbitrary host-file read whose result `kv get` hands straight back (`~/.claude/.credentials.json` is valid JSON). The roots mirror what `build_bwrap_cmd` binds **for this user**: `$ISTOTA_DEFERRED_DIR`, `{mount}/Users/{ISTOTA_USER_ID}`, the task's own `{mount}/Channels/{ISTOTA_CONVERSATION_TOKEN}`, and `{mount}/Talk` for reads only (matching its read-only bind). `NEXTCLOUD_MOUNT_PATH` is deliberately the *shared* mount root for everyone — per-user isolation comes from the bwrap bind plus each CLI self-scoping by `ISTOTA_USER_ID`, and a host-side path argument does neither — so taking the mount root whole would have handed one user another's workspace back through `kv get`. Same scoping as `scheduler_deferred._source_path_allowed`. `resolve_host_path` returns the **resolved** path and callers must operate on it: validating one path and reopening the original re-walks its symlinks, reopening the window the check closed (the kv read additionally opens with `O_NOFOLLOW`). A destination is checked before any `mkdir`, so a refused `cp-out` can't leave an out-of-bounds tree behind. The rule lives in `src/istota/skill_host_paths.py`, shared with devbox's `cp-in`/`cp-out`, which had the only (and mount-root-wide) copy before.

**`set-contains` is batched**; `list` is bounded. `set-contains` takes `nargs="+"` and returns a map (`{"contains": {"id-a": true, …}, "present": N, "missing": M}`) for two or more members, keeping the scalar `{"contains": bool}` for exactly one so existing prompts don't break — an N-check run was N spawns, N proxy crossings and N full parses of the same array, and the parse was always the cost, not the storage. Every response carries `"batched": bool`, because a caller building its member list from a variable-length collection would otherwise get the scalar exactly when its batch happened to hold one item. A stored array of non-string members produces the standard error envelope rather than an unhashable-type traceback. `list` truncates each value to 2048 chars by default, marking the entry `"truncated": true` with a `value_chars` count of the real length and reporting `truncated_count` on the envelope (`--keys-only` drops values entirely, `--max-value-chars 0` returns them whole); `get` and `set-members` are explicit content requests and are never truncated. The operator CLI (`istota kv list`) shares the renderer but defaults to whole values — a human piping to jq wants the entry — and `istota kv set` has its own unscoped `--value-file`, since it runs as the operator in their own shell with no task roots to scope to.

### `tasks/` - Task state read surface
**Subcommands**: `status <id> [--max-chars N]`, `recent [--since 30m|2h|7d|<UTC ts>] [--parent ID] [--status S] [--source-type S] [--limit N]`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID` (both handed to the CLI by the proxy, not to the model)
**Note**: `admin_only` + `cli`. Answers "what happened to the subtask / scheduled run I handed off" (ISSUE-237) — before this the skill was write-only (deferred subtask creation), so an agent needing the answer hand-rolled a poll against the SQLite file and got `unable to open database file` for the whole Bash timeout. Backed by `db.get_task_state_for_user` / `db.list_recent_tasks_for_user`, both of which take `user_id` as a **mandatory ownership predicate** rather than an optional filter, and return `not_found` identically for "no such task" and "not yours" so the surface is no existence oracle for task ids. That scope is the boundary — and it is now the *only* thing that needs to hold per-user, since the framework DB is no longer reachable from the sandbox at all (its directory is masked; see `.claude/rules/executor.md`). It was already the only real boundary before that: the old read-only bind blocked a raw read in some sidecar states and not at all on the standalone install. `status` carries `result` (capped, with `result_truncated`/`result_chars`) and an untrusted-content `notice`, since a result body routinely quotes email/web/feed text — hence `companion_skills: [untrusted_input]`; `recent` is an index with a `prompt_excerpt` (160 chars), no result bodies, and an echo of the filters it applied. `--since` is parsed rather than shape-matched (`2026-13-45` would otherwise compare as a string and silently match nothing) and bounded, so an oversized window can't raise `OverflowError` past the `ValueError` handler. `--status` is a `choices=` set; `--source-type` stays free-form and is echoed back instead. Rows carry `conversation_token`: the scope is the user, not the room, so a caller has to be able to see that a result came from elsewhere. The skill body also carries the out-of-sandbox doctrine (skill CLI → devbox → handoff) and the poll-loop rules (never `2>/dev/null` the probe, abort after two non-zero exits, cap the wait).

**`admin_only` does not gate execution.** It filters eager selection, companion expansion, the menu and `skills show` — all documentation surfaces. Three paths reach a CLI without consulting it:

1. The skill proxy's `allowed_skills` is *every* `cli: true` skill in the index (`executor.py:3336-3338`) — deliberately wide, so a menu-pulled skill works without a re-plumb.
2. `format_cli_skills` built the prompt's "Skill CLI tools" list off `meta.cli` alone, so the first `admin_only: true` + `cli: true` skill would have been advertised to every non-admin *and* executable. Fixed: `format_cli_skills(skill_index, *, is_admin)` takes the flag keyword-only with no default.
3. A CRON.md `command: istota-skill tasks recent …` row is promoted to a skill-task by `cron_loader._parse_skill_command` and run by `scheduler._execute_skill_task`, which is explicitly not admin-gated and sets `ISTOTA_DB_PATH` unconditionally. A non-admin reaches the CLI this way — as they now do on the LLM path too, since `ISTOTA_DB_PATH` goes to the proxy for every user rather than admins only.

So `admin_only` is a documentation gate, and a CLI that needs a real boundary must carry its own. `tasks` scopes every query by `ISTOTA_USER_ID`, which holds on all three paths — path 3 in particular leaves no cross-user leak, only a non-admin reading their own tasks.

### `code_review/` - Branch-diff review (one or two text-only reviewers)
**Subcommands**: `run --worktree PATH [--base REF] [--range REV..REV] [--intent TEXT] [--agents both|conformance|bughunt]`
**Env vars**: `DEVELOPER_REPOS_DIR` (containment root), `ISTOTA_BRAIN_NATIVE_API_KEY` (`sensitive`, **not** `proxy_only`), plus the framework's `ISTOTA_DB_PATH` / `ISTOTA_USER_ID` / `ISTOTA_TASK_ID` for the call counter
**Note**: `admin_only` + `cli`. Runs host-side through the proxy, where `load_config()`, `make_brain()` and the worktree all are. The precedent is `memory/sleep_cycle.py:_run_sleep_cycle_brain` — a privileged, text-only `BrainRequest` with `allowed_tools=[]`, a timeout, no streaming, no sandbox wrap, behind the shared availability breaker. Two reviewers run concurrently: a conformance checker and a bug hunter, sized from the diff (`both_agents_threshold_lines`, default 150) with `boundary_patterns` forcing both on an otherwise tiny diff that touches auth, secrets or credentials. Findings are merged across reviewers on `(file, line)`, `low` and preference entries dropped, sorted by severity then path then line. `engine.py` holds everything testable without a model; `run_review` is the only part that calls the brain.

**The API key must be `sensitive` and must not be `proxy_only`, and the ordering is what decides it.** `executor.py:3679` splits the proxy-only set out of the env *before* `executor.py:3688` splits the credential set, so a var flagged both lands in `proxy_only_env` and never reaches `credential_env`. `proxy_only_env` is folded into `proxy_base_env` (`executor.py:3727`), which **every** skill CLI receives unscoped — `derive_proxy_only_set` says as much in its docstring, that these "aren't secrets, so there is nothing to leak between skills". An API key is a secret. Declared `sensitive` alone it goes through `derive_skill_credential_map` (`executor.py:1181-1200`), which scopes injection to skills whose own manifest declared the var, so only `code_review` ever sees it. It is withheld from the model either way; the difference is whether every other skill CLI also gets a copy. On a `claude_code` deployment the CLI is already authenticated via `CLAUDE_CODE_OAUTH_TOKEN`, which survives `build_clean_env` only because an explicit line puts it there (`executor.py:918-921`); on a native one, without this env spec `make_brain(config)` would build a brain with `api_key = ""` and every review would fail auth.

**The git runner is hardened, because the worktree is model-writable.** `repos_dir` is bound read-write into the admin sandbox (`executor.py:1564`, `executor.py:1818`), so a path that `resolve_under_repos` approves cleanly can still be a repository whose *configuration* the model wrote. Four classes of escape were demonstrated end to end across the Stage 2 and Stage 3 reviews, each now pinned by a regression test in `tests/test_code_review_engine.py`, and the module docstring in `engine.py` is the full statement of them:

1. **Config-driven execution.** `diff.external`, a `.gitattributes` textconv or diff driver, `core.fsmonitor`, or `log.showSignature` plus `gpg.program` makes a plain `git diff` or `git log` run a command *as the daemon user* — the user holding `GITLAB_TOKEN` and `GITHUB_TOKEN`. That is the feature turning into RCE, not a read primitive. Answered by command-line overrides (which beat the repository's own values) plus `--no-ext-diff --no-textconv` on every content-producing command.
2. **Upward search.** A contained plain directory with no `.git` sends git searching upward, so the collected diff is the repository *above* the root. Answered by a discovery ceiling at the root.
3. **Relocation.** A `.git` file containing `gitdir: <outside>`, or a linked-worktree git dir whose `commondir` points outside. `rev-parse --show-toplevel` reports a contained path in the first case and `--absolute-git-dir` reports one in the second, so neither check alone catches both — `git_dir` puts `--absolute-git-dir` *and* `--git-common-dir` back through `resolve_under_repos`, and every collector calls it before reading content.
4. **Option injection.** A caller-supplied range is a bare argv element, so `--output=<path>` is an arbitrary daemon-side write and `--ext-diff` turns a driver back on. Answered by `--end-of-options` before every revision.

**Content comes out of the object store, never off the filesystem.** A symlink planted in a worktree makes `(worktree / path).read_text()` read straight out of the root with no race needed, and git lists such a path in `--name-only` quite happily; `git show <rev>:<path>` returns the link *text* instead, so the class does not arise. Nothing here opens a path inside a worktree directly, and nothing should start. What this does not close: validation is not atomic with use — the tree stays writable throughout, so a component can be replaced between a check and a read. Reading through git shrinks that window to git's own resolution rather than eliminating it.

`_git` also bounds stdout (`MAX_GIT_OUTPUT_BYTES`, 32 MiB), closes stdin — `rev-list --stdin` on an inherited stdin is a hang with no diagnosis — and carries a deadline, since the size of a diff is chosen by the same party that chose the path.

**`status` splits three ways and the workflow branches on it** (`developer/skill.md` step 9). `ok` means act on the findings. `error` — a bad range, a path outside the allowed roots, an unreadable worktree — blocks the push: something is wrong with the request, and the caller can correct it and re-run. `skipped` does **not** block: `brain_unavailable`, `brain_unsupported` (a `tmux_claude` deployment has no text-only path), `call_cap`, `repos_root_unavailable`, `review_disabled`, and — from `run_review` itself, after the calls were made — `review_failed` (every reviewer's call failed) and `malformed_output` (every reviewer answered unusably twice) are all states of the environment rather than of the diff, and none resolves by refusing to push. The model lands the work and reports it unreviewed, naming the reason. A sandboxed-but-proxy-less deployment belongs in that same environment bucket but produces **no envelope at all**: `skill_client._run_direct` (`skill_client.py:101-110`) prints to stderr and exits 1 before the module starts, so a caller branching on `status` sees only a non-zero exit, indistinguishable from the `error` case. The workflow has to read that one as "review unavailable" from the exit alone, which is why it is stated in `code_review/skill.md` rather than left to the envelope. Getting this split wrong in the blocking direction is how a deployment that deliberately turned review off becomes unable to land anything. `malformed_output` sat on the blocking side until ISSUE-266 and did exactly that: a broken brain adapter made every review on the deployment unparseable, so no branch could be pushed for a reason no branch could fix. Four more fields decide whether an `ok` is actually clean: `empty` (nothing in the range — not a pass), `partial` (a reviewer was lost), `dropped_findings` (a reviewer wrote findings naming no file, so they were discarded) and `need_files_note`. The engine's two `skipped` reasons are the only ones that arrive with a full envelope rather than `_skip`'s minimal one, so `counts` is present and all zero on them; `rounds` (kept on the envelope rather than popped with the charge) is what distinguishes a skip that spent invocations from one refused before any, and `notice` carries `FAILED_NOTICE` there, since the untrusted model text on that path is in `error` rather than in findings.

**The agent budget is clamped to fit under the proxy, and the envelope says so.** `security.skill_proxy_timeout` kills the whole command, so a `timeout_seconds` that would not fit under it minus `ASSEMBLY_ALLOWANCE_SECONDS` (60) is lowered to one that does, bounded below by `MIN_AGENT_TIMEOUT_SECONDS` (30) — every review would otherwise die half-finished having already paid for both agents. The ceiling only ever *lowers*: written as `max(floor, ceiling - allowance)` over the configured value it could raise a small budget instead, which is not a clamp and makes the fit worse. A non-positive `timeout_seconds` is floored, since the brains disagree about what it means (native runs unbounded, `claude_code` kills each agent at once) and neither is a review; a small positive one is left alone. `agent_timeout_seconds`, `agent_timeout_configured` and `agent_timeout_clamped` carry the result to the caller, because the clamp's warning goes to the daemon log and the model that invoked the CLI cannot read it — a review cut to a third of its budget is otherwise indistinguishable from one that had all of it. They ride on any envelope that reached a reviewer; a guard's `_skip` / `_fail` envelope never got a budget and carries none of them. A ceiling too tight for `allowance + floor` cannot be satisfied at all and gets its own warning, since there the proxy kills the command with empty stdout and no envelope reaches anyone.

**The call cap counts invocations that returned, not successes.** `code_review_calls` (`schema.sql`, `db.code_review_calls_get` / `_increment`) is keyed by task id, capped by `max_calls_per_task` (default 8). Counting only *successful* rounds would let a reviewer stuck answering in prose run unbounded: every round spends real model calls, and a counter that moves only on a clean answer never reaches the cap that is supposed to stop the spend. The re-runs now come from an operator or a user rather than from the workflow — since ISSUE-266 an all-failed round is `skipped` and exit 0, so the workflow lands unreviewed instead of retrying — but the money is spent either way, which is what the rule is about. Guard refusals and breaker short-circuits stay free; a malformed-output retry belongs to the round that provoked it. A round is a **wave** of calls, so a run charges 1 or 2 whatever the agent count — one wave is up to four invocations (two agents, each retrying a malformed answer once) and the `need_files` round trip adds one *per reviewer*, so a run is at most two rounds and six invocations. `max_calls_per_task <= 0` permits nothing, matching `max_need_files = 0` next door. The counter is advisory against concurrency: it is read before the calls and written after, so two concurrent reviews of one task each overshoot by one.

**`need_files` is one round trip, never a loop.** A reviewer may name files it wants to read; the CLI serves them from inside the worktree via `git show` and re-invokes that reviewer once. `MAX_NEED_FILE_REQUESTS` bounds how many entries are considered, `max_need_files` how many are served, `MAX_NEED_FILE_BYTES` refuses an oversized blob on its size before `git show` reads it, and refused paths are truncated before being echoed into the reviewer's prompt and the caller's envelope. Type and size are asked for together, which is also what catches a directory — `git show <rev>:<dir>` prints a tree listing, and labelling that "(whole file)" is how a reviewer comes to believe a directory is a two-line module. Containment runs on the normalised path as well as the raw one, since normalising can *create* the option shape it was meant to refuse (`./-output=x` collapses to `-output=x`); that check is defence in depth, not the boundary — `_show` embeds the path behind `--end-of-options` in `f"{rev}:{path}"`, and git reads everything after the first colon as a literal tree path. Any net drop in findings between the two answers is recorded in `need_files_note`, because a second answer with an empty list is byte-identical to a genuinely clean review and retraction cannot be told from forgetfulness from outside.

**Two guarantees are weaker than they read.** The `ON DELETE CASCADE` on `code_review_calls` is decorative — `PRAGMA foreign_keys` is never enabled on these connections — so counters outlive their tasks and are pruned by whatever sweeps `tasks`. And `allowed_tools=[]` is binding on the native brain only; on `claude_code` it means the argv carries neither an allow-list nor `--dangerously-skip-permissions`, which rests on the upstream CLI's default rather than on anything this repo enforces.

### `email/` - IMAP/SMTP (two-way client)
**Read subcommands**: `list` (+`--since`/`--from`/`--unread`, snippet + has_attachments), `read` (headers, plain **and** html, attachment manifest), `search` (raw IMAP SEARCH string, verbatim — errors, never silent subject-match), `thread` (real References/In-Reply-To walk), `attachments <id> --dest`, `from-senders --senders` (server-side SEARCH, no 100-truncation — the digest/batching path), `newsletters --sources` (required). Every read verb takes `--scope {mine,shared,all}` (default `all`).
**Write subcommands**: `send` (+`--cc`/`--bcc`/`--attach`(repeatable)/`--reply-to`; Bcc never transmitted), `reply`/`reply-all <id>` (threaded from a fetched message), `mark <id> {read,unread,flagged}` + `delete <id>` (destructive — refuse without `--confirmed`), `output` (deferred structured reply).

**Outbound approval gate.** `send` / `reply` / `reply-all` run every recipient (To + Cc + Bcc) through `outbound_policy.recipients_require_hold` before anything leaves. A hold writes an `outbound_drafts` row and returns `{"status": "held", "needs_confirmation": true, "draft_id": N, "reason": …, "held_recipients": [...]}` at **exit 0** — a hold is a successful outcome of the verb, and a non-zero exit invites a retry with different arguments, which is the exact behaviour the gate exists to stop. A gate that cannot *run* (no `ISTOTA_USER_ID`, unloadable config, unreachable DB, unstorable draft) returns the standard error envelope and exits 1: it never falls through to a send, because a gate that fails open on a missing database is not a gate. There is deliberately no `--confirmed` — a self-supplied flag is friction, not a gate.

Three placement decisions that look arbitrary and are not. **The gate is in the CLI verbs, not in `send_email` / `reply_to_email`**: those library functions also carry briefing delivery and `outbound_drafts.release`, so gating them would hold every briefing and re-hold every approval the user had just granted. **It is not in the skill proxy** (`skill_proxy.py`), which would need per-skill argv parsing to find recipients. **It is not in the scheduler**, which sees the task only after SMTP has run.

**`output` is not exempt, and the CLI verbs are not the only check** (ISSUE-246). `cmd_output` writes a deferred file and returns; the send happens later in `transport/email/outbound.deliver_email_result`, which called the library functions directly and consulted no policy at all. That was the model's *preferred* reply verb, so the gate covered the two verbs it rarely used and missed the one it did. `_hold_if_unapproved` now runs in `deliver_email_result` immediately before each of its three send sites (thread reply, fresh send, legacy briefing), which is the only point every path passes — `email output`, a hand-written deferred file, `_deliver_deferred_email_output`'s gap-case delivery. The CLI checks stay as the early, better-worded in-turn refusal; the delivery-leg check is what makes the guarantee true rather than conventional. A hold there returns `True` (the task already reported success, so a hold must not fail it) and writes an `outbound_drafts` row with the threading headers snapshotted; a check that *cannot run* returns `False` and sends nothing. The hold announces itself immediately (`_announce_hold`, `purpose="alert"`), which is what tells the user a message they were told had gone out is in fact waiting — ISSUE-247 removed `_notify_confirmed_email_result`, whose held-vs-sent wording used to carry that, and `_announce_hold` covers every hold rather than only a gated task's.

The spec's original exemption reasoned that `output`'s recipient had already cleared the inbound gate. Both halves fail: a plain `yes` at an inbound prompt authorizes one message and writes no trust row, and thread-matched mail from the address the bot wrote to still clears the inbound gate without a trust row (ISSUE-234 narrowed that route to the correspondent, it did not make clearing it a grant). Keeping the check here means a thread continuation can still *run* a task, but the reply to it is held unless the recipient is authorized.

A reply snapshots `in_reply_to` / `references` / the resolved recipients / the subject *before* deciding to hold, so `release` sends from the row rather than re-fetching over IMAP.

**`--attach` paths are scoped on every send, held or not** (`_scoped_attachments` → `skill_host_paths.resolve_host_path`), and the caller attaches the **resolved** path. This is not part of the approval decision and is not reachable-around by setting the policy to `off`: the CLI is spawned host-side by the proxy with the daemon's whole filesystem view and `_attach_files` does a bare `read_bytes`, so an unscoped path argument was an arbitrary-file-read-to-email primitive. A *held* attachment is narrowed further to `{mount}/Users/{uid}` (`_holdable_attachments`), because `outbound_drafts._confined_attachment` re-checks against that workspace at release — a pending draft sits indefinitely and the path stays writable the whole time — so anything else would be a draft the user could approve and never send.

`effective_policy` is resolved **before** the DB connection is opened, so `off` costs no database at all; opening first made an unreachable or busy DB fail sends on an instance that had deliberately switched the gate off.

Answered via `!drafts` (Talk and web). The scheduler's `nag_stale_outbound_drafts` raises one `purpose="alert"` notification per draft left pending 24 hours, stamped `nagged_at` only after the notification is actually delivered. `release` raises `DraftSentButUnrecorded` when the bookkeeping *after* a successful SMTP transaction fails — its own class because that is the one failure a caller must never describe as "still waiting, try again"; `!drafts` reports it as sent-but-unrecorded and tells the user not to resend.
**Read scoping**: shared `istota.email_ownership` module resolves who owns an inbound message (plus-address → sender-match → thread-match); the inbound poll (`transport/email/inbound.py`) and the read-scope filter agree exactly, so an unscoped read can't leak one user's mail to another. `shared`/`all` fail closed if the framework DB (thread arm) is unavailable. `--scope mine` pushes all three arms down to the server: `TO bot+<user>@ OR FROM <addrs> OR HEADER References/In-Reply-To <ids we sent>`, the last built from the caller's own newest `_MINE_THREAD_MAX_IDS` `sent_emails` rows (ISSUE-252 — without it a reply to the bot's bare `From:` never entered the fetched window, so correctly-routed mail was invisible under the one scope meaning "mine"). That cap is in sends, not days, and `--since` does not widen it — a wider date only admits older ids, which the cap cuts first; `search` filters the whole window client-side and is the way to an older thread. The prefilter may over-fetch; the client-side ownership filter stays authoritative. Fetched bodies/snippets are wrapped in an untrusted-content delimiter; the whole payload carries an `untrusted: true` notice.
**Env vars**: `IMAP_HOST/PORT/USER/PASSWORD`, `IMAP_TIMEOUT`, `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM`, `ISTOTA_USER_ID`, `ISTOTA_DB_PATH`, `ISTOTA_TASK_ID`, `ISTOTA_DEFERRED_DIR`. Read verbs `load_config()` (via `ISTOTA_CONFIG_PATH`) for the user table + DB (scoping); IMAP/SMTP creds come from the proxy-injected env.
**Key fns**: `list_emails()`, `read_email()`, `fetch_emails_full()`, `send_email()`, `reply_to_email()`, `mark_email()`, `search_emails()`, `get_newsletters()`, `delete_email()`, `delete_emails_before()` (library-only retention primitive — no CLI verb, so the agent can't reach it and the `--confirmed` gate on `delete` doesn't apply; bounded per sweep and, like `delete_email`, scoped to its own UIDs via `UID EXPUNGE` on a UIDPLUS server — see `.claude/rules/scheduler.md`), `cmd_output()`; `email_ownership.resolve_email_owner/owner_in_scope`.
**Multipart sends**: `send_email` / `reply_to_email` take an optional `html_body`; non-empty makes the message `multipart/alternative` via `_set_body` (`set_content(body)` plain fallback + `add_alternative(html_body, subtype="html")`), and `content_type` then applies only to the single-part case. Empty or omitted is byte-identical to the historical single-part send, so the CLI verbs and every existing caller are unaffected. The only consumer is briefing email delivery (see `.claude/rules/transport.md` "Briefing email bodies"); the CLI `send` verbs do not expose it.

### `calendar/` - CalDAV
**Subcommands**: `list` (`--date`, `--week`), `create`, `update` (`--clear-location`, `--clear-description`), `delete`
**Env vars**: `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
**Key fns**: `get_caldav_client()`, `get_calendars_for_user()`, `get_events()`, `get_event_by_uid()`, `create_event()`, `update_event()`, `delete_event()`

### `markets/` - Market Data CLI
**Subcommands**: `quote`, `summary`, `finviz`
**Env vars**: `BROWSER_API_URL` (finviz only)
**Key fns**: `get_quotes()`, `get_futures_quotes()`, `get_index_quotes()`, `format_market_summary()`, `fetch_finviz_data()`, `format_finviz_briefing()`

### `browse/` - Headless Browser
**Subcommands**: `get`, `render`, `screenshot`, `extract`, `interact`, `links`, `close`
**Env vars**: `BROWSER_API_URL`
**Note**: `render` is the first move on any page whose structure matters — it returns the page as markdown, so a headline keeps its URL and its position (ISSUE-192); `get` flattens the DOM and drops every href. `--mode full` for hubs, `--mode article` for bodies. Article mode is overridden to full when the URL looks like a section front *and* no single `<article>` node dominates the page, so a headline grid can't be silently discarded; `notes` says when that happened. Declares `requires_capability: [browser]`, so the whole skill drops out of selection and the menu when `config.browser.enabled` is off.

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
**`import-garmin-tracks`**: imports Garmin watch GPS tracks into `location.db` via the shared `istota.location.garmin_import.import_tracks` (also driving the web "Import GPS tracks" button and the cron script). Direct/delegated split like health `garmin-sync`: with `ISTOTA_SECRET_KEY` in env it runs inline; sandboxed it writes a `task_<id>_garmin_import.json` deferred op that `scheduler_deferred._process_deferred_garmin_import` runs in-process post-task (where the key lives) and notifies the user. The deferred-op path is what makes this work: `location.db` is not in the sandbox (its directory is masked), and the token-decrypt key is stripped, so the write happens post-task in the daemon.

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
**Note**: CLI wrapper around the standalone `gws` binary. Credentials injected via skill proxy. OAuth tokens stored in `google_oauth_tokens` DB table, refreshed automatically. `[google_workspace] scopes` is the **ceiling**, not the request: `istota.google_scopes` maps six services (Drive, Gmail, Calendar, Sheets, Docs, Chat) to their read-only and full scope sets, each user picks a `{service: off|readonly|full}` subset stored on `user_profiles.google_scopes`, and `google_connect` passes the clamped resolution as `scope=` at `authorize_redirect` time rather than relying on the registered client default. Empty selection = unset = the whole ceiling (what every user had before the picker), but a *non-empty* selection is authoritative and a service it does not name is off, so widening the ceiling never silently widens an existing request. A ceiling scope the map does not know (`drive.file`, `gmail.send`, `openid`) has no picker row, so nobody can decline it — it is therefore appended to every request verbatim and surfaced as `unoffered_scopes`; dropping it would have silently downgraded every user on an instance running narrow scopes, and refused connect outright once the resolution came back empty. The skill body has no per-verb gate and never will — the token is what Google enforces; what it does carry is the distinction between "not connected" and "connected without this scope", which are different advice and used to be reported as the same thing.

### `money/` - Accounting (in-process)
**Subcommands**: `list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `add-transaction`, `edit-transaction`, `backfill-ids`, `sync-monarch`, `debug-monarch`, `import-csv`, `invoice` (sub: `generate`, `list`, `paid`, `unpaid`, `create`, `void`), `work` (sub: `list`, `add`, `update`, `remove`, `backfill-ids`), `portfolio` (sub: `import`, `snapshots`, `summary`, `history`, `diff`, `symbol`, `delete-snapshot`, `accounts`, `classifications`, `classify`, `unclassify`, `autoclass`)
**Tax config (operator CLI only)**: `istota money tax set|rates|schedule|pattern`. `set` takes `--state` (`""` = no state tax); `rates` carries the payroll scalars; `schedule` carries brackets and standard deductions keyed on `(year, jurisdiction, filing_status)`, replacing the old filing-status-agnostic `--ca-*` flags. See AGENTS.md "Money taxes".
**Env vars**: `MONEY_USER` (the istota user_id; config resolved from the per-user money DB via `resolve_for_user`)
**Note**: In-process facade — resolves the user's `UserContext` via `istota.money.resolve_for_user` and invokes the `istota.money` Click CLI via `CliRunner` with the `Context` injected. No subprocess, no HTTP. Money is fully istota-native: **no standalone `money` binary**, no `MONEY_CONFIG`/`config.toml`/`load_context`, and no TOML config-read fallback — config (invoicing/monarch/tax) lives only in the per-user money DB (`config_store`), seeded from legacy TOML once by `_migrate` on first touch. The same operations are operator-reachable as `istota money <op> …` (`cli_money.py`): the CLI resolves the user the istota way (`-u USER` → DB) and forwards to the same Click tree. argparse can't capture a leading option through `REMAINDER`, so `main()` peels `money <operational-cmd>` off before `parse_args` and routes via `cli_money.dispatch_operational`; config-management commands (`config|client|company|service|tax|monarch`) stay native argparse. `lots` and `wash-sales` are `@requires_feature`-gated (`money_tax` / `money_wash_sales`); gated-off calls return the standard error envelope. Transactions carry a stable `id:` metadata line (backfilled once via `backfill-ids`, auto-run from `ensure_initialised`; stamped by every writer, plus `monarch-id:` on synced entries). `edit-transaction` locates by id and rewrites the directive in place under a ledger flock with `bean-check` + rollback (`core/edit.py`); `edited:`-marked entries are left alone by Monarch sync's reconciler.

**Invoice auto-matching** (`core/invoice_matching.py`, wired from `cli._apply_invoice_matching`) closes the loop between a synced bank credit and the invoice it pays: the sync booked the income but left the invoice open, so every payment needed a manual `invoice paid` afterwards. A credit settles an invoice only when **exactly one** open invoice fits — total equal to the cent (or within `--tolerance`) and not issued after the payment. Three shapes of invoice are excluded outright, because a total that isn't the outstanding balance is how the wrong invoice gets settled: partly paid (some entries stamped `paid_date`), partly unrecognised (`build_line_items` silently drops an entry whose service left the config, understating the total), and fully paid. A work entry carries `invoice_date`, stamped in the same `_work_lock` acquisition as `invoice` by every path that assigns one (`assign_invoice_number`, `assign_invoice_number_by_uids`, `add_work_entry` for `invoice create`, `_sync_invoice_date` for `work update --invoice`, which inherits the date of an invoice that already went out rather than stamping today, and leaves an entry joining a pre-field invoice unstamped so it keeps the fallback) and cleared by `void_invoice`. `work.invoice_issue_date` is the single rule every reader goes through — `_open_invoices`, `invoice list` and the web invoice list — and it returns the stored date when there is one. Invoices raised before the field existed have none and nothing can reconstruct one, so they fall back to `max(entry.date)`, the latest work billed: a sound *lower bound*, since an invoice can't predate the last work on it, but a loose one that admits credits from the gap between last work and issue. For those, amount uniqueness rather than the date is what keeps a match honest. The fallback is permanent (ISSUE-256). It marks paid by calling `record_invoice_payment` directly rather than routing through `invoice paid`, which is what makes it equivalent to `--no-post`: the ledger already carries the income and posting again would double-count it. Ambiguity is never resolved — two invoices fitting one credit, or two credits fitting one invoice (`_demote_contested`), are reported under `invoice_matching.review` and left alone. Matching runs once across *all* profiles rather than per profile: `sync_all_profiles` fetches from Monarch once and dedups per profile, so contention across profiles is reachable, and a per-profile pass would let whichever ran first settle the invoice silently. Money is compared as integer cents (`_cents`) because an invoice total is a sum of floats. The whole step is best-effort and wrapped in one `try` for that reason: it runs *after* the ledger append, the staging-file write and the dedup commit, and on the `run-scheduled` path *before* invoice generation, so a raise would both report failure for work that succeeded and stop the rest of the cron run. A malformed work TOML must not be able to break a bank sync. A write that returns zero rows is also demoted to `review` rather than reported as settled — open invoices are read without the work lock, so a web-UI payment can land in the gap. `--no-match-invoices` turns it off; `invoice unpaid` is the way back from a wrong match (the inverse of `invoice paid`, unlike `invoice void`, which un-invoices the work entirely).

**Work entries** carry the same stable-identity treatment as transactions, for the same reason. A work entry's `id` is a 1-based display index recomputed on every load — safe for the CLI's read-then-act loop, unsafe for anything holding a reference across time, since an insert before it silently shifts what `work update 5` hits. Every entry therefore also carries a `uid` (`core/ids.new_txn_id`), stamped by `add_work_entry`, backfilled by `backfill_work_ids` (auto-run from `ensure_initialised`, exposed as `work backfill-ids`), and re-stamped as a side effect of any `_save_entries` write. Programmatic callers — the web API above all — use `update_work_entry_by_uid` / `remove_work_entry_by_uid`, which resolve *inside* the `_work_lock` and return a typed `WorkMutationResult` (`ok` / `not_found` / `invoiced` / `conflict` / `no_fields`) so a route can tell 404 from 409. `entry_etag(entry)` (sha256 of the serialized form, truncated) is the optimistic-concurrency token: pass it as `expect_etag` and a mutation refuses with `conflict` if the entry changed since it was read. Reading never stamps a uid, so `work list` can't mutate the store.

Invoice generation is the third uid-addressed caller: `generate_invoices_for_period` resolves its billable entries, renders PDFs (seconds), and only then stamps, so it must use `assign_invoice_number_by_uids` — an index-addressed stamp across that window lands on whatever shifted into the slot, giving one client's entry another's invoice number while the entry on the rendered PDF stays uninvoiced and is billed again next run. It `backfill_work_ids` first (a hand-added entry can arrive with no uid) and logs `invoice_stamp_incomplete` at ERROR if any billable entry didn't get stamped, since nothing downstream can detect the double-billing. `assign_invoice_number` (index-addressed) survives for resolve-and-stamp-immediately callers and test setup.

Year files are hand-editable: unrecognised keys round-trip via `WorkEntry.extra` (quoted when they can't be written bare), but **comments and nested tables do not survive** a programmatic write (the serializer is hand-rolled string building over `tomli` reads). Because the serializer is hand-rolled, two guards keep a bad write from bricking a year: `_escape` escapes the **full** TOML control-character set (a bare `\r` in a description makes the file unparseable for every reader, invoicing included), and `_save_year` parses its own output before `os.replace`, raising rather than persisting a file that won't load. The web routes additionally reject non-string and control-character field values at the boundary (`_coerce_work_fields`).

The **read** side is correspondingly forgiving, since a hand edit reaches it first: `_load_year` coerces a quoted date/number and narrows a TOML datetime, so the plausible mistake doesn't surface three layers later as an `AttributeError` from `.isoformat()` or a `TypeError` from the sort (which took down every reader). An unusable *optional* number is dropped and the entry kept (a visible $0 row beats a billable entry that silently vanished); a row with no usable date/client/service is skipped and its year recorded in `_QUARANTINED_YEARS`. Quarantine is what makes skipping safe: a write rewrites the whole year from the loaded list, so `_save_year` refuses (`WorkFileQuarantined`) any write to that year that would change its visible content, and silently skips one that wouldn't (`_save_entries` rewrites every year it knows about, not just the target). Net effect versus before: the year stays readable, and the unreadable row can't be deleted by the next write. Reads also no longer *create* the store — `_load_all` returns `[]` for a missing `invoices/work/` rather than mkdir-ing it (and a lock anchor) on the mount for users who never invoice, and `backfill_work_ids` does its "is anything missing?" pass lock-free so the `ensure_initialised`-on-every-request no-op doesn't contend with `invoice generate`.

**Invoicing config (clients / entities / services)** is editable from the browser as well as the CLI: the Clients tab and the money settings **Invoicing** section drive the `/config/*` routes that already backed `istota money client|company|service`. Validation is split by who it protects against. The **invariants whose violation changes behaviour silently** live in `config_store` and raise `ValueError`, so the CLI and the agent are held to them too: `service.type` ∈ `hours|days|flat|other` (an unknown type has no branch in `entry_line_item` and quietly bills as hours), `client.schedule` ∈ `on-demand|monthly` (`check_scheduled_invoices` only acts on `monthly`), a finite non-negative `rate`, integer day ranges, `terms` as `int | str` (a *numeric string* obeys the same `>= 0` rule as an int — the column is TEXT and `load_invoicing` coerces `"-5"` back to `-5`, which renders a due date before the invoice date), beancount account/commodity shapes, an entity `logo` that stays inside the accounting folder (it is base64-embedded into the PDF and `accounting_path / logo` lets an absolute operand escape; `core/invoicing._resolve_logo` is the read-side half), and a slug-shaped **new** key. Account validation is **Unicode-aware** (`_is_account`, mirroring beancount's own `[\p{Lu}][\p{L}\p{Nd}\-]*` — an ASCII-only regex rejected `Assets:Forderungen:Müller`, locking a non-English ledger out of an account it had been posting to); the commodity shape allows a single letter, as beancount does. **Client keys are additionally lowercase-only**: `add_work_entry` stores `client.lower()`, so a mixed-case config key matches no entry, `build_line_items` skips every one of that client's rows, and the client's work is silently never billed — a hazard only an operator could reach before the browser form existed. Entity and service keys are unconstrained (entries store those verbatim).

Only the fields a caller actually **changed** are validated (`unchanged_fields(fields, current)`, computed inside the write transaction against the stored row). Validating the merged record would make a legacy non-conforming row uneditable; validating every *passed* field is nearly as bad, because a form seeds each input from the stored value and sends the lot back — so renaming a service typed `hourly` 400s on a field the user never touched. Changing such a field still has to produce a valid value, so the rule only grandfathers what is already on disk. The forms cooperate: an out-of-set `type`/`schedule` is surfaced as its own `(unrecognised)` dropdown option with a warning, rather than silently displaying `Hourly` for a record that isn't. The **shape** checks (`_coerce_{client,entity,service}_fields` in `routes.py`, modelled on `_coerce_work_fields`) stay route-side: unknown keys rejected by name (the store carries its own `_reject_unknown` gate too, so the two allowlists must stay in step), JSON types checked with the `isinstance(value, bool)` guard, control characters refused via `_CONTROL_CHARS_RE` (which lets `\n` through, so multi-line `address` / `payment_instructions` work). The route maps the store's `ValueError` to a 400 with the message intact. `save_invoicing` — the bulk path used by the legacy-TOML migration and `money config import`, which bypassed all of the above — **sanitizes** out-of-set enums and non-finite rates with a `money_config_sanitized` WARNING rather than raising, since refusing would strand a user mid-migration and each coercion lands on the behaviour that value already had.

Route guarantees the forms depend on: `POST` **creates** (409 on a taken key, rather than the silent upsert it used to be) — decided by `create_only=True` *inside* the write transaction (`config_store.KeyExistsError`, a `ValueError` subclass), so two concurrent creates can't both pass a pre-check and have the second overwrite the first; `PUT` keeps upsert semantics for `ensure`-style callers, with `?create=false` (what the forms send) making it 404 rather than resurrect a record another tab deleted; a body that is present but unparseable or not an object is a 400, never a silent defaults-only write; `DELETE` 404s on a missing record; and `PUT /config/invoicing` is shape-checked like the collections (a string `next_invoice_number` used to raise inside the generator, and a `default_entity` naming no company is what leaves `load_invoicing` falling back to an arbitrary one).

The **delete guards** live in `money/config_refs.py`, not in the route, so they hold on **both** surfaces — an agent reaching for `istota money service remove` must not be able to unbill work in a way the browser refuses to. They sit outside `config_store` because the references being counted aren't in the config DB at all (work entries are TOML in the user's workspace, so the scan needs a `data_dir`). Strictness matches how badly the absence corrupts things: a **service** named by any work entry is refused (deletion unbills future work *and* re-renders past invoices short, since `GET /invoices` rebuilds totals from live config); an **entity** is refused while a client names it, **a work entry pins it** (`resolve_entity` checks `entry.entity` *first*, ahead of the client's), it is the stored `default_entity`, or it is the *effective* default blank-entity clients fall back to; a **client** is allowed, returning its `work_entries` count (matched case-insensitively) so the confirm dialog can say what it costs. Two subtleties the guards got wrong first: the effective default is `cfg.company.key` — what `resolve_entity` really falls back to — **not** the stored scalar, which names no company at all after an ordinary migration of a TOML with clients but no `[companies]` block (the stored scalar is still reported separately, so a fresh user's only entity stays deletable); and a **quarantined year file** (a row `_load_year` skipped without raising) makes every count a lower bound, so the two strict deletes refuse rather than reading it as zero, while the soft client delete reports it and proceeds. A scan that can't complete refuses for the strict kinds and degrades to an unknown count for the client.

Keys are immutable — they are referenced by plain string from four places with no foreign keys and no transaction spanning the money DB and the work TOML files — so the forms take a key on create and render it as static text thereafter; the regex and the client-lowercase rule are exported once from `web/src/lib/money/api.ts` rather than restated per form. Reading is split for the same class of reason as the work form's omit-vs-null rule: the Clients page loads `GET /clients` (defaults resolved in, drives the cards) *and* `GET /config/clients` (raw, drives the form), because binding an edit form to the resolved shape materialises the default onto the record and silently stops a later `default_entity` change from propagating. Forms send `""` to clear an optional field (the store skips `None`) and omit `bundles` / `separate` entirely (the merge preserves them, which is what lets them ship without a nested-list editor); a blank *rate* is likewise omitted rather than stored as 0, which would reprice every past invoice carrying that service to nothing. The mock API (`web/vite-mock-api.ts`) mirrors the value invariants, so the 400-validation class is exercised under `VITE_MOCK_API=1` instead of only in production. Rendered invoice HTML escapes every interpolated user string (`generate_invoice_html`). Spec: `Specs/Done/money-config-editing-clients-entities-services.md`.

**Portfolio (positions snapshots)** — point-in-time investment portfolio state in the per-user `money.db` (`src/istota/money/portfolio.py`: `portfolio_snapshots`/`portfolio_positions`/`portfolio_accounts`/`portfolio_classifications`), imported from Fidelity "Portfolio Positions" CSV exports (all three format revisions incl. the Aug-2026 sentence-case header) or fina's `portfolio_history.csv` via the importer registry's `kind="positions"` axis (`core/importers/{positions_base,fidelity_positions,fina_history}.py`, dispatched by `parse_positions_file`). Content-hash dedup (rows only, never the export timestamp) makes re-imports a clean `duplicate`; the web upload additionally offers Replace/Keep-both on a same-day collision. The account registry (free-text group label — an owner, a purpose, any grouping; the settings field autocompletes from groups already in use — free-text type guessed once on first sight, reversible `excluded` flag; the original `owner` column is renamed to `account_group` by a `PRAGMA`-gated in-place migration in `ensure_schema`) and symbol classifications (normalized symbol → asset class/sub-class/geography, seeded once from a bundled map, sentinel `portfolio_classifications_seeded_at`) are per-user data edited on `/money/settings/portfolio`; classification resolves at **read time** (explicit row → cash patterns → options detection → Unclassified), so an edit retroactively reclassifies all history. The classification catalogue is readable from the CLI as well as the web settings page — `portfolio classifications` lists the explicit rows (seeded map included), which is what lets the agent answer "how is this symbol categorized" without inferring it from a `summary` slice. **New symbols auto-classify on import** (`src/istota/money/portfolio_autoclass.py`): a yfinance ticker-metadata lookup (quote type / fund category / equity sector + country, guarded import, fail-soft) with conservative offline description heuristics as fallback ("…TREASURY BD ETF" → Fixed Income; a bare company name is deliberately left Unclassified rather than guessed). The two tiers read different signals on purpose. The **lookup** tier reads the category and the fund name with different trust. *Which asset class it is* is the category's job alone for commodities, because a fund name is marketing text that routinely carries a metal word — "Goldman Sachs ActiveBeta US Large Cap Equity ETF" is a large-cap equity fund and "Sprott Gold Miners" is equity too, and concatenating name onto category made the first a Commodities/Gold row. *Everything else reads both*, because the detail lives in the name: Morningstar says "Long Government" and the name says "20+ Year Treasury", it says "Large Blend" and the name says "Total Stock Market" — reading the category alone dropped TLT/SHY/IEF out of the tier entirely and demoted VTI from Total Market to Large Cap. A category that is emphatically *not* plain equity (`Trading--Inverse`, `Preferred`, `Convertible`, `Derivative Income`) returns `None` so the heuristic gets a say instead of being short-circuited by a durable wrong `Stocks` row; an equity category merely lacking a known sub-class (`Technology`, `Europe Stock`, `Moderate Allocation`) is still equity, since falling through for those cost the tier every sector, region, allocation and target-date fund. The **description** tier gates each branch on something a company name cannot have: a fund marker for the fund-shaped classes, and for a direct bond that, a coupon and maturity date, or a bond-shaped `security_type`. Without those gates (and word-anchored needles) `BARRICK GOLD CORP` classified as Commodities/Gold and `TREASURE GLOBAL INC` as Fixed Income. The coupon/maturity branch is the one that fires in production: Fidelity's `Type` column is `Cash`/`Margin` (an account registration), and neither shipped importer populates `security_type` with a security type at all, so that third branch is forward compatibility rather than the live path — which is why its test isolates it with a description carrying neither a coupon nor a date. Auto rows carry `source='auto'` on `portfolio_classifications` (`'seed'`/`'user'` for the other writers; column added by a race-guarded ALTER — `ensure_schema` runs on every request, cron and skill invocation, so a bare check-then-ALTER 500s the loser with `duplicate column name`, which `busy_timeout` cannot help). A `source='auto'` write is an **`INSERT OR IGNORE`** (`portfolio.insert_classification_if_absent`), so it can never replace an existing row *structurally* — the row's own primary key decides inside one statement. A read-then-write gate could not promise that: it gated on the *resolved value*, so a user row set to `Unclassified` (an offered value on every surface) was overwritten, and the multi-second network fetch sat between the read and the write, so a concurrent edit on `/money/settings` was clobbered and its `source` flipped to `auto`. The web card badges auto rows and a user edit clears the badge. Backfill for symbols already imported: `portfolio autoclass` (CLI + skill) or `POST /portfolio/classifications/auto` (the settings-card "Auto-classify" button, serialized per money DB — a second concurrent run 409s). Import runs classification off the event loop, **once per import rather than once per snapshot** (a fina history file parses into one snapshot per export date, and an unresolvable symbol writes no row, so per-snapshot classification re-fetched the same symbols on each — ~1200 lookups for twenty distinct tickers on the advertised one-time migration), and bounds it twice over: `MAX_LOOKUPS_PER_RUN` caps the count, `LOOKUP_BUDGET_SECONDS` the wall clock, and `LOOKUP_TIMEOUT_SECONDS` one call. The per-call timeout is enforced by running the lookup on a daemon thread because yfinance exposes none — `get_info()` takes no argument and defaults to 30s per HTTP call, several calls deep, so a count cap alone bounds a run at tens of minutes, past nginx's read timeout (`/istota/` now carries `proxy_read_timeout 300s` as belt-and-braces, since an import commits before it classifies and a 504 would report failure for an import that succeeded). Anything unresolved stays in `unclassified_symbols`. `[money] autoclass_lookup` (default on) gates the third-party egress — held symbols are private financial data and the call runs unsandboxed, outside the CONNECT allowlist — and the response carries `lookups_available` so the settings card can say "ticker lookup unavailable — heuristics only" rather than reporting a dead tier as "nothing to classify". That flag means the tier was *usable* (dependency present, operator switch on), not that it found anything — a delisted or private holding legitimately returns no metadata, and treating one of those as an outage told the user their lookup was down. The `lookups_attempted`/`lookups_failed` counts carry the rest, and an all-empty run of three or more warns in the operator log only. yfinance is in both the `markets` and `money` extras, since the portfolio module advertises lookup as its primary tier. Analytics (summary/group-bys/holdings, history series, per-symbol history, snapshot diff) filter excluded accounts before any aggregation; the stored snapshot `total_value` stays the raw file total. Web UI: a **Portfolio** money tab (Overview | History | Import — the Import subpage is one card with a source picker (Fidelity CSV / FINA CSV) whose selection is passed as `?source=` so a mismatched file errors instead of falling back to detection; a new source is a new dropdown row, not a new card; ledger picker suppressed — snapshots aren't ledger-scoped). Never writes the beancount ledgers.

### `health/` - Body Stats, Bloodwork, Biomarker Trends
**Subcommands** (44): body data — `log`, `stats`, `latest`, `summary`, `settings`, `set`; bloodwork — `panels`, `panel`, `add-panel`, `add-biomarker`, `trend`, `upload`, `import-csv`, `export-csv`; medical history — `encounters`, `encounter`, `add-encounter`, `update-encounter`, `delete-encounter`, `diagnoses`, `diagnosis`, `add-diagnosis`, `update-diagnosis`, `resolve-diagnosis`, `delete-diagnosis`, `link-encounter`, `unlink-encounter`, `history-summary`; immunizations — `immunizations`, `immunization`, `add-immunization`, `update-immunization`, `delete-immunization`, `vaccine-refs`, `coverage`, `import-immunizations`, `explain-immunization`; Garmin — `garmin-status`, `garmin-sync`, `garmin-disconnect`; documents — `documents [--entity TYPE:ID]`, `document <id>`, `attach-document --path P --to TYPE:ID`, `detach-document <id> --from TYPE:ID`.

**Conditions and encounters**: `link-encounter <diagnosis-id> --encounter <id|@ref>` / `unlink-encounter <diagnosis-id> --encounter <id>` maintain the `diagnosis_encounters` many-to-many. `@ref` resolves an encounter created earlier in the same sandboxed batch, mirroring `add-panel --ref`. Sandboxed they defer `link_diagnosis_encounter` / `unlink_diagnosis_encounter`. `skill.md` tells the model to link an existing condition rather than `add-diagnosis` a second copy of it, and that unlinking keeps both records — removing the last link does not delete the condition.

**Documents**: `attach-document` files a user-supplied file against an `encounter` / `diagnosis` / `immunization`. `_parse_entity_ref` parses the `TYPE:ID` token and emits the standard error envelope on a bad type or non-integer id; `encounter:@NAME` resolves an encounter created earlier in the same sandboxed batch, mirroring `add-panel --ref` → `add-biomarker @ref` (`add-encounter` gained the matching `--ref`). Sandboxed it defers an `attach_document` op; unsandboxed it writes directly, resolving the real `HealthContext` through `load_config` + `_loader.resolve_for_user` rather than deriving `uploads_dir` from `HEALTH_DB_PATH` — on a server deploy the DB is on local disk while uploads stay on the mount, so deriving would file documents where the web route can't find them. `detach-document` removes one link and leaves the document. Deletion is deliberately **not** exposed to the agent (web-UI only, behind a confirm): an irreversible destructive action on medical records should need a human click. `skill.md`'s "Filing paperwork" section carries the attach-to-the-most-specific-record rule and the constraint that the agent files files the *user supplied* rather than going looking for medical documents in the workspace.
**Env vars**: `HEALTH_DB_PATH` (injected via `setup_env` hook from `istota.health.resolve_for_user(user_id, config).db_path`); the OCR/explainer paths additionally use the active brain for structured extraction.
**Note**: Standard module — on by default; per-user opt-out via `disabled_modules`. All values stored metric. Writes flow through deferred ops (`task_<id>_health_ops.json`) under sandbox; `scheduler_deferred._process_deferred_health_ops` replays them post-task. The web UI ships pre-written explainer payloads in the mock API for development.

**`garmin-sync` direct/delegated routing.** Garmin OAuth tokens live in the encrypted secrets table; the engine decrypts and re-encrypts rotated tokens mid-run. Subprocess callers don't have `ISTOTA_SECRET_KEY` by design, so `cmd_garmin_sync` checks `secrets_store.secret_key_available()` and dispatches: **direct** (operator shell with the EnvironmentFile sourced) runs the engine inline; **delegated** (sandboxed LLM Bash, hand-written CRON `command:` rows, dev shells without the env file) enqueues a `skill="health"` task with `max_attempts=1` and polls every 0.5s up to 60s, then surfaces the engine's JSON payload. The scheduler's `_run_garmin_sync_inprocess` short-circuit (see scheduler.md) makes the delegated path execute on the daemon thread where the key lives. The enqueue writes the framework DB, so it only runs where that DB is reachable — the daemon. A sandboxed `istota-skill health garmin-sync` reaches the delegated branch through the proxy (host-side), so it enqueues normally; the fail-loud `/garmin/sync` hint remains for a caller that genuinely can't open the DB. Project note `Skill proxy execution model and the master-key boundary.md` covers why this isn't auto-injected via the skill proxy.

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
source_types: [briefing]
cli: true
dependencies: [some-package]
env: [{"var":"MY_API_KEY","from":"secret","service":"my_service","key":"api_key","sensitive":true}]
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

### 4. Declare env vars in the manifest

The skill's `env:` block is the **only** place env vars should be wired — no
executor edits. `build_skill_env()` walks every loaded skill's manifest and
resolves each `EnvSpec` against the task's `EnvContext`.

```yaml
env:
  - {"var":"MY_API_KEY","from":"secret","service":"my_service","key":"api_key","sensitive":true}
  - {"var":"MY_API_HOST","from":"config","config_path":"my_section.api_host","when":"my_section.enabled"}
```

Available `from:` sources: `config` (dotted config path with `when` guard),
`secret` (per-user encrypted secret), `setup_env` (skill-defined hook in
`__init__.py:setup_env(ctx)`), `template_file` (auto-create from template),
`user_id` (literal task user_id).

Two flags decide who sees the resolved value. `sensitive: true` marks a
credential: stripped from Claude's env, injected by the proxy only for the
skills whose manifests declare it, fetchable via `credential-fetch`, and an
auto-authorization signal. `proxy_only: true` also withholds the var from
Claude and hands it to the proxy, but carries none of that machinery — it is
for non-secret values the model still must not hold, which today means paths
to SQLite files (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`; the framework
`ISTOTA_DB_PATH` gets the same treatment from `_EXECUTOR_PROXY_ONLY_VARS`,
since it belongs to no manifest). Don't set both: the credential split runs
first, so the var routes as a credential and the `proxy_only` flag is inert. The resource-backed sources (`resource`,
`resource_json`, `user_resource_config`) were removed in the Resources sunset
— no bundled skill used them.

## Skills

Self-contained `src/istota/skills/<name>/skill.md` (YAML frontmatter + body). **Single-axis model:** a skill is either **eager** (full body in the prompt, because a deterministic rule in `select_skills` picked it — `always_include` / `source_types` / `file_types` / `sticky` / `companions`, minus `excludes`) or in the **menu** (a one-line entry the model pulls in full via `istota-skill skills show <name>`, which also delivers that skill's companions). The menu is the full eligible catalogue (`eligible_skill_names` — every loadable skill not already eager), so the capable main model self-selects from it. Keyword (`triggers`) and `resource_types` matching are NOT selectors (kept as `!skills`-surfaced documentation); `resource_types` survives only as a menu-membership gate. There is no eager/lazy `disclosure` field, no `progressive_disclosure` flag, no `always_eager` list — the menu is intrinsic. CLI skills expose `python -m istota.skills.<name>` and run through the credential-injecting skill proxy. The `skills` core skill is the on-demand loader. (The former LLM "Pass 2 semantic routing" pre-router and the two-axis eager/lazy disclosure model were both removed — the menu replaced them.) Full details in this file.

## Nextcloud control plane

The `nextcloud` skill is a multi-group CLI over `src/istota/nextcloud/` (spec `Specs/Done/nextcloud-skill-cli.md`): `capabilities` (a one-call deployment fit-check with `--check` as a shell gate, also `istota nextcloud capabilities`), `user`/`group` lookup (non-admin-safe via `/core/autocomplete/get`; the provisioning verbs name admin rights as the cause of a 997), `share` (incl. `share link` — the ISSUE-193 fix: default expiry, optional password, a synthesized direct-download URL, a revoke loop), `files` (the WebDAV operations the mount can't express — properties, indexed server-side SEARCH, versions, trash, quota, chunked upload; deliberately **no** read/write/mkdir/rm/mv/cp), `talk` (an _agent-facing_ control surface, not a second delivery path), and `notify`/`activity` reads. Every failure raises `OcsError` carrying HTTP status + OCS status + the server's message. Gated by `requires_capability: [nextcloud]` (keyed on `nextcloud.url`, so it vanishes on a standalone install). Inbound text is untrusted-framed; outbound sharing/messaging is confirmation-gated; destructive verbs need `--confirmed`; `files`/`share` paths are scoped to `/Users/<caller>/` for non-admins. `nextcloud_client.py` survives as the `None`-returning shim for four best-effort daemon paths. **Live-verified**: `tests/test_nextcloud_skill_live.py` (`pytest -m integration`, needs `NC_URL`/`NC_USER`/`NC_PASS`) drives `main(argv)` for all 44 verbs against a real server, tiered behind `NC_TEST_ADMIN`/`NC_TEST_TALK`/`NC_TEST_DESTRUCTIVE`/`NC_OTHER_USER`; `TestLiveCoverage` in `tests/test_nextcloud_skill_cli.py` runs in the **default** suite and fails when a new verb ships without a live test (the live file is skipped without credentials, so a guard living there would guard nothing). The first live run found five defects the mocked suite had encoded rather than caught — SEARCH's scope href must be DAV-root-relative (`/files/<user>`, not `/remote.php/dav/files/<user>`, which Sabre 404s); a named activity stream is `/activity/<filter>` while the reserved `filter` segment is for object lookups; `talk send` must unwrap the OCS envelope (`send_message` alone returns the raw body) or it reports no message id; Talk's unified-search `from` param means "the page I'm on" and _excludes_ that conversation, so `--token` filters client-side on `attributes.conversation`; and `share link` expiry bases on **UTC**, since a client west of the server otherwise computes a date the server already calls past. See `docs/features/nextcloud.md`.
