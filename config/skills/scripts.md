You can create and maintain reusable Python scripts in the user's persistent scripts directory on Nextcloud.

### Scripts Directory

```
{scripts_dir}
```

Create the directory if it doesn't exist: `mkdir -p {scripts_dir}`

### When to Script

Consider creating a script when a task is:
- **Recurring** — "every Monday", "at the end of each month", "whenever I get a new invoice"
- **Multi-step and deterministic** — a fixed sequence of actions that doesn't need judgment each time
- **Cron-worthy** — could run unattended on a schedule

If you're unsure whether something should be a script or a one-off action, ask the user.

### Before Creating

Always check what already exists first:
```bash
ls {scripts_dir}
```

An existing script might already do what's needed, or could be extended.

### Script Style

- Python 3, functional style
- `#!/usr/bin/env python3` shebang, `chmod +x` after creating
- `snake_case.py` naming — descriptive (e.g., `backup_project_notes.py`, `weekly_report_email.py`)
- Type hints, docstrings, `argparse` for CLI arguments
- Standalone — import only stdlib and packages available in istota's uv environment
- Keep it simple. No unnecessary abstractions

### After Creating or Updating

Tell the user the path and how to run it:
```
Script saved: {scripts_dir}/weekly_report_email.py
Run with: python {scripts_dir}/weekly_report_email.py
```
