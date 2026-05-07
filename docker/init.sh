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
#   bash docker/init.sh             # full wizard, then asks before bringing up the stack
#   bash docker/init.sh --minimal   # skip optional sections (passwords + Claude + user only)
#   bash docker/init.sh --force     # overwrite an existing .env without asking
#   bash docker/init.sh --start     # bring the stack up unconditionally (skip the prompt)
#   bash docker/init.sh --no-start  # only write .env; never run docker compose up

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"
FORCE=false
MINIMAL=false
START_PROMPT="ask"   # ask | yes | no

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f)   FORCE=true; shift ;;
        --minimal|-m) MINIMAL=true; shift ;;
        --start)      START_PROMPT="yes"; shift ;;
        --no-start)   START_PROMPT="no";  shift ;;
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

# --- splash ---
# ANSI Shadow figlet rendering of "ISTOTA". Hardcoded so a fresh box without
# `toilet` / `figlet` installed still gets the welcome screen.
echo
printf "${_BLUE}"
cat <<'EOF'
  ██╗███████╗████████╗ ██████╗ ████████╗ █████╗
  ██║██╔════╝╚══██╔══╝██╔═══██╗╚══██╔══╝██╔══██╗
  ██║███████╗   ██║   ██║   ██║   ██║   ███████║
  ██║╚════██║   ██║   ██║   ██║   ██║   ██╔══██║
  ██║███████║   ██║   ╚██████╔╝   ██║   ██║  ██║
  ╚═╝╚══════╝   ╚═╝    ╚═════╝    ╚═╝   ╚═╝  ╚═╝
EOF
printf "${_RESET}"
echo
dim "A CYNIUM Lamplight Release"
dim "first-run setup wizard"
echo

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
dim "Choose carefully — this is the name your bot will go by, in Nextcloud,"
dim "in Talk, in emails, on the web. The Nextcloud login is derived from it"
dim "(lowercased, ASCII), and Nextcloud has no clean way to rename a user"
dim "after creation. You can't change it once the stack is provisioned."
dim "(You wouldn't rename your child or pet either.)"
echo
prompt_value ISTOTA_BOT_NAME "User-facing bot name" "Istota"
mark ISTOTA_BOT_NAME

# Derive the Nextcloud bot username from the bot name (same sanitizer as
# the istota entrypoint's bot_dir_name): lowercase ASCII, spaces→underscore,
# fall back to "istota" if the result is empty or hits a reserved NC name.
# This is set in stone at first provisioning — NC has no clean rename — so
# changing ISTOTA_BOT_NAME post-boot only updates display name, not username.
BOT_USER="$(ISTOTA_BOT_NAME="$ISTOTA_BOT_NAME" python3 -c '
import os, re
name = os.environ["ISTOTA_BOT_NAME"].lower().strip()
name = re.sub(r"\s+", "_", name)
name = re.sub(r"[^a-z0-9_\-]", "", name)
if not name or name in {"admin", "guest", "root", "nextcloud"}:
    name = "istota"
print(name)
')"
mark BOT_USER
if [ "$BOT_USER" != "istota" ]; then
    dim "Nextcloud bot login: ${BOT_USER}"
fi
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
# so we enable it by default on x86_64 hosts and on Apple Silicon (where
# Docker Desktop's Rosetta lets the linux/amd64 image run, slowly).
section "Container profiles"
HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
HOST_OS="$(uname -s 2>/dev/null || echo unknown)"
COMPOSE_PROFILES=""
case "$HOST_ARCH" in
    x86_64|amd64)
        COMPOSE_PROFILES="browser"
        ok "Browser container enabled (host arch: $HOST_ARCH)"
        ;;
    arm64|aarch64)
        if [ "$HOST_OS" = "Darwin" ]; then
            COMPOSE_PROFILES="browser"
            warn "Browser container enabled under Rosetta emulation (host: $HOST_OS/$HOST_ARCH). Expect slow page loads; suitable for previews only."
        else
            warn "Browser container disabled (host: $HOST_OS/$HOST_ARCH; Chrome has no ARM packages and qemu emulation is unreliable)."
        fi
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
# Pin the Compose project name. Without this, Compose names the project after
# the parent directory (typically "docker") and clones in different paths with
# the same parent name silently merge into the same project — recreating each
# other's containers and (worst case) mixing up volumes.
COMPOSE_PROJECT_NAME="istota"
mark COMPOSE_PROJECT_NAME

