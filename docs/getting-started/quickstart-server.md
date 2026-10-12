# Quickstart

Istota runs one way: the Docker stack from `docker/`, on a Debian 13 machine of its own, with every task in a bubblewrap sandbox. A VM, a VPS or a bare-metal box all work, as long as it runs nothing else. `host/provision.sh` sets the machine up and one command, `istota-stack`, manages it afterwards. Images are built on the machine from a signed release tag; nothing is pulled from a registry.

The machine has to be dedicated to this install. The stack grants its container what bubblewrap needs to build a namespace, which is only acceptable when nothing else shares the host. [Running tasks sandboxed](../deployment/docker.md#running-tasks-sandboxed) explains why. For the same reason the stack does not run directly on a workstation, Docker Desktop included. On a Mac, run it in a Lima VM ([below](#on-a-mac)).

## Requirements

- A fresh Debian 13 machine and root on it
- A release tag and the release signing key (one line of an SSH public key). Every tag `istota-stack update` installs is checked against that key, and a branch, an unsigned tag or a tag signed by another key is refused.
- A model backend: a Claude Code OAuth token, or any OpenAI-compatible endpoint through the [native brain](../configuration/native-brain.md)
- Optional: an existing Nextcloud, with a user and app password for the bot, for full Nextcloud integration with Talk. Without one the workspace is a local directory.

## Install

As root, on the machine:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh \
  | sudo bash -s -- --server --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases'
```

Read the installer before piping it into a shell:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
less install.sh
sudo bash install.sh --server --tag vX.Y.Z --signing-key 'ssh-ed25519 AAAA... releases'
```

It clones the repository into `/srv/app/istota/src`, verifies the tag's signature before running anything from it, runs `host/provision.sh` (Docker Engine, the AppArmor and seccomp profiles, zram, the journald cap, the systemd units), builds the images with `istota-stack update <tag>`, runs the setup wizard with `istota-stack setup`, rebuilds the images when `istota-stack check-image` reports the new config needs Claude Code (the first build ran before the config existed), runs `provision.sh` again for the wizard's host settings, and starts `istota-stack.service`. Without a terminal it stops after the build and prints the rest.

For a cloud VM, `host/cloud-init.yaml` does the same first half as user data: fill in the signing key, the repository and the tag, and run the printed steps once the machine is up.

## What the wizard asks

`istota-stack setup` runs `istota setup` inside the image. It asks for the first admin's user id, the public hostname and how the stack is reached (`local`, `proxied` behind your own reverse proxy, or `direct` with its own TLS certificate), whether to connect to a Nextcloud (its URL, the bot's user and app password, an OAuth2 client for web login), the model backend and its credential, email, GPS location, the modules, the developer skill, and the optional containers.

It writes `config.toml` and the admins file under `/srv/app/istota/config/`, one file per credential under `/srv/app/istota/secrets/` (0400, owned by uid 10001), the stack's `.env` and `host.env`. After that `config.toml` is yours: nothing regenerates it. [Running the stack](../deployment/docker.md) covers the settings, the [storage modes](../deployment/docker.md#storage) and [ingress and TLS](../deployment/docker.md#ingress-and-tls).

## Day to day

```bash
istota-stack update vX.Y.Z      # verify, build and switch to a new release
istota-stack rollback           # back to the previous release, when its image can read the database
istota-stack apply plan.toml    # write config.toml from a plan; restarts the stack when it changed
istota-stack up | down          # start or stop istota-stack.service
istota-stack compose logs -f daemon
istota user ensure --name bob --display-name "Bob"   # the istota CLI, run in the daemon container
```

The `istota` command on the machine runs the CLI inside the daemon container as uid 10001, never as root.

To change a setting, edit `/srv/app/istota/config/config.toml` and restart with `istota-stack down && istota-stack up`. A credential is a file under `/srv/app/istota/secrets/`; replace it and restart.

The optional containers (`browser`, `whatsapp-baileys`, `signaling`) are Compose profiles. The wizard turns them on from your answers by writing `COMPOSE_PROFILES` in `/srv/app/istota/.env`; see [Optional profiles](../deployment/docker.md#optional-profiles).

## On a Mac

`host/lima/istota.yaml` is a [Lima](https://lima-vm.io/) template for a VM dedicated to one install: Debian 13 under Apple's virtualization framework, with 4 CPUs, 6 GiB of memory and a 40 GiB disk. It mounts an istota checkout from the Mac read-only and runs `host/provision.sh` from it on first boot, so the VM is set up exactly as a server would be. Lima forwards the VM's port 8080 to the Mac's loopback, and the browser container builds natively for arm64. From a checkout of the repository on the Mac:

```bash
limactl create --name istota --param ISTOTA_REPO="$PWD" host/lima/istota.yaml
limactl start istota
limactl shell istota sudo istota-stack update --unsigned vX.Y.Z
limactl shell istota sudo istota-stack setup --ingress local --hostname localhost:8080
limactl shell istota sudo /srv/app/istota/src/host/provision.sh
limactl shell istota sudo systemctl enable --now istota-stack
```

The VM's own checkout at `/srv/app/istota/src` is a clone of the mounted one, so `istota-stack update` fetches tags from the Mac checkout. `--unsigned` is needed until `/srv/app/istota/host.env` names a `RELEASE_SIGNING_KEY`; with one, drop the flag and the tag is verified as it is on a server. Then open `http://localhost:8080` and sign in. Everything else on this page applies inside the VM: prefix a command with `limactl shell istota sudo`, or open a shell there with `limactl shell istota`.

## Managing the machine from Ansible

An estate that manages hosts with Ansible can use the optional role in `ansible/` instead of editing files on the machine. It writes `host.env`, the secrets and an `istota apply` plan from inventory variables, then runs `provision.sh` and `istota-stack`. See [Ansible management role](../deployment/ansible.md).

## Coming from a bare-metal install

An install made by the old Ansible role moves over with `host/export-bare-metal.sh` on the old host and `istota-stack import-bare-metal` on the new one. See [Migrating a bare-metal install](../deployment/migrating-from-bare-metal.md).

## Next steps

See [post-install](post-install.md) for authenticating Claude and testing.
