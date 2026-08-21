#!/bin/bash
# Istota container entrypoint.
# Waits for Nextcloud provisioning, completes API-based setup, starts scheduler.

set -euo pipefail

CONFIG_FILE="/data/config/config.toml"
PROVISION_FLAG="/mnt/shared/.istota-provisioned"
API_PROVISION_FLAG="/data/config/.api-provisioned"
NC_URL="${NC_INTERNAL_URL:-http://nextcloud}"

# render-config.sh sits beside this file — /render-config.sh in the image, and
# docker/istota/ in a checkout, which is where the test harness and the lean
# stack's compose file call it from.
ENTRYPOINT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Admin allowlist ---
#
# Web admin dashboard (`/istota/admin`) gates on ISTOTA_ADMINS_FILE via
# `_user_is_web_admin`, which fails closed on an empty allowlist (distinct
# from `Config.is_admin`'s legacy "empty = all admin" behaviour). Seed
# USER_NAME on first boot so the dashboard is reachable in a fresh deploy;
# operators can edit the file directly to grant access to additional users.
# Done up front (before NC provisioning) because the web service polls for
# config.toml to start serving — if the admins file landed after config.toml,
# web could cache an empty allowlist and 403 the dashboard until restart.
ADMINS_FILE="/data/config/admins"
mkdir -p /data/config
touch "$ADMINS_FILE"
if [ -n "${USER_NAME:-}" ] && ! grep -qxF "$USER_NAME" "$ADMINS_FILE"; then
    printf '%s\n' "$USER_NAME" >> "$ADMINS_FILE"
    echo "[istota] Added '${USER_NAME}' to admin allowlist (${ADMINS_FILE})."
fi
export ISTOTA_ADMINS_FILE="$ADMINS_FILE"

# --- Wait for Nextcloud provisioning (occ-based, runs in NC container) ---

echo "[istota] Waiting for Nextcloud provisioning..."
WAIT=0
while [ ! -f "$PROVISION_FLAG" ]; do
    sleep 5
    WAIT=$((WAIT + 5))
    if [ "$WAIT" -ge 600 ]; then
        echo "[istota] ERROR: Timed out waiting for provisioning after 600s"
        exit 1
    fi
    if [ $((WAIT % 30)) -eq 0 ]; then
        echo "[istota] Still waiting for provisioning... (${WAIT}s)"
    fi
done

echo "[istota] Provisioning detected."

# shellcheck source=/dev/null
source "$PROVISION_FLAG"

# --- API-based provisioning (Talk rooms, uses NC HTTP API) ---

APP_PASSWORD="${BOT_PASSWORD}"
ROOM_TOKEN=""
GENERAL_TOKEN=""
LOG_TOKEN=""
ALERTS_TOKEN=""

# Helper: extract OCS token from a room-create response on stdin.
# Prints the token to stdout (empty string on failure).
parse_room_token() {
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    inner = data.get("ocs", {}).get("data") or {}
    if isinstance(inner, dict):
        print(inner.get("token", ""))
    else:
        print("")
except Exception:
    print("")
'
}

# Helper: check whether a user is a participant of a given room. Used to
# scope name-based room lookups to USER_NAME so the bot doesn't return
# another deployment's identically-named room on a shared NC instance.
room_has_participant() {
    local token="$1"
    local user="$2"
    local body_file found
    body_file=$(mktemp)
    curl -sS -o "$body_file" \
        -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
        -H "OCS-APIRequest: true" \
        -X GET "${NC_URL}/ocs/v2.php/apps/spreed/api/v4/room/${token}/participants?format=json" \
        2>/dev/null || true

    found=$(python3 - "$user" <<'PY' < "$body_file"
import json, sys
target = sys.argv[1]
try:
    data = json.load(sys.stdin)
    parts = data.get("ocs", {}).get("data") or []
    if isinstance(parts, list):
        for p in parts:
            if p.get("actorId") == target or p.get("userId") == target:
                print("yes")
                break
except Exception:
    pass
PY
    )
    rm -f "$body_file"
    [ "$found" = "yes" ]
}

