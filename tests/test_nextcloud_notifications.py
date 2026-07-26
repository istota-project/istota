"""Notifications and activity (Stage 6)."""

import json
from unittest.mock import patch

import pytest

from istota.config import Config, NextcloudConfig
from istota.nextcloud import OcsError, notifications as notify
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


def _run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    return (json.loads(out) if out.strip() else None), code


NOTIFICATIONS = [
    {
        "notification_id": 1,
        "app": "files_sharing",
        "datetime": "2026-07-25T10:00:00+00:00",
        "subject": "Bob shared report.pdf with you",
        "message": "have a look",
        "link": "https://cloud.example.com/f/1",
        "object_type": "remote_share",
        "object_id": "7",
    },
    {"notification_id": 2, "app": "spreed", "subject": "New message", "message": ""},
    {"notification_id": 3, "app": "spreed", "subject": "Another", "message": ""},
]

ACTIVITY = [
    {
        "activity_id": 10,
        "app": "files",
        "type": "file_created",
        "datetime": "2026-07-25T10:00:00+00:00",
        "user": "alice",
        "subject": "You created report.pdf",
        "message": "",
        "object_type": "files",
        "object_name": "/report.pdf",
    }
]

CAPS_DELETE_ALL = {
    "capabilities": {"notifications": {"ocs-endpoints": ["list", "delete", "delete-all"]}}
}
CAPS_NO_DELETE_ALL = {"capabilities": {"notifications": {"ocs-endpoints": ["list", "delete"]}}}


class TestNotificationsModule:
    @patch("istota.nextcloud.notifications.ocs_get")
    def test_list_is_bounded(self, mock_get, nc_config):
        mock_get.return_value = NOTIFICATIONS
        assert len(notify.list_notifications(nc_config, limit=2)) == 2

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_default_limit(self, mock_get, nc_config):
        mock_get.return_value = NOTIFICATIONS
        assert notify.DEFAULT_LIMIT == 25
        assert len(notify.list_notifications(nc_config)) == 3

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_empty_response(self, mock_get, nc_config):
        mock_get.return_value = None
        assert notify.list_notifications(nc_config) == []

    @patch("istota.nextcloud.notifications.ocs_request")
    def test_dismiss(self, mock_request, nc_config):
        assert notify.dismiss(nc_config, 1)["dismissed"] == 1
        assert mock_request.call_args[0][1] == "DELETE"
        assert mock_request.call_args[0][2].endswith("/notifications/1")

    @patch("istota.nextcloud.notifications.ocs_request")
    def test_dismiss_all_when_supported(self, mock_request, nc_config):
        result = notify.dismiss_all(nc_config, capabilities=CAPS_DELETE_ALL)
        assert result["dismissed"] == "all"
        assert mock_request.call_args[0][2] == notify.NOTIFICATIONS_PATH

    @patch("istota.nextcloud.notifications.ocs_request")
    def test_dismiss_all_gated_on_the_capability(self, mock_request, nc_config):
        with pytest.raises(OcsError) as exc:
            notify.dismiss_all(nc_config, capabilities=CAPS_NO_DELETE_ALL)
        assert "individually" in exc.value.message
        mock_request.assert_not_called()

    @patch("istota.nextcloud.notifications.ocs_request")
    def test_dismiss_all_proceeds_when_capabilities_unknown(self, mock_request, nc_config):
        """A failed probe must not block a legitimate clear."""
        notify.dismiss_all(nc_config, capabilities=None)
        mock_request.assert_called_once()

    def test_supports_delete_all(self):
        assert notify.supports_delete_all(CAPS_DELETE_ALL) is True
        assert notify.supports_delete_all(CAPS_NO_DELETE_ALL) is False
        assert notify.supports_delete_all({}) is False


