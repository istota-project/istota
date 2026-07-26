"""Share expansion and the safe-link workflow (Stage 3)."""

import json
from datetime import date
from unittest.mock import patch

import pytest

from istota.config import Config, NextcloudConfig
from istota.nextcloud import OcsError, shares
from istota.skills.nextcloud import build_parser, main


@pytest.fixture
def nc_config():
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com", username="istota", app_password="pw"
        )
    )


@pytest.fixture(autouse=True)
def _nc_env(monkeypatch):
    monkeypatch.setenv("NC_URL", "https://cloud.example.com")
    monkeypatch.setenv("NC_USER", "istota")
    monkeypatch.setenv("NC_PASS", "secret")
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("NC_SHARE_DEFAULT_EXPIRE_DAYS", raising=False)


def _run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    return (json.loads(out) if out.strip() else None), code


FILE_SHARE = {
    "id": 42,
    "token": "abc123",
    "url": "https://cloud.example.com/s/abc123",
    "item_type": "file",
    "permissions": 1,
    "share_type": 3,
}

FOLDER_SHARE = {
    "id": 43,
    "token": "def456",
    "url": "https://cloud.example.com/s/def456",
    "item_type": "folder",
    "permissions": 1,
    "share_type": 3,
}


# --- direct-download URL synthesis ---


class TestDownloadUrl:
    def test_file_gets_download_suffix(self):
        assert shares.download_url(FILE_SHARE) == "https://cloud.example.com/s/abc123/download"

    def test_folder_without_file_downloads_whole_folder(self):
        assert shares.download_url(FOLDER_SHARE) == "https://cloud.example.com/s/def456/download"

    def test_folder_with_named_file_uses_query_form(self):
        url = shares.download_url(FOLDER_SHARE, file_name="notes.md")
        assert url == "https://cloud.example.com/s/def456/download?path=/&files=notes.md"

    def test_named_file_is_url_quoted(self):
        url = shares.download_url(FOLDER_SHARE, file_name="my report.pdf")
        assert url.endswith("files=my%20report.pdf")

    def test_file_name_ignored_for_a_file_share(self):
        url = shares.download_url(FILE_SHARE, file_name="notes.md")
        assert url == "https://cloud.example.com/s/abc123/download"

    def test_trailing_slash_not_doubled(self):
        share = dict(FILE_SHARE, url="https://cloud.example.com/s/abc123/")
        assert shares.download_url(share) == "https://cloud.example.com/s/abc123/download"

    def test_missing_url_is_empty(self):
        assert shares.download_url({"item_type": "file"}) == ""


# --- expiry ---


class TestExpiry:
    def test_expiry_date_from_days(self):
        assert shares.expiry_date(14, today=date(2026, 1, 1)) == "2026-01-15"

    def test_zero_days_means_no_expiry(self):
        assert shares.expiry_date(0, today=date(2026, 1, 1)) is None

    def test_negative_days_means_no_expiry(self):
        assert shares.expiry_date(-3, today=date(2026, 1, 1)) is None

    def test_clamp_below_limit_untouched(self):
        assert shares.clamp_expiry_days(7, 30) == (7, False)

    def test_clamp_above_limit(self):
        assert shares.clamp_expiry_days(60, 30) == (30, True)

    def test_no_limit_no_clamp(self):
        assert shares.clamp_expiry_days(60, None) == (60, False)

    def test_never_expiring_is_not_clamped(self):
        """--days 0 is an explicit opt-out; the server request will be refused
        by the server itself if it enforces expiry."""
        assert shares.clamp_expiry_days(0, 30) == (0, False)


