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
ARG GO_VERSION=1.22.2
RUN curl -fsSL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz \
        -o /tmp/go.tar.gz \
    && tar -C /usr/local -xzf /tmp/go.tar.gz \
    && rm /tmp/go.tar.gz
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

# Layer 5: devbox credential-proxy client shims. Each shim is a thin
# Python script that frames a JSON request and writes it to the Unix
# socket bind-mounted at /run/istota-cred/sock — the compose template
# mounts the per-user proxy directory (not the socket file) so daemon
# restarts don't strand the container against a dead inode. The host-
# side daemon injects the actual token. Tokens never enter the container.
USER root
COPY lib/istota_devbox_client.py /usr/local/lib/istota_devbox/istota_devbox_client.py
COPY scripts/git-credential-istota /usr/local/bin/git-credential-istota
COPY scripts/gitlab-api /usr/local/bin/gitlab-api
COPY scripts/github-api /usr/local/bin/github-api
COPY scripts/gh /usr/local/bin/gh
COPY scripts/glab /usr/local/bin/glab
COPY etc/gitconfig /etc/gitconfig
RUN chmod 0755 /usr/local/bin/git-credential-istota \
        /usr/local/bin/gitlab-api /usr/local/bin/github-api \
        /usr/local/bin/gh /usr/local/bin/glab \
    && chmod 0644 /usr/local/lib/istota_devbox/istota_devbox_client.py \
        /etc/gitconfig
USER dev

# Wire the API wrappers and curated CLI shims into env so the developer
# skill's prompt picks them up unchanged ($GITLAB_API_CMD / $GITHUB_API_CMD).
ENV GITLAB_API_CMD=/usr/local/bin/gitlab-api \
    GITHUB_API_CMD=/usr/local/bin/github-api \
    GH_PATH=/usr/local/bin/gh \
    GLAB_PATH=/usr/local/bin/glab \
    ISTOTA_CRED_SOCK=/run/istota-cred/sock

# The container is an exec target — keep it alive, no PID 1 service.
CMD ["sleep", "infinity"]
