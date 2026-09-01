# Docker deployment

!!! warning "Experimental"
    The Docker deployment is functional but unstable. For production, use [Ansible](ansible.md) or [bare metal install](../getting-started/quickstart-bare-metal.md).

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
| `webhooks` (profile) | GPS webhook receiver |

## Configuration

```bash
cd docker
cp .env.example .env
# Edit .env: set CLAUDE_CODE_OAUTH_TOKEN, passwords, USER_NAME
docker compose up -d
```

The `.env` file exposes most settings available in the Ansible role: scheduler intervals, conversation tuning, progress updates, sleep cycle, memory search, email, ntfy, developer skill, and per-user overrides.

### Forge binaries

The image ships `gh` and `glab` under `/usr/local/lib/istota_forge`, deliberately off `PATH` so the only `gh` or `glab` a task can resolve by name is the policy wrapper. `ISTOTA_DEVELOPER_GH_BIN_PATH` and `ISTOTA_DEVELOPER_GLAB_BIN_PATH` exist for pointing at your own build; leave both empty otherwise.

Being off `PATH` is a guard against habit, not a boundary — the sandbox binds `/usr` read-only, so an absolute path still reaches the real binary. The boundary is the skill proxy, which keeps the token out of the model's environment.

A container still running from before its upgrade keeps the `[developer]` block written before the binaries existed. Nothing needs editing either way: restarting re-renders the block, and the skill also probes the install location directly rather than trusting the configured path.

## Changing settings

`/data/config/config.toml` is generated from `docker/.env` on every start. Edit the variable and restart:

```bash
$EDITOR docker/.env
docker compose restart istota
docker compose restart web webhooks   # webhooks only if you run the location profile
```

The boot logs every key that changed, so `docker compose logs istota` is where you confirm an edit landed. The outgoing file is kept as `/data/config/config.toml.prev`.

Editing the rendered file directly does not survive a restart — that is the point of rendering it each time, and it is why `docker/.env` is the place to make a change. Values provisioning derives once are not re-derived: the OAuth2 client, the Talk room tokens, the location ingest token and the web session signing key all persist beside the config and are fed back into each render. (The secrets-store key is different again: it lives at `/data/.secret_key` and reaches the daemon through the environment, never through the config.) If you genuinely need to hand-maintain the file, set `ISTOTA_CONFIG_RENDER=preserve`; the boot then keeps it and logs every key that has drifted from `docker/.env`, so the staleness is at least visible.

## Upgrading an existing deployment

One thing this stack does is first-install only: `provision-nc.sh` is a Nextcloud post-installation hook, so it runs against a fresh instance and never again. A release whose fix is a new `occ` call therefore lands on new installs and needs a hand patch on old ones. The CHANGELOG says so where it applies.

Config keys are no longer in that category. The entrypoint used to write `/data/config/config.toml` only when the file was absent, and it lives on the `istota_data` volume that `rebuild.sh` keeps — so a release adding or renaming a key landed on new installs only, and an operator editing `docker/.env` got no error, no warning and no change (ISSUE-368). The config is rendered on every boot now, so **restarting `istota` is the patch** for the three that used to be listed here: the DAV prefix and share flag below, the `[models.roles]` → `[models.aliases]` rename, and the `tmux_claude` brain's explicit `fallback`. Each is kept below for the half a restart cannot do, and for anyone reading an older CHANGELOG entry that still names it.

As of the DAV-prefix release, the shared volume reaches Nextcloud as an external storage mount, so the bot's own folder tree puts everything one level below the path the bot was asking for, and sharing is refused on an external mount by default. Restarting `istota` renders both keys:

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

An install created before the model-alias rename had `[models.roles]` in its config, which is now read by nothing: the per-role map was dropped and a warning naming the retired key was logged on every process start. A restart renders the current name, which is what you should now see:

```toml
# was [models.roles]
[models.aliases]
fast = "..."
general = "..."
smart = "..."
```

This only changes behaviour if you pointed a role at something other than `ISTOTA_BRAIN_NATIVE_MODEL` — an unmapped role already falls back to the single configured model, so an install that left all three the same loses only the warning.

The same goes for any install running `ISTOTA_BRAIN_KIND=tmux_claude` created before ISSUE-362. That brain used to fail over to `claude_code` with nothing configured; failover is explicit now, for every brain kind, and the render writes `fallback = "claude_code"` in for it. An install that has not restarted since keeps a `[brain]` block with no `fallback` key and has no failover at all — a tmux launch failure or a usage limit fails the task — and logs one INFO line per process start saying so. A restart renders:

```toml
[brain]
kind = "tmux_claude"
fallback = "claude_code"
```

Having no failover is a valid choice now, which is why the INFO line exists; on a `tmux_claude` primary, `ISTOTA_BRAIN_FALLBACK` has to name a different brain to change what gets written, since an unset value there is filled in with `claude_code`.

