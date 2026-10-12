#!/bin/bash
# Re-apply the devbox egress rules in DOCKER-USER: no devbox reaches link-local
# and cloud metadata, the Azure host agent, RFC 1918 or CGNAT addresses. The
# host-loopback block and the proxied listener's rule, further down, are the
# stack's other DOCKER-USER rules, kept here so one script owns the chain.
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
# (ISTOTA_DEVBOX_SUBNET, from /srv/app/istota/host.env through the unit), and
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

# --- The host's loopback (HOST_LOOPBACK_BLOCK=1, the local VM) ---
#
# Lima forwards its host gateway (HOST_GATEWAY_ADDR) to the Mac's loopback, so
# without this every container reaches the user's local databases and admin
# consoles. Dropped for every container, except the TCP ports in
# HOST_LOOPBACK_ALLOW (a model server on the Mac) and port 53: Lima serves DNS
# at the gateway itself, and a container on the default bridge (an image
# build's RUN step) resolves through it. The VM's own traffic is not forwarded
# and is not filtered.
#
# These rules land in front of the devbox drops, so their RETURNs exclude the
# devbox subnet, which then meets the drop below or its own. One comment on
# every rule, removed as a set first. Anything this cannot read fails the unit
# after applying what it can: an unreadable allow list allows no port, and an
# unknown switch value blocks. IPv4 only.
LOOPBACK_COMMENT="istota-host-loopback: containers reach the host gateway on allowed ports only"
loopback_failed=0

iptables -w 5 -S DOCKER-USER | { grep -F -- "--comment \"${LOOPBACK_COMMENT}\"" || true; } \
    | sed 's/^-A /-D /' | while read -r rule; do
        eval "iptables -w 5 ${rule}"
    done

is_ipv4() {
    local IFS=.
    local -a octets
    [[ "$1" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    read -r -a octets <<< "$1"
    for octet in "${octets[@]}"; do
        [ "$((10#$octet))" -le 255 ] || return 1
    done
}

is_port() {
    [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 65535 ]
}

loopback_block="${HOST_LOOPBACK_BLOCK:-0}"
if [ "$loopback_block" != "0" ] && [ -n "$loopback_block" ]; then
    if [ "$loopback_block" != "1" ]; then
        echo "HOST_LOOPBACK_BLOCK is 0 or 1, not '${loopback_block}'; blocking" >&2
        loopback_failed=1
    fi
    gateway="${HOST_GATEWAY_ADDR:-}"
    if ! is_ipv4 "$gateway"; then
        echo "HOST_LOOPBACK_BLOCK needs HOST_GATEWAY_ADDR, an IPv4 address, not '${gateway}'" >&2
        loopback_failed=1
    else
        allowed=""
        for port in $(printf '%s' "${HOST_LOOPBACK_ALLOW:-}" | tr ',' ' '); do
            if ! is_port "$port"; then
                echo "HOST_LOOPBACK_ALLOW: '${port}' is not a TCP port; allowing none" >&2
                loopback_failed=1
                allowed=""
                break
            fi
            allowed="${allowed} ${port}"
        done
        loopback_rule() {
            iptables -w 5 -I DOCKER-USER 1 -d "$gateway" "$@" -m comment --comment "$LOOPBACK_COMMENT"
        }
        loopback_rule -j DROP
        for port in $allowed; do
            loopback_rule ! -s "$SUBNET" -p tcp --dport "$port" -j RETURN
        done
        loopback_rule ! -s "$SUBNET" -p udp --dport 53 -j RETURN
        loopback_rule ! -s "$SUBNET" -p tcp --dport 53 -j RETURN
    fi
fi

# --- The proxied listener (INGRESS=proxied) ---
#
# Only the upstream proxy may reach the published nginx port: with
# `trusted_proxy_hops = 2` a client that reached nginx directly could forge the
# X-Forwarded-For the hop arithmetic reads, which defeats the per-IP login
# throttle. nginx's `allow`/`deny` is the readable rule; this one stays true if
# the template is edited. In DOCKER-USER, not INPUT, because a published port
# bypasses INPUT, and matched on conntrack's original destination, because by
# FORWARD the packet has been rewritten to the container's address and port.
# Every rule carries one comment, so a changed UPSTREAM_PROXY removes the old
# set before the new one is written. IPv4 only, like the rules above.
PROXIED_COMMENT="istota-proxied: only UPSTREAM_PROXY reaches the listener"

iptables -w 5 -S DOCKER-USER | { grep -F -- "--comment \"${PROXIED_COMMENT}\"" || true; } \
    | sed 's/^-A /-D /' | while read -r rule; do
        # iptables' own output, re-read as arguments (the comment is quoted).
        eval "iptables -w 5 ${rule}"
    done

if [ "${INGRESS:-}" = "proxied" ]; then
    listen_addr="${LISTEN_ADDR:?INGRESS=proxied needs LISTEN_ADDR}"
    listen_port="${LISTEN_PORT:-8080}"
    upstreams="$(printf '%s' "${UPSTREAM_PROXY:-}" | tr ',' ' ')"
    if [ -z "${upstreams// /}" ]; then
        echo "INGRESS=proxied needs UPSTREAM_PROXY" >&2
        exit 1
    fi
    match=(-p tcp -m conntrack --ctorigdst "$listen_addr" --ctorigdstport "$listen_port" --ctdir ORIGINAL)
    iptables -w 5 -I DOCKER-USER 1 "${match[@]}" -m comment --comment "$PROXIED_COMMENT" -j DROP
    for upstream in $upstreams; do
        iptables -w 5 -I DOCKER-USER 1 -s "$upstream" "${match[@]}" \
            -m comment --comment "$PROXIED_COMMENT" -j RETURN
    done
fi

[ "$loopback_failed" = 0 ] || exit 1
