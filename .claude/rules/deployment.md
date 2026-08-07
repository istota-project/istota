---
paths:
  - "deploy/**"
  - "docker/**"
  - "scripts/**"
---

# Deployment

**Ansible**: role at `deploy/ansible/` (symlinked from the operator's `ansible-server` checkout). When adding config fields, update `defaults/main.yml` + `templates/config.toml.j2`. The role runs `files/validate_config.py` against the rendered TOML before any handler can restart the scheduler — structural bugs fail the play instead of the running daemon. Three systemd units (`istota-scheduler`, `istota-web`, `istota-webhooks`) all read `ISTOTA_ADMINS_FILE` and `ISTOTA_SECRET_KEY` from the same EnvironmentFile.

**Docker**: `docker/docker-compose.yml` brings up nginx (single host port, reverse-proxies `/` → Nextcloud and `/istota/` → web service) + nextcloud + postgres + redis + istota (multi-stage Dockerfile builds the SvelteKit frontend; separate scheduler / web / webhooks services). The image installs `bubblewrap` + `tmux`, and the entrypoint defaults `sandbox_enabled = true` and `skill_proxy_enabled = true` (overridable via `ISTOTA_SECURITY_SANDBOX_ENABLED` / `ISTOTA_SECURITY_SKILL_PROXY_ENABLED`); the network proxy is off (`[security.network] enabled = false`) since the container's own network is the boundary. The container runs as **root** (no `USER` directive), so the CLI brains' `--dangerously-skip-permissions` is accepted via `IS_SANDBOX=1`, which `ClaudeCodeBrain`/`TmuxClaudeBrain` set automatically when running as root; bwrap doesn't `--clearenv`, so it propagates into the sandboxed `claude`. The entrypoint auto-provisions `#general`, `#logs`, `#alerts` group rooms (lookups are scoped by `USER_NAME` participation, so identically-named rooms across users on a shared NC don't collide), registers an OAuth2 client in NC via inline PHP, generates `LOCATION_INGEST_TOKEN` + `ISTOTA_SECRET_KEY` (persisted to `/data/.secret_key`), and seeds workspace files. Modules default on (`ISTOTA_*_ENABLED=true`). Key env: `CLAUDE_CODE_OAUTH_TOKEN`, `ADMIN_PASSWORD`, `USER_NAME`/`USER_PASSWORD`, `BOT_PASSWORD`, `POSTGRES_PASSWORD`, `DOMAIN`, `ISTOTA_WEB_INSECURE_COOKIES` (toggle for plaintext localhost).

**Nextcloud mount**: `/srv/mount/nextcloud/content` via rclone (`istota_use_nextcloud_mount: true`).
