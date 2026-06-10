---
name: health
triggers: [health, weight, bloodwork, labs, lab results, biomarker, biomarkers, panel, blood pressure, heart rate, body fat, cholesterol, glucose, bmi, vitals, body temp, body temperature, resting hr, spo2, sleep, sleep score, stress, body battery, steps, hrv, vo2, vo2 max, garmin, encounter, doctor, doctor visit, procedure, screening, hospitalization, diagnosis, diagnosed, condition, medical history, icd10, chronic, immunization, immunizations, vaccine, vaccines, vaccinated, vaccination, shot, booster, flu shot, tdap, mmr, shingles, hpv, covid shot, travel vaccine, mychart vaccines]
description: Health tracking — body stats, bloodwork panels, biomarker trends, lab analysis, and Garmin daily summaries.
cli: true
env: [{"var":"HEALTH_DB_PATH","from":"setup_env"}]
---

# Health Skill

Body-stats time series, bloodwork panels, biomarker trends, and lab result tracking. Per-user SQLite at `{workspace}/health/data/health.db`. All values stored metric (kg, cm, °C, mmHg, bpm); the display layer converts to the user's preferred units.

## When to use

- The user logs a measurement ("I weigh 82.5 kg", "BP 128/82", "resting HR 60").
- The user uploads or mentions lab/bloodwork results.
- The user asks how a biomarker is trending, what's flagged, or for a health summary.
- Recording or retrieving any vital sign, weight, or biomarker over time.

Pair with `transcribe` / `whisper` when the user submits a photo of a lab report or speaks measurements.

## CLI

`istota-skill health --help` shows the live arg list. All output is JSON. Writes are deferred under sandbox (the scheduler applies the ops post-task).

```bash
# Body stats
istota-skill health log weight 82.5                       # canonical units assumed (kg)
istota-skill health log weight 182 --unit lb              # converts to kg
istota-skill health log resting_hr 60
istota-skill health log blood_pressure_systolic 128
istota-skill health log blood_pressure_diastolic 82
istota-skill health log body_fat_pct 18.5 --date 2026-05-08
istota-skill health stats --metric weight --since 2026-01-01 --limit 30
istota-skill health latest                                # latest value per metric

# Bloodwork
istota-skill health panels --since 2026-01-01 --limit 10
istota-skill health panel 12                              # show panel + biomarkers
istota-skill health add-panel --drawn-at 2026-05-08 --lab Kaiser --type CBC
istota-skill health add-biomarker 12 Hemoglobin 14.8 g/dL --ref-low 13.5 --ref-high 17.5
istota-skill health add-biomarker 12 WBC 12.5 10^3/uL --flag H

# Adding a panel and its biomarkers in ONE sandboxed task: the panel id
# doesn't exist yet (the write is deferred), so give the panel a --ref name
# and reference it as @name from add-biomarker instead of a numeric id.
istota-skill health add-panel --drawn-at 2026-05-08 --lab Kaiser --type CBC --ref cbc
istota-skill health add-biomarker @cbc Hemoglobin 14.8 g/dL --ref-low 13.5 --ref-high 17.5
istota-skill health add-biomarker @cbc WBC 12.5 10^3/uL --flag H

istota-skill health trend Cholesterol_Total --since 2026-01-01
istota-skill health upload /path/to/lab.pdf --drawn-at 2026-05-08 --lab Kaiser

# Bulk CSV (Date,Lab,Marker (unit) layout)
istota-skill health import-csv /path/to/bloodwork.csv             # skip duplicate (date, lab) panels
istota-skill health import-csv /path/to/bloodwork.csv --on-collision replace
istota-skill health export-csv --output /tmp/bloodwork.csv        # all confirmed panels

# Dashboard snapshot
istota-skill health summary

# Profile + display preferences
istota-skill health settings
istota-skill health set dob 1985-03-12
istota-skill health set height 178                         # cm; accepts "5ft10in" / "70in" too
istota-skill health set sex M
istota-skill health set display.weight lb
istota-skill health set display.temp F

# Medical history — encounters and diagnoses
istota-skill health encounters --since 2025-01-01
istota-skill health encounter 3
istota-skill health add-encounter --date 2026-05-13 --type procedure \
    --provider "Dr. Smith" --facility "Kaiser Sunset" \
    --specialty gastroenterology --reason "Screening colonoscopy" \
    --notes "Grade I-II internal hemorrhoids found. No polyps."
istota-skill health update-encounter 3 --notes "Follow-up in 3 years"
istota-skill health delete-encounter 3

istota-skill health diagnoses --status active
istota-skill health diagnosis 7
istota-skill health add-diagnosis "Internal hemorrhoids" \
    --date-diagnosed 2026-05-13 --encounter-id 3 \
    --severity mild --icd10 K64.0
istota-skill health resolve-diagnosis 7 --date 2026-06-15
istota-skill health update-diagnosis 7 --status chronic
istota-skill health delete-diagnosis 7

istota-skill health history-summary                       # new-doctor packet

# Immunizations
istota-skill health immunizations [--name Influenza] [--since 2020-01-01]
istota-skill health immunization 12                       # show one record
istota-skill health add-immunization --name Influenza --date 2025-11-28 \
    --product-name "Fluzone trivalent" --manufacturer Sanofi \
    --site "left deltoid" --route IM --facility "CVS Pharmacy" \
    --notes "Annual 2025-26"
istota-skill health update-immunization 12 --lot-number ABC123
istota-skill health delete-immunization 12

istota-skill health vaccine-refs                          # bundled canonical list
istota-skill health coverage                              # status per ref
istota-skill health coverage --due-soon                   # filter
istota-skill health coverage --overdue

istota-skill health import-immunizations --paste @clipboard.txt --dry-run
istota-skill health import-immunizations --paste @clipboard.txt --confirm

istota-skill health explain-immunization Influenza        # educational primer

# Garmin (initial connect happens in the web UI — health settings page)
istota-skill health garmin-status
istota-skill health garmin-sync --days-back 7
istota-skill health garmin-disconnect
```

