# Servarr

The `servarr` skill lets people request films, shows and albums, and ask what the download stack is doing, from chat. It talks to the operator's Sonarr (shows), Radarr (films), Lidarr (artists and albums) and Prowlarr (indexers) over their own APIs. "Get me *Paris, Texas*", "what's stuck?" and "why hasn't the new album come down?" are the requests it is built for.

There is one connection per app for the whole install, set by the operator. Users configure nothing. Admins get every verb; users the operator lists as requesters may search, request with the stack's own defaults, and follow the queue and their requests. Everyone else gets nothing.

The skill is a menu skill with a CLI (`istota-skill servarr`). It appears only on an install with at least one app configured.

## What the operator sets

```toml
[servarr]
requesters = ["bob"]              # user ids besides admins who may request

[private_hosts]
"192.168.1.10" = ["servarr"]  # allowed a private address and plain HTTP

[servarr.apps.sonarr]
url = "http://192.168.1.10:8989"

[servarr.apps.radarr]
url = "http://192.168.1.10:7878"

[servarr.apps.lidarr]
url = "http://192.168.1.10:8686"

[servarr.apps.prowlarr]
url = "http://192.168.1.10:9696"

[servarr.defaults.sonarr]
quality_profile = "HD - 720p/1080p"
root_folder = "/data/media/shows"

[servarr.defaults.radarr]
quality_profile = "HD - 720p/1080p"
root_folder = "/data/media/films"
minimum_availability = "released"

[servarr.defaults.lidarr]
quality_profile = "Standard"
metadata_profile = "Standard"
root_folder = "/data/media/music"
```

Each app is a `[servarr.apps.<app>]` table with the app's URL. Its API key, from the app's Settings > General > Security, is a deployment credential: the stack secret file `istota_servarr_<app>_api_key` (`istota_servarr_radarr_api_key` and so on), which the container exports as `ISTOTA_SERVARR_<APP>_API_KEY`. That variable wins over an `api_key` written in the table, which still loads but does not belong there. A table with a URL and no key anywhere is reported and left out until the key arrives. An app with no table is not configured, and a verb that needs it answers `app_not_configured`. Configure only the apps the stack runs. Bazarr is not supported; its name is reserved.

**`requesters`** lists the users, besides admins, who get the requester tier. Admins never need listing. An empty list leaves the skill to admins. Matching ignores case, and a listed id that is not a configured user is warned about at load.

