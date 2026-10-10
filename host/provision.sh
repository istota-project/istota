#!/bin/bash
# Provision a Debian 13 machine dedicated to one istota install. Run as root.
# Idempotent: a failed run can be re-run, and a run after `istota-stack setup`
# picks up host.env (the mount, the firewall rules, the certificate units).
#
#   host/provision.sh                  # from a checkout, by hand or from Lima
#   ISTOTA_REPO_URL=... provision.sh   # cloud-init clones first, then runs this
#
# Ported from the Ansible role's host tasks (Docker Engine, the userns sysctl,
# zram, the journald cap, the auditd bound, the rclone mount unit, the devbox
# egress rules, the browser watchdog), with the role's values. It installs
# plain files from host/; the units read /srv/istota/host.env for the few
# values that differ per machine, so nothing here is a template.
#
# The machine's choices come from /srv/istota/host.env, written by
# `istota setup --vm-dir`:
#
#   STORAGE              local | nextcloud (the rclone mount)
#   INGRESS              local | proxied | direct
#   DOMAIN, TLS_CERT_SOURCE, UPSTREAM_PROXY, LISTEN_ADDR, LISTEN_PORT
#   RELEASE_SIGNING_KEY  the release key, one line of an SSH public key
#   CLAUDE_CODE_VERSION  the Claude Code pin a build uses, when it uses one
#   ACME_EMAIL, ACME_SERVER   optional, for certbot in direct mode
#   RCLONE_REMOTE        the rclone remote name in /srv/istota/rclone.conf
#   ZRAM_ENABLED         0 to leave swap alone
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
STACK="${ISTOTA_STACK_DIR:-/srv/istota}"
HOST_ENV="${STACK}/host.env"
ISTOTA_UID=10001
UNITS=/etc/systemd/system

log() { echo "[provision] $*"; }
die() { echo "[provision] $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -r /etc/debian_version ] || die "this is for Debian 13"

# host.env, if setup has written one. Values only; the file is not executed.
host_env() {
    [ -f "$HOST_ENV" ] || return 0
    awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); v = $0 } END { if (v != "") print v }' "$HOST_ENV"
}

changed_file() {
    # Write $2 to $1 when it differs; answer whether it did.
    local path="$1" content="$2"
    if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then
        return 1
    fi
    install -d "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
    return 0
}

export DEBIAN_FRONTEND=noninteractive

# --- Packages and Docker Engine ----------------------------------------------
# Docker's own apt repository, not Debian's docker.io: the compose plugin and a
# current Engine come from there, as in the role.
apt_install() { apt-get install -y -qq --no-install-recommends "$@" >/dev/null; }

apt-get update -qq
apt_install ca-certificates curl git python3 iptables apparmor apparmor-utils

if [ ! -f /etc/apt/sources.list.d/docker.sources ]; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release)
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt-get update -qq
fi
apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Lima's own rootless containerd, where this runs in a Lima VM: nothing but
# Docker Engine runs containers here.
if [ -n "${SUDO_USER:-}" ] && id "$SUDO_USER" >/dev/null 2>&1; then
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$SUDO_USER")" \
        systemctl --user disable --now containerd.service buildkit.service >/dev/null 2>&1 || true
fi

# The userland proxy off, so nginx sees the client's real address: with it on,
# some connections arrive from the bridge gateway, and the proxied listener's
# allow list then refuses the upstream or admits everything.
daemon_json_changed="$(python3 - <<'PY'
import json, os
path = "/etc/docker/daemon.json"
try:
    with open(path) as f:
        conf = json.load(f)
except FileNotFoundError:
    conf = {}
want = dict(conf, **{"userland-proxy": False})
if want != conf:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(want, f, indent=2)
        f.write("\n")
    print("1")
PY
)"
systemctl enable --now docker.service >/dev/null
if [ -n "$daemon_json_changed" ]; then
    log "docker: userland-proxy off, restarting dockerd"
    systemctl restart docker.service
fi

# --- Kernel settings ---------------------------------------------------------
# bubblewrap creates an unprivileged user namespace for every task. Debian 13's
# kernel allows it by default; the key is written so a host that disabled it is
# set back, and its absence on a kernel without the knob is not an error.
if changed_file /etc/sysctl.d/99-istota-sandbox.conf "kernel.unprivileged_userns_clone = 1"; then
    sysctl -q -p /etc/sysctl.d/99-istota-sandbox.conf 2>/dev/null || true
fi

# --- Swap: zram --------------------------------------------------------------
# Compressed swap in RAM, as the role installs it (robustness spec, Track A):
# with no swap, cold tmpfs and shmem pages cannot be evicted at all.
if [ "$(host_env ZRAM_ENABLED)" != "0" ]; then
    apt_install systemd-zram-generator
    changed_file /etc/systemd/zram-generator.conf "$(cat <<'EOF'
