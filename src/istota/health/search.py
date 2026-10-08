"""Search the user's health records in their existing module database."""

from istota.lib.date_parse import iso_utc
from istota.lib.text_match import like_predicate, make_snippet
from istota.modules import module_loader
from istota.search.core import Provider, ProviderResult, SearchHit, open_with_deadline, resolve_module_user
from istota.search.links import route


# Table, searched columns, date, title, subtitle, kind, destination.
_TABLES = (
    ("documents", ["original_filename", "notes", "ocr_text"], "created_at",
     "COALESCE(original_filename, filename)", "NULL", "health_document", "/health/documents/"),
    ("panels", ["lab_name", "panel_type", "notes", "ocr_text", "specimen"], "drawn_at",
     "COALESCE(panel_type, lab_name, 'Lab panel')", "lab_name", "health_panel", "/health/labs/panel/"),
    ("encounters", ["encounter_type", "provider", "facility", "specialty", "reason", "notes"], "encounter_date",
     "encounter_type", "COALESCE(provider, facility)", "health_encounter", "/health/history/encounter/"),
    ("diagnoses", ["name", "icd10", "notes"], "COALESCE(date_diagnosed, created_at)",
     "name", "icd10", "health_diagnosis", "/health/history/diagnoses/"),
    ("immunizations", ["name", "product_name", "manufacturer", "facility", "notes"], "date_given",
     "name", "COALESCE(product_name, facility)", "health_immunization", "/health/immunizations/detail/"),
)


def _sort_key(hit):
    stable_id = hit.id if hit.kind == "health_marker" else int(hit.id.rsplit(":", 1)[1])
    return iso_utc(hit.date) or "", hit.kind, stable_id


def search(ctx, terms, mode, limit, offset):
    _, resolve, connect, not_found = module_loader("health")
    hits = []
    count = offset + limit + 1
    try:
        user = resolve_module_user(ctx, resolve)
        if not user.db_path.is_file():
            return ProviderResult([], False)
        with open_with_deadline(lambda **options: connect(user.db_path, **options), ctx.deadline) as conn:
            for table, columns, date, title, subtitle, kind, path in _TABLES:
                predicate, params = like_predicate(columns, terms, mode)
                rows = conn.execute(
                    f"SELECT *, {date} AS hit_date, {title} AS title, {subtitle} AS subtitle "
                    f"FROM {table} WHERE {predicate} ORDER BY julianday({date}) DESC, id DESC LIMIT ?",
                    [*params, count],
                ).fetchall()
                for row in rows:
                    snippet, highlights = make_snippet(" ".join(row[c] or "" for c in columns), terms)
                    hits.append(SearchHit(
                        id=f"health:{table}:{row['id']}", kind=kind,
                        title=" ".join((row["title"] or "").split()),
                        subtitle=" ".join((row["subtitle"] or "").split()) or None,
                        snippet=snippet, highlights=highlights, date=iso_utc(row["hit_date"], preserve_date=True),
                        link=route(path, **({} if table == "diagnoses" else {"id": row["id"]})), badges=[],
                    ))
            predicate, params = like_predicate(["b.name", "b.display_name"], terms, mode)
            rows = conn.execute(
                "WITH markers AS (SELECT b.name, p.drawn_at AS hit_date, "
                "MAX(b.display_name) OVER (PARTITION BY b.name) AS display_name, "
                f"MAX({predicate}) OVER (PARTITION BY b.name) AS matched, "
                "ROW_NUMBER() OVER (PARTITION BY b.name ORDER BY julianday(p.drawn_at) DESC, b.id DESC) AS position "
                "FROM biomarkers b JOIN panels p ON p.id = b.panel_id) "
                "SELECT name, display_name, hit_date FROM markers WHERE position=1 AND matched "
                "ORDER BY julianday(hit_date) DESC, name DESC LIMIT ?",
                [*params, count],
            ).fetchall()
            for row in rows:
                title = row["display_name"] or row["name"]
                snippet, highlights = make_snippet(f"{title} {row['name']}", terms)
                hits.append(SearchHit(
                    id=f"health:marker:{row['name']}", kind="health_marker",
                    title=" ".join(title.split()), subtitle=None,
                    snippet=snippet, highlights=highlights, date=iso_utc(row["hit_date"], preserve_date=True),
                    link=route("/health/labs/marker/", name=row["name"]), badges=[],
                ))
    except (not_found, FileNotFoundError):
        return ProviderResult([], False)
    hits.sort(key=_sort_key, reverse=True)
    return ProviderResult(hits[offset:offset + limit], len(hits) > offset + limit)


PROVIDER = Provider("health", "Health", 70, "health", 1.5, False, search)
