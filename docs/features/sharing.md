# Sharing with Istota

You can give Istota a calendar or a folder without giving it a password for your whole account. Each route uses an open protocol, so it works with any provider that speaks it, and the provider decides what Istota can reach:

| What | How | Istota can |
|---|---|---|
| A calendar on any provider | its secret iCal (ICS) address | read it |
| Istota's own calendar | invitations to your address, or a feed you subscribe to | put events on it, move them and cancel them |
| A folder on Nextcloud or another Open Cloud Mesh server | a federated share to your Istota address | read it, and write to it if the share allows editing |

All three live on the Shared with Istota card in Settings > Connections. This is a second route beside sharing with the bot's Nextcloud account ([Nextcloud](nextcloud.md)), which keeps working where the operator runs that Nextcloud.

## Calendars you subscribe Istota to

Most calendar services give each calendar a secret address for subscribing from another app: Fastmail, iCloud, Google Calendar ("Secret address in iCal format") and Nextcloud all have one. On the card, choose Add calendar and paste it. A `webcal://` address works too.

Istota fetches the address once while you wait, checks that it is a calendar, and stores it encrypted. The address is not shown again and never reaches a task: the daemon reads it and tasks read the copy it keeps. The calendar's own name, if it has one, is shown on the card. After that Istota reads the calendar again about every 15 minutes, asking only for changes. A calendar that stops answering is marked Not refreshing with the reason, and tasks keep reading the last copy until it answers again.

Each calendar gets an id, such as `feed:12`. Tasks read it with the calendar skill:

```bash
istota-skill calendar calendars
istota-skill calendar list --calendar feed:12 --week --tz "Europe/Berlin"
```

`calendar list` without `--calendar` includes every subscribed calendar beside any your bot account can read. Istota cannot change a subscribed calendar; a write to one is refused as `read_only_calendar`. Event titles, places and descriptions are somebody else's text and reach tasks marked as untrusted.

Remove a calendar from its row menu. Adding the same address again brings the same calendar back. The operator sets how many calendars each person may add (10 by default) and the largest calendar Istota reads (2 MiB by default).

A refusal when adding names its cause. `not_a_calendar` means the address answered with something else, usually a login page because it is not the secret address. `host_refused` means the address points at a private network address the operator has not allowed. `too_large` means the calendar is over the size limit.

## Istota's calendar

Istota keeps a calendar of its own for the events it puts on your calendar. A task adds one with `istota-skill calendar create --calendar istota`, and moves or cancels it with `update` and `delete` and the event's UID. Events are saved when the task finishes, at most 10 changes per task. Times are stored in UTC.

The card's Send new events by choice decides how the events reach you:

- **Invitations** (the default). Istota emails each event to the first address on your profile as a calendar invitation, from the bot's address. A move sends an update and a cancel sends a cancellation, under the same event, so your calendar app keeps one copy. When you accept or decline, your calendar app replies to the bot; Istota records the answer and starts no task for it. A reply is only recorded when it comes from your own address and your mail server vouches for it (see the operator notes below).
- **Feed**. Choose Get feed address, then subscribe to that address once in your calendar app. The address is shown only that once; Reset address replaces it and the old one stops working at once.

The feed always holds every current event, whichever way you choose, so switching loses nothing. Do not use both: an accepted invitation and the same event in a subscribed feed show as two events in most calendar apps. When the server sends no email, events reach you through the feed only, and the card says so.

On a local install the feed address names this computer, so only calendar apps on it can read the feed.

## Invitations sent to Istota

When someone sends the bot's address a meeting invitation, and the mail is not held by the untrusted-sender gate, Istota records it if the mail is authenticated and comes from the organizer. Tasks read these with `--calendar feed:invitations`. Istota never accepts or declines an invitation itself. Only mail the organizer sends to the bot's address directly (as To, Cc or Bcc) is recorded. A forwarded invitation is not, because its sender is you rather than the organizer, so ask the organizer to include the bot's address.

## Folders shared with Istota

When the operator has turned this on, the card shows your federated sharing address, such as `alice@istota.example.com`. Copy it, then share a folder with it from your own Nextcloud (or any server with federated sharing) the way you share with anyone on another server. Choose edit rights if Istota should be able to write.

The offer arrives as a waiting offer on the card and on the Files page under From other servers, and the bell shows "A folder was shared with you. Review it in Istota." The bell's text never includes the folder's name or the sender's words, and its Review link opens From other servers. Istota can reach nothing in the folder until you accept it. A single file can be shared the same way; Istota treats it as a share of one file.

- **Accept** opens the folder once to check that the share works and to read what it allows. The dialog asks whether scheduled jobs and briefings may use the folder; that is off unless you tick it. When exactly one offer is waiting, the bell's detail view has an Accept as well, with scheduled use off.
- **Decline** tells the sender's server, which removes the share. The owner has to share it again before Istota can reach it.
- An offer nobody answers is declined after 14 days, without telling the sender.

An accepted folder appears on the card with an id, such as `share:7`, and on the Files page under From other servers, in the section named after the bot. That page lists every share you accepted and every offer waiting, with its name, whether it is a file or a folder, who shared it (the display name and federated id the sender gave, and the owner too when someone else shared it), the server it comes from, whether it can be written, and when you accepted it. Open a folder to browse it like any other folder in Files: open its subfolders, see names, sizes and dates, preview a file or download it. A file share opens as its one file. The Files page reads the share only; uploading, new folders and deleting are for tasks. Downloads are limited to 64 MB, the same limit tasks have. Names in a share are the sharer's words, and the page shows them as plain text.

