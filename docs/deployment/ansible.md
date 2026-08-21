# Ansible deployment

The Ansible role at `deploy/ansible/` is the canonical provisioning tool. It handles everything: system packages, Python environment, config files, systemd services, nginx, rclone mount, backups, and optional features.

## Prerequisites

- Debian 13+ or Ubuntu target host (the role's own support target; the standalone `deploy/install.sh` path only warns below Debian 12)
- Nextcloud instance with app password
- Ansible 2.14+ with `community.general` and `ansible.posix` collections

## Example playbook

```yaml
- hosts: your-server
  become: yes
  roles:
    - role: istota
      vars:
        istota_nextcloud_url: "https://nextcloud.example.com"
        istota_nextcloud_app_password: "{{ vault_istota_nc_password }}"
        istota_rclone_password_obscured: "{{ vault_rclone_password }}"
        istota_admin_users:
          - alice
        istota_users:
          alice:
            display_name: "Alice"
            email_addresses: ["alice@example.com"]
            timezone: "America/New_York"
```

## Using the role

Point `roles_path` at `deploy/ansible/`:

```ini
# ansible.cfg
[defaults]
roles_path = /path/to/istota/deploy/ansible
```

Or symlink into your roles directory:

```bash
ln -s /path/to/istota/deploy/ansible /path/to/roles/istota
```

## Feature flags

| Feature | Variable | Default |
|---|---|---|
| Email | `istota_email_enabled` | `false` |
| Browser container | `istota_browser_enabled` | `false` |
| Memory search | `istota_memory_search_enabled` | `true` |
| Sleep cycle | `istota_sleep_cycle_enabled` | `true` |
| Channel sleep cycle | `istota_channel_sleep_cycle_enabled` | `true` |
| Whisper transcription | `istota_whisper_enabled` | `false` |
| Static web root at `/` (nginx) | `istota_web_root_enabled` | `true` |
| Node.js | `istota_nodejs_enabled` | `false` |
| Developer/GitLab | `istota_developer_enabled` | `false` |
| Database backups | `istota_backup_enabled` | `true` |
| Journal size cap | `istota_journald_manage` | `true` |
| auditd log rotation | `istota_auditd_manage` | `true` |
| zram swap | `istota_zram_enabled` | `true` |
| Disk swapfile (second tier) | `istota_swapfile_enabled` | `false` |
| Default Talk rooms | `istota_provision_talk_rooms` | `true` |
| Bubblewrap sandbox | `istota_security_sandbox_enabled` | `true` |
| Web interface | `istota_web_enabled` | `true` |
| GPS location | `istota_location_enabled` | `false` |

## Variables

All variables with defaults are in `deploy/ansible/defaults/main.yml`. Key groups:

- **Core**: `istota_namespace`, `istota_home`, `istota_repo_url`
- **Nextcloud**: `istota_nextcloud_url`, `istota_nextcloud_username`, `istota_nextcloud_app_password`
- **Security**: `istota_security_sandbox_enabled`, `istota_use_environment_file`
- **Users**: `istota_users` (dict), `istota_admin_users` (list)
- **Scheduler**: `istota_scheduler_*` (poll intervals, worker limits, timeouts)
- **Web**: `istota_web_enabled`, `istota_web_port`, `istota_web_chat_max_attachment_mb`, `istota_web_graceful_shutdown_seconds`, `istota_web_stop_timeout_seconds`
- **Email**: `istota_email_enabled`, `istota_email_outbound_approval_floor`, plus per-user `outbound_approval` and `external_turn_display` keys inside `istota_users`

`istota_email_outbound_approval_floor` (default **`"untrusted"`**) is the [outbound approval gate](../features/email.md#the-outbound-approval-gate)'s floor, and the role is the only supported place to change it — a hand edit to `config.toml` is overwritten on the next run. **Quote the value.** `off` unquoted is a YAML boolean: it renders `outbound_approval_floor = "False"`, which the daemon refuses to load. The play asserts the floor and each per-user `outbound_approval` before templating, so a bad value fails naming the variable rather than leaving an unloadable config on disk for the next restart to find.

Per-user `outbound_approval` / `external_turn_display` under `istota_users` are passed to `istota user ensure`, not templated into `[users.X]` — the TOML keys seed only a user with no profile row yet, while the CLI flags update an existing one.

Two web variables are worth knowing about before changing them:

`istota_web_chat_max_attachment_mb` (default **100**, against an application default of 25) feeds **two** consumers — the `[web.chat] max_attachment_mb` setting in `config.toml` and nginx's `client_max_body_size`. Do not split them: if nginx's ceiling is the lower of the two it rejects the upload with its own HTML error page, which the browser client cannot parse into a message.

`istota_web_graceful_shutdown_seconds` bounds uvicorn's wait for open connections. It matters because the web chat room stream is a session-lived SSE connection whose generator exits only on client disconnect — which a server shutdown does not trigger — so without the flag a restart with any browser tab open sat out the full `TimeoutStopSec` and was eventually SIGKILLed. Note the unit template is skipped in web-only mode, so a changed value lands on the next full or `istota_update_only` run.

## The clone credential

A private `istota_repo_url` needs a token to clone and to fetch updates. That token is **not** interpolated into the URL. It used to be, which persisted it as `remote.origin.url` and expanded it into the argument vector of `git-remote-https` on every fetch — one every two minutes from the auto-update cron, readable by anyone with root on the box.

It now lives in a 0600 root-only file read by a six-line credential helper, registered per-host at system scope, with the clone using the bare URL. A deploy rewrites the old value out of hosts already set up the other way.

**Rotate the token if your host predates this.** It sat in `.git/config` and in process arguments for the life of the old shape, so the fix removes the exposure going forward but cannot undo it.

The helper answers `get` and ignores every other verb. Git's own `store --file=` was the obvious choice and is deliberately not used: it implements `erase`, and git calls `erase` on any 401, so one revoked or freshly rotated token would truncate the file and leave the cron fetch failing silently — worst in the deploy right after a rotation. `store` also rewrites its file on a successful auth, which would let an unrelated root git operation against the same host swap the deploy credential.

`GIT_TERMINAL_PROMPT=0` is set on the clone, the tag fetch and the update script. With the token out of the URL, a missing or rejected credential makes git prompt rather than fail, and a prompt inside the update script would hang the run while it holds its flock — after which every later run exits silently at the lock and updates stop with nothing reported.

## Host memory headroom

The role gives the host swap and puts a soft ceiling on the istota units. Both came out of an August 2026 outage: a host running with `Total swap = 0` had nowhere to put cold memory, threw away the page cache every program was running from, and spent 41 minutes reading itself back off disk.

| Variable | Default | Purpose |
|---|---|---|
| `istota_zram_enabled` | `true` | Install `systemd-zram-generator` and configure a zram swap device |
| `istota_zram_size` | `"ram / 2"` | The device's **uncompressed** capacity, not its RAM cost |
| `istota_zram_algorithm` | `"zstd"` | Compression algorithm |
| `istota_zram_priority` | `100` | Swap priority; Linux prefers the higher number |
| `istota_swapfile_enabled` | `false` | Optional second-tier disk swapfile |
| `istota_swapfile_size_mb` | `2048` | Its size |
| `istota_swapfile_path` | `/swapfile` | Where it lives |
| `istota_swapfile_priority` | `10` | Below the zram priority, so zram fills first |
| `istota_scheduler_memory_high` | `"5G"` | `MemoryHigh=` on the scheduler unit (`""` omits it) |
| `istota_web_memory_high` | `"1G"` | `MemoryHigh=` on the web and webhooks units |
| `istota_scheduler_cpu_weight` | `50` | `CPUWeight=` on the scheduler unit |

zram rather than a disk swapfile because the disk was already the saturated resource, at roughly 1.7 GB/s of forced re-reads. Compressed in-RAM swap adds no disk traffic. Sizing is worth reading twice: `zram-size` sets the device's uncompressed capacity, so `ram / 2` on an 8 GB box is a ~4 GB swap device costing about 1.3 GB of RAM at typical compression ratios.

`MemoryHigh`, not `MemoryMax`. Past the limit the kernel puts the cgroup under heavy reclaim pressure and slows it; a hard cap would kill it, and killing the daemon takes everything down. The scheduler's figure covers the daemon plus every `claude` subprocess and its children. `CPUWeight` sits below the systemd default of 100 so every other unit wins under contention; no `CPUQuota` is set, because PSI showed the cores idle-waiting on memory rather than oversubscribed.

Setting `istota_zram_enabled: false` makes every zram task a no-op, so a host where the operator arranged swap another way is left as it is. Note the asymmetry: false means "this role does not manage swap", not "tear down the swap this role previously set up". Flipping true to false on a host that already has it leaves the device in place — disable `systemd-zram-setup@zram0` and delete `/etc/systemd/zram-generator.conf` by hand.

The scheduler's own [host memory breadcrumb](../architecture/scheduler.md#host-memory-breadcrumb) is the matching instrument: `istota_scheduler_host_pressure_enabled` (default `true`) and `istota_scheduler_host_pressure_breadcrumb_interval` (default `300`).

## Disk growth

Three things on the host grew without a bound until this was fixed, and between them they filled a root disk.

| Variable | Default | Purpose |
|---|---|---|
| `istota_journald_manage` | `true` | Cap the system journal, which otherwise defaults to a tenth of the disk |
| `istota_journald_max_use` | `"500M"` | Total journal size across all files |
| `istota_journald_max_file_size` | `"50M"` | Per-file cap, so rotation stays granular |
| `istota_auditd_manage` | `true` | Rotate and delete audit logs, which were set to rotate forever |
| `istota_auditd_num_logs` | `5` | File count; the cap is this times `istota_auditd_max_log_file` |
| `istota_auditd_max_log_file` | `6` | MB per file |
| `istota_claude_versions_keep` | `2` | Old `claude` CLI builds to keep, at ~320 MB each |

Audit files stranded above the new limit are removed on the next deploy. Setting either `_manage` variable to false hands that log back to you. `istota_claude_versions_keep` keeps at least one build to roll back to after a bad release, and the build currently in use is never removed even when it is the older one.

## Default Talk rooms

`istota_provision_talk_rooms` (default `true`) creates `#general`, `#logs` and `#alerts` for each user on deploy and fills in the channel tokens, which a bare-metal install previously had no way to get — the setting that turns the execution log on asked for a room token the operator could not know yet. It calls `istota nextcloud provision-rooms` and is idempotent.

The rooms are private group rooms, not public ones: a public room is joinable by anyone holding its link, which is wrong for rooms carrying an execution log and security alerts. Rooms that already exist are reused untouched, so this only affects new installs.

A channel you already set is left alone, and so is one you deliberately cleared — turning the execution log off in the web UI stays off across deploys. Also gated on `istota_talk_enabled` and a non-empty app password.

## Inlined dependencies

External role dependencies are inlined as tasks:

- **Docker**: `apt-get install docker.io docker-compose-plugin` (when browser enabled)
- **rclone**: install + config (when rclone configured)
- **rclone mount**: systemd unit for FUSE mount (when mount enabled)
- **nginx**: install + config (when location or web enabled)
- **Node.js**: NodeSource 20.x (when Node.js enabled)

## Update mode

Skip full installation for config changes or code updates:

```bash
ansible-playbook playbook.yml -e "istota_update_only=true"
```

## Post-install

Claude auth is provisioned during install from the `istota_claude_code_oauth_token` variable (generate the token with `claude setup-token`; the wizard prompts for it and the role writes the credentials file). No separate login is needed.

Only if you deployed without the token (and aren't using `ANTHROPIC_API_KEY`), authenticate manually:

```bash
sudo -u istota HOME=/srv/app/istota claude login
```

## Adding config fields

When adding new fields to the config system:

1. Add the field to the dataclass in `config.py`
2. Update `config/config.example.toml`
3. Update `deploy/ansible/defaults/main.yml`
4. Update `deploy/ansible/templates/config.toml.j2`
