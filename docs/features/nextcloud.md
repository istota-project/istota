# Nextcloud

Istota depends on Nextcloud for file storage, sharing, chat transport and (via CalDAV) calendars. The `nextcloud` skill is the agent's control surface over that server: what it supports, who is on it, what is shared with whom, and the file operations the mounted filesystem cannot express.

Everything runs as the bot's own Nextcloud account (`nextcloud.username` / `nextcloud.app_password`). That is the accurate identity for the operations in scope: the workspace layout lives in the bot's storage and is shared out to each user, and the bot is the Talk participant. There is no `--as-user` mode — Istota does hold per-user Nextcloud OAuth tokens, but their encryption key is delivered to the web unit only and deliberately never reaches a task environment.

## Availability

The skill declares `requires_capability: [nextcloud]`, satisfied when `nextcloud.url` is set. On a standalone local install with no Nextcloud it disappears from selection and from the on-demand menu entirely, rather than offering verbs that would all fail.

Reading and writing ordinary files is **not** part of this skill. The workspace is a mounted filesystem; the `files` skill and ordinary POSIX tools handle that, everywhere, with or without Nextcloud.

## Command groups

```bash
istota-skill nextcloud capabilities [--raw] [--check a,b]
istota-skill nextcloud user     whoami|search|get|groups
istota-skill nextcloud group    list|members
istota-skill nextcloud share    link|list|get|create|update|revoke|delete|search
istota-skill nextcloud files    stat|list|search|upload|download|versions|restore-version|trash|favorite|quota
istota-skill nextcloud talk     rooms|room|create|rename|describe|invite|participants|read|send|share-file|mentions|search|leave|delete
istota-skill nextcloud notify   list|get|dismiss|dismiss-all
istota-skill nextcloud activity list
```

All output is JSON. `istota-skill nextcloud <group> --help` lists a group's flags.

## Errors carry the reason

Every failure is a structured envelope rather than a bare string:

```json
{
  "status": "error",
  "error": "Unauthorised — this endpoint usually requires admin rights…",
  "http_status": 200,
  "ocs_status": 997,
  "endpoint": "/cloud/users/alice"
}
```

`ocs_status: 997` on a provisioning endpoint almost always means the bot is a regular user on that server, so the message names admin rights as the likely cause and points at `user search` as the alternative. The same envelope shape is what `scheduler._execute_command_task` detects, so a failed verb marks the task failed with the server's message intact.

## capabilities — the deployment fit-check

```bash
istota nextcloud capabilities                                # operator shell
istota nextcloud capabilities --check sharing.public,talk    # exits non-zero if missing
istota-skill nextcloud capabilities                          # from a task
```

One call reports server version and edition, the WebDAV root, the sharing knobs (public links enabled, password enforced, maximum expiry), Talk features, notification endpoints, activity API, versioning and undelete, chunked upload, and the bot account's quota.

`--check` takes dotted feature names and exits non-zero when one is missing, so it works as a deployment gate in a shell or a heartbeat `shell-command` check. Names:

| Name | Meaning |
|---|---|
| `sharing`, `sharing.api` | Sharing app present / API enabled |
| `sharing.public` | Public links allowed |
| `sharing.public.password_enforced` | Server requires a password on links |
| `sharing.public.expire_date`, `sharing.public.expire_date_enforced` | Link expiry available / mandatory |
| `sharing.resharing`, `sharing.federation`, `sharing.email` | Resharing, federated shares, email shares |
| `talk` | Talk (spreed) installed |
| `notifications`, `activity` | Notification and activity apps |
| `files.versioning`, `files.undelete` | Version history, trash bin |
| `dav.chunking` | Chunked upload |

This is the verb to reach for when a managed Nextcloud refuses something and it isn't clear whether it's a permissions problem or a missing app.

## Sharing, and the download-link workflow

`share link` is the answer to "give me a download link":

```bash
istota-skill nextcloud share link "/Users/alice/report.pdf"
istota-skill nextcloud share link "/Users/alice/report.pdf" --days 3 --password-generate
istota-skill nextcloud share link "/Users/alice/project" --file notes.md
```

A link share is live (it serves the current file, not a copy), revocable, expiring, and optionally passworded. It applies `nextcloud.share_default_expire_days` (default 14) unless `--days 0` opts out, and clamps to the server's enforced maximum with a `notice` saying it did.

The response carries the whole lifecycle: `url`, `download_url`, `token`, `share_id`, `expires`, `has_password`, `permissions`, and a literal `revoke_command`. `download_url` matters because a link share resolves to a preview page, not a file — a file or whole folder takes `/download`, and `--file NAME` targets one entry inside a shared folder.

`share revoke` closes the loop by id, by token, or across a path. The path form can remove several links at once, so it refuses without `--confirmed`.

**A public link is for giving a file to someone else.** It is a bearer URL: anyone who ends up holding it can open the file, so "who did I show this to" stops being answerable. That makes it the wrong instrument for handing a user a file they already own — in web chat the assistant uses the authenticated `GET /api/chat/files?path=` download instead (see `config/guidelines/web.md`), and in Talk the file is already in the user's Nextcloud. The confirmation rule follows from that: a user *asking* for a link is itself the authorization, the assistant deciding a link would be handy for the requesting user means not creating one at all, and any other recipient is confirmed every time. `--type user|group` is the narrower instrument whenever the recipient is known — revocable, attributable, and reaching exactly one account.

