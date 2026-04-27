# Ansible deployment

The Ansible role at `deploy/ansible/` is the canonical provisioning tool. It handles everything: system packages, Python environment, config files, systemd services, nginx, rclone mount, backups, and optional features.

## Prerequisites

- Debian 13+ or Ubuntu target host
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
| Sleep cycle | `istota_sleep_cycle_enabled` | `false` |
| Channel sleep cycle | `istota_channel_sleep_cycle_enabled` | `false` |
| Whisper transcription | `istota_whisper_enabled` | `false` |
| Nginx site hosting | `istota_site_enabled` | `false` |
| Node.js | `istota_nodejs_enabled` | `false` |
| Developer/GitLab | `istota_developer_enabled` | `false` |
| Database backups | `istota_backup_enabled` | `true` |
| Bubblewrap sandbox | `istota_security_sandbox_enabled` | `true` |
| Web interface | `istota_web_enabled` | `false` |
| GPS location | `istota_location_enabled` | `false` |

## Variables

All variables with defaults are in `deploy/ansible/defaults/main.yml`. Key groups:

- **Core**: `istota_namespace`, `istota_home`, `istota_repo_url`
- **Nextcloud**: `istota_nextcloud_url`, `istota_nextcloud_username`, `istota_nextcloud_app_password`
- **Security**: `istota_security_sandbox_enabled`, `istota_security_skill_proxy_enabled`, `istota_security_outbound_gate_email`, `istota_use_environment_file`
- **Users**: `istota_users` (dict), `istota_admin_users` (list)
- **Scheduler**: `istota_scheduler_*` (poll intervals, worker limits, timeouts)

## Inlined dependencies

External role dependencies are inlined as tasks:

- **Docker**: `apt-get install docker.io docker-compose-plugin` (when browser enabled)
- **rclone**: install + config (when rclone configured)
- **rclone mount**: systemd unit for FUSE mount (when mount enabled)
- **nginx**: install + config (when site enabled)
- **Node.js**: NodeSource 20.x (when Node.js enabled)

## Update mode

Skip full installation for config changes or code updates:

```bash
ansible-playbook playbook.yml -e "istota_update_only=true"
```

## Post-install

```bash
sudo -u istota HOME=/srv/app/istota claude login
```

## Adding config fields

When adding new fields to the config system:

1. Add the field to the dataclass in `config.py`
2. Update `config/config.example.toml`
3. Update `deploy/ansible/defaults/main.yml`
4. Update `deploy/ansible/templates/config.toml.j2`
