#!/bin/bash
# export-bare-metal.sh: one tarball of a bare-metal install's state, for
# `istota migrate import-bare-metal` on the new VM. Run as root on the source,
# by hand. Never by Ansible.
#
#   export-bare-metal.sh --namespace NS [--home DIR] [--output FILE]
#                        [--no-stop] [--with-browser-profile] [--dirty-timeout SECONDS]
#
# The tar stream goes to stdout unless --output names a file, so it can be
# piped off a host with no room for it: `ssh host export-bare-metal.sh
# --namespace NS > export.tar`. A file is written 0600.
#
# Default (stopped) mode is the cutover:
#   1. refuses while the update cron (/etc/cron.d/NS-update) is enabled, since
#      it restarts the units on any new commit; comment its line out first;
#   2. stops and disables NS-scheduler, NS-web, NS-webhooks and
#      NS-whatsapp-baileys, and stops the devbox credential proxies;
#   3. waits until no rclone VFS cache entry is marked "Dirty" (a write not yet
#      uploaded exists only on this host), stops and disables the mount, and
#      checks again;
#   4. snapshots every database with the daemon's own db_backup, as the
#      service user, into a scratch directory, and writes the tarball.
#
# --no-stop is the rehearsal's online mode: nothing is stopped, the cron is not
# checked and the snapshot is online. The export is then a copy of a running
# bot and is imported only with `--rehearsal`.
#
# The scratch directory holds the database snapshot and copies of the secrets
# files, and is deleted on exit whatever happens. The tarball's members are
# what the import reads: MANIFEST, config/, secrets/, db/<date>/, and repos/,
# whatsapp-baileys-session/, logs/ and browser-profile/ when present.
#
# ISTOTA_EXPORT_ETC replaces /etc, for the tests; it is the one thing that lets
# the script run as a user other than root.
# --- end usage ---
set -euo pipefail

ETC="${ISTOTA_EXPORT_ETC:-/etc}"
NS=""
HOME_DIR=""
OUTPUT="-"
NO_STOP=0
WITH_BROWSER=0
DIRTY_TIMEOUT=600
scratch=""

log() { echo "[export-bare-metal] $*" >&2; }
die() { echo "[export-bare-metal] REFUSE: $*" >&2; exit 1; }
usage() {
    sed -n '2,/^# --- end usage ---$/p' "$0" | grep -v '^# --- end usage ---$' | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --namespace) NS="${2:?--namespace needs a value}"; shift 2 ;;
        --home) HOME_DIR="${2:?--home needs a value}"; shift 2 ;;
        --output) OUTPUT="${2:?--output needs a value}"; shift 2 ;;
        --stdout) OUTPUT="-"; shift ;;
        --no-stop) NO_STOP=1; shift ;;
        --with-browser-profile) WITH_BROWSER=1; shift ;;
        --dirty-timeout) DIRTY_TIMEOUT="${2:?--dirty-timeout needs a value}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done

[ -n "$NS" ] || { usage >&2; exit 64; }
case "$NS" in *[!a-z0-9_-]*) die "namespace '${NS}' is not a plain name" ;; esac
case "$DIRTY_TIMEOUT" in ''|*[!0-9]*) die "--dirty-timeout takes whole seconds" ;; esac
if [ "$(id -u)" -ne 0 ] && [ -z "${ISTOTA_EXPORT_ETC:-}" ]; then
    die "run as root: it stops units and reads the secrets files"
fi
if [ "$OUTPUT" = "-" ] && [ -t 1 ]; then
    die "stdout is a terminal; redirect it to a file or pass --output FILE"
fi

HOME_DIR="${HOME_DIR:-/srv/app/${NS}}"
CHECKOUT="${HOME_DIR}/istota"
CONFIG="${CHECKOUT}/config/config.toml"
PYTHON="${CHECKOUT}/.venv/bin/python"
CRON_FILE="${ETC}/cron.d/${NS}-update"
SECRETS_DIR="${ETC}/${NS}"
VFS_META="${HOME_DIR}/.cache/rclone/vfsMeta"
[ -f "$CONFIG" ] || die "no config at ${CONFIG} (--home?)"
[ -x "$PYTHON" ] || die "no istota venv at ${PYTHON}"
[ -f "${SECRETS_DIR}/secrets.env" ] || die "no ${SECRETS_DIR}/secrets.env: the master key is in it"
SERVICE_USER="$(stat -c %U "$HOME_DIR" 2>/dev/null || stat -f %Su "$HOME_DIR")"

