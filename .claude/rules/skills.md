# Skills System

## Skills Loader (`src/istota/skills_loader.py`)

### `SkillMeta` Dataclass (L12-21)
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
```

### Functions
```python
load_skill_index(skills_dir: Path) -> dict[str, SkillMeta]       # L24-43: Load _index.toml
select_skills(prompt, source_type, user_resource_types, skill_index, is_admin=True) -> list[str]  # L46-89
compute_skills_fingerprint(skills_dir: Path) -> str               # L92-105: SHA-256, first 12 hex chars
load_skills_changelog(skills_dir: Path) -> str | None             # L108-114: CHANGELOG.md
load_skills(skills_dir: Path, skill_names: list[str]) -> str      # L117-130: Concatenate skill docs
```

### Selection Logic (`select_skills`, L46-89)
Skills with `admin_only=True` are skipped when `is_admin=False`.
A skill is selected if ANY of these match:
1. `meta.always_include == True`
2. `source_type in meta.source_types`
3. Any `user_resource_types ∩ meta.resource_types`
4. Any `meta.keywords` found in `prompt.lower()`
5. Any `meta.file_types` match attachment extensions
6. `meta.companion_skills` of already-selected skills are pulled in (respects admin_only + dependency checks)

**Pre-transcription**: Before skill selection, `_pre_transcribe_attachments()` in executor.py transcribes audio attachments and enriches `task.prompt` with the spoken text. This allows keyword-based skills to match on voice memo content.

Returns sorted list of skill names.

## Skill Index (`config/skills/_index.toml`)

| Skill | always_include | keywords | resource_types | source_types |
|---|---|---|---|---|
| `files` | yes | — | — | — |
| `sensitive-actions` | yes | — | — | — |
| `memory` | yes | — | — | — |
| `scripts` | yes | — | — | — |
| `memory-search` | yes | — | — | — |
| `email` | — | email, mail, send, inbox, reply, message | email_folder | email |
| `calendar` | — | calendar, event, meeting, schedule, appointment, caldav | calendar | briefing |
| `todos` | — | todo, task, checklist, reminder, done, complete | todo_file | — |
| `tasks` | — | subtask, queue, background, later | — | — | admin_only |
| `markets` | — | market, stock, stocks, ticker, index, indices, futures, ... | — | briefing |
| `notes` | — | — | notes_file | briefing |
| `reminders` | — | remind, reminder, remind me, alert me, notify me, don't forget, ... | — | — |
| `schedules` | — | schedule, recurring, cron, daily, weekly, ... | — | — |
| `nextcloud` | — | share, sharing, nextcloud, permission, access | — | — |
| `browse` | — | browse, website, scrape, screenshot, url, http, ... | — | — |
| `briefing` | — | — | — | briefing |
| `briefings-config` | — | briefing config, briefing schedule, ... | — | — |
| `heartbeat` | — | heartbeat, monitoring, health check, alert, ... | — | — |
| `accounting` | — | accounting, ledger, invoice, expense, tax, ... | ledger, invoicing | — |
| `transcribe` | — | transcribe, ocr, screenshot, scan, ... | — | — |
| `whisper` | — | transcribe, whisper, audio, voice, speech, dictation, recording, voice memo | — | — |
| `developer` | — | git, gitlab, repo, repository, commit, branch, MR, ... | — | — |

## Skill CLI Modules (`src/istota/skills/`)

### `accounting.py` - Beancount + Invoicing CLI
**Subcommands**: `list`, `check`, `balances`, `query`, `report`, `lots`, `wash-sales`, `import-monarch`, `sync-monarch`, `add-transaction`, `invoice` (sub: `generate`, `list`, `paid`, `create`)
**Env vars**: `LEDGER_PATH`, `LEDGER_PATHS` (JSON), `ACCOUNTING_CONFIG`, `INVOICING_CONFIG`, `ISTOTA_DB_PATH`, `ISTOTA_USER_ID`
**Key fns**: `_run_bean_check()`, `_run_bean_query()`, `cmd_sync_monarch()`, `cmd_invoice_generate()`, `cmd_invoice_list()`, `cmd_invoice_paid()`, `cmd_invoice_create()`, `cmd_add_transaction()`

### `email.py` - IMAP/SMTP
**Subcommands**: `send`, `output`
**Env vars**: `IMAP_HOST/PORT/USER/PASSWORD`, `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM`, `ISTOTA_TASK_ID`, `ISTOTA_DEFERRED_DIR`
**Key fns**: `list_emails()`, `read_email()`, `send_email()`, `reply_to_email()`, `search_emails()`, `get_newsletters()`, `delete_email()`, `cmd_output()`

### `calendar.py` - CalDAV
**Subcommands**: `list`, `create`, `delete`
**Env vars**: `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
**Key fns**: `get_caldav_client()`, `get_calendars_for_user()`, `get_events()`, `create_event()`, `delete_event()`, `update_event()`

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

### Library-Only Modules (no CLI)
- `files.py` - Nextcloud file ops (mount-aware, rclone fallback)
- `invoicing.py` - Invoice generation, PDF export, cash-basis income

## How to Add a New Skill

### 1. Create the skill doc
Create `config/skills/<name>.md` with reference documentation for Claude.

### 2. Register in `_index.toml`
```toml
[my-skill]
description = "What it does"
keywords = ["trigger", "words"]        # Optional
resource_types = ["my_resource"]       # Optional
source_types = ["briefing"]            # Optional
always_include = false                 # Default
```

### 3. (Optional) Create CLI module
Create `src/istota/skills/<name>.py`:
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
