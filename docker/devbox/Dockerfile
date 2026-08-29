FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# Layer 1: system + diagnostic toolkit. Kept in one apt invocation so the
# image stays slim — anything else the agent needs can be installed at
# runtime with apt-get / pip / cargo / npm.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config \
        python3 python3-venv python3-pip \
        git curl wget jq yq sqlite3 ripgrep fd-find htop less \
        ffmpeg imagemagick pandoc poppler-utils tesseract-ocr \
        dnsutils whois iputils-ping traceroute mtr-tiny \
        nmap netcat-openbsd openssl httpie iproute2 \
        tcpdump iperf3 netbase \
        sudo ca-certificates locales \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Layer 2: Node 24 via NodeSource. Kept on its own layer so apt cache
# changes here don't bust the heavier base layer above.
#
# 24 is the active LTS line. Node 26 has been out since April and does not
# become LTS until October, and this is a box people build releases on.
#
# Note that docker/istota/Dockerfile and the Ansible role still install Node 20
# — that is the runtime the shipped web bundle is built and served with, and
# moving it is a change to the deployed artifact rather than to this box.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Layer 3: Go. Pinned to a versioned URL so rebuilds are reproducible — bump
# deliberately, not opportunistically.
#
# 1.26.7 rather than 1.27.0: both were published on the same day and 1.27.0 is
# the .0 of a line that is days old, while 1.26 is the mature supported line.
# Lagging one minor costs nothing here — GOTOOLCHAIN defaults to `auto`, so a
# go.mod requiring a newer toolchain fetches it rather than failing.
#
# The asset is chosen per architecture, the same way docker/istota/Dockerfile
# picks its forge assets, and for the same reason: this image had a hardcoded
# linux-amd64 tarball, which meant an arm64 developer machine could only build
# it under emulation — tens of minutes — so the only tier that tests this image
# ran once before a release, if then (ISSUE-280).
#
# The arch comes from `dpkg --print-architecture` rather than BuildKit's
# TARGETARCH: dpkg's names are the ones Go and both forge projects already use
# for their assets, and it is correct under the legacy builder, where TARGETARCH
# is simply empty.
#
# Two checksums per artifact rather than one is the real cost of this change,
# and the honest one — the existing pins are a verified-download guard rather
# than a version floor, and that property has to survive. Go publishes no
# checksum file alongside the tarball, so this layer had no verification at all
# before; it does now. An architecture with no pinned checksum fails the build
# rather than going unverified.
ARG GO_VERSION=1.26.7
ARG GO_SHA256_AMD64=ffb5f8de10c62550dfddab66b36b57030721e0a44a3218e9e1181d7b59f121ca
ARG GO_SHA256_ARM64=5a4ec883379d51ee9ce1040d5e87f8d35e20387574dd8c947feb01eabc3c1b37
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) go_sha="${GO_SHA256_AMD64}" ;; \
        arm64) go_sha="${GO_SHA256_ARM64}" ;; \
        *) echo "no pinned go checksum for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz" \
        -o /tmp/go.tar.gz; \
    echo "${go_sha}  /tmp/go.tar.gz" | sha256sum -c -; \
    tar -C /usr/local -xzf /tmp/go.tar.gz; \
    rm /tmp/go.tar.gz; \
    /usr/local/go/bin/go version
ENV PATH="/usr/local/go/bin:${PATH}"

# Non-root user with passwordless sudo (it's the user's box; sudo unlocks
# `apt install` at runtime). Home is the persistent volume mount point.
#
# The uid and gid are build args because the daemon and this container share a
# filesystem — the per-user repos mount — and a shared mount with two different
# uids on it fails in both directions. The daemon's `git worktree add` produces
# 0755 daemon-owned directories that `dev` cannot create a `node_modules/`
# inside, so every build fails EACCES on its first write; and once that is
# worked around, a tree the container wrote is one the daemon's
# `git worktree remove` cannot unlink, so every worktree that ever ran a build
# becomes permanently unreapable. Neither has an error message anywhere that
# says "uid".
#
# The deploy passes the daemon's own uid and gid, read with `getent`. They
# default to 1000 so a build with no args reproduces the image this recipe
# built before they existed, exactly. Nothing is lost in distribution by
# building per host: the Ansible role has always built this image on the
# machine it runs on, and hashes the directory's contents to trigger a rebuild.
#
# The uid is checked against the base image's own accounts first. Debian
# allocates system uids from 100 upward and so does the istota role, so a
# collision is reachable — and `useradd`'s own "UID 42 is not unique" arrives at
# deploy time, in a build log, naming neither the arg nor the account it hit.
#
# One consequence is worth naming rather than discovering: the preflight also
# refuses `DEV_UID=0`, which is the answer for a compose deployment, whose
# istota service runs as root. Nothing here supports that yet — this ships bare
# metal only — and reusing the existing root account rather than creating a
# second one is a different recipe, not a different argument. The refusal is at
# least loud, and it says which account it collided with.
ARG DEV_UID=1000
ARG DEV_GID=1000
RUN set -eux; \
    if taken="$(getent passwd "${DEV_UID}")"; then \
        echo "DEV_UID=${DEV_UID} is already used by: ${taken}" >&2; \
        echo "pick a different uid for the istota daemon, or rebuild on a base without it" >&2; \
        exit 1; \
    fi; \
    if taken="$(getent group "${DEV_GID}")"; then \
        echo "DEV_GID=${DEV_GID} is already used by: ${taken}" >&2; \
        exit 1; \
    fi; \
    groupadd -g "${DEV_GID}" dev; \
    useradd -m -s /bin/bash -d /home/dev -u "${DEV_UID}" -g "${DEV_GID}" dev; \
    echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev; \
    chmod 0440 /etc/sudoers.d/dev

