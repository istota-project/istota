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


class TestExpectedType:
    """What the workspace mount must be, and when nothing says: the stray
    workspace the vm tier left on its VM, config.toml naming the folder's mount
    while host.env said STORAGE=local, so istota-stack passed no type."""

    def test_the_host_declares_it(self):
        env = {workspace_marker.FSTYPE_ENV: " virtiofs "}
        assert workspace_marker.expected_fstype(False, env) == "virtiofs"
        assert workspace_marker.expected_fstype(True, env) == "virtiofs"

    def test_full_integration_falls_back_to_rclone(self):
        assert workspace_marker.expected_fstype(True, {}) == "fuse.rclone"

    def test_otherwise_nothing_says(self):
        assert workspace_marker.expected_fstype(False, {}) == ""

    def test_a_mount_path_nobody_vouches_for_is_undeclared(self):
        assert workspace_marker.undeclared_mount("/mnt/shared", False, {}) is True

    def test_a_declared_an_rclone_or_no_mount_is_not(self):
        env = {workspace_marker.FSTYPE_ENV: "virtiofs"}
        assert workspace_marker.undeclared_mount("/mnt/shared", False, env) is False
        assert workspace_marker.undeclared_mount("/mnt/shared", True, {}) is False
        assert workspace_marker.undeclared_mount(None, False, {}) is False
