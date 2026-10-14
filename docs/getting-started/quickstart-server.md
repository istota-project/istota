# Quickstart

Istota has one deployment: the istota stack on a Debian 13 machine of its own, with every task in a bubblewrap sandbox. A VM, a VPS or a bare-metal box all work, as long as it runs nothing else. `install.sh` installs it, `host/provision.sh` sets the machine up, and one command, `istota-stack`, manages it afterwards. Inside, the stack is a set of containers run by Docker Compose (`docker/`). Images are built on the machine from a signed release tag; nothing is pulled from a registry.

The machine has to be dedicated to this install. The stack grants its container what bubblewrap needs to build a namespace, which is only acceptable when nothing else shares the host. [Running tasks sandboxed](../deployment/running-the-stack.md#running-tasks-sandboxed) explains why. For the same reason the stack does not run directly on a workstation, Docker Desktop included. On a Mac, it runs in a Lima VM ([below](#on-a-mac)).

## Requirements

- A fresh Debian 13 machine and root on it
- A model backend: a Claude Code OAuth token, or any OpenAI-compatible endpoint through the [native brain](../configuration/native-brain.md)
- Optional: an existing Nextcloud, with a user and app password for the bot, for full Nextcloud integration with Talk. Without one the workspace is a local directory.

## Install

As root, on the machine:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash -s -- --server
```

This installs the newest release: the highest `vX.Y.Z` tag in the repository. The installer checks the tag's signature against the project's release signing key, which it carries (`host/release-signing-key.pub`), and refuses a branch, an unsigned tag or a tag signed by another key. If the newest tag fails that check, it stops and installs nothing; it never falls back to an older one. Two flags override the defaults: `--tag vX.Y.Z` installs a specific release, and `--signing-key 'ssh-ed25519 AAAA...'` names another key, for a fork's releases. The key is written to `/srv/app/istota/host.env` on the first run, and every later update is checked against that key.

Read the installer before piping it into a shell:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
less install.sh
sudo bash install.sh --server
```

It clones the repository into `/srv/app/istota/src`, picks the newest release tag, verifies its signature before running anything from it, runs `host/provision.sh` (Docker Engine, the AppArmor and seccomp profiles, zram, the journald cap, the systemd units), builds the images with `istota-stack update <tag>`, and starts the setup in the browser (`istota-setup.service`). It ends by printing the address to open, a one-use setup code and the certificate's fingerprint; `sudo istota-stack setup-code` prints them again. When the page finishes, the machine builds what your answers need, runs `provision.sh` again and starts `istota-stack.service`. See [First-run setup](../deployment/running-the-stack.md#first-run-setup).

To set up in the terminal instead, add `--terminal-setup`. The installer then runs the setup wizard with `istota-stack setup`, rebuilds the images when `istota-stack check-image` reports the new config needs Claude Code (the first build ran before the config existed), runs `provision.sh` again for the wizard's host settings, and starts `istota-stack.service`. Without a terminal that path stops after the build and prints the rest.

For a cloud VM, `host/cloud-init.yaml` does the same as user data, as it is: it carries the project's key and installs the newest release. Set `TAG` in it for a specific release, or the repository and the key for a fork. It prints the setup's address, code and fingerprint to the console, which a VPS provider's web console shows.

### Checking the release key

The key's fingerprint is `SHA256:msQ02X7UFfotEHfGWtElonM/WEQxCXCMBdLhBAjsX88`. To compare it with the key the machine verifies against:

```bash
awk '{print $3, $4}' /srv/app/istota/allowed_signers | ssh-keygen -lf -
```

`ssh-keygen -lf` cannot read `allowed_signers` itself, so the command takes the key out of it first. [Security](../deployment/security.md#release-signing) says what the key protects.

## What the wizard asks

The browser setup the installer starts asks the common questions in a few short screens. See [First-run setup](../deployment/running-the-stack.md#first-run-setup). The terminal wizard, which `--terminal-setup` runs and `istota-stack setup` runs on a machine that is already provisioned and built, asks every question, operator settings included.

`istota-stack setup` runs `istota setup` inside the image. It asks for the first admin's user id, the public hostname and how the stack is reached (`local`, `proxied` behind your own reverse proxy, or `direct` with its own TLS certificate), whether to connect to a Nextcloud (its URL, the bot's user and app password, an OAuth2 client for web login), the model backend and its credential, email, GPS location, the modules, the developer skill, and the optional containers.

It writes `config.toml` and the admins file under `/srv/app/istota/config/`, one file per credential under `/srv/app/istota/secrets/` (0400, owned by uid 10001), the stack's `.env` and `host.env`. After that `config.toml` is yours: nothing regenerates it. [Running the stack](../deployment/running-the-stack.md) covers the settings, the [storage modes](../deployment/running-the-stack.md#storage) and [ingress and TLS](../deployment/running-the-stack.md#ingress-and-tls).

## Day to day

```bash
istota-stack update             # the newest signed release
istota-stack update --unsigned latest   # follow main instead: deploy its tip, unsigned
istota-stack rollback           # back to the previous release, when its image can read the database
istota-stack apply plan.toml    # write config.toml from a plan; restarts the stack when it changed
istota-stack up | down          # start or stop istota-stack.service
istota-stack compose logs -f daemon
istota user ensure --name bob --display-name "Bob"   # the istota CLI, run in the daemon container
```

The `istota` command on the machine runs the CLI inside the daemon container as uid 10001, never as root.

`update --unsigned latest` installs the current tip of `main` without a signature check and records it as `main-<commit>`, a name that keeps pointing at that commit, so `rollback` works as it does after a release. Use it only if you trust the repository's `main` branch as much as a release.

## Updating

```bash
sudo istota-stack update          # the newest signed release
sudo istota-stack update vX.Y.Z   # a specific release
sudo istota-stack rollback        # back to the release before
```

`update` fetches the repository's tags and picks the newest `vX.Y.Z`. When the stack already runs it, the command says so and changes nothing. Otherwise it checks the tag's signature against the key in `/srv/app/istota/allowed_signers`, which came from the key given at the first install, and refuses a tag that fails the check without trying an older one. Then it builds the images on the machine from that tag and restarts the stack on them, which takes a few minutes. The config, the credentials and the data in `/srv/app/istota/data` are kept. A build that fails leaves the running stack as it was.

`update vX.Y.Z` names the release, and rebuilds even when the stack already runs it. `rollback` goes back one release, the one `update` replaced, as long as that release's image can read the database. [Running the stack](../deployment/running-the-stack.md#upgrading-an-existing-deployment) has the details.

The stack's state (the databases, the master key and, with local storage, the workspace) is the directory `/srv/app/istota/data`, bound at `/data` in the containers. Back it up. See [State and volumes](../deployment/running-the-stack.md#state-and-volumes).

To change a setting, edit `/srv/app/istota/config/config.toml` and restart with `istota-stack down && istota-stack up`, or, as an admin, change it in the web UI's Configuration pane, which hands the change to the machine to apply ([Changing configuration from the web](../configuration/web-editor.md)). The running services read `config/` read-only. A credential is a file under `/srv/app/istota/secrets/`; replace it and restart.

The optional containers (`browser`, `whatsapp-baileys`, `signaling`) are Compose profiles. The wizard turns them on from your answers by writing `COMPOSE_PROFILES` in `/srv/app/istota/.env`; see [Optional profiles](../deployment/running-the-stack.md#optional-profiles).

## On a Mac

On an Apple silicon Mac the same stack runs in a Lima VM dedicated to it, with a folder in your home (`~/Istota`) as the workspace. `install.sh --local` installs a Mac-side `istota-stack` that creates the VM, runs this page's sequence inside it and forwards every command above to it:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --local
```

Run it as yourself, without `sudo`. [Local install](local-install.md) covers the requirements, the workspace folder, the browser setup, backups to the Keychain and the folder, and moving a standalone install in.

## Managing the machine from Ansible

The install above is the way to install and configure Istota. An estate that already manages its hosts with Ansible can add the optional role in `ansible/` on top, to keep the machine's settings in inventory rather than in files edited on the machine. It writes `host.env`, the secrets and an `istota apply` plan from inventory variables, then runs `provision.sh` and `istota-stack`. See [Ansible management role](../deployment/ansible.md).

## Coming from a bare-metal install

An install made by the old Ansible role moves over with `host/export-bare-metal.sh` on the old host and `istota-stack import-bare-metal` on the new one. See [Migrating a bare-metal install](../deployment/migrating-from-bare-metal.md).

## Next steps

See [post-install](post-install.md) for authenticating Claude and testing.
