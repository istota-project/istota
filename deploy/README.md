# Deployment

Two deployment paths are available:

## Option 1: install.sh (standalone)

Single-script deployment for Debian 13+ servers. No Ansible required.

```bash
# Interactive setup wizard (recommended for first install)
sudo ./install.sh --interactive

# Update existing installation
sudo ./install.sh --update

# Use a custom settings file
sudo ./install.sh --settings /path/to/settings.toml
```

### Settings file

The interactive wizard writes a settings file to `/etc/istota/settings.toml`. This file drives all subsequent `--update` runs. Settings can also be overridden via environment variables with `ISTOTA_` prefix.

Example minimal settings:

```toml
home = "/srv/app/istota"
namespace = "istota"
nextcloud_url = "https://nextcloud.example.com"
nextcloud_username = "istota"
nextcloud_app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
use_nextcloud_mount = true
nextcloud_mount_path = "/srv/mount/nextcloud/content"
rclone_password_obscured = "xxxxxxx"
use_environment_file = true

[users.alice]
display_name = "Alice"
timezone = "America/New_York"
email_addresses = ["alice@example.com"]
```

See `deploy/ansible/defaults/main.yml` for the full list of available settings (use names without the `istota_` prefix).

### Config generator

`deploy/render_config.py` converts a settings file into all the config files istota needs:

```bash
python3 deploy/render_config.py --settings /etc/istota/settings.toml --output-dir /
python3 deploy/render_config.py --settings settings.toml --dry-run  # preview
```

## Option 2: Ansible

Full infrastructure-as-code deployment. See `deploy/ansible/README.md`.

```yaml
# In your playbook:
- hosts: your-server
  roles:
    - role: istota
      vars:
        istota_nextcloud_url: "https://nextcloud.example.com"
        istota_nextcloud_app_password: "{{ vault_istota_nc_password }}"
```

Point your Ansible `roles_path` at the `deploy/ansible/` directory, or symlink it into your roles directory.

## Prerequisites

- Debian 13+ (Trixie) server
- Nextcloud instance with an app password for the bot user
- Claude Code CLI subscription (authenticate after install with `sudo -u istota claude login`)

## Post-install

1. Authenticate Claude CLI: `sudo -u istota HOME=/srv/app/istota claude login`
2. Create the bot user in Nextcloud and generate an app password
3. Add users to the config and restart: `systemctl restart istota-scheduler`
4. Invite the bot user to Nextcloud Talk conversations

## Service management

```bash
systemctl status istota-scheduler
systemctl restart istota-scheduler
journalctl -u istota-scheduler -f
```
