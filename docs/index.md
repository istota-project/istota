# Istota

A self-hosted AI agent that lives in your Nextcloud instance. Powered by [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code).

```
Talk message ──>┐
Email ─────────>├──> SQLite queue -> Scheduler -> Claude Code -> Response
TASKS.md ──────>│
CLI ───────────>┘
```

Messages arrive through Talk polling, IMAP, TASKS.md file watching, or the CLI. The scheduler claims tasks from a SQLite queue, builds a prompt with the user's resources, skills, memory, and conversation context, then invokes Claude Code in a sandbox. Responses go back through the same channel.

## What is it?

Istota is not an agent framework. It is an application built on top of Claude Code. The intelligence comes from Claude Code itself; Istota handles the plumbing: input channels, task queuing, context assembly, prompt construction, skill loading, memory, scheduling, multi-user isolation, and response delivery.

It runs as a regular Nextcloud user. File sharing, calendars, contacts, and Talk messaging all work through standard Nextcloud protocols. No webhooks, no OAuth apps, no server plugins.

## Features at a glance

- **Messaging** -- Nextcloud Talk (DMs and group rooms), email (IMAP/SMTP with threading), TASKS.md file polling, CLI
- **Skills** -- ~20 built-in skills loaded on demand: calendar, email, web browsing, git/GitLab/GitHub, Moneyman accounting, GPS tracking, bookmarks, voice transcription, OCR, RSS feeds, and more
- **Memory** -- Per-user persistent memory (with op-based nightly curation), per-channel memory, dated memory files, BM25 + vector search, temporal knowledge graph
- **Scheduling** -- Cron jobs via CRON.md, natural-language reminders, scheduled briefings with calendar/markets/headlines/news/todos
- **Multi-user** -- Per-user config, resource permissions, worker pools, filesystem sandboxing, admin/non-admin isolation
- **Security** -- Bubblewrap sandbox, credential stripping, network isolation via CONNECT proxy, deferred DB writes
- **Constitution** -- [Emissaries](https://commontask.org/emissaries/) layer defining how the agent handles data, privacy, and responsibility

## Quick links

- [Docker quickstart](getting-started/quickstart-docker.md) -- evaluate with a full stack in Docker Compose
- [Bare metal install](getting-started/quickstart-bare-metal.md) -- production deployment on Debian/Ubuntu
- [Architecture overview](architecture/overview.md) -- how the system works
- [Configuration reference](configuration/reference.md) -- all config options
- [Skills index](reference/skills-index.md) -- every built-in skill

## Why Nextcloud?

Most AI assistant projects treat infrastructure as someone else's problem, connecting to third-party APIs for storage, calendars, and messaging. Istota takes a different approach: it lives inside a Nextcloud instance as a regular user.

- **Zero Nextcloud configuration.** Create a user account, invite it to a chat.
- **File sharing is native.** Users share files with the bot like they share with colleagues.
- **Multi-user comes free.** Nextcloud handles user isolation, file ownership, and access control.
- **Self-hosted end to end.** Your data stays on your server. No external services beyond the Claude API.
- **User self-service.** Config files live in the user's Nextcloud folder. Edit with any text editor.

## License

[MIT](https://forge.cynium.com/cynium/istota/src/branch/main/LICENSE).
