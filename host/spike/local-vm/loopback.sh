#!/bin/bash
# local-vm-install Stage 1 (c): does container traffic to the Lima host
# gateway pass DOCKER-USER, does a drop there block it, and what happens to
# DNS? Run as root in the guest while the Mac listens on 127.0.0.1:BLOCKED and
# 127.0.0.1:ALLOWED. Leaves DOCKER-USER as it found it.
#   loopback.sh <blocked-port> <allowed-port> [image]
set -uo pipefail
BLOCKED="$1"
ALLOWED="$2"
IMAGE="${3:-istota-test/lean:e74eebd3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
GW=192.168.5.2

probe() {
    local label="$1"; shift
    docker run --rm "$@" -v "$HERE:/probe:ro" --entrypoint python3 "$IMAGE" \
        /probe/netprobe.py "$label" "$GW" "$BLOCKED" "$ALLOWED" 192.168.5.2 192.168.5.3
}
probe_all() {
    probe default-bridge --network bridge
    probe user-network --network spike-net
    probe host-network --network host
    echo "guest itself: $(python3 "$HERE/netprobe.py" guest "$GW" "$BLOCKED" "$ALLOWED" 192.168.5.2 192.168.5.3)"
}
counters() { iptables -w 5 -L DOCKER-USER -v -n -x | grep -F 'spike-loopback' || true; }
clear_rules() {
    iptables -w 5 -S DOCKER-USER | { grep -F 'spike-loopback' || true; } | sed 's/^-A /-D /' | while read -r rule; do
        eval "iptables -w 5 ${rule}"
    done
}
rule() { iptables -w 5 -I DOCKER-USER 1 -d "$GW" "$@"; }

echo "== iptables: $(iptables --version)"
echo "== guest route to the gateway: $(ip route get $GW | head -1)"
echo "== DOCKER-USER before"; iptables -w 5 -S DOCKER-USER
docker network create spike-net >/dev/null 2>&1 || true
clear_rules

echo; echo "== 1. no rule"
probe_all

echo; echo "== 2. a counting rule (RETURN, no verdict)"
rule -m comment --comment "spike-loopback: count" -j RETURN
probe user-network --network spike-net
counters
clear_rules

echo; echo "== 3. drop to $GW, the allowed port returned first"
rule -m comment --comment "spike-loopback: drop" -j DROP
rule -p tcp --dport "$ALLOWED" -m comment --comment "spike-loopback: allow port" -j RETURN
probe_all
counters
clear_rules

echo; echo "== 4. as 3, plus udp and tcp 53 to $GW returned"
rule -m comment --comment "spike-loopback: drop" -j DROP
rule -p tcp --dport "$ALLOWED" -m comment --comment "spike-loopback: allow port" -j RETURN
rule -p udp --dport 53 -m comment --comment "spike-loopback: dns udp" -j RETURN
rule -p tcp --dport 53 -m comment --comment "spike-loopback: dns tcp" -j RETURN
probe_all
counters

echo; echo "== 5. as 4, an image build's RUN step"
printf 'FROM %s\nRUN python3 /dev/stdin <<EOF\nimport socket\nprint("build-dns", socket.gethostbyname("debian.org"))\nEOF\n' "$IMAGE" \
    | docker build --no-cache --progress=plain -t spike-build-dns - 2>&1 | grep -E 'build-dns|ERROR|gaierror' | head -5
docker rmi spike-build-dns >/dev/null 2>&1 || true

clear_rules
docker network rm spike-net >/dev/null
echo; echo "== DOCKER-USER after"; iptables -w 5 -S DOCKER-USER
