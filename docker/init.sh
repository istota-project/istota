#!/bin/bash
# Istota Docker — first-run setup wizard
#
# Writes a .env next to docker-compose.yml so the user can run
# `docker compose up -d` straight after. Auto-generates passwords for
# Nextcloud / Postgres / bot / human-user accounts and walks through
# the same optional-feature prompts the bare-metal wizard asks (email,
# ntfy, GPS location, developer credentials), so the resulting Docker
# stack lights up the same surface area as a "real" install.
#
# Usage:
#   bash docker/init.sh             # full wizard
#   bash docker/init.sh --minimal   # skip optional sections (passwords + Claude + user only)
#   bash docker/init.sh --force     # overwrite an existing .env without asking

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"
FORCE=false
MINIMAL=false

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f)   FORCE=true; shift ;;
        --minimal|-m) MINIMAL=true; shift ;;
        --help|-h)
            sed -n '2,/^$/s/^# \?//p' "$0"
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- output helpers ---
_BOLD="\033[1m"; _BLUE="\033[1;34m"; _GREEN="\033[1;32m"
_YELLOW="\033[1;33m"; _RED="\033[1;31m"; _DIM="\033[2m"; _RESET="\033[0m"
info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
die()     { echo -e "${_RED}ERROR:${_RESET} $*" >&2; exit 1; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }
dim()     { echo -e "${_DIM}  $*${_RESET}"; }

# --- input helpers (match deploy/wizard.sh) ---
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

# --- preflight ---
[ -f "$EXAMPLE_FILE" ] || die ".env.example not found at $EXAMPLE_FILE"
command -v openssl >/dev/null 2>&1 || die "openssl is required (used to generate passwords)"

DOCKER_MISSING=false
COMPOSE_MISSING=false
command -v docker >/dev/null 2>&1 || DOCKER_MISSING=true
if ! docker compose version >/dev/null 2>&1; then
    COMPOSE_MISSING=true
fi
if [ "$DOCKER_MISSING" = true ] || [ "$COMPOSE_MISSING" = true ]; then
    warn "Docker prerequisites are not in PATH on this machine."
    if [ "$DOCKER_MISSING" = true ]; then
        echo "    docker:          missing  →  https://docs.docker.com/engine/install/"
    else
        echo "    docker:          ok"
    fi
    if [ "$COMPOSE_MISSING" = true ]; then
        echo "    docker compose:  missing  →  https://docs.docker.com/compose/install/"
    else
        echo "    docker compose:  ok"
    fi
    echo "  Install Docker on the host where you intend to run the stack before"
    echo "  running 'docker compose up -d'. This script will still produce .env."
    echo
fi

if [ -f "$ENV_FILE" ] && [ "$FORCE" = false ]; then
    warn "$ENV_FILE already exists."
    read -rp "  Overwrite? [y/N]: " ans
    case "$ans" in
        [yY]*) : ;;
        *) echo "  Aborted. Use --force to skip this prompt."; exit 0 ;;
    esac
fi

# --- password generator ---
# url-safe, ~24 chars, no shell-special characters
gen_pw() { openssl rand -base64 18 | tr -d '/+=\n' | head -c 24; }

# Tracks which keys the wizard actively set. Anything not in this list
# flows through unchanged from .env.example, so --minimal preserves the
# example's defaults rather than zeroing out optional features.
ACTIVE_KEYS=()
mark() { ACTIVE_KEYS+=("$1"); }

