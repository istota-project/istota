# Docker deployment

!!! warning "Experimental"
    The Docker deployment is functional but unstable. For production, use [Ansible](ansible.md) or [bare metal install](../getting-started/quickstart-bare-metal.md).

## Stack overview

`docker/docker-compose.yml` defines a complete stack:

| Service | Purpose |
|---|---|
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

The config at `/data/config/config.toml` is generated on first start. To change settings after setup:

```bash
docker compose exec istota vi /data/config/config.toml
docker compose restart istota
```

## Optional profiles

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS tracking
docker compose --profile browser --profile location up -d  # Both
```

The browser container requires x86-64 (Chrome has no ARM packages).

## Volumes

| Volume | Purpose |
|---|---|
| `nextcloud_data` | Nextcloud user data |
| `shared_files` | Shared between Nextcloud and Istota (RW both) |
| `postgres_data` | PostgreSQL data |
| `redis_data` | Redis data |

Nextcloud's native data volume is mounted RO in istota at `/mnt/nc-data` for Talk attachment fallback.

## Security differences

- **No network proxy**: Docker's network isolation replaces the CONNECT proxy
- **Sandbox + skill proxy**: enabled by default, work inside the container
- **All extras installed**: every optional dependency included in the image

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
