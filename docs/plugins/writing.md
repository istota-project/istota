# Writing a plugin

This page builds a small plugin from an empty directory to an installed wheel, then lists every rule the loader holds a plugin to. The example skill is called `example`. It has two verbs: `status`, which says whether its API token is set, and `wordcount`, which counts the words in a file from the task's workspace. Between them they show the three things most plugins need: a command-line program, a secret, and a path argument.

Read [Skill plugins](overview.md) first if you have not decided between a plugin and a bundled skill. A plugin cannot bring its own dependencies.

## 1. The package

A plugin is one top-level regular package, shipped as a pure-Python wheel:

```
example-plugin/
├── pyproject.toml
└── example_plugin/
    ├── __init__.py      # build_parser(), main(); optional setup_env(ctx)
    ├── __main__.py      # the template below, unchanged
    └── skill.md         # frontmatter and body, as for a bundled skill
```

`pyproject.toml` registers the skill in the `istota.skills` entry-point group. The entry point's name is the skill's name; its value is the package.

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

Write `dependencies = []` and do not list `istota`: the wheel is installed with `--no-deps` into the image's own environment, where Istota is already present. Hatchling ships every file inside the package directory, so `skill.md` needs no package-data table.

## 2. The manifest: `skill.md`

The frontmatter tells the loader when to offer the skill and what its program needs. The body is what the model reads before it runs the program, so write it for the model: what each verb does, what it returns, and when to use it.

```markdown
---
description: Count words in workspace files, and check the example API token
triggers: [word count, example]
cli: true
env: [{"var":"PLUGIN_EXAMPLE_TOKEN","from":"secret","service":"plugin_example","key":"token","sensitive":true}]
---

# Example

Run `istota-skill example <verb>`. Every verb prints one JSON object.

- `status`: `{"status": "ok", "token_set": true}` when the user has stored a token.
- `wordcount --file PATH`: the number of words in a file. PATH must be an absolute path in the user's workspace or the task's attachments.

On a failure the answer is `{"status": "error", "error": "..."}` and the exit status is 1. Tell the user what the error says; do not retry with a different path.
```

`description` is the one line shown in the skill menu and in `!skills`. `triggers` is documentation only. `cli: true` says the skill has a program. The placeholders `{BOT_NAME}`, `{BOT_DIR}` and `{user_id}` are substituted at load time, as for a bundled skill.

