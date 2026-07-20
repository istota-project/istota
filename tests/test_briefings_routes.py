"""Tests for the briefings FastAPI router (reader + editor endpoints).

Mirrors ``test_feeds_routes``: a minimal app mounts the router and overrides
auth + context to inject a tmp-path-backed BriefingsContext.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.briefings import db as bdb
from istota.briefings.models import BriefingsContext
from istota.briefings.routes import get_user_context, require_auth, router, verify_origin
from istota.briefings.workspace import synthesize_briefings_context


@pytest.fixture
def ctx(tmp_path: Path) -> BriefingsContext:
    c = synthesize_briefings_context("stefan", tmp_path)
    c.ensure_dirs()
    bdb.init_db(c.db_path)
    return c


@pytest.fixture
def client(ctx: BriefingsContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/briefings")
    app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    app.dependency_overrides[verify_origin] = lambda: None
    return TestClient(app)


class TestArchive:
    def test_empty_archive(self, client):
        resp = client.get("/istota/api/briefings/archive")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"items": [], "total": 0, "briefing_names": []}

    def test_list_and_get(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="Morning", subject="Morning Briefing",
                body_md="📰 the news", delivered_to=["talk"],
            )
            conn.commit()
        resp = client.get("/istota/api/briefings/archive")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["subject"] == "Morning Briefing"
        assert body["items"][0]["body_md"] == "📰 the news"

        item = client.get(f"/istota/api/briefings/archive/{aid}").json()
        assert item["id"] == aid
        assert item["delivered_to"] == ["talk"]

    def test_get_missing_404(self, client):
        assert client.get("/istota/api/briefings/archive/999").status_code == 404

    def test_filter_by_name(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            bdb.insert_archive(conn, briefing_name="Morning", subject="m", body_md="x")
            bdb.insert_archive(conn, briefing_name="Evening", subject="e", body_md="y")
            conn.commit()
        body = client.get("/istota/api/briefings/archive?briefing_name=Evening").json()
        assert body["total"] == 1
        assert body["items"][0]["subject"] == "e"


class TestBlocksSources:
    def test_create_update_delete_block(self, client):
        # Create.
        resp = client.put(
            "/istota/api/briefings/blocks",
            json={"briefing_name": "M", "title": "News", "directive": "3 stories"},
        )
        assert resp.status_code == 200
        block = resp.json()["block"]
        assert block["title"] == "News"
        bid = block["id"]

        # Update.
        resp = client.put(
            "/istota/api/briefings/blocks",
            json={"id": bid, "title": "Headlines", "render_mode": "structured"},
        )
        assert resp.json()["block"]["title"] == "Headlines"
        assert resp.json()["block"]["render_mode"] == "structured"

        # Delete.
        assert client.delete(f"/istota/api/briefings/blocks/{bid}").status_code == 200

    def test_reorder_blocks(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            a = bdb.add_block(conn, briefing_name="M", title="A")
            b = bdb.add_block(conn, briefing_name="M", title="B")
            conn.commit()
        resp = client.put(
            "/istota/api/briefings/blocks",
            json={"reorder": {"briefing_name": "M", "ordered_ids": [b, a]}},
        )
        assert resp.status_code == 200
        with bdb.connect(ctx.db_path) as conn:
            titles = [x.title for x in bdb.list_blocks(conn, "M")]
        assert titles == ["B", "A"]

    def test_create_missing_fields_400(self, client):
        assert client.put("/istota/api/briefings/blocks", json={}).status_code == 400

    def test_create_update_delete_source(self, client):
        block = client.put(
            "/istota/api/briefings/blocks",
            json={"briefing_name": "M", "title": "News"},
        ).json()["block"]
        bid = block["id"]
        resp = client.put(
            "/istota/api/briefings/sources",
            json={"block_id": bid, "kind": "email", "config": {"mode": "shared"}},
        )
        assert resp.status_code == 200
        sid = resp.json()["id"]

        resp = client.put(
            "/istota/api/briefings/sources",
            json={"id": sid, "enabled": False},
        )
        assert resp.status_code == 200

        assert client.delete(f"/istota/api/briefings/sources/{sid}").status_code == 200

    def test_invalid_source_kind_400(self, client):
        block = client.put(
            "/istota/api/briefings/blocks",
            json={"briefing_name": "M", "title": "X"},
        ).json()["block"]
        resp = client.put(
            "/istota/api/briefings/sources",
            json={"block_id": block["id"], "kind": "bogus"},
        )
        assert resp.status_code == 400

    def test_config_shape(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            bid = bdb.add_block(conn, briefing_name="M", title="News")
            bdb.add_source(conn, block_id=bid, kind="email", config={"mode": "shared"})
            conn.commit()
        body = client.get("/istota/api/briefings/config").json()
        assert body["briefings"][0]["name"] == "M"
        assert body["briefings"][0]["blocks"][0]["sources"][0]["kind"] == "email"
        assert "email" in body["source_kinds"]


class TestPickers:
    def test_browse_presets(self, client):
        body = client.get("/istota/api/briefings/browse-presets").json()
        keys = [p["key"] for p in body["presets"]]
        assert "ap" in keys and "reuters" in keys

    def test_feed_options_soft_degrade(self, client):
        # No feeds configured for the stub → available False, empty lists.
        body = client.get("/istota/api/briefings/feed-options").json()
        assert body["available"] is False
        assert body["subscriptions"] == []
