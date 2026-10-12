#!/bin/bash
# Provision a Debian 13 machine dedicated to one istota install. Run as root.
# Idempotent: a failed run can be re-run, and a run after `istota-stack setup`
# picks up host.env (the mount, the firewall rules, the certificate units).
#
#   host/provision.sh                  # from a checkout, by hand or from Lima
#   ISTOTA_REPO_URL=... provision.sh   # cloud-init clones first, then runs this
#   host/provision.sh --digest         # what a run would act on; changes nothing
#
# Ported from the Ansible role's host tasks (Docker Engine, the userns sysctl,
# zram, the journald cap, the auditd bound, the rclone mount unit, the devbox
# egress rules, the browser watchdog), with the role's values. It installs
# plain files from host/; the units read /srv/app/istota/host.env for the few
# values that differ per machine, so nothing here is a template.
#
# The machine's choices come from /srv/app/istota/host.env, written by
# `istota setup --vm-dir`:
#
#   STORAGE              local | nextcloud (the rclone mount)
#   INGRESS              local | proxied | direct
#   DOMAIN, TLS_CERT_SOURCE, UPSTREAM_PROXY, LISTEN_ADDR, LISTEN_PORT
#   RELEASE_SIGNING_KEY  the release key, one line of an SSH public key
#   CLAUDE_CODE_VERSION  the Claude Code pin a build uses, when it uses one
#   ACME_EMAIL, ACME_SERVER   optional, for certbot in direct mode
#   RCLONE_REMOTE        the rclone remote name in /srv/app/istota/rclone.conf
#   ZRAM_ENABLED         0 to leave swap alone
set -euo pipefail
# The checkout it clones is what the images are built from: under a hardened
# umask (027) it would be root-only, and out of uid 10001's reach in them.
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
STACK="${ISTOTA_STACK_DIR:-/srv/app/istota}"
# The Nextcloud mount, outside the stack directory at the path the bare-metal
# installs use. mount-nextcloud.service names it too; the override is the tests'.
MOUNT_DIR="${ISTOTA_MOUNT_DIR:-/srv/mount/nextcloud/content}"
HOST_ENV="${STACK}/host.env"
ISTOTA_UID=10001
UNITS=/etc/systemd/system

log() { echo "[provision] $*"; }
die() { echo "[provision] $*" >&2; exit 1; }

# What this run acts on: host.env, rclone.conf, .env's COMPOSE_PROFILES (the
# browser watchdog) and the checkout's commit (the release's host/ files). A
# successful run records it in .provisioned, so the management role can tell
# a run a failed play never finished from one already done. `--digest` prints
# it and changes nothing.
inputs_digest() {
    {
        printf 'host.env\n'; cat "$HOST_ENV" 2>/dev/null || true
        printf '\0rclone.conf\n'; cat "${STACK}/rclone.conf" 2>/dev/null || true
        printf '\0profiles\n'; grep -E '^COMPOSE_PROFILES=' "${STACK}/.env" 2>/dev/null || true
        printf '\0release\n'; cat "${STACK}/src/.git/HEAD" 2>/dev/null || true
    } | sha256sum | cut -d' ' -f1
}

if [ "${1:-}" = "--digest" ]; then
    inputs_digest
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "run as root"
digest="$(inputs_digest)"
[ -r /etc/debian_version ] || die "this is for Debian 13"
# A run that dies partway may already have installed this checkout's host files.
# With the old record left in place, a rollback to the inputs it names would
# read as provisioned and keep them (ISSUE-710).
rm -f "${STACK}/.provisioned"

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

# An install the old Ansible role converged has its own mount-nextcloud.service,
# at this path, run as its service user. While that unit is enabled or running
# the install is live: replacing, stopping or chowning it pulls the workspace
# from under its daemon. The export disables it; the run after that takes over.
bare_metal_mount_live() {
    local unit="${UNITS}/mount-nextcloud.service"
    [ -f "$unit" ] || return 1
    grep -qx "User=${ISTOTA_UID}" "$unit" && return 1
    systemctl is-enabled --quiet mount-nextcloud.service 2>/dev/null \
        || systemctl is-active --quiet mount-nextcloud.service 2>/dev/null
}

