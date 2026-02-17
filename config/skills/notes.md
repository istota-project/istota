Users can share notes files (markdown format) for agenda items and reminders. These files are:
- Stored as user resources with type `notes_file`
- Freeform markdown - interpret what's relevant based on context
- May contain dates, priorities, agenda items, or general notes

When processing notes files for briefings:
1. Read the file: `cat /srv/mount/nextcloud/content/path/to/notes.md`
2. Look for relevant content based on:
   - Today's or tomorrow's date
   - Priority markers (high, urgent, etc.)
   - Upcoming deadlines
   - Action items or reminders
3. Include relevant items in the briefing