# Layer 4: uv, at a system path.
#
# It used to be `curl https://astral.sh/uv/install.sh | sh` as `dev`, which put
# it in /home/dev/.local/bin, and rustup beside it in /home/dev/.cargo. That is
# the *volume* mount point: Docker seeds a named volume from the image only
# when the volume is empty at container-create time, so both survived a first
# boot and nothing else. `devbox reset` empties the volume and restarts the
# container — a restart, which copies nothing — so uv and the whole Rust
# toolchain went away permanently, from an operation whose own name promises a
# clean box rather than a smaller one (ISSUE-336).
#
# So uv comes from the upstream image, pinned to a release the way
# `docker/test/Dockerfile` pins it (`docker/istota/Dockerfile` still tracks
# `:latest`), and lands outside the mount where nothing at runtime can mask or
# delete it. istota is a uv project and `uv`/`uvx`/`pip` are in
# `config.DEFAULT_SHIM_COMMANDS`, so on a devbox-backed deployment a missing uv
# is not a missing tool in a scratch box — it is `uv sync` failing inside a
# shim, at a remove from anywhere the reader would look.
#
# Rust is deliberately *not* here. Nothing in istota is Rust, and rustup with a
# minimal profile is ~600 MB of image for a toolchain the box may never use.
# `cargo`, `rustc` and `rustup` stay shimmed — routing is still correct, the
# container is exactly where they belong — and a task that wants them installs
# them with rustup at the time. That install lands in /home/dev and dies with
# the next reset, which is now the documented boundary rather than a surprise.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

USER dev
WORKDIR /home/dev

# Where a *user*-installed toolchain lands, kept on PATH on purpose: rustup
# writes to ~/.cargo, `uv tool install` to ~/.local/bin, and both are inside
# the volume. Empty on a fresh box, which is correct — see Layer 4.
ENV PATH="/home/dev/.cargo/bin:/home/dev/.local/bin:${PATH}"