# The certificate a reverse proxy pins (INGRESS=proxied, TLS_CERT_SOURCE=
# self-signed): made once and never replaced, since a new one breaks the pin.
# Ten years, for the same reason; the proxy trusts this one certificate, not a
# chain, so its lifetime is the only expiry there is.
ensure_self_signed_cert() {
    local dir="$1" domain="$2" addr="$3" names fingerprint
    if [ ! -s "${dir}/fullchain.pem" ] || [ ! -s "${dir}/privkey.pem" ]; then
        names="DNS:${domain}"
        [ -n "$addr" ] && names="${names},IP:${addr}"
        install -d -m 0755 "$dir"
        (umask 077; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
            -days 3650 -subj "/CN=${domain}" -addext "subjectAltName=${names}" \
            -keyout "${dir}/privkey.pem" -out "${dir}/fullchain.pem" 2>/dev/null)
        chmod 0600 "${dir}/privkey.pem"
        chmod 0644 "${dir}/fullchain.pem"
        log "made a self-signed certificate for ${names} in ${dir}"
    fi
    fingerprint="$(openssl x509 -in "${dir}/fullchain.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
    log "the reverse proxy pins ${dir}/fullchain.pem (SHA-256 ${fingerprint}): copy that file to it,"
    log "  forward to https://, and verify against it, e.g. in nginx: proxy_ssl_verify on;"
    log "  proxy_ssl_trusted_certificate <the copy>; proxy_ssl_name ${domain}; (docs/deployment/docker.md)"
}

export DEBIAN_FRONTEND=noninteractive

# --- Packages and Docker Engine ----------------------------------------------
# Docker's own apt repository, not Debian's docker.io: the compose plugin and a
# current Engine come from there, as in the role.
apt_install() { apt-get install -y -qq --no-install-recommends "$@" >/dev/null; }

# rclone for the Nextcloud mount: rclone.org's own .deb, pinned by version and
# per-arch SHA-256 (from the release's signed SHA256SUMS), never Debian's
# package (#694). Debian builds rclone against its own 2016 bazil.org/fuse,
# which answers any plain errno with EIO, so every xattr call on a workspace
# file fails and shutil.copy2 out of the mount breaks. rclone.org's builds use
# the fuse rclone pins, which passes the errno through. Held, so apt never
# swaps Debian's build back in.
RCLONE_VERSION=1.75.2
RCLONE_SHA256_AMD64=efbfe852181f7191eb3c9043ed1ab49c9b2d0ba045c61c926a5da111c939ec5c
RCLONE_SHA256_ARM64=b8ba161d5c837206fad79ff1aabf0a81aa02a5fb164cdbc0ff5db4160aecd278
# The package's binary, which the mount unit runs, asked by path: a host moved
# from the Ansible role keeps that role's rclone in /usr/local/bin, ahead on PATH,
# and asking that one would reinstall the package on every run.
RCLONE_BIN=/usr/bin/rclone
install_rclone() {
    local have arch sha tmp
    have="$("$RCLONE_BIN" version 2>/dev/null || true)"
    if [ "${have%%$'\n'*}" = "rclone v${RCLONE_VERSION}" ]; then
        # A run killed between the install and the hold left it unheld.
        apt-mark hold rclone >/dev/null
        return 0
    fi
    arch="$(dpkg --print-architecture)"
    case "$arch" in
        amd64) sha="$RCLONE_SHA256_AMD64" ;;
        arm64) sha="$RCLONE_SHA256_ARM64" ;;
        *) die "no pinned rclone checksum for ${arch}" ;;
    esac
    tmp="$(mktemp -d)"
    curl -fsSL -o "${tmp}/rclone.deb" \
        "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${arch}.deb"
    if ! echo "${sha}  ${tmp}/rclone.deb" | sha256sum -c - >/dev/null 2>&1; then
        rm -rf "$tmp"
        die "rclone ${RCLONE_VERSION} for ${arch} does not match its pinned SHA-256"
    fi
    dpkg -i "${tmp}/rclone.deb" >/dev/null
    rm -rf "$tmp"
    apt-mark hold rclone >/dev/null
    log "installed rclone ${RCLONE_VERSION} from rclone.org (was: ${have%%$'\n'*})"
}

