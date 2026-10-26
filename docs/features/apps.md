# Apps

Apps are small single-page tools that run in the web interface, get an entry in the Apps section, and reach Istota's data through a narrow, declared bridge. An app is a folder in your workspace with a `manifest.toml` and an `index.html`, written by you or by Istota. Unlike a module or a skill, an app is code Istota did not write and you did not review line by line, so it runs inside a sandbox and reaches nothing it did not ask for.

Apps is an experimental module. Enable it by adding `module_apps` to `[experimental] features` in `config.toml`; until then the Apps section is not shown. A user can also disable it for themselves in Settings.

## The Apps section

Apps sits in the top nav after Files. Its sidebar lists your installed apps; the main pane shows installed apps, any install waiting for your approval, and the app folders in your workspace that are not installed or whose files have changed. Open an app from the sidebar or its card.

On a phone the sidebar becomes a drawer. An app is judged against the pane it gets, not the device, so a desktop app in a narrow split view is treated as narrow.

## Installing an app

A draft is an app folder at `{bot_dir}/apps/<id>/` in your workspace. Istota can write it, and you can write it, but a draft never runs.

Select **Review install** on a draft in "All apps". Istota builds the folder into one document, records it as a pending install, and opens the approval screen. The screen shows, in order:

- the capabilities the app requests, with the ones that are new against the current version marked;
- the narrowest pane it says it needs, and why, when that is not a phone;
- the size of the built document;
- the validator's warnings, such as `alert()` in a phone-range app or a fixed width too wide for a phone;
- a diff of the source files against the current version, or the full source for a first install.

**Approve** makes it the next version and current. A version that requests nothing beyond the current version's grants is approved with one button. A version that requests a new capability shows the button after the capability list, and asks you to confirm that list, so an approval made on a screen that has since gone stale is refused.

While a pending install waits, later edits to the folder change nothing: the pending install is a copy Istota built and holds.

## Permissions

Every app is granted two capabilities, and the approval screen lists them without asking:

| Capability | What it gives an app |
| --- | --- |
| `kv:own` | Its own key-value namespace, `app.<id>` |
| `files:own` | Its own folder, `{bot_dir}/apps/<id>/data/` |

An app may request more, and you are asked for each:

| Capability | What it gives an app |
| --- | --- |
| `kv:read:<namespace>` | Read one of your other key-value namespaces |
| `files:read:<folder>` | Read a folder of your files |
| `calendar:read`, `calendar:read:<name>` | Read your calendars, or one by name |
| `skill:<skill>.<operation>` | One read-only operation from a fixed list |

The read capabilities have limits of their own. `kv:read` never reaches a reserved namespace or another app's `app.` namespace. `files:read` never reaches your `{bot_dir}` folder, `memories`, or a hidden folder, and follows no link inside the folder it was given. `calendar:read` reads only calendars you own, from `from` to `to` (dates, or dates and times in your time zone, at most 366 days apart); a calendar named in the capability is looked up by its display name each time, so a renamed calendar stops being readable. Every check is made on the server, against the permission you granted, on each call.

An app reads them through the kit: `istota.kv.read(namespace, key)` and `istota.kv.readList(namespace, {prefix})`, `istota.files.listShared(folder, path)` and `istota.files.readShared(folder, path, {encoding})`, and `istota.calendar.list({from, to, calendar})`, where `calendar` names the calendar in a `calendar:read:<name>` capability and is left out for `calendar:read`. The namespace and folder are the ones the manifest names; for a folder you changed when granting it, the app still names its own and reads the one you chose.

The `skill:` operations are these six, and no others:

| Capability | Arguments | Answers |
| --- | --- | --- |
| `skill:markets.quote` | `symbols`, a list of 1 to 20 market symbols | `{quotes: [{symbol, name, price, change, change_percent, timestamp}]}`; a symbol that fails to fetch is left out |
| `skill:health.trend` | `name` (a biomarker or one of its aliases), optional `since` and `until` as `YYYY-MM-DD` | `{name, display_name, unit, points: [{drawn_at, value, unit, flag}]}`, confirmed panels only |
| `skill:location.current` | none | `{last_ping, current_visit}` |
| `skill:location.places` | none | `{places: [{id, name, lat, lon, radius_meters, category, notes}]}` |
| `skill:memory_search.facts` | optional `subject` and `predicate` | `{facts: [...], truncated}`, the facts Istota holds as current |
| `skill:memory_search.timeline` | `subject` | `{subject, facts: [...], truncated}`, every fact about it, past ones included |

