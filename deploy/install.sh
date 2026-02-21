#!/bin/bash
# Istota deployment script
# Installs or updates istota on a Debian/Ubuntu server
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/alicekubicki/istota/main/deploy/install.sh -o install.sh
#   sudo bash install.sh
#
#   install.sh [OPTIONS]
#     --interactive     Guided setup wizard (writes settings file, default on first run)
#     --update          Update only (skip system setup, just pull + config + restart)
#     --skip-system     Skip system package/tool installation (git, uv, rclone, claude, etc.)
#     --dry-run         Run wizard and generate config to temp dir without system changes
#     --settings PATH   Settings file path (default: /etc/istota/settings.toml)
#     --home PATH       Override install directory
#     --help            Show this help
#
# Settings can also be provided via environment variables (ISTOTA_ prefix).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd 2>/dev/null || echo "/tmp")"

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
DRY_RUN=false
SKIP_SYSTEM=false

# Wizard state (populated during interactive mode)
_WIZ_NC_URL=""
_WIZ_NC_USERNAME=""
_WIZ_NC_APP_PASSWORD=""
_WIZ_USE_MOUNT=true
_WIZ_MOUNT_PATH="/srv/mount/nextcloud/content"
_WIZ_RCLONE_PASS_OBSCURED=""
_WIZ_BOT_NAME=""
_WIZ_EMAIL_ENABLED=false
_WIZ_BROWSER_ENABLED=false
_WIZ_MEMORY_SEARCH_ENABLED=true
_WIZ_SLEEP_CYCLE_ENABLED=false
_WIZ_USERS_BLOCK=""
_WIZ_ADMIN_BLOCK="admin_users = []"
_WIZ_EMAIL_IMAP_HOST=""
_WIZ_EMAIL_IMAP_USER=""
_WIZ_EMAIL_IMAP_PASSWORD=""
_WIZ_EMAIL_SMTP_HOST=""
_WIZ_EMAIL_BOT_ADDRESS=""
_WIZ_CLAUDE_TOKEN=""
_WIZ_USER_IDS=()

# ============================================================
# Output helpers
# ============================================================

_BOLD="\033[1m"
_BLUE="\033[1;34m"
_GREEN="\033[1;32m"
_YELLOW="\033[1;33m"
_RED="\033[1;31m"
_DIM="\033[2m"
_RESET="\033[0m"

info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
error()   { echo -e "${_RED}ERROR:${_RESET} $*" >&2; }
die()     { error "$@"; exit 1; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }
dim()     { echo -e "${_DIM}  $*${_RESET}"; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "This script must be run as root (or with sudo)"
    fi
}

command_exists() {
    command -v "$1" &>/dev/null
}

# ============================================================
# Input helpers
# ============================================================

prompt_value() {
    local varname="$1" prompt="$2" default="${3:-}"
    local value
    if [ -n "$default" ]; then
        read -rp "  $prompt [$default]: " value
        value="${value:-$default}"
    else
        read -rp "  $prompt: " value
    fi
    eval "$varname=\"\$value\""
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
    eval "$varname=\"\$value\""
}

# Read a TOML value from the settings file using python3
read_setting() {
    local key="$1" default="${2:-}"
    python3 -c "
import tomllib
with open('$SETTINGS_FILE', 'rb') as f: s = tomllib.load(f)
parts = '$key'.split('.')
d = s
for p in parts[:-1]:
    d = d.get(p, {})
v = d.get(parts[-1], None)
if v is None:
    print('$default')
else:
    print(v)
" 2>/dev/null || echo "$default"
}

# ============================================================
# Parse arguments
# ============================================================

while [ $# -gt 0 ]; do
    case "$1" in
        --interactive)  INTERACTIVE=true; shift ;;
        --update)       UPDATE_ONLY=true; shift ;;
        --skip-system)  SKIP_SYSTEM=true; shift ;;
        --dry-run)      DRY_RUN=true; INTERACTIVE=true; shift ;;
        --settings)     SETTINGS_FILE="$2"; shift 2 ;;
        --home)         ISTOTA_HOME="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,/^$/s/^# \?//p' "$0"
            exit 0 ;;
        *) die "Unknown option: $1. Use --help for usage." ;;
    esac
done

# ============================================================
# Pre-flight checks
# ============================================================