# Installed by istota's host/provision.sh. zram-size is the device's
# uncompressed capacity; at zstd's ~3:1 half of RAM costs about a sixth of it.
[zram0]
zram-size = ram / 2
compression-algorithm = zstd
fs-type = swap
swap-priority = 100
EOF
)" || true
    # The generator runs on daemon-reload; the swap unit it writes has no
    # [Install], so starting it is what activates it now and the generator what
    # brings it back at boot.
    systemctl daemon-reload
    systemctl start dev-zram0.swap || log "zram: dev-zram0.swap did not start; see journalctl -u dev-zram0.swap"
fi

# --- Logs: the journald cap and the auditd bound -----------------------------
if changed_file /etc/systemd/journald.conf.d/istota.conf "$(cat <<'EOF'
# Installed by istota's host/provision.sh. Without it SystemMaxUse defaults to
# 10% of the filesystem, which the journal reaches and then holds.
[Journal]
SystemMaxUse=500M
SystemMaxFileSize=50M
EOF
)"; then
    systemctl restart systemd-journald
fi

if [ -f /etc/audit/auditd.conf ]; then
    # Anchored, so a key is replaced rather than a second one appended: auditd
    # reads the first.
    for setting in "num_logs = 5" "max_log_file = 6" "max_log_file_action = ROTATE"; do
        key="${setting%% =*}"
        if grep -qE "^\s*${key}\s*=" /etc/audit/auditd.conf; then
            sed -i -E "s|^\s*${key}\s*=.*|${setting}|" /etc/audit/auditd.conf
        else
            echo "$setting" >> /etc/audit/auditd.conf
        fi
    done
    # ROTATE reclaims nothing above the new bound; remove what an older
    # keep_logs setting stranded there.
    for old in /var/log/audit/audit.log.*; do
        [ -e "$old" ] || continue
        n="${old##*.}"
        case "$n" in *[!0-9]*) continue ;; esac
        [ "$n" -ge 5 ] && rm -f "$old"
    done
    systemctl try-restart auditd.service 2>/dev/null || true
fi

# --- The daemon's uid on the host --------------------------------------------
# uid and gid 10001 own the secret files, the config directory and, in full
# Nextcloud integration, the rclone mount, so the containers' daemon reads and
# writes them as itself.
getent group "$ISTOTA_UID" >/dev/null || groupadd -g "$ISTOTA_UID" istota
getent passwd "$ISTOTA_UID" >/dev/null \
    || useradd -u "$ISTOTA_UID" -g "$ISTOTA_UID" -M -d "$STACK" -s /usr/sbin/nologin istota

# --- /srv/istota -------------------------------------------------------------
install -d -m 0755 "$STACK" "${STACK}/mount" "${STACK}/certs" "${STACK}/letsencrypt" "${STACK}/acme"
install -d -m 0750 -o "$ISTOTA_UID" -g "$ISTOTA_UID" "${STACK}/config"
install -d -m 0700 -o "$ISTOTA_UID" -g "$ISTOTA_UID" "${STACK}/secrets"
[ -f "${STACK}/.env" ] || install -m 0644 /dev/null "${STACK}/.env"

# The checkout istota-stack builds from. Cloned from the checkout this script
# runs from (or ISTOTA_REPO_URL), then pointed at ISTOTA_REPO_URL when one is
# given, so `istota-stack update` fetches release tags from there.
if [ ! -d "${STACK}/src/.git" ]; then
    source_repo="${ISTOTA_REPO_URL:-$REPO}"
    log "cloning ${source_repo} into ${STACK}/src"
    git clone --quiet --no-checkout "$source_repo" "${STACK}/src"
    git -C "${STACK}/src" checkout --quiet --detach "$(git -C "${STACK}/src" rev-parse HEAD)"
fi
if [ -n "${ISTOTA_REPO_URL:-}" ]; then
    git -C "${STACK}/src" remote set-url origin "$ISTOTA_REPO_URL"
fi

# The release key. `istota-stack update` verifies every tag against this file
# and nothing else.
signing_key="$(host_env RELEASE_SIGNING_KEY)"
if [ -n "$signing_key" ]; then
    changed_file "${STACK}/allowed_signers" "releases namespaces=\"git\" ${signing_key}" || true
    chmod 0644 "${STACK}/allowed_signers"
elif [ ! -s "${STACK}/allowed_signers" ]; then
    log "no RELEASE_SIGNING_KEY in ${HOST_ENV} yet: istota-stack update refuses every tag until there is one."
fi

