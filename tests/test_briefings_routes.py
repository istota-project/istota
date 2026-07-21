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


class TestScheduleNames:
    """A briefing added to ``briefing_configs`` must show up in the editor's
    dropdown (``schedule_names``) without a SIGHUP, even when it has no blocks
    yet and isn't in the in-memory config snapshot (the "none configured" bug)."""

    def test_db_briefing_appears_without_reload(self, tmp_path):
        from types import SimpleNamespace

        import istota.db as fdb
        import istota.user_briefings as ub
        from istota.briefings.routes import _schedule_names

        framework_db = tmp_path / "istota.db"
        fdb.init_db(framework_db)
        ub.ensure_briefing(
            framework_db, user_id="stefan", name="Evening", cron="0 18 * * *",
        )
        cfg = SimpleNamespace(db_path=framework_db, users={})

        assert _schedule_names(cfg, "stefan") == ["Evening"]

    def test_disabled_db_briefing_excluded(self, tmp_path):
        from types import SimpleNamespace

        import istota.db as fdb
        import istota.user_briefings as ub
        from istota.briefings.routes import _schedule_names

        framework_db = tmp_path / "istota.db"
        fdb.init_db(framework_db)
        ub.ensure_briefing(
            framework_db, user_id="stefan", name="Muted", cron="0 6 * * *",
            enabled=False,
        )
        cfg = SimpleNamespace(db_path=framework_db, users={})

        assert _schedule_names(cfg, "stefan") == []


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


class TestPathPicker:
    @pytest.fixture
    def path_client(self, tmp_path: Path) -> TestClient:
        from istota.config import Config, UserConfig

        cfg = Config(
            db_path=tmp_path / "istota.db",
            nextcloud_mount_path=tmp_path / "mount",
            users={"stefan": UserConfig(timezone="UTC")},
        )
        user_root = cfg.nextcloud_mount_path / "Users" / "stefan"
        (user_root / "shared").mkdir(parents=True, exist_ok=True)
        (user_root / "istota" / "config").mkdir(parents=True, exist_ok=True)
        (user_root / "shared" / "team-todo.md").write_text("- [ ] x\n")
        (user_root / "istota" / "config" / "TODO.md").write_text("- [ ] y\n")
        (user_root / "istota" / "config" / "notes.bin").write_text("ignore me")

        app = FastAPI()
        app.include_router(router, prefix="/istota/api/briefings")
        app.state.istota_config = cfg
        app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
        app.dependency_overrides[verify_origin] = lambda: None
        return TestClient(app)

    def test_check_existing_file(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-check", params={"path": "shared/team-todo.md"},
        ).json()
        assert body["ok"] is True
        assert body["resolved"] == "Users/stefan/shared/team-todo.md"

    def test_check_missing_file(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-check", params={"path": "shared/nope.md"},
        ).json()
        assert body["ok"] is False
        assert body["error"]

    def test_check_blank_path(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-check", params={"path": ""},
        ).json()
        assert body["ok"] is False

    def test_check_is_user_scoped(self, path_client, tmp_path):
        # A file outside the user's folder is not reachable via a relative path.
        (tmp_path / "mount" / "secret.md").write_text("nope")
        body = path_client.get(
            "/istota/api/briefings/path-check", params={"path": "../secret.md"},
        ).json()
        assert body["ok"] is False

    def test_suggest_lists_text_files(self, path_client):
        body = path_client.get("/istota/api/briefings/path-suggest").json()
        paths = body["paths"]
        assert "shared/team-todo.md" in paths
        assert "istota/config/TODO.md" in paths
        # Non-text files are excluded.
        assert not any(p.endswith(".bin") for p in paths)

    def test_suggest_filters_by_query(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-suggest", params={"q": "team"},
        ).json()
        paths = body["paths"]
        assert paths == ["shared/team-todo.md"]

    def test_suggest_query_matches_directory_component(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-suggest", params={"q": "config"},
        ).json()
        assert "istota/config/TODO.md" in body["paths"]

    def test_suggest_query_no_match(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-suggest", params={"q": "zzzznope"},
        ).json()
        assert body["paths"] == []

    def test_suggest_finds_deep_file_by_query(self, path_client, tmp_path):
        # A file deeper than the on-load walk would surface only via query.
        deep = (
            tmp_path / "mount" / "Users" / "stefan"
            / "istota" / "notes" / "2026" / "q3" / "sprint-plan.md"
        )
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.write_text("plan\n")
        body = path_client.get(
            "/istota/api/briefings/path-suggest", params={"q": "sprint"},
        ).json()
        assert "istota/notes/2026/q3/sprint-plan.md" in body["paths"]
