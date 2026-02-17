#!/bin/bash
# Istota deployment script
# Installs or updates istota on a Debian 13+ server
#
# Usage:
#   install.sh [OPTIONS]
#     --interactive     Guided setup wizard (writes settings file)
#     --update          Update only (skip system setup, just pull + config + restart)
#     --settings PATH   Settings file path (default: /etc/istota/settings.toml)
#     --home PATH       Override install directory
#     --help            Show this help
#
# Settings can also be provided via environment variables (ISTOTA_ prefix).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults
SETTINGS_FILE="${ISTOTA_SETTINGS_FILE:-/etc/istota/settings.toml}"
ISTOTA_HOME="${ISTOTA_HOME:-/srv/app/istota}"
ISTOTA_NAMESPACE="${ISTOTA_NAMESPACE:-istota}"
ISTOTA_USER="${ISTOTA_USER:-$ISTOTA_NAMESPACE}"
ISTOTA_GROUP="${ISTOTA_GROUP:-$ISTOTA_NAMESPACE}"
REPO_URL="${ISTOTA_REPO_URL:-https://github.com/alicekubicki/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"
UPDATE_ONLY=false
INTERACTIVE=false

# ============================================================
# Helpers
# ============================================================

info()  { echo -e "\033[1;34m==>\033[0m $*"; }
ok()    { echo -e "\033[1;32m  ✓\033[0m $*"; }
warn()  { echo -e "\033[1;33m  !\033[0m $*"; }
error() { echo -e "\033[1;31mERROR:\033[0m $*" >&2; }
die()   { error "$@"; exit 1; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "This script must be run as root (or with sudo)"
    fi
}

command_exists() {
    command -v "$1" &>/dev/null
}

prompt_value() {
    local varname="$1" prompt="$2" default="${3:-}"
    local value
    if [ -n "$default" ]; then
        read -rp "  $prompt [$default]: " value
        value="${value:-$default}"
    else
        read -rp "  $prompt: " value
    fi
    eval "$varname=\"$value\""
}

prompt_bool() {
    local varname="$1" prompt="$2" default="${3:-n}"
    local value
    if [ "$default" = "y" ]; then
        read -rp "  $prompt [Y/n]: " value
        value="${value:-y}"
    else
        read -rp "  $prompt [y/N]: " value
        value="${value:-n}"
    fi
    case "$value" in
        [yY]*) eval "$varname=true" ;;
        *)     eval "$varname=false" ;;
    esac
}

prompt_secret() {
    local varname="$1" prompt="$2"
    local value
    read -rsp "  $prompt: " value
    echo
    eval "$varname=\"$value\""
}

# ============================================================
# Parse arguments
# ============================================================

while [ $# -gt 0 ]; do
    case "$1" in
        --interactive) INTERACTIVE=true; shift ;;
        --update)      UPDATE_ONLY=true; shift ;;
        --settings)    SETTINGS_FILE="$2"; shift 2 ;;
        --home)        ISTOTA_HOME="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,/^$/s/^# \?//p' "$0"
            exit 0 ;;
        *) die "Unknown option: $1. Use --help for usage." ;;
    esac
done

# ============================================================
# Interactive setup wizard
# ============================================================

