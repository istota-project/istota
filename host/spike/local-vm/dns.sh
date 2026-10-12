#!/bin/bash
# local-vm-install Stage 1 (c): which resolver the guest and its containers
# use, and whether 192.168.5.3 answers from the guest itself. Run as root.
set -uo pipefail
IMAGE="${1:-istota-test/lean:e74eebd3}"

query='
import socket, struct, random, sys
def dns(server):
    q = struct.pack(">HHHHHH", random.randrange(65536), 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in "debian.org".split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    try:
        s.sendto(q, (server, 53)); r = s.recv(512); return f"ok answers={struct.unpack(\">H\", r[6:8])[0]}"
    except OSError as e:
        return type(e).__name__
for server in sys.argv[1:]:
    print(f"  {server}: {dns(server)}")
'

echo "== guest /etc/resolv.conf"; grep -v '^#' /etc/resolv.conf
echo "== guest upstream (resolved)"; grep -v '^#' /run/systemd/resolve/resolv.conf 2>/dev/null
resolvectl dns 2>/dev/null
echo "== guest queries"; python3 -c "$query" 192.168.5.3 192.168.5.2
echo "== listeners on the gateway addresses, from the guest: ss to 5.2/5.3 port 53 tcp"
for a in 192.168.5.2 192.168.5.3; do
    printf '  tcp %s:53 ' "$a"; timeout 3 bash -c "echo > /dev/tcp/$a/53" 2>/dev/null && echo open || echo closed
done
echo "== default-bridge container resolv.conf"
docker run --rm --entrypoint cat "$IMAGE" /etc/resolv.conf | grep -v '^#'
echo "== default-bridge container queries"
docker run --rm --entrypoint python3 "$IMAGE" -c "$query" 192.168.5.3 192.168.5.2
echo "== user-network container resolv.conf"
docker network create spike-dns >/dev/null
docker run --rm --network spike-dns --entrypoint cat "$IMAGE" /etc/resolv.conf | grep -v '^#'
docker network rm spike-dns >/dev/null