An app calls one with `istota.skill(operation, args)`, such as `istota.skill('markets.quote', {symbols: ['AAPL']})`. An argument the operation does not take is refused. `health.trend` answers `not_found` when Health is off for you, and both `location.` operations when Location is; any of the six does when you or the operator turned its skill off. A health capability is shown under a warning on the approval screen.

**Activity** in an app's menu lists its last 200 calls, with the capability, the operation and whether it was answered, and the errors its current version reported. A call's arguments are never stored, so Activity says what an app asked for, not which keys or files.

There are no write capabilities on anything outside an app's own storage: an app cannot post to a room, send email, or write to your calendar, and it cannot ask Istota to do any of those things on its behalf.

Data an app writes into its namespace is treated as untrusted when Istota reads it back, the same as anything else written by something other than you.

## Versions and rollback

Every approved install is a version, kept until you delete it. **Versions** in an app's menu lists them with their dates and capabilities. **Make current** makes an older version current again, with the permissions it was approved for; an app's frame that is still open on the version you replaced is told to reload on its next call.

Each app's capabilities, its storage use, and an uninstall control are in Settings → Apps.

## Removing an app

**Uninstall** removes the app. Its key-value data and its `data/` folder are kept unless you check "also delete data"; its folder in your workspace is never touched. An app you uninstall stays in your workspace as a draft, so you can install it again.

## What an app cannot do

An app cannot reach the network: its document has no subresources, and the browser policy it runs under allows no request. It cannot open a page or download a file; to hand you a file it writes it to its own folder, which you open from Files. Copying to the clipboard, showing a dialog, and telling Istota about an error go through the shell, not the browser. It cannot use the camera, the microphone, your location, or your session.

An app runs only while its page is open. There are no background jobs, no scheduled apps, and no offline apps: without the server the frame does not load. In the iOS app an app gets the phone layout, and no native capability reaches it.

## Starter apps

Some apps ship with Istota and are installed for every user who has Apps enabled, the first time they open the Apps section. They run under the same sandbox and permissions as any other app. A starter app that asks for more than its own storage asks on first open, marked "Shipped with Istota". If you uninstall one, it is not installed again and a new release does not bring it back; **Reinstall** on its card in "All apps" does. When a release changes a starter app, it is installed as the next version, and any capability the new version adds is asked for on next open.

### Board

Cards in columns, kept next to your conversations. Ask Istota in chat to "put the three apartment options on the board with rent and commute", and it writes the cards; you drag them between columns, strike them out, colour them and add notes; later, "which one did I end up favouring, and why?" has Istota read the board back.

A card opens for editing when you tap or click it, and its **⋯** menu moves it up or down, to the top or bottom, or to another column, strikes it out, or deletes it. With a mouse you can drag a card from anywhere on it. On a touch screen, press and hold the grip at the card's left edge, then drag; a swipe anywhere else scrolls. On a phone the columns become one list with a column picker above it: a drag reorders the list, and the **⋯** menu moves a card to another column. A column's **⋯** menu renames it, moves it left or right, or deletes it once it is empty.

Board needs only its own storage (`kv:own`), so it opens without asking anything.

#### The `app.board` data layout

Board keeps everything in the key-value namespace `app.board`, and Istota writes the same keys through the `kv` skill:

| Key | Value |
| --- | --- |
| `columns` | `[{"id": "...", "title": "..."}]`, in display order |
| `card:<id>` | `{"id", "column", "order", "title", "body", "struck", "color", "updated_at"}` |

- `id` in a card is the part of the key after `card:`; the key wins if the two differ.
- `column` is a column's `id`. A card whose column does not exist, or that has none, is shown in an "Unsorted" column until it is moved.
- `order` is a number; cards sort by it within a column, lowest first. A card with no `order` sorts after the ordered ones, oldest `updated_at` first. Moving a card gives it an order between its new neighbours, so one move usually rewrites one key.
- `title` and `body` are plain text. `struck` is `true` or `false`. `color` is one of `sky`, `coral`, `teal`, `plum` or `citron`, or `null`.
- `updated_at` is an ISO 8601 time, set by Board on every change.
- Only `title` is needed to add a card; Board fills in the rest when it next writes that card, and keeps any other fields a card carries.

When Istota reads these values back, they are fenced as untrusted, since the text in them may have been written by the app's code rather than typed by you.

### Ledger of guesses

Write down a prediction with a probability and the day it resolves. Once that day comes, Ledger lists the guess under "Ready to resolve" with **Happened** and **Didn't happen**. Each resolved guess goes into your Brier score and a calibration chart, which plots how often your guesses at each probability came true against the diagonal a perfectly calibrated forecaster would follow. A table under the chart gives the same numbers. In chat, "log that as a guess at 60%, resolving at the end of the month" has Istota add the guess for you.

