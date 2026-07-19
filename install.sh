#!/bin/bash
# Istota installer dispatcher
#
# Run interactively (from a terminal) it asks which install mode you want:
#
#   Server      Multi-user deployment on a Debian/Ubuntu server, backed by
#               Nextcloud (files, Talk chat, CalDAV, web login) with per-user
#               bubblewrap isolation. Installs system-wide via Ansible and
#               needs root. This is the full Istota.
#
#   Standalone  Single-user install on your own machine (macOS or Linux). No
#               server, no Nextcloud, no login, no sandbox. You chat through
#               the local web UI and the REPL; everything runs as your user in
#               one process (istota serve). Lighter and quick to start, but
#               unsandboxed — only give it content and instructions you trust.
#
# Server, interactive (curl-pipe): pick from the menu, or force it with --bare
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash
#
# Standalone, interactive (run WITHOUT sudo — it installs into your user):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --standalone
#
# Server via Docker — brings up the full-stack compose file:
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
#
# Usage:
#   install.sh [--bare | --docker | --standalone] [other flags...]
#     --bare          Server install, native via Ansible (needs root)
#     --docker        Server install, Docker path (docker/init.sh)
#     --standalone    Local single-user install (uv tool install + istota setup)
#     --help          Show this help
#   With no mode flag on a terminal you are asked to choose; with no mode flag
#   and no terminal, the historical default (--bare) is used.
#
# All other flags pass through to the chosen subscript:
#   bare metal: --headless, --update, --dry-run, --settings PATH
#   docker:     --minimal, --force, --start, --no-start
#   standalone: any `istota setup` flag (--yes, --force, --workspace, --port, …)
#
# The bare-metal and docker paths default to running an interactive wizard
# (skip with --headless / --force). Standalone always runs `istota setup`.
#
# Environment overrides (curl-pipe path):
#   ISTOTA_REPO_URL     Repo to clone (default: https://github.com/istota-project/istota.git)
#   ISTOTA_REPO_BRANCH  Branch / tag to clone (default: main)
#   ISTOTA_CLONE_DIR    Where to clone the repo. Defaults to /tmp/istota-install
#                       for bare metal and standalone (the Ansible deploy lands
#                       in /srv/app/istota, and standalone installs a wheel via
#                       uv, so the clone is throwaway) and ~/istota for docker
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
info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
error()   { echo -e "${_RED}ERROR:${_RESET} $*" >&2; }
die()     { error "$@"; exit 1; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }

show_help() {
    sed -n '2,/^$/s/^# \?//p' "$0"
}

# Parse only the flags this dispatcher cares about; everything else forwards.
# MODE_EXPLICIT tracks whether the user chose a mode via a flag — if not, and
# we're interactive, we prompt for one before dispatching.
MODE="bare"
MODE_EXPLICIT=false
FORWARD_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --docker)     MODE="docker";     MODE_EXPLICIT=true; shift ;;
        --bare)       MODE="bare";       MODE_EXPLICIT=true; shift ;;
        --standalone) MODE="standalone"; MODE_EXPLICIT=true; shift ;;
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
    # Already interactive — nothing to do.
    [ -t 0 ] && return 0
    # /dev/tty can exist as a device node yet be unopenable when there's no
    # controlling terminal (cron, CI, a detached pipe). Probe in a subshell so a
    # failed open doesn't abort the script under `set -e`; only reattach for real
    # once we know it opens.
    if [ -e /dev/tty ] && (exec < /dev/tty) 2>/dev/null; then
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

# --- Standalone (local single-user) helpers --------------------------------

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        ok "uv available ($(uv --version 2>/dev/null))"
        return 0
    fi
    info "Installing uv (Python package/tool manager)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        die "uv is required but not installed, and neither curl nor wget is available to fetch it.
  Install uv manually (https://docs.astral.sh/uv/) and re-run."
    fi
    # uv's installer drops the binary in ~/.local/bin by default.
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 \
        || die "uv installed but not on PATH. Open a new shell (or run '. ~/.local/bin/env') and re-run."
    ok "uv installed"
}

# A source-clone install has no pre-built web UI assets; the wheel only ships
# them when src/istota/web_static exists (a PyPI/wheel install would already
# carry them). Best-effort build so the web UI isn't blank. The REPL works
# regardless.
maybe_build_web_static() {
    local root="$1"
    if [ -d "$root/src/istota/web_static" ] && [ -n "$(ls -A "$root/src/istota/web_static" 2>/dev/null)" ]; then
        return 0
    fi
    if [ ! -f "$root/scripts/build-web-static.sh" ]; then
        return 0
    fi
    if command -v npm >/dev/null 2>&1; then
        info "Building the web UI assets (npm — this can take a minute)"
        if bash "$root/scripts/build-web-static.sh"; then
            ok "Web UI assets built"
        else
            warn "Web UI asset build failed — the web UI may be blank."
            warn "The REPL ('istota repl') still works. Re-run scripts/build-web-static.sh later, then re-install."
        fi
    else
        warn "npm not found — skipping web UI asset build; the web UI will have no assets."
        warn "Use 'istota repl', or install Node.js, run scripts/build-web-static.sh, and re-install."
    fi
}

