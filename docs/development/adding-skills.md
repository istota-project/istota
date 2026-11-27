# Adding skills

Skills are self-contained directories under `src/istota/skills/`. Each skill needs a `skill.md` file with YAML frontmatter for metadata and a markdown body for documentation.

## 1. Create the skill directory

```
src/istota/skills/my_skill/
├── skill.md       # Frontmatter metadata + documentation (required)
├── __init__.py    # CLI module (optional)
└── __main__.py    # python -m support (optional)
```

## 2. Write skill.md with frontmatter

All metadata lives in the YAML frontmatter block. The markdown body is the documentation loaded into Claude's prompt.

```yaml
---
name: my_skill
triggers: [my_keyword, another_keyword]
description: One-line description shown in the menu catalogue and `!skills`
cli: true
dependencies: [some-package]
env: [{"var":"MY_API_KEY","from":"secret","service":"my_service","key":"api_key","sensitive":true}]
---

# My Skill

Instructions for Claude on how to use this skill...
```

Use `{BOT_NAME}`, `{BOT_DIR}`, and `{user_id}` placeholders -- they're substituted at load time.

### Frontmatter fields

| Field | Type | Purpose |
|---|---|---|
| `triggers` | list | Documentation-only keywords surfaced by `!skills`; not a selector |
| `description` | string | Shown in the menu catalogue and `!skills` |
| `always_include` | bool | Load for every task |
| `admin_only` | bool | Hidden from non-admin users |
| `cli` | bool | Whether this skill has a CLI module |
| `source_types` | list | Auto-include for these task source types |
| `file_types` | list | Auto-include for these attachment extensions |
| `companion_skills` | list | Pull in these skills when this one is selected |
| `exclude_skills` | list | Remove these skills when this one is selected |
| `dependencies` | list | Python packages required (skip skill if missing) |
| `requires_capability` | list | Gate the skill on a runtime capability (`browser`, `devbox`) being configured |
| `experimental` | bool | Hide unless the operator enables the `skill_<name>` feature flag |
| `exclude_memory` | bool | Skip memory loading for tasks using this skill |
| `exclude_persona` | bool | Skip persona loading |
| `env` | JSON array | Declarative env var specs (see env var sources below) |
| `shared_room` | string | `private` (default) or `safe`. See below |

Boolean fields default to `false`. List fields default to `[]`. Only include fields that differ from defaults.

`shared_room` decides whether a task that withholds everything can still use the skill. A guest's turn in a [shared room](../features/shared-rooms.md), and a task nobody asked in a shared room, withhold every `private` skill. Mark a skill `safe` only if it reads nothing belonging to the user it runs as: no workspace, no credential, no personal data. Today that is `room`, `untrusted_input` and `sensitive_actions`. Any value other than exactly `safe` reads as `private`. See [rooms and multi-user chat](../architecture/rooms.md#what-a-task-may-reach).

There is no `name` field. The directory name *is* the skill's identity — `_loader.py` takes `name=skill_dir.name` and never reads a `name:` key, so writing one has no effect and renaming a skill means renaming its directory.

## 3. (Optional) Create a CLI module

Skills can expose Python CLIs invoked by Claude via `python -m istota.skills.my_skill`:

```python
# __init__.py
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

```python
# __main__.py
from istota.skills._cli import run_skill_main

from . import main

run_skill_main(main)
```

Every `__main__.py` has this one shape, pinned by `tests/test_skill_cli_facade.py`. `run_skill_main` keeps `load_config`'s warnings about the config document off the skill's stderr, then exits with what `main()` returns.

Pattern: `build_parser()` + `main()`, JSON output, credentials via env vars. See [credentials](../configuration/credentials.md) for the two-tier model and how to wire new credentials into the proxy.

## 4. Declare env vars in the manifest

The skill's `env:` block is the **only** place env vars should be wired. The hardcoded credential-injection block in `executor.py` is gone; `build_skill_env()` walks every loaded skill's manifest and resolves each `EnvSpec` against the task's `EnvContext`. The `derive_*` helpers (see [credentials](../configuration/credentials.md#how-credentials-flow-at-runtime)) compute the proxy strip-set, auth map, and lookup allowlist directly from these manifests — no executor edits, no separate `_PROXY_CREDENTIAL_VARS` / `_CREDENTIAL_SKILL_MAP` to keep in sync.

```yaml
env:
  - {"var":"MY_API_KEY","from":"secret","service":"my_service","key":"api_key","sensitive":true}
  - {"var":"MY_API_HOST","from":"config","config_path":"my_section.api_host","when":"my_section.enabled"}
