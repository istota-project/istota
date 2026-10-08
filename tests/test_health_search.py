import time

import pytest

from istota.config import Config, UserConfig
from istota.lib.text_match import parse_query
from istota.modules import module_loader
from istota.search.core import SearchContext, run_search
from istota.health import db


@pytest.fixture
def config(tmp_path):
    cfg = Config(db_path=tmp_path / "framework.db", nextcloud_mount_path=tmp_path / "mount",
                 users={"alice": UserConfig(), "bob": UserConfig()})
    cfg.nextcloud_mount_path.mkdir()
    return cfg


def populate(config, user="alice"):
    _, resolve, connect, _ = module_loader("health")
    ctx = resolve(user, config)
    db.init_db(ctx.db_path)
    return connect(ctx.db_path)


def search(config, query, limit=25, offset=0):
    from istota.health.search import PROVIDER
    return PROVIDER.run(SearchContext(config, "alice", time.monotonic() + 2),
                        parse_query(query), "strict", limit, offset)


async def test_missing_and_disabled_module(config):
    result = await run_search(config, "alice", "falcon", sources=["health"])
    assert result["groups"][0]["results"] == []
    assert result["groups"][0]["error"] is None
    assert not module_loader("health")[1]("alice", config).db_path.exists()
    config.users["alice"].disabled_modules = ["health"]
    assert (await run_search(config, "alice", "falcon", sources=["health"]))["groups"] == []


TABLES = {
    "documents": (["original_filename", "notes", "ocr_text"],
                  {"filename": "scan.pdf", "mime": "application/pdf", "byte_size": 1,
                   "content_hash": "example", "stored_path": "1/scan.pdf", "created_at": "2026-01-06"},
                  "health_document", "/health/documents/"),
    "panels": (["lab_name", "panel_type", "notes", "ocr_text", "specimen"],
               {"drawn_at": "2026-01-05"}, "health_panel", "/health/labs/panel/"),
    "encounters": (["encounter_type", "provider", "facility", "specialty", "reason", "notes"],
                   {"encounter_date": "2026-01-04", "encounter_type": "visit"},
                   "health_encounter", "/health/history/encounter/"),
    "diagnoses": (["name", "icd10", "notes"], {"name": "Condition", "date_diagnosed": "2026-01-03"},
                  "health_diagnosis", "/health/history/diagnoses/"),
    "immunizations": (["name", "product_name", "manufacturer", "facility", "notes"],
                      {"name": "Vaccine", "date_given": "2026-01-02"},
                      "health_immunization", "/health/immunizations/detail/"),
}


def insert(conn, table, values):
    return conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                        list(values.values())).lastrowid


@pytest.mark.parametrize("table,column", [(table, column) for table, spec in TABLES.items() for column in spec[0]])
def test_each_text_column_and_link(config, table, column):
    _, values, kind, path = TABLES[table]
    with populate(config) as conn:
        insert(conn, table, {**values, column: "Falcon"})
        conn.commit()
    hit = search(config, "falcon").hits[0]
    assert hit.kind == kind
    assert hit.link == {"type": "route", "path": path,
                        "params": {} if table == "diagnoses" else {"id": "1"}}
    assert [hit.snippet[a:b] for a, b in hit.highlights] == ["Falcon"]


async def test_merge_dates_marker_dedupe_scope_and_fallback(config):
    for user in ("alice", "bob"):
        with populate(config, user) as conn:
            for table, (_, values, _, _) in TABLES.items():
                insert(conn, table, {**values, "notes": f"{user} Falcon 50%"})
            panel = insert(conn, "panels", {"drawn_at": "2026-01-07"})
            for pid in (1, panel):
                insert(conn, "biomarkers", {"panel_id": pid, "name": "marker", "display_name": "Falcon marker", "value": 1, "unit": "u"})
            conn.commit()
    result = search(config, "falcon", limit=3)
    assert [h.kind for h in result.hits] == ["health_marker", "health_document", "health_panel"]
    assert result.has_more
    assert result.hits[0].date == "2026-01-07"
    assert result.hits[0].link["params"] == {"name": "marker"}
    second = search(config, "falcon", limit=3, offset=3)
    assert [h.kind for h in second.hits] == ["health_encounter", "health_diagnosis", "health_immunization"]
    assert not second.has_more
    assert len(search(config, "50%").hits) == 5
    assert len(search(config, "marker").hits) == 1
    assert search(config, "bob").hits == []
    group = (await run_search(config, "alice", "falcon absent", sources=["health"]))["groups"][0]
    assert group["relaxed"] and group["error"] is None
    assert len(group["results"]) == 5


def test_reference_tables_are_not_searchable(config):
    with populate(config) as conn:
        conn.execute("INSERT INTO biomarker_refs(name, display_name, category, default_unit) VALUES ('falcon', 'Falcon', 'other', 'u')")
        conn.execute("INSERT INTO immunization_refs(name, display_name, category, schedule) VALUES ('falcon', 'Falcon', 'other', 'once')")
        conn.commit()
    assert search(config, "falcon").hits == []


def test_equal_dates_keep_numeric_order_across_pages(config):
    with populate(config) as conn:
        for _ in range(15):
            insert(conn, "panels", {"drawn_at": "2026-01-01", "notes": "Falcon"})
        conn.commit()
    first = search(config, "falcon", limit=5)
    second = search(config, "falcon", limit=5, offset=5)
    assert [h.id for h in first.hits + second.hits] == [f"health:panels:{i}" for i in range(15, 5, -1)]


def test_calendar_dates_stay_dates_and_timestamps_sort_chronologically(config):
    with populate(config) as conn:
        insert(conn, "panels", {"drawn_at": "2026-01-04", "notes": "Falcon"})
        insert(conn, "panels", {"drawn_at": "2026-01-03T23:30:00-02:00", "notes": "Falcon"})
        insert(conn, "encounters", {"encounter_date": "2026-01-04", "encounter_type": "Falcon"})
        conn.commit()
    hits = search(config, "falcon").hits
    assert hits[0].date == "2026-01-04T01:30:00Z"
    assert [h.date for h in hits[1:]] == ["2026-01-04", "2026-01-04"]
