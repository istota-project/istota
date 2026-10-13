# Local install: a Lima VM on your Mac

The local install runs Istota on your own Apple silicon Mac. It is the same stack a server runs: the same image, compose file, `provision.sh`, `istota-stack` and bubblewrap sandbox, inside a [Lima](https://lima-vm.io/) VM that is dedicated to it. The one thing a server does not have is a folder in your home, `~/Istota`, which is the bot's workspace. The VM mounts it, the bot writes its files there, and you open them with any Mac app. Time Machine backs it up, and the database snapshots land in it too.

A command on the Mac, also called `istota-stack`, creates and controls the VM. It handles the VM's own life (create, start, stop, restore) and passes every other verb to the `istota-stack` inside the VM unchanged, so the [server documentation](../deployment/docker.md) applies as written.

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
- A release tag and the release signing key (one line of an SSH public key). The VM checks the tag's signature before it runs anything from it, as a server does.
- A model backend: a Claude Code OAuth token, an API key, or an OpenAI-compatible server through the [native brain](../configuration/native-brain.md). A model server on the Mac itself needs one more step, [below](#reaching-a-service-on-your-mac).
- Room for the VM: 4 CPUs, 4 GiB of memory and a 40 GiB disk by default.

## Install

As yourself, not with `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh \
  | bash -s -- --local --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases'
```

`--tag` and `--signing-key` are both required. The installer refuses to run as root, on anything but an Apple silicon Mac, or without `limactl` (exit 69, with the `brew install lima` line). It installs `istota-stack` and `istota` into `~/.local/bin` and the Lima template into `~/.local/share/istota/istota.yaml`, prints the `PATH` line to add if `~/.local/bin` is not on yours, and runs `istota-stack create` with the tag, the key and every other flag you gave it:

| Flag | Default | What it sets |
|---|---|---|
| `--workspace DIR` | `~/Istota` | the workspace folder ([rules below](#where-the-folder-may-be)) |
| `--port N` | `8080` | the Mac port the web UI is on, 1024 or higher |
| `--cpus N` | `4` | the VM's CPUs |
| `--memory N` | `6` | the VM's memory, in whole GiB |
| `--adopt` | off | use a folder that already holds files |
| `--no-setup` | off | stop before the setup questions ([setting up in the browser](#setting-up-in-the-browser)) |

The scripts the installer downloads come from `main`, as with any `curl | bash` installer, and are used only to run `create`. Once the VM has verified the tag, `create` replaces them with the copies from the verified checkout, and every later `istota-stack update` does the same. From then on the Mac scripts are covered by the tag's signature.

## What `create` does

1. Refuses a folder in the wrong place, a port something on the Mac already listens on (it names the process), and an existing non-empty folder without `--adopt`. Nothing is written before these checks pass.
2. Creates the folder and an empty `.istota-workspace` marker file at its root.
3. Creates and starts the Lima VM. On first boot the VM clones the repository, verifies the tag against the key, checks it out and runs `host/provision.sh` from it.
4. Writes the VM's `host.env`: `STORAGE=host-folder`, `INGRESS=local`, and the block on the Mac's loopback (`HOST_LOOPBACK_BLOCK=1`, `HOST_GATEWAY_ADDR=192.168.5.2`).
5. Builds the images with `istota-stack update <tag>`.
6. Runs the setup wizard in your terminal, with storage, ingress and the `localhost:<port>` hostname already set. It asks the same questions a server operator answers.
7. Rebuilds when the config needs Claude Code (`check-image` exits 2), runs `provision.sh` again, and starts `istota-stack.service`.
8. Refreshes the Mac scripts from the VM's checkout, copies the master key into your login Keychain, and prints a one-use link to set your password, opening it in the browser.

If a step fails, `create` prints the step, the command it ran in the VM, its exit status and its last 20 lines, and exits with that status. It never retries a build or a provision on its own. Run `istota-stack create` again, with no arguments, and it continues from the first step whose effect is missing; it keeps what you gave it the first time in a file under `~/.config/istota-stack/`.

### Setting up in the browser

The [web setup](../deployment/docker.md#in-the-browser) works in the local VM too. It asks fewer questions than the terminal wizard, and can test an API key before saving it:

```bash
istota-stack create --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases' --no-setup
istota-stack setup --web --local
```

`setup --web --local` prints an `http://localhost:8080/istota/setup#code=…` address. Open it on the Mac and answer the screens; choose "Only on this computer" for how people reach Istota. The page keeps the Mac folder as the workspace. When it finishes, the VM builds what your answers need and starts the stack on the same address. Then run `istota-stack create` once more, with no arguments: it skips the steps that are done, copies the master key into the Keychain and refreshes the Mac scripts. Until you do, there is no copy of the key outside the VM.

The setup page listens on the VM's port 8080, so this works with the default `--port`. With another port, use the terminal wizard.

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
istota-stack update vX.Y.Z     # verify, build and switch to a release, then refresh the Mac scripts
istota task "..." -u alice -x  # the istota CLI, in the daemon container as uid 10001
```

`stop` stops the stack before the VM so SQLite closes its files cleanly. `autostart on` runs `limactl autostart enable`, which writes a LaunchAgent that starts the VM at your next login; it does not start or stop a running VM.

Every other verb is the VM's own `istota-stack`, run as root in the VM with its arguments unchanged and its exit status passed back: `setup`, `rollback`, `exec`, `compose`, `up`, `down`, `check-image` and the rest. `apply <plan>` and `import-bare-metal <dir>` take a path on the Mac: it is copied into a root-only directory in the VM for the call and removed afterwards.

The VM is the Lima instance `istota`, in Lima's default home, so `limactl list` shows it. If you already have an instance of that name, set `ISTOTA_LIMA_INSTANCE` to another name for every `istota-stack` call.

To change a setting, edit `/srv/app/istota/config/config.toml` in the VM (`istota-stack shell`, then `sudo -e` it) or use `istota-stack apply plan.toml` with a plan on the Mac, and restart with `istota-stack down && istota-stack up`.

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

`secrets/` is never copied into the folder, so after a restore the stack's own credentials (the model key, mail and Nextcloud passwords) are empty. Write them again as files, one per credential (the names are in [Credentials](../deployment/docker.md#credentials)), with `put-files`, which takes JSON on stdin:

```bash
istota-stack put-files < credentials.json    # {"secrets": {"anthropic_api_key": "sk-..."}}
istota-stack down && istota-stack up
```

Credentials you stored through the web UI or `istota secret` are in the database and come back with it.

`istota-stack destroy` deletes the VM after you type its name. It refuses when the Keychain has no copy of the master key, or, while the VM runs, when the copy is not the VM's own key, since the snapshots in your folder cannot be decrypted without it; `--force` deletes regardless. It leaves the folder and the Keychain item, and says so.

`istota-stack destroy --purge` deletes those too, along with the settings `create` saved under `~/.config/istota-stack/`, so nothing is left to restore from. It lists what it will delete and asks for the name, `--force` or not. It deletes only the folder `create` recorded for this VM, and keeps it when `.istota-workspace` is missing from it, when it is reached through a link, or when another VM's settings name it. A folder it keeps keeps the Keychain key too, since the backups in it cannot be read without it. With the VM already gone, it removes what a plain `destroy` left.

## Moving from the standalone install

`import-standalone` moves a [standalone install](standalone-install.md) (`~/.istota`) into a new local VM, with its history:

```bash
istota-stack import-standalone --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases' --dry-run
istota-stack import-standalone --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases'
```

Stop `istota serve` first; the import refuses while it runs. `--from DIR` and `--config FILE` name a standalone install and config other than `~/.istota` and `~/.config/istota/config.toml`, and the import takes `create`'s options for the new VM. `--dry-run` needs a running VM and changes nothing.

1. It backs up each database with `sqlite3 .backup`, which is consistent while the database is in WAL mode, and stages them, the config, the admins file and the session transcripts in the VM. It creates the VM first, without the setup questions, when there is none.
2. In the VM it writes a new `config.toml` for the stack: the brain, email, CalDAV and briefing settings and the `[users.*]` blocks are kept, the sandbox is on, and the native login replaces `auth = "none"`. A path under `~/.istota` is moved to the workspace. A setting that pointed anywhere else on the Mac is dropped, since the VM cannot see it, and listed in the report. A loopback URL is listed with the `allow-host-port` it needs.
3. `istota.env` goes in on stdin, never as a file or an argument. The master key (the first `ISTOTA_SECRET_KEY` line, the one `istota serve` used) becomes `/data/.secret_key`, and every credential in the file goes into the stack's secrets files. An older install's `istota.env` may have no master key. The import then makes a new one, unless the database holds credentials encrypted under a key, in which case it refuses and names the tables: put that key back in `istota.env` and run the import again. A name the import does not know is refused by name rather than dropped.
4. It restores the databases and rewrites the paths they store.
5. Back on the Mac it copies your files from `~/.istota` into the workspace folder. Copied, never moved. The databases, scratch, transcripts and snapshots stay behind, and a file already in the folder is never replaced: one that differs is named in the output.
6. It starts the stack, copies the key into the Keychain, prints your sign-in link and runs `istota doctor`.

`~/.istota` is not changed. Only after a clean `doctor` does the import tell you how to move it to the Trash, and it never does that itself. User ids that would collide on a case-insensitive folder (`alice` and `Alice`) are refused before anything is written. Your own files that name `~/.istota` paths, such as a `CRON.md` or a script, are not rewritten; the import prints the `istota migrate workspace-paths --map` command that does it.

A refused import exits 1 with nothing written; a partial one exits 2, and running it again resumes.

## What the VM protects, and what it does not

The local VM's boundary protects your Mac from the VM, not the VM from your Mac. Read [The local VM](../deployment/security.md#the-local-vm) in the security page before you rely on it. In short:

- Anything running as you on the Mac can already run commands as root in the VM (`limactl shell istota sudo ...`), so a Mac process that edits the folder gains nothing it did not have.
- The folder is the only Mac path the VM mounts. A link in it that points elsewhere on the Mac leads to nothing in the VM.
- Inside the VM every process can write anywhere in the folder, whatever its uid. Tasks are kept to their own `Users/<id>/` by the sandbox, as on a server.
- Files the VM writes reach the Mac without the quarantine flag, so macOS does not check a program the bot downloaded before you open it.

## Developing on a checkout

From a checkout of the repository, `host/mac/istota-stack create --repo "$PWD"` makes a VM that mounts the checkout read-only and provisions from it at every boot, and installs `update --unsigned latest` (or the `--tag` you give). Run from a checkout, the script does not refresh any installed copy. The `vm` test tier uses its own instance, `istota-vm-1`, so the two never meet; [testing](../development/testing.md) covers it. To try a whole deployment by hand next to an install you already run, see [a full deployment on a Mac](../development/setup.md#a-full-deployment-on-a-mac).

## Removing it

```bash
istota-stack autostart off
istota-stack destroy
rm ~/.local/bin/istota-stack ~/.local/bin/istota ~/.local/share/istota/istota.yaml
```

The folder and the Keychain item stay until you delete them yourself, the item in Keychain Access. `istota-stack destroy --purge` in place of `destroy` deletes them as well, files and backups included.
