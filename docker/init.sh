#!/bin/bash
# Istota Docker — first-run bootstrap
#
# Writes a minimal .env next to docker-compose.yml so the user can run
# `docker compose up -d` straight after. Auto-generates passwords for the
# Nextcloud / Postgres / bot / human-user accounts; only the Claude Code
# OAuth token and the human user's name need to be entered by hand.
#
# Usage:
#   bash docker/init.sh             # interactive
#   bash docker/init.sh --force     # overwrite an existing .env without asking

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"
FORCE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f) FORCE=true; shift ;;
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

# --- Claude Code OAuth token instructions ---
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
echo

# --- primary user ---
section "Primary user"
default_user="$(id -un 2>/dev/null || echo user)"
read -rp "  USER_NAME (Nextcloud login id) [$default_user]: " USER_NAME
USER_NAME="${USER_NAME:-$default_user}"

read -rp "  USER_DISPLAY_NAME (e.g. Alice Example) [$USER_NAME]: " USER_DISPLAY_NAME
USER_DISPLAY_NAME="${USER_DISPLAY_NAME:-$USER_NAME}"

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
read -rp "  USER_TIMEZONE (IANA, e.g. Europe/Berlin) [$default_tz]: " USER_TIMEZONE
USER_TIMEZONE="${USER_TIMEZONE:-$default_tz}"

# --- browser container default ---
# The browser profile bundles a Chromium + bot-detection countermeasures
# container that the `browse` skill talks to. Chrome has no ARM packages,
# so we only enable it by default on x86_64 hosts.
section "Optional containers"
HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
case "$HOST_ARCH" in
    x86_64|amd64)
        COMPOSE_PROFILES="browser"
        ok "Browser container enabled by default (host arch: $HOST_ARCH)"
        dim "Disable later by removing 'browser' from COMPOSE_PROFILES in .env."
        ;;
    *)
        COMPOSE_PROFILES=""
        warn "Browser container disabled (host arch: $HOST_ARCH; Chrome has no ARM packages)."
        dim "GPS / location ingest: enable later by adding 'location' to COMPOSE_PROFILES."
        ;;
esac

# --- generate passwords ---
section "Generating passwords"
ADMIN_PASSWORD="$(gen_pw)";    ok "ADMIN_PASSWORD"
USER_PASSWORD="$(gen_pw)";     ok "USER_PASSWORD"
BOT_PASSWORD="$(gen_pw)";      ok "BOT_PASSWORD"
POSTGRES_PASSWORD="$(gen_pw)"; ok "POSTGRES_PASSWORD"
if [ -n "$COMPOSE_PROFILES" ]; then
    VNC_PASSWORD="$(gen_pw)";  ok "VNC_PASSWORD (browser container noVNC)"
else
    VNC_PASSWORD=""
fi

# --- write .env ---
# Start from .env.example and patch the values we manage; this preserves
# every comment and optional knob the example file documents.
TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/istota-env.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

# Use python for the substitution: it's already a hard dep of the project,
# and sed-with-special-chars (passwords, tokens) is a footgun. Values are
# passed via the environment so the heredoc can stay single-quoted —
# avoids any shell expansion of the rendered passwords/tokens.
ADMIN_PASSWORD="$ADMIN_PASSWORD" \
POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
BOT_PASSWORD="$BOT_PASSWORD" \
USER_NAME="$USER_NAME" \
USER_PASSWORD="$USER_PASSWORD" \
USER_DISPLAY_NAME="$USER_DISPLAY_NAME" \
USER_TIMEZONE="$USER_TIMEZONE" \
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
VNC_PASSWORD="$VNC_PASSWORD" \
COMPOSE_PROFILES="$COMPOSE_PROFILES" \
python3 - "$EXAMPLE_FILE" "$TMP_ENV" <<'PYEOF'
import os, sys, re
src, dst = sys.argv[1], sys.argv[2]
keys = (
    "ADMIN_PASSWORD", "POSTGRES_PASSWORD", "BOT_PASSWORD",
    "USER_NAME", "USER_PASSWORD", "USER_DISPLAY_NAME",
    "USER_TIMEZONE", "CLAUDE_CODE_OAUTH_TOKEN",
    "VNC_PASSWORD", "COMPOSE_PROFILES",
)
overrides = {k: os.environ.get(k, "") for k in keys}
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

section "Done"
ok "Wrote $ENV_FILE (mode 600)"
echo
echo -e "  ${_BOLD}Generated credentials${_RESET} (also saved in $ENV_FILE):"
echo "    Nextcloud admin   :  admin / $ADMIN_PASSWORD"
echo "    Primary user      :  $USER_NAME / $USER_PASSWORD"
echo "    Bot user          :  istota / $BOT_PASSWORD"
echo "    Postgres          :  $POSTGRES_PASSWORD"
if [ -n "$VNC_PASSWORD" ]; then
    echo "    Browser noVNC     :  $VNC_PASSWORD"
fi
echo
if [ -n "$COMPOSE_PROFILES" ]; then
    echo -e "  ${_BOLD}Active compose profiles:${_RESET} $COMPOSE_PROFILES"
    echo
fi
if [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    warn "No Claude Code token set. Edit $ENV_FILE and set"
    warn "  CLAUDE_CODE_OAUTH_TOKEN=... or ANTHROPIC_API_KEY=... before bringing the stack up."
    echo
fi
echo -e "  ${_BOLD}Next steps:${_RESET}"
echo "    cd $SCRIPT_DIR"
echo "    docker compose up -d"
echo
dim "Tip: review $ENV_FILE for optional features (DOMAIN, email, ntfy, location, …)."
echo
