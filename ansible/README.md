# The istota management role

An optional Ansible role for operators who manage an istota VM from an estate. It is not an install path: the supported install is `host/cloud-init.yaml` (or `host/provision.sh` by hand) followed by `istota-stack setup`, and nothing here is needed for that. Use this role when an Ansible inventory, rather than a hand-edited file, is the source of truth for the VM's settings.

The role is thin. It writes the inputs `host/provision.sh` and `istota-stack` read, then calls them:

1. `host.env` from variables, the keys of the stack's `.env` that `istota setup` owns, and `rclone.conf` when the storage is Nextcloud.
2. `provision.sh`, when what it acts on (`host.env`, `rclone.conf`, `.env`'s `COMPOSE_PROFILES`, the checkout's commit) differs from the digest its last successful run recorded, or where `istota-stack` is missing. A play that dies between writing an input and `provision.sh` finishing therefore still provisions on the next play.
3. One file per deployment secret under `secrets/` (0400, uid 10001) from vault variables, and `config/admins` when `istota_admins` is set, through `istota-stack put-files`. Both directories are uid 10001's, so the files are written as 10001 with the values on stdin, and root never opens a file there.
4. `istota-stack update <tag>`, only when `istota_release` differs from `ISTOTA_TAG` in `.env`. An update is a deliberate change to that variable.
5. The config, from the `config` of `istota_plan`, applied with `istota-stack apply --input-format role-vars`. Exit 2 is reported as changed. `istota-stack` restarts a running stack once, only when the result says a committed change needs it; when user settings follow, it passes `--no-restart` and leaves that restart to the user settings apply, so a play changing both restarts the stack once (twice when the image is also rebuilt). Before it, the user settings are checked with `istota-stack apply --check --json`, which reads no config and no database, runs under `--check` too and is never a change; a refusal there fails the play, naming the field and the fix, before anything is written.
6. `istota-stack check-image`, after the apply: a build decides from `config.toml` whether Claude Code goes in, so when the image lacks what the config needs (or has another version than `istota_claude_code_version`), the deployed tag is rebuilt.
7. The user settings, once the stack is healthy: profiles, folder resources, briefings, scheduled jobs, heartbeats, provisionable credentials and shared briefing definitions, from the rest of `istota_plan`. They go through the same `istota-stack apply`, because they need the database the daemon initializes on its first start. The role adds no restart of its own: this apply restarts the stack once for its own changes and for the config's, and the role waits for it to be healthy again.

Both applies read the role's variables from a private 0600 file that is removed afterwards, whatever the outcome; credential values are never on a command line. The command prints `istota apply`'s JSON result, and a refused or partial apply fails the play with the error codes and field paths from that result.

It renders no unit, nginx config or cron entry, and runs no build or migration. `provision.sh` and `istota-stack` own those.

## Before the first run

The VM must already be bootstrapped from a verified release: `host/cloud-init.yaml`, or `host/provision.sh` run from a checkout of a signed tag. The role refuses a VM with no checkout at `/srv/app/istota/src`, because nothing should run from a checkout before its tag is verified, and that bootstrap is where the first verification happens.

## Variables

`defaults/main.yml` lists every variable with a comment, and is the overview of what an operator sets. Its values are generic placeholders, except `istota_release_signing_key`, which defaults to the project's release key (`host/release-signing-key.pub`). An estate's real values (hostnames, addresses, every credential) belong in the estate's own `host_vars` and vault, never in this repository. On an install imported from a bare-metal host, `istota migrate import-bare-metal --emit-plan` writes the rewritten config as a plan, which is a starting point for `istota_plan`.

A minimal `host_vars` file for a proxied VM:

```yaml
istota_release: v0.45.0
istota_ingress: proxied
istota_domain: istota.example.com
istota_upstream_proxy: 192.0.2.10
istota_listen_addr: 10.0.0.5
istota_listen_port: 8080
istota_admins: [alice]
istota_secrets:
  istota_provider_openrouter_api_key: "{{ vault_openrouter_api_key }}"
  istota_web_session_secret_key: "{{ vault_web_session_secret }}"
istota_plan:
  config:
    bot_name: Istota
    site: {hostname: istota.example.com}
    # ... the rest of config.toml
  users:
    alice:
      profile: {display_name: Alice, email_addresses: [alice@example.com]}
```

### Application settings: `istota_plan`

The application settings are one plan, `istota_plan`. The variables the role took before it (`istota_config`, `istota_users`, `istota_user_resources`, `istota_user_briefings`, `istota_user_secrets`, `istota_user_profile_mode`) are retired, and a play that still sets one fails before it changes anything. Deployment variables (`istota_secrets`, `istota_admins`, the `host.env` variables, `istota_rclone_conf`) sit beside it, because a snapshot does not cover them.

