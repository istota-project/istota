# Where a task may send bytes

This page covers the egress decision: which tasks are limited to hosts their owner approved, what enforces it, and what stays open on purpose. The rules for owners are in [Scheduling](../features/scheduling.md).

## What it does and does not close

A task that can read private data and also fetch an arbitrary URL can be made to leak that data in the URL. Any reachable host can receive data in a path, a query string, a subdomain lookup, a redirect chain or a search term. The audit's finding H-2 names this, and the general case cannot be closed while a task has both open browsing and private data.

**H-2 is closed for an unattended task on its job's own terms. It is not closed for an attended turn.** A person asking in chat still gets open web with their private data in reach, and a page that turn reads can still make it send that data somewhere. That is accepted, because the person is present and open browsing is the point. `tests/test_egress_closure.py::TestTheAcceptedRisk` shows the canary reaching the attacker's host on both brains, so the risk stays a tested statement.

## The decision

`rooms.scopes.reach_for_task` computes one answer per task, from the root of the `parent_task_id` walk, so a subtask or `!retry` copy gets its root's answer. `Reach.egress_hosts` is None for any public host, or the exact set of hosts approved.

| The task | `egress_hosts` |
| --- | --- |
| A guest's or correspondent's turn | None. `web_tools_load` decides whether it has web tools at all |
| An attended own turn (Talk, web, email, SMS, WhatsApp, REPL, CLI) | None |
| Briefing or shared block research | None. It holds nothing private |
| A job with an approved `open:` for its current text | None |
| A job with an approved `public:` for its current text, whose row asks for `public` | None. It holds nothing private |
| Any other scheduled job, personal briefing, assignment wake or TASKS.md line | The hosts in its grants |
| Any other unattended source type, a read failure, no database | The empty set |

TASKS.md counts as unattended because a limited task can write a line into it.

## Grants and subjects

A row in `egress_grants` means approved, and there is no pending or denied state. A grant hangs on a subject: `cron:<job name>`, `briefing:<name>`, `assignment:<id>` or `tasks-file`.

| Entry | Meaning |
| --- | --- |
| `<host>` | That exact host, for WebFetch and the shell. No subdomain, scheme, path, port, wildcard or IP literal |
| `skill:<name>` | One `external` skill. `skill:browse` runs on the isolated profile with no credential verbs |
| `public:<digest>` | This exact job text runs with no private data and open web |
| `open:<digest>` | This exact job text runs as it did before, with the user's browser profile and every skill |

The digest covers a job's `prompt`, `command`, `skill` and `skill_args`. The sync resolves `prompt_file` into `prompt`, so the file's content is covered. Editing any of them drops the job's `open:` and `public:` approvals and asks again. A schedule, model or delivery edit does not.

Only an authenticated person writes a grant: the web route for the signed-in user's own subjects, the operator's `istota egress`, and the one-time seed. `tests/test_egress_grants.py` holds that list, and no skill CLI or deferred handler imports the module. An approval over the web carries the digest of the text the page showed, and a row that has moved since is refused.

## What enforces it

| Channel | Limited task | Open task |
| --- | --- | --- |
| WebFetch, native brain | `WebFetchPolicy.exact_hosts`, checked on every redirect hop before anything resolves | Public addresses |
| WebFetch, CLI brains | The same policy, in the document `istota_web` reads | Public addresses |
| Shell, git, pip, npm, cron `command:` jobs | `extra_hosts`, the Nextcloud host and each grant at 443. Registries and forges only if granted | The skill-keyed allowlist |
| WebSearch | Unchanged | Unchanged |
| Browser | Withheld without `skill:browse`. With it, the isolated profile | The user's profile |
| Devbox and other `external` skills | Withheld without a `skill:` grant. `developer` stays, and without `skill:devbox` it has no exec socket, so a shimmed `npm` or `node` exits 120 | Available |
| `feeds add`, `feeds import-opml`, `bookmarks add`, `ntfy --topic` | Refused, `egress_limited` | Available |
| `briefings` `sources add`, `sources set`, `blocks add`, `blocks save` | Refused. The daemon fetches what they name | Available |
| `briefings_config` `shared-block set`, `set-prompt`, `run`, `remove`, `group save`, `group set` | Refused. Research runs with open web on what they write | Available |
| Mail | Anything but the user's own addresses is held as a draft. Mail naming the bot's address is refused | The user's own policy |
| `room post` | No private origin, so refused | Available |

