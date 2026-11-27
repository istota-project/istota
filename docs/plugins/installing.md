# Installing plugins

A plugin is not sandboxed. Its program runs as the daemon, with the daemon's network and files, the databases and the master key included. Install a wheel only if you would run its code as the daemon. [Trust and credentials](security.md) has the details.

Plugins live beside the image, never in it. They need no image build, and they survive `istota-stack update` and `rollback` with no step of their own.

## On the stack

To add a plugin, copy its wheel in as root and sync:

```bash
install -m 0644 -o root -g root example_plugin-0.1.0-py3-none-any.whl /srv/app/istota/plugins/src/
istota-stack plugins sync
istota doctor --only skills.plugins
```

To upgrade one, replace its wheel in `plugins/src/` and sync. To remove one, delete its wheel and sync. Keep one wheel per plugin: two versions of the same distribution fail the sync.

A plugin added, upgraded or removed takes effect when the daemon and web processes restart, because each reads the skill index once at start. `plugins sync` restarts both.

### The layout

`host/provision.sh` makes these directories. A VM provisioned before plugins existed gets them from the update that installs plugin support, which runs `provision.sh`.

| Path | Owner, mode | What it holds |
|---|---|---|
| `/srv/app/istota/plugins/` | root, 0755 | the two below and `.synced` |
| `plugins/src/` | root, 0755 | the wheels, which you copy in as root |
| `plugins/installed/` | uid 10001, 0755 | `site/`, the installed tree, written only by `plugins sync` |
| `plugins/.synced` | root, 0644 | the digest of the wheels `site/` was built from |

`istota-stack` binds `plugins/` read-only at `/app/plugins` into the `daemon` and `web` containers alone (`docker/docker-compose.plugins.yml`), when `plugins/installed` exists and the deployed release has that file. No task's sandbox binds it.

### What `plugins sync` does

`istota-stack plugins sync [--force] [--no-restart]` takes the stack lock and:

1. Refuses, before any container starts, unless every entry in `plugins/src/` is a regular file named like a wheel (`NAME-VERSION-TAGS.whl`, of letters, digits and `_.!+-`, starting with a letter or a digit), owned by root and writable by root alone, and `plugins/` and `plugins/src/` are root's and writable by no one else. It also refuses when the layout is missing (run `provision.sh`) or the deployed release has no plugins overlay (update first).
2. Hashes the wheels. When nothing changed since the last sync it prints `unchanged` and exits 0, unless `--force` is given.
3. Installs the wheels in a one-shot container, as uid 10001, with the image's own `uv`: no index, no network and no dependencies (`--no-deps`), into `plugins/installed/next/`. It then refuses the tree if a wheel declares no `istota.skills` entry point, has a distribution name or package name the image's virtual environment already has (which would hide the plugin), or puts anything at the top level besides its one package and that package's metadata. Only then does the new tree replace `site/`. With no wheels the result is an empty `site/`. A run killed in the middle of that replacement leaves the old tree under another name, and the next sync puts it back before it does anything else.
4. On a failure, prints what the one-shot printed and exits 1. `site/` and `.synced` stay as they were, and nothing restarts.
5. Otherwise writes `.synced`, prints `changed`, restarts `daemon` and `web` if the stack is running, and exits 2. The other services do not read plugins and are not restarted, and a stack that is down stays down. If the restart fails after a good install, it exits 1 with `.synced` already written, and `istota-stack up` finishes the job.

| `plugins sync` exit | Meaning |
|---|---|
| 0 | nothing changed |
| 2 | the plugins changed and the stack restarted onto them |
| 1 | a refused entry in `plugins/src/`, a missing layout, a failed install, or a failed restart |
| 64 | a usage error |

`--no-restart` skips step 5's restart. The management role uses it so that one play restarts the stack once. Do not use it by hand: until the next restart, a newly spawned plugin CLI runs the new tree while the daemon's skill index and the proxy's list of skills are the old ones, so a removed plugin is still offered and fails when it runs.

