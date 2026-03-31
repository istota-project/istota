#!/bin/bash
# Istota deployment bootstrap
# Ensures Ansible is available, then delegates to the Ansible role.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/muinyc/istota/main/deploy/install.sh -o install.sh
#   sudo bash install.sh
#
#   install.sh [OPTIONS]
#     --interactive     Guided setup wizard (writes settings file, default on first run)
#     --update          Update only (pull code + config + restart, skip full system setup)
#     --dry-run         Run wizard, generate Ansible vars, show what would change
#     --settings PATH   Settings file path (default: /etc/istota/settings.toml)
#     --help            Show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd 2>/dev/null || echo "/tmp")"

# Defaults
SETTINGS_FILE="${ISTOTA_SETTINGS_FILE:-/etc/istota/settings.toml}"
VARS_FILE="${ISTOTA_VARS_FILE:-/etc/istota/vars.yml}"
REPO_URL="${ISTOTA_REPO_URL:-https://github.com/muinyc/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"
INSTALL_DIR=""  # Set after repo is available
UPDATE_ONLY=false
INTERACTIVE=false
DRY_RUN=false

# Output helpers
_BOLD="\033[1m"
_BLUE="\033[1;34m"
_GREEN="\033[1;32m"
_YELLOW="\033[1;33m"
_RED="\033[1;31m"
_DIM="\033[2m"
_RESET="\033[0m"

info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
error()   { echo -e "${_RED}ERROR:${_RESET} $*" >&2; }
die()     { error "$@"; exit 1; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }

command_exists() { command -v "$1" &>/dev/null; }

# ============================================================
# Parse arguments
# ============================================================

while [ $# -gt 0 ]; do
    case "$1" in
        --interactive)  INTERACTIVE=true; shift ;;
        --update)       UPDATE_ONLY=true; shift ;;
        --dry-run)      DRY_RUN=true; INTERACTIVE=true; shift ;;
        --settings)     SETTINGS_FILE="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,/^$/s/^# \?//p' "$0"
            exit 0 ;;
        *) die "Unknown option: $1. Use --help for usage." ;;
    esac
done

# ============================================================
# Pre-flight
# ============================================================

preflight() {
    section "Pre-flight Checks"

    if [ "$(id -u)" -ne 0 ]; then
        die "This script must be run as root (or with sudo)"
    fi

    # OS detection
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "$ID" in
            debian)
                if [ "${VERSION_ID:-0}" -lt 12 ] 2>/dev/null; then
                    warn "Debian $VERSION_ID detected. Debian 12+ recommended."
                else
                    ok "OS: $PRETTY_NAME"
                fi
                ;;
            ubuntu) ok "OS: $PRETTY_NAME" ;;
            *) warn "Untested OS: $PRETTY_NAME. Debian/Ubuntu recommended." ;;
        esac
    else
        warn "Could not detect OS. Debian/Ubuntu recommended."
    fi

    # Internet
    if curl -sf --max-time 5 https://github.com > /dev/null 2>&1; then
        ok "Internet connectivity"
    else
        die "No internet connectivity. Cannot reach github.com."
    fi

    # Python 3.11+
    if command_exists python3; then
        local pyver pymajor pyminor
        pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        pymajor=$(echo "$pyver" | cut -d. -f1)
        pyminor=$(echo "$pyver" | cut -d. -f2)
        if [ "$pymajor" -ge 3 ] && [ "$pyminor" -ge 11 ]; then
            ok "Python $pyver"
        else
            warn "Python $pyver found. 3.11+ required (will be installed)."
        fi
    else
        warn "Python not found (will be installed)"
    fi

    # Existing settings
    if [ -f "$SETTINGS_FILE" ]; then
        ok "Existing settings found at $SETTINGS_FILE"
        if [ "$INTERACTIVE" = true ]; then
            echo
            local overwrite
            read -rp "  Overwrite existing settings with new wizard? [y/N]: " overwrite
            case "$overwrite" in
                [yY]*) : ;;
                *)     INTERACTIVE=false; info "Skipping wizard, using existing settings" ;;
            esac
        fi
    fi
}

