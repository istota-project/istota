"""Search the user's briefings records in their existing module database."""

from istota.lib.date_parse import iso_utc
from istota.lib.text_match import like_predicate, make_snippet
from istota.modules import module_loader
from istota.search.core import Provider, ProviderResult, SearchHit, open_with_deadline, resolve_module_user
from istota.search.links import route


def search(ctx, terms, mode, limit, offset):
    _, resolve, connect, not_found = module_loader("briefings")
    columns = ["subject", "body_md"]
    predicate, params = like_predicate(columns, terms, mode)
    try:
        user = resolve_module_user(ctx, resolve)
        if not user.db_path.is_file():
            return ProviderResult([], False)
        with open_with_deadline(lambda **options: connect(user.db_path, **options), ctx.deadline) as conn:
            rows = conn.execute(
                f"SELECT * FROM briefing_archive WHERE {predicate} ORDER BY generated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit + 1, offset],
            ).fetchall()
    except (not_found, FileNotFoundError):
        return ProviderResult([], False)
    hits = []
    for row in rows[:limit]:
        snippet, highlights = make_snippet(" ".join(row[c] or "" for c in columns), terms)
        hits.append(SearchHit(
            id=f"briefings:{row['id']}", kind="briefing",
            title=" ".join((row["subject"] or row["briefing_name"]).split()),
            subtitle=" ".join((row["briefing_name"] or "").split()) or None,
            snippet=snippet, highlights=highlights, date=iso_utc(row["generated_at"]),
            link=route("/briefings/", id=row["id"]), badges=[],
        ))
    return ProviderResult(hits, len(rows) > limit)


PROVIDER = Provider("briefings", "Briefings", 50, "briefings", 1.5, False, search)
