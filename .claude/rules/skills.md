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
load_skill_index(skills_dir: Path) -> dict[str, SkillMeta]       # Load skill.md frontmatter (toml fallback)
select_skills(prompt, source_type, user_resource_types, skill_index, is_admin=True, attachments=None, disabled_skills=None) -> list[str]
classify_skills(prompt, skill_index, already_selected, model="haiku", timeout=3.0) -> list[str]  # Pass 2 LLM classification
build_skill_manifest(skill_index, exclude) -> str                 # Manifest for LLM classification
compute_skills_fingerprint(skills_dir: Path) -> str               # SHA-256, first 12 hex chars
load_skills_changelog(skills_dir: Path) -> str | None             # CHANGELOG.md
load_skills(skills_dir: Path, skill_names: list[str]) -> str      # Concatenate skill docs (strips frontmatter)
```

### Two-Pass Skill Selection

**Pass 1: Keyword matching** (`select_skills`) — fast, deterministic, zero-cost.
Skills with `admin_only=True` are skipped when `is_admin=False`.
Skills with unmet `dependencies` (missing Python packages) are skipped via `_check_dependencies()`.
Skills listed in `disabled_skills` (instance-level or per-user) are excluded.
A skill is selected if ANY of these match:
1. `meta.always_include == True`
2. `source_type in meta.source_types`
3. Any `user_resource_types ∩ meta.resource_types`
4. Any `meta.keywords` found in `prompt.lower()`
5. Any `meta.file_types` match attachment extensions
6. `meta.companion_skills` of already-selected skills are pulled in (respects admin_only + dependency checks)
7. `meta.exclude_skills` of selected skills are removed from the final set (e.g., briefing excludes email)

**Pre-transcription**: Before skill selection, `_pre_transcribe_attachments()` in executor.py transcribes audio attachments and enriches `task.prompt` with the spoken text. This allows keyword-based skills to match on voice memo content.

**Pass 2: Semantic routing** (`classify_skills`) — LLM-based, additive to Pass 1.
When `config.skills.semantic_routing` is enabled (default: true), a Haiku call sees the task prompt + a manifest of unselected skills and returns additional skills to load. Results are unioned with Pass 1. On timeout/error, falls back to Pass 1 only.

Config: `[skills]` section — `semantic_routing` (bool), `semantic_routing_model` (str), `semantic_routing_timeout` (float).

Returns sorted list of skill names.

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
| `accounting` | — | accounting, ledger, invoice, expense, tax, ... | ledger, invoicing | — |
| `transcribe` | — | transcribe, ocr, screenshot, scan, image, ... | — | — |
| `whisper` | — | transcribe, whisper, audio, voice, speech, dictation, ... | — | — |
| `notes` | — | note, save, write, markdown | notes_folder | — |
| `developer` | — | git, gitlab, repo, repository, commit, branch, MR, ... | — | — |
| `location` | — | location, gps, where, place, tracking, ... | — | — |
| `bookmarks` | — | bookmark, karakeep, save, read later, ... | karakeep | — |
| `website` | — | website, site, publish, blog, ... | — | — |
| `feeds` | — | feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe | miniflux | — |
| `google_workspace` | — | google drive, google docs, google sheets, google calendar, google chat, google workspace, gmail, spreadsheet, gws | — | — |
| `moneyman` | — | accounting, ledger, beancount, invoice, invoicing, expense, transaction, ... | moneyman | — |

Note: `accounting` and `moneyman` mutually exclude each other via `exclude_skills`. Users with a `moneyman` resource get the API-based skill; users with `ledger` resources get the direct beancount skill.

## Skill CLI Modules (`src/istota/skills/`)

### `accounting.py` - Beancount + Invoicing CLI
**Subcommands**: `list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `import-monarch`, `sync-monarch`, `add-transaction`, `invoice` (sub: `generate`, `list`, `paid`, `create`)
**Env vars**: `LEDGER_PATH`, `LEDGER_PATHS` (JSON), `ACCOUNTING_CONFIG`, `INVOICING_CONFIG`, `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`
**Key fns**: `_run_bean_check()`, `_run_bean_query()`, `cmd_sync_monarch()`, `cmd_invoice_generate()`, `cmd_invoice_list()`, `cmd_invoice_paid()`, `cmd_invoice_create()`, `cmd_add_transaction()`

### `email.py` - IMAP/SMTP
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

### `browse.py` - Headless Browser
**Subcommands**: `get`, `screenshot`, `extract`, `interact`, `close`
**Env vars**: `BROWSER_API_URL`

### `transcribe.py` - OCR
**Subcommands**: `ocr`
**Env vars**: None
**Deps**: `pytesseract`, `PIL`

### `memory_search.py` - Memory Search CLI
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
**Subcommands**: `current`, `history`, `places`, `learn`, `update`, `delete`, `attendance`, `reverse-geocode`, `day-summary`
**Env vars**: `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`, `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
**Optional deps**: `caldav` (in `calendar` extra group)

### `bookmarks/` - Karakeep Bookmark Management
**Subcommands**: `search`, `list`, `get`, `add`, `tags`, `tag`, `untag`, `lists`, `list-bookmarks`, `summarize`, `stats`
**Env vars**: `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY`

### `feeds/` - Miniflux RSS Feed Management
**Subcommands**: `list`, `add`, `remove`, `categories`, `entries`, `refresh`
**Env vars**: `MINIFLUX_BASE_URL`, `MINIFLUX_API_KEY`
**Key fns**: `cmd_list()`, `cmd_add()`, `cmd_remove()`, `cmd_categories()`, `cmd_entries()`, `cmd_refresh()`

### `google_workspace/` - Google Workspace CLI Passthrough
**Subcommands**: Passes all arguments through to `gws` binary (Drive, Gmail, Calendar, Sheets, Docs, Chat)
**Env vars**: `GOOGLE_WORKSPACE_CLI_TOKEN` (injected via `setup_env` hook from DB OAuth tokens), `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` (writable cache dir)
**Note**: CLI wrapper around the standalone `gws` binary. Credentials injected via skill proxy. OAuth tokens stored in `google_oauth_tokens` DB table, refreshed automatically. Scopes configurable via `[google_workspace]` config section (default: read-only).

### `moneyman/` - Moneyman Accounting API Client
**Subcommands**: `list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `add-transaction`, `sync-monarch`, `import-csv`, `invoice` (sub: `generate`, `list`, `paid`, `create`), `work` (sub: `list`, `add`, `update`, `remove`)
**Env vars**: `MONEYMAN_API_URL`, `MONEYMAN_API_KEY`
**Note**: Dual-mode client — CLI subprocess (preferred, via `MONEYMAN_CLI_PATH`) or HTTP REST API. Mutually exclusive with `accounting` skill.

### Library-Only Modules (no CLI)
- `files.py` - Nextcloud file ops (mount-aware, rclone fallback)
- `invoicing.py` - Invoice generation, PDF export, cash-basis income
- `finviz.py` - FinViz scraping for market data

### Top-Level Library Modules (outside skills/)
- `feeds.py` (`src/istota/feeds.py`) - Miniflux API client + HTML feed page generation (used by scheduler for periodic regen)

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
