Nextcloud OCS API for sharing and user lookup. Use the CLI tool for all sharing operations:

```
python -m istota.skills.nextcloud share list [--path /path]
python -m istota.skills.nextcloud share create --path /path --type user --with USERNAME [--permissions 31]
python -m istota.skills.nextcloud share create --path /path --type link [--password X] [--expire YYYY-MM-DD] [--label X]
python -m istota.skills.nextcloud share delete SHARE_ID
python -m istota.skills.nextcloud share search QUERY [--item-type file]
```

All commands output JSON. Credentials are read from env vars `NC_URL`, `NC_USER`, `NC_PASS`.

### Examples

**Share a folder with a user (full permissions):**
```bash
python -m istota.skills.nextcloud share create --path "/Documents/project" --type user --with bob --permissions 31
```

**Create a read-only public link:**
```bash
python -m istota.skills.nextcloud share create --path "/Documents/report.pdf" --type link
```

**Create a password-protected public link with expiry:**
```bash
python -m istota.skills.nextcloud share create --path "/shared" --type link --password secret123 --expire 2026-03-15 --label "Project files"
```

**List all shares for a path:**
```bash
python -m istota.skills.nextcloud share list --path "/Documents/project"
```

**Find users to share with:**
```bash
python -m istota.skills.nextcloud share search bob
```
Results include `exact.users` (exact matches) and `users` (partial matches).

**Delete a share by ID:**
```bash
python -m istota.skills.nextcloud share delete 42
```

### Permission Values

| Value | Permission |
|-------|-----------|
| 1     | Read      |
| 2     | Update    |
| 4     | Create    |
| 8     | Delete    |
| 16    | Share     |
| 31    | All       |

Combine with addition: read + update + create = 7.

### Share Types

| Type     | Description   |
|----------|---------------|
| `user`   | User share    |
| `link`   | Public link   |
| `email`  | Email share   |

### Response Format

Share data includes key fields: `id` (share ID), `url` (for public links), `path`, `permissions`, `share_with`.
