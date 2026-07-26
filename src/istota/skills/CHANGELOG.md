# Skills Changelog

## 2026-07-26
- `nextcloud` grew from four sharing verbs into the full control plane: `capabilities` (a deployment fit-check), `user`/`group` lookup, extended `share` including `share link` for download links, a `files` group for the WebDAV operations the mount can't express, a `talk` control surface, and `notify`/`activity` reads
- Failures now carry the HTTP status, the OCS status code and the server's own message instead of "Failed to …"
- The skill hides itself on deployments with no Nextcloud (`requires_capability`)

## 2026-02-08
- `memory-search` is now always-included — semantic search available without keywords
- Memory search enabled by default with proactive usage guidance
