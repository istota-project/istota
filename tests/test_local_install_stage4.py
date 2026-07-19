"""Stage 4 tests: packaging static-dir fallback + workspace-as-local-folder polish."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from istota.config import Config, NextcloudConfig, UserConfig


# ---------------------------------------------------------------------------
# Static-dir resolution (packaging)
# ---------------------------------------------------------------------------

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)


@_needs_web_deps
class TestStaticDirResolution:
    def test_env_override_wins(self, tmp_path):
        from istota.web_app import _pick_static_dir
        repo = tmp_path / "repo"
        repo.mkdir()
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        got = _pick_static_dir(str(tmp_path / "env"), repo, pkg)
        assert got == tmp_path / "env"

    def test_repo_build_preferred_over_packaged(self, tmp_path):
        from istota.web_app import _pick_static_dir
        repo = tmp_path / "repo"
        repo.mkdir()
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        assert _pick_static_dir("", repo, pkg) == repo

    def test_packaged_fallback_when_repo_absent(self, tmp_path):
        from istota.web_app import _pick_static_dir
        repo = tmp_path / "repo"  # not created
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        assert _pick_static_dir("", repo, pkg) == pkg

    def test_falls_back_to_repo_path_when_neither_exists(self, tmp_path):
        from istota.web_app import _pick_static_dir
        repo = tmp_path / "repo"
        pkg = tmp_path / "pkg"
        assert _pick_static_dir("", repo, pkg) == repo


# ---------------------------------------------------------------------------
# Workspace as a local folder
# ---------------------------------------------------------------------------


def _local_config(tmp_path, nc_url=""):
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "workspace",
        nextcloud=NextcloudConfig(url=nc_url),
        users={"stefan": UserConfig(display_name="Stefan")},
        bot_name="Istota",
    )


class TestWorkspaceLocalFolder:
    def test_full_layout_created(self, tmp_path):
        from istota.storage import ensure_user_directories_v2
        cfg = _local_config(tmp_path)
        ensure_user_directories_v2(cfg, "stefan")
        base = cfg.nextcloud_mount_path / "Users" / "stefan"
        assert (base / "inbox").is_dir()
        assert (base / "memories").is_dir()
        assert (base / "shared").is_dir()
        bot = base / cfg.bot_dir_name
        assert (bot / "config").is_dir()
        assert (bot / "scripts").is_dir()
        assert (bot / "examples").is_dir()

    def test_share_skipped_when_nc_blank(self, tmp_path, monkeypatch):
        import istota.storage as storage
        called = MagicMock()
        monkeypatch.setattr(storage, "share_folder_with_user", called)
        cfg = _local_config(tmp_path, nc_url="")
        storage.ensure_user_directories_v2(cfg, "stefan")
        called.assert_not_called()

    def test_share_called_when_nc_configured(self, tmp_path, monkeypatch):
        import istota.storage as storage
        called = MagicMock(return_value=True)
        monkeypatch.setattr(storage, "share_folder_with_user", called)
        cfg = _local_config(tmp_path, nc_url="https://cloud.example.com")
        storage.ensure_user_directories_v2(cfg, "stefan")
        called.assert_called_once()

    def test_ensure_workspace_seeds_memory_once(self, tmp_path):
        from istota.storage import (
            ensure_workspace_for_user,
            get_memory_line_count_v2,
        )
        cfg = _local_config(tmp_path)
        assert get_memory_line_count_v2(cfg, "stefan") is None
        ensure_workspace_for_user(cfg, "stefan")
        assert get_memory_line_count_v2(cfg, "stefan") is not None

    def test_ensure_workspace_does_not_clobber_memory(self, tmp_path):
        from istota.storage import (
            ensure_workspace_for_user,
            get_user_memory_path,
            _get_mount_path,
        )
        cfg = _local_config(tmp_path)
        ensure_workspace_for_user(cfg, "stefan")
        mem = _get_mount_path(cfg, get_user_memory_path("stefan", cfg.bot_dir_name))
        mem.write_text("# my custom memory\n")
        # Re-run must not overwrite.
        ensure_workspace_for_user(cfg, "stefan")
        assert mem.read_text() == "# my custom memory\n"


# ---------------------------------------------------------------------------
# Local extras group is declared
# ---------------------------------------------------------------------------


class TestLocalExtras:
    def test_local_extras_declared(self):
        import tomllib
        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text())
        extras = data["project"]["optional-dependencies"]
        assert "local" in extras
        joined = " ".join(extras["local"])
        for part in ("web", "feeds", "calendar", "email", "markets"):
            assert part in joined

    def test_wheel_artifacts_include_web_static(self):
        import tomllib
        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text())
        artifacts = data["tool"]["hatch"]["build"]["targets"]["wheel"].get("artifacts", [])
        assert any("web_static" in a for a in artifacts)
