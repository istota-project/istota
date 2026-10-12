# Moving to a local workspace

An install in full Nextcloud integration keeps its workspace in the bot's Nextcloud files, through the VM's rclone mount. A local workspace keeps it in `/data/workspace` on the state volume instead, with Talk and the `nextcloud` skill still connected to your Nextcloud. [Storage](docker.md#storage) describes the two choices, and [Where the workspace lives](../features/nextcloud.md#where-the-workspace-lives) what each means for the `nextcloud` skill.

`istota-stack localize-workspace` moves an existing install from the mount to local storage, on the VM, with the stack stopped. It copies the files and leaves the Nextcloud copy in place, so going back is a config change.

## What the move does

1. Checks that `config.toml` can be moved: it is in full integration, and `istota apply` did not write it (see [A VM the management role manages](#a-vm-the-management-role-manages)). Then checks that `/data/workspace` is empty, or that you passed `--resume`. A refusal here stops nothing.
2. Stops `daemon`, `web` and `whatsapp-baileys`, the services that write the workspace. `nginx` and `signaling` keep running.
3. Copies `Users/`, `Channels/`, `Groups/` and `PERSONA.md` from `/mnt/shared` to `/data/workspace`, in a one-shot container running as the daemon's user. Nothing else at the top of the bot's Nextcloud files belongs to the workspace: the bot's `Talk/` folder, folders users shared with the bot and `Backups/` stay where they are. Database snapshots are made again on the new volume.
4. Rewrites `config.toml`: `workspace_path = "/data/workspace"`, no `workspace_mount_path` (nor its old name, `nextcloud_mount_path`), and `auto_share_bot_dir = false`. `dav_prefix` stays, since the `nextcloud` skill still uses it for the bot's own Nextcloud files. The previous file is kept as `config/config.toml.pre-localize`.
5. Writes `MOVED.md` into each user's bot folder in Nextcloud. It tells them their files now live in the web UI and that the folder is a frozen copy. This is the only change made to the Nextcloud files, and it happens only once the config no longer names the mount. A user who already has a `MOVED.md` there keeps theirs and gets no note; the verb names them. A failure here is a warning and the move goes on.
6. Sets `STORAGE=local` in `host.env` and runs `provision.sh`, which stops and disables `mount-nextcloud.service`.
7. Starts the stack, then rebuilds the memory search index on the new paths and removes the entries that still name `/mnt/shared`.

## Before you start

Run a dry run first. It stops nothing and writes nothing but the report, and lists what the copy would skip:

```bash
istota-stack localize-workspace --dry-run
```

The report is `/data/workspace-localize-report.json` on the state volume, and the same summary is printed. The copy skips anything that is not a regular file or a directory: a symlink, a FIFO, a socket or a device. It never follows a symlink, so a link a task left in a user's folder is not copied, and neither is what it points at.

Check that the state volume has room for the workspace. The dry run prints the bytes it would copy for each top-level folder, and `istota doctor`'s `runtime.workspace_disk` warns once the volume is low.

## Running it

```bash
istota-stack localize-workspace
```

The copy exits with one of four codes, and the verb acts on each:

| Exit | Meaning | What happens next |
|---|---|---|
| 0 | copied | the config, `host.env` and the stack move on |
| 1 | refused, nothing written | the verb stops; the stack is down |
| 2 | copied, with entries skipped | the verb asks before changing the config, or stops when there is no terminal |
| 3 | an I/O error partway | the verb stops; the report says how far it got |

When the copy skipped entries, read them first. To go on without a terminal, pass `--resume --accept-skipped`.

An interrupted move leaves the stack down and `config.toml` unchanged. Either run it again with `--resume`, which keeps files already copied with the same size and modification time, or run `istota-stack up` to go back to the mount.

`--resume` keeps every file already in `/data/workspace`, including ones the mount does not have. A VM that ran with a local workspace before it moved to the mount has such leftovers, and the move refuses to start over them without `--resume`. If they are stale, move them aside before the move rather than passing `--resume`:

```bash
istota-stack compose run --rm --no-deps -T --entrypoint istota-drop daemon \
    mv /data/workspace /data/workspace.before-move
```

`--no-note` skips `MOVED.md`.

## A VM the management role manages

When the Ansible management role manages the VM, `istota apply` writes `config.toml` from the role's plan, and the next play would undo an edit made on the VM. The verb refuses that config and names this route instead:

1. On the VM, copy the files and leave the stack down:

   ```bash
   istota-stack localize-workspace --copy-only
   ```

   `--copy-only` writes no `MOVED.md`, since the config has not moved yet and the play turns the mount off afterwards. Tell your users that their bot folder in Nextcloud is now a frozen copy.

2. In the role's variables, set `istota_storage: local`, leave `workspace_mount_path` out of `istota_config`, and set `workspace_path = "/data/workspace"` and `[nextcloud] auto_share_bot_dir = false` there. Run the play. If the stack is still down afterwards, run `istota-stack up`.
3. Once the stack is up, rebuild the memory search index:

   ```bash
   istota-stack exec istota workspace reindex --forget-root /mnt/shared
   ```

## After the move

Users reach their files in the web UI, under Workspace. Their bot folder in Nextcloud stays as it was on the day of the move, and the share that showed it to them is left in place. A change made there after the move does not reach Istota.

Back up the `istota_data` volume, which now holds the workspace as well as the databases. See [Backing up a local install](docker.md#backing-up-a-local-install).

## Going back

The Nextcloud copy is unchanged apart from `MOVED.md`, so returning to the mount is a config change:

1. `istota-stack down`.
2. Copy `config/config.toml.pre-localize` back over `config/config.toml` (as uid 10001, or `chown 10001:10001` afterwards).
3. Set `STORAGE=nextcloud` in `/srv/app/istota/host.env` and run `/srv/app/istota/src/host/provision.sh`, which starts the mount again.
4. Remove the `MOVED.md` notes, which would otherwise tell users their edits are ignored. A `MOVED.md` that is not a note the move wrote is left alone:

   ```bash
   istota-stack compose run --rm --no-deps -T --entrypoint istota-drop daemon \
       istota workspace moved-note --from /mnt/shared --remove
   ```

5. `istota-stack up`.
6. Remove the memory search entries that name the local copy, which stays on the volume and is not bound into a task on the mount:

   ```bash
   istota-stack exec istota workspace reindex --forget-root /data/workspace
   ```

Anything written to the local workspace after the move is not copied back.

## The commands underneath

`istota-stack localize-workspace` runs four `istota` verbs, which also work on their own inside the stack's containers:

```bash
istota workspace localize --from /mnt/shared --to /data/workspace --scheduler-stopped [--check] [--dry-run] [--resume] [--report PATH]
istota workspace localize-config [--check] [--workspace /data/workspace]
istota workspace moved-note --from /mnt/shared [--remove]
istota workspace reindex --forget-root /mnt/shared
```

`localize` refuses unless the config is in full integration and `--from` is the configured workspace, and without `--scheduler-stopped` unless it is a dry run or `--check`. `localize --check` answers whether the copy would start, including the empty-destination rule, and writes nothing. `localize-config --check` answers whether the config can be moved and writes nothing. `moved-note` refuses while `--from` is still the configured workspace, and exits 2 when a user got no note, or under `--remove` when a `MOVED.md` that is not a move's note was left in place. `reindex` refuses an old root that contains the current workspace.
