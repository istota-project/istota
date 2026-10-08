import time

import pytest

from istota.config import Config, UserConfig
from istota.lib.text_match import parse_query
from istota.modules import module_loader
from istota.search.core import SearchContext, run_search
from istota.briefings import db


@pytest.fixture
def config(tmp_path):
    cfg = Config(db_path=tmp_path / "framework.db", nextcloud_mount_path=tmp_path / "mount",
                 users={"alice": UserConfig(), "bob": UserConfig()})
    cfg.nextcloud_mount_path.mkdir()
    return cfg


def populate(config, user="alice"):
    _, resolve, connect, _ = module_loader("briefings")
    ctx = resolve(user, config)
    db.init_db(ctx.db_path)
    return connect(ctx.db_path)


def search(config, query, limit=25, offset=0):
    from istota.briefings.search import PROVIDER
    return PROVIDER.run(SearchContext(config, "alice", time.monotonic() + 2),
                        parse_query(query), "strict", limit, offset)


async def test_missing_and_disabled_module(config):
    result = await run_search(config, "alice", "falcon", sources=["briefings"])
    assert result["groups"][0]["results"] == []
    assert result["groups"][0]["error"] is None
    assert not module_loader("briefings")[1]("alice", config).db_path.exists()
    config.users["alice"].disabled_modules = ["briefings"]
    assert (await run_search(config, "alice", "falcon", sources=["briefings"]))["groups"] == []


async def test_archive_columns_scope_links_and_paging(config):
    for user in ("alice", "bob"):
        with populate(config, user) as conn:
            db.insert_archive(conn, briefing_name="Morning", subject=f"{user} Falcon",
                              body_md="Ordinary body", generated_at="2026-01-02T00:00:00Z")
            db.insert_archive(conn, briefing_name="Evening", subject=None,
                              body_md="Falcon 50% complete", generated_at="2026-01-01T00:00:00Z")
            conn.commit()
    result = search(config, "falcon", limit=1)
    assert result.has_more
    assert result.hits[0].title == "alice Falcon"
    assert result.hits[0].link == {"type": "route", "path": "/briefings/", "params": {"id": "1"}}
    second = search(config, "falcon", limit=1, offset=1)
    assert not second.has_more
    assert second.hits[0].title == "Evening"
    assert second.hits[0].subtitle == "Evening"
    assert search(config, "50%").hits[0].id == second.hits[0].id
    assert search(config, "falcon", offset=10).hits == []
    group = (await run_search(config, "alice", "falcon absent", sources=["briefings"]))["groups"][0]
    assert group["relaxed"] and group["error"] is None
    assert len(group["results"]) == 2
