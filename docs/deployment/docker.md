# Docker deployment

:::warning[Experimental]
The Docker deployment is functional but unstable. For production, use [Ansible](ansible.md) or [bare metal install](../getting-started/quickstart-bare-metal.md).
:::

## Stack overview

`docker/docker-compose.yml` defines a complete stack:

| Service | Purpose |
|---|---|
| `init-shared` | One-shot: creates and chowns the shared-files volume before the rest start |
| `postgres` | Nextcloud database |
| `redis` | Nextcloud session cache |
| `nextcloud` | Fresh Nextcloud instance with auto-provisioning |
| `istota` | Scheduler + Claude Code |
| `web` | SvelteKit + FastAPI web UI |
| `nginx` | Reverse proxy (single entry port) |
| `browser` (profile) | Chrome + VNC container for web browsing |
| `webhooks` (`location` or `sms` profile) | GPS and SMS webhook receiver |

## Configuration

istota's configuration is `/data/config/config.toml` on the state volume. `istota setup`, run inside the image, writes it once; after that it is yours to edit. Nothing regenerates it. The stack's `.env` holds compose settings only (profiles, the hostname, ports), and each credential is a file under `docker/secrets/`, which compose mounts at `/run/secrets`.

```bash
cd docker
cp .env.example .env    # fill in the bundled Nextcloud's values
docker compose build istota
touch vm.env && sudo chown 10001:10001 .env vm.env
sudo install -d -o 10001 -g 10001 -m 0700 secrets
for n in $(docker compose config --format json | python3 -c 'import json,sys; print(*json.load(sys.stdin)["secrets"])'); do
    sudo install -o 10001 -g 10001 -m 0400 /dev/null "secrets/$n"; done
docker compose run --rm --no-deps -v "$PWD:/vm" --entrypoint istota-drop istota istota setup --vm-dir /vm
docker compose up -d
```

The empty secret files are there because compose refuses to run a service whose secret file is missing; `istota setup` replaces them. It runs as the daemon's uid 10001, writes the config, `/data/config/admins` and `/data/.secret_key`, and updates `.env` (keeping every line it does not own). See [SMS](../features/sms.md) and [WhatsApp](../features/whatsapp.md) for those blocks, which you add to `config.toml` yourself.

### Forge binaries

The image ships `gh` and `glab` under `/usr/local/lib/istota_forge`, deliberately off `PATH` so the only `gh` or `glab` a task can resolve by name is the policy wrapper. `[developer] gh_bin_path` and `glab_bin_path` name them; `istota setup` writes both, and you change them only to point at your own build.

Being off `PATH` is a guard against habit, not a boundary — the sandbox binds `/usr` read-only, so an absolute path still reaches the real binary. The boundary is the skill proxy, which keeps the token out of the model's environment.

A config written before the binaries existed names the old default path. Nothing needs editing: the skill also probes the install location directly rather than trusting the configured path.

## Changing settings

Edit `/data/config/config.toml` and restart:

```bash
docker compose exec istota istota-drop vi /data/config/config.toml   # or copy it out and back
docker compose restart istota web webhooks
```

A credential is a file under `docker/secrets/` (0400, owned by uid 10001); replace the file and restart. An existing install keeps the `config.toml` its old entrypoint rendered, unchanged, as its own file; `docker/istota/config-diff.py` in the image compares two configs by key if you want to see what an old render would have changed.

## Upgrading an existing deployment

### Room identities: offline maintenance required

When upgrading a volume with legacy room identities, build the new image, then stop all three application services before running the migration. Keep Nextcloud and its database running for the server-side channel directory moves. From `docker/`, with the existing config and workspace volumes retained:

```bash
docker compose build istota
docker compose stop istota web webhooks
docker compose run --rm --no-deps --entrypoint /app/.venv/bin/istota istota -c /data/config/config.toml init --relocate-rooms --scheduler-stopped
docker compose up -d istota web webhooks
```

