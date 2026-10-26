---
paths:
  - "src/istota/apps/**"
  - "web/src/apps/**"
  - "web/src/lib/apps/**"
  - "web/src/lib/components/apps/**"
  - "web/src/routes/apps/**"
  - "web/src/routes/settings/apps/**"
  - "web/tests-apps-escape/**"
  - "tests/fixtures/apps/**"
---

# Apps

An app is untrusted code the user chose to run: a folder in their workspace with a `manifest.toml` and an `index.html`, installed into the Apps section and served as one self-contained document inside a sandboxed iframe. It is written by the user or by Istota, reviewed only by the user on the approval screen, and may be buggy or written under injected content. The model has three parts: **the browser is the isolation** (an opaque-origin frame with the CSP below), **the server is the enforcement point** (every call is checked against the installed version's grants), and **the shell is only the relay**. Nothing an app does is trusted — not its manifest, not its code, not what it reports about itself.

## The rules a change must keep

- **The frame's `sandbox` attribute is exactly `allow-scripts`.** Never `allow-same-origin` (it would put the app on Istota's origin with the user's session), and none of the popup, download or top-navigation flags. The exact attribute is asserted in `AppFrame.svelte.test.ts`, and the escape tier frames the real document with it.
- **The installed document has no subresources.** `apps.document.build_document` inlines every referenced script and stylesheet and the house kit, and the CSP pins the sha256 of each emitted script, so the policy admits exactly the code the user approved. `connect-src 'none'`, `frame-src 'none'`, `frame-ancestors 'self'`, `sandbox allow-scripts`. A script containing `</script` or `<!--` is refused, not escaped, so the hash always covers what runs.
- **The kit is inserted before any element can open** — straight after the doctype, or at offset 0 — so its guard runs before any app script. A following `<html>` or `<head>` in the source merges into the open one.
- **The bridge is bound to the installed document, not the frame's window.** The shell hands one `MessagePort` on the frame's first `load`, and nothing is answered or sent on it until the kit echoes its one-time key in a `hello`. The kit makes that key in its own closure and posts it to the parent (`{istota: 1, kind: 'key', k}`) while it is parsed, before any app script runs; the bridge, created before the frame gets its `src`, takes only the first message whose `source` is the frame's window, and if that was not a well-formed key no hello is ever taken. The key is never in the URL or anywhere else the frame's realm can read back: a fragment key was readable from the navigation timing entry in every browser and from an error stack in Firefox, so app code could forward it to a page it navigated to. A page that navigated the frame before that load is handed the port but not the key, so its hello fails and its calls are dropped; a hello that never arrives makes the shell treat the frame as navigated.
- **Only the current version runs.** The server refuses every call with `stale_version` unless `version` is the app's current version, so a frame left open across an upgrade, a rollback or an uninstall cannot keep using its old grants. Grants are stored per version and roll back with it.
- **Nothing runs from the draft.** Only a pending install the host built from the folder, and a version the user approved, is served; the draft never runs.
- **Every call is checked on the server**: the version, then the grant for that version, then the capability's scope (the KV key prefix, the file folder), then the rate limit, then recorded in `app_calls`. The shell adds only the app id and version. There is no "run this command" capability, no free-form shell, no raw SQL.
- **`app.` KV values are fenced as untrusted when the agent reads them.** The `kv` skill fences any per-user namespace under `app.`, so text an app wrote cannot reach a prompt as instructions.
- **Caps come from the `[apps]` config block and have no per-app override**: the built document, the KV value and namespace size, and the app's `data/` folder.
- **Rerun the escape tier after any change to the frame, its CSP, the kit's guard or the bridge**: `npm --prefix web run test:apps-escape`, then `scripts/test-apps-escape-negative-control.sh`, which must go red in all three browsers. The recorder is the leak verdict, not Playwright's request events (Chromium emits an event for a CSP-blocked request the network never saw).

## Capabilities

| Capability | Gives | Granted |
|---|---|---|
| `kv:own` | the KV namespace `app.<id>` | every app |
| `files:own` | `{bot_dir}/apps/<id>/data/` | every app |
| `kv:read:<ns>` | another of the user's KV namespaces | asked |
| `files:read:<path>` | a subtree of the user's files | asked |
| `calendar:read[:<name>]` | all owned calendars, or one by name | asked |
| `skill:<skill>.<op>` | one allowlisted read-only operation | asked |

`skill:` operations live in `apps/skill_caps.py`, whose `SKILL_CAPS` keys are `capabilities.SKILL_OPERATIONS` and whose `sensitive` flag is `is_sensitive`, so a seventh operation is one entry there. Each declares its arguments (`check_args` refuses any other key) and calls the library function the skill's CLI calls, sharing its serializer (`markets.quote_to_dict`, `memory_search.fact_to_dict`); its op is `call`, the stored grant must name the same operation, and a skill in the instance's or the user's `disabled_skills`, or a module off for the user, answers `not_found`. A handler writes nothing: a missing module database is an empty answer.

Read-only by default. `kv:read` excludes reserved namespaces and everything under `app.`; `files:read` excludes a first segment of `{bot_dir}` or `memories` and any dot segment. A version that requests any capability beyond the current version's grants needs the full approval screen, and the server requires the client to echo the list of new capabilities back.

## Where things live

- `src/istota/apps/` is a module alongside `feeds/` and `health/`; `apps.db` per user at `module_db_path(user_id, "apps")`, masked from every sandbox.
- The built document is a BLOB in `app_documents`, deduplicated by content hash. KV stays in `istota_kv` under `app.<id>`; the app's files stay under `{bot_dir}/apps/<id>/data/`.
- The kit is built by `web/vite.apps.config.ts` into `web/build-apps/`, found through `webui/static_dir.resolve_apps_dir()`, and served from the version's stored copy, so an unbuilt kit breaks new installs and nothing already installed.