# Whether the running mount's process is an rclone other than the one on disk:
# dpkg replaced it under the mount, in this run or one that died before the
# restart. Read off the process, so a re-run still sees it.
mount_runs_stale_rclone() {
    local pid exe
    pid="$(systemctl show -p MainPID --value mount-nextcloud.service 2>/dev/null || true)"
    [ -n "$pid" ] && [ "$pid" != 0 ] || return 1
    exe="$(readlink "/proc/${pid}/exe" 2>/dev/null)" || return 1
    [ "$exe" != "$RCLONE_BIN" ]
}

apt-get update -qq
apt_install ca-certificates curl git python3 iptables apparmor apparmor-utils openssl

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

bare_metal_mount=""
bare_metal_mount_live && bare_metal_mount=1
[ -z "$bare_metal_mount" ] \
    || log "a bare-metal install's mount-nextcloud.service is live: left alone until the export disables it, then re-run this."

# --- /srv/app/istota ---------------------------------------------------------
install -d -m 0755 "$STACK" "${STACK}/certs" "${STACK}/letsencrypt" "${STACK}/acme"
[ -n "$bare_metal_mount" ] || install -d -m 0755 "$MOUNT_DIR"
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
    log "no RELEASE_SIGNING_KEY in ${HOST_ENV} yet: istota-stack update refuses every tag until there is one, except with --unsigned."
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
    if [ "$unit" = mount-nextcloud.service ] && [ -n "$bare_metal_mount" ]; then
        continue
    fi
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
    apt_install fuse3
    install_rclone
    grep -qx 'user_allow_other' /etc/fuse.conf || sed -i 's/^#\s*user_allow_other$/user_allow_other/' /etc/fuse.conf
    grep -qx 'user_allow_other' /etc/fuse.conf || echo 'user_allow_other' >> /etc/fuse.conf
    if [ -f "${STACK}/rclone.conf" ]; then
        chown "$ISTOTA_UID:$ISTOTA_UID" "${STACK}/rclone.conf"
        chmod 0600 "${STACK}/rclone.conf"
    else
        log "STORAGE=nextcloud: put the rclone remote's config at ${STACK}/rclone.conf (written by you, never on a command line), then re-run this."
    fi
    # Never write into an unmounted path: the stack requires the mount.
    changed_file "${dropin}/mount.conf" "$(cat <<'EOF'
[Unit]
Requires=mount-nextcloud.service
After=mount-nextcloud.service
EOF
)" || true
    systemctl daemon-reload
    if [ -n "$bare_metal_mount" ]; then
        : # left alone, logged above
    elif [ -f "${STACK}/rclone.conf" ]; then
        chown "$ISTOTA_UID:$ISTOTA_UID" "$MOUNT_DIR"
        # The role left the parent 0770 to its own group; fusermount refuses a
        # mountpoint the mount's user cannot reach.
        chmod o+x "$(dirname "$MOUNT_DIR")"
        systemctl enable --now mount-nextcloud.service
        # A running mount keeps the flags and the binary it started with.
        # Restarting it restarts the stack too (Requires=), so only on a
        # changed unit or a replaced rclone.
        if [ -n "$mount_unit_changed" ] || mount_runs_stale_rclone; then
            log "mount-nextcloud.service or rclone changed; restarting the mount"
            systemctl restart mount-nextcloud.service
        fi
    fi
else
    rm -f "${dropin}/mount.conf"
    systemctl daemon-reload
    [ -n "$bare_metal_mount" ] || systemctl disable --now mount-nextcloud.service >/dev/null 2>&1 || true
fi

if [ "$(host_env INGRESS)" = "proxied" ] && [ "$(host_env TLS_CERT_SOURCE)" = "self-signed" ]; then
    ensure_self_signed_cert "${STACK}/certs" "$(host_env DOMAIN)" "$(host_env LISTEN_ADDR)"
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

# Last, so a run that failed anywhere above leaves no record and the next
# update, rollback or play provisions again.
printf '%s\n' "$digest" > "${STACK}/.provisioned.tmp"
mv "${STACK}/.provisioned.tmp" "${STACK}/.provisioned"