class TestCreateLink:
    @patch("istota.nextcloud.shares.create_share")
    def test_applies_default_expiry_and_reports_lifecycle(self, mock_create, nc_config):
        mock_create.return_value = FILE_SHARE
        result = shares.create_link(
            nc_config, path="/Users/alice/r.pdf", days=14, today=date(2026, 1, 1)
        )

        assert mock_create.call_args.kwargs["expire_date"] == "2026-01-15"
        assert result["url"] == "https://cloud.example.com/s/abc123"
        assert result["download_url"] == "https://cloud.example.com/s/abc123/download"
        assert result["expires"] == "2026-01-15"
        assert result["token"] == "abc123"
        assert result["share_id"] == 42
        assert result["has_password"] is False
        assert result["revoke_command"] == "istota-skill nextcloud share revoke 42"
        assert "notice" not in result

    @patch("istota.nextcloud.shares.create_share")
    def test_days_zero_sends_no_expiry(self, mock_create, nc_config):
        mock_create.return_value = FILE_SHARE
        result = shares.create_link(nc_config, path="/Users/alice/r.pdf", days=0)
        assert mock_create.call_args.kwargs["expire_date"] is None
        assert result["expires"] is None

    @patch("istota.nextcloud.shares.create_share")
    def test_server_limit_clamps_and_says_so(self, mock_create, nc_config):
        mock_create.return_value = FILE_SHARE
        result = shares.create_link(
            nc_config,
            path="/Users/alice/r.pdf",
            days=60,
            server_expiry_limit=30,
            today=date(2026, 1, 1),
        )
        assert result["expires"] == "2026-01-31"
        assert "30 days" in result["notice"]

    @patch("istota.nextcloud.shares.create_share")
    def test_password_is_reported_back(self, mock_create, nc_config):
        mock_create.return_value = FILE_SHARE
        result = shares.create_link(
            nc_config, path="/Users/alice/r.pdf", days=7, password="hunter2"
        )
        assert result["has_password"] is True
        assert result["password"] == "hunter2"

    @patch("istota.nextcloud.shares.create_share")
    def test_read_only_by_default(self, mock_create, nc_config):
        mock_create.return_value = FILE_SHARE
        shares.create_link(nc_config, path="/Users/alice/r.pdf", days=7)
        assert mock_create.call_args.kwargs["permissions"] == 1

    def test_generated_password_is_long_and_random(self):
        a, b = shares.generate_password(), shares.generate_password()
        assert len(a) == 20
        assert a != b


# --- revoke ---


class TestRevoke:
    @patch("istota.nextcloud.shares.delete_share")
    def test_by_id(self, mock_delete, nc_config):
        result = shares.revoke(nc_config, share_id=42)
        mock_delete.assert_called_once_with(nc_config, 42, timeout=10.0)
        assert result["revoked"] == [{"id": 42}]

    @patch("istota.nextcloud.shares.delete_share")
    @patch("istota.nextcloud.shares.list_shares")
    def test_by_token(self, mock_list, mock_delete, nc_config):
        mock_list.return_value = [
            {"id": 1, "token": "other", "path": "/x"},
            {"id": 42, "token": "abc123", "path": "/Users/alice/r.pdf"},
        ]
        result = shares.revoke(nc_config, token="abc123")
        assert mock_delete.call_args[0][1] == 42
        assert result["revoked"][0]["path"] == "/Users/alice/r.pdf"

    @patch("istota.nextcloud.shares.list_shares")
    def test_unknown_token_raises(self, mock_list, nc_config):
        mock_list.return_value = []
        with pytest.raises(OcsError) as exc:
            shares.revoke(nc_config, token="nope")
        assert exc.value.ocs_status == 404

    @patch("istota.nextcloud.shares.delete_share")
    @patch("istota.nextcloud.shares.list_shares")
    def test_by_path_removes_every_link_and_reports_each(self, mock_list, mock_delete, nc_config):
        mock_list.return_value = [
            {"id": 1, "share_type": 3, "token": "t1", "url": "u1", "label": "one"},
            {"id": 2, "share_type": 0, "token": "", "url": ""},  # user share, untouched
            {"id": 3, "share_type": 3, "token": "t3", "url": "u3", "label": ""},
        ]
        result = shares.revoke(nc_config, path="/Users/alice/r.pdf")
        assert result["count"] == 2
        assert [r["id"] for r in result["revoked"]] == [1, 3]
        assert mock_delete.call_count == 2

    def test_nothing_specified_raises(self, nc_config):
        with pytest.raises(OcsError):
            shares.revoke(nc_config)


# --- update ---


