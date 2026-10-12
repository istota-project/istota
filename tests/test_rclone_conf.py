"""`lib/rclone_conf.py`: rclone's obscuring, reimplemented, and the remote it renders."""

from __future__ import annotations

import pytest

from istota.lib.rclone_conf import obscure, render_webdav_remote, reveal


class TestObscure:
    def test_it_matches_rclones_own_vectors(self):
        """From rclone's `fs/config/obscure/obscure_test.go`, `TestObscure`."""
        assert obscure("", iv=b"a" * 16) == "YWFhYWFhYWFhYWFhYWFhYQ"
        assert obscure("potato", iv=b"a" * 16) == "YWFhYWFhYWFhYWFhYWFhYXMaGgIlEQ"
        assert obscure("potato", iv=b"b" * 16) == "YmJiYmJiYmJiYmJiYmJiYp3gcEWbAw"

    @pytest.mark.parametrize("value", ["nc-app-pass", "", "pässwörd with spaces", "a" * 200])
    def test_it_round_trips_and_is_salted(self, value):
        first, second = obscure(value), obscure(value)
        assert first != second
        assert "=" not in first and "+" not in first and "/" not in first
        assert reveal(first) == value and reveal(second) == value

    def test_a_short_iv_is_refused(self):
        with pytest.raises(ValueError):
            obscure("x", iv=b"short")


class TestTheRemote:
    def test_it_renders_the_nextcloud_files_root(self):
        assert render_webdav_remote("nextcloud", "https://cloud.example.test/", "zed-bot", "OBS") == (
            "[nextcloud]\n"
            "type = webdav\n"
            "url = https://cloud.example.test/remote.php/dav/files/zed-bot/\n"
            "vendor = nextcloud\n"
            "user = zed-bot\n"
            "pass = OBS\n"
        )

    @pytest.mark.parametrize("field", range(4))
    def test_a_line_break_in_any_value_is_refused(self, field):
        values = ["nextcloud", "https://cloud.example.test", "zed-bot", "OBS"]
        values[field] += "\n[evil]\ntype = local"
        with pytest.raises(ValueError):
            render_webdav_remote(*values)
