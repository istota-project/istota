#!/bin/sh
# Nextcloud post-installation provisioning for Istota.
# Mounted into /docker-entrypoint-hooks.d/post-installation/ in the NC container.
# Runs as www-data after first install (NC entrypoint drops privileges).
#
# This script handles occ commands only. API-based provisioning (Talk rooms,
# app passwords) runs from the istota container after NC is fully up.

set -eu

PROVISION_FLAG="/mnt/shared/.istota-provisioned"

if [ -f "$PROVISION_FLAG" ]; then
    echo "[istota-provision] Already provisioned, skipping."
    exit 0
fi

echo "[istota-provision] Starting Nextcloud provisioning..."

OCC="php /var/www/html/occ"

# --- Users ---

echo "[istota-provision] Creating bot user: ${BOT_USER}"
OC_PASS="$BOT_PASSWORD" $OCC user:add "${BOT_USER}" --password-from-env --display-name "Istota" || true

echo "[istota-provision] Creating human user: ${USER_NAME}"
DISPLAY_NAME="${USER_DISPLAY_NAME:-$USER_NAME}"
OC_PASS="$USER_PASSWORD" $OCC user:add "${USER_NAME}" --password-from-env --display-name "${DISPLAY_NAME}" || true

# --- Apps ---

echo "[istota-provision] Enabling required apps..."
$OCC app:enable spreed || true
$OCC app:enable calendar || true
$OCC app:enable files_external || true

# --- External storage ---

echo "[istota-provision] Configuring external storage..."

# files_external:create outputs "Storage created with id N"
MOUNT_ID=$($OCC files_external:create "Shared Files" local null::null \
    -c datadir=/mnt/shared 2>&1 | grep -o '[0-9]*$') || true

if [ -n "$MOUNT_ID" ] && [ "$MOUNT_ID" -gt 0 ] 2>/dev/null; then
    $OCC files_external:applicable --add-user "${BOT_USER}" "${MOUNT_ID}"
    echo "[istota-provision] External storage mount ${MOUNT_ID} created for ${BOT_USER}"
else
    echo "[istota-provision] Warning: could not create external storage mount for bot."
fi

USER_MOUNT_ID=$($OCC files_external:create "Istota" local null::null \
    -c "datadir=/mnt/shared/Users/${USER_NAME}" 2>&1 | grep -o '[0-9]*$') || true

if [ -n "$USER_MOUNT_ID" ] && [ "$USER_MOUNT_ID" -gt 0 ] 2>/dev/null; then
    $OCC files_external:applicable --add-user "${USER_NAME}" "${USER_MOUNT_ID}"
    echo "[istota-provision] External storage mount ${USER_MOUNT_ID} created for ${USER_NAME}"
fi

# --- Directory structure on shared volume ---

echo "[istota-provision] Creating directory structure..."

SHARED="/mnt/shared"
USER_BASE="${SHARED}/Users/${USER_NAME}"

mkdir -p "${USER_BASE}/istota/config"
mkdir -p "${USER_BASE}/istota/exports"
mkdir -p "${USER_BASE}/istota/examples"
mkdir -p "${USER_BASE}/inbox"
mkdir -p "${USER_BASE}/memories"
mkdir -p "${USER_BASE}/shared"
mkdir -p "${USER_BASE}/scripts"
mkdir -p "${SHARED}/Channels"

# --- Write provisioning flag ---
# API-based provisioning (Talk room, app password) happens in the istota container.

cat > "$PROVISION_FLAG" <<ENDOFFILE
# Istota provisioning results
USER_NAME=${USER_NAME}
BOT_USER=${BOT_USER}
ENDOFFILE

chmod 644 "$PROVISION_FLAG"

echo "[istota-provision] Nextcloud provisioning complete."
