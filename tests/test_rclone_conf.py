"""`lib/rclone_conf.py`: rclone's obscuring, reimplemented, and the remote it renders."""

from __future__ import annotations

import pytest

from istota.lib.rclone_conf import obscure, render_webdav_remote, reveal, webdav_remote_for


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


class TestKeepingTheExistingRemote:
    """rclone's obscuring is salted, so re-rendering the same answers changes
    the file, and with it `provision.sh --digest` (#720). An existing file that
    says the same thing once its password is revealed is kept as it is."""

    ANSWERS = ("nextcloud", "https://cloud.example.test/", "zed-bot", "nc-app-pass")

    def _existing(self) -> str:
        remote, url, username, password = self.ANSWERS
        return render_webdav_remote(remote, url, username, obscure(password))

    def test_the_same_answers_keep_the_existing_file(self):
        existing = self._existing()
        assert webdav_remote_for(existing, *self.ANSWERS) == existing

    @pytest.mark.parametrize("field,value", [
        (1, "https://other.example.test"), (2, "other-bot"), (3, "new-app-pass"), (0, "other"),
    ], ids=["url", "username", "password", "remote"])
    def test_a_changed_answer_renders_afresh(self, field, value):
        existing = self._existing()
        answers = list(self.ANSWERS)
        answers[field] = value
        text = webdav_remote_for(existing, *answers)
        assert text != existing
        assert text.startswith(f"[{answers[0]}]\n")
        assert reveal(text.splitlines()[-1].removeprefix("pass = ")) == answers[3]

    @pytest.mark.parametrize("existing", [
        None, "", "[nextcloud]\ntype = webdav\n", "pass = not-base64!\n", "pass = AAAA\n",
    ], ids=["absent", "empty", "no-pass", "garbage-pass", "short-pass"])
    def test_nothing_usable_renders_afresh(self, existing):
        text = webdav_remote_for(existing, *self.ANSWERS)
        assert reveal(text.splitlines()[-1].removeprefix("pass = ")) == "nc-app-pass"

    def test_an_edited_file_with_the_same_password_is_rewritten(self):
        existing = self._existing().replace("vendor = nextcloud\n", "vendor = nextcloud\nheaders = X-Y,z\n")
        text = webdav_remote_for(existing, *self.ANSWERS)
        assert "headers" not in text