**Add guess** asks for the guess, a probability in percent and the date it resolves. A guess's **⋯** menu records or changes its outcome, reopens it, edits it or deletes it.

Ledger needs only its own storage (`kv:own`), so it opens without asking anything.

#### The `app.ledger` data layout

Ledger keeps one key per guess in the key-value namespace `app.ledger`, and Istota writes the same keys through the `kv` skill:

| Key | Value |
| --- | --- |
| `guess:<id>` | `{"id", "text", "p", "resolves_on", "outcome", "created_at", "resolved_at"}` |

- `id` is the part of the key after `guess:`; the key wins if the two differ.
- `text` is the prediction, as plain text.
- `p` is the probability as a fraction from 0 to 1. A number above 1 and up to 100 is read as a percentage, so `60` means 60%.
- `resolves_on` is a date, `YYYY-MM-DD`. A guess is ready to resolve from that day on, in your own time zone.
- `outcome` is `true` (it happened), `false` (it did not), or `null` while it is open.
- `created_at` and `resolved_at` are ISO 8601 times; `resolved_at` is `null` while the guess is open.
- Only `text` is needed to add a guess. A guess with no `p` is listed but left out of the score, and one with no `resolves_on` is never ready to resolve until it gets one. Other fields a guess carries are kept.

The Brier score is the mean of `(p - outcome)²` over resolved guesses that have a probability, with `outcome` counted as 1 or 0. Lower is better: 0 is perfect, and saying 50% every time scores 0.25. The calibration bins are tenths of probability (0–9%, 10–19%, up to 90–100%). For each bin the chart plots the bin's average probability against the share of its guesses that came true.

As with Board, the values are fenced as untrusted when Istota reads them back.

### Recall

Spaced-repetition review over your own notes. In chat, "make cards from my aviation weather notes" has Istota read the notes and write a card for each thing worth remembering. Recall shows the cards due today one at a time: the question first, then **Show answer**, then how well you remembered it (**Again**, **Hard**, **Good** or **Easy**, each with the interval it would give). When you answer **Again**, Recall shows the paragraph of the note the card came from before moving on. Once the day's cards are done it says when the next one is due. **Add card** writes a card by hand, and a card's **⋯** menu starts its schedule over or deletes it.

Recall asks on first open to read a folder of your files, `notes` unless you change it on that screen. It reads only that folder, and only to show a missed card's paragraph; the server checks every read against the folder you granted, not the one the manifest names. Choose **Not now** and Recall still reviews, but cannot show the paragraph until you allow it from **Permissions**.

#### The `app.recall` data layout

| Key | Value |
| --- | --- |
| `card:<id>` | `{"id", "q", "a", "source_path", "source_anchor", "created_at"}` |
| `review:<id>` | `{"due", "interval", "ease", "reps", "lapses", "history"}` |

- `id` is the part of the key after `card:` or `review:`; a card and its review share it, and the key wins if the value says otherwise.
- `q` and `a` are the question and answer as plain text. Only `q` is needed.
- `source_path` is the note's path in your files, such as `notes/aviation/weather.md`. Recall reads it inside the folder it was granted, trying the path as written and then without each leading folder in turn, so `notes/aviation/weather.md` is found as `aviation/weather.md` when `notes` is the folder granted. A path with an empty, `.`, `..` or hidden part is not read.
- `source_anchor` is a heading or a phrase from the paragraph, matched without regard to case, spacing or `#` marks. The first paragraph holding it is shown; a heading is shown with the paragraph under it. With no anchor, Recall names the note and shows nothing from it.
- `created_at` is an ISO 8601 time. New cards are reviewed oldest first, after the cards already due.
- Istota writes cards and leaves `review:` keys to Recall. A card with no review is new and is due today.

The review state is SM-2. A grade of 3 or more (Hard, Good, Easy are 3, 4 and 5) passes: the first interval is one day, the second six, and each after that is the last interval times the card's ease. Again is 1: the card starts over at one day and `lapses` goes up. The ease starts at 2.5, moves by `0.1 - (5 - q)(0.08 + (5 - q) × 0.02)` on every answer and never drops below 1.3. `due` is a date, `YYYY-MM-DD`, in your own time zone; `history` holds the newest 100 answers as `{"on", "grade", "interval"}`.

As with Board, the values are fenced as untrusted when Istota reads them back.

## The house kit

Every installed app gets Istota's tokens, a small client library and a set of components (list, table, form, button, dialog, chart). The kit applies your theme and text size before the first paint, keeps forms usable with the on-screen keyboard, and follows the same touch-target, type-size and layout rules as the rest of the interface. An app that does not use the kit still gets its sandbox and its permissions; the kit is what makes it look and feel like the rest of Istota.
