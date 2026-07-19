---
name: todos
triggers: [todo, task, checklist, reminder, done, complete]
description: TODO file format and operations
---
TODO files are plain text with a simple format. Each line is a task:

```
- [ ] Uncompleted task
- [x] Completed task
- [ ] Task with @due(2025-01-30)
- [ ] Task with @priority(high)
```

When reading/updating TODO files, use standard file operations:

```bash
# Read the TODO file
cat {workspace}/path/to/TODO.txt

# Add a task
echo "- [ ] New task" >> {workspace}/path/to/TODO.txt

# Edit in place (changes save directly to your workspace)
```
