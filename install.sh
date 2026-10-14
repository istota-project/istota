#!/bin/bash
# Istota installer
#
#   Server      One istota install on a Debian 13 machine of its own (a VM, a
#               VPS or a bare-metal box): the Docker stack, with per-user
#               bubblewrap isolation, set up by host/provision.sh and run by
#               istota-stack. Needs root.
#
#   Local       Istota in a Lima VM on your Apple silicon Mac: the same stack as a
#               server, with a folder in your home (~/Istota) as its workspace.
#               Installs the Mac's istota-stack and istota into ~/.local/bin and
#               runs `istota-stack create`. Needs Lima (brew install lima); no root.
#
#   The standalone install (--standalone) was removed. An existing one moves
#   into the local VM with `istota-stack import-standalone`.
#
# Server (as root, on the machine that will run it):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash -s -- --server
#
# Local (WITHOUT sudo, on the Mac):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --local
#
# Both install the newest signed release (the highest vX.Y.Z tag), verified
# against the project's release key (host/release-signing-key.pub). --tag and
# --signing-key override them, for a specific release or a fork's releases.
#
# Usage:
#   install.sh [--server | --local] [other flags...]
#     --server             Server install (needs root, Debian 13)
#       --tag TAG          the signed release tag to install (or ISTOTA_RELEASE_TAG);
#                          default: the newest release
#       --signing-key KEY  the release signing key, one line of an SSH public key
#                          (or RELEASE_SIGNING_KEY); default: the one host.env
#                          records, else the project's
#       --terminal-setup   set up in the terminal (`istota-stack setup`) rather
#                          than in a browser
#     --local              Local VM install on an Apple silicon Mac (no root); takes
#                          --tag and --signing-key too, with the same defaults, and
#                          forwards every other flag to `istota-stack create`
#                          (--workspace, --port, --cpus, ...)
#     --help               Show this help
#   With no mode flag on a terminal you are asked to choose; with no mode flag
#   and no terminal, --server is used.
#
# The server path does what host/cloud-init.yaml does: clones the repository
# into /srv/app/istota/src, picks the newest release tag unless --tag names one,
# verifies its signature against the key before running anything from it,
# runs host/provision.sh, then `istota-stack update <tag>` (builds the
# images), and starts the setup in a browser (`systemctl start
# istota-setup`). It prints the address to open, the one-use setup code and
# the certificate fingerprint, and `istota-stack setup-code` prints them
# again. The setup installs the config, builds what it needs, provisions again
# and starts the stack.
#
# With --terminal-setup it runs `istota-stack setup` (the wizard) instead, then
# rebuilds if the config needs it, runs provision.sh again for host.env, and
# `systemctl enable --now istota-stack`. Without a terminal that path stops
# after the build and prints the rest.
#
# Environment overrides:
#   ISTOTA_REPO_URL     Repo to clone (default: https://github.com/istota-project/istota.git)
#   ISTOTA_REPO_BRANCH  Branch --local fetches the Mac scripts from when not run
#                       from a checkout (default: main)
#   ISTOTA_RAW_URL      Where --local fetches the Mac scripts when not run from a
#                       checkout (default: raw.githubusercontent.com, this branch)

set -euo pipefail

REPO_URL="${ISTOTA_REPO_URL:-https://github.com/istota-project/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"
RELEASE_TAG="${ISTOTA_RELEASE_TAG:-}"
SIGNING_KEY="${RELEASE_SIGNING_KEY:-}"
# host/release-signing-key.pub, which is authoritative; tests/test_host_units.py
# holds this copy equal to it.
DEFAULT_SIGNING_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM8wnX7YSjgMETMV8EOf/hQ6J5HpZsw9WfY/r0Glfq4z istota-releases'

STACK=/srv/app/istota
SRC="${STACK}/src"
ALLOWED_SIGNERS="${STACK}/allowed_signers"
HOST_ENV="${STACK}/host.env"