`istota_plan` is an `istota apply` plan as a mapping, with the CLI's own field names ([Declarative settings](../docs/configuration/declarative-settings.md)). A snapshot of a running install is one, and `--format ansible` writes it as a `group_vars` file that sets `istota_plan`, so it goes into the inventory as it is:

```sh
plan=$(ssh vm sudo istota snapshot --format ansible) \
  && printf '%s\n' "$plan" | ansible-vault encrypt --output inventory/group_vars/istota/istota_plan.json -
```

The snapshot goes into a variable first because a failed snapshot writes nothing to stdout, and piping it straight into `ansible-vault encrypt` would replace the inventory's plan with an encrypted empty file.

Run that again whenever the VM's settings should become the inventory's. The output holds no credential values; it does hold names, addresses and paths, which is why it goes in the vault. The snapshot writes the same bytes when nothing changed, but `ansible-vault encrypt` salts each run, so the encrypted file changes in git every time; compare with `ansible-vault view` to see whether a setting did. The play applies the inventory's copy, so a setting changed in the web UI since the last snapshot is put back by the next play; take a snapshot first when the VM is the source of truth. `ansible-playbook --check` after a snapshot shows no drift.

```yaml
istota_plan_secrets:
  users:
    alice: {karakeep: {api_key: "{{ vault_alice_karakeep }}"}}
```

`istota_plan_secrets` is the plan's private sidecar: `users` maps user, service and key to a value, and `config` replaces inline config credentials by path. The config is managed, and read-only in the web config editor, only when the plan sets `meta.managed = true`: the role passes the plan's `meta` through and adds none. Secrets and the admins list are marked managed when the role sets them. Each user's `profile_mode` stays as the plan says. A snapshot says `update`, which neither adds nor releases a field lock; set `managed` on the profiles the inventory should own.

`istota_plan_prune: true` removes the folders, briefings, jobs and heartbeat checks a user's plan does not list, from the collections it does list. It never removes users or credentials. `istota_plan_prune_secrets: true`, which needs `istota_plan_prune`, also removes the provisionable keys of a service a user's `secrets` entry names but does not list. Both need `istota_plan`.

An image older than this role's `istota-stack` cannot read the role's input and returns no result document. The play then fails with nothing written. Update the release first (`--tags istota_update`), then run the whole play.

### Release or latest

`istota_update_mode` picks how the VM is updated. `release` (the default) runs the signed tag in `istota_release`, verified against `istota_release_signing_key` (the project's key unless a fork sets its own), and updates only when that variable changes. `latest` follows main: each play asks `istota-stack latest-tag` for the name main's tip would install (`main-<12 hex>`, read from origin without fetching) and runs `istota-stack update --unsigned latest` only when it differs from the deployed one. Nothing is verified in `latest` mode, so no key is required and `istota_release` is ignored. Either way the rebuild after a config change uses the way the deployed tag was installed, so an unsigned tag is rebuilt unsigned.

```yaml
istota_update_mode: latest
```

To update the version without touching anything else, run the play with `--tags istota_update`: it runs the variable checks, the update for the mode, a provision from the new checkout when the update ran, and waits for the stack to be healthy, and skips the config, the secrets and the users. An update runs only when there is something new, a changed `istota_release` or a moved main. `-e istota_update_force=true` runs it anyway, as typing `istota-stack update` on the VM does, which rebuilds the images and restarts the stack. `--check` reports either.

```sh
ansible-playbook site.yml --limit istota-vm --tags istota_update
ansible-playbook site.yml --limit istota-vm --tags istota_update -e istota_update_force=true
```

### iOS push deployment

`--tags istota_push` installs an APNs signing key and updates only `[push]` in the live config. It skips the saved settings plan, user provisioning, release updates and firewall tasks. The deployed image must already include native push and HTTP/2 support. A normal role run does not select these tasks; check mode refuses before staging a credential.

```yaml
istota_apns_key_file: "{{ playbook_dir }}/files/app-keys/AuthKey.p8"
istota_push_config:
  enabled: true
  apns_key_id: ABCDEFGHIJ
  apns_team_id: KLMNOPQRST
  apns_topic: com.example.app
```

Keep the `.p8` source vault-encrypted. The role stages it with `no_log` and diff output disabled, then uses the installed stack's container privilege drop to write `config/apns/AuthKey.p8` as the service account, mode 0600. The configured path inside the container is `/data/config/apns/AuthKey.p8`. A conflicting `ISTOTA_APNS_KEY_PATH` override refuses the change. The key's P-256 format is checked locally; matching its key ID and Apple authorization still needs an APNs send.

```sh
ansible-playbook site.yml --limit istota-vm --tags istota_push -e @push-deployment.yml
```

The helper holds the deployment lock and uses the existing settings writer's lock, validation and backups. It preserves unrelated live settings, unspecified push settings and the config-management marker. A changed config or key restarts the stack through its existing restart path; an unchanged rerun does not. A partial write records a restart owed, so a corrected rerun completes the restart. The role removes its staging directory on success or failure.

Refresh the inventory settings snapshot after deployment, before the next full role run. That run still applies the saved plan and can otherwise replace the new push settings. This tag does not merge or deploy application code, send a test notification, or verify phone delivery.

### Writing the config

The plan's `config` is config.toml written as YAML, key for key: a TOML table is a mapping, an array of tables (`[[briefing_shared_blocks]]`) is a list of mappings, and the key names do not change. `config/config.example.toml` documents every key. Credentials go in `istota_secrets` (the deployment's files) or `istota_plan_secrets` (inline config credentials), never in the plan. A key left out takes the code's default, and the apply validates the whole plan through the config loader before it writes anything, so an unknown key warns and a bad value is refused.

