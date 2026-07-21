"""Tests for the briefings `kv` / `shared_block` source resolvers."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from istota import db
from istota.briefings.sources import SourceContext, resolve_source
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


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


class TestScope:
    def test_shared_scope_reads_shared_kv(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps({"text": "hello"}), "admin")
        gs = resolve_source(
            "kv", {"scope": "shared", "namespace": "ns", "key": "k"},
            _ctx(db_path, conn),
        )
        assert gs.ok
        assert "hello" in gs.text

    def test_own_scope_reads_peruser_kv(self, db_path, conn):
        db.kv_set(conn, "stefan", "ns", "k", json.dumps({"text": "mine"}))
        gs = resolve_source(
            "kv", {"scope": "own", "namespace": "ns", "key": "k"},
            _ctx(db_path, conn),
        )
        assert gs.ok
        assert "mine" in gs.text
        assert "written_by" not in gs.provenance
        assert "by " not in gs.provenance

    def test_own_scope_uses_ctx_user_not_config(self, db_path, conn):
        # A source config can't point at another user's personal KV.
        db.kv_set(conn, "other", "ns", "k", json.dumps({"text": "secret"}))
        gs = resolve_source(
            "kv", {"scope": "own", "namespace": "ns", "key": "k", "user_id": "other"},
            _ctx(db_path, conn),
        )
        assert gs.ok is False  # stefan has no such key


class TestGranularity:
    def test_items_shape(self, db_path, conn):
        val = {"items": [{"title": "A", "summary": "s", "url": "http://x"}]}
        db.shared_kv_set(conn, "ns", "k", json.dumps(val), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.items and gs.items[0]["title"] == "A"
        assert not gs.text

    def test_text_shape(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps({"text": "body"}), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.text == "body"
        assert not gs.items

    def test_bare_string_is_text(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps("just text"), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.text == "just text"

    def test_bare_list_is_items(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps([{"title": "X"}]), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.items[0]["title"] == "X"

    def test_non_dict_item_coerced(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps(["headline one"]), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.items[0]["title"] == "headline one"

    def test_malformed_object_empty(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps({"nope": 1}), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.ok is False
        assert "unusable" in gs.provenance

    def test_invalid_json_empty(self, db_path, conn):
        conn.execute(
            "INSERT INTO shared_kv (namespace, key, value, written_by) VALUES (?,?,?,?)",
            ("ns", "k", "not json", "admin"),
        )
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.ok is False
        assert "malformed" in gs.provenance


class TestFreshness:
    def _seed_aged(self, conn, hours_ago):
        ts = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO shared_kv (namespace, key, value, written_by, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("ns", "k", json.dumps({"text": "content"}), "admin", ts),
        )

    def test_fresh_within_window(self, db_path, conn):
        self._seed_aged(conn, 2)
        gs = resolve_source(
            "kv", {"namespace": "ns", "key": "k", "max_age_hours": 12},
            _ctx(db_path, conn, now=NOW),
        )
        assert gs.ok
        assert "written 2h ago" in gs.provenance

    def test_stale_dropped(self, db_path, conn):
        self._seed_aged(conn, 20)
        gs = resolve_source(
            "kv", {"namespace": "ns", "key": "k", "max_age_hours": 12},
            _ctx(db_path, conn, now=NOW),
        )
        assert gs.ok is False
        assert "stale" in gs.provenance

    def test_max_age_zero_disables_check(self, db_path, conn):
        self._seed_aged(conn, 100)
        gs = resolve_source(
            "kv", {"namespace": "ns", "key": "k", "max_age_hours": 0},
            _ctx(db_path, conn, now=NOW),
        )
        assert gs.ok

    def test_absent_max_age_disables_check(self, db_path, conn):
        self._seed_aged(conn, 100)
        gs = resolve_source(
            "kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn, now=NOW),
        )
        assert gs.ok


class TestMissingAndUntrusted:
    def test_missing_key_empty(self, db_path, conn):
        gs = resolve_source("kv", {"namespace": "ns", "key": "gone"}, _ctx(db_path, conn))
        assert gs.ok is False
        assert "no shared KV" in gs.provenance

    def test_untrusted_by_default(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps({"text": "t"}), "admin")
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, _ctx(db_path, conn))
        assert gs.untrusted is True

    def test_trusted_flag_disables_wrap(self, db_path, conn):
        db.shared_kv_set(conn, "ns", "k", json.dumps({"text": "t"}), "admin")
        gs = resolve_source(
            "kv", {"namespace": "ns", "key": "k", "trusted": True}, _ctx(db_path, conn),
        )
        assert gs.untrusted is False

    def test_no_conn_fails_soft(self, db_path):
        cfg = Config(db_path=db_path, users={"stefan": UserConfig()})
        ctx = SourceContext(app_config=cfg, user_id="stefan", conn=None)
        gs = resolve_source("kv", {"namespace": "ns", "key": "k"}, ctx)
        assert gs.ok is False


class TestSharedBlockSugar:
    def test_reads_briefing_shared_blocks_namespace(self, db_path, conn):
        db.shared_kv_set(
            conn, "briefing_shared_blocks", "world-headlines",
            json.dumps({"text": "news"}), "__system__",
        )
        gs = resolve_source(
            "shared_block", {"name": "world-headlines"}, _ctx(db_path, conn),
        )
        assert gs.ok
        assert "news" in gs.text

    def test_unknown_configured_block_clearer_note(self, db_path, conn):
        class _Block:
            name = "markets-summary"

        gs = resolve_source(
            "shared_block", {"name": "world-headlines"},
            _ctx(db_path, conn, shared_blocks=[_Block()]),
        )
        assert gs.ok is False
        assert "unknown shared block 'world-headlines'" in gs.provenance

    def test_missing_name(self, db_path, conn):
        gs = resolve_source("shared_block", {}, _ctx(db_path, conn))
        assert gs.ok is False
        assert "missing name" in gs.provenance

    def test_custom_published_key_not_unknown(self, db_path, conn):
        # A publish_shared_kv job wrote a key with no shared_block_configs
        # definition. The configured set lists only some other block, but the
        # live key is valid — must not be flagged "unknown".
        db.shared_kv_set(
            conn, "briefing_shared_blocks", "film-digest",
            json.dumps({"text": "film news", "trusted": False}), "stefan",
        )

        class _Block:
            name = "markets-summary"

        gs = resolve_source(
            "shared_block", {"name": "film-digest"},
            _ctx(db_path, conn, shared_blocks=[_Block()]),
        )
        assert gs.ok
        assert "film news" in gs.text


class TestTrustFromStoredValue:
    def test_stored_trusted_true_honored(self, db_path, conn):
        db.shared_kv_set(
            conn, "briefing_shared_blocks", "mk",
            json.dumps({"text": "table", "trusted": True}), "stefan",
        )
        gs = resolve_source("shared_block", {"name": "mk"}, _ctx(db_path, conn))
        assert gs.ok
        assert gs.untrusted is False  # honored from the stored value

    def test_consumer_trusted_ignored_for_shared_block_ns(self, db_path, conn):
        # Stored trusted=False; a consumer asking trusted=True can't unwrap it.
        db.shared_kv_set(
            conn, "briefing_shared_blocks", "mk",
            json.dumps({"text": "web stuff", "trusted": False}), "stefan",
        )
        gs = resolve_source(
            "kv",
            {"scope": "shared", "namespace": "briefing_shared_blocks",
             "key": "mk", "trusted": True},
            _ctx(db_path, conn),
        )
        assert gs.ok
        assert gs.untrusted is True  # consumer's trusted:true ignored here

    def test_other_namespace_keeps_consumer_trusted(self, db_path, conn):
        # A non-shared-block namespace keeps the old consumer-set behavior.
        db.shared_kv_set(conn, "ns", "k", json.dumps({"text": "t"}), "admin")
        gs = resolve_source(
            "kv", {"namespace": "ns", "key": "k", "trusted": True},
            _ctx(db_path, conn),
        )
        assert gs.untrusted is False
