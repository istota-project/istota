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

# --- OAuth2 client for the web UI ---
# Stock NC's oauth2 app exposes only `ImportLegacyOcClient` via occ — there is
# no `oauth2:add-client` / `:list-clients` / `:remove-client`. Admin UI is the
# only documented path (Settings → Security → OAuth 2.0 clients).
#
# For unattended provisioning we replicate what SettingsController::addClient()
# does: generate plaintext client_id and secret via NC's SecureRandom, hash the
# secret with NC's ICrypto::calculateHMAC(), and INSERT into oauth2_clients via
# the QueryBuilder. Using NC's own crypto API means the resulting row is
# byte-identical to one created from the admin UI — no schema/encryption drift
# across NC versions.

OAUTH_CLIENT_NAME="istota-web"
OAUTH_REDIRECT_URI="${ISTOTA_WEB_CALLBACK_URL:-http://localhost:8766/istota/callback}"
OAUTH_CLIENT_ID=""
OAUTH_CLIENT_SECRET=""

echo "[istota-provision] Registering OAuth2 client '${OAUTH_CLIENT_NAME}' -> ${OAUTH_REDIRECT_URI}"

OAUTH_OUT=$(OAUTH_NAME="$OAUTH_CLIENT_NAME" OAUTH_REDIRECT="$OAUTH_REDIRECT_URI" \
    php <<'PHP' 2>&1 || true
<?php
$name = getenv('OAUTH_NAME');
$redirect = getenv('OAUTH_REDIRECT');
$_SERVER['HTTP_HOST'] = 'localhost';
require '/var/www/html/lib/base.php';
\OC_App::loadApp('oauth2');
$crypto = \OC::$server->get(\OCP\Security\ICrypto::class);
$random = \OC::$server->get(\OCP\Security\ISecureRandom::class);
$db = \OC::$server->get(\OCP\IDBConnection::class);

// Idempotency: any existing row with the same name is replaced. NC stores the
// secret hashed, so we cannot recover the plaintext for an existing row —
// delete and recreate. Reached only when /mnt/shared/.istota-provisioned is
// missing, so this is not per-boot churn.
$qb = $db->getQueryBuilder();
$rows = $qb->select('id')->from('oauth2_clients')
    ->where($qb->expr()->eq('name', $qb->createNamedParameter($name)))
    ->executeQuery()->fetchAll();
foreach ($rows as $row) {
    $qb2 = $db->getQueryBuilder();
    $qb2->delete('oauth2_clients')
        ->where($qb2->expr()->eq('id', $qb2->createNamedParameter((int)$row['id'])))
        ->executeStatement();
    fwrite(STDERR, "[oauth2] Deleted stale client id=" . $row['id'] . "\n");
}

// Match SettingsController::addClient() exactly: 64 chars from the same alphabet,
// HMAC-hashed secret stored hex-encoded.
$validChars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
$plain = $random->generate(64, $validChars);
$hash = bin2hex($crypto->calculateHMAC($plain));
$cid = $random->generate(64, $validChars);

$qb = $db->getQueryBuilder();
$qb->insert('oauth2_clients')->values([
    'name' => $qb->createNamedParameter($name),
    'redirect_uri' => $qb->createNamedParameter($redirect),
    'client_identifier' => $qb->createNamedParameter($cid),
    'secret' => $qb->createNamedParameter($hash),
])->executeStatement();

echo "OAUTH_CLIENT_ID=" . $cid . "\n";
echo "OAUTH_CLIENT_SECRET=" . $plain . "\n";
PHP
)

OAUTH_CLIENT_ID=$(printf '%s\n' "$OAUTH_OUT" | sed -n 's/^OAUTH_CLIENT_ID=\(.*\)$/\1/p' | head -1)
OAUTH_CLIENT_SECRET=$(printf '%s\n' "$OAUTH_OUT" | sed -n 's/^OAUTH_CLIENT_SECRET=\(.*\)$/\1/p' | head -1)

if [ -n "$OAUTH_CLIENT_ID" ] && [ -n "$OAUTH_CLIENT_SECRET" ]; then
    echo "[istota-provision] OAuth2 client created (id=${OAUTH_CLIENT_ID})."
else
    echo "[istota-provision] Warning: OAuth2 client registration failed. Web UI auth will be unavailable."
    echo "[istota-provision] PHP output was:"
    printf '%s\n' "$OAUTH_OUT" | sed 's/^/[istota-provision]   /'
    OAUTH_CLIENT_ID=""
    OAUTH_CLIENT_SECRET=""
fi

# --- Write provisioning flag ---
# API-based provisioning (Talk room, app password) happens in the istota container.

cat > "$PROVISION_FLAG" <<ENDOFFILE
# Istota provisioning results
USER_NAME=${USER_NAME}
BOT_USER=${BOT_USER}
OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}
OAUTH_CLIENT_SECRET=${OAUTH_CLIENT_SECRET}
OAUTH_REDIRECT_URI=${OAUTH_REDIRECT_URI}
ENDOFFILE

chmod 644 "$PROVISION_FLAG"

echo "[istota-provision] Nextcloud provisioning complete."
