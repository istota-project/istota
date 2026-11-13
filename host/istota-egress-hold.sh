#!/bin/bash
# istota-egress-hold: keep a restored stack's containers off the network until
# the operator has checked it (#946).
#
#   istota-egress-hold [apply]   the table in while the state file says held,
#                                gone otherwise (istota-egress-hold.service, at boot)
#   istota-egress-hold held      exit 0 while the state file says held
#   istota-egress-hold present   exit 0 while the table is in
#
# The hold is its own nft table, with a forward-hook base chain ahead of
# iptables-nft's filter. A drop in any base chain is final, so nothing dockerd
# or istota-devbox-egress writes into DOCKER-USER can open it again, and the
# grants timer and provision.sh no longer reorder it. The unit is ordered
# before docker.service, so the table is in before dockerd starts the
# containers whose restart policy brings them back at boot.
#
# What it drops: every packet in a connection's original direction, and any
# packet conntrack has no direction for, that enters from a compose bridge
# (br-*) and leaves by anything but a bridge. That covers DNS where the VM's
# resolver is not on loopback (Docker then asks it from the container's address),
# connections opened before the hold went in, and whatever subnets the
# stack's networks have. Replies to inbound clients pass, traffic between
# the stack's own networks passes, and docker0 stays open for image builds.
# `inet` covers IPv6 too. Processes on the host itself (the rclone mount,
# certbot) are not held.
#
# The state is ${STACK}/.egress-hold, not host.env: the management role writes
# host.env key by key, update and setup copy it into the workspace's backups,
# and a restore installs the backup's over the live one. Any content other
# than empty or 0 means held, as with HOST_LOOPBACK_BLOCK. Only
# `istota-stack hold on|off` and `restore --hold` write it.
set -euo pipefail

STACK="${ISTOTA_STACK_DIR:-/srv/app/istota}"
HOLD_FILE="${STACK}/.egress-hold"
# An identifier: istota-stack, doctor's remedy and the runbook name it.
TABLE=istota_egress_hold

log() { echo "[istota-egress-hold] $*"; }

held() {
    local value
    [ -e "$HOLD_FILE" ] || [ -L "$HOLD_FILE" ] || return 1
    # A file that exists and cannot be read is a hold.
    value="$(tr -d '[:space:]' < "$HOLD_FILE" 2>/dev/null)" || return 0
    [ -n "$value" ] && [ "$value" != 0 ]
}

present() {
    command -v nft >/dev/null 2>&1 && nft list table inet "$TABLE" >/dev/null 2>&1
}

# One transaction: the table is declared, deleted and written again, so a
# re-run replaces the rules with no moment in which none are in.
install_table() {
    nft -f - <<EOF
table inet ${TABLE}
delete table inet ${TABLE}
table inet ${TABLE} {
    chain forward {
        type filter hook forward priority filter - 10; policy accept;
        iifname "br-*" oifname != "br-*" ct state invalid,untracked counter drop comment "istota-egress-hold: untracked"
        iifname "br-*" oifname != "br-*" ct direction original counter drop comment "istota-egress-hold: outbound"
    }
}
EOF
}

apply() {
    if held; then
        command -v nft >/dev/null 2>&1 \
            || { log "the stack is held (${HOLD_FILE}) but there is no nft to hold it with" >&2; exit 1; }
        install_table || { log "the stack is held (${HOLD_FILE}) but nft refused the table" >&2; exit 1; }
        log "held: the stack's containers reach nothing outside their own networks"
        return 0
    fi
    if present; then
        nft delete table inet "$TABLE" \
            || { log "could not remove the hold's table (nft delete table inet ${TABLE})" >&2; exit 1; }
        log "the hold is lifted"
    fi
}

case "${1:-apply}" in
    apply) apply ;;
    held) held ;;
    present) present ;;
    *) echo "usage: istota-egress-hold [apply|held|present]" >&2; exit 64 ;;
esac