# --- write .env ---
# Start from .env.example and patch the values we manage; this preserves
# every comment and optional knob the example file documents.
TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/istota-env.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

# Pass values via the environment so the heredoc can stay single-quoted —
# avoids any shell expansion of the rendered passwords/tokens.
ADMIN_PASSWORD="$ADMIN_PASSWORD" \
POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
BOT_USER="$BOT_USER" \
BOT_PASSWORD="$BOT_PASSWORD" \
USER_NAME="$USER_NAME" \
USER_PASSWORD="$USER_PASSWORD" \
USER_DISPLAY_NAME="$USER_DISPLAY_NAME" \
USER_TIMEZONE="$USER_TIMEZONE" \
USER_EMAIL="$USER_EMAIL" \
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
VNC_PASSWORD="$VNC_PASSWORD" \
COMPOSE_PROFILES="$COMPOSE_PROFILES" \
COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" \
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
section "Configuration written"
ok "Wrote $ENV_FILE (mode 600)"
echo
echo -e "  ${_BOLD}Generated credentials${_RESET} (also saved in $ENV_FILE):"
echo "    Nextcloud admin   :  admin / $ADMIN_PASSWORD"
echo "    Primary user      :  $USER_NAME / $USER_PASSWORD"
echo "    Bot user          :  $BOT_USER / $BOT_PASSWORD"
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
    warn "No Claude Code token set. The stack will start, but the bot can't"
    warn "  call the model until you set CLAUDE_CODE_OAUTH_TOKEN or"
    warn "  ANTHROPIC_API_KEY in $ENV_FILE and 'docker compose restart istota'."
    echo
fi

# --- decide whether to bring the stack up ---
should_start=false
if [ "$DOCKER_MISSING" = true ] || [ "$COMPOSE_MISSING" = true ]; then
    warn "Docker / docker compose not available on this host — skipping startup."
    warn "  Copy this directory to a host with Docker, then run 'docker compose up -d'."
else
    case "$START_PROMPT" in
        yes) should_start=true ;;
        no)  should_start=false ;;
        ask)
            echo
            prompt_bool _start_now "Bring the stack up now (docker compose up -d --build)?" "y"
            should_start="$_start_now"
            ;;
    esac
fi

# Build URLs from the .env we just wrote (NC_PORT may have come from the example).
nc_port_raw="$(grep -E '^NC_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
nc_port="${nc_port_raw:-8080}"
public_proto="$(grep -E '^ISTOTA_PUBLIC_PROTO=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
public_proto="${public_proto:-http}"

# Localhost is always reachable when running on this host. The NC_PORT bind in
# docker-compose.yml maps to nginx :80, which proxies both / (Nextcloud) and
# /istota/ (web UI), so they share the host:port.
local_base="http://localhost:${nc_port}"

# Public URL (only meaningful when DOMAIN is set). DOMAIN may already include
# :port; if not, assume the proxy in front terminates on the default port for
# the proto. We don't try to second-guess that.
public_base=""
if [ -n "$DOMAIN" ]; then
    case "$DOMAIN" in
        *:*) public_base="${public_proto}://${DOMAIN}" ;;
        *)   public_base="${public_proto}://${DOMAIN}" ;;
    esac
fi

print_urls() {
    echo
    echo -e "  ${_BOLD}URLs (localhost — always works on this host):${_RESET}"
    echo "    Nextcloud   :  ${local_base}/"
    echo "    Istota web  :  ${local_base}/istota/"
    if [ -n "$public_base" ]; then
        echo
        echo -e "  ${_BOLD}URLs (public — once DNS / your reverse proxy is in place):${_RESET}"
        echo "    Nextcloud   :  ${public_base}/"
        echo "    Istota web  :  ${public_base}/istota/"
    fi
}

if [ "$should_start" = true ]; then
    # Footgun guard: if there's already an "istota" project running from a
    # different path, `docker compose up` from here would merge into it —
    # recreating its containers with our config. Refuse to proceed unless the
    # operator explicitly takes the existing stack down or moves it aside.
    existing_path="$(docker compose ls --format json 2>/dev/null \
        | python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for row in rows or []:
    if row.get("Name") == "istota":
        files = (row.get("ConfigFiles") or "").split(",")
        if files:
            print(os.path.dirname(files[0]))
        break