# Inert defaults — only written to .env if `mark` adds the key.
DOMAIN=""
ISTOTA_BOT_NAME="Istota"
USER_EMAIL=""
ISTOTA_EMAIL_ENABLED="false"
ISTOTA_EMAIL_IMAP_HOST=""
ISTOTA_EMAIL_IMAP_USER=""
ISTOTA_EMAIL_IMAP_PASSWORD=""
ISTOTA_EMAIL_SMTP_HOST=""
ISTOTA_EMAIL_BOT_ADDRESS=""
ISTOTA_NTFY_ENABLED="false"
ISTOTA_NTFY_SERVER_URL="https://ntfy.sh"
ISTOTA_NTFY_TOPIC=""
ISTOTA_NTFY_TOKEN=""
ISTOTA_DEVELOPER_GITLAB_TOKEN=""
ISTOTA_DEVELOPER_GITLAB_USERNAME=""
ISTOTA_DEVELOPER_GITHUB_TOKEN=""
ISTOTA_DEVELOPER_GITHUB_USERNAME=""
LOCATION_ENABLED=false  # internal flag — adds "location" to COMPOSE_PROFILES

# --- bot identity & public hostname ---
section "Bot identity"
prompt_value ISTOTA_BOT_NAME "User-facing bot name" "Istota"
mark ISTOTA_BOT_NAME
echo
dim "Public hostname this stack will be reached at. Leave empty for"
dim "localhost-only evaluation; set it once and OAuth2 callback URL,"
dim "Nextcloud trusted domains and the SvelteKit site host all derive"
dim "from it. Examples: 'istota.example.com', 'home.example.com:8080'."
prompt_value DOMAIN "DOMAIN" ""
[ -n "$DOMAIN" ] && mark DOMAIN

# --- Claude Code OAuth token ---
section "Claude Code OAuth token"
cat <<'EOF'
  Istota needs a long-lived Claude Code OAuth token to talk to the model.

  On a machine that already has Claude Code installed and authenticated,
  run:

      claude setup-token

  That prints a token starting with "sk-ant-...". Copy it and paste it
  below. The token does not expire automatically; revoke it from the
  Anthropic console if you ever need to.

  If you don't have Claude Code yet:
      npm install -g @anthropic-ai/claude-code
      claude          # log in interactively, then run setup-token

  You can also leave this blank and set ANTHROPIC_API_KEY later in .env.

EOF
read -rp "  CLAUDE_CODE_OAUTH_TOKEN (paste, or empty to skip): " CLAUDE_CODE_OAUTH_TOKEN
mark CLAUDE_CODE_OAUTH_TOKEN
echo

# --- primary user ---
section "Primary user"
default_user="$(id -un 2>/dev/null || echo user)"
prompt_value USER_NAME       "USER_NAME (Nextcloud login id)" "$default_user"
mark USER_NAME
prompt_value USER_DISPLAY_NAME "USER_DISPLAY_NAME (e.g. Alice Example)" "$USER_NAME"
mark USER_DISPLAY_NAME

# Best-effort timezone detection
default_tz="UTC"
if [ -L /etc/localtime ]; then
    tz_link="$(readlink /etc/localtime 2>/dev/null || true)"
    case "$tz_link" in
        */zoneinfo/*) default_tz="${tz_link#*/zoneinfo/}" ;;
    esac
elif [ -r /etc/timezone ]; then
    default_tz="$(tr -d '\n' < /etc/timezone)"
fi
prompt_value USER_TIMEZONE "USER_TIMEZONE (IANA, e.g. Europe/Berlin)" "$default_tz"
mark USER_TIMEZONE

if [ "$MINIMAL" = false ]; then
    prompt_value USER_EMAIL "USER_EMAIL (optional, enables email-related features when matched against IMAP)" ""
    [ -n "$USER_EMAIL" ] && mark USER_EMAIL
fi

