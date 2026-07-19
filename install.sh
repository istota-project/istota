#!/bin/bash
# Istota installer dispatcher
#
# Bare metal (default) — installs natively via Ansible (Debian/Ubuntu, requires root):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash
#
# Docker — brings up the full-stack compose file:
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
#
# Usage:
#   install.sh [--docker] [other flags...]
#     --docker        Use the Docker path (docker/init.sh) instead of bare metal
#     --help          Show this help
#
# All other flags pass through to the chosen subscript:
#   bare metal: --headless, --update, --dry-run, --settings PATH
#   docker:     --minimal, --force, --start, --no-start
#
# Both paths default to running an interactive wizard. Pass --headless
# (bare metal) or use --force / a pre-existing .env (docker) to skip it.
#
# Environment overrides (curl-pipe path):
#   ISTOTA_REPO_URL     Repo to clone (default: https://github.com/istota-project/istota.git)
#   ISTOTA_REPO_BRANCH  Branch / tag to clone (default: main)
#   ISTOTA_CLONE_DIR    Where to clone the repo. Defaults to /tmp/istota-install
#                       for bare metal (the Ansible deploy lands in
#                       /srv/app/istota regardless) and ~/istota for docker
#                       (so you can come back for `docker compose` ops).

set -euo pipefail

REPO_URL="${ISTOTA_REPO_URL:-https://github.com/istota-project/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"
# CLONE_DIR default is mode-dependent; bare metal uses a temp clone (the
# Ansible deploy lands in /srv/app/istota anyway), docker uses a stable
# user-visible path so the user can come back for `docker compose` ops.
CLONE_DIR="${ISTOTA_CLONE_DIR:-}"

_BOLD="\033[1m"; _BLUE="\033[1;34m"; _GREEN="\033[1;32m"
_YELLOW="\033[1;33m"; _RED="\033[1;31m"; _DIM="\033[2m"; _RESET="\033[0m"
info()  { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()    { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()  { echo -e "${_YELLOW}  !${_RESET} $*"; }
error() { echo -e "${_RED}ERROR:${_RESET} $*" >&2; }
die()   { error "$@"; exit 1; }

show_help() {
    sed -n '2,/^$/s/^# \?//p' "$0"
}

# Parse only the flags this dispatcher cares about; everything else forwards.
MODE="bare"
FORWARD_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --docker) MODE="docker"; shift ;;
        --bare)   MODE="bare"; shift ;;
        --help|-h) show_help; exit 0 ;;
        *) FORWARD_ARGS+=("$1"); shift ;;
    esac
done

# Find the repo root. If install.sh is sitting next to deploy/ and docker/,
# we're in a clone. Otherwise we were curl-piped and need to clone.
script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$script_path")" && pwd)"
else
    SCRIPT_DIR=""
fi

_default_clone_dir() {
    # Docker default lands at $HOME/istota so the user can come back for
    # `docker compose` ops. When invoked via `sudo bash`, sudo's default
    # `env_reset` strips HOME, leaving root's home — which surprises users
    # following the curl-pipe install instructions. Fall back to the
    # invoking user's home when SUDO_USER is set, then HOME, then /root.
    local home_dir=""
    if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
        home_dir="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
    fi
    if [ -z "$home_dir" ]; then
        home_dir="${HOME:-/root}"
    fi
    echo "${home_dir}/istota"
}

repo_root_or_empty() {
    local d="$1"
    if [ -n "$d" ] && [ -d "$d/deploy" ] && [ -d "$d/docker" ] && [ -f "$d/deploy/install.sh" ]; then
        echo "$d"
    fi
}

REPO_ROOT="$(repo_root_or_empty "$SCRIPT_DIR")"

# When curl-piped, stdin is the pipe carrying the script, not a TTY — so any
# wizard `read` calls in the subscripts will fail. Reattach stdin to /dev/tty
# if we have one, so `[ -t 0 ]` checks pass downstream. Best-effort: if
# /dev/tty isn't available (true non-interactive run), leave stdin alone and
# let the subscript decide whether it can proceed.
reattach_tty_if_needed() {
    if [ ! -t 0 ] && [ -e /dev/tty ]; then
        exec < /dev/tty
    fi
}

ensure_repo() {
    [ -n "$REPO_ROOT" ] && return 0

    info "Fetching istota repo (curl-pipe mode)"
    if ! command -v git >/dev/null 2>&1; then
        if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq && apt-get install -y -qq git
        else
            die "git is required but not installed. Install git and re-run."
        fi
    fi

    if [ -d "$CLONE_DIR/.git" ]; then
        info "Updating existing clone at $CLONE_DIR"
        git -C "$CLONE_DIR" fetch origin --tags --quiet
        git -C "$CLONE_DIR" reset --hard "origin/$REPO_BRANCH" --quiet
    else
        rm -rf "$CLONE_DIR"
        info "Cloning $REPO_URL ($REPO_BRANCH) → $CLONE_DIR"
        git clone --depth 1 --branch "$REPO_BRANCH" --quiet "$REPO_URL" "$CLONE_DIR"
    fi
    REPO_ROOT="$CLONE_DIR"
    ok "Repo ready at $REPO_ROOT"
}

run_bare() {
    # If git isn't installed and we're not root, we can't apt-get it. Bail
    # early with a clear message rather than failing midway through clone.
    if [ -z "$REPO_ROOT" ] && ! command -v git >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ]; then
        die "git is required but not installed. Install git, or re-run with sudo:
  curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash"
    fi

    : "${CLONE_DIR:=/tmp/istota-install}"
    ensure_repo
    reattach_tty_if_needed
    local target="$REPO_ROOT/deploy/install.sh"
    [ -f "$target" ] || die "deploy/install.sh not found at $target"

    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            info "Bare-metal install requires root — re-executing deploy/install.sh under sudo"
            exec sudo -E bash "$target" "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"
        else
            die "Bare-metal install requires root. Re-run as root or install sudo."
        fi
    fi

    exec bash "$target" "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"
}

run_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        die "docker not found. Install Docker first: https://docs.docker.com/engine/install/"
    fi
    if ! docker compose version >/dev/null 2>&1; then
        die "'docker compose' plugin not available. Install docker-compose-plugin."
    fi

    : "${CLONE_DIR:=$(_default_clone_dir)}"
    ensure_repo
    reattach_tty_if_needed
    local target="$REPO_ROOT/docker/init.sh"
    [ -f "$target" ] || die "docker/init.sh not found at $target"
    exec bash "$target" "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"
}

case "$MODE" in
    bare)   run_bare ;;
    docker) run_docker ;;
    *)      die "Unknown mode: $MODE" ;;
esac