A heartbeat `shell-command` has no task row and no subject. It reaches operator hosts only.

## Refusals and the bell

Each seam appends `{"channel", "entry"}` to `egress-refused.jsonl` in the task's control directory, and the executor reads it when the task ends. A refused hostname can carry data, so it stays in the framework database, only its owner reads it, no log line names it, and it is never pushed. The channel decides what a line may become, so `https://open/` cannot read as a request for full access.

The bell has one row per subject with fixed text. Its detail names the subject and no host, and its one action opens Settings, Network access. There is no Allow in the bell, because a hostile page chooses the hostname.

A verb refusal logs nothing, since no grant could allow a verb.

## Public jobs

A public job is the research posture applied to one approved job. It keeps its user identity, its room and its delivery target. It has `browse`, `skills` and `untrusted_input`, and loses everything else.

- It has no memory, no files, no vault and no conversation context.
- It has no notes of its own in its room. `Channels/<token>` is not bound and `CHANNEL.md` is not loaded, because in a private room that file is the user's notes.
- Its deferred files are purged except `subtasks`, because the directory is writable by a model reading strangers' pages.
- A subtask of a public job is public too, and so is a `!retry` copy.
- The posture is on the task row. `tasks.egress_posture` is written once by the daemon before the model runs, as `public` or `private`, and the drain and descendants read that rather than the job row, which a `once` job has lost by then and an edit can change in flight. An occurrence nobody decided whose job has gone drops its deferred files, and its descendants withhold every scope. A job re-run is a new occurrence and decides from the job as it stands.
- It needs the sandbox, the network proxy, the skill proxy and the browser. A missing one fails the job with the same reason research gives, so a job's last error can read `briefing_research_network_proxy_unavailable`.

## Deliberate open paths

Each of these is known and left open.

- **Attended turns.** As above: H-2 stays open for them.
- **HEARTBEAT.md `url-health`.** A limited admin task can plant a `url-health` URL that the daemon fetches with no egress limit. HEARTBEAT.md is writable by a task, the check runs from the daemon's network on the next sweep, and the URL can carry private data in its query. `url-health` is admin-only, and the check exists to probe internal services. This is open pending a decision.
- **Delayed egress.** A limited task can write text into memory or a file, and a later attended turn can read it and act on it. A public job's answer is delivered, and may be indexed where a job's output is.
- **Jobs on `open`.** Every job that existed at upgrade starts on `open` and keeps this risk until its owner narrows it. `istota doctor` counts them under `security.egress_open_jobs`.
- **What an `open` job reads.** The digest does not cover a workspace file the job follows or a script it runs. Anyone who can edit that file can steer a job with full access, a limited task included.
- **WebSearch.** The query reaches the search provider. That is a deliberate disclosure: the provider is not a recipient an attacker can read.
- **Fixed-provider lookups.** `markets` and yfinance lookups carry what the model puts in a symbol to a fixed provider, the same standing as WebSearch.
- **Shared platforms.** Granting `github.com` or `pypi.org` grants a host that also serves attacker accounts. Grants are per host, not per tenant, and the page says so.
- **The browser and the devbox once granted.** `skill:browse` and `skill:devbox` are unbounded for that job. A leak cannot be stopped per URL in a browser, since redirects, clicks and forms all send data.
- **No sandbox or network proxy.** WebFetch and the skill gates still apply, and shell egress is then unbounded. With `skill_proxy_enabled = false` nothing sets `ISTOTA_EGRESS_LIMITED`, so the verb rules do not apply.
- **Skill CLIs run host-side.** The CONNECT list does not bound them. A `local` skill is local because the model cannot pick its destination beyond the verb rules, and that survey is not a proof.
- **Mail with a person in the loop.** A held draft may carry private data out once a person approves it. An own address that forwards into the bot's mailbox passes the gate, since Istota cannot see the forward. `--reply-to` the bot on mail to an own address is not refused, since returning needs the person to reply.
- **`ntfy --click` and links in mail to the user.** A URL the model wrote reaches the user's own device and leaves only if they tap it.
- **Credentials a limited task keeps.** A limited task keeps its vault and grants. A placeholder can go only to an approved host, and `istota-credential get` for an entry marked readable returns the value into the task, where it can leave only by an approved host or an open verb.
- **Writers left open on `briefings_config`.** `group run` (with `--mode publish`), `group delete`, `group delivery`, `group revoke-approval`, `schedule set` and `schedule remove` change no prompt or source and stay open under the flag. `group run` can start research on a definition its owner already wrote.
- **Pollers.** An already-subscribed feed's poller, `feeds refresh` and `poll` keep fetching. A limited task cannot change what they fetch.
- **CRON.md `target`.** A planted job's `target` can name a room. A shared room is refused. Whether another user's private room is refused was not established in this work.

