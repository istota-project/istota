"""The shared Nextcloud sign-in classifier and ``lib/unreachable.is_read_timeout``.

``sign_in`` is what the setup check's step 2 and the doctor check both read, so
each row of its classification table has a case here, driven through the real
``ocs_get`` with ``httpx.get`` replaced at the ``_http`` seam.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from istota.config import Config, NextcloudConfig
from istota.lib.unreachable import is_read_timeout
from istota.nextcloud._http import OcsError
from istota.nextcloud.capabilities import SignIn, sign_in

GET = "istota.nextcloud._http.httpx.get"


@pytest.fixture
def nc_config():
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="istota",
            app_password="secret",
        )
    )


def _ocs_response(data, status_code=100, http_status=200, message="OK"):
    resp = MagicMock()
    resp.status_code = http_status
    resp.json.return_value = {
        "ocs": {"meta": {"statuscode": status_code, "message": message}, "data": data}
    }
    resp.text = ""
    return resp


def _plain_response(http_status, text="<html>nope</html>"):
    resp = MagicMock()
    resp.status_code = http_status
    resp.json.side_effect = ValueError("not json")
    resp.text = text
    resp.headers = {}
    return resp


def _raising(exc):
    def get(*args, **kwargs):
        raise exc
    return get


class TestIsReadTimeout:
    def _chained(self, inner):
        try:
            try:
                raise inner
            except Exception as exc:
                raise OcsError("Could not reach Nextcloud", None, None, "/cloud/user") from exc
        except OcsError as outer:
            return outer

    def test_a_chained_read_timeout_is_true(self):
        assert is_read_timeout(self._chained(httpx.ReadTimeout("slow")))

    def test_a_connect_timeout_is_false(self):
        assert not is_read_timeout(self._chained(httpx.ConnectTimeout("no route")))

    def test_a_connect_error_is_false(self):
        assert not is_read_timeout(self._chained(httpx.ConnectError("refused")))

    def test_none_is_false(self):
        assert not is_read_timeout(None)


class TestSignIn:
    def test_ok(self, nc_config):
        account = {"id": "istota", "display-name": "Istota", "groups": []}
        with patch(GET, return_value=_ocs_response(account)):
            result = sign_in(nc_config)
        assert result == SignIn("ok", account, "istota", None, None, None)

    def test_http_401_is_auth_failed(self, nc_config):
        with patch(GET, return_value=_plain_response(401)):
            result = sign_in(nc_config)
        assert result.outcome == "auth_failed"
        assert result.http_status == 401
        assert result.account == {}
        assert result.account_id == ""
        assert isinstance(result.exc, OcsError)

    def test_ocs_997_is_auth_failed(self, nc_config):
        with patch(GET, return_value=_ocs_response(None, status_code=997, http_status=200)):
            result = sign_in(nc_config)
        assert result.outcome == "auth_failed"
        assert result.ocs_status == 997

    def test_http_403_is_forbidden(self, nc_config):
        with patch(GET, return_value=_plain_response(403)):
            result = sign_in(nc_config)
        assert result.outcome == "forbidden"
        assert result.http_status == 403

    def test_a_read_timeout_is_slow(self, nc_config):
        with patch(GET, side_effect=_raising(httpx.ReadTimeout("timed out"))):
            result = sign_in(nc_config)
        assert result.outcome == "slow"
        assert result.http_status is None
        assert isinstance(result.exc.__cause__, httpx.ReadTimeout)

    def test_a_connect_error_is_unreachable(self, nc_config):
        with patch(GET, side_effect=_raising(httpx.ConnectError("Connection refused"))):
            result = sign_in(nc_config)
        assert result.outcome == "unreachable"
        assert result.http_status is None

    def test_a_connect_timeout_is_unreachable_not_slow(self, nc_config):
        with patch(GET, side_effect=_raising(httpx.ConnectTimeout("timed out"))):
            result = sign_in(nc_config)
        assert result.outcome == "unreachable"

    def test_http_429_is_rate_limited(self, nc_config):
        with patch(GET, return_value=_plain_response(429)):
            result = sign_in(nc_config)
        assert result.outcome == "rate_limited"
        assert result.http_status == 429

    def test_http_404_is_not_found(self, nc_config):
        with patch(GET, return_value=_plain_response(404)):
            result = sign_in(nc_config)
        assert result.outcome == "not_found"

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_http_5xx_is_server_error(self, nc_config, status):
        with patch(GET, return_value=_plain_response(status)):
            result = sign_in(nc_config)
        assert result.outcome == "server_error"
        assert result.http_status == status

    def test_any_other_ocs_failure_is_error(self, nc_config):
        with patch(GET, return_value=_ocs_response(None, status_code=999, http_status=200)):
            result = sign_in(nc_config)
        assert result.outcome == "error"
        assert result.ocs_status == 999
        assert result.http_status == 200

    def test_an_http_400_is_error(self, nc_config):
        with patch(GET, return_value=_plain_response(400)):
            result = sign_in(nc_config)
        assert result.outcome == "error"

    def test_a_success_with_no_account_id_is_error(self, nc_config):
        with patch(GET, return_value=_ocs_response({"display-name": "Istota"})):
            result = sign_in(nc_config)
        assert result.outcome == "error"
        assert result.account_id == ""
        assert result.exc is None

    def test_a_success_as_another_account_is_wrong_account(self, nc_config):
        account = {"id": "bot@example.com"}
        with patch(GET, return_value=_ocs_response(account)):
            result = sign_in(nc_config)
        assert result.outcome == "wrong_account"
        assert result.account_id == "bot@example.com"
        assert result.account == account

    def test_the_comparison_is_exact(self, nc_config):
        with patch(GET, return_value=_ocs_response({"id": "Istota"})):
            result = sign_in(nc_config)
        assert result.outcome == "wrong_account"

    def test_the_timeout_reaches_the_request(self, nc_config):
        with patch(GET, return_value=_ocs_response({"id": "istota"})) as get:
            sign_in(nc_config, timeout=3.5)
        assert get.call_args.kwargs["timeout"] == 3.5

    def test_anything_but_an_ocs_error_propagates(self, nc_config):
        with patch("istota.nextcloud.capabilities.fetch_account", side_effect=RuntimeError("bug")):
            with pytest.raises(RuntimeError):
                sign_in(nc_config)
