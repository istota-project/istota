---
name: notes
triggers: [note, save, write, markdown]
description: Markdown file conventions and note-saving workflows
resource_types: [notes_folder]
---
## File format

- Always save notes as `.md` (markdown)
- Filename: descriptive sentence with spaces (e.g. `Meeting notes from Thursday.md`), not slugified or kebab-case
- Use YAML frontmatter with at least a `created` date field

## Markdown conventions

- No escaping of special characters in markdown body text
- Use standard markdown links: `[label](<path with spaces>)`
- Never use `%20` encoding in link paths
- Let markdown render handle special characters naturally

## Save location

The `notes_folder` resource is specifically where {BOT_NAME} writes new notes and transcriptions. This is distinct from broader shared folders the user may have — those are for reading, this is the write destination.

Save to whichever of these applies (first match wins):
1. A path the user specifies in their message
2. A specific path mentioned in user memory
3. A `notes_folder` resource configured in the user's resources
4. The default `{BOT_DIR}/notes/` folder

Don't ask where to save unless the destination is ambiguous.
