
<p align="center">
  <img src="web/static/logo-512.png" alt="Istota's octopus mark" width="160">
</p>

<h1 align="center">Istota</h1>

<p align="center"><strong>Your personal AI operating system, on your own server.</strong></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/istota-project/istota" alt="EUPL-1.2 license"></a>
  <a href="https://github.com/istota-project/istota/commits/main"><img src="https://img.shields.io/github/last-commit/istota-project/istota?logo=github" alt="Last commit"></a>
  <a href="https://istota.cynium.com/docs"><img src="https://img.shields.io/badge/docs-istota.cynium.com-blue" alt="Documentation"></a>
</p>

Istota (ee-stoh-tah, Polish for *being* or *entity*) is a self-hosted personal AI operating system that puts a loyal and opinionated octopus-shaped agent at the center of your digital life.

It runs on your own server (or laptop) and unifies your calendar, email, files, location, health, finances, feeds, conversations, and memory — or whatever subset of these you choose — across a single command layer.

Istota can reconstruct a day from appointments, places, and purchases; prepare you for a meeting from mail, documents, and past decisions; or turn unread feeds and newsletters into a briefing delivered every morning.

It has a curated skillset, durable tasks, schedules, persistent memory, human approvals, and secure defaults. A full web app gives chat, health, location, money, feeds, briefings, notifications, and administration proper interfaces of their own. Talk to the agent there, over email, or in Nextcloud Talk.

Istota works with Claude through the [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) CLI or with any OpenAI-compatible endpoint, including OpenRouter and local model servers. It can run as a private single-user app on your own machine or as a multi-user service backed by Nextcloud.

Multi-user is native. One Istota server can host several separate personal agents, each with its own memory, files, credentials, module data, rooms, and task queues. Shared rooms and files are explicit; using the same server does not collapse several people's lives into one agent profile.

If you've used a local agent like OpenClaw or Hermes, parts of Istota will feel familiar: chat channels, tools, memory, scheduled jobs, and a choice of model providers. Istota's center of gravity is the personal system behind the agent. Health, location, money, feeds, and briefings are native applications with their own storage, CLI, and web UI. Its skillset is deliberately curated, and every executable skill uses the same command interface and credential boundary. The modules remain useful without a model. Together they give the agent durable, structured records it can reliably write, query and combine.

