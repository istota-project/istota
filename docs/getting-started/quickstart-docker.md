# Docker quickstart

The Docker setup runs the Istota scheduler, its web app and nginx. This page sets the stack up by hand with `docker compose`, which suits a try-out on a machine you already have. For a server, use the [server quickstart](quickstart-server.md): it provisions a dedicated machine, verifies a signed release and installs `istota-stack` to manage it. The stack is the same either way. It ships no Nextcloud: the workspace is a local directory by default, and `istota setup` can connect it to a Nextcloud you already run (full integration, with Talk). See [Storage](../deployment/docker.md#storage).

## Install

```bash
git clone https://github.com/istota-project/istota.git ~/istota
cd ~/istota/docker
```

Requires Docker with the `docker compose` plugin. From `~/istota/docker`:

1. `cp .env.example .env`.
2. `docker compose build istota`.
3. Give the image's uid 10001 the stack's `.env`, `host.env` and `secrets/` directory, with an empty file per secret (compose refuses to run a service whose secret file is missing). [Docker deployment](../deployment/docker.md#configuration) has the exact commands.
4. Run the wizard inside the image:

   ```bash
   docker compose run --rm --no-deps -v "$PWD:/vm" --entrypoint istota-drop daemon istota setup --vm-dir /vm
   ```

5. `docker compose up -d`.

`istota setup` asks for the first admin's user id, the public hostname and how the stack is reached, whether to connect to a Nextcloud (its URL, the bot's user and app password, the folder the workspace lives in, an OAuth2 client for web login), the model backend and its credential, email, a CalDAV server when there is no Nextcloud, GPS location, the modules (feeds, money, health, briefings), the developer skill, and the optional containers. It writes `/data/config/config.toml`, the admins file and the master key, sets `COMPOSE_PROFILES` in `.env`, and writes each credential to a file under `secrets/`.

**The signaling server is registered on your Nextcloud.** With the `signaling` profile, run `occ talk:signaling:add` there, pointing at this stack's `/standalone-signaling/` path. See [Talk over the signaling server](../deployment/docker.md#talk-over-the-signaling-server).

Answering no to a module records it in your user's `disabled_modules`, which seeds your profile the first time the stack boots. After that the stored profile wins, so change it in the web settings.

With Talk configured, the first start creates your Talk rooms on your Nextcloud -- private group rooms, not public ones.

When it's up, open `http://localhost:8080`, log in, and start chatting.

## Optional services

Four run as Docker Compose profiles: `browser` (Chrome with bot-detection countermeasures), `location` (the shared webhook receiver for GPS), `sms` (the same receiver for SMS), and `signaling` (Talk over a WebSocket instead of polling).

```bash
docker compose --profile browser up -d              # Web browsing
docker compose --profile location up -d             # GPS webhook receiver
docker compose --profile sms up -d                  # SMS webhook receiver
docker compose --profile browser --profile location up -d  # Combine as needed
```

Setting `COMPOSE_PROFILES` in `.env` is the alternative to naming them per command; `istota setup` writes it from your answers. The `location` and `sms` profiles share one receiver, so turn the matching feature on in `config.toml` as well (`[location] enabled`, `[sms] enabled`); the wizard does this for location. The browser container requires an x86-64 host, since Chrome has no ARM packages. `signaling` needs its registration in place before the first boot -- see above.

## Configuration after first start

`/data/config/config.toml` is yours: nothing regenerates it. Edit it and restart:

```bash
docker compose restart daemon web webhooks   # webhooks for location or SMS
```

A credential is a file under `docker/secrets/`; replace it and restart. A release that adds or renames a config key changes nothing until you edit the file; the CHANGELOG says what to write. See [Docker deployment](../deployment/docker.md) for the settings that matter on this stack.

## The sandbox

The `daemon` service carries the run contract that lets bubblewrap work inside the container (a seccomp profile, an AppArmor profile, `systempaths=unconfined`, no capabilities after a short root phase), so every task runs sandboxed per user, with the network allowlist on. That grant is only acceptable on a host that runs nothing else, which is why a server install is a dedicated machine. [Running tasks sandboxed](../deployment/docker.md#running-tasks-sandboxed) has the details.

## Next steps

See [post-install](post-install.md) for first steps after deployment.
