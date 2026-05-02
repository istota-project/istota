"""Feed source providers.

Each provider exposes ``fetch(identifier, **kwargs) -> list[FetchedItem]``.
The poller dispatches to one of these based on ``feed.source_type``:

* ``rss``    — handled in :mod:`istota.feeds.poller` via ``feedparser``
* ``tumblr`` — :mod:`istota.feeds.providers.tumblr`
* ``arena``  — :mod:`istota.feeds.providers.arena`

Both Tumblr and Are.na providers are vendored from
``~/Repos/rss-bridger/src/rss_bridger/providers/`` — the bridger repo's
sole valuable parts. The bridger's FastAPI/Atom/cache layers are not
vendored; native polling calls these providers directly.
"""

from istota.feeds.providers import arena, tumblr

__all__ = ["arena", "tumblr"]
