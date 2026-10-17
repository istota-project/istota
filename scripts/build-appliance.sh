#!/bin/bash
# Build the release directory the Mac app ships for one signed tag.
#
#   scripts/build-appliance.sh --tag vX.Y.Z --key ~/.ssh/release_key
#       [--repo PATH] [--out DIR] [--cpus N] [--memory GiB] [--disk GiB]
#       [--apt-snapshot YYYYMMDDTHHMMSSZ | --no-apt-snapshot] [--keep-vm]
#
# Writes <out>/<tag>/ (default desktop/build/release/<tag>/, gitignored):
#
#   istota-appliance-<tag>.raw.zst   the appliance disk (the Mac app spec, "The appliance disk")
#   images-<tag>.tar                 docker save of istota (no Claude Code), istota-browser-base
#                                    (no Chrome), istota-whatsapp-baileys and nginx, by tag
#   images-<tag>.json, .json.sig     the images manifest, signed by scripts/sign-images-manifest.sh
#   istota-<tag>.bundle              the tag's full history, for `update --bundle`
#   packages-<tag>.txt               every package in the disk and the images, with licences
#
# The disk is the release's own code path, not a second one: a VM made in a
# LIMA_HOME of its own from the tag's host/lima/istota.yaml in release mode,
# cloned from the bundle and verified against the key, provisioned by the tag's
# provision.sh with the host.env the Mac CLI writes for an appliance VM, and
# generalized by the tag's host/appliance-generalize.sh. Lima's user in that VM
# is `build`, so the disk never holds the builder's login name or Mac uid.
#
# Checks that fail the build (scripts/release-artifacts.py): no Claude Code or
# Google Chrome in the disk or the images (row M7), no licence that forbids
# redistribution, and neither the builder's login name nor home path anywhere
# in the disk's data. Both are read at runtime, never written down here.
#
# --key is the private half of the release key: it signs the manifest, and its
# public half is the key the VM verifies the tag with. On the maintainer's Mac
# that is the tag-signing key (scripts/release-mac.sh, Stage 10 of the spec);
# the vm tier passes a throwaway one. Needs an Apple silicon Mac, limactl 2.x,
# zstd, ssh-keygen, git and python3. ISTOTA_BUILD_LIMA_HOME names the LIMA_HOME
# (default ~/.istota-build-lima; Lima's sockets cap its length, and the
# default ~/.lima is refused).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT="$(cd "${HERE}/.." && pwd)"
VM=istota-appliance-build
BUNDLE_DIR=/srv/mount/host-folder

tag="" key="" repo="$CHECKOUT" out="${CHECKOUT}/desktop/build/release"
cpus=6 memory=8 disk=40 keep_vm="" snapshot="$(date -u +%Y%m%dT%H%M%SZ)"

die() { echo "build-appliance: $*" >&2; exit 1; }
usage() { echo "build-appliance: $*" >&2; exit 64; }
log() { echo "[build-appliance] $*"; }
now() { date +%s; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --tag|--key|--repo|--out|--cpus|--memory|--disk|--apt-snapshot)
            [ "$#" -ge 2 ] || usage "$1 needs a value"
            case "$1" in
                --tag) tag="$2" ;;
                --key) key="$2" ;;
                --repo) repo="$2" ;;
                --out) out="$2" ;;
                --cpus) cpus="$2" ;;
                --memory) memory="$2" ;;
                --disk) disk="$2" ;;
                --apt-snapshot) snapshot="$2" ;;
            esac
            shift ;;
        --no-apt-snapshot) snapshot="" ;;
        --keep-vm) keep_vm=1 ;;
        *) usage "unknown argument: $1" ;;
    esac
    shift
done
[ -n "$tag" ] && [ -n "$key" ] || usage "--tag and --key are required"
case "$tag" in
    ""|-*|*[!A-Za-z0-9._-]*|*..*) usage "'${tag}' is not a release tag name" ;;
