# Ansible Role: istota

Deploys istota as a systemd service on Debian 13+.

## Prerequisites

- Debian 13+ target host
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
            # timezone is a web-UI / Nextcloud preference, not an inventory
            # field — setting it here would overwrite the user's choice on
            # every deploy (ISSUE-102).
```

## Using this role

Point your `roles_path` at the `deploy/ansible/` directory:

```ini
# ansible.cfg
[defaults]
roles_path = /path/to/istota/deploy/ansible
```

Or symlink into your existing roles directory:

```bash
ln -s /path/to/istota/deploy/ansible /path/to/roles/istota
```

## Variables

All variables with defaults are documented in `defaults/main.yml`. Key groups:

- **Core**: `istota_namespace`, `istota_home`, `istota_repo_url`
- **Nextcloud**: `istota_nextcloud_url`, `istota_nextcloud_username`, `istota_nextcloud_app_password`
- **Security**: `istota_security_mode`, `istota_security_sandbox_enabled`, `istota_use_environment_file`
- **Users**: `istota_users` (dict), `istota_admin_users` (list)
- **Scheduler**: `istota_scheduler_*` (poll intervals, worker limits, timeouts)
- **Logging**: `istota_logging_*`
- **Brain backend**: `istota_brain_kind` (`claude_code` default, or `native`), `istota_brain_native_*`, `istota_brain_source_type_overrides`. See [docs/configuration/native-brain.md](../../docs/configuration/native-brain.md) for the native-brain runbook.

## Feature flags

| Feature | Variable | Default |
|---|---|---|
| Email integration | `istota_email_enabled` | `false` |
| Browser container | `istota_browser_enabled` | `false` |
| Memory search | `istota_memory_search_enabled` | `true` |
| Sleep cycle | `istota_sleep_cycle_enabled` | `false` |
| Channel sleep cycle | `istota_channel_sleep_cycle_enabled` | `false` |
| Whisper transcription | `istota_whisper_enabled` | `false` |
| Nginx site hosting | `istota_site_enabled` | `false` |
| Node.js | `istota_nodejs_enabled` | `false` |
| Developer/GitLab | `istota_developer_enabled` | `false` |
| Database backups | `istota_backup_enabled` | `true` |
| Bubblewrap sandbox | `istota_security_sandbox_enabled` | `true` |

## Inlined dependencies

The following external role dependencies have been inlined as direct tasks. You can replace them with dedicated roles if preferred:

- **Docker**: `apt-get install docker.io docker-compose-plugin` (when `istota_browser_enabled`)
- **rclone**: `curl https://rclone.org/install.sh | bash` + config file (when `istota_configure_rclone`)
- **rclone mount**: Systemd unit for FUSE mount (when `istota_use_nextcloud_mount`)
- **nginx**: `apt-get install nginx` (when `istota_site_enabled`)
- **Node.js**: NodeSource 20.x setup (when `istota_nodejs_enabled`)

## Update mode

Skip full installation (useful for config changes or code updates):

```bash
ansible-playbook playbook.yml -e "istota_update_only=true"
```

## Post-install

Claude auth is provisioned during install from the `istota_claude_code_oauth_token` variable (generate the token with `claude setup-token`; the wizard prompts for it and the role writes the credentials file). No separate login is needed.

Only if you deployed without the token (and aren't using `ANTHROPIC_API_KEY`), authenticate manually:

```bash
sudo -u istota HOME=/srv/app/istota claude login
```

## Running the CLI on the host

The role installs a `<namespace>-run` wrapper (e.g. `istota-run`) to
`/usr/local/bin`. It self-sudoes into the service user, loads the same secret
bundle (`/etc/<namespace>/secrets.env`) and admins file the daemon uses, then
passes its arguments straight through to the `istota` CLI. The caller needs
sudo rights.

```bash
istota-run repl -u stefan        # interactive REPL as that user
istota-run list                  # any istota subcommand works
istota-run task "..." -u stefan -x
```