# Layer 5: forge CLIs. The real `gh` and `glab` live under
# /usr/local/lib/istota_forge/ and are reached only through the wrapper — the
# same src/istota/forge_cli.py the sandbox uses, vendored here because Docker
# cannot COPY from outside its build context (scripts/sync-devbox-lib.sh keeps
# the copy honest and a test fails when the two drift).
#
# Pinned to the exact versions the spec's reconnaissance stage measured against
# (gh 2.98.0, glab 1.114.0), with the published sha256 verified before the
# archive is opened. Trixie packages both now, where bookworm packaged only gh,
# but they are 2.46.0 and 1.53.0 — still far behind the versions measured there
# — so these keep coming from the vendors' release artifacts rather than apt.
# Same versions as docker/istota/Dockerfile and the Ansible role;
# tests/test_ansible_forge_cli_install.py holds the three equal, so bump them
# together.
#
# Extracted from the .deb rather than installed with it: dpkg would put a
# second, real `gh` at /usr/bin/gh, and the whole point is that the only `gh`
# on PATH is the wrapper.
#
# The `dpkg-deb | tar` pipeline has no pipefail under Docker's default /bin/sh
# (dash), so a dpkg-deb that fails partway leaves tar exiting 0 over a
# truncated stream. The `--version` calls at the end of this layer are what
# actually catches that — a truncated ELF cannot exec — so do not remove them
# thinking they are decorative.
#
# Per-architecture assets and checksums, matching docker/istota/Dockerfile
# exactly — same `dpkg --print-architecture` source, same case statement, same
# refusal on an architecture with no pin (ISSUE-280). The two files pinning the
# same six values is what `tests/test_docker_forge_clis.py` holds.
USER root
ARG GH_VERSION=2.98.0
ARG GH_DEB_SHA256_AMD64=f65a3fa2fa0eb2e97c445ee3f5e087a40aae03b64847f45a8f13805e504535d6
ARG GH_DEB_SHA256_ARM64=bbc4ac7964c2a091fd555cd1758d10a7cfcfdc472e405f0b0fb958f05d535cb6
ARG GLAB_VERSION=1.114.0
ARG GLAB_DEB_SHA256_AMD64=6df680f88a2a45700d1dd6a08a44a74838473ef3dcc5cb1ad243f4d1fd704ad0
ARG GLAB_DEB_SHA256_ARM64=c2bcb44cc10d849cccb879e687f3b9e102a3415c3d02f0cecfd9ce1442519350
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) gh_sha="${GH_DEB_SHA256_AMD64}"; glab_sha="${GLAB_DEB_SHA256_AMD64}" ;; \
        arm64) gh_sha="${GH_DEB_SHA256_ARM64}"; glab_sha="${GLAB_DEB_SHA256_ARM64}" ;; \
        *) echo "no pinned gh/glab checksum for ${arch}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /usr/local/lib/istota_forge; \
    curl -fsSL -o /tmp/gh.deb \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.deb"; \
    echo "${gh_sha}  /tmp/gh.deb" | sha256sum -c -; \
    dpkg-deb --fsys-tarfile /tmp/gh.deb \
        | tar -xO ./usr/bin/gh > /usr/local/lib/istota_forge/gh; \
    curl -fsSL -o /tmp/glab.deb \
        "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${GLAB_VERSION}/glab_${GLAB_VERSION}_linux_${arch}.deb"; \
    echo "${glab_sha}  /tmp/glab.deb" | sha256sum -c -; \
    dpkg-deb --fsys-tarfile /tmp/glab.deb \
        | tar -xO ./usr/bin/glab > /usr/local/lib/istota_forge/glab; \
    rm -f /tmp/gh.deb /tmp/glab.deb; \
    chmod 0755 /usr/local/lib/istota_forge/gh /usr/local/lib/istota_forge/glab; \
    /usr/local/lib/istota_forge/gh --version; \
    /usr/local/lib/istota_forge/glab --version

# Layer 6: the wrapper, the credential helper, and the seeded CLI config.
#
# The wrapper is installed under four names. `gh` and `glab` are the live ones;
# `github-api` and `gitlab-api` are the retired REST wrappers, kept so a cached
# habit or an old CRON.md job gets the one-line explanation instead of "command
# not found". forge_from_argv0 dispatches on argv[0], so these are copies
# rather than symlinks.
COPY lib/istota_devbox_client.py /usr/local/lib/istota_devbox/istota_devbox_client.py
COPY lib/istota_forge_cli.py /usr/local/lib/istota_forge/istota_forge_cli.py
COPY scripts/git-credential-istota /usr/local/bin/git-credential-istota
COPY etc/gitconfig /etc/gitconfig
RUN set -eux; \
    for name in gh glab github-api gitlab-api; do \
        cp /usr/local/lib/istota_forge/istota_forge_cli.py "/usr/local/bin/$name"; \
        chmod 0755 "/usr/local/bin/$name"; \
    done; \
    chmod 0755 /usr/local/bin/git-credential-istota; \
    chmod 0644 /usr/local/lib/istota_devbox/istota_devbox_client.py \
        /usr/local/lib/istota_forge/istota_forge_cli.py \
        /etc/gitconfig

