#!/bin/bash
# mac-desktop-app Stage 1 (b): a second VM from the appliance disk, through a
# local image with its digest, with the VM's outbound traffic blocked.
#
#   boot-appliance.sh NAME OFFLINE OFFLINE_RULE [IMAGE]
#     OFFLINE=1        REJECT every new outbound connection before provision
#     OFFLINE_RULE=1   skip `apt-get update`, as Stage 2's rule would
#     IMAGE            default: the .raw.zst build-appliance.sh made
#   LIMACTL=/path/to/limactl overrides the limactl used (Stage 1 (d)).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

name="$1" offline="$2" rule="$3"
image="${4:-${ARTIFACTS}/istota-appliance-${TAG}.raw.zst}"
LIMACTL="${LIMACTL:-limactl}"
digest="sha256:$(shasum -a 256 "$image" | cut -d' ' -f1)"
yaml="${ARTIFACTS}/${name}.yaml"
sed -e "s#@IMAGE@#${image}#" -e "s#@DIGEST@#${digest}#" "${HERE}/appliance.yaml.in" > "$yaml"
key="$(cat "${REPO}/host/release-signing-key.pub")"

t0="$(now)"
"$LIMACTL" create --tty=false --name="$name" \
    --param RELEASE_SIGNING_KEY="$key" --param WORKSPACE="$SPIKE_WS" \
    --param OFFLINE="$offline" --param OFFLINE_RULE="$rule" "$yaml"
echo "[host] create: $(elapsed "$t0") s"
t1="$(now)"
rc=0
"$LIMACTL" start --tty=false --timeout 15m "$name" || rc=$?
echo "[host] start: exit ${rc}, $(elapsed "$t1") s; create+start $(elapsed "$t0") s"
"$LIMACTL" shell --workdir / "$name" sudo bash -c '
    cat /var/tmp/spike-provision.log
    echo "== calls"; cat /var/tmp/spike-calls.log
    echo "== dropped outbound"; journalctl -k --no-pager | grep -c spike-out-drop || true
    journalctl -k --no-pager | grep spike-out-drop | sed -E "s/.*(DST=[^ ]+).*(PROTO=[^ ]+).*(DPT=[^ ]+).*/\1 \2 \3/" | sort | uniq -c | head
    echo "== systemd-analyze"; systemd-analyze 2>/dev/null | head -2
    echo "== docker"; systemctl is-active docker; docker info --format "{{.ServerVersion}} images={{.Images}}"
    echo "== disk"; df -h / | tail -1'