# Helper: look up a Talk room by exact displayName where USER_NAME is also
# a participant, return its token (or empty). Used to recover tokens after
# API_PROVISION_FLAG loss without creating duplicates. Scoping to USER_NAME
# prevents collisions when the bot is in multiple users' identically-named
# rooms (e.g. each user's own #general).
find_room_by_name() {
    local room_name="$1"
    local body_file candidates token
    body_file=$(mktemp)
    curl -sS -o "$body_file" \
        -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
        -H "OCS-APIRequest: true" \
        -X GET "${NC_URL}/ocs/v2.php/apps/spreed/api/v4/room?format=json" \
        2>/dev/null || true

    candidates=$(python3 - "$room_name" <<'PY' < "$body_file"
import json, sys
target = sys.argv[1]
try:
    data = json.load(sys.stdin)
    rooms = data.get("ocs", {}).get("data") or []
    if isinstance(rooms, list):
        for r in rooms:
            if r.get("displayName") == target or r.get("name") == target:
                tok = r.get("token", "")
                if tok:
                    print(tok)
except Exception:
    pass
PY
    )
    rm -f "$body_file"

    for token in $candidates; do
        if room_has_participant "$token" "$USER_NAME"; then
            printf '%s' "$token"
            return 0
        fi
    done
}

# Helper: create a Talk group room (roomType=2) and invite USER_NAME.
# Group (roomType=2), not public (roomType=3): #logs carries the execution log and
# #alerts carries confirmations and security alerts, and a public room is
# joinable by anyone holding its token. Matches istota/provision_rooms.py, which
# is the Ansible path's implementation of the same provisioning.
# Reuses an existing room with the same name when one is already present
# (idempotent across API_PROVISION_FLAG loss). Logs go to stderr; stdout = token.
create_group_room() {
    local room_name="$1"
    local token http_code body_file

    token=$(find_room_by_name "$room_name")
    if [ -n "$token" ]; then
        echo "[istota] Group room already exists: ${room_name} -> ${token}" >&2
        printf '%s' "$token"
        return 0
    fi

    body_file=$(mktemp)
    http_code=$(curl -sS -o "$body_file" -w '%{http_code}' \
        -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
        -H "OCS-APIRequest: true" \
        -H "Content-Type: application/json" \
        -X POST "${NC_URL}/ocs/v2.php/apps/spreed/api/v4/room?format=json" \
        -d "{\"roomType\":2,\"roomName\":\"${room_name}\"}" 2>/dev/null || echo "000")

    token=""
    if [ "$http_code" = "200" ] || [ "$http_code" = "201" ]; then
        token=$(parse_room_token < "$body_file")
    fi
    rm -f "$body_file"

    if [ -z "$token" ]; then
        echo "[istota] Warning: could not create group room '${room_name}' (http=${http_code})." >&2
        printf ''
        return 0
    fi

    # Invite the human user. Best-effort — the room exists either way.
    curl -sS -o /dev/null \
        -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
        -H "OCS-APIRequest: true" \
        -H "Content-Type: application/json" \
        -X POST "${NC_URL}/ocs/v2.php/apps/spreed/api/v4/room/${token}/participants" \
        -d "{\"newParticipant\":\"${USER_NAME}\",\"source\":\"users\"}" 2>/dev/null || \
        echo "[istota] Warning: could not invite ${USER_NAME} to '${room_name}'." >&2

    echo "[istota] Group room created: ${room_name} -> ${token}" >&2
    printf '%s' "$token"
}

# Helper: post a chat message to a room. Best-effort.
post_room_message() {
    local token="$1"
    local message="$2"
    [ -z "$token" ] && return 0

    curl -sS -o /dev/null \
        -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
        -H "OCS-APIRequest: true" \
        -H "Content-Type: application/json" \
        -X POST "${NC_URL}/ocs/v2.php/apps/spreed/api/v1/chat/${token}" \
        -d "$(python3 -c '
import json, sys
print(json.dumps({"message": sys.argv[1]}))
' "$message")" 2>/dev/null || true
}

# Load any pre-existing tokens so we can detect what's already provisioned
# (handles upgrades from versions that only stored ROOM_TOKEN).
if [ -f "$API_PROVISION_FLAG" ]; then
    # shellcheck source=/dev/null
    source "$API_PROVISION_FLAG"