run_interactive() {
    echo
    info "Istota Setup Wizard"
    echo

    prompt_value ISTOTA_HOME "Install directory" "$ISTOTA_HOME"
    prompt_value ISTOTA_NAMESPACE "Namespace (sets user, group, paths)" "$ISTOTA_NAMESPACE"
    ISTOTA_USER="$ISTOTA_NAMESPACE"
    ISTOTA_GROUP="$ISTOTA_NAMESPACE"

    echo
    info "Nextcloud settings (required)"
    local nc_url nc_username nc_app_password
    prompt_value nc_url "Nextcloud URL (e.g. https://nextcloud.example.com)" ""
    prompt_value nc_username "Nextcloud username" "$ISTOTA_NAMESPACE"
    prompt_secret nc_app_password "Nextcloud app password"

    echo
    local use_mount mount_path rclone_password_obscured
    prompt_bool use_mount "Mount Nextcloud via rclone FUSE?" "y"
    if [ "$use_mount" = "true" ]; then
        prompt_value mount_path "Mount path" "/srv/mount/nextcloud/content"
        echo
        echo "  Generate obscured password with: rclone obscure \"your-app-password\""
        prompt_secret rclone_password_obscured "Obscured rclone password"
    else
        mount_path="/srv/mount/nextcloud/content"
        rclone_password_obscured=""
    fi

    echo
    info "Optional features"
    local email_enabled browser_enabled memory_search_enabled sleep_cycle_enabled
    prompt_bool email_enabled "Enable email integration?" "n"
    prompt_bool browser_enabled "Enable web browser container?" "n"
    prompt_bool memory_search_enabled "Enable memory search (BM25 + vector)?" "y"
    prompt_bool sleep_cycle_enabled "Enable nightly memory extraction?" "n"

    local email_imap_host email_imap_user email_imap_password email_smtp_host email_bot_address
    email_imap_host="" email_imap_user="" email_imap_password=""
    email_smtp_host="" email_bot_address=""
    if [ "$email_enabled" = "true" ]; then
        echo
        info "Email settings"
        prompt_value email_imap_host "IMAP host" ""
        prompt_value email_imap_user "IMAP username" ""
        prompt_secret email_imap_password "IMAP password"
        prompt_value email_smtp_host "SMTP host" "$email_imap_host"
        prompt_value email_bot_address "Bot email address" "$email_imap_user"
    fi

    echo
    info "Users"
    echo "  Define at least one user. Enter blank user ID to finish."
    local users_block=""
    while true; do
        local uid uname utz uemail
        prompt_value uid "User ID (e.g. alice)" ""
        [ -z "$uid" ] && break
        prompt_value uname "Display name" "$uid"
        prompt_value utz "Timezone" "UTC"
        prompt_value uemail "Email (optional)" ""
        users_block+="
[users.$uid]
display_name = \"$uname\"
timezone = \"$utz\"
"
        if [ -n "$uemail" ]; then
            users_block+="email_addresses = [\"$uemail\"]
"
        fi
    done

    echo
    info "Admin users"
    echo "  Leave blank for all users to be admins."
    local admin_line
    prompt_value admin_line "Admin user IDs (comma-separated)" ""
    local admin_block="admin_users = []"
    if [ -n "$admin_line" ]; then
        admin_block="admin_users = [$(echo "$admin_line" | sed 's/[[:space:]]*,[[:space:]]*/", "/g; s/^/"/; s/$/"/' )]"
    fi

    # Write settings file
    local settings_dir
    settings_dir="$(dirname "$SETTINGS_FILE")"
    mkdir -p "$settings_dir"

    cat > "$SETTINGS_FILE" <<TOML
# Istota settings - generated by install.sh interactive wizard
# Re-run install.sh --update to apply changes

home = "$ISTOTA_HOME"
namespace = "$ISTOTA_NAMESPACE"
repo_url = "$REPO_URL"
repo_branch = "$REPO_BRANCH"
use_environment_file = true

nextcloud_url = "$nc_url"
nextcloud_username = "$nc_username"
nextcloud_app_password = "$nc_app_password"

use_nextcloud_mount = $use_mount
nextcloud_mount_path = "$mount_path"
rclone_password_obscured = "$rclone_password_obscured"

$admin_block

[email]
enabled = $email_enabled
imap_host = "$email_imap_host"
imap_user = "$email_imap_user"
imap_password = "$email_imap_password"
smtp_host = "$email_smtp_host"
bot_email = "$email_bot_address"

[browser]
enabled = $browser_enabled

[memory_search]
enabled = $memory_search_enabled

[sleep_cycle]
enabled = $sleep_cycle_enabled

$users_block
TOML

    chmod 600 "$SETTINGS_FILE"
    ok "Settings written to $SETTINGS_FILE"
    echo
}