## Optional profiles

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS tracking
docker compose --profile browser --profile location up -d  # Combine as needed
```

The browser container requires x86-64 (Chrome has no ARM packages).

### The devbox is Ansible-only

This stack ships no devbox service, and the `devbox` skill cannot be used on it. That is a decision rather than a gap. Three separate reasons, any one of which is enough on its own:

- **The skill cannot be switched on.** `devbox.enabled` defaults to false and `render-config.sh` writes no `[devbox]` section, so the generated config always has it off.
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
| `browser_profile` | Chrome profile for the browser container (logged-in sessions) |

Nextcloud's native data volume is mounted RO in istota at `/mnt/nc-data` for Talk attachment fallback.

## Security differences

- **No network allowlist.** `render-config.sh` writes `[security.network] enabled = false` unconditionally, so no CONNECT proxy runs and a task's outbound traffic is whatever the container's network permits. Docker's bridge is not a substitute: it isolates the container from the host's other services, and does nothing about which hosts on the internet a task may reach. The Ansible shape is where the `host:port` allowlist runs
- **The filesystem sandbox does not run here**, though the config says it is on. Every task runs unconfined, with the framework database, every user's module databases, `config.toml` and `.secret_key` in view. See below
- **Skill proxy**: enabled by default and works inside the container. It is what keeps credentials out of the model's environment, and with the sandbox off it is the only thing doing so
- **All extras installed**: every optional dependency included in the image
- **No devbox**: this stack ships no devbox service and the skill cannot be enabled on it. [Details above](#the-devbox-is-ansible-only)

### Running tasks sandboxed

`sandbox_enabled` is true in the generated config, but Docker's default seccomp profile blocks the `unshare(CLONE_NEWUSER)` bubblewrap needs, so the daemon's startup probe fails and `build_bwrap_cmd` hands back every command unwrapped. It says so at startup, in a line carrying `bubblewrap unavailable` — as `SECURITY UNSUPPORTED CONFIGURATION` with more than one user configured, and as a plainer `SECURITY` warning with one.

Two settings on the `istota` service fix it, and they are a pair:

```yaml
    security_opt:
      - seccomp:unconfined
      - systempaths=unconfined
```

Seccomp alone lets bwrap create the user namespace but not mount a procfs inside one, which every sandbox does. `--cap-add=SYS_ADMIN` is not an alternative: it gets past the unshare and then fails at `pivot_root`.

The shipped compose file grants neither, deliberately, and the cost is worth reading before you add them. The container runs as root and is not user-namespace remapped, so `systempaths=unconfined` gives container root a writable `/proc/sys`, and `/proc/sys/kernel` is not namespaced — entries like `core_pattern` are a route to running a command on the host. `seccomp:unconfined` separately removes the syscall filter standing between the container and the kernel's whole surface. So the trade is the container-to-host boundary for the task-to-daemon one. On a multi-user deployment that is plausibly the right way round, since without bwrap one user's task can read every other user's data and the credentials besides. On the single-user stack this page is mostly written for, it usually is not. The supported production shape is bare metal via Ansible, where bwrap unshares the user namespace unasked and neither setting is needed.

### Nothing acts on the browser container's unhealthy verdict

The browser container's healthcheck is thorough. It probes the liveness endpoint's deep tier, which asks whether the Chrome process is alive, whether Chrome's DevTools endpoint answers, and — since ISSUE-384 — whether the API process can still drive the browser it is reporting on. What this stack does not have is anything that reads the resulting `unhealthy` and does something about it. `restart: unless-stopped` reacts to a process exiting, not to a failing healthcheck, so a container that reports itself wedged stays wedged and stays running.

The Ansible shape has the actor: a cron watchdog reads `.State.Health.Status` every minute, restarts after a debounce, and pages if the restarts start looping. There is no equivalent here, and adding one to a compose file is not straightforward — the point of the debounce and the crash-loop guard is that they are judgement, not a restart policy. So this is the same call the sandbox section above makes: bare metal via Ansible is the supported production shape, and this stack states the gap rather than half-closing it. The verdict is still worth reading by hand (`docker compose ps`, or `/health` on port 9223, which reports the CDP heartbeat in `cdp_healthy` and `cdp_consecutive_failures`) when browsing stops working.

## Key env vars

| Variable | Purpose |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude authentication |
| `ADMIN_PASSWORD` | Nextcloud admin |
| `USER_NAME` / `USER_PASSWORD` | Your Nextcloud account |
| `BOT_PASSWORD` | Bot's Nextcloud account |
| `POSTGRES_PASSWORD` | Database |

## Upload limits

nginx is given a generous `NGINX_CLIENT_MAX_BODY_SIZE` (default `512M`), so the binding limit on a chat attachment is the application's own `[web.chat] max_attachment_mb` — 25 MB unless you raise it in `config.toml`. This is the opposite arrangement to the Ansible deployment, which derives the nginx ceiling from the application setting so the two cannot drift; there is no equivalent variable here.

The web service also runs uvicorn without `--timeout-graceful-shutdown`, so a `docker compose restart` with a browser tab holding the chat room stream open waits out the stop timeout before the container is killed.