fi

# Re-run API provisioning whenever any expected token is missing.
# Helpers (find_room_by_name) make this safe to retry — existing rooms get reused,
# not duplicated.
if [ -z "${ROOM_TOKEN:-}" ] || [ -z "${GENERAL_TOKEN:-}" ] || \
   [ -z "${LOG_TOKEN:-}" ] || [ -z "${ALERTS_TOKEN:-}" ]; then
    echo "[istota] Running API-based provisioning..."

    # Wait for Nextcloud API + Spreed app to be responsive (probe authenticated
    # OCS endpoint, not just status.php — spreed migrations can lag the install).
    echo "[istota] Waiting for Nextcloud + Spreed..."
    for _ in $(seq 1 60); do
        if curl -sf "${NC_URL}/status.php" 2>/dev/null | grep -q '"installed":true' && \
           curl -sf -o /dev/null -w '%{http_code}' \
             -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
             -H "OCS-APIRequest: true" \
             "${NC_URL}/ocs/v2.php/apps/spreed/api/v4/room?format=json" 2>/dev/null \
             | grep -q '^200$'; then
            echo "[istota] Nextcloud + Spreed API ready."
            break
        fi
        sleep 2
    done

    # 1:1 DM between bot and user. Spreed only allows one 1:1 per user pair, so
    # repeated calls return the existing token — naturally idempotent.
    if [ -z "${ROOM_TOKEN:-}" ]; then
        body_file=$(mktemp)
        http_code=$(curl -sS -o "$body_file" -w '%{http_code}' \
            -u "${BOT_USER:-istota}:${BOT_PASSWORD}" \
            -H "OCS-APIRequest: true" \
            -H "Content-Type: application/json" \
            -X POST "${NC_URL}/ocs/v2.php/apps/spreed/api/v4/room?format=json" \
            -d "{\"roomType\":1,\"invite\":\"${USER_NAME}\"}" 2>/dev/null || echo "000")
        if [ "$http_code" = "200" ] || [ "$http_code" = "201" ]; then
            ROOM_TOKEN=$(parse_room_token < "$body_file")
        fi
        rm -f "$body_file"

        if [ -n "$ROOM_TOKEN" ]; then
            echo "[istota] Talk 1:1 room created: ${ROOM_TOKEN}"
        else
            echo "[istota] Warning: could not create 1:1 Talk room. Create one manually in Nextcloud Talk."
        fi
    fi

    # Default channels: #general, #logs, #alerts. Names are not user-prefixed —
    # find_room_by_name() scopes lookups by USER_NAME participation, so each
    # user gets their own set of identically-named rooms without collision.
    [ -z "${GENERAL_TOKEN:-}" ] && GENERAL_TOKEN=$(create_group_room "general")
    [ -z "${LOG_TOKEN:-}" ] && LOG_TOKEN=$(create_group_room "logs")
    [ -z "${ALERTS_TOKEN:-}" ] && ALERTS_TOKEN=$(create_group_room "alerts")

    # Seed CHANNEL.md for #general only (log/alerts are bot-write-only).
    # chown to www-data (uid 33) so the NC container can also access via WebDAV.
    if [ -n "$GENERAL_TOKEN" ]; then
        GENERAL_CHAN_DIR="/mnt/shared/Channels/${GENERAL_TOKEN}"
        if [ ! -f "${GENERAL_CHAN_DIR}/CHANNEL.md" ]; then
            mkdir -p "$GENERAL_CHAN_DIR"
            cat > "${GENERAL_CHAN_DIR}/CHANNEL.md" <<'CHANEOF'
# Channel Memory — general

General-purpose assistant channel. Use this room for questions, requests,
and conversation. The bot remembers context across messages here.
CHANEOF
            chown -R 33:33 "$GENERAL_CHAN_DIR" 2>/dev/null || true
            echo "[istota] Seeded CHANNEL.md for #general."
        fi
    fi

    # Intro message in the alerts channel so the user knows it exists.
    # Only post if this is a brand-new alerts room (no prior provisioning).
    if [ -n "$ALERTS_TOKEN" ] && [ ! -f "$API_PROVISION_FLAG" ]; then
        post_room_message "$ALERTS_TOKEN" \
            "This is your alerts channel. Important notifications from your assistant — confirmations, errors, heartbeat failures, reminders — will appear here."
    fi

    echo "[istota] API provisioning complete."
