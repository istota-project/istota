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
- Nextcloud instance with an app password for the bot user (no Nextcloud yet? [Nextcloud All-in-One](https://github.com/nextcloud/all-in-one) is the quickest path — enable Nextcloud Talk during setup)
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

## Optional features

The core install covers Talk integration, email, scheduling, and Claude Code execution. The install wizard (`--interactive`) prompts for the features below and sets them up automatically: memory search, sleep cycle, channel sleep cycle, whisper transcription, ntfy notifications, automated backups, and the browser container (including Docker installation). Fava and nginx site hosting are not covered by the wizard and require manual setup. For manual setup or customization, the reference instructions are provided here. All settings go in `/etc/istota/settings.toml`, then re-run `install.sh --update` to regenerate config.

Throughout this section, `$HOME` refers to the istota home directory (default `/srv/app/istota`) and commands are run as root unless noted otherwise.

### Browser container

Dockerized Playwright container for the web browsing skill. Provides headless browsing with optional VNC for CAPTCHA fallback.

**Settings:**

```toml
[browser]
enabled = true
api_port = 9223
vnc_port = 6080
vnc_password = "changeme"
vnc_external_url = "https://vnc.example.com:6080"  # optional, for CAPTCHA access
```

**Setup:**

```bash
# Install Docker (official convenience script, works across Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh

# Add istota to docker group
usermod -aG docker istota

# Deploy the container
cat > $HOME/browser.env <<EOF
VNC_PASSWORD=changeme
BROWSER_VNC_URL=https://vnc.example.com:6080
MAX_BROWSER_SESSIONS=3
EOF
chown istota:istota $HOME/browser.env
chmod 600 $HOME/browser.env

cat > $HOME/docker-compose.browser.yml <<EOF
services:
  browser:
    build:
      context: $HOME/src/docker/browser/
      dockerfile: Dockerfile
    container_name: istota-browser
    ports:
      - "127.0.0.1:9223:9223"
      - "0.0.0.0:6080:6080"
    shm_size: 2gb
    volumes:
      - $HOME/data/browser-profile:/data/browser-profile
    env_file:
      - $HOME/browser.env
    deploy:
      resources:
        limits:
          memory: 2G
    restart: unless-stopped
EOF
chown istota:istota $HOME/docker-compose.browser.yml

cd $HOME && docker compose -f docker-compose.browser.yml up -d --build

# Allow Docker containers to reach host (if using UFW)
ufw allow from 172.18.0.0/16 comment "Docker containers (istota)"
```

**Verify:** `curl -s http://localhost:9223/health` should return OK.

### Automated backups

SQLite database backups (every 6h) and Nextcloud file backups (nightly) with local + remote rotation.

**Setup:**

```bash
# Create backup directories
mkdir -p $HOME/data/backups/{daily,weekly}
chown -R istota:istota $HOME/data/backups

# Create remote backup dirs (requires active mount)
MOUNT=/srv/mount/nextcloud/content
mkdir -p $MOUNT/Backups/db/{daily,weekly}
chown -R istota:istota $MOUNT/Backups

# Deploy backup script
cat > /usr/local/bin/istota-backup.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail

DB="/srv/app/istota/data/istota.db"
LOCAL_DIR="/srv/app/istota/data/backups"
REMOTE_DIR="/srv/mount/nextcloud/content/Backups"
MOUNT_PATH="/srv/mount/nextcloud/content"
DAILY_RETENTION=7
WEEKLY_RETENTION=4
RCLONE_CONFIG="/srv/app/istota/.config/rclone/rclone.conf"
RCLONE_REMOTE="nextcloud"
RCLONE_BACKUP_PATH="Backups"

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
DAY_OF_WEEK=$(date +%u)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

mount_available() { mountpoint -q "$MOUNT_PATH" 2>/dev/null; }

backup_db() {
    log "Starting database backup"
    local tmp_file
    tmp_file=$(mktemp "${LOCAL_DIR}/backup-XXXXXX.db")
    trap 'rm -f "$tmp_file"' RETURN

    sqlite3 "$DB" ".backup '$tmp_file'"
    local check
    check=$(sqlite3 "$tmp_file" "PRAGMA integrity_check;")
    if [ "$check" != "ok" ]; then
        log "ERROR: Backup integrity check failed: $check"
        return 1
    fi

    local backup_name="istota-${TIMESTAMP}.db.gz"
    gzip -c "$tmp_file" > "${LOCAL_DIR}/daily/${backup_name}"
    log "Local daily backup: ${LOCAL_DIR}/daily/${backup_name}"

    if [ "$DAY_OF_WEEK" -eq 7 ]; then
        cp "${LOCAL_DIR}/daily/${backup_name}" "${LOCAL_DIR}/weekly/${backup_name}"
        log "Local weekly backup saved"
    fi

    if mount_available; then
        cp "${LOCAL_DIR}/daily/${backup_name}" "${REMOTE_DIR}/db/daily/${backup_name}"
        [ "$DAY_OF_WEEK" -eq 7 ] && cp "${LOCAL_DIR}/weekly/${backup_name}" "${REMOTE_DIR}/db/weekly/${backup_name}"
        log "Remote backup copied"
    fi

    find "${LOCAL_DIR}/daily" -name "istota-*.db.gz" -mtime +${DAILY_RETENTION} -delete 2>/dev/null || true
    find "${LOCAL_DIR}/weekly" -name "istota-*.db.gz" -mtime +$((WEEKLY_RETENTION * 7)) -delete 2>/dev/null || true
    if mount_available; then
        find "${REMOTE_DIR}/db/daily" -name "istota-*.db.gz" -mtime +${DAILY_RETENTION} -delete 2>/dev/null || true
        find "${REMOTE_DIR}/db/weekly" -name "istota-*.db.gz" -mtime +$((WEEKLY_RETENTION * 7)) -delete 2>/dev/null || true
    fi
    log "Database backup complete"
}

backup_files() {
    log "Starting files backup"
    rclone sync --config "$RCLONE_CONFIG" "${RCLONE_REMOTE}:Users" "${RCLONE_REMOTE}:${RCLONE_BACKUP_PATH}/files/Users" --exclude "*/shared/**" --verbose 2>&1 | while IFS= read -r line; do log "  $line"; done
    rclone sync --config "$RCLONE_CONFIG" "${RCLONE_REMOTE}:Channels" "${RCLONE_REMOTE}:${RCLONE_BACKUP_PATH}/files/Channels" --verbose 2>&1 | while IFS= read -r line; do log "  $line"; done
    log "Files backup complete"
}

case "${1:-}" in
    db) backup_db ;;
    files) backup_files ;;
    all) backup_db; backup_files ;;
    *) echo "Usage: $0 {db|files|all}" >&2; exit 1 ;;
esac
SCRIPT
chmod 755 /usr/local/bin/istota-backup.sh

# Deploy cron (DB every 6h, files nightly at 3am)
cat > /etc/cron.d/istota-backup <<EOF
MAILTO=""
0 */6 * * * istota /usr/local/bin/istota-backup.sh db >> /var/log/istota/istota-backup.log 2>&1
0 3 * * * istota /usr/local/bin/istota-backup.sh files >> /var/log/istota/istota-backup.log 2>&1
EOF
```

**Verify:** `sudo -u istota /usr/local/bin/istota-backup.sh db` should create a gzipped backup in `$HOME/data/backups/daily/`.

### Fava ledger viewer

Per-user web UI for browsing beancount ledger files. Each user with a ledger resource and a configured port gets a systemd service.

**Prerequisites:** User must have a `ledger` resource configured.

**Settings (per-user):**

```toml
[users.alice]
fava_port = 5010
```

**Setup (per-user):**

```bash
USER_ID=alice
FAVA_PORT=5010
MOUNT=/srv/mount/nextcloud/content
# Adjust this path to match the user's ledger resource path
LEDGER_PATH="$MOUNT/Users/$USER_ID/beancount/main.beancount"

cat > /etc/systemd/system/istota-fava-${USER_ID}.service <<EOF
[Unit]
Description=Istota Fava Ledger Viewer ($USER_ID)
After=network.target mount-nextcloud.service
Wants=mount-nextcloud.service

[Service]
Type=simple
User=istota
Group=istota
ExecStart=/srv/app/istota/.venv/bin/fava --host 0.0.0.0 --port $FAVA_PORT $LEDGER_PATH
Restart=always
RestartSec=5
Environment=HOME=/srv/app/istota
StandardOutput=journal
StandardError=journal
SyslogIdentifier=istota-fava-$USER_ID
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/srv/app/istota/tmp
ReadWritePaths=/var/log/istota
ReadOnlyPaths=$MOUNT

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now istota-fava-${USER_ID}

# Optional: allow istota user to restart fava (for auto-reload after ledger changes)
echo "istota ALL=(root) NOPASSWD: /bin/systemctl restart istota-fava-*" > /etc/sudoers.d/istota-fava
chmod 440 /etc/sudoers.d/istota-fava
visudo -cf /etc/sudoers.d/istota-fava
```

**Verify:** `curl -s http://localhost:5010/` should return the Fava web interface. Restrict access to private networks (VPN/wireguard) — Fava has no authentication.

### Nginx site hosting

Serve per-user static sites from Nextcloud-managed HTML directories. Users edit their site files in Nextcloud; nginx serves them.

**Settings:**

```toml
[site]
enabled = true
hostname = "istota.example.com"
```

Per-user opt-in:

```toml
[users.alice]
site_enabled = true
```

**Setup:**

```bash
HOSTNAME=istota.example.com
MOUNT=/srv/mount/nextcloud/content

apt-get install -y nginx
usermod -aG www-data istota
usermod -aG nextcloud-mount www-data

# Create site root
mkdir -p /srv/app/istota/html
chown istota:www-data /srv/app/istota/html

# Create per-user HTML directories (repeat for each user)
mkdir -p $MOUNT/Users/alice/istota/html
chown istota:www-data $MOUNT/Users/alice/istota/html

# Deploy nginx config
cat > /etc/nginx/conf.d/${HOSTNAME}.conf <<'EOF'
server {
    listen 80;
    server_name istota.example.com;
    root /srv/app/istota/html;
    index index.html;

    # Per-user sites at /~username/
    location ~ ^/~([^/]+)(/.*)?$ {
        alias /srv/mount/nextcloud/content/Users/$1/istota/html$2;
        index index.html;
    }
}
EOF

systemctl reload nginx
```

**Verify:** `curl -s http://istota.example.com/` should return the site root. User sites at `http://istota.example.com/~alice/`. Add TLS via certbot or a reverse proxy.

### Whisper audio transcription

Transcribe audio attachments using faster-whisper (CPU, int8 quantization). Pre-downloads the model at install time.

**Settings:**

```toml
[whisper]
enabled = true
model = "small"         # Model to pre-download: tiny/base/small/medium/large-v3
max_model = "small"     # Max model for auto-selection at runtime
```

**Setup:**

```bash
# Install the optional dependency group
cd /srv/app/istota/src && uv sync --extra whisper
chown -R istota:istota /srv/app/istota/src/.venv

# Pre-download the model
sudo -u istota HOME=/srv/app/istota \
  /srv/app/istota/.venv/bin/python -m istota.skills.whisper download small

# Add WHISPER_MAX_MODEL to the systemd service environment
# Edit /etc/systemd/system/istota-scheduler.service and add:
#   Environment=WHISPER_MAX_MODEL=small
# Then reload:
systemctl daemon-reload && systemctl restart istota-scheduler
```

**Verify:** Send a voice memo to the bot in Talk — it should transcribe before processing.

### ntfy push notifications

One-way push notifications via ntfy.sh (or self-hosted ntfy). Used for heartbeat alerts, scheduled job failures, and other system events.

**Settings:**

```toml
[ntfy]
enabled = true
server_url = "https://ntfy.sh"
topic = "istota-alerts"
token = "tk_xxxxxxxxxx"     # bearer token (preferred)
# Or use basic auth:
# username = "user"
# password = "pass"
priority = 3                 # 1=min, 3=default, 5=max
```

Per-user topic override:

```toml
[users.alice]
ntfy_topic = "alice-alerts"
```

**Setup:** Add the settings above and re-run `install.sh --update`. No additional system setup needed — ntfy is a remote service.

**Verify:** `curl -d "test" https://ntfy.sh/istota-alerts` should send a notification to your subscribed device.
