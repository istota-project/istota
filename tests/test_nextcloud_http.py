"""Tests for the istota.nextcloud client foundation (Stage 1).

Covers the structured error model (``OcsError`` + the OCS status-code table),
the raising ``ocs_*`` variants, the generic ``dav_request`` primitive, and the
per-user path-scoping resolver.
"""

from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config, NextcloudConfig


@pytest.fixture
def nc_config():
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="istota",
            app_password="secret",
        )
    )


@pytest.fixture
def empty_config():
    return Config(nextcloud=NextcloudConfig(url="", username="", app_password=""))


def _ocs_response(data, status_code=200, http_status=200, message="OK"):
    resp = MagicMock()
    resp.status_code = http_status
    resp.json.return_value = {
        "ocs": {"meta": {"statuscode": status_code, "message": message}, "data": data}
    }
    resp.text = ""
    return resp


# --- OCS status-code table ---


class TestOcsStatusMessages:
    @pytest.mark.parametrize(
        "code,fragment",
        [
            (400, "bad request"),
            (403, "forbidden"),
            (404, "not found"),
            (996, "server error"),
            (997, "admin"),
            (998, "not found"),
            (999, "not enabled"),
        ],
    )
    def test_each_code_maps_to_human_text(self, code, fragment):
        from istota.nextcloud._http import describe_ocs_status

        assert fragment in describe_ocs_status(code).lower()

    def test_success_codes(self):
        from istota.nextcloud._http import is_ocs_success

        assert is_ocs_success(100) is True
        assert is_ocs_success(200) is True
        assert is_ocs_success(403) is False

    def test_997_names_admin_rights_as_likely_cause(self):
        from istota.nextcloud._http import describe_ocs_status

        text = describe_ocs_status(997).lower()
        assert "admin" in text
        assert "app password" in text

    def test_unknown_code_still_described(self):
        from istota.nextcloud._http import describe_ocs_status

        assert "555" in describe_ocs_status(555)


# --- OcsError ---


