# Docker quickstart

The Docker setup spins up a complete stack: Postgres, Redis, a fresh Nextcloud instance, and the Istota scheduler. If you already have a Nextcloud instance, use [bare metal](quickstart-bare-metal.md) instead -- Docker Compose creates its own Nextcloud.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
```

The one-liner clones the repo to `~/istota` and runs an interactive wizard that auto-generates the passwords, prompts for your Claude token and optional features, writes `docker/.env`, and brings the stack up. Requires Docker with the `docker compose` plugin.

First start takes a few minutes: Nextcloud initializes the database, creates user accounts, installs apps (Talk, Calendar, External Storage), sets up shared folders, and creates a Talk room between you and the bot.

When it's up, open `http://localhost:8080`, log in with the username and password the wizard set, go to Talk, and start chatting.

### Wizard flags

The `--docker` flag and everything after it forwards to `docker/init.sh`:

```bash
bash docker/init.sh --minimal    # passwords + Claude token + user only, skip optional sections
bash docker/init.sh --force      # overwrite an existing .env without asking
bash docker/init.sh --no-start   # write .env but don't run `docker compose up`
```

### Manual configuration (from a clone)

To skip the wizard and edit the environment by hand, copy the example and fill it in:

```bash
cd ~/istota/docker
cp .env.example .env
```

Set at minimum:

- `CLAUDE_CODE_OAUTH_TOKEN` -- generate with `claude setup-token` (or set `ANTHROPIC_API_KEY` for direct API access)
- `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, `BOT_PASSWORD`, `USER_PASSWORD`
- `USER_NAME` -- your Nextcloud username

Optional but recommended:

- `USER_DISPLAY_NAME` -- your full name
- `USER_TIMEZONE` -- e.g. `America/New_York` (defaults to UTC)
- `USER_EMAIL` -- enables email features

Then bring the stack up:

```bash
docker compose up -d
```

## Optional services

The browser container (Google Chrome with bot-detection countermeasures) and GPS webhook receiver run as Docker Compose profiles:

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS webhook receiver
docker compose --profile browser --profile location up -d  # Both
```

The browser container requires an x86-64 host.

## Configuration after first start

The config file at `/data/config/config.toml` inside the container is generated on first start and not overwritten on restart. To change settings:

```bash
docker compose exec istota vi /data/config/config.toml
docker compose restart istota
```

The `.env` file exposes most of the same settings available in the Ansible role. See `.env.example` for the full list.

## Differences from bare metal

| Aspect | Docker | Bare metal |
|---|---|---|
| Network proxy | Disabled (Docker network isolation) | CONNECT proxy with domain allowlist |
| Users | Single user provisioned | Multi-user from config |
| Nextcloud | Bundled (new instance) | Connects to existing instance |
| Backups | Your responsibility (volume backups) | Ansible sets up cron-based DB backups |
| Python extras | All installed | Configurable per feature |

Bubblewrap filesystem sandboxing and the skill credential proxy work inside the container. Bubblewrap degrades gracefully if user namespaces aren't available -- add `--cap-add SYS_ADMIN` if needed.

## Next steps

See [post-install](post-install.md) for first steps after deployment.
