# The Mac app

`Istota.app` runs Istota on an Apple silicon Mac with nothing else installed: no Homebrew, no Lima and no terminal. You download it, open it, and it walks you through setup.

Inside, it runs the same kind of Lima VM and the same stack as the [command-line local install](local-install.md), with `~/Istota` as the workspace. The app carries what the first run needs: the VM's disk, the container images and a signed Istota release. Setup needs no network connection and builds nothing. The web UI opens in the app's own window, already signed in, and a menu-bar icon starts, stops and updates Istota.

The app is a separate project from this repository, with version numbers of its own. Each version of the app carries one Istota release, verified against the project's release signing key.

## Requirements

- A Mac with Apple silicon (M1 or later). Intel Macs are not supported.
- macOS 14 or later.
- 25 GB free on the startup disk.

You also need a model backend: a Claude Code OAuth token, an API key, or an OpenAI-compatible server through the [native brain](../configuration/native-brain.md). For Ollama or LM Studio on the Mac itself, let Istota reach its port in the app's Settings, under Add-ons, then point the brain at `http://192.168.5.2:<port>/v1` ([reaching a service on your Mac](local-install.md#reaching-a-service-on-your-mac)).

## Install

1. [Download the newest version](https://stacks.cynium.com/istota-mac/download.html), which saves `Istota-<version>.dmg`; the version is the app's own, not Istota's. The DMG's folder on the same host, `https://stacks.cynium.com/istota-mac/<version>/`, also holds `Istota-<version>.dmg.sha256`; `shasum -a 256 -c Istota-<version>.dmg.sha256` in the folder you saved both to checks the download.
2. Open the DMG and drag Istota into Applications.
3. Open Istota from Applications.

The app is signed with the maintainer's Developer ID and notarized by Apple. macOS asks once whether to open an app downloaded from the internet, and then opens it.

## First run

The app first checks the chip, the macOS version and the free disk space, and names anything missing. With a requirement missing, it offers only Connect to a Remote Istota. Otherwise the Welcome screen offers:

- **Set Up Istota on This Mac.** Choose the folder Istota keeps its files in (`~/Istota` by default) and the port its window opens on (8080, or the next free port). Resources, closed by default, holds the VM's CPUs, memory and disk; the app picks a size from what the Mac can spare. A progress screen shows each step: unpacking the disk, booting, provisioning, loading the images. Cancel stops it, and Resume picks it up again, also at the next launch.
- **Move Your Command-Line Istota into the App.** Offered in place of the first choice when the [command-line local install](local-install.md) is on this Mac. See [Moving a command-line install](#moving-a-command-line-install).
- **Import Your Standalone Istota.** Offered beside Set Up a New Istota when an older standalone install (`~/.istota`) is on this Mac. Its conversations, settings and files come into the new VM, and `~/.istota` is left as it is.
- **Connect to a Remote Istota.** Opens an Istota running on a server in the app's window. Nothing is set up on the Mac.

The folder has the same rules as the command-line install's: not an iCloud or cloud-storage folder, and not under Documents, Desktop or Downloads ([where the folder may be](local-install.md#where-the-folder-may-be)).

Once the VM is up, the web setup page opens in the app's window. It asks for your name, timezone, the model and its key, and email. Keys you enter there go into the VM and are not stored on the Mac. After setup the app signs you in, opens the web UI, and offers to open Istota when you log in. [Post-install](post-install.md) covers what to do next; its commands need the [command-line tools](#command-line-tools).

In the background the app then installs Claude Code and Google Chrome inside the VM, since those cannot be shipped in the app. Istota works without them while they install. The menu and Settings show their progress, with a Retry button if one fails. An outbound firewall such as Little Snitch may hold these downloads until you allow the app's `limactl` to connect.

## Day to day

The status window opens when you start the app (not when macOS opens it at login) and says what Istota is doing. Closing it, or any other window, leaves Istota running, and the app lives in the menu bar. The menu-bar menu opens Istota and its folder in Finder, starts, stops and restarts it, and holds Settings, Check for Updates and Show Logs. Quit Istota stops the stack and the VM, after asking when tasks are running.

On this Mac the app signs you in with a one-use code from the VM, so there is no password to type. Email sign-in in the web UI stays on, for a phone or another browser. Settings shows the folder and changes the port and the VM's size. It also holds remote profiles, the command-line tools, the background installs, which local model servers Istota may reach, restoring from a backup and removing the VM.

Only one Istota runs on a Mac at a time. If the command-line install, or anything else, holds the app's port, the app says so and starts nothing.

### Updates

The app looks for a new version when it starts and once a day, and the menu shows "Update to …" when one is published. Nothing is installed until you choose it. An update is a download of about 1.7 GB. It replaces the app, and the new app installs the Istota release it carries into the VM without downloading anything else. Config, credentials, databases and the folder are kept. Settings has Roll Back to … after an update, which returns the VM to the release it ran before.

An update is installed only when its signature verifies against the update key built into the app. The Istota release inside it is installed only when its tag and images verify against the release key the VM was set up with.

### Command-line tools

Settings, under Command-Line Tools, installs `istota-stack` and `istota` for Terminal, into `/usr/local/bin` (after an administrator prompt) or `~/.local/bin`. They work on the app's VM, so `istota-stack status`, `istota-stack exec`, `istota user list` and the rest of the [local install](local-install.md#day-to-day) and [server](../deployment/running-the-stack.md) documentation apply.

## Backups and recovery

Backups work as in the [command-line install](local-install.md#backups-and-recovery): database snapshots and config copies go into the folder's `Backups/`, and a copy of the master key goes into your login Keychain. Settings, under Maintenance, has Restore…, from the newest backup or the backup of a given day. As with `istota-stack restore`, credentials the stack keeps as files (the model key, mail and Nextcloud passwords) are not in the backups, and you enter them again in the web UI's settings. An admin can keep them with the encrypted configuration backup in the web UI ([changing configuration from the web](../configuration/web-editor.md)).

## Moving a command-line install

The app keeps its VM apart from the command-line install's, under `~/Library/Application Support/Istota`, and uses the same folder and the same Keychain item. Moving stops the command-line install's VM, then makes the app's VM from the backups in the folder and the master key in the Keychain, as `istota-stack restore` does. If the old VM is running when you start, the app reads its credentials first and writes them into the new one; otherwise you enter them again in the web UI.

When the command-line install is running, the app first has it back up its databases, so the move carries them as they are at that moment. If that backup fails, or a database comes out empty, the app refuses the move and leaves the old install running. A stopped install is not started for this: the move restores its newest backup, and the form says when that backup was written. Backups are daily by default, so conversations, tasks and other database changes made after it are not carried over. Files in the folder always are. If you used the command-line install since its last backup, start it before moving it.

The app refuses the move, and leaves the command-line install running, when that install runs a newer Istota release than the one the app carries. Update the app first.

The old VM is left stopped, not deleted. If you turned on `istota-stack autostart`, run `istota-stack autostart off` so it does not start at your next login and hold the port. Once Istota works in the app, delete the old VM with the command-line install's own `istota-stack destroy`, without `--purge`. `--purge` would also delete the folder and the Keychain key the app now uses, and the app's backups cannot be read without that key. Then remove that install's commands from `~/.local/bin`, since the app's command-line tools never replace a file they did not write.

## Removing it

1. In Settings, under Command-Line Tools, remove the tools if you installed them.
2. In Settings, under Maintenance, choose Remove Istota…, which deletes the VM. The folder, its backups and the master key in your login Keychain stay, so a restore can bring the install back.
3. Quit Istota and drag it to the Trash.
4. Delete `~/Library/Application Support/Istota`, which holds the app's state, its logs and copies of the releases it installed, and `~/Library/WebKit/org.istota.desktop`.

Delete the folder, and the `istota.local` item in Keychain Access, only when you want nothing left to restore from.

## Security

The app runs the local VM in a window, so [The local VM](../deployment/security.md#the-local-vm) and [The Mac app](../deployment/security.md#the-mac-app) in the security page apply. The web UI's windows have no bridge into the app, a link to another site opens in your default browser, and each remote profile keeps its own cookies.

## Choosing between the app and the command line

Use the app unless you want to drive the VM from Terminal only, pick the release tag or signing key yourself, or develop on a checkout of this repository. The [command-line local install](local-install.md) covers those. It needs Homebrew and Lima, downloads the release and builds the images in the VM, and runs the same kind of VM.
