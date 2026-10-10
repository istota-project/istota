#!/bin/bash
# Re-apply the devbox egress rules in DOCKER-USER: no devbox reaches link-local
# and cloud metadata, the Azure host agent, RFC 1918 or CGNAT addresses.
#
# Ported from the Ansible role's istota-devbox-iptables.sh.j2, with the same
# rules and the same three properties, each found on a live host:
#
#   - Position: `-I DOCKER-USER 1`, never `-A`. A user-defined chain stops at
#     its first terminal rule, and a stock dockerd before v28 seeds DOCKER-USER
#     with `-j RETURN`, so an appended rule is listed by `iptables -S` and never
#     evaluated (ISSUE-295).
#   - Convergence: each rule is deleted wherever it sits and re-inserted at the
#     front, so a host whose rules were appended by an older script is repaired
#     rather than reported fine by a presence check.
#   - `-w 5` on every call. dockerd programs iptables at start and this unit is
#     ordered after docker.service, so the xtables lock is contended here.
#
# Plain file, not a template: the subnet comes from the environment
# (ISTOTA_DEVBOX_SUBNET, from /srv/istota/host.env through the unit), and
# defaults to `[devbox] network_subnet`'s default. The two must agree, or
# every rule here is scoped to a range no container uses.
set -euo pipefail

SUBNET="${ISTOTA_DEVBOX_SUBNET:-172.30.0.0/24}"

ensure_drop() {
    local dest="$1"
    local comment="$2"
    while iptables -w 5 -C DOCKER-USER -s "$SUBNET" -d "$dest" \
            -m comment --comment "$comment" -j DROP 2>/dev/null; do
        iptables -w 5 -D DOCKER-USER -s "$SUBNET" -d "$dest" \
            -m comment --comment "$comment" -j DROP
    done
    iptables -w 5 -I DOCKER-USER 1 -s "$SUBNET" -d "$dest" \
        -m comment --comment "$comment" -j DROP
}

# The whole link-local /16: AWS serves instance DNS at 169.254.169.253 and ECS
# task credentials at 169.254.170.2 as well as metadata at .169.254.
ensure_drop "169.254.0.0/16" "istota-devbox: block link-local (incl. cloud metadata)"
# Azure's host agent, neither link-local nor RFC 1918.
ensure_drop "168.63.129.16/32" "istota-devbox: block Azure host agent"
ensure_drop "10.0.0.0/8" "istota-devbox: block 10.0.0.0/8"
ensure_drop "172.16.0.0/12" "istota-devbox: block 172.16.0.0/12"
ensure_drop "192.168.0.0/16" "istota-devbox: block 192.168.0.0/16"
# RFC 6598 carrier-grade NAT, and the range Tailscale hands out: on a VM that
# joins a tailnet the devbox would otherwise masquerade straight into it.
ensure_drop "100.64.0.0/10" "istota-devbox: block 100.64.0.0/10"
