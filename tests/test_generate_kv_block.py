"""End-to-end: assemble_briefing_input with a shared_block source."""

import json

from istota import db
from istota.briefings import db as bdb
from istota.briefings import ensure_initialised, resolve_for_user
from istota.briefings.generate import assemble_briefing_input
from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
from istota.config import Config, UserConfig


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )


def _ctx_with_blocks(cfg, blocks):
    ctx = resolve_for_user("stefan", cfg)
    ensure_initialised(ctx, app_config=cfg)
    with bdb.connect(ctx.db_path) as conn:
        for spec in blocks:
            bid = bdb.add_block(
                conn, briefing_name="M", title=spec["title"],
                render_mode=spec.get("render_mode", "synthesis"),
                directive=spec.get("directive"),
            )
            for s in spec.get("sources", []):
                bdb.add_source(conn, block_id=bid, kind=s["kind"],
                               config=s.get("config", {}))
        conn.commit()
    return ctx


class TestKvBlockGeneration:
    def test_renders_when_fresh(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, SHARED_BLOCK_NAMESPACE, "digest",
                json.dumps({"text": "Big news today."}), "admin",
            )
        ctx = _ctx_with_blocks(cfg, [
            {"title": "🌍 Curated", "render_mode": "structured", "sources": [
                {"kind": "shared_block", "config": {"name": "digest"}},
            ]},
        ])
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert result is not None
        assert result.rendered_blocks == 1
        assert "Big news today." in result.prompt
        # Untrusted framing rides the content by default (no stored `trusted`).
        assert "UNTRUSTED CONTENT" in result.prompt

    def test_trusted_flag_omits_untrusted_wrap(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, SHARED_BLOCK_NAMESPACE, "digest",
                json.dumps({"text": "Trusted.", "trusted": True}), "admin",
            )
        ctx = _ctx_with_blocks(cfg, [
            {"title": "Curated", "render_mode": "structured", "sources": [
                {"kind": "shared_block", "config": {"name": "digest"}},
            ]},
        ])
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert "Trusted." in result.prompt
        assert "UNTRUSTED CONTENT" not in result.prompt

    def test_omits_block_when_missing(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        ctx = _ctx_with_blocks(cfg, [
            {"title": "Curated", "sources": [
                {"kind": "shared_block", "config": {"name": "world-headlines"}},
            ]},
        ])
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert result is not None
        assert result.rendered_blocks == 0
        assert "Curated" not in result.prompt

    def test_shared_block_renders(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, "briefing_shared_blocks", "world-headlines",
                json.dumps({"items": [{"title": "Story A", "url": "http://a"}]}),
                "__system__",
            )
        ctx = _ctx_with_blocks(cfg, [
            {"title": "🌍 Headlines", "sources": [
                {"kind": "shared_block", "config": {"name": "world-headlines"}},
            ]},
        ])
        with db.get_db(cfg.db_path) as conn:
            result = assemble_briefing_input(ctx, "M", cfg, conn=conn)
        assert result.rendered_blocks == 1
        assert "Story A" in result.prompt
