# Per-user configuration

Per-user data lives in three DB tables and (optionally) the user's Nextcloud workspace:

1. **DB tables** (authoritative)
   - `user_profiles` — display_name, timezone, channels, worker overrides, email lists, trusted senders, quiet senders, disabled_skills, **disabled_modules**, **delivery routing** (`default_destination` + a purpose-keyed `routing` table), `default_briefings`, `briefing_email_html`, `timezone_follow_location`, `outbound_approval`, `external_turn_display`
   - `user_resources` — folder mounts (`folder`) and internal `shared_file` organizer state. Only `folder` is declarable after the Resources sunset; the other path-shaped types were retired (calendars are CalDAV-discovered, todo/reminders/notes are workspace-convention files).
   - `briefing_configs` — briefing schedules. `enabled=0` mutes a briefing without deletion.
   - `secrets` — Fernet-encrypted credentials (Karakeep, Monarch, Tumblr, Overland ingest token, ntfy, etc.). See [credentials](credentials.md) for the full per-user inventory.
2. `[users.alice]` block in main `config/config.toml` (the docker entrypoint path) — DB rows still win at config-load time.
3. User workspace files in Nextcloud (`PERSONA.md`, `CRON.md`, `HEARTBEAT.md`, `TASKS.md`, `USER.md`).

> The legacy `config/users/{user_id}.toml` file (and its `.user.json` overlay) was retired with the OIDC retirement / Phase 7 sweep. The `config/users/` directory is gone, Ansible no longer renders per-user TOML, and `Config.users_dir` / `load_user_configs()` no longer exist.

The DB rows are populated four ways:

- **Ansible**: `istota user|resource|briefing|secret ensure …` — each idempotent and prints `STATE: created|updated|noop` for `changed_when` semantics.
- **Web UI**: `/istota/settings` (Profile + Connected services + module pages) and the per-feature settings under `/istota/{feeds,money,location}/settings`.
- **Auto-seed**: on first OAuth login the profile row is created from the Nextcloud display_name and any `[users.X]` block. Subsequent logins do not overwrite values the user has edited.
- **TOML migration**: on scheduler startup, `import_from_user_configs` (one each for profiles / resources / briefings) seeds DB rows from any remaining `[users.X]` block whose natural key isn't already present.

## Per-user TOML settings

```toml
# main config.toml: [users.alice]
display_name = "Alice"
email_addresses = ["alice@example.com", "alice.work@company.com"]
timezone = "America/New_York"

# Per-user worker limits (0 = use global default)
max_foreground_workers = 2
max_background_workers = 1

# Skills to exclude for this user
disabled_skills = ["markets"]

# Verbose tool logging to a dedicated Talk room
log_channel = "room456"

# Talk room for confirmations and security alerts
alerts_channel = "room789"

# Trusted email addresses, in both directions: their mail bypasses the inbound
# confirmation gate, and mail to them is sent without waiting for approval.
# Supports fnmatch patterns.
trusted_email_senders = ["*@company.com", "boss@other.com"]

# This user's outbound email approval policy: off | untrusted | all.
# Omit (or "") to follow [email] outbound_approval_floor, which is a minimum —
# a value weaker than it has no effect. Seed-only: read when the user has no
# profile row yet. For an existing user the DB row wins, so set it with
# `istota user ensure --outbound-approval`.
outbound_approval = "all"

# How much of a turn that arrived from outside the room (an external contact's
# email) shows inline in web chat: full | collapsed (default) | hidden.
# The turn itself always renders. Seed-only, as above.
external_turn_display = "collapsed"

# Modules to opt out of (default-on otherwise)
disabled_modules = ["money"]

# Default delivery surface for results/notifications when nothing else applies
default_destination = "talk"   # talk | email | ntfy | web | surface:channel | comma list

# Where replies to inbound email threads are delivered
email_reply_routing = "origin+thread"   # origin+thread (default) | origin | thread

# Purpose-keyed routing table — overrides default_destination per purpose.
# Purposes: reply, alert, log, briefing, notification
[users.alice.routing]
alert = "ntfy"                 # heartbeat + security alerts go to ntfy
log = "web:<room-token>"       # verbose execution log streamed to a web chat room
```

