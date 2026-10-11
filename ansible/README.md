# The istota management role

An optional Ansible role for operators who manage an istota VM from an estate. It is not an install path: the supported install is `host/cloud-init.yaml` (or `host/provision.sh` by hand) followed by `istota-stack setup`, and nothing here is needed for that. Use this role when an Ansible inventory, rather than a hand-edited file, is the source of truth for the VM's settings.

The role is thin. It writes the inputs `host/provision.sh` and `istota-stack` read, then calls them:

1. `host.env` from variables, the keys of the stack's `.env` that `istota setup` owns, and `rclone.conf` when the storage is Nextcloud.
2. `provision.sh`, when what it acts on (`host.env`, `rclone.conf`, `.env`'s `COMPOSE_PROFILES`, the checkout's commit) differs from the digest its last successful run recorded, or where `istota-stack` is missing. A play that dies between writing an input and `provision.sh` finishing therefore still provisions on the next play.
3. One file per deployment secret under `secrets/` (0400, uid 10001) from vault variables, and `config/admins` when `istota_admins` is set, through `istota-stack put-files`. Both directories are uid 10001's, so the files are written as 10001 with the values on stdin, and root never opens a file there.
4. `istota-stack update <tag>`, only when `istota_release` differs from `ISTOTA_TAG` in `.env`. An update is a deliberate change to that variable.
5. `istota_config`, the whole of `config.toml` as a nested dict, written as an `istota apply` plan and applied with `istota-stack apply`. Exit 2 is reported as changed, and `istota-stack` restarts a running stack on it.
6. `istota-stack check-image`, after the apply: a build decides from `config.toml` whether Claude Code goes in, so when the image lacks what the config needs (or has another version than `istota_claude_code_version`), the deployed tag is rebuilt.
7. The `istota user|resource|briefing|secret ensure` loops, through the VM's `istota` wrapper. A secret's value goes on stdin (`--value-stdin`), and an empty one is skipped. The declarative-apply work will replace these loops.

It renders no unit, nginx config or cron entry, and runs no build or migration. `provision.sh` and `istota-stack` own those.

## Before the first run

The VM must already be bootstrapped from a verified release: `host/cloud-init.yaml`, or `host/provision.sh` run from a checkout of a signed tag. The role refuses a VM with no checkout at `/srv/app/istota/src`, because nothing should run from a checkout before its tag is verified, and that bootstrap is where the first verification happens.

## Variables

`defaults/main.yml` lists every variable with a comment, and is the overview of what an operator sets. Its values are generic placeholders. An estate's real values (hostnames, addresses, the release key, every credential) belong in the estate's own `host_vars` and vault, never in this repository. On an install imported from a bare-metal host, `istota migrate import-bare-metal --emit-plan` writes the rewritten config as a plan, which is a starting point for `istota_config`.

A minimal `host_vars` file for a proxied VM:

```yaml
istota_release: v0.45.0
istota_release_signing_key: "ssh-ed25519 AAAA... releases"
istota_ingress: proxied
istota_domain: istota.example.com
istota_upstream_proxy: 192.0.2.10
istota_listen_addr: 10.0.0.5
istota_listen_port: 8080
istota_admins: [alice]
istota_secrets:
  istota_brain_native_api_key: "{{ vault_native_api_key }}"
  istota_web_session_secret_key: "{{ vault_web_session_secret }}"
istota_config:
  bot_name: Istota
  site: {hostname: istota.example.com}
  # ... the rest of config.toml
istota_users:
  alice: {display-name: Alice, email: [alice@example.com]}
```

A secret named in `istota_secrets` is written exactly, an empty string included. A secret left out is created empty if its file is missing and otherwise left alone, so a file the wizard or the import wrote survives a play that does not manage it.

## A playbook

```yaml
- hosts: istota
  become: true
  roles:
    - role: /path/to/istota/ansible
```

## Check mode

`ansible-playbook --check` reports drift. The config step runs `istota-stack apply --dry-run` against the plan the play would apply, the secrets step runs `istota-stack put-files --dry-run`, an update, a provision or a rebuild that would run is reported as changed, and the other file writes are checked as usual. The ensure loops have no dry run and are skipped under `--check`.

## Tests

`tests/test_management_role.py`, in the default suite, holds the role's names to the code: the secret names to `setup_wizard.SECRET_NAMES`, the `host.env` keys to what `provision.sh` documents and reads and what `istota-stack` reads, the `.env` keys and their derived values to the wizard's, and the `istota-stack` verbs the role calls to the script's own. It also requires that no task writes below `config/` or `secrets/`, that every task whose arguments carry a secret has `no_log`, that no secret value is on an ensure call's argv, and that an exit 2 counts as a change only when the command ran. The vm tier applies its proxied and nextcloud modes with this role and checks that a `--check` right after a converge changes nothing, that a link planted in `secrets/` is not written through, that a provision a dead play left undone runs on the next, and that a claude_code brain gets an image with Claude Code (`tests/vm/test_management_role.py`).
