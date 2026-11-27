# Trust and credentials

A plugin runs with the trust of a bundled skill. Installing one is that trust decision. The manifest rules in [Writing a plugin](writing.md#the-rules) limit what a plugin can do by mistake and what reaches the model. They do not contain a plugin written to do harm.

Adding a plugin needs root on the VM, to write `plugins/src/` and run `istota-stack`. That already meant code execution as the daemon, so plugins move no boundary. This page says what a plugin can reach once it is installed.

## What a plugin can reach

- **It is not sandboxed, and can read whatever the daemon can.** Its program runs in the daemon container, outside the task's sandbox, as the daemon's user (uid 10001), with the daemon's network and filesystem, the databases and the master key (`/data/.secret_key`) included, whatever its manifest declares.
- **Its package `__init__` runs inside the scheduler process** on every task, when the daemon looks for a `setup_env` hook, with the daemon's full environment. An import that raises is caught and the plugin's hook is skipped until the next restart, but its top-level code has already run once.
- **Its code is in no task's sandbox on the stack.** The installed tree is bound at `/app/plugins` into the daemon and web containers alone, so a task cannot read a plugin's source. In a development checkout a plugin installed into the virtual environment is readable to tasks, since every sandbox binds that environment read-only. Keep nothing secret in the package either way.
- **It cannot change its own installed code.** On the stack the installed tree belongs to the daemon's uid and is read-only in the containers that use it, the same protection `config/` has. A process running as that uid on the VM itself, outside any container, could change it, and could already read the master key.

## How its secrets are handled

- **A plugin's sensitive variables reach its own program alone, on the proxy path.** Tasks, CRON `command:` jobs and heartbeat shell checks run skill programs through the skill proxy, which hands each one only its own skill's credentials. The model's environment never holds them.
- **A skill task is the exception**, for bundled skills and plugins alike. A CRON line `command: istota-skill example …` runs as a skill task, its program receives every skill's credentials, and a plugin's variables reach every skill task of a user who has the secret.
- **A plugin's variables are never served by name.** `istota-credential env PLUGIN_EXAMPLE_TOKEN` is refused, with the credential broker on or off.
- **A plugin gets no private credential channel.** A bundled skill's program gets a private socket for vault reads, wallet cards, one-time codes and mail account passwords; a plugin's does not. A plugin that needs a vault credential reads it through the public socket under the reveal policy ([Credentials](../configuration/credentials.md)), or declares its own `env:` secret. Credential-name arguments need that channel and do not work in a plugin.
- **A variable not marked `sensitive` is not a secret.** Its stored value is shown to its owner on Settings, Plugins, and it is in the model's environment whenever the skill is in use. Mark every credential `sensitive`.
- **With the skill proxy off there is no credential split.** Under `[security] skill_proxy_enabled = false` every skill's secret variables, a plugin's included, are in the model's environment. Doctor's `security.skill_proxy` reports that configuration.

## What is left to the plugin author

- **Admin-only verbs.** `admin_only` hides a skill from the menu and does not stop a non-admin from running it. A plugin whose verbs are for admins checks `load_config().is_admin(os.environ["ISTOTA_USER_ID"])` itself ([Admin-only verbs](writing.md#admin-only-verbs)).
- **Path arguments.** Nothing enforces the host-path rule for a plugin. The tests that hold bundled skills to it walk the bundled directory and cannot see a plugin. Stamp every path argument and parse through `parse_and_resolve` ([Path arguments](writing.md#path-arguments)).
- **Text from somebody else.** A plugin that returns content a third party wrote (a web page, a message, a document) hands the model text that can carry instructions. Say so in the skill's body, and list `untrusted_input` in `companion_skills` so the model gets the rules for reading it.