# ============================================================
# System setup (full install only)
# ============================================================

setup_system() {
    info "Installing system dependencies"
    apt-get update -qq
    apt-get install -y -qq \
        git curl sqlite3 python3 python3-venv \
        tesseract-ocr \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        bubblewrap
    ok "System packages installed"
}

setup_uv() {
    if command_exists uv; then
        ok "uv already installed"
        return
    fi
    info "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="/root/.local/bin:$PATH"
    ok "uv installed"
}

setup_claude_cli() {
    if command_exists claude; then
        ok "Claude CLI already installed"
        return
    fi
    info "Installing Claude CLI"
    HOME="$ISTOTA_HOME" curl -fsSL https://claude.ai/install.sh | bash
    if [ -f "$ISTOTA_HOME/.local/bin/claude" ] && [ ! -f /usr/local/bin/claude ]; then
        ln -sf "$ISTOTA_HOME/.local/bin/claude" /usr/local/bin/claude
    fi
    ok "Claude CLI installed"
    warn "Remember to authenticate: sudo -u $ISTOTA_USER HOME=$ISTOTA_HOME claude login"
}

setup_rclone() {
    if command_exists rclone; then
        ok "rclone already installed"
    else
        info "Installing rclone"
        curl -s https://rclone.org/install.sh | bash
        ok "rclone installed"
    fi

    # Write rclone config if we have settings
    if [ -f "$SETTINGS_FILE" ]; then
        local nc_url nc_user rclone_pass rclone_remote
        nc_url=$(python3 -c "
import tomllib, sys
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('nextcloud_url', ''))
" 2>/dev/null || echo "")
        nc_user=$(python3 -c "
import tomllib, sys
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('nextcloud_username', '$ISTOTA_NAMESPACE'))
" 2>/dev/null || echo "$ISTOTA_NAMESPACE")
        rclone_pass=$(python3 -c "
import tomllib, sys
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('rclone_password_obscured', ''))
" 2>/dev/null || echo "")
        rclone_remote=$(python3 -c "
import tomllib, sys
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('rclone_remote', 'nextcloud'))
" 2>/dev/null || echo "nextcloud")

        if [ -n "$nc_url" ] && [ -n "$rclone_pass" ]; then
            local rclone_dir="$ISTOTA_HOME/.config/rclone"
            mkdir -p "$rclone_dir"
            cat > "$rclone_dir/rclone.conf" <<EOF
[$rclone_remote]
type = webdav
url = ${nc_url}/remote.php/dav/files/${nc_user}/
vendor = nextcloud
user = $nc_user
pass = $rclone_pass
EOF
            chown -R "$ISTOTA_USER:$ISTOTA_GROUP" "$rclone_dir"
            chmod 600 "$rclone_dir/rclone.conf"
            ok "rclone configured"
        fi
    fi
}

setup_rclone_mount() {
    local mount_path
    mount_path=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
if s.get('use_nextcloud_mount', True):
    print(s.get('nextcloud_mount_path', '/srv/mount/nextcloud/content'))
" 2>/dev/null || echo "")

    [ -z "$mount_path" ] && return 0

    info "Setting up Nextcloud FUSE mount"
    mkdir -p "$mount_path"

    # Create mount group
    if ! getent group nextcloud-mount &>/dev/null; then
        groupadd --system nextcloud-mount
    fi
    usermod -aG nextcloud-mount "$ISTOTA_USER"

    # Enable allow_other in FUSE
    if [ -f /etc/fuse.conf ]; then
        sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
        if ! grep -q '^user_allow_other' /etc/fuse.conf; then
            echo 'user_allow_other' >> /etc/fuse.conf
        fi
    fi

    local rclone_remote
    rclone_remote=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('rclone_remote', 'nextcloud'))
" 2>/dev/null || echo "nextcloud")

    # Deploy mount service
    cat > /etc/systemd/system/mount-nextcloud.service <<EOF
