#!/bin/bash
# Istota container entrypoint, the unprivileged phase.
#
# Runs as uid 10001 with every capability set empty: the image's ENTRYPOINT is
# the root phase (root-phase.sh), which delegates the cgroup, fixes ownership
# and execs this through `istota-drop`. That drop also ran `istota-secrets`, so
# every credential compose mounts under /run/secrets is already in the
# environment here. Nothing in this file may need root.
#
# config.toml is an input. `istota setup` writes it once and the operator owns
# it after that; this script reads it and never writes it. What it does:
#
#   1. refuses to start without a config, or with one it must not run
#      (see the preflight below);
#   2. resolves the secrets store's master key and the web-only token key;
#   3. runs `istota init`, the only migration runner;
#   4. ensures the first admin when there are no users at all;
#   5. provisions that admin's Talk rooms, when Talk is configured;
#   6. writes Claude Code's credentials file, when a token was given;
#   7. execs the scheduler.
set -euo pipefail

# The state volume. Overridable only so tests/test_container_entrypoint.py can
# run this file against a temporary directory; the image never sets it.
DATA_DIR="${ISTOTA_DATA_DIR:-/data}"
CONFIG_FILE="${DATA_DIR}/config/config.toml"
ADMINS_FILE="${DATA_DIR}/config/admins"
SECRET_KEY_FILE="${DATA_DIR}/.secret_key"
WEB_TOKEN_KEY_FILE="${DATA_DIR}/.web_token_key"

# The admin allowlist and the config, for this process and every CLI call it
# makes. Compose declares both too, for `docker compose exec`.
export ISTOTA_ADMINS_FILE="$ADMINS_FILE"
export ISTOTA_CONFIG_PATH="$CONFIG_FILE"

log() { echo "[istota] $*"; }

# 78 (EX_CONFIG), the code the root phase uses for a missing sandbox grant, so
# an operator reading `docker compose ps` sees one code for "fix the setup".
if [ ! -f "$CONFIG_FILE" ]; then
    echo "[istota] REFUSE: there is no ${CONFIG_FILE}. Write one first:" >&2
    echo "[istota]   docker compose run --rm --entrypoint istota-drop istota istota setup" >&2
    exit 78
fi

# --- Preflight: a config this stack must not run ---
#
# Before anything is written. Three refusals, each naming the fix:
#   - it names the compose Nextcloud (`http://nextcloud`), which the shipped
#     compose file no longer runs, and no such host resolves;
#   - full Nextcloud integration, and the workspace is not the VM's rclone
#     mount: a daemon writing into an unmounted /mnt/shared writes into the
#     container, and those files never reach Nextcloud (parity row 9);
#   - behind TLS, `[site] hostname` is not the stack's DOMAIN, which is a login
#     that fails its origin check.
# ISTOTA_TESTBED_SHARED_VOLUME_WORKSPACE is the full test tier's concession: its
# Nextcloud shares a Docker volume with this container rather than an rclone
# mount, so a mount point of any type passes. Nothing an operator runs sets it.
python3 - "$CONFIG_FILE" <<'PY' || exit 78
import os, re, socket, sys
from pathlib import Path
from urllib.parse import urlsplit
from istota.config import load_config

config = load_config(Path(sys.argv[1]))
refusals = []
url = config.nextcloud.url
if url and urlsplit(url).hostname == "nextcloud":
    try:
        socket.getaddrinfo("nextcloud", None)
    except OSError:
        refusals.append(
            f"config.toml names the bundled Nextcloud ({url}), which this compose file no longer runs. "
            "Move it, unchanged, into a compose project of its own and point [nextcloud] url at it, or "
            "switch to local storage; see docs/deployment/moving-the-bundled-nextcloud.md.")
if config.storage_is_nextcloud and config.workspace_path is not None:
    workspace = os.path.normpath(str(config.workspace_path))
    mountpoint, fstype = "/", "unknown"
    with open(os.environ.get("ISTOTA_MOUNTINFO", "/proc/self/mountinfo")) as mounts:
        for line in mounts:
            fields = line.split()
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
            within = workspace == point or workspace.startswith(point.rstrip("/") + "/")
            if within and len(point) >= len(mountpoint):
                mountpoint, fstype = point, fields[fields.index("-") + 1]
    concession = os.environ.get("ISTOTA_TESTBED_SHARED_VOLUME_WORKSPACE") == "1"
    if not (fstype == "fuse.rclone" or (concession and mountpoint == workspace)):
        refusals.append(
            f"the workspace {workspace} is on {mountpoint} ({fstype}), not the VM's rclone mount "
            "(fuse.rclone). Start mount-nextcloud.service on the VM before the stack; nothing is "
            "written into an unmounted store.")
ingress, domain = os.environ.get("INGRESS", ""), os.environ.get("DOMAIN", "")
if ingress in ("direct", "proxied") and config.site.hostname != domain:
    refusals.append(
        f"[site] hostname is {config.site.hostname!r} and the stack's DOMAIN is {domain!r}; "
        "behind TLS they must be the same name, or every login fails its origin check.")
for refusal in refusals:
    print(f"[istota] REFUSE: {refusal}", file=sys.stderr)
