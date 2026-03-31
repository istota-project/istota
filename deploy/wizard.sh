#!/bin/bash
# Istota interactive setup wizard
# Writes a settings TOML file for use with install.sh and Ansible.
#
# Usage:
#   bash wizard.sh [--settings /path/to/settings.toml]
#
# This script is called by install.sh --interactive, but can also be run
# standalone to (re)generate a settings file.

set -euo pipefail

# Defaults
SETTINGS_FILE="${ISTOTA_SETTINGS_FILE:-/etc/istota/settings.toml}"
ISTOTA_HOME="${ISTOTA_HOME:-/srv/app/istota}"
ISTOTA_NAMESPACE="${ISTOTA_NAMESPACE:-istota}"
REPO_URL="${ISTOTA_REPO_URL:-https://github.com/muinyc/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"

# Wizard state
_WIZ_NC_URL=""
_WIZ_NC_USERNAME=""
_WIZ_NC_APP_PASSWORD=""
_WIZ_USE_MOUNT=true
_WIZ_MOUNT_PATH="/srv/mount/nextcloud/content"
_WIZ_RCLONE_PASS_OBSCURED=""
_WIZ_BOT_NAME=""
_WIZ_EMAIL_ENABLED=false
_WIZ_BROWSER_ENABLED=false
_WIZ_BROWSER_VNC_PASSWORD=""
_WIZ_MEMORY_SEARCH_ENABLED=true
_WIZ_SLEEP_CYCLE_ENABLED=true
_WIZ_CHANNEL_SLEEP_ENABLED=true
_WIZ_WHISPER_ENABLED=true
_WIZ_WHISPER_MODEL="small"
_WIZ_NTFY_ENABLED=false
_WIZ_NTFY_SERVER=""
_WIZ_NTFY_TOPIC=""
_WIZ_NTFY_TOKEN=""
_WIZ_LOCATION_ENABLED=false
_WIZ_WEBHOOKS_PORT=8765
_WIZ_BACKUP_ENABLED=true
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

# ============================================================
# Parse arguments
# ============================================================

