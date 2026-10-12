"""`lib/workspace_marker`: the file that says a host folder is the workspace.

Its three readers (the entrypoint's preflight, doctor's liveness check, the
backup's guard) each have their own tests; these hold the two rules they share.
"""

from __future__ import annotations

import os

import pytest

from istota.lib import workspace_marker


class TestRequired:
    @pytest.mark.parametrize("value,expected", [
        ("1", True), (" 1 ", True), ("", False), ("0", False), ("true", False),
    ])
    def test_only_a_one_asks_for_it(self, value, expected):
        assert workspace_marker.marker_required({workspace_marker.REQUIRED_ENV: value}) is expected

    def test_unset_asks_for_nothing(self):
        assert workspace_marker.marker_required({}) is False


class TestPresent:
    def test_a_regular_file_is_the_marker(self, tmp_path):
        (tmp_path / workspace_marker.MARKER_NAME).write_text("")
        assert workspace_marker.marker_present(tmp_path) is True

    def test_nothing_there_is_not(self, tmp_path):
        assert workspace_marker.marker_present(tmp_path) is False

    def test_a_directory_is_not(self, tmp_path):
        (tmp_path / workspace_marker.MARKER_NAME).mkdir()
        assert workspace_marker.marker_present(tmp_path) is False

    def test_a_link_is_not_even_to_a_marker(self, tmp_path):
        """A link is resolved on the side that reads it; a Mac-made link in the
        folder names a path the VM cannot see (Stage 1)."""
        real = tmp_path / "elsewhere"
        real.mkdir()
        (real / workspace_marker.MARKER_NAME).write_text("")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        os.symlink(real / workspace_marker.MARKER_NAME, workspace / workspace_marker.MARKER_NAME)
        assert workspace_marker.marker_present(workspace) is False

    def test_an_unreadable_root_is_not(self, tmp_path):
        assert workspace_marker.marker_present(tmp_path / "missing" / "deeper") is False
