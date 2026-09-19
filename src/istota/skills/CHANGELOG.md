# Skills Changelog

## 2026-09-19
- New `rooms` skill: `istota-skill rooms list` names every room you are in — its token, its name, which surface it lives on, whether it is also open in Talk, and the `target` descriptor to deliver into it. `nextcloud talk rooms` lists *Nextcloud Talk conversations*, so a web chat room was absent from it and read as a room that did not exist
- The prompt header now names the room a task is in, with the `target` and `room` values to write in `CRON.md`. "Post this here" no longer needs a lookup
- `schedules` and `reminders` document the room descriptors. `target = "talk"` with a web room's token posts nowhere, and `room:<token>` — the spelling that reads as "this room, every surface" — delivers nothing at all from a scheduled job
- `nextcloud talk create` refuses a name your room registry already holds, and says what to write instead. A conversation created that way is bound to no room: it never carries the room's transcript and nobody is watching it. `--force` creates one anyway

## 2026-09-07
- `whisper transcribe --save` writes beside the audio file, so it now needs a directory you could write to yourself. A recording in the user's workspace, in this room's directory or in the task's deferred directory saves as before; one in `Talk` is refused with `"reason": "host_path_refused"`, before the transcription runs, since `Talk` is shared and read-only. Copy the recording into the workspace and transcribe that, or take the transcript out of the output and write it where it belongs

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