else
    echo "[istota] API provisioning already done."
fi

# --- Module provisioning (location token generation) ---
#
# Location's ingest token must be stable across boots so the user's phone
# keeps working. Resolution order: env var → previously-persisted flag value
# → freshly generated. Done here (before config gen) because the token feeds
# both the [[users.X.resources]] block and the activation banner below.
if [ "${ISTOTA_LOCATION_ENABLED:-false}" = "true" ]; then
    if [ -z "${LOCATION_INGEST_TOKEN:-}" ]; then
        LOCATION_INGEST_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        echo "[istota] Generated new LOCATION_INGEST_TOKEN."
    fi
fi

# Persist all tokens so subsequent boots skip the work and modules survive
# restarts without re-asking the user. Rewriting unconditionally keeps the
# flag in sync when modules are toggled on/off across runs.
cat > "$API_PROVISION_FLAG" <<EOF
APP_PASSWORD=${APP_PASSWORD}
ROOM_TOKEN=${ROOM_TOKEN}
GENERAL_TOKEN=${GENERAL_TOKEN}
LOG_TOKEN=${LOG_TOKEN}
ALERTS_TOKEN=${ALERTS_TOKEN}
LOCATION_INGEST_TOKEN=${LOCATION_INGEST_TOKEN:-}
EOF

# --- Generate config ---

if [ ! -f "$CONFIG_FILE" ]; then
    # The render lives in render-config.sh, executed rather than sourced. This
    # file runs `set -euo pipefail`, so a sourced render would inherit `-u` and
    # abort the whole boot on any unset variable it happened to read; as a
    # subprocess it fails the render alone. It is also what the image tier and
    # the lean compose stack call directly, neither of which has a Nextcloud to
    # provision against.
    #
    # Its inputs are the provisioning locals above, which `source
    # "$PROVISION_FLAG"` and the room-create calls left as shell variables
    # rather than environment ones. `export` on an unset name is a no-op that
    # puts nothing in the child environment, so the script's own `:-` defaults
    # still apply. The full contract is documented in its header.
    export CONFIG_FILE USER_NAME NC_URL APP_PASSWORD BOT_USER \
        USER_DISPLAY_NAME USER_TIMEZONE USER_EMAIL \
        USER_LOG_CHANNEL USER_ALERTS_CHANNEL USER_DISABLED_SKILLS \
        USER_MAX_FOREGROUND_WORKERS USER_MAX_BACKGROUND_WORKERS \
        LOG_TOKEN ALERTS_TOKEN LOCATION_INGEST_TOKEN \
        OAUTH_CLIENT_ID OAUTH_CLIENT_SECRET OAUTH_REDIRECT_URI \
        WEB_PORT MONARCH_EMAIL MONARCH_PASSWORD
    "$ENTRYPOINT_DIR/render-config.sh"
else
    echo "[istota] Config already exists, skipping generation."

    # Upgrade path: if a prior config was generated before auto-channel support,
    # backfill log_channel/alerts_channel under [users.${USER_NAME}] when the
    # tokens are now available and the keys are absent. Done with a small
    # python helper so we don't re-parse TOML in shell.
    if [ -n "${LOG_TOKEN:-}" ] || [ -n "${ALERTS_TOKEN:-}" ]; then
        python3 - "$CONFIG_FILE" "$USER_NAME" "${LOG_TOKEN:-}" "${ALERTS_TOKEN:-}" <<'PY'
import sys, re
path, user, log_tok, alert_tok = sys.argv[1:5]
text = open(path, "r", encoding="utf-8").read()
section_re = re.compile(rf"^\[users\.{re.escape(user)}\]\s*$", re.M)
m = section_re.search(text)
if not m:
    sys.exit(0)
# Find end of section (next [heading] or EOF).
next_hdr = re.search(r"^\[", text[m.end():], re.M)
end = m.end() + (next_hdr.start() if next_hdr else len(text) - m.end())
section = text[m.start():end]
additions = []
if log_tok and not re.search(r"^log_channel\s*=", section, re.M):
    additions.append(f'log_channel = "{log_tok}"')
