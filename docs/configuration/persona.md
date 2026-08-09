# Persona and emissaries

Istota's behavior is shaped by three layers: emissaries (constitutional principles), persona (character), and guidelines (channel-specific formatting).

## Emissaries

Defined in `config/emissaries.md`. These are constitutional principles injected before the persona in every prompt. They are global only -- not user-overridable and not subject to `{BOT_NAME}` substitution.

Emissaries define how the agent reasons about:

- Being and autonomy
- Public/private distinction in agent behavior
- Responsibility and accountability
- The emissary role (representing judgment, not just executing)
- What cannot be delegated (honesty, dignity, proportionality)
- Data access and privacy
- Cognitive limitations and engagement

Based on the [Emissaries](https://github.com/istota-project/emissaries) framework.

Controlled by `emissaries_enabled` (default `true`). Skipped for briefings.

## Persona

Defined in `config/persona.md` (global default). Users can override with their own `PERSONA.md` in their workspace (`/Users/{user_id}/{bot_dir}/config/PERSONA.md`). The user version is seeded from the global file on first run.

The persona defines:

- Character identity and traits
- Communication style
- Working practices
- Writing style
- Boundaries

Placeholders `{BOT_NAME}` and `{BOT_DIR}` are substituted at load time.

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
