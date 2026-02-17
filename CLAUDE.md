See @AGENTS.md for project overview, commands, and conventions.

## Key Architecture Notes

- **Technical identifiers** (package, env vars, DB tables, CLI): always `istota`
- **User-facing identity** (Nextcloud folders, chat persona, email signatures): configurable via `bot_name` config field (default: "Istota")
- `config.bot_dir_name` sanitizes `bot_name` for filesystem use (ASCII lowercase, spaces→underscores, non-alphanumeric stripped)
- All storage path functions require explicit `bot_dir` parameter — no hidden defaults
- Skill docs, persona, and guidelines use `{BOT_NAME}` and `{BOT_DIR}` placeholders, substituted at load time
- Per-user persona: user workspace `PERSONA.md` overrides global `config/persona.md` (seeded from global on first run)