# --- optional features ---
if [ "$MINIMAL" = false ]; then

    # Email
    section "Email integration"
    dim "IMAP polling for incoming requests, SMTP for replies and outbound."
    dim "If your provider needs an app password (Gmail, iCloud, Fastmail), generate one first."
    prompt_bool email_enabled "Enable email integration?" "n"
    if [ "$email_enabled" = "true" ]; then
        ISTOTA_EMAIL_ENABLED="true"
        echo
        prompt_value  ISTOTA_EMAIL_IMAP_HOST     "IMAP host" ""
        prompt_value  ISTOTA_EMAIL_IMAP_USER     "IMAP username" ""
        prompt_secret ISTOTA_EMAIL_IMAP_PASSWORD "IMAP password"
        prompt_value  ISTOTA_EMAIL_SMTP_HOST     "SMTP host" "$ISTOTA_EMAIL_IMAP_HOST"
        prompt_value  ISTOTA_EMAIL_BOT_ADDRESS   "Bot email address" "$ISTOTA_EMAIL_IMAP_USER"
        mark ISTOTA_EMAIL_ENABLED
        mark ISTOTA_EMAIL_IMAP_HOST
        mark ISTOTA_EMAIL_IMAP_USER
        mark ISTOTA_EMAIL_IMAP_PASSWORD
        mark ISTOTA_EMAIL_SMTP_HOST
        mark ISTOTA_EMAIL_BOT_ADDRESS
    else
        ISTOTA_EMAIL_ENABLED="false"
        mark ISTOTA_EMAIL_ENABLED
    fi

    # ntfy
    section "ntfy push notifications"
    dim "Sends alerts and confirmations to your phone via ntfy.sh (or self-hosted)."
    prompt_bool ntfy_enabled "Enable ntfy push notifications?" "n"
    if [ "$ntfy_enabled" = "true" ]; then
        ISTOTA_NTFY_ENABLED="true"
        echo
        prompt_value  ISTOTA_NTFY_SERVER_URL "ntfy server URL" "https://ntfy.sh"
        prompt_value  ISTOTA_NTFY_TOPIC      "ntfy topic" ""
        prompt_secret ISTOTA_NTFY_TOKEN      "ntfy access token (optional, press Enter to skip)"
        mark ISTOTA_NTFY_ENABLED
        mark ISTOTA_NTFY_SERVER_URL
        mark ISTOTA_NTFY_TOPIC
        [ -n "$ISTOTA_NTFY_TOKEN" ] && mark ISTOTA_NTFY_TOKEN
    else
        ISTOTA_NTFY_ENABLED="false"
        mark ISTOTA_NTFY_ENABLED
    fi

    # GPS location
    section "GPS location tracking"
    dim "Webhook receiver for the Overland app (iOS/Android). Adds the 'location'"
    dim "compose profile so the receiver container starts. The bearer token Overland"
    dim "sends with each ping is auto-generated on first boot and surfaced in the logs."
    prompt_bool location_enabled "Enable GPS location tracking?" "n"
    if [ "$location_enabled" = "true" ]; then
        LOCATION_ENABLED=true
    fi

    # Developer skill
    section "Developer (git, GitLab, GitHub)"
    dim "Lets the bot push commits, open MRs/PRs, and use 'gh'/GitLab APIs."
    dim "Tokens are optional and stored only in your local .env."
    prompt_bool dev_enabled "Configure developer credentials now?" "n"
    if [ "$dev_enabled" = "true" ]; then
        echo
        prompt_value  ISTOTA_DEVELOPER_GITLAB_USERNAME "GitLab username (empty to skip)" ""
        if [ -n "$ISTOTA_DEVELOPER_GITLAB_USERNAME" ]; then
            prompt_secret ISTOTA_DEVELOPER_GITLAB_TOKEN "GitLab personal access token"
            mark ISTOTA_DEVELOPER_GITLAB_USERNAME
            mark ISTOTA_DEVELOPER_GITLAB_TOKEN
        fi
        prompt_value  ISTOTA_DEVELOPER_GITHUB_USERNAME "GitHub username (empty to skip)" ""
        if [ -n "$ISTOTA_DEVELOPER_GITHUB_USERNAME" ]; then
            prompt_secret ISTOTA_DEVELOPER_GITHUB_TOKEN "GitHub personal access token (gh-style)"
            mark ISTOTA_DEVELOPER_GITHUB_USERNAME
            mark ISTOTA_DEVELOPER_GITHUB_TOKEN
        fi
    fi

fi  # end optional features

