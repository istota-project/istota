"""Tests for shared_file_organizer module."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config, NextcloudConfig, UserConfig
from istota import db
from istota.shared_file_organizer import (
    OrganizedFile,
    discover_and_organize_shared_files,
    get_file_owner,
)


PROPFIND_XML_ALICE = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:propstat>
      <d:prop>
        <oc:owner-id>alice</oc:owner-id>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>"""

PROPFIND_XML_NO_OWNER = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:propstat>
      <d:prop>
        <oc:size>1024</oc:size>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>"""


@pytest.fixture
def db_path(tmp_path):
    """Create an initialized SQLite database."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(tmp_path, db_path):
    """Create a Config with reasonable defaults for testing."""
    def _make(**overrides):
        cfg = Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://cloud.example.com",
                username="istota",
                app_password="secret",
            ),
            users={"alice": UserConfig(display_name="Alice")},
            nextcloud_mount_path=tmp_path / "mount",
        )
        for key, val in overrides.items():
            setattr(cfg, key, val)
        return cfg
    return _make


# --- get_file_owner tests ---


class TestGetFileOwner:
    @patch("istota.shared_file_organizer.httpx.request")
    def test_parses_owner_from_xml(self, mock_request, make_config):
        config = make_config()
        mock_response = MagicMock()
        mock_response.text = PROPFIND_XML_ALICE
        mock_response.raise_for_status = MagicMock()
        mock_request.return_value = mock_response

        owner = get_file_owner(config, "shared_doc.txt")

        assert owner == "alice"
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert call_args[0][0] == "PROPFIND"
        assert "shared_doc.txt" in call_args[0][1]

    @patch("istota.shared_file_organizer.httpx.request")
    def test_no_owner_in_xml(self, mock_request, make_config):
        config = make_config()
        mock_response = MagicMock()
        mock_response.text = PROPFIND_XML_NO_OWNER
        mock_response.raise_for_status = MagicMock()
        mock_request.return_value = mock_response

        owner = get_file_owner(config, "some_file.txt")
        assert owner is None

    @patch("istota.shared_file_organizer.httpx.request")
    def test_request_error_returns_none(self, mock_request, make_config):
        config = make_config()
        mock_request.side_effect = Exception("Connection refused")

        owner = get_file_owner(config, "file.txt")
        assert owner is None

    def test_no_nextcloud_config_returns_none(self):
        config = Config(nextcloud=NextcloudConfig(url="", username=""))
        owner = get_file_owner(config, "file.txt")
        assert owner is None


# --- discover_and_organize_shared_files tests ---