class TestOcsError:
    def test_fields_populated_from_error_body(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_get

        resp = _ocs_response(None, status_code=403, message="Wrong path, file/folder doesn't exist")
        with patch("istota.nextcloud._http.httpx.get", return_value=resp):
            with pytest.raises(OcsError) as exc:
                ocs_get(nc_config, "/apps/files_sharing/api/v1/shares")

        err = exc.value
        assert err.ocs_status == 403
        assert err.http_status == 200
        assert err.endpoint == "/apps/files_sharing/api/v1/shares"
        assert "Wrong path" in err.message

    def test_str_is_the_message(self):
        from istota.nextcloud._http import OcsError

        err = OcsError("boom", 500, None, "/cloud/user")
        assert str(err) == "boom"

    def test_to_envelope(self):
        from istota.nextcloud._http import OcsError

        env = OcsError("nope", 403, 997, "/cloud/users/alice").to_envelope()
        assert env == {
            "status": "error",
            "error": "nope",
            "http_status": 403,
            "ocs_status": 997,
            "endpoint": "/cloud/users/alice",
        }

    def test_falls_back_to_table_when_server_sends_no_message(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_get

        resp = _ocs_response(None, status_code=997, message="")
        with patch("istota.nextcloud._http.httpx.get", return_value=resp):
            with pytest.raises(OcsError) as exc:
                ocs_get(nc_config, "/cloud/users/alice")

        assert "admin" in exc.value.message.lower()


# --- raising ocs_* variants ---


class TestRateLimitMessage:
    """A 429 arrives as a non-OCS body, so it lands in the fallback branch.

    Bare "HTTP 429 from Nextcloud" is unactionable — the caller can't tell
    whether to retry now or in ten minutes. Hit live by creating public links
    faster than the server's 20-per-10-minutes cap.
    """

    def _rate_limited(self, retry_after=None):
        resp = MagicMock()
        resp.status_code = 429
        resp.json.side_effect = ValueError("not json")
        resp.text = "<html>429</html>"
        resp.headers = {"Retry-After": retry_after} if retry_after else {}
        return resp

    def test_message_explains_the_cap(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_post

        with patch("istota.nextcloud._http.httpx.post", return_value=self._rate_limited()):
            with pytest.raises(OcsError) as e:
                ocs_post(nc_config, "/shares", data={"path": "/a"})
        assert e.value.http_status == 429
        assert "Rate limited" in e.value.message
        assert "20 per 10 minutes" in e.value.message

    def test_retry_after_is_surfaced_when_the_server_sends_it(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_post

        with patch("istota.nextcloud._http.httpx.post", return_value=self._rate_limited("120")):
            with pytest.raises(OcsError) as e:
                ocs_post(nc_config, "/shares", data={"path": "/a"})
        assert "Retry after 120 seconds" in e.value.message

    def test_a_junk_retry_after_falls_back_to_generic_advice(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_post

        with patch("istota.nextcloud._http.httpx.post", return_value=self._rate_limited("soon")):
            with pytest.raises(OcsError) as e:
                ocs_post(nc_config, "/shares", data={"path": "/a"})
        assert "Wait a few minutes" in e.value.message

    def test_other_statuses_keep_the_body_detail(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_post

        resp = self._rate_limited()
        resp.status_code = 500
        resp.text = "upstream exploded"
        with patch("istota.nextcloud._http.httpx.post", return_value=resp):
            with pytest.raises(OcsError) as e:
                ocs_post(nc_config, "/shares", data={"path": "/a"})
        assert "upstream exploded" in e.value.message


class TestOcsRequest:
    def test_get_returns_data(self, nc_config):
        from istota.nextcloud._http import ocs_get

        resp = _ocs_response({"id": "istota"})
        with patch("istota.nextcloud._http.httpx.get", return_value=resp) as m:
            assert ocs_get(nc_config, "/cloud/user") == {"id": "istota"}

        assert m.call_args[0][0] == "https://cloud.example.com/ocs/v2.php/cloud/user"
        assert m.call_args.kwargs["auth"] == ("istota", "secret")
        assert m.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"

    def test_post_sends_form_data(self, nc_config):
        from istota.nextcloud._http import ocs_post

        resp = _ocs_response({"id": 42})
        with patch("istota.nextcloud._http.httpx.post", return_value=resp) as m:
            assert ocs_post(nc_config, "/shares", data={"path": "/a"}) == {"id": 42}

        assert m.call_args.kwargs["data"] == {"path": "/a"}

    def test_put_uses_httpx_put(self, nc_config):
        from istota.nextcloud._http import ocs_put

        resp = _ocs_response({"ok": True})
        with patch("istota.nextcloud._http.httpx.put", return_value=resp) as m:
            ocs_put(nc_config, "/shares/1", data={"permissions": 1})

        assert m.call_args.kwargs["data"] == {"permissions": 1}

    def test_delete_returns_data(self, nc_config):
        from istota.nextcloud._http import ocs_delete

        resp = _ocs_response([])
        with patch("istota.nextcloud._http.httpx.delete", return_value=resp):
            assert ocs_delete(nc_config, "/shares/1") == []

    def test_not_configured_raises(self, empty_config):
        from istota.nextcloud._http import OcsError, ocs_get

        with pytest.raises(OcsError) as exc:
            ocs_get(empty_config, "/cloud/user")
        assert "not configured" in exc.value.message.lower()

    def test_http_error_status_carried(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_get

        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        resp.json.side_effect = ValueError("not json")
        with patch("istota.nextcloud._http.httpx.get", return_value=resp):
            with pytest.raises(OcsError) as exc:
                ocs_get(nc_config, "/cloud/user")

        assert exc.value.http_status == 401

    def test_transport_error_becomes_ocs_error(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_get

        with patch("istota.nextcloud._http.httpx.get", side_effect=OSError("refused")):
            with pytest.raises(OcsError) as exc:
                ocs_get(nc_config, "/cloud/user")

        assert exc.value.http_status is None
        assert "refused" in exc.value.message

    def test_non_json_success_body_raises(self, nc_config):
        from istota.nextcloud._http import OcsError, ocs_get

        resp = MagicMock()
        resp.status_code = 200
        resp.text = "<html>login</html>"
        resp.json.side_effect = ValueError("not json")
        with patch("istota.nextcloud._http.httpx.get", return_value=resp):
            with pytest.raises(OcsError):
                ocs_get(nc_config, "/cloud/user")


# --- dav_request ---


class TestDavRequest:
    def test_builds_files_url(self, nc_config):
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(nc_config, "/Users/alice/a.txt") == (
            "https://cloud.example.com/remote.php/dav/files/istota/Users/alice/a.txt"
        )

    def test_files_url_quotes_special_characters(self, nc_config):
        from istota.nextcloud._http import dav_files_url

        url = dav_files_url(nc_config, "/Users/alice/my report #1.pdf")
        assert "my%20report%20%231.pdf" in url

    def test_issues_method_positionally(self, nc_config):
        from istota.nextcloud._http import dav_request

        resp = MagicMock(status_code=207, text="<d:multistatus/>")
        with patch("istota.nextcloud._http.httpx.request", return_value=resp) as m:
            dav_request(nc_config, "PROPFIND", "https://cloud.example.com/remote.php/dav/x")

        assert m.call_args[0][0] == "PROPFIND"
        assert m.call_args[0][1].endswith("/remote.php/dav/x")
        assert m.call_args.kwargs["auth"] == ("istota", "secret")

    def test_raises_ocs_error_on_failure_status(self, nc_config):
        from istota.nextcloud._http import OcsError, dav_request

        resp = MagicMock(status_code=404, text="Not Found")
        with patch("istota.nextcloud._http.httpx.request", return_value=resp):
            with pytest.raises(OcsError) as exc:
                dav_request(nc_config, "PROPFIND", "https://cloud.example.com/remote.php/dav/x")

        assert exc.value.http_status == 404

    def test_mkcol_405_is_tolerated_when_asked(self, nc_config):
        from istota.nextcloud._http import dav_request

        resp = MagicMock(status_code=405, text="")
        with patch("istota.nextcloud._http.httpx.request", return_value=resp):
            out = dav_request(
                nc_config,
                "MKCOL",
                "https://cloud.example.com/remote.php/dav/x",
                ok_statuses=(201, 405),
            )
        assert out.status_code == 405

    def test_not_configured_raises(self, empty_config):
        from istota.nextcloud._http import OcsError, dav_request

        with pytest.raises(OcsError):
            dav_request(empty_config, "PROPFIND", "https://x/remote.php/dav/y")


# --- path scoping ---


class TestPathScoping:
    def test_absolute_in_workspace_allowed(self):
        from istota.nextcloud._http import resolve_scoped_path

        assert resolve_scoped_path("/Users/alice/notes.md", "alice") == "/Users/alice/notes.md"

    def test_relative_path_resolves_under_workspace(self):
        from istota.nextcloud._http import resolve_scoped_path

        assert resolve_scoped_path("notes.md", "alice") == "/Users/alice/notes.md"

    def test_workspace_root_itself_allowed(self):
        from istota.nextcloud._http import resolve_scoped_path

        assert resolve_scoped_path("/Users/alice", "alice") == "/Users/alice"
        assert resolve_scoped_path("", "alice") == "/Users/alice"

    def test_dotdot_escape_rejected(self):
        from istota.nextcloud._http import PathScopeError, resolve_scoped_path

        with pytest.raises(PathScopeError):
            resolve_scoped_path("/Users/alice/../bob/secret.txt", "alice")

    def test_other_user_workspace_rejected(self):
        from istota.nextcloud._http import PathScopeError, resolve_scoped_path

        with pytest.raises(PathScopeError):
            resolve_scoped_path("/Users/bob/secret.txt", "alice")

    def test_prefix_lookalike_rejected(self):
        from istota.nextcloud._http import PathScopeError, resolve_scoped_path

        with pytest.raises(PathScopeError):
            resolve_scoped_path("/Users/alice2/secret.txt", "alice")

    def test_admin_may_escape(self):
        from istota.nextcloud._http import resolve_scoped_path

        assert resolve_scoped_path("/Channels/abc", "alice", is_admin=True) == "/Channels/abc"

    def test_admin_relative_path_still_anchored_to_workspace(self):
        from istota.nextcloud._http import resolve_scoped_path

        assert resolve_scoped_path("notes.md", "alice", is_admin=True) == "/Users/alice/notes.md"

    def test_no_user_id_non_admin_rejected(self):
        from istota.nextcloud._http import PathScopeError, resolve_scoped_path

        with pytest.raises(PathScopeError):
            resolve_scoped_path("/anything", "")

    def test_no_user_id_admin_allowed(self):
        from istota.nextcloud._http import resolve_scoped_path

        assert resolve_scoped_path("/anything", "", is_admin=True) == "/anything"

    def test_error_message_names_the_workspace(self):
        from istota.nextcloud._http import PathScopeError, resolve_scoped_path

        with pytest.raises(PathScopeError) as exc:
            resolve_scoped_path("/Users/bob/x", "alice")
        assert "/Users/alice" in str(exc.value)


# --- legacy shim compatibility ---


class TestLegacyShim:
    def test_ocs_get_returns_none_instead_of_raising(self, nc_config):
        from istota.nextcloud_client import ocs_get as legacy_get

        resp = _ocs_response(None, status_code=997, message="")
        with patch("istota.nextcloud._http.httpx.get", return_value=resp):
            assert legacy_get(nc_config, "/cloud/users/alice") is None

    def test_ocs_post_returns_none_instead_of_raising(self, nc_config):
        from istota.nextcloud_client import ocs_post as legacy_post

        with patch("istota.nextcloud._http.httpx.post", side_effect=OSError("down")):
            assert legacy_post(nc_config, "/shares", data={}) is None

    def test_ocs_delete_returns_false_instead_of_raising(self, nc_config):
        from istota.nextcloud_client import ocs_delete as legacy_delete

        with patch("istota.nextcloud._http.httpx.delete", side_effect=OSError("down")):
            assert legacy_delete(nc_config, "/shares/1") is False

    def test_package_exports_public_names(self):
        import istota.nextcloud as pkg

        for name in (
            "OcsError",
            "ocs_get",
            "ocs_post",
            "ocs_put",
            "ocs_delete",
            "dav_request",
            "resolve_scoped_path",
        ):
            assert hasattr(pkg, name), name

    def test_shim_exports_every_legacy_name(self):
        import istota.nextcloud_client as shim

        for name in (
            "ocs_get",
            "ocs_post",
            "ocs_delete",
            "webdav_get_owner",
            "ocs_list_shares",
            "ocs_create_share",
            "ocs_delete_share",
            "ocs_search_sharees",
            "ocs_create_public_link",
            "ocs_share_folder",
            "_nc_auth",
            "_nc_base_url",
            "_nc_configured",
            "_ocs_headers",
        ):
            assert hasattr(shim, name), name