_BOLD="\033[1m"; _BLUE="\033[1;34m"; _GREEN="\033[1;32m"
_YELLOW="\033[1;33m"; _RED="\033[1;31m"; _DIM="\033[2m"; _RESET="\033[0m"
info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
error()   { echo -e "${_RED}ERROR:${_RESET} $*" >&2; }
die()     { error "$@"; exit 1; }
refuse_tag() { error "REFUSE: $*"; exit 65; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }

show_help() {
    sed -n '2,/^$/s/^# \?//p' "$0"
}

# Parse only the flags this script cares about; everything else forwards to
# `istota-stack create` on the local path.
MODE="server"
MODE_EXPLICIT=false
SETUP_IN_TERMINAL=false
FORWARD_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --server)     MODE="server";     MODE_EXPLICIT=true; shift ;;
        --standalone)
            die "--standalone is gone: the standalone install was removed.
  On an Apple silicon Mac, install the local VM: --local --tag <release> --signing-key '<key>'.
  An existing standalone install moves into it with \`istota-stack import-standalone\`
  (docs/getting-started/local-install.md)." ;;
        --local)      MODE="local";      MODE_EXPLICIT=true; shift ;;
        --tag)
            [ $# -ge 2 ] || die "--tag needs a value"
            RELEASE_TAG="$2"; shift 2 ;;
        --signing-key)
            [ $# -ge 2 ] || die "--signing-key needs a value"
            SIGNING_KEY="$2"; shift 2 ;;
        --terminal-setup) SETUP_IN_TERMINAL=true; shift ;;
        --bare|--docker)
            die "$1 is gone: a server is one Debian 13 machine running the stack.
  Use --server (see --help)." ;;
        --help|-h) show_help; exit 0 ;;
        *) FORWARD_ARGS+=("$1"); shift ;;
    esac
done

# --local run from a checkout installs that checkout's Mac scripts.
script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$script_path")" && pwd)"
else
    SCRIPT_DIR=""
fi

repo_root_or_empty() {
    local d="$1"
    if [ -n "$d" ] && [ -f "$d/pyproject.toml" ] && [ -d "$d/src/istota" ]; then
        echo "$d"
    fi
}

REPO_ROOT="$(repo_root_or_empty "$SCRIPT_DIR")"

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

# --- Server ------------------------------------------------------------------

# Copied from host/istota-stack, which is authoritative: nothing from the
# checkout may run before its tag is verified, so this cannot source it.
# tests/test_host_units.py holds the copy byte for byte equal to the original.
# --- release tags ------------------------------------------------------------

# git against the checkout, reading no configuration but this.
verified_git() {
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -C "$SRC" \
        -c gpg.format=ssh \
        -c "gpg.ssh.allowedSignersFile=${ALLOWED_SIGNERS}" \
        -c gpg.openpgp.program=false \
        -c gpg.x509.program=false \
        "$@"
}

check_tag_name() {
    case "$1" in
        ""|-*|*[!A-Za-z0-9._-]*|*..*) refuse_tag "'$1' is not a release tag name." ;;
    esac
}

# Print the object id of a release tag that passes every check, or refuse.
verify_release_tag() {
    local tag="$1" object kind named body
    check_tag_name "$tag"
    if ! object="$(verified_git rev-parse -q --verify "refs/tags/${tag}" 2>/dev/null)"; then
        refuse_tag "${tag} is not a tag in the checkout. Only a signed release tag is deployed, never a branch or a commit."
    fi
    kind="$(verified_git cat-file -t "$object")"
    if [ "$kind" != "tag" ]; then
        refuse_tag "${tag} is a lightweight tag; a release tag is annotated and signed."
    fi
    # Read once and tested as text: a `cat-file | grep -q` under pipefail
    # reads a match as a failure when grep closes the pipe early.
    body="$(verified_git cat-file tag "$object")"
    named="$(awk '/^tag / { print $2; exit } /^$/ { exit }' <<<"$body")"
    if [ "$named" != "$tag" ]; then
        refuse_tag "the ref ${tag} points at a tag object named '${named}'."
    fi
    if ! grep -qx -- '-----BEGIN SSH SIGNATURE-----' <<<"$body"; then
        refuse_tag "${tag} carries no SSH signature."
    fi
    if [ ! -s "$ALLOWED_SIGNERS" ]; then
        refuse_tag "${ALLOWED_SIGNERS} is missing or empty; provision.sh writes it from RELEASE_SIGNING_KEY."
    fi
    if ! verified_git verify-tag "$object" >/dev/null 2>&1; then
        refuse_tag "${tag} is not signed by the key in ${ALLOWED_SIGNERS}."
    fi
    echo "$object"
}

# --- newest release ---
# The newest release among the tag names on stdin, one bare name per line: the
# highest vX.Y.Z by number (v0.10.0 is newer than v0.9.9). Prints nothing and
# fails when no name is one. It only picks: the caller verifies the tag. Not
# `sort -V`, which macOS's sort lacks.
newest_release_tag() {
    local newest
    newest="$(grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//' \
        | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)" || true
    [ -n "$newest" ] || return 1
    echo "v${newest}"
}
# --- end newest release ---

# --- server setup ---
# The first run after the build: in a browser by default, as cloud-init does,
# or the wizard in the terminal with --terminal-setup.

server_setup() {
    if [ "$SETUP_IN_TERMINAL" = true ]; then
        server_setup_in_terminal
    else
        server_setup_in_browser
    fi
}

# What host/cloud-init.yaml's bootstrap does after its build. The unit runs
# `istota-stack setup --web`, which installs the config, builds what it needs,
# provisions again and starts the stack once the page finishes.
server_setup_in_browser() {
    # The unit is skipped once a config exists, so a re-run only updates.
    if [ -e "${STACK}/config/config.toml" ]; then
        section "Done"
        ok "istota is already set up; ${RELEASE_TAG} is installed"
        echo
        exit 0
    fi
    section "Setup"
    systemctl start istota-setup.service
    # The unit mints the code and the certificate first; print its
    # instructions once they exist.
    local _
    for _ in $(seq 1 60); do
        if istota-stack setup-code 2>/dev/null; then
            echo
            echo -e "  ${_DIM}\`istota-stack setup-code\` prints this again; \`journalctl -u istota-setup\` follows the setup.${_RESET}"
            echo
            exit 0
        fi
        # A setup that refused or failed has exited and will never print one.
        systemctl is-active --quiet istota-setup.service || break
        sleep 5
    done
    die "the setup page is not running. \`systemctl status istota-setup\` and \`journalctl -u istota-setup\` say why."
}

server_setup_in_terminal() {
    reattach_tty_if_needed
    if [ ! -t 0 ]; then
        cat <<EOF

No terminal for the setup wizard. On the machine, as root:

  istota-stack setup
  istota-stack check-image || istota-stack update ${RELEASE_TAG}
  ${SRC}/host/provision.sh
  systemctl enable --now istota-stack

EOF
        exit 0
    fi

    section "Setup"
    istota-stack setup
    # The build ran before config.toml existed, so it left Claude Code out.
    local image_rc=0
    istota-stack check-image || image_rc=$?
    if [ "$image_rc" -eq 2 ]; then
        section "Rebuilding ${RELEASE_TAG} for the new config"
        istota-stack update "$RELEASE_TAG"
    elif [ "$image_rc" -ne 0 ]; then
        die "istota-stack check-image failed (exit ${image_rc})."
    fi
    "${SRC}/host/provision.sh"
    systemctl enable --now istota-stack

    section "Done"
    ok "istota ${RELEASE_TAG} is running"
    echo
    echo -e "  ${_BOLD}Check it:${_RESET}  istota doctor"
    echo -e "  ${_BOLD}Update:${_RESET}    istota-stack update"
    echo
    # stdin may now be the terminal; exit rather than read on from it.
    exit 0
}
# --- end server setup ---

host_env_key() {
    [ -f "$HOST_ENV" ] || return 0
    sed -n 's/^RELEASE_SIGNING_KEY=//p' "$HOST_ENV" | tail -n 1
}

# The key every tag is verified against. host.env holds it once written, and
# then wins over the project's; a different key given here is refused rather
# than swapped in.
server_signing_key() {
    local recorded
    recorded="$(host_env_key)"
    if [ -n "$recorded" ]; then
        if [ -n "$SIGNING_KEY" ] && [ "$SIGNING_KEY" != "$recorded" ]; then
            die "${HOST_ENV} already names a different RELEASE_SIGNING_KEY; edit it there if the key changed."
        fi
        echo "$recorded"
    else
        echo "${SIGNING_KEY:-$DEFAULT_SIGNING_KEY}"
    fi
}

run_server() {
    if [ ${#FORWARD_ARGS[@]} -gt 0 ]; then
        die "The server install does not take ${FORWARD_ARGS[*]} (see --help)."
    fi
    [ "$(id -u)" -eq 0 ] || die "The server install needs root. Re-run under sudo."
    if [ ! -r /etc/debian_version ] || ! command -v apt-get >/dev/null 2>&1; then
        die "The server install is for Debian 13."
    fi

    local recorded
    recorded="$(host_env_key)"
    SIGNING_KEY="$(server_signing_key)"
    case "$SIGNING_KEY" in
        *$'\n'*) die "The signing key is one line of an SSH public key." ;;
    esac

    install -d -m 0755 "$STACK"
    if [ -z "$recorded" ]; then
        printf 'RELEASE_SIGNING_KEY=%s\n' "$SIGNING_KEY" >> "$HOST_ENV"
        chmod 0644 "$HOST_ENV"
    fi

    info "Installing git"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git openssh-client ca-certificates

    # The file provision.sh writes from host.env, in the same form.
    printf 'releases namespaces="git" %s\n' "$SIGNING_KEY" > "$ALLOWED_SIGNERS"
    chmod 0644 "$ALLOWED_SIGNERS"
    if [ -d "${SRC}/.git" ]; then
        info "Fetching tags into ${SRC}"
        verified_git fetch --quiet --tags origin || die "fetching tags into ${SRC} failed."
    else
        info "Cloning ${REPO_URL} into ${SRC}"
        git clone --quiet --no-checkout "$REPO_URL" "$SRC"
    fi
    if [ -z "$RELEASE_TAG" ]; then
        RELEASE_TAG="$(verified_git tag --list | newest_release_tag)" \
            || die "${REPO_URL} has no release tag (vX.Y.Z). Name the release to install with --tag."
    fi
    section "Server install: ${RELEASE_TAG}"
    local object
    if ! object="$(verify_release_tag "$RELEASE_TAG")"; then
        error "nothing was installed. --tag picks another release."
        exit 65
    fi
    verified_git checkout --quiet --detach "${object}^{commit}"
    ok "${RELEASE_TAG} is signed by the release key"

    section "Provisioning the host"
    ISTOTA_REPO_URL="$REPO_URL" "${SRC}/host/provision.sh"

    section "Building ${RELEASE_TAG}"
    istota-stack update "$RELEASE_TAG"

    server_setup
}

# --- Local VM (macOS) ---------------------------------------------------------

# The newest release on the remote, read without a clone: the VM verifies it.
newest_remote_release_tag() {
    local listing
    listing="$(git ls-remote --tags --refs "$REPO_URL" 'v*')" \
        || die "could not list the release tags at ${REPO_URL}. Name the release to install with --tag."
    sed 's|^.*refs/tags/||' <<<"$listing" | newest_release_tag \
        || die "${REPO_URL} has no release tag (vX.Y.Z). Name the release to install with --tag."
}

# One file of the Mac half, from the checkout this ran from or else from the
# branch this script was fetched from, put in place through a rename. Only to
# run `create`: after the VM's first verified update, istota-stack replaces
# itself and the template with the copies from the tag the VM verified.
install_mac_file() {
    local source="$1" target="$2" mode="$3" tmp
    tmp="$(mktemp "$(dirname "$target")/.$(basename "$target").XXXXXX")"
    if [ -n "$REPO_ROOT" ]; then
        cp "${REPO_ROOT}/${source}" "$tmp"
    elif ! curl -fsSL "${ISTOTA_RAW_URL:-https://raw.githubusercontent.com/istota-project/istota/${REPO_BRANCH}}/${source}" -o "$tmp"; then
        rm -f "$tmp"
        die "could not fetch ${source}."
    fi
    chmod "$mode" "$tmp"
    mv -f "$tmp" "$target"
}

run_local() {
    [ "$(id -u)" -ne 0 ] || die "The local install runs as you, not root. Re-run it without sudo."
    if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
        die "The local install is for an Apple silicon Mac. A Debian 13 machine takes --server."
    fi
    if ! command -v limactl >/dev/null 2>&1; then
        error "Lima is not installed: brew install lima, then re-run this."
        exit 69
    fi
    if [ -z "$RELEASE_TAG" ]; then
        command -v git >/dev/null 2>&1 \
            || die "git is needed to find the newest release: xcode-select --install, or name the release with --tag."
        RELEASE_TAG="$(newest_remote_release_tag)"
    fi

    local bin="${ISTOTA_MAC_BIN_DIR:-$HOME/.local/bin}"
    local data="${XDG_DATA_HOME:-$HOME/.local/share}/istota"
    section "Local install: ${RELEASE_TAG}"
    mkdir -p "$bin" "$data"
    install_mac_file host/mac/istota-stack "${bin}/istota-stack" 0755
    install_mac_file host/mac/istota "${bin}/istota" 0755
    install_mac_file host/lima/istota.yaml "${data}/istota.yaml" 0644
    ok "istota-stack and istota in ${bin}"
    case ":${PATH}:" in
        *":${bin}:"*) ;;
        *) warn "${bin} is not on your PATH; add it in your shell's profile: export PATH=\"${bin}:\$PATH\"" ;;
    esac

    # The setup wizard runs in the VM and asks on this terminal.
    reattach_tty_if_needed
    local rc=0
    # Without --signing-key, create takes the key it recorded, else the project's.
    local key_args=()
    [ -z "$SIGNING_KEY" ] || key_args=(--signing-key "$SIGNING_KEY")
    "${bin}/istota-stack" create --tag "$RELEASE_TAG" "${key_args[@]+"${key_args[@]}"}" \
        "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}" || rc=$?
    # stdin may now be the terminal; exit rather than read on from it.
    exit "$rc"
}

# --- Interactive mode selection --------------------------------------------

# When no mode flag was given and we have a terminal, ask which install shape
# the user wants. Sets $MODE. With no terminal, leaves the default (server).
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
        return 0                                               # no terminal: keep the default
    fi

    local root_note=""
    if [ "$(id -u)" -eq 0 ]; then
        root_note="  ${_DIM}(run without sudo for this option)${_RESET}"
    fi

    echo
    echo -e "${_BOLD}How do you want to install Istota?${_RESET}"
    echo
    echo -e "  ${_BOLD}1) Server${_RESET} ${_DIM}(default)${_RESET}"
    echo "     One install on a Debian 13 machine of its own, multi-user, with"
    echo "     per-user bubblewrap isolation. Needs root."
    echo
    echo -e "  ${_BOLD}2) Local VM${_RESET}${root_note}"
    echo "     Istota in a Lima VM on your Apple silicon Mac: the server's stack,"
    echo "     sandbox included, with ~/Istota as its workspace. Needs Lima."
    echo
    local choice=""
    read -rp "  Choose [1/2, default 1]: " choice < "$tty_src" || true
    case "$choice" in
        2|l|L|local|Local)           MODE="local" ;;
        ""|1|server|Server)          MODE="server" ;;
        *) warn "Unrecognized choice '$choice'; defaulting to Server."; MODE="server" ;;
    esac
}

if [ "$MODE_EXPLICIT" = false ]; then
    prompt_install_mode
fi

if [ "$SETUP_IN_TERMINAL" = true ] && [ "$MODE" != server ]; then
    die "--terminal-setup is for the server install."
fi

case "$MODE" in
    server)     run_server ;;
    local)      run_local ;;
    *)          die "Unknown mode: $MODE" ;;
esac
