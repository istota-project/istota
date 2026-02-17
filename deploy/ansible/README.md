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
            timezone: "America/New_York"
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

## Feature flags

| Feature | Variable | Default |
|---|---|---|
| Email integration | `istota_email_enabled` | `false` |
| Browser container | `istota_browser_enabled` | `false` |
| Memory search | `istota_memory_search_enabled` | `true` |
| Sleep cycle | `istota_sleep_cycle_enabled` | `false` |
| Channel sleep cycle | `istota_channel_sleep_cycle_enabled` | `false` |
| Whisper transcription | `istota_whisper_enabled` | `false` |
| Fava ledger viewer | `istota_fava_enabled` | `false` |
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

Authenticate the Claude CLI:

```bash
sudo -u istota HOME=/srv/app/istota claude login
```