```

For complex setups that need to compute values, write helper scripts, or bind-mount files into the sandbox (see `developer` for a worked example), export `setup_env(ctx) -> dict[str, str]` in the skill's `__init__.py` and use `from: "setup_env"` for the corresponding `var`. The hook fires for the full index regardless of selection, so the skill's helper scripts work even when the skill itself isn't keyword-matched.

## 5. (Optional) Read workspace files

If your skill reads user files (notes, todos, reminders), read them by
**workspace convention** under the user's bot dir
(`{BOT_DIR}/notes/`) via the `files` skill's
mount-aware `read_text`. There is no longer a declarable resource type for
these — the `todo_file` / `reminders_file` / `notes_folder` types were
retired by the Resources sunset. Out-of-workspace folder mounts are the
only remaining declarable resource (`folder`), provisioned by an operator
via `istota resource ensure`.

## Env var sources

| `from:` | Purpose |
|---|---|
| `config` | Dotted config path. Use `when:` (string or list) to gate on truthy paths |
| `secret` | Per-user encrypted secret (`service` + `key` from the `secrets` table) |
| `setup_env` | Value computed by the skill's `setup_env(ctx)` hook in `__init__.py` |
| `template_file` | Auto-create file from a template |
| `user_id` | Literal task `user_id` |

`EnvSpec` flags:

| Flag | Meaning |
|---|---|
| `sensitive: true` | Treat as a credential — strip from Claude's env, route through the proxy. Auto-enrolls the skill in `derive_credential_set` / `derive_skill_credential_map` |
| `proxy_only: true` | Withhold from Claude's env and hand to the proxy, *without* credential semantics: no auto-authorization, no manifest lookup, no per-skill scoping. For non-secret values the model still must not hold — today, paths to SQLite files (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`). Mutually exclusive with `sensitive`; the credential split runs first, so a spec marked both routes as a credential |
| `fallback_var: "FOO"` | Read `os.environ["FOO"]` if primary resolution fails. Honored on the value path only — auto-authorization disables fallbacks so an instance-wide `EnvironmentFile` value can't fan out to per-user auth |
| `gate_has_discovered_calendars: true` | Only resolve when CalDAV discovery returned at least one calendar. A scheduled skill task runs discovery only for a skill that declares a spec with this flag |

## External skill packages

A skill can also live in its own Python distribution, outside this repository: a skill plugin. Istota finds it through an entry point, loads its `skill.md` like any other skill's, and runs its CLI as `python -m <package>`. Once loaded it is an ordinary skill: selection, `disabled_skills`, `admin_only`, `requires_capability`, `experimental`, companions, per-skill credential injection, per-user overlays and `skills show` treat it the same way. Where a plugin is treated differently, the rule is stricter, and every difference is listed below.

