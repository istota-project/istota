#!/bin/bash
# local-vm-install Stage 1: what the spike VM is. Run as root in the guest.
set -uo pipefail

echo "== kernel and distro"
uname -srm
cat /etc/debian_version
echo "== engines"
docker version --format 'docker {{.Server.Version}}' 2>&1
docker compose version 2>&1
docker images --format '{{.Repository}}:{{.Tag}}' 2>&1 | head -20
echo "== workspace mount"
findmnt -T /srv/mount/host-folder -o TARGET,SOURCE,FSTYPE,OPTIONS
grep -E 'virtiofs|host-folder' /proc/self/mountinfo
echo "== tools"
for t in bwrap bindfs chronyc timedatectl; do printf '%s: ' "$t"; command -v "$t" || echo missing; done
for u in chrony systemd-timesyncd; do printf '%s: ' "$u"; systemctl is-active "$u"; done