esac
if [ -n "$snapshot" ] && ! [[ "$snapshot" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    usage "--apt-snapshot takes a snapshot.debian.org id, YYYYMMDDTHHMMSSZ"
fi
export LIMA_HOME="${ISTOTA_BUILD_LIMA_HOME:-${HOME}/.istota-build-lima}"
case "$LIMA_HOME" in
    "${HOME}/.lima"|"${HOME}/.lima/"*) die "LIMA_HOME is the default ~/.lima, where the user's own VMs live; set ISTOTA_BUILD_LIMA_HOME" ;;
esac
for tool in ssh-keygen git python3; do
    command -v "$tool" >/dev/null 2>&1 || die "${tool} is not installed"
done
[ -f "$key" ] || die "${key} is not a key file"
mkdir -p "$out"
out="$(cd "$out" && pwd -P)"
release="${out}/${tag}"
work="$(dirname "$out")/work/${tag}"
[ ! -e "$release" ] || die "${release} exists; remove it to build ${tag} again"

# The public half, and the tag verified against it before anything is built.
pub="$(ssh-keygen -y -f "$key" | awk '{ print $1 " " $2 }')"
[ -n "$pub" ] || die "no public key in ${key}"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/build-appliance.XXXXXX")"
printf 'releases namespaces="git" %s\n' "$pub" > "${scratch}/allowed_signers"
tag_git() {
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -C "$repo" \
        -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=${scratch}/allowed_signers" \
        -c gpg.openpgp.program=false -c gpg.x509.program=false "$@"
}
[ "$(tag_git cat-file -t "refs/tags/${tag}" 2>/dev/null)" = tag ] || die "${tag} is not an annotated tag in ${repo}"
tag_git verify-tag "refs/tags/${tag}" >/dev/null 2>&1 || die "${tag} is not signed by the key in ${key}"
tag_git show "${tag}:host/lima/istota.yaml" > "${scratch}/istota.yaml" \
    || die "${tag} has no host/lima/istota.yaml"
tag_git show "${tag}:host/appliance-generalize.sh" > "${scratch}/appliance-generalize.sh" \
    || die "${tag} has no host/appliance-generalize.sh; it predates the appliance"

# Everything above runs anywhere; from here it is a Mac's Lima.
[ "$(uname -s) $(uname -m)" = "Darwin arm64" ] || die "the appliance is built on an Apple silicon Mac"
for tool in limactl zstd shasum; do
    command -v "$tool" >/dev/null 2>&1 || die "${tool} is not installed"
done
mkdir -p "$LIMA_HOME"

cleanup() {
    rm -rf "$scratch"
    if [ -z "$keep_vm" ]; then limactl delete --force "$VM" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

started="$(now)"
declare -a timings=()
step() { timings+=("$1 $(( $(now) - $2 ))s"); }

rm -rf "$work"
mkdir -p "$work"
t="$(now)"
log "bundling ${tag} with its history"
tag_git bundle create --quiet "${work}/istota-${tag}.bundle" "refs/tags/${tag}"
step bundle "$t"

# The host.env the Mac CLI writes for an appliance VM, before the template's
# provision reads it, so the disk holds what that provision installs; and the
# snapshot the packages come from. Generalize removes both.
preseed="#!/bin/bash
set -eu
install -d -m 0755 /srv/app/istota
[ -e /srv/app/istota/host.env ] || cat > /srv/app/istota/host.env <<'EOF'
INGRESS=local
STORAGE=host-folder
HOST_LOOPBACK_BLOCK=1
HOST_GATEWAY_ADDR=192.168.5.2
ISTOTA_DESKTOP_APP=1
APT_SNAPSHOT=${snapshot}
EOF
"
expression="$(python3 -I - "$preseed" <<'PY'
import json, sys
preseed = json.dumps({"mode": "system", "script": sys.argv[1]})
print(
    'del(.mounts[] | select(.mountPoint == "/mnt/istota-repo" or .mountPoint == "/mnt/istota-release"))'
    ' | .portForwards = []'
    ' | .user = {"name": "build", "comment": "Istota appliance build", "uid": 1000}'
    f' | .provision = [{preseed}] + .provision'
)
PY
)"

limactl delete --force "$VM" >/dev/null 2>&1 || true
t="$(now)"
log "creating the build VM in ${LIMA_HOME} (${cpus} cpus, ${memory} GiB, ${disk} GiB)"
limactl create --tty=false --name="$VM" \
    --param ISTOTA_TAG="$tag" --param RELEASE_SIGNING_KEY="$pub" \
    --param ISTOTA_REPO_URL="${BUNDLE_DIR}/istota-${tag}.bundle" \
    --param WORKSPACE="$work" \
    --set "$expression" \
    --cpus "$cpus" --memory "$memory" --disk "$disk" \
    "${scratch}/istota.yaml"
limactl start --tty=false --timeout 40m "$VM"
vm() { limactl shell --workdir / "$VM" sudo "$@"; }
vm test -e /srv/app/istota/.lima-bootstrapped || die "the template's provision did not finish in ${VM}"
step provision "$t"

# The images, built in the VM from its verified checkout as `istota-stack`
# builds them, without Claude Code, and the browser without Chrome.
t="$(now)"
log "building the images"
vm bash -s -- "$tag" "$BUNDLE_DIR" <<'VM'
set -euo pipefail
tag="$1" out="$2"
src=/srv/app/istota/src
export BUILDX_BUILDER=default
ISTOTA_TAG="$tag" INSTALL_CLAUDE_CODE=0 WITHOUT_DEVBOX=1 COMPOSE_PROFILES=whatsapp-baileys \
    istota-stack compose build daemon whatsapp-baileys
docker build -q --target base -t "istota-browser-base:${tag}" "${src}/docker/browser" >/dev/null
# nginx by its tag: saved by digest it loads with no name, and compose's
# digest reference then misses and pulls.
nginx="$(awk '/image: nginx:alpine@/ { print $2; exit }' "${src}/docker/docker-compose.yml")"
docker pull -q "$nginx" >/dev/null
docker tag "$nginx" nginx:alpine
refs=("istota:${tag}" "istota-browser-base:${tag}" "istota-whatsapp-baileys:${tag}" nginx:alpine)
for ref in "${refs[@]}"; do
    printf '%s=%s\n' "$ref" "$(docker image inspect --format '{{.Id}}' "$ref")"
done > "${out}/image-ids.txt"
docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "istota:${tag}" \
    | sed -n 's/^ISTOTA_CLAUDE_CODE_VERSION=//p' > "${out}/claude-code-version.txt"
docker save -o "${out}/images-${tag}.tar" "${refs[@]}"
dpkg-query -W -f='${Package} ${Version}\n' | sort > "${out}/disk-packages.txt"
# Every path on the disk but Docker's, which generalize empties.
find / -xdev \( -path /proc -o -path /sys -o -path /var/lib/docker -o -path /var/lib/containerd \) -prune \
    -o -print > "${out}/disk-paths.txt"
VM
step images "$t"

t="$(now)"
log "generalizing the disk; the VM powers itself off"
vm bash -s < "${scratch}/appliance-generalize.sh"
for _ in $(seq 1 120); do
    [ "$(limactl list --format '{{.Status}}' "$VM")" = Stopped ] && break
    sleep 5
done
[ "$(limactl list --format '{{.Status}}' "$VM")" = Stopped ] || die "${VM} did not power itself off after generalizing"
step generalize "$t"

t="$(now)"
raw="${work}/istota-appliance-${tag}.raw"
# An APFS clone: sparse and instant.
cp -c "${LIMA_HOME}/${VM}/disk" "$raw"
log "checking the disk for Claude Code and the builder's identity"
checks=0
python3 -I "${HERE}/release-artifacts.py" disk "$raw" \
    --forbid "the builder's login name=$(id -un)" --forbid "the builder's home path=${HOME}" || checks=1
python3 -I "${HERE}/release-artifacts.py" paths "${work}/disk-paths.txt" || checks=1
log "compressing the disk"
zstd -q -T0 -19 --long=27 "$raw" -o "${raw}.zst"
disk_allocated="$(du -k "$raw" | cut -f1)"
rm -f "$raw"
if [ -z "$keep_vm" ]; then limactl delete --force "$VM" >/dev/null; fi
step disk "$t"

t="$(now)"
log "checking the images and listing their packages"
python3 -I "${HERE}/release-artifacts.py" images "${work}/images-${tag}.tar" \
    --packages "${work}/packages-${tag}.txt" --disk-packages "${work}/disk-packages.txt" \
    --apt-snapshot "$snapshot" --tag "$tag" || checks=1
step scan "$t"
[ "$checks" -eq 0 ] || die "the release failed its content checks (above); ${work} is kept for a look"

# shellcheck disable=SC2046
"${HERE}/sign-images-manifest.sh" --tag "$tag" --archive "${work}/images-${tag}.tar" --key "$key" \
    --appliance "${raw}.zst" --claude-code-version "$(cat "${work}/claude-code-version.txt")" \
    $(cat "${work}/image-ids.txt") >/dev/null

mkdir -p "$release"
for name in "istota-appliance-${tag}.raw.zst" "images-${tag}.tar" "images-${tag}.json" \
        "images-${tag}.json.sig" "istota-${tag}.bundle" "packages-${tag}.txt"; do
    mv "${work}/${name}" "${release}/${name}"
done
rm -rf "$work"
rmdir "$(dirname "$work")" 2>/dev/null || true

log "built ${release} in $(( $(now) - started ))s (${timings[*]}); disk ${disk_allocated} KiB allocated before compression"
(cd "$release" && ls -ln | awk 'NR > 1 { printf "  %12d  %s\n", $5, $9 }')