# --- Commands and units ------------------------------------------------------
install -m 0755 "${HERE}/istota-stack" /usr/local/sbin/istota-stack
install -m 0755 "${HERE}/istota" /usr/local/bin/istota
install -m 0755 "${HERE}/istota-devbox-egress.sh" /usr/local/sbin/istota-devbox-egress
install -m 0755 "${HERE}/istota-browser-watchdog.sh" /usr/local/sbin/istota-browser-watchdog
install -m 0755 "${HERE}/istota-certbot" /usr/local/sbin/istota-certbot
mount_unit_changed=""
for unit in istota-stack.service istota-devbox-egress.service \
        istota-browser-watchdog.service istota-browser-watchdog.timer \
        istota-certbot.service istota-certbot.timer mount-nextcloud.service; do
    if [ "$unit" = mount-nextcloud.service ] && ! cmp -s "${HERE}/${unit}" "${UNITS}/${unit}"; then
        mount_unit_changed=1
    fi
    install -m 0644 "${HERE}/${unit}" "${UNITS}/${unit}"
done

# The AppArmor profile, loaded before the stack starts (istota-stack.service is
# ordered after apparmor.service, and `istota-stack up` reloads it from the
# deployed tag's checkout).
if [ -d /sys/kernel/security/apparmor ]; then
    profile="${STACK}/src/docker/istota/apparmor-istota"
    [ -f "$profile" ] || profile="${REPO}/docker/istota/apparmor-istota"
    install -m 0644 "$profile" /etc/apparmor.d/istota
    apparmor_parser -r /etc/apparmor.d/istota
else
    log "no AppArmor on this kernel; the stack's apparmor=istota option is ignored."
fi

# --- Full Nextcloud integration: the rclone mount ----------------------------
dropin="${UNITS}/istota-stack.service.d"
if [ "$(host_env STORAGE)" = "nextcloud" ]; then
    [ -n "$(host_env RCLONE_REMOTE)" ] \
        || die "STORAGE=nextcloud needs RCLONE_REMOTE in ${HOST_ENV}: the remote's name in ${STACK}/rclone.conf."
    apt_install rclone fuse3
    grep -qx 'user_allow_other' /etc/fuse.conf || sed -i 's/^#\s*user_allow_other$/user_allow_other/' /etc/fuse.conf
    grep -qx 'user_allow_other' /etc/fuse.conf || echo 'user_allow_other' >> /etc/fuse.conf
    if [ -f "${STACK}/rclone.conf" ]; then
        chown "$ISTOTA_UID:$ISTOTA_UID" "${STACK}/rclone.conf"
        chmod 0600 "${STACK}/rclone.conf"
    else
        log "STORAGE=nextcloud: put the rclone remote's config at ${STACK}/rclone.conf (written by you, never on a command line), then re-run this."
    fi
    chown "$ISTOTA_UID:$ISTOTA_UID" "${STACK}/mount"
    # Never write into an unmounted path: the stack requires the mount.
    changed_file "${dropin}/mount.conf" "$(cat <<'EOF'
[Unit]
Requires=mount-nextcloud.service
After=mount-nextcloud.service
EOF
)" || true
    systemctl daemon-reload
    if [ -f "${STACK}/rclone.conf" ]; then
        systemctl enable --now mount-nextcloud.service
        # A running mount keeps the flags it started with. Restarting it
        # restarts the stack too (Requires=), so only on a changed unit.
        if [ -n "$mount_unit_changed" ]; then
            log "mount-nextcloud.service changed; restarting it"
            systemctl restart mount-nextcloud.service
        fi
    fi
else
    rm -f "${dropin}/mount.conf"
    systemctl daemon-reload
    systemctl disable --now mount-nextcloud.service >/dev/null 2>&1 || true
fi

# --- Firewall: devbox egress and the proxied listener ------------------------
systemctl enable istota-devbox-egress.service >/dev/null
systemctl restart istota-devbox-egress.service

# --- Browser watchdog, when the browser profile is on ------------------------
if grep -Eq '^COMPOSE_PROFILES=.*\bbrowser\b' "${STACK}/.env"; then
    systemctl enable --now istota-browser-watchdog.timer >/dev/null
else
    systemctl disable --now istota-browser-watchdog.timer >/dev/null 2>&1 || true
fi

# --- Certificates: certbot on the host, for INGRESS=direct with acme ---------
if [ "$(host_env INGRESS)" = "direct" ] && [ "$(host_env TLS_CERT_SOURCE)" = "acme" ]; then
    apt_install certbot
    systemctl enable --now istota-certbot.timer >/dev/null
else
    systemctl disable --now istota-certbot.timer >/dev/null 2>&1 || true
fi

# --- The stack ---------------------------------------------------------------
# Enabled only once there is something to start: without a config the
# entrypoint refuses, and compose's restart policy would loop on it.
if [ -f "${STACK}/config/config.toml" ] && [ -n "$(awk -F= '$1 == "ISTOTA_TAG" { print $2 }' "${STACK}/.env")" ]; then
    systemctl enable istota-stack.service >/dev/null
    log "provisioned. The stack starts with istota-stack.service."
else
    log "provisioned. Next: istota-stack update <tag>, istota-stack setup, re-run this script,"
    log "then systemctl start istota-stack."
fi