Inspect the migration result before the final command. Exit 1 is a refusal, and exit 2 means partial work needs attention; follow the [refusal and retry procedure](ansible.md#room-identity-migration). The command is safe to rerun while the application services remain stopped. `--scheduler-stopped` recovers the task rows a stopped scheduler left in flight, which would otherwise refuse as `live_tasks` on every rerun; leave it off if anything else is executing a task. Existing room history, native surface bindings and old links survive the identity change. Ordinary container boot initializes the schema but does not run this offline migration: the scheduler entrypoint cannot stop sibling web and webhook containers.


One thing this stack does is first-install only: `provision-nc.sh` is a Nextcloud post-installation hook, so it runs against a fresh instance and never again. A release whose fix is a new `occ` call therefore lands on new installs and needs a hand patch on old ones. The CHANGELOG says so where it applies.

Config keys are in that category again, deliberately: `config.toml` is yours now and nothing rewrites it, so a release that adds or renames a key changes nothing on an existing install until you edit the file. The three such changes listed below each say what to write.

As of the DAV-prefix release, the shared volume reaches Nextcloud as an external storage mount, so the bot's own folder tree puts everything one level below the path the bot was asking for, and sharing is refused on an external mount by default. Your config needs both keys:

```toml
[nextcloud]
# Must match the mount name — "Shared Files" unless you set
# ISTOTA_NC_SHARED_MOUNT_NAME, which compose feeds to both sides.
dav_prefix = "Shared Files"
auto_share_bot_dir = false
```

The other half is not a config key and still needs doing by hand: in the Nextcloud container, enable sharing on each of the two mounts the installer created — the one named after the shared volume and the one named after the bot:

```bash
docker compose exec -u www-data nextcloud php /var/www/html/occ files_external:list
docker compose exec -u www-data nextcloud php /var/www/html/occ files_external:option <mount_id> enable_sharing true
```

Restart `istota` afterwards. Without the config keys the `nextcloud` skill's `files` and `share` verbs answer 404 and the bot logs `Failed to share folder` on every boot; without the mount option every share of anything in the workspace is refused.

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

The Ansible role runs `python -m istota.maintenance.repos_relocate` on every deploy, to move developer clones from one shared tree into per-user subtrees. This stack does not, and does not need to: the old environment-rendered config left `repos_dir` empty and never shipped a value, so a Docker deployment made before the per-user split has no clones in the old layout to move. `istota setup` writes `repos_dir = "/data/repos"` when the developer skill is on, which is the per-user layout from the start.

If you set `repos_dir` by hand on an install predating the per-user split, run the migration yourself once before the clones are used:

```bash
docker compose exec istota python -m istota.maintenance.repos_relocate --dry-run
docker compose exec istota python -m istota.maintenance.repos_relocate
```

It refuses rather than guessing when it cannot tell whose clones are whose, and exits 0 with nothing to do on an install that never set it.

## Optional profiles

There are six: `browser`, `location`, `sms`, `whatsapp`, `whatsapp-baileys` and `signaling`.

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS tracking
docker compose --profile sms up -d                  # SMS webhooks
docker compose --profile whatsapp up -d             # WhatsApp via Meta's Cloud API
docker compose --profile whatsapp-baileys up -d     # WhatsApp via a paired session
docker compose --profile browser --profile location up -d  # Combine as needed
```

Rather than naming them per command, set `COMPOSE_PROFILES` in `.env` — a comma-separated list every `docker compose` in that directory then picks up. `istota setup --vm-dir` writes it from the answers you give it; a hand-copied `.env.example` leaves it empty, which means the core stack only.

The `location`, `sms` and `whatsapp` profiles select the same `webhooks` service. Set the matching `[location]`, `[sms]` or `[whatsapp]` `enabled = true` in `config.toml`; the profile starts the shared process, while the setting controls which feature accepts work. Nginx is the public endpoint for `/webhooks/`; the receiver port is exposed only inside the Compose network. Enabling several profiles still runs one receiver.

**The two WhatsApp profiles are alternatives, not a pair.** The surface has two adapters and they need opposite halves of the stack: Meta's Cloud API receives over a signed HTTP callback, so it wants `whatsapp` and the receiver; a paired WhatsApp Web session receives over a Unix socket, so it wants `whatsapp-baileys` and the Node sidecar, and no receiver at all. A profile cannot read the config, so pick the one matching `[whatsapp] provider`. Selecting both costs a receiver whose handlers answer 404, which is inert rather than harmful.

Pairing a Baileys session is not reachable from inside this stack — `istota whatsapp pair` starts a sidecar of its own and the istota image ships neither the program nor its dependencies. Pair on a host with a checkout and node, then move the session directory into the `istota_data` volume at `/data/db/whatsapp-baileys-session`, 0700 and owned by the uid the containers run as.

The browser container requires x86-64 (Chrome has no ARM packages).

### Talk over the signaling server

```bash
docker compose --profile signaling up -d
```

This replaces polling Nextcloud for Talk messages with a WebSocket that Nextcloud pushes to. It cuts inbound latency to a single round trip and removes a request per room per cycle, which is what makes it worth the extra container on a deployment with more than a handful of rooms.

**Three things have to line up, and the profile is only the first.**

1. Talk has to have the server registered. `provision-nc.sh` does that in the post-installation hook, so `ISTOTA_TALK_SIGNALING_SERVER` and `ISTOTA_TALK_SIGNALING_SECRET` have to be set **before the first install**. Setting them later means running `occ talk:signaling:add` by hand.
2. `[talk.signaling] enabled = true` in `config.toml`, which is what tells the daemon to use it. `istota setup` writes it, with `url`, when you choose the profile.
3. The `websockets` library, which comes with the `signaling` extra and is already in the image.

Set both in `.env` before the first `docker compose up`, which is the only moment the automatic registration can happen.

**`ISTOTA_TALK_SIGNALING_SERVER` has to resolve from two places.** A browser connects to it, and Nextcloud's own PHP posts room and chat events to it — that second leg is what makes inbound a push at all, and it runs inside the `nextcloud` container. So `http://localhost:8081` is wrong even for a laptop: that is a host-side publish, and from PHP `localhost` is Nextcloud's own loopback.

The stack's own answer is the nginx it already runs, which proxies `/standalone-signaling/` through to the server. With `DOMAIN` set, the URL is `${ISTOTA_PUBLIC_PROTO}://${DOMAIN}/standalone-signaling/` — the same address Nextcloud is served from, which the wizard offers as the default. It holds as long as `DOMAIN` resolves from inside the `nextcloud` container as well as from a browser: a public name on a host that can reach its own address does, a split-horizon setup that answers differently inside may not. Check it with `docker compose exec nextcloud curl -s https://your.domain/standalone-signaling/api/v1/welcome`, which answers `{"nextcloud-spreed-signaling":"Welcome",...}`. Send a GET, not a HEAD: the signaling server registers that route for GET only and answers a `curl -I` with 404, which reads as a broken proxy and is not one.

A front end of your own in front of `ISTOTA_TALK_SIGNALING_PORT` works the same way; register that URL instead.

**On a localhost-only stack it still works, but not on `localhost`.** With no `DOMAIN`, use this machine's own address on the network instead — `http://192.168.x.y:8080/standalone-signaling/` — which is reachable both from a browser on the host and from inside the `nextcloud` container, where a published port on the host's address answers and `localhost` is Nextcloud's own loopback. The URL is baked into Nextcloud at first install, so it dies the day the machine's address changes, which on DHCP is a matter of when. Good enough to exercise the push path on a laptop, not something to leave in place.

That covers istota's inbound, whose sessions name the container network (`http://nextcloud`, in `ISTOTA_TALK_SIGNALING_BACKEND_URLS`). Talk's own browser client names the URL Nextcloud advertises for itself — `OVERWRITECLIURL`, which with no `DOMAIN` is `http://localhost:8080` — and the signaling server posts back to whatever a client names, so from inside that container it posts to itself. For a local stack where every part agrees, set `DOMAIN` to the same `192.168.x.y:8080` instead of leaving it empty: trusted domains, the overwrite URL, the backend list and the signaling URL then all derive from it.

The URL is baked into Nextcloud at first install, so changing it later — or changing the port — means running `talk:signaling:add` again. That registration is a config write and nothing more: in Talk's `Add.php`, `--verify` is stored as the per-server flag for validating the server's TLS certificate rather than being a reachability probe, so it does not matter that neither nginx nor the signaling container is up at the moment the post-installation hook runs. The `full` tier is the standing evidence for that, not the flag's name: it registers `http://signaling:8080` while the server is provably down — `signaling` waits on `nextcloud` being healthy, which is the install completing — and its signaling assertions need Talk in `external` mode, which only a registration that landed produces. The `|| true` on that line in `provision-nc.sh` means the exit code proves nothing either way.

`[talk.signaling] url` is a separate thing and `istota setup` fills it in: it is the daemon's own route, `http://signaling:8080`, since the daemon is on the container network beside the server. Left empty the daemon reads the browser-facing URL out of Talk's settings, which on this stack is the wrong answer.

One consequence of turning it on before the server is registered: the daemon refuses to boot, and `restart: unless-stopped` makes that a loop that keeps `web` and `webhooks` down with it, since both wait for `istota` to be healthy. Set `[talk.signaling] enabled = false` to back out. Note also that nothing orders `istota` after `signaling` (compose would then start the server on every deployment), so a first boot can post its provisioning message while the server is still coming up; the daemon's own watchers retry, but that one post can fail.

If the daemon is told to use it and cannot — Talk still in `internal` signaling mode, or the library missing — **it refuses to boot**. That is deliberate: a daemon quietly polling while you believe push is live is worse than one that did not start. `istota doctor --only talk.signaling_reachable` says which of the three is missing.

**Registering an external signaling server changes call signaling for every Talk user on this Nextcloud**, not only for istota. With no MCU configured media stays peer to peer and calls keep working, but it is a change to a shared service. The container itself is one small Go binary — no Janus, no external NATS, and istota never publishes or subscribes to a media stream.

**Two consequences to expect once it is on.** istota holds an active Talk session in every room it watches, around the clock, so it shows as present to everyone else in those rooms; it is not in a call and never joins one. And a second daemon pointed at the same Nextcloud opens its own sessions and receives every event — Talk allows several sessions per attendee — so duplicate work is prevented by the read cursor rather than by the transport. Point a staging daemon at a staging Nextcloud.

There is no secret to configure on istota's side. It authenticates as its own Nextcloud user, so the server URL, the connection token and the per-room session are minted on demand from calls the bot account can already make. The shared secret above is Talk's, for the server to trust Nextcloud.

`[talk.signaling] payload_direct = true` goes one step further and ingests the message the server relays instead of refetching it. Leave it off unless you have a reason: it is the only part of this path that can be wrong about message content rather than about timing, and Talk only relays a message at all from roughly Talk 21 — below that every event is a bare notification and the setting changes nothing.

### The devbox is Ansible-only

This stack ships no devbox service, and the `devbox` skill cannot be used on it. That is a decision rather than a gap. Three separate reasons, any one of which is enough on its own:

- **The skill cannot be switched on.** `devbox.enabled` defaults to false and `istota setup` writes no `[devbox]` section.
- **The daemon has no way in.** The skill CLI reaches a devbox over a Unix socket into a server running inside it, and nothing in this shape publishes that socket to both sides — the container is not in the compose file, so there is no bind mount or named volume connecting them. That was true of the older `docker exec` route too, and more bluntly: the CLI runs inside the `istota` container, which installs no docker client and mounts no docker socket. Mounting the host socket there was never the fix either, since the filesystem sandbox does not run in this shape (see below).
- **No credential proxy.** Even given a way in, `gh`, `glab` and `git push` would fail inside the container, because the credential daemon is a host process rather than a service in the stack. See ISSUE-282.

Earlier releases did ship a `devbox` profile here. Nothing could reach it, and its only working consequence was that every change to the Ansible devbox had to be mirrored into a service nobody could use — which is how it drifted into having no credential socket in the first place. Devbox work goes through the Ansible deployment, which renders one container per user from the same `docker/devbox/Dockerfile`.

**Upgrading from a release that had the profile:** the service going away does not itself remove the container, but the next `./rebuild.sh` will. That script runs `docker compose down --remove-orphans`, and a container whose service is no longer in the file is precisely what that removes. Everything the box accumulated — installed packages, build output, anything outside `/home/dev` — is in its writable layer rather than in the volume, so it goes too. Copy out or `docker commit` whatever you want to keep before the next rebuild.

The volume and the network outlive the change either way, including `down --volumes`, because compose no longer declares them. Remove all three by hand when you are done with them:

```bash
docker rm -f devbox-$USER_NAME
docker volume rm docker_devbox_home     # after checking what is in it
docker network rm docker_devbox-net
```

The `docker_` prefix on those two is the compose project name, which defaults to the directory the compose file sits in. If you set `COMPOSE_PROJECT_NAME`, use yours.

If you want the workbench itself, build and run it by hand — the image is not istota-specific:

```bash
docker build -t istota-devbox:latest docker/devbox
```

## Volumes

| Volume | Purpose |
|---|---|
| `istota_data` | Istota's `/data` — config, databases, workspace. **This is the one to back up.** |
| `nextcloud_data` | Nextcloud user data |
| `nextcloud_html` | Nextcloud application code and installed apps |
| `shared_files` | Shared between Nextcloud and Istota (RW both) |
| `postgres_data` | PostgreSQL data |
| `redis_data` | Redis data |
| `browser_profile` | Persistent Chrome profiles per user, including their logins; also the console TLS certificate and parked legacy profile |

Nextcloud's native data volume is mounted RO in istota at `/mnt/nc-data` for Talk attachment fallback.

## Browser profiles

Each user has a separate profile under `/data/browser-profile/users/`. Logins and site storage survive task completion, session closure, process reaping and container restarts. They are never shared between users. Later tasks for the same user inherit that user's authenticated access without another vault fetch. Cloudflare clearance warms up separately for each user. Profiles are not automatically deleted.

Both compose files pass these settings to the browser service:

| Variable | Default | Purpose |
|---|---|---|
| `BROWSER_MAX_INSTANCES` | `2` | Maximum live Chrome processes |
| `BROWSER_INSTANCE_IDLE_S` | `900` | Idle seconds before a process with no live sessions is reaped |
| `MAX_BROWSER_SESSIONS` | `2` | Session limit per user |
| `BROWSER_MAX_TOTAL_SESSIONS` | `4` | Session limit across all users |
| `BROWSER_DISK_CACHE_BYTES` | `104857600` | Chrome disk-cache limit per profile, not a limit on total profile size |

The container retains its 3 GiB memory limit. Setting `BROWSER_MAX_INSTANCES=1` reduces process memory while keeping separate profiles; switching users can require a cold start. A full process pool evicts only an instance with no live sessions, otherwise it returns a capacity error with a retry delay. Session limits are separate: opening a session evicts the requesting user's oldest first, and can evict another user's oldest at the global cap when the requester has none. Browser requests still execute serially.

The Ansible equivalents are `istota_browser_max_instances`, `istota_browser_instance_idle_s`, `istota_browser_max_sessions`, `istota_browser_max_total_sessions` and `istota_browser_disk_cache_bytes`. Ansible retains its existing per-user session default of `3`. The role writes these to `browser.env`, which its compose template loads.

Use `istota-skill browse state` to inspect the requesting user's profile size and cookie domains, never cookie values. Domains are `null` when its browser is stopped or disconnected; state inspection does not start Chrome. Close that user's sessions before `browse forget --origin https://example.com` or `browse forget --all` to clear cookies and origin storage. Origin clearing includes cookies shared through a parent domain and can log out sibling sites. `browse forget --all --profile` removes the complete profile only after verified browser shutdown, and resets history, permissions and caches too.

The noVNC console is for operators only. Reach `/instances.html` on the existing console address over the deployment's VPN or management network, choose the user, then enter the deployment's VNC password when configured. Each view is routed to that user's display; a login completed there belongs only to that user. Routing tokens are addresses, not credentials. The index exposes live user IDs to anyone who can reach the console port, so keep the existing bind and firewall restrictions. Do not give users console links or publish this port to the internet.

**Upgrade:** Rebuild and recreate the browser container when updating the skill. For Ansible installations, run the full play. On first boot the old shared profile is parked under `legacy-profile/` in the same volume; no user inherits it. Each user signs in again unless an operator deliberately recovers the old profile. TLS certificates and existing per-user profiles stay in place. A parking collision needs operator attention and does not overwrite either copy. Until the image is updated, non-credential calls warn with `shared_profile` and credential fills refuse. After upgrading, verify `/health` advertises `per_user_profiles: true`. Direct API clients must send `X-Istota-User` on scoped requests; IDs must be single path components, ASCII, without control characters or surrounding whitespace.

## Security differences

- **The network allowlist is the default.** `istota setup` leaves `[security.network] enabled` at its default, on, as the Ansible shape has it, so each task gets `--unshare-net` and the CONNECT proxy's `host:port` allowlist. A config the old entrypoint rendered says `enabled = false`, and keeps saying it until you change it
- **The filesystem sandbox runs here**, under the run contract the `istota` service carries. See below
- **Skill proxy**: enabled by default and works inside the container. It is what keeps credentials out of the model's environment
- **All extras installed**: every optional dependency included in the image
- **No devbox**: this stack ships no devbox service and the skill cannot be enabled on it. [Details above](#the-devbox-is-ansible-only)

### Running tasks sandboxed

The `istota` service in `docker-compose.yml` carries the grant bubblewrap needs and nothing wider:

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

`seccomp-istota.json` is Docker's default profile plus the six calls bwrap needs to build a namespace (`clone`, `clone3`, `mount`, `pivot_root`, `umount2`, `unshare`), without the default's `CAP_SYS_ADMIN` rule, so `bpf`, `keyctl`, `userfaultfd`, `perf_event_open` and the rest of the default denylist stay refused for every task. `apparmor-istota` is Docker's `docker-default` plus `mount` and `pivot_root`; it keeps `docker-default`'s refusal of writes to `/proc/sys` and `/proc/sysrq-trigger`, which `systempaths=unconfined` would otherwise leave open to a root `docker exec`. On a host with AppArmor, load it before the stack starts:

```bash
sudo apparmor_parser -r -W docker/istota/apparmor-istota
```

A host without AppArmor, Docker Desktop among them, ignores the option.

The container starts in a short root phase that delegates its own cgroup to the daemon, so each task gets `memory.max`, `pids.max` and `cpu.max` of its own, and then drops to uid 10001 with every capability set empty. `docker compose exec` does not inherit that drop, so run CLI commands through it: `docker compose exec istota istota-drop istota doctor`. If the sandbox cannot be built (a profile missing, a compose file without these lines), the container exits at boot and its log names the lines to add, rather than running tasks unconfined.

These settings are meant for a host that runs nothing else, such as a VM dedicated to the install: `systempaths=unconfined` would let a uid-0 process in the container reach host sysctls if AppArmor were not loaded, and nothing in the container runs as uid 0 after the root phase.

### Nothing acts on the browser container's unhealthy verdict

The browser container's healthcheck is thorough. It probes the liveness endpoint's deep tier, which asks whether the Chrome process is alive, whether Chrome's DevTools endpoint answers, and — since ISSUE-384 — whether the API process can still drive the browser it is reporting on. What this stack does not have is anything that reads the resulting `unhealthy` and does something about it. `restart: unless-stopped` reacts to a process exiting, not to a failing healthcheck, so a container that reports itself wedged stays wedged and stays running.

The Ansible shape has the actor: a cron watchdog reads `.State.Health.Status` every minute, restarts after a debounce, and pages if the restarts start looping. There is no equivalent here, and adding one to a compose file is not straightforward — the point of the debounce and the crash-loop guard is that they are judgement, not a restart policy. So this stack states the gap rather than half-closing it. The verdict is still worth reading by hand (`docker compose ps`, or `/health` on port 9223, which reports the CDP heartbeat in `cdp_healthy` and `cdp_consecutive_failures`) when browsing stops working.

## Credentials

Each is a file under `docker/secrets/`, named for the variable the daemon reads it from, lowercased: `claude_code_oauth_token`, `anthropic_api_key`, `istota_brain_native_api_key`, `istota_nextcloud_app_password`, `istota_web_oauth2_client_secret`, `istota_web_session_secret_key`, `istota_email_imap_password`, `istota_caldav_password`, `istota_developer_gitlab_token`, `istota_developer_github_token`. An empty file is a credential the install does not use. Credentials without a file here (the SMS and WhatsApp ones) go in `config.toml`, which `istota setup` creates 0600.

The bundled Nextcloud's own settings (`ADMIN_PASSWORD`, `USER_NAME` / `USER_PASSWORD`, `BOT_PASSWORD`, `POSTGRES_PASSWORD`) stay in `.env` for as long as it is in the compose file.

## Upload limits

nginx is given a generous `NGINX_CLIENT_MAX_BODY_SIZE` (default `512M`), so the binding limit on a chat attachment is the application's own `[web.chat] max_attachment_mb` — 25 MB unless you raise it in `config.toml`. This is the opposite arrangement to the Ansible deployment, which derives the nginx ceiling from the application setting so the two cannot drift; there is no equivalent variable here.

The web service also runs uvicorn without `--timeout-graceful-shutdown`, so a `docker compose restart` with a browser tab holding the chat room stream open waits out the stop timeout before the container is killed.

## Signing in without Nextcloud

Set `[web] auth = ["email"]` and `[site] hostname` to the public hostname in `config.toml`, and configure SMTP for email sign-in codes and password resets; `istota setup` writes email sign-in by default when there is no Nextcloud login to offer. Restart `istota` and `web` after changing the config. See [email login setup](../features/web-interface.md#email-login) for the CLI bootstrap and recovery commands. These settings change authentication only; they do not remove the stack's Nextcloud, storage or Talk services.

To migrate an existing installation, set `auth = ["nextcloud", "email"]`, attach an email identity to each existing user, and inspect `istota auth list`. Once every user has an enabled identity and a working password or email-code path, change the value to `email`. Existing Nextcloud sessions then stop working. Dropping Nextcloud for storage, Talk and CalDAV is a separate change.

`none` is refused by the Docker web launcher. A loopback backend behind a public proxy is still public. Custom outer proxies must exclude `/istota/auth/set-password`, or omit query strings from access logs; the shipped nginx and uvicorn suppression cannot control an outer proxy.