**[`[private_hosts]`](../configuration/reference.md#private_hosts)** grants the exact host names or IP addresses an app may resolve to a private or loopback address, as `"<host>" = ["servarr"]`. Without a grant every app must be on a public address over HTTPS. Plain HTTP is allowed only to a granted host, since an arr stack on a home or office network usually has no certificate. If the host is already in the table for another consumer, add `"servarr"` to its existing list: a second line for the same host is a TOML error.

**`[servarr.defaults.<app>]`** is what an add uses when nobody names a profile or a root folder, which is always the case for a requester:

| Field | Apps | Meaning |
|---|---|---|
| `quality_profile` | sonarr, radarr, lidarr | quality profile name, matched exactly and then ignoring case |
| `root_folder` | sonarr, radarr, lidarr | root folder path, matched exactly |
| `metadata_profile` | lidarr | metadata profile name |
| `minimum_availability` | radarr | `tba`, `announced`, `inCinemas` or `released` (default `released`) |
| `monitor` | sonarr, lidarr | sonarr: which episodes a new show monitors (default `all`); lidarr: which albums a whole-artist add monitors (default `all`) |

`minimum_availability` has no command-line flag: it comes from this table or the default. Radarr takes no `monitor`, because a film is always added monitored. A field that does not apply to its app is warned about and ignored.

For each of the profile, root folder and metadata profile, an add takes the flag (admins only), then the configured default, then the app's only option if it has exactly one. A configured name the app no longer has is `stale_default`, naming the config key and what the app does have; the skill does not quietly fall back, because that would hide drift between the stack and this config. With no default and several options the answer is `choice_required`, and a requester is told that the operator has to set a default. `istota-skill servarr describe` shows each app's profiles and root folders and what an add would use.

Two timeouts: `lookup_timeout_seconds` (30) for the lookups that reach TMDB, TVDB or MusicBrainz through the app, and `release_search_timeout_seconds` (120) for an interactive release search or an indexer test, which reach every indexer. A run finishes 30 seconds before the skill proxy's own timeout for `servarr` (300 seconds by default) so it can still answer; the loader warns when the release timeout leaves less than 60 seconds of that.

The reference for every key is [`[servarr]`](../configuration/reference.md#servarr).

### The API keys

An arr API key is full admin on that app, so it is a secret file like every other deployment credential, never part of `config.toml` or a settings plan. Set it one of three ways:

- the app's API key field under `[servarr]` on Admin > Configuration, which hands it to the host applier to write;
- on the VM, by writing `/srv/app/istota/secrets/istota_servarr_<app>_api_key` and restarting, as the [deployment credential procedure](../configuration/credentials.md#provisioning-global-credentials) describes;
- with the management role, in `istota_secrets` beside the other files:

```yaml
istota_secrets:
  istota_servarr_radarr_api_key: "{{ vault_radarr_api_key }}"
  istota_servarr_sonarr_api_key: "{{ vault_sonarr_api_key }}"
```

The rest of `[servarr]` goes in the plan like any other config table.

### Checking the connection

Admin > Connections has a Servarr card. It lists each configured app with its URL, whether a key is set, and the result of the daemon's last `servarr.connection` check, which asks each app for its version with its key. A table missing its URL or its key, a URL with a user name or password in it, and plain HTTP to a host not granted to `servarr` in `[private_hosts]` show as setup incomplete. Check now runs the check again. `istota doctor --only servarr.connection` gives the same results from a terminal. The web interface never calls an arr app and never shows a key. The card only shows status: Configure, and Add apps on an install with none, open the `[servarr]` section of Admin > Configuration.

The `[servarr]` section of Admin > Configuration has a block for each app: whether it is connected, its URL and its API key, with a badge saying where the key is held. The requesters and the add defaults are ordinary fields in the same section. When a URL needs a grant in `[private_hosts]` (plain HTTP, or a private IP address), the block says so as you type and says what to write in `private_hosts` under General: `"<host>" = ["servarr"]`. That edit needs the typed confirmation. The block cannot read a table edit that is still pending, so it stops judging the host while one is open, and the table edit and a new app on that host can go in one change. The key field starts empty and an empty field keeps the stored key. A new key goes to its secret file, and turning an app off clears that file as well as removing the table. Where the secret files are managed by a deployment role, the key shows whether it is set and cannot be changed here. The change goes through the page's review, confirmation and host applier, and Istota restarts to load it. The section is read-only on an install with no config applier, and where a plan manages `config.toml` (an Ansible-managed install). Ownership is recorded for the whole of `config.toml`, not per section, so on a managed install `[servarr]` is changed in the settings plan and the keys in its secret files.

## Network

The skill's CLI runs host-side, outside the task's sandbox, and connects to the apps from the daemon's network. **Do not add the arr ports to `[security.network] extra_hosts`.** Without an entry the sandbox has no route to the apps, so the skill's verbs are the only way a task reaches them. With a route and a copy of a key, the tier table stops being the limit.

Every request goes to the configured URL and nowhere else. No argument the model passes becomes a host, a URL or a path segment; ids are checked as whole numbers or MusicBrainz UUIDs first. Redirects are refused rather than followed, so the key is never sent anywhere else. The key travels in the `X-Api-Key` header only, never in a query string, an argument or an answer.

If the stack runs on another machine behind a firewall, that firewall has to admit the istota host. On a Docker host with a `DOCKER-USER` allowlist, published ports drop everything not on the list, and a dropped connection hangs rather than failing fast. The skill reports that as `connection_failed`, names the firewall as a likely cause, and sends nothing more to that app for the rest of the call. Add the address istota's traffic leaves from (on a Docker install, the host's own address, after NAT) to the allowlist.

## Who may use it

| Tier | Who | May use |
|---|---|---|
| admin | listed in the [admins file](../configuration/overview.md#admin-users) | every verb and flag |
| requester | listed in `requesters` | `apps`, `status`, `search`, `add` for a film, show or album with the stack's defaults (`--monitor` and `--no-search` allowed; `--profile`, `--root`, `--metadata-profile` not), `queue`, `why` with or without `--releases`, `requests`, and `search-now` for their own requests |
| none | everyone else | nothing |

Admin-only: `describe`, `wanted`, `indexers` (with or without `--test`), adding a whole artist, the three add overrides, and `search-now` for an item someone else requested. A refused call sends nothing to any app, and the answer names what was refused (`not_authorized`, `verb_requires_admin`, or `flag_requires_admin` with the flag), so the model can say it plainly.

The room narrows the tier:

- A turn with any scope withheld gets nothing: a guest's turn in a [shared room](shared-rooms.md#what-a-task-can-reach), or a task nobody asked for there. The skill is also `shared_room: private`, so a guest's turn never has it.
- An email thread gets the requester tier at most, and `--confirmed` is refused there whatever the tier (`confirmation_not_allowed_here`). An admitted correspondent's mail runs as the host, so "the user agreed" could be the correspondent agreeing.

An operator's `disabled_skills`, or a per-user disable, refuses the CLI too.

**The keys never reach a user.** The daemon reads them from its own environment and hands them to the servarr CLI alone, the way it hands the model credentials to the code reviewer: never to the task's environment, never to any other skill's CLI, and never through the credential lookup the sandbox can ask. No user's vault holds them, and the skill declares no environment variable for them, so `istota-credential` has nothing servarr-related to list or serve.

**Where these claims hold.** All of the above holds on the supported shape: Linux, bubblewrap and the skill proxy on. With the skill proxy off, a skill CLI runs in the model's own environment, where the user id can be set and the config file can be read; the tier table is then advisory, as every skill CLI's checks are on that shape.

## What it does

`apps` sends nothing and reports the caller's tier, the configured apps and what the tier allows. `status` is each app's version and health checks. The skill expects Sonarr 4, Radarr 5, Lidarr 2 and Prowlarr 1 or later, and refuses an older app with `unsupported_version`.

**Requests.** `search` looks a title up and says for each result whether it is already in the library. `add` takes the id `search` returned (TMDB for films, TVDB for shows, MusicBrainz for artists and albums), never a title, and the model asks the user when several results could be the one they meant. A monitored item already in the library is not changed: the answer is `already_present` with who requested it. A film or show in the library but unmonitored is how the stack holds something nobody has asked for yet, so `add` requests it in place: Radarr's movie editor monitors the film and adds the caller's tag in one write. For a show, Sonarr's series editor adds the tag first (and, for `--monitor all` or `future`, sets new seasons to be monitored), then the season pass applies the monitor mode to the episodes. A search follows unless `--no-search`. The tag goes on before the monitor because a monitored item answers `already_present` on a repeat: if the season pass fails, the show is still unmonitored and the same add finishes the request. `--profile` and `--root` are refused for such an item, since a request keeps its own settings. The answer is `already_present` with `monitored_now: true`, and the item then shows in the caller's `requests` and can take their `search-now`. `--monitor none` is refused for such a show, since it would leave it unmonitored. Shows take `--monitor` (`all`, `future`, `missing`, `existing`, `firstSeason`, `lastSeason`, `pilot`, `none`); `--monitor` is refused for films, albums and artists.

Every item added through the skill carries a `req-<user>` tag, and `requests` lists a user's tagged items. A monitored item already in the library is never retagged, not even with the requester's own tag; an unmonitored film or show gets the requester's tag when they ask for it.

An album search by title runs Lidarr's own search (`search?term=`, as its web UI does) and returns the album rows whoever they are by, each with its secondary types and release count, so a Various Artists soundtrack is found by its title even when a band has the same name. `--artist NAME` lists one artist's albums instead. Lidarr's `album/lookup` is not used for a title: it leaves out albums the search finds.

An album the library does not hold is added with `POST album`, the way Lidarr's UI adds one from search. Under an artist already in the library it goes in whether or not that artist's album list includes it, which matters for Various Artists (Lidarr never lists the whole catalogue) and for release types the metadata profile leaves out, such as soundtracks under the default Standard profile. When the artist is new, the same request adds it, tagged, with `addOptions.monitor: none` and `albumsToMonitor` naming this album, so no other album is monitored. Lidarr's Wanted list, which downloaders such as Soularr poll, holds an album only when its artist is monitored as well, and the refresh after the add stores the new artist unmonitored whatever the request body says. An `artist/editor` write sent while that refresh runs is undone, so the add polls `GET command` until the artist's `RefreshArtist` command finishes (up to 60 seconds), then monitors the artist with new albums off and searches for the album. A failure after the album was added answers `partial: true` with what exists now, so the model does not repeat the add blind; a refresh that does not finish in time answers `artist_refreshing`, and the same add finishes the request. That repeat, and any add of an album whose artist is in the library, first checks for a refresh of the artist still running and waits it out. An album already in the library is monitored and searched in place. If that artist is not monitored, the add monitors it and leaves its new-albums setting alone; any of its albums already monitored become wanted with it, and the answer's `note` says so. That album carries no tag, because tags live on the artist and the artist is not retagged; it does not show in `requests`, and a requester cannot `search-now` it.

Adding a whole artist is admin-only and asks first: without `--confirmed` it answers `confirmation_required` with a line naming the artist and how many albums it has, and the model passes `--confirmed` only after the user agrees in the conversation. Film, show and album adds are not gated: the request is the user's own, and an add can be undone in the app.

**Activity.** `queue` merges the apps' download queues, problems first (an import blocked or failed, a warning, an import pending for more than an hour after the download was added). `--mine` keeps the caller's requests. Each app's queue is read 200 rows at a time; a longer one is reported as `truncated`. `why` explains one item from its state, its queue rows and its recent history, with a plain `hint`. `why --releases` runs an interactive search across every indexer and lists each release with the reasons it was rejected. That search is slow and counts against indexer API limits, and the skill never grabs a release from it. `search-now` asks an app to search for one item; a requester may do this for their own requests only, and an item that is not monitored is refused. `wanted` (admins) lists monitored items still missing, or below their quality cutoff.

**Indexers.** `indexers` (admins) lists Prowlarr's indexers, each `ok`, `disabled_by_user` or `failing` until a given time, with Prowlarr's health checks. `--test` tests every indexer and reports which failed and why. Indexers that sit behind a Cloudflare challenge solver commonly fail the test when the challenge changes.

A call that visits several apps answers for each separately: one app down is an error entry for that app, and the others still answer. A call that runs out of time stops and names the apps it did not reach.

Writes are sent once. A write that ends ambiguously (the connection dropped after the request went out) answers `outcome_unknown` with a lookup command to check whether it applied.

Text from the apps (titles, overviews, release and indexer names, health and error messages) is returned inside untrusted-content markers.

## Requesting from a command

`!arr` runs `search` and `add` directly, with no model turn. `!arr search film Paris, Texas` (or just `!arr film Paris, Texas`) lists the matches, each with its id and whether it is already in the library. `!arr add film 655`, `!arr add show 70533 lastSeason` or `!arr add album <musicbrainz-id>` then requests one. A result can also be named by its number in the listing: `!arr add album 2` adds the second result of your last search in the same room, and `!arr add show 2 lastSeason` takes a monitor mode as before. The number must match the kind you last searched for, and a search in another room or by another person does not count. For films and shows, a number with no search behind it is read as a TMDB or TVDB id. `movie`, `tv` and `series` work as kinds too, and a show's monitor mode can be typed in any case. `!arr add` with a title instead of an id runs the search, since nothing is added by title.

The command runs the skill's own verbs through the same access table, so the tiers above apply: a requester adds with the stack's defaults and their own tag, and a user with no tier is refused before anything is sent. Adding a whole artist is not offered from the command. A command is the sender's own turn, so nothing is withheld in a shared room; the reply is posted in the room the command was typed in. A command run gets 45 seconds, less than the CLI, so a new artist's album add can answer `artist_refreshing`; the same command finishes it.

## Not offered

- Deleting anything: media, files, queue items, blocklist entries. Nothing in the skill sends a `DELETE`.
- Changing an app's settings, an item's profile or monitoring after it was added, or Prowlarr's indexers.
- Grabbing a specific release over the app's own choice.
- Talking to the download client (qBittorrent, SABnzbd, slskd) directly. The arrs' queues show what matters, including import state.
- Bazarr and subtitles.
- Per-user connections to a user's own stack, and more than one instance of an app.

## Testing against a live stack

The default test suite drives the skill against a fake transport with responses built from the arr API documentation, which proves request shape and parsing only. Whether a real arr accepts the add bodies, the per-item history paths, the exists error codes and Prowlarr's indexer status and test shapes has to be checked once against a running stack: a film, a show, an album with a new artist, an album for an existing artist, a gated artist add (each removed in the app afterwards), `queue --problems` against the app's Activity page, and `indexers` against Prowlarr's own list.