## Residuals

- **The isolated profile is shared.** A job that signs in by typing with `--fill` or `--type` leaves that session in the profile every isolated task uses. Credential verbs are refused there, and typing cannot be.
- **Browser capacity.** All isolated tasks hold one of two Chrome slots. With two users holding live tabs the isolated profile gets a 503 until a tab passes its ten-minute lifetime, and a kept isolated tab holds a slot against a third user. At four tabs in all, a new owner's tab replaces the oldest anywhere. Tasks take turns on the display and do not run in parallel. Capacity was measured with Chrome stubbed, so there are no memory or latency figures.
- **Other browser identities.** Shared blocks browse under `__system__`, a profile of its own. An own turn in an incognito room keeps the user's profile. `istota user ensure __isolated__` is not refused, though the row is inert.
- **A flood of refusals.** A task can fill the log's 64 KiB cap with chosen names and the table keeps 20 rows per subject, so a real host can be pushed out. A request about the job survives eviction.
- **Ports.** A CONNECT to a granted host on another port is refused and logged as that host. Adding it does not help, because a grant is port 443 only.
- **An operator's skill override.** An override of a bundled skill with no `egress` loads as `external`. For a skill with `exclude_memory` that withholds it on a limited task and its exclusion no longer applies, so an override of `briefing` with no field loads dated memories into a limited briefing's prompt.
- **No database.** `reach_for_task` with no database answers the empty set, even for an attended task.
- **A renamed job starts from nothing.** The old name's rows go and the new name has no approvals.
- **A module job's stale rows.** The seed covers `_module.*` rows too, where they do nothing, and the module sync's prefix delete leaves them behind. Doctor does not count them and the page hides them.
- **Skill jobs.** A cron `skill:` job has no proxy, so its reach is read with no `own_addresses`, and a public `skill:` job's environment holds the index's declared credentials, as every skill job's does. It can run only `browse`, `skills` or `untrusted_input`, with arguments its owner approved by digest.

## What verifies this

The default suite drives real proxies and real CLIs on the host and checks argv, so it has not executed the sandbox path. `scripts/test-linux.sh` runs the namespace, `uv run pytest -m image -n0` the browser container, and `scripts/test-upgrade.sh` the seed migration over an older release. A public job's namespace has no witness of its own yet, and nothing has run a public job through the shipped scheduler and a real proxy; `uv run pytest -m smoke -n0` is where one belongs.
