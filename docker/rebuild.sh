#!/usr/bin/env bash
#
# Tear down and bring the istota docker-compose stack back up, always
# rebuilding the images so the containers run the latest local code.
#
# Usage:
#   ./rebuild.sh [options] [-- SERVICE ...]
#
# Options:
#   -v, --volumes     Also remove named volumes on teardown (DESTRUCTIVE:
#                     wipes Postgres, Nextcloud, istota data, etc.).
#   -c, --no-cache    Build with no layer cache (full clean rebuild).
#   -p, --profile P   Enable a compose profile (repeatable: browser,
#                     location, devbox). Passed to both down and up.
#   -f, --follow      Follow logs after starting (foreground) instead of
#                     detaching.
#       --no-pull     Skip pulling newer base images before building.
#   -h, --help        Show this help.
#
# Anything after `--` is passed through as the list of services to bring
# up (default: all).
set -euo pipefail

cd "$(dirname "$0")"

volumes=false
no_cache=false
follow=false
pull=true
profiles=()
services=()

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--volumes)  volumes=true; shift ;;
    -c|--no-cache) no_cache=true; shift ;;
    -f|--follow)   follow=true; shift ;;
    --no-pull)     pull=false; shift ;;
    -p|--profile)  profiles+=("$2"); shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    --)            shift; services=("$@"); break ;;
    *)             echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

profile_args=()
for p in "${profiles[@]}"; do
  profile_args+=(--profile "$p")
done

compose=(docker compose "${profile_args[@]}")

if $volumes; then
  read -r -p "This will DELETE all named volumes (databases, Nextcloud data). Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
  down_args=(--volumes --remove-orphans)
else
  down_args=(--remove-orphans)
fi

echo "==> Tearing down stack..."
"${compose[@]}" down "${down_args[@]}"

build_args=()
$no_cache && build_args+=(--no-cache)
$pull && build_args+=(--pull)

echo "==> Building images (force rebuild)..."
"${compose[@]}" build "${build_args[@]}" "${services[@]}"

up_args=(--force-recreate)
$follow || up_args+=(--detach)

echo "==> Starting stack..."
"${compose[@]}" up "${up_args[@]}" "${services[@]}"

$follow || echo "==> Stack is up. Tail logs with: docker compose logs -f"