The fields a plugin may use and the ones it may not are in [What the manifest may say](#what-the-manifest-may-say) below.

## 3. The program: `__init__.py` and `__main__.py`

The convention is the bundled skills' one: a `build_parser()` that returns an argparse parser, and a `main()` that parses, runs one verb and prints a JSON envelope.

```python
"""Example skill plugin: count words in a workspace file."""

import argparse
import os

from istota.skills._cli import parse_and_resolve, run_skill_cli
from istota.skills._hostpath import host_path


def build_parser():
    parser = argparse.ArgumentParser(prog="istota-skill example")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Say whether the API token is set")
    count = sub.add_parser("wordcount", help="Count the words in a file")
    host_path(count, "--file", mode="read", required=True,
              help="An absolute path in the workspace")
    return parser


def cmd_status(args):
    return {"status": "ok", "token_set": bool(os.environ.get("PLUGIN_EXAMPLE_TOKEN"))}


def cmd_wordcount(args):
    with open(args.file, encoding="utf-8") as f:
        words = len(f.read().split())
    return {"status": "ok", "file": args.file, "words": words}


COMMANDS = {"status": cmd_status, "wordcount": cmd_wordcount}


def main(argv=None):
    args = parse_and_resolve(build_parser(), argv)
    run_skill_cli(COMMANDS, args)
```

Three things in it matter:

- **`parse_and_resolve`, not `parser.parse_args`.** It parses, then checks every path argument stamped with `host_path` against the task's own places and writes the resolved absolute path back. A path outside them is refused before any handler runs, with `reason: host_path_refused` and exit 1. Without the stamp, `--file` would be a read of anything the daemon can read, at the model's choice. See [Path arguments](#path-arguments).
- **`run_skill_cli`** runs the handler named by `args.command`, prints what it returns as JSON, and exits 1 when that is an error envelope (`{"status": "error", ...}`). An exception in a handler becomes an error envelope too, so the model always gets JSON on stdout rather than a traceback.
- **Every `add_subparsers` has a `dest`.** The path check finds the subcommand that ran through it.

`__main__.py` is this template, unchanged:

```python
"""Allow running as `python -m <package>`."""

from istota.skills._cli import run_skill_main

from . import main

run_skill_main(main)
```

`run_skill_main` keeps `load_config`'s warnings about the config document off the skill's stderr, where the model would read them, then exits with what `main()` returns.

**Keep the package `__init__` cheap, and never let it raise.** The daemon imports it on every task to look for a `setup_env` hook, inside the scheduler process and with the daemon's full environment. Put imports of anything slow inside the functions that need them.

## 4. A secret

The `env:` entry in the manifest asks for the user's secret `plugin_example`/`token` to be put in the program's environment as `PLUGIN_EXAMPLE_TOKEN`. Because it is marked `"sensitive": true`, the value is kept out of the model's environment and handed only to this plugin's program, through the skill proxy.

The operator or the user stores the value ([Giving a plugin its credentials](installing.md#giving-a-plugin-its-credentials)). With no value stored, the variable is absent, which is what `status` reports.

Mark every credential `sensitive`. A variable without it is shown to its owner on Settings, Plugins with its stored value, and is in the model's environment whenever the skill is in use.

## 5. Build, install, try it

```bash
cd example-plugin
uv build --wheel
```

That writes `dist/example_plugin-0.1.0-py3-none-any.whl`.

To try it in a development checkout of Istota, install it editable into the checkout's environment and run the program directly:

```bash
cd ~/src/istota
uv pip install -e ../example-plugin
uv run istota doctor --only skills.plugins
uv run python -m example_plugin status
```

Then restart the daemon and web processes so their skill index includes it, and ask Istota to count the words in a file.

To install the wheel on a server, see [Installing plugins](installing.md#on-the-stack). Bump `version` in `pyproject.toml` for each release, so the file name changes and Settings shows which version is installed.

## The rules

The loader checks these each time it reads the skill index. A plugin that breaks one is left out of the index, and `istota doctor`'s `skills.plugins` names it with the reason ([Why a plugin did not load](installing.md#why-a-plugin-did-not-load)).

### Names and packaging

- **The entry point name is the skill name.** It matches `^[a-z][a-z0-9_]*$` and is none of the reserved names `files`, `memory`, `recall`, `memory_search`, `room_notes` and `web`. Skill names share a namespace with the scopes a shared room withholds by name, so a plugin called `room_notes` would be withheld on every restricted task and take the room's notes folder with it. A `name:` in the frontmatter is ignored, as it is for a bundled skill.
- **A plugin never replaces a bundled skill.** A plugin named like one is refused (`clashes_with_bundled`) and the bundled skill stays. Two plugins with the same name are both refused (`duplicate_plugin`). To change a bundled skill's documentation, use an operator override, `config/skills/<name>/skill.md`.
- **The entry point value is one top-level package name**: `example_plugin`, never `example_plugin.cli` and never `example_plugin:main`. A plain module and a namespace package are refused too. The loader finds the package without importing it, and a dotted name would run the parent package's `__init__.py` on every load.
- **`skill.md` sits in the package directory.** Without it the plugin is refused. A `skill.toml` there is ignored.
- **`__main__.py` is the template** above. A `cli: true` plugin without `__main__.py` loads, but its program cannot run, and doctor's `skills.plugins.entry` warns.
- **No dependencies and no compiled code.** A wheel whose distribution name or package name the image already has is refused at sync, since the image's copy would hide it. So is a wheel that installs anything beside its one package: a second top-level package or module, a script, a `.pth` file or a `sitecustomize.py`. The installed tree is on the path of every Python process in the image, the web process included, and nothing but the plugin's package belongs there.

### What the manifest may say

A plugin's frontmatter is a subset of a bundled skill's ([Frontmatter fields](../development/adding-skills.md#frontmatter-fields)). Every field there is allowed except the ones in the table below, and `env:` is held to its own allowlist.

A plugin is always `egress: external` and `shared_room: private`, which is what leaving both keys out gives. These fields are refused (`forbidden_manifest_field`):

| Field | Why |
|---|---|
| `always_include: true` | with `exclude_skills`, it could take `sensitive_actions` out of every task's skill set |
| `exclude_skills` | the same |
| `egress: local` | it would skip the `skill:` approval a limited scheduled job needs, on the plugin's own word |
| `shared_room` other than `private` | it would put the plugin on a guest's turn in a shared room |
| `dependencies` | the daemon imports every name listed |

So a limited scheduled job needs its owner to approve `skill:<name>` before it can run a plugin, as for any `external` skill.

`admin_only` hides the skill from non-admins' menus. It does not stop a non-admin from running the program ([Admin-only verbs](#admin-only-verbs)).

### Environment variables

`env:` entries are held to an allowlist (`forbidden_env_spec`):

- **Every variable is `PLUGIN_<NAME>_…`**, where `<NAME>` is the skill name in upper case: `PLUGIN_EXAMPLE_TOKEN` for the skill `example`. Credential values are kept in one table keyed by variable name across every skill, so a plugin that could name `KARAKEEP_API_KEY` would be handed the bookmarks key, or could replace another skill's value. Two plugins declaring the same variable are both refused (`duplicate_env_var`).
- **`from:` is `secret`, `setup_env` or `user_id`.** Not `config`, which reads any configuration value, and not `template_file`.
- **The only fields are `var`, `from`, `service`, `key`, `sensitive` and `when`.** Not `fallback_var`, which copies a variable from the daemon's environment, and not `proxy_only`, which hands a value to every skill program.
- **Every secret service is `plugin_…`**: `plugin_example`, never `example` or a built-in service such as `karakeep`, `vault` or `mail_account`.
- **Every secret key is lowercase letters, digits and underscores**: `token`, `api_key`, never `apiKey` or `client-id`. The key is part of the address the web UI writes a secret at.

| `from:` | The value |
|---|---|
| `secret` | the user's secret at `service` and `key` |
| `setup_env` | what the package's `setup_env(ctx)` hook returns for this variable |
| `user_id` | the task's user id |

### The `setup_env` hook

For a value that has to be computed per task, define `setup_env(ctx)` in the package's `__init__.py` and declare the variable with `"from": "setup_env"`:

```python
def setup_env(ctx):
    return {"PLUGIN_EXAMPLE_HOME": f"/Users/{ctx.task.user_id}/example"}
```

The hook runs in the scheduler process for every task, whether or not the skill was selected, and receives the task's context: `ctx.config`, `ctx.task`, `ctx.user_config`, `ctx.user_temp_dir` and `ctx.is_admin`. It returns a `dict[str, str]`. Only the variables the manifest declares with `from: setup_env` are kept; anything else is dropped, with one WARNING per plugin per daemon process. A hook that raises is skipped with one ERROR line in the log.

`ctx` exposes the daemon's configuration objects, which are not part of the supported surface below. Read what you need and nothing more, and keep the hook fast: it runs on every task.

### What a plugin may import

A plugin may import these from Istota and nothing else:

- `istota.skills._cli`: `run_skill_main`, `run_skill_cli`, `emit`, `fail`, `error_envelope`, `parse_and_resolve`
- `istota.skills._hostpath.host_path`
- `istota.config.load_config`, and `Config.is_admin`

Anything else may change between releases without notice. `emit(payload)` prints one envelope and exits 1 if it is an error; `fail(message, **extra)` prints an error envelope and exits 1; `error_envelope(message, **extra)` builds one without printing.

### Path arguments

**Every path-shaped argument carries a `host_path` stamp, and the parser goes through `parse_and_resolve`.** A plugin's program runs with the daemon's view of the filesystem, so an unstamped path argument is a read or write anywhere the daemon can reach, at the model's choice. The stamp's mode says where the bytes go, and so which of the task's places the path may be in:

| `mode=` | For | Allowed under |
|---|---|---|
| `"read"` | a file the program reads | the user's workspace, the task's deferred directory, its room's folder, and its own attachment copies |
| `"write"` | a destination the program writes | the same, less the read-only places |
| `"egress"` | a file whose bytes leave: uploaded, mailed or sent to a service | the user's workspace and the task's deferred directory |
| `"remote"` | a path on another machine, which that machine scopes | not resolved; requires `note=` saying why |

`host_path(parser, *names, mode=..., note="", **kwargs)` takes the same arguments as `add_argument`. An empty path is refused. A relative one is resolved against the daemon's working directory and is almost always refused, so tell the model in `skill.md` to pass absolute paths.

The check happens at parse time. The handler then opens the resolved path by name.

The bundled skills are held to this rule by tests that walk their parsers. Those tests cannot see a plugin. Keeping the rule is the plugin author's job, and nothing checks it.

### Admin-only verbs

`admin_only: true` hides a skill from non-admins' menus and from `skills show`. It does not gate execution: the skill proxy runs any `cli: true` skill, and any user with a CRON.md can schedule `command: istota-skill example …`. A plugin with verbs for admins only checks for itself:

```python
import os

from istota.config import load_config
from istota.skills._cli import fail


def require_admin():
    if not load_config().is_admin(os.environ["ISTOTA_USER_ID"]):
        fail("this verb is for admins only")
```

A proxied program has `ISTOTA_USER_ID`, `ISTOTA_CONFIG_PATH` and `ISTOTA_ADMINS_FILE` in its environment. With an empty admins file, `is_admin` is true only on a single-user install.

### Testing

The bundled skills' test helpers are not part of the supported surface. Test a plugin's verbs by calling `main([...])` with an argv list and reading what it prints, with `PLUGIN_*` variables set in the environment as the proxy would set them. Before you ship a wheel, install it into a development checkout and run `istota doctor --only skills.plugins` and `istota doctor --deep --only skills.plugin_imports`, which catch a manifest the loader refuses and an import that fails.

## Known gaps

- When a `skill.md`'s frontmatter cannot be parsed, the shared frontmatter parser logs a WARNING of its own. In a `skills` subprocess, which configures no logging, that line reaches stderr once per process, where the model can see it in a tool result.
