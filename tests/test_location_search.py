import time

import pytest

from istota.config import Config, UserConfig
from istota.lib.text_match import parse_query
from istota.modules import module_loader
from istota.search.core import SearchContext, run_search
from istota.location import db


@pytest.fixture
def config(tmp_path):
    cfg = Config(db_path=tmp_path / "framework.db", nextcloud_mount_path=tmp_path / "mount",
                 users={"alice": UserConfig(), "bob": UserConfig()})
    cfg.nextcloud_mount_path.mkdir()
    return cfg


def populate(config, user="alice"):
    _, resolve, connect, _ = module_loader("location")
    ctx = resolve(user, config)
    db.init_db(ctx.db_path)
    return connect(ctx.db_path)


def search(config, query, limit=25, offset=0):
    from istota.location.search import PROVIDER
    return PROVIDER.run(SearchContext(config, "alice", time.monotonic() + 2),
                        parse_query(query), "strict", limit, offset)


async def test_missing_and_disabled_module(config):
    result = await run_search(config, "alice", "falcon", sources=["location"])
    assert result["groups"][0]["results"] == []
    assert result["groups"][0]["error"] is None
    assert not module_loader("location")[1]("alice", config).db_path.exists()
    config.users["alice"].disabled_modules = ["location"]
    assert (await run_search(config, "alice", "falcon", sources=["location"]))["groups"] == []


async def test_places_columns_scope_links_and_paging(config):
    for user in ("alice", "bob"):
        with populate(config, user) as conn:
            for name, category, notes in ((f"{user} Falcon", None, None),
                                          ("Alpha", "falcon", None),
                                          ("Beta", None, "Falcon 50% complete")):
                conn.execute("INSERT INTO places(name, category, notes, lat, lon) VALUES (?, ?, ?, 0, 0)",
                             (name, category, notes))
            conn.commit()
    result = search(config, "falcon", limit=2)
    assert [h.title for h in result.hits] == ["Alpha", "Beta"]
    assert result.has_more
    assert result.hits[0].subtitle == "falcon"
    assert result.hits[0].link == {"type": "route", "path": "/location/", "params": {"place": "2"}}
    second = search(config, "falcon", limit=2, offset=2)
    assert [h.title for h in second.hits] == ["alice Falcon"]
    assert not second.has_more
    assert search(config, "50%").hits[0].title == "Beta"
    group = (await run_search(config, "alice", "falcon absent", sources=["location"]))["groups"][0]
    assert group["relaxed"] and group["error"] is None
    assert len(group["results"]) == 3
