# Migrating a bare-metal install

A bare-metal install is one the Ansible role converged onto a Debian host: systemd units named after the install's namespace (`<ns>-scheduler`, `<ns>-web`, …), state under `/srv/app/<ns>`, secrets in `/etc/<ns>/secrets.env`, and a two-minute update cron. This page moves such an install onto a dedicated VM running the stack: provisioned by `host/provision.sh`, updated by `istota-stack update <tag>`.

There is no general export format. The two shapes share the database schema, the master key and the workspace, so the move is a stopped-service copy plus one rewrite step:

- `host/export-bare-metal.sh`, run as root on the old host, stops the install and writes one tarball of its state.
- `istota migrate import-bare-metal`, run in a one-shot container on the new VM, reads that tarball and lands it in the container layout: it rewrites `config.toml`, splits the secrets files, restores the databases, copies the repositories and repairs their worktrees, and rebases the host paths the database stores.

On an install whose workspace is an external Nextcloud the workspace does not move at all: the new VM mounts the same Nextcloud.

## What moves

| State | Old host | New VM | How |
|---|---|---|---|
| Framework and module databases | `/srv/app/<ns>/data/` | `/data/db/` in the state volume | `db_backup` snapshot on the old host, `db_restore` on the new one, `quick_check` on both sides |
| `config.toml` | the role's render | `/srv/istota/config/config.toml` | rewritten by the import; every change is printed |
| Admins | `/etc/<ns>/admins` | `/srv/istota/config/admins` | copied |
| Secrets | `/etc/<ns>/secrets.env`, `web-secrets.env` | `/srv/istota/secrets/*`, `/data/.secret_key`, `/data/.web_token_key` | one file per credential; the master key and the web token key byte for byte |
| Developer repositories | `developer.repos_dir` | `/data/repos` | copied, then `git worktree repair` per repository |
| Baileys session | `<data>/whatsapp-baileys-session` | `/data/db/whatsapp-baileys-session` | copied |
| Session transcripts | `<data>/logs` | `/data/db/logs` | copied |
| Browser profile | `<data>/browser-profile` | the `browser_profile` volume | optional, by hand (below) |
| Devbox homes | per-user Docker volumes | per-user Docker volumes | optional, by hand (below) |

Not carried: the credential broker's CA (regenerated, and each task gets the current one), the WhatsApp media staging and inbound spool, temp and control directories, the role's own backups under `data/backups`, superseded Baileys sessions, Claude Code's home directory (a `claude_code` brain authenticates from `CLAUDE_CODE_OAUTH_TOKEN`), the update cron's clone token, and the CLI configuration of the forge tools (they read their token from the environment).

The rclone configuration is the operator's input to `provision.sh` on the new VM. Copy `~/.config/rclone/rclone.conf` from the old service user's home to `/srv/istota/rclone.conf` by hand; it is not in the tarball.

## What the import rewrites

In `config.toml`, every state path is set to what `istota setup` writes for the container (`db_path`, `temp_dir`, `workspace_path`, `nextcloud_mount_path`, `developer.repos_dir`, the forge binary paths, the devbox socket directories, `browser.api_url`). Keys the container derives are dropped so they derive (`module_data_dir`, `skills_dir`, a set `brain.native.session_log.dir`, `logging.file`, `scheduler.host_pressure_docker_socket`, the Baileys paths). Keys this shape removed are dropped (`[devbox] docker_cli`, `[security] allow_unsandboxed_multi_user_vaults`). `web.trusted_proxy_hops` follows `--ingress`. Everything else stays as you had it, and any string still naming an old host path is listed.

In the database, values under an old state root are rebased with a path boundary, inside JSON as decoded values: `tasks.attachments`, `memory_chunks.source_id` with the same rows' `metadata_json`, the `note_resurface_configs` and `note_resurface_history` tables when a user's script created them, and the `corpus` and `resurface` history rows in `istota_kv`. A value under no old root is listed and left alone. Free-text history (task prompts and results, logs, messages) is a record of what happened and is not rewritten.

Some rows are listed and not rewritten, because they come from somewhere else:

- `scheduled_jobs.command` and `.prompt` are synced from each user's `CRON.md`. A job that calls `/srv/app/<ns>/istota/.venv/bin/…` or names a host path has to be fixed in that `CRON.md`, or the next sync writes the old value back. The import report names each job as `<user>/<job>`.
- Any other `istota_kv` row naming a host path, such as a `system_check` result naming the old Claude Code home.

