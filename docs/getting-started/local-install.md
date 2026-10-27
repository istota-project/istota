# Local install: a Lima VM on your Mac

The local install runs Istota on your own Apple silicon Mac. It is the same stack a server runs: the same image, compose file, `provision.sh`, `istota-stack` and bubblewrap sandbox, inside a [Lima](https://lima-vm.io/) VM that is dedicated to it. The one thing a server does not have is a folder in your home, `~/Istota`, which is the bot's workspace. The VM mounts it, the bot writes its files there, and you open them with any Mac app. Time Machine backs it up, and the database snapshots land in it too.

A command on the Mac, also called `istota-stack`, creates and controls the VM. It handles the VM's own life (create, start, stop, restore) and passes every other verb to the `istota-stack` inside the VM unchanged, so the [server documentation](../deployment/running-the-stack.md) applies as written.

```
Mac                                          Lima VM "istota" (Debian 13)
~/.local/bin/istota-stack ──limactl shell──▶ istota-stack (the server's command)
~/.local/bin/istota       ──limactl shell──▶ istota (the CLI, as uid 10001)
~/Istota/                 ◀── virtiofs ────▶ /srv/mount/host-folder, bound at /mnt/shared
http://localhost:8080     ◀── port forward ─ nginx on the VM's loopback
login Keychain: istota.local / secret-key-istota (a copy of the master key)
```

## Requirements

- A Mac with Apple silicon. An Intel Mac is refused.
- Lima 2.0 or later: `brew install lima`. The installer does not install Homebrew or Lima for you.
- A model backend: a Claude Code OAuth token, an API key, or an OpenAI-compatible server through the [native brain](../configuration/native-brain.md). A model server on the Mac itself needs one more step, [below](#reaching-a-service-on-your-mac).
- Room for the VM. By default it takes half the Mac's CPUs (2 to 8), a quarter of its memory (4 to 16 GiB) and a disk of up to a quarter of the free space (40 to 100 GiB). The disk grows as it is used, so its size is a ceiling. On a terminal, `create` shows these defaults and asks for each one; Enter takes the default. The size is chosen once, when the VM is made.

## Install

As yourself, not with `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --local
```

This installs the newest release, the highest `vX.Y.Z` tag in the repository. The VM checks the tag's signature against the project's release signing key before it runs anything from it, as a server does, and installs nothing when the check fails. `--tag vX.Y.Z` installs a specific release instead, and `--signing-key 'ssh-ed25519 AAAA...'` names another key, for a fork's releases. The key's fingerprint is `SHA256:msQ02X7UFfotEHfGWtElonM/WEQxCXCMBdLhBAjsX88`; `istota-stack shell`, then `awk '{print $3, $4}' /srv/app/istota/allowed_signers | ssh-keygen -lf -`, prints the one the VM uses.

The installer refuses to run as root, on anything but an Apple silicon Mac, or without `limactl` (exit 69, with the `brew install lima` line). It installs `istota-stack` and `istota` into `~/.local/bin` and the Lima template into `~/.local/share/istota/istota.yaml`, prints the `PATH` line to add if `~/.local/bin` is not on yours, and runs `istota-stack create` with the tag and every other flag you gave it:

| Flag | Default | What it sets |
|---|---|---|
| `--workspace DIR` | `~/Istota` | the workspace folder ([rules below](#where-the-folder-may-be)) |
| `--port N` | `8080` | the Mac port the web UI is on, 1024 or higher |
| `--cpus N` | half the Mac's, 2 to 8 | the VM's CPUs, at most the Mac's |
| `--memory N` | a quarter of the Mac's, 4 to 16 | the VM's memory in whole GiB, leaving macOS at least 2 |
| `--disk N` | a quarter of the free disk, 40 to 100 | the most the VM's disk grows to, in whole GiB from 10 |
| `--adopt` | off | use a folder that already holds files |
| `--terminal-setup` | off | answer the setup questions in the terminal rather than in the browser ([setting up](#setting-up)) |
| `--no-setup` | off | stop before the setup questions |
| `--appliance DIR` | off | make the VM from an unpacked release, `<releases>/<tag>/`, with no network: its disk, checked against its signed manifest and unpacked with `zstd`, then its bundle and images archive, which the VM reads from `<releases>` mounted read-only. `update --appliance DIR` installs a later release from the same `<releases>` |
| `--web-setup-in-app` | off | print the setup page's address as `ISTOTA_SETUP_URL=<url>` once it answers, and open no browser (the Mac app) |

The scripts the installer downloads come from `main`, as with any `curl | bash` installer, and are used only to run `create`. Once the VM has verified the tag, `create` replaces them with the copies from the verified checkout, and every later `istota-stack update` does the same. From then on the Mac scripts are covered by the tag's signature.

## What `create` does

1. Refuses a folder in the wrong place, a port something on the Mac already listens on (it names the process), and an existing non-empty folder without `--adopt`. Nothing is written before these checks pass.
2. Creates the folder and an empty `.istota-workspace` marker file at its root.
3. Creates and starts the Lima VM. On first boot the VM clones the repository, verifies the tag against the key, checks it out and runs `host/provision.sh` from it.
4. Writes the VM's `host.env`: `STORAGE=host-folder`, `INGRESS=local`, the block on the Mac's loopback (`HOST_LOOPBACK_BLOCK=1`, `HOST_GATEWAY_ADDR=192.168.5.2`), and `LOCAL_FORWARD_PORT`, the port Lima forwards, which setup publishes nginx and the setup page on.
5. Builds the images with `istota-stack update <tag>`.
6. Runs the setup in the browser ([below](#setting-up)): the VM prints an address and a one-use code, and `create` opens the page once it answers. When you finish it, the VM builds what your answers need, runs `provision.sh` again and starts the stack. With `--terminal-setup`, the setup wizard asks its questions in your terminal instead, with storage, ingress and the `localhost:<port>` hostname already set; `create` then rebuilds when the config needs Claude Code (`check-image` exits 2), runs `provision.sh` again, and starts `istota-stack.service` itself.
7. Refreshes the Mac scripts from the VM's checkout, copies the master key into your login Keychain, and prints a one-use link to set your password, opening it in the browser. After a browser setup you already have a password, and it says so.

If a step fails, `create` prints the step, the command it ran in the VM, its exit status and its last 20 lines, and exits with that status. It never retries a build or a provision on its own. Run `istota-stack create` again, with no arguments, and it continues from the first step whose effect is missing. It keeps what you gave it the first time, and the tag the VM last deployed, in `~/.config/istota-stack/istota.env` (under `$XDG_CONFIG_HOME` when that is set). On a VM that already has a tag deployed, a re-run keeps that tag unless you give `--tag` again; `istota-stack update` is the way to move it on.

### Setting up

`create` sets the stack up in the browser. The VM prints an `http://localhost:<port>/istota/setup#code=…` address, and on a terminal `create` opens it once the page answers; without a terminal (a piped installer) it only prints it. Answer the screens and choose "Only on this computer" for how people reach Istota. The page keeps the Mac folder as the workspace, and its local address is the port Lima forwards. It asks fewer questions than the terminal wizard and can test an API key before saving it. The [web setup](../deployment/running-the-stack.md#in-the-browser) section of the server documentation covers the rest.

Ctrl-C ends the setup and `create` stops; so does closing the page and leaving it for an hour. Nothing is installed, and `istota-stack create` again starts a new setup with a new code. From another shell, `istota-stack setup-cancel` ends a setup that has no terminal to press Ctrl-C in.

`create --terminal-setup` (and `install.sh --local --terminal-setup`) runs the setup wizard in your terminal instead. It asks the same questions a server operator answers.

### Changing the setup

```bash
istota-stack setup --web --force   # the setup page again, filled in with what the stack has now
istota-stack setup --force         # the terminal wizard, every question defaulting to the current answer
```

Both write the VM's `LOCAL_FORWARD_PORT` first when it is not the port this Mac forwards. Neither changes your username, email, display name, time zone or module choices; they are shown as they are, and Settings is where you change them. The browser re-run stops the stack while the page is open and starts it again when you finish. Ctrl-C, `istota-stack setup-cancel` or an idle hour puts the previous setup back and starts the stack on it. Before either re-run writes anything, the current `config.toml` goes to `config.toml.bak.<time>` and `.env`, `host.env` and `rclone.conf` to `/srv/app/istota/backups/setup-<time>/` in the VM, the newest ten of each kept.

A setup re-run writes the whole `config.toml` from its answers, so a setting it does not ask about goes back to its default. For a small change, use the admin Configuration page or `istota-stack apply`.

## The workspace folder

The folder has the layout every install has: `Users/<id>/` (with `istota/`, `inbox/`, `memories/` and `shared/` under it), `Channels/`, `Groups/` and `Backups/`. Your own files are in `~/Istota/Users/<your id>/`, and `istota-stack open --files` shows that folder in Finder. A file you save there is visible inside the VM within a fraction of a second, and the same holds the other way.

Some things stay on the VM's disk and never go into the folder: the framework and module databases, the master key (`/data/.secret_key`), `config/`, `secrets/`, the session transcripts and the Docker volumes. SQLite does not run on the shared folder.

A few behaviours to know about:

- **The marker.** The stack will not start without `.istota-workspace` at the folder's root. If the folder is renamed, deleted or on a volume that is not plugged in, Lima still mounts an empty directory in its place, and the marker is what tells the two apart. On a running stack, `istota doctor` reports the missing marker as a failure, its hourly run alerts the admins, and backups stop. `istota-stack status` says whether the marker is there.
- **Case.** The folder is as case-insensitive as the Mac volume it is on, usually APFS's case-insensitive default. `Notes.md` and `notes.md` are the same file to the bot as well.
- **Both sides editing one file.** If you and a task write the same file at the same time, the last write wins. There is no locking across the VM boundary.
- **Finder's files.** `.DS_Store` files appear in the folder, and the bot may see them in a file listing.

### Where the folder may be

`create` refuses, before it creates anything:

- a folder in iCloud Drive or a cloud storage provider's folder (`~/Library/Mobile Documents`, `~/Library/CloudStorage`): those providers replace files with placeholders the VM reads as empty;
- a folder under `~/Documents`, `~/Desktop` or `~/Downloads`: macOS lets an app reach those only with a grant of its own, and Lima has none;
- `~/.istota`, the standalone install's folder, which `import-standalone` reads;
- your home folder itself, `/`, a folder that contains `~/.lima` or `~/.ssh`, or one inside them.

A link is followed before these checks, and the comparison ignores case. An external volume is allowed. An existing folder that holds files and has no marker is refused unless you pass `--adopt`; with it, the files stay and the bot sees them.

## Day to day

```bash
istota-stack status            # the VM, the deployed tag, the containers, the folder and its marker, autostart
istota-stack open              # the web UI in your browser
istota-stack open --files      # your folder in Finder
istota-stack stop              # stop the stack (at most 60 s), then the VM
istota-stack start             # start the VM; the stack starts with it
istota-stack autostart on      # start the VM when you log in (off turns it back off)
istota-stack shell             # a shell in the VM
istota-stack update            # the newest signed release, then refresh the Mac scripts
istota task "..." -u alice -x  # the istota CLI, in the daemon container as uid 10001
```

`stop` stops the stack before the VM so SQLite closes its files cleanly. `autostart on` runs `limactl autostart enable`, which writes a LaunchAgent that starts the VM at your next login; it does not start or stop a running VM.

Every other verb is the VM's own `istota-stack`, run as root in the VM with its arguments unchanged and its exit status passed back: `rollback`, `setup-cancel`, `exec`, `compose`, `up`, `down`, `check-image` and the rest. `apply <plan>` and `import-bare-metal <dir>` take a path on the Mac: it is copied into a directory of its own in the VM for the call and removed afterwards.

The VM is the Lima instance `istota`, in Lima's default home, so `limactl list` shows it. If you already have an instance of that name, set `ISTOTA_LIMA_INSTANCE` to another name for every `istota-stack` call.

To change a setting, use the admin Configuration page, edit `/srv/app/istota/config/config.toml` in the VM (`istota-stack shell`, then `sudo -e` it) or use `istota-stack apply plan.toml` with a plan on the Mac, and restart with `istota-stack down && istota-stack up`. To go through the setup questions again, see [changing the setup](#changing-the-setup).

`istota-stack status` says when nginx publishes on a port other than the one Lima forwards, which a setup from before `LOCAL_FORWARD_PORT` could leave behind, and names the fix: `istota-stack setup --web --force`, keeping `localhost:<port>` as the address.

### Updating

```bash
istota-stack update            # the newest signed release
istota-stack update vX.Y.Z     # a specific release
istota-stack rollback          # back to the release before
```

These run in the VM, without `sudo` on the Mac. `update` picks the newest `vX.Y.Z` tag; when the stack already runs it, the command says so and changes nothing. Otherwise the VM checks the tag's signature against the key it was created with, and refuses a tag that fails the check without trying an older one. Then it builds the images in the VM from that tag and restarts the stack on them, which takes a few minutes. The config, the credentials, the databases and the folder are kept. After a successful update the Mac scripts are replaced with the copies from the verified checkout.

`update vX.Y.Z` names the release, and rebuilds even when the stack already runs it. `rollback` goes back one release, the one `update` replaced, as long as that release's image can read the database.

### Reaching a service on your Mac

No container in the VM can reach a service listening on your Mac's loopback: your development databases, admin consoles and local model servers stay out of reach. The VM's egress unit drops traffic from every container to the Mac's address as the VM sees it, `192.168.5.2`, except DNS, which Lima serves at that address. To let containers reach one port, for example Ollama for the native brain:

```bash
istota-stack allow-host-port 11434     # Ollama; LM Studio is 1234
istota-stack deny-host-port 11434      # close it again
```

Then point the brain at `http://192.168.5.2:11434/v1`. An allowed port is open to the daemon and the browser container; a devbox cannot reach it.

### Sleep and missed jobs

The VM is paused while the Mac sleeps, and Lima's guest agent resets the VM's clock from the Mac's when it runs again. A scheduled job or briefing whose time passed during sleep runs once after wake, as long as its oldest missed time is under 12 hours ago (`[scheduler] cron_max_staleness_minutes = 720`, which setup writes for the local VM). Several missed runs of one job give one run. A job whose oldest missed run is older than that is skipped and runs at its next scheduled time, so a job every five minutes is skipped after a 13-hour sleep. Nothing keeps the Mac awake while a task runs.

## Backups and recovery

Three things together restore a lost VM, and two of them are on the Mac already:

- **The database snapshots**, which the stack writes into `~/Istota/Backups/db/snapshots/` on its normal backup schedule.
- **The config copies** in `~/Istota/Backups/config/<date>/`: `config.toml` and the admins file, copied with each snapshot, and the stack's `.env` and `host.env` (as `stack.env` and `host.env`), copied on every `update`, `setup` and `apply` once the stack has a config. None of these files holds a credential.
- **The master key**, in your login Keychain as a generic password, service `istota.local`, account `secret-key-istota`. The snapshots cannot be decrypted without it, and it is never in the folder, so a copy of the folder alone does not open the stored credentials.

`create` writes the key through `security -i`, which reads its command on stdin, so the key never appears on a command line where `ps` could show it. It reads the item back and stops if it does not match. If the Keychain already holds a different key for the instance, `create` stops rather than replace it, because that key decrypts an earlier VM's snapshots: rename the old item in Keychain Access first. The login keychain is a file on the Mac, which iCloud Keychain does not sync.

To rebuild from the folder:

```bash
istota-stack restore                       # the newest good snapshot
istota-stack restore --date 2026-10-01     # a given day
```

`restore` checks the folder's marker, the backups, the Keychain item, `create`'s options and, while the old VM runs, that the Keychain's key is that VM's own, before it touches anything. Then it deletes the existing VM, after you type its name, creates a new one without the setup questions, and in it writes the key from the Keychain (on stdin), the config files, the `.env` and `host.env` copies in effect on the restored config's date, and the databases. Then it starts the stack. `--force` restores a snapshot that looks empty. It takes `create`'s options too (`--tag`, `--signing-key`, `--port`, ...), and without them uses the ones `create` recorded and installs the tag the VM last ran, which every `create` and `update` records.

`secrets/` is never copied into the folder, so after a restore the stack's own credentials (the model key, mail and Nextcloud passwords) are empty. Write them again as files, one per credential (the names are in [Credentials](../deployment/running-the-stack.md#credentials)), with `put-files`, which takes JSON on stdin:

```bash
istota-stack put-files < credentials.json    # {"secrets": {"istota_provider_anthropic_api_key": "sk-..."}}
istota-stack down && istota-stack up
```

Credentials you stored through the web UI or `istota secret` are in the database and come back with it.

`istota-stack destroy` deletes the VM after you type its name. It refuses when the Keychain has no copy of the master key, or, while the VM runs, when the copy is not the VM's own key, since the snapshots in your folder cannot be decrypted without it; `--force` deletes regardless. It leaves the folder and the Keychain item, and says so.

`istota-stack destroy --purge` deletes those too, along with the settings `create` saved under `~/.config/istota-stack/`, so nothing is left to restore from. It lists what it will delete and asks for the name, `--force` or not. It deletes only the folder `create` recorded for this VM, and keeps it when `.istota-workspace` is missing from it, when it is reached through a link, or when another VM's settings name it. A folder it keeps keeps the Keychain key too, since the backups in it cannot be read without it. With the VM already gone, it removes what a plain `destroy` left.

## Moving from the standalone install

The standalone install (`~/.istota`, run by `istota serve`) was removed. `import-standalone` moves one into a new local VM, with its history:

```bash
istota-stack import-standalone --dry-run
istota-stack import-standalone
```

Stop `istota serve` first; the import refuses while it runs. `--from DIR` and `--config FILE` name a standalone install and config other than `~/.istota` and `~/.config/istota/config.toml`, and the import takes `create`'s options for the new VM. `--dry-run` needs a running VM and changes nothing.

1. It backs up each database with `sqlite3 .backup`, which is consistent while the database is in WAL mode, and stages them, the config, the admins file and the session transcripts in the VM. It creates the VM first, without the setup questions, when there is none.
2. In the VM it writes a new `config.toml` for the stack: the brain, email, CalDAV and briefing settings and the `[users.*]` blocks are kept, the sandbox is on, and the native login replaces `auth = "none"`. A path under `~/.istota` is moved to the workspace. A setting that pointed anywhere else on the Mac is dropped, since the VM cannot see it, and listed in the report. A loopback URL is listed with the `allow-host-port` it needs.
3. `istota.env` goes in on stdin, never as a file or an argument. The master key (the first `ISTOTA_SECRET_KEY` line, the one `istota serve` used) becomes `/data/.secret_key`, and every credential in the file goes into the stack's secrets files. An older install's `istota.env` may have no master key. The import then makes a new one, unless the database holds credentials encrypted under a key, in which case it refuses and names the tables: put that key back in `istota.env` and run the import again. A name the import does not know is refused by name rather than dropped.
4. It restores the databases and rewrites the paths they store.
5. Back on the Mac it copies your files from `~/.istota` into the workspace folder. Copied, never moved. The databases, scratch, transcripts and snapshots stay behind, and a file already in the folder is never replaced: one that differs is named in the output.
6. It starts the stack, copies the key into the Keychain, prints your sign-in link and runs `istota doctor`.

`~/.istota` is not changed. Only after a clean `doctor` does the import tell you how to move it to the Trash, and it never does that itself. User ids that would collide on a case-insensitive folder (`alice` and `Alice`) are refused before anything is written. Your own files that name `~/.istota` paths, such as a `CRON.md` or a script, are not rewritten; the import prints the `istota migrate workspace-paths --map` command that does it.

A refused import writes nothing: a refusal on the Mac (a running `istota serve`, a missing `istota.env`, a different key already in the Keychain) exits 78, and one from the import in the VM exits 1. A partial import exits 2, and running it again resumes.

## What the VM protects, and what it does not

The local VM's boundary protects your Mac from the VM, not the VM from your Mac. Read [The local VM](../deployment/security.md#the-local-vm) in the security page before you rely on it. In short:

- Anything running as you on the Mac can already run commands as root in the VM (`limactl shell istota sudo ...`), so a Mac process that edits the folder gains nothing it did not have.
- The folder is the only Mac path the VM mounts. A link in it that points elsewhere on the Mac leads to nothing in the VM.
- Inside the VM every process can write anywhere in the folder, whatever its uid. Tasks are kept to their own `Users/<id>/` by the sandbox, as on a server.
- Files the VM writes reach the Mac without the quarantine flag, so macOS does not check a program the bot downloaded before you open it.

## Developing on a checkout

From a checkout of the repository, `host/mac/istota-stack create --repo "$PWD"` makes a VM that mounts the checkout read-only and provisions from it at every boot, and installs `update --unsigned latest` (or the `--tag` you give). Run from a checkout, the script still refreshes `~/.local/bin/istota-stack` and `istota` when an install is there, with the copies from the VM's checkout; set `ISTOTA_MAC_BIN_DIR` to another directory to keep an installed copy as it is. The `vm` test tier uses its own instance, `istota-vm-1`, so the two never meet; [testing](../development/testing.md) covers it. To try a whole deployment by hand next to an install you already run, see [a full deployment on a Mac](../development/setup.md#a-full-deployment-on-a-mac).

## Removing it

```bash
istota-stack autostart off
istota-stack destroy
rm ~/.local/bin/istota-stack ~/.local/bin/istota ~/.local/share/istota/istota.yaml
```

The folder and the Keychain item stay until you delete them yourself, the item in Keychain Access. `istota-stack destroy --purge` in place of `destroy` deletes them as well, files and backups included.
