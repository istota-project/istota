"""Search the user's location records in their existing module database."""

from istota.lib.date_parse import iso_utc
from istota.lib.text_match import like_predicate, make_snippet
from istota.modules import module_loader
from istota.search.core import Provider, ProviderResult, SearchHit, open_with_deadline
from istota.search.links import route


def search(ctx, terms, mode, limit, offset):
    _, resolve, connect, not_found = module_loader("location")
    columns = ["name", "category", "notes"]
    predicate, params = like_predicate(columns, terms, mode)
    try:
        user = resolve(ctx.user_id, ctx.config)
        if not user.db_path.is_file():
            return ProviderResult([], False)
        with open_with_deadline(lambda: connect(user.db_path), ctx.deadline) as conn:
            rows = conn.execute(
                f"SELECT * FROM places WHERE {predicate} ORDER BY name, id LIMIT ? OFFSET ?",
                [*params, limit + 1, offset],
            ).fetchall()
    except (not_found, FileNotFoundError):
        return ProviderResult([], False)
    hits = []
    for row in rows[:limit]:
        snippet, highlights = make_snippet(" ".join(row[c] or "" for c in columns), terms)
        hits.append(SearchHit(
            id=f"location:{row['id']}", kind="place",
            title=" ".join((row["name"]).split()),
            subtitle=" ".join((row["category"] or "").split()) or None,
            snippet=snippet, highlights=highlights, date=iso_utc(row["created_at"]),
            link=route("/location/", place=row["id"]), badges=[],
        ))
    return ProviderResult(hits, len(rows) > limit)


PROVIDER = Provider("location", "Location", 80, "location", 1.5, False, search)