preflight() {
    section "Pre-flight Checks"

    # OS detection
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "$ID" in
            debian)
                if [ "${VERSION_ID:-0}" -lt 12 ] 2>/dev/null; then
                    warn "Debian $VERSION_ID detected. Debian 12+ recommended."
                else
                    ok "OS: $PRETTY_NAME"
                fi
                ;;
            ubuntu)
                ok "OS: $PRETTY_NAME"
                ;;
            *)
                warn "Untested OS: $PRETTY_NAME. Debian/Ubuntu recommended."
                ;;
        esac
    else
        warn "Could not detect OS. Debian/Ubuntu recommended."
    fi

    # Internet connectivity
    if curl -sf --max-time 5 https://github.com > /dev/null 2>&1; then
        ok "Internet connectivity"
    else
        die "No internet connectivity. Cannot reach github.com."
    fi

    # Disk space
    local avail_kb
    avail_kb=$(df -k / | awk 'NR==2 {print $4}')
    local avail_gb=$((avail_kb / 1024 / 1024))
    if [ "$avail_gb" -lt 3 ]; then
        warn "Low disk space: ${avail_gb}GB available (5GB+ recommended)"
    else
        ok "Disk space: ${avail_gb}GB available"
    fi

    # Python 3.11+
    if command_exists python3; then
        local pyver
        pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        local pymajor pyminor
        pymajor=$(echo "$pyver" | cut -d. -f1)
        pyminor=$(echo "$pyver" | cut -d. -f2)
        if [ "$pymajor" -ge 3 ] && [ "$pyminor" -ge 11 ]; then
            ok "Python $pyver"
        else
            warn "Python $pyver found. Python 3.11+ recommended (will be installed)."
        fi
    else
        dim "Python not found (will be installed)"
    fi

    # Existing installation
    if [ -f "$SETTINGS_FILE" ]; then
        ok "Existing settings found at $SETTINGS_FILE"
        if [ "$INTERACTIVE" = true ]; then
            echo
            local overwrite
            prompt_bool overwrite "Overwrite existing settings with new wizard?" "n"
            if [ "$overwrite" = "false" ]; then
                INTERACTIVE=false
                info "Skipping wizard, using existing settings"
            fi
        fi
    fi
}

# ============================================================
# Interactive setup wizard
# ============================================================

run_interactive() {
    echo
    echo -e "${_BOLD}╔══════════════════════════════════════╗${_RESET}"
    echo -e "${_BOLD}║       Istota Setup Wizard            ║${_RESET}"
    echo -e "${_BOLD}╚══════════════════════════════════════╝${_RESET}"
    echo
    dim "This wizard will guide you through configuring istota."
    dim "Press Enter to accept defaults shown in [brackets]."
    echo

    wiz_basics
    wiz_nextcloud
    wiz_mount
    wiz_users
    wiz_features
    wiz_claude_auth
    wiz_review
    wiz_write_settings
}

wiz_basics() {
    section "1. Basics"

    prompt_value _WIZ_BOT_NAME "Bot name (user-facing identity)" "Istota"
    prompt_value ISTOTA_HOME "Install directory" "$ISTOTA_HOME"

    echo
    dim "Advanced: namespace sets the system user, group, and service names."
    local customize_ns
    prompt_bool customize_ns "Customize namespace?" "n"
    if [ "$customize_ns" = "true" ]; then
        prompt_value ISTOTA_NAMESPACE "Namespace" "$ISTOTA_NAMESPACE"
    fi
    ISTOTA_USER="$ISTOTA_NAMESPACE"
    ISTOTA_GROUP="$ISTOTA_NAMESPACE"
}