run_standalone() {
    # Root check FIRST — before touching the terminal or anything else. Standalone
    # installs into your own user account (uv tool + ~/.config/istota); running it
    # as root would install into root's home and is almost never what the user
    # wants. This must error+exit immediately (it needs no terminal), so a user who
    # piped the Server command through sudo and picked Standalone gets a clear
    # message instead of proceeding wrong (or, historically, hanging).
    if [ "$(id -u)" -eq 0 ]; then
        local hint=""
        if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
            hint="
  You ran this with sudo — re-run it as ${SUDO_USER}, without sudo."
        fi
        die "Standalone mode installs into your own user account and must not run as root.${hint}
  Re-run WITHOUT sudo:  curl -fsSL <install-url> | bash -s -- --standalone
  (Only the Server install needs root.)"
    fi

    # Non-root: give `istota setup` a real terminal in the curl-pipe case. Safe
    # here (the dispatch case above is already fully read) and paired with the
    # explicit exit at the end so bash doesn't read on from the swapped-in tty.
    reattach_tty_if_needed

    section "Standalone install"
    ensure_uv

    # No PyPI release yet, so install from the repo: use the local checkout if
    # we're already in one, otherwise clone it (ensure_repo sets REPO_ROOT).
    : "${CLONE_DIR:=/tmp/istota-install}"
    ensure_repo
    { [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/pyproject.toml" ]; } \
        || die "Could not locate the istota source (looked at '$REPO_ROOT')."

    maybe_build_web_static "$REPO_ROOT"

    info "Installing istota[local] from $REPO_ROOT"
    uv tool install --force "${REPO_ROOT}[local]"
    export PATH="$HOME/.local/bin:$PATH"
    command -v istota >/dev/null 2>&1 \
        || die "istota installed but not on PATH. Run 'uv tool update-shell' (or add ~/.local/bin to PATH), open a new shell, then: istota setup"
    ok "istota installed"

    section "Running setup"
    istota setup "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"

    section "Done"
    ok "Standalone install complete"
    echo
    echo -e "  ${_BOLD}Start it:${_RESET}  istota serve"
    echo -e "             ${_DIM}then open the printed http://127.0.0.1:<port>/istota URL (no login)${_RESET}"
    echo -e "  ${_BOLD}REPL:${_RESET}      istota repl"
    echo -e "  ${_BOLD}Docs:${_RESET}      docs/LOCAL_INSTALL.md"
    echo
    echo -e "  ${_DIM}Standalone runs unsandboxed as your user — only give it content you trust.${_RESET}"
    echo
    # Explicit: run_standalone reattached stdin to /dev/tty (for `istota setup`),
    # so exit rather than let bash try to read another command from the terminal.
    exit 0
}

# --- Interactive mode selection --------------------------------------------

# When no mode flag was given and we have a terminal, ask which install shape
# the user wants. Sets $MODE. With no terminal, leaves the historical default
# (--bare) untouched so non-interactive `curl | sudo bash` still works.
#
# IMPORTANT: we must NOT `exec < /dev/tty` here. Under `curl | bash`, the shell
# is still reading the rest of this script (the dispatch `case` below) from the
# pipe on stdin; swapping stdin to the terminal mid-script makes bash try to
# read the remaining script from the keyboard, which hangs. So we read the
# choice with a redirect on the `read` command alone and leave stdin untouched.
prompt_install_mode() {
    local tty_src
    if [ -t 0 ]; then
        tty_src="/dev/stdin"                                   # already interactive
    elif [ -e /dev/tty ] && (exec < /dev/tty) 2>/dev/null; then
        tty_src="/dev/tty"                                     # curl-pipe: prompt on the terminal
    else
        return 0                                               # no terminal — keep the default
    fi

    local root_note=""
    if [ "$(id -u)" -eq 0 ]; then
        root_note="  ${_DIM}(run without sudo for this option)${_RESET}"
    fi

    echo
    echo -e "${_BOLD}How do you want to install Istota?${_RESET}"
    echo
    echo -e "  ${_BOLD}1) Server${_RESET} ${_DIM}(default)${_RESET}"
    echo "     Multi-user deployment on a Debian/Ubuntu server. Backed by"
    echo "     Nextcloud (files, Talk chat, CalDAV, web login) with per-user"
    echo "     bubblewrap isolation. Installs system-wide via Ansible and needs"
    echo "     root. This is the full Istota."
    echo
    echo -e "  ${_BOLD}2) Standalone${_RESET}${root_note}"
    echo "     Single-user install on your own machine (macOS or Linux). No"
    echo "     server, no Nextcloud, no login, no sandbox. You chat through the"
    echo "     local web UI and the REPL; everything runs as you in one process."
    echo "     Lighter and quick to start, but unsandboxed — only give it"
    echo "     content and instructions you trust."
    echo
    echo -e "  ${_DIM}Tip: the Server path can also run as Docker — re-run with --docker.${_RESET}"
    echo
    local choice=""
    read -rp "  Choose [1/2, default 1]: " choice < "$tty_src" || true
    case "$choice" in
        2|s|S|standalone|Standalone) MODE="standalone" ;;
        ""|1|server|Server)          MODE="bare" ;;
        *) warn "Unrecognized choice '$choice' — defaulting to Server."; MODE="bare" ;;
    esac
}

if [ "$MODE_EXPLICIT" = false ]; then
    prompt_install_mode
fi

case "$MODE" in
    bare)       run_bare ;;
    docker)     run_docker ;;
    standalone) run_standalone ;;
    *)          die "Unknown mode: $MODE" ;;
esac
