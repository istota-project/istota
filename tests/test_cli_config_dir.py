"""The per-task `CLAUDE_CONFIG_DIR` helpers, moved out of the tmux brain."""

import json
import os
import stat
from pathlib import Path

import pytest

from istota.brain import _cli_config_dir as cfgdir


class TestWriteNewFile:
    def test_writes_private(self, tmp_path):
        target = tmp_path / "settings.json"
        cfgdir._write_new_file(target, "{}")
        assert target.read_text() == "{}"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_refuses_an_existing_file(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("planted")
        with pytest.raises(FileExistsError):
            cfgdir._write_new_file(target, "{}")
        assert target.read_text() == "planted"

    def test_refuses_a_planted_symlink(self, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("untouched")
        link = tmp_path / "settings.json"
        link.symlink_to(victim)
        with pytest.raises(OSError):
            cfgdir._write_new_file(link, "{}")
        assert victim.read_text() == "untouched"


class TestSeedOnboarding:
    def test_marks_install_and_project_onboarded(self, tmp_path):
        cfgdir._seed_onboarding(tmp_path, Path("/data/tmp/alice"))
        seeded = json.loads((tmp_path / ".claude.json").read_text())
        assert seeded["theme"] == "dark"
        assert seeded["hasCompletedOnboarding"] is True
        assert seeded["bypassPermissionsModeAccepted"] is True
        project = seeded["projects"]["/data/tmp/alice"]
        assert project["hasTrustDialogAccepted"] is True
        assert project["hasCompletedProjectOnboarding"] is True

    def test_a_failed_write_is_not_swallowed(self, tmp_path):
        (tmp_path / ".claude.json").write_text("planted")
        with pytest.raises(FileExistsError):
            cfgdir._seed_onboarding(tmp_path, tmp_path)


class TestLinkCredentials:
    def test_links_the_home_copy(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        token = home / ".claude" / ".credentials.json"
        token.write_text("{}")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfgdir._link_credentials(config_dir, str(home))
        link = config_dir / ".credentials.json"
        assert link.is_symlink()
        assert os.readlink(link) == str(token)

    @pytest.mark.parametrize("home", ["", "missing"])
    def test_nothing_without_a_file(self, tmp_path, home):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfgdir._link_credentials(config_dir, str(tmp_path / home) if home else "")
        assert list(config_dir.iterdir()) == []