if alert_tok and not re.search(r"^alerts_channel\s*=", section, re.M):
    additions.append(f'alerts_channel = "{alert_tok}"')
if not additions:
    sys.exit(0)
# Insert just before the next heading (or at EOF), preserving trailing newlines.
insertion = ("\n" if not section.endswith("\n") else "") + "\n".join(additions) + "\n"
text = text[:end] + insertion + text[end:]
open(path, "w", encoding="utf-8").write(text)
print(f"[istota] Backfilled {len(additions)} channel field(s) in {path}", file=sys.stderr)
PY
    fi

    # Upgrade path #1b: emit a missing [web] / [site] block when the OAuth2
    # client landed on a later boot. Operators upgrading from versions where
    # provision-nc.sh's OAuth2 step silently failed (e.g. the pre-Phase-2.1
    # script that called the nonexistent occ oauth2:add-client) end up with a
    # config.toml that has no [web] section. Without this, `provision-nc.sh`
    # could be fixed and re-run but the web service would still 500.
    if [ -n "${OAUTH_CLIENT_ID:-}" ] && [ -n "${OAUTH_CLIENT_SECRET:-}" ] \
       && ! grep -q '^\[web\]' "$CONFIG_FILE"; then
        WEB_NC_EXTERNAL_URL="${ISTOTA_WEB_NC_EXTERNAL_URL:-${NC_URL}}"
        WEB_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        WEB_SITE_HOSTNAME="${ISTOTA_WEB_SITE_HOSTNAME:-localhost:${WEB_PORT:-8766}}"
        WEB_REDIRECT_URI="${OAUTH_REDIRECT_URI:-${ISTOTA_WEB_CALLBACK_URL:-http://localhost:${WEB_PORT:-8766}/istota/callback}}"
        cat >> "$CONFIG_FILE" <<TOML

[web]
enabled = true
port = ${WEB_PORT:-8766}
oauth2_provider = "${WEB_NC_EXTERNAL_URL}"
oauth2_client_id = "${OAUTH_CLIENT_ID}"
oauth2_client_secret = "${OAUTH_CLIENT_SECRET}"
oauth2_token_endpoint = "${NC_URL}/index.php/apps/oauth2/api/v1/token"
oauth2_userinfo_endpoint = "${NC_URL}/ocs/v2.php/cloud/user?format=json"
oauth2_redirect_uri = "${WEB_REDIRECT_URI}"
session_secret_key = "${WEB_SESSION_SECRET}"
token_storage = "${ISTOTA_WEB_TOKEN_STORAGE:-encrypted}"

[web.chat]
talk_read_sync_interval = ${ISTOTA_WEB_CHAT_TALK_READ_SYNC_INTERVAL:-60}

[site]
hostname = "${WEB_SITE_HOSTNAME}"
TOML
        echo "[istota] Backfilled [web] / [site] in ${CONFIG_FILE} (OAuth2 client=${OAUTH_CLIENT_ID})"
    fi

    # Upgrade path #2: when a module is enabled after first config generation
    # (operator flips ISTOTA_FEEDS_ENABLED / ISTOTA_MONEY_ENABLED /
    # ISTOTA_LOCATION_ENABLED to true on a subsequent boot), backfill the
    # corresponding [[users.X.resources]] entry. Without this, the workspace
    # dirs below would get seeded but the loader would never find a resource
    # → silent module failure.
    python3 - "$CONFIG_FILE" "$USER_NAME" \
        "${ISTOTA_FEEDS_ENABLED:-false}" \
        "${ISTOTA_MONEY_ENABLED:-false}" \
        "${MONARCH_EMAIL:-}" "${MONARCH_PASSWORD:-}" \
        "${ISTOTA_LOCATION_ENABLED:-false}" \
        "${LOCATION_INGEST_TOKEN:-}" <<'PY'
import sys, re
(path, user, feeds_on, money_on, monarch_email, monarch_password,
 location_on, location_token) = sys.argv[1:9]

text = open(path, "r", encoding="utf-8").read()


