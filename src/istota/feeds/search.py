"""Search the user's feed entries using their existing module database."""

from istota.feeds import db
from istota.lib.text_match import MARK_OPEN, fts5_match, markers_to_offsets
from istota.modules import module_loader
from istota.search.core import Provider, ProviderResult, SearchHit, open_with_deadline
from istota.search.links import route


def search_entries(conn, match: str, limit: int, offset: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.*, COALESCE(f.title, f.url) AS feed_title,
               snippet(feed_entries_fts, 2, char(57344), char(57345), '…', 24) AS body_snippet,
               snippet(feed_entries_fts, 0, char(57344), char(57345), '…', 24) AS title_snippet
        FROM feed_entries_fts
        JOIN feed_entries e ON e.id = feed_entries_fts.rowid
        JOIN feeds f ON f.id = e.feed_id
        WHERE feed_entries_fts MATCH ?
        ORDER BY bm25(feed_entries_fts, 2.0, 0.5, 1.0), e.published_at DESC, e.id DESC
        LIMIT ? OFFSET ?
        """,
        (match, limit, offset),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        body = item.pop("body_snippet") or ""
        title = item.pop("title_snippet") or ""
        item["snippet"] = body if MARK_OPEN in body else title or body
        result.append(item)
    return result


def search(ctx, terms, mode, limit, offset):
    _, resolve, connect, not_found = module_loader("feeds")
    try:
        user = resolve(ctx.user_id, ctx.config)
        # The regular module connector creates an empty file when missing.
        if not user.db_path.is_file():
            return ProviderResult([], False)
        with open_with_deadline(lambda: connect(user.db_path), ctx.deadline) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='feed_entries_fts'").fetchone():
                db._migrate_v8_to_v9(conn)
                conn.commit()
            rows = search_entries(conn, fts5_match(terms, mode), limit + 1, offset)
    except (not_found, FileNotFoundError):
        return ProviderResult([], False)
    hits = []
    for row in rows[:limit]:
        snippet, highlights = markers_to_offsets(row["snippet"])
        badges = []
        if row["starred"]:
            badges.append("starred")
        if row["status"] == "unread":
            badges.append("unread")
        hits.append(SearchHit(
            id=f"feeds:{row['id']}", kind="feed_entry",
            title=" ".join((row["title"] or "Untitled entry").split()),
            subtitle=" ".join((row["feed_title"] or "").split()),
            snippet=snippet, highlights=highlights,
            date=row["published_at"] or row["fetched_at"],
            link=route("/feeds/", entry=row["id"]), badges=badges,
        ))
    return ProviderResult(hits, len(rows) > limit)


PROVIDER = Provider("feeds", "Feeds", 60, "feeds", 2.0, False, search)
