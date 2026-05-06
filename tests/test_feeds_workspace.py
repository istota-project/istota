"""Tests for feeds workspace synthesis."""

from istota.feeds.workspace import synthesize_feeds_context


class TestSynthesizeFeedsContext:
    def test_defaults(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        assert ctx.user_id == "stefan"
        assert ctx.data_dir == (tmp_path / "feeds").resolve()
        assert ctx.db_path == ctx.data_dir / "data" / "feeds.db"

    def test_explicit_data_dir(self, tmp_path):
        explicit = tmp_path / "elsewhere" / "feeds"
        ctx = synthesize_feeds_context("stefan", tmp_path, data_dir=explicit)
        assert ctx.data_dir == explicit.resolve()
        assert ctx.db_path == explicit.resolve() / "data" / "feeds.db"

    def test_explicit_db_path(self, tmp_path):
        explicit = tmp_path / "custom.db"
        ctx = synthesize_feeds_context("stefan", tmp_path, db_path=explicit)
        assert ctx.db_path == explicit.resolve()

    def test_tumblr_api_key_passthrough(self, tmp_path):
        ctx = synthesize_feeds_context(
            "stefan", tmp_path, tumblr_api_key="sk_test",
        )
        assert ctx.tumblr_api_key == "sk_test"

    def test_ensure_dirs_creates_data_and_db_parent(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        ctx.ensure_dirs()
        assert ctx.data_dir.is_dir()
        assert ctx.db_path.parent.is_dir()
