# Ansible management role

`ansible/` is an optional extra for operators who already manage their machines from an Ansible inventory. It is not a way to install Istota, and an install does not use it to configure anything by default. A machine is installed with the [server quickstart](../getting-started/quickstart-server.md) (or the [local install](../getting-started/local-install.md) on a Mac) and managed with `istota-stack`. Use the role only when an inventory, rather than files edited on the machine, should be the source of truth for its settings.

The role is thin. It writes the inputs `host/provision.sh` and `istota-stack` read, then calls them:

- `host.env`, the stack's `.env` keys that `istota setup` owns, and `rclone.conf` for full Nextcloud integration
- `provision.sh`, when what it acts on (`host.env`, `rclone.conf`, `.env`'s `COMPOSE_PROFILES`, the checkout's commit) differs from what its last successful run recorded, so a play that died partway still provisions on the next
- one file per deployment secret, and the admins file, written as uid 10001 through `istota-stack put-files`
- the update: with `istota_update_mode: release` (the default), `istota-stack update <tag>` only when `istota_release` differs from the deployed tag; with `istota_update_mode: latest`, `istota-stack update --unsigned latest` only when `istota-stack latest-tag` names a different `main-<hex>` than the one deployed. `latest` verifies nothing and needs no signing key. `--tags istota_update` runs only the update and waits for the stack to be healthy, and `-e istota_update_force=true` runs it even when nothing moved
- the application settings through `istota-stack apply`: the users' settings are checked first, then the config is applied, then, once the stack is healthy, the users' profiles, folders, briefings, jobs, heartbeats and credentials
- `istota-stack check-image` after the config apply, and a rebuild of the deployed tag when the image lacks what the config needs (Claude Code, or another version than the pin)

The settings come either as `istota_plan`, an `istota apply` plan such as the output of `istota snapshot --format json`, or as the older `istota_config`, `istota_users`, `istota_user_resources`, `istota_user_briefings` and `istota_user_secrets`, never both. The role itself renders no unit, nginx config or cron entry, and runs no build or migration: `provision.sh` and `istota-stack` do those. `ansible-playbook --check` reports drift, per-user settings included, without changing anything. See [Declarative settings](../configuration/declarative-settings.md) for the plan itself.

The machine must already be bootstrapped from a verified release (`host/cloud-init.yaml`, or `install.sh --server`); the role refuses one with no checkout. Both bootstraps start the browser setup (`istota-setup.service`), which listens on port 443 for up to an hour without a request. A machine the role will configure does not need it: stop it with `systemctl stop istota-setup` before the first play, or run the installer with `--terminal-setup` and no terminal, which stops after the build.

`ansible/README.md` is the reference: what each step does, a minimal `host_vars`, a playbook, check mode and the tests. `ansible/defaults/main.yml` lists every variable with a comment. Its values are generic placeholders; an estate's real values belong in its own `host_vars` and vault. On an install imported from the old bare-metal role, `istota migrate import-bare-metal --emit-plan` writes the imported config as a plan, which is a starting point for `istota_config`, and `istota migrate role-vars` converts the old role's vault into this role's variables.

The bare-metal role that used to live in `deploy/ansible/`, and its variables, are gone. See [Migrating a bare-metal install](migrating-from-bare-metal.md).