`istota-stack plugins status` takes no lock and prints each wheel in `plugins/src/`, then `synced` or `not synced`. It does not say what loaded; `istota doctor --only skills.plugins` does ([Checking what loaded](#checking-what-loaded)).

## With the Ansible role

The optional [management role](../deployment/ansible.md) takes a list of wheel paths on the controller in `istota_skill_plugins`. Each is copied to `/srv/app/istota/plugins/src/` (root's, 0644), a wheel there that the list does not name is removed, and `istota-stack plugins sync --no-restart` installs them. A change restarts the stack once, together with any other input that changed. This runs after the update and before the users' settings, so a plan can provision a plugin's secret in the same play.

Under `--check` nothing is copied, removed or synced. One task lists by file name the wheels a real run would copy and remove, so a wheel rebuilt under a file name the VM already has is not reported. Because nothing is synced, a plan naming the secret of a plugin the VM has not synced yet is refused with `secret_service_not_provisionable`, so the first `--check` of a play that adds a plugin and its secret together fails at that step. A real run, or a `--check` once the plugin is synced, does not.

## In a development checkout

Install the plugin into the checkout's virtual environment and restart the daemon and web processes yourself:

```bash
uv pip install -e path/to/example-plugin
```

The loader finds the plugin through the distribution's metadata, so no `plugins/` directory is involved. An editable install keeps the package in your project directory, away from its metadata. The loader accepts that because the install records the project directory (`direct_url.json`), and still refuses a package of the same name found anywhere else (`shadowed`).

In a development checkout a plugin's source is readable from inside a task, because every sandbox binds the virtual environment read-only. On the stack it is not.

## Giving a plugin its credentials

A plugin's secrets are ordinary rows in the `secrets` table, under a service named `plugin_<something>`. The plugin's manifest says which service and key each variable comes from ([Writing a plugin](writing.md#4-a-secret)). There are three ways to write one, and all three accept a `plugin_*` service only while the plugin is loaded. On the stack, sync the plugin first.

**From the command line**, for one user:

```bash
istota secret ensure -u alice --service plugin_example --key token --value-stdin
```

**From an `istota apply` plan**, with the value in the plan's sidecar file ([Declarative settings](../configuration/declarative-settings.md)).

**From the web UI**, by each user for themselves. Settings, Plugins lists the plugins the user can use, each with its package and version, and shows one card per `plugin_*` service. An `admin_only` plugin and its card show to admins only. With no plugin installed the page says so. The manifest has no display text, so a card is titled by the service name and each field by its key. A field whose variable is marked `"sensitive": true` is write-only: the stored value is never sent back to the browser. A field without it shows its stored value, like a server URL.

## Checking what loaded

```bash
istota doctor --only skills.plugins
istota doctor --deep --only skills.plugin_imports
```

`skills.plugins` lists the plugins that loaded, with their distribution and package, and fails for a plugin that is installed and did not load, or a wheel in `plugins/src/` that loaded as no plugin. `skills.plugin_imports`, with `--deep` only, imports each plugin's package in a child process, which finds a missing dependency. See [Doctor checks](../reference/doctor-checks.md#users-and-configuration).

Two other places show plugins:

- `istota-skill skills list` gives each skill's `origin` (`bundled`, `plugin`, `operator` or `legacy`) and, for a plugin, `plugin` with its distribution and version.
- In chat, `!skills` tags a plugin's line `plugin: <distribution> <version>`, and `!skills <name>` adds a `Plugin:` line.

### Why a plugin did not load

Doctor names one of these reasons for each plugin it found and did not load:

| Reason | What is wrong |
|---|---|
| `bad_name` | the entry point name is not lowercase letters, digits and underscores starting with a letter |
| `reserved_name` | the name is `files`, `memory`, `recall`, `memory_search`, `room_notes` or `web` |
| `clashes_with_bundled` | a bundled skill has the same name; the bundled skill stays |
| `duplicate_plugin` | two plugins use the same name; both are refused |
| `bad_value` | the entry point value is not one top-level package name (`example_plugin`, not `example_plugin.cli` or `example_plugin:main`) |
| `module_not_found` | nothing on the path has that package name |
| `not_a_package` | the value names a plain module, not a package directory |
| `namespace_package` | the package has no `__init__.py` |
| `shadowed` | another package of the same name is earlier on the path |
| `no_skill_md` | there is no `skill.md` in the package directory |
| `unparseable_skill_md` | the frontmatter does not parse |
| `forbidden_manifest_field` | the frontmatter uses a field a plugin may not ([What the manifest may say](writing.md#what-the-manifest-may-say)) |
| `forbidden_env_spec` | an `env:` entry breaks the allowlist ([Environment variables](writing.md#environment-variables)) |
| `duplicate_env_var` | two plugins declare the same variable; both are refused |

A plugin whose package `__init__` raises at import is loaded but skipped when the daemon runs its `setup_env` hook, with one ERROR line in the daemon log. It is not imported again until the daemon restarts.

A `cli: true` plugin with no `__main__.py` loads, but its program cannot run, and `skills.plugins.entry` warns.

## Updates, rollbacks and restores

`update` and `rollback` leave `plugins/` alone, and the new containers bind the same directory. If a later image gains a distribution with a plugin's name, that plugin stops loading at the next start with no error at load time; doctor's `skills.plugins.wheels` reports it.

A rollback to a release from before plugins existed runs that release's `istota-stack`, which binds nothing. The plugins' skills are absent there, and the daemon removes their jobs at its start (below).

`plugins/` is in none of the workspace backups, and `istota-stack restore` does not bring it back. Keep the wheels with the rest of the deployment. On a restored stack, or a stack moved to a new VM, copy them into `plugins/src/` and run `istota-stack plugins sync` before you start the stack.

## Scheduled jobs that run a plugin

A CRON.md line `command: istota-skill example …` runs as a skill task. Because a plugin is `egress: external`, a limited job needs its owner to approve `skill:example` on Settings, Scheduled jobs before it can run.

**A plugin absent at a daemon start loses its jobs' approvals.** At every start the daemon deletes the scheduled jobs that run a skill it does not have, fails their pending tasks, and a job's network approvals go with its row. When the plugin comes back, the CRON.md sync creates the job again from nothing, so a limited job has to be approved again, its `skill:` approval included. A plugin that is installed and fails to load, a problem doctor reports, keeps its jobs. When the daemon cannot read the list of installed plugins at all, it deletes no job at that start and says so in its log.