# As the service user when root, so nothing the daemon owns gains a root-owned
# file (opening a WAL database writes its -shm), and so root never starts the
# venv's interpreter: the service user can write the venv, and a .pth file in
# it runs at interpreter start.
as_service_user() {
    if [ "$(id -u)" -eq 0 ]; then runuser -u "$SERVICE_USER" -- "$@"; else "$@"; fi
}

config_value() {
    as_service_user "$PYTHON" -I - "$CONFIG" "$1" <<'PY'
import sys, tomllib
node = tomllib.load(open(sys.argv[1], "rb"))
for part in sys.argv[2].split("."):
    node = node.get(part) if isinstance(node, dict) else None
print(node if isinstance(node, str) else "")
PY
}

DB_PATH="$(config_value db_path)"
[ -n "$DB_PATH" ] || die "the config names no db_path"
DATA_DIR="$(dirname "$DB_PATH")"
REPOS_DIR="$(config_value developer.repos_dir)"
MOUNT="$(config_value nextcloud_mount_path)"
SESSION_LOGS="$(config_value brain.native.session_log.dir)"
SESSION_LOGS="${SESSION_LOGS:-${DATA_DIR}/logs}"

cron_enabled() {
    [ -f "$CRON_FILE" ] && grep -Eq '^[[:space:]]*[^#[:space:]].*-update' "$CRON_FILE"
}

dirty_entries() {
    [ -d "$VFS_META" ] || { echo 0; return; }
    { grep -rlE '"Dirty"[[:space:]]*:[[:space:]]*true' "$VFS_META" 2>/dev/null || true; } | wc -l | tr -d ' '
}

wait_until_clean() {
    local waited=0 count
    while :; do
        count="$(dirty_entries)"
        [ "$count" -eq 0 ] && return 0
        if [ "$waited" -ge "$DIRTY_TIMEOUT" ]; then
            die "${count} rclone VFS cache entr(y/ies) under ${VFS_META} still marked Dirty after ${DIRTY_TIMEOUT}s: a write not yet uploaded exists only on this host. The units are stopped; wait and re-run, or start them again."
        fi
        log "${count} cache entr(y/ies) waiting to upload; checking again in 5s"
        sleep 5
        waited=$((waited + 5))
    done
}

# Whether systemd has the unit at all: an install that never ran WhatsApp has
# no Baileys unit, and `systemctl stop` naming one it lacks fails the stop.
unit_known() {
    [ "$(systemctl show -p LoadState --value "$1")" != "not-found" ]
}

stop_units() {
    local unit units=()
    for unit in "${NS}-scheduler" "${NS}-web" "${NS}-webhooks" "${NS}-whatsapp-baileys"; do
        if unit_known "$unit"; then units+=("$unit"); else log "no ${unit} unit; nothing to stop"; fi
    done
    [ "${#units[@]}" -gt 0 ] || die "systemd knows none of the ${NS}-* units; is --namespace right?"
    log "stopping and disabling ${units[*]}"
    systemctl stop "${units[@]}"
    # A pattern matches loaded instances only, so none running is not an error.
    systemctl stop "${NS}-devbox-proxy@*.service"
    systemctl disable "${units[@]}" 2>/dev/null || true
    for unit in "${units[@]}"; do
        if systemctl is-active --quiet "$unit"; then
            die "${unit} is still active after the stop"
        fi
    done
}

release_mount() {
    [ -n "$MOUNT" ] || return 0
    if ! mountpoint -q "$MOUNT"; then
        log "${MOUNT} is not mounted; nothing to flush"
        return 0
    fi
    unit_known mount-nextcloud || die "${MOUNT} is mounted, but not by a mount-nextcloud unit; unmount it by hand once the cache is clean, and re-run"
    wait_until_clean
    log "stopping and disabling mount-nextcloud"
    systemctl stop mount-nextcloud
    systemctl disable mount-nextcloud 2>/dev/null || true
    if mountpoint -q "$MOUNT"; then
        die "${MOUNT} is still mounted after mount-nextcloud stopped"
    fi
    # rclone may finish or abandon uploads on unmount; what is left marked
    # Dirty now never reached the server.
    local count
    count="$(dirty_entries)"
    if [ "$count" -ne 0 ]; then
        die "${count} rclone VFS cache entr(y/ies) under ${VFS_META} are still marked Dirty after the unmount: those writes never reached the server. Copy them from the cache by hand before going on (see the runbook)."
    fi
}