Tasks reach a share through the shares skill:

```bash
istota-skill shares list
istota-skill shares ls share:7
istota-skill shares get share:7:receipts/march.pdf --dest <folder in your files>
istota-skill shares put <file in your files> --to share:7:reports
```

The skill also has `mkdir`, `move` and `delete`; `delete` needs confirmation. A share of one file is listed with `ls share:7` and fetched with `get share:7`, saved under the share's name. File and folder names are the sharer's words and reach tasks marked as untrusted. A share accepted while a task is running is usable from the next task.

What a share allows comes from the sharer's server. Changing a share from read-only to edit, or back, sends Istota no notice; each time a task uses the folder or you open it on the Files page, Istota asks the server and uses the answer. Until then the card shows what it last saw.

Each accepted folder has a credential named `ocm_share_<id>` in Settings > Credentials. Its grant there decides which rooms may use the folder and whether scheduled jobs may. The grant is about tasks: you can always open your own shares on the Files page. You can delete the credential; the folder is then removed the next time a task tries it or you open it. You cannot edit its value.

Ending a share works from either side:

- **Unsharing it on your Nextcloud** tells Istota, which removes the folder and its credential.
- **Remove** in the row menu on the card removes it in Istota and tells the sender's server, which deletes the share.
- A share the sender's server no longer accepts is removed the next time a task tries it or you open it on the Files page.

Accepting is refused, with the reason on the card, on a multi-user server whose tasks run without a sandbox, since a task there could read another user's credentials.

## Who can reach what

Shared calendars and folders belong to the person who added them. They follow the same room rules as every other skill ([Shared rooms](shared-rooms.md)): your own turn in a room reaches them when you ask, a guest's turn and a task nobody asked in a shared room reach none of them. Istota's prompt names your calendars and folders, by id and host only, in your private room. It leaves them out in a shared or group room, where others read the conversation; an email thread counts as your own correspondence and keeps them. Removing your account removes every calendar and folder you shared, with their addresses and credentials.

## Operator notes

The settings are in `config.toml`:

```toml
[sharing]
private_hosts = []              # exact host names allowed a private address, and plain http
ics_poll_interval = 900         # seconds between reads of one calendar; at least 300
ics_max_bytes = 2097152         # largest calendar read
ics_max_feeds_per_user = 10
events_per_task = 10            # changes to Istota's calendar per task; 0 turns them off

[sharing.ocm]
enabled = false                 # receive folder shares on [site] hostname
max_pending_per_user = 20
pending_days = 14
max_discovery_fetches_per_minute = 30
```

### Private hosts

Every address Istota fetches for this feature, a calendar or a sharing server, must resolve to a public address and be reached over https. `[sharing] private_hosts` lists exact host names that may resolve to a private address and may be reached over plain http, for a calendar server or a Nextcloud on your own network. Names only, with no wildcards or ports. A redirect may reach a private address only when it stays on the same listed host, so a public calendar cannot send Istota onto your network.

### Invitations

Invitations are sent through the bot's mail account. Replies are only applied when `[email] authserv_id` is set and your mail server's Authentication-Results show a DMARC pass for the sender. Without it, replies are consumed and never recorded, and `istota doctor` warns under `sharing.ics_feeds` once someone uses invitations. The calendar feed is served at `https://<site hostname>/istota/calendar/<token>.ics`; nginx keeps that path out of the access log, since the path is the token.

### Turning on folder shares

Folder shares use Open Cloud Mesh, served on `[site] hostname`. Federated addresses are `<user_id>@<site hostname>`. Before enabling it:

- The other server must reach `[site] hostname` over https, and Istota must reach the other server. A local install (`localhost`) cannot use it.
- `[site] hostname` must not be your Nextcloud's host name; `load_config` refuses that combination, since that Nextcloud already owns those paths and addresses there.
- The stack's nginx always routes `/ocm-provider/`, `/.well-known/ocm` and `/ocm/` to the web app, which answers 404 there while sharing is off. A proxy in front of the stack must pass those paths through.

`istota doctor --only sharing.ocm` checks the host name rules, then fetches `https://<site hostname>/ocm-provider/` the way a remote server would and fails unless the answer is Istota's.

Two things you may notice afterwards:

- When Istota refuses an offer, Nextcloud tries again on its older sharing endpoint, so the access log shows 404s for `/ocs-provider/` and `/ocs/v2.php/cloud/shares`. That is expected; Istota does not serve those paths.
- Nextcloud remembers what it learned about a server: for 24 hours after a successful check and 5 minutes after a failed one. A fix on Istota's side is seen by a Nextcloud only once that time has passed.

Turning `[sharing.ocm] enabled` off later stops new offers. Accepted folders keep working, but Istota no longer hears when one is unshared; such a share is removed the next time a task tries it. The card says so.

### A Nextcloud on your own network

A Nextcloud that shares with Istota on the same network, as in the test environment, needs a few settings so the two can reach each other:

- `occ config:system:set allow_local_remote_servers --value true --type boolean`, so Nextcloud will talk to a server on a private address.
- Nextcloud must trust the certificate Istota is served with. Its federated sharing calls use the system certificate store, so a private CA goes into the container's store (`update-ca-certificates`); `occ security:certificates:import` is not enough.
- `overwritehost` and `overwrite.cli.url` set to the name Istota reaches Nextcloud by. Nextcloud puts that name in the sender's cloud id (`alice@https://cloud.example.org`), and Istota answers to that name.
- That name in Istota's `[sharing] private_hosts` when it resolves to a private address or is served over plain http.