class TestActivityModule:
    @patch("istota.nextcloud.notifications.ocs_get")
    def test_default_limit_is_sent(self, mock_get, nc_config):
        mock_get.return_value = ACTIVITY
        notify.list_activity(nc_config)
        assert mock_get.call_args.kwargs["params"]["limit"] == "25"

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_since_and_filter(self, mock_get, nc_config):
        mock_get.return_value = []
        notify.list_activity(nc_config, since=100, activity_filter="files", limit=5)
        assert mock_get.call_args[0][1] == "/apps/activity/api/v2/activity/filter/files"
        assert mock_get.call_args.kwargs["params"] == {"limit": "5", "since": "100"}

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_object_filter_needs_both_halves(self, mock_get, nc_config):
        mock_get.return_value = []
        notify.list_activity(nc_config, object_type="files", object_id="7")
        params = mock_get.call_args.kwargs["params"]
        assert params["object_type"] == "files"
        assert params["object_id"] == "7"

        mock_get.reset_mock()
        notify.list_activity(nc_config, object_type="files")
        assert "object_type" not in mock_get.call_args.kwargs["params"]

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_zero_limit_falls_back_to_the_default(self, mock_get, nc_config):
        """An unbounded activity feed is a context-flooding hazard."""
        mock_get.return_value = []
        notify.list_activity(nc_config, limit=0)
        assert mock_get.call_args.kwargs["params"]["limit"] == "25"


class TestNotifyCli:
    @patch("istota.nextcloud.notifications.ocs_get")
    def test_list_is_untrusted_framed(self, mock_get, capsys):
        mock_get.return_value = NOTIFICATIONS
        out, code = _run(capsys, ["notify", "list"])
        assert code == 0
        assert out["untrusted"] is True
        assert "[UNTRUSTED NEXTCLOUD CONTENT" in out["notifications"][0]["subject"]
        assert "Bob shared report.pdf" in out["notifications"][0]["subject"]

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_list_limit(self, mock_get, capsys):
        mock_get.return_value = NOTIFICATIONS
        out, _ = _run(capsys, ["notify", "list", "--limit", "1"])
        assert out["count"] == 1

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_get(self, mock_get, capsys):
        mock_get.return_value = NOTIFICATIONS[0]
        out, code = _run(capsys, ["notify", "get", "1"])
        assert code == 0
        assert out["notification"]["id"] == 1

    @patch("istota.nextcloud.notifications.ocs_request")
    def test_dismiss(self, mock_request, capsys):
        out, code = _run(capsys, ["notify", "dismiss", "1"])
        assert code == 0
        assert out["dismissed"] == 1

    @patch("istota.nextcloud.notifications.ocs_request")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_dismiss_all_blocked_without_the_capability(self, mock_caps, mock_req, capsys):
        mock_caps.return_value = CAPS_NO_DELETE_ALL
        out, code = _run(capsys, ["notify", "dismiss-all"])
        assert code == 1
        assert out["status"] == "error"
        mock_req.assert_not_called()

    @patch("istota.nextcloud.notifications.ocs_request")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_dismiss_all_allowed(self, mock_caps, mock_req, capsys):
        mock_caps.return_value = CAPS_DELETE_ALL
        out, code = _run(capsys, ["notify", "dismiss-all"])
        assert code == 0
        assert out["dismissed"] == "all"

    @patch("istota.nextcloud.notifications.ocs_get")
    def test_activity_list(self, mock_get, capsys):
        mock_get.return_value = ACTIVITY
        out, code = _run(capsys, ["activity", "list", "--limit", "5"])
        assert code == 0
        assert out["untrusted"] is True
        assert out["count"] == 1
        assert "[UNTRUSTED NEXTCLOUD CONTENT" in out["activity"][0]["subject"]


class TestNotifyParser:
    def test_verbs_parse(self):
        parser = build_parser()
        for argv in (
            ["notify", "list"],
            ["notify", "get", "1"],
            ["notify", "dismiss", "1"],
            ["notify", "dismiss-all"],
            ["activity", "list"],
        ):
            parser.parse_args(argv)

    def test_activity_filters(self):
        args = build_parser().parse_args([
            "activity", "list", "--since", "5", "--type", "files",
            "--object-type", "files", "--object-id", "7",
        ])
        assert args.since == 5
        assert args.type == "files"
        assert args.object_id == "7"