' 2>/dev/null || true)"
    if [ -n "$existing_path" ] && [ "$existing_path" != "$SCRIPT_DIR" ]; then
        warn "An 'istota' Compose project is already running from:"
        warn "    $existing_path"
        warn "Bringing this stack up here would recreate that one's containers."
        warn "Take it down first ('docker compose -f ${existing_path}/docker-compose.yml down')"
        warn "or set COMPOSE_PROJECT_NAME to a different value in $ENV_FILE."
        die "Refusing to start to protect the existing deployment."
    fi

    # Stale-volume guard: postgres only initializes the DB on first volume
    # create. If istota_postgres_data exists from a prior run, the new
    # POSTGRES_PASSWORD in .env won't take effect — Nextcloud's installer
    # will fail with "password authentication failed for user nextcloud".
    stale_volumes=()
    for v in postgres_data nextcloud_html nextcloud_data shared_files istota_data; do
        if docker volume inspect "${COMPOSE_PROJECT_NAME}_${v}" >/dev/null 2>&1; then
            stale_volumes+=("${COMPOSE_PROJECT_NAME}_${v}")
        fi
    done
    if [ ${#stale_volumes[@]} -gt 0 ]; then
        warn "Found existing Docker volumes from a previous run:"
        for v in "${stale_volumes[@]}"; do
            echo "    $v"
        done
        warn "Postgres won't pick up the new POSTGRES_PASSWORD from .env, so"
        warn "Nextcloud's first-boot installer will fail with an auth error."
        echo
        prompt_bool _wipe_volumes "Remove these volumes and start fresh?" "n"
        if [ "$_wipe_volumes" = true ]; then
            info "Running: docker compose down -v"
            (cd "$SCRIPT_DIR" && docker compose down -v) || \
                die "docker compose down -v failed. Inspect the output above."
        else
            warn "Keeping existing volumes. If startup fails with a postgres auth"
            warn "  error, re-run with --force after 'docker compose down -v'."
        fi
    fi

    section "Starting the stack"
    info "Running: docker compose up -d --build"
    if ! (cd "$SCRIPT_DIR" && docker compose up -d --build); then
        die "docker compose failed. Inspect the output above, then re-run 'docker compose up -d' from $SCRIPT_DIR."
    fi
    echo

    # Poll Nextcloud's status endpoint via the localhost bind. First boot can
    # take a minute or two while NC runs migrations and the istota entrypoint
    # provisions Talk rooms + the OAuth2 client. Cap at 5 minutes — beyond
    # that the user should look at the logs anyway.
    info "Waiting for Nextcloud to come up (first boot can take a minute or two)..."
    nc_status_url="${local_base}/status.php"
    waited=0
    nc_ready=false
    while [ "$waited" -lt 300 ]; do
        if curl -sf "$nc_status_url" 2>/dev/null | grep -q '"installed":true'; then
            nc_ready=true
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    if [ "$nc_ready" = true ]; then
        ok "Nextcloud is up at ${local_base}/"
    else
        warn "Nextcloud didn't respond at ${local_base}/ within 5 minutes."
        warn "  Check logs with: docker compose -f $SCRIPT_DIR/docker-compose.yml logs nextcloud istota"
    fi

    section "Ready"
    print_urls
    echo
    echo -e "  ${_BOLD}Log in:${_RESET}"
    echo "    Open ${local_base}/ and sign in as ${USER_NAME} / ${USER_PASSWORD}"
    echo "    Then visit ${local_base}/istota/ — sign in there with the same"
    echo "    Nextcloud user (OAuth2 redirects through Nextcloud)."
    echo
    dim "Tail logs:    docker compose -f $SCRIPT_DIR/docker-compose.yml logs -f"
    dim "Stop stack:   docker compose -f $SCRIPT_DIR/docker-compose.yml down"
    echo
else
    section "Done"
    echo -e "  ${_BOLD}Next steps:${_RESET}"
    echo "    cd $SCRIPT_DIR"
    echo "    docker compose up -d --build"
    print_urls
    echo
    dim "Tip: re-run with --start to bring the stack up automatically, --no-start"
    dim "to skip the prompt entirely, or --minimal for a shorter wizard."
    echo
fi