[Unit]
Description=Nextcloud rclone FUSE mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=$ISTOTA_USER
Group=$ISTOTA_GROUP
ExecStartPre=/bin/mkdir -p $mount_path
ExecStart=/usr/bin/rclone mount \\
  --config $ISTOTA_HOME/.config/rclone/rclone.conf \\
  --allow-other \\
  --vfs-cache-mode full \\
  --vfs-cache-max-age 1h \\
  --dir-cache-time 5s \\
  --poll-interval 10s \\
  ${rclone_remote}: $mount_path
ExecStop=/bin/fusermount -u $mount_path
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable mount-nextcloud
    if ! systemctl is-active --quiet mount-nextcloud; then
        systemctl start mount-nextcloud
    fi
    ok "Nextcloud mount configured at $mount_path"
}

setup_user() {
    if id "$ISTOTA_USER" &>/dev/null; then
        ok "User $ISTOTA_USER already exists"
        return
    fi
    info "Creating system user $ISTOTA_USER"
    groupadd --system "$ISTOTA_GROUP" 2>/dev/null || true
    useradd --system --home-dir "$ISTOTA_HOME" --gid "$ISTOTA_GROUP" --shell /bin/bash --no-create-home "$ISTOTA_USER"
    ok "User created"
}

setup_directories() {
    info "Creating directories"
    local dirs=(
        "$ISTOTA_HOME"
        "$ISTOTA_HOME/data"
        "$ISTOTA_HOME/data/browser-profile"
        "$ISTOTA_HOME/tmp"
        "$ISTOTA_HOME/.config"
        "/var/log/$ISTOTA_NAMESPACE"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
        chown "$ISTOTA_USER:$ISTOTA_GROUP" "$d"
    done
    ok "Directories created"
}

setup_logrotate() {
    info "Deploying logrotate config"
    cat > "/etc/logrotate.d/$ISTOTA_NAMESPACE" <<EOF
/var/log/$ISTOTA_NAMESPACE/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    copytruncate
    create 0640 $ISTOTA_USER $ISTOTA_GROUP
}
EOF
    ok "Logrotate configured"
}

# ============================================================
# Code deployment (always runs)
# ============================================================

deploy_code() {
    info "Deploying code"

    # Git safe directory
    git config --global --get safe.directory "$ISTOTA_HOME/src" &>/dev/null ||
        git config --global --add safe.directory "$ISTOTA_HOME/src"

    if [ -d "$ISTOTA_HOME/src/.git" ]; then
        info "Updating repository"
        git -C "$ISTOTA_HOME/src" fetch origin
        git -C "$ISTOTA_HOME/src" reset --hard "origin/$REPO_BRANCH"
    else
        info "Cloning repository"
        git clone --branch "$REPO_BRANCH" "$REPO_URL" "$ISTOTA_HOME/src"
    fi
    chown -R "$ISTOTA_USER:$ISTOTA_GROUP" "$ISTOTA_HOME/src"
    ok "Code deployed"

    # Python dependencies
    info "Installing Python dependencies"
    local uv_bin
    uv_bin=$(command -v uv || echo "/root/.local/bin/uv")
    local extras=""

    # Check settings for optional features
    if [ -f "$SETTINGS_FILE" ]; then
        local mem_search whisper
        mem_search=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print('true' if s.get('memory_search', {}).get('enabled', True) else 'false')
" 2>/dev/null || echo "true")
        whisper=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print('true' if s.get('whisper', {}).get('enabled', False) else 'false')
" 2>/dev/null || echo "false")
        [ "$mem_search" = "true" ] && extras="$extras --extra memory-search"
        [ "$whisper" = "true" ] && extras="$extras --extra whisper"
    fi

    (cd "$ISTOTA_HOME/src" && PATH="/root/.local/bin:$PATH" $uv_bin sync $extras)
    chown -R "$ISTOTA_USER:$ISTOTA_GROUP" "$ISTOTA_HOME/src/.venv"

    # Symlink venv
    ln -sfn "$ISTOTA_HOME/src/.venv" "$ISTOTA_HOME/.venv"
    chown -h "$ISTOTA_USER:$ISTOTA_GROUP" "$ISTOTA_HOME/.venv"
    ok "Dependencies installed"
}

