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
#   Standalone  Single-user install on your own machine (macOS or Linux). No
#               server, no Nextcloud, no login, no sandbox. You chat through
#               the local web UI and the REPL; everything runs as your user in
#               one process (istota serve). Deprecated, and kept until the
#               desktop install replaces it. Only give it content you trust.
#
# Server (as root, on the machine that will run it):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh \
#       | sudo bash -s -- --server --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases'
#
# Local (WITHOUT sudo, on the Mac):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh \
#       | bash -s -- --local --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases'
#
# Standalone (WITHOUT sudo; it installs into your user):
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --standalone
#
# Usage:
#   install.sh [--server | --local | --standalone] [other flags...]
#     --server             Server install (needs root, Debian 13)
#       --tag TAG          the signed release tag to install (or ISTOTA_RELEASE_TAG)
#       --signing-key KEY  the release signing key, one line of an SSH public key
#                          (or RELEASE_SIGNING_KEY; read from host.env once it has one)
#     --local              Local VM install on an Apple silicon Mac (no root); takes
#                          --tag and --signing-key too, and forwards every other flag
#                          to `istota-stack create` (--workspace, --port, --cpus, ...)
#     --standalone         Local single-user install (uv tool install + istota setup)
#     --help               Show this help
#   With no mode flag on a terminal you are asked to choose; with no mode flag
#   and no terminal, --server is used.
#
# The server path does what host/cloud-init.yaml does, then the steps after it:
# clones the repository into /srv/app/istota/src, verifies the release tag's
# signature against the key before running anything from it, runs
# host/provision.sh, then `istota-stack update <tag>` (builds the images),
# `istota-stack setup` (the wizard), provision.sh again for host.env, and
# `systemctl enable --now istota-stack`. Without a terminal it stops after the
# build and prints the rest.
#
# Standalone forwards every other flag to `istota setup` (--yes, --force,
# --workspace, --port, ...). With a non-Anthropic (native) brain:
#   ... --standalone --brain native --native-model <id> --native-api-key <key>
# The key is written to ~/.config/istota/istota.env, never to config.toml.
#
# Environment overrides:
#   ISTOTA_REPO_URL     Repo to clone (default: https://github.com/istota-project/istota.git)
#   ISTOTA_REPO_BRANCH  Branch or tag the standalone clone takes (default: main)
#   ISTOTA_CLONE_DIR    Where standalone clones the repo (default:
#                       ${XDG_DATA_HOME:-~/.local/share}/istota/src)
#   ISTOTA_RAW_URL      Where --local fetches the Mac scripts when not run from a
#                       checkout (default: raw.githubusercontent.com, this branch)

set -euo pipefail

REPO_URL="${ISTOTA_REPO_URL:-https://github.com/istota-project/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"
CLONE_DIR="${ISTOTA_CLONE_DIR:-}"
RELEASE_TAG="${ISTOTA_RELEASE_TAG:-}"
SIGNING_KEY="${RELEASE_SIGNING_KEY:-}"

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
# `istota setup` on the standalone path.
MODE="server"
MODE_EXPLICIT=false
FORWARD_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --server)     MODE="server";     MODE_EXPLICIT=true; shift ;;
        --standalone) MODE="standalone"; MODE_EXPLICIT=true; shift ;;
        --local)      MODE="local";      MODE_EXPLICIT=true; shift ;;
        --tag)
            [ $# -ge 2 ] || die "--tag needs a value"
            RELEASE_TAG="$2"; shift 2 ;;
        --signing-key)
            [ $# -ge 2 ] || die "--signing-key needs a value"
            SIGNING_KEY="$2"; shift 2 ;;
        --bare|--docker)
            die "$1 is gone: a server is one Debian 13 machine running the stack.
  Use --server --tag <release> --signing-key '<key>' (see --help)." ;;
        --help|-h) show_help; exit 0 ;;
        *) FORWARD_ARGS+=("$1"); shift ;;
    esac
done

# A standalone install from a checkout uses that checkout rather than cloning.
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
    local tag="$1" object kind named
    check_tag_name "$tag"
    if ! object="$(verified_git rev-parse -q --verify "refs/tags/${tag}" 2>/dev/null)"; then
        refuse_tag "${tag} is not a tag in the checkout. Only a signed release tag is deployed, never a branch or a commit."
    fi
    kind="$(verified_git cat-file -t "$object")"
    if [ "$kind" != "tag" ]; then
        refuse_tag "${tag} is a lightweight tag; a release tag is annotated and signed."
    fi
    named="$(verified_git cat-file tag "$object" | awk '/^tag / { print $2; exit } /^$/ { exit }')"
    if [ "$named" != "$tag" ]; then
        refuse_tag "the ref ${tag} points at a tag object named '${named}'."
    fi
    if ! verified_git cat-file tag "$object" | grep -qx -- '-----BEGIN SSH SIGNATURE-----'; then
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