sys.exit(1 if refusals else 0)
PY

# --- The secrets store's master key ---
#
# Every credential in the encrypted `secrets` table is derived from it, so it is
# created once and never replaced. `istota setup` writes the file; this is the
# same rule for a volume that has none. A key from the environment (a compose
# secret named istota_secret_key, for an install that keeps it on the VM) wins.
if [ -z "${ISTOTA_SECRET_KEY:-}" ] && [ -s "$SECRET_KEY_FILE" ]; then
    ISTOTA_SECRET_KEY=$(cat "$SECRET_KEY_FILE")
fi
if [ -z "${ISTOTA_SECRET_KEY:-}" ]; then
    if [ -e "$SECRET_KEY_FILE" ]; then
        echo "[istota] REFUSE: ${SECRET_KEY_FILE} exists but is empty; restore it from a backup." >&2
        exit 78
    fi
    # A subshell, so the umask does not reach the daemon and the files it
    # creates in the workspace.
    ( umask 077 && python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$SECRET_KEY_FILE" )
    ISTOTA_SECRET_KEY=$(cat "$SECRET_KEY_FILE")
    log "Generated a new master key at ${SECRET_KEY_FILE}. Back it up."
fi
export ISTOTA_SECRET_KEY

# --- The web-only token key ---
#
# Encrypts the user-scoped Nextcloud OAuth pairs in `web_user_tokens`. Created
# here so the web service can read it, and deliberately not exported into this
# process: only the web service holds it, which is the point of a second key.
if [ ! -s "$WEB_TOKEN_KEY_FILE" ]; then
    ( umask 077 && python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$WEB_TOKEN_KEY_FILE" )
    log "Generated the web token key at ${WEB_TOKEN_KEY_FILE} (web service only)."
fi

log "Initializing database..."
istota -c "$CONFIG_FILE" init

# --- First admin and their Talk rooms ---
#
# Users live in the database, populated by `istota user ensure`. The only user
# this script creates is the first admin, and only into an empty table, so a
# multi-user install is never rewritten by a restart. The row is seeded from the
# admin's `[users.<id>]` block when the config has one (`ensure_profile`'s
# `seed_from`): `user ensure` with no flags would create a row with none of
# those values, and a row, once it exists, owns every field, so an email
# address the wizard wrote would stop counting as the user's own. Talk rooms are
# provisioned for that admin on every boot; `provision-rooms` is idempotent (it
# reuses the rooms it recorded, ISSUE-342) and a channel the user cleared stays
# cleared.
#
# One Python pass reads the config and the table, ensures the row, and prints
# shell assignments through shlex, since the admin's name comes from a file.
eval "$(python3 - "$CONFIG_FILE" "$ADMINS_FILE" <<'PY'
import shlex
import sys
from pathlib import Path

from istota import user_profiles
from istota.config import load_config

config = load_config(Path(sys.argv[1]))
admin = ""
try:
    for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            admin = line
            break
except OSError:
    pass
ensured = ""
if admin:
    db_path = Path(config.db_path)
    if not user_profiles.list_profiles(db_path):
        seed = config.users.get(admin)
        user_profiles.ensure_profile(
            db_path, admin,
            display_name=getattr(seed, "display_name", "") or admin,
            timezone=getattr(seed, "timezone", "") or "",
            seed_from=seed,
        )
        ensured = "1"
nc = config.nextcloud
talk = bool(config.talk.enabled and nc.url and nc.username and nc.app_password)
print(f"FIRST_ADMIN={shlex.quote(admin)}")
print(f"ENSURED={ensured}")
print(f"TALK_CONFIGURED={'1' if talk else ''}")
PY
)"

if [ -z "$FIRST_ADMIN" ]; then
    log "Warning: ${ADMINS_FILE} names nobody; the admin dashboard stays closed until it does."
elif [ -n "$ENSURED" ]; then
    log "Ensured the first admin, ${FIRST_ADMIN}, from the config."
fi

if [ -n "$TALK_CONFIGURED" ] && [ -n "$FIRST_ADMIN" ]; then
    # Best-effort: a Nextcloud that is down at boot costs the rooms until the
    # next restart, not the daemon.
    istota -c "$CONFIG_FILE" nextcloud provision-rooms --user "$FIRST_ADMIN" \
        || log "Warning: Talk room provisioning for ${FIRST_ADMIN} failed; it is retried on the next boot."
fi

# --- Claude Code ---
#
# The claude_code and tmux_claude brains authenticate the `claude` CLI, which
# reads an OAuth token from this file (an API key works from the environment
# alone). Never printed.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    mkdir -p "${HOME}/.claude"
    ( umask 077 && python3 -c '
import json, os, sys
json.dump({"claudeAiOauth": {"accessToken": os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
           "expiresAt": "9999-12-31T23:59:59.999Z"}}, sys.stdout)
' > "${HOME}/.claude/.credentials.json" )
    log "Claude Code OAuth token configured."
fi

# From the venv, never through `uv run`, which would try to sync the read-only
# root. The image puts /app/.venv/bin first on PATH.
log "Starting scheduler daemon..."
exec istota-scheduler --daemon -c "$CONFIG_FILE"
