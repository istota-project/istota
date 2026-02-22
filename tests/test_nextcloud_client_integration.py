"""Integration tests for nextcloud_client against a live Nextcloud instance.

Run with: pytest -m integration tests/test_nextcloud_client_integration.py -v

Requires env vars: NC_URL, NC_USER, NC_PASS
Optional: NC_OTHER_USER (a real Nextcloud username for share tests)
"""

import os
import uuid

import pytest

from istota.config import Config, NextcloudConfig
from istota.nextcloud_client import (
    ocs_create_public_link,
    ocs_create_share,
    ocs_delete_share,
    ocs_get,
    ocs_list_shares,
    ocs_post,
    ocs_search_sharees,
    ocs_share_folder,
    webdav_get_owner,
)

_url = os.environ.get("NC_URL", "")
_user = os.environ.get("NC_USER", "")
_pass = os.environ.get("NC_PASS", "")
_other_user = os.environ.get("NC_OTHER_USER", "")

_skip_reason = None
if not _url:
    _skip_reason = "NC_URL not set"
elif not _user:
    _skip_reason = "NC_USER not set"
elif not _pass:
    _skip_reason = "NC_PASS not set"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or ""),
]


@pytest.fixture
def config():
    return Config(
        nextcloud=NextcloudConfig(
            url=_url,
            username=_user,
            app_password=_pass,
        ),
    )


@pytest.fixture
def temp_folder(config):
    """Create a temporary folder via WebDAV, yield its path, then delete."""
    import httpx

    folder_name = f"__test_{uuid.uuid4().hex[:8]}"
    base_url = config.nextcloud.url.rstrip("/")
    dav_url = f"{base_url}/remote.php/dav/files/{_user}/{folder_name}"
    auth = (_user, _pass)

    resp = httpx.request("MKCOL", dav_url, auth=auth, timeout=10.0)
    assert resp.status_code in (201, 405), f"MKCOL failed: {resp.status_code}"

    yield f"/{folder_name}"

    httpx.request("DELETE", dav_url, auth=auth, timeout=10.0)


@pytest.fixture
def other_user():
    if not _other_user:
        pytest.skip("NC_OTHER_USER not set")
    return _other_user


class TestOcsGetLive:
    def test_fetch_own_user_info(self, config):
        data = ocs_get(config, f"/cloud/users/{_user}")
        assert data is not None
        assert "displayname" in data or "id" in data

    def test_fetch_capabilities(self, config):
        data = ocs_get(config, "/cloud/capabilities")
        assert data is not None
        assert "capabilities" in data

    def test_nonexistent_user_returns_data_or_none(self, config):
        fake_user = f"nonexistent_{uuid.uuid4().hex[:8]}"
        result = ocs_get(config, f"/cloud/users/{fake_user}")
        assert result is None or isinstance(result, (dict, list))


class TestWebdavGetOwnerLive:
    def test_bot_user_root_has_owner(self, config):
        owner = webdav_get_owner(config, "")
        assert owner == _user

    def test_nonexistent_path_returns_none(self, config):
        fake = f"__nonexistent_{uuid.uuid4().hex[:8]}__"
        assert webdav_get_owner(config, fake) is None


class TestOcsListSharesLive:
    def test_list_all_shares(self, config):
        shares = ocs_list_shares(config)
        assert shares is not None
        assert isinstance(shares, list)

    def test_list_shares_for_nonexistent_path(self, config):
        fake = f"/__nonexistent_{uuid.uuid4().hex[:8]}"
        result = ocs_list_shares(config, path=fake)
        # May return None (404) or empty list
        assert result is None or result == []


class TestOcsCreateAndDeleteShareLive:
    def test_create_public_link_and_delete(self, config, temp_folder):
        """Create a public link for a temp folder, verify it, then delete it."""
        share = ocs_create_public_link(config, temp_folder)
        assert share is not None
        assert "id" in share
        assert "url" in share

        # Verify it appears in the list
        shares = ocs_list_shares(config, path=temp_folder)
        assert shares is not None
        share_ids = [s["id"] for s in shares]
        assert share["id"] in share_ids

        # Delete it
        assert ocs_delete_share(config, share["id"]) is True

        # Verify it's gone
        shares_after = ocs_list_shares(config, path=temp_folder)
        if shares_after:
            remaining_ids = [s["id"] for s in shares_after]
            assert share["id"] not in remaining_ids

    def test_create_user_share(self, config, temp_folder, other_user):
        """Create a user share and clean up."""
        share = ocs_create_share(
            config, temp_folder, share_type=0, share_with=other_user, permissions=31,
        )
        assert share is not None
        assert share.get("share_type") == 0

        # Clean up
        ocs_delete_share(config, share["id"])

    def test_create_share_nonexistent_path(self, config):
        fake = f"/__nonexistent_{uuid.uuid4().hex[:8]}"
        result = ocs_create_share(config, fake, share_type=3)
        assert result is None

    def test_delete_nonexistent_share(self, config):
        assert ocs_delete_share(config, 999999999) is False


class TestOcsSearchShareesLive:
    def test_search_for_bot_user(self, config):
        result = ocs_search_sharees(config, _user)
        assert result is not None
        # Should have exact and/or partial matches structure
        assert "exact" in result or "users" in result

    def test_search_for_nonexistent_user(self, config):
        fake = f"nonexistent_{uuid.uuid4().hex[:8]}"
        result = ocs_search_sharees(config, fake)
        assert result is not None


class TestOcsShareFolderLive:
    def test_share_creates_and_is_idempotent(self, config, temp_folder, other_user):
        assert ocs_share_folder(config, temp_folder, other_user) is True
        assert ocs_share_folder(config, temp_folder, other_user) is True

    def test_share_nonexistent_folder_fails(self, config):
        fake = f"/__nonexistent_{uuid.uuid4().hex[:8]}"
        result = ocs_share_folder(config, fake, _user)
        assert result is False

    def test_share_with_nonexistent_user_fails(self, config, temp_folder):
        fake_user = f"nonexistent_{uuid.uuid4().hex[:8]}"
        result = ocs_share_folder(config, temp_folder, fake_user)
        assert result is False


class TestOcsPostLive:
    def test_post_returns_data_on_valid_endpoint(self, config):
        result = ocs_post(
            config,
            "/apps/files_sharing/api/v1/shares",
            data={"path": "/__nonexistent_path__", "shareType": 0, "shareWith": _user},
        )
        assert result is None or isinstance(result, (dict, list))