def has_resource(rtype: str) -> bool:
    """True if [[users.X.resources]] of this type already exists."""
    pattern = (
        rf'\[\[users\.{re.escape(user)}\.resources\]\][^\[]*'
        rf'type\s*=\s*"{re.escape(rtype)}"'
    )
    return re.search(pattern, text, re.S) is not None


def toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


additions: list[str] = []

if feeds_on == "true" and not has_resource("feeds"):
    additions.append(
        f'[[users.{user}.resources]]\n'
        f'type = "feeds"\n'
        f'name = "Feeds"'
    )

if money_on == "true" and not has_resource("money"):
    block = (
        f'[[users.{user}.resources]]\n'
        f'type = "money"\n'
        f'name = "Money"'
    )
    if monarch_email:
        block += (
            f'\nmonarch_email = {toml_str(monarch_email)}'
            f'\nmonarch_password = {toml_str(monarch_password)}'
        )
    additions.append(block)

if location_on == "true" and location_token and not has_resource("overland"):
    additions.append(
        f'[[users.{user}.resources]]\n'
        f'type = "overland"\n'
        f'name = "Location"\n'
        f'ingest_token = {toml_str(location_token)}'
    )

if not additions:
    sys.exit(0)

# Append at EOF — array-of-table entries are order-independent in TOML and
# can live anywhere after the parent [users.X] table.
suffix = ("" if text.endswith("\n") else "\n") + "\n" + "\n\n".join(additions) + "\n"
open(path, "w", encoding="utf-8").write(text + suffix)
print(f"[istota] Backfilled {len(additions)} module resource(s) in {path}",
      file=sys.stderr)
PY
fi