> ntfy push notifications are **not** a profile field. They live in the encrypted `secrets` table — provision via the web UI (`/istota/settings` → Connected services → ntfy push) or `istota secret ensure --user alice --service ntfy --key topic --value …`.

### Resources (folder mounts)

After the Resources sunset, the only declarable resource type is `folder` —
an out-of-workspace path mounted into the sandbox (a cross-user share, an
absolute path elsewhere). In-workspace paths are already covered by the
wholesale user-dir bind, so a `folder` row only does real work for paths
outside `Users/<id>/`. Provision via Ansible (`istota resource ensure
--user alice --type folder --path /shared/Projects --name Projects`) or the
`[[users.X.resources]]` TOML block. Calendars are CalDAV-discovered;
todo/reminders/notes read an explicit path (a briefing-source `path`, or a
deprecated `todo_file`/`reminders_file` resource override) — there is no
convention-default filename, and the `notes/` folder is prompt guidance for
the model only; email folders have no consumer. `calendar`, `notes_folder`,
`email_folder`, and the module/credential types are auto-cleaned from
`user_resources` on scheduler startup; `todo_file`/`reminders_file` are left
in place as deprecated overrides (removed by hand when the user migrates).

```toml
[[users.alice.resources]]
type = "folder"
path = "/shared/Projects"
name = "Projects"
permissions = "write"
```

`folder` is the only declarable resource type (plus the internal `shared_file` organizer state). `permissions` is `read` (default) or `readwrite` — no other value grants writes.

> **Modules vs resources vs connected services.** The retired `feeds` / `money` / `monarch` / `karakeep` / `overland` resource types were split apart in the modules / connected services refactor:
> - **Modules** (`feeds`, `money`, `location`, `health`, `briefings`) are on by default; opt out per user via `disabled_modules`. Module-owned secrets (Tumblr API key, Monarch session, Overland ingest token) live on the per-module settings page.
> - **Connected services** (`karakeep`, `google_workspace`) are external API credentials in the encrypted `secrets` table.
> - The scheduler auto-cleans the obsolete resource types from `user_resources` on startup; their TOML extras are migrated into `secrets` via `secrets_store.import_from_user_configs`.

CalDAV calendars are auto-discovered from Nextcloud and don't need to be configured as resources.

### Briefings

Schedule and delivery are provisioned with `istota briefings schedule ensure --user alice --name morning --cron "0 6 * * *" --conversation-token room123 --output both`, or from the web UI (briefings tab → settings). The `[[users.X.briefings]]` TOML block is a docker-entrypoint shortcut.

Content is an ordered list of **blocks**, each with 1..N sources — the boolean `components` model and its `--component` / `--components-json` flags are retired. Config-authored blocks are seeded **once** into the user's briefings module DB as an editable baseline, after which web edits win and operator re-runs never clobber them.

```toml
[[users.alice.briefings]]
name = "morning"
cron = "0 6 * * *"
conversation_token = "room123"
output = "both"

  [[users.alice.briefings.blocks]]
  title = "Today"
  render_mode = "structured"

    [[users.alice.briefings.blocks.sources]]
    kind = "calendar"
    config = {}

    [[users.alice.briefings.blocks.sources]]
    kind = "todos"
    config = { path = "istota/TODO.md" }
```

Blocks and sources are also editable from the CLI (`istota briefings blocks|sources …`) and by the bot itself through the `briefings` skill. See [briefings](../features/briefings.md) for details.

### Delivery routing

Each user has a default delivery surface (`default_destination`, defaults to `talk`) plus an optional purpose-keyed `routing` table that overrides it per purpose. The purposes are `reply`, `alert`, `log`, `briefing`, and `notification`; each maps to an `output_target` descriptor (`talk`, `email`, `ntfy`, `web`, `surface:channel`, or a comma list). Routing notifications by purpose (e.g. `alert = "ntfy"`) is what reroutes heartbeat and security alerts off Talk; the `log` purpose drives the verbose per-task execution log to any user-routable surface (it supersedes the legacy `log_channel` shorthand). `web` is a routable delivery surface — alerts, the execution log, and notifications routed to it land in a web chat room as system messages.

