Nextcloud OCS API for sharing and user lookup. Credentials are available via environment variables:
- `NC_URL`: Nextcloud base URL (e.g., `https://nextcloud.example.com`)
- `NC_USER`: Username for authentication
- `NC_PASS`: App password for authentication

All endpoints return JSON when `format=json` is appended. Auth uses HTTP Basic.

### Share API

Base path: `$NC_URL/ocs/v2.php/apps/files_sharing/api/v1/shares`

**Create user share** (shareType=0):
```bash
curl -s -u "$NC_USER:$NC_PASS" -X POST \
  "$NC_URL/ocs/v2.php/apps/files_sharing/api/v1/shares" \
  -d path="/path/to/file" -d shareType=0 -d shareWith="bob" -d permissions=31 \
  -H "OCS-APIRequest: true" -H "Accept: application/json"
```

**Create public link** (shareType=3):
```bash
curl -s -u "$NC_USER:$NC_PASS" -X POST \
  "$NC_URL/ocs/v2.php/apps/files_sharing/api/v1/shares" \
  -d path="/path/to/file" -d shareType=3 -d permissions=1 \
  -H "OCS-APIRequest: true" -H "Accept: application/json"
```

**List shares for a path:**
```bash
curl -s -u "$NC_USER:$NC_PASS" \
  "$NC_URL/ocs/v2.php/apps/files_sharing/api/v1/shares?path=/path/to/file&format=json" \
  -H "OCS-APIRequest: true"
```

**Delete a share:**
```bash
curl -s -u "$NC_USER:$NC_PASS" -X DELETE \
  "$NC_URL/ocs/v2.php/apps/files_sharing/api/v1/shares/SHARE_ID" \
  -H "OCS-APIRequest: true"
```

### Sharee Lookup

Search for users to share with:
```bash
curl -s -u "$NC_USER:$NC_PASS" \
  "$NC_URL/ocs/v2.php/apps/files_sharing/api/v1/sharees?search=bob&itemType=file&format=json" \
  -H "OCS-APIRequest: true"
```

Results are in `ocs.data.exact.users` (exact matches) and `ocs.data.users` (partial matches).

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

| Value | Type          |
|-------|---------------|
| 0     | User share    |
| 3     | Public link   |
| 4     | Email share   |

### Response Format

Successful responses have `ocs.meta.statuscode` of 200. Share data is in `ocs.data`. Key fields: `id` (share ID), `url` (for public links), `path`, `permissions`, `share_with`.
