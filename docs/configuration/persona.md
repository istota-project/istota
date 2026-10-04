# Persona and emissaries

Istota's behavior is shaped by three layers: emissaries (constitutional principles), persona (character), and guidelines (channel-specific formatting).

## Emissaries

Defined in `config/emissaries.md`. These are constitutional principles injected before the persona in every prompt. They are global only -- not user-overridable, not copied to the file root like the persona, and not subject to `{BOT_NAME}` substitution.

Emissaries define how the agent reasons about:

- Being and autonomy
- Public/private distinction in agent behavior
- Responsibility and accountability
- The emissary role (representing judgment, not just executing)
- What cannot be delegated (honesty, dignity, proportionality)
- Data access and privacy
- Cognitive limitations and engagement

Based on the [Emissaries](https://github.com/istota-project/emissaries) framework.

The framework also shares the humanist premise of [Common Task](https://commontask.org/): people are ends rather than inputs to a system, and technology should be judged by how it affects their agency and well-being.

Controlled by `emissaries_enabled` (default `true`). Skipped for briefings.

## Persona

There is one persona per installation, owned by the operator and used for every task, in every room and for every user. Users cannot replace it.

The operator's copy is `PERSONA.md` at the bot's file root, beside `Users/` and `Channels/` (the Nextcloud mount, `/mnt/shared` on Docker, the workspace folder on a standalone install). Edit it there. Its upstream is the shipped `config/persona.md` in the repository, and `istota init` and a scheduler pass every five minutes keep the two in step the way Debian treats a config file:

- **No file at the root**: the shipped persona is written there.
- **Unedited copy**: a copy that matches any version ever shipped is replaced by each new shipped version, so you get updates without doing anything. Line endings and surrounding whitespace do not count as edits.
- **Edited copy**: kept as you wrote it. When a release changes the shipped persona, the new text is written beside yours as `PERSONA.md.shipped`, and `istota doctor` warns (`config.operator_persona`). Compare the two, merge what you want, and delete `PERSONA.md.shipped`.
- **Empty file**: means "use the shipped persona". The file is left alone and the shipped text is used, without updating the file. Delete the file instead if you want the shipped text written back and kept current.

An edit takes effect on the next task. The copy at the root survives every update, because updates reset only the repository checkout.

If the file root cannot be read (a mount outage), tasks use the last copy the sync read successfully, which is stored in the database, and then the shipped persona. A symlink, a pipe, a file that is not UTF-8 or a file over 64 KiB is refused the same way, and `doctor` says which. A deployment with no file root uses the shipped persona.

Per-user preferences (language, length, tone toward one person) and standing instructions for one user (a role, how to handle their mail, whom to escalate to) go in that user's `USER.md`, or the user can just tell the bot. `USER.md` is read below the persona, so it adjusts the persona and cannot replace it. To keep the nightly memory curator from pruning a block of standing instructions, put it under a `## ` heading marked `<!-- pinned -->` (see [memory](../features/memory.md#pinned-sections)).

### Upgrading from per-user personas

Earlier releases seeded a copy of the persona into every user's `{bot_dir}/config/PERSONA.md` and preferred it. `istota init` now retires those copies once: a copy that matches any shipped version, or the operator's copy, is deleted, and an edited copy is renamed `PERSONA.md.retired` in the same folder and stops being read. Its owner gets one notification saying so. Nothing is overwritten; if `PERSONA.md.retired` already exists the new name carries a timestamp.

If a user's copy held a role or standing instructions rather than character, move that text into their `USER.md` before you upgrade, under one pinned `## ` heading with `### ` for its subsections. To see what the retirement will do without changing anything, run `python -m istota.maintenance.persona_retire --list` (or `--dry-run`, which also prints the sync's action). Run without either flag, it refuses while any task is in flight; stop the services first, or let `istota init` do it on the next deploy.

The persona defines:

- Character identity and traits
- Communication style
- Working practices
- Writing style
- Boundaries

Placeholders `{BOT_NAME}` and `{BOT_DIR}` are substituted at load time. Keep them in the file rather than writing the name out, so a `bot_name` change needs no edit.

Skipped for briefings and when `skip_persona` is set.

## Guidelines

Channel-specific formatting rules in `config/guidelines/`:

- **`talk.md`**: Brief, conversational, minimal formatting, ~500 word limit
- **`email.md`**: Plain text or HTML, email etiquette, ALL CAPS section headers
- **`briefing.md`**: Concise, scannable, time-sensitive info prioritized
- **`web.md`**: Web chat formatting, including how a file is handed to the user

Loaded by `source_type` — the loader reads `{source_type}.md` generically, so adding a file is all it takes to cover a new surface. Applied after the request section in the prompt. Guidelines substitute a third placeholder beyond the two above, `{user_id}`, which `web.md` needs for its file-handover link.

## Custom system prompt

When `custom_system_prompt = true`, `config/system-prompt.md` replaces Claude Code's default system prompt with a minimal one (~1,200 words) focused on tool usage and working practices. This eliminates identity conflicts with persona/emissaries and removes irrelevant interactive/git/IDE instructions.

Disabled by default. Toggle via config.

Unlike the files above, this one is passed to Claude Code as a *path* and read by the CLI itself, from inside the sandbox — so it is bind-mounted read-only where it sits. Nothing else in the config directory is: emissaries, persona and the guidelines are read by the daemon and become prompt text, and `config.toml` is deliberately left outside. Keep `system-prompt.md` where `skills_dir`'s parent puts it, and out of the database directory (which the sandbox blanks out last of all).

## Technical vs user-facing identity

- **Technical identifiers** (package, env vars, DB tables, CLI): always `istota`
- **User-facing identity** (Nextcloud folders, chat persona, email signatures): configurable via `bot_name` config field (default: "Istota")
- `bot_dir_name` sanitizes `bot_name` for filesystem use (ASCII lowercase, spaces to underscores, non-alphanumeric stripped)
