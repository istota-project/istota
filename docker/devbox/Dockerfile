FROM debian:bookworm-slim

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

# Layer 2: Node 20 via NodeSource. Kept on its own layer so apt cache
# changes here don't bust the heavier base layer above.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Layer 3: Go (latest stable). Pinned to a versioned URL so rebuilds are
# reproducible — bump deliberately, not opportunistically.
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
ARG GO_VERSION=1.22.2
ARG GO_SHA256_AMD64=5901c52b7a78002aeff14a21f93e0f064f74ce1360fce51c6ee68cd471216a17
ARG GO_SHA256_ARM64=36e720b2d564980c162a48c7e97da2e407dfcc4239e1e58d98082dfa2486a0c1
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
RUN useradd -m -s /bin/bash -d /home/dev -u 1000 dev \
    && echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev

USER dev
WORKDIR /home/dev

# Layer 4: uv (per-user) and rustup (per-user). Both install to /home/dev,
# which is masked at runtime by the persistent volume — but baking them
# into the image gives a working environment on first boot, before the
# user has done anything in their volume. Anything the user installs
# afterwards persists on top.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --profile minimal
ENV PATH="/home/dev/.cargo/bin:/home/dev/.local/bin:${PATH}"

# Layer 5: forge CLIs. The real `gh` and `glab` live under
# /usr/local/lib/istota_forge/ and are reached only through the wrapper — the
# same src/istota/forge_cli.py the sandbox uses, vendored here because Docker
# cannot COPY from outside its build context (scripts/sync-devbox-lib.sh keeps
# the copy honest and a test fails when the two drift).
#
# Pinned to the exact versions the spec's reconnaissance stage measured against
# (gh 2.98.0, glab 1.114.0), with the published sha256 verified before the
# archive is opened. Bookworm's own gh is 2.23 — far behind the 2.98 measured
# there — and ships no glab at all, so these come from the vendors' release
# artifacts rather than apt. The pins are a known-good mark, not a floor: none
# has ever been derived from the verbs the skill uses. `istota doctor` WARNs
# below them (forge_cli.GH_KNOWN_GOOD / GLAB_KNOWN_GOOD, bumped with these).
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
USER dev

# ISTOTA_CRED_SOCK is the only thing the wrapper needs from the environment;
# everything else it might have read from there — the policy, the real binary,
# the config and data dirs, the forge URL — travels in the policy file, whose
# location the wrapper computes rather than accepts. See policy_path().
ENV ISTOTA_CRED_SOCK=/run/istota-cred/sock

# The container is an exec target — keep it alive, no PID 1 service.
CMD ["sleep", "infinity"]
