TODO files are plain text with a simple format. Each line is a task:

```
- [ ] Uncompleted task
- [x] Completed task
- [ ] Task with @due(2025-01-30)
- [ ] Task with @priority(high)
```

When reading/updating TODO files, use standard file operations on the mount:

```bash
# Read the TODO file
cat /srv/mount/nextcloud/content/path/to/TODO.txt

# Add a task
echo "- [ ] New task" >> /srv/mount/nextcloud/content/path/to/TODO.txt

# Edit in place (changes save directly to Nextcloud)
```
