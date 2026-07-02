# Bare metal quickstart

Bare metal is the canonical deployment. It runs Istota natively on a Debian/Ubuntu VM and connects to an existing Nextcloud instance. If you don't have a Nextcloud, use the [Docker quickstart](quickstart-docker.md) instead — it bundles its own.

Requirements: a Nextcloud instance, a Debian/Ubuntu VM, and a model backend (a Claude Code subscription/OAuth token, or any OpenAI-compatible endpoint — see the [native brain runbook](../configuration/native-brain.md)).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash
```

That's the whole install. The one-liner clones the repo, installs prerequisites, and runs an interactive wizard that walks you through connecting to Nextcloud, setting up users, and choosing optional features.

Prefer to read before you pipe? Download and inspect it first:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

When you're done, see [post-install](post-install.md) to authenticate Claude and send your first message.

## What install.sh does

The dispatcher clones the repo (when curl-piped) and hands off to `deploy/install.sh`, a bootstrap that:

1. Ensures Python 3.11+, pipx, and ansible-core are installed
2. Installs required Ansible collections (`community.general`, `ansible.posix`)
3. Gets the Ansible role (from local repo or cloned copy)
4. Runs the setup wizard by default (writes `/etc/istota/settings.toml`); skipped under `--headless` or `--update`
5. Converts `settings.toml` to Ansible vars via `settings_to_vars.py`
6. Runs `ansible-playbook` in local connection mode

## Common commands

```bash
# Default: runs the interactive setup wizard
sudo ./install.sh

# Skip the wizard — requires existing settings (or --settings PATH)
sudo ./install.sh --headless

# Update existing installation
sudo ./install.sh --update

# Preview changes without applying
sudo ./install.sh --dry-run

# Use a custom settings file
sudo ./install.sh --settings /path/to/settings.toml
```

## Settings file

The wizard writes `/etc/istota/settings.toml`. This file drives all subsequent `--update` runs. Minimal example:

```toml
home = "/srv/app/istota"
namespace = "istota"
nextcloud_url = "https://nextcloud.example.com"
nextcloud_username = "istota"
nextcloud_app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
use_nextcloud_mount = true
nextcloud_mount_path = "/srv/mount/nextcloud/content"
use_environment_file = true

[users.alice]
display_name = "Alice"
timezone = "America/New_York"
email_addresses = ["alice@example.com"]
```

See `deploy/ansible/defaults/main.yml` for the full list of available settings.

## Using Ansible directly

For infrastructure-as-code workflows, use the Ansible role without `install.sh`:

```yaml
- hosts: your-server
  roles:
    - role: istota
      vars:
        istota_nextcloud_url: "https://nextcloud.example.com"
        istota_nextcloud_app_password: "{{ vault_istota_nc_password }}"
```

Point your `roles_path` at `deploy/ansible/`, or symlink it into your roles directory. See [Ansible deployment](../deployment/ansible.md) for details.

## Prerequisites

- Debian 12+ or Ubuntu server
- Nextcloud instance with an app password for the bot user
- A model backend: a Claude Code subscription/OAuth token (default), or any OpenAI-compatible endpoint via the [native brain](../configuration/native-brain.md)

## Optional features

The wizard prompts for these and configures them automatically:

- Memory search (semantic search over conversations)
- Sleep cycle (nightly memory extraction)
- Whisper (audio transcription)
- GPS location tracking
- Automated backups
- Browser container (web browsing via Docker)

Features requiring manual Ansible vars:

- Nginx site hosting
- Web interface (Nextcloud OAuth2)
- Developer skill (Git/GitLab/GitHub)
- Auto-update

All settings go in `/etc/istota/settings.toml`, then re-run `install.sh --update` to apply.

## Next steps

See [post-install](post-install.md) for authenticating Claude and testing.
