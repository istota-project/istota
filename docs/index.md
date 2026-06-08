# Istota

A self-hosted AI agent that lives in your Nextcloud instance. Run it on the [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) CLI, or on its own agentic loop against any OpenAI-compatible model.

```
Talk message ──>┐
Web chat ──────>│
Email ─────────>├──> SQLite queue -> Scheduler -> Brain -> Response
TASKS.md ──────>│
CLI / REPL ────>┘
```

Messages arrive through Talk polling, the in-app web chat, IMAP, TASKS.md file watching, the interactive REPL, or the CLI. The scheduler claims tasks from a SQLite queue, builds a prompt with the user's resources, skills, memory, and conversation context, then hands it to a **Brain** in a sandbox. Responses go back through the same channel.

## What is it?

Istota runs as a regular Nextcloud user and handles the plumbing around a language model: input channels, task queuing, context assembly, prompt construction, skill loading, memory, scheduling, multi-user isolation, and response delivery. The reasoning comes from whichever model you point it at.

It is not tied to a single vendor. A pluggable **Brain** sits at the model seam: the default brain delegates to the Claude Code CLI, while the native brain runs Istota's own in-process agentic loop — tool dispatch, context compaction, retries — against any OpenAI-compatible endpoint (Anthropic, OpenRouter, or a local model). So Istota can run fully standalone on open models.

It runs as a regular Nextcloud user. File sharing, calendars, contacts, and Talk messaging all work through standard Nextcloud protocols. No webhooks, no OAuth apps, no server plugins.

## Features at a glance

- **Messaging** -- Nextcloud Talk (DMs and group rooms), in-app web chat (always-on rooms with live streaming), email (IMAP/SMTP with threading), TASKS.md file polling, interactive REPL, CLI
- **Skills** -- ~30 built-in skills loaded on demand: calendar, email, web browsing, git/GitLab/GitHub, Beancount accounting, GPS tracking, bookmarks, voice transcription, OCR, RSS feeds, health tracking, and more
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
- [Credentials](configuration/credentials.md) -- global and per-user credential architecture
- [Skills index](reference/skills-index.md) -- every built-in skill

## Why Nextcloud?

Most AI assistant projects treat infrastructure as someone else's problem, connecting to third-party APIs for storage, calendars, and messaging. Istota takes a different approach: it lives inside a Nextcloud instance as a regular user.

- **Zero Nextcloud configuration.** Create a user account, invite it to a chat.
- **File sharing is native.** Users share files with the bot like they share with colleagues.
- **Multi-user comes free.** Nextcloud handles user isolation, file ownership, and access control.
- **Self-hosted end to end.** Your data stays on your server. The only external dependency is a model provider — Claude, any OpenAI-compatible API, or a model you host yourself.
- **User self-service.** Config files live in the user's Nextcloud folder. Edit with any text editor.

## License

[MIT](https://forge.cynium.com/cynium/istota/src/branch/main/LICENSE).
