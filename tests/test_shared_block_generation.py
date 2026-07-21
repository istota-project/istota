"""Tests for shared-block generation (assemble_shared_block_input / run_shared_block)."""

import json

import pytest

from istota import db
from istota.briefings import shared_blocks
from istota.briefings.shared_blocks import (
    assemble_shared_block_input,
    run_shared_block,
)
from istota.briefings.sources import GatheredSource
from istota.config import BriefingSharedBlock, Config, UserConfig


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )


class TestAssemble:
    def test_drops_disallowed_kinds(self, tmp_path, caplog, monkeypatch):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        # rss + calendar are user-specific → dropped; markets is allowed.
        block = BriefingSharedBlock(
            name="mix", cron="0 6 * * *", title="Mix",
            sources=[
                {"kind": "rss", "config": {}},
                {"kind": "calendar", "config": {}},
                {"kind": "markets", "config": {}},
            ],
        )

        def _fake_gather(config, sources, now):
            # Only the markets source should reach the gather.
            assert [s["kind"] for s in sources] == ["markets"]
            return [GatheredSource(kind="markets", title="Markets", text="DOW up")]

        monkeypatch.setattr(shared_blocks, "_gather_shared", _fake_gather)
        prompt = assemble_shared_block_input(block, cfg)
        assert prompt is not None
        assert "DOW up" in prompt
        assert "Mix" in prompt

    def test_no_usable_sources_returns_none(self, tmp_path):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="bad", cron="0 6 * * *",
            sources=[{"kind": "rss", "config": {}}],
        )
        assert assemble_shared_block_input(block, cfg) is None

    def test_all_empty_gather_returns_none(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="empty", cron="0 6 * * *",
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", ok=False),
            ],
        )
        assert assemble_shared_block_input(block, cfg) is None


class TestGatherShared:
    def test_gather_preserves_source_order_under_stagger(
        self, tmp_path, monkeypatch
    ):
        """``_gather_shared`` must reassemble by source index, not completion.

        The first source sleeps longest, so a completion-order implementation
        returns it last. Reassembly by slot keeps configured order.
        """
        import time

        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        sources = [
            {"kind": "markets", "config": {}},
            {"kind": "browse", "config": {}},
            {"kind": "email", "config": {}},
        ]
        # Delay is inversely proportional to index → completion order reverses
        # configured order.
        delays = {"markets": 0.15, "browse": 0.05, "email": 0.0}

        def _fake_resolve(kind, cfg_, ctx):
            time.sleep(delays[kind])
            return GatheredSource(kind=kind, title=kind, text=kind)

        monkeypatch.setattr(shared_blocks, "resolve_source", _fake_resolve)
        gathered = shared_blocks._gather_shared(cfg, sources, None)
        assert [g.kind for g in gathered] == ["markets", "browse", "email"]


class TestRunSharedBlock:
    def test_returns_text_dict(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="hl", cron="0 6 * * *",
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="data"),
            ],
        )
        monkeypatch.setattr(
            shared_blocks, "_run_section_brain",
            lambda config, prompt, label: (True, "📈 Markets\nUp today."),
        )
        result = run_shared_block(block, cfg)
        assert result == {"text": "📈 Markets\nUp today.", "trusted": False}

    def test_trusted_flag_flows_from_def(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="hl", cron="0 6 * * *", trusted=True,
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="data"),
            ],
        )
        monkeypatch.setattr(
            shared_blocks, "_run_section_brain",
            lambda config, prompt, label: (True, "synth"),
        )
        result = run_shared_block(block, cfg)
        assert result == {"text": "synth", "trusted": True}

    def test_failed_brain_returns_none(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="hl", cron="0 6 * * *",
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="data"),
            ],
        )
        monkeypatch.setattr(
            shared_blocks, "_run_section_brain",
            lambda config, prompt, label: (False, ""),
        )
        assert run_shared_block(block, cfg) is None


class TestVerbatimStructured:
    def test_structured_skips_brain(self, tmp_path, monkeypatch):
        """A structured block stores gathered text verbatim with NO brain call."""
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="mk", cron="0 6 * * *", render_mode="structured", trusted=True,
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="📈 DOW +1%"),
            ],
        )

        def _boom(*a, **k):
            raise AssertionError("brain must not be called for structured blocks")

        monkeypatch.setattr(shared_blocks, "_run_section_brain", _boom)
        result = run_shared_block(block, cfg)
        assert result == {"text": "📈 DOW +1%", "trusted": True}

    def test_structured_concats_in_slot_order(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="multi", cron="0 6 * * *", render_mode="structured",
            sources=[
                {"kind": "markets", "config": {}},
                {"kind": "browse", "config": {}},
            ],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", text="first"),
                GatheredSource(kind="browse", title="B", text="second"),
            ],
        )
        monkeypatch.setattr(
            shared_blocks, "_run_section_brain",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no brain")),
        )
        result = run_shared_block(block, cfg)
        assert result == {"text": "first\n\nsecond", "trusted": False}

    def test_structured_empty_gather_returns_none(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="empty", cron="0 6 * * *", render_mode="structured",
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(kind="markets", title="M", ok=False),
            ],
        )
        assert run_shared_block(block, cfg) is None

    def test_structured_items_rendered_as_bullets(self, tmp_path, monkeypatch):
        """Defensive: a structured source returning items (no shared kind does
        today) renders a bullet list so verbatim has content."""
        cfg = _config(tmp_path)
        block = BriefingSharedBlock(
            name="it", cron="0 6 * * *", render_mode="structured",
            sources=[{"kind": "markets", "config": {}}],
        )
        monkeypatch.setattr(
            shared_blocks, "_gather_shared",
            lambda config, sources, now: [
                GatheredSource(
                    kind="markets", title="M",
                    items=[{"title": "One"}, {"title": "Two"}],
                ),
            ],
        )
        monkeypatch.setattr(
            shared_blocks, "_run_section_brain",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no brain")),
        )
        result = run_shared_block(block, cfg)
        assert result == {"text": "- One\n- Two", "trusted": False}


class TestSchedulerWorker:
    def test_generate_writes_shared_kv(self, tmp_path, monkeypatch):
        from istota import scheduler
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        block = BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[])
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda b, config, now=None: {"text": "generated"},
        )
        scheduler._generate_shared_block(cfg, block)
        with db.get_db(cfg.db_path) as conn:
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "hl")
        assert json.loads(row["value"]) == {"text": "generated"}
        assert row["written_by"] == "__system__"

    def test_none_result_preserves_prior(self, tmp_path, monkeypatch):
        from istota import scheduler
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, "briefing_shared_blocks", "hl",
                json.dumps({"text": "old"}), "__system__",
            )
        block = BriefingSharedBlock(name="hl", cron="0 6 * * *", sources=[])
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda b, config, now=None: None,
        )
        scheduler._generate_shared_block(cfg, block)
        with db.get_db(cfg.db_path) as conn:
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "hl")
        assert json.loads(row["value"]) == {"text": "old"}  # last-known-good