# --- browser container default ---
# The browser profile bundles a Chromium + bot-detection countermeasures
# container that the `browse` skill talks to. Chrome has no ARM packages,
# so we only enable it by default on x86_64 hosts.
section "Container profiles"
HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
COMPOSE_PROFILES=""
case "$HOST_ARCH" in
    x86_64|amd64)
        COMPOSE_PROFILES="browser"
        ok "Browser container enabled (host arch: $HOST_ARCH)"
        ;;
    *)
        warn "Browser container disabled (host arch: $HOST_ARCH; Chrome has no ARM packages)."
        ;;
esac
if [ "$LOCATION_ENABLED" = true ]; then
    if [ -n "$COMPOSE_PROFILES" ]; then
        COMPOSE_PROFILES="$COMPOSE_PROFILES,location"
    else
        COMPOSE_PROFILES="location"
    fi
    ok "Location webhook receiver enabled"
fi
if [ -n "$COMPOSE_PROFILES" ]; then
    dim "COMPOSE_PROFILES=$COMPOSE_PROFILES (edit .env to change)"
fi

# --- generate passwords ---
section "Generating passwords"
ADMIN_PASSWORD="$(gen_pw)";    mark ADMIN_PASSWORD;    ok "ADMIN_PASSWORD"
USER_PASSWORD="$(gen_pw)";     mark USER_PASSWORD;     ok "USER_PASSWORD"
BOT_PASSWORD="$(gen_pw)";      mark BOT_PASSWORD;      ok "BOT_PASSWORD"
POSTGRES_PASSWORD="$(gen_pw)"; mark POSTGRES_PASSWORD; ok "POSTGRES_PASSWORD"
mark COMPOSE_PROFILES
case ",$COMPOSE_PROFILES," in
    *,browser,*) VNC_PASSWORD="$(gen_pw)"; mark VNC_PASSWORD; ok "VNC_PASSWORD (browser noVNC)" ;;
    *)           VNC_PASSWORD="" ;;
esac

# --- write .env ---
# Start from .env.example and patch the values we manage; this preserves
# every comment and optional knob the example file documents.
TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/istota-env.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

# Pass values via the environment so the heredoc can stay single-quoted —
# avoids any shell expansion of the rendered passwords/tokens.
ADMIN_PASSWORD="$ADMIN_PASSWORD" \
POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
BOT_PASSWORD="$BOT_PASSWORD" \
USER_NAME="$USER_NAME" \
USER_PASSWORD="$USER_PASSWORD" \
USER_DISPLAY_NAME="$USER_DISPLAY_NAME" \
USER_TIMEZONE="$USER_TIMEZONE" \
USER_EMAIL="$USER_EMAIL" \
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
VNC_PASSWORD="$VNC_PASSWORD" \
COMPOSE_PROFILES="$COMPOSE_PROFILES" \
DOMAIN="$DOMAIN" \
ISTOTA_BOT_NAME="$ISTOTA_BOT_NAME" \
ISTOTA_EMAIL_ENABLED="$ISTOTA_EMAIL_ENABLED" \
ISTOTA_EMAIL_IMAP_HOST="$ISTOTA_EMAIL_IMAP_HOST" \
ISTOTA_EMAIL_IMAP_USER="$ISTOTA_EMAIL_IMAP_USER" \
ISTOTA_EMAIL_IMAP_PASSWORD="$ISTOTA_EMAIL_IMAP_PASSWORD" \
ISTOTA_EMAIL_SMTP_HOST="$ISTOTA_EMAIL_SMTP_HOST" \
ISTOTA_EMAIL_BOT_ADDRESS="$ISTOTA_EMAIL_BOT_ADDRESS" \
ISTOTA_NTFY_ENABLED="$ISTOTA_NTFY_ENABLED" \
ISTOTA_NTFY_SERVER_URL="$ISTOTA_NTFY_SERVER_URL" \
ISTOTA_NTFY_TOPIC="$ISTOTA_NTFY_TOPIC" \
ISTOTA_NTFY_TOKEN="$ISTOTA_NTFY_TOKEN" \
ISTOTA_DEVELOPER_GITLAB_TOKEN="$ISTOTA_DEVELOPER_GITLAB_TOKEN" \
ISTOTA_DEVELOPER_GITLAB_USERNAME="$ISTOTA_DEVELOPER_GITLAB_USERNAME" \
ISTOTA_DEVELOPER_GITHUB_TOKEN="$ISTOTA_DEVELOPER_GITHUB_TOKEN" \
ISTOTA_DEVELOPER_GITHUB_USERNAME="$ISTOTA_DEVELOPER_GITHUB_USERNAME" \
ACTIVE_KEYS="$(IFS=,; echo "${ACTIVE_KEYS[*]}")" \
python3 - "$EXAMPLE_FILE" "$TMP_ENV" <<'PYEOF'
import os, sys, re
src, dst = sys.argv[1], sys.argv[2]
active = [k for k in os.environ.get("ACTIVE_KEYS", "").split(",") if k]
overrides = {k: os.environ.get(k, "") for k in active}
seen = set()
out = []
key_re = re.compile(r"^([A-Z_][A-Z0-9_]*)=")
with open(src) as f:
    for line in f:
        m = key_re.match(line)
        if m and m.group(1) in overrides:
            k = m.group(1)
            seen.add(k)
            out.append(f"{k}={overrides[k]}\n")
        else:
            out.append(line)