class TestUpdate:
    @patch("istota.nextcloud.shares.ocs_put")
    def test_one_put_per_field(self, mock_put, nc_config):
        mock_put.return_value = {"id": 42}
        shares.update_share(nc_config, 42, permissions=1, expire_date="2026-09-01")
        sent = [c.kwargs["data"] for c in mock_put.call_args_list]
        assert {"permissions": 1} in sent
        assert {"expireDate": "2026-09-01"} in sent
        assert len(sent) == 2

    def test_no_fields_raises(self, nc_config):
        with pytest.raises(OcsError) as exc:
            shares.update_share(nc_config, 42)
        assert "at least one" in exc.value.message


# --- through the CLI ---


class TestShareCliVerbs:
    @patch("istota.nextcloud.shares.create_share")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_link_verb(self, mock_caps, mock_create, capsys):
        mock_caps.return_value = {"capabilities": {}}
        mock_create.return_value = FILE_SHARE
        out, code = _run(capsys, ["share", "link", "/Users/alice/r.pdf"])
        assert code == 0
        assert out["download_url"].endswith("/download")
        assert out["expires"] is not None

    @patch("istota.nextcloud.shares.create_share")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_link_password_generate_reports_password(self, mock_caps, mock_create, capsys):
        mock_caps.return_value = {"capabilities": {}}
        mock_create.return_value = FILE_SHARE
        out, _ = _run(capsys, ["share", "link", "/Users/alice/r.pdf", "--password-generate"])
        assert len(out["password"]) == 20
        assert mock_create.call_args.kwargs["password"] == out["password"]

    @patch("istota.nextcloud.shares.create_share")
    def test_link_days_zero_skips_the_capabilities_probe(self, mock_create, capsys):
        """No expiry requested means no reason to ask the server about its limit."""
        mock_create.return_value = FILE_SHARE
        with patch("istota.nextcloud.capabilities.fetch_capabilities") as mock_caps:
            out, code = _run(capsys, ["share", "link", "/Users/alice/r.pdf", "--days", "0"])
        assert code == 0
        assert out["expires"] is None
        mock_caps.assert_not_called()

    @patch("istota.nextcloud.shares.create_share")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_link_honours_configured_default_days(
        self, mock_caps, mock_create, capsys, monkeypatch
    ):
        monkeypatch.setenv("NC_SHARE_DEFAULT_EXPIRE_DAYS", "3")
        mock_caps.return_value = {"capabilities": {}}
        mock_create.return_value = FILE_SHARE
        _run(capsys, ["share", "link", "/Users/alice/r.pdf"])
        assert mock_create.call_args.kwargs["expire_date"] == shares.expiry_date(3)

    @patch("istota.nextcloud.shares.create_share")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_link_survives_a_capabilities_failure(self, mock_caps, mock_create, capsys):
        mock_caps.side_effect = OcsError("down", None, None, "/cloud/capabilities")
        mock_create.return_value = FILE_SHARE
        out, code = _run(capsys, ["share", "link", "/Users/alice/r.pdf"])
        assert code == 0
        assert out["expires"] is not None

    @patch("istota.nextcloud.shares.get_share")
    def test_get_verb(self, mock_get, capsys):
        mock_get.return_value = FILE_SHARE
        out, code = _run(capsys, ["share", "get", "42"])
        assert code == 0
        assert out["id"] == 42

    @patch("istota.nextcloud.shares.ocs_put")
    def test_update_verb(self, mock_put, capsys):
        mock_put.return_value = FILE_SHARE
        out, code = _run(capsys, ["share", "update", "42", "--permissions", "1"])
        assert code == 0
        assert mock_put.call_args.kwargs["data"] == {"permissions": 1}

    @patch("istota.nextcloud.shares.delete_share")
    def test_revoke_by_id_verb(self, mock_delete, capsys):
        out, code = _run(capsys, ["share", "revoke", "42"])
        assert code == 0
        assert out["revoked"] == [{"id": 42}]

    def test_revoke_by_path_refuses_without_confirmed(self, capsys):
        out, code = _run(capsys, ["share", "revoke", "--path", "/Users/alice/r.pdf"])
        assert code == 1
        assert out["needs_confirmation"] is True
        assert "--confirmed" in out["error"]

    @patch("istota.nextcloud.shares.delete_share")
    @patch("istota.nextcloud.shares.list_shares")
    def test_revoke_by_path_with_confirmed(self, mock_list, mock_delete, capsys):
        mock_list.return_value = [dict(FILE_SHARE)]
        out, code = _run(
            capsys, ["share", "revoke", "--path", "/Users/alice/r.pdf", "--confirmed"]
        )
        assert code == 0
        assert out["count"] == 1

    def test_revoke_with_nothing_fails(self, capsys):
        out, code = _run(capsys, ["share", "revoke"])
        assert code == 1
        assert out["status"] == "error"

    @patch("istota.nextcloud.shares.list_shares")
    def test_list_shared_with_me_flag(self, mock_list, capsys):
        mock_list.return_value = []
        _run(capsys, ["share", "list", "--shared-with-me"])
        assert mock_list.call_args.kwargs["shared_with_me"] is True

    @patch("istota.nextcloud.shares.create_share")
    def test_create_with_note_uses_the_extended_path(self, mock_create, capsys):
        mock_create.return_value = FILE_SHARE
        _run(capsys, [
            "share", "create", "--path", "/Users/alice/r.pdf", "--type", "link",
            "--note", "for review",
        ])
        assert mock_create.call_args.kwargs["note"] == "for review"

    @patch("istota.nextcloud.shares.create_share")
    def test_create_send_mail_flag(self, mock_create, capsys):
        mock_create.return_value = FILE_SHARE
        _run(capsys, [
            "share", "create", "--path", "/Users/alice/r.pdf", "--type", "email",
            "--with", "bob@example.com", "--send-mail",
        ])
        assert mock_create.call_args.kwargs["send_mail"] is True


