# The WhatsApp Baileys sidecar: one Node program holding one WhatsApp Web
# session, dialling the daemon's Unix socket.
#
# **Its own image rather than a stage in docker/istota/Dockerfile**, on the size
# ground the spec names and this directory's README records: the library pulls
# in libsignal and protobufjs, `[whatsapp] enabled` is false by default, and
# `provider = "baileys"` is one of two adapters — so folding it in charges every
# deployment for a surface most of them do not run. The devbox and browser
# images are separate for the same reason.
#
# The build context is this directory, not the repository root: the program is
# three files and needs nothing from the tree, and the root context is large
# enough that using it would dominate the build.
# `node:20-trixie-slim` rather than `node:20-slim`, which tracks bookworm: this
# is a runtime image whose Debian release is this repository's decision, and
# every other image it owns is on trixie. The web builder in
# docker/istota/Dockerfile stays on the untagged variant because it copies
# static JS out and contributes no library to the runtime.
FROM node:20-trixie-slim

WORKDIR /app

# Lock first, so a change to the program does not re-resolve the tree. `npm ci`
# rather than `npm install`: it installs exactly what package-lock.json pins and
# fails rather than silently resolving something else, which for a program that
# holds a full WhatsApp account is the property worth having. `--omit=dev`
# leaves out nothing of ours — this manifest declares no devDependencies.
#
# **No git here, and that is measured rather than assumed.** One of Baileys'
# own *runtime* dependencies is a git dependency rather than a registry
# tarball — `libsignal`, which is the cryptography — and the lockfile records
# it as `git+ssh://git@github.com/…`, which GitHub serves only to an
# authenticated key. That reads like a build needing git and an https rewrite, and this image
# carried both for one commit. It does not: npm fetches a *hosted* git
# dependency as a codeload tarball over https and only shells out to git when
# that fails, so the install succeeds on this base, which ships no git at all.
# Verified by building with neither and checking `node_modules/libsignal` is
# there. A git dependency on a forge `hosted-git-info` does not know would need
# both back. There were two such dependencies until the 6.7.24 bump: 6.7.18
# also declared an eslint config under `dependencies`.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY index.js ./

# **No USER directive, and that is a constraint rather than an oversight.** The
# session directory is 0700 and `ensure_session_dir` refuses one owned by
# another uid, so the sidecar and the daemon must run as the same user. The
# istota image declares no USER either, so both are root; a `USER node` here
# would leave this container unable to read the credential the daemon paired.
#
# It writes nothing to stdout or stderr by contract — Baileys' own logger is
# chatty about JIDs and message bodies — so `docker logs` on this service is
# empty by design. Its diagnostics are sidecar.log inside the session directory,
# at ISTOTA_BAILEYS_LOG_LEVEL.
#
# Both ISTOTA_BAILEYS_SOCKET and ISTOTA_BAILEYS_SESSION_DIR are required and
# neither is defaulted here: with either missing the program exits 2, because
# the session directory is also where its log would go and there is no channel
# left to complain on. The compose service and the systemd unit set both.
CMD ["node", "/app/index.js"]
