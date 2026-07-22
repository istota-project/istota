"""Tests for the admin shared-block routes (admin-shared-briefing-blocks Stage 3)
plus the per-user shared-block-options picker (Stage 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota import db
from istota.briefings.routes import require_admin, require_auth, router, verify_origin
from istota.config import Config, UserConfig


def _make_app(tmp_path: Path, *, username="stefan", admins=("stefan",)) -> FastAPI:
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    cfg = Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC"), "bob": UserConfig()},
        admin_users=set(admins),
    )
    app = FastAPI()
    app.state.istota_config = cfg
    app.include_router(router, prefix="/istota/api/briefings")
    app.dependency_overrides[require_auth] = lambda: {"username": username}
    app.dependency_overrides[verify_origin] = lambda: None
    return app


@pytest.fixture
def client(tmp_path):
    return TestClient(_make_app(tmp_path))


class TestAdminGate:
    def test_non_admin_403(self, tmp_path):
        app = _make_app(tmp_path, username="bob")  # bob not in admins
        c = TestClient(app)
        assert c.get("/istota/api/briefings/shared-blocks").status_code == 403
        assert c.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "x", "cron": "0 6 * * *"},
        ).status_code == 403

    def test_empty_admins_fails_closed(self, tmp_path):
        app = _make_app(tmp_path, username="stefan", admins=())
        c = TestClient(app)
        assert c.get("/istota/api/briefings/shared-blocks").status_code == 403


class TestCrud:
    def test_create_list_delete(self, client):
        resp = client.put(
            "/istota/api/briefings/shared-blocks",
            json={
                "name": "world-headlines", "cron": "0 6 * * *", "title": "World",
                "render_mode": "synthesis", "trusted": False,
                "sources": [{"kind": "browse", "config": {"preset": "ap"}}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["shared_block"]["name"] == "world-headlines"

        listed = client.get("/istota/api/briefings/shared-blocks").json()
        assert [b["name"] for b in listed["shared_blocks"]] == ["world-headlines"]
        assert "allowed_source_kinds" in listed
        assert set(listed["allowed_source_kinds"]) == {"browse", "markets", "email"}
        # The configured shared-block timezone is surfaced for the UI label.
        assert listed["shared_block_timezone"] == "UTC"

        d = client.delete("/istota/api/briefings/shared-blocks/world-headlines")
        assert d.status_code == 200
        assert client.get("/istota/api/briefings/shared-blocks").json()["shared_blocks"] == []

    def test_delete_missing_404(self, client):
        assert client.delete("/istota/api/briefings/shared-blocks/nope").status_code == 404

    def test_delete_value_option(self, client, tmp_path):
        client.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "mk", "cron": "0 6 * * *",
                  "sources": [{"kind": "markets", "config": {}}]},
        )
        cfg = client.app.state.istota_config
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, "briefing_shared_blocks", "mk",
                json.dumps({"text": "t"}), "stefan",
            )
        client.delete("/istota/api/briefings/shared-blocks/mk?delete_value=true")
        with db.get_db(cfg.db_path) as conn:
            assert db.shared_kv_get(conn, "briefing_shared_blocks", "mk") is None


class TestValidation:
    def test_rejects_bad_cron(self, client):
        resp = client.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "x", "cron": "not a cron"},
        )
        assert resp.status_code == 400

    def test_rejects_bad_render_mode(self, client):
        resp = client.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "x", "cron": "0 6 * * *", "render_mode": "weird"},
        )
        assert resp.status_code == 400

    def test_rejects_disallowed_source_kind(self, client):
        for bad in ("rss", "calendar", "todos"):
            resp = client.put(
                "/istota/api/briefings/shared-blocks",
                json={"name": "x", "cron": "0 6 * * *",
                      "sources": [{"kind": bad, "config": {}}]},
            )
            assert resp.status_code == 400, bad

    def test_rejects_bad_slug(self, client):
        resp = client.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "Has Spaces!", "cron": "0 6 * * *"},
        )
        assert resp.status_code == 400


class TestRunNow:
    def test_run_executes_generation(self, client, monkeypatch):
        import istota.scheduler as sched

        client.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "mk", "cron": "0 6 * * *",
                  "sources": [{"kind": "markets", "config": {}}]},
        )
        called = {}

        def _fake_gen(config, block):
            called["name"] = block.name
            with db.get_db(config.db_path) as conn:
                db.shared_kv_set(
                    conn, "briefing_shared_blocks", block.name,
                    json.dumps({"text": "fresh", "trusted": False}), "__system__",
                )

        monkeypatch.setattr(sched, "_generate_shared_block", _fake_gen)
        resp = client.post("/istota/api/briefings/shared-blocks/mk/run")
        assert resp.status_code == 200
        assert called["name"] == "mk"
        assert resp.json()["block_status"]["has_content"] is True

    def test_run_missing_404(self, client):
        assert client.post("/istota/api/briefings/shared-blocks/nope/run").status_code == 404

    def test_surfaces_configured_timezone(self, tmp_path):
        # A non-UTC shared_block_timezone flows into the list response so the
        # UI can label the Cron column with the operator's chosen zone.
        app = _make_app(tmp_path)
        app.state.istota_config.briefings.shared_block_timezone = "America/Los_Angeles"
        c = TestClient(app)
        listed = c.get("/istota/api/briefings/shared-blocks").json()
        assert listed["shared_block_timezone"] == "America/Los_Angeles"


class TestOptions:
    def test_lists_config_and_custom(self, client):
        cfg = client.app.state.istota_config
        # A defined (config) block.
        client.put(
            "/istota/api/briefings/shared-blocks",
            json={"name": "world-headlines", "cron": "0 6 * * *",
                  "sources": [{"kind": "browse", "config": {}}]},
        )
        # A custom-published key with no definition + content.
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, "briefing_shared_blocks", "film-digest",
                json.dumps({"text": "film"}), "stefan",
            )
        opts = client.get("/istota/api/briefings/shared-block-options").json()["options"]
        by_name = {o["name"]: o for o in opts}
        assert by_name["world-headlines"]["source"] == "config"
        assert by_name["world-headlines"]["has_content"] is False
        assert by_name["film-digest"]["source"] == "custom"
        assert by_name["film-digest"]["has_content"] is True

    def test_options_available_to_non_admin(self, tmp_path):
        # The picker is read-only discovery, available to any authenticated user.
        app = _make_app(tmp_path, username="bob")
        c = TestClient(app)
        assert c.get("/istota/api/briefings/shared-block-options").status_code == 200
