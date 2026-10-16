#!/bin/bash
# mac-desktop-app Stage 1 (c): the stack's images with INSTALL_CLAUDE_CODE=0,
# through `docker save | zstd`, then removed and loaded back. Run as root in a
# VM from the appliance, network on.
#
#   images.sh build      # istota-stack update <tag> with the browser and baileys profiles
#   images.sh roundtrip  # save, compress, remove, load, compare image IDs
set -euo pipefail
TAG="${SPIKE_TAG:-v0.46.0}"
STACK=/srv/app/istota
OUT=/var/tmp/spike-images
NGINX="$(awk '/image: nginx:alpine@/ { print $2; exit }' "${STACK}/src/docker/docker-compose.yml")"
REFS=("istota:${TAG}" "istota-browser:${TAG}" "istota-whatsapp-baileys:${TAG}" "$NGINX")
t() { date +%s.%N; }
since() { awk -v a="$1" -v b="$(t)" 'BEGIN { printf "%.1f", b - a }'; }

case "${1:-}" in
build)
    # No config.toml, so needs_claude_code answers 0: the image the app ships.
    grep -q '^COMPOSE_PROFILES=' "${STACK}/.env" \
        || echo 'COMPOSE_PROFILES=browser,whatsapp-baileys' >> "${STACK}/.env"
    t0="$(t)"
    istota-stack update "$TAG" || echo "update exit $? (the stack cannot start without a config)"
    echo "update: $(since "$t0") s"
    docker pull -q "$NGINX" >/dev/null
    docker image inspect --format '{{.Config.Env}}' "istota:${TAG}" | tr ' ' '\n' | grep -i claude || true
    docker run --rm --entrypoint sh "istota:${TAG}" -c 'command -v claude || echo "no claude binary"'
    docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}'
    docker info --format 'storage: {{.Driver}} {{.DriverStatus}}'
    ;;
roundtrip)
    mkdir -p "$OUT"
    for ref in "${REFS[@]}"; do
        printf '%s %s\n' "$ref" "$(docker image inspect --format '{{.Id}}' "$ref")"
    done > "${OUT}/before.txt"
    cat "${OUT}/before.txt"
    t0="$(t)"
    docker save "${REFS[@]}" -o "${OUT}/images.tar"
    echo "save: $(since "$t0") s, tar $(stat -c %s "${OUT}/images.tar") bytes"
    t0="$(t)"
    zstd -q -T0 -19 --long=27 "${OUT}/images.tar" -o "${OUT}/images.tar.zst"
    echo "zstd -19 --long=27: $(since "$t0") s, $(stat -c %s "${OUT}/images.tar.zst") bytes"
    rm -f "${OUT}/images.tar"
    istota-stack compose down >/dev/null 2>&1 || true
    docker rm -f $(docker ps -aq) >/dev/null 2>&1 || true
    docker rmi -f "${REFS[@]}" >/dev/null
    docker image prune -af >/dev/null
    docker builder prune -af >/dev/null
    echo "images left: $(docker images -q | wc -l)"
    t0="$(t)"
    zstd -q -d --long=27 -c "${OUT}/images.tar.zst" | docker load -q
    echo "unzstd | docker load: $(since "$t0") s"
    for ref in "${REFS[@]}"; do
        printf '%s %s\n' "$ref" "$(docker image inspect --format '{{.Id}}' "$ref")"
    done > "${OUT}/after.txt"
    if diff "${OUT}/before.txt" "${OUT}/after.txt"; then echo "image IDs: equal"; else echo "image IDs: CHANGED"; fi
    ;;
digest)
    # Saved by digest, nginx comes back with its ID and no name, and compose's
    # `nginx:alpine@sha256:...` then misses and would pull. Saved by tag?
    set +e
    d="${NGINX##*@}"
    printf 'by nginx@digest: '; docker image inspect --format '{{.Id}}' "nginx@${d}" 2>&1 | sed -n 1p
    docker tag "$d" nginx:alpine
    printf 'after tag, %s: ' "$NGINX"; docker image inspect --format '{{.Id}}' "$NGINX" 2>&1 | sed -n 1p
    docker save nginx:alpine -o /tmp/nginx-tagged.tar
    docker rmi -f nginx:alpine "$d" >/dev/null 2>&1 || true
    docker image prune -af >/dev/null
    printf 'removed: '; docker image inspect --format '{{.Id}}' "$NGINX" 2>&1 | sed -n 1p
    docker load -q -i /tmp/nginx-tagged.tar
    printf 'after a load of the tagged save, %s: ' "$NGINX"; docker image inspect --format '{{.Id}}' "$NGINX" 2>&1 | sed -n 1p
    ;;
*) echo "usage: $0 build|roundtrip|digest" >&2; exit 64 ;;
esac
