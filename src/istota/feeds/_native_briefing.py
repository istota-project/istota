"""Native-backend briefing reader.

Mirror of :func:`istota.feeds._miniflux.fetch_miniflux_entries` for the
``[feeds] backend = "native"`` path. Reads recent entries directly from
the per-user workspace SQLite and returns the same ``FeedItem`` shape so
the briefing skill (and any other consumer) is backend-agnostic.

Public entry point: :func:`fetch_native_entries`. The dispatcher in
:mod:`istota.feeds` picks between this and ``fetch_miniflux_entries``
based on the istota config flag.
"""

from __future__ import annotations

from istota.feeds import db as feeds_db
from istota.feeds._loader import resolve_for_user
from istota.feeds._miniflux import (
    FeedItem,
    _extract_image_from_content,
)


def fetch_native_entries(user_id: str, istota_config, limit: int = 500) -> list[FeedItem]:
    """Return recent entries for ``user_id`` from the native feeds DB.

    Output matches :class:`FeedItem` so callers don't care which backend
    produced the rows.
    """
    ctx = resolve_for_user(user_id, istota_config)
    feeds_db.init_db(ctx.db_path)

    with feeds_db.connect(ctx.db_path) as conn:
        feeds = {f.id: f for f in feeds_db.list_feeds(conn)}
        entries = feeds_db.list_entries(
            conn,
            limit=limit,
            order="published_at",
            direction="desc",
        )

    items: list[FeedItem] = []
    for e in entries:
        feed = feeds.get(e.feed_id)
        feed_name = (feed.title or feed.url) if feed else ""
        image_url = e.image_urls[0] if e.image_urls else _extract_image_from_content(
            e.content_html,
        )
        items.append(FeedItem(
            id=e.id,
            user_id=user_id,
            feed_name=feed_name,
            item_id=str(e.id),
            title=e.title,
            url=e.url,
            content_text=e.content_text,
            content_html=e.content_html,
            image_url=image_url,
            author=e.author,
            published_at=e.published_at,
            fetched_at=e.fetched_at,
        ))
    return items