# ============================================================
# Ensure prerequisites: Python, pipx, Ansible
# ============================================================

ensure_python() {
    if command_exists python3; then
        local pyminor
        pyminor=$(python3 -c "import sys; print(sys.version_info.minor)")
        if [ "$pyminor" -ge 11 ]; then
            return
        fi
    fi
    info "Installing Python 3"
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv 2>&1 | tail -3
    ok "Python installed"
}

ensure_pipx() {
    if command_exists pipx; then
        ok "pipx available"
        return
    fi
    info "Installing pipx"
    apt-get install -y -qq pipx 2>&1 | tail -3 \
        || python3 -m pip install --user pipx 2>&1 | tail -3
    # Ensure pipx is on PATH for the rest of this script
    eval "$(pipx ensurepath --force 2>/dev/null)" || true
    export PATH="$HOME/.local/bin:$PATH"
    ok "pipx installed"
}

ensure_ansible() {
    if command_exists ansible-playbook; then
        ok "Ansible available ($(ansible --version 2>/dev/null | head -1))"
        return
    fi
    info "Installing ansible-core via pipx"
    pipx install ansible-core 2>&1 | tail -3
    eval "$(pipx ensurepath --force 2>/dev/null)" || true
    export PATH="$HOME/.local/bin:$PATH"
    if command_exists ansible-playbook; then
        ok "Ansible installed"
    else
        die "Failed to install ansible-core. Install manually: pipx install ansible-core"
    fi
}

ensure_collections() {
    info "Ensuring Ansible collections"
    ansible-galaxy collection install community.general ansible.posix --force-with-deps 2>&1 | tail -5
    ok "Ansible collections ready"
}

# ============================================================
# Get the repo (for the Ansible role + wizard)
# ============================================================

ensure_repo() {
    # If we're running from a cloned repo, use it directly
    if [ -f "$SCRIPT_DIR/local-playbook.yml" ] && [ -d "$SCRIPT_DIR/ansible" ]; then
        INSTALL_DIR="$SCRIPT_DIR"
        ok "Using local repo at $INSTALL_DIR"
        return
    fi

    # Otherwise, clone to a temp location
    local clone_dir="/tmp/istota-deploy"
    if [ -d "$clone_dir/.git" ]; then
        info "Updating deploy repo"
        git -C "$clone_dir" fetch origin --tags
        git -C "$clone_dir" reset --hard "origin/$REPO_BRANCH"
    else
        info "Cloning deploy repo"
        apt-get install -y -qq git 2>&1 | tail -1
        git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$clone_dir"
    fi
    INSTALL_DIR="$clone_dir/deploy"
    ok "Deploy repo ready at $INSTALL_DIR"
}

# ============================================================
# Settings → Ansible vars conversion
# ============================================================

convert_settings() {
    if [ ! -f "$SETTINGS_FILE" ]; then
        die "No settings file found at $SETTINGS_FILE. Run with --interactive for guided setup."
    fi

    info "Converting settings to Ansible vars"
    python3 "$INSTALL_DIR/settings_to_vars.py" \
        --settings "$SETTINGS_FILE" \
        --output "$VARS_FILE"
    ok "Vars written to $VARS_FILE"
}

# ============================================================
# Run Ansible
# ============================================================

run_ansible() {
    local playbook="$INSTALL_DIR/local-playbook.yml"

    if [ ! -f "$playbook" ]; then
        die "Playbook not found at $playbook"
    fi

    local extra_args=()
    extra_args+=(--extra-vars "@$VARS_FILE")

    if [ "$UPDATE_ONLY" = true ]; then
        extra_args+=(--extra-vars "istota_update_only=true")
    fi

    if [ "$DRY_RUN" = true ]; then
        extra_args+=(--check --diff)
    fi

    section "Running Ansible"
    echo -e "  ${_BOLD}Playbook:${_RESET}  $playbook"
    echo -e "  ${_BOLD}Vars:${_RESET}      $VARS_FILE"
    echo -e "  ${_BOLD}Mode:${_RESET}      $([ "$DRY_RUN" = true ] && echo "dry-run" || ([ "$UPDATE_ONLY" = true ] && echo "update" || echo "full install"))"
    echo

    ansible-playbook "$playbook" \
        --connection local \
        --inventory localhost, \
        "${extra_args[@]}"
}

