"""Tests for the briefings `shared_block` source resolver."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from istota import db
from istota.briefings.sources import SourceContext, resolve_source
from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
from istota.config import Config, UserConfig


@pytest.fixture
def conn(db_path):
    with db.get_db(db_path) as c:
        yield c


def _ctx(db_path, conn, *, now=None, shared_blocks=None):
    cfg = Config(
        db_path=db_path,
        nextcloud_mount_path=db_path.parent / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
    )
    if shared_blocks is not None:
        cfg.briefing_shared_blocks = shared_blocks
    return SourceContext(app_config=cfg, user_id="stefan", conn=conn, now=now)


def _seed(conn, name, value, *, written_by="__system__"):
    db.shared_kv_set(conn, SHARED_BLOCK_NAMESPACE, name, json.dumps(value), written_by)


def _block(db_path, conn, config, **ctx_kwargs):
    return resolve_source("shared_block", config, _ctx(db_path, conn, **ctx_kwargs))


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


class TestGranularity:
    def test_items_shape(self, db_path, conn):
        _seed(conn, "k", {"items": [{"title": "A", "summary": "s", "url": "http://x"}]})
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.items and gs.items[0]["title"] == "A"
        assert not gs.text

    def test_text_shape(self, db_path, conn):
        _seed(conn, "k", {"text": "body"})
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.text == "body"
        assert not gs.items

    def test_bare_string_is_text(self, db_path, conn):
        _seed(conn, "k", "just text")
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.text == "just text"

    def test_bare_list_is_items(self, db_path, conn):
        _seed(conn, "k", [{"title": "X"}])
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.items[0]["title"] == "X"

    def test_non_dict_item_coerced(self, db_path, conn):
        _seed(conn, "k", ["headline one"])
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.items[0]["title"] == "headline one"

    def test_malformed_object_empty(self, db_path, conn):
        _seed(conn, "k", {"nope": 1})
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.ok is False
        assert "unusable" in gs.provenance

    def test_invalid_json_empty(self, db_path, conn):
        conn.execute(
            "INSERT INTO shared_kv (namespace, key, value, written_by) VALUES (?,?,?,?)",
            (SHARED_BLOCK_NAMESPACE, "k", "not json", "__system__"),
        )
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.ok is False
        assert "malformed" in gs.provenance


class TestFreshness:
    def _seed_aged(self, conn, name, hours_ago):
        ts = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO shared_kv (namespace, key, value, written_by, updated_at) "
            "VALUES (?,?,?,?,?)",
            (SHARED_BLOCK_NAMESPACE, name, json.dumps({"text": "content"}), "__system__", ts),
        )

    def test_fresh_within_window(self, db_path, conn):
        self._seed_aged(conn, "k", 2)
        gs = _block(db_path, conn, {"name": "k", "max_age_hours": 12}, now=NOW)
        assert gs.ok
        assert "written 2h ago" in gs.provenance

    def test_stale_dropped(self, db_path, conn):
        self._seed_aged(conn, "k", 20)
        gs = _block(db_path, conn, {"name": "k", "max_age_hours": 12}, now=NOW)
        assert gs.ok is False
        assert "stale" in gs.provenance

    def test_max_age_zero_disables_check(self, db_path, conn):
        self._seed_aged(conn, "k", 100)
        gs = _block(db_path, conn, {"name": "k", "max_age_hours": 0}, now=NOW)
        assert gs.ok

    def test_absent_max_age_disables_check(self, db_path, conn):
        self._seed_aged(conn, "k", 100)
        gs = _block(db_path, conn, {"name": "k"}, now=NOW)
        assert gs.ok


class TestMissingAndUntrusted:
    def test_missing_key_empty(self, db_path, conn):
        gs = _block(db_path, conn, {"name": "gone"})
        assert gs.ok is False
        assert "no shared KV" in gs.provenance

    def test_untrusted_by_default(self, db_path, conn):
        _seed(conn, "k", {"text": "t"})
        gs = _block(db_path, conn, {"name": "k"})
        assert gs.untrusted is True

    def test_no_conn_fails_soft(self, db_path):
        cfg = Config(db_path=db_path, users={"stefan": UserConfig()})
        ctx = SourceContext(app_config=cfg, user_id="stefan", conn=None)
        gs = resolve_source("shared_block", {"name": "k"}, ctx)
        assert gs.ok is False


class TestSharedBlockSugar:
    def test_reads_briefing_shared_blocks_namespace(self, db_path, conn):
        _seed(conn, "world-headlines", {"text": "news"})
        gs = _block(db_path, conn, {"name": "world-headlines"})
        assert gs.ok
        assert "news" in gs.text

    def test_unknown_configured_block_clearer_note(self, db_path, conn):
        class _Block:
            name = "markets-summary"

        gs = _block(
            db_path, conn, {"name": "world-headlines"}, shared_blocks=[_Block()]
        )
        assert gs.ok is False
        assert "unknown shared block 'world-headlines'" in gs.provenance

    def test_missing_name(self, db_path, conn):
        gs = _block(db_path, conn, {})
        assert gs.ok is False
        assert "missing name" in gs.provenance

    def test_custom_published_key_not_unknown(self, db_path, conn):
        # A publish_shared_kv job (or the escape-hatch curation script) wrote a
        # key with no shared_block_configs definition. The configured set lists
        # only some other block, but the live key is valid — must not be flagged
        # "unknown".
        _seed(conn, "film-digest", {"text": "film news", "trusted": False}, written_by="stefan")

        class _Block:
            name = "markets-summary"

        gs = _block(db_path, conn, {"name": "film-digest"}, shared_blocks=[_Block()])
        assert gs.ok
        assert "film news" in gs.text


class TestTrustFromStoredValue:
    def test_stored_trusted_true_honored(self, db_path, conn):
        _seed(conn, "mk", {"text": "table", "trusted": True}, written_by="stefan")
        gs = _block(db_path, conn, {"name": "mk"})
        assert gs.ok
        assert gs.untrusted is False  # honored from the stored value

    def test_stored_trusted_false_wraps(self, db_path, conn):
        # A consuming user can never flip trust — it's the stored value's flag.
        _seed(conn, "mk", {"text": "web stuff", "trusted": False}, written_by="stefan")
        gs = _block(db_path, conn, {"name": "mk"})
        assert gs.ok
        assert gs.untrusted is True