# Anything we wanted to set but didn't see in the example — append.
missing = [k for k in overrides if k not in seen]
if missing:
    out.append("\n# --- added by init.sh ---\n")
    for k in missing:
        out.append(f"{k}={overrides[k]}\n")
with open(dst, "w") as f:
    f.writelines(out)
PYEOF

mv "$TMP_ENV" "$ENV_FILE"
chmod 600 "$ENV_FILE"
trap - EXIT

# --- summary ---
section "Done"
ok "Wrote $ENV_FILE (mode 600)"
echo
echo -e "  ${_BOLD}Generated credentials${_RESET} (also saved in $ENV_FILE):"
echo "    Nextcloud admin   :  admin / $ADMIN_PASSWORD"
echo "    Primary user      :  $USER_NAME / $USER_PASSWORD"
echo "    Bot user          :  istota / $BOT_PASSWORD"
echo "    Postgres          :  $POSTGRES_PASSWORD"
[ -n "$VNC_PASSWORD" ] && echo "    Browser noVNC     :  $VNC_PASSWORD"
echo
echo -e "  ${_BOLD}Configuration:${_RESET}"
echo "    Bot name          :  $ISTOTA_BOT_NAME"
echo "    Public hostname   :  ${DOMAIN:-(localhost-only)}"
echo "    Compose profiles  :  ${COMPOSE_PROFILES:-(none — only the core stack)}"
echo "    Email             :  $ISTOTA_EMAIL_ENABLED"
echo "    ntfy              :  $ISTOTA_NTFY_ENABLED"
[ -n "$ISTOTA_DEVELOPER_GITLAB_USERNAME" ] && echo "    Developer GitLab  :  $ISTOTA_DEVELOPER_GITLAB_USERNAME"
[ -n "$ISTOTA_DEVELOPER_GITHUB_USERNAME" ] && echo "    Developer GitHub  :  $ISTOTA_DEVELOPER_GITHUB_USERNAME"
echo
if [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    warn "No Claude Code token set. Edit $ENV_FILE and set"
    warn "  CLAUDE_CODE_OAUTH_TOKEN=... or ANTHROPIC_API_KEY=... before bringing the stack up."
    echo
fi
echo -e "  ${_BOLD}Next steps:${_RESET}"
echo "    cd $SCRIPT_DIR"
echo "    docker compose up -d"
echo
dim "Tip: re-run with --minimal to skip optional sections, or edit $ENV_FILE directly."
echo