host_env_key() {
    [ -f "$HOST_ENV" ] || return 0
    sed -n 's/^RELEASE_SIGNING_KEY=//p' "$HOST_ENV" | tail -n 1
}

run_server() {
    [ "$(id -u)" -eq 0 ] || die "The server install needs root. Re-run under sudo."
    if [ ! -r /etc/debian_version ] || ! command -v apt-get >/dev/null 2>&1; then
        die "The server install is for Debian 13."
    fi
    [ -n "$RELEASE_TAG" ] || die "Name the release to install: --tag vX.Y.Z (or ISTOTA_RELEASE_TAG)."

    # The key every tag is verified against. host.env holds it once written;
    # a different key on the command line is refused rather than swapped in.
    local recorded
    recorded="$(host_env_key)"
    if [ -n "$recorded" ]; then
        if [ -n "$SIGNING_KEY" ] && [ "$SIGNING_KEY" != "$recorded" ]; then
            die "${HOST_ENV} already names a different RELEASE_SIGNING_KEY; edit it there if the key changed."
        fi
        SIGNING_KEY="$recorded"
    fi
    [ -n "$SIGNING_KEY" ] \
        || die "Give the release signing key: --signing-key 'ssh-ed25519 AAAA... releases' (or RELEASE_SIGNING_KEY)."
    case "$SIGNING_KEY" in
        *$'\n'*) die "The signing key is one line of an SSH public key." ;;
    esac

    section "Server install: ${RELEASE_TAG}"
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
    local object
    object="$(verify_release_tag "$RELEASE_TAG")"
    verified_git checkout --quiet --detach "${object}^{commit}"
    ok "${RELEASE_TAG} is signed by the release key"

    section "Provisioning the host"
    ISTOTA_REPO_URL="$REPO_URL" "${SRC}/host/provision.sh"

    section "Building ${RELEASE_TAG}"
    istota-stack update "$RELEASE_TAG"

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
    echo -e "  ${_BOLD}Update:${_RESET}    istota-stack update <tag>"
    echo
    # stdin may now be the terminal; exit rather than read on from it.
    exit 0
}

