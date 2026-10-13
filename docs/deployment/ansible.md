# Ansible management role

`ansible/` is an optional Ansible role for operators who manage an Istota machine from an estate inventory. It is not an install path: a machine is installed with the [server quickstart](../getting-started/quickstart-server.md), and nothing in the role is needed for that. Use it when an inventory, rather than files edited on the machine, is the source of truth for the machine's settings.

The role is thin. It writes the inputs `host/provision.sh` and `istota-stack` read, then calls them:

- `host.env`, the stack's `.env` keys that `istota setup` owns, and `rclone.conf` for full Nextcloud integration
- one file per deployment secret, and the admins file, written as uid 10001
- `istota-stack update <tag>`, only when `istota_release` differs from the deployed tag
- the application settings through `istota-stack apply`: the config first, then, once the stack is healthy, the users' profiles, folders, briefings, jobs, heartbeats and credentials

The settings come either as `istota_plan`, an `istota apply` plan such as the output of `istota snapshot --format json`, or as the older `istota_config`, `istota_users`, `istota_user_resources`, `istota_user_briefings` and `istota_user_secrets`, never both. It renders no unit, nginx config or cron entry, and runs no build or migration. `ansible-playbook --check` reports drift, per-user settings included, without changing anything. See [Declarative settings](../configuration/declarative-settings.md) for the plan itself.

The machine must already be bootstrapped from a verified release (`host/cloud-init.yaml`, or `install.sh --server`); the role refuses one with no checkout. Both bootstraps start the browser setup (`istota-setup.service`), which listens on port 443 for up to an hour without a request. A machine the role will configure does not need it: stop it with `systemctl stop istota-setup` before the first play, or run the installer with `--terminal-setup` and no terminal, which stops after the build.

`ansible/README.md` is the reference: what each step does, a minimal `host_vars`, a playbook, check mode and the tests. `ansible/defaults/main.yml` lists every variable with a comment. Its values are generic placeholders; an estate's real values belong in its own `host_vars` and vault. On an install imported from the old bare-metal role, `istota migrate import-bare-metal --emit-plan` writes the imported config as a plan, which is a starting point for `istota_config`.

The bare-metal role that used to live in `deploy/ansible/`, and its variables, are gone. See [Migrating a bare-metal install](migrating-from-bare-metal.md).
