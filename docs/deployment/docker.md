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
| `devbox` (profile) | Per-user development container for the developer skill |

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

An upgraded container keeps the `[developer]` block written before the binaries existed, because `config.toml` is generated only when absent and lives in a named volume. Nothing needs editing: the skill probes the install location directly rather than trusting the configured path, so upgrading the container is enough.

The config at `/data/config/config.toml` is generated on first start. To change settings after setup:

```bash
docker compose exec istota vi /data/config/config.toml
docker compose restart istota
```

## Upgrading an existing deployment

Two things this stack does are first-install only, and both are easy to mistake for "the upgrade did not work". `entrypoint.sh` writes `/data/config/config.toml` only when the file is absent, and it lives on the `istota_data` volume; `provision-nc.sh` is a Nextcloud post-installation hook, so it runs against a fresh instance and never again. A release whose fix is a new config key or a new `occ` call therefore lands on new installs and needs a hand patch on old ones. The CHANGELOG says so where it applies.

One such patch is outstanding as of the DAV-prefix release. The shared volume reaches Nextcloud as an external storage mount, so the bot's own folder tree puts everything one level below the path the bot was asking for, and sharing is refused on an external mount by default. A fresh install gets both right. An existing one needs, in `/data/config/config.toml`:

```toml
[nextcloud]
# Must match the mount name — "Shared Files" unless you set
# ISTOTA_NC_SHARED_MOUNT_NAME, which compose feeds to both sides.
dav_prefix = "Shared Files"
auto_share_bot_dir = false
```

and, in the Nextcloud container, sharing enabled on each of the two mounts the installer created — the one named after the shared volume and the one named after the bot:

```bash
docker compose exec -u www-data nextcloud php /var/www/html/occ files_external:list
docker compose exec -u www-data nextcloud php /var/www/html/occ files_external:option <mount_id> enable_sharing true
```

Restart `istota` afterwards. Without the config keys the `nextcloud` skill's `files` and `share` verbs answer 404 and the bot logs `Failed to share folder` on every boot; without the mount option every share of anything in the workspace is refused.

A second patch is outstanding for any install created before the model-alias rename. `[models.roles]` was renamed to `[models.aliases]` and the old key is now read by nothing, so a config generated under the old name has its per-role map dropped and logs a warning naming the retired key on every process start. In `/data/config/config.toml`:

```toml
# was [models.roles]
[models.aliases]
fast = "..."
general = "..."
smart = "..."
```

Restart `istota` afterwards. This only changes behaviour if you pointed a role at something other than `ISTOTA_BRAIN_NATIVE_MODEL` — an unmapped role already falls back to the single configured model, so an install that left all three the same loses only the warning.

## Optional profiles

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS tracking
docker compose --profile devbox up -d               # Developer skill container
docker compose --profile browser --profile location up -d  # Combine as needed
```

The browser container requires x86-64 (Chrome has no ARM packages).

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
| `devbox_home` | Home directory for the devbox container |

Nextcloud's native data volume is mounted RO in istota at `/mnt/nc-data` for Talk attachment fallback.

## Security differences

- **No network allowlist.** `render-config.sh` writes `[security.network] enabled = false` unconditionally, so no CONNECT proxy runs and a task's outbound traffic is whatever the container's network permits. Docker's bridge is not a substitute: it isolates the container from the host's other services, and does nothing about which hosts on the internet a task may reach. The Ansible shape is where the `host:port` allowlist runs
- **The filesystem sandbox does not run here**, though the config says it is on. Every task runs unconfined, with the framework database, every user's module databases, `config.toml` and `.secret_key` in view. See below
- **Skill proxy**: enabled by default and works inside the container. It is what keeps credentials out of the model's environment, and with the sandbox off it is the only thing doing so
- **All extras installed**: every optional dependency included in the image
- **No devbox credential proxy**: this shape runs no host-side credential daemon, so `gh`, `glab` and `git push` do not work inside the devbox container. That is deliberate — the proxy is a host process rather than a service in the stack. The Ansible deployment runs one per user and has the capability; here, do forge work outside the box. See ISSUE-282.
- **No devbox network filtering**: the DOCKER-USER rules that drop RFC1918 and cloud metadata for the devbox are added by the Ansible role and are not present here

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
