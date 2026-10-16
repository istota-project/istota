#!/bin/bash
# mac-desktop-app Stage 1 (a): generalize a provisioned VM into an appliance
# disk. Run as root in the guest (build-appliance.sh copies it in). The draft
# of what Stage 7's host/appliance-generalize.sh does, with what the spike found.
#
# The last steps cannot run inside the Lima user's own ssh session (its home is
# removed, its processes killed), so they are a transient unit that powers the
# VM off when done; limactl then sees it stopped.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
STACK=/srv/app/istota

echo "== before"
df -B1 --output=used / | tail -1

# Docker: no images (they arrive from the archive), no state tied to this host.
docker system prune -af --volumes >/dev/null
systemctl stop docker.service docker.socket containerd.service
rm -rf /var/lib/docker/network /var/lib/docker/swarm /var/lib/docker/engine-id \
    /var/lib/docker/buildkit /var/lib/docker/tmp/*

# The stack directory keeps the checkout and the layout provision.sh made; what
# names this machine or a user goes. provision.sh recreates the directories,
# and the first boot's host.env carries the pinned key.
rm -rf "${STACK}/host.env" "${STACK}/secrets" "${STACK}/config" "${STACK}/allowed_signers" \
    "${STACK}/.provisioned" "${STACK}/.lima-bootstrapped" "${STACK}/data"/* "${STACK}/requests"/*
rm -f /etc/systemd/system/istota-stack.service.d/host-folder.conf

# Lima's mounts are cloud-init's (comment=cloudconfig); the new instance writes its own.
sed -i '/comment=cloudconfig/d' /etc/fstab

# apt: the downloaded .debs go, the lists stay (measured: 160M) so an offline
# `apt-get install` of an installed package still resolves.
apt-get clean

# Identity: machine-id, ssh host keys, cloud-init's record of this instance.
rm -f /etc/ssh/ssh_host_*
cloud-init clean --logs --seed --machine-id

# Logs.
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
find /var/log -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete
find /var/log -type f -exec truncate -s 0 {} +

# The build user (the maintainer's Mac login name) and Lima's guest-side files
# for it: a new Mac's Lima creates its own user from its cidata.
# Lima's user takes the Mac's uid (501 here), so it is found by its home.
users="$(awk -F: '$6 ~ /^\/home\// { print $1 }' /etc/passwd)"
echo "removing users: ${users}"
# Its trace stays on the disk; the first boot from the appliance reads and removes it.
cat > /run/spike-finish.sh <<EOF
#!/bin/bash
exec >/var/tmp/spike-finish.log 2>&1
set -x
sleep 3
for u in ${users}; do
    pkill -KILL -u "\$u" || true
    sleep 1
    userdel -r -f "\$u" || true
    rm -f "/etc/sudoers.d/90-lima-users" "/etc/sudoers.d/\$u"
done
rm -rf /home/*.guest /home/*.linux
fstrim -av
sync
echo finish-done
sync
systemctl poweroff
EOF
chmod 0755 /run/spike-finish.sh
# /run is noexec on Debian 13: the script is bash's argument, not the command.
systemd-run --unit=spike-finish --collect /bin/bash /run/spike-finish.sh
echo "finish unit scheduled; the VM powers off when it is done"