### Secrets

Each variable in the two env files goes to the file the stack reads it from, under the variable's name lowercased (`ISTOTA_NEXTCLOUD_APP_PASSWORD` becomes `secrets/istota_nextcloud_app_password`). `ISTOTA_SECRET_KEY` becomes `/data/.secret_key` and `ISTOTA_WEB_TOKEN_KEY` becomes `/data/.web_token_key`, both 0600 and byte for byte: every stored credential is encrypted under the first, and every stored web token under the second.

A variable the stack has no secrets file for is a refusal, with nothing written. The import never drops a credential. The stack's compose file declares the set (`setup_wizard.SECRET_NAMES` is the same list); a credential outside it has to be added there before the import can carry it.

## Before the day

1. **Provision the new VM** with `host/provision.sh`, then `istota-stack update <tag>` and `istota-stack setup` with the answers this install needs: full Nextcloud integration, `proxied` ingress, the same public hostname. Do not start the stack. Its first start comes after the import.
2. **Keep the public hostname.** Talk's bot callback, the Nextcloud OAuth redirect URIs, the Meta and Twilio or Telnyx webhook URLs and the signaling server's backend URL are all keyed to it. Only the upstream proxy's target changes.
3. **Read the upstream proxy's configuration.** Write down the server block for this install: the `server` address and port it forwards to, the scheme in `proxy_pass`, and how it sets `X-Forwarded-For`. In `proxied` mode the new VM listens on plain HTTP, so the cutover changes the scheme as well as the address, unless the stack runs with `TLS_CERT_SOURCE=files`. With `trusted_proxy_hops = 2`, the chain the app reads is `<client>, <upstream>`, which is right for one upstream proxy in front of the stack's nginx.
4. **Find the upstream's maintenance switch** for this hostname: a per-host flag that answers every request with a 503 page. It holds the public name during the stop-copy-start window, so no webhook delivery and no login lands on either side mid-copy.
5. **Check the export fits.** The export streams to stdout, so it needs no space on the old host beyond the database snapshot, which sits in a scratch directory beside the databases and is deleted when the script exits.
6. **Rehearse** (below), and time the rehearsal's stop-copy-start: that is the downtime to announce.

## Rehearsal

The rehearsal runs in a local Lima VM provisioned from `host/lima/istota.yaml`. It holds the production credentials, so it must not become a second live bot: it would poll the production Talk rooms and mailbox, answer users, and write into the production Nextcloud.

1. On the old host, take an online export. Nothing is stopped and the update cron is not checked:

   ```sh
   ssh root@<old-host> /path/to/export-bare-metal.sh --namespace <ns> --no-stop > rehearsal.tar
   ```

2. Point the rehearsal VM's rclone remote at a copy of the workspace, never at the production mount: `rclone sync` the users' folders into a scratch remote, and name that remote in the VM's `rclone.conf`.
3. Unpack and import with `--rehearsal` and `--ingress local` (the import command is in step 6 of the cutover). A rehearsal import turns `talk`, `email`, `sms`, `whatsapp` and both sleep cycles off in the config, withholds every transport credential (Nextcloud, email, SMS, WhatsApp, CalDAV, Google Workspace, the Nextcloud OAuth client secret), suspends every scheduled job and disables every briefing in the database, and records itself as a rehearsal in the reserved `_migration` namespace.
4. Check the import: the printed config changes and rebase counts, `istota doctor` clean apart from what the withheld credentials explain, the web UI logging in, and one task run by hand with `istota task`, which also confirms a `claude_code` brain runs on the token alone.
5. Delete the rehearsal VM afterwards. It holds the master key and every user's data.

## Cutover

Every step on the old host and on the upstream proxy is a change to production. Read each one before running it.