class TestDiscoverAndOrganize:
    @patch("istota.shared_file_organizer.ensure_user_directories_v2")
    @patch("istota.shared_file_organizer.move_file", return_value=True)
    @patch("istota.shared_file_organizer.path_exists", return_value=False)
    @patch("istota.shared_file_organizer.get_file_owner", return_value="alice")
    @patch("istota.shared_file_organizer.list_files")
    def test_organizes_file(
        self, mock_list, mock_owner, mock_exists, mock_move, mock_ensure, make_config
    ):
        config = make_config()
        mock_list.return_value = [
            {"name": "report.pdf", "is_dir": False, "size": 100}
        ]

        result = discover_and_organize_shared_files(config)

        assert len(result) == 1
        assert result[0].original_path == "report.pdf"
        assert result[0].owner_id == "alice"
        assert result[0].resource_type == "shared_file"
        assert result[0].new_path == "/Users/alice/shared/report.pdf"
        mock_move.assert_called_once()
        mock_ensure.assert_called_once_with(config, "alice")

        # Verify resource was created in DB
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM user_resources WHERE user_id = ?", ("alice",)
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["resource_type"] == "shared_file"
            assert rows[0]["resource_path"] == "/Users/alice/shared/report.pdf"

    @patch("istota.shared_file_organizer.list_files")
    def test_skips_users_directory(self, mock_list, make_config):
        config = make_config()
        mock_list.return_value = [
            {"name": "Users", "is_dir": True, "size": 0}
        ]

        result = discover_and_organize_shared_files(config)
        assert len(result) == 0

    @patch("istota.shared_file_organizer.get_file_owner", return_value=None)
    @patch("istota.shared_file_organizer.list_files")
    def test_skips_unknown_owner(self, mock_list, mock_owner, make_config):
        config = make_config()
        mock_list.return_value = [
            {"name": "mystery.txt", "is_dir": False, "size": 50}
        ]

        result = discover_and_organize_shared_files(config)
        assert len(result) == 0

    @patch("istota.shared_file_organizer.path_exists", return_value=True)
    @patch("istota.shared_file_organizer.get_file_owner", return_value="alice")
    @patch("istota.shared_file_organizer.list_files")
    def test_skips_already_organized(self, mock_list, mock_owner, mock_exists, make_config):
        config = make_config()
        mock_list.return_value = [
            {"name": "old_file.txt", "is_dir": False, "size": 100}
        ]

        result = discover_and_organize_shared_files(config)
        assert len(result) == 0

    @patch("istota.shared_file_organizer.ensure_user_directories_v2")
    @patch("istota.shared_file_organizer.move_file", return_value=False)
    @patch("istota.shared_file_organizer.path_exists", return_value=False)
    @patch("istota.shared_file_organizer.get_file_owner", return_value="alice")
    @patch("istota.shared_file_organizer.list_files")
    def test_skips_when_move_fails(
        self, mock_list, mock_owner, mock_exists, mock_move, mock_ensure, make_config
    ):
        config = make_config()
        mock_list.return_value = [
            {"name": "locked.pdf", "is_dir": False, "size": 100}
        ]

        result = discover_and_organize_shared_files(config)
        assert len(result) == 0

    @patch("istota.shared_file_organizer.ensure_user_directories_v2")
    @patch("istota.shared_file_organizer.move_file", return_value=True)
    @patch("istota.shared_file_organizer.path_exists", return_value=False)
    @patch("istota.shared_file_organizer.get_file_owner", return_value="alice")
    @patch("istota.shared_file_organizer.list_files")
    def test_creates_folder_resource(
        self, mock_list, mock_owner, mock_exists, mock_move, mock_ensure, make_config
    ):
        config = make_config()
        mock_list.return_value = [
            {"name": "Projects", "is_dir": True, "size": 0}
        ]

        result = discover_and_organize_shared_files(config)

        assert len(result) == 1
        assert result[0].is_dir is True
        assert result[0].resource_type == "folder"

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM user_resources WHERE user_id = ?", ("alice",)
            ).fetchall()
            assert rows[0]["resource_type"] == "folder"

    @patch("istota.shared_file_organizer.ensure_user_directories_v2")
    @patch("istota.shared_file_organizer.move_file", return_value=True)
    @patch("istota.shared_file_organizer.path_exists", return_value=False)
    @patch("istota.shared_file_organizer.get_file_owner", return_value="alice")
    @patch("istota.shared_file_organizer.list_files")
    def test_creates_shared_file_resource(
        self, mock_list, mock_owner, mock_exists, mock_move, mock_ensure, make_config
    ):
        config = make_config()
        mock_list.return_value = [
            {"name": "notes.md", "is_dir": False, "size": 200}
        ]

        result = discover_and_organize_shared_files(config)

        assert len(result) == 1
        assert result[0].is_dir is False
        assert result[0].resource_type == "shared_file"

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM user_resources WHERE user_id = ?", ("alice",)
            ).fetchall()
            assert rows[0]["resource_type"] == "shared_file"
            assert rows[0]["display_name"] == "notes.md"

    @patch("istota.shared_file_organizer.list_files")
    def test_empty_root(self, mock_list, make_config):
        config = make_config()
        mock_list.return_value = []

        result = discover_and_organize_shared_files(config)
        assert result == []

    @patch("istota.shared_file_organizer.get_file_owner", return_value="bob")
    @patch("istota.shared_file_organizer.list_files")
    def test_skips_unconfigured_user(self, mock_list, mock_owner, make_config):
        """Files owned by users not in config.users are skipped."""
        config = make_config()  # only has 'alice'
        mock_list.return_value = [
            {"name": "bobs_file.txt", "is_dir": False, "size": 100}
        ]

        result = discover_and_organize_shared_files(config)
        assert len(result) == 0

    @patch("istota.shared_file_organizer.list_files")
    def test_skips_users_case_insensitive(self, mock_list, make_config):
        """The 'users' directory check is case-insensitive."""
        config = make_config()
        mock_list.return_value = [
            {"name": "users", "is_dir": True, "size": 0}
        ]

        result = discover_and_organize_shared_files(config)
        assert len(result) == 0

    @patch("istota.shared_file_organizer.list_files")
    def test_list_files_error_returns_empty(self, mock_list, make_config):
        config = make_config()
        mock_list.side_effect = RuntimeError("mount unavailable")

        result = discover_and_organize_shared_files(config)
        assert result == []