while [ $# -gt 0 ]; do
    case "$1" in
        --settings)     SETTINGS_FILE="$2"; shift 2 ;;
        --home)         ISTOTA_HOME="$2"; shift 2 ;;
        --namespace)    ISTOTA_NAMESPACE="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: wizard.sh [--settings PATH] [--home PATH] [--namespace NAME]"
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ============================================================
# Wizard sections
# ============================================================

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
        dim "from the app password during installation."
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
    prompt_bool _WIZ_SLEEP_CYCLE_ENABLED "Enable nightly memory extraction?" "y"

    # Channel sleep cycle
    if [ "$_WIZ_SLEEP_CYCLE_ENABLED" = "true" ]; then
        echo
        dim "Channel sleep cycle extracts shared context from group conversations."
        prompt_bool _WIZ_CHANNEL_SLEEP_ENABLED "Enable channel memory extraction?" "y"
    else
        _WIZ_CHANNEL_SLEEP_ENABLED=false
    fi

    # Whisper
    echo
    dim "Whisper provides audio-to-text transcription via faster-whisper."
    dim "Requires ~1-2GB disk depending on model size."
    prompt_bool _WIZ_WHISPER_ENABLED "Enable audio transcription?" "y"
    if [ "$_WIZ_WHISPER_ENABLED" = "true" ]; then
        echo
        dim "Model sizes: tiny (~75MB), base (~150MB), small (~500MB), medium (~1.5GB)"
        prompt_value _WIZ_WHISPER_MODEL "Whisper model" "small"
    fi

    # ntfy
    echo
    dim "ntfy enables push notifications to phones/desktops via ntfy.sh."
    prompt_bool _WIZ_NTFY_ENABLED "Enable ntfy push notifications?" "n"
    if [ "$_WIZ_NTFY_ENABLED" = "true" ]; then
        echo
        prompt_value _WIZ_NTFY_SERVER "ntfy server URL" "https://ntfy.sh"
        prompt_value _WIZ_NTFY_TOPIC "ntfy topic" ""
        prompt_secret _WIZ_NTFY_TOKEN "ntfy access token (optional, press Enter to skip)"
    fi

    # Location tracking
    echo
    dim "GPS location tracking via Overland app (webhook receiver)."
    prompt_bool _WIZ_LOCATION_ENABLED "Enable GPS location tracking?" "n"
    if [ "$_WIZ_LOCATION_ENABLED" = "true" ]; then
        echo
        prompt_value _WIZ_WEBHOOKS_PORT "Webhook receiver port" "8765"
    fi

    # Backups
    echo
    dim "Automated backups of the database and Nextcloud files with rotation."
    prompt_bool _WIZ_BACKUP_ENABLED "Enable automated backups?" "y"

    # Browser
    echo
    dim "Browser container provides web browsing capability via Docker."
    prompt_bool _WIZ_BROWSER_ENABLED "Enable web browser container?" "n"
    if [ "$_WIZ_BROWSER_ENABLED" = "true" ]; then
        if ! command_exists docker; then
            warn "Docker not found. It will be installed during deployment."
        fi
        echo
        prompt_secret _WIZ_BROWSER_VNC_PASSWORD "VNC password for browser viewer"
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
    echo -e "  ${_BOLD}Channel sleep:${_RESET}     $_WIZ_CHANNEL_SLEEP_ENABLED"
    echo -e "  ${_BOLD}Whisper:${_RESET}           $_WIZ_WHISPER_ENABLED$([ "$_WIZ_WHISPER_ENABLED" = "true" ] && echo " (model: $_WIZ_WHISPER_MODEL)")"
    echo -e "  ${_BOLD}ntfy:${_RESET}              $_WIZ_NTFY_ENABLED$([ "$_WIZ_NTFY_ENABLED" = "true" ] && echo " (topic: $_WIZ_NTFY_TOPIC)")"
    echo -e "  ${_BOLD}Location:${_RESET}          $_WIZ_LOCATION_ENABLED$([ "$_WIZ_LOCATION_ENABLED" = "true" ] && echo " (port: $_WIZ_WEBHOOKS_PORT)")"
    echo -e "  ${_BOLD}Backups:${_RESET}           $_WIZ_BACKUP_ENABLED"
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
# Istota settings - generated by setup wizard
# Edit this file and re-run install.sh to apply changes.
# See deploy/ansible/defaults/main.yml for all available settings
# (use names without the istota_ prefix).

home = "$ISTOTA_HOME"
namespace = "$ISTOTA_NAMESPACE"
bot_name = "$_WIZ_BOT_NAME"
repo_url = "$REPO_URL"
repo_branch = "$REPO_BRANCH"
repo_tag = "latest"
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
vnc_password = "$_WIZ_BROWSER_VNC_PASSWORD"

[memory_search]
enabled = $_WIZ_MEMORY_SEARCH_ENABLED

[sleep_cycle]
enabled = $_WIZ_SLEEP_CYCLE_ENABLED

[channel_sleep_cycle]
enabled = $_WIZ_CHANNEL_SLEEP_ENABLED

[whisper]
enabled = $_WIZ_WHISPER_ENABLED
model = "$_WIZ_WHISPER_MODEL"

[ntfy]
enabled = $_WIZ_NTFY_ENABLED
server_url = "$_WIZ_NTFY_SERVER"
topic = "$_WIZ_NTFY_TOPIC"
token = "$_WIZ_NTFY_TOKEN"

[location]
enabled = $_WIZ_LOCATION_ENABLED
webhooks_port = $_WIZ_WEBHOOKS_PORT

[backup]
enabled = $_WIZ_BACKUP_ENABLED

$_WIZ_USERS_BLOCK
TOML

    chmod 600 "$SETTINGS_FILE"
    ok "Settings written to $SETTINGS_FILE"
}

# ============================================================
# Main
# ============================================================

main() {
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

main "$@"