Expiry is computed from **UTC**, not the local clock, because Nextcloud evaluates it against its own. A caller west of the server rolls over later than the server does, so a local base makes `--days 1` land on a date the server already calls past — a several-hour window every day, and a hard failure rather than a wrong date. A server running *ahead* of UTC can still see a one-day expiry as same-day; that residual would need the date from the server itself.

Nextcloud rate-limits share creation to 20 per 10 minutes per account. Past that, every attempt returns a 429 whose message names the cap and, when the server sends `Retry-After`, how long to wait — a bare status code gives an agent nothing to decide on, and the skill tells it to report the wait rather than retry into another refusal.

## files — only what the filesystem can't do

There is deliberately no `read`, `write`, `mkdir`, `rm`, `mv` or `cp`. The mount does those with ordinary POSIX calls, and an HTTP variant would give the model two ways to do one thing with no rule for choosing.

What is here:

- `stat` / `list` — server-side properties the filesystem cannot show: `fileid` (the stable id the versions API keys on), `permissions`, `share_types`, `favorite`, `owner_id`, preview and mount type.
- `search` — indexed, server-side, scoped, filtered by name glob, MIME, minimum size and mtime. A `find` over the FUSE mount walks the network filesystem and is unusably slow on a large tree.
- `versions` / `restore-version`, `trash list|restore|empty`, `favorite`, `quota`.
- `upload` / `download` — the exception to the rule, for a large file (chunked above a threshold, plain `PUT` below, and a plain `PUT` when the server reports no `dav.chunking`), a file originating outside the mount, and rclone mode, where there is no local path at all.

`files trash empty` is irreversible and refuses without `--confirmed`.

## talk — a control surface, not a delivery path

The transport seam and its persistent-loop `TalkClient` keep owning inbound polling and outbound delivery. This group is for the agent to look a room up, read what was said in one, search history, or deliberately post somewhere other than the current conversation.

`talk share-file` is the verb that bridges the two surfaces: a Talk attachment is a share of type 10 whose `shareWith` is the conversation token.

The skill CLI is a one-shot subprocess with no persistent asyncio runtime, so it uses `talk.transient_client(config)` — the single documented exemption to the "no `TalkClient` outside the singleton" invariant, recorded in `.claude/rules/transport.md`.

`talk search --token` restricts results to one conversation, and does so client-side. The unified-search provider's `from` parameter looks like the way to scope a search but means "the page I am currently on", so the provider *excludes* that conversation — passing the requested token there returns every room except the one asked for. The filter matches on each entry's `attributes.conversation` instead, over-fetching so `--limit` applies to the matching subset.

## notify / activity

Read and dismiss only. *Sending* a Nextcloud notification needs the `admin_notifications` app and admin rights, and the bot already has two working push channels to its own user (ntfy and Talk), so the send path is deferred rather than half-built. `dismiss-all` is gated on the server advertising the `delete-all` endpoint.

Both feeds are bounded by default (25 entries) — an unbounded activity stream is a context-flooding hazard.

## Trust posture

**Inbound is untrusted.** Talk messages, participant and room names, share notes, notifications and activity entries are all text other people wrote. Every such payload comes back wrapped in an explicit untrusted-content delimiter, and the skill declares `companion_skills: [untrusted_input, sensitive_actions]` so the inbound-handling guidance arrives whether the skill was selected eagerly or pulled from the menu. A room the bot merely sits in is an ingestion surface.

**Outbound is confirmation-gated.** Creating a share, sending a Talk message, posting a file into a room and inviting a participant all reach other people. Two narrow exemptions, stated in the skill body rather than left to inference: a share with the task's own user, and a message to a room where the user is the only other participant.

**Destructive verbs default to refusing.** `talk delete`, `files trash empty` and `share revoke --path` return `{"needs_confirmation": true, …}` without `--confirmed`, so the model gets a machine-readable "this needs a confirmation turn" rather than an indistinguishable failure.

**Paths are scoped to the caller.** The bot's credentials reach every user's workspace; the skill must not. `files` and `share` verbs resolve a caller-supplied path against `/Users/<caller>/` and refuse anything escaping it (after `..` normalization) unless the caller is an admin.

## Configuration

```toml
[nextcloud]
url = "https://nextcloud.example.com"
username = "istota"
app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
share_default_expire_days = 14   # 0 = no default expiry on `share link`
```

Ansible: `istota_nextcloud_share_default_expire_days`.

Credentials reach the skill as `NC_URL` / `NC_USER` / `NC_PASS`. Only `NC_PASS` is `sensitive`, so it is stripped from the model's environment and injected server-side by the skill proxy. With the proxy off, the Nextcloud host is added to the CONNECT allowlist when the skill is authorized.

## Client internals

The client lives in `src/istota/nextcloud/`:

| Module | Contents |
|---|---|
| `_http.py` | `ocs_request` + `ocs_get/post/put/delete`, `dav_request`, `OcsError`, the OCS status table, path scoping |
| `capabilities.py` | Capabilities probe, feature map, `--check` evaluation |
| `shares.py` | Share CRUD, the link helper, download-URL synthesis |
| `users.py` | `whoami`, autocomplete search, groups |
| `dav.py` | PROPFIND, SEARCH, versions, trash, upload/download, favorites, quota |
| `notifications.py` | Notifications and activity reads |

`src/istota/nextcloud_client.py` remains as a back-compat shim holding the `None`-returning variants four best-effort daemon paths depend on: startup user hydration, the `ocs_share_folder` pre-check, the shared-file organizer's owner lookup, and `!search`. A Nextcloud hiccup must not fail daemon startup, so those keep the historical contract; only the CLI takes the raising path.