## Metric keys

Canonical names (use these for `log`):

| Key | Unit | Notes |
|---|---|---|
| `weight` | `kg` | `lb` input is converted at log time |
| `resting_hr` | `bpm` | |
| `blood_pressure_systolic` | `mmHg` | Paired with diastolic |
| `blood_pressure_diastolic` | `mmHg` | |
| `body_fat_pct` | `%` | |
| `body_temp` | `°C` | `°F` input is converted at log time |
| `respiratory_rate` | `brpm` | |
| `blood_oxygen` | `%` | SpO2 |
| `sleep_duration_min` | `min` | Garmin daily; total sleep time |
| `sleep_score` | `score` | Garmin daily; 0–100 composite |
| `sleep_deep_min` / `sleep_light_min` / `sleep_rem_min` / `sleep_awake_min` | `min` | Garmin sleep stages |
| `stress_avg` / `stress_max` | `score` | Garmin daily 0–100 |
| `body_battery_high` / `body_battery_low` | `score` | Garmin daily 0–100 |
| `steps` | `steps` | Garmin daily total |
| `active_calories` | `kcal` | Garmin (excl. BMR) |
| `spo2_avg` | `%` | Garmin overnight average |
| `hrv_status` | `ms` | Garmin RMSSD |
| `vo2_max` | `ml/kg/min` | Garmin estimate |
| `respiration_avg` | `brpm` | Garmin waking average |

Height is **not** a stat — it's a single value in `settings` (`health set height …`). BMI is derived on read from latest weight + settings height.

## Biomarker naming

Use canonical names where possible (`Hemoglobin`, `LDL`, `HDL`, `Cholesterol_Total`, `TSH`, `WBC`, `Glucose`, …). The skill normalises common aliases (`Hgb` → `Hemoglobin`) automatically. If the lab uses a name we don't recognise, pass it through verbatim — the trend command matches on canonical name first, then aliases.