# The policy the wrapper reads when there is none beside it, which is the case
# here — policy_path() falls back to /etc/istota-forge/policy.json. Generated
# by seed-forge-policy from the vendored wrapper's own baseline, so the
# container and the sandbox cannot disagree about the *baseline* deny list.
# They can still differ at the edges: `developer.forge_cli_extra_denied` and
# `forge_cli_permit` are read per task by the sandbox's setup_env and are not
# plumbed into this build, so an operator's own additions do not reach the
# container. Said plainly here because a deny list that silently does not
# apply reads exactly like one that does.
#
# On modes: the container gives `dev` passwordless sudo (above), so nothing
# here is a boundary — the boundary in the devbox is the token's own scope, as
# it already is for git. What the modes buy is the accident guard.
#
# Only the two `config.yml` files are chowned to `dev`, not the directories.
# glab refuses to start unless config.yml is exactly 0600 *and* readable by
# the running user, which root ownership cannot satisfy; the directories have
# no such constraint, so they stay root-owned. Chowning the directories too
# would let `cp` over the seeded file without sudo, and gh expands `aliases`
# from config.yml before command dispatch — which is the whole reason the file
# is seeded rather than left absent.
#
# Both behaviours are measured against the pinned versions, not assumed: glab
# 1.114 runs fine with an empty 0600 config.yml and writes nothing back to it.
COPY scripts/seed-forge-policy /usr/local/lib/istota_forge/seed-forge-policy
RUN set -eux; \
    mkdir -p /etc/istota-forge/gh /etc/istota-forge/glab /etc/istota-forge/data; \
    : > /etc/istota-forge/gh/config.yml; \
    : > /etc/istota-forge/glab/config.yml; \
    chown dev:dev /etc/istota-forge/gh/config.yml /etc/istota-forge/glab/config.yml; \
    chmod 0755 /etc/istota-forge/gh /etc/istota-forge/glab; \
    chmod 0600 /etc/istota-forge/gh/config.yml /etc/istota-forge/glab/config.yml; \
    chmod 0555 /etc/istota-forge/data; \
    python3 /usr/local/lib/istota_forge/seed-forge-policy; \
    chmod 0644 /etc/istota-forge/policy.json

# Layer 7: the exec transport.
#
# The devbox stops being a `docker exec` target and becomes a service host. A
# server listens on a Unix socket in a per-user directory the istota side also
# sees; a client on that side sends one request and gets the command's real
# exit status back. That replaces `docker exec` through the Docker-API proxy,
# which runs the command and loses its status — the CLI's flow is create,
# start, *inspect*, and the proxy evicts the exec id before relaying the start,
# so by inspect time there is nothing to inspect and every shimmed command
# reports failure whatever it did.
#
# Three files, and the split is the same one `istota_forge_cli.py` already has:
# the protocol module is vendored from `src/istota/devbox_exec_protocol.py`
# because Docker cannot COPY from outside its build context, and
# `scripts/sync-devbox-lib.sh` is what keeps the copy honest. It goes under
# /usr/local/lib/istota_devbox_exec/ rather than beside the server, because the
# server's own module search names that directory — deliberately, rather than
# reading a path out of the environment, in a process whose whole job is
# refusing paths.
#
# `istota-exec-run` is the container's CMD below, not the server: a server as
# PID 1 makes a server crash take every concurrent build with it, and a `sleep`
# as PID 1 neither restarts anything nor reaps what reparents to it. The
# per-user service is to be declared with `init: true` so tini does the
# reaping — that half lands with the deployment template, and until it does
# this script is PID 1 and forwards the stop signal itself.
#
# The two checks at the end are a build-time smoke, and neither is decorative —
# `--help` is what proves the vendored module is importable at the path the
# server looks for it at, which a `COPY` cannot tell you.
COPY lib/istota_devbox_exec_protocol.py /usr/local/lib/istota_devbox_exec/istota_devbox_exec_protocol.py
COPY scripts/istota-exec-serve /usr/local/bin/istota-exec-serve
COPY scripts/istota-exec-run /usr/local/bin/istota-exec-run
RUN set -eux; \
    chmod 0644 /usr/local/lib/istota_devbox_exec/istota_devbox_exec_protocol.py; \
    chmod 0755 /usr/local/bin/istota-exec-serve /usr/local/bin/istota-exec-run; \
    /usr/local/bin/istota-exec-serve --help > /dev/null; \
    sh -n /usr/local/bin/istota-exec-run

USER dev

# ISTOTA_CRED_SOCK is the only thing the wrapper needs from the environment;
# everything else it might have read from there — the policy, the real binary,
# the config and data dirs, the forge URL — travels in the policy file, whose
# location the wrapper computes rather than accepts. See policy_path().
ENV ISTOTA_CRED_SOCK=/run/istota-cred/sock

# The container runs the exec transport's supervisor. It used to run
# `sleep infinity` and be an exec target; see Layer 7 for why neither half of
# that survives, and `scripts/istota-exec-run` for what the supervisor does.
#
# The per-user service will override `command` with the same value and add
# `init: true`; that change lands with the deployment template, which still
# says `sleep infinity` today, so this image change is inert on a deployment
# until it does. Setting `CMD` here rather than only there is what makes a
# plain `docker run` of this image the thing the image tier can assert
# against — and what makes the two definitions agree once both exist.
CMD ["/usr/local/bin/istota-exec-run"]
