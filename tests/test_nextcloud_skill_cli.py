"""Tests for the nextcloud skill CLI (python -m istota.skills.nextcloud)."""

import json
from unittest.mock import patch

import pytest

from istota.skills.nextcloud import build_parser, cmd_share_create, cmd_share_delete, cmd_share_list, cmd_share_search


@pytest.fixture(autouse=True)
def _nc_env(monkeypatch):
    monkeypatch.setenv("NC_URL", "https://cloud.example.com")
    monkeypatch.setenv("NC_USER", "istota")
    monkeypatch.setenv("NC_PASS", "secret")


class TestBuildParser:
    def test_share_list_no_path(self):
        parser = build_parser()
        args = parser.parse_args(["share", "list"])
        assert args.group == "share"
        assert args.command == "list"
        assert args.path is None

    def test_share_list_with_path(self):
        parser = build_parser()
        args = parser.parse_args(["share", "list", "--path", "/Documents"])
        assert args.path == "/Documents"

    def test_share_create_user(self):
        parser = build_parser()
        args = parser.parse_args([
            "share", "create", "--path", "/test", "--type", "user", "--with", "bob", "--permissions", "31",
        ])
        assert args.path == "/test"
        assert args.type == "user"
        assert args.with_user == "bob"
        assert args.permissions == 31

    def test_share_create_link(self):
        parser = build_parser()
        args = parser.parse_args([
            "share", "create", "--path", "/test", "--type", "link",
            "--password", "secret", "--expire", "2026-12-31", "--label", "my link",
        ])
        assert args.type == "link"
        assert args.password == "secret"
        assert args.expire == "2026-12-31"
        assert args.label == "my link"

    def test_share_delete(self):
        parser = build_parser()
        args = parser.parse_args(["share", "delete", "42"])
        assert args.command == "delete"
        assert args.share_id == 42

    def test_share_search(self):
        parser = build_parser()
        args = parser.parse_args(["share", "search", "bob"])
        assert args.command == "search"
        assert args.query == "bob"
        assert args.item_type == "file"

    def test_share_search_custom_item_type(self):
        parser = build_parser()
        args = parser.parse_args(["share", "search", "alice", "--item-type", "folder"])
        assert args.item_type == "folder"


class TestCmdShareList:
    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_success(self, mock_list, capsys):
        mock_list.return_value = [{"id": 1, "path": "/test"}]
        parser = build_parser()
        args = parser.parse_args(["share", "list"])
        cmd_share_list(args)
        output = json.loads(capsys.readouterr().out)
        assert output == [{"id": 1, "path": "/test"}]

    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_with_path_filter(self, mock_list, capsys):
        mock_list.return_value = []
        parser = build_parser()
        args = parser.parse_args(["share", "list", "--path", "/Documents"])
        cmd_share_list(args)
        mock_list.assert_called_once()
        assert mock_list.call_args.kwargs["path"] == "/Documents"

    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_failure_exits(self, mock_list):
        mock_list.return_value = None
        parser = build_parser()
        args = parser.parse_args(["share", "list"])
        with pytest.raises(SystemExit, match="1"):
            cmd_share_list(args)


class TestCmdShareCreate:
    @patch("istota.skills.nextcloud.ocs_create_share")
    def test_user_share(self, mock_create, capsys):
        mock_create.return_value = {"id": 42, "share_type": 0}
        parser = build_parser()
        args = parser.parse_args(["share", "create", "--path", "/test", "--type", "user", "--with", "bob", "--permissions", "31"])
        cmd_share_create(args)
        output = json.loads(capsys.readouterr().out)
        assert output["id"] == 42
        mock_create.assert_called_once()
        call_kw = mock_create.call_args.kwargs
        assert call_kw["share_type"] == 0
        assert call_kw["share_with"] == "bob"
        assert call_kw["permissions"] == 31

    @patch("istota.skills.nextcloud.ocs_create_public_link")
    def test_link_share(self, mock_link, capsys):
        mock_link.return_value = {"id": 99, "url": "https://nc.example.com/s/abc"}
        parser = build_parser()
        args = parser.parse_args(["share", "create", "--path", "/test", "--type", "link"])
        cmd_share_create(args)
        output = json.loads(capsys.readouterr().out)
        assert "url" in output
        mock_link.assert_called_once()

    @patch("istota.skills.nextcloud.ocs_create_public_link")
    def test_link_with_options(self, mock_link, capsys):
        mock_link.return_value = {"id": 100, "url": "https://nc.example.com/s/xyz"}
        parser = build_parser()
        args = parser.parse_args([
            "share", "create", "--path", "/test", "--type", "link",
            "--password", "pw", "--expire", "2026-06-01", "--label", "test",
        ])
        cmd_share_create(args)
        call_kw = mock_link.call_args.kwargs
        assert call_kw["password"] == "pw"
        assert call_kw["expire_date"] == "2026-06-01"
        assert call_kw["label"] == "test"

    def test_user_share_without_with_exits(self):
        parser = build_parser()
        args = parser.parse_args(["share", "create", "--path", "/test", "--type", "user"])
        with pytest.raises(SystemExit, match="1"):
            cmd_share_create(args)

    @patch("istota.skills.nextcloud.ocs_create_share")
    def test_failure_exits(self, mock_create):
        mock_create.return_value = None
        parser = build_parser()
        args = parser.parse_args(["share", "create", "--path", "/test", "--type", "user", "--with", "bob"])
        with pytest.raises(SystemExit, match="1"):
            cmd_share_create(args)


class TestCmdShareDelete:
    @patch("istota.skills.nextcloud.ocs_delete_share")
    def test_success(self, mock_delete, capsys):
        mock_delete.return_value = True
        parser = build_parser()
        args = parser.parse_args(["share", "delete", "42"])
        cmd_share_delete(args)
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "deleted"
        assert output["share_id"] == 42

    @patch("istota.skills.nextcloud.ocs_delete_share")
    def test_failure_exits(self, mock_delete):
        mock_delete.return_value = False
        parser = build_parser()
        args = parser.parse_args(["share", "delete", "999"])
        with pytest.raises(SystemExit, match="1"):
            cmd_share_delete(args)


class TestCmdShareSearch:
    @patch("istota.skills.nextcloud.ocs_search_sharees")
    def test_success(self, mock_search, capsys):
        mock_search.return_value = {
            "exact": {"users": [{"label": "Bob", "value": {"shareWith": "bob"}}]},
            "users": [],
        }
        parser = build_parser()
        args = parser.parse_args(["share", "search", "bob"])
        cmd_share_search(args)
        output = json.loads(capsys.readouterr().out)
        assert output["exact"]["users"][0]["label"] == "Bob"

    @patch("istota.skills.nextcloud.ocs_search_sharees")
    def test_failure_exits(self, mock_search):
        mock_search.return_value = None
        parser = build_parser()
        args = parser.parse_args(["share", "search", "nobody"])
        with pytest.raises(SystemExit, match="1"):
            cmd_share_search(args)


class TestEnvVarConfig:
    def test_missing_env_vars_exits(self, monkeypatch):
        monkeypatch.delenv("NC_URL")
        from istota.skills.nextcloud import _config_from_env
        with pytest.raises(SystemExit, match="1"):
            _config_from_env()

    def test_valid_env_vars(self):
        from istota.skills.nextcloud import _config_from_env
        config = _config_from_env()
        assert config.nextcloud.url == "https://cloud.example.com"
        assert config.nextcloud.username == "istota"
        assert config.nextcloud.app_password == "secret"