deploy_config() {
    info "Generating configuration"

    # Ensure /etc/istota exists
    mkdir -p "/etc/$ISTOTA_NAMESPACE"

    if [ -f "$SETTINGS_FILE" ]; then
        # Use render_config.py for config generation
        local render_script="$SCRIPT_DIR/render_config.py"
        if [ ! -f "$render_script" ]; then
            render_script="$ISTOTA_HOME/src/deploy/render_config.py"
        fi

        if [ -f "$render_script" ]; then
            python3 "$render_script" --settings "$SETTINGS_FILE" --output-dir /
            ok "Config files generated from settings"
        else
            warn "render_config.py not found, skipping config generation"
        fi
    else
        # No settings file — copy example config if nothing exists
        if [ ! -f "$ISTOTA_HOME/src/config/config.toml" ]; then
            if [ -f "$ISTOTA_HOME/src/config/config.example.toml" ]; then
                cp "$ISTOTA_HOME/src/config/config.example.toml" "$ISTOTA_HOME/src/config/config.toml"
                warn "Created config from example — edit $ISTOTA_HOME/src/config/config.toml"
            fi
        fi
    fi

    # Fix permissions
    if [ -f "$ISTOTA_HOME/src/config/config.toml" ]; then
        chown "$ISTOTA_USER:$ISTOTA_GROUP" "$ISTOTA_HOME/src/config/config.toml"
        chmod 600 "$ISTOTA_HOME/src/config/config.toml"
    fi
    for f in "$ISTOTA_HOME"/src/config/users/*.toml; do
        [ -f "$f" ] || continue
        chown "$ISTOTA_USER:$ISTOTA_GROUP" "$f"
        chmod 600 "$f"
    done
    if [ -f "/etc/$ISTOTA_NAMESPACE/secrets.env" ]; then
        chown "root:$ISTOTA_GROUP" "/etc/$ISTOTA_NAMESPACE/secrets.env"
        chmod 640 "/etc/$ISTOTA_NAMESPACE/secrets.env"
    fi
    if [ -f "/etc/$ISTOTA_NAMESPACE/admins" ]; then
        chown root:root "/etc/$ISTOTA_NAMESPACE/admins"
        chmod 644 "/etc/$ISTOTA_NAMESPACE/admins"
    fi
}

deploy_db() {
    info "Initializing database"
    local db_path="$ISTOTA_HOME/data/$ISTOTA_NAMESPACE.db"
    "$ISTOTA_HOME/.venv/bin/python" -c "
from pathlib import Path
import sys
sys.path.insert(0, 'src')
from istota.db import init_db
init_db(Path('$db_path'))
" 2>/dev/null || warn "DB init failed (may need config first)"

    if [ -f "$db_path" ]; then
        chown "$ISTOTA_USER:$ISTOTA_GROUP" "$db_path"
        # Apply migrations
        sqlite3 "$db_path" "ALTER TABLE processed_emails ADD COLUMN \"references\" TEXT;" 2>/dev/null || true
        sqlite3 "$db_path" "ALTER TABLE tasks ADD COLUMN output_target TEXT;" 2>/dev/null || true
        sqlite3 "$db_path" "ALTER TABLE scheduled_jobs ADD COLUMN output_target TEXT;" 2>/dev/null || true
        ok "Database ready"
    fi
}

deploy_services() {
    info "Deploying systemd services"

    # The systemd service file should already be generated by render_config.py
    # If not, deploy a basic one
    if [ ! -f /etc/systemd/system/istota-scheduler.service ]; then
        cat > /etc/systemd/system/istota-scheduler.service <<EOF
[Unit]
Description=Istota Task Scheduler Daemon
After=network.target

[Service]
Type=simple
User=$ISTOTA_USER
Group=$ISTOTA_GROUP
WorkingDirectory=$ISTOTA_HOME/src
ExecStart=$ISTOTA_HOME/.venv/bin/python -m istota.scheduler --daemon --config $ISTOTA_HOME/src/config/config.toml
Restart=always
RestartSec=5
Environment=PATH=$ISTOTA_HOME/.venv/bin:$ISTOTA_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$ISTOTA_HOME
Environment=PYTHONUNBUFFERED=1
Environment=ISTOTA_ADMINS_FILE=/etc/$ISTOTA_NAMESPACE/admins
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$ISTOTA_NAMESPACE-scheduler
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=$ISTOTA_HOME
ReadWritePaths=/var/log/$ISTOTA_NAMESPACE

[Install]
WantedBy=multi-user.target
EOF
    fi

    # Clean up old webhook service
    systemctl stop "$ISTOTA_NAMESPACE-webhook" 2>/dev/null || true
    systemctl disable "$ISTOTA_NAMESPACE-webhook" 2>/dev/null || true
    rm -f "/etc/systemd/system/$ISTOTA_NAMESPACE-webhook.service"

    systemctl daemon-reload
    systemctl enable istota-scheduler
    ok "Services deployed"
}

start_services() {
    info "Starting services"
    systemctl restart istota-scheduler
    ok "Scheduler started"
    echo
    echo "  Status:  systemctl status istota-scheduler"
    echo "  Logs:    journalctl -u istota-scheduler -f"
}

# ============================================================
# Post-install summary
# ============================================================

show_summary() {
    echo
    info "Deployment complete"
    echo
    echo "  Install dir:  $ISTOTA_HOME"
    echo "  Config:       $ISTOTA_HOME/src/config/config.toml"
    echo "  Database:     $ISTOTA_HOME/data/$ISTOTA_NAMESPACE.db"
    echo "  Service:      istota-scheduler"
    echo "  Logs:         journalctl -u istota-scheduler -f"
    if [ -f "$SETTINGS_FILE" ]; then
        echo "  Settings:     $SETTINGS_FILE"
    fi
    echo
    if ! sudo -u "$ISTOTA_USER" HOME="$ISTOTA_HOME" claude --version &>/dev/null 2>&1; then
        warn "Claude CLI not authenticated. Run:"
        echo "    sudo -u $ISTOTA_USER HOME=$ISTOTA_HOME claude login"
    fi
    echo
}

# ============================================================
# Main
# ============================================================

main() {
    require_root

    if [ "$INTERACTIVE" = true ]; then
        run_interactive
    fi

    # Load settings if file exists
    if [ -f "$SETTINGS_FILE" ]; then
        # Override from settings file
        ISTOTA_HOME=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('home', '$ISTOTA_HOME'))
" 2>/dev/null || echo "$ISTOTA_HOME")
        ISTOTA_NAMESPACE=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('namespace', '$ISTOTA_NAMESPACE'))
" 2>/dev/null || echo "$ISTOTA_NAMESPACE")
        ISTOTA_USER="$ISTOTA_NAMESPACE"
        ISTOTA_GROUP="$ISTOTA_NAMESPACE"
        REPO_URL=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('repo_url', '$REPO_URL'))
" 2>/dev/null || echo "$REPO_URL")
        REPO_BRANCH=$(python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
print(s.get('repo_branch', '$REPO_BRANCH'))
" 2>/dev/null || echo "$REPO_BRANCH")
    fi

    echo
    info "Istota Deployment"
    echo "  Home:      $ISTOTA_HOME"
    echo "  Namespace: $ISTOTA_NAMESPACE"
    echo "  Mode:      $([ "$UPDATE_ONLY" = true ] && echo "update" || echo "full install")"
    echo

    if [ "$UPDATE_ONLY" = false ]; then
        setup_system
        setup_uv
        setup_user
        setup_directories
        setup_logrotate
        setup_claude_cli
        setup_rclone
        setup_rclone_mount
    fi

    deploy_code
    deploy_config
    deploy_db
    deploy_services
    start_services
    show_summary
}

main