On the stack, plugins are wheels in `/srv/app/istota/plugins/src/`, installed by `istota-stack plugins sync` ([Running the stack](../deployment/running-the-stack.md#skill-plugins)), or listed in the management role's `istota_skill_plugins` ([Ansible management role](../deployment/ansible.md)). The Mac app is not supported in this version: its VM runs the same stack, but nothing tests a plugin there and the app has no way to add one.

### The package

A plugin is one top-level regular package, shipped as a pure-Python wheel:

```
example-plugin/
├── pyproject.toml
└── example_plugin/
    ├── __init__.py      # build_parser(), main(); optional setup_env(ctx)
    ├── __main__.py      # the bundled template, unchanged
    └── skill.md         # frontmatter and body, as for a bundled skill
```

`pyproject.toml` registers the skill in the `istota.skills` entry-point group:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "example-plugin"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.entry-points."istota.skills"]
example = "example_plugin"
```

Hatchling ships every file inside the package directory, so `skill.md` needs no package-data table. `uv build --wheel` writes `dist/example_plugin-0.1.0-py3-none-any.whl`.

The loader checks these rules each time it reads the skill index. A plugin that breaks one is left out of the index, and `istota doctor`'s `skills.plugins` names it with the reason ([Doctor checks](../reference/doctor-checks.md#users-and-configuration)):

- **The entry point name is the skill name.** It matches `^[a-z][a-z0-9_]*$` and is none of the reserved names `files`, `memory`, `recall`, `memory_search`, `room_notes` and `web`. Skill names share a namespace with the scopes a shared room withholds by name, so a plugin called `room_notes` would be withheld on every restricted task and take the room's notes folder with it. A `name:` in the frontmatter is ignored, as it is for a bundled skill.
- **A plugin never replaces a bundled skill.** A plugin named like one is refused (`clashes_with_bundled`) and the bundled skill stays. Two plugins with the same name are both refused (`duplicate_plugin`). To change a bundled skill's documentation, use `config/skills/<name>/skill.md`.
- **The entry point value is one top-level package name**: `example_plugin`, never `example_plugin.cli` and never `example_plugin:main`. A plain module and a namespace package are refused too. The loader finds the package without importing it, and a dotted name would run the parent package's `__init__.py` on every load.
- **`skill.md` sits in the package directory.** Without it the plugin is refused. A `skill.toml` there is ignored.
- **`__main__.py` is the bundled template** from step 3: it imports `run_skill_main` from `istota.skills._cli` and calls it with `main`. A `cli: true` plugin without `__main__.py` loads, but its CLI cannot run, and `skills.plugins.entry` warns.
- **The package `__init__` must be cheap and must not raise.** The daemon imports it on every task to look for `setup_env`, in the scheduler process, with the daemon's full environment. A plugin whose import raises is skipped with one ERROR line in the daemon log and not imported again until the daemon restarts, but its top-level code has already run once.
- **No dependencies and no compiled code.** The wheel is installed with `--no-deps`, so a plugin runs on what the image's virtual environment already holds. Write `dependencies = []`, and do not declare `istota` itself. A wheel whose distribution name or package name the image already has is refused at sync, since the image's copy would hide it. So is a wheel that installs anything beside its one package: a second top-level package or module, a script, a `.pth` file or a `sitecustomize.py`. The installed tree is on the path of every Python process in the image, the web process included, and nothing but the plugin's package belongs there. If a plugin needs a library, the library has to become an optional extra of istota first.

### What the manifest may say

A plugin's frontmatter is a subset of a bundled skill's. A plugin is always `egress: external` and `shared_room: private`, which is what leaving both keys out gives. These fields are refused (`forbidden_manifest_field`):

| Field | Why |
|---|---|
| `always_include: true` | with `exclude_skills`, it could take `sensitive_actions` out of every task's skill set |
| `exclude_skills` | the same |
| `egress: local` | it would skip the `skill:` approval a limited scheduled job needs, on the plugin's own word |
| `shared_room` other than `private` | it would put the plugin on a guest's turn in a shared room |
| `dependencies` | the daemon imports every name listed |

So a limited scheduled job needs its owner to approve `skill:<name>` before it can run a plugin, as for any `external` skill.

`env:` entries are held to an allowlist (`forbidden_env_spec`):

- **Every variable is `PLUGIN_<NAME>_…`**, where `<NAME>` is the skill name in upper case: `PLUGIN_EXAMPLE_TOKEN` for the skill `example`. Credential values are kept in one table keyed by variable name across every skill, so a plugin that could name `KARAKEEP_API_KEY` would be handed the bookmarks key, or could replace another skill's value. Two plugins declaring the same variable are both refused (`duplicate_env_var`).
- **`from:` is `secret`, `setup_env` or `user_id`.** Not `config`, which reads any configuration value, and not `template_file`.
- **The only fields are `var`, `from`, `service`, `key`, `sensitive` and `when`.** Not `fallback_var`, which copies a variable from the daemon's environment, and not `proxy_only`, which hands a value to every skill CLI.
- **Every secret service is `plugin_…`**: `plugin_example`, never `example` or a built-in service such as `karakeep`, `vault` or `mail_account`.

```yaml
env: [{"var":"PLUGIN_EXAMPLE_TOKEN","from":"secret","service":"plugin_example","key":"token","sensitive":true}]
```

A `setup_env(ctx)` hook may return only the variables its manifest declares with `from: setup_env`. Anything else is dropped, with one WARNING per plugin per daemon process.

A plugin's secret is an ordinary row in the `secrets` table, written with `istota secret ensure -u alice --service plugin_example --key token --value-stdin`, or by an `istota apply` plan with the value in its sidecar ([Declarative settings](../configuration/declarative-settings.md)). Both accept a `plugin_*` service and key only while the plugin is loaded, so on the stack sync the plugin before a plan provisions its secret.

A user can also enter their own in the web UI. Settings, Connections shows one card per `plugin_*` service, to each user who can use the plugin's skill (an `admin_only` plugin's card shows to admins only). The manifest has no display text, so the card is titled by the service name and each field by its key. An entry marked `"sensitive": true` is a write-only field: the stored value is never sent back to the browser. An entry without it is shown with its stored value, like a server URL, because such a value is already in the model's environment. Mark every credential `sensitive`.

An operator's `config/skills/<name>/skill.md` over a plugin replaces its documentation and metadata. The skill still runs the plugin's package, every rule in the next section still applies, and the operator file's `env:` is not held to the allowlist above, since `config/` is the operator's file.

### Credentials and security

A plugin runs with the trust of a bundled skill, and installing one is that trust decision. The manifest rules above limit what a plugin can do by mistake and what reaches the model. They do not contain a plugin written to do harm.

- **A plugin is not sandboxed, and can read whatever the daemon can.** Its CLI runs in the daemon container, outside the task's sandbox, as the daemon's user, with the daemon's network and filesystem, the databases and the master key (`/data/.secret_key`) included, whatever its manifest declares. Its package `__init__` runs inside the scheduler process on every task.
- **A plugin's code is in no task's sandbox on the stack.** The installed tree is bound at `/app/plugins` into the daemon and web containers alone, and no sandbox binds it, so a task cannot read a plugin's source. In a development checkout a plugin installed into the virtual environment is readable to tasks, since every sandbox binds that environment read-only. Keep nothing secret in the package either way.
- **A plugin's sensitive variables reach its own CLI alone, on the proxy path.** Tasks, CRON `command:` jobs and heartbeat shell checks run skill CLIs through the skill proxy, which hands each CLI only its own skill's credentials. A skill-task is the exception, for bundled skills and plugins alike: a CRON line `command: istota-skill example …` runs as one, its CLI receives every skill's credentials, and a plugin's variables reach every skill-task of a user who has the secret.
- **A plugin's variables are never served by name.** `istota-credential env PLUGIN_EXAMPLE_TOKEN` is refused, with the credential broker on or off.
- **A plugin gets no private credential channel.** A bundled skill's CLI gets a private socket for vault reads, wallet cards, one-time codes and mail account passwords; a plugin's does not. A plugin that needs a vault credential reads it through the public socket under the reveal policy, or declares its own `env:` secret. Credential-name arguments (`istota.skills._credref`) need that channel and do not work in a plugin.
- **With the skill proxy off there is no credential split.** Under `[security] skill_proxy_enabled = false` every skill's secret variables, a plugin's included, are in the model's environment. Doctor's `security.skill_proxy` reports that configuration.
- **`admin_only` hides a skill from the menu and does not stop a non-admin from running it.** The proxy runs any `cli: true` skill, and any user with a CRON.md can schedule `command: istota-skill example …`. A plugin whose verbs are for admins checks for itself with `load_config().is_admin(os.environ["ISTOTA_USER_ID"])`, as `skills/code_review` does. A proxied CLI has `ISTOTA_CONFIG_PATH` and `ISTOTA_ADMINS_FILE` in its environment. With an empty admins file `is_admin` is true only on a single-user install.
- **Nothing enforces the host-path rule for a plugin** (below).

On the stack the installed tree belongs to the daemon's uid and is read-only in the containers that use it, the protection `config/` has: the daemon and web processes cannot change plugin code. A process running as that uid on the VM itself, outside any container, could, and it could already read the master key.

### The supported CLI surface

A plugin may import these from istota and nothing else:

- `istota.skills._cli`: `run_skill_main`, `run_skill_cli`, `emit`, `fail`, `error_envelope`, `parse_and_resolve`
- `istota.skills._hostpath.host_path`
- `istota.config.load_config`, and `Config.is_admin`

The CLI convention is the bundled one: JSON envelopes on stdout, and `{"status": "error", ...}` with exit 1 on a failure.

**Every path-shaped argument carries a `host_path` stamp, and the parser goes through `parse_and_resolve`.** A plugin's CLI runs with the daemon's view of the filesystem, so an unstamped path argument is a read or write anywhere the daemon can reach, at the model's choice. The stamp and `parse_and_resolve` scope it to the task's own places, as for a bundled skill (the modes are in `skills/_hostpath.py`). The tests that hold bundled skills to this rule (`tests/test_skill_host_paths_coverage.py`, `tests/test_skill_hostpath_declaration.py`, `tests/test_skill_host_paths_refusals.py` and `tests/test_skill_credential_refs.py`) walk the bundled directory and cannot see a plugin. Keeping the rule is the plugin author's job, and nothing checks it.

### Developing and installing

In a development checkout, `uv pip install -e path/to/example-plugin` puts the plugin in the virtual environment, where the loader finds it through the distribution's metadata. An editable install keeps the package in your project directory, away from its metadata; the loader accepts that because the install records the project directory (`direct_url.json`), and still refuses a package of the same name found anywhere else (`shadowed`). No `plugins/` directory is involved.

A plugin added, upgraded or removed takes effect when the daemon and web processes restart: each reads the skill index, and with it the proxy's list of skills, once. On the stack `istota-stack plugins sync` restarts both. In a development checkout, restart them yourself.

`istota doctor --only skills.plugins` says what loaded, and `istota doctor --deep --only skills.plugin_imports` imports each plugin's package in a child process, which finds a missing dependency. `istota-skill skills list` gives each skill's `origin` (`bundled`, `plugin`, `operator` or `legacy`) and, for a plugin, `plugin` with its distribution and version. `!skills` tags a plugin's line `plugin: <distribution> <version>`, and `!skills <name>` adds a `Plugin:` line.

One known gap: when a `skill.md`'s frontmatter cannot be parsed, the shared frontmatter parser logs a WARNING of its own, for a plugin as for any skill. The loader records the plugin's problem at DEBUG, but in a `skills` subprocess, which configures no logging, the parser's line reaches stderr, once per process, where the model can see it in a tool result.