Provision via the CLI:

```bash
istota user ensure -u alice \
  --default-destination email \
  --route alert=ntfy \
  --route log=web:<room-token>
```

`--route` is repeatable and validates the purpose against the allowed set. The web Preferences card surfaces `default_destination`, the `alert` route, and the `log` route; CLI-set routes for the other purposes are preserved on round-trip.

## User workspace files

These files live in the user's Nextcloud folder at `/Users/{user_id}/{bot_dir}/config/` and can be edited through the Nextcloud web UI:

| File | Purpose | See |
|---|---|---|
| `USER.md` | Persistent memory (auto-loaded into prompts) | [Memory](../features/memory.md) |
| `TASKS.md` | File-based task queue with status markers | [Scheduling](../features/scheduling.md) |
| `PERSONA.md` | Personality customization (overrides global) | [Persona](persona.md) |
| `CRON.md` | Scheduled jobs (markdown + TOML) | [Scheduling](../features/scheduling.md) |
| `HEARTBEAT.md` | Health monitoring checks | [Heartbeat](../features/heartbeat.md) |
| `skills/<name>.md` | Per-skill instruction overlay (one file per skill) | [below](#per-skill-overlays) |

### TASKS.md format

```markdown
# Tasks
- [ ] Send email to john about the meeting tomorrow
- [~] Checking calendar for tomorrow's schedule...
- [x] 2025-01-26 12:34 | Summarized report | Result: Summary saved to exports/
- [!] 2025-01-26 12:35 | Failed task | Error: timeout (attempt 2/3)
```

Status markers: `[ ]` pending, `[~]` in progress, `[x]` completed, `[!]` failed.

### Per-skill overlays

A user can customize one skill's instructions without forking the skill. Put a markdown file at `config/skills/<skill-name>.md` and its contents are appended to that skill's body every time the skill loads — both when the skill is selected eagerly and when the bot pulls it from the on-demand menu with `istota-skill skills show`.

```
/Users/alice/istota/config/skills/
├── calendar.md
├── developer.md
└── notes.md
```

The text lands at the end of the skill's section under a `#### alice's configuration for this skill` heading, followed by a line saying these instructions take precedence over the skill's own wherever the two conflict. That is the whole precedence mechanism: position in the prompt, plus the label. There is no resolver.

**Additive, not replace.** This is the difference from the operator override at `config/skills/<name>/skill.md` in the *deployment's* config directory, which substitutes the whole document. An overlay adds three lines and keeps taking upstream edits to the bundled skill; an override means hand-merging every upstream change forever. Dropping a forked skill document into the overlay path instead puts two contradictory bodies in one prompt, which is what the size cap below exists to catch.

Rules:

- Plain markdown. Bullets, prose, fenced code blocks — whatever the instruction needs. YAML frontmatter is stripped if present.
- No `# ` or `## ` headings. The overlay is rendered under a `#### ` label of its own, and a heading that shallow ends that block and floats the rules up as a new section peer to the whole skills reference, detached from the skill they configure. One written anyway is demoted to `#### ` at load time rather than dropped, so a hand-edited file misbehaves visibly; `istota doctor` warns about it. `### ` and deeper are fine.
- Over 24 KB warns that the cap is close. Over 32 KB is not loaded at all — the file stays on disk and stays editable, but it reaches no prompt. `istota-skill skills overlays` reports both, as a warning and as `binds: false` with `reason: over_cap`.
- `sensitive_actions` and `untrusted_input` accept no overlay. Not a security boundary — the operator override can still replace either document — but a guard against a casual preference line landing in the safety layer.
- A file named for a skill that does not exist is silently never read. Two things report it: `istota-skill skills overlays` for one user, and `istota doctor`'s `config.skill_overlays` check across every user's tree. Doctor fails on a name within a character or two of a real skill, and names the skill it thinks you meant — that is a misspelled overlay, and its rules reach no prompt. A wrong case (`Developer.md`) and a dropped plural (`note.md` for `notes`) both count. A name built around a real skill (`developer.local.md`, `01-developer.md`) fails too, and says which skill it names — distance alone gets blinder the more deliberately a name is decorated, which is the wrong way round. A copy of a real overlay (`notes2.md`, `notes~.md`, `notes.bak.md`) only warns, and so does an unrecognizable name: this directory is writable from inside the sandbox, and a file a task left behind should not hold a deployment alert red. Everything found is listed by name whichever status it holds, including alongside a failure.
- Overlays are read from the workspace mount, so a deployment without one has no overlays. Every failure above degrades to exactly the prompt the skill would have had with no overlay at all.
- Editing an overlay does not move the skills fingerprint, so it does not fire the "skills changed" notice. That notice would otherwise appear and then say nothing about the edit that triggered it.

**Which file a rule belongs in.** A rule in `USER.md` reaches every task. A rule in an overlay reaches only the tasks where that skill loaded, and skill selection is a heuristic — a menu skill's overlay arrives only if the bot decides to pull the skill. So the test is:

> Would it be *wrong* to ignore this rule on a task where the skill was not loaded? If yes → `USER.md`. If it is merely irrelevant there → skill overlay.

A note-naming convention is irrelevant on a task that writes no note, and belongs in the `notes` overlay. "Never write a new file to the base notes folder" is not, because the task that needs to hear it is the one that never recognized itself as a notes task; that stays in `USER.md`. A rule covering two skills is written into both files — there is no include mechanism. A rule covering three or more has failed the test.

Apply the test to the **action** rather than the topic. If what the rule governs can be done with the skill unloaded, the rule is not skill-specific however much it sounds like it, and an overlay is the wrong home because it will not be loaded on the task that gets it wrong. Editing `CRON.md` is the case that looks most like an exception and is not: only the `reminders` and `schedules` skills write that file, but it is an ordinary file in the config directory that any task can edit, and a malformed edit unschedules every job silently — so its rules belong in `USER.md`.

**Editing them.** As files. Open one in the Nextcloud editor, or ask the bot to change it and it will use its file tools. There is no CLI that writes an overlay, deliberately: an overlay is a document you author, and it was tried the other way — the memory CLI carried the same one-bullet-at-a-time ops it uses on `USER.md`, and they could address roughly a fifth of a real overlay. They could not write a paragraph or a code block at all, and removing a bullet orphaned the code block underneath it.

Two read-only commands, on the skills CLI:

```bash
istota-skill skills overlays            # inventory: what is customized, and does each one load
istota-skill skills overlay developer   # print one
```

**Check `binds` after an edit.** That is the whole verification story, and it is a better one than a write-time gate: binding is a property of the file, so one command catches a misspelled skill name, a file over the cap, a denylisted slot and a file holding nothing but frontmatter, and says which in its `reason` field. Nothing warns you at the moment of the edit, because nothing is watching the edit — a hand edit produces no audit entry and no notification anywhere. It cannot corrupt a prompt either; the worst it can do is not load.

Overlay text is indexed for memory search under the `skill_overlay` source type, so a search finds a rule without the user knowing which file holds it. That index is refreshed on a schedule (`scheduler.skill_overlay_reindex_interval`, six hours by default, and once on start-up), by a pass that walks the whole directory — so a rule you added by hand becomes searchable within a few hours rather than immediately, and one you deleted stops being searchable on the same schedule. The nightly curator is shown the inventory — skill names and line counts, never the bodies — so it does not copy a rule back into `USER.md` a week after it moved out.

## Admin vs non-admin

Admin users are listed in `/etc/istota/admins`. Empty file = all users are admin.

Non-admin restrictions:

- Scoped mount path (`/Users/{user_id}` only)
- No subtask creation
- `admin_only` skills filtered out (e.g., tasks, schedules)

Database access is **not** one of the differences. No task of either kind can open a database file — they are masked out of the sandbox for everyone — and every skill CLI that reads one runs host-side scoped by `ISTOTA_USER_ID`, so an admin and a non-admin both get exactly their own rows. `ISTOTA_DB_PATH` used to be exported for admins only, which was not a boundary (the path is derivable) but did break `tasks status`, `memory_search` and `kv` reads for non-admins; it now goes to the skill proxy for every user.
