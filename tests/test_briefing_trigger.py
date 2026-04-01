"""Tests for briefing trigger file processing."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from istota.config import BriefingConfig, Config, UserConfig


class TestCheckBriefingTriggers:
    def _make_config(self, tmp_path):
        users_dir = tmp_path / "users"
        users_dir.mkdir()
        cfg = Config(users_dir=users_dir, db_path=tmp_path / "test.db")
        cfg.users["alice"] = UserConfig(
            display_name="Alice",
            timezone="UTC",
            briefings=[
                BriefingConfig(
                    name="morning",
                    cron="0 6 * * *",
                    conversation_token="room123",
                    output="talk",
                    components={"calendar": True},
                ),
            ],
        )
        return cfg

    @patch("istota.scheduler.build_briefing_prompt", return_value="test prompt")
    @patch("istota.scheduler.db")
    def test_processes_valid_trigger(self, mock_db, mock_build, tmp_path):
        cfg = self._make_config(tmp_path)
        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir()
        (triggers_dir / "briefing_alice_morning.json").write_text(
            json.dumps({"user_id": "alice", "briefing_name": "morning"})
        )

        mock_conn = MagicMock()
        mock_db.get_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.get_db.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.create_task.return_value = 42

        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)

        assert result == [42]
        mock_build.assert_called_once()
        mock_db.create_task.assert_called_once()
        # Trigger file should be deleted
        assert not (triggers_dir / "briefing_alice_morning.json").exists()

    def test_no_triggers_dir(self, tmp_path):
        cfg = self._make_config(tmp_path)

        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)
        assert result == []

    def test_empty_triggers_dir(self, tmp_path):
        cfg = self._make_config(tmp_path)
        (tmp_path / "triggers").mkdir()

        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)
        assert result == []

    def test_invalid_trigger_deleted(self, tmp_path):
        cfg = self._make_config(tmp_path)
        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir()
        (triggers_dir / "briefing_bad.json").write_text(
            json.dumps({"user_id": "", "briefing_name": ""})
        )

        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)
        assert result == []
        assert not (triggers_dir / "briefing_bad.json").exists()

    def test_unknown_user_trigger_deleted(self, tmp_path):
        cfg = self._make_config(tmp_path)
        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir()
        (triggers_dir / "briefing_ghost_morning.json").write_text(
            json.dumps({"user_id": "ghost", "briefing_name": "morning"})
        )

        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)
        assert result == []
        assert not (triggers_dir / "briefing_ghost_morning.json").exists()

    def test_unknown_briefing_trigger_deleted(self, tmp_path):
        cfg = self._make_config(tmp_path)
        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir()
        (triggers_dir / "briefing_alice_nonexistent.json").write_text(
            json.dumps({"user_id": "alice", "briefing_name": "nonexistent"})
        )

        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)
        assert result == []
        assert not (triggers_dir / "briefing_alice_nonexistent.json").exists()

    def test_no_users_dir(self):
        cfg = Config()
        from istota.scheduler import check_briefing_triggers
        result = check_briefing_triggers(cfg.db_path, cfg)
        assert result == []