# --- path scoping through the CLI ---


class TestPathScopingInCli:
    @patch("istota.skills.nextcloud.load_admin_users", return_value={"root"})
    @patch("istota.nextcloud.shares.create_share")
    def test_escape_is_refused_for_a_non_admin(self, mock_create, _admins, capsys):
        out, code = _run(capsys, ["share", "link", "/Users/bob/secret.pdf"])
        assert code == 1
        assert "/Users/alice" in out["error"]
        mock_create.assert_not_called()

    @patch("istota.skills.nextcloud.load_admin_users", return_value={"root"})
    @patch("istota.nextcloud.shares.create_share")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_relative_path_anchors_to_the_workspace(
        self, mock_caps, mock_create, _admins, capsys
    ):
        mock_caps.return_value = {"capabilities": {}}
        mock_create.return_value = FILE_SHARE
        _run(capsys, ["share", "link", "report.pdf"])
        assert mock_create.call_args.kwargs["path"] == "/Users/alice/report.pdf"

    @patch("istota.skills.nextcloud.load_admin_users", return_value={"alice"})
    @patch("istota.nextcloud.shares.create_share")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_admin_may_address_anything(self, mock_caps, mock_create, _admins, capsys):
        mock_caps.return_value = {"capabilities": {}}
        mock_create.return_value = FILE_SHARE
        out, code = _run(capsys, ["share", "link", "/Channels/abc/file.pdf"])
        assert code == 0
        assert mock_create.call_args.kwargs["path"] == "/Channels/abc/file.pdf"


class TestConfig:
    def test_default_expire_days_field(self):
        assert NextcloudConfig().share_default_expire_days == 14

    def test_parsed_from_toml(self, tmp_path):
        from istota.config import load_config

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[nextcloud]\nurl = "https://nc.example.com"\n'
            'username = "istota"\nshare_default_expire_days = 3\n'
        )
        config = load_config(cfg_file)
        assert config.nextcloud.share_default_expire_days == 3


class TestParser:
    def test_link_flags(self):
        args = build_parser().parse_args([
            "share", "link", "/p", "--days", "3", "--password-generate", "--file", "a.md",
        ])
        assert args.days == 3
        assert args.password_generate is True
        assert args.file == "a.md"

    def test_revoke_accepts_bare_id(self):
        args = build_parser().parse_args(["share", "revoke", "42"])
        assert args.share_id == 42
        assert args.token is None

    def test_revoke_accepts_token(self):
        args = build_parser().parse_args(["share", "revoke", "--token", "abc"])
        assert args.share_id is None
        assert args.token == "abc"
