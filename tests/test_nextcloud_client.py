"""Tests for nextcloud_client module — shared Nextcloud HTTP plumbing."""

from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config, NextcloudConfig


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
def nc_config(tmp_path):
    return Config(
        nextcloud_mount_path=tmp_path / "mount",
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="istota",
            app_password="secret",
        ),
    )


@pytest.fixture
def empty_config():
    return Config(nextcloud=NextcloudConfig(url="", username="", app_password=""))


# --- Helper tests ---


class TestHelpers:
    def test_nc_configured_true(self, nc_config):
        from istota.nextcloud_client import _nc_configured
        assert _nc_configured(nc_config) is True

    def test_nc_configured_false_no_url(self, empty_config):
        from istota.nextcloud_client import _nc_configured
        assert _nc_configured(empty_config) is False

    def test_nc_configured_false_no_username(self):
        from istota.nextcloud_client import _nc_configured
        config = Config(nextcloud=NextcloudConfig(url="https://nc.example.com", username=""))
        assert _nc_configured(config) is False

    def test_nc_auth(self, nc_config):
        from istota.nextcloud_client import _nc_auth
        assert _nc_auth(nc_config) == ("istota", "secret")

    def test_nc_base_url_strips_trailing_slash(self):
        from istota.nextcloud_client import _nc_base_url
        config = Config(nextcloud=NextcloudConfig(url="https://nc.example.com/"))
        assert _nc_base_url(config) == "https://nc.example.com"

    def test_ocs_headers(self):
        from istota.nextcloud_client import _ocs_headers
        headers = _ocs_headers()
        assert headers["OCS-APIRequest"] == "true"
        assert headers["Accept"] == "application/json"


# --- ocs_get tests ---


