# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The assistant can now read your mailbox, not just send. New email commands list, search, read, thread, and fetch attachments, batch-fetch mail from named senders for digests, and reply / reply-all in-thread, with cc, bcc, and attachments on outbound mail.
- Read scoping: you see your own mail plus the shared pool (anything sent to the bare bot address), and never another user's. Mail meant for one person goes to their `bot+<you>@…` plus-address.
- Quiet senders: name senders (fnmatch patterns) whose mail should be filed silently — no task, no assistant session. Their mail waits in the inbox for a briefing or scheduled job to read back on demand. Set it per user on the Settings → Preferences card or with `istota user ensure --quiet-sender`.

### Changed
- Marking or deleting mail now requires an explicit confirmation flag, so a stray or content-driven request can't quietly change or destroy your mailbox.
- Email connections use an explicit socket timeout, so an unreachable mail server fails fast instead of hanging the poll.
- The cross-room chat views (All, Unread, Starred) are quieter: the room label drops its hash, and hovering a message shows only its task number and star instead of the model and timings. Room views are unchanged.
- The feeds sidebar's All / Unread / Starred entries match the chat sidebar's, and chat's New room button now sits below the views rather than above them.
- The Nextcloud connection card moved under Connected services, above Google Workspace, and reads like the other service cards — a status pill beside the title. Connect and Disconnect are now the same button in two colours.

## [0.30.0] - 2026-07-16

### Added
- Cross-room chat views: All, Unread, and Starred entries in the web chat sidebar show one combined message stream across every room, with a room label on each message that jumps to its room. The Unread entry carries a total-unread badge and the views page backwards on scroll like a room transcript.
- Per-message starring in web chat: hover any finished message (from either surface — web or Talk) and hit the star to bookmark it. Stars are private to you, and the Starred view collects them.
- A mark-all-read button in the chat header clears every room's unread badge in one confirmed click.
- Opt-in Nextcloud connection for web chat (encrypted token storage): a message sent from the web UI now appears in the bound Talk conversation instantly, authored by you, with the bot's answer following as its own post — replacing the bot-attributed repost at task completion. The retained login token is encrypted with a key only the web process holds, and a settings card shows the connection with a Disconnect button.
- Read-state sync between web chat and Talk (same opt-in): reading a room in the web UI clears its badge on your Talk clients, and reading it in Talk clears the web badge within a minute. Web-only notification messages keep their badge until actually seen in web.

### Changed
- Rendered lists in web chat have more space between items, so they're easier to scan.

### Fixed
- The admin dashboard's system banner stacked its cells vertically instead of laying them out side by side.

## [0.29.0] - 2026-07-14

### Added
- The bookmarks skill can now read Karakeep highlights — the passages you've highlighted in an article, with your annotations. Fetch all of them or just one bookmark's, ready to pull into notes.

### Fixed
- Monarch sync no longer books pending (not-yet-settled) card charges. Monarch replaces a pending charge with a fresh one carrying a new id and the final amount once it settles, which left the pre-tip pending copy stranded in the ledger as a duplicate. Transactions are now imported only after they settle, so the final amount (tip included) is the only entry that lands. A recently-pending charge appears a sync or two later.

## [0.28.0] - 2026-07-13

### Added
- A restore command brings a per-user database back from its most recent good off-host snapshot, refusing to overwrite a database the app still has open or to restore an obviously-empty snapshot by mistake.

### Changed
- Per-user module databases (feeds, health, location, money) now live on local disk and run in WAL mode instead of on the Nextcloud mount, which restores concurrent reads and writes. Your files (bloodwork uploads, ledgers, feed exports) stay in Nextcloud as before; only the database index moved. A one-time relocation runs automatically on deploy, and the databases are snapshotted back to Nextcloud daily so they stay backed up off-host.
- Off-host database backups now keep a rolling history of dated daily snapshots instead of overwriting a single copy, so a corrupted or emptied database can't wipe out the last good backup. A snapshot that suddenly comes back empty is set aside rather than trusted, backups are skipped instead of written to the wrong place when the Nextcloud mount is down, and you're alerted if backups start failing or silently stop.

### Fixed
- The scheduler could stall for several minutes under database lock contention, briefly pausing all task dispatch. Fixed the connection handling and storage that caused it, with a safeguard so a locked read skips a cycle instead of blocking the loop.

## [0.27.0] - 2026-07-08

### Added
- Garmin GPS track import into the location module. A new importer pulls tracks for watch-recorded runs, hikes, and walks into your location history and map, filling only the gaps where the phone-based tracker has no data — your phone's track always wins where it exists. It never double-tracks a run the phone already recorded, and re-running is safe. Imported points show on the map but don't create place visits.
- One-click GPS track import: an "Import GPS tracks" button on the Garmin card (Settings → Connected services) imports the last 30 days on demand. You can also ask the assistant in chat ("import my garmin tracks") — it runs the import and reports back what it added.
- Location pings now record their source (phone tracker vs. Garmin), so the two can be told apart.

### Changed
- Garmin Connect moved from the Health settings page to Settings → Connected services, since it now feeds both Health (daily summaries) and Location (GPS tracks). Connect once and both use it; the Health settings page links across to it.

## [0.26.3] - 2026-07-08

### Added
- Section navigation in the web app (Health, Money, Location) collapses into a dropdown on phone-width screens instead of wrapping, keeping the header on one line.

### Changed
- Dropdowns across the web interface now use a single consistent, theme-matching control instead of a mix of the app's styled dropdown and native browser ones. This covers form dropdowns (settings, health, feeds) and filter controls (money year, feeds sort, location activity, medical-history type), with dropdown heights now matching the text fields beside them.
- Card and tile grids across every module (dashboard, health, feeds, money, admin, settings) share one responsive layout.

### Fixed
- The Health stats page no longer scrolls sideways on phones; its cards reflow to fit narrow screens.
- On the Health "log measurement" form, the value field is no longer squeezed to near-zero width by the unit picker next to it.
- Manually adding a transaction to a ledger now actually records it. The `add-transaction` command reported success but wrote the entry to a location no ledger read from, so it never appeared in any query, balance, invoice, or report. Entries now land in the main ledger alongside every other write path. (The bulk sync and CSV import paths were never affected.)
- The web interface no longer crashes when opening feeds. The per-user feeds, money, location, and health databases sit on the network-backed Nextcloud storage, where the previous database journaling mode could crash the whole web process — showing up as feeds failing to load and intermittent errors. They now use a journaling mode that's safe on network storage.
- Database backups are now resilient to a single failing database. Previously one database that failed to back up would silently abort the entire run, skipping every remaining user's backup and leaving a stray temporary file. Each database and file sync is now backed up independently, the run reports a clear failure (and can trigger an optional alert) instead of stopping early, and temporary files are always cleaned up.

## [0.26.2] - 2026-07-07

### Changed
- Migrated the Docker apt repository setup in the Ansible deploy to the deb822 source format. The old `apt_repository` module is deprecated and due for removal in a future ansible-core release; the stale auto-named `.list` file is cleaned up on the next run. Deployment-only change.

### Fixed
- Hardened the Nextcloud rclone mount on the production deploy. A dead mount endpoint now self-heals on service restart instead of needing a manual unmount, and a slow or wedged backend fails fast (about 30 seconds) rather than blocking a file operation for minutes.

## [0.26.1] - 2026-07-03

### Changed
- Removed the gradient fade over the bottom of feed grid cards. It was meant to signal clipped content but overlapped the card's meta row (star, feed name, date); cards now clip with a clean edge.

### Fixed
- The assistant no longer mistakes a publication date in fetched content for today's date. When a task ran late in the evening — past midnight in UTC or Europe — it could pick up a "tomorrow"-stamped feed item or web page and write summaries, scripts, and reminders as if that were the current date. It now always trusts the date given in its own prompt.
- Health dates (vaccinations, lab draw dates, encounters) no longer show a day early for viewers in timezones west of UTC. Date-only values were read as UTC midnight and then shifted into the local timezone; they now render on their own calendar day.
- Importing medical conditions or immunizations from more than one source no longer creates duplicate entries. The same condition named across two documents reconciles to a single record — matched by diagnosis code, or by name when no code is present — and a repeated vaccination merges unless it's a genuine booster on a different date.
- Feed entries that were saved before the image de-duplication fix now get corrected too. The earlier fix only cleaned up newly-fetched entries, so already-stored posts — most visibly xkcd, whose whole body is a single comic image — kept showing the image twice. A one-time cleanup pass now rewrites existing entries so each image shows once, while leaving genuine inline images in place.

## [0.26.0] - 2026-07-03

### Added
- The feed reader can now show every entry in a whole category, not just an individual feed or all/unread — click a category name in the sidebar to filter to it.
- You can now open and read a full post straight from the feed grid, without switching to list view. Clicking a card opens a reader overlay with the full content; the arrows step through the current view (paging in more as needed) and disable at the ends.

### Changed
- Repositioned how the project describes itself. Istota is now presented as a self-hosted personal AI assistant that works with any model — Claude, or any OpenAI-compatible endpoint like OpenRouter — and integrates with Nextcloud as a deep but not required integration, rather than something that runs inside Nextcloud. The README was also streamlined.

### Fixed
- Feed entries no longer show the same image two or three times. Some feeds list one photo at several sizes, and others embed the article's lead image in the body as well as using it as the thumbnail; the reader now shows each image once while keeping genuine inline images further down an article.

## [0.25.0] - 2026-06-25

### Changed
- The headless browser now heals itself from a wider class of freeze. Its health check used to ask only "is the browser process alive?", so a browser that was running but internally frozen — pages and the remote view both hung — still looked healthy and never got restarted. The check now also confirms the browser is actually responding, so a genuine freeze is caught and the existing restart-and-alert machinery kicks in, while a long legitimate page load still isn't mistaken for a hang.
- The assistant can now properly tidy its own long-term memory. It can edit a saved note in place, remove an outdated note, and drop a whole stale section — including notes filed under a sub-heading, which it previously couldn't touch at all. Before this it could mostly only add new notes, so old or wrong entries tended to pile up.

### Fixed
- Stopped the assistant's memory file from leaving a stray `.lock` file in each user's cloud folder. The short-lived lock that guards memory writes now lives in local scratch space instead of next to the memory file on the network drive, where it cluttered the folder and where the lock itself could be unreliable.
- Fixed silent memory loss for non-admin users. Long-term memory writes by a non-admin resolved to a wrong, doubled folder path that nothing ever read back, so the saved note effectively disappeared. Non-admin file paths are now resolved correctly; per-user isolation is unchanged (still enforced by the sandbox).

## [0.24.0] - 2026-06-17

### Added
- The assistant can now search the web. Web search is available to every task; reading a specific page is routed to the built-in browser so it can handle JavaScript-heavy sites.

### Fixed
- A single hung scheduled task can no longer wedge the background queue. One stuck job used to hold the only background slot indefinitely, so other scheduled work piled up unrun and each piled-up task eventually posted a "task cancelled" notice — a once-a-minute flood for a per-minute job. Now a scheduled job won't start a new run while its previous run is still going, a stuck task's whole process tree is force-stopped at the timeout, and those internal cancellation notices are no longer sent to chat for automated tasks.
- A slow or unreachable news/markets source no longer freezes the whole assistant. Briefings used to fetch all their live data on the scheduler's main loop, so when a source hung, every chat message in every room went unanswered until it returned. Briefings now defer that fetch to the background worker that runs them, so a stuck source only delays that one briefing.
- Fixed duplicate invoice numbers when generating invoices. After accounting config moved into the database, the next-invoice counter was still written to an old config file that nothing reads, so it never advanced and a later run reissued numbers that already existed. The generator now starts from one past the highest invoice already issued and saves the advanced counter back to the database, which also reconciles a counter that had already drifted.
- Fixed a startup crash that could leave the app in a restart loop on upgrade. When the database already held completed conversations, the one-time chat-history migration failed to read its own rows and aborted initialization. It now reads them correctly and finishes.
- Nextcloud Talk rooms the bot sits in but hasn't been messaged in now show up in web chat. Previously a room only appeared once someone had addressed the bot there; a quiet room the bot had merely joined stayed invisible. The bot now registers each room it participates in as it polls, so they surface on their own.

### Changed
- The headless browser now heals itself more reliably. Its watchdog checks more often and only restarts when the browser is genuinely wedged (a long, legitimate page load no longer looks like a hang), stops restarting in a loop when something is persistently broken, and can alert an operator when it acts. As a safety net, the assistant also warns an operator if its task scheduler ever stops making progress.
- The accounting tools are now fully part of Istota, with no separate standalone mode. Every accounting operation is reachable from the command line as `istota money …` (for example `istota money invoice generate -u <user>`), resolving the user and configuration the same way as the rest of Istota. The old separate `money` command, its environment-variable config, and the file-based config fallback are gone — all accounting configuration now lives in the per-user database.
- The assistant now runs with its full built-in toolset instead of a fixed allow-list of a handful of tools. Because it runs non-interactively, any tool outside that list used to be silently unavailable; the sandbox, network proxy, and credential stripping are the real security boundary, so the allow-list added little. Its own multi-agent fan-out tool stays disabled, so work keeps flowing through Istota's own skills.
- Deleting an imported Talk room from web chat is now clearly a per-person hide, not a delete: it's a single click with no type-the-name confirmation, the wording says so, and the room comes back if you post in it again. The Talk conversation and its history are never touched, and hiding it for yourself doesn't hide it for anyone else. Deleting a web-only room is still a real, confirmed delete.

## [0.23.0] - 2026-06-10

### Added
- The assistant can now learn reusable "playbooks" — step-by-step procedures for tasks it has done before. After a successful multi-step task, the nightly memory cycle distils how it was done and, when a similar request comes up later, recalls that approach so it gets it right on the first try. Playbooks are plain guidance the assistant reads, never code it runs. Off by default; an operator enables it.
- Progressive skill loading is now the default way the assistant picks its tools. Heavy skills (like the developer or health tools) load as a one-line summary up front, and the assistant pulls the full instructions on demand only when it needs them. The summary list now covers every skill it could use, so it can reach for the right tool even when a task doesn't obviously call for it — while the prompt stays small.
- Web chat now flags rooms with unread messages: the room name goes bold in the sidebar and a small count chip appears to its right. Opening a room clears it, and a room that receives a notification or scheduled post while you're elsewhere lights up on its own. Unread is tracked per person and only for the web view, so reading on the phone via Talk doesn't change it.

### Changed
- Removed the separate helper model that pre-guessed which extra skills a task needs. That step started a fresh process on every task and added several seconds of delay before any work began (and was timing out in production). The wider skill summary list above gives the main model everything it needs to choose its own tools, with no per-task delay, so the helper and its settings are gone entirely.
- Rewrote the assistant's built-in operating instructions (the opt-in custom system prompt) to track Claude Code's current guidance more closely. It now leads with the outcome, carries stronger coding-craft rules, and adds a self-check step that has the assistant prod its own work — re-reading the change, running the real command, reporting honestly — before answering. Aimed at better, more reliable coding output. Takes effect on the next deploy.

