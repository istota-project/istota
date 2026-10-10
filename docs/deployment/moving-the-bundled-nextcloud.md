# Moving the bundled Nextcloud

Earlier Docker releases ran a Nextcloud inside the istota stack: the `nextcloud`, `postgres`, `redis` and `init-shared` services in `docker/docker-compose.yml`, provisioned at first install by `provision-nc.sh`. The compose file no longer runs them. The stack ships no Nextcloud; istota either stores its workspace locally or connects to a Nextcloud you run yourself.

An install whose `config.toml` still names `http://nextcloud` is refused at boot when no such host resolves. The log names the two ways forward:

- **Keep the Nextcloud.** Move it, unchanged, into a compose project of its own, and run istota in full Nextcloud integration against it. Talk rooms, OAuth logins and every file stay where they are.
- **Switch to local storage.** Copy the workspace onto the state volume and drop Nextcloud. Simpler, for an install that does not use Talk or Nextcloud's file access.

Both start with the stack stopped: `docker compose down` in the old `docker/` directory. Do not pass `--volumes`; the volumes are the data.

## Keep the Nextcloud in a project of its own

The old services used five named volumes, prefixed with the old compose project name (`istota_` if `.env` set `COMPOSE_PROJECT_NAME=istota`, otherwise the directory name, often `docker_`): `nextcloud_html`, `nextcloud_data`, `shared_files`, `postgres_data` and `redis_data`. The new project reuses them as external volumes, so nothing is copied and both `files_external` mounts `provision-nc.sh` created (the bot's `Shared Files` over the shared volume, and the user's view of their bot folder) keep working.

1. Make a directory for it, for example `/srv/nextcloud`, and write a `compose.yml` with the old service definitions. Take them from the release you are running (`git show <tag>:docker/docker-compose.yml`), drop the `istota`, `web`, `webhooks`, `browser` and `whatsapp-baileys` services, and declare the five volumes as external under their existing names:

   ```yaml
   volumes:
     nextcloud_html:
       external: true
       name: istota_nextcloud_html
     nextcloud_data:
       external: true
       name: istota_nextcloud_data
     shared_files:
       external: true
       name: istota_shared_files
     postgres_data:
       external: true
       name: istota_postgres_data
     redis_data:
       external: true
       name: istota_redis_data
   ```

   Copy the Nextcloud passwords (`ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, `BOT_PASSWORD` and the rest) from the old `.env` into this project's `.env`. Nextcloud now needs a front end of its own: publish its port, or put it behind your reverse proxy, and set `OVERWRITEHOST`, `OVERWRITEPROTOCOL` and `NEXTCLOUD_TRUSTED_DOMAINS` to the name browsers will use for it.

2. Start it (`docker compose up -d` in `/srv/nextcloud`) and check you can log in.

3. On the istota VM, mount the bot's `Shared Files` folder over WebDAV with rclone, as the VM's `mount-nextcloud.service` does: the remote is the bot user's WebDAV root with the app password istota already uses, and the mount is rooted at `Shared Files`, not at the bot's whole tree. Mounted at `/srv/istota/mount`, compose binds it into the containers at `/mnt/shared`, which is where the workspace was before. No stored path changes, and nothing needs rebasing.

4. Edit `/data/config/config.toml`:

   ```toml
   nextcloud_mount_path = "/mnt/shared"
   workspace_path = "/mnt/shared"

   [nextcloud]
   url = "https://cloud.example.com"   # the moved Nextcloud, as istota reaches it
   dav_prefix = "Shared Files"
   auto_share_bot_dir = false
   ```

   `dav_prefix` and `auto_share_bot_dir` are the values the old compose file set; keep them. The OAuth2 client's endpoints under `[web]` (`oauth2_provider`, `oauth2_token_endpoint`, `oauth2_userinfo_endpoint`) named `http://nextcloud` and the old front end; point them at the moved Nextcloud too. The client itself, its redirect URI and every Talk room token are stored in Nextcloud's database and survive the move, since only the URL changed. If the redirect URI named the old stack's address and that address changes, update it in Nextcloud's OAuth2 admin settings.

5. Start the istota stack. The entrypoint checks that `/mnt/shared` is the rclone mount before the daemon starts; if the mount is not up it refuses and compose retries.

## Switch to local storage

1. Copy the workspace into the state volume. With the old project name `istota`:

   ```bash
   docker run --rm -v istota_shared_files:/from:ro -v istota_istota_data:/data alpine \
       sh -c 'mkdir -p /data/workspace && cp -a /from/. /data/workspace/ && rm -f /data/.ownership-10001'
   ```

   Removing `/data/.ownership-10001` makes the next boot hand the copied files to the daemon's uid, which it does once.

2. Edit `/data/config/config.toml`: remove the `[nextcloud]` table and any `nextcloud_mount_path`, set `workspace_path = "/data/workspace"`, set `[talk] enabled = false`, and if users logged in through Nextcloud, switch `[web] auth` to `["email"]` and give each user an email identity first (see [email login](../features/web-interface.md#email-login)).

3. Start the stack. The Nextcloud volumes are now unused; keep them until you are sure, then remove them by hand.

Talk rooms do not survive this path: there is no Talk without Nextcloud. Web chat rooms do.
