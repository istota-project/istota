# The Mac app

`Istota.app` runs Istota on an Apple silicon Mac with nothing else installed: no Homebrew, no Lima, no terminal. It is the [local install](local-install.md) with a window and a menu-bar icon. Inside, it is the same Lima VM running the same stack, with `~/Istota` as the workspace. What the app adds is that everything the first run needs comes inside the app, so setting up needs no network connection, and the web UI opens in the app's own window, signed in.

## Requirements

- A Mac with Apple silicon (M1 or later).
- macOS 14 or later.
- 25 GB free on the startup disk.

## First run

Open the app. It checks the three requirements above and names any that are missing. Then it offers one of three things:

- **Set up Istota on this Mac.** Choose the folder Istota keeps its files in (`~/Istota` by default; not an iCloud folder, and not Documents, Desktop or Downloads), and the app makes the VM from the disk and images it carries. A progress screen shows each step: unpacking the disk, booting, provisioning, loading the images. Cancel stops it; Resume picks it up again.
- **Move your command-line Istota into the app.** Offered when a [command-line install](local-install.md) is on this Mac. See [below](#moving-a-command-line-install).
- **Import your standalone Istota.** Offered when an older standalone install (`~/.istota`) is on this Mac. Its conversations, settings and files come into the new VM; `~/.istota` is left as it is.

Once the VM is up, the web setup page opens in the app's window. It asks for your name, timezone, the model and its key, and email. Keys you enter there go into the VM and are not stored on the Mac.

After setup the app signs you in and opens the web UI, and offers to open Istota when you log in. In the background it installs Claude Code and Google Chrome inside the VM, which cannot be shipped in the app. Istota works without them. The menu and Settings show their progress, with a Retry button if one fails. An outbound firewall such as Little Snitch can hold those downloads until you allow the app's `limactl` to connect.

## Signing in

On this Mac the app signs you in with a one-use code it asks the VM for, so there is no password to type. Only your macOS account can ask for one. The web UI's email sign-in stays on, for a phone or another browser.

## Moving a command-line install

The app keeps its VM apart from the command-line install's: its own Lima directory under `~/Library/Application Support/Istota`, and its own state. Moving stops the command-line install's VM, then makes the app's VM from the backups in its folder and the master key in your login Keychain, as `istota-stack restore` does. The old VM is left stopped, not deleted.

The moved install becomes the app's: login codes are added to its sign-in methods, and it gets the same settings a new app install has. Anything the old setup did not write is kept. Credentials stored only in the old VM's secret files (the model's key, mail and Nextcloud passwords) are not in the backups, so enter them again in the web UI's settings.

Once Istota works in the app, delete the old VM in Terminal with the command-line install's own `istota-stack destroy`. Without `--purge` that leaves the folder, which the app now uses.

## Remote installs

The app also opens an Istota running on a server. Add one in Settings, under Profiles, or with "Connect to a remote Istota" on the first-run screen: give it a short name (lowercase letters, digits and dashes) and its address, which must be `https://`, or `http://` to this Mac only. Sign in as that install asks. Nothing is set up on this Mac for a remote install.

Each remote install keeps its own cookies and site data in the app, so signing in to one never signs you in to another. When there is more than one profile, the menu lists them and switches the window between them; the window's title names the one open. Removing a profile in Settings also deletes its site data.

## Command-line tools

Settings, under Command-line tools, installs `istota-stack` and `istota` for Terminal. They work on the app's VM: `istota-stack status`, `istota-stack exec`, `istota user list` and the rest of what the [local install](local-install.md) and the [server documentation](../deployment/running-the-stack.md) describe. Two places are offered:

- `/usr/local/bin`, which macOS asks an administrator to approve once.
- `~/.local/bin`, which needs no approval. If your shell does not find the commands, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zprofile`.

The tools find the app wherever it is, so moving the app to another folder needs nothing more than opening it once. The app never replaces a file of the same name it did not write. On a Mac with the command-line install, `~/.local/bin/istota-stack` is that install's own command, so choose `/usr/local/bin`, or remove the command-line install first.

## Settings

- **Profiles** and **Command-line tools**, above.
- **Background installs**: the state of Claude Code and Chrome, Retry, and "Don't install Claude Code".
- **Local models**: model servers listening on this Mac (Ollama, LM Studio), and whether Istota may reach each one's port.
- **Restore from backup**: makes the VM again from the backups in the workspace folder, the newest or a given day's.
- **Remove Istota**: deletes the VM. The folder, its backups and the master key in your login Keychain stay, so a restore can bring it back.

The model, its key and your other credentials are in Istota's own settings, in the web UI.

## Quitting

Closing the window leaves Istota running. Quit Istota in the menu stops the stack and the VM, after asking when tasks are running.
