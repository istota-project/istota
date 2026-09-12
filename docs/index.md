# Istota

[![GitHub stars](https://img.shields.io/github/stars/istota-project/istota?style=flat&logo=github)](https://github.com/istota-project/istota/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/istota-project/istota?style=flat&logo=github)](https://github.com/istota-project/istota/network/members)
[![Last commit](https://img.shields.io/github/last-commit/istota-project/istota?logo=github)](https://github.com/istota-project/istota/commits/main)
[![License](https://img.shields.io/github/license/istota-project/istota)](https://github.com/istota-project/istota/blob/main/LICENSE)

**Istota is a self-hosted personal AI operating system.** It runs on your own server or laptop and brings calendars, email, files, location, health, money, feeds, conversations, and memory into one command layer. You can use all of those sources or only the ones you choose.

The native modules remain useful without a model. They have their own storage, command-line interfaces, and web pages. The agent adds a shared language interface and can reason across the records a task is allowed to read.

Istota works with Claude through the [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) CLI or with any OpenAI-compatible endpoint, including OpenRouter and local model servers. You can talk to it through the built-in web app, SMS, email, Nextcloud Talk, a watched `TASKS.md` file, the terminal REPL, or the CLI.

One server can host several separate personal systems. Each user has their own memory, files, credentials, module data, rooms, and task queues. Shared rooms and files are explicit.

> **Istota is pre-1.0 software under active development.** Interfaces, configuration, and database schemas may change between releases. Pin a release and read the [changelog](https://github.com/istota-project/istota/blob/main/CHANGELOG.md) before upgrading.

## A personal agent on rails

A dependable personal agent needs more than a model loop. Istota gives it known ways to act, boundaries around those actions, durable state, and background execution.

| Part | What it does |
| --- | --- |
| **Curated skills** | A maintained set of skills exposes structured commands for calendars, email, files, memory, health, money, feeds, and connected services. |
| **Durable execution** | Every request enters a queue with a task identity, status, brain, permissions, retries, streamed events, and usage records. |
| **Structured personal data** | Health, location, money, feeds, and briefings have schemas, per-user storage, command-line interfaces, and web pages. |
| **Durable memory** | Per-user and per-room memory, hybrid search, a temporal knowledge graph, and learned playbooks carry facts and decisions across sessions. |
| **Proactive work** | Scheduled jobs, reminders, briefings, and heartbeat checks run beyond a single chat turn. Deterministic jobs can run without a model. |
| **Human control** | Confirmations, notifications, audit logs, explicit resource grants, and administrator-only operations keep consequential actions visible. |

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

Read the [architecture overview](architecture/overview.md) for the full task path and module map.

## Personal data stays under your control

Location, health, financial, calendar, email, and conversation records form an unusually complete account of a person. Istota is self-hosted so that record can remain on infrastructure you administer. Its data lives in SQLite and plain formats such as Markdown, TOML, CSV, JSON, and Beancount.

Self-hosting does not make connected services disappear. A hosted model provider receives the prompts and tool results sent to it, and every service you connect receives the traffic needed to use it. Istota also supports a local OpenAI-compatible model endpoint. Those connections are operator choices rather than requirements of an Istota account or control plane.

The supported bare-metal deployment confines tasks with bubblewrap, credential isolation, scoped files, an outbound network proxy, and optional confirmation gates. The standalone install and shipped Docker stack run tasks unsandboxed. Read the [security model](deployment/security.md) before exposing an instance or giving it sensitive resources.

## Nextcloud is optional

Nextcloud is a first-class integration, not the foundation of the system. When connected, it provides files, calendars, contacts, OAuth login, notifications, and Talk while Istota acts as an ordinary Nextcloud user over standard protocols.

Without Nextcloud, the workspace is an ordinary local directory, web chat handles conversations, and CalDAV and IMAP can point at other providers. The [standalone install](getting-started/local-install.md) uses this shape. Read the [Nextcloud guide](features/nextcloud.md) for the integration itself.

## Principles

Istota ships with an [Emissaries](https://github.com/istota-project/emissaries) layer that defines what a personal agent owes to its user and to people it encounters on that user's behalf. It separates private counsel from public action, treats access to private data as power, and keeps outward commitments answerable to a person. Structural security remains separate: sandboxing, credential isolation, and confirmation gates enforce boundaries that a prompt cannot.

This view shares the humanist premise of [Common Task](https://commontask.org/): people are ends rather than inputs to a system, and technology should be judged by how it affects their agency and well-being.

## Choose an installation

- [Bare-metal server](getting-started/quickstart-bare-metal.md): the recommended persistent, multi-user deployment on Debian or Ubuntu. It connects to an existing Nextcloud and supports bubblewrap and cgroups.
- [Docker server](getting-started/quickstart-docker.md): a self-contained stack with Nextcloud, PostgreSQL, Redis, nginx, and the Istota web app. Agent tasks are unsandboxed in the shipped Compose configuration.
- [Local standalone](getting-started/local-install.md): a trusted single-user installation with a local workspace, loopback-only web app, and no required Nextcloud or login screen. It is unsandboxed by design.

## Explore the documentation

- [Architecture overview](architecture/overview.md)
- [Web interface](features/web-interface.md)
- [SMS](features/sms.md)
- [WhatsApp](features/whatsapp.md)
- [Skills index](reference/skills-index.md)
- [Configuration reference](configuration/reference.md)
- [Command reference](reference/commands.md)
- [Development setup](development/setup.md)

## License

Istota is released under the [European Union Public Licence 1.2](https://github.com/istota-project/istota/blob/main/LICENSE) (EUPL v1.2). It permits commercial use, but modified versions distributed or provided to the public as a network service must keep their source available under the license terms.
