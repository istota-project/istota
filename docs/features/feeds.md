# Feeds

A native feed reader — RSS, Atom, Tumblr, and Are.na — with its own web tab and per-user SQLite store. There is no external service; the poller, the store, and the reader are all in-tree (`src/istota/feeds/`).

The `feeds` module is on by default. Opt out per user with `disabled_modules = ["feeds"]`.

## Reading

The **Feeds** tab is a masonry card grid with an image/text filter, sort by published or added, grid and list views, a navigable image lightbox, and a click-to-expand reader overlay showing a card's full un-clipped content with `←`/`→` navigation between posts.

The sidebar scopes the view to everything, unread only, a single feed, or a whole category. Per-entry starring is bound to `f`; bulk mark-as-read (`Shift-A`) honours whatever scope is active rather than clearing the whole account. Entries mark themselves read after 1.5 seconds in the viewport.

Video embedded in a post plays inline with ordinary controls. Nothing autoplays — a grid of cards all starting at once is not what scrolling a reader asks for — and the image/text filter hides inline video along with pictures, since a filter that left clips playing would only mean "some of the media". Embeds resolve through a host allowlist (YouTube, youtube-nocookie, Vimeo) rather than passing a provider's own iframe HTML through the sanitizer.

### Repeat images

As a reblogged photo travels through the blogs you follow, the same picture arrives several times. A duplicate inside one post is dropped, and across posts an image a newer entry already showed is hidden on the older ones — the post still appears, with a note counting what was hidden.

This is a display-time decision, never a row mutation, and it is bounded two ways: to a look-back window (`image_dedupe_window_days`, default 14, 0 = off) and to the slice you are currently viewing. An image resurfacing months later still shows, and browsing one blog never hides a tile because of something in another.

## Subscriptions

Manage subscriptions, categories, and OPML import/export from the sprocket-icon settings page, or from the skill CLI:

```bash
istota-skill feeds list                      # subscribed feeds
istota-skill feeds categories                # categories
istota-skill feeds entries                   # entries
istota-skill feeds add URL                   # subscribe
istota-skill feeds remove ID                 # unsubscribe
istota-skill feeds refresh                   # mark feeds due for the next poll
istota-skill feeds poll                      # poll everything due, now
istota-skill feeds import-opml PATH
istota-skill feeds export-opml
```

`run-scheduled` is the scheduler's entry point, not something to run by hand.

Are.na runs on the v3 API. Six block types have typed builders (`Text`, `Image`, `Link`, `Embed`, `Attachment`, `Channel`) over a generic fallback, so a type Are.na adds later still renders rather than breaking the poll.

## Storage

Per-user `feeds.db` at `Config.module_db_path(user_id, "feeds")`, with tables `feed_categories`, `feeds`, `feed_entries`, `entry_images`, and `schema_meta`. Settings live as rows keyed under `feeds_settings.*` — `default_poll_interval_minutes` and `image_dedupe_window_days`.

A poll **refreshes** an entry it already holds rather than discarding it, so a provider-side fix reaches entries already on file. User state (read status, starred, starred timestamp) is never overwritten, and a field is only replaced by a non-empty value, so a sparser re-fetch cannot blank a title you already have. The "N new" count still counts only genuinely new inserts.

## Related

- [Briefings](briefings.md) — an `rss` briefing source reads from the same subscriptions.
- [Web interface](web-interface.md) — where the Feeds tab sits in the nav.