class TestOcsGet:
    @patch("istota.nextcloud_client.httpx.get")
    def test_success(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_get

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ocs": {"data": {"displayname": "Alice", "email": "alice@example.com"}}
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = ocs_get(nc_config, "/cloud/users/alice")
        assert result == {"displayname": "Alice", "email": "alice@example.com"}

        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert "/cloud/users/alice" in call_kwargs[0][0]
        assert call_kwargs.kwargs["auth"] == ("istota", "secret")

    @patch("istota.nextcloud_client.httpx.get")
    def test_with_params(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_get

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ocs": {"data": []}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        ocs_get(nc_config, "/apps/files_sharing/api/v1/shares", params={"path": "/test"})
        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["params"] == {"path": "/test"}

    @patch("istota.nextcloud_client.httpx.get")
    def test_error_returns_none(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_get

        mock_get.side_effect = Exception("Connection refused")
        result = ocs_get(nc_config, "/cloud/users/alice")
        assert result is None

    def test_not_configured_returns_none(self, empty_config):
        from istota.nextcloud_client import ocs_get
        result = ocs_get(empty_config, "/cloud/users/alice")
        assert result is None

    @patch("istota.nextcloud_client.httpx.get")
    def test_custom_timeout(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_get

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ocs": {"data": {}}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        ocs_get(nc_config, "/test", timeout=30.0)
        assert mock_get.call_args.kwargs["timeout"] == 30.0


# --- ocs_post tests ---


class TestOcsPost:
    @patch("istota.nextcloud_client.httpx.post")
    def test_success(self, mock_post, nc_config):
        from istota.nextcloud_client import ocs_post

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ocs": {"data": {"id": 42}}}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = ocs_post(nc_config, "/apps/files_sharing/api/v1/shares", data={"path": "/test"})
        assert result == {"id": 42}

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["data"] == {"path": "/test"}
        assert call_kwargs.kwargs["auth"] == ("istota", "secret")

    @patch("istota.nextcloud_client.httpx.post")
    def test_error_returns_none(self, mock_post, nc_config):
        from istota.nextcloud_client import ocs_post

        mock_post.side_effect = Exception("500 Internal Server Error")
        result = ocs_post(nc_config, "/test", data={})
        assert result is None

    def test_not_configured_returns_none(self, empty_config):
        from istota.nextcloud_client import ocs_post
        result = ocs_post(empty_config, "/test", data={})
        assert result is None


# --- ocs_delete tests ---


class TestOcsDelete:
    @patch("istota.nextcloud_client.httpx.delete")
    def test_success(self, mock_delete, nc_config):
        from istota.nextcloud_client import ocs_delete

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_delete.return_value = mock_resp

        result = ocs_delete(nc_config, "/apps/files_sharing/api/v1/shares/42")
        assert result is True

        mock_delete.assert_called_once()
        call_args = mock_delete.call_args
        assert "/shares/42" in call_args[0][0]
        assert call_args.kwargs["auth"] == ("istota", "secret")

    @patch("istota.nextcloud_client.httpx.delete")
    def test_error_returns_false(self, mock_delete, nc_config):
        from istota.nextcloud_client import ocs_delete

        mock_delete.side_effect = Exception("404 Not Found")
        result = ocs_delete(nc_config, "/apps/files_sharing/api/v1/shares/999")
        assert result is False

    def test_not_configured_returns_false(self, empty_config):
        from istota.nextcloud_client import ocs_delete
        result = ocs_delete(empty_config, "/test")
        assert result is False

    @patch("istota.nextcloud_client.httpx.delete")
    def test_custom_timeout(self, mock_delete, nc_config):
        from istota.nextcloud_client import ocs_delete

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_delete.return_value = mock_resp

        ocs_delete(nc_config, "/test", timeout=30.0)
        assert mock_delete.call_args.kwargs["timeout"] == 30.0


# --- webdav_get_owner tests ---


class TestWebdavGetOwner:
    @patch("istota.nextcloud_client.httpx.request")
    def test_parses_owner_from_xml(self, mock_request, nc_config):
        from istota.nextcloud_client import webdav_get_owner

        mock_resp = MagicMock()
        mock_resp.text = PROPFIND_XML_ALICE
        mock_resp.raise_for_status = MagicMock()
        mock_request.return_value = mock_resp

        owner = webdav_get_owner(nc_config, "shared_doc.txt")
        assert owner == "alice"

        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert call_args[0][0] == "PROPFIND"
        assert "shared_doc.txt" in call_args[0][1]
        assert "istota" in call_args[0][1]

    @patch("istota.nextcloud_client.httpx.request")
    def test_no_owner_in_xml(self, mock_request, nc_config):
        from istota.nextcloud_client import webdav_get_owner

        mock_resp = MagicMock()
        mock_resp.text = PROPFIND_XML_NO_OWNER
        mock_resp.raise_for_status = MagicMock()
        mock_request.return_value = mock_resp

        assert webdav_get_owner(nc_config, "file.txt") is None

    @patch("istota.nextcloud_client.httpx.request")
    def test_error_returns_none(self, mock_request, nc_config):
        from istota.nextcloud_client import webdav_get_owner

        mock_request.side_effect = Exception("Connection refused")
        assert webdav_get_owner(nc_config, "file.txt") is None

    def test_not_configured_returns_none(self, empty_config):
        from istota.nextcloud_client import webdav_get_owner
        assert webdav_get_owner(empty_config, "file.txt") is None


# --- ocs_list_shares tests ---


class TestOcsListShares:
    @patch("istota.nextcloud_client.ocs_get")
    def test_list_all_shares(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_list_shares

        mock_get.return_value = [{"id": 1}, {"id": 2}]
        result = ocs_list_shares(nc_config)
        assert result == [{"id": 1}, {"id": 2}]
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["params"] == {}

    @patch("istota.nextcloud_client.ocs_get")
    def test_list_shares_with_path(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_list_shares

        mock_get.return_value = [{"id": 1}]
        result = ocs_list_shares(nc_config, path="/Documents")
        assert result == [{"id": 1}]
        assert mock_get.call_args.kwargs["params"]["path"] == "/Documents"

    @patch("istota.nextcloud_client.ocs_get")
    def test_list_shares_with_reshares(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_list_shares

        mock_get.return_value = []
        ocs_list_shares(nc_config, reshares=True)
        assert mock_get.call_args.kwargs["params"]["reshares"] == "true"

    @patch("istota.nextcloud_client.ocs_get")
    def test_error_returns_none(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_list_shares

        mock_get.return_value = None
        assert ocs_list_shares(nc_config) is None


# --- ocs_create_share tests ---


class TestOcsCreateShare:
    @patch("istota.nextcloud_client.ocs_post")
    def test_create_user_share(self, mock_post, nc_config):
        from istota.nextcloud_client import ocs_create_share

        mock_post.return_value = {"id": 42, "share_type": 0}
        result = ocs_create_share(nc_config, "/test", share_type=0, share_with="bob", permissions=31)
        assert result == {"id": 42, "share_type": 0}

        call_data = mock_post.call_args.kwargs["data"]
        assert call_data["path"] == "/test"
        assert call_data["shareType"] == 0
        assert call_data["shareWith"] == "bob"
        assert call_data["permissions"] == 31

    @patch("istota.nextcloud_client.ocs_post")
    def test_create_link_share(self, mock_post, nc_config):
        from istota.nextcloud_client import ocs_create_share

        mock_post.return_value = {"id": 99, "url": "https://nc.example.com/s/abc"}
        result = ocs_create_share(
            nc_config, "/test", share_type=3, permissions=1,
            password="secret", expire_date="2026-12-31", label="my link",
        )
        assert result["url"] == "https://nc.example.com/s/abc"

        call_data = mock_post.call_args.kwargs["data"]
        assert call_data["shareType"] == 3
        assert call_data["password"] == "secret"
        assert call_data["expireDate"] == "2026-12-31"
        assert call_data["label"] == "my link"
        assert "shareWith" not in call_data

    @patch("istota.nextcloud_client.ocs_post")
    def test_minimal_params(self, mock_post, nc_config):
        from istota.nextcloud_client import ocs_create_share

        mock_post.return_value = {"id": 1}
        ocs_create_share(nc_config, "/test", share_type=3)

        call_data = mock_post.call_args.kwargs["data"]
        assert call_data == {"path": "/test", "shareType": 3}

    @patch("istota.nextcloud_client.ocs_post")
    def test_error_returns_none(self, mock_post, nc_config):
        from istota.nextcloud_client import ocs_create_share

        mock_post.return_value = None
        assert ocs_create_share(nc_config, "/test", share_type=0) is None


# --- ocs_delete_share tests ---


class TestOcsDeleteShare:
    @patch("istota.nextcloud_client.ocs_delete")
    def test_success(self, mock_delete, nc_config):
        from istota.nextcloud_client import ocs_delete_share

        mock_delete.return_value = True
        assert ocs_delete_share(nc_config, 42) is True
        mock_delete.assert_called_once()
        assert "/shares/42" in mock_delete.call_args[0][1]

    @patch("istota.nextcloud_client.ocs_delete")
    def test_failure(self, mock_delete, nc_config):
        from istota.nextcloud_client import ocs_delete_share

        mock_delete.return_value = False
        assert ocs_delete_share(nc_config, 999) is False


# --- ocs_search_sharees tests ---


class TestOcsSearchSharees:
    @patch("istota.nextcloud_client.ocs_get")
    def test_search(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_search_sharees

        mock_get.return_value = {
            "exact": {"users": [{"label": "Bob", "value": {"shareWith": "bob"}}]},
            "users": [],
        }
        result = ocs_search_sharees(nc_config, "bob")
        assert result["exact"]["users"][0]["label"] == "Bob"

        call_params = mock_get.call_args.kwargs["params"]
        assert call_params["search"] == "bob"
        assert call_params["itemType"] == "file"

    @patch("istota.nextcloud_client.ocs_get")
    def test_custom_item_type(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_search_sharees

        mock_get.return_value = {}
        ocs_search_sharees(nc_config, "alice", item_type="folder")
        assert mock_get.call_args.kwargs["params"]["itemType"] == "folder"

    @patch("istota.nextcloud_client.ocs_get")
    def test_error_returns_none(self, mock_get, nc_config):
        from istota.nextcloud_client import ocs_search_sharees

        mock_get.return_value = None
        assert ocs_search_sharees(nc_config, "nobody") is None


# --- ocs_create_public_link tests ---


class TestOcsCreatePublicLink:
    @patch("istota.nextcloud_client.ocs_create_share")
    def test_creates_link_share(self, mock_create, nc_config):
        from istota.nextcloud_client import ocs_create_public_link

        mock_create.return_value = {"id": 50, "url": "https://nc.example.com/s/xyz"}
        result = ocs_create_public_link(nc_config, "/Documents/report.pdf")
        assert result["url"] == "https://nc.example.com/s/xyz"

        mock_create.assert_called_once_with(
            nc_config,
            path="/Documents/report.pdf",
            share_type=3,
            permissions=1,
            password=None,
            expire_date=None,
            label=None,
            timeout=10.0,
        )

    @patch("istota.nextcloud_client.ocs_create_share")
    def test_with_options(self, mock_create, nc_config):
        from istota.nextcloud_client import ocs_create_public_link

        mock_create.return_value = {"id": 51, "url": "https://nc.example.com/s/abc"}
        ocs_create_public_link(
            nc_config, "/test", permissions=3, password="pw",
            expire_date="2026-06-01", label="test link",
        )
        mock_create.assert_called_once_with(
            nc_config,
            path="/test",
            share_type=3,
            permissions=3,
            password="pw",
            expire_date="2026-06-01",
            label="test link",
            timeout=10.0,
        )

    @patch("istota.nextcloud_client.ocs_create_share")
    def test_error_returns_none(self, mock_create, nc_config):
        from istota.nextcloud_client import ocs_create_public_link

        mock_create.return_value = None
        assert ocs_create_public_link(nc_config, "/test") is None


# --- ocs_share_folder tests ---


class TestOcsShareFolder:
    @patch("istota.nextcloud_client.ocs_create_share")
    @patch("istota.nextcloud_client.ocs_list_shares")
    def test_creates_new_share(self, mock_list, mock_create, nc_config):
        from istota.nextcloud_client import ocs_share_folder

        mock_list.return_value = []
        mock_create.return_value = {"id": 42}

        result = ocs_share_folder(nc_config, "/Users/alice/notes", "alice")
        assert result is True
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["share_with"] == "alice"
        assert mock_create.call_args.kwargs["permissions"] == 31

    @patch("istota.nextcloud_client.ocs_create_share")
    @patch("istota.nextcloud_client.ocs_list_shares")
    def test_already_shared(self, mock_list, mock_create, nc_config):
        from istota.nextcloud_client import ocs_share_folder

        mock_list.return_value = [
            {"share_with": "alice", "share_type": 0, "id": 42},
        ]

        result = ocs_share_folder(nc_config, "/Users/alice/notes", "alice")
        assert result is True
        mock_create.assert_not_called()

    @patch("istota.nextcloud_client.ocs_create_share")
    @patch("istota.nextcloud_client.ocs_list_shares")
    def test_different_user_share(self, mock_list, mock_create, nc_config):
        from istota.nextcloud_client import ocs_share_folder

        mock_list.return_value = [
            {"share_with": "bob", "share_type": 0, "id": 10},
        ]
        mock_create.return_value = {"id": 43}

        result = ocs_share_folder(nc_config, "/Users/alice/notes", "alice")
        assert result is True
        mock_create.assert_called_once()

    @patch("istota.nextcloud_client.ocs_create_share")
    @patch("istota.nextcloud_client.ocs_list_shares")
    def test_create_failure_returns_false(self, mock_list, mock_create, nc_config):
        from istota.nextcloud_client import ocs_share_folder

        mock_list.return_value = []
        mock_create.return_value = None

        result = ocs_share_folder(nc_config, "/Users/alice/notes", "alice")
        assert result is False

    @patch("istota.nextcloud_client.ocs_create_share")
    @patch("istota.nextcloud_client.ocs_list_shares")
    def test_list_failure_still_tries_create(self, mock_list, mock_create, nc_config):
        from istota.nextcloud_client import ocs_share_folder

        mock_list.return_value = None
        mock_create.return_value = {"id": 44}

        result = ocs_share_folder(nc_config, "/Users/alice/notes", "alice")
        assert result is True
        mock_create.assert_called_once()

    def test_not_configured_returns_false(self, empty_config):
        from istota.nextcloud_client import ocs_share_folder
        result = ocs_share_folder(empty_config, "/test", "alice")
        assert result is False
