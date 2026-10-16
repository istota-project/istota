"""Tests for the briefings FastAPI router (reader + editor endpoints).

Mirrors ``test_feeds_routes``: a minimal app mounts the router and overrides
auth + context to inject a tmp-path-backed BriefingsContext.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from fastapi.testclient import TestClient

from istota.briefings import db as bdb
from istota.briefings.models import BriefingsContext
from istota.briefings.document import document_to_markdown
from istota.briefings.routes import get_user_context, require_auth, router, verify_origin
from istota.briefings.workspace import synthesize_briefings_context


@pytest.fixture
def ctx(tmp_path: Path) -> BriefingsContext:
    c = synthesize_briefings_context("alice", tmp_path)
    c.ensure_dirs()
    bdb.init_db(c.db_path)
    return c


@pytest.fixture
def client(ctx: BriefingsContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/briefings")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
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

    def test_detail_document_is_additive_and_list_stays_linear(self, ctx, client):
        document = {
            "version": 1, "title": "Morning edition",
            "blocks": [{
                "block_id": 42, "title": "Headlines", "kind": "markdown",
                "body_md": "[A story](https://example.com/story)",
                "presentation": {"width": "wide"}, "source_status": "partial",
            }],
        }
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="morning", subject=document["title"],
                body_md=document_to_markdown(document), document=document,
                block_meta={"42": {"error": "internal source detail"}},
            )
            conn.commit()
        detail = client.get(f"/istota/api/briefings/archive/{aid}").json()
        assert detail["document"] == document
        assert detail["body_md"] == document_to_markdown(document)
        assert "block_meta" not in detail
        listed = client.get("/istota/api/briefings/archive").json()["items"][0]
        assert "document" not in listed
        assert listed["body_md"] == document_to_markdown(document)

    def test_preview_is_on_list_rows_only(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="morning", subject="Edition", body_md="The first words",
            )
            conn.commit()
        listed = client.get("/istota/api/briefings/archive").json()["items"][0]
        assert listed["preview"] == "The first words"
        detail = client.get(f"/istota/api/briefings/archive/{aid}").json()
        assert "preview" not in detail

    @pytest.mark.parametrize("stored", [None, "{", '{"version": 99}', '{"version": 1}'])
    def test_detail_keeps_legacy_body_when_document_is_unreadable(self, ctx, client, stored):
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="morning", subject="Old edition", body_md="Whole old body",
            )
            conn.execute("UPDATE briefing_archive SET document_json = ? WHERE id = ?", (stored, aid))
            conn.commit()
        detail = client.get(f"/istota/api/briefings/archive/{aid}").json()
        assert detail["document"] is None
        assert detail["body_md"] == "Whole old body"

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

    def test_delete_archive_item(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(
                conn, briefing_name="Morning", subject="m", body_md="x",
            )
            conn.commit()
        resp = client.delete(f"/istota/api/briefings/archive/{aid}")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        # Gone from both the item and list endpoints.
        assert client.get(f"/istota/api/briefings/archive/{aid}").status_code == 404
        assert client.get("/istota/api/briefings/archive").json()["total"] == 0

    def test_delete_archive_item_missing_404(self, client):
        assert client.delete("/istota/api/briefings/archive/999").status_code == 404


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
            framework_db, user_id="alice", name="Evening", cron="0 18 * * *",
        )
        cfg = SimpleNamespace(db_path=framework_db, users={})

        assert _schedule_names(cfg, "alice") == ["Evening"]

    def test_disabled_db_briefing_remains_editable(self, tmp_path):
        from types import SimpleNamespace

        import istota.db as fdb
        import istota.user_briefings as ub
        from istota.briefings.routes import _schedule_names

        framework_db = tmp_path / "istota.db"
        fdb.init_db(framework_db)
        ub.ensure_briefing(
            framework_db, user_id="alice", name="Muted", cron="0 6 * * *",
            enabled=False,
        )
        cfg = SimpleNamespace(db_path=framework_db, users={})

        assert _schedule_names(cfg, "alice") == ["Muted"]


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
            workspace_path=tmp_path / "mount",
            users={"alice": UserConfig(timezone="UTC")},
        )
        user_root = cfg.workspace_path / "Users" / "alice"
        (user_root / "shared").mkdir(parents=True, exist_ok=True)
        (user_root / "istota" / "config").mkdir(parents=True, exist_ok=True)
        (user_root / "shared" / "team-todo.md").write_text("- [ ] x\n")
        (user_root / "istota" / "config" / "TODO.md").write_text("- [ ] y\n")
        (user_root / "istota" / "config" / "notes.bin").write_text("ignore me")

        app = FastAPI()
        app.include_router(router, prefix="/istota/api/briefings")
        app.state.istota_config = cfg
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[verify_origin] = lambda: None
        return TestClient(app)

    def test_check_existing_file(self, path_client):
        body = path_client.get(
            "/istota/api/briefings/path-check", params={"path": "shared/team-todo.md"},
        ).json()
        assert body["ok"] is True
        assert body["resolved"] == "Users/alice/shared/team-todo.md"

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

    def test_check_does_not_follow_a_symlink(self, path_client, tmp_path):
        """ISSUE-596: no existence oracle through a link, and no false yes."""
        (tmp_path / "outside.md").write_text("x")
        (tmp_path / "mount/Users/alice/link.md").symlink_to(tmp_path / "outside.md")
        body = path_client.get(
            "/istota/api/briefings/path-check", params={"path": "link.md"},
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
            tmp_path / "mount" / "Users" / "alice"
            / "istota" / "notes" / "2026" / "q3" / "sprint-plan.md"
        )
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.write_text("plan\n")
        body = path_client.get(
            "/istota/api/briefings/path-suggest", params={"q": "sprint"},
        ).json()
        assert "istota/notes/2026/q3/sprint-plan.md" in body["paths"]


class TestAtomicBlockWrites:
    def test_create_duplicate_and_legacy_patch(self, client):
        payload = {
            "briefing_name": "M", "title": "News", "directive": "Concise",
            "render_mode": "structured", "options": {"custom": 42, "presentation": {"width": "full"}},
            "sources": [{"kind": "notes", "config": {"path": "shared/notes.md"}, "enabled": False},
                        {"kind": "calendar", "config": {"days": 3}, "enabled": True}],
        }
        response = client.put("/istota/api/briefings/blocks", json=payload)
        assert response.status_code == 200
        block = response.json()["block"]
        assert len(block["sources"]) == 2
        assert block["sources"][0]["enabled"] is False
        copy_payload = {**payload, "title": block["title"] + " (copy)"}
        duplicate = client.put("/istota/api/briefings/blocks", json=copy_payload).json()["block"]
        assert duplicate["id"] != block["id"]
        assert duplicate["position"] == block["position"] + 1
        assert duplicate["options"] == block["options"]
        assert duplicate["directive"] == block["directive"]
        assert duplicate["render_mode"] == block["render_mode"]
        assert {s["id"] for s in duplicate["sources"]}.isdisjoint(s["id"] for s in block["sources"])
        for source, copied in zip(block["sources"], duplicate["sources"]):
            assert {k: v for k, v in source.items() if k != "id"} == {k: v for k, v in copied.items() if k != "id"}
        patched = client.put("/istota/api/briefings/blocks", json={"id": block["id"], "title": "Renamed"}).json()["block"]
        assert patched["sources"] == block["sources"]
        assert patched["options"] == block["options"]
        empty = client.put("/istota/api/briefings/blocks", json={"id": block["id"], "sources": []}).json()["block"]
        assert empty["sources"] == []
        assert empty["title"] == "Renamed"
        # Existing zero-source definitions remain editable.
        assert client.put("/istota/api/briefings/blocks", json={"id": block["id"], "title": "Empty"}).status_code == 200

    @pytest.mark.parametrize("patch", [
        {"sources": []}, {"sources": None}, {"sources": {}},
        {"sources": [{"kind": "unknown"}]}, {"sources": [{"kind": "notes", "config": []}]},
        {"sources": [{"kind": "notes", "enabled": "false"}]},
        {"sources": [{"id": 999, "kind": "notes"}]},
        {"options": {"presentation": {"width": "bad"}}},
        {"title": []}, {"render_mode": "bad"},
    ])
    def test_invalid_creation_leaves_no_block(self, client, patch):
        response = client.put("/istota/api/briefings/blocks", json={
            "briefing_name": "M", "title": "Copy", "sources": [{"kind": "notes"}], **patch,
        })
        assert response.status_code == 400
        assert client.get("/istota/api/briefings/config").json()["briefings"] == []

    @pytest.mark.parametrize("case", ["foreign", "duplicate", "invalid_config", "invalid_options"])
    def test_invalid_edit_preserves_whole_definition(self, ctx, client, case):
        with bdb.connect(ctx.db_path) as conn:
            bid = bdb.add_block(conn, briefing_name="M", title="Original", options={"custom": 42})
            sid = bdb.add_source(conn, block_id=bid, kind="notes", config={"path": "shared/notes.md"})
            foreign = bdb.add_block(conn, briefing_name="Other", title="Other")
            foreign_sid = bdb.add_source(conn, block_id=foreign, kind="calendar")
            conn.commit()
        before = client.get("/istota/api/briefings/config").json()["briefings"]
        sources = [{"id": sid, "kind": "notes", "config": {"path": "changed.md"}}]
        patch = {"id": bid, "title": "Changed", "sources": sources}
        if case == "foreign":
            sources.append({"id": foreign_sid, "kind": "calendar"})
        elif case == "duplicate":
            sources.append(sources[0])
        elif case == "invalid_config":
            sources.append({"kind": "calendar", "config": []})
        else:
            patch["options"] = {"presentation": {"width": "bad"}}
        response = client.put("/istota/api/briefings/blocks", json=patch)
        assert response.status_code == 400
        assert client.get("/istota/api/briefings/config").json()["briefings"] == before

    @pytest.mark.parametrize("case", ["duplicate", "missing", "foreign", "bool", "string", "shape"])
    def test_invalid_reorder_keeps_positions(self, ctx, client, case):
        with bdb.connect(ctx.db_path) as conn:
            a = bdb.add_block(conn, briefing_name="M", title="A")
            b = bdb.add_block(conn, briefing_name="M", title="B")
            foreign = bdb.add_block(conn, briefing_name="Other", title="Other")
            conn.commit()
        orders = {"duplicate": [a, a], "missing": [b], "foreign": [b, a, foreign],
                  "bool": [True, b], "string": [str(b), str(a)], "shape": "bad"}
        response = client.put("/istota/api/briefings/blocks", json={
            "reorder": {"briefing_name": "M", "ordered_ids": orders[case]},
        })
        assert response.status_code == 400
        with bdb.connect(ctx.db_path) as conn:
            assert [(x.id, x.position) for x in bdb.list_blocks(conn, "M")] == [(a, 0), (b, 1)]

    def test_source_and_block_ids_stay_in_authenticated_user_database(self, tmp_path):
        from istota.config import Config, UserConfig

        cfg = Config(db_path=tmp_path / "framework.db", workspace_path=tmp_path / "workspace",
                     users={"alice": UserConfig(), "bob": UserConfig()})
        app = FastAPI()
        app.state.istota_config = cfg
        app.include_router(router, prefix="/istota/api/briefings")
        identity = {"username": "bob"}
        app.dependency_overrides[require_auth] = lambda: identity
        app.dependency_overrides[verify_origin] = lambda: None
        with TestClient(app) as client:
            bob = client.put("/istota/api/briefings/blocks", json={"briefing_name": "M", "title": "Private", "sources": [{"kind": "notes"}]}).json()["block"]
            identity["username"] = "alice"
            assert client.put("/istota/api/briefings/blocks", json={"id": bob["id"], "title": "Changed"}).status_code == 404
            response = client.put("/istota/api/briefings/blocks", json={"briefing_name": "M", "title": "Copy", "sources": [{"id": bob["sources"][0]["id"], "kind": "notes"}]})
            assert response.status_code == 400
            assert client.get("/istota/api/briefings/config").json()["briefings"] == []
            identity["username"] = "bob"
            assert client.get("/istota/api/briefings/config").json()["briefings"][0]["blocks"][0] == bob

    def test_catalog_contains_editable_defaults_without_user_selections(self, client):
        config = client.get("/istota/api/briefings/config").json()
        catalog = config["block_templates"]
        assert [t["label"] for t in catalog] == ["News from feeds", "News from websites", "Newsletters", "My email", "Calendar", "Markets", "Tasks", "Notes", "Reminder", "Shared content", "Custom"]
        assert len({t["id"] for t in catalog}) == 11
        for template in catalog:
            assert template["description"]
            block = template["block"]
            assert block["title"] and block["render_mode"] in {"synthesis", "structured"}
            assert block["options"]["presentation"] == {"width": "auto"}
            assert "id" not in block
            for source in block["sources"]:
                assert source["kind"] in config["source_kinds"]
                assert source["config"] == {} and source["enabled"] is True
                assert "id" not in source
        assert catalog[-1]["block"]["sources"] == []


    def test_host_auth_and_origin_checks_guard_atomic_writes(self, tmp_path, monkeypatch):
        from istota.config import Config, UserConfig
        from istota.webui import app as web_app

        cfg = Config(db_path=tmp_path / "framework.db", workspace_path=tmp_path / "workspace",
                     users={"alice": UserConfig()})
        cfg.site.hostname = "bot.example.com"
        monkeypatch.setattr(web_app, "_config", cfg)
        app = FastAPI()
        app.state.istota_config = cfg
        app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
        app.add_exception_handler(web_app._UnauthorizedException, web_app._handle_unauthorized)
        app.add_exception_handler(web_app._ForbiddenException, web_app._handle_forbidden)
        app.include_router(router, prefix="/istota/api/briefings")
        app.dependency_overrides[require_auth] = web_app._require_api_auth
        app.dependency_overrides[verify_origin] = web_app._verify_origin
        payload = {"briefing_name": "M", "title": "News", "sources": [{"kind": "notes"}]}
        with TestClient(app) as client:
            assert client.put("/istota/api/briefings/blocks", json=payload,
                              headers={"Origin": "https://bot.example.com"}).status_code == 401
            app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
            for headers in ({}, {"Origin": "https://other.example.com"}):
                assert client.put("/istota/api/briefings/blocks", json=payload, headers=headers).status_code == 403
            assert not cfg.module_db_path("alice", "briefings").exists()
            assert client.put("/istota/api/briefings/blocks", json=payload,
                              headers={"Origin": "https://bot.example.com"}).status_code == 200


class TestPreview:
    @pytest.fixture
    def document(self):
        return {
            "version": 1, "title": "Morning edition",
            "blocks": [{
                "block_id": 42, "title": "Headlines <em>today</em>", "kind": "markdown",
                "body_md": "[Story](https://example.com/a_b?one=two_three)\n\nComplete final paragraph.",
                "presentation": {"width": "wide"}, "source_status": "partial",
            }],
        }

    def test_renders_inline_document_without_content_lookup_or_side_effects(self, client, document, monkeypatch):
        from unittest.mock import Mock
        from istota.skills.briefing import render_briefing_document_html, render_briefing_document_plain

        forbidden = Mock(side_effect=AssertionError("preview crossed content boundary"))
        monkeypatch.setattr(bdb, "connect", forbidden)
        monkeypatch.setattr("istota.briefings.routes.ensure_initialised", forbidden)
        monkeypatch.setattr("istota.briefings.generate.resolve_source", forbidden)
        monkeypatch.setattr("istota.scheduler.execute_task", forbidden)
        monkeypatch.setattr("smtplib.SMTP", forbidden)
        monkeypatch.setattr(Path, "read_text", forbidden)
        response = client.post("/istota/api/briefings/preview", json={"document": document})
        assert response.status_code == 200
        assert response.json() == {
            "body_md": document_to_markdown(document),
            "html": render_briefing_document_html(document),
            "plain": render_briefing_document_plain(document),
        }
        forbidden.assert_not_called()

    @pytest.mark.parametrize("payload", [None, [], {}, {"document": {}}, {"document": {"version": 9}}])
    def test_malformed_shape_is_bounded_400(self, client, payload):
        response = client.post("/istota/api/briefings/preview", json=payload)
        assert response.status_code == 400
        assert len(response.text) < 120

    def test_invalid_json_is_400(self, client):
        assert client.post("/istota/api/briefings/preview", content=b"{").status_code == 400

    def test_block_count_and_utf8_body_bounds(self, client, document):
        block = document["blocks"][0]
        document["blocks"] = [{**block, "block_id": i} for i in range(100)]
        assert client.post("/istota/api/briefings/preview", json={"document": document}).status_code == 200
        document["blocks"].append({**block, "block_id": 100})
        assert client.post("/istota/api/briefings/preview", json={"document": document}).status_code == 413
        document["blocks"] = [{**block, "body_md": "é" * 32768}]
        assert client.post("/istota/api/briefings/preview", json={"document": document}).status_code == 200
        document["blocks"][0]["body_md"] += "é"
        assert client.post("/istota/api/briefings/preview", json={"document": document}).status_code == 413

    def test_total_body_bound_precedes_json_decode(self, client):
        response = client.post("/istota/api/briefings/preview", content=b" " * (2 * 1024 * 1024 + 1))
        assert response.status_code == 413
        assert len(response.text) < 120

    def test_stream_is_bounded_even_with_false_declared_length(self, client):
        response = client.post("/istota/api/briefings/preview", content=b" " * (2 * 1024 * 1024 + 1),
                               headers={"content-length": "1"})
        assert response.status_code == 413

    def test_existing_auth_origin_and_module_gates(self, tmp_path, document, monkeypatch):
        from istota.config import Config, UserConfig
        from istota.webui import app as web_app

        cfg = Config(db_path=tmp_path / "framework.db", users={"alice": UserConfig()})
        cfg.site.hostname = "bot.example.com"
        monkeypatch.setattr(web_app, "_config", cfg)
        app = FastAPI()
        app.state.istota_config = cfg
        app.add_middleware(SessionMiddleware, secret_key="test-session-secret")
        app.add_exception_handler(web_app._UnauthorizedException, web_app._handle_unauthorized)
        app.add_exception_handler(web_app._ForbiddenException, web_app._handle_forbidden)
        app.include_router(router, prefix="/istota/api/briefings")
        app.dependency_overrides[require_auth] = web_app._require_api_auth
        app.dependency_overrides[verify_origin] = web_app._verify_origin
        with TestClient(app) as gate_client:
            path = "/istota/api/briefings/preview"
            assert gate_client.post(path, json={"document": document},
                                    headers={"Origin": "https://bot.example.com"}).status_code == 401
            app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
            for headers in ({}, {"Origin": "https://other.example.com"}):
                assert gate_client.post(path, json={"document": document}, headers=headers).status_code == 403
            headers = {"Origin": "https://bot.example.com"}
            assert gate_client.post(path, json={"document": document}, headers=headers).status_code == 200
            cfg.users["alice"].disabled_modules = ["briefings"]
            assert gate_client.post(path, json={"document": document}, headers=headers).status_code == 403
            assert not cfg.module_db_path("alice", "briefings").exists()


class TestArchiveReads:
    def test_gets_do_not_read_and_post_is_idempotent(self, ctx, client):
        with bdb.connect(ctx.db_path) as conn:
            aid = bdb.insert_archive(conn, briefing_name="Morning", subject="One", body_md="Body")
            other = bdb.insert_archive(conn, briefing_name="Evening", subject="Two", body_md="Body")
            conn.commit()
        listing = client.get("/istota/api/briefings/archive").json()
        assert all(row["read_at"] is None for row in listing["items"])
        assert client.get(f"/istota/api/briefings/archive/{aid}").json()["read_at"] is None
        with bdb.connect(ctx.db_path) as conn:
            assert bdb.count_unread_archive(conn) == 2
        response = client.post(f"/istota/api/briefings/archive/{aid}/read")
        assert response.status_code == 200
        assert response.json() == {"id": aid, "read_at": response.json()["read_at"]}
        assert response.json()["read_at"] is not None
        assert client.post(f"/istota/api/briefings/archive/{aid}/read").json() == response.json()
        assert client.get(f"/istota/api/briefings/archive/{aid}").json()["read_at"] == response.json()["read_at"]
        with bdb.connect(ctx.db_path) as conn:
            assert bdb.get_archived(conn, other).read_at is None
            bdb.delete_archived(conn, aid)
            conn.commit()
        assert client.post(f"/istota/api/briefings/archive/{aid}/read").status_code == 404

    @pytest.mark.parametrize("name", ["Morning", " Morning "])
    def test_bulk_reads_beyond_page_and_exact_name(self, ctx, client, name):
        with bdb.connect(ctx.db_path) as conn:
            for _ in range(125):
                bdb.insert_archive(conn, briefing_name=name, subject="One", body_md="Body")
            bdb.insert_archive(conn, briefing_name="morning", subject="Other", body_md="Body")
            conn.commit()
        url = "/istota/api/briefings/mark-as-read"
        response = client.post(url, json={"scope": "briefing", "briefing_name": name})
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "updated": 125}
        with bdb.connect(ctx.db_path) as conn:
            assert bdb.count_unread_archive(conn) == 1
        assert client.post(url, json={"scope": "briefing", "briefing_name": "Missing"}).json()["updated"] == 0
        assert client.post(url, json={"scope": "all"}).json() == {"status": "ok", "updated": 1}
        assert client.post(url, json={"scope": "all"}).json()["updated"] == 0

    @pytest.mark.parametrize("body", [
        "not json", "null", "[]", '"all"', '{}', '{"scope": "other"}',
        '{"scope": "briefing"}', '{"scope": "briefing", "briefing_name": ""}',
        '{"scope": "briefing", "briefing_name": 1}',
        '{"scope": "briefing", "briefing_name": null}',
        '{"scope": "all", "briefing_name": "Morning"}',
        '{"scope": "all", "briefing_name": null}',
        '{"scope": "all", "user_id": "bob"}',
        '{"scope": "all", "db_path": "/tmp/other.db"}',
    ])
    def test_bulk_rejects_malformed_payload(self, client, body):
        response = client.post(
            "/istota/api/briefings/mark-as-read", content=body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400


@pytest.fixture
async def host_client(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import MagicMock
    from tests.test_web_app import _make_config
    import istota.webui.app as host

    cfg = _make_config(tmp_path)
    monkeypatch.setattr(host, "_config", cfg)
    monkeypatch.setattr(host, "_oauth", MagicMock())
    monkeypatch.setattr(host.app.state, "istota_config", cfg, raising=False)
    monkeypatch.setattr(host.app.state, "briefings_initialised_dbs", set(), raising=False)
    async with AsyncClient(transport=ASGITransport(app=host.app), base_url="https://example.com") as client:
        yield client, cfg


class TestArchiveReadHostGates:
    @pytest.mark.parametrize("url,body", [
        ("/istota/api/briefings/archive/1/read", None),
        ("/istota/api/briefings/mark-as-read", {"scope": "all"}),
    ])
    async def test_auth_origin_module_and_user_isolation(self, host_client, url, body):
        from unittest.mock import AsyncMock
        from istota import user_profiles
        from istota.briefings._loader import resolve_for_user
        from istota.briefings._migrate import ensure_initialised
        import istota.webui.app as host

        client, cfg = host_client
        paths = {}
        for username in ("alice", "bob"):
            ctx = resolve_for_user(username, cfg)
            ensure_initialised(ctx, app_config=cfg)
            paths[username] = ctx.db_path
            with bdb.connect(ctx.db_path) as conn:
                assert bdb.insert_archive(conn, briefing_name="Morning", subject=username, body_md="Body") == 1
                conn.commit()
        headers = {"origin": "https://example.com"}
        assert (await client.post(url, json=body, headers=headers)).status_code == 401
        host._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
        assert (await client.get("/istota/callback", follow_redirects=False)).status_code == 302
        assert (await client.post(url, json=body)).status_code == 403
        assert (await client.post(url, json=body, headers={"origin": "https://other.example.com"})).status_code == 403
        user_profiles.update_profile(cfg.db_path, "alice", disabled_modules=["briefings"])
        assert (await client.post(url, json=body, headers=headers)).status_code == 404
        for path in paths.values():
            with bdb.connect(path) as conn:
                assert bdb.count_unread_archive(conn) == 1
        user_profiles.update_profile(cfg.db_path, "alice", disabled_modules=[])
        response = await client.post(url, json=body, headers=headers)
        assert response.status_code == 200
        with bdb.connect(paths["alice"]) as conn:
            assert bdb.count_unread_archive(conn) == 0
        with bdb.connect(paths["bob"]) as conn:
            assert bdb.count_unread_archive(conn) == 1
        host._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "bob"})
        assert (await client.get("/istota/callback", follow_redirects=False)).status_code == 302
        assert (await client.post(url, json=body, headers=headers)).status_code == 200
        with bdb.connect(paths["bob"]) as conn:
            assert bdb.count_unread_archive(conn) == 0


def test_deleted_archive_acknowledgment_cannot_read_replacement(ctx, client):
    with bdb.connect(ctx.db_path) as conn:
        old_id = bdb.insert_archive(conn, briefing_name="Morning", subject="Old", body_md="Old body")
        conn.commit()
    assert client.get(f"/istota/api/briefings/archive/{old_id}").status_code == 200
    assert client.delete(f"/istota/api/briefings/archive/{old_id}").status_code == 200
    with bdb.connect(ctx.db_path) as conn:
        new_id = bdb.insert_archive(conn, briefing_name="Morning", subject="New", body_md="New body")
        conn.commit()
    assert client.post(f"/istota/api/briefings/archive/{old_id}/read").status_code == 404
    assert client.get(f"/istota/api/briefings/archive/{new_id}").json()["read_at"] is None