```toml
# config.toml
[speech_gate]
mode = "classifier"

[brain.native]
model = "general"
```

```yaml
# host_vars
istota_plan:
  config:
    speech_gate: {mode: classifier}
    brain:
      native: {model: general}
```

### Moving from the old role

An estate that ran `deploy/ansible/` keeps its settings as flat `istota_*` variables in a vault. After the bare-metal import, `istota migrate role-vars` writes this role's variables from that vault and the migrated VM's own files, so the first `--check` reports no drift:

```sh
ssh vm sudo cat /srv/app/istota/config/config.toml > config.toml   # likewise host.env, .env, rclone.conf
ansible-vault view group_vars/istota.yml > old.yml
uv run istota migrate role-vars --old-vars old.yml --config config.toml \
    --host-env host.env --stack-env .env --rclone-conf rclone.conf --output new.yml
ansible-vault encrypt new.yml
shred -u old.yml config.toml rclone.conf
```

`--old-vars` repeats, later files winning, for a host_vars file on top of group_vars. The output is 0600 and only names are printed. It maps the old secret variables to `istota_secrets` and the old per-user credentials to `istota_plan_secrets`, and keeps the master key and web token key as `vault_istota_secret_key` and `vault_istota_web_token_key` (no task reads them; the vault is their only copy off the VM). It writes no `istota_plan`: the import already put the users and the config on the VM, with the paths rewritten, so the VM's own `istota snapshot --format ansible` is the plan. A Twilio or WhatsApp Cloud credential has no secrets file, so the converter refuses and writes nothing; every old variable it does not carry is listed.

A secret named in `istota_secrets` is written exactly, an empty string included. A secret left out is created empty if its file is missing and otherwise left alone, so a file the wizard or the import wrote survives a play that does not manage it.

## A playbook

```yaml
- hosts: istota
  become: true
  roles:
    - role: /path/to/istota/ansible
```

## Check mode

`ansible-playbook --check` reports drift. Both settings steps run `istota-stack apply --dry-run` against the settings the play would apply, so a changed profile, resource, briefing, job, heartbeat or credential shows as changed, and nothing is written or restarted. The secrets step runs `istota-stack put-files --dry-run`, an update, a provision or a rebuild that would run is reported as changed, and the other file writes are checked as usual. On a VM with no deployed release or no config yet, the user settings cannot be dry-run and are reported as a change.

## Tests

`tests/test_management_role.py`, in the default suite, holds the role's names to the code: the secret names to `setup_wizard.SECRET_NAMES`, the `host.env` keys to what `provision.sh` documents and reads and what `istota-stack` reads, the `.env` keys and their derived values to the wizard's, and the `istota-stack` verbs the role calls to the script's own. It also requires that no task writes below `config/` or `secrets/`, that every task whose arguments carry a secret has `no_log`, that no secret value is on an argv, and that an exit 2 counts as a change only when the command ran. It renders the role's settings envelopes and passes each through `istota apply`'s own converter, and checks the variable refusals, the `--dry-run` and prune flags, and the failure message read from a result document. The vm tier applies its proxied and nextcloud modes with this role and checks that a `--check` right after a converge changes nothing, that a link planted in `secrets/` is not written through, that a provision a dead play left undone runs on the next, that a claude_code brain gets an image with Claude Code, and that the VM's own snapshot used as `istota_plan` is no drift while a profile changed in it is drift under `--check` and is applied by a converge (`tests/vm/test_management_role.py`).
