#!/bin/bash
# Stage 1 spike: rootful Docker Engine, the compose plugin and Podman on a
# fresh Debian 13 Lima VM. Run as root. Lima's own rootless containerd is
# disabled first so nothing but these engines runs containers in the VM.
set -euo pipefail

if [ -n "${SUDO_USER:-}" ]; then
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$SUDO_USER")" \
        systemctl --user disable --now containerd.service buildkit.service || true
fi

apt-get update -qq
apt-get install -y -qq ca-certificates curl >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    podman >/dev/null

docker version --format 'docker engine {{.Server.Version}}'
docker compose version
podman --version
runc --version | head -1
docker info --format 'cgroup v{{.CgroupVersion}} driver={{.CgroupDriver}} secopts={{.SecurityOptions}}'
