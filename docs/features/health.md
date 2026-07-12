# Health tracking

Body stats, bloodwork panels, biomarker trends, Garmin Connect daily summaries, immunization registry, and medical history. Per-user SQLite at `{workspace}/health/data/health.db`. All measurements stored metric (kg, cm, °C, mmHg, bpm); the display layer converts to the user's preferred units.

Health is an on-by-default module with per-user opt-out via `disabled_modules` in user settings.

## Features

**Body stats** — Time series for weight, blood pressure, resting HR, body fat %, body temp, respiratory rate, SpO2. Manual logging via skill CLI or auto-populated from Garmin sync. Unit-aware input (accepts lb, °F, etc. and converts at log time). BMI derived from latest weight + profile height.

**Bloodwork** — Panel ingestion from three sources: drag-and-drop OCR upload (PDF/image → LLM extraction with review-and-confirm), CSV bulk import, or manual entry via skill CLI. 60+ canonical biomarkers with sex-specific reference ranges, alias normalization, and auto-flagging (H/L/C). Blood-pressure and resting-HR biomarker rows fan out to the stats time series.

**Biomarker trends** — Per-marker trend charts with out-of-range zones shaded. LLM-generated educational explainer cards for flagged markers (never diagnoses or prescriptions — hard guardrails in the prompt). Explainers cached per-user per `(name, direction)`.

**Garmin Connect** — OAuth connection via the general **Settings → Connected services** page. Garmin is a cross-module connected service, shared with the location track importer — connect once and both features use the same token. Sync runs on demand (the "Sync health data" button on that Garmin card, the `garmin-sync` skill CLI, or a user-configured CRON job), not on an automatic schedule. It pulls sleep (duration, score, stages), stress, body battery, steps, active calories, SpO2, HRV, VO2 max, and respiration. A multi-day backfill is available but not auto-triggered on connect.

**Immunizations** — Registry of administered vaccines with date, product, manufacturer, lot, site, route, and facility. Bundled canonical vaccine reference list with recommended schedules. Coverage tracker shows due-soon and overdue immunizations. Bulk import from MyChart/clipboard paste with dry-run preview. Static educational explainers per vaccine.

**Medical history** — Encounters (doctor visits, procedures, screenings, hospitalizations) and diagnoses (active, resolved, chronic) with ICD-10 codes. `history-summary` command generates a new-doctor packet.

## Setup

Health requires no additional configuration — it's enabled by default for all users. To disable for a specific user, add `health` to their `disabled_modules` list in user settings or via the web UI Preferences page.

Install optional dependencies for full functionality:

```bash
uv sync --extra all
# or specifically for OCR:
uv sync --extra transcribe   # pytesseract + Pillow
```

OCR upload also requires `pdftotext` (from poppler-utils) for PDF text extraction.

## Database

Per-user SQLite at `{workspace}/health/data/health.db`. Tables:

| Table | Purpose |
|---|---|
| `stats` | Body stat time series (metric, value, unit, date, source) |
| `panels` | Bloodwork panels (drawn_at, lab, type, draft/confirmed, content_hash) |
| `biomarkers` | Individual biomarker results linked to panels |
| `biomarker_explainers` | Cached LLM explainer text per (name, direction) |
| `biomarker_refs` | Bundled canonical biomarker reference ranges and aliases |
| `immunizations` | Vaccine administration records |
| `immunization_refs` | Bundled canonical vaccine reference list and schedules |
| `encounters` | Medical encounters (visits, procedures, screenings) |
| `diagnoses` | Diagnoses with status (active, resolved, chronic) |
| `health_settings` | Key/value store for profile (DOB, height, sex) and unit display preferences |

Garmin Connect OAuth tokens are not stored here — they live in the framework-level encrypted `secrets` table under `service="garmin"` (Fernet via `ISTOTA_SECRET_KEY`).

## Web pages

| Path | Content |
|---|---|
| `/health/stats` | Netdata-style sparkline grid for all body stats |
| `/health/bloodwork` | Dates-as-rows × markers-as-columns spreadsheet with category bands |
| `/health/bloodwork/panel?id=…` | Panel detail with inline-edit table and source preview |
| `/health/bloodwork/upload` | Drag-and-drop OCR review-and-confirm |
| `/health/bloodwork/marker?name=…` | Trend chart, related markers, clinical description, explainer card |
| `/health/immunizations` | Registry table, coverage status, import controls |
| `/health/settings` | DOB/height/sex, display preferences (Garmin connect/sync lives on Settings → Connected services) |

## Skill CLI

The `health` skill exposes `istota-skill health <subcommand>`. Key subcommands:

- `log`, `stats`, `latest` — body stat CRUD and queries
- `panels`, `panel`, `add-panel`, `add-biomarker` — bloodwork management
- `trend`, `summary` — biomarker analysis
- `upload`, `import-csv`, `export-csv` — bulk data operations
- `settings`, `set` — profile and display preferences
- `encounters`, `add-encounter`, `update-encounter`, `delete-encounter` — medical visits
- `diagnoses`, `add-diagnosis`, `resolve-diagnosis`, `update-diagnosis`, `delete-diagnosis` — conditions
- `immunizations`, `add-immunization`, `update-immunization`, `delete-immunization` — vaccine records
- `vaccine-refs`, `coverage`, `explain-immunization` — reference data and coverage
- `import-immunizations` — bulk import from clipboard/paste
- `garmin-status`, `garmin-sync`, `garmin-disconnect` — Garmin integration

All mutating operations are deferred under sandbox (written to `task_<id>_health_ops.json`, replayed post-task by the scheduler).

## Privacy

Health data is the most sensitive data in the system. The health DB is the single source of truth.

- Quantitative health data (measurements, biomarker values, lab dates, symptoms) must never be written to USER.md, the knowledge graph, dated memories, or KV.
- Biomarker values are excluded from briefings and log channels.
- Source files for uploaded labs are served only through the auth-gated `/panels/{id}/source` route.
- Stable identity-level medical facts (allergies, named chronic conditions) belong in the knowledge graph; detailed records stay in the health DB.
