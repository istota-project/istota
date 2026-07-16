#!/bin/bash
# Istota container entrypoint.
# Waits for Nextcloud provisioning, completes API-based setup, starts scheduler.

set -euo pipefail

CONFIG_FILE="/data/config/config.toml"
PROVISION_FLAG="/mnt/shared/.istota-provisioned"
API_PROVISION_FLAG="/data/config/.api-provisioned"
NC_URL="${NC_INTERNAL_URL:-http://nextcloud}"

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

# Helper: create a Talk group room (roomType=3) and invite USER_NAME.
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
        -d "{\"roomType\":3,\"roomName\":\"${room_name}\"}" 2>/dev/null || echo "000")

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
    echo "[istota] Generating config.toml..."

    DISPLAY_NAME="${USER_DISPLAY_NAME:-$USER_NAME}"
    TIMEZONE="${USER_TIMEZONE:-UTC}"

    cat > "$CONFIG_FILE" <<TOML
# Istota configuration — generated by Docker entrypoint

bot_name = "${ISTOTA_BOT_NAME:-Istota}"
emissaries_enabled = ${ISTOTA_EMISSARIES_ENABLED:-true}

db_path = "/data/db/istota.db"
nextcloud_mount_path = "/mnt/shared"
skills_dir = "/app/config/skills"
temp_dir = "/data/tmp"
max_memory_chars = ${ISTOTA_MAX_MEMORY_CHARS:-0}
TOML

    # Model (optional)
    if [ -n "${ISTOTA_MODEL:-}" ]; then
        echo "model = \"${ISTOTA_MODEL}\"" >> "$CONFIG_FILE"
    fi

    # Disabled skills (optional)
    if [ -n "${ISTOTA_DISABLED_SKILLS:-}" ]; then
        echo "disabled_skills = [$(echo "$ISTOTA_DISABLED_SKILLS" | sed 's/[^,]*/"&"/g')]" >> "$CONFIG_FILE"
    fi

    # Brain (model backend). kind=claude_code (default) shells out to the
    # Claude CLI; kind=native runs the in-process agent loop. The provider
    # API key is read from ISTOTA_BRAIN_NATIVE_API_KEY at load time, so it is
    # deliberately not written into config.toml.
    cat >> "$CONFIG_FILE" <<TOML

