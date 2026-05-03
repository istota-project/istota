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
              sticky_skills=None) -> list[str]
classify_skills(prompt, skill_index, already_selected,
                disabled_skills=None, is_admin=True,
                model="haiku", timeout=3.0) -> list[str]  # Pass 2 LLM classification
build_skill_manifest(skill_index, exclude, disabled_skills=None, is_admin=True,
                     user_resource_types=None) -> str
    # When user_resource_types is given, prepends "User has resources: …" header
    # and appends [needs resource: …] hints per skill — helps Pass 2 disambiguate.
compute_skills_fingerprint(skills_dir: Path) -> str               # SHA-256, first 12 hex chars
load_skills_changelog(skills_dir: Path) -> str | None             # CHANGELOG.md
load_skills(skills_dir: Path, skill_names: list[str], bot_name, bot_dir, skill_index=None, bundled_dir=None) -> str
    # Concatenate skill docs (strips frontmatter)
```

### Two-Pass Skill Selection

**Pass 1: Deterministic matching** (`select_skills`) — fast, zero-cost.

Filters applied to every candidate before any rule fires:
- `admin_only=True` skipped when `is_admin=False`
- Unmet `dependencies` (missing Python packages) skipped via `_check_dependencies()`
- Names in `disabled_skills` (instance-level + per-user, merged) skipped

Selection rules (priority order, with `continue` short-circuits in `_loader.py:344-374`):
1. `meta.always_include == True`
2. `source_type in meta.source_types`
3. Any `meta.file_types` match attachment extensions
4. Any `meta.keywords` found in `prompt.lower()` — additionally requires `user_resource_types ∩ meta.resource_types` if `meta.resource_types` is set

After the main loop:
5. **Sticky skills** (`_loader.py:376-387`) — names supplied via `sticky_skills` are added (filtered by disabled/admin_only/deps). Always-include skills are not re-added.
6. **Companion skills** — `meta.companion_skills` of already-selected skills are pulled in (respects disabled/admin_only/deps).
7. **Exclude pass** — `meta.exclude_skills` of selected skills are removed from the final set (e.g., briefing excludes email).

**Sticky skills source** (`executor.py:1761-1789`): for `talk` and `email` tasks with a `conversation_token`, the executor populates `sticky_skills` from:
- `db.get_recent_conversation_skills(conversation_token, max_age_minutes=30, limit=2)` — skills from the last two tasks in the same conversation within the last 30 minutes
- `parent.selected_skills` from `db.get_reply_parent_task()` when `task.reply_to_talk_id` is set (no time limit)

After execution, the resolved skill set is persisted via `db.save_task_selected_skills()` so future tasks in the conversation can carry it forward.

**Pre-transcription**: before skill selection, `_pre_transcribe_attachments()` (`executor.py:225`) transcribes audio attachments and enriches `task.prompt` with the spoken text so keyword rules match voice memos.

**Pass 2: Semantic routing** (`classify_skills`) — LLM-based, additive to Pass 1.
When `config.skills.semantic_routing` is enabled (default: true), a Haiku call sees the task prompt + a manifest of unselected skills (filtered for admin_only/disabled/deps) plus the user's resource types (so it can reason "user has miniflux → feeds is plausible"), and returns additional skill names. Results are unioned with Pass 1. On timeout/error, falls back to Pass 1 only.

After the union, the executor (`executor.py:1815-1823`) re-applies `exclude_skills` because newly added skills may exclude previously-selected ones.

Config: `[skills]` section — `semantic_routing` (bool), `semantic_routing_model` (str), `semantic_routing_timeout` (float).

Returns sorted list of skill names.

**Selection observability**: `select_skills` emits a single INFO log per task with each selected skill annotated by the rule that fired (`pass1_selection count=N: foo(always_include), bar(keyword='kw'), …`). `classify_skills` emits `pass2_added skills=…` on additions, `pass2_no_additions` when nothing was added, and `pass2_timeout after=Xs` when Haiku exceeded the timeout. Use these to reconcile selection misses against runtime proxy rejections (see executor.md).

### Skill Metadata (YAML frontmatter)
All metadata lives in YAML frontmatter at the top of each `skill.md` file:
- `name`, `triggers` (keyword list), `description` (for LLM routing manifest)
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
| `email` | — | email, mail, send, inbox, reply, message | email_folder | email |
| `calendar` | — | calendar, event, meeting, schedule, appointment, caldav | calendar | briefing |
| `todos` | — | todo, task, checklist, reminder, done, complete | todo_file | — |
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
| `notes` | — | note, save, write, markdown | notes_folder | — |
| `developer` | — | git, gitlab, repo, repository, commit, branch, MR, ... | — | — |
| `location` | — | location, gps, where, place, tracking, ... | — | — |
| `bookmarks` | — | bookmark, karakeep, save, read later, ... | karakeep | — |
| `website` | — | website, site, publish, blog, ... | — | — |
| `feeds` | — | feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe, opml | feeds | — |
| `google_workspace` | — | google drive, google docs, google sheets, google calendar, google chat, google workspace, gmail, spreadsheet, gws | — | — |
| `money` | — | accounting, ledger, beancount, invoice, invoicing, expense, transaction, ... | money (legacy `moneyman` accepted) | — |
| `untrusted_input` | — | — | — | — |

Note: `money` is the sole accounting skill. It runs in-process via the vendored `money` package (no subprocess, no HTTP).

`untrusted_input` is a doc-only companion skill — no triggers, no source_types, never selected directly. It loads via `companion_skills` declarations on the seven ingest-shaped skills (`email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks`) so its rules ride along whenever a task is processing content from outside the trust boundary. Paired with `sensitive_actions`: outbound rules in that one, inbound-reading rules here, per-action authorization principle stated in both.

## Skill CLI Modules (`src/istota/skills/`)

### `kv/` - Key-Value Store
**Subcommands**: `get NAMESPACE KEY`, `set NAMESPACE KEY '<json>'`, `list NAMESPACE`, `delete NAMESPACE KEY`, `namespaces`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `ISTOTA_DEFERRED_DIR`, `ISTOTA_TASK_ID`
**Note**: `always_include` core skill. Persistent per-user, namespaced JSON store. Writes go through deferred-DB pattern under sandbox.

### `email/` - IMAP/SMTP
**Subcommands**: `send`, `output`
**Env vars**: `IMAP_HOST/PORT/USER/PASSWORD`, `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM`, `ISTOTA_TASK_ID`, `ISTOTA_DEFERRED_DIR`
**Key fns**: `list_emails()`, `read_email()`, `send_email()`, `reply_to_email()`, `search_emails()`, `get_newsletters()`, `delete_email()`, `cmd_output()`

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
**Subcommands**: `current`, `history`, `places`, `learn`, `update`, `delete`, `attendance`, `reverse-geocode`, `day-summary`, `discover`, `dismiss-cluster`, `list-dismissed`, `restore-dismissed`, `place-stats`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
**Optional deps**: `caldav` (in `calendar` extra group)
**Shared logic**: cluster discovery, dismiss-zone management, and per-place visit stats live in `istota.location_logic` (pure SQL + `geo.haversine`). Both the FastAPI web routes and this skill import the same `_location_*` helpers — the web UI's "discovered clusters", "dismissed clusters", and place-detail visit stats are now reachable from CLI parity.

### `bookmarks/` - Karakeep Bookmark Management
**Subcommands**: `search`, `list`, `get`, `add`, `tags`, `tag`, `untag`, `lists`, `list-bookmarks`, `summarize`, `stats`
**Env vars**: `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY`

### `feeds/` - Native RSS / Atom / Tumblr / Are.na (in-process)
**Subcommands**: `list`, `categories`, `entries`, `add`, `remove`, `refresh`, `poll`, `run-scheduled`, `import-opml`, `export-opml`
**Env vars**: `FEEDS_USER` (set by executor); `TUMBLR_API_KEY` optional fallback
**Note**: In-process facade — resolves the user's `FeedsContext` via `istota.feeds.resolve_for_user` and invokes `istota.feeds.cli` through `CliRunner`. No subprocess, no Miniflux. Per-user SQLite at `{workspace}/feeds/data/feeds.db`; subscriptions in `feeds.toml`. Scheduler auto-seeds `_module.feeds.run_scheduled` (`*/15 * * * *`) for users with a `[[resources]] type = "feeds"` entry.

### `google_workspace/` - Google Workspace CLI Passthrough
**Subcommands**: Passes all arguments through to `gws` binary (Drive, Gmail, Calendar, Sheets, Docs, Chat)
**Env vars**: `GOOGLE_WORKSPACE_CLI_TOKEN` (injected via `setup_env` hook from DB OAuth tokens), `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` (writable cache dir)
**Note**: CLI wrapper around the standalone `gws` binary. Credentials injected via skill proxy. OAuth tokens stored in `google_oauth_tokens` DB table, refreshed automatically. Scopes configurable via `[google_workspace]` config section (default: read-only).

### `money/` - Accounting (in-process)
**Subcommands**: `list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `add-transaction`, `sync-monarch`, `import-csv`, `invoice` (sub: `generate`, `list`, `paid`, `create`, `void`), `work` (sub: `list`, `add`, `update`, `remove`)
**Env vars**: `MONEY_CONFIG`, `MONEY_USER`
**Note**: In-process facade — imports the vendored `money` package and invokes its Click CLI via `CliRunner`. No subprocess, no HTTP.

### Library-Only Modules (no CLI)
- `files/` - Nextcloud file ops (mount-aware, rclone fallback)
- `markets/finviz.py` - FinViz scraping for market data (internal helper for `markets`)

### Top-Level Library Modules (outside skills/)
- `feeds/_miniflux.py` — Legacy Miniflux briefing client (HTML feed page generation). Kept until the `[feeds] backend` flag in Phase 3+ retires the proxy path.

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
