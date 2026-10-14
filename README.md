<p align="center">
  <img src="web/static/logo-512.png" alt="Istota's octopus mark" width="160">
</p>

<h1 align="center">Istota</h1>

<p align="center"><strong>An open-source personal operating system with an AI agent and a first-class web UI.</strong></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/istota-project/istota" alt="EUPL-1.2 license"></a>
  <a href="https://github.com/istota-project/istota/commits/main"><img src="https://img.shields.io/github/last-commit/istota-project/istota?logo=github" alt="Last commit"></a>
  <a href="https://istota.cynium.com/docs"><img src="https://img.shields.io/badge/docs-istota.cynium.com-blue" alt="Documentation"></a>
</p>

Istota runs on your own server or in a VM on your Mac. It combines chat, persistent memory, background jobs, and web applications for feeds, briefings, health, location, and money. The agent works across these through a maintained set of tools, with filesystem isolation, network restrictions, and credential handling enforced by the runtime. You choose the model and the services it can access.

Istota has been in active development since February 2026. The application, task history, and personal records live on infrastructure you control, and the installer brings up a working system.

[Documentation](https://istota.cynium.com/docs/) · [Security model](docs/deployment/security.md) · [Changelog](CHANGELOG.md) · [EUPL-1.2](LICENSE)

## Install

Choose a fresh Debian 13 machine dedicated to Istota, or an Apple silicon Mac with Lima. Both run the same container stack and Linux task sandbox. The machine must be dedicated because the container gets the access bubblewrap needs to build the sandbox, which is only acceptable with nothing else on the host. Have a model backend ready: an Anthropic or OpenRouter API key, or a Claude subscription token. Nextcloud is optional.

### Server

On a dedicated Debian 13 VM, VPS, or bare-metal machine:

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
less install.sh
sudo bash install.sh --server
```

The installer verifies a signed release tag, provisions the machine, builds the images, and prints an address and one-use code for browser setup. The setup page configures your first user, model, storage, and connections, then starts the stack. The setup page uses a self-signed certificate, so your browser warns before opening it; compare the certificate's fingerprint with the one the installer printed. Add `--terminal-setup` to use the terminal wizard instead.

[Server quickstart](docs/getting-started/quickstart-server.md)

### Local on a Mac

Requires Apple silicon and Lima 2.0 or later. With Homebrew installed:

```bash
brew install lima
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
less install.sh
bash install.sh --local
```

Run this as your normal user. It creates a dedicated Debian VM, runs setup in the terminal, and opens a web link to set your password. The web app is available at `http://localhost:8080/istota/`. Your workspace is `~/Istota`, accessible from Finder; databases stay inside the VM, with snapshots written to the workspace. A copy of the credential master key is stored in your login Keychain.

[Local install, requirements, and recovery](docs/getting-started/local-install.md)

The older standalone single-user install was removed. `istota-stack import-standalone` moves an existing one into the local VM; see [moving from the standalone install](docs/getting-started/local-install.md#moving-from-the-standalone-install).

### After setup

Start in web chat. Chat, memory, task execution, and scheduling are included. Modules are on by default; turn off any you don't use, and connect accounts for email, calendars, or other external services. Their integrations ship with Istota; you still supply the accounts and credentials.

Use `sudo istota-stack update` on the server or `istota-stack update` on the Mac to install the newest signed release. Updates build on your machine and preserve configuration and data. `istota-stack rollback` (with `sudo` on the server) returns to the previous release when its image can read the current database.

Istota is pre-1.0 software. Configuration and schemas can change between releases; read the [changelog](CHANGELOG.md) before upgrading. A server can keep background jobs running around the clock. A local VM pauses while the Mac sleeps.

## A complete system around the agent

The applications hold your records, the scheduler runs background work, and the agent uses their tools under a shared permission model, with documented interfaces and ways to inspect the result.

- **A maintained toolset.** Calendar, email, files, browsing, memory, scheduling, and the native modules ship with documented commands. You do not need to assemble a plugin collection to get the basic system working. Executable skills share the `istota-skill <skill> <command>` interface, with user scoping and credential access enforced behind it.
- **Durable tasks.** Requests enter a SQLite queue before execution. Task status, retries, confirmations, cancellation, streamed events, and usage records follow the request. Separate foreground and background workers keep scheduled work from occupying every conversational worker.
- **Work between conversations.** Reminders, recurring prompts, briefings, and heartbeat checks run through the scheduler. Repeatable operations can run as scheduled commands without calling a model.
- **Memory you can inspect.** Markdown memory, conversation search, a temporal knowledge graph, and reusable playbooks carry context between tasks. Memory is scoped to users, rooms, and groups.

The toolset is extensible. Operators can override a skill's instructions, users can add their own instructions to a skill, and contributors can add new skills. See the [skills index](docs/reference/skills-index.md), [per-skill overlays](docs/configuration/per-user.md#per-skill-overlays), and [adding skills](docs/development/adding-skills.md).

## A web UI for the work and its records

The responsive SvelteKit web app is a primary interface. Use chat to delegate work, then use the module pages to inspect and edit the underlying records.

| Area | What is included |
| --- | --- |
| Chat | Rooms, streamed replies, attachments, voice notes, message replies, and task progress. |
| Feeds and briefings | An RSS/Atom reader with Tumblr and Are.na support, plus saved briefings assembled from sources such as feeds, newsletters, calendars, and the web. |
| Health | Measurements, bloodwork, biomarker trends, encounters, immunizations, source documents, and Garmin data. |
| Location | Maps, GPS history, detected visits, and saved places. |
| Money | A Beancount ledger with transaction and account views, financial reports, portfolio history, invoices, and work records. |
| Search and notifications | Search across configured sources and an inbox for failures, pending work, and other items that need attention. |
| Settings and administration | Preferences, delivery, connections, credentials, and wallet; for admins, service health, usage, logs, and configuration editing. |

The record views and ordinary module operations work without a model call. The agent can query several modules in one task: prepare a meeting brief from calendar events and correspondence, compare a trip's visits with its expenses, or turn unread feeds into a morning briefing. See the [web interface](docs/features/web-interface.md) documentation.

You can also reach the agent through [email](docs/features/email.md), [WhatsApp](docs/features/whatsapp.md), [SMS](docs/features/sms.md), [Nextcloud Talk](docs/features/talk.md), or the [CLI](docs/reference/cli.md). These use the same task system; each external service needs its own connection setup.

## Isolation and credential handling

The supported deployment runs on a dedicated Linux machine or VM. Inside it, the task sandbox limits what model-driven tools can reach.

- **Filesystem isolation.** Bubblewrap exposes the task's allowed workspace and resources. Framework and module databases are hidden from sandboxed processes; access goes through user-scoped operations outside the sandbox. A Linux deployment refuses to start if it cannot build the sandbox.
- **Network isolation.** Sandboxed processes have no direct network access. Outbound connections pass through a proxy with an allowed destination list. Host-side skills, web fetching, and browser services have separate access policies, described in the security documentation.
- **Credential separation.** Skill credentials are injected into host-side operations. The credential broker lets sandboxed tools use placeholders that are replaced with secrets only for authorized destinations. Broker and reveal enforcement are on by default; explicitly readable entries are an exception.
- **Resource containment.** Task cgroups bound memory, CPU, and process use. With the optional devbox enabled, development builds run in a separate per-user container. Tasks receive no Docker socket.

These controls limit the damage a mistaken or manipulated model can cause. They do not make every permitted action correct or prevent data from leaving through an authorized service. Confirmation requirements and service permissions still matter. The operator controls the host and remains trusted.

On a Mac, these controls run inside the Lima VM. Running the development server (`uv run istota serve`) directly on macOS does not provide the same isolation.

Read the [security model](docs/deployment/security.md) and [credential broker documentation](docs/configuration/credentials.md#credential-broker) for the boundaries and exceptions.

## Emissaries

The agent's standing instructions include [`emissaries.md`](config/emissaries.md), a set of principles for how a personal agent acts for its user and toward the people it deals with on that user's behalf. They sit before the persona, apply to every user, and cannot be overridden per user. An operator can turn them off, and briefings skip them.

- **Private counsel, public accountability.** In private, the agent may disagree and say so. Anything it sends, posts, or commits to in public must be something its user would endorse, and stays traceable to that person. When in doubt, it asks first.
- **Obligations to third parties.** The agent does not claim to be human when sincerely asked, does not demean or manipulate the people it deals with, and does not take irreversible actions on ambiguous instructions. The user cannot instruct it out of these.
- **Access is not license.** Access to private mail, relationships, and finances is used only in the user's interest, and private things stay private.

The principles shape the agent's judgment; the sandbox, credential handling, and confirmation requirements enforce the boundaries a prompt cannot. See [persona and emissaries](docs/configuration/persona.md) and the [Emissaries framework](https://github.com/istota-project/emissaries).

## Your data and your model

Istota requires no Istota cloud account or hosted control plane. Records live in SQLite, plain files, and formats such as Markdown, JSON, CSV, and Beancount. You can inspect, back up, and move them with ordinary tools. Nextcloud can provide files, calendars, contacts, login, and Talk; a local workspace works without it.

Two main model paths are supported:

- **Claude Code:** the Claude Code CLI runs the agent loop.
- **Native brain:** Istota runs the loop against an OpenAI-compatible Chat Completions endpoint: Anthropic's API, OpenRouter, or a local model server. A local server is configured after setup. The model and endpoint need compatible tool calling; model quality and supported modalities affect which tasks work well.

Changing the backend leaves your application data and task system in place. A hosted provider receives the prompts and tool results sent to it. A local model keeps those inference requests local; connected services still receive the traffic needed to use them.

See [native brain configuration](docs/configuration/native-brain.md) and the [`[brain]` reference](docs/configuration/reference.md#brain). A model server on the Mac itself also needs the local VM's explicit [host-port allowance](docs/getting-started/local-install.md#reaching-a-service-on-your-mac).

## One install for several people

Each user has separate files, credentials, personal memory, module data, and task queues. User identity follows a request through intake, sandbox setup, skill calls, and delivery.

Shared rooms and groups make collaboration explicit. In shared chat rooms, personal memory is withheld from the prompt. A member's turn runs with that member's full reach; a guest's turn runs as the host and reaches nothing of the host's. Sharing a room is therefore a deliberate audience choice. Email thread rooms differ: admitted mail runs as the host, with memory loaded.

See [shared rooms](docs/features/shared-rooms.md) and [groups](docs/features/groups.md).

## Development

The backend and task runtime are Python; the web app is SvelteKit with a FastAPI backend. Docker Compose and host scripts define the deployment. An optional Ansible role manages installations from an existing inventory.

```bash
uv sync --extra test
npm --prefix web ci
```

Start with the [development setup](docs/development/setup.md), [architecture overview](docs/architecture/overview.md), and [testing guide](docs/development/testing.md). Linux sandbox and deployment changes have dedicated test tiers; the ordinary unit suite does not exercise those boundaries.

## License

[European Union Public Licence 1.2](LICENSE), a reciprocal open-source license.

---

© 2026 [Stefan Kubicki](https://kubicki.org) · A [CYNIUM Lamplight](https://lamplight.cynium.com) release