### Security
- The opt-in developer dev-container (an escape hatch the assistant uses for tasks the sandbox can't handle) no longer has the host's full Docker control socket inside it. A per-user proxy now sits in front of the socket and allows only a small set of operations on that user's own container — running commands, copying files, inspecting it, and restarting it — while refusing anything that could create new containers, mount the host filesystem, or gain elevated privileges. This closes a path where a task could have used the socket to take over the host.

### Fixed
- The headless browser no longer navigates to a stale page when Chrome auto-completes a typed address from history. It now clears any auto-completed suffix before submitting the address, so it goes where it was told.
- Web chat no longer shows raw placeholder tokens in mirrored Talk messages. A message that shared a file, @mentioned someone, or used a poll/location/card used to render literal tokens like `{file}` or `{user1}` when its history was recovered from the message cache. These now resolve to the file name, an @name, `@all`, or a readable label.
- Web chat no longer drops detail when the assistant works in steps. When it wrote a substantial explanation, then took an action (like editing a note), then gave a short confirmation, the explanation used to vanish from the reply once the action started — leaving only the terse wrap-up. Substantial intermediate passages now stay visible as their own blocks in the answer, in order, with the tool activity folded between them. Short lead-ins ("Let me check…") are still hidden, and the intermediate blocks and tool-activity chips now have clean spacing between them. Applies to the live web/REPL view and to reloaded history alike.
- A group Talk room (the bot plus more than one person) now appears in the web chat room list for every participant, not just one. Rooms used to be owned by a single user, so a shared conversation could surface in web for only one member and was invisible to the rest. Each participant now gets their own view of the same room, and history stays shared. Deleting or hiding such a room in web now only hides it for you, instead of removing it for everyone.
- Follow-up steps the assistant queues for itself (a cleanup, a deferred action) no longer fail silently when malformed. A queued step with the wrong shape, or saved under an unexpected filename, is now logged instead of vanishing with no trace.
- The assistant can record a lab panel and its individual markers in a single step. Previously, adding the markers right after creating the panel could fail because the panel didn't have an id yet; it now refers to the just-created panel by name within the same operation.

## [0.22.0] - 2026-06-09

### Added
- A conversation can now be carried across Nextcloud Talk and the in-app web chat. A new surface-independent room registry backs both, with one canonical message store per room, so opening a Talk room in web shows its full history and a reply typed in web is mirrored into the Talk room (and onto your phone). The bot keeps full cross-surface context with no extra work. Talk rooms the bot is in surface automatically in the web room list; a web room can be promoted to Talk ("Also open in Talk") to create a real Nextcloud Talk conversation, and renames propagate both ways. When a room is open in web while a Talk message is being answered, the reply streams live in both places.

### Changed
- Talk rooms shown in the web chat room list now display their real Talk titles, pulled automatically on each poll, instead of a generic "Talk room". A room folded in from before this change picks up its title the next time the bot polls.
- A question you type in web chat is now reposted into the mirrored Talk room (attributed to you) just above the bot's reply, so the Talk side reads as a normal exchange rather than an answer with no visible question. The repost is Talk-only — it never duplicates your message in web history or cross-surface context.
- The calendar and location skill CLIs now accept the natural subcommand names the assistant tends to reach for: `calendar agenda` works as `calendar list`, and `location last` as `location current`. The assistant is also now told to confirm a skill subcommand exists (running `--help` when the skill's docs aren't loaded) instead of guessing from memory.

### Fixed
- Web chat now shows a room's real history even after its turns age out. The transcript is read from the durable message store instead of the task queue, which is cleaned up after a few days — so a dormant room used to open to a handful of stale, out-of-context messages while its actual conversation had been pruned. A one-time recovery (`istota chat backfill-history`) rebuilds older rooms' history from the Talk message cache.
- Web chat messages now show their date, not just the time of day. Days are separated by a divider row labelled "Today", "Yesterday", a weekday, or a date, so backfilled history is no longer ambiguous.
- The web chat composer placeholder is now a plain "Your message…" instead of prepending the room name with a "#".
- Talk-origin rooms in the web chat sidebar are now marked with a small cloud icon on the left of the name instead of a trailing "Talk" chip, so long room names get the full row width.
- A Nextcloud Talk room you delete (or remove the bot from) no longer lingers in the web chat room list. The bot now reconciles its room list against Nextcloud each poll and hides rooms it's no longer part of.
- Closed a path where the bot could silently lose earlier conversation history. The unified history reader now falls back to its complete source whenever any past turn isn't yet mirrored into the new message store, instead of switching over as soon as the latest turn was. Relatedly, the one-time room-sync migration no longer marks itself done if a step fails partway, so it retries cleanly on the next start rather than stranding a partial copy.
- A task that was mid-response when the scheduler restarted no longer hangs as an uncancellable spinner in web chat. On startup the scheduler now reclaims tasks the previous run left in progress right away — retrying them, or cleanly cancelling and closing the ones you'd already asked to stop — instead of waiting out the several-minute stale-worker timeout.
- Scheduled and recurring jobs that post to a room (a location alert, a daily sync) now also appear in that room's web chat view, not just in Talk, so a room whose only recent activity was scheduled output no longer looks frozen at its last conversation. A one-time cleanup also clears noise an earlier history backfill left in some web rooms — literal "NO_ACTION" lines and empty placeholder prompts.
- The hover metadata under a web chat message now lines up with the message's timestamp instead of floating above it.

## [0.21.0] - 2026-06-09

### Added
- New `tmux_claude` brain that drives the interactive Claude CLI in a detached tmux session, so model traffic keeps drawing on a Claude subscription instead of the metered headless credit. Switch the whole instance to it with `brain.kind = "tmux_claude"`; the standard `claude_code` brain stays as an automatic fallback. If the tmux path can't drive a task, that task falls back to the headless brain, and after repeated failures a circuit breaker degrades to headless instance-wide and alerts the operator — so a broken CLI upgrade is one alert plus graceful degradation, not a pile of timeouts. Web/REPL still stream tool use and intermediate text live. Tunable via a new `[brain.tmux]` config block (fallback thresholds, timeouts, a pinned CLI version, and the dialog/readiness markers). Works in the Docker image (which now bundles tmux) including when the container runs as root.
- The scheduler daemon now logs a periodic one-line health summary (thread count, open file descriptors, memory use, running tasks, and active workers) so operators can spot resource leaks early in the logs. The interval is configurable and can be turned off.
- When someone replies to an email the bot sent on your behalf, the reply now comes back to wherever you asked for it — the web chat room, Nextcloud Talk room, or email thread it started in — instead of only landing in your email. By default the reply appears in the origin surface *and* continues the email thread, so an external correspondent who replied by email still gets an email reply. A new per-user setting (`istota user ensure --email-reply-routing origin+thread|origin|thread`) switches this to origin-only or thread-only.

### Changed
- Web chat picks up a quick follow-up message much faster. A message sent moments after a reply is now claimed within about half a second instead of waiting several seconds for the idle worker's poll cycle.

### Fixed
- A task interrupted by a scheduler restart could be picked up and run by two workers at once on retry, producing two different answers for the same request. Reclaiming a stuck task now resets its liveness state so a second worker can't grab it mid-run, a worker that was superseded mid-run discards its result instead of delivering a duplicate, and the threshold for declaring a worker dead was widened to avoid false reclaims of a busy-but-alive worker.
- Quieted repetitive startup log noise from background skill subprocesses (feeds/money) that loaded config without the secret key or admins-file path. These expected, harmless notices are now DEBUG, and the admins-file path is passed through so it resolves correctly on custom-namespace deployments.
- Light mode: the dashboard feature cards no longer render with dark backgrounds and unreadable titles, and the mobile slide-out sidebar tab now matches the light theme.
- Light mode web chat: the message area and composer now share a clean white background, with lighter code blocks and activity chips. Dark mode is unchanged.
- When two messages are queued in the same chat room, the bot no longer briefly spins up a redundant worker that just churns the database while waiting for the first to finish.

## [0.20.0] - 2026-06-08

### Added
- A light/dark theme toggle in the web UI. It switches the whole interface between light and dark, and your choice is remembered per browser.
- Web chat now syntax-highlights fenced code blocks, so code in replies is colored by language.
- Web chat can show the model's own reasoning as a separate, collapsible segment in the activity chip, alongside the tools it ran.

### Changed
- Web chat and the REPL now stream the assistant's answer live, token by token, as the model produces it instead of arriving all at once when the task finishes. This works on both brains — the native brain and the Claude Code brain. Nextcloud Talk is unchanged.
- Web chat renders an assistant turn as ordered, interleaved text and tool steps, with the whole work trace folded into a single activity chip that expands to the full list of tool calls. Only the actual answer streams into the message body; lead-in narration like "Let me check…" is held back so it can't leak into the answer.
- Web chat visual polish: softer overall contrast, lighter message panes, more readable code blocks, and a subtle pulse on the active-tool strip.

### Fixed
- Web chat: a task that fails terminally no longer leaves the room stuck on "Working…" — the room settles to the failure instead of spinning forever.
- Web chat: when an attempt fails and retries, the room now shows a "retrying" notice instead of a silent spinner.
- Web chat: the live stream no longer freezes partway through a response.
- Web chat: several light-mode rendering glitches (message-row hover, the bot avatar initial, and the browser-geolocation dot) are fixed.

## [0.19.0] - 2026-06-08

### Added
- In-app web chat. A new always-on "Chat" tab in the web UI lets you talk to the bot without leaving the dashboard — a full-page console with Discord/Slack-style rooms in a sidebar, live streaming of tool use and intermediate text, inline `!commands`, confirmation prompts rendered as Confirm/Cancel cards, cancel, and drag-drop/paste file attachments. Each room is its own persistent conversation with its own channel memory. It complements Nextcloud Talk rather than replacing it.
- The full `!command` set now works the same in web chat as in Nextcloud Talk, including `!export` and `!search` (export reads your conversation from the database, so it no longer needs a Talk server; search uses the memory index).
- Web chat is now a destination you can route output to. Alerts, the verbose execution log, and notifications can be sent to a chat room — pick "Web chat" in the logs/alerts routing settings, default destination, or briefing output (it appears automatically alongside Talk, email, and ntfy). Routed messages land in your room as a system message and show up live in an open room.
- Web chat rooms now have a settings menu (the ⋮ on each room). You can rename a room, copy its token to paste into a `web:<token>` output route, and delete a room. Delete is a permanent, type-the-name-to-confirm action that removes the room and its whole conversation; a room with a task still running can't be deleted until it finishes. You can also deep-link straight to a room with `/chat?room=<token>`.

### Changed
- The `!model` model-override prefix now works in web chat. Previously `!model opus …` only worked in Nextcloud Talk; in web chat it errored as an unknown command instead of running your message on the chosen model.
- Web chat now uses a Discord/Slack-style transcript instead of chat bubbles: full-width rows with an avatar, the author name and time, and grouping of consecutive messages from the same author. Messages show your real display name and the bot's configured name.
- Web chat message rendering moved to a full Markdown library. Bot replies now render nested lists, blockquotes, tables, strikethrough, and auto-linked URLs, on top of the formatting already supported.
- Web chat live progress is more informative. Instead of a generic "Thinking…" placeholder, the in-progress line shows the same "working on it" verbs Nextcloud Talk uses (now shared between both surfaces), and intermediate text as it streams.
- Web chat tool use is now a single box that shows the active tool with its details while running and a "✓ N tool calls" summary when done; click it to expand the full list of tool calls. Replaces the row of individually-expanding chips.

### Fixed
- Web chat: tool indicators no longer spin forever after a task finishes — running tools are marked complete on the task's final event.
- Web chat: a transient "Something went wrong." no longer flashes mid-response when the live event stream falls back to polling.
- Web chat: confirmation prompts now work. An action that needs your approval parks correctly and shows the Confirm/Cancel card instead of silently completing as a plain message, and any staged side effects wait until you confirm. Previously the confirmation only fired for Nextcloud Talk.
- Web chat: file attachments now actually reach the bot. Uploaded files were saved but never linked to the message, so the bot couldn't see them; the message now carries them through, and a message can only reference files you uploaded.
- Web chat: confirming or double-clicking a task that isn't awaiting confirmation no longer wipes a running task's live progress log.
- Web chat: the server-side stream poll interval is now read from config instead of a fixed value.
- Web chat: Markdown tables now render. The chat renderer gained GitHub-style pipe-table support with per-column alignment, so tabular answers (health, money, feeds data) display as real tables.
- Web chat: fixed a rendering bug where `*` or `**` inside a link URL produced a broken link. (Not exploitable — links were already safe — but the link came out malformed.)
- Web chat: archived rooms no longer accept new messages, and leaving the Chat tab mid-response no longer leaves a stream connection open in the background.
- Web chat: `!command` output (like `!help`) now renders left-aligned with proper lists and code blocks, instead of being centered.
- Web chat: the hover-revealed message metadata (task id, duration, tool count) no longer steals a column from the message body — it overlays the top-right corner, so the content uses the full width (noticeably better on mobile).

## [0.18.0] - 2026-06-07

### Added
- New `<namespace>-run` host wrapper for ad-hoc CLI use in production. It self-sudoes into the service user, loads the same secret bundle and admins file the daemon uses, and passes its arguments straight through to the `istota` CLI — so an interactive REPL is just `istota-run repl -u <user>` instead of a long hand-built environment incantation. Deployed by Ansible; the caller needs sudo rights.

### Changed
- The per-user delivery-routing settings are simpler. The Preferences page previously showed a five-row "per purpose" matrix (reply/alert/log/briefing/notification), most of which duplicated dedicated settings or did nothing. It now shows a default delivery destination plus a single optional "Send alerts to" override for heartbeat and security alerts. Finer per-purpose routes are still settable from the CLI, and any existing CLI-set routes are preserved.
- Briefings can now be sent to ntfy, and the output picker shows the real delivery surfaces (talk / email / ntfy) instead of the old `talk`/`email`/`both` choices. The legacy `both` value still works everywhere it was already saved.
- Delivery-destination pickers (default destination, alert route, briefing output) no longer list internal surfaces that aren't valid user choices — the TASKS.md write-back and terminal-stream surfaces are hidden from the UI. They still work as automatic/programmatic destinations.
- The native-brain per-user API key is no longer offered in the web settings UI. It only ever overrode the key, not the provider or model, so a self-serve toggle implied more than it delivered. Operators can still set per-user keys from the CLI.

### Fixed
- The `<namespace>-run repl` wrapper crashed on startup because it ran the chat in the install directory, which the sandbox refuses to bind as a workspace. Server REPLs now default to the per-user workspace; pass `--workspace` to override.
- **Native-brain tasks lost their conversation context.** Context triage still shelled out to the `claude` CLI even under the native brain, where there's no login token, so the CLI failed and context silently collapsed to the last few messages. Triage now runs through the task's own brain — native tasks use their own provider, Claude Code tasks keep the CLI. Triage also fails open on any error now, keeping all older messages instead of dropping them.

## [0.17.0] - 2026-06-06

### Added
- **The native brain reaches parity with the Claude Code brain on four fronts.** Reasoning effort now flows through to thinking-capable models. Prompt caching marks cache breakpoints (on by default against Anthropic, off for other endpoints) and logs a per-task cache hit-rate. A mid-task context overflow is recovered by compacting the conversation and continuing instead of failing the task. Image-bearing tool results are passed through to vision models. All native-brain only; the Claude Code brain is unchanged.
- **The Bash tool can keep noisy command output out of the model's context.** A command can stream its full output to you while the model sees only a short stub, so large or repetitive output doesn't crowd the context window. Failure markers still ride along on the stub, so a failed command is never mistaken for a success.

### Changed
- Internal: all Nextcloud Talk I/O now runs on one persistent asyncio loop with a single reused HTTP client, instead of spinning up a fresh event loop and connection per call. Connections to Nextcloud are pooled across the daemon's lifetime, and a class of event-loop-teardown leaks becomes structurally impossible. Behavior-preserving; no config changes.
- Internal: email is now a first-class transport (`transport/email/`) mirroring Talk, with shared non-transport helpers in `email_support`. Behavior-preserving refactor; adds end-to-end tests for the email send path.

### Fixed
- Internal: the persistent Talk loop now cancels any in-flight request before closing the shared HTTP client on shutdown, avoiding a spurious "client closed" error when the daemon stops mid-poll.
- Internal: one-shot runs (`istota run`, cron single-pass) now shut the persistent loop down cleanly instead of dropping pooled connections on process exit.

## [0.16.0] - 2026-06-06

### Added
- **Task event streaming — a single persisted event log per task feeds every output surface.** Each task now produces a typed event stream (started, tool started/finished, intermediate text, result, error, cancelled, done) that's persisted and fanned out to Talk, the log channel, push notifications, and new web endpoints. The native brain's tool events carry real durations and success/failure. New SSE, snapshot, and admin task-event endpoints let a client replay a task's progress live or after the fact (backend for the upcoming web chat).

### Changed
- **Progress configuration simplified.** `progress_style` and its rate-limit/display siblings are gone; Talk shows the latest tool call and edits the ack into a completion summary on finish. New `[scheduler]` settings: `event_log_enabled` (kill-switch for the event table), `push_notification_threshold_seconds`, and `push_notification_sources`.

## [0.15.1] - 2026-06-05

### Fixed
- **Slow-but-healthy tasks are no longer reclaimed and run twice** (ISSUE-112). A `running` task was treated as stuck after a hardcoded 15 minutes, well below the 30-minute task timeout, so a long task still executing got re-queued and a second worker ran a duplicate — eventually surfacing as a spurious "task failed" with a backoff cadence. This hit the native brain hardest because it runs in-process with no killable PID to prove liveness. Running workers now emit a periodic liveness heartbeat, and stuck-task reclaim keys on that heartbeat instead of raw runtime: a worker that keeps pinging is never reclaimed however long it runs, while a crashed worker is recovered within minutes. Two new `[scheduler]` settings, `worker_heartbeat_seconds` and `worker_stuck_minutes`, tune it.
- **Native brain now streams in-progress updates to Nextcloud Talk again** (ISSUE-111). The native brain runs its agent loop inside its own event loop, but the Talk progress callback edits messages with `asyncio.run()`, which raises when called from a running loop — so every tool-call and partial-text update was silently dropped and the chat sat blank until the final reply. The brain now dispatches the progress callback on a worker thread, restoring the live action trail. Cancellation stays responsive.

## [0.15.0] - 2026-06-05

### Added
- **Native brain — Istota's own agentic loop.** Istota can now run its own in-process agent loop instead of delegating to the Claude Code CLI, driving any OpenAI-compatible model (Anthropic, OpenRouter, or a self-hosted model). The Claude Code CLI stays the default brain; switch the whole instance or route specific task types to either.

## [0.14.0] - 2026-06-02

### Added
- **Transaction editing in the money web UI, backed by stable transaction ids.** Every beancount transaction now carries an `id:` metadata line — backfilled onto legacy entries by a one-time reversible migration and stamped by every writer — so the kebab-menu "Edit transaction" action rewrites the directive in place (recategorize, fix payee/narration/date/amount), located by id rather than a fragile field tuple. Edits are re-validated with `bean-check` and rolled back if they unbalance the entry. Edited entries are marked so Monarch sync leaves them alone instead of re-applying its own category. New `money edit-transaction` and `money backfill-ids` subcommands expose the same path.
- **Insertion-time staleness gate for cron-driven tasks.** When the daemon comes back from a long outage, `check_scheduled_jobs` and `check_briefings` no longer fire every missed instance on the first tick. Computed `next_run` more than `cron_max_staleness_minutes` (default 60) behind now is skipped and `last_run_at` is bumped so the schedule resumes from the next future fire. Set the threshold to 0 to restore the prior unconditional catch-up.
- **Money web UI: row-click detail and a kebab actions menu.** Transaction and invoice rows now expand their detail (postings / line items) on click, and a kebab (⋮) menu holds the actions. Invoices gain mark-paid, mark-pending, and download-PDF; transactions gain the Edit action (see the transaction-editing entry above for the backend).
- **Timezone is now a settings dropdown.** The settings page offers an IANA time-zone picker instead of a free-text field, with a note that the Istota value overrides the one from Nextcloud.

### Changed
- **Default model upgraded to Claude Opus 4.8.** The bare `opus` alias and the default now resolve to Opus 4.8; `opus-47` / `opus-47-high` aliases were added so the prior version can still be pinned per task or per job.
- **The Istota timezone preference is authoritative over Nextcloud.** A timezone set in the web UI now takes effect immediately across briefings, scheduled jobs, heartbeat quiet-hours, exports, and the sleep cycle — not just the prompt — and survives a redeploy. Nextcloud's zone is only used to seed a new user (ISSUE-099, ISSUE-102).

### Fixed
- **Money ledger writes now serialize the contended writers.** The transaction-edit lock only covered the editor and the id backfill; the Monarch-sync append and manual `add-transaction` wrote unlocked, so a scheduled sync racing a web edit could lose an update or roll back a correct edit. Both writers now take the same ledger lock (ISSUE-104). Ledger rollback restores go through the atomic temp-file write instead of a bare overwrite that could truncate on a FUSE flush (ISSUE-105). Editing a transaction by id now refuses a duplicate id rather than silently editing the first match (ISSUE-106), and refuses an amount edit on a posting carrying a cost-basis or price annotation rather than silently dropping the lot (ISSUE-107).
- **Streaming brain tasks silently failed with "produced no output" after a `claude` CLI update.** A new CLI behavior aborts the stdin read after a few seconds and proceeds with an empty prompt; the brain wrote the prompt only after a PID-recording DB write that can stall under daemon load, so contended interactive tasks lost the race while background command/skill tasks were unaffected. The prompt is now delivered on a dedicated thread so a slow callback can't delay it, and the sandbox network bridge no longer shares or sleeps in front of the prompt pipe.
- **CalDAV `DAVClient` leak in the scheduler exhausted host memory and broke whisper transcription** (ISSUE-101). Every task constructed a fresh `caldav.DAVClient` without closing it; the underlying urllib3 pool spawned a daemon watchdog thread and held an open socket retained for the daemon's lifetime. Over three days the scheduler accumulated 6234 watchdog threads and 6214 CLOSE-WAIT sockets, eating ~5 GB on an 8 GB host. Whisper's memory pre-check was reporting the truth — the cause was several layers away. Fix wraps `DAVClient` in `with`/try-finally at every construction site.
- **Per-user `health.db` is now backed up WAL-safely** (ISSUE-094). It was relying on a plain file copy that isn't safe for a WAL-mode SQLite database; it now uses the same WAL-aware backup path as the feeds, money, and location databases.
- **Whisper memory pre-check rejected loadable models on busy hosts.** `get_available_memory_gb` now returns `available + cached + buffers` instead of `available`. The kernel reclaims page cache under allocation pressure, so the real loadable capacity is meaningfully larger than `psutil`'s conservative estimate — the old gate misfired any time the page cache was warm.
- **The Claude Code harness no longer nudges the bot toward its built-in orchestrator.** The harness auto-injects a "use the Workflow tool" reminder whenever the word "workflow" appears anywhere in the assembled prompt, which fired on essentially every task. Workflow is now disallowed at the tool boundary alongside Agent, and the word was rephrased out of the skill prompt docs so the nudge stops triggering. Istota orchestrates through its own skills, not the harness fan-out tools.

## [0.13.0] - 2026-05-21

### Added
- **Garmin Connect integration for the health module.** Four-stage build-out lands the full pipeline from "user pastes Garmin credentials" to "daily sleep, stress, body battery, steps, SpO₂, HRV, VO₂ max, resting HR, respiration, body composition, and active calories show up in the unified stats time series." Auth flow at `/istota/api/health/garmin/{status,connect,mfa,sync,disconnect}` with a process-local LRU cache for MFA mid-flight; the credentials never round-trip back to the browser (status returns only `{connected, email, last_sync, error}`). The garminconnect SDK is gated `python_version >= '3.12'` and lazy-imported, so istota's 3.11 baseline still installs cleanly. `_RealGarminAdapter` rewritten against the SDK's actual surface — `Garmin.client.dumps()` for the session blob, `return_on_mfa=True` + `resume_login(mfa_state, code)` for MFA — and probes `/userprofile-service/socialProfile` after rehydration to surface unusable sessions early instead of leaving the UI falsely connected. The sync engine partitions errors per Garmin endpoint so a single 5xx doesn't poison the whole tick; only 401/403 escalates to the `garmin_error` flag that prompts re-auth. All measurements stored metric, tagged `source='garmin'`, anchored at noon UTC; a partial unique index on `stats(metric, measured_at, source)` where `source != 'manual'` blocks double-inserts without disturbing the multi-row-per-day manual entry pattern. Scheduler auto-seeds `_module.health.garmin_sync` (`0 */6 * * *`, `--days-back 2`) for users with both the health module enabled and stored Garmin tokens; first seed queues a one-shot 30-day backfill so newly connected accounts populate history without waiting for the first scheduled tick. Stale rows are reaped via the shared module-jobs sync engine when tokens disappear. Settings UI has a three-mode Garmin card (idle → MFA → connected with last-sync + sync-now + disconnect) and `METRIC_LABELS` / `METRIC_UNITS` extended for the 16 Garmin keys so the existing stat picker + charts render the new series automatically. New `istota-skill health garmin-status|garmin-sync|garmin-disconnect` subcommands; skill triggers picked up `garmin`, `sleep score`, `stress`, `body battery`, `hrv`, `vo2`. **Tokens live encrypted in the `secrets` table** (Fernet via `ISTOTA_SECRET_KEY`) — moved out of plaintext `health_settings` during the Mulder/Scully hardening pass alongside per-user timezone awareness, tighter auth-error detection, and a token-wipeout-on-error path. New live integration test exercises the full live-connect → encrypted-store → rehydrate → live-sync round trip against `api.garmin.com` (skipped by default; reads `tests/.env`).
- **Medical history registry — encounters + diagnoses + history summary.** New per-user surface for tracking doctor visits, procedures, hospitalizations, and the diagnoses that come out of them. Two new tables on the per-user `health.db` — `encounters` (date, type, provider, facility, specialty, reason, notes, `dedup_key`) and `diagnoses` (FK to encounter, condition name, status, onset, resolved_at, provider, notes, `dedup_key`) — both with unique partial indices on `dedup_key` so deferred-op retries are idempotent after a partial replay. Inline schema-version bump to 2; existing health DBs migrate in place. Panels gain an `encounter_id` link so a bloodwork draw can be tied to the visit that ordered it. New `/istota/api/health/encounters/*`, `/diagnoses/*`, and `/history/summary` routes (the summary endpoint windows recent procedures to the last 5 years). Skill CLI adds `encounters`, `encounter <id>`, `add-encounter`, `update-encounter`, `delete-encounter`, `diagnoses`, `diagnosis <id>`, `add-diagnosis`, `update-diagnosis`, `resolve-diagnosis`, `delete-diagnosis`, and `history-summary`; all mutations flow through the deferred-op pipeline (`insert_encounter` / `insert_diagnosis` carry `dedup_key` UUIDs so a mid-replay crash can't double-insert). `update_encounter` accepts explicit `None` to clear nullable fields rather than silently keeping the old value. Web UI gets a History tab at `/health/history` (timeline view + conditions list), `/health/history/encounter?id=…` per-encounter detail, and `/health/history/diagnoses` list. Type filter tolerates free-text encounter types with a fallback badge palette. Failed deferred health ops persist to `task_<id>_health_op_failures.json` for auditability.
- **Encounter import from doctor's-visit paperwork.** Mirrors the existing bloodwork and immunization import flows: upload a screenshot or PDF discharge summary, the active brain extracts date, encounter type, provider, facility, specialty, reason, notes, and any diagnoses; reviewer confirms and a single bulk insert persists the encounter plus all linked diagnoses. New `POST /encounters/extract` + `POST /encounters/bulk` routes with the same review-and-confirm UX as the rest of the health import surface; new page at `/health/history/import` with a tab UI for paste-vs-upload.
- **Immunizations registry, parser, web UI, and explainers.** Per-user vaccine tracking with adult-relevant coverage rules. Three new tables on `health.db` (`immunizations`, `immunization_refs`, `immunization_explainers` — the explainers table was subsequently removed when explainer content moved to a static bundle, see below) and a bundled `data/immunization_refs.json` seeded via hash-gated re-seed (19 vaccines: Tdap, Td, MMR, Varicella, Zoster, HPV, Hep A/B, Pneumococcal, Influenza, COVID-19, RSV, Meningococcal, etc.). A single `compute_coverage(schedule, dose_count, last_given) → status` rule drives every status pill — fully unit-testable, no special cases scattered across endpoints. Eight REST endpoints land alongside (`GET/POST /immunizations`, `GET /refs`, `GET /coverage` with an "Other" bucket for non-canonical names, `POST /parse`, `POST /bulk`, `GET /{name}/explainer`). Dashboard payload gains `overdue_count`, `due_soon_count`, `last_given`; history-summary payload gains `up_to_date` and `action_needed` blocks. Nine skill CLI subcommands: `immunizations`, `immunization`, `add-immunization`, `update-immunization`, `delete-immunization`, `vaccine-refs`, `coverage`, `import-immunizations`, `explain-immunization`. All mutations route through deferred ops with `dedup_key_prefix` for mid-replay idempotency. Web UI ships four pages under `/health/immunizations`: main page with color-coded coverage strip and quick-log, `/paste` parser preview, `/detail` per-record edit, `/vaccine` drill-down with explainer card. Risk-based vaccines collapse into an expandable section. **Unified import flow** consolidates the two ingest paths into `/health/immunizations/import` — a tab toggle picks between paste-text (uses the existing parser) and screenshot/PDF upload (new `POST /immunizations/extract`, `pdftotext` for text-native PDFs with vision fallback for images / scanned PDFs, transient processing with no persistence). **Static explainers** replace the original runtime-brain explainer path: hand-reviewed `data/immunization_explainers.json` (19 vaccines, clinical detail covering mechanism, indications, pregnancy caveats, wound intervals, age-specific notes). The vaccine drill-down card is collapsible by default (`<details>/<summary>` with rotating chevron). The orphan `immunization_explainers` SQLite table is dropped — no migration needed since nothing was ever read from it.
- **Health card on the dashboard.** New feature card alongside Feeds, Location, and Money — "Body stats, bloodwork, and biomarker trends" — gated on the health module being enabled for the user.
- **`istota-skill health garmin-sync` direct/delegated routing.** The CLI subcommand now picks an execution mode based on whether the master Fernet key is reachable. Direct mode (`ISTOTA_SECRET_KEY` in env — operator shell that sourced the daemon's EnvironmentFile) runs the engine inline as before. Delegated mode (sandboxed callers, hand-written `command:` CRON rows, dev shells without env file) enqueues a `skill="health"` task with `max_attempts=1` and polls until the scheduler dispatches it via the in-daemon short-circuit, then surfaces the engine's JSON payload. Read-only DB (LLM Bash through bwrap) and timeout cases emit explicit error envelopes pointing the operator at `/garmin/sync` instead of the misleading `token_expired` the previous code path produced. See "Skill proxy execution model and the master-key boundary" in `~/Dust/Notes/Projects/Istota/`.
- **Flight-pattern signal in the location map.** Decoupled "we lost the signal here" gap rendering into two visual styles: `'flight'` (coral dashed great-circle arc, `#e88a8a`, 2.5 px, opacity 0.45) and `'sparse'` (muted-grey straight dash, `#7a8794`, 2 px, opacity 0.4). Two layer IDs (`path-gap-flight`, `path-gap-sparse`) let each style stand on its own. Overland's significant-location-change mode produces large spatial gaps that are still ground travel, not teleports — the sparse style now reads as "we didn't see this leg" instead of falsely claiming a flight happened. Classification uses three rules in `gapKind()`: (1) speed alone via `FLIGHT_SPEED_MS` for typical cruise; (2) **distance-based** — `dist > 300 km` (`FLIGHT_DIST_MIN_M`) AND `speed > 28 m/s` (`FLIGHT_DIST_SPEED_MS`, ~highway pace) — catches short regional flights like Munich → Warsaw where airport dwell averages the implied speed below cruise; (3) **endpoints-at-rest** — `dist > 300 km` AND `speed > 28 m/s` AND both endpoint pings show OS-reported instantaneous speed ≤ 5 m/s, which distinguishes a real flight from a high-speed rail run with signal loss (passenger still in motion at gap boundaries). `endpointInMotion()` treats null speed as at-rest (backward compat for older pings without the field). Switched distance math from equirectangular to haversine, which fixed the ~24,000 km anti-meridian wrap errors and the 17% transcontinental overestimation that were skewing flight thresholds. `excludeDwellPings()` now preserves the first and last ping of each dwell cluster (5+ min, ≤ 50 m spread) so multi-leg flights still have anchor pings for the endpoint-rest check. Mock fixture upgraded to exercise all three rendering styles in one map (Berlin dense walking, Berlin → LAX flight gap, sparse ground sample). One bug along the way: the mock fixture was emitting `recorded_at` instead of `timestamp`, which silently NaN'd every edge duration and made the flight detector inert in dev preview.

### Changed
- **Health page-level loading notices use the shared `.loading` class.** Replaces ten ad-hoc page-scoped `.empty` divs across `/health/{bloodwork,history,immunizations}` and their subpages with the same centered loading style the money module uses. Admin page picks up the same alignment.

### Fixed
- **Command-type scheduled jobs lost `setup_env`-resolved env vars** (ISSUE-097). `scheduler._execute_command_task` was missing the `dispatch_setup_env_hooks` call that `_execute_skill_task` gained in 0.12.0, so `LOCATION_DB_PATH` / `HEALTH_DB_PATH` (and any future `from: "setup_env"` manifest spec) only reached the subprocess when the daemon happened to carry them in ambient env. A daemon restart on 2026-05-18 stranded `gym-check-sync` with `LOCATION_DB_PATH not set`. The fix `env.update(...)`s the hook output unconditionally, so per-user values overwrite any stray systemd-leaked daemon env that would otherwise point at the wrong user's DB.
- **Cron-driven Garmin sync could never succeed from a subprocess** (ISSUE-098). The encrypted-secrets refactor in 0.12.0 moved Garmin OAuth tokens behind Fernet, but the scheduled skill-task path strips `ISTOTA_SECRET_KEY` from the subprocess env by design. The engine read+writes encrypted secrets multiple times per sync (oauth blob, rotated SDK tokens, error flag, last_sync), so a single pre-resolved env injection wouldn't have covered the round trips. The scheduler now short-circuits `skill="health"` + `skill_args[0]=="garmin-sync"` into `_run_garmin_sync_inprocess`, which executes in the daemon thread where the key is in scope — mirroring what the web `/garmin/sync` endpoint already does.
- **Heartbeat `shell-command` checks had the same `setup_env`-hook gap.** A `shell-command` heartbeat invoking `istota-skill location current` / `istota-skill health …` saw `LOCATION_DB_PATH` / `HEALTH_DB_PATH` unset and the skill CLI emitted a `{"status":"error","error":"…"}` envelope to stdout while exiting 0 — which the no-condition branch silently treated as healthy, letting the monitoring check rot. `heartbeat._check_shell_command` now resolves the same `build_skill_env` + `dispatch_setup_env_hooks` env-spec chain `_execute_command_task` uses (sharing a per-task `EnvContext` with CalDAV discovery skipped to avoid a PROPFIND per tick), and parses the JSON error envelope as failure when no explicit condition is configured. Hook resolution exceptions degrade to a WARN log rather than killing the check.

## [0.12.0] - 2026-05-15

### Added
- **Devbox credential proxy.** Per-user host-side asyncio daemon (`src/istota/devbox_proxy.py` + `devbox_proxy_protocol.py`) that lets the devbox container do git over HTTPS and call the GitLab/GitHub REST APIs without ever holding a token. The daemon listens on `/var/run/{namespace}/devbox-cred-<user>.sock`, bind-mounted into each container at `/run/istota-cred.sock`; the proxy clients inside the image (`git-credential-istota`, `gitlab-api`, `github-api`, `gh`, `glab` — `docker/devbox/scripts/*` with shared helper at `docker/devbox/lib/istota_devbox_client.py`) frame single-line JSON requests. The daemon injects `PRIVATE-TOKEN` / `Authorization: token …` headers and `username=x-access-token` git credentials server-side; tokens never appear in container env, filesystem, or memory beyond the brief moment git uses them mid-handshake. Endpoint allowlist enforcement reuses `developer.{gitlab,github}_api_allowlist`; cross-host `git_credential get` (e.g. `bitbucket.org`) returns `no_token` so git fails cleanly. Audit logger `istota.devbox_proxy.audit` emits one key-value journal line per action (`user=, action=, result=, dur_ms=, method=, endpoint=, status=`) with an optional file fan-out via `developer.devbox_proxy_audit_log`. `gh` / `glab` shims cover the curated subset the agent uses (`pr|mr create|view|list|close`, `issue create|view|list`, `repo view`, `auth status`) — anything else exits 2 pointing at the raw `github-api` / `gitlab-api` wrappers. Defaults on when devbox is on; toggle via `[developer] devbox_proxy_enabled` / Ansible's `istota_devbox_proxy_enabled`. Bundled `/etc/gitconfig` wires `[credential] helper = istota` globally so existing `git push` flows work unchanged. Systemd instance template `{namespace}-devbox-proxy@<user>.service` brought up before compose so the bind-mount target exists at container creation; existing devbox-image rebuild trigger was generalized to hash the whole `docker/devbox/{Dockerfile,lib,scripts,etc}` tree.
- **Vendored Monarch Money client.** Replaced the third-party `monarchmoneycommunity` package with a slim aiohttp client at `src/istota/money/_vendor/monarch_client.py`. Talks to `api.monarch.com` using the cookie-based auth model the API now requires (Django CSRF on `/graphql` — session cookies + `X-Csrftoken` + `Origin` + `Referer` on every request). Six distinct exception types map to specific HTTP statuses in the new web route so the UI can render targeted error messages: `MonarchAuthError` (401), `MonarchAPIError` (5xx / malformed), `MonarchMFARequired` (412), `MonarchClientOutdated` (503 — operator bumps `CLIENT_VERSION`), `MonarchCaptchaRequired` (503 — sticky bot-protection, must use cookie-paste), `MonarchCloudflareBlocked` (503 — server IP blocked).
- **`money debug-monarch` subcommand.** Cheap auth health check that calls `me { id email }` and emits a `{"status":"ok","auth_ok":true,"who":{...}}` envelope (or an error envelope on failure). Useful for heartbeat checks and operator diagnosis.
- **Programmatic Monarch login flow** — `POST /api/money/monarch/login` takes email / password / optional MFA TOTP, calls `/auth/login/`, captures the resulting session cookies, and stores them in the encrypted secrets table. Plaintext credentials are never persisted. SvelteKit settings page at `/money/settings` now exposes two collapsible options: Option A (login form) and Option B (paste cookies from browser DevTools). Option A may fail on cloud-hosted deployments because Monarch's CAPTCHA gate is sticky once tripped — the UI surfaces this with a specific message routing the user to Option B.
- **`scripts/probe_monarch_login.py`** — operator-facing live probe of the Monarch `/auth/login/` flow. Useful next time auth changes: takes credentials from env, prints structured output, exits non-zero on failure.
- **SQLite self-healing sweep** — new `db_health` module (`PRAGMA quick_check` + self-healing `REINDEX`) and a daily scheduler tick that walks the framework DB plus every configured user's `feeds`, `health`, `location`, `money` DB. Self-heals stale-index corruption (the failure mode you get when a WAL DB on a FUSE-backed mount sees an ungraceful shutdown or network hiccup mid-write — visible in the reader UI as an entry stuck on `status=unread` in counters while rendering with the `SEEN` overlay). Unrepairable damage is logged at ERROR for operator follow-up. Runs immediately on the first daemon tick after start, then every `db_health_check_interval` (default 24h).
- **Health module.** New per-user module for body-stats time series, bloodwork panels, biomarker trends, and lab-result extraction. Standard on-by-default module alongside `feeds`, `money`, and `location`; per-user opt-out via `disabled_modules`. Per-user SQLite at `{workspace}/health/data/health.db` follows the established feeds/location/money pattern. Surfaces:
  - `src/istota/health/` package — `models`, `workspace`, `_loader`, `_migrate`, `db`, `units`, `routes`, `ocr` modules.
  - FastAPI router at `/istota/api/health` — stats CRUD + series + latest, panels CRUD + upload + extract + source streaming, biomarker trend + summary + canonical refs, profile/display settings, dashboard aggregator. Blood-pressure / resting-HR biomarker rows fan out to `stats` so the unified time series picks them up. Flag computation uses Istota canonical ranges (sex-aware when set); lab-printed ranges are preserved per-row but not the flagging source. Panel delete cascades to biomarkers, derived stats, and the on-disk source file.
  - Skill at `src/istota/skills/health/` — `istota-skill health log|stats|latest|panels|panel|add-panel|add-biomarker|trend|upload|summary|settings|set` plus a `setup_env` hook that injects `HEALTH_DB_PATH`. Writes flow through the deferred-op file (`task_<id>_health_ops.json`) under sandbox; new `scheduler_deferred._process_deferred_health_ops` handler replays them post-task.
  - Web UI at `web/src/routes/health/{dashboard,stats,bloodwork,bloodwork/[id],bloodwork/upload,settings}` — dashboard cards (BMI derived from latest weight + settings height), Chart.js stat trend with metric selector + range picker + manual entry + history table, panels list, panel detail with split biomarker table / source preview / inline edit / draft-confirm flow, drag-and-drop upload page with OCR review-and-edit pipeline, profile + display-unit settings. New `User.features.health` flag drives nav-link visibility.
  - OCR pipeline (`istota.health.ocr`): PDF text extraction via `pdftotext` / `pypdf` with image-OCR fallback via `pdftoppm` + Tesseract; image input goes straight to Tesseract. The extracted text plus the canonical `biomarker_refs` list is passed to the active brain (`general` role alias) which returns structured JSON. Sanity-checks every biomarker against the widest canonical range and surfaces a warning when a value is >10× outside the bounds (likely OCR error). All stages degrade gracefully when an optional dependency is missing — the review UI lets the user fill in or correct rows by hand either way.
  - Bundled `biomarker_refs.json` seed (CBC, CMP, Liver, Lipid, Thyroid, Iron, Vitamins, Inflammation, Hormones, Diabetes, other) with sex-specific ranges where clinically meaningful and alias maps for the most common spellings.
  - 52 new tests across `test_health_db.py`, `test_health_routes.py`, `test_health_skill.py`, `test_health_ocr.py`.

### Changed
- **Monarch credentials are now cookie-only.** `MonarchCredentials` reduced to `(session_id, csrftoken)`. The settings UI exposes only those two fields; the legacy `email` / `password` / `session_token` fields have been removed everywhere — schema (`secret_schema.py`), env loader (`MONARCH_SESSION_ID` / `MONARCH_CSRFTOKEN` are the only env vars now), config-store load/save paths, money skill manifest. Cookies last weeks-to-months on a trusted-device login. Existing orphan rows in the secrets table from prior installs are invisible in the UI and ignored by the loader; remove them via `istota secret remove` if you want a clean DB.

### Removed
- **`monarchmoneycommunity` dependency.** Dropped from `pyproject.toml` along with its `gql` / `graphql-core` / `oathtool` transitive deps. `aiohttp>=3.10` is now a declared direct dep of the `money` extra (was previously transitive).

### Fixed
- **Monarch sync silently broke when the API switched to Django CSRF auth.** The third-party client only sent `Authorization: Token <...>`, which the API now rejects with `403 "CSRF Failed: Referer checking failed - no Referer."`. Sync resumes after pasting `session_id` + `csrftoken` cookies via `/money/settings` → Option B (or via `istota secret ensure -u <user> -s monarch -k session_id` / `... -k csrftoken`).
- **Feed entry stuck "unread" after being seen** — a feed entry could keep contributing to the unread badge in the reader UI while individually rendering with the `SEEN` overlay. Root cause was stale-index corruption on the per-user `feeds.db` (FUSE-mounted WAL DB after an ungraceful event): the `(feed_id, status)` covering index held a pointer with no matching row, so count queries used the index and returned 1, while the row-fetching card query saw the true `status='read'`. The new daily SQLite self-healing sweep detects and repairs this with `REINDEX`.
- **Devbox container had no outbound connectivity.** DNS resolved (via Docker's embedded resolver on the bridge gateway) but every outbound TCP, UDP, and ICMP packet was dropped. Root cause was `iptables-persistent` fighting Docker's runtime iptables management: the Ansible role was running `netfilter-persistent save` after `docker compose up`, which snapshotted the whole table including Docker's then-current POSTROUTING MASQUERADE + DOCKER-FORWARD ACCEPT rules. On the next reboot iptables-persistent restored that snapshot, but dockerd's network-create rules for the new devbox bridge never re-landed in the saved file, so the bridge had no NAT and no FORWARD path. Reworked the role to stop using iptables-persistent for this. The DOCKER-USER DROP rules (metadata + RFC1918 blocks) are now reapplied at boot by a small `istota-devbox-iptables.service` oneshot that runs `After=docker.service` with idempotent `iptables -C || -A` checks. Docker is left to own POSTROUTING / DOCKER-FORWARD on its own bridges, which it does correctly when nothing stomps on it. **Manual recovery on existing hosts:** after deploying this change, run `sudo systemctl restart docker` once on the host to make dockerd reprogram the missing bridge rules. Pre-existing `/etc/iptables/rules.v4` snapshots stay on disk but are ignored by our role; an operator who wants a fully clean boot path can `sudo systemctl disable --now netfilter-persistent.service`.
- **Devbox image missing `/etc/services`.** `whois` and other tools that resolve port names by symbolic lookup failed with "service not found" because the slim Debian base ships without `/etc/services`. Added `netbase` to the apt install layer.
- **Devbox image not rebuilt on Dockerfile-only changes.** The Ansible role tracked the compose template via `notify`, but `docker/devbox/Dockerfile` (which is pulled to the host via git, not templated) was invisible to the role's change detection. A Dockerfile edit would ship to the host but the running container would keep the old image until something else triggered the rebuild handler. Added `stat` + checksum-marker tasks that hash the Dockerfile on the host and notify `restart istota-devbox` when the digest changes. Runs under `istota_update_only=true` like the rest of the DEVBOX section. One-time cost on existing hosts: the first run after this change rebuilds the image because the marker file doesn't exist yet.

## [0.11.1] - 2026-05-09

### Added
- **KV set ops** — `kv set-contains`, `set-size`, `set-members` (read), `set-add`, `set-remove` (deferred-write) for membership-tracking patterns. Operate on a JSON-array value at `<ns>/<key>` with plain-string members. Bootstraps `[]` if missing. `set-add` / `set-remove` accept multiple members per call. Deferred ops carry only the member list; the scheduler re-reads the current value at apply time so concurrent set-adds across tasks compose correctly. Existing `get` / `set` / `list` / `delete` / `namespaces` semantics unchanged. Avoids round-tripping large blobs (e.g. ~44 KB seen-IDs arrays) through the skill proxy when the caller just needs a membership check.

### Changed
- **Per-user `location.db`** — GPS data (`location_pings`, `places`, `visits`, `location_state`, `dismissed_clusters`) moved out of framework `istota.db` into per-user `{workspace}/location/data/location.db` files. New module package at `src/istota/location/` with `resolve_for_user(user_id, config)` mirroring the `feeds` / `money` pattern. The two global Nominatim caches (`geocode_cache`, `reverse_geocode_cache`) intentionally stay in framework `istota.db` to preserve cross-user dedup; skill subcommands and web routes that need them open a second connection via `location.db.with_geocode_conn(...)`. Skill CLI reads `LOCATION_DB_PATH` (set by a `setup_env` hook) instead of `ISTOTA_DB_PATH` for per-user data.
- **One-shot Ansible migration** — new idempotent block stops services, runs `python -m istota.location._migrate` to copy each enabled user's rows into their per-user file (FK-orphan NULL pass + `pragma foreign_key_check`/`integrity_check` validation + sentinel-gated re-run safety), drops the framework tables, restarts services. Pre-checks halt the deploy on orphan rows for users not enabled for the location module unless `-e include_orphans=true` is set.

### Removed
- Framework `db.py` location helpers and dataclasses (`Place`, `Visit`, `LocationState`, `LocationPing`, plus `insert_location_ping`, `get_places`, `add_place`, `update_place`, `delete_place`, `reconcile_visits`, `get_location_state`, `set_location_state`, `list_dismissed_clusters`, and the rest of the location section). Replaced by per-user equivalents in `istota.location.db`.
- Five `CREATE TABLE` declarations from `schema.sql` for the now-per-user tables. Fresh `init_db` produces only framework tables.

### Fixed
- **Calendar owns dates for scheduled events (ISSUE-089).** Dropped `has_appointment` from the sleep-cycle predicate vocabulary and added explicit "Calendar-managed events" guidance to both `src/istota/memory/sleep_cycle.py` and `src/istota/skills/memory/skill.md`: do not write KG facts that carry the date of a calendar-managed event. KG facts may capture date-less metadata (procedure type, fasting requirements, location details) using predicates like `has_scheduled_procedure` or `has_medical_workup`; `valid_from` / `--from` on those facts = when the fact was learned, never the event date. Eliminates the dual-write pattern that caused the calendar and KG to confidently agree on the same wrong date with no tiebreaker.
- **WAL-safe backups for per-user module DBs (ISSUE-088).** `deploy/ansible/templates/istota-backup.sh.j2` now WAL-backs up `feeds.db`, `money.db`, `location.db` (and any future module DB listed in `MODULE_NAMES`) alongside the framework DB. Previously these were only covered by the nightly rclone files sync — a plain copy that can capture a torn page mid-write. Refactored `backup_db` into a reusable `_backup_one_db` helper plus a `_discover_module_dbs` walker over `${MOUNT_PATH}/Users` that wildcards the bot-dir name. Backups land as `${user}-${module}-${timestamp}.db.gz` with isolated retention windows.
- **UTC anchor + elapsed-time rule in prompt (ISSUE-091).** Added `Current UTC: <ISO 8601 Z>` to the prompt header alongside the existing user-local time / today / timezone lines, and added a rule (admin: #9, non-admin: #8) telling the model to normalize timestamps to ISO 8601 UTC before subtracting — never subtract clock-face hours/minutes by hand. Removes the failure mode where "X ago" calculations across UTC midnight (or end-of-month, or DST) reported the wrong elapsed time.

## [0.11.0] - 2026-05-08

### Added
- **`/spec` skill** — codifies a spec-driven development workflow against the user's `notes_folder`. Specs default to `{notes_folder}/Specs/{Drafts,Active,Done}/`, branch into a project subfolder only when the user names one explicitly. Doc-only — filesystem ops piggyback on the always-include `files` skill. Triggers: `spec`, `draft spec`, `design doc`, etc. The skill frames specs as detailed implementation documents fit for blind handoff to a coding agent (named files/interfaces, edge cases, schema/config changes, test strategy, ordered stages, recorded decisions, explicit open questions); thin drafts shouldn't leave `Drafts/`.
- **`!model <alias> <prompt>` Talk prefix** — per-task model override. Aliases: `default`, role aliases (`fast`, `general`, `smart`, plus operator-defined custom roles), provider aliases (`opus`, `opus-high`, `opus-xhigh`, `opus-max`, `opus-46`, `opus-46-high`, `sonnet`, `sonnet-high`, `haiku`). Stored canonical on the task row so the DB stays version-pinned. Companion `!models` command lists the resolved alias table; `!help` mentions the prefix. Empty-remainder + attachments path is a valid intent ("read this image with opus") and falls through to the default attachment-processing prompt.
- **`[models.roles]` operator role aliases** — provider-agnostic role names like `smart` / `general` / `fast` (and operator-defined custom roles like `deep` / `cheap`) rebindable via TOML. Defaults: `fast`→Haiku, `general`→Sonnet, `smart`→Opus. A deployment that wants to stay on Opus 4.6 in prod can write `[models.roles] smart = "opus-46-high"` and every call site that reads `smart` follows. `Brain.validate_role_override` warns on typos and provider-alias collisions at config-load time. Ansible: new `istota_models_roles: {}` default with template support.
- **`Brain` Protocol gains four resolver methods** — `resolve_alias`, `resolve_model_name`, `list_aliases`, `validate_role_override`. Each brain owns its own model namespace (canonical IDs, provider aliases, default role targets); consumers reach them through `make_brain(config.brain).resolve_*` only. A future OpenRouter / Anthropic-direct brain ships its own naming scheme without any caller changes.
- **First-class `OPUS_46` constant** alongside `OPUS` / `SONNET` / `HAIKU` — kept available for prod pinning. Convention: bare aliases like `opus` always resolve to a versioned canonical ID (`OPUS = "claude-opus-4-7"`); older versions get first-class constants only when there's a concrete reason to pin.

### Changed
- **Model identity is now brain-scoped.** Anthropic canonical IDs and the provider alias table moved into `brain/claude_code.py`. Operator role overrides (provider-agnostic) live in new `brain/_roles.py`. Old `brain/_models.py` deleted. All in-codebase model references go through brain resolvers — `executor.py` per-task `BrainRequest`, semantic routing, context selection, sleep cycle, cron task creation. No module outside the brain hardcodes a `claude-*` ID anymore.
- **Config defaults switched to role aliases.** `selection_model` default `"haiku"` → `"fast"`; `extraction_model` / `curation_model` `"sonnet"` → `"general"`; `semantic_routing_model` `"haiku"` → `"fast"`. Operator-overridable via `[models.roles]`. Existing `"haiku"` / `"sonnet"` strings in inventory files keep working unchanged (they're valid provider aliases).
- **Subtasks inherit parent `model` / `effort`.** Deferred subtask JSON entries can override per child via `model` / `effort` keys. Previously, a parent task started with `!model opus-46-high` would silently spawn children with the default config model.
- **Cron jobs pre-resolve aliases at task-creation time** so the DB stores canonical IDs (matches the talk-poller's `!model` behavior). `cron_loader._validate_model` now consults `brain.resolve_alias` and `get_role_overrides` before warning, so role-aliased CRON.md rows no longer log-spam.
- **Documentation surface clarified.** `deploy/ansible/defaults/main.yml` and `config/config.example.toml` get explicit "three knobs that interact" headers explaining `model` / `effort` / `[models.roles]` resolution chain (`!model` per-task → `model`+`effort` → brain default), the alias-effort footgun (`model = "opus-46-high"` resolves model only — `effort` is read independently in TOML/Ansible config), and which role aliases internal code uses (`fast` for triage / classification, `general` for sleep cycle, `smart` purely user-facing).

### Fixed
- **`executor.py:2434` resolved aliases for every task source.** The per-task `BrainRequest` previously shipped `task.model || config.model` to `claude --model` raw. Talk-poller tasks happened to work because the `!model` parser pre-resolved, but cron jobs, briefings, email tasks, TASKS.md tasks, and the operator `istota_model = "smart"` default would have hit `claude --model smart` and failed. Now wraps with `brain.resolve_model_name(...)` — alias surface contract documented in Ansible / config defaults now matches runtime behavior.
- **`scheduler.py:2887` cron jobs pre-resolve `job.model`** before `db.create_task` so the DB stays canonical. Combined with the `_validate_model` fix, a CRON.md row with `model = "smart"` (or `"general"` / `"opus-high"`) now works end-to-end without warnings.
- **Role overrides no longer silently shadow provider aliases.** `[models.roles] opus = "haiku"` (a single typo away from the intended `smart = "haiku"`) used to silently make every `!model opus` resolve to Haiku without warning. The new `Brain.validate_role_override` surfaces these collisions at config-load time, plus warns when an override target is neither a known alias nor a canonical `claude-*` ID (so `smart = "garbage-not-a-real-model"` fails loudly at startup instead of silently at task time).
- **`_role_overrides` mutation is now atomic** (dict rebind instead of `clear()` + `update()`) so a hypothetical concurrent reader during a SIGHUP/reload always sees a coherent snapshot. Today `set_role_overrides` only fires single-threaded at config-load, but the safety is free.
- **`_compose_full_result` redesign.** Replaced the interleaved CM-aware / terse-recovery branches in `executor.py` with a clean two-mechanism design. Mechanism A (CM-aware, ISSUE-026) walks segments delimited by `cm_boundary` and returns the last segment whose joined text crosses `_CM_SEGMENT_MIN_CHARS=200`; always runs when CM events exist, including for automated tasks. Mechanism B (terse-recovery, ISSUE-025) is gated on `_is_automated_task(task)` (`source_type ∈ {scheduled, briefing}` plus `heartbeat_silent` / `scheduled_job_id` structural fallbacks) AND `_is_terse(result_text)` (≤150 chars OR matches a short reference regex like `see above` / `done` / `ok`); when triggered it walks segments delimited by both `tool` and `cm_boundary` and returns the last region ≥`_TRAILING_REGION_MIN_CHARS=500`. Both mechanisms share one `_last_substantial_region` helper. Crucially: replace, never prepend — the old prepend-and-glue path was the source of the 2026-05-08 cron incident where a 5KB skill-doc preamble got jammed in front of a 900-char real summary on a `scheduled` task. The function shrunk from ~92 lines to ~75 (with the helper at ~20). Every override now emits an INFO log line for calibration. See `Notes/Projects/Istota/Compose full result redesign spec.md`.
- **Unified credential resolution refactor (Phases 1–4).** Skill manifests are now the single source of truth for credential and connection env vars. Phase 1 extended `EnvSpec` with `sensitive` / `fallback_var` / `gate_user_has_resource` / `gate_has_discovered_calendars` / `from: "setup_env"` / `from: "secret"`, added `tasks.skill` / `tasks.skill_args` columns, introduced `_execute_skill_task` so cron's auto-seeded `_module.<name>.run_scheduled` rows run as `python -m istota.skills.<name>` with `build_skill_env` resolving env on the trusted side (no `ISTOTA_SECRET_KEY` propagation, no proxy split), and dropped the `_STRIPPED_ENV_PRESERVE` whitelist so `build_stripped_env` strips any var matching `SECRET` unconditionally. Phase 2 migrated every previously-hardcoded credential (`NC_*`, `CALDAV_*`, `IMAP_*`, `SMTP_*`, `KARAKEEP_*`, `GITLAB_TOKEN`, `GITHUB_TOKEN`, `MONARCH_SESSION_TOKEN`, `GOOGLE_WORKSPACE_CLI_TOKEN`, `NTFY_*`, `TUMBLR_API_KEY`) to its skill's manifest with `sensitive: true` where applicable; the developer skill's 230-line shell-script generator (git credential helpers, gitlab-/github-api wrappers) extracted into `src/istota/skills/developer/__init__.py::setup_env(ctx)` and `dispatch_setup_env_hooks` now iterates the full index so the hook fires regardless of selection. Phase 3 deleted `_PROXY_CREDENTIAL_VARS`, `_CREDENTIAL_SKILL_MAP`, `_allowed_credentials_for_skills`, `_authorized_skills_from_credentials`, `_build_skill_credential_map` and replaced them with four pure helpers — `derive_credential_set`, `derive_authorized_skills` (calls `_resolve_env_spec` with `fallbacks_disabled=True` so an instance-wide `EnvironmentFile` fallback can never trigger per-user auto-authorization), `derive_skill_credential_map`, `derive_lookup_allowlist` — all derived from the loaded skill_index every task. `_PROXY_LOOKUP_BLOCKED` retained as defense-in-depth. Phase 4 promotes operator CRON.md `command = "istota-skill <name> [args]"` rows to skill-tasks at sync time via `_parse_skill_command` / `_resolve_job_dispatch` / `fj_is_disallowed_command`; anything with shell metacharacters, env-var prefix (`MONEY_USER=foo istota-skill …`), pipes, redirects, or non-trivial quoting stays a command-task and keeps the admin gate. `migrate_db_jobs_to_file` round-trips skill-task rows back to a single `command:` line so CRON.md remains operator-editable.
- **Trusted subprocess paths now resolve env from skill manifests too.** `_execute_skill_task` and `_execute_command_task` no longer carry hand-maintained credential lists. Both build the subprocess env via `build_skill_env(list(skill_index), …)` over an `EnvContext` populated by a new shared helper `discover_calendars_for_task(task, config)` (extracted from `executor.execute_task`). Three behavior consequences: (1) skill-tasks now receive every co-declared env var that resolves under their context, not just the requested skill's — closes a latent bug where a future CalDAV-touching skill-job promoted via CRON.md would have silently lost credentials; (2) `_execute_command_task` no longer hardcodes `NC_*` / `CALDAV_*` — the manifest is the single source of truth for those names too; (3) `gate_has_discovered_calendars` now fires on the command-task path, so `CALDAV_*` is omitted when discovery returns empty (matches the LLM path).
- **`_execute_command_task` confirmed master-key-clean.** The path was already master-key-free after Phase 1.4 (`build_stripped_env` strips any var matching `SECRET`); a regression test now pins the property end-to-end so a future commit can't quietly re-introduce the propagation.

### Added
- **`tests/test_credential_derivation.py`.** 373 cases pinning the four derivation helpers' invariants — sensitive-only collection, `fallback_var` doesn't auto-authorize per-user, perf budget (<50ms cold), `build_skill_env` conflict semantics. Replaces a scatter of indirect assertions previously spread across the proxy / executor / security suites.

### Removed
- **Parallel credential constants in `executor.py`.** `_PROXY_CREDENTIAL_VARS`, `_CREDENTIAL_SKILL_MAP`, `_allowed_credentials_for_skills`, `_authorized_skills_from_credentials`, `_build_skill_credential_map`. Replaced by manifest-derived helpers — every new credential or skill now needs only a `skill.md` edit.
- **`_STRIPPED_ENV_PRESERVE` whitelist.** Module-skill subprocesses no longer need `ISTOTA_SECRET_KEY` propagated to fetch their own secrets; they're either skill-tasks (env pre-resolved on the trusted side) or LLM-path subprocesses behind the proxy (which never had the master key).

## [0.10.0] - 2026-05-06

### Added
- **Modules / connected services UI split (Phase 2 + 3 of the modules refactor).** New `web/src/lib/components/settings/ServiceCard.svelte` wraps the "service name + status pill + updated meta + used-by line + secret fields / OAuth button" pattern; replaces the inline secret blocks each settings page used to maintain. New endpoints: `GET /istota/api/settings/modules` returns the module registry plus per-user `disabled_modules`; `GET /istota/api/settings/module-services/{module}` returns the service cards belonging to one module's settings page (currently `feeds → tumblr_api_key`, `money → monarch.{email,password,session_token}`, `location → overland.ingest_token`); both signal `module_enabled: bool` so per-module pages can render a "module disabled — enable in /settings → Preferences" banner instead of the configuration UI. `GET /istota/api/settings/services` now returns only Connected services (`karakeep`, `google_workspace`); module-owned services no longer leak through. New "Disabled modules" multiselect on `/settings → Preferences`; values are validated server-side against `MODULE_NAMES`. New `/location/settings` page with the Overland ingest-token SecretField, a webhook-URL hint (`https://<host>/webhooks/location?token=<token>` — the token never leaves the server, so the URL shown uses a placeholder), and read-only place-detection knobs. Cog icon added to `/location/+layout.svelte` mirroring the feeds and money toggle pattern.
- **Karakeep base URL is now an encrypted secret.** Both `KARAKEEP_BASE_URL` and `KARAKEEP_API_KEY` resolve from the secrets table at task time. The legacy `[[resources]] type = "karakeep" base_url = "…"` form still works on first startup — the value is migrated into the secrets table and the resource row is cleaned up by `db.cleanup_obsolete_resources` (already in the modules-refactor Phase 1 migration).
- **`from: "secret"` env-spec source for skill CLI proxies.** Skills can now declare per-user encrypted-secret env vars directly in their `skill.md` frontmatter (`{"var":"X","from":"secret","service":"Y","key":"Z"}`); the bookmarks skill is the first consumer.

- **Encrypted secrets store (Docker onboarding Phase 5).** New `secrets` table holds tier-2 credentials (Monarch, Karakeep, Tumblr API key) per `(user, service, key)`, encrypted at rest with a Fernet key derived from `$ISTOTA_SECRET_KEY`. Docker entrypoint auto-generates the key on first boot and persists it to `/data/.secret_key` on the istota_data volume; operators can pin a value via `.env` (Ansible: `istota_secret_key`). On startup, the scheduler walks user resources and copies tier-2 fields from TOML extras into the secrets table — idempotent, never overwrites a value already present (so web-UI edits beat stale TOML defaults). New `secrets_store.resolve_secret()` helper does three-tier resolution: secrets table → resource extras → env var. Karakeep credentials in the executor and Monarch credentials in the money loader now consult the table first. New `/istota/api/settings/services` (read-only cards) and `PUT|DELETE /istota/api/settings/secrets/{service}/{key}` (write-only mutations, CSRF-gated) power a new SvelteKit `/istota/settings` page with per-service forms and "configured" badges. Plaintext values are never returned to the browser. Adds `cryptography>=42.0` as a core dependency.
- **Admin dashboard at `/istota/admin` (Phase 1, read-only).** New page in the SvelteKit web UI with a system banner, users table, task-activity KPIs, per-module health cards (feeds / money / location), scheduler jobs with expandable error rows, and storage stats. Auto-refreshes every 60s. Backed by a single new `GET /istota/api/admin/stats` endpoint that aggregates everything in one read-only payload; sub-aggregators are best-effort (a broken section becomes `payload.error` instead of failing the request). All response timestamps are normalized to canonical `YYYY-MM-DDTHH:MM:SSZ` UTC. Admin nav link is gated on a new `features.admin` flag returned by `/api/me`. Admin access uses the existing `/etc/istota/admins` allowlist via a new web-only `_user_is_web_admin()` helper that fails closed on missing/empty allowlist (distinct from `Config.is_admin`, which retains its back-compat "empty = all admin" rule for sandbox/skill checks). The feeds module emits a degraded card with `users_resolved: 0`, `status: "unreachable"` when feeds are configured but the workspace can't be resolved (e.g. docker-compose deploys without `nextcloud_mount_path`), instead of silently disappearing from the response.
- **Runtime memory CLI (`istota-skill memory`).** New `append`, `add-heading`, `remove`, `show`, `headings` subcommands write to `USER.md` through the same op-based engine the nightly curator uses — same heading routing, dedup, opaque subsections, audit log. Optional `--channel TOKEN` flag retargets to `CHANNEL.md` (cross-channel writes are refused). Replaces `echo "- ..." >> USER.md` as the durable-memory write path.
- **Three-branch classification gate in the memory skill body.** Temporal events ("ordered X on …", "decided to …", "returned …") and stable factual claims (allergies, family, biography, languages, residence) route to `istota-skill memory_search add-fact`; behavioral instructions ("default to short replies", "always sign as …") route to `istota-skill memory append`. Worked CLI examples and explicit "don't echo >>" callout in the skill body.
- **Phase-A lint pass on USER.md.** Nightly curator scans for date-stamped temporal-verb bullets ("ordered X on YYYY-MM-DD" or `YYYY-MM-DD: …` lead-date form) and logs them as candidates (`entry_kind="lint_candidate"`) in `USER.md.audit.jsonl` without migrating. Filtered by behavior heading allowlist and KG dedup pre-check; capped at N=3 per run. A future Phase B will gate the actual migration on a config flag once the candidate quality is verified in the field.
- **Bypass-write detection.** New `USER.md.last_seen.json` sidecar stores size + sha256. If USER.md changes without an audit entry recording it, the next nightly run logs a WARNING and writes a synthetic `source="legacy"` audit entry. Catches bypass writes from `echo >>`, manual edits, or third-party tools.
- **`apply_ops_with_db()` (curation engine).** Sibling of `apply_ops()` that handles a new `add_fact` op alongside the existing file-only ops, against a writable sqlite3 connection. The runtime memory_search CLI uses this when running outside the sandbox; sandboxed runs go through the deferred-DB pattern.
- **Sandbox-aware fact writes.** `istota-skill memory_search add-fact|invalidate|delete-fact` now defers to `task_<id>_kg_ops.json` when `ISTOTA_DEFERRED_DIR` is set. The scheduler's new `_process_deferred_kg_ops` applies them post-task using `task.user_id` (always wins over any user_id in the file).
- **`source` and `entry_kind` fields on the curation audit log.** `source ∈ {nightly, runtime, cli, legacy}`; `entry_kind ∈ {batch, lint_candidate, aborted, legacy_detected}`. Existing entries lacking these fields read back cleanly. Per-run summary log line: `memory_curation_run user=… ops_applied=… ops_rejected=… lint_candidates=… legacy_detected=… agents_header_added=…`.
- **Curator concurrency safety.** Per-file flock around the parse-modify-write window in both the runtime CLI and the nightly curator. The curator additionally re-reads USER.md after the LLM returns and aborts (with `entry_kind="aborted"`) if sha256 changed during the brain's wall time, so a runtime CLI write that lands during the nightly call is never clobbered.
- **Agents-header HTML comment on USER.md.** New seed files include a `<!-- agents: ... -->` preamble describing what belongs in USER.md vs the knowledge graph; existing USER.md files get the comment one-shot prepended on the next nightly curation run (idempotent substring match).
- **Five new suggested predicates** in the extraction prompt: `acquired`, `disposed_of`, `grew_up_in`, `born_in`. (`allergic_to`, `speaks` were already present.)

### Changed
- **`/settings/services` now returns Connected services only.** Module-owned credentials (Monarch, Tumblr API key, Overland ingest token) live on their per-module settings page and are reachable via `GET /settings/module-services/{module}`. The frontend `/settings` page replaces its inline secret loop with `ServiceCard` instances and drops the "module-specific credentials live on…" cross-link hint — the per-module pages are the authoritative location.
- **`_user_has_feeds` / `_user_has_money` / `_user_has_location` now go through `Config.is_module_enabled`.** No more "user has a `[[resources]] type = feeds`" check — a configured user is presumed to have the module unless they've added it to `disabled_modules`. Same gate flips for the admin dashboard's per-module health cards.
- **`_service_status` simplified.** Status is now purely a function of which keys are configured; the legacy "unavailable when no resource declaration" branch is gone (modules + connected services own their own gating now). All-optional services (e.g. Tumblr API key) report `configured` once any key is set.
- **Knowledge-graph fuzzy dedup is now scoped to identical predicates and compares object tokens only.** Previously the Jaccard signature `<predicate> <object>` was compared as a single string, so opposing-verb facts about the same object (e.g. `acquired pilot prera fountain pen` vs `disposed_of pilot prera fountain pen`) shared 4 tokens out of 6, scored 0.67 ≥ 0.6, and the second insert was silently dropped. Now: facts with different predicates never fuzzy-collide; same-predicate facts dedup on object tokens (substring fast-path + Jaccard ≥ 0.6).
- **`memory_search` skill body trimmed to read-only knowledge-graph operations.** `add-fact`, `invalidate`, and `delete-fact` documentation moved to the `memory` skill, where the classification gate decides where a piece of information goes. The `memory_search` skill now points writers there.

### Fixed
- **Runtime memory writes can no longer land under the wrong heading.** The previous skill recipe was `echo "- … (noted $(date +%Y-%m-%d))" >> USER.md`, which appends to EOF — i.e. under whatever `## ` heading is last in the file. Real-world failure: temporal facts about a fountain-pen purchase / decision / return ended up under "Emailing on stefan's behalf" because that section happened to be last. The runtime CLI requires `--heading` and rejects unknown ones with the available-heading list, so the model self-corrects instead of dumping under the wrong section.

### Removed
- **Miniflux backend, code-side cleanup (Phase 5 of the native RSS migration).** Production was already running native (Flux VM decommissioned 2026-05-04, Ansible defaults flipped 2026-05-02); this removes the dual-backend support that made the cutover reversible. Gone: `src/istota/feeds/_miniflux.py` (HTTP client) and `_native_briefing.py` (the dispatch wrapper had no live consumers); `FeedsConfig` and the `[feeds] backend` flag; `miniflux_proxy_router` plus the `_get_miniflux_creds` / `_extract_images` / `_strip_image_from_content` / `_map_entry` helpers in `web_app.py`; the Miniflux env-var injection (`MINIFLUX_BASE_URL`, `MINIFLUX_API_KEY`) and the per-user network-allowlist code in `executor.py`; `MINIFLUX_API_KEY` from `_PROXY_CREDENTIAL_VARS` and `_CREDENTIAL_SKILL_MAP`; the `miniflux` resource type from `_CREDENTIAL_RESOURCE_TYPES` and the Ansible user template; the `[feeds] backend = "native"` writer in the docker entrypoint. CSRF protection on the feeds router is preserved via a new overridable `verify_origin` dependency in `feeds/routes.py` (mirrors `require_auth`); `web_app.py` installs `_verify_origin` for it via `app.dependency_overrides`. `tests/test_feeds.py` and `tests/test_feeds_config.py` deleted (covered the removed dispatch); `test_web_app.py` proxy + image-extraction tests dropped, fixtures rewired to use the `feeds` resource type.

## [0.9.0] - 2026-05-03

### Added
- **Navigable lightbox in the feeds reader.** Clicking any image in a multi-image post now opens a lightbox that navigates the entry's full image set with prev/next buttons, ArrowLeft / ArrowRight keys (wrap-around), and a bottom "X / N" counter. Clicking the `+N` overflow tile on a gallery with 5+ images opens the lightbox at that position so the previously-hidden images are reachable.
- **Starring + bulk mark-as-read in the feeds reader (Miniflux parity, Tranche A).** Per-entry `starred` flag independent of read status, surfaced via a star button on every card (hover-revealed, pinned-visible when starred). New "Starred" sidebar entry (alongside "All") filters the reader to starred entries; star survives `read` / `unread` / `removed` transitions. Toolbar "mark as read" button (`CheckCheck` icon) bulk-marks the current scope (selected feed if one is picked, otherwise everything) behind a confirm modal. Keyboard shortcuts: `f` toggles star on the focused/hovered card, `Shift-A` marks every visible entry as read using the new `before_id` cap so concurrent infinite-scroll loads are not clobbered. Backend: schema v2 migration adds `starred` / `starred_at` columns + a partial index, with the first real `_MIGRATIONS` table since the module was created. New `POST /feeds/mark-as-read` route (`scope=all|feed|category` + optional `before_id`); existing entry PUT routes accept `status` and/or `starred` additively (back-compat with older clients). New CLI subcommands `feeds star --id N [--unstar] [--ids 1,2,3]`, `feeds starred`, and `feeds mark-read --all|--feed N|--category SLUG|--category-id N [--before-id N]`. Wire format gains `starred` / `starred_at` on every entry response.
- **Native RSS / Atom / Tumblr / Are.na feed manager (`istota.feeds`).** Replaces the previous Miniflux + PostgreSQL + bridger setup with an in-process pipeline: `feedparser` for RSS/Atom, vendored Tumblr API v2 and Are.na API providers, per-user SQLite at `{workspace}/feeds/data/feeds.db`, FEEDS.toml as the source of truth for subscriptions and categories, OPML import/export with automatic rewriting of bridger URLs (`localhost:8900/{provider}/{id}/feed.xml`) to bare `tumblr:` / `arena:` form. Conditional GET (ETag, Last-Modified), per-feed adaptive polling with exponential backoff (24h cap), HTML sanitisation via `bleach`, and a scheduler-managed `_module.feeds.run_scheduled` cron job (`*/15 * * * *`) seeded automatically when a user has a `[[resources]] type = "feeds"` entry. New `feeds` install extra (`feedparser`, `bleach`, `click`) and `istota-skill feeds` CLI with subcommands `list`, `categories`, `entries`, `add`, `remove`, `refresh`, `poll`, `run-scheduled`, `import-opml`, `export-opml`.
- **Backend selector at `/istota/api/feeds`.** New `[feeds] backend = "miniflux" | "native"` config flag picks between the legacy Miniflux proxy and the new native module at module-import time. Both surfaces use the same JSON shape, so the SvelteKit reader is backend-agnostic. Unknown backend values raise at config load.
- **Feeds settings page** at `/istota/feeds/settings`, opened via a new sprocket icon between the grid/list view chips and the Sources toggle in the feeds layout. Diagnostics card (subscriptions / entries / unread / errors / last poll), default poll interval input, categories table with rename + reorder + delete, subscriptions table with type pill, title, URL, category, interval, last fetch + last error column, edit + delete actions. OPML import (with bridger-URL rewrite reporting) and download. Edits buffer locally and apply via a "Save changes" button that PUTs the whole FEEDS.toml back. Mock backend (`VITE_MOCK_API=1`) supports the new endpoints for offline UI dev.

### Changed
- **`feeds` skill is now native and CLI-driven.** Rewritten from a Miniflux HTTP API wrapper into a thin `CliRunner` facade over `istota.feeds.cli`, mirroring the money skill pattern. Skill metadata moved from `resource_types: [miniflux]` + Miniflux env spec to `resource_types: [feeds]` + `FEEDS_USER`. The Tumblr API key is sourced from `extra.tumblr_api_key` on the resource (with `TUMBLR_API_KEY` as an env-var fallback for the migration window).
- **Briefing entry fetching dispatches on the feeds backend flag.** `fetch_briefing_entries()` reads from the workspace SQLite under `backend = "native"` and falls back to the legacy Miniflux client otherwise. The legacy `fetch_miniflux_entries` shape is preserved.
- **`src/istota/feeds.py` is now a package.** The legacy Miniflux briefing client lives at `src/istota/feeds/_miniflux.py`; previous imports continue to work via re-exports until the production cutover retires the proxy path entirely.

### Fixed
- **Heartbeat shell-command checks now propagate `ISTOTA_CONFIG_PATH` (ISSUE-067).** A heartbeat invoking `istota-skill feeds …` previously fell back to a default `Config()` with empty users, exited 0 with a JSON error envelope, and the heartbeat looked healthy. `_check_shell_command` now injects `ISTOTA_CONFIG_PATH`, `ISTOTA_DB_PATH`, and `ISTOTA_USER_ID` — the same env subset the scheduler propagates.
- **Feeds `_poll_due` rolls per-source errors up to the outer envelope (ISSUE-068).** Any errors → `status="partial_error"` (visible in logs, not a hard failure); every polled feed errored → `status="error"` (triggers scheduler failure detection + non-zero exit).
- **Money `run-scheduled` rolls Monarch sub-errors up to the outer envelope (ISSUE-069).** A failed `_run_monarch_sync` was nested under `out["monarch"]` while the outer envelope stayed `"ok"`, hiding broken Monarch credentials from the scheduler's JSON-error detector. Now: top-level monarch error → outer `status="error"`; per-profile failures → outer `status="partial_error"` with a `monarch_errors` summary.
- **Monarch sync no longer generates phantom category-change entries after manual ledger edits (ISSUE-071).** When a transaction's posting account was manually edited (via `istota-skill money` CLI or direct file edit), the next sync compared the stale DB tracking record against the new Monarch category, saw a discrepancy, and emitted a category-change entry that double-counted the correction. The reconciliation loop now verifies the ledger still contains the OLD account before emitting the change — if the change was already made out-of-band, the entry is skipped and the DB tracking record is updated to match reality.
- **Multi-image feed cards no longer clip the meta strip.** The grid-view 2×2 gallery sized each cell with `aspect-ratio: 1`, so on wide cards the gallery's natural height exceeded the card's `max-height: 420px` and `overflow: hidden` ate the meta row. Galleries now use a fixed 320px height with `object-fit: cover` and per-count layouts: two-image entries collapse to a single row, three-image entries put the first image full-height on the left with the others stacked beside it (no awkward empty cell), four-and-up keep the existing 2×2 grid.
- **Module-skill subprocess errors now mark scheduled tasks as failed instead of silently completing.** `_execute_command_task` keyed success entirely off `proc.returncode == 0`, but skill facades catch their own errors and print `{"status":"error","error":"…"}` to stdout while exiting 0. The scheduler thought every broken run had succeeded, retries never triggered, alerts never fired, and bugs rotted unnoticed for hours (both feeds bugs this week were hidden by exactly this gap; money's `run-scheduled` was hiding monarch sync failures the same way). Two-layer fix: (1) `_execute_command_task` now parses stdout when it looks like JSON and treats `{"status":"error",…}` as failure; (2) `_output()` in the feeds and money skill facades now exits non-zero when emitting an error envelope, so the subprocess returncode reflects reality.
- **`feeds run-scheduled` no longer crashes with `cmd_poll() got multiple values for argument 'ctx'`.** The Click command for `run-scheduled` was implemented as `cmd_poll.callback(ctx=ctx, limit=limit)`, but `cmd_poll` is wrapped by `make_pass_decorator(FeedsContext)`, which re-injects the context as a positional first argument — collision. Refactored both commands to call a shared `_poll_due()` helper. The previous config-propagation fix unblocked the subprocess but exposed this latent bug, which was the actual reason feeds polling and the settings-page refresh button still appeared dead after the daemon restart.
- **Module-skill scheduled jobs (`_module.feeds.run_scheduled`, `_module.money.run_scheduled`) now find the istota config.** The daemon's `--config` flag wasn't visible to subprocess children, and their cwd is `config.temp_dir` — not a directory containing `config/config.toml`. Every recent run silently errored with `user 'X' not in istota config`, so feeds polling and money's run-scheduled work appeared to stop. `load_config()` now honours `ISTOTA_CONFIG_PATH`, the daemon records the loaded path on `Config`, and `_execute_command_task()` propagates it to subprocesses. The settings-page "refresh now" button is unblocked by the same fix.
- **Continuous walking tracks no longer break into false dwell-gap segments (ISSUE-066).** `stripIsolatedPlacePings` in `LocationMap.svelte` deleted isolated bounce-back pings (e.g. a stale Wi-Fi/cell fix into Home while walking away from it) instead of just nulling their place. Removing the ping created a 5+ minute time hole between its surviving neighbours, which then tripped `isGap`'s `timeDeltaS >= DWELL_MIN_DURATION_S` rule and rendered a dashed gap segment across normal walking. Fix: relabel the ping to `place: null` on a clone (so other consumers like `buildPingPointsGeoJSON` keep the original `place`) — the place-crossing gap rule still doesn't fire on it, but the ping's timestamp stays in the time series so the dwell-duration rule sees no discontinuity. The coords may show a small spatial wobble (~100m) at the bounce point — preferable to a missing track segment.

## [0.8.2] - 2026-04-29

### Fixed
- **Visit reconciliation no longer creates phantom duplicates on sliding windows (ISSUE-064).** `db.reconcile_visits()` runs every minute over a 6h sliding window. The previous DELETE clause filtered on `entered_at >= since AND entered_at < until`, so once `since` advanced past a visit's first ping but `until` still covered its last ping, the prior reconciler output was never cleaned up — the new run produced a fresh visit record with a later `entered_at` (because the read window had also slid past the early pings) while the original record persisted. A single ~11 min stop accumulated 7 phantom rows over the hour following the visit. Fix: read pings from `since - 24h` so visits straddling `since` are reconstructed in full, keep only segments whose last ping is in `[since, until)`, and switch the DELETE to overlap-aware (`exited_at >= since AND entered_at < until`). The reconciler is now idempotent across sliding windows; existing phantoms are cleaned up on the next nightly pass.

## [0.8.1] - 2026-04-29

### Added
- **Knowledge graph extraction now sees the existing facts.** The nightly extraction prompt includes the user's current KG facts so the model can skip re-emission and produce informed updates (refinements, supersessions) instead of letting mechanical dedup quietly drop duplicates. Closes the loop between the LLM and `add_fact()`'s dedup logic.
- **Fact provenance via `source_ref`.** Extracted facts can attach a `source_ref` integer task id pointing at the conversation that supports them; the value flows through to `add_fact(source_task_id=...)` so the KG audit trail is now traceable to a specific task. Optional — omit when the evidence is diffuse.
- **Five new suggested predicates** in the extraction prompt: `traveled_to`, `has_appointment`, `has_family_member`, `interested_in`, `completed`. Predicates remain freeform; these are documented hints with usage guidance (e.g., `traveled_to` and `has_appointment` route their dates to `valid_from`/`valid_until` rather than the object string).
- **Post-extraction sanity check.** When the model narrates its process instead of producing bullets ("Memory extraction complete..."), the sleep cycle now logs a warning, advances state, and skips the file write rather than persisting empty/junk content. Catches the regression that lost a day of memories on 2026-04-26.

### Changed
- **`decided` predicate guidance.** Extraction prompt now distinguishes durable decisions (no `valid_until`) from one-time actions, cancellations, and purchases (set `valid_until` a few weeks out so they age out of the current-fact view automatically). Aimed at the stale-decision pattern where one-time `decided` entries linger past their relevance.
- **Object length capped at 100 characters / 10 words** in extracted KG facts. Long objects (full sentences) broke fuzzy dedup — rewordings drifted past Jaccard 0.6 — and crowded out other context once existing facts started loading back into the prompt. Validation rejects over-length objects; the prompt explicitly routes context to MEMORIES rather than fact objects.
- **Sleep cycle module relocated** from `src/istota/sleep_cycle.py` to `src/istota/memory/sleep_cycle.py`. Pure refactor — no behavior change, no public API change. Imports inside the package and tests updated. The logger name (`"istota.sleep_cycle"`) is preserved so operator log filters keep working.

### Added
- **Sleep cycle goes through the brain abstraction.** Nightly memory extraction (user + channel) and op-based USER.md curation now invoke the configured `[brain] kind` instead of calling the `claude` CLI directly, so a future direct-HTTP brain doesn't leave a quiet hole in privileged orchestration. Per-feature model overrides via new `[sleep_cycle] extraction_model` / `curation_model` and `[channel_sleep_cycle] extraction_model` (each defaulting to `"sonnet"`). Calls remain text-only — empty `allowed_tools`, no streaming, no sandbox, no skill proxy.
- **CHANNEL.md is now indexed for memory recall.** Each channel sleep cycle re-indexes the durable `CHANNEL.md` file under a new `source_type="channel_memory_durable"`, so decisions and project status that users hand-edit there become searchable via auto-recall. Distinct from the dated `channel_memory` chunks (which expire with retention) so there's no risk of the file getting pruned by age.
- **Knowledge graph audit log.** Every fact mutation — inserts, single-valued supersessions, fuzzy-dedup skips, invalidations, deletions — is recorded in a new `knowledge_facts_audit` table with the before/after snapshot and source-task pointer. Inspect via `istota-skill memory_search fact-history [--entity X] [--since DATE]`. Pruned at 4× the user's `[sleep_cycle] memory_retention_days` so historical visibility outlives recall.
- **Per-chunk topics for dated memory indexing.** The model already emits per-task topic classifications in the extraction's TOPICS section; previously the sleep cycle collapsed them to a single dominant topic per file. Each indexed chunk now inherits the topic of the first `ref:N` bullet it contains, so `--topic` filters keep precision when a day spans multiple categories. Bullets without a `ref:N` mark their chunk as NULL-topic — those are still returned in topic-filtered searches by design.
- **USER.md size observability.** Curation audit JSONL records `user_md_size_bytes` on every run so growth curves are inspectable from the audit alone. A one-line warning posts to the user's `log_channel` when USER.md crosses 8 KB, before it starts crowding out recall and KG facts in interactive prompts. Surfaced in `istota-skill memory_search stats` as `user_md_size_bytes`.

### Changed
- **`max_knowledge_facts` default 0 → 50, with smarter truncation.** Defaults are now bounded; opt in to unlimited by setting `0` explicitly. Identity facts (subject == user_id) are anchors and never truncated even when over cap — over-budget identity is surfaced in debug logs rather than hidden by silent truncation. Matched facts (subject/object in prompt) sort by `updated_at` desc so the most recently changed surface first.
- **Tighter knowledge-graph fuzzy dedup.** Word-Jaccard threshold lowered from 0.7 to 0.6, paired with a token-subset fast-path scoped to identical predicates (so `python` ⇄ `python 3` and `acme` ⇄ `acme corp` collapse cleanly, but `is_allergic_to` vs `allergic_to` and `tech_1` vs `tech_10` don't). The audit log records each skip's `match_type` (`token_subset` vs `jaccard`) so the threshold is tunable from data.
- **Auto-recall dedupes against conversation context.** Memory recall used to surface the same task back to the prompt twice — once because context selection picked it as recent history, once because BM25 found it as topically relevant. Recall now filters out conversation chunks whose task_id is already injected as context, scoped per namespace (user and channel).
- **Dated memory filenames now follow the user's timezone, not the server's.** A user in a far-east tz on a UTC server previously saw filenames one calendar day off from when their cron actually fired. Filenames and bullet date stamps are computed in `user_config.timezone`. Existing files are not migrated — fix forward only. Channel sleep cycle stays UTC because channels span timezones.
- **Curation top-region constraint surfaced in user-facing docs** (`docs/features/memory.md`) with a worked example layout. The rule is unchanged — ops only edit content above the first `### subheading` in each `## section` — but it was previously documented only as an implementation note.

### Added
- **Op-based USER.md curation.** When `[sleep_cycle] curate_user_memory = true`, the nightly USER.md update is now a JSON list of small ops (`append` / `add_heading` / `remove`) rather than a full Sonnet rewrite of the whole file. Ops only operate on the top region of a section (the lines before any `### subheading`); the applier dedups appends, rejects ambiguous removes, and never raises on bad input. Every applied or rejected op is recorded in a sidecar `USER.md.audit.jsonl` so each night's changes are reviewable. After applied ops the file is re-indexed and a one-line summary is posted to the user's `log_channel` (gated by new `curation_log_summary`, default true). The previous full-rewrite path is gone. Setting remains opt-in (default `false`).
- **Unified memory retention.** `[sleep_cycle] memory_retention_days` now governs both dated memory FILES (existing) and ephemeral `memory_chunks` rows (new — `conversation`, `memory_file`, `channel_memory`). `[channel_sleep_cycle] memory_retention_days` does the same for the channel side, scoped to `channel_memory`. Durable `user_memory` chunks are not pruned by age — they refresh on file edit. Default stays `0` (unlimited), so existing deployments are bug-compatible until you opt in.
- Location: dual-source GPS deduplication. The day-summary pipeline (skill + web) now drops one of any two pings within 5 seconds of each other — Overland on iOS occasionally emits a high-accuracy GPS fix paired with a low-accuracy cell/Wi-Fi fix anchored elsewhere, fragmenting clusters and breaking trip detection. Tie-breaks on `activity_type` presence, then accuracy.
- Location: place-aware day-summary clustering. A cluster is now attributed to a place by counting per-ping geofence matches across its member pings — not by the centroid. A cluster inherits a place_id only when at least 3 member pings carry it (matches ~20 s inside the geofence at the typical 10 s ping cadence, which rules out drive-bys while keeping brief legitimate stops). Fixes the case where walking legs through a 250 m cluster radius dragged the centroid outside a place's match radius and caused the day-summary to report a nearby road name instead of the place the real-time webhook had correctly tagged. The previous centroid-validation band-aid (`validate_cluster_places`) is removed — it was the wrong direction for this failure mode.

### Changed
- Memory subsystem reorganized under `src/istota/memory/` (was scattered at the package root). `memory_search.py` → `memory/search.py`, `knowledge_graph.py` → `memory/knowledge_graph.py`, plus the new `memory/curation/` package. Pure refactor — no behavior change, no public API change. Logger names preserved (`"istota.memory_search"`, `"istota.knowledge_graph"`) so operator log filters keep working.
- Auto-indexing for memory search now skips silent scheduled jobs (`heartbeat_silent=True`). Those are high-volume retrieve-and-render crons whose conversations have no recall value and were inflating `memory_chunks`. Going-forward growth from this source is killed; historical orphan chunks settle out as retention catches up.
- USER.md is automatically re-indexed after `curate_user_memory` writes, keeping `source_type='user_memory'` chunks in sync with the file. Without this, searches returned the pre-curation text until a manual reindex.
- README refresh: added Email routing, Web interface, and Pluggable model backend sections; updated Skills (two-pass selection + sticky-skills carryover, current inline skill list including `money` in-process and Google Workspace); per-job model/effort overrides under Scheduling; comparison table skill count updated to ~28; workspace tree expanded; lowercase `git/gitlab/github` throughout prose.
- Project now publishes to a github mirror at `istota-project/istota` in addition to the gitlab canonical repo. Mirror is push-driven (no auto-sync); see DEVLOG for the dual-`pushurl` setup.

### Fixed
- Memory chunk retention computed the wrong cutoff. The cutoff used Python's `datetime.isoformat()` (`'T'` separator + microseconds), but SQLite's column default produces space-separated second-precision strings — `' '` lex-compares less than `'T'`, so for any row whose date prefix matched the cutoff date the comparison was unconditionally true and up to 24 hours of rows on the boundary day were deleted instead of kept. Cutoff now uses `strftime('%Y-%m-%d %H:%M:%S')` to match SQLite. Existing tests passed because the test fixture wrote backdated rows in the same buggy format; new regression tests exercise the production write path (insert via column default + age via SQLite's own `datetime('now', '-N days')`).
- `cleanup_old_chunks` vec-cascade no longer silently skips remaining rows after one delete fails. The vec-row try/except now sits inside the loop instead of around it, so a single sqlite-vec hiccup can't leave orphan vec rows pointing at a deleted main-table row that subsequently gets committed.
- USER.md curation no longer triggers spurious nightly rewrites on hand-edited files with formatting drift (trailing whitespace on headings, missing trailing newline, CRLF line endings). The skip-write decision is now outcome-based — write iff at least one applied op had a real effect — instead of comparing serialized output against the file's exact byte content.
- USER.md curation now inserts a blank line between a section-ending paragraph and a newly appended bullet so they don't visually fuse in rendered markdown. Bullet→bullet adjacency is unchanged.
- USER.md curation accepts hashtag/footnote/issue-reference content like `"#hashtag"`, `"#footnote-1"`, `"See issue #42"` as bullet text. The previous heading-prefix guard rejected any leading `#`; it's now narrowed to actual heading shapes (`# `, `## `, ..., `###### `).

### Added
- Pluggable `Brain` abstraction in `src/istota/brain/` separates model invocation (subprocess, stream parsing, retry) from executor orchestration (memory, skills, sandbox, deferred DB writes). Selected via new `[brain] kind = "..."` config section; defaults to `"claude_code"` (current behavior) so existing deployments need no changes. Lays the groundwork for in-process brains (OpenRouter, Anthropic-direct) without a parallel execution path.
- New `untrusted_input` skill (doc-only) loads alongside skills that ingest content from outside the trust boundary — `email`, `browse`, `calendar`, `transcribe`, `whisper`, `feeds`, `bookmarks` — via `companion_skills`. Pairs with `sensitive_actions`: that one governs outbound, this one governs how inbound is *read*. Covers nine concrete injection patterns (instruction injection, fake system framing, impersonation, pre-authorization claims, encoded payloads, hidden HTML, reply-chain quote injection, entity-name probes, gradual escalation) and re-states the per-action authorization rule so it travels with whichever side of the boundary the model is reading from.
- Web UI: **+ New place** action in the location sidebar — works on both Today and History. Click it, then click anywhere on the map to drop a place there.
- Web UI: **Discover** chip on the location History page overlays unknown recurring clusters and dismissed zones onto the same map, in the same spatial context as your pings and tracks. Click a yellow cluster to name it (or dismiss it); click a dimmer dismissed circle to restore.

### Security
- Reverted the outbound email recipient gate ("Layer A") that shipped briefly earlier today. Its allowlist counted any prior correspondent — anyone we'd mailed before, anyone who'd mailed us — as authorized to receive outbound mail, which let a single inbound email permanently allowlist a sender as an outbound recipient. That was strictly less safe than the prior pre-action confirmation flow, which is restored. The agent-side confirmation rule for external email sends is back in place.
- Strengthened the `sensitive_actions` skill prompt: explicit public/private boundary, narrow scoping of what trust authorizes (the trust list permits processing a sender's inbound mail, it does NOT authorize sharing user data outbound to that party), per-action authorization (a prior `yes` does not carry forward to new actions), and a broader action list covering all egress channels (email, file shares, ntfy, browser submissions, third-party APIs). The persona file already carries the boundary principle but is dropped for briefings; `sensitive_actions` is `always_include`, so the operational rule lives there too.

### Changed
- Web UI: location Today now uses the same full-width bottom bar + collapsible details panel as History — current visit (place / duration / time-since / battery) is inlined, then ping/stop/transit/trip counts, then a single Show details toggle. The floating info card is gone.
- Web UI: the standalone Places page has been removed. Place creation, discovery, and dismissal are reachable from Today and History via the shared sidebar and the Discover chip — there is no longer a separate `/location/places` view.
- `scripts/release.sh` now consolidates duplicate `###` subsections (Added / Changed / Fixed / …) when extracting the version's notes for the git tag annotation. GitHub Releases get one of each header in Keep-a-Changelog order instead of repeating headers in dev-time chronology. The `CHANGELOG.md` file itself is left untouched.

### Fixed
- Per-job `model` override in CRON.md no longer inherits `config.effort`. Previously a job pinned to Haiku (or another model that doesn't accept `--effort`) would still get the global effort flag and fail. Now: if a job sets `model` but not `effort`, no `--effort` is passed; set both fields explicitly to combine a model override with an effort.
- `!stop` could no-op for the microsecond between `Popen` returning and the prompt being written to the subprocess's stdin, because the PID was recorded after the stdin write. PID is now recorded immediately after `Popen` so a stop signal in that window finds a registered subprocess.
- Talk notifications for email-source tasks no longer silently disappear when the task's `conversation_token` is a synthetic email-thread hash (the case for any chain initiated by inbound mail). Delivery now falls back to the user's resolved alerts channel — same chain as the inbound confirmation gate. Without this fix, thread-match follow-ups (an external contact replying to a bot-initiated thread) sent the SMTP reply correctly but never produced the `output_target="both"` Talk post. Heuristic-based; the proper structural fix (separate `talk_delivery_token` column from the email-thread grouping `conversation_token`) is queued as ISSUE-057.
- Task timestamps (`started_at`, `completed_at`, `updated_at`) are now written in UTC via SQLite `datetime('now')` instead of Python's local-time `datetime.now().isoformat()`. Two bugs collapsed into one fix: on non-UTC machines the local-time write made fresh tasks look hours old to SQLite, causing the "release stuck running" cleanup in `claim_task` to fire immediately and break the per-channel foreground gate; on UTC machines the `T`-vs-space separator difference made the same comparison silently always-false, so the 15-minute stuck-running detection (and the `completed_at`-based old-task cleanup) had never actually fired in production. Both now work as written.
- Web UI: location map no longer crashes on initialisation when discovered clusters are present. The previous `circle-stroke-dasharray` paint property is unsupported by MapLibre and was throwing a style-validation error that dropped the WebGL context as soon as the cluster source contained any features.
- Web UI: location Today info panel no longer covers the map zoom controls — moved to bottom-left to match the existing mobile layout.
- Local dev: `VITE_MOCK_API=1` mock backend now actually persists place creation/edit/delete and cluster dismiss/restore in-memory across requests, so the full flows can be exercised without a live FastAPI backend.

## [0.8.0] - 2026-04-26

### Changed
- Web UI top nav collapses into a hamburger menu on mobile (≤ 640 px) so the page links no longer wrap below the "Istota" title and there's headroom for more sections. Built on `bits-ui` `DropdownMenu` for keyboard/ARIA correctness; desktop layout unchanged.
- Sidebar toggle on mobile (≤ 768 px) is now a vertically-centered chevron tab affixed to the left edge, replacing the earlier bottom-left chip that clashed with bottom-anchored UI like the location day-summary card. Affects feeds (Sources), location (Places), money/transactions (Accounts), and money/accounts.
- Mobile sidebar can now be dismissed by tapping anywhere outside it. The toggle hides while the sidebar is open since the sidebar would obscure it.
- Money year selectors now show "All" instead of "All years" so they fit in their intrinsic-width state on mobile (transactions, reports, accounts).
- On the money transactions and accounts pages the year + filter inputs stay on a single row at all viewport sizes; the filter input grows to fill the space the year selector leaves.
- Money transactions list rows now align flush with the section header above them — previously they were inset by an extra ~0.5 rem due to nested padding.

### Security
- Deferred subtask creation now bounds prompt-injection blast radius. New `scheduler.max_subtask_depth` (default 3) refuses subtask creation when the parent chain is already at the cap — worst-case fan-out drops from unbounded to 10 + 100 + 1000. New `scheduler.max_subtask_prompt_chars` (default 8000) skips oversize prompts. The existing per-task cap of 10 is now exposed as `scheduler.max_subtasks_per_task`. INFO log on creation lists prompt prefixes for audit trail.
- Linux + bubblewrap is now documented as the only supported deployment configuration. Non-Linux / no-bwrap setups still run for development but provide no isolation guarantees. Scheduler now logs `SECURITY UNSUPPORTED CONFIGURATION` at WARNING level when sandbox is unavailable or explicitly disabled with multiple users configured (previously a softer informational message). Closes audit item M4.
- Skill proxy and network proxy Unix sockets are now created with `0o600` permissions immediately after `bind()`, so other local users on the same host can no longer connect during a task window. (Audit L2.)
- Web API place-creation and cluster-dismiss endpoints no longer return raw exception strings to the browser. The full exception is still logged server-side; the response body is a generic `failed to create place` / `failed to dismiss cluster`. (Audit L7.)
- `!status` system-wide running/queued counts are now hidden from non-admin users. The per-user task list is unchanged. Admins still see the full system view. (Audit L8.)
- ntfy `Title` and `Tags` headers are now CR/LF-stripped at the boundary, replacing any newlines with a single space. Prevents header injection if the input ever contains a stray newline (httpx already rejects them, so this avoids the `RequestError` rather than introducing exploitability). (Audit L10.)

### Changed
- Skill proxy credential authorization is now decoupled from skill selection. Any CLI skill whose mapped credentials are present in the user's task environment can request them at runtime — Pass 1 keyword matching and Pass 2 semantic routing only control which skill docs go into the prompt, not which credentials are accessible. Fixes the long-standing failure mode where a keyword miss would silently strand an agent without the credentials it needed.
- Pass 2 (semantic routing) prompt now includes the user's resource types so Haiku can reason "user has miniflux configured → feeds is plausible" without keyword overlap. Each skill line in the manifest also carries a `[needs resource: …]` hint when applicable.

### Added
- Structured WARNING logs on every skill-proxy rejection, keyed by task and reason code (`proxy_rejected task_id=… type=skill|credential reason=unknown_skill|not_authorized_credential|credential_not_present`). Companion INFO logs from skill selection (`pass1_selection count=N: foo(always_include), bar(keyword='kw'), …` and `pass2_added` / `pass2_no_additions` / `pass2_timeout`) make it possible to count selection misses vs. real abuse attempts.
- Skill-proxy rejection responses now include `reason`, `name`/`skill`, and `authorized_skills` fields. The `istota-skill` client surfaces the authorized-skills list to the model via stderr so it can adapt rather than retry blindly.

### Added
- Five new `istota-skill location` subcommands matching the web UI: `discover` (find unknown recurring clusters), `dismiss-cluster` / `list-dismissed` / `restore-dismissed` (manage zones the discover view should skip), and `place-stats` (visit count, first/last/longest visit, total time spent — derived from pings).

### Changed
- Skill docs (`money`, `bookmarks`, `location`, `memory_search`, `feeds`) now point at `istota-skill <name> --help` for the live argument list, alongside the existing hand-enumerated examples.
- `money/skill.md` lists `run-scheduled` (previously omitted) and includes it in the mutation/concurrency rule.

### Fixed
- `istota-skill money run-scheduled` now works. The Click subcommand existed in the underlying `istota.money` CLI but was never wired into the `istota-skill` argparse wrapper, so the auto-seeded `_module.money.run_scheduled` cron job exited with usage help instead of running.
- Sidebar no longer side-scrolls when child content exceeds its width. Long place names truncate with an ellipsis instead of expanding the row.
- Place row hover background now reads symmetric top/bottom (explicit `line-height` + rebalanced padding) and left/right (matching gutter on both sides of the sidebar list).

### Changed
- Location places sidebar: removed the per-row radius badge and the hover-to-delete `×`. Rows now show the place name only.
- Place delete moved into the place edit modal as a left-aligned "Delete" link guarded by a confirmation prompt — no more accidental deletes from the sidebar.
- Place edit modal's category dropdown now lists the base categories *plus* every distinct category in use across the user's places (deduped, alphabetized), so a category created once stays available for the next place.

### Added
- Reusable web UI primitives in `web/src/lib/components/ui/`: `AppShell`, `ShellHeader`, `Sidebar`, `SidebarToggle`, `CategoryGroup`, `NavLink`, `Button`, `Select` (bits-ui Select wrapper), `Modal` (bits-ui Dialog wrapper). Replaces ~400 lines of duplicated shell/sidebar CSS across the four route layouts.
- `--chip-padding-x` and `--chip-gap` CSS variables in `app.css`; `.nav-hang` utility for hanging-pill alignment so chip text aligns with surrounding heading text.
- `CategoryGroup` supports a `collapsible` prop with caret toggle. Location places sidebar groups now collapse like the transactions account tree.
- Vite middleware mock (`web/vite-mock-api.ts`, gated on `VITE_MOCK_API=1`) lets `npm run dev` render the full UI with HMR without the FastAPI backend running.
- Logout link in the top nav is now a Lucide `LogOut` icon.
- Per-job `model` and `effort` overrides in `CRON.md`. Add `model = "claude-sonnet-4-6"` and/or `effort = "low"` to any `[[jobs]]` block to pin that one job to a specific Claude model and effort level. Per-task wins over `config.model` / `config.effort`; neither set = CLI default. Useful for downgrading volume "retrieve-and-render" jobs (briefings, transcription cron, feed digests) to Sonnet without touching the global default.
- Loose validation on `CRON.md` load: warns (never rejects) when `model` is missing the `claude-` prefix or contains whitespace, and when `effort` isn't in `{low, medium, high, xhigh, max}`.
- `!cron` listing now surfaces per-job `model: X` / `effort: Y` inline.
- Log channel finalize header now appends the resolved model + effort inline — e.g. `✅ Done (3 actions) - cli (claude-opus-4-7 high)` — so per-job overrides are visible at a glance without cross-referencing CRON.md.
- `effort` config field (top-level in `config.toml`, `istota_effort` in Ansible) wires Claude Code's `--effort` flag for adaptive reasoning. Accepts `low`, `medium`, `high`, `xhigh`, `max`. Supported on Opus 4.7, Opus 4.6, Sonnet 4.6. Empty = model default.
- `agents:` markdown frontmatter convention baked into the system prompt: per-file instructions (1–3 sentence string) travel with a file and are honored on reads from trusted paths, ignored on untrusted paths.
- In-tree `istota.money` subpackage (formerly the standalone moneyman service): accounting CLI, business logic, and SvelteKit pages folded into istota. Optional install: `pip install istota[money]`.
- Money web pages at `/istota/money/*` (Accounts, Transactions, Reports, Taxes, Business). Feature flag exposed via `/istota/api/me` as `features.money`; nav item appears when the user has a money resource.
- Money skill is in-process — no API key, no HTTP round-trip. Resource type accepts both `money` and legacy `moneyman`.
- Per-user money scheduled job `_module.money.run_scheduled` (daily 8 AM). Seeded under a reserved `_module.money.*` name prefix; auto-removed when a user's resource or feature config disappears. Folds in an opportunistic monarch sync (when `monarch_config` is set) followed by the invoice schedule check. Skipped entirely for ledger-only users. Workspace-mode users are seeded too — previously skipped.
- Workspace-mode money config loading: `INVOICING.md` / `TAX.md` / `MONARCH.md` files (TOML in fenced code blocks) in the user's workspace `config/` dir. Legacy `*.toml` files still accepted as a fallback.
- `EnvSpec.resource_types` — a declarative skill env spec can now match any of multiple resource types.
- `scripts/migrate_money_workspace_config.py` — one-shot migration from legacy `*.toml` to `*.md`.
- `Config.namespace` field — the install namespace (drives `/etc/{namespace}/`, etc.) is now a first-class config field, parsed from the TOML's top level and emitted by the ansible role.

### Changed
- Web UI: secondary navbars across feeds / location / money standardized — same chip styling, font size (`--text-sm`), padding, gap, line-height. App nav background bumped to `#1a1a1a` to differentiate from the page bg `#111`. Sidebar default width unified to 220px.
- `routes/location/+layout.svelte`, `routes/feeds/+layout.svelte`, `routes/money/+layout.svelte`, `routes/money/transactions/+layout.svelte` migrated onto the new shell/sidebar primitives. `lib/components/location/PlaceForm.svelte` uses `Modal` + `Select` + `Button` instead of hand-rolled overlay/backdrop/select.
- Three raw `<select>` elements (ledger picker, transactions year picker, place category) replaced with the bits-ui-backed `Select` primitive.
- Custom system prompt (`config/system-prompt.md`, used when `custom_system_prompt = true`) gained an "Executing actions with care" section covering reversibility, risky-op examples, investigate-before-destroy, and scoped authorization. Sleep guidance split into two specific rules. Synced against Claude Code 2.1.120's extracted prompts; pieces that duplicate `emissaries.md` / `persona.md` were intentionally left out.
- Documentation now recommends pinning the `model` config to a full version ID (e.g. `claude-opus-4-7`) rather than an alias (`opus`), so a Claude Code update can't silently swap the model out from under us. Aliases still work but float to whatever Anthropic ships next.
- Money is now `src/istota/money/` instead of a top-level `src/money/` package; the standalone-extract scaffolding is gone. Web routes, skill, and scheduler all call the same in-process `istota.money.resolve_for_user(user_id, istota_config)`.
- Money skill no longer marshals env vars for workspace mode; it resolves the user's `UserContext` in-process and injects it into Click directly. The standalone `money` CLI keeps file-based config support (`MONEY_CONFIG=...` or `-c <path>`) for terminal use.
- Money scheduled jobs invoke `istota-skill money <cmd>` with `MONEY_USER` set, instead of `MONEY_CONFIG=… money --user X <cmd>`. `MONEY_SECRETS_FILE` is no longer exported by seeded jobs — the skill reads credentials in-process.
- `run-scheduled` now bundles an opportunistic monarch sync (when `monarch_config` is set) before the invoice check, with a new `--skip-monarch` flag for opt-out. Replaces the previously separate `monarch_sync` auto-seeded job — users who want a narrated/observable monarch sync layer their own prompt-based job in `CRON.md` on top.
- Monarch credentials (`monarch_session_token` / `monarch_email` / `monarch_password`) now live on the user's `[[resources]] type = "money"` entry, matching the karakeep / miniflux / overland convention. The previous per-user `/etc/{namespace}/secrets/{user_id}/money.toml` file is removed.
- The `[[resources]] type = "moneyman"` rendering now emits `type = "money"` (the loader still accepts both forms).
- Ansible: `[moneyman]` block removed from `config.toml.j2`; the moneyman nginx include is dropped; standalone moneyman cron entries are no longer used (the istota scheduler runs them per-user).

### Removed
- Standalone money REST API (`istota.money.api` package) and the `money serve` CLI subcommand. The SvelteKit pages consume `istota.money.routes` (session-auth router mounted by the istota web app), and the skill calls money in-process — no separate HTTP service needed.
- Per-user money secrets file at `/etc/{namespace}/secrets/{user_id}/money.toml` and the `money-secrets.toml.j2` ansible template. Replaced by colocated credentials on the money resource entry.
- `money.config` module (TTL cache, `set_loader`, mtime invalidation) — replaced by direct `resolve_for_user` calls.
- `MONEY_WORKSPACE` / `MONEY_DATA_DIR` / `MONEY_CONFIG_DIR` / `MONEY_LEDGERS` / `MONEY_DB_PATH` environment variables and the `setup_env` hook on the money skill — no longer needed once the resolver runs in-process.
- `web_app._install_money_loader` and the SIGHUP loader-reinstall step — replaced by setting `app.state.istota_config` after each config load.
- Public-extract tooling for moneyman (`scripts/extract_money_to_standalone.py`, `scripts/check_money_isolation.sh`, `tests/test_money_extract.py`).

### Deprecated
- `istota_moneyman_*` ansible vars (`api_url`, `api_key`, `cli_path`, `config_path`) — kept as no-ops for inventory compatibility but no longer rendered into config. Use the per-user `[[resources]] type = "money"` entry instead.

### Fixed
- Prompt now carries a single, consistent answer to "what's today" in the user's local zone. The header emits `Current time` / `Today's date` / `User timezone`, conversation-context timestamps (Talk and DB) render in `user_tz` instead of UTC, and the rules section explicitly tells the model to ignore the auto-memory `currentDate` (which Claude Code injects in UTC). Closes ISSUE-056.
- Money skill in sandboxed task runs no longer fails with "Unknown user" for workspace-mode users (resource entry without `config_path`). The unified resolver handles workspace and legacy modes uniformly across web, skill, and scheduler call sites.
- Monarch sync no longer fails with "No Monarch credentials configured" on instances whose namespace differs from `"istota"`. The hardcoded fallback in `_loader.load_user_secrets` and `scheduler._sync_money_module_jobs` was reading from the wrong `/etc/...` path; now uses `Config.namespace` directly. Also obsoleted by the unified credential storage on the resource entry.
- Monarch `sync-monarch` recategorization for income postings: removing a `#business` tag from an income transaction in Monarch produced a malformed ledger entry (double-credited the income account, introduced a phantom personal-expense debit, never reversed the original contra leg). The formatter now branches by account type — true reversal for income postings, category swap for expenses — and `monarch_synced_transactions` tracks `contra_account` so the reversal has the second leg available. Income→income category changes flip signs symmetrically. Income recats for rows synced before this change are skipped and surfaced in the sync result for manual reversal.

## [0.7.0] - 2026-04-24

### Added
- Temporal knowledge graph: entity-relationship triples with validity windows, queryable via `!memory facts` and the memory_search CLI, surfaced into prompts as relevance-filtered "Known facts."
- Topic and entity metadata on memory chunks, with filtered search via `--topic` and `--entity`.
- Place categories for medical, hotel, and transit; notes field on places (web + CLI); UI flow to create a place from a map click; dismissable cluster zones on the location places page.
- Asymmetric place-visit detection: accuracy gate, dwell-based exit, and a periodic batch reconciler that re-derives closed visits from recent pings.
- Speed-gradient path coloring (extended through magenta/white for rail), transit-run heuristic, browser-timezone day grouping, dwell-weighted heatmap, and great-circle arcs for long gap edges in the location web UI.
- Service manifest spec tiers (T1–T4) and per-service install field for the upcoming installer wizard (ISSUE-032).
- `log_channel_show_skills` config to include selected skills in log channel entries.

### Changed
- GPS outlier detection: lookahead + perpendicular test catches chained and off-axis bad fixes; single-ping outliers dropped before path rendering; transit-stop pings kept in path while true dwells are dropped.
- Path runs split at dwell boundaries to quiet up the month view; long gap edges rendered as great-circle arcs.
- Sleep cycle routes personal attributes to FACTS only, uses annotated suggested predicates with temporal-field guidance, and de-duplicates via word-bigram Jaccard; USER.md curation cross-references the KG to avoid restating facts.
- Behavioral instructions split out of user memory into the relevant skill prompts so USER.md only carries durable user-specific facts.
- Adaptive KNN `k` in vector memory search to survive post-filter starvation; UNIQUE constraint on current knowledge-fact triples (legacy rows de-duplicated on migration).
- Feeds page paginates with a `before` cursor under the unread filter, fixing infinite-scroll stalls.
- Persona and Talk guideline tightened: emoji defaults to none, work-effort puffery banned.

### Fixed
- Inbound tasks that trip the Anthropic policy filter no longer retry silently three times — they fail immediately and post a named alert to the user (ISSUE-033).
- Indoor GPS gaps no longer drop intermediate stops from the day summary (ISSUE-043).
- Location stop discovery no longer fragments a single dwell into multiple stops; `duration_minutes` surfaced.
- `!stop` cancelled tasks no longer get retried by the scheduler.
- Channel-level single-foreground-task gate now enforced at task claim time, preventing two workers running concurrently in the same channel.
- `selected_skills` missing from `get_task` SELECT and from log-channel entries for tasks with no tool calls.
- Heartbeat interval-elapsed test uses UTC to match production behavior.

## [0.6.1] - 2026-04-06

### Added
- Google Workspace skill: OAuth web-UI authentication, Drive/Gmail/Calendar/Sheets/Docs/Chat via the standalone `gws` binary, configurable scopes (read-only by default), credentials injected via the skill proxy.
- Email confirmation gate: plus-addressed mail from untrusted senders is held in `pending_confirmation` until the user approves via Talk; trusted-sender list editable at runtime from Talk.
- Suspicious-email user alerts: deferred `user_alerts.json` posts to the alerts channel for prompt injection / exfil attempts; alerts channel also notified after confirmed email tasks complete.
- Skill stickiness for conversation follow-ups: skills from the last 2 conversation tasks (within 30 min) and the explicit reply parent are added to Pass 1.

### Changed
- Skill-proxy credential allowlist derived from the skill index instead of hardcoded; all CLI-capable skills get their credentials regardless of which were selected for the task.

### Fixed
- Email replies to emissary tasks were silently dropped (ISSUE-031).
- Day summary merged Home stops across separate trips, hiding short away-from-home segments.

### Security
- High- and medium-severity findings from the codebase audit fixed across the skill proxy, deferred-file handling, web auth, and sandbox env handling.

## [0.6.0] - 2026-04-04

### Added
- Per-user plus-addressed email ingest (`bot+user_id@domain`) so external contacts can email a specific user's agent directly.
- Place management for the location skill: full CRUD via CLI and web UI, drag-to-reposition, ping-based visit stats, geofence circle interpolation through zoom 22, and ping reassignment when a place moves.
- `custom_system_prompt` config toggle replaces Claude Code's default prompt with a minimal one focused on tool use.
- Viewport-based read tracking on the feeds web page, with a "New" filter chip and unread count badge.
- Two-pass skill selection: deterministic Pass 1 plus a Haiku-based semantic-routing Pass 2, configurable via `[skills]`.
- Moneyman/Fava integration in the web UI: Services page (later replaced), per-user Fava reverse proxy under nginx.
- `git-cliff` configuration and changelog generation in the release workflow.

### Changed
- All bundled skill metadata consolidated into `skill.md` YAML frontmatter; `skill.toml` files removed from bundled skills (operator overrides may still use TOML).
- Network allowlist scoped to the current task's user (M-2); CSRF Origin checks added to all state-changing web endpoints (M-3); OIDC session rotated on login (M-4); deferred `sent_emails` no longer trust user_id from JSON (M-1).
- Location config moved from `LOCATION.md` to per-user `[[resources]]` of type `overland`; `LOCATION.md` removed.
- Stationary pings rendered as dots instead of connected lines; path segmentation only breaks on real spatial/time gaps, not activity changes.
- Moneyman service config moved from per-user resource to instance-level `[moneyman]`; per-user API key derived for HTTP calls.

### Removed
- `LOCATION.md` (replaced by DB-backed places + per-user TOML config).

### Fixed
- Context-management mid-response no longer causes duplicate delivery — the executor segments by CM boundaries and uses the last substantial segment (ISSUE-026).
- `SMTP_FROM` plus-addressed sender that some mail servers rejected.
- Stop-detection centroid drift; visit splitting now uses elsewhere-based detection instead of a fixed time gap.
- Location date display rendering in UTC instead of local time (ISSUE-029).
- Feeds infinite scroll not loading when filters hid most entries.
- Geofence radius display + map reset on place drag.

### Security
- Four medium-severity findings from the codebase audit fixed; warning when web session secret uses the insecure default.

## [0.5.0] - 2026-03-22

### Added
- `!search` command for searching Talk conversation history across the memory index, the Talk unified search API, and exported conversations.
- New `feeds` skill with Miniflux CLI (list, add, remove, categories, entries, refresh) and `miniflux` resource type.
- Authenticated web interface: SvelteKit frontend with Nextcloud OIDC login, dashboard, feeds page reading directly from Miniflux.
- Moneyman skill: dual-mode (CLI subprocess preferred, HTTP fallback) accounting client for ledgers, transactions, invoicing, and work log.
- `!more #<task_id>` command and `actions_taken` / `execution_trace` columns to surface task internals.

### Changed
- Replaced built-in feed polling with Miniflux as the RSS aggregator; non-RSS sources bridged via a separate `rss-bridger` service.
- `install.sh` rewritten as a thin Ansible bootstrap; the 1765-line script is gone, the wizard delegates to the bundled role.
- Briefing delivery is now deterministic: Claude returns structured JSON, the scheduler handles delivery, the email skill is excluded from briefing tasks.
- Stream parser deduplicates by tool/text block ID instead of `stop_reason`, so tool calls and interrupted responses are no longer dropped (ISSUE-024 follow-up, ISSUE-025).

### Fixed
- Malformed model output (raw tool-call XML under context pressure) is now detected and routed through retry instead of delivered as a "successful" empty response (ISSUE-019, partial).
- Browse skill no longer flags small passive reCAPTCHA badges as captchas.
- Five Debian 13 install bugs caught via Docker-based testing (pipx ensurepath, missing unzip/cron, Ansible v12 yaml callback, rclone password override, rclone obscure invocation).

### Removed
- `!usage` command — Anthropic blocks non-official clients from `/api/oauth/usage`.
- `garmin` skill — Garmin's SSO change broke `garth`; data access moves to the browse skill.
- Direct `accounting` skill, invoice scheduler, and the `accounting` extras group — all accounting flows through Moneyman now.

## [0.4.1] - 2026-03-18

### Added
- Emissary draft-approve-send flow: confirmed tasks get the bot's previous output injected as `confirmation_context` so it executes instead of re-drafting (ISSUE-016 Phase 2).
- Emissary email thread tracking: outbound mail recorded in `sent_emails`; replies from external contacts route back to the originating Talk conversation (ISSUE-016 Phase 1).
- Headlines briefing component: pre-fetches frontpages from AP, Reuters, Guardian, FT, Al Jazeera, Le Monde, Der Spiegel via the browser API.
- Briefing digest persistence — the previous briefing's body is included in the next prompt to reduce repetition.
- `prompt_file` field for CRON.md jobs so long prompts can live in separate files; `--tz` flag on calendar create/update for timezone-aware events.
- Per-user scripts directory under the bot dir, plus Garmin and Monarch credentials configurable as `[[resources]]` entries.

### Changed
- Sleep cycle memory extraction reworked: tail-biased excerpts, dynamic per-task budgets, conversation grouping, tightened prompt with examples (ISSUE-018).
- External email default sender identity is the bot, not the user, unless explicitly asked (ISSUE-017).
- Empty `all_descriptions` no longer leaves the ack stuck on "Riffing…"; reruns post a fresh ack so edit-in-place works.

### Fixed
- Briefing pipeline could leak one user's calendar events into another user's output; the unscoped fallback is gone and CalDAV credentials only flow when the user has discovered calendars (ISSUE-015).
- `location history --date` was filtering by naive UTC and capping at 20 pings; both fixed, `--tz` flag added.
- FinViz fetch now retries up to 3 times before giving up; previously a single transient failure stripped market data from the briefing.

## [0.4.0] - 2026-03-13

### Added
- Network isolation for the bwrap sandbox: each task runs in its own network namespace and reaches the outside world only through a CONNECT proxy with a host:port allowlist; defaults cover the Anthropic API and PyPI.
- Credential-isolated developer tokens: `GITLAB_TOKEN` and `GITHUB_TOKEN` go through a `credential-fetch` helper instead of the subprocess env when the skill proxy is enabled.
- Docker Compose stack: postgres, redis, Nextcloud, and Istota in four containers with auto-provisioning, optional browser and webhooks profiles.

### Changed
- Per-skill credential scoping: the proxy only returns secrets needed by the task's selected skills, and each skill CLI subprocess only sees its own env vars.
- Admin Nextcloud mounts now match non-admin scoping — own user dir + channel dir + explicit resources, not the whole content tree.
- Intermediate text blocks accumulated during streaming are prepended to the final result so tool-interleaved status updates aren't lost.

### Fixed
- CONNECT proxy no longer kills streaming API responses — tunnel timeouts are cleared after CONNECT and TCP keepalive is enabled.
- Skill proxy socket bind-mounted into the bwrap sandbox so it's actually visible at `/tmp/istota-proxy-{task_id}.sock`.
- `_warn_orphaned_email_output` no longer deletes legitimate deferred email files for briefings.
- Talk poller `list_conversations` cached with a 60s TTL and 15s timeout so transient ReadTimeouts don't abort poll cycles.

## [0.3.1] - 2026-03-13

### Fixed
- Skill proxy Unix socket was invisible inside the bwrap sandbox; now bind-mounted at `/tmp/istota-proxy-{task_id}.sock`.

## [0.3.0] - 2026-03-13

### Added
- Credential isolation via Unix-socket skill proxy: secret env vars stripped from Claude's environment, skill CLIs run through a server-side proxy that injects credentials.
- GPS location tracking via Overland webhook receiver with hysteresis-based place transitions, calendar attendance correlation, day summaries, and reverse geocoding.
- DST-safe scheduling: cron evaluation now uses naive local wall-clock times so spring-forward doesn't double-fire jobs and briefings.
- Per-user log channel for verbose tool-by-tool execution traces, with configurable `progress_style` (`replace` / `full` / `legacy` / `none`).
- `!export` command exports a Talk channel's full conversation history to a file in the user's workspace (markdown or text, incremental on repeat).
- Multi-user Talk room support: bot only responds when @mentioned in rooms with 3+ participants; reply threading and @mentions on the final response in group chats.
- Memory recall with BM25 search, dated-memory auto-load (`auto_load_dated_days`), `max_memory_chars` cap, and optional nightly USER.md curation.
- Heavy optional deps moved to extras groups; `!skills` shows availability with install hints; `dependencies` declared per-skill in `skill.toml`.

### Changed
- Skills restructured into self-contained directory packages under `src/istota/skills/` with `skill.toml` manifests and declarative env-var wiring.
- Conversation context now reads from a poller-fed local cache (`talk_messages`) instead of per-task Talk API calls; recency window (`context_recency_hours`) added.
- Briefing system consolidated into `skills/briefing/`; legacy `briefing.py`, `briefing_loader.py`, and `skills_loader.py` shims removed.
- One-time CRON.md jobs (`once = true`) are auto-removed from both DB and file after success; reminders skill template updated to match.

### Fixed
- Bot replies were absent from conversation context after the cache migration — root cause was a multi-thread race between poller and scheduler when re-tagging `:progress` to `:result`. Fixed with direct upsert and `ON CONFLICT DO UPDATE` preserving result tags.
- Production crash when `check_briefings()` held a write transaction across slow network I/O; split into read → prefetch → write phases.
- Per-channel gate after `!stop` no longer rejects new messages from cancelled tasks still in `running`.

## [0.2.0] - 2026-03-01

### Added
- Per-user filesystem sandbox via bubblewrap (`bwrap`): Claude Code subprocess runs inside a mount namespace, non-admins see only their own subtree.
- Deferred DB operations pattern: with the sandbox mounting the DB read-only, Claude and skill CLIs write JSON request files to a per-user temp dir for the scheduler to process.
- Three-tier worker concurrency: separate fg/bg instance caps, per-user limits, and a per-channel gate that queues duplicate-channel messages instead of discarding them.
- `!command` dispatch (`!help`, `!stop`, `!status`, `!memory`, `!cron`, `!check`, `!skills`) intercepted in the Talk poller before task creation.
- Heartbeat monitoring system with five check types (file-watch, shell-command, url-health, calendar-conflicts, task-deadline, plus self-check), cooldowns, and quiet hours.
- Hybrid BM25 + vector memory search (sqlite-vec + sentence-transformers), with channel sleep cycle and channel-namespace indexing.
- Whisper audio transcription skill with RAM-aware model selection; pre-transcription before skill selection so voice memos hit keyword rules.
- ntfy push notifications and a centralized notifications dispatcher; per-user `ntfy_topic` override.
- Webhook receiver service for GPS pings (Overland), separate from the scheduler.

### Changed
- Talk progress edits the initial ack message in-place instead of posting up to 5 separate messages; final message shows "Done — N actions (Xs)".
- Scheduled jobs gain isolation: excluded from interactive context, prioritized below interactive in dispatch, `silent_unless_action` mode, auto-disable after N consecutive failures.
- `[security]` clean-env subprocess + `--allowedTools` whitelist + credential stripping for heartbeat/cron commands; `EnvironmentFile=` support in systemd.
- Scheduled job definitions moved from sqlite-only to user-editable `CRON.md` files with TOML `[[jobs]]` blocks.
- Per-user directory structure: `workspace/` renamed to `{bot_dir}/`, config files moved into `{bot_dir}/config/`, with auto-migration.

### Fixed
- Bubblewrap on Debian 13: re-enabled unprivileged user namespaces, fixed merged-usr symlink resolution and dest-path handling for `/etc/resolv.conf` so DNS works inside the sandbox.
- Email header newline injection in `In-Reply-To`/`References` no longer causes outbound delivery failures.
- Briefings excluded from auto-loading user/dated memory to prevent private context leaking into newsletter output.

## [0.1.1] - 2026-02-21

### Fixed
- E2BIG on large prompts — prompt is now passed via stdin instead of `argv`, bypassing the 128 KB execve limit.
- `claude -p` requires the prompt as a positional arg; restored after the stdin migration.

## [0.1.0] - 2026-02-21

### Added
- Initial public release of Istota — Claude Code-powered assistant with Nextcloud Talk interface, forked from Zorg.
- Talk integration via long-polling (user API, not bot API), email input/output via IMAP/SMTP, and TASKS.md file polling.
- Per-user concurrent task queues with atomic locking, retry with exponential backoff, and stale-task cleanup.
- Streaming task execution: `subprocess.Popen` with `--output-format stream-json`, real-time tool-use progress posted to Talk.
- Skills system with selective loading by keyword/resource/source type; bundled skills cover files, email, calendar, todos, memory, markets, browse, accounting, developer (Git/GitLab/GitHub), nextcloud, and more.
- Sleep cycle: nightly memory extraction writes dated `YYYY-MM-DD.md` files; multi-tiered memory model (USER.md, CHANNEL.md, dated memories).
- Briefings: cron-based, components for calendar/todos/email/markets/news/notes/reminders, BRIEFINGS.md user config.
- Scheduled jobs (DB-driven), invoicing system with PDF export and beancount A/R, Monarch Money sync, Fava per-user systemd service.
- OCS API skill, OCR transcription skill, web browsing skill via Dockerized Playwright with VNC captcha fallback.
- Admin/non-admin user isolation via root-owned `/etc/istota/admins`; admin-only skills filtered for non-admin users.
- Emissaries (constitutional principles) layered before persona; per-user `PERSONA.md` overrides global persona.
- Interactive install wizard (`deploy/install.sh`) with Nextcloud connectivity validation, rclone obscure auto-generation, and `--dry-run` mode.
- Tag-based release deployment via `repo_tag` setting (`"latest"` resolves highest `v*` tag).

- MIT license, README rewritten with security model and origin story.
- Hybrid context selection: recent N messages always included, older messages triaged by Haiku/Sonnet.
- Native `imap-tools` + `smtplib` email backend with RFC 5322 References-header threading (replacing the pre-fork himalaya CLI).

[Unreleased]: https://gitlab.com/cynium/istota/-/compare/v0.30.0...main
[0.30.0]: https://gitlab.com/cynium/istota/-/releases/v0.30.0
[0.29.0]: https://gitlab.com/cynium/istota/-/releases/v0.29.0
[0.28.0]: https://gitlab.com/cynium/istota/-/releases/v0.28.0
[0.27.0]: https://gitlab.com/cynium/istota/-/releases/v0.27.0
[0.26.3]: https://gitlab.com/cynium/istota/-/releases/v0.26.3
[0.26.2]: https://gitlab.com/cynium/istota/-/releases/v0.26.2
[0.26.1]: https://gitlab.com/cynium/istota/-/releases/v0.26.1
[0.26.0]: https://gitlab.com/cynium/istota/-/releases/v0.26.0
[0.25.0]: https://gitlab.com/cynium/istota/-/releases/v0.25.0
[0.24.0]: https://gitlab.com/cynium/istota/-/releases/v0.24.0
[0.23.0]: https://gitlab.com/cynium/istota/-/releases/v0.23.0
[0.22.0]: https://gitlab.com/cynium/istota/-/releases/v0.22.0
[0.21.0]: https://gitlab.com/cynium/istota/-/releases/v0.21.0
[0.20.0]: https://gitlab.com/cynium/istota/-/releases/v0.20.0
[0.19.0]: https://gitlab.com/cynium/istota/-/releases/v0.19.0
[0.18.0]: https://gitlab.com/cynium/istota/-/releases/v0.18.0
[0.17.0]: https://gitlab.com/cynium/istota/-/releases/v0.17.0
[0.16.0]: https://gitlab.com/cynium/istota/-/releases/v0.16.0
[0.15.1]: https://gitlab.com/cynium/istota/-/releases/v0.15.1
[0.15.0]: https://gitlab.com/cynium/istota/-/releases/v0.15.0
[0.14.0]: https://gitlab.com/cynium/istota/-/releases/v0.14.0
[0.13.0]: https://gitlab.com/cynium/istota/-/releases/v0.13.0
[0.12.0]: https://gitlab.com/cynium/istota/-/releases/v0.12.0
[0.11.1]: https://gitlab.com/cynium/istota/-/releases/v0.11.1
[0.11.0]: https://gitlab.com/cynium/istota/-/releases/v0.11.0
[0.10.0]: https://gitlab.com/cynium/istota/-/releases/v0.10.0
[0.9.0]: https://gitlab.com/cynium/istota/-/releases/v0.9.0
[0.8.2]: https://gitlab.com/cynium/istota/-/releases/v0.8.2
[0.8.1]: https://gitlab.com/cynium/istota/-/releases/v0.8.1
[0.8.0]: https://gitlab.com/cynium/istota/-/releases/v0.8.0
[0.7.0]: https://gitlab.com/cynium/istota/-/releases/v0.7.0
[0.6.1]: https://gitlab.com/cynium/istota/-/releases/v0.6.1
[0.6.0]: https://gitlab.com/cynium/istota/-/releases/v0.6.0
[0.5.0]: https://gitlab.com/cynium/istota/-/releases/v0.5.0
[0.4.1]: https://gitlab.com/cynium/istota/-/releases/v0.4.1
[0.4.0]: https://gitlab.com/cynium/istota/-/releases/v0.4.0
[0.3.1]: https://gitlab.com/cynium/istota/-/releases/v0.3.1
[0.3.0]: https://gitlab.com/cynium/istota/-/releases/v0.3.0
[0.2.0]: https://gitlab.com/cynium/istota/-/releases/v0.2.0
[0.1.1]: https://gitlab.com/cynium/istota/-/releases/v0.1.1
[0.1.0]: https://gitlab.com/cynium/istota/-/releases/v0.1.0