# --- Local VM (macOS) ---------------------------------------------------------

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
    [ -n "$RELEASE_TAG" ] || die "Name the release to install: --tag vX.Y.Z (or ISTOTA_RELEASE_TAG)."
    [ -n "$SIGNING_KEY" ] \
        || die "Give the release signing key: --signing-key 'ssh-ed25519 AAAA... releases' (or RELEASE_SIGNING_KEY)."

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
    "${bin}/istota-stack" create --tag "$RELEASE_TAG" --signing-key "$SIGNING_KEY" \
        "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}" || rc=$?
    # stdin may now be the terminal; exit rather than read on from it.
    exit "$rc"
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
write_install_record() {
    # Record install provenance for `istota update` (standalone only). JSON with
    # the source checkout, the extras string, the git ref, and the method — the
    # updater re-runs `git fetch/reset` here + `uv tool install --reinstall`.
    #
    # Path matches where `istota setup` writes config.toml and where load_config /
    # the updater look ($HOME/.config/istota — the codebase hardcodes ~/.config,
    # so do NOT honor XDG_CONFIG_HOME here or the updater won't find this file).
    local source="$1" extras="$2" ref="$3"
    # Prefer the checkout's actual branch over the clone-time default, so an
    # install from a local checkout on a feature branch doesn't get yanked onto
    # main on the first update. Detached HEAD / failure falls back to the arg.
    local actual_ref
    actual_ref="$(git -C "$source" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ -n "$actual_ref" ] && [ "$actual_ref" != "HEAD" ]; then
        ref="$actual_ref"
    fi
    # Update channel: track tagged releases by default. A checkout on a non-main
    # feature branch is a developer install — track that branch instead so
    # `istota update` doesn't reset it onto an unrelated release tag. Override
    # with ISTOTA_UPDATE_CHANNEL=main|stable.
    local channel="${ISTOTA_UPDATE_CHANNEL:-stable}"
    if [ -z "${ISTOTA_UPDATE_CHANNEL:-}" ] && [ "$ref" != "main" ]; then
        channel="main"
    fi
    local config_dir="$HOME/.config/istota"
    mkdir -p "$config_dir"
    cat > "$config_dir/install.json" <<EOF
{
  "method": "checkout",
  "source": "$source",
  "extras": "$extras",
  "ref": "$ref",
  "channel": "$channel"
}
EOF
    ok "Recorded install provenance at $config_dir/install.json (channel: $channel)"
}

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
    # The clone must be DURABLE, not /tmp: `istota update` re-fetches + reinstalls
    # from this exact directory, and a /tmp clone is wiped on reboot (which would
    # break every update after the first). Land it under XDG data home.
    : "${CLONE_DIR:=${XDG_DATA_HOME:-$HOME/.local/share}/istota/src}"
    ensure_repo
    { [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/pyproject.toml" ]; } \
        || die "Could not locate the istota source (looked at '$REPO_ROOT')."

    maybe_build_web_static "$REPO_ROOT"

    # Install every module the local shape ships. These are all light on disk —
    # money adds beancount/beanquery/weasyprint, location adds geopy — so there's
    # nothing to gate at install time. What's actually *enabled* is a config
    # choice made in `istota setup` below, not a packaging one. (weasyprint's
    # native libs are only needed for invoice-PDF rendering, and only then; on
    # macOS that one command needs `brew install pango`.)
    local extras="local,money,location"

    # --reinstall (implies --refresh) forces a fresh build. Without it, uv can
    # reuse a cached wheel keyed on the package version — and this project ships
    # code to `main` between version bumps, so a same-version reinstall would
    # silently install stale bits from a prior run.
    info "Installing istota[${extras}] from $REPO_ROOT"
    uv tool install --force --reinstall "${REPO_ROOT}[${extras}]"
    export PATH="$HOME/.local/bin:$PATH"
    command -v istota >/dev/null 2>&1 \
        || die "istota installed but not on PATH. Run 'uv tool update-shell' (or add ~/.local/bin to PATH), open a new shell, then: istota setup"
    ok "istota installed"

    section "Running setup"
    istota setup "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"

    # Record install provenance so `istota update` knows where to pull from — a
    # uv-tool-installed package retains no pointer back to its source checkout.
    write_install_record "$REPO_ROOT" "$extras" "$REPO_BRANCH"

    section "Done"
    ok "Standalone install complete"
    echo
    echo -e "  ${_BOLD}Start it:${_RESET}  istota serve"
    echo -e "             ${_DIM}then open the printed http://127.0.0.1:<port>/istota URL (no login)${_RESET}"
    echo -e "  ${_BOLD}REPL:${_RESET}      istota repl"
    echo -e "  ${_BOLD}Docs:${_RESET}      docs/getting-started/standalone-install.md"
    echo
    echo -e "  ${_DIM}Standalone runs unsandboxed as your user — only give it content you trust.${_RESET}"
    echo
    # Explicit: run_standalone reattached stdin to /dev/tty (for `istota setup`),
    # so exit rather than let bash try to read another command from the terminal.
    exit 0
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
    echo "     per-user bubblewrap isolation. Needs root, --tag and --signing-key."
    echo
    echo -e "  ${_BOLD}2) Local VM${_RESET}${root_note}"
    echo "     Istota in a Lima VM on your Apple silicon Mac: the server's stack,"
    echo "     sandbox included, with ~/Istota as its workspace. Needs Lima,"
    echo "     --tag and --signing-key."
    echo
    echo -e "  ${_BOLD}3) Standalone${_RESET}${root_note}"
    echo "     Single-user install on your own machine (macOS or Linux). No"
    echo "     server, no Nextcloud, no login, no sandbox. You chat through the"
    echo "     local web UI and the REPL; everything runs as you in one process."
    echo "     Unsandboxed: only give it content and instructions you trust."
    echo
    local choice=""
    read -rp "  Choose [1/2/3, default 1]: " choice < "$tty_src" || true
    case "$choice" in
        2|l|L|local|Local)           MODE="local" ;;
        3|s|S|standalone|Standalone) MODE="standalone" ;;
        ""|1|server|Server)          MODE="server" ;;
        *) warn "Unrecognized choice '$choice'; defaulting to Server."; MODE="server" ;;
    esac
}

if [ "$MODE_EXPLICIT" = false ]; then
    prompt_install_mode
fi

case "$MODE" in
    server)     run_server ;;
    local)      run_local ;;
    standalone) run_standalone ;;
    *)          die "Unknown mode: $MODE" ;;
esac