# ============================================================
# Main
# ============================================================

main() {
    if [ "$DRY_RUN" = true ]; then
        # Dry-run only needs Python for the wizard and converter
        if [ "$(id -u)" -ne 0 ]; then
            die "This script must be run as root (or with sudo)"
        fi
        ensure_python
        ensure_repo

        # Run wizard to temp file
        local tmpdir
        tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/istota-dry-run.XXXXXX")
        local orig_settings="$SETTINGS_FILE"
        SETTINGS_FILE="$tmpdir/settings.toml"
        VARS_FILE="$tmpdir/vars.yml"

        bash "$INSTALL_DIR/wizard.sh" --settings "$SETTINGS_FILE"
        python3 "$INSTALL_DIR/settings_to_vars.py" \
            --settings "$SETTINGS_FILE" \
            --output "$VARS_FILE"

        section "Dry-run Output"
        echo -e "  ${_BOLD}Settings:${_RESET}  $SETTINGS_FILE"
        echo -e "  ${_BOLD}Vars:${_RESET}      $VARS_FILE"
        echo
        echo -e "  ${_DIM}Inspect:${_RESET}"
        echo "    cat $SETTINGS_FILE"
        echo "    cat $VARS_FILE"
        echo
        echo -e "  ${_DIM}Clean up when done:${_RESET}"
        echo "    rm -rf $tmpdir"
        echo

        # If Ansible is available, also show check mode
        if command_exists ansible-playbook; then
            ensure_repo
            section "Ansible Check Mode"
            ansible-playbook "$INSTALL_DIR/local-playbook.yml" \
                --connection local \
                --inventory localhost, \
                --extra-vars "@$VARS_FILE" \
                --check --diff || true
        fi
        return
    fi

    preflight

    # Auto-detect interactive mode for first-time installs
    if [ "$INTERACTIVE" = false ] && [ "$UPDATE_ONLY" = false ] && [ ! -f "$SETTINGS_FILE" ]; then
        if [ -t 0 ]; then
            INTERACTIVE=true
        else
            die "No settings file found and stdin is not a terminal.
  For interactive setup: bash install.sh --interactive
  Or provide a settings file: bash install.sh --settings /path/to/settings.toml"
        fi
    fi

    # Step 1: Ensure Python, pipx, Ansible
    section "Bootstrap"
    ensure_python
    ensure_pipx
    ensure_ansible
    ensure_collections

    # Step 2: Get the repo
    ensure_repo

    # Step 3: Run wizard if interactive
    if [ "$INTERACTIVE" = true ]; then
        bash "$INSTALL_DIR/wizard.sh" --settings "$SETTINGS_FILE"
    fi

    # Step 4: Convert settings to Ansible vars
    convert_settings

    # Step 5: Run Ansible
    run_ansible

    section "Done"
    if [ "$UPDATE_ONLY" = true ]; then
        ok "Update complete"
    else
        ok "Installation complete"
    fi
    echo
    echo -e "  ${_BOLD}Settings:${_RESET}  $SETTINGS_FILE"
    echo -e "  ${_BOLD}Service:${_RESET}   journalctl -u istota-scheduler -f"
    echo
    echo -e "  To update: ${_DIM}sudo bash install.sh --update${_RESET}"
    echo -e "  To reconfigure: ${_DIM}sudo bash install.sh --interactive${_RESET}"
    echo
}

# Error trap
_on_error() {
    local exit_code=$? line_no="${BASH_LINENO[0]}"
    echo
    error "Failed at line $line_no (exit code $exit_code)"
    echo
    echo "  After fixing the issue, re-run:"
    echo "    sudo bash install.sh --update"
    echo
    exit "$exit_code"
}
trap _on_error ERR

main "$@"
