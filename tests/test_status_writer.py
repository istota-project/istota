"""Tests for status file writer."""

import json
from unittest.mock import MagicMock, patch

from istota.config import Config, NextcloudConfig
from istota.status_writer import init_status_writer, write_status


def _make_config(**kwargs):
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="botuser",
            app_password="secret",
        ),
        **kwargs,
    )


def _dav_ok():
    """Patch the shared WebDAV primitive's transport, returning success."""
    return patch(
        "istota.nextcloud._http.httpx.request",
        return_value=MagicMock(status_code=201, text=""),
    )


def _call(mock_request, method):
    """The recorded httpx.request call for a given WebDAV method."""
    for call in mock_request.call_args_list:
        if call[0][0] == method:
            return call
    raise AssertionError(f"no {method} call recorded")


class TestStatusWriter:
    def test_writes_valid_json(self):
        cfg = _make_config()
        init_status_writer()

        with _dav_ok() as mock_request:
            write_status(cfg, active_workers=2, pending_fg=3, pending_bg=1)

            # One PUT of the status file, and nothing else: the account root
            # always exists, so there is no directory to create first.
            methods = [c[0][0] for c in mock_request.call_args_list]
            assert methods == ["PUT"]

            content = _call(mock_request, "PUT").kwargs["content"]
            data = json.loads(content)
            assert data["status"] == "online"
            assert data["worker_pool"]["active"] == 2
            assert data["queue"]["pending_foreground"] == 3
            assert data["queue"]["pending_background"] == 1

    def test_includes_bot_name_and_version(self):
        cfg = _make_config(bot_name="Istota")
        init_status_writer()

        with _dav_ok() as mock_request:
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            content = _call(mock_request, "PUT").kwargs["content"]
            data = json.loads(content)
            assert data["bot_name"] == "Istota"
            assert "version" in data

    def test_no_nextcloud_config_is_noop(self):
        cfg = Config()
        # Should not raise
        write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

    def test_webdav_url_correct(self):
        cfg = _make_config()
        init_status_writer()

        with _dav_ok() as mock_request:
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            put_url = _call(mock_request, "PUT")[0][1]
            assert put_url == (
                "https://cloud.example.com/remote.php/dav/files/botuser/status.json"
            )

    def test_creates_no_directory(self):
        """The whole write is one PUT to the account root, and nothing else.

        It used to be preceded by ``MKCOL config``, which left a directory
        beside ``Users/`` and ``Channels/`` holding one file. Asserting the
        exact request sequence rather than the absence of an MKCOL is what
        makes the name and the assertion say the same thing: any extra
        request fails this, whatever its method or path.
        """
        cfg = _make_config()
        init_status_writer()

        with _dav_ok() as mock_request:
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            calls = [(c[0][0], c[0][1]) for c in mock_request.call_args_list]
            assert calls == [
                ("PUT", "https://cloud.example.com/remote.php/dav/files/botuser/status.json")
            ]

    def test_a_failed_put_is_logged_rather_than_raised(self):
        """The 60s `status-write` gate calls this; a raise would break the tick.

        A 403 on the account root is the live failure mode — `dav_request`
        raises `OcsError` on any non-2xx, and swallowing it here is what keeps
        an unwritable root to a log line rather than a broken scheduler tick.
        """
        cfg = _make_config()
        init_status_writer()

        with patch(
            "istota.nextcloud._http.httpx.request",
            return_value=MagicMock(status_code=403, text="Forbidden"),
        ):
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

    def test_users_configured_count(self):
        from istota.config import UserConfig
        cfg = _make_config()
        cfg.users["alice"] = UserConfig(display_name="Alice")
        cfg.users["bob"] = UserConfig(display_name="Bob")

        init_status_writer()

        with _dav_ok() as mock_request:
            write_status(cfg, active_workers=0, pending_fg=0, pending_bg=0)

            content = _call(mock_request, "PUT").kwargs["content"]
            data = json.loads(content)
            assert data["users_configured"] == 2