wiz_nextcloud() {
    section "2. Nextcloud Connection"

    dim "Istota needs a Nextcloud user account to operate."
    dim "Create a dedicated user (e.g. 'istota') and generate an app password"
    dim "in Nextcloud > Settings > Security > Devices & sessions."
    echo

    while true; do
        prompt_value _WIZ_NC_URL "Nextcloud URL" ""
        # Normalize: strip trailing slash
        _WIZ_NC_URL="${_WIZ_NC_URL%/}"

        if [[ ! "$_WIZ_NC_URL" =~ ^https?:// ]]; then
            warn "URL should start with https://. Prepending..."
            _WIZ_NC_URL="https://$_WIZ_NC_URL"
        fi

        # Test connectivity
        echo -n "  Testing connection... "
        if curl -sf --max-time 10 "$_WIZ_NC_URL/status.php" > /dev/null 2>&1; then
            echo -e "${_GREEN}OK${_RESET}"
            break
        else
            echo -e "${_RED}FAILED${_RESET}"
            warn "Could not reach $_WIZ_NC_URL/status.php"
            local retry
            prompt_bool retry "Try again?" "y"
            [ "$retry" = "false" ] && break
        fi
    done

    prompt_value _WIZ_NC_USERNAME "Bot's Nextcloud username" "$ISTOTA_NAMESPACE"

    while true; do
        prompt_secret _WIZ_NC_APP_PASSWORD "App password"
        if [ -z "$_WIZ_NC_APP_PASSWORD" ]; then
            warn "App password is required"
            continue
        fi

        # Test authentication
        echo -n "  Verifying credentials... "
        local http_code
        http_code=$(curl -sf --max-time 10 -o /dev/null -w "%{http_code}" \
            -u "$_WIZ_NC_USERNAME:$_WIZ_NC_APP_PASSWORD" \
            -H "OCS-APIRequest: true" \
            "$_WIZ_NC_URL/ocs/v1.php/cloud/users/$_WIZ_NC_USERNAME?format=json" 2>/dev/null || echo "000")

        if [ "$http_code" = "200" ]; then
            echo -e "${_GREEN}OK${_RESET}"
            break
        elif [ "$http_code" = "401" ]; then
            echo -e "${_RED}FAILED${_RESET}"
            warn "Authentication failed. Check username and app password."
            local retry
            prompt_bool retry "Try again?" "y"
            [ "$retry" = "false" ] && break
        else
            echo -e "${_YELLOW}UNKNOWN (HTTP $http_code)${_RESET}"
            warn "Could not verify credentials (may still work). Continuing."
            break
        fi
    done
}

wiz_mount() {
    section "3. File Access (rclone Mount)"

    dim "Istota accesses Nextcloud files via a FUSE mount using rclone."
    dim "This is strongly recommended for full functionality."
    echo

    prompt_bool _WIZ_USE_MOUNT "Enable Nextcloud file mount?" "y"
    if [ "$_WIZ_USE_MOUNT" = "true" ]; then
        prompt_value _WIZ_MOUNT_PATH "Mount path" "/srv/mount/nextcloud/content"
        echo
        dim "The rclone obscured password will be generated automatically"
        dim "from the app password after rclone is installed."
    fi
}

wiz_users() {
    section "4. Users"

    dim "Define the Nextcloud users who will interact with istota."
    dim "Enter a blank user ID when finished."
    echo

    _WIZ_USERS_BLOCK=""
    _WIZ_USER_IDS=()
    local first_user=true

    while true; do
        local uid uname utz uemail
        if [ "$first_user" = true ]; then
            prompt_value uid "User ID (Nextcloud username, e.g. alice)" ""
        else
            prompt_value uid "Another user ID (blank to finish)" ""
        fi
        [ -z "$uid" ] && break

        prompt_value uname "Display name" "$uid"
        prompt_value utz "Timezone" "UTC"
        prompt_value uemail "Email address (optional)" ""

        _WIZ_USERS_BLOCK+="
[users.$uid]
display_name = \"$uname\"
timezone = \"$utz\"
"
        if [ -n "$uemail" ]; then
            _WIZ_USERS_BLOCK+="email_addresses = [\"$uemail\"]
"
        fi

        _WIZ_USER_IDS+=("$uid")
        first_user=false
        echo
    done

    if [ ${#_WIZ_USER_IDS[@]} -eq 0 ]; then
        warn "No users defined. You can add users later in the settings file."
    fi

    # Admin users
    echo
    if [ ${#_WIZ_USER_IDS[@]} -le 1 ]; then
        dim "With one user, they're automatically an admin."
        _WIZ_ADMIN_BLOCK="admin_users = []"
    else
        dim "Admin users get full system access (DB, all files, admin-only skills)."
        dim "Leave blank to make all users admins."
        local admin_line
        prompt_value admin_line "Admin user IDs (comma-separated)" ""
        _WIZ_ADMIN_BLOCK="admin_users = []"
        if [ -n "$admin_line" ]; then
            _WIZ_ADMIN_BLOCK="admin_users = [$(echo "$admin_line" | sed 's/[[:space:]]*,[[:space:]]*/", "/g; s/^/"/; s/$/"/' )]"
        fi
    fi
}

wiz_features() {
    section "5. Optional Features"

    dim "Configure additional capabilities. All can be changed later."
    echo

    # Email
    prompt_bool _WIZ_EMAIL_ENABLED "Enable email integration?" "n"
    if [ "$_WIZ_EMAIL_ENABLED" = "true" ]; then
        echo
        prompt_value _WIZ_EMAIL_IMAP_HOST "IMAP host" ""
        prompt_value _WIZ_EMAIL_IMAP_USER "IMAP username" ""
        prompt_secret _WIZ_EMAIL_IMAP_PASSWORD "IMAP password"
        prompt_value _WIZ_EMAIL_SMTP_HOST "SMTP host" "$_WIZ_EMAIL_IMAP_HOST"
        prompt_value _WIZ_EMAIL_BOT_ADDRESS "Bot email address" "$_WIZ_EMAIL_IMAP_USER"
        echo
    fi

    # Memory search
    echo
    dim "Memory search enables semantic search over conversations and memories."
    dim "Requires ~2GB disk for PyTorch + sentence-transformers."
    prompt_bool _WIZ_MEMORY_SEARCH_ENABLED "Enable memory search?" "y"

    # Sleep cycle
    echo
    dim "Sleep cycle extracts daily memories from conversations overnight."
    prompt_bool _WIZ_SLEEP_CYCLE_ENABLED "Enable nightly memory extraction?" "n"

    # Browser
    echo
    dim "Browser container provides web browsing capability via Docker."
    prompt_bool _WIZ_BROWSER_ENABLED "Enable web browser container?" "n"
    if [ "$_WIZ_BROWSER_ENABLED" = "true" ]; then
        if ! command_exists docker; then
            warn "Docker not found. It will need to be installed separately."
        fi
    fi
}

wiz_claude_auth() {
    section "6. Claude Authentication"

    dim "Istota uses the Claude CLI which needs authentication."
    dim "You can either provide an OAuth token now, or authenticate"
    dim "interactively after installation."
    echo

    local has_token
    prompt_bool has_token "Do you have a Claude OAuth token?" "n"
    if [ "$has_token" = "true" ]; then
        prompt_secret _WIZ_CLAUDE_TOKEN "Claude OAuth token"
    else
        dim "You'll authenticate after installation with:"
        dim "  sudo -u $ISTOTA_NAMESPACE HOME=$ISTOTA_HOME claude login"
    fi
}

wiz_review() {
    section "7. Review Configuration"

    echo -e "  ${_BOLD}Bot name:${_RESET}          $_WIZ_BOT_NAME"
    echo -e "  ${_BOLD}Install dir:${_RESET}       $ISTOTA_HOME"
    echo -e "  ${_BOLD}Namespace:${_RESET}         $ISTOTA_NAMESPACE"
    echo
    echo -e "  ${_BOLD}Nextcloud URL:${_RESET}     $_WIZ_NC_URL"
    echo -e "  ${_BOLD}NC username:${_RESET}       $_WIZ_NC_USERNAME"
    echo -e "  ${_BOLD}NC app password:${_RESET}   ****"
    echo
    echo -e "  ${_BOLD}File mount:${_RESET}        $_WIZ_USE_MOUNT"
    if [ "$_WIZ_USE_MOUNT" = "true" ]; then
        echo -e "  ${_BOLD}Mount path:${_RESET}        $_WIZ_MOUNT_PATH"
    fi
    echo
    if [ ${#_WIZ_USER_IDS[@]} -gt 0 ]; then
        echo -e "  ${_BOLD}Users:${_RESET}             ${_WIZ_USER_IDS[*]}"
    else
        echo -e "  ${_BOLD}Users:${_RESET}             (none defined)"
    fi
    echo
    echo -e "  ${_BOLD}Email:${_RESET}             $_WIZ_EMAIL_ENABLED"
    echo -e "  ${_BOLD}Memory search:${_RESET}     $_WIZ_MEMORY_SEARCH_ENABLED"
    echo -e "  ${_BOLD}Sleep cycle:${_RESET}       $_WIZ_SLEEP_CYCLE_ENABLED"
    echo -e "  ${_BOLD}Browser:${_RESET}           $_WIZ_BROWSER_ENABLED"
    echo -e "  ${_BOLD}Claude token:${_RESET}      $([ -n "$_WIZ_CLAUDE_TOKEN" ] && echo "provided" || echo "authenticate later")"
    echo

    local confirm
    prompt_bool confirm "Proceed with installation?" "y"
    if [ "$confirm" = "false" ]; then
        die "Installation cancelled"
    fi
}

wiz_write_settings() {
    section "Writing Settings"

    local settings_dir
    settings_dir="$(dirname "$SETTINGS_FILE")"
    mkdir -p "$settings_dir"

    cat > "$SETTINGS_FILE" <<TOML
# Istota settings - generated by install.sh interactive wizard
# Edit this file and re-run install.sh --update to apply changes

home = "$ISTOTA_HOME"
namespace = "$ISTOTA_NAMESPACE"
bot_name = "$_WIZ_BOT_NAME"
repo_url = "$REPO_URL"
repo_branch = "$REPO_BRANCH"
use_environment_file = true

nextcloud_url = "$_WIZ_NC_URL"
nextcloud_username = "$_WIZ_NC_USERNAME"
nextcloud_app_password = "$_WIZ_NC_APP_PASSWORD"

use_nextcloud_mount = $_WIZ_USE_MOUNT
nextcloud_mount_path = "$_WIZ_MOUNT_PATH"
rclone_password_obscured = "$_WIZ_RCLONE_PASS_OBSCURED"

$_WIZ_ADMIN_BLOCK
claude_oauth_token = "$_WIZ_CLAUDE_TOKEN"

[security]
mode = "restricted"
sandbox_enabled = true

[email]
enabled = $_WIZ_EMAIL_ENABLED
imap_host = "$_WIZ_EMAIL_IMAP_HOST"
imap_user = "$_WIZ_EMAIL_IMAP_USER"
imap_password = "$_WIZ_EMAIL_IMAP_PASSWORD"
smtp_host = "$_WIZ_EMAIL_SMTP_HOST"
bot_email = "$_WIZ_EMAIL_BOT_ADDRESS"

[browser]
enabled = $_WIZ_BROWSER_ENABLED

[memory_search]
enabled = $_WIZ_MEMORY_SEARCH_ENABLED

[sleep_cycle]
enabled = $_WIZ_SLEEP_CYCLE_ENABLED

$_WIZ_USERS_BLOCK
TOML

    chmod 600 "$SETTINGS_FILE"
    ok "Settings written to $SETTINGS_FILE"
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
        bubblewrap \
        2>&1 | while IFS= read -r line; do
            # Suppress apt noise but show errors
            case "$line" in
                W:*|E:*) echo "  $line" ;;
            esac
        done
    ok "System packages installed"

    # Enable unprivileged user namespaces for sandbox
    if [ ! -f /etc/sysctl.d/99-istota-sandbox.conf ]; then
        echo "kernel.unprivileged_userns_clone = 1" > /etc/sysctl.d/99-istota-sandbox.conf
        sysctl -p /etc/sysctl.d/99-istota-sandbox.conf 2>/dev/null || true
    fi
}

setup_uv() {
    if command_exists uv; then
        ok "uv already installed"
        return
    fi
    info "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>&1 | tail -1
    export PATH="/root/.local/bin:$PATH"
    ok "uv installed"
}

setup_claude_cli() {
    if command_exists claude; then
        ok "Claude CLI already installed"
    else
        info "Installing Claude CLI"
        HOME="$ISTOTA_HOME" curl -fsSL https://claude.ai/install.sh | bash 2>&1 | tail -1
        if [ -f "$ISTOTA_HOME/.local/bin/claude" ] && [ ! -f /usr/local/bin/claude ]; then
            ln -sf "$ISTOTA_HOME/.local/bin/claude" /usr/local/bin/claude
        fi
        ok "Claude CLI installed"
    fi

    # Set up OAuth token if provided in settings
    if [ -f "$SETTINGS_FILE" ]; then
        local token
        token=$(read_setting "claude_oauth_token" "")
        if [ -n "$token" ]; then
            # Create credentials file for the istota user
            local claude_dir="$ISTOTA_HOME/.claude"
            mkdir -p "$claude_dir"
            echo "{\"claudeAiOauth\":{\"accessToken\":\"$token\",\"expiresAt\":\"9999-12-31T23:59:59.999Z\"}}" \
                > "$claude_dir/.credentials.json"
            chown -R "$ISTOTA_USER:$ISTOTA_GROUP" "$claude_dir"
            chmod 600 "$claude_dir/.credentials.json"
            ok "Claude OAuth token configured"
        fi
    fi
}

setup_rclone() {
    if command_exists rclone; then
        ok "rclone already installed"
    else
        info "Installing rclone"
        curl -s https://rclone.org/install.sh | bash 2>&1 | tail -1
        ok "rclone installed"
    fi

    # Auto-obscure password if needed
    if [ -f "$SETTINGS_FILE" ]; then
        local nc_url nc_user rclone_pass rclone_remote
        nc_url=$(read_setting "nextcloud_url" "")
        nc_user=$(read_setting "nextcloud_username" "$ISTOTA_NAMESPACE")
        rclone_pass=$(read_setting "rclone_password_obscured" "")
        rclone_remote=$(read_setting "rclone_remote" "nextcloud")

        # If no obscured password but we have the app password, auto-obscure it
        if [ -z "$rclone_pass" ] && command_exists rclone; then
            local app_pass
            app_pass=$(read_setting "nextcloud_app_password" "")
            if [ -n "$app_pass" ]; then
                info "Generating obscured rclone password"
                rclone_pass=$(rclone obscure "$app_pass")
                # Update settings file with the obscured password
                python3 -c "
import re
with open('$SETTINGS_FILE', 'r') as f:
    content = f.read()
content = re.sub(
    r'rclone_password_obscured\s*=\s*\"[^\"]*\"',
    'rclone_password_obscured = \"$rclone_pass\"',
    content
)
with open('$SETTINGS_FILE', 'w') as f:
    f.write(content)
" 2>/dev/null
                ok "rclone password auto-obscured"
            fi
        fi

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
            chmod 700 "$rclone_dir"
            chmod 600 "$rclone_dir/rclone.conf"
            ok "rclone configured"
        fi
    fi
}

setup_rclone_mount() {
    if [ ! -f "$SETTINGS_FILE" ]; then
        return 0
    fi

    local use_mount
    use_mount=$(read_setting "use_nextcloud_mount" "true")
    [ "$use_mount" != "True" ] && [ "$use_mount" != "true" ] && return 0

    local mount_path
    mount_path=$(read_setting "nextcloud_mount_path" "/srv/mount/nextcloud/content")
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
    rclone_remote=$(read_setting "rclone_remote" "nextcloud")

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
        systemctl start mount-nextcloud || warn "Mount service failed to start (may need valid credentials)"
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
    info "Installing Python dependencies (this may take a few minutes)"
    local uv_bin
    uv_bin=$(command -v uv || echo "/root/.local/bin/uv")
    local extras=""

    # Check settings for optional features
    if [ -f "$SETTINGS_FILE" ]; then
        local mem_search whisper
        mem_search=$(read_setting "memory_search.enabled" "true")
        whisper=$(read_setting "whisper.enabled" "false")
        [ "$mem_search" = "True" ] || [ "$mem_search" = "true" ] && extras="$extras --extra memory-search"
        [ "$whisper" = "True" ] || [ "$whisper" = "true" ] && extras="$extras --extra whisper"
    fi

    # shellcheck disable=SC2086
    (cd "$ISTOTA_HOME/src" && PATH="/root/.local/bin:$PATH" $uv_bin sync $extras 2>&1 | tail -5)
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
    (cd "$ISTOTA_HOME/src" && "$ISTOTA_HOME/.venv/bin/python" -c "
from pathlib import Path
from istota.db import init_db
init_db(Path('$db_path'))
") 2>/dev/null || warn "DB init failed (may need config first)"

    if [ -f "$db_path" ]; then
        chown "$ISTOTA_USER:$ISTOTA_GROUP" "$db_path"
        # Apply migrations (idempotent — fails silently if column exists)
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
}

# ============================================================
# Post-install verification
# ============================================================

verify_installation() {
    section "Verification"

    local all_ok=true

    # Check scheduler service
    sleep 2
    if systemctl is-active --quiet istota-scheduler; then
        ok "Scheduler service is running"
    else
        warn "Scheduler service is not running"
        dim "Check logs: journalctl -u istota-scheduler -n 20"
        all_ok=false
    fi

    # Check Claude CLI
    if command_exists claude; then
        local claude_ver
        claude_ver=$(claude --version 2>/dev/null || echo "unknown")
        ok "Claude CLI: $claude_ver"

        # Check authentication
        if sudo -u "$ISTOTA_USER" HOME="$ISTOTA_HOME" claude --version &>/dev/null 2>&1; then
            ok "Claude CLI accessible by $ISTOTA_USER"
        else
            warn "Claude CLI not accessible by $ISTOTA_USER"
            all_ok=false
        fi
    else
        warn "Claude CLI not found"
        all_ok=false
    fi

    # Check mount if enabled
    if [ -f "$SETTINGS_FILE" ]; then
        local use_mount mount_path
        use_mount=$(read_setting "use_nextcloud_mount" "false")
        if [ "$use_mount" = "True" ] || [ "$use_mount" = "true" ]; then
            mount_path=$(read_setting "nextcloud_mount_path" "/srv/mount/nextcloud/content")
            if mountpoint -q "$mount_path" 2>/dev/null; then
                ok "Nextcloud mount active at $mount_path"
            else
                warn "Nextcloud mount not active at $mount_path"
                dim "Check: systemctl status mount-nextcloud"
                all_ok=false
            fi
        fi
    fi

    # Check database
    local db_path="$ISTOTA_HOME/data/$ISTOTA_NAMESPACE.db"
    if [ -f "$db_path" ]; then
        ok "Database exists at $db_path"
    else
        warn "Database not found at $db_path"
        all_ok=false
    fi

    # Check config
    if [ -f "$ISTOTA_HOME/src/config/config.toml" ]; then
        ok "Config file exists"
    else
        warn "Config file not found"
        all_ok=false
    fi

    echo
    if [ "$all_ok" = true ]; then
        ok "All checks passed"
    else
        warn "Some checks failed — review warnings above"
    fi
}

# ============================================================
# Post-install summary
# ============================================================

show_summary() {
    section "Installation Complete"

    echo -e "  ${_BOLD}Install dir:${_RESET}   $ISTOTA_HOME"
    echo -e "  ${_BOLD}Config:${_RESET}        $ISTOTA_HOME/src/config/config.toml"
    echo -e "  ${_BOLD}Database:${_RESET}      $ISTOTA_HOME/data/$ISTOTA_NAMESPACE.db"
    echo -e "  ${_BOLD}Service:${_RESET}       istota-scheduler"
    if [ -f "$SETTINGS_FILE" ]; then
        echo -e "  ${_BOLD}Settings:${_RESET}      $SETTINGS_FILE"
    fi
    echo

    echo -e "${_BOLD}Useful commands:${_RESET}"
    echo "  journalctl -u istota-scheduler -f        # follow logs"
    echo "  systemctl status istota-scheduler         # service status"
    echo "  systemctl restart istota-scheduler        # restart"
    echo "  install.sh --update                       # update code + config"
    echo

    # Check if Claude is authenticated
    local needs_auth=false
    if ! sudo -u "$ISTOTA_USER" HOME="$ISTOTA_HOME" claude --version &>/dev/null 2>&1; then
        needs_auth=true
    fi

    if [ "$needs_auth" = true ]; then
        echo -e "${_YELLOW}Next steps:${_RESET}"
        echo "  1. Authenticate Claude CLI:"
        echo "     sudo -u $ISTOTA_USER HOME=$ISTOTA_HOME claude login"
        echo
    else
        echo -e "${_YELLOW}Next steps:${_RESET}"
    fi

    echo "  $([ "$needs_auth" = true ] && echo "2" || echo "1"). Invite $ISTOTA_NAMESPACE to Nextcloud Talk conversations"
    echo "  $([ "$needs_auth" = true ] && echo "3" || echo "2"). Test with:"
    if [ ${#_WIZ_USER_IDS[@]:-0} -gt 0 ] 2>/dev/null; then
        echo "     sudo -u $ISTOTA_USER HOME=$ISTOTA_HOME istota task \"Hello\" -u ${_WIZ_USER_IDS[0]} -x"
    else
        echo "     sudo -u $ISTOTA_USER HOME=$ISTOTA_HOME istota task \"Hello\" -u USER_ID -x"
    fi
    echo
}

# ============================================================
# Main
# ============================================================

main() {
    # Dry-run mode: skip root, use temp dirs, wizard + config generation only
    if [ "$DRY_RUN" = true ]; then
        main_dry_run
        return
    fi

    require_root

    # Auto-detect interactive mode for first-time installs
    if [ "$INTERACTIVE" = false ] && [ "$UPDATE_ONLY" = false ] && [ ! -f "$SETTINGS_FILE" ]; then
        if [ -t 0 ]; then
            # Terminal is interactive — run wizard
            INTERACTIVE=true
        else
            die "No settings file found and stdin is not a terminal.
  For interactive setup: bash install.sh --interactive
  Or download first:     curl -fsSL <url> -o install.sh && bash install.sh"
        fi
    fi

    preflight

    if [ "$INTERACTIVE" = true ]; then
        run_interactive
    fi

    # Load settings if file exists
    if [ -f "$SETTINGS_FILE" ]; then
        ISTOTA_HOME=$(read_setting "home" "$ISTOTA_HOME")
        ISTOTA_NAMESPACE=$(read_setting "namespace" "$ISTOTA_NAMESPACE")
        ISTOTA_USER="$ISTOTA_NAMESPACE"
        ISTOTA_GROUP="$ISTOTA_NAMESPACE"
        REPO_URL=$(read_setting "repo_url" "$REPO_URL")
        REPO_BRANCH=$(read_setting "repo_branch" "$REPO_BRANCH")
    fi

    section "Deploying Istota"

    echo -e "  ${_BOLD}Home:${_RESET}      $ISTOTA_HOME"
    echo -e "  ${_BOLD}Namespace:${_RESET} $ISTOTA_NAMESPACE"
    local mode="full install"
    [ "$UPDATE_ONLY" = true ] && mode="update"
    [ "$SKIP_SYSTEM" = true ] && mode="skip system"
    echo -e "  ${_BOLD}Mode:${_RESET}      $mode"
    echo

    if [ "$UPDATE_ONLY" = false ]; then
        if [ "$SKIP_SYSTEM" = false ]; then
            setup_system
            setup_uv
            setup_claude_cli
            setup_rclone
        fi
        setup_user
        setup_directories
        setup_logrotate
        setup_rclone_mount
    fi

    deploy_code
    deploy_config
    deploy_db
    deploy_services
    start_services
    verify_installation
    show_summary
}

main_dry_run() {
    local tmpdir
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/istota-dry-run.XXXXXX")
    SETTINGS_FILE="$tmpdir/settings.toml"

    info "Dry-run mode — no system changes will be made"
    dim "Output directory: $tmpdir"
    echo

    # Run preflight (informational only, skip OS-specific checks)
    section "Pre-flight Checks"
    ok "Dry-run mode (skipping OS and connectivity checks)"
    if command_exists python3; then
        local pyver
        pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        ok "Python $pyver"
    fi

    # Run wizard
    run_interactive

    # Generate config files using render_config.py
    if [ -f "$SETTINGS_FILE" ]; then
        section "Generating Config Files"

        local render_script="$SCRIPT_DIR/render_config.py"
        if [ ! -f "$render_script" ]; then
            render_script="$(cd "$(dirname "$0")" && pwd)/render_config.py"
        fi

        if [ -f "$render_script" ]; then
            python3 "$render_script" --settings "$SETTINGS_FILE" --output-dir "$tmpdir"
            echo
            ok "Config files generated"
        else
            warn "render_config.py not found at $render_script"
        fi
    fi

    # Show what was generated
    section "Generated Files"

    echo -e "  ${_BOLD}Settings file:${_RESET}"
    echo -e "  ${_DIM}$SETTINGS_FILE${_RESET}"
    echo
    if command_exists find; then
        # List all generated files with paths relative to tmpdir
        while IFS= read -r f; do
            local relpath="${f#$tmpdir/}"
            local size
            size=$(wc -c < "$f" | tr -d ' ')
            echo -e "  ${_DIM}${relpath}${_RESET} (${size}B)"
        done < <(find "$tmpdir" -type f | sort)
    fi

    echo
    info "Inspect generated files:"
    echo "  cat $SETTINGS_FILE"
    echo "  ls -la $tmpdir/"
    echo
    dim "To view a specific file:"
    dim "  cat $tmpdir/srv/app/istota/src/config/config.toml"
    dim "  cat $tmpdir/etc/istota/secrets.env"
    echo
    dim "Clean up when done:"
    dim "  rm -rf $tmpdir"
    echo
}

main "$@"
