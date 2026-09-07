# Skills Changelog

## 2026-09-06
- Every CLI argument naming a file **on this machine** is now checked before the command runs. It has to be inside the user's workspace, the task's deferred directory, this task's channel directory, or `Talk` (reads only). Anything else is refused with `"reason": "host_path_refused"` and nothing happens
- A file that leaves the task is narrower still: `email --attach` and `--body-file`, `memory_search index file`, `nextcloud files upload --local` and the `health` upload/import verbs take the user's own workspace and the deferred directory, so a `Talk` attachment or a channel file has to be copied into the workspace before it can be mailed, indexed or uploaded
- One refused path refuses the whole call — `email --attach a b c` with `b` outside the allowlist sends nothing, rather than sending two attachments
- `health import-immunizations --paste @PATH` is gone. Use `--paste-file PATH`; `--paste` is literal text and refuses a leading `@` rather than importing it as a record
- An empty value is refused rather than read as "not given": `--output ""` used to mean the process working directory, which is not a directory anything named
- Handlers receive the *resolved* path, so a symlinked or relative argument comes back in the output as the absolute path it pointed at
- `google_workspace` arguments are scanned for host paths before `gws` runs, under the read rule — `drive +upload` therefore still accepts a `Talk` attachment, where `email --attach` no longer does. Google's own identifiers (`--fileId`, `--parents`, spreadsheet ids, a `--query` containing a slash) are untouched, and a relative path resolves against the user's workspace

## 2026-07-26
- `nextcloud` grew from four sharing verbs into the full control plane: `capabilities` (a deployment fit-check), `user`/`group` lookup, extended `share` including `share link` for download links, a `files` group for the WebDAV operations the mount can't express, a `talk` control surface, and `notify`/`activity` reads
- Failures now carry the HTTP status, the OCS status code and the server's own message instead of "Failed to …"
- The skill hides itself on deployments with no Nextcloud (`requires_capability`)

## 2026-02-08
- `memory-search` is now always-included — semantic search available without keywords
- Memory search enabled by default with proactive usage guidance