[brain]
kind = "${ISTOTA_BRAIN_KIND:-claude_code}"
TOML
    if [ "${ISTOTA_BRAIN_KIND:-claude_code}" = "native" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[brain.native]
provider = "${ISTOTA_BRAIN_NATIVE_PROVIDER:-openai_compat}"
base_url = "${ISTOTA_BRAIN_NATIVE_BASE_URL:-https://api.anthropic.com/v1}"
context_window = ${ISTOTA_BRAIN_NATIVE_CONTEXT_WINDOW:-0}
max_turns = ${ISTOTA_BRAIN_NATIVE_MAX_TURNS:-100}
max_tokens = ${ISTOTA_BRAIN_NATIVE_MAX_TOKENS:-16384}
prompt_caching = ${ISTOTA_BRAIN_NATIVE_PROMPT_CACHING:-false}
TOML
        if [ -n "${ISTOTA_BRAIN_NATIVE_MODEL:-}" ]; then
            echo "model = \"${ISTOTA_BRAIN_NATIVE_MODEL}\"" >> "$CONFIG_FILE"

            # Internal subsystems (conversation selection, sleep-cycle
            # extraction, OCR, …) request models by ROLE — "fast" / "general" /
            # "smart". The claude_code brain maps those to Haiku/Sonnet/Opus;
            # a native brain has no built-in mapping, so without [models.roles]
            # the role name passes through to the endpoint as a bogus model id.
            # Each role defaults to the one configured model; set
            # ISTOTA_BRAIN_NATIVE_MODEL_{FAST,GENERAL,SMART} to point a role at a
            # different model served by the same endpoint.
            cat >> "$CONFIG_FILE" <<TOML

[models.roles]
fast = "${ISTOTA_BRAIN_NATIVE_MODEL_FAST:-$ISTOTA_BRAIN_NATIVE_MODEL}"
general = "${ISTOTA_BRAIN_NATIVE_MODEL_GENERAL:-$ISTOTA_BRAIN_NATIVE_MODEL}"
smart = "${ISTOTA_BRAIN_NATIVE_MODEL_SMART:-$ISTOTA_BRAIN_NATIVE_MODEL}"
TOML
        fi
    fi

    cat >> "$CONFIG_FILE" <<TOML

[security]
sandbox_enabled = ${ISTOTA_SECURITY_SANDBOX_ENABLED:-true}
skill_proxy_enabled = ${ISTOTA_SECURITY_SKILL_PROXY_ENABLED:-true}
skill_proxy_timeout = ${ISTOTA_SECURITY_SKILL_PROXY_TIMEOUT:-300}

[security.network]
enabled = false

[nextcloud]
url = "${NC_URL}"
username = "${BOT_USER:-istota}"
app_password = "${APP_PASSWORD}"

[talk]
enabled = ${ISTOTA_TALK_ENABLED:-true}
bot_username = "${BOT_USER:-istota}"

[email]
enabled = ${ISTOTA_EMAIL_ENABLED:-false}
TOML

    if [ "${ISTOTA_EMAIL_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML
imap_host = "${ISTOTA_EMAIL_IMAP_HOST:-}"
imap_port = ${ISTOTA_EMAIL_IMAP_PORT:-993}
imap_user = "${ISTOTA_EMAIL_IMAP_USER:-}"
imap_password = "${ISTOTA_EMAIL_IMAP_PASSWORD:-}"
smtp_host = "${ISTOTA_EMAIL_SMTP_HOST:-}"
smtp_port = ${ISTOTA_EMAIL_SMTP_PORT:-587}
poll_folder = "${ISTOTA_EMAIL_POLL_FOLDER:-INBOX}"
bot_email = "${ISTOTA_EMAIL_BOT_ADDRESS:-}"
TOML
    fi

    cat >> "$CONFIG_FILE" <<TOML

[conversation]
enabled = ${ISTOTA_CONVERSATION_ENABLED:-true}
lookback_count = ${ISTOTA_CONVERSATION_LOOKBACK_COUNT:-25}
selection_model = "${ISTOTA_CONVERSATION_SELECTION_MODEL:-fast}"
selection_timeout = ${ISTOTA_CONVERSATION_SELECTION_TIMEOUT:-30.0}
skip_selection_threshold = ${ISTOTA_CONVERSATION_SKIP_SELECTION_THRESHOLD:-3}
use_selection = ${ISTOTA_CONVERSATION_USE_SELECTION:-true}
always_include_recent = ${ISTOTA_CONVERSATION_ALWAYS_INCLUDE_RECENT:-5}
context_truncation = ${ISTOTA_CONVERSATION_CONTEXT_TRUNCATION:-0}
context_recency_hours = ${ISTOTA_CONVERSATION_CONTEXT_RECENCY_HOURS:-0}
context_min_messages = ${ISTOTA_CONVERSATION_CONTEXT_MIN_MESSAGES:-10}
previous_tasks_count = ${ISTOTA_CONVERSATION_PREVIOUS_TASKS_COUNT:-3}
talk_context_limit = ${ISTOTA_CONVERSATION_TALK_CONTEXT_LIMIT:-100}

[logging]
level = "${ISTOTA_LOGGING_LEVEL:-INFO}"
output = "${ISTOTA_LOGGING_OUTPUT:-console}"
file = "${ISTOTA_LOGGING_FILE:-}"
rotate = ${ISTOTA_LOGGING_ROTATE:-true}
max_size_mb = ${ISTOTA_LOGGING_MAX_SIZE_MB:-10}
backup_count = ${ISTOTA_LOGGING_BACKUP_COUNT:-5}

[scheduler]
poll_interval = ${ISTOTA_SCHEDULER_POLL_INTERVAL:-5}
talk_poll_interval = ${ISTOTA_SCHEDULER_TALK_POLL_INTERVAL:-10}
talk_poll_timeout = ${ISTOTA_SCHEDULER_TALK_POLL_TIMEOUT:-30}
talk_poll_wait = ${ISTOTA_SCHEDULER_TALK_POLL_WAIT:-2.0}
email_poll_interval = ${ISTOTA_SCHEDULER_EMAIL_POLL_INTERVAL:-60}
briefing_check_interval = ${ISTOTA_SCHEDULER_BRIEFING_CHECK_INTERVAL:-60}
tasks_file_poll_interval = ${ISTOTA_SCHEDULER_TASKS_FILE_POLL_INTERVAL:-30}
shared_file_check_interval = ${ISTOTA_SCHEDULER_SHARED_FILE_CHECK_INTERVAL:-120}
heartbeat_check_interval = ${ISTOTA_SCHEDULER_HEARTBEAT_CHECK_INTERVAL:-60}
progress_updates = ${ISTOTA_SCHEDULER_PROGRESS_UPDATES:-true}
progress_show_tool_use = ${ISTOTA_SCHEDULER_PROGRESS_SHOW_TOOL_USE:-true}
progress_show_text = ${ISTOTA_SCHEDULER_PROGRESS_SHOW_TEXT:-false}
event_log_enabled = ${ISTOTA_SCHEDULER_EVENT_LOG_ENABLED:-true}
push_notification_threshold_seconds = ${ISTOTA_SCHEDULER_PUSH_NOTIFICATION_THRESHOLD_SECONDS:-30}
push_notification_sources = ${ISTOTA_SCHEDULER_PUSH_NOTIFICATION_SOURCES:-[]}
task_timeout_minutes = ${ISTOTA_SCHEDULER_TASK_TIMEOUT_MINUTES:-30}
confirmation_timeout_minutes = ${ISTOTA_SCHEDULER_CONFIRMATION_TIMEOUT_MINUTES:-120}
stale_pending_warn_minutes = ${ISTOTA_SCHEDULER_STALE_PENDING_WARN_MINUTES:-30}
stale_pending_fail_hours = ${ISTOTA_SCHEDULER_STALE_PENDING_FAIL_HOURS:-2}
max_retry_age_minutes = ${ISTOTA_SCHEDULER_MAX_RETRY_AGE_MINUTES:-60}
task_retention_days = ${ISTOTA_SCHEDULER_TASK_RETENTION_DAYS:-7}
email_retention_days = ${ISTOTA_SCHEDULER_EMAIL_RETENTION_DAYS:-7}
worker_idle_timeout = ${ISTOTA_SCHEDULER_WORKER_IDLE_TIMEOUT:-30}
max_foreground_workers = ${ISTOTA_SCHEDULER_MAX_FOREGROUND_WORKERS:-3}
max_background_workers = ${ISTOTA_SCHEDULER_MAX_BACKGROUND_WORKERS:-2}
user_max_foreground_workers = ${ISTOTA_SCHEDULER_USER_MAX_FOREGROUND_WORKERS:-2}
user_max_background_workers = ${ISTOTA_SCHEDULER_USER_MAX_BACKGROUND_WORKERS:-1}
scheduled_job_max_consecutive_failures = ${ISTOTA_SCHEDULER_SCHEDULED_JOB_MAX_CONSECUTIVE_FAILURES:-5}
talk_cache_max_per_conversation = ${ISTOTA_SCHEDULER_TALK_CACHE_MAX_PER_CONVERSATION:-200}
temp_file_retention_days = ${ISTOTA_SCHEDULER_TEMP_FILE_RETENTION_DAYS:-7}
location_ping_retention_days = ${ISTOTA_SCHEDULER_LOCATION_PING_RETENTION_DAYS:-365}

[sleep_cycle]
enabled = ${ISTOTA_SLEEP_CYCLE_ENABLED:-true}
cron = "${ISTOTA_SLEEP_CYCLE_CRON:-0 2 * * *}"
lookback_hours = ${ISTOTA_SLEEP_CYCLE_LOOKBACK_HOURS:-24}
memory_retention_days = ${ISTOTA_SLEEP_CYCLE_MEMORY_RETENTION_DAYS:-0}
auto_load_dated_days = ${ISTOTA_SLEEP_CYCLE_AUTO_LOAD_DATED_DAYS:-3}
curate_user_memory = ${ISTOTA_SLEEP_CYCLE_CURATE_USER_MEMORY:-false}

[channel_sleep_cycle]
enabled = ${ISTOTA_CHANNEL_SLEEP_CYCLE_ENABLED:-true}
cron = "${ISTOTA_CHANNEL_SLEEP_CYCLE_CRON:-0 3 * * *}"
lookback_hours = ${ISTOTA_CHANNEL_SLEEP_CYCLE_LOOKBACK_HOURS:-24}
memory_retention_days = ${ISTOTA_CHANNEL_SLEEP_CYCLE_MEMORY_RETENTION_DAYS:-0}

[memory_search]
enabled = ${ISTOTA_MEMORY_SEARCH_ENABLED:-false}
TOML

    if [ "${ISTOTA_MEMORY_SEARCH_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML
auto_index_conversations = ${ISTOTA_MEMORY_SEARCH_AUTO_INDEX_CONVERSATIONS:-true}
auto_index_memory_files = ${ISTOTA_MEMORY_SEARCH_AUTO_INDEX_MEMORY_FILES:-true}
auto_recall = ${ISTOTA_MEMORY_SEARCH_AUTO_RECALL:-false}
auto_recall_limit = ${ISTOTA_MEMORY_SEARCH_AUTO_RECALL_LIMIT:-5}
TOML
    fi

    # ntfy push notifications are configured per-user via the web settings UI
    # (`/istota/settings`). No operator-shared block is written.

    # Browser (optional)
    if [ "${ISTOTA_BROWSER_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[browser]
enabled = true
api_url = "${ISTOTA_BROWSER_API_URL:-http://localhost:9223}"
vnc_url = "${ISTOTA_BROWSER_VNC_URL:-}"
TOML
    fi

    # Location tracking (optional)
    if [ "${ISTOTA_LOCATION_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[location]
enabled = true
webhooks_port = ${ISTOTA_WEBHOOKS_PORT:-8765}
TOML
    fi

    # Developer skill (optional)
    if [ "${ISTOTA_DEVELOPER_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[developer]
enabled = true
repos_dir = "${ISTOTA_DEVELOPER_REPOS_DIR:-}"
gitlab_url = "${ISTOTA_DEVELOPER_GITLAB_URL:-https://gitlab.com}"
gitlab_token = "${ISTOTA_DEVELOPER_GITLAB_TOKEN:-}"
gitlab_username = "${ISTOTA_DEVELOPER_GITLAB_USERNAME:-}"
gitlab_default_namespace = "${ISTOTA_DEVELOPER_GITLAB_DEFAULT_NAMESPACE:-}"
gitlab_reviewer_id = "${ISTOTA_DEVELOPER_GITLAB_REVIEWER_ID:-}"
github_url = "${ISTOTA_DEVELOPER_GITHUB_URL:-https://github.com}"
github_token = "${ISTOTA_DEVELOPER_GITHUB_TOKEN:-}"
github_username = "${ISTOTA_DEVELOPER_GITHUB_USERNAME:-}"
github_default_owner = "${ISTOTA_DEVELOPER_GITHUB_DEFAULT_OWNER:-}"
github_reviewer = "${ISTOTA_DEVELOPER_GITHUB_REVIEWER:-}"
TOML
        if [ -n "${ISTOTA_DEVELOPER_AUTHOR_CREDIT:-}" ]; then
            echo "author_credit = \"${ISTOTA_DEVELOPER_AUTHOR_CREDIT}\"" >> "$CONFIG_FILE"
        fi
    fi

    # Briefing defaults (optional)
    if [ -n "${ISTOTA_BRIEFING_DEFAULTS_NEWS_LOOKBACK_HOURS:-}" ] || [ -n "${ISTOTA_BRIEFING_DEFAULTS_NEWS_SOURCES:-}" ]; then
        echo "" >> "$CONFIG_FILE"
        echo "[briefing_defaults]" >> "$CONFIG_FILE"
        echo "[briefing_defaults.news]" >> "$CONFIG_FILE"
        echo "lookback_hours = ${ISTOTA_BRIEFING_DEFAULTS_NEWS_LOOKBACK_HOURS:-12}" >> "$CONFIG_FILE"
    fi

    # Web UI (auto-configured when provision-nc.sh registered an OAuth2 client).
    # OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OAUTH_REDIRECT_URI come from the
    # provisioning flag we sourced earlier.
    if [ -n "${OAUTH_CLIENT_ID:-}" ] && [ -n "${OAUTH_CLIENT_SECRET:-}" ]; then
        # NC user-facing URL — what the browser uses to authorize. Compose
        # defaults this to the nginx-proxied public URL; bare-metal can pin it
        # via ISTOTA_WEB_NC_EXTERNAL_URL. NC_URL (internal) is the last resort.
        WEB_NC_EXTERNAL_URL="${ISTOTA_WEB_NC_EXTERNAL_URL:-${NC_URL}}"
        WEB_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        # Site hostname feeds web_app's external-origin / cookie scoping.
        # Defaults to the public host (proxied via nginx); falls back to the
        # web service's own port for bypass-nginx setups.
        WEB_SITE_HOSTNAME="${ISTOTA_WEB_SITE_HOSTNAME:-localhost:${WEB_PORT:-8766}}"
        # Redirect URI: prefer the value provision-nc.sh registered with NC
        # (sourced from $API_PROVISION_FLAG / $PROVISION_FLAG). Fall back to
        # ISTOTA_WEB_CALLBACK_URL (set by compose to the proxied path) so we
        # never write a stale default into config when the flag predates the
        # OAuth2 fields.
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
token_storage = "ephemeral"

[site]
hostname = "${WEB_SITE_HOSTNAME}"
TOML
        echo "[istota] Web UI configured (OAuth2 client=${OAUTH_CLIENT_ID})"
    fi

    # Primary user
    cat >> "$CONFIG_FILE" <<TOML

[users.${USER_NAME}]
display_name = "${DISPLAY_NAME}"
timezone = "${TIMEZONE}"
TOML

    if [ -n "${USER_EMAIL:-}" ]; then
        echo "email_addresses = [\"${USER_EMAIL}\"]" >> "$CONFIG_FILE"
    fi
    # log_channel: explicit env var overrides auto-provisioned token
    EFFECTIVE_LOG_CHANNEL="${USER_LOG_CHANNEL:-${LOG_TOKEN:-}}"
    if [ -n "$EFFECTIVE_LOG_CHANNEL" ]; then
        echo "log_channel = \"${EFFECTIVE_LOG_CHANNEL}\"" >> "$CONFIG_FILE"
    fi
    # alerts_channel: explicit env var overrides auto-provisioned token
    EFFECTIVE_ALERTS_CHANNEL="${USER_ALERTS_CHANNEL:-${ALERTS_TOKEN:-}}"
    if [ -n "$EFFECTIVE_ALERTS_CHANNEL" ]; then
        echo "alerts_channel = \"${EFFECTIVE_ALERTS_CHANNEL}\"" >> "$CONFIG_FILE"
    fi
    if [ -n "${USER_MAX_FOREGROUND_WORKERS:-}" ]; then
        echo "max_foreground_workers = ${USER_MAX_FOREGROUND_WORKERS}" >> "$CONFIG_FILE"
    fi
    if [ -n "${USER_MAX_BACKGROUND_WORKERS:-}" ]; then
        echo "max_background_workers = ${USER_MAX_BACKGROUND_WORKERS}" >> "$CONFIG_FILE"
    fi
    if [ -n "${USER_DISABLED_SKILLS:-}" ]; then
        echo "disabled_skills = [$(echo "$USER_DISABLED_SKILLS" | sed 's/[^,]*/"&"/g')]" >> "$CONFIG_FILE"
    fi

    # --- Module resources ---
    # Append [[users.X.resources]] entries for opt-in modules. Resource
    # blocks must come after all scalar [users.X] keys (TOML rule: subtable
    # arrays close the parent table). Workspace dirs and starter files are
    # seeded outside this block (after the if/else) so toggling a module on
    # later still gets the workspace, even if config.toml already exists.

    if [ "${ISTOTA_FEEDS_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[[users.${USER_NAME}.resources]]
type = "feeds"
name = "Feeds"
TOML
    fi

    if [ "${ISTOTA_LOCATION_ENABLED:-false}" = "true" ] && [ -n "${LOCATION_INGEST_TOKEN:-}" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[[users.${USER_NAME}.resources]]
type = "overland"
name = "Location"
ingest_token = "${LOCATION_INGEST_TOKEN}"
TOML
    fi

    if [ "${ISTOTA_MONEY_ENABLED:-false}" = "true" ]; then
        cat >> "$CONFIG_FILE" <<TOML

[[users.${USER_NAME}.resources]]
type = "money"
name = "Money"
TOML
        # Escape into a TOML basic string (backslash + double-quote only;
        # control chars are unlikely in passwords but get caught by Python's
        # repr fallback). Plain shell interpolation would let a `"` in the
        # password break the TOML.
        if [ -n "${MONARCH_EMAIL:-}" ]; then
            python3 - "${MONARCH_EMAIL}" "${MONARCH_PASSWORD:-}" >> "$CONFIG_FILE" <<'PY'
import sys
def toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
print(f"monarch_email = {toml_str(sys.argv[1])}")
print(f"monarch_password = {toml_str(sys.argv[2])}")
PY
        fi
    fi

    echo "[istota] Config written to ${CONFIG_FILE}"
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
token_storage = "ephemeral"

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