1. **Hold the public name.** Turn on the upstream's maintenance switch for this hostname.
2. **Disable the update cron** on the old host: comment out the line in `/etc/cron.d/<ns>-update`, or move the file out of `/etc/cron.d`. The export refuses while it is enabled, because the cron resets the checkout and restarts the units on any new commit, which would bring the old daemon back beside the new one.
3. **Export.** As root on the old host:

   ```sh
   ssh root@<old-host> /path/to/export-bare-metal.sh --namespace <ns> > export.tar
   ```

   In this mode the script stops and disables `<ns>-scheduler`, `<ns>-web`, `<ns>-webhooks` and `<ns>-whatsapp-baileys`, and stops the devbox credential proxies. It then waits until no rclone VFS cache entry is marked `"Dirty"` (`--dirty-timeout`, ten minutes by default), stops and disables `mount-nextcloud`, and checks the cache again. A file still marked dirty after the unmount never reached Nextcloud and exists only in the old host's cache: the script refuses, and that file has to be copied up by hand before going on. Then it snapshots the databases as the service user and writes the tarball. Add `--with-browser-profile` to carry the logged-in browser sessions.

   Both daemons must never run at once: they would read the same Talk rooms, mailbox and webhooks, and both would answer. The units are disabled as well as stopped so a reboot does not bring them back.

4. **Copy the tarball** to the new VM and unpack it where only root and uid 10001 can read it:

   ```sh
   install -d -m 0700 -o 10001 -g 10001 /srv/istota/import
   tar -xf export.tar -C /srv/istota/import
   chown -R 10001:10001 /srv/istota/import
   ```

5. **Copy `rclone.conf`** to `/srv/istota/rclone.conf` if you have not, and re-run `provision.sh` so the mount unit is in place. The new mount runs as uid 10001, the daemon's uid in the container, so the workspace is writable from inside it.
6. **Import**, in a one-shot container with the config directory and the secrets directory writable. Look at the plan first:

   ```sh
   ISTOTA_CONFIG_READ_ONLY=false istota-stack compose run --rm --no-deps -T \
     --entrypoint istota-drop \
     -v /srv/istota/import:/import:ro -v /srv/istota/secrets:/secrets \
     istota istota migrate import-bare-metal \
       --source /import --secrets-dir /secrets --ingress proxied --dry-run
   ```

   Then run the same command without `--dry-run`. Add `--emit-plan /import-out/plan.json` (with a writable mount for it) to get the rewritten config as an `istota apply` plan, so the variables of a config manager can start from what this install runs.

   Exit codes: 0 done, or already imported; 1 refused, nothing written; 2 partial. A partial leaves `/data/.bare-metal-import.in-progress` naming the export, and re-running the same command with the same export finishes it. The import refuses a target whose database already holds tasks, since it replaces the database and the master key.

7. **Fix each `CRON.md`** the report names, in the users' workspaces.
8. **Start the stack**: `systemctl enable --now istota-stack`, then `istota doctor` through the VM's `istota` wrapper.
9. **Switch the upstream.** In this hostname's server block, change the `server` address and port to the new VM's `LISTEN_ADDR:LISTEN_PORT`, and `https://` to `http://` in `proxy_pass` (unless the stack serves TLS from files). Reload the proxy.
10. **Lift the maintenance switch**, log in, and send the bot a message.
11. **Delete `/srv/istota/import`.** It holds every credential and a copy of every database.

### Optional state

- **Browser profile.** With `--with-browser-profile` the tarball holds `browser-profile/`. The import does not place it, because the istota container does not mount the browser's volume. Copy it in with the stack stopped: `docker run --rm -v istota_browser_profile:/v -v /srv/istota/import/browser-profile:/src:ro debian:trixie-slim sh -c 'cp -a /src/. /v/'`, and give it the browser image's user. Without it, users sign in to their sites again.
- **Devbox homes.** The repositories live in `repos_dir`, not in the devbox home, so this is optional. Copy a home with `docker run --rm -v <volume>:/v … tar` on each side.
- **Baileys session.** The import copies it. Never run both WhatsApp sidecars at once, which is a replaced-connection loop; the old one is stopped by the export. Re-pairing on the new side is the alternative.

## Rollback

Until the new side has done real work, rolling back is: turn the maintenance switch on, stop the new stack, re-enable and start the old units and `mount-nextcloud`, re-enable the update cron, point the upstream back at the old host, and lift the switch. The old host's state is as the export left it.

After the new side has done real work, going back loses it: answered messages, written rows, files written into the workspace from the new side's state. Take the first successful scheduled run on the new side as the point after which rollback means losing data. Keep the old host stopped, not deleted, until you are past it.
