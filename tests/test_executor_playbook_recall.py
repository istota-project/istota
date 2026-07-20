"""Tests for playbook recall + cap integration (Part B, Stage 6)."""

from unittest.mock import patch

from istota import db
from istota.config import Config, PlaybooksConfig, UserConfig
from istota.executor import _apply_memory_cap, _recall_playbooks, build_prompt


def _task(source_type="talk", **kw):
    defaults = dict(
        id=1, status="running", source_type=source_type, user_id="alice",
        prompt="raise a PR for the logging change", conversation_token="room1",
    )
    defaults.update(kw)
    return db.Task(**defaults)


class _FakeResult:
    def __init__(self, content):
        self.content = content
        self.source_type = "playbook"


class TestRecallPlaybooks:
    def _config(self, tmp_path, *, enabled=True):
        return Config(
            db_path=tmp_path / "t.db",
            temp_dir=tmp_path / "temp",
            playbooks=PlaybooksConfig(enabled=enabled, recall_limit=3),
            users={"alice": UserConfig()},
        )

    def test_queries_playbook_source_type(self, tmp_path):
        config = self._config(tmp_path)
        captured = {}

        def fake_search(conn, user_id, query, **kw):
            captured.update(kw)
            captured["query"] = query
            return [_FakeResult("# Open a PR\n1. gh pr create")]

        with patch("istota.memory.search.search", side_effect=fake_search):
            out = _recall_playbooks(config, object(), _task())

        assert captured["source_types"] == ["playbook"]
        assert captured["limit"] == 3
        assert "Open a PR" in out

    def test_disabled_returns_none(self, tmp_path):
        config = self._config(tmp_path, enabled=False)
        with patch("istota.memory.search.search") as s:
            assert _recall_playbooks(config, object(), _task()) is None
            s.assert_not_called()

    def test_automated_task_returns_none(self, tmp_path):
        config = self._config(tmp_path)
        with patch("istota.memory.search.search") as s:
            assert _recall_playbooks(config, object(), _task(source_type="scheduled")) is None
            s.assert_not_called()

    def test_skip_memory_returns_none(self, tmp_path):
        config = self._config(tmp_path)
        with patch("istota.memory.search.search") as s:
            assert _recall_playbooks(config, object(), _task(), skip_memory=True) is None
            s.assert_not_called()

    def test_no_results_returns_none(self, tmp_path):
        config = self._config(tmp_path)
        with patch("istota.memory.search.search", return_value=[]):
            assert _recall_playbooks(config, object(), _task()) is None

    def test_touches_recalled_playbook_mtime(self, tmp_path):
        """ISSUE-174 Concern 3: recall stamps use-recency onto the file so the
        retention prune keys on last-use, not last-write."""
        import os
        import time

        config = self._config(tmp_path)
        pb_file = tmp_path / "open-a-pr.md"
        pb_file.write_text("# Open a PR\n1. gh pr create\n")
        past = time.time() - 40 * 86400
        os.utime(pb_file, (past, past))

        class _R:
            content = "# Open a PR\n1. gh pr create"
            source_type = "playbook"
            source_id = str(pb_file)

        with patch("istota.memory.search.search", return_value=[_R()]):
            out = _recall_playbooks(config, object(), _task())

        assert "Open a PR" in out
        assert pb_file.stat().st_mtime > past + 86400  # mtime advanced to ~now


class TestMemoryCapWithPlaybooks:
    def test_playbooks_truncated_last(self, tmp_path):
        config = Config(db_path=tmp_path / "t.db", max_memory_chars=250)
        # recalled(100)+kf(100)+dated(100)+playbooks(100) = 400, cap 250 → drop 150.
        u, d, c, r, k, pb = _apply_memory_cap(
            config, None, "D" * 100, None, "R" * 100, "K" * 100, "P" * 100,
        )
        # recalled fully dropped (100), then kf dropped (next 50 of 100 → truncated).
        assert r is None
        # playbooks survive intact (most protected).
        assert pb == "P" * 100
        assert d == "D" * 100

    def test_no_cap_passthrough(self, tmp_path):
        config = Config(db_path=tmp_path / "t.db", max_memory_chars=0)
        u, d, c, r, k, pb = _apply_memory_cap(
            config, "U", "D", "C", "R", "K", "P",
        )
        assert pb == "P"


class TestBuildPromptPlaybooks:
    def _config(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "t.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty",
            temp_dir=tmp_path / "temp",
        )

    def test_playbooks_section_rendered(self, tmp_path):
        config = self._config(tmp_path)
        prompt = build_prompt(_task(), [], config, playbooks="- # Open a PR\n  1. gh pr create")
        assert "## Learned Playbooks" in prompt
        assert "gh pr create" in prompt

    def test_no_section_when_none(self, tmp_path):
        config = self._config(tmp_path)
        prompt = build_prompt(_task(), [], config, playbooks=None)
        assert "Learned Playbooks" not in prompt
