"""A linked resource keeps its identity from validation to mount (ISSUE-881).

The render used to name the resource by path, so whatever sat at that name when
bwrap walked it became the bind's source. These hold the shape that replaces it:
no source path in the argv at all, and a launcher that opens the validated object
without following a link. `tests/linux/test_resource_bind_identity.py` runs it on
a real kernel.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import executor
from istota.config import Config
from istota.sandbox import bind_launcher
from istota.sandbox.plan import MountPlan, render_bwrap_argv, user_data_mounts
from istota.sandbox.shell_exec import on_daemon_path

BWRAP = "/usr/bin/bwrap"


def _resource_mount(mount: Path, path: str, permissions: str = "read"):
    entries = user_data_mounts(
        Config(workspace_path=mount), user_id="alice", conversation_token=None,
        user_resources=[SimpleNamespace(resource_path=path, permissions=permissions)],
        withheld_scopes=frozenset(), group_ids=[],
    )
    return next(m for m in entries if m.reason == "user_resource")


def _render(entries, tmp_path, bwrap=BWRAP):
    with (
        patch.object(executor, "_bwrap_fd_bind_binary", return_value=bwrap),
        patch.object(executor, "_bwrap_supports_disable_userns", return_value=False),
        patch.object(executor, "_bwrap_requires_unshare_user", return_value=False),
    ):
        return render_bwrap_argv(
            MountPlan(mounts=tuple(entries), chdir=tmp_path), ["true"],
            user_temp_dir=tmp_path,
        )


@pytest.fixture
def world(tmp_path):
    mount = tmp_path / "mount"
    linked = mount / "Shared" / "docs"
    linked.mkdir(parents=True)
    (linked / "note.txt").write_text("approved")
    private = tmp_path / "private"
    private.mkdir()
    (private / "canary.txt").write_text("private")
    return SimpleNamespace(
        root=tmp_path, mount=mount.resolve(), parent=mount / "Shared", linked=linked,
        private=private.resolve(),
    )


class TestRender:
    def test_a_swap_after_planning_puts_no_other_path_in_the_argv(self, world):
        entry = _resource_mount(world.mount, "Shared/docs")
        world.linked.rename(world.root / "moved")
        world.linked.symlink_to(world.private, target_is_directory=True)

        argv = _render([entry], world.root)

        assert not any(str(world.private) in token for token in argv)
        assert "--ro-bind" not in argv

    def test_the_launcher_wraps_bwrap_and_names_the_walk_not_the_target(self, world):
        entry = _resource_mount(world.mount, "Shared/docs")

        argv = _render([entry], world.root)

        separator = argv.index("--")
        assert argv[:5] == [
            sys.executable, "-I", "-m", "istota.sandbox.bind_launcher", "--binds",
        ]
        assert argv[separator + 1] == BWRAP
        at = argv.index("--ro-bind-fd")
        assert argv[at + 1] == bind_launcher.FD_PLACEHOLDER
        assert argv[at + 2] == str(world.mount / "Shared" / "docs")
        [bind] = json.loads(argv[5])
        assert bind["root"] == str(world.mount)
        assert bind["parts"] == ["Shared", "docs"]

    def test_a_readwrite_resource_is_a_writable_fd_bind(self, world):
        entry = _resource_mount(world.mount, "Shared/docs", "readwrite")

        argv = _render([entry], world.root)

        assert "--bind-fd" in argv and "--ro-bind-fd" not in argv

    def test_a_task_with_no_resource_keeps_a_bare_bwrap(self, world):
        with patch.object(executor, "_bwrap_fd_bind_binary") as binary:
            with (
                patch.object(executor, "_bwrap_supports_disable_userns", return_value=False),
                patch.object(executor, "_bwrap_requires_unshare_user", return_value=False),
            ):
                argv = render_bwrap_argv(
                    MountPlan(mounts=(), chdir=world.root), ["true"],
                    user_temp_dir=world.root,
                )
        assert argv[0] == "bwrap"
        binary.assert_not_called()

    def test_a_bwrap_without_fd_binds_loses_the_bind_not_the_identity(self, world, caplog):
        entry = _resource_mount(world.mount, "Shared/docs")

        argv = _render([entry], world.root, bwrap=None)

        assert argv[0] == "bwrap"
        assert not any("Shared/docs" in token for token in argv)
        assert "no --ro-bind-fd" in caplog.text


class TestOpenSource:
    def test_the_fd_stays_on_the_validated_object_after_a_swap(self, world):
        before = os.stat(world.linked)
        fd = bind_launcher.open_source(str(world.mount), ["Shared", "docs"])
        try:
            world.linked.rename(world.root / "moved")
            world.linked.symlink_to(world.private, target_is_directory=True)
            after = os.fstat(fd)
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert os.get_inheritable(fd)
        finally:
            os.close(fd)

    def test_a_link_at_the_leaf_is_refused(self, world):
        world.linked.rename(world.root / "moved")
        world.linked.symlink_to(world.private, target_is_directory=True)

        assert bind_launcher.open_source(str(world.mount), ["Shared", "docs"]) is None

    def test_a_link_at_a_parent_is_refused(self, world):
        world.parent.rename(world.root / "moved")
        (world.private / "docs").mkdir()
        world.parent.symlink_to(world.private, target_is_directory=True)

        assert bind_launcher.open_source(str(world.mount), ["Shared", "docs"]) is None

    def test_a_regular_file_opens(self, world):
        fd = bind_launcher.open_source(str(world.mount), ["Shared", "docs", "note.txt"])
        assert fd is not None
        os.close(fd)

    def test_a_fifo_is_refused_without_blocking(self, world):
        os.mkfifo(world.linked / "pipe")

        assert bind_launcher.open_source(str(world.mount), ["Shared", "docs", "pipe"]) is None

    @pytest.mark.parametrize("parts", [[], [".."], ["Shared", ".."], ["a/b"], ["Shared", ""]])
    def test_a_part_that_is_not_one_component_is_refused(self, world, parts):
        assert bind_launcher.open_source(str(world.mount), parts) is None


class TestApplyBinds:
    def _bind(self, world, parts):
        return {"root": str(world.mount), "parts": parts}

    def test_each_refused_bind_leaves_the_others_where_they_were(self, world):
        (world.mount / "Other").mkdir()
        command = [
            BWRAP, "--ro-bind-fd", bind_launcher.FD_PLACEHOLDER, "/gone",
            "--bind-fd", bind_launcher.FD_PLACEHOLDER, "/kept",
            "--ro-bind-fd", bind_launcher.FD_PLACEHOLDER, "/gone-too",
            "--", "true",
        ]
        binds = [
            self._bind(world, ["Absent"]),
            self._bind(world, ["Other"]),
            self._bind(world, ["Also", "absent"]),
        ]

        result = bind_launcher.apply_binds(binds, command)

        assert result[0] == BWRAP
        assert result[1] == "--bind-fd"
        assert result[2].isdigit()
        assert result[3:] == ["/kept", "--", "true"]
        os.close(int(result[2]))

    def test_a_bind_with_no_placeholder_is_refused(self, world):
        command = [BWRAP, "--ro-bind", "/x", "/y"]
        with pytest.raises(bind_launcher.Refused):
            bind_launcher.apply_binds([self._bind(world, ["Shared"])], command)

    def test_nothing_past_the_separator_is_a_slot(self, world):
        command = [
            BWRAP, "--", "/bin/sh", "--ro-bind-fd", bind_launcher.FD_PLACEHOLDER, "/x",
        ]
        with pytest.raises(bind_launcher.Refused):
            bind_launcher.apply_binds([self._bind(world, ["Shared"])], command)


class TestTheLauncherProcess:
    def _run(self, binds, command, **kwargs):
        return subprocess.run(
            [
                sys.executable, "-I", "-m", "istota.sandbox.bind_launcher",
                "--binds", json.dumps(binds), "--", *command,
            ],
            capture_output=True, text=True, timeout=30, **kwargs,
        )

    def test_it_execs_with_the_opened_fd_inherited(self, world):
        result = self._run(
            [{"root": str(world.mount), "parts": ["Shared", "docs", "note.txt"]}],
            [
                "/bin/sh", "-c", 'cat "/dev/fd/$2"', "sh", "--ro-bind-fd",
                bind_launcher.FD_PLACEHOLDER, "/dest",
            ],
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "approved"

    def test_a_refused_bind_still_runs_the_command_and_says_so(self, world):
        result = self._run(
            [{"root": str(world.mount), "parts": ["Absent"]}],
            [
                "/bin/sh", "-c", 'echo "args=$#"', "--ro-bind-fd",
                bind_launcher.FD_PLACEHOLDER, "/dest",
            ],
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "args=0"
        assert "/dest" in result.stderr

    def test_a_relative_command_is_refused_and_nothing_searches_path(self, world):
        result = self._run([], ["sh", "-c", "echo ran"])

        assert result.returncode == 2
        assert "ran" not in result.stdout

    def test_it_needs_nothing_from_the_environment(self, world):
        result = self._run([], ["/bin/sh", "-c", "echo ran"], env={})

        assert result.returncode == 0
        assert result.stdout.strip() == "ran"


class TestTheBinaryProbe:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        saved = dict(executor._bwrap_flag_support)
        executor._bwrap_flag_support.clear()
        yield
        executor._bwrap_flag_support.clear()
        executor._bwrap_flag_support.update(saved)

    def _help(self, text):
        return patch.object(
            executor.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=text, stderr=b""),
        )

    def test_a_bwrap_that_lists_the_flag_gives_its_absolute_path(self):
        with (
            patch.object(executor, "_bwrap_available", return_value=True),
            self._help(b"  --ro-bind-fd FD DEST  Bind open directory or path fd\n"),
            patch("shutil.which", return_value="/usr/bin/bwrap"),
        ):
            assert executor._bwrap_fd_bind_binary() == "/usr/bin/bwrap"

    def test_a_bwrap_that_does_not_gives_none(self):
        with (
            patch.object(executor, "_bwrap_available", return_value=True),
            self._help(b"  --ro-bind SRC DEST\n"),
        ):
            assert executor._bwrap_fd_bind_binary() is None

    def test_no_bwrap_gives_none_without_running_anything(self):
        with (
            patch.object(executor, "_bwrap_available", return_value=False),
            patch.object(executor.subprocess, "run") as run,
        ):
            assert executor._bwrap_fd_bind_binary() is None
        run.assert_not_called()


class TestABareCommandIsNotSearchedOnTheTasksPath:
    """A task's `PATH` starts under a directory a model can write."""

    def _plant(self, tmp_path):
        planted = tmp_path / "planted"
        planted.mkdir()
        marker = tmp_path / "ran"
        tool = planted / "some-sandbox-tool"
        tool.write_text(f"#!/bin/sh\ntouch {marker}\n")
        tool.chmod(0o755)
        return planted, marker

    @pytest.mark.asyncio
    async def test_the_native_spawn_refuses_what_only_the_tasks_path_has(
        self, tmp_path, monkeypatch,
    ):
        from istota.session.tools import remote

        planted, marker = self._plant(tmp_path)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        with pytest.raises(remote.ToolServerError, match="daemon's PATH"):
            await remote.start_tool_server(
                {},
                sandbox_wrap=lambda cmd: ["some-sandbox-tool", *cmd],
                cwd=tmp_path,
                env={"PATH": str(planted)},
            )
        assert not marker.exists()

    def test_on_daemon_path_reads_the_daemons_path(self, tmp_path, monkeypatch):
        planted, _ = self._plant(tmp_path)
        monkeypatch.setenv("PATH", str(planted))
        assert on_daemon_path("some-sandbox-tool") == str(planted / "some-sandbox-tool")
        monkeypatch.setenv("PATH", "/nonexistent")
        assert on_daemon_path("some-sandbox-tool") is None