snapshot_databases() {
    log "snapshotting the databases"
    as_service_user "$PYTHON" - "$CONFIG" "${scratch}/db" <<'PY'
import sys
from pathlib import Path

from istota.config import load_config
from istota.maintenance import db_backup

# This runs the source's own code, which may predate record_last_run; every
# release has _write_last_run, so silencing it keeps the install's clock.
db_backup._write_last_run = lambda *args, **kwargs: None

config = load_config(Path(sys.argv[1]))
config.scheduler.db_backup_enabled = True
config.scheduler.db_backup_dir = sys.argv[2]
config.scheduler.db_backup_retention = 0
results = db_backup.backup_databases(config)
for result in results:
    print(f"[export-bare-metal]   {result['label']}: {result['status']}", file=sys.stderr)
framework = [r for r in results if r["label"] == "framework" and r["status"] == "ok"]
failed = [r for r in results if r["status"] not in ("ok", "skip_missing")]
sys.exit(0 if framework and not failed else 1)
PY
}

copy_private() {
    install -m 0600 "$1" "$2"
}

write_manifest() {
    local release
    release="$(git -C "$CHECKOUT" describe --tags --always 2>/dev/null || echo unknown)"
    {
        echo "format=1"
        echo "source_host=$(hostname -f 2>/dev/null || hostname)"
        echo "source_release=${release}"
        echo "exported_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "mode=$([ "$NO_STOP" -eq 1 ] && echo online || echo stopped)"
        echo "namespace=${NS}"
        echo "istota_home=${HOME_DIR}"
    } > "${scratch}/MANIFEST"
}

# `-C parent name` for a tree, renamed to the member name the import reads.
# Every member under it starts with its own name, so a prefix rename is exact.
tar_member() {
    local path="$1" name="$2" base
    [ -d "$path" ] || return 0
    base="$(basename "$path")"
    if [ "$base" != "$name" ]; then
        if tar --version 2>/dev/null | grep -q GNU; then
            renames+=(--transform "s,^${base},${name},")
        else
            renames+=(-s ",^${base},${name},")
        fi
    fi
    members+=(-C "$(dirname "$path")" "$base")
}

if [ "$NO_STOP" -eq 0 ]; then
    if cron_enabled; then
        die "the update cron ${CRON_FILE} is enabled: it restarts the units on any new commit. Comment its line out (or move the file away) and re-run."
    fi
    stop_units
    release_mount
else
    log "--no-stop: nothing is stopped; the export is a copy of a running bot, for a rehearsal only"
fi

umask 077
scratch_parent="${DATA_DIR}"
scratch="$(mktemp -d "${scratch_parent}/.export.XXXXXX")"
trap 'rm -rf "${scratch:-}"' EXIT
if [ "$(id -u)" -eq 0 ]; then chown "$SERVICE_USER" "$scratch"; fi
snapshot_databases || die "the database snapshot failed; nothing was written"
if [ "$(id -u)" -eq 0 ]; then chown -R root:root "$scratch"; fi

mkdir "${scratch}/config" "${scratch}/secrets"
copy_private "$CONFIG" "${scratch}/config/config.toml"
if [ -f "${SECRETS_DIR}/admins" ]; then copy_private "${SECRETS_DIR}/admins" "${scratch}/config/admins"; fi
copy_private "${SECRETS_DIR}/secrets.env" "${scratch}/secrets/secrets.env"
if [ -f "${SECRETS_DIR}/web-secrets.env" ]; then
    copy_private "${SECRETS_DIR}/web-secrets.env" "${scratch}/secrets/web-secrets.env"
fi
write_manifest

renames=()
members=(-C "$scratch" MANIFEST config secrets db)
if [ -n "$REPOS_DIR" ]; then tar_member "$REPOS_DIR" repos; fi
tar_member "${DATA_DIR}/whatsapp-baileys-session" whatsapp-baileys-session
tar_member "$SESSION_LOGS" logs
if [ "$WITH_BROWSER" -eq 1 ]; then tar_member "${DATA_DIR}/browser-profile" browser-profile; fi

if [ "$OUTPUT" = "-" ]; then
    log "writing the tarball to stdout"
    tar "${renames[@]}" -cf - "${members[@]}"
else
    log "writing ${OUTPUT}"
    tar "${renames[@]}" -cf "$OUTPUT" "${members[@]}"
    chmod 0600 "$OUTPUT"
fi
log "done; the snapshot and the secrets copies are deleted on exit"