`flag` is `H` (high), `L` (low), or `C` (critical). Routes auto-compute `H`/`L` against Istota's canonical reference range (sex-aware when `sex` is set); `C` is only ever respected from the lab.

## Talk patterns

| User says | Run |
|---|---|
| "I weigh 82.5 kg" | `log weight 82.5` |
| "weight is 182 lbs" | `log weight 182 --unit lb` |
| "BP 128/82" | `log blood_pressure_systolic 128` then `log blood_pressure_diastolic 82` |
| "Resting HR 60" | `log resting_hr 60` |
| "What's my latest weight?" | `latest` and format the weight entry |
| "How's my cholesterol?" | `trend Cholesterol_Total` |
| "Show me my bloodwork history" | `panels` |
| "Any biomarkers out of range?" | `summary` — surface entries from `alerts` |
| "Here are my lab results" (+ image) | OCR via `transcribe`/`whisper`, then `add-panel` + `add-biomarker` per row |
| "I saw the GI doctor today, colonoscopy was clean" | `add-encounter --date 2026-05-15 --type procedure --specialty gastroenterology --reason "Colonoscopy" --notes "Clean, no findings"` |
| "Diagnosed with X today" | `add-diagnosis "X" --status active --date-diagnosed 2026-05-15` (plus `add-encounter` if visit details given) |
| "The hemorrhoids cleared up" | `resolve-diagnosis <id> --date 2026-05-15` |
| "What are my active conditions?" | `diagnoses --status active` |
| "When was my last eye exam?" | `encounters --since … --type screening` and filter |
| "Give me a summary for my new doctor" | `history-summary` |
| "Got my flu shot today" | `add-immunization --name Influenza --date 2026-05-16` |
| "Tdap booster 2 weeks ago at the pharmacy" | `add-immunization --name Tdap --date 2026-05-02 --facility "pharmacy"` |
| "Here's my MyChart vaccine list: …" | `import-immunizations --paste @inline --dry-run` then `--confirm` after the user reviews the parsed rows |
| "Am I due for anything?" | `coverage --due-soon` and `coverage --overdue` |
| "When was my last tetanus?" | `immunizations --name Tdap --limit 1` |
| "What's the deal with Shingrix?" | `explain-immunization Shingles` |
| "Show me everything I've had" | `immunizations --limit 500` |
| "Sync my Garmin data" | `garmin-sync --days-back 7` |
| "Is my Garmin connected?" | `garmin-status` |
| "How did I sleep last night?" | `latest` — read `sleep_duration_min` / `sleep_score` from Garmin rows |
| "Steps yesterday?" | `stats --metric steps --limit 1` |

When the user uploads a photo or PDF of a lab report through the web UI, the upload pipeline runs the OCR + LLM extraction automatically and the user confirms the extraction in the web UI. From a Talk message with an image attachment, you can transcribe the image and call `add-panel` + `add-biomarker` directly, or recommend they upload via the web UI for the full review-and-edit flow.

## Privacy

Health data is the most sensitive data in the system. The health DB is the single source of truth — quantitative health data must not be duplicated elsewhere.

- Never write measurements, biomarker values, medication doses, lab dates, or current symptoms to USER.md, the knowledge graph, dated memories, or KV. Those stores are not scoped for clinical data and surface in unrelated prompt contexts.
- Don't include biomarker values in news / briefings / log channels.
- When another skill or response needs a current value, call `istota-skill health latest` or `health trend NAME` at the moment of use rather than caching the number.
- Stable identity-level medical facts (allergies, named chronic conditions) stay in the knowledge graph via the `memory` skill — see its classification rules. The detailed encounter/diagnosis registry stays in the health DB; only the identity-level fact (`has_condition: hypertension`) belongs in the KG.
- Source files for uploaded labs are only served through the auth-gated `/panels/{id}/source` route, never via static file serving.