Istota also ships with [`emissaries.md`](config/emissaries.md), a constitutional layer for what a personal agent owes to its user and to the people it encounters on that user's behalf. Think of it as a code of ethics for agents. It draws a sharp line between private counsel and public action: the agent may disagree honestly in private, but outward commitments must reflect its principal's judgment and remain traceable to a responsible person. It treats access to private data as power rather than license, and places honesty, dignity, and proportionality toward third parties above persona or user preference. Istota treats those principles and structural security as separate requirements; the sandbox, credential isolation, and confirmation gates enforce boundaries that a prompt cannot. In that respect, Istota shares the humanist premise of [Common Task](https://commontask.org/): people are ends rather than inputs to a system, and technology should be judged by how it affects their agency and well-being.

> **Istota is pre-1.0 software under active development.** Interfaces, configuration, and database schemas may change between releases. Pin a release and read the [changelog](CHANGELOG.md) before upgrading.

## A personal agent on rails

A model can reason, but a dependable personal agent needs much more than a model loop. It needs known ways to act, boundaries around those actions, durable state, background execution, and deterministic interfaces for the work.

The design is intentionally *cybernetic*. The agent observes, compares what happened with what was intended, acts, and records the result. Modules, schedules, heartbeats, notifications, and memory keep that loop running beyond a single chat turn.

It's also opinionated. Common tasks have supported paths. Skills expose structured commands instead of teaching the model to improvise against internal files and databases. Sensitive operations pass through permission checks and credential boundaries. Repeatable work can become a scheduled command that does not call a model at all.

| Rail | What it means in Istota |
| --- | --- |
| **Secure defaults** | Tasks start with scoped files, no direct database access, no ambient credentials, and allowlisted network access on the supported sandboxed deployment. Wider access is an operator decision. |
| **Curated skillset** | Istota ships a maintained standard library of skills with documented behavior and live CLIs. Skills are selected on demand, and users can add personal instructions without changing the shared skill. |
| **One command surface** | `istota-skill <skill> <command>` is the common interface to calendars, email, memory, health, money, feeds, files, Nextcloud, and other services. The agent, scheduler, and operator use the same command families. |
| **Durable execution** | Every request enters a queue with a task identity, status, brain, permissions, retries, streamed events, and usage records. Foreground chat and background jobs have separate workers. |
| **Structured personal data** | Health, location, money, feeds, and briefings have schemas, storage, CLIs, and web pages. Clear module interfaces let the agent cross-reference them in one task without inventing a new representation in each conversation. |
| **Durable memory** | Per-user and per-room memory, hybrid search, a temporal knowledge graph, and learned playbooks carry facts and decisions across sessions. |
| **Native multi-user** | One service hosts separate personal systems. User identity scopes every room, task, file, memory, credential, resource, and module query from intake through delivery. |
| **Human control** | Confirmations, notifications, audit logs, explicit resource grants, and administrator-only operations keep consequential actions visible. |

Those rails are the operating system around the agent. Native modules are its applications, skills are its system calls, the task queue is its scheduler, and the web app, chat surfaces, and CLI are ways to work with it. Per-user isolation gives the system real users and permissions. SQLite, plain files, Beancount, and optional Nextcloud storage provide the filesystem layer.

The rails do not make the model mandatory. You can still read feeds, review transactions, inspect health records, browse location history, and use the module CLIs when no model is available. The agent adds a shared language interface and the ability to reason and act across the system.

## Your life is not a data product

This is not hyperbole. Location history shows where you sleep, work, travel, and seek care. Health records describe your body. Transactions reveal habits and relationships. Calendar, email, files, conversations, and reading history fill in the rest. Put together, this is an unusually complete record of a person.

Istota is self-hosted because that record should remain under your control. The web app, scheduler, memory, task history, and module databases run on infrastructure you administer. There is no required Istota cloud account or hosted control plane. Data lives in SQLite and plain formats such as Markdown, TOML, CSV, JSON, and Beancount, so access does not depend on one company continuing to operate or choosing to treat you well.

Self-hosting does not make outside services disappear. If you connect Nextcloud, email, Garmin, a market-data source, or another service, those systems receive the traffic needed to use them. If you choose a hosted model provider, task prompts and tool results sent to that model leave your server under that provider's terms. Istota lets you choose the model backend, including a local OpenAI-compatible endpoint, and makes those connections operator-controlled rather than mandatory parts of an Istota service.

The agent is also constrained inside your own server. Sensitive credentials stay outside the model process, task files and network access are scoped, databases are hidden from sandboxed tasks, and consequential operations can require confirmation. The [security model](#secure-defaults-explicit-boundaries) describes what each deployment shape enforces and where its limits are.

## The records connect

Personal data becomes more informative when its sources can be read together. Each source records a different part of reality:

- Your calendar records intent: where you meant to be, who you expected to meet, and what you set aside time to do.

- Location history records presence: where your devices observed you and how long you stayed.

- Transactions record economic activity: when and where money moved, what it was for, and which account it touched.

- Email, conversations, files, and durable memory preserve the surrounding decisions and explanations.

- Health records and Garmin data show measurements, activity, and changes over time.

- Feeds, newsletters, bookmarks, and briefings record what you follow and what has already reached your attention.

The pattern is feedback: compare intention with observation, notice the difference, act, and carry the result forward in memory.

No source is a complete account. Their overlap can answer questions that one app cannot:

| Task | Records Istota can combine |
| --- | --- |
| **Reconstruct a day or trip** | Compare the calendar's plan with observed visits, card transactions, messages, and saved documents to build a sourced timeline of what happened. |
| **Prepare for a meeting** | Gather the calendar event, recent correspondence, shared files, open todos, room history, and remembered decisions into one briefing. |
| **Understand an expense** | Use its date, merchant, location, nearby calendar events, travel history, and work records to identify the likely purpose before changing the ledger. |
| **Reconcile work and invoices** | Compare scheduled meetings, the work log, client correspondence, invoices, and incoming payments to find work that may not have been billed or paid. |
| **Review a health change** | Put measurements and Garmin trends beside travel, schedule changes, encounters, and notes to surface correlations for review without treating timing as proof of cause. |
| **Follow an interest** | Collect unread feeds and newsletters, remove repeated stories, add relevant bookmarks or web sources, summarize the result into a personal briefing, and deliver it on a schedule. |
| **Resume an old thread** | Search conversations, email, files, memory, and the knowledge graph to recover what was decided, why it was decided, and what remained open. |

These compositions can happen in response to a question or as a scheduled workflow. Istota does not silently merge every source into one store. Each module owns its schema and permissions. The agent performs the join at task time through the same scoped CLI that exposes each module, reading only the records the task is allowed to use.

## The web app

Istota has a responsive, installable web app built with SvelteKit. It is the main place to see and organize information that does not fit well in a chat reply.

| Area | What you can do |
| --- | --- |
| **Chat** | Keep separate rooms for different parts of your life, stream replies, attach files, record voice notes, reply to messages, and continue a conversation between the web and Nextcloud Talk. Recent rooms and messages remain available offline, and messages written without a connection are sent when it returns. |
| **Health** | Track body measurements, import bloodwork, inspect biomarker trends, keep encounters and diagnoses, manage immunizations, attach source documents, and sync Garmin data. |
| **Location** | See recent positions on a map, review visits and travel history, save places, and inspect automatically detected places and stays. |
| **Money** | Review accounts and transactions, inspect balance-sheet, cash-flow, and income reports, track investments, estimate quarterly taxes, manage clients and invoices, and keep a work log. |
| **Feeds** | Subscribe to RSS, Atom, Tumblr, and Are.na sources; read entries in a native feed reader; keep read state; and manage subscriptions without a third-party reading service. |
| **Briefings** | Read and archive personal briefings assembled from newsletters, feeds, web pages, markets, calendars, todos, and shared content blocks. |
| **Notifications** | Use one inbox for items that need attention, including failed scheduled jobs, expired Garmin authentication, and health imports awaiting confirmation. |
| **Settings and administration** | Manage personal preferences and module settings. Administrators can inspect service health, usage, logs, and a credential-redacted view of the loaded configuration. |

## Native modules

### Health

The health module keeps structured records and the documents behind them in one place. It handles body-stat time series, bloodwork panels, biomarker trends, encounters, diagnoses, immunizations, and Garmin daily data. Bloodwork can be entered manually, imported from CSV, or extracted from a document with OCR. Explanations generated by a model are stored separately from the source values, and measurements retain their units.

### Location

The location module accepts GPS pings from the Istota iOS app, Overland, and Garmin, identifies recurring places, and turns raw points into visits and travel history. The web map shows where you have been and lets you manage saved places. The agent can use the same data through a scoped location skill.

### Money

The money module combines a plain-text Beancount ledger with web views for accounts, transactions, financial statements, portfolio history, tax estimates, clients, invoices, and work records. Your ledger remains usable with ordinary Beancount tools outside Istota. The agent can help classify transactions, prepare reports, and work with the records through the same module interface.

### Feeds

The feed reader polls RSS and Atom feeds as well as Tumblr and Are.na sources. It stores subscriptions, entries, images, deduplication state, and read state in a per-user SQLite database. You can read in the web app, manage feeds from the CLI, or ask the agent to find and summarize material from your subscriptions.

### Briefings

A briefing is a saved, readable report built from ordered blocks. A block can gather newsletters, feeds, a browsed front page, market data, calendar events, todos, or content shared by more than one briefing. Briefings have their own reader and archive and can also be delivered through Talk, email, or ntfy.

The modules are separate by design. Health does not need access to your ledger, and the feed reader does not need access to your calendar. Their common CLI gives the agent a controlled composition layer: it can query more than one module for a task without hard-wiring every possible relationship into the modules themselves.

## An agent that can act within the system

Istota does not hand the model a pile of unrelated integrations. A task receives the relevant conversation, memories, resources, and skill instructions, then works through a defined runtime with one of several interchangeable model backends.

- **Pluggable brains.** Use the Claude Code CLI, Istota's in-process agent loop with any OpenAI-compatible API, or the Claude terminal UI. Choose a default, route certain source types to another brain, or pin an allowed brain to a room or scheduled job.

- **Curated standard library.** Istota ships with 36 skills for calendar, email, files, web browsing, Google Workspace, GitHub and GitLab work, bookmarks, transcription, OCR, reminders, schedules, health, money, location, feeds, briefings, and more. This is a maintained skillset with common conventions and security rules, not an open-ended marketplace installed into every task.

- **Unified CLI.** Skills with executable operations share the `istota-skill <skill> <command>` interface and return structured output. The credential proxy, user scope, path checks, and audit behavior sit behind that interface, so callers do not need to reproduce them. The same commands can power an agent turn, a scheduled job, or an operator script:

  ```bash
  istota-skill calendar list --date today
  istota-skill feeds entries --status unread
  istota-skill money balances
  istota-skill tasks recent
  ```

- **Persistent memory.** Each person and room can have durable Markdown memory. Nightly curation extracts dated memories, updates a temporal knowledge graph, and can distill successful procedures into reusable playbooks. Hybrid keyword and vector search retrieves relevant material later, so a thread can resume with its past decisions and unfinished work.

- **Proactive work.** A conversation can become a reminder, a recurring prompt, or a deterministic scheduled command. Heartbeat checks watch files, URLs, deadlines, calendar conflicts, commands, and Istota itself. Cooldowns, quiet hours, failure tracking, notifications, and delivery rules let Istota act again when something changes or comes due.

- **Several ways to talk.** The same task system accepts web chat, Nextcloud Talk, SMS, threaded email, watched `TASKS.md` files, a terminal REPL, and direct CLI requests. Web chat and Talk share a room model, while SMS remains a separate external conversation.

- **Durable execution.** Every request enters a SQLite queue before it runs. Foreground and background worker pools are separate for each user, so a long report or overnight job does not block an active conversation. Retries, confirmations, streamed events, cancellations, and usage records follow the task through one lifecycle.

## Files and personal cloud

Every user has a workspace for notes, configuration, inbox files, memories, scripts, and generated exports. That workspace can be an ordinary local directory or a Nextcloud-backed file tree.

Nextcloud is a first-class integration, not a requirement. On a server deployment, Istota can use Nextcloud for files, calendars, contacts, OAuth login, notifications, and Talk while acting as an ordinary user over standard protocols. The standalone install uses a local directory, binds the web app to loopback, and needs neither Nextcloud nor a login screen.

Data stays in formats that can be inspected and moved without Istota: SQLite, Markdown, TOML, JSON, CSV, and Beancount. Module data is stored per user. Online database backups, restore tooling, migration checks, and `istota doctor` cover the operational side of keeping that data on your own server.

## One server, many personal systems

Istota's multi-user model runs through the whole system. Every task carries its user's identity from message intake through the queue, prompt assembly, sandbox, skill calls, module queries, and final delivery. That identity determines which files, memories, credentials, resources, rooms, and databases the task can reach.

Each person has their own profile and persona, module data, workspace, secrets, worker pools, and conversation history. Administrators can grant resources and administrator-only skills without making them ambient for everyone else. One person's long-running background job does not occupy another person's queue.

Sharing is a separate, explicit act. Talk rooms and Nextcloud files create common spaces when people want them; private memory and module data remain personal. This makes one Istota instance suitable for a household, a small organization, or several independent users while preserving a separate personal system for each person.

## Secure defaults, explicit boundaries

The rails are enforced in code rather than left as instructions in the prompt. Istota treats model output as untrusted input. The supported bare-metal server deployment uses several independent controls to limit what a task can reach:

- **Filesystem isolation.** Each task runs in a bubblewrap sandbox with a scoped view of the current user's files. Framework and module databases are masked from the sandbox, and one administrator's repositories are never mounted into another's task.

- **Credential isolation.** Sensitive credentials are removed from the model process. Skill commands run through a Unix-socket proxy that injects only the credentials declared for that skill and available to that user. The encrypted secret store's master key is never returned through the lookup channel.

- **Network isolation.** Sandboxed tasks run in their own network namespace. Outbound connections pass through an allowlist-based CONNECT proxy, with TLS kept end to end. The native web-fetch path runs outside the task sandbox but applies its own SSRF and egress policy.

- **Constrained writes.** The agent cannot open Istota's databases directly. Operations that originate in a sandbox are written as requests, validated against the task's own identity, and applied by the scheduler after the task succeeds.

- **Resource limits.** Linux cgroups can cap memory, processes, and CPU for each task. Development builds run in a separate per-user dev container rather than through a Docker socket exposed to the task.

These controls depend on the deployment shape. Bubblewrap on Linux is the supported sandbox. The local standalone install is intentionally unsandboxed and should be treated as a trusted single-user process. The shipped Docker stack also runs agent tasks without bubblewrap because it does not grant the container privileges that nested sandboxing needs. Read the [security documentation](https://istota.cynium.com/docs/deployment/security/) before exposing an instance or giving it sensitive resources.

## How a request runs

```text
Web chat ──────────┐
Nextcloud Talk ────┤
SMS ───────────────┤
Email ─────────────┤
TASKS.md ──────────┼──> durable task queue ──> prompt + relevant data ──> Brain
CLI / REPL ────────┤                                  │              │
Scheduled jobs ────┤                                  │              ├──> skills and connected services
Heartbeat checks ──┘                                  │              ├──> native modules
                                                      │              └──> streamed response
                                                      └──> memory, resources, room history
```

The scheduler claims a task, selects its brain, assembles two prompt layers, starts the task runtime, and streams structured events to the relevant surfaces. The system layer contains standing instructions and skill documentation. User-controlled material such as memories, retrieved facts, conversation history, and attachments stays in the user layer. A task keeps the brain and permissions it had when it was queued, even if room settings change while it is running.

## Install

Istota has three deployment shapes.

| Shape | Intended use | Nextcloud | Task sandbox |
| --- | --- | --- | --- |
| **Bare-metal server** | A persistent, multi-user installation on a Debian or Ubuntu VM | Connects to an existing Nextcloud | Bubblewrap and cgroups |
| **Docker server** | Evaluation or a self-contained server stack with bundled Nextcloud, PostgreSQL, Redis, nginx, and the Istota web app | Bundled | Unsandboxed in the shipped Compose configuration |
| **Standalone** | A trusted single-user installation on your own machine | Not required | Unsandboxed by design |

### Bare-metal server

This is the recommended production shape. It installs Istota as system services and connects to an existing Nextcloud instance.

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash
```

### Docker server

This starts the self-contained Compose stack and its own Nextcloud.

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
```

### Local standalone

This installs a single-user instance with a local workspace and no authentication screen. `istota serve` starts the scheduler and web app together at `http://localhost:8766/istota`.

```bash
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --standalone
```

You can also install the Python application directly and run the setup wizard:

```bash
uv tool install 'istota[local]'
istota setup
istota serve
```

Read an installer before piping it into a shell. For prerequisites, setup choices, and updates, use the [Docker quickstart](https://istota.cynium.com/docs/getting-started/quickstart-docker/), [bare-metal quickstart](https://istota.cynium.com/docs/getting-started/quickstart-bare-metal/), or [standalone guide](https://istota.cynium.com/docs/getting-started/local-install/).

## Configuration

Opinionated defaults do not make the system fixed. Configuration starts in TOML and can be overlaid with per-user records from the database. Users can manage their own resources, briefing settings, module settings, secrets, and preferences through the CLI or web app, subject to administrator policy.

Common entry points:

```bash
istota setup                         # interactive standalone setup
istota serve                         # scheduler + web UI in one process
istota doctor                        # check runtime and service dependencies
istota task "Summarize my unread feeds" -u alice -x
istota secret --help                 # provision and inspect credentials
istota user --help                   # manage users
istota resource --help               # manage connected resources
```

See the [configuration overview](https://istota.cynium.com/docs/configuration/overview/) and [complete reference](https://istota.cynium.com/docs/configuration/reference/).

## Development

Istota's backend and task runtime are Python. The web app is SvelteKit. The repository also contains the Ansible role, Docker stack, browser service, deployment tests, and a full local testbed.

```bash
uv sync --extra test

ruff check --output-format concise src tests testbed docker/browser docker/devbox docker/istota scripts
scripts/qtest uv run pytest
```

For web work:

```bash
npm --prefix web ci
npm --prefix web run lint:design
npm --prefix web run check
scripts/qtest npm --prefix web run test
npm --prefix web run format:check
```

The default Python suite omits tests that need live services, Linux sandboxing, built images, Docker Compose, the full testbed, or a deployment host. Read the [testing guide](https://istota.cynium.com/docs/development/testing/) before working on those paths.

## Documentation

- [Documentation](https://istota.cynium.com/docs)
- [Web interface](https://istota.cynium.com/docs/features/web-interface/)
- [SMS](https://istota.cynium.com/docs/features/sms/)
- [WhatsApp](https://istota.cynium.com/docs/features/whatsapp/)
- [Architecture overview](https://istota.cynium.com/docs/architecture/overview/)
- [Skills index](https://istota.cynium.com/docs/reference/skills-index/)
- [Command reference](https://istota.cynium.com/docs/reference/commands/)
- [Changelog](CHANGELOG.md)

## License

Istota is released under the [European Union Public Licence 1.2](LICENSE) (EUPL v1.2), the European Commission's reciprocal open-source license.

---

© 2026 [Stefan Kubicki](https://kubicki.org) · A [CYNIUM Lamplight](https://lamplight.cynium.com) release
