# Running the stack

Istota has one deployment: the Docker stack in `docker/`, on a Debian 13 machine dedicated to it, set up by `host/provision.sh` and managed with `istota-stack`. The [quickstart](../getting-started/quickstart-server.md) installs it on a server, and the [local install](../getting-started/local-install.md) in a Lima VM on a Mac. This page covers running it afterwards; [Local VM](#local-vm) below says what differs on a Mac.

Every command here runs as root on that machine. `istota-stack compose <...>` is `docker compose` with the stack's own files, paths and settings, and `istota <...>` runs the CLI in the daemon container as uid 10001. Don't run a bare `docker compose` in `src/docker/`: it misses the paths and the log driver `istota-stack` passes, and a `docker compose exec` starts as root with the container's capabilities.

## Stack overview

`docker/docker-compose.yml` defines the stack. It runs no Nextcloud of its own: storage is local by default, and full Nextcloud integration points istota at a Nextcloud you already run (see [Storage](#storage)).

| Service | Purpose |
|---|---|
| `daemon` | Scheduler + Claude Code |
| `web` | SvelteKit + FastAPI web UI |
| `nginx` | Reverse proxy (single entry port) |
| `browser` (profile) | Chrome + VNC container for web browsing |
| `whatsapp-baileys` (profile) | The WhatsApp sidecar for a paired session |
| `signaling` (profile) | Talk's signaling server |

## First-run setup

A fresh machine has no `config.toml`, and nothing serves the web UI until one exists. You write it once, in the browser or in the terminal. Both run the same validation and write the same files for the same answers.

### In the browser

```bash
istota-stack setup --web
```

This is the default first run. It mints a one-use setup code and a self-signed certificate valid for two days, prints an address with the code in it and the certificate's SHA-256 fingerprint, and runs a setup page on port 443:

```
Open this address in your browser to set up Istota:

  https://203.0.113.5/istota/setup#code=7KQ2M-X9D4F-H3T8W-QP6ZA
```

Your browser warns that the connection is not private. Check the fingerprint in its certificate viewer before you continue. The code is in the address's fragment, which the browser never sends to the server, and the page clears it once used.

The page asks one topic per screen: the assistant's name, your name, email and time zone, a password, the AI service (an Anthropic API key, an OpenRouter API key, or a Claude subscription token from `claude setup-token`), how people reach Istota (only on this computer, at your own web address with a certificate from Let's Encrypt, or behind a proxy you run), an optional Nextcloud, an optional email account or calendar, and the features, the web browser included. API keys, Nextcloud, email and the calendar can be tested before you save them. Finish writes the config, the admins file, the secrets and your sign-in, and the page sends you to the sign-in page with your email filled in once the stack answers.

After the page finishes, `istota-stack` installs `.env` and `host.env`, builds what the answers need and the images lack (the browser image, or Claude Code for a subscription), runs `provision.sh` again when the host settings changed, and starts `istota-stack.service`. The first start takes a few minutes when something has to be built.

- `istota-stack setup-code` prints the address, code and fingerprint again while a setup is running.
- `--local` serves plain HTTP on `127.0.0.1:8080` with no certificate, for the [local VM](#local-vm) or your own SSH tunnel.
- `ISTOTA_SETUP_PUBLISH=0.0.0.0:8443` moves the page off port 443 when something else holds it.
- The first browser to use the code gets the only session. To start over, stop the running setup (Ctrl-C, or `sudo systemctl stop istota-setup`) and run `istota-stack setup --web` again, which mints a new code.
- A setup that does not finish installs nothing on the host; if Finish failed partway, the run names any config or secret files it left so you can remove them. After an hour with no request the page stops, and so does the run. Fifty wrong codes stop it too.
- With `--local` the page answers only at `localhost`, `127.0.0.1` or `[::1]`.
- On a cloud machine behind its provider's NAT, the printed address is the private one; open the public address instead, and the code still works.
- It refuses when `config.toml` exists. Change a configured stack by editing `config.toml`, with `istota-stack apply`, or with `istota-stack setup --force`.

`istota-setup.service` runs `istota-stack setup --web` as a unit. `provision.sh` installs it and never enables it, and it is skipped once `config.toml` exists. `host/cloud-init.yaml` starts it after the first build and prints `istota-stack setup-code` to the console, which a VPS provider's web console shows; `install.sh --server` does the same and prints them in the terminal. `journalctl -u istota-setup` has the same lines.

The page leaves out the operator features: WhatsApp, the Talk signaling server, the developer skill, devboxes, Nextcloud login, the DAV prefix and the auto-share setting. Set those up afterwards in `config.toml` or with the terminal setup.

### In the terminal

```bash
istota-stack setup
```

This runs `istota setup` in the image and asks every question, operator settings included. Unlike the web setup it stops after writing the files: run `istota-stack check-image || istota-stack update <tag>`, then `provision.sh`, then `systemctl enable --now istota-stack`. The browser question defaults to yes; a scripted `--yes` run without `--profile browser` still leaves the browser off.

## Configuration

Everything the stack reads lives under `/srv/app/istota`:

| Path | Holds |
|---|---|
| `config/config.toml` | istota's configuration; `config/` is bound read-only at `/data/config` in every service |
| `config/admins` | the admin user ids |
| `secrets/` | one file per credential (0400, owned by uid 10001), mounted at `/run/secrets` |
| `.env` | compose settings only: profiles, the hostname, published ports, the deployed and previous tags |
| `host.env` | the machine's choices: storage and ingress modes, the domain and TLS source, the release key, the Claude Code pin |
| `compose.devbox.yml` | the per-user devbox services, rendered from `config.toml` (see [Devboxes](#devboxes)) |
| `src/` | the repository, checked out at the deployed tag |

Either first-run setup runs `istota setup` in the image as uid 10001 and writes these once, plus the master key at `/data/.secret_key`. It updates `.env` and keeps every line it does not own. After that `config.toml` is yours to edit; nothing regenerates it. See [SMS](../features/sms.md) and [WhatsApp](../features/whatsapp.md) for those blocks, which you add to `config.toml` yourself.

An estate managed with Ansible can write these files from inventory instead, through `istota-stack apply` and `istota-stack put-files`; see the [Ansible management role](ansible.md).

### Forge binaries

The image ships `gh` and `glab` under `/usr/local/lib/istota_forge`, deliberately off `PATH` so the only `gh` or `glab` a task can resolve by name is the policy wrapper. `[developer] gh_bin_path` and `glab_bin_path` name them; `istota setup` writes both, and you change them only to point at your own build.

Being off `PATH` is a guard against habit, not a boundary — the sandbox binds `/usr` read-only, so an absolute path still reaches the real binary. The boundary is the skill proxy, which keeps the token out of the model's environment.

A config written before the binaries existed names the old default path. Nothing needs editing: the skill also probes the install location directly rather than trusting the configured path.

## Changing settings

Edit `/srv/app/istota/config/config.toml` and restart:

```bash
istota-stack down && istota-stack up
```

`istota-stack apply plan.toml` is the other way in: it writes `config.toml` from a plan's `[config]` and restarts the stack only when something changed.

A credential is a file under `/srv/app/istota/secrets/` (0400, owned by uid 10001); replace the file and restart. An existing install keeps the `config.toml` its old entrypoint rendered, unchanged, as its own file; `docker/istota/config-diff.py` in the image compares two configs by key if you want to see what an old render would have changed.

## Upgrading an existing deployment

Updates keep the current and previous deployment images for rollback and remove older Istota image tags. Before building and after each update attempt, including a failed build or start, `istota-stack update` removes dangling images labeled for the Istota Compose project and trims unused build cache toward a 2 GB target. Tagged images, containers and volumes are preserved by this cache cleanup. Unlabeled dangling images are left alone because their owner is unknown.

Stack builds use Docker's default builder explicitly. Its cache is shared with other builds on that Docker daemon, so cleanup can make those builds slower too. The target bounds reclaimable cache, not total Docker storage: layers needed by retained images and active builds cannot be reclaimed. Each prune prints the space it reclaimed; a cleanup failure prints a warning without changing the update's exit status.

### The daemon service is called `daemon`

The scheduler's compose service was called `istota` and is now `daemon`, so its container is `istota-daemon-1` rather than `istota-istota-1`, and the image built for it is `istota:<tag>` rather than `istota-istota:<tag>`. `istota-stack update` brings the stack up with `docker compose up -d --remove-orphans`, which removes the old `istota-istota-1` container. Nothing moves on disk: the state volume is keyed by its own name, `istota_data`, not by the service, so the new container mounts the same data.

### Room identities: offline maintenance required

When upgrading a volume with legacy room identities, update to the new release, then stop the two services that write rooms, `daemon` and `web`, and the WhatsApp sidecar before running the migration. The sidecar holds inbound messages in memory for only four minutes while the daemon is away, so stopping it leaves them with WhatsApp instead; naming it is harmless when its profile is off. Leave the rest running. With full Nextcloud integration, keep your Nextcloud and the VM's rclone mount (`mount-nextcloud.service`) up: the channel directories move over WebDAV, and the migration refuses when either is unavailable. `istota-stack update` starts the new release before you stop it, and that is safe: no boot runs this migration, so the release already runs against legacy rooms on any install that has not done it yet, and after a partial migration.

```bash
istota-stack update vX.Y.Z
istota-stack compose stop daemon web whatsapp-baileys
istota-stack compose run --rm --no-deps --entrypoint istota-drop daemon istota init --relocate-rooms --scheduler-stopped
istota-stack up
```

Inspect the migration result before the final command. Exit 0 is done. Exit 1 is a refusal with nothing changed: `refusal: live_tasks` means a task was still running, so wait and rerun; any other reason needs fixing first, and the line names it. Exit 2 means partial work needs attention: the failure lines say which rooms or users, and each admin gets a bell notice listing them. Start the services again, fix what the lines name, and rerun in a later window. The command is safe to rerun while the application services remain stopped. `--scheduler-stopped` recovers the task rows a stopped scheduler left in flight, which would otherwise refuse as `live_tasks` on every rerun; leave it off if anything else is executing a task. Existing room history, native surface bindings and old links survive the identity change. Ordinary container boot initializes the schema but does not run this offline migration: the daemon's entrypoint cannot stop the web container beside it.


### An install that ran the bundled Nextcloud

The compose file no longer runs the `nextcloud`, `postgres` and `redis` services. An install whose `config.toml` still names `http://nextcloud` is refused at boot, with a message naming the two ways forward: move that Nextcloud, unchanged, into a compose project of its own, or switch to local storage. [Moving the bundled Nextcloud](moving-the-bundled-nextcloud.md) walks through both.

Config keys are in the same category, deliberately: `config.toml` is yours now and nothing rewrites it, so a release that adds or renames a key changes nothing on an existing install until you edit the file. The changes listed below each say what to write.

An install created before the model-alias rename had `[models.roles]` in its config, which is now read by nothing: the per-role map was dropped and a warning naming the retired key is logged on every process start. Write the current name instead:

```toml
# was [models.roles]
[models.aliases]
fast = "..."
general = "..."
smart = "..."
```

This only changes behaviour if you pointed a role at something other than `[brain.native] model` — an unmapped role already falls back to the single configured model, so an install that left all three the same loses only the warning.

The same goes for any install running `kind = "tmux_claude"` created before ISSUE-362. That brain used to fail over to `claude_code` with nothing configured; failover is explicit now, for every brain kind. A `[brain]` block with no `fallback` key has no failover at all — a tmux launch failure or a usage limit fails the task — and logs one INFO line per process start saying so. To keep the old behaviour, write:

```toml
[brain]
kind = "tmux_claude"
fallback = "claude_code"
```

Having no failover is a valid choice now, which is why the INFO line exists.

### The repository-layout migration does not apply here

`python -m istota.maintenance.repos_relocate` moves developer clones from one shared tree into per-user subtrees. The old bare-metal role ran it on every deploy. The stack does not, and does not need to: the old environment-rendered config left `repos_dir` empty and never shipped a value, so a stack installed before the per-user split has no clones in the old layout to move. A bare-metal install brought over with `istota-stack import-bare-metal` was already in the per-user layout, since its role ran the migration, and the import copies the clones as they are. `istota setup` writes `repos_dir = "/data/repos"` when the developer skill is on, which is the per-user layout from the start.

If you set `repos_dir` by hand on an install predating the per-user split, run the migration yourself once before the clones are used:

```bash
istota-stack exec python -m istota.maintenance.repos_relocate --dry-run
istota-stack exec python -m istota.maintenance.repos_relocate
```

It refuses rather than guessing when it cannot tell whose clones are whose, and exits 0 with nothing to do on an install that never set it.

## Optional profiles

There are three: `browser` (web browsing), `whatsapp-baileys` (WhatsApp through a paired session) and `signaling` (Talk over a WebSocket).

They are turned on by `COMPOSE_PROFILES` in `/srv/app/istota/.env`, a comma-separated list that `istota-stack setup` writes from your answers. Empty means the core stack only. To change it, edit the line:

```bash
COMPOSE_PROFILES=browser,signaling
```

Then run `istota-stack up`, which starts the containers of a profile that was added and removes those of one that was taken out, and re-run `/srv/app/istota/src/host/provision.sh`, which enables or disables the browser watchdog timer to match. Both are needed. `istota-stack up` never runs `provision.sh`, and `provision.sh` does not start or stop containers for a profile change: it runs `istota-stack up` only when it has just installed a different `istota-stack`.

The web service serves the webhook receiver's routes too (location pings, SMS and Meta's WhatsApp callbacks), so no profile is needed for them; each route answers only when its feature is enabled in the config. Set the matching `[location]`, `[sms]` or `[whatsapp]` `enabled = true` in `config.toml`. Nginx is the public endpoint for `/webhooks/`.

**Only the Baileys adapter needs a profile.** Meta's Cloud API receives over a signed HTTP callback, which the web service already serves; a paired WhatsApp Web session receives over a Unix socket from the Node sidecar, which `whatsapp-baileys` starts. A profile cannot read the config, so turn it on only when `[whatsapp] provider = "baileys"`.

A Baileys session is paired from the running stack, the first time and every time after. On a host that has never paired, the sidecar is up and offering codes. Pair from Admin, Connections with the control that asks you to type a phrase, or run `istota whatsapp pair --reset` and type the phrase. A plain `istota whatsapp pair` is refused, because the daemon cannot tell a session that was never paired from a working one. See [WhatsApp](../features/whatsapp.md).

The browser container requires x86-64 (Chrome has no ARM packages).

### Talk over the signaling server

The `signaling` profile replaces polling Nextcloud for Talk messages with a WebSocket that Nextcloud pushes to. It cuts inbound latency to a single round trip and removes a request per room per cycle, which is what makes it worth the extra container on a deployment with more than a handful of rooms.

**Three things have to line up, and the profile is only the first.**

1. Your Nextcloud's Talk has to have the server registered: `occ talk:signaling:add <url> <secret> --verify`, with `ISTOTA_TALK_SIGNALING_SECRET` from `.env` as the secret, and set `ISTOTA_TALK_SIGNALING_BACKEND_URLS` to your Nextcloud's URL (both the one browsers use and the one this stack uses, comma-separated, if they differ).
2. `[talk.signaling] enabled = true` in `config.toml`, which is what tells the daemon to use it. `istota setup` writes it, with `url`, when you choose the profile.
3. The `websockets` library, which comes with the `signaling` extra and is already in the image.

**The registered URL has to resolve from two places.** A browser connects to it, and your Nextcloud's own PHP posts room and chat events to it, which is what makes inbound a push at all. The stack's answer is the nginx it already runs, which proxies `/standalone-signaling/` through to the server, so the URL is `https://<this stack's DOMAIN>/standalone-signaling/`. Check it from the Nextcloud host with `curl -s https://your.domain/standalone-signaling/api/v1/welcome`, which answers `{"nextcloud-spreed-signaling":"Welcome",...}`. Send a GET, not a HEAD: the signaling server registers that route for GET only and answers a `curl -I` with 404.

The registration is a config write on the Nextcloud side and nothing more: in Talk's `Add.php`, `--verify` is the per-server flag for validating the server's TLS certificate, not a reachability probe. Changing the URL later means running `talk:signaling:add` again.

`[talk.signaling] url` is a separate thing and `istota setup` fills it in: it is the daemon's own route, `http://signaling:8080`, since the daemon is on the container network beside the server. Left empty the daemon reads the browser-facing URL out of Talk's settings, which on this stack is the wrong answer.

One consequence of turning it on before the server is registered: the daemon refuses to boot, and `restart: unless-stopped` makes that a loop that keeps `web` down with it, since it waits for the daemon to be healthy. Set `[talk.signaling] enabled = false` to back out. Note also that nothing orders `daemon` after `signaling` (compose would then start the server on every deployment), so a first boot can post its provisioning message while the server is still coming up; the daemon's own watchers retry, but that one post can fail.

If the daemon is told to use it and cannot — Talk still in `internal` signaling mode, or the library missing — **it refuses to boot**. That is deliberate: a daemon quietly polling while you believe push is live is worse than one that did not start. `istota doctor --only talk.signaling_reachable` says which of the three is missing.

**Registering an external signaling server changes call signaling for every Talk user on this Nextcloud**, not only for istota. With no MCU configured media stays peer to peer and calls keep working, but it is a change to a shared service. The container itself is one small Go binary — no Janus, no external NATS, and istota never publishes or subscribes to a media stream.

**Two consequences to expect once it is on.** istota holds an active Talk session in every room it watches, around the clock, so it shows as present to everyone else in those rooms; it is not in a call and never joins one. And a second daemon pointed at the same Nextcloud opens its own sessions and receives every event — Talk allows several sessions per attendee — so duplicate work is prevented by the read cursor rather than by the transport. Point a staging daemon at a staging Nextcloud.

There is no secret to configure on istota's side. It authenticates as its own Nextcloud user, so the server URL, the connection token and the per-room session are minted on demand from calls the bot account can already make. The shared secret above is Talk's, for the server to trust Nextcloud.

`[talk.signaling] payload_direct = true` goes one step further and ingests the message the server relays instead of refetching it. Leave it off unless you have a reason: it is the only part of this path that can be wrong about message content rather than about timing, and Talk only relays a message at all from roughly Talk 21 — below that every event is a bare notification and the setting changes nothing.

### Devboxes

A devbox is a persistent per-user Linux container, built from `docker/devbox/Dockerfile` at uid 10001, the daemon's own. Compose cannot loop over users, so the services live in a second compose file that istota renders from `config.toml`:

```toml
[devbox]
enabled = true
users = ["alice"]
```

`istota devbox compose-file` prints one `devbox-<user>` service per entry, plus the `daemon` service's mounts of each user's two socket volumes, and creates each user's repos directory first. `istota-stack` writes its output to `/srv/app/istota/compose.devbox.yml` on `setup`, `update` and an `apply` that changed the config, and passes the file as a second `-f` to every compose command. Only `update` builds the devbox image. After editing `users` in `config.toml` by hand, run `istota-stack update` with the deployed tag (`ISTOTA_TAG` in `.env`) to re-render the file and build the image. If `.env` has `ISTOTA_TAG_UNSIGNED=1`, as on a Lima VM or an install following main, pass `--unsigned`: `istota-stack update --unsigned main-<commit>` rebuilds that same commit, where `--unsigned latest` would move to main's current tip. The next `up` removes the service of a user taken out and keeps their home volume.

What each devbox gets, and nothing else:

- `devbox-home-<user>` at `/home/dev`, its persistent home.
- `devbox-exec-<user>` at `/run/istota-exec`: the exec transport's socket, which the server inside the box creates and the daemon container reaches at `[developer.container] exec_socket_dir`/`<user>`.
- `devbox-cred-<user>` at `/run/istota-cred`: the credential proxy's socket, which istota creates at `[developer] devbox_proxy_socket_dir`/`<user>`. The proxy answers on that socket as that user, whoever connects, so it matters that no other container mounts it and no task sandbox binds any credential socket directory.
- Its own subdirectory of the state volume, `/data/repos/<user>`, at the same path on both sides.

`istota setup` writes both socket directories under `/data/devbox/` when the developer skill is on; the render refuses socket directories outside `/data`, because only mounts there are handed to uid 10001 when the daemon container starts.

`istota-skill devbox reset` needs no Docker CLI: it is a request on the exec transport. The server empties `/home/dev` if it is a mount point, answers, and exits, and the service's `restart: unless-stopped` brings the box back.

**Egress filtering is the VM's.** The devbox network has a fixed subnet (`[devbox] network_subnet`, `172.30.0.0/24` by default), and `host/istota-devbox-egress.service` on the VM drops traffic from it to link-local and cloud metadata, RFC 1918 and CGNAT addresses. On a Docker host without that unit a devbox reaches the local network.

**Upgrading from a release that had a `devbox` profile in this file:** that container is not the one rendered here. Copy out or `docker commit` what you want from it, then remove it, its `docker_devbox_home` volume and its `docker_devbox-net` network by hand.

## Storage

Two questions decide the storage: whether istota uses a Nextcloud (`[nextcloud] url`), and where the workspace lives (`workspace_mount_path`, when it is a mount). `STORAGE` in `host.env` says which mount, if any, the machine provides. `istota setup` writes all three from your answers:

| Mode | `config.toml` | Workspace | `host.env` |
|---|---|---|---|
| Local (the default) | no `[nextcloud] url` | `/data/workspace` on the state volume | `STORAGE=local` |
| Nextcloud, local workspace | `url` names your Nextcloud, no `workspace_mount_path` | `/data/workspace` on the state volume | `STORAGE=local` |
| Full Nextcloud integration | `url` names your Nextcloud, `workspace_mount_path = "/mnt/shared"` | the VM's rclone mount of the bot's files, bound at `/mnt/shared` | `STORAGE=nextcloud` |
| Host folder (the [local VM](#local-vm)) | no `[nextcloud] url`; `workspace_mount_path = "/mnt/shared"` | a Mac folder Lima shares into the VM at `/srv/mount/host-folder`, bound at `/mnt/shared` | `STORAGE=host-folder` |

An older config may name the mount `nextcloud_mount_path`, the key's old name; it is read as `workspace_mount_path`.

When you connect a Nextcloud, setup asks where the workspace should live: `mount` (the default) or `local`. A scripted run takes `--workspace mount|local` and defaults to `mount` when it is given `--nextcloud-url`. The browser setup asks the same on its Nextcloud screen, with the mount chosen until you pick "On this server". With either answer the bot is a regular user on your Nextcloud: Talk, the `nextcloud` skill's user, group, Talk and share-listing verbs, and avatars all work.

With a local workspace, `provision.sh` installs no rclone and no mount unit, and setup writes no `dav_prefix` and turns `auto_share_bot_dir` off, since nothing of the workspace is in Nextcloud. Users reach their files in the web UI, not in Nextcloud. A file posted in Talk is fetched into the room's folder, `Channels/<room>/media/`, and `nextcloud talk share-file` uploads a copy before posting it. The skill's `files` verbs, `share create` and `share link` refuse, because the file they name is not in Nextcloud. Back up the `istota_data` volume, which now holds the workspace too (see [Backing up a local install](#backing-up-a-local-install)).

In full integration the bot is a regular user on a Nextcloud you run, and it sees what users share with it. The VM mounts that account's files with rclone at `/srv/mount/nextcloud/content` (`mount-nextcloud.service`), and compose binds the mount into `daemon` and `web` at `/mnt/shared` with `rslave` propagation, so a remount shows through without restarting them. `istota setup` writes `workspace_path` and `workspace_mount_path` as `/mnt/shared`, and the mount's `rclone.conf` (one WebDAV remote named `nextcloud`, the app password obscured as `rclone obscure` does it) with `RCLONE_REMOTE=nextcloud` in `host.env`, so the mount needs no hand-written config.

The entrypoint refuses to start the daemon when full integration is configured and `/mnt/shared` is not a mount of type `fuse.rclone`, so the daemon never writes into an unmounted path; compose's restart policy retries until the mount is up. A host folder must be a `virtiofs` mount with a `.istota-workspace` file at its root. A `workspace_mount_path` with no Nextcloud URL and no type that `STORAGE` declares is refused too, so a config for one mode does not start on a machine provisioned for another. `istota doctor`'s `runtime.mount_liveness` is the runtime check after that. Docker Desktop refuses `rslave` on a macOS path, which is one reason a Mac runs the stack in a Lima VM rather than in Docker Desktop.

To move an install in full integration to a local workspace, see [Moving to a local workspace](moving-to-a-local-workspace.md).

Workspace files have no versions or trash in either mode.

## Ingress and TLS

The compose `nginx` is the only listener the stack publishes. The mode is `INGRESS` in `host.env`, which `istota setup --vm-dir` writes beside the compose file and which both `nginx` and `istota` read:

| `INGRESS` | nginx listens on | TLS |
|---|---|---|
| `local` (the default) | loopback | none |
| `proxied` | one private address (`LISTEN_ADDR:LISTEN_PORT`), allowing only `UPSTREAM_PROXY` | on the hop from the proxy: a self-signed certificate the proxy pins (`TLS_CERT_SOURCE=self-signed`), a certificate from files, or none |
| `direct` | ports 80 and 443 on every address | a certificate from certbot on the VM (`TLS_CERT_SOURCE=acme`) or from files |

`docker/nginx/istota-ingress.sh` renders the server blocks for the mode around the one location template, `istota.conf.template`, before nginx starts, and refuses a mode it cannot render (proxied with no upstream address, for one). In `direct` mode, before the first certificate exists, nginx serves only the ACME challenge and the redirect on port 80; `istota doctor`'s `web.tls` reports FAIL until the certificate is there. The published ports are `NGINX_PUBLISH` and `NGINX_PUBLISH_TLS` in `.env`, which the wizard writes per mode.

Behind TLS (`direct` and `proxied`) the entrypoint refuses to start when `[site] hostname` and the stack's `DOMAIN` differ, since every login would then fail its origin check.

### Encrypting the hop from a reverse proxy

In `proxied` mode the reverse proxy terminates the public TLS and forwards to the VM. What crosses that hop is everything: passwords at login, session cookies, OAuth codes, and webhook payloads (messages, location points). Plain HTTP there is acceptable only on a network nothing else shares. Otherwise encrypt it, and make the proxy check what it connects to. A certificate the proxy does not verify stops only passive reading: anyone who can redirect the traffic presents their own certificate and the proxy accepts it.

`TLS_CERT_SOURCE=self-signed` (the wizard's default for `proxied`) does both. `provision.sh` makes a certificate once, in `/srv/app/istota/certs/`, naming `DOMAIN` and `LISTEN_ADDR`, valid for ten years, and never replaces it, since a new one would break the pin; it prints the SHA-256 fingerprint on every run. nginx then listens with TLS on `LISTEN_ADDR:LISTEN_PORT`. Copy `fullchain.pem` to the proxy and make it trust that certificate alone for this upstream:

- nginx:

  ```nginx
  proxy_pass https://<LISTEN_ADDR>:<LISTEN_PORT>;
  proxy_ssl_verify on;
  proxy_ssl_trusted_certificate /etc/nginx/istota-upstream.pem;
  proxy_ssl_name <DOMAIN>;
  proxy_ssl_server_name on;
  ```

- Caddy: `reverse_proxy https://<LISTEN_ADDR>:<LISTEN_PORT>` with a `transport http { tls_trust_pool file /etc/caddy/istota-upstream.pem  tls_server_name <DOMAIN> }` block.
- HAProxy: `server istota <LISTEN_ADDR>:<LISTEN_PORT> ssl verify required ca-file /etc/haproxy/istota-upstream.pem verifyhost <DOMAIN> sni str(<DOMAIN>)`.
- Traefik: a `serversTransport` with `rootCAs` naming the file and `serverName: <DOMAIN>`, referenced by the service.

To replace the certificate, delete both files in `/srv/app/istota/certs/`, run `provision.sh`, copy the new `fullchain.pem` to the proxy and reload it, then restart the stack. `TLS_CERT_SOURCE=files` is the same listener with a certificate you provide, for a proxy that verifies against an internal CA instead.

## Volumes

| Volume | Purpose |
|---|---|
| `istota_data` | Istota's `/data`: the databases (`/data/db`), and with a local workspace, with or without Nextcloud, the workspace (`/data/workspace`). `/data/config` is a bind of the VM's config directory, not part of the volume. **This is the one to back up.** |
| `browser_profile` | Persistent Chrome profiles per user, including their logins; also the console TLS certificate and parked legacy profile |

In full Nextcloud integration and on the local VM the workspace is not a volume: it is the VM's rclone mount or the Mac folder, bound at `/mnt/shared`.

## Local VM

On a Mac the stack runs in a Lima VM that the Mac's own `istota-stack` creates and controls; the [local install](../getting-started/local-install.md) page covers installing and running it. Inside the VM it is this stack, with three host settings that `create` writes into `host.env`:

| `host.env` | Value | Effect |
|---|---|---|
| `STORAGE` | `host-folder` | the workspace is the Mac folder at `/srv/mount/host-folder`; `provision.sh` makes `istota-stack.service` require that mount, and never creates, chmods or chowns the folder |
| `INGRESS` | `local` | nginx on the VM's loopback, which Lima forwards to the Mac's `localhost:<port>` |
| `HOST_LOOPBACK_BLOCK`, `HOST_GATEWAY_ADDR`, `HOST_LOOPBACK_ALLOW` | `1`, `192.168.5.2`, empty | containers cannot reach the Mac's loopback except DNS and the ports listed ([Security](security.md#the-local-vm)) |

Every command on this page works from the Mac: `istota-stack <verb>` there runs the VM's `istota-stack <verb>` as root, and `istota` runs the CLI in the daemon container. `apply` and `import-bare-metal` take a path on the Mac. Paths such as `/srv/app/istota/config/config.toml` are in the VM; `istota-stack shell` opens a shell there.

What differs from a server:

- **First run.** The Mac's `istota-stack create` runs the terminal setup with storage, ingress and hostname set. `istota-stack setup --web --local` after `create --no-setup` is the browser route; it keeps the host folder as the workspace. Either way setup writes `[scheduler] cron_max_staleness_minutes = 720` for local ingress, so work missed while the Mac slept runs once after wake.
- **Releases.** The VM verifies the tag as a server does. After each `update` the Mac's `istota-stack` copies itself, the `istota` shim and the Lima template out of the VM's verified checkout.
- **Backups.** The database snapshots and the config copies land in the Mac folder, and `istota-stack update`, `setup` and `apply` copy `.env` and `host.env` there too (`Backups/config/<date>/`). The master key goes to the Mac's login Keychain. `istota-stack restore` on the Mac rebuilds a lost VM from the two; in the VM, `istota-stack restore` is the half that runs `istota migrate restore-workspace` with the key on stdin, onto a stack that was never set up.
- **The folder's marker.** The daemon will not start without `.istota-workspace` at the folder's root, and `istota doctor`'s `runtime.mount_liveness` fails without it on a running stack. A folder that is renamed or on an unplugged volume reads as an empty `virtiofs` mount, and the marker is how the stack tells.

### Backing up a local install

Istota backs up nothing but its databases. On a timer it snapshots them to `[scheduler] db_backup_dir`, by default `Backups/db/snapshots` under the workspace. In full Nextcloud integration that is on the mount, so the snapshots leave the VM. In local storage mode it is `/data/workspace/Backups/db/snapshots`, on the same `istota_data` volume as the databases themselves: a snapshot there gets you back from a corrupted database file, not from losing the volume. `istota doctor` reports this as a warning under `backups.off_volume`, and `runtime.workspace_disk` warns when the volume runs low on space.

The workspace and the volume as a whole are yours to back up. Either back up the volume with whatever backs up the VM's disk, or stop the stack and archive it:

```bash
istota-stack down
docker run --rm -v istota_istota_data:/data:ro -v "$PWD":/backup alpine \
  tar -czf /backup/istota-data.tar.gz -C /data .
istota-stack up
```

Compose prefixes the volume with the project name, so on the VM it is `istota_istota_data`. Unless a secrets file supplies it, the archive holds `/data/.secret_key` beside the databases it decrypts, so keep it somewhere at least as private as the VM (see `docs/deployment/security.md`). To keep the database snapshots off the volume without archiving it, set `db_backup_dir` to a directory on another disk mounted into the stack.

## Container logs

On the VM every service logs to the systemd journal, under the container's name: `journalctl CONTAINER_NAME=istota-daemon-1` reads the daemon, and anything that ships the host's journal ships the stack's output with it. `docker logs` and `istota-stack compose logs` still work. The journal's size cap from `provision.sh` (500 MB) bounds it. Devbox containers are the exception: they keep their own rotated `json-file` logs, because build and test output would push the host's own lines out of the journal.

The compose file's own default is Docker's `json-file` driver, which is what the test tiers get when they run it outside `istota-stack`, on hosts with no journald.

## Browser profiles

Each user has a separate profile under `/data/browser-profile/users/`. Logins and site storage survive task completion, session closure, process reaping and container restarts. They are never shared between users. Later tasks for the same user inherit that user's authenticated access without another vault fetch. Cloudflare clearance warms up separately for each user. Profiles are not automatically deleted.

The compose file passes these settings to the browser service:

| Variable | Default | Purpose |
|---|---|---|
| `BROWSER_MAX_INSTANCES` | `2` | Maximum live Chrome processes |
| `BROWSER_INSTANCE_IDLE_S` | `900` | Idle seconds before a process with no live sessions is reaped |
| `MAX_BROWSER_SESSIONS` | `2` | Session limit per user |
| `BROWSER_MAX_TOTAL_SESSIONS` | `4` | Session limit across all users |
| `BROWSER_DISK_CACHE_BYTES` | `104857600` | Chrome disk-cache limit per profile, not a limit on total profile size |
| `BROWSE_WATCHDOG_DEADLINE_S` | `90` | Seconds a request may run before Chrome is killed and relaunched; `0` disables the watchdog. Keep it between about 65 and 120: lower cuts off long `type` actions and Cloudflare challenge solving, and above 120 the skill's own client gives up first |
| `BROWSE_WATCHDOG_POLL_S` | `5` | Seconds between the watchdog's checks of the in-flight request; at least `1` |
| `BROWSER_CDP_FAILURE_THRESHOLD` | `3` | Consecutive CDP failures that mark the container unhealthy; `0` disables this check |
| `BROWSER_CDP_FAILURE_WINDOW_S` | `900` | How long that verdict stays red after the last failure; values below `210` are raised to `210` |
| `BROWSER_WEDGE_RECOVERY_THRESHOLD` | `3` | Watchdog recoveries within the window that mark the container unhealthy; `0` disables this check |
| `BROWSER_WEDGE_RECOVERY_WINDOW_S` | `900` | Window for counting those recoveries; values below `210` are raised to `210` |

The container retains its 3 GiB memory limit. Setting `BROWSER_MAX_INSTANCES=1` reduces process memory while keeping separate profiles; switching users can require a cold start. A full process pool evicts only an instance with no live sessions, otherwise it returns a capacity error with a retry delay. Session limits are separate: opening a session evicts the requesting user's oldest first, and can evict another user's oldest at the global cap when the requester has none. Browser requests still execute serially.

Use `istota-skill browse state` to inspect the requesting user's profile size and cookie domains, never cookie values. Domains are `null` when its browser is stopped or disconnected; state inspection does not start Chrome. Close that user's sessions before `browse forget --origin https://example.com` or `browse forget --all` to clear cookies and origin storage. Origin clearing includes cookies shared through a parent domain and can log out sibling sites. `browse forget --all --profile` removes the complete profile only after verified browser shutdown, and resets history, permissions and caches too.

The noVNC console is for operators only. Reach `/instances.html` on the existing console address over the deployment's VPN or management network, choose the user, then enter the deployment's VNC password when configured. Each view is routed to that user's display; a login completed there belongs only to that user. Routing tokens are addresses, not credentials. The index exposes live user IDs to anyone who can reach the console port, so keep the existing bind and firewall restrictions. Do not give users console links or publish this port to the internet.

**Upgrade:** Rebuild and recreate the browser container when updating the skill (`istota-stack update` does both). On first boot the old shared profile is parked under `legacy-profile/` in the same volume; no user inherits it. Each user signs in again unless an operator deliberately recovers the old profile. TLS certificates and existing per-user profiles stay in place. A parking collision needs operator attention and does not overwrite either copy. Until the image is updated, non-credential calls warn with `shared_profile` and credential fills refuse. After upgrading, verify `/health` advertises `per_user_profiles: true`. Direct API clients must send `X-Istota-User` on scoped requests; IDs must be single path components, ASCII, without control characters or surrounding whitespace.

## Security

- **The network allowlist is the default.** `istota setup` leaves `[security.network] enabled` at its default, on, so each task gets `--unshare-net` and the CONNECT proxy's `host:port` allowlist. A config the old entrypoint rendered says `enabled = false`, and keeps saying it until you change it
- **The filesystem sandbox runs here**, under the run contract the `daemon` service carries. See below
- **Skill proxy**: enabled by default and works inside the container. It is what keeps credentials out of the model's environment
- **All extras installed**: every optional dependency included in the image
- **Devboxes are rendered per user** into a second compose file, and each one's credential socket is a volume only it and the daemon mount. [Details above](#devboxes)

### Running tasks sandboxed

The `daemon` service in `docker-compose.yml` carries the grant bubblewrap needs and nothing wider:

```yaml
    security_opt:
      - seccomp=./istota/seccomp-istota.json
      - apparmor=istota
      - systempaths=unconfined
      - no-new-privileges:true
    cap_drop: [ALL]
    cap_add: [CHOWN, FOWNER, SETUID, SETGID, SETPCAP, SYS_ADMIN]
    read_only: true
    cgroup: private
```

`seccomp-istota.json` is Docker's default profile plus the six calls bwrap needs to build a namespace (`clone`, `clone3`, `mount`, `pivot_root`, `umount2`, `unshare`), without the default's `CAP_SYS_ADMIN` rule, so `bpf`, `keyctl`, `userfaultfd`, `perf_event_open` and the rest of the default denylist stay refused for every task. `apparmor-istota` is Docker's `docker-default` plus `mount` (with no bind or move out of `/proc` or `/sys` and no `sysfs` mount) and `pivot_root`; it keeps `docker-default`'s refusal of writes to `/proc/sys` and `/proc/sysrq-trigger`, which `systempaths=unconfined` would otherwise leave open to a root `docker exec`. `provision.sh` installs the profile and `istota-stack up` loads it before starting anything; if it does not load, the stack is not started.

The container starts in a short root phase that delegates its own cgroup to the daemon, so each task gets `memory.max`, `pids.max` and `cpu.max` of its own, gives up `CAP_SYS_ADMIN` straight after the remount that needs it, and then drops to uid 10001 with every capability set empty. `docker compose exec` does not inherit that drop, which is why the machine's `istota` command and `istota-stack exec` both run through `istota-drop`. If the sandbox cannot be built (a profile missing, a compose file without these lines), the container exits at boot and its log names the lines to add, rather than running tasks unconfined.

These settings are why the machine has to be dedicated to the install: `systempaths=unconfined` would let a uid-0 process in the container reach host sysctls if AppArmor were not loaded, and nothing in the container runs as uid 0 after the root phase.

### Acting on the browser container's unhealthy verdict

The browser container's healthcheck probes the liveness endpoint's deep tier, which asks whether the Chrome process is alive, whether Chrome's DevTools endpoint answers, and (since ISSUE-384) whether the API process can still drive the browser it is reporting on. `restart: unless-stopped` reacts to a process exiting, not to a failing healthcheck, so compose alone leaves a wedged container running.

A watchdog timer that `provision.sh` installs (`istota-browser-watchdog.timer`, enabled when `provision.sh` last ran with the `browser` profile on) reads the verdict every minute, restarts the container after a debounce, stops and alerts if the restarts start looping, and restarts it once a day. To read the verdict yourself, use `istota-stack compose ps`, or `/health` on port 9223, which reports the CDP heartbeat in `cdp_healthy` and `cdp_consecutive_failures`.

## Credentials

Each is a file under `/srv/app/istota/secrets/`, named for the variable the daemon reads it from, lowercased: `claude_code_oauth_token`, `anthropic_api_key`, `istota_brain_native_api_key`, `istota_nextcloud_app_password`, `istota_web_oauth2_client_secret`, `istota_web_session_secret_key`, `istota_email_imap_password`, `istota_caldav_password`, `istota_developer_gitlab_token`, `istota_developer_github_token`, `istota_email_smtp_password`, `istota_sms_telnyx_api_key`, `istota_sms_telnyx_public_key`, `istota_sms_telnyx_messaging_profile_id`, `istota_google_workspace_client_secret`. An empty file is a credential the install does not use; the SMTP password falls back to the IMAP one. Credentials without a file here (the Twilio and WhatsApp ones) go in `config.toml`, which `istota setup` creates 0600.

The stack has no settings of its own for Nextcloud: there is no bundled one to configure.

## Upload limits

nginx is given a generous `NGINX_CLIENT_MAX_BODY_SIZE` (default `512M`), so the binding limit on a chat attachment is the application's own `[web.chat] max_attachment_mb` — 25 MB unless you raise it in `config.toml`. A proxy in front of the stack needs a ceiling at least as large.

The web service also runs uvicorn without `--timeout-graceful-shutdown`, so a restart with a browser tab holding the chat room stream open waits out the stop timeout before the container is killed.

## Signing in without Nextcloud

Set `[web] auth = ["email"]` and `[site] hostname` to the public hostname in `config.toml`, and configure SMTP for email sign-in codes and password resets; `istota setup` writes email sign-in by default when there is no Nextcloud login to offer. Restart the stack after changing the config. See [email login setup](../features/web-interface.md#email-login) for the CLI bootstrap and recovery commands. These settings change authentication only; storage and Talk are separate (see [Storage](#storage)).

To migrate an existing installation, set `auth = ["nextcloud", "email"]`, attach an email identity to each existing user, and inspect `istota auth list`. Once every user has an enabled identity and a working password or email-code path, change the value to `email`. Existing Nextcloud sessions then stop working. Dropping Nextcloud for storage, Talk and CalDAV is a separate change.

`none` is refused by the stack's web launcher. A loopback backend behind a public proxy is still public. Custom outer proxies must exclude `/istota/auth/set-password`, or omit query strings from access logs; the shipped nginx and uvicorn suppression cannot control an outer proxy.