# --- Module workspace seeding ---
#
# Workspace dirs live on the shared Nextcloud volume and persist across
# container rebuilds. Seeding runs on every boot so that flipping a module
# on after first config generation still gets the workspace prepared. All
# operations are idempotent: mkdir -p, [ ! -f ] guards on starter files.
#
# {workspace} = /mnt/shared/Users/${USER_NAME}/${BOT_DIR}
# Module loaders (feeds/_loader.py, money/_loader.py) compute this path
# from nextcloud_mount_path + user_id + config.bot_dir_name.
BOT_DIR_NAME=$(python3 -c '
import re, sys, os
name = os.environ.get("ISTOTA_BOT_NAME", "Istota").lower().strip()
name = re.sub(r"\s+", "_", name)
name = re.sub(r"[^a-z0-9_\-]", "", name)
print(name or "istota")
')
WORKSPACE_DIR="/mnt/shared/Users/${USER_NAME}/${BOT_DIR_NAME}"

if [ "${ISTOTA_FEEDS_ENABLED:-false}" = "true" ]; then
    FEEDS_DIR="${WORKSPACE_DIR}/feeds"
    # data/ holds the per-user SQLite (feeds.db) — the sole source of
    # truth for subscriptions, categories, entries, and read state.
    # Add subscriptions via Talk, the CLI (`istota-skill feeds add ...`),
    # or the web UI's Feeds settings page.
    mkdir -p "${FEEDS_DIR}/data"
    chown -R 33:33 "$FEEDS_DIR" 2>/dev/null || true
fi

if [ "${ISTOTA_MONEY_ENABLED:-false}" = "true" ]; then
    MONEY_DIR="${WORKSPACE_DIR}/money"
    # Workspace synth: data_dir={workspace}/money, db_path={data_dir}/data/money.db,
    # default ledger={data_dir}/ledgers/main.beancount. Config files (INVOICING.md
    # etc) live in {data_dir}/config first.
    mkdir -p "${MONEY_DIR}/data" "${MONEY_DIR}/ledgers" "${MONEY_DIR}/config"
    if [ ! -f "${MONEY_DIR}/ledgers/main.beancount" ]; then
        cat > "${MONEY_DIR}/ledgers/main.beancount" <<'BEANEOF'
;; Main ledger — add accounts and transactions below.
;; Or use Talk: "add transaction $85.50 groceries at Whole Foods from checking"

option "title" "Personal Ledger"
option "operating_currency" "USD"

; === Chart of Accounts ===

; Assets
2020-01-01 open Assets:Bank:Checking USD
2020-01-01 open Assets:Bank:Savings USD
2020-01-01 open Assets:Cash USD

; Expenses
2020-01-01 open Expenses:Food USD
2020-01-01 open Expenses:Housing USD
2020-01-01 open Expenses:Transport USD
2020-01-01 open Expenses:Utilities USD
2020-01-01 open Expenses:Shopping USD
2020-01-01 open Expenses:Health USD
2020-01-01 open Expenses:Entertainment USD
2020-01-01 open Expenses:Other USD

; Income
2020-01-01 open Income:Salary USD
2020-01-01 open Income:Other USD

; Equity
2020-01-01 open Equity:Opening-Balances USD
BEANEOF
        echo "[istota] Seeded starter ledger at ${MONEY_DIR}/ledgers/main.beancount"
    fi
    chown -R 33:33 "$MONEY_DIR" 2>/dev/null || true
fi

# --- Module activation summary (visible in `docker logs istota`) ---
{
    enabled_any=0
    if [ "${ISTOTA_FEEDS_ENABLED:-false}" = "true" ] || \
       [ "${ISTOTA_LOCATION_ENABLED:-false}" = "true" ] || \
       [ "${ISTOTA_MONEY_ENABLED:-false}" = "true" ]; then
        enabled_any=1
    fi
    if [ "$enabled_any" = "1" ]; then
        echo "==========================================================="
        echo " ISTOTA MODULES"
        echo "==========================================================="
        if [ "${ISTOTA_FEEDS_ENABLED:-false}" = "true" ]; then
            echo " Feeds:    enabled — manage in web UI (Feeds → settings) or via 'istota-skill feeds'"
        fi
        if [ "${ISTOTA_LOCATION_ENABLED:-false}" = "true" ]; then
            # Webhooks bind their port directly (not nginx-proxied), so the
            # banner URL is {proto}://{host_without_port}:{WEBHOOKS_PORT}.
            # Source order for the public host: ISTOTA_PUBLIC_HOST → DOMAIN
            # → "localhost". Any trailing :port is stripped before reattaching
            # the webhooks port.
            _PUBLIC_HOST_RAW="${ISTOTA_PUBLIC_HOST:-${DOMAIN:-localhost}}"
            _PUBLIC_HOST_BARE="${_PUBLIC_HOST_RAW%%:*}"
            _BANNER_URL="${ISTOTA_PUBLIC_PROTO:-http}://${_PUBLIC_HOST_BARE}:${ISTOTA_WEBHOOKS_PORT:-8765}/webhooks/location"
            echo " Location: enabled"
            echo "   Configure Overland (iOS):"
            echo "     URL:   ${_BANNER_URL}"
            echo "     Token: ${LOCATION_INGEST_TOKEN}"
            echo "   (Run 'docker compose --profile location up -d' to start the webhooks service."
            echo "    If you enabled location *after* first boot, also run"
            echo "    'docker compose restart webhooks' so it picks up the new token.)"
        fi
        if [ "${ISTOTA_MONEY_ENABLED:-false}" = "true" ]; then
            if [ -n "${MONARCH_EMAIL:-}" ]; then
                MONARCH_STATUS="enabled (${MONARCH_EMAIL})"
            else
                MONARCH_STATUS="not configured"
            fi
            echo " Money:    enabled — ledger at ${WORKSPACE_DIR}/money/ledgers/main.beancount"
            echo "   Monarch sync: ${MONARCH_STATUS}"
        fi
        echo "==========================================================="
    fi
} >&2

# --- Application secret key (Phase 5) ---
#
# ISTOTA_SECRET_KEY derives the Fernet key that encrypts tier-2 credentials
# in the SQLite ``secrets`` table. Resolution order:
#   1. environment (operator-supplied via .env / compose)
#   2. previously-persisted file at /data/.secret_key
#   3. fresh hex-32 value, written to that file with mode 600
# The file lives on the istota_data volume so it survives container rebuilds.
# Losing the key makes existing secrets unrecoverable — operators are warned
# in .env.example to back it up.
SECRET_KEY_FILE="/data/.secret_key"
if [ -z "${ISTOTA_SECRET_KEY:-}" ] && [ -f "$SECRET_KEY_FILE" ]; then
    ISTOTA_SECRET_KEY=$(cat "$SECRET_KEY_FILE")
fi
if [ -z "${ISTOTA_SECRET_KEY:-}" ]; then
    ISTOTA_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    # Subshell so the restrictive umask doesn't leak into the rest of this
    # script — the Python scheduler later seeds workspace files (README.md,
    # notes/, scripts/) on the shared NC volume, and a leaked 0077 umask
    # made them unreadable to NC's www-data.
    ( umask 077 && printf '%s' "$ISTOTA_SECRET_KEY" > "$SECRET_KEY_FILE" )
    chmod 600 "$SECRET_KEY_FILE"
    echo "[istota] Generated new ISTOTA_SECRET_KEY (persisted to ${SECRET_KEY_FILE})."
fi
export ISTOTA_SECRET_KEY

# --- Web-only user-token key ---
#
# ISTOTA_WEB_TOKEN_KEY encrypts the user-scoped Nextcloud OAuth pairs in the
# web_user_tokens table (post-as-user Talk mirroring + read-state sync). It
# is generated and persisted here so the web service can pick it up, but it
# is deliberately NOT exported into this (scheduler) process — only the web
# service loads it. That custody boundary is the point of the separate key.
WEB_TOKEN_KEY_FILE="/data/.web_token_key"
if [ ! -f "$WEB_TOKEN_KEY_FILE" ]; then
    ( umask 077 && python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$WEB_TOKEN_KEY_FILE" )
    chmod 600 "$WEB_TOKEN_KEY_FILE"
    echo "[istota] Generated new web token key (persisted to ${WEB_TOKEN_KEY_FILE}; web service only)."
fi

# --- Initialize database ---

echo "[istota] Initializing database..."
uv run istota -c "$CONFIG_FILE" init

# --- Claude Code authentication ---

echo "[istota] Configuring Claude Code..."
CLAUDE_DIR="${HOME}/.claude"
mkdir -p "$CLAUDE_DIR"

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    # OAuth token provided — write credentials file
    echo "{\"claudeAiOauth\":{\"accessToken\":\"${CLAUDE_CODE_OAUTH_TOKEN}\",\"expiresAt\":\"9999-12-31T23:59:59.999Z\"}}" \
        > "$CLAUDE_DIR/.credentials.json"
    chmod 600 "$CLAUDE_DIR/.credentials.json"
    echo "[istota] Claude Code OAuth token configured."
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "[istota] Using ANTHROPIC_API_KEY (direct API access)."
else
    echo "[istota] WARNING: No Claude Code credentials found. Set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY."
fi

if claude --version >/dev/null 2>&1; then
    echo "[istota] Claude Code: $(claude --version 2>&1 | head -1)"
fi

# --- Workspace perms — make NC (www-data, uid 33) co-owner ---
#
# The istota container runs as root, but NC's PHP runs as www-data (uid 33)
# against the same /mnt/shared volume. The scheduler seeds workspace files
# (README.md, notes/, scripts/, config/*.md, …) lazily AFTER this script
# execs to the daemon, so we can't chown them post-hoc here. Instead:
#
#   - chown /mnt/shared to 33:33 — current files are now www-data-owned.
#   - setgid (chmod 2775) every dir — files Python creates inside inherit
#     group=33 automatically (kernel rule for setgid dirs).
#   - umask 002 below — files come out 664, dirs 2775; combined with the
#     inherited group=33, www-data has read AND write access.
#
# Idempotent — safe on every boot, including restarts after the volume is
# already populated. The whole block is best-effort; failures (e.g. files
# the scheduler is mid-write to) shouldn't block startup.
for d in /mnt/shared/Users /mnt/shared/Channels; do
    if [ -d "$d" ]; then
        chown -R 33:33 "$d" 2>/dev/null || true
        find "$d" -type d -exec chmod 2775 {} + 2>/dev/null || true
        find "$d" -type f -exec chmod 664 {} + 2>/dev/null || true
    fi
done
umask 002

# --- Start scheduler ---

echo "[istota] Starting scheduler daemon..."
exec uv run istota-scheduler --daemon -c "$CONFIG_FILE"
