"""Tests for the devbox skill CLI."""

import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from istota.skills import devbox


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ISTOTA_USER_ID", "bob")
    monkeypatch.setenv("ISTOTA_DEVBOX_CONTAINER", "devbox-bob")
    monkeypatch.setenv("ISTOTA_DEVBOX_DOCKER_CLI", "/usr/bin/docker")
    monkeypatch.delenv("ISTOTA_DEVBOX_EXEC_TIMEOUT", raising=False)
    monkeypatch.delenv("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", raising=False)
    # cp-in / cp-out require an allowlist; point at tmp_path so tests
    # can build host paths inside it.
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)


def _ownership_sequence(*, owner: str = "bob", running: bool = True) -> list[tuple[int, bytes, bytes]]:
    """The _check_owned() helper makes two inspect calls. Return the standard
    "container is running, owned by current user" response pair."""
    return [
        (0, b"true" if running else b"false", b""),
        (0, owner.encode(), b""),
    ]


def _drain(returns):
    """Iterator factory — pop in order. Tests stage docker responses in a list."""
    it = iter(returns)
    return lambda argv, timeout: next(it)


class TestContainerName:
    def test_uses_env_var(self):
        assert devbox._container_name() == "devbox-bob"

    def test_falls_back_to_user_id(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_DEVBOX_CONTAINER")
        assert devbox._container_name() == "devbox-bob"

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_DEVBOX_CONTAINER")
        monkeypatch.delenv("ISTOTA_USER_ID")
        assert devbox._container_name() is None

    @pytest.mark.parametrize("bad", [
        "devbox bob",          # space
        "devbox-bob;rm -rf /", # shell metachars
        "../escape",              # path traversal
        "$(whoami)",              # command substitution
    ])
    def test_rejects_dangerous_names(self, monkeypatch, bad):
        monkeypatch.setenv("ISTOTA_DEVBOX_CONTAINER", bad)
        assert devbox._container_name() is None


class TestTimeoutAndCap:
    def test_default_timeout(self):
        assert devbox._exec_timeout() == 300

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEVBOX_EXEC_TIMEOUT", "60")
        assert devbox._exec_timeout() == 60

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEVBOX_EXEC_TIMEOUT", "not-a-number")
        assert devbox._exec_timeout() == 300

    def test_max_output_floor(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", "10")
        assert devbox._max_output_bytes() == 1024


class TestTruncate:
    def test_short_passes_through(self):
        assert devbox._truncate(b"hello", 100) == "hello"

    def test_long_signals_truncation(self):
        out = devbox._truncate(b"x" * 200, 50)
        assert out.startswith("x" * 50)
        assert "[truncated: 150 more bytes]" in out


class TestValidateCommand:
    def test_accepts_normal(self):
        assert devbox._validate_command("dig MX example.com") is None

    def test_rejects_nul_byte(self):
        err = devbox._validate_command("echo hi\x00; rm -rf /")
        assert err is not None
        assert "NUL byte" in err

    def test_rejects_oversized(self):
        big = "x" * (devbox.MAX_COMMAND_BYTES + 1)
        err = devbox._validate_command(big)
        assert err is not None
        assert "exceeds" in err


class TestValidateHostPath:
    def test_rejects_path_outside_allowlist(self, monkeypatch, tmp_path):
        # /etc/passwd is outside ISTOTA_DEFERRED_DIR.
        err = devbox._validate_host_path(Path("/etc/passwd"), must_exist=True)
        assert err is not None
        assert "outside allowed roots" in err

    def test_accepts_path_inside_deferred_dir(self, tmp_path):
        p = tmp_path / "ok.txt"
        p.write_text("hi")
        assert devbox._validate_host_path(p, must_exist=True) is None

    def test_rejects_symlink_source(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        err = devbox._validate_host_path(link, must_exist=True)
        assert err is not None
        assert "symlink" in err

    def test_dest_creates_parent(self, tmp_path):
        dest = tmp_path / "nested" / "out.txt"
        assert not dest.parent.exists()
        assert devbox._validate_host_path(dest, must_exist=False) is None
        assert dest.parent.exists()

    def test_refuses_when_allowlist_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR")
        err = devbox._validate_host_path(tmp_path / "x", must_exist=False)
        assert err is not None
        assert "allowed host roots" in err

    def test_nextcloud_mount_path_is_also_allowed(self, monkeypatch, tmp_path):
        nc = tmp_path / "nc"
        nc.mkdir()
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(nc))
        candidate = nc / "Users" / "bob" / "f.txt"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("hi")
        assert devbox._validate_host_path(candidate, must_exist=True) is None


class TestParser:
    def test_exec(self):
        args = devbox.build_parser().parse_args(["exec", "echo hi"])
        assert args.subcommand == "exec"
        assert args.command == "echo hi"

    def test_exec_with_timeout(self):
        args = devbox.build_parser().parse_args(["exec", "sleep 1", "--timeout", "10"])
        assert args.timeout == 10

    def test_exec_file(self):
        args = devbox.build_parser().parse_args(
            ["exec-file", "/tmp/x.py", "--interpreter", "python3"]
        )
        assert args.subcommand == "exec-file"
        assert args.path == "/tmp/x.py"
        assert args.interpreter == "python3"

    def test_cp_in(self):
        args = devbox.build_parser().parse_args(["cp-in", "/a", "/b"])
        assert args.subcommand == "cp-in"
        assert args.src == "/a"
        assert args.dest == "/b"

    def test_status(self):
        args = devbox.build_parser().parse_args(["status"])
        assert args.subcommand == "status"

    def test_reset_requires_yes(self):
        args = devbox.build_parser().parse_args(["reset"])
        assert args.subcommand == "reset"
        assert args.yes is False


class TestExec:
    def test_returns_error_when_no_container(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_DEVBOX_CONTAINER")
        monkeypatch.delenv("ISTOTA_USER_ID")
        args = type("A", (), {"command": "echo hi", "timeout": None})()
        result = devbox.cmd_exec(args)
        assert result["status"] == "error"
        assert "No devbox" in result["error"]

    def test_errors_when_not_running(self, monkeypatch):
        # _check_owned probes inspect Running first; "false" short-circuits.
        monkeypatch.setattr(devbox, "_run_docker", _drain([(0, b"false", b"")]))
        args = type("A", (), {"command": "echo hi", "timeout": None})()
        result = devbox.cmd_exec(args)
        assert result["status"] == "error"
        assert "not running" in result["error"]

    def test_refuses_when_owner_label_mismatches(self, monkeypatch):
        # State=running, but label points at the wrong user.
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            (0, b"true", b""),
            (0, b"alice", b""),
        ]))
        args = type("A", (), {"command": "echo hi", "timeout": None})()
        result = devbox.cmd_exec(args)
        assert result["status"] == "error"
        assert "owned by 'alice'" in result["error"]

    def test_refuses_nul_byte_in_command(self):
        args = type("A", (), {"command": "echo hi\x00bad", "timeout": None})()
        result = devbox.cmd_exec(args)
        assert result["status"] == "error"
        assert "NUL" in result["error"]

    def test_happy_path(self, monkeypatch):
        invocations = []
        seq = iter([
            *_ownership_sequence(),       # _check_owned
            (0, b"hi\n", b""),            # the actual exec
        ])
        def fake_run(argv, timeout):
            invocations.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"command": "echo hi", "timeout": None})()
        result = devbox.cmd_exec(args)
        assert result["status"] == "ok"
        assert result["exit_code"] == 0
        assert result["stdout"] == "hi\n"
        exec_argv = invocations[-1]
        assert exec_argv[0] == "exec"
        assert "devbox-bob" in exec_argv
        assert exec_argv[-3] == "bash"
        assert exec_argv[-2] == "-c"
        assert exec_argv[-1] == "echo hi"
        # The working directory must be somewhere `docker cp` can reach, or a
        # relative path written by the command cannot be copied back out
        # (ISSUE-306).
        cwd = exec_argv[exec_argv.index("-w") + 1]
        assert cwd == devbox._DEFAULT_WORKDIR
        for mount in devbox._CONTAINER_TMPFS_MOUNTS:
            assert cwd != mount and not cwd.startswith(mount + "/")

    def test_timeout_returns_error(self, monkeypatch):
        # Ownership pair, then the exec times out, then the straggler-kill
        # helper makes a probe + (zero) kill calls; we return empty.
        seq = iter([
            *_ownership_sequence(),
            None,  # sentinel — actual exec raises TimeoutExpired below
            (0, b"", b""),  # _kill_stragglers ps probe (empty)
        ])
        def fake_run(argv, timeout):
            val = next(seq)
            if val is None:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            return val
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"command": "sleep 999", "timeout": 1})()
        result = devbox.cmd_exec(args)
        assert result["status"] == "error"
        assert "timed out" in result["error"]


class TestExecFile:
    def test_happy_path(self, monkeypatch, tmp_path):
        script = tmp_path / "x.py"
        script.write_text("print('hi')\n")
        invocations = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),                # mkdir -p staging dir
            (0, b"", b""),                # cp into container
            (0, b"", b""),                # chmod 0755
            (0, b"hi\n", b""),            # the actual run
            (0, b"", b""),                # rm cleanup
        ])
        def fake_run(argv, timeout):
            invocations.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"path": str(script), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "ok"
        assert result["exit_code"] == 0
        assert result["stdout"] == "hi\n"
        # Last call must be a cleanup rm -f on the staged path.
        last = invocations[-1]
        assert last[0] == "exec"
        assert "rm" in last and "-f" in last

    def test_refuses_unusual_basename(self, monkeypatch, tmp_path):
        # A space in the basename trips the regex.
        sneaky = tmp_path / "sneaky name.sh"
        sneaky.write_text("#!/bin/sh\n")
        monkeypatch.setattr(devbox, "_run_docker", _drain(_ownership_sequence()))
        args = type("A", (), {"path": str(sneaky), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "error"
        assert "basename" in result["error"]

    def test_cleanup_runs_on_timeout(self, monkeypatch, tmp_path):
        script = tmp_path / "y.py"
        script.write_text("import time; time.sleep(999)\n")
        invocations = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),                # mkdir -p staging dir
            (0, b"", b""),                # cp into container
            (0, b"", b""),                # chmod 0755
            None,                          # exec → TimeoutExpired
            (0, b"", b""),                # _kill_stragglers probe
            (0, b"", b""),                # rm cleanup
        ])
        def fake_run(argv, timeout):
            invocations.append(argv)
            val = next(seq)
            if val is None:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            return val
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"path": str(script), "interpreter": "python3", "timeout": 1})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "error"
        assert "timed out" in result["error"]
        # Cleanup rm must have run despite the timeout.
        rm_calls = [c for c in invocations if c[0] == "exec" and "rm" in c]
        assert rm_calls, "cleanup rm did not run after timeout"

    def test_rejects_host_path_outside_allowlist(self, monkeypatch):
        args = type("A", (), {"path": "/etc/passwd", "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]


    def test_stages_onto_the_persistent_volume_not_the_tmpfs(self, monkeypatch, tmp_path):
        """ISSUE-306: `docker cp` cannot traverse a tmpfs mount, so a script
        staged into /workspace lands in the shadowed rootfs directory and the
        interpreter reports "No such file or directory"."""
        script = tmp_path / "probe.py"
        script.write_text("print('hi')\n")
        invocations = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),                # mkdir -p staging dir
            (0, b"", b""),                # cp into container
            (0, b"", b""),                # chmod 0755
            (0, b"hi\n", b""),            # the actual run
            (0, b"", b""),                # rm cleanup
        ])
        def fake_run(argv, timeout):
            invocations.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"path": str(script), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "ok"

        cp_idx = next(i for i, c in enumerate(invocations) if c[0] == "cp")
        remote = invocations[cp_idx][2].split(":", 1)[1]
        assert remote.startswith(devbox._EXEC_STAGING_DIR + "/"), remote
        for mount in devbox._CONTAINER_TMPFS_MOUNTS:
            assert not remote.startswith(mount + "/"), f"staged into tmpfs {mount}"

        # `docker cp` creates no parent directory, so the mkdir has to precede it.
        mkdir_idx = next(i for i, c in enumerate(invocations) if "mkdir" in c)
        assert mkdir_idx < cp_idx
        assert devbox._EXEC_STAGING_DIR in invocations[mkdir_idx]

        # The interpreter and the cleanup must name the same staged path.
        run_idx = next(i for i, c in enumerate(invocations) if "python3" in c)
        assert invocations[run_idx][-1] == remote
        assert invocations[-1][-1] == remote

    @pytest.mark.parametrize("name", ["probe", "probe.py"])
    def test_fixes_the_staged_mode_on_both_branches(self, monkeypatch, tmp_path, name):
        """`docker cp` preserves the host file's uid/gid and mode, and the
        daemon user is not the container's `dev` — so a 0600 script arrives
        unreadable by the user about to run it. That bites the interpreter
        branch as hard as the fallback, and `chmod +x` would grant execute
        without read."""
        script = tmp_path / name
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o600)
        invocations = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),                # mkdir -p
            (0, b"", b""),                # cp
            (0, b"", b""),                # chmod
            (0, b"hi\n", b""),            # the run
            (0, b"", b""),                # rm cleanup
        ])
        def fake_run(argv, timeout):
            invocations.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"path": str(script), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "ok"
        chmod_call = [c for c in invocations if "chmod" in c][0]
        assert chmod_call[:3] == ["exec", "-u", "root"]
        assert "0755" in chmod_call

    def test_reports_a_failed_chmod_instead_of_an_opaque_eacces(self, monkeypatch, tmp_path):
        script = tmp_path / "probe"
        script.write_text("#!/bin/sh\n")
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),                          # mkdir -p
            (0, b"", b""),                          # cp
            (1, b"", b"chmod: Operation not permitted"),
            (0, b"", b""),                          # rm cleanup
        ]))
        args = type("A", (), {"path": str(script), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "error"
        assert "chmod" in result["error"]

    def test_removes_the_partial_copy_when_the_copy_fails(self, monkeypatch, tmp_path):
        """`docker cp` extracts a tar, so a failure part-way leaves a truncated
        file behind on the persistent volume."""
        script = tmp_path / "probe.py"
        script.write_text("print(1)\n")
        invocations = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),                        # mkdir -p
            (1, b"", b"no space left on device"), # cp
            (0, b"", b""),                        # rm cleanup
        ])
        def fake_run(argv, timeout):
            invocations.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"path": str(script), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "error"
        assert "cp into devbox failed" in result["error"]
        assert "rm" in invocations[-1] and "-f" in invocations[-1]

    def test_reports_a_failed_mkdir(self, monkeypatch, tmp_path):
        script = tmp_path / "probe.py"
        script.write_text("print(1)\n")
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (1, b"", b"mkdir: cannot create directory"),
        ]))
        args = type("A", (), {"path": str(script), "interpreter": None, "timeout": None})()
        result = devbox.cmd_exec_file(args)
        assert result["status"] == "error"
        assert "staging" in result["error"]


class TestCp:
    def test_cp_in_missing_source(self, tmp_path):
        args = type("A", (), {"src": str(tmp_path / "no-such"), "dest": "/home/dev/x"})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "error"
        assert "not found" in result["error"]

    def test_cp_in_success(self, monkeypatch, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("hello")
        calls = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
            (0, b"", b""),  # arrival check
        ])
        def fake_run(argv, timeout):
            calls.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"src": str(src), "dest": "/home/dev/a.txt"})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "ok"
        cp_call = [c for c in calls if c[0] == "cp"][0]
        assert cp_call[2] == "devbox-bob:/home/dev/a.txt"

    def test_cp_in_rejects_path_outside_allowlist(self):
        args = type("A", (), {"src": "/etc/passwd", "dest": "/home/dev/p"})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]

    def test_cp_out_creates_parent(self, monkeypatch, tmp_path):
        dest = tmp_path / "nested" / "out.json"
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
        ]))
        args = type("A", (), {"src": "/home/dev/out.json", "dest": str(dest)})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "ok"
        assert dest.parent.exists()

    def test_cp_out_rejects_path_outside_allowlist(self, tmp_path_factory):
        # Build a *separate* tmp dir outside ISTOTA_DEFERRED_DIR. Writable by
        # the test user, but not on the allowlist — the right rejection path.
        outside = tmp_path_factory.mktemp("outside")
        args = type("A", (), {"src": "/home/dev/x", "dest": str(outside / "payload")})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]

    def test_cp_out_propagates_docker_failure(self, monkeypatch, tmp_path):
        dest = tmp_path / "out.txt"
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (1, b"", b"Error: no such file"),
        ]))
        args = type("A", (), {"src": "/home/dev/missing", "dest": str(dest)})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "error"
        assert "no such file" in result["error"]


    @pytest.mark.parametrize("dest", [
        "/workspace",
        "/workspace/a.txt",
        "/workspace/nested/a.txt",
        "/workspace/",
        "//workspace/a.txt",
        "/workspace/./a.txt",
        "/home/dev/../../workspace/a.txt",
        # `docker cp` reads a container path as relative to `/`, not to the
        # image's WORKDIR, so these name the tmpfs just as squarely.
        "workspace/a.txt",
        "./workspace/a.txt",
        "home/dev/../../workspace/a.txt",
    ])
    def test_cp_in_refuses_a_tmpfs_destination(self, monkeypatch, tmp_path, dest):
        """ISSUE-306: `docker cp` writes into the rootfs directory shadowed by
        the tmpfs mount, so the file exists nowhere the container can see it —
        and the copy reports success."""
        src = tmp_path / "a.txt"
        src.write_text("hello")
        def refuse(argv, timeout):
            raise AssertionError(f"docker must not be called for a tmpfs dest: {argv}")
        monkeypatch.setattr(devbox, "_run_docker", refuse)
        args = type("A", (), {"src": str(src), "dest": dest})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "error"
        assert "tmpfs" in result["error"]
        assert "/home/dev" in result["error"]

    @pytest.mark.parametrize("src", [
        "/workspace/out.json", "/workspace", "workspace/out.json",
    ])
    def test_cp_out_refuses_a_tmpfs_source(self, monkeypatch, tmp_path, src):
        def refuse(argv, timeout):
            raise AssertionError(f"docker must not be called for a tmpfs src: {argv}")
        monkeypatch.setattr(devbox, "_run_docker", refuse)
        args = type("A", (), {"src": src, "dest": str(tmp_path / "out.json")})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "error"
        assert "tmpfs" in result["error"]

    @pytest.mark.parametrize("dest", [
        "/workspaces/a.txt",
        "/workspace-old/a.txt",
        "/home/dev/workspaces/a.txt",
    ])
    def test_cp_in_allows_a_path_that_merely_starts_with_the_mount_name(
        self, monkeypatch, tmp_path, dest,
    ):
        """The prefix match is anchored on a separator: `/workspaces` is a
        different directory from `/workspace`, and dropping the anchor would
        swallow it."""
        src = tmp_path / "a.txt"
        src.write_text("hello")
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
            (0, b"", b""),  # arrival check
        ]))
        args = type("A", (), {"src": str(src), "dest": dest})()
        assert devbox.cmd_cp_in(args)["status"] == "ok"

    def test_cp_in_checks_arrival_at_the_path_docker_cp_actually_wrote(
        self, monkeypatch, tmp_path,
    ):
        """The check runs through `docker exec`, whose cwd is the image WORKDIR,
        while `docker cp` resolves against `/`. A relative destination has to be
        anchored before it is read back or a copy that landed is called a
        failure."""
        src = tmp_path / "a.txt"
        src.write_text("hello")
        calls = []
        inner = _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
            (0, b"", b""),  # arrival check
        ])
        def fake_run(argv, timeout):
            calls.append(argv)
            return inner(argv, timeout)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"src": str(src), "dest": "sub/a.txt"})()
        assert devbox.cmd_cp_in(args)["status"] == "ok"
        # docker cp is handed the path as given; the readback is anchored.
        assert [c for c in calls if c[0] == "cp"][0][2] == "devbox-bob:sub/a.txt"
        assert "/sub/a.txt" in calls[-1]

    def test_cp_in_of_a_directory_checks_the_destination_itself(
        self, monkeypatch, tmp_path,
    ):
        """A directory copied onto a path that does not exist *becomes* that
        path, rather than a child of it, so the basename form would look for a
        file that was never going to be there."""
        src = tmp_path / "payload"
        src.mkdir()
        (src / "inner.txt").write_text("hello")
        calls = []
        inner = _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
            (0, b"", b""),  # arrival check
        ])
        def fake_run(argv, timeout):
            calls.append(argv)
            return inner(argv, timeout)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"src": str(src), "dest": "/home/dev/target"})()
        assert devbox.cmd_cp_in(args)["status"] == "ok"
        script = calls[-1][calls[-1].index("-c") + 1]
        assert script == 'test -e "$1"'

    def test_cp_in_errors_when_the_file_did_not_arrive(self, monkeypatch, tmp_path):
        """`docker cp` exiting 0 is not evidence the container can see the file.
        Silent loss on a copy that reported success is the worse half of
        ISSUE-306."""
        src = tmp_path / "a.txt"
        src.write_text("hello")
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp — exits 0
            (1, b"", b""),  # arrival check — the file is not there
        ]))
        args = type("A", (), {"src": str(src), "dest": "/home/dev/a.txt"})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "error"
        assert "/home/dev/a.txt" in result["error"]

    def test_cp_in_arrival_check_passes_the_dest_as_argv_not_as_script_text(
        self, monkeypatch, tmp_path,
    ):
        src = tmp_path / "a.txt"
        src.write_text("hello")
        calls = []
        inner = _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
            (0, b"", b""),  # arrival check
        ])
        def fake_run(argv, timeout):
            calls.append(argv)
            return inner(argv, timeout)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        dest = "/home/dev/$(touch /tmp/pwned).txt"
        args = type("A", (), {"src": str(src), "dest": dest})()
        assert devbox.cmd_cp_in(args)["status"] == "ok"
        check = calls[-1]
        assert check[0] == "exec"
        # The destination arrives as a positional argument, never spliced into
        # the shell script the check runs.
        assert dest in check
        script = check[check.index("-c") + 1]
        assert dest not in script


class TestStatus:
    def test_status_parses_inspect_output(self, monkeypatch):
        # status now also surfaces the owner label as the 6th field.
        seq = iter([
            (0, b"true|2026-05-13T10:00:00Z|istota-devbox:latest|deadbeef1234abcd|0|bob", b""),
            (0, b"42M\n", b""),
        ])
        monkeypatch.setattr(devbox, "_run_docker", lambda argv, timeout: next(seq))
        result = devbox.cmd_status(type("A", (), {})())
        assert result["status"] == "ok"
        assert result["running"] is True
        assert result["image"] == "istota-devbox:latest"
        assert result["id"] == "deadbeef1234"
        assert result["restart_count"] == 0
        assert result["home_size"] == "42M"
        assert result["owner"] == "bob"

    def test_status_propagates_inspect_error(self, monkeypatch):
        monkeypatch.setattr(devbox, "_run_docker", lambda argv, timeout: (1, b"", b"No such container"))
        result = devbox.cmd_status(type("A", (), {})())
        assert result["status"] == "error"
        assert "No such container" in result["error"]


class TestReset:
    def test_refuses_without_yes(self):
        args = type("A", (), {"yes": False})()
        result = devbox.cmd_reset(args)
        assert result["status"] == "error"
        assert "Refusing" in result["error"]

    def test_refuses_when_home_not_mountpoint(self, monkeypatch):
        # Ownership ok, but mountpoint -q returns 1 → refuse.
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (1, b"", b""),  # mountpoint -q /home/dev — not a mountpoint
        ]))
        args = type("A", (), {"yes": True})()
        result = devbox.cmd_reset(args)
        assert result["status"] == "error"
        assert "not a mountpoint" in result["error"]

    def test_runs_wipe_and_restart(self, monkeypatch):
        calls = []
        seq = iter([
            *_ownership_sequence(),
            (0, b"", b""),  # mountpoint -q → ok
            (0, b"", b""),  # find …rm -rf wipe
            (0, b"", b""),  # restart
        ])
        def fake_run(argv, timeout):
            calls.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"yes": True})()
        result = devbox.cmd_reset(args)
        assert result["status"] == "ok"
        # find / wipe runs as root
        wipe = [c for c in calls if c[0] == "exec" and "find" in " ".join(c)]
        assert wipe and "-u" in wipe[0] and "root" in wipe[0]
        # restart fires
        assert calls[-1] == ["restart", "devbox-bob"]


class TestMain:
    def test_main_prints_json_envelope(self, monkeypatch, capsys):
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"hi", b""),
        ]))
        devbox.main(["exec", "echo hi"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["stdout"] == "hi"

    def test_main_error_envelope_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.delenv("ISTOTA_DEVBOX_CONTAINER")
        monkeypatch.delenv("ISTOTA_USER_ID")
        with pytest.raises(SystemExit) as exc:
            devbox.main(["exec", "echo hi"])
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"


class TestExcludeSkills:
    """devbox is a plain menu skill — no selection-time exclusion.

    The old `exclude_skills: [devbox]` gate on the seven ingest skills (which
    kept the raw docker socket away from untrusted-content tasks) was removed
    once the Docker-API allowlist proxy made the socket safe to bind
    unconditionally. The boundary is now the proxy (exec/cp/inspect/restart on
    the user's own container only), not co-selection avoidance."""

    def test_devbox_not_always_include(self):
        from pathlib import Path
        from istota.skills._loader import load_skill_index
        idx = load_skill_index(Path("config/skills"))
        devbox_meta = idx.get("devbox")
        assert devbox_meta is not None
        assert devbox_meta.always_include is False

    @pytest.mark.parametrize("skill", ["email", "browse", "calendar", "transcribe", "whisper", "feeds", "bookmarks"])
    def test_ingest_skill_no_longer_excludes_devbox(self, skill):
        from pathlib import Path
        from istota.skills._loader import load_skill_index
        idx = load_skill_index(Path("config/skills"))
        meta = idx.get(skill)
        assert meta is not None
        assert "devbox" not in meta.exclude_skills, (
            f"{skill} must NOT exclude devbox — the proxy is the boundary now"
        )



# ---------------------------------------------------------------------------
# ISSUE-284: the shipped body and the CLI have to agree, and the executor must
# not export a name nothing reads.

_REPO = Path(__file__).resolve().parents[1]
_SKILL_DIR = _REPO / "src" / "istota" / "skills" / "devbox"
_EXECUTOR = _REPO / "src" / "istota" / "executor.py"

# Both forms a name can be read back in. A plain substring search over the CLI
# source would be satisfied by a mention in a docstring, and the module
# docstring already names env vars in prose.
_READ_FORM = re.compile(
    r"""(?:environ\.get|getenv)\(\s*['"](ISTOTA_DEVBOX_[A-Z_]+)['"]"""
    r"""|environ\[\s*['"](ISTOTA_DEVBOX_[A-Z_]+)['"]\s*\]"""
)


def _documented_argv() -> list[tuple[int, list[str]]]:
    """Every `istota-skill devbox …` line in the shipped body, as argv.

    The body is what the model reads and copies verbatim, so each line is
    parsed rather than restated here — a body edited back to a form the CLI
    refuses fails this instead of quietly passing against a copy.
    """
    out = []
    body = (_SKILL_DIR / "skill.md").read_text()
    for i, line in enumerate(body.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("istota-skill devbox "):
            continue
        tokens = shlex.split(stripped, comments=True)
        out.append((i, tokens[2:]))
    return out


def _subparser_for(verb: str) -> argparse.ArgumentParser:
    """The subparser `verb` dispatches to, so a test can read its flags."""
    for action in devbox.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[verb]
    raise AssertionError("devbox parser declares no subcommands")


class TestDocumentedCommandsMatchTheCLI:
    """ISSUE-284: `skill.md` listed `reset` with no `--yes`, which the CLI
    refuses. The model read the doc, ran the documented form, got an error and
    retried."""

    def test_the_scraper_finds_every_documented_line(self):
        """A parity test that silently matched nothing is the failure mode this
        class exists to prevent, so count the lines a second, independent way
        and require the two to agree."""
        body = (_SKILL_DIR / "skill.md").read_text()
        expected = sum(
            1 for line in body.splitlines()
            if line.strip().startswith("istota-skill devbox ")
        )
        assert expected >= 5, "skill.md documents almost nothing — body gutted?"
        assert len(_documented_argv()) == expected

    def test_every_documented_verb_parses(self):
        """Catches a documented verb the CLI does not have, and wrong
        positional arity. It does *not* catch ISSUE-284 itself — see
        `test_documented_forms_carry_their_confirmation_flags`."""
        parser = devbox.build_parser()
        for lineno, argv in _documented_argv():
            assert argv, f"skill.md:{lineno} names no verb"
            assert argv[0] in devbox._DISPATCH, (
                f"skill.md:{lineno} documents verb {argv[0]!r}, which the CLI "
                f"does not dispatch (has: {sorted(devbox._DISPATCH)})"
            )
            try:
                parser.parse_args(argv)
            except SystemExit as exc:  # argparse exits rather than raising
                raise AssertionError(
                    f"skill.md:{lineno} does not parse: istota-skill devbox "
                    f"{' '.join(argv)}"
                ) from exc

    def test_documented_forms_carry_their_confirmation_flags(self):
        """The reported bug class, for every verb rather than just `reset`.

        A confirmation flag is `store_true` and so optional as far as argparse
        is concerned: the documented form parses cleanly and is then refused at
        runtime. Parsing alone would not have caught ISSUE-284, and would not
        catch the same drift if `cp-out` grew a gate tomorrow.
        """
        checked = 0
        for lineno, argv in _documented_argv():
            sub = _subparser_for(argv[0])
            for action in sub._actions:
                if action.const is not True or not action.option_strings:
                    continue
                if "required" not in (action.help or "").lower():
                    continue
                checked += 1
                assert any(opt in argv for opt in action.option_strings), (
                    f"skill.md:{lineno} documents `istota-skill devbox "
                    f"{' '.join(argv)}`, but {argv[0]} refuses without "
                    f"{action.option_strings[0]} — the documented form cannot run"
                )
        assert checked, (
            "no documented verb has a confirmation flag; this test found "
            "nothing to check and would pass against anything"
        )

    def test_documented_reset_actually_runs(self, monkeypatch):
        """End to end through the real `cmd_reset`, with docker stubbed: the
        documented argv reaches the wipe rather than the refusal."""
        resets = [argv for _, argv in _documented_argv() if argv[0] == "reset"]
        assert resets, "skill.md no longer documents reset"
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),   # mountpoint -q /home/dev
            (0, b"", b""),   # find … -exec rm -rf
            (0, b"", b""),   # restart
        ]))
        args = devbox.build_parser().parse_args(resets[0])
        result = devbox.cmd_reset(args)
        assert result["status"] == "ok", (
            f"the documented reset form was refused: {result.get('error')}"
        )

    def test_reset_description_does_not_promise_image_recreation(self):
        """`reset` wipes /home/dev and restarts the container. It recreates
        nothing from the base image, and the old wording said it did."""
        body = (_SKILL_DIR / "skill.md").read_text()
        for line in body.splitlines():
            if "devbox reset" not in line:
                continue
            assert "base image" not in line, (
                f"reset does not recreate from the base image: {line.strip()!r}"
            )


class TestExecutorExportsNothingTheCLIIgnores:
    """ISSUE-284: `ISTOTA_DEVBOX_DOCKER_SOCKET` was written into the model's
    own environment and read by nothing. The name is the path of the *real*
    root-equivalent socket, and `build_bwrap_cmd` uses the same config field as
    the in-sandbox mount point for the allowlist proxy — one field, two
    meanings. A name in the model's environment invites a later reader to treat
    it as "the socket you may use"."""

    def _exported(self) -> set[str]:
        """Both routes a var can take into the task env: the imperative block
        in `execute_task`, and the manifest `env:` block — which is the
        sanctioned route per `.claude/rules/skills.md`, and so the likelier way
        this comes back."""
        from istota.skills._loader import load_skill_index
        imperative = set(re.findall(
            r"""env\[\s*['"](ISTOTA_DEVBOX_[A-Z_]+)['"]\s*\]\s*=""",
            _EXECUTOR.read_text(),
        ))
        meta = load_skill_index(Path("config/skills")).get("devbox")
        declared = {
            spec.var for spec in (getattr(meta, "env_specs", None) or [])
            if spec.var and spec.var.startswith("ISTOTA_DEVBOX_")
        }
        return imperative | declared

    def _read_by_cli(self) -> set[str]:
        source = (_SKILL_DIR / "__init__.py").read_text()
        return {name for pair in _READ_FORM.findall(source) for name in pair if name}

    def test_the_scans_find_something(self):
        assert self._exported(), "found no devbox env exports — regex stale?"
        assert self._read_by_cli(), "found no devbox env reads — regex stale?"

    def test_every_exported_devbox_var_has_a_reader(self):
        unread = sorted(self._exported() - self._read_by_cli())
        assert not unread, (
            f"{unread} reach the sandboxed task environment and the devbox "
            f"skill CLI reads none of these. Give each a reader, drop it, or "
            f"— if the reader legitimately lives elsewhere (docker/devbox/lib, "
            f"a setup_env hook, another skill CLI) — widen this search and say "
            f"where it went."
        )


class TestTmpfsMountList:
    """`_CONTAINER_TMPFS_MOUNTS` is a hand-maintained mirror of the `tmpfs:`
    keys in the devbox compose files. A mount added there and not here is a
    path `docker cp` will silently swallow again, so pin the two together."""

    REPO_ROOT = Path(__file__).resolve().parents[1]

    @staticmethod
    def _tmpfs_destinations(lines: list[str]) -> set[str]:
        found: set[str] = set()
        in_block = False
        for line in lines:
            stripped = line.strip()
            if stripped == "tmpfs:":
                in_block = True
                continue
            if in_block:
                # A comment or a blank line inside the list must not be read as
                # the end of it, or the scan truncates and pins nothing.
                if not stripped or stripped.startswith("#"):
                    continue
                if not stripped.startswith("- "):
                    in_block = False
                    continue
                found.add(stripped[2:].split(":", 1)[0].strip())
        return found

    def test_ansible_template_declares_nothing_unlisted(self):
        path = self.REPO_ROOT / "deploy/ansible/templates/docker-compose.devbox.yml.j2"
        declared = self._tmpfs_destinations(path.read_text().splitlines())
        assert declared == set(devbox._CONTAINER_TMPFS_MOUNTS)

    def test_compose_devbox_service_declares_nothing_unlisted(self):
        path = self.REPO_ROOT / "docker/docker-compose.yml"
        lines = path.read_text().splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "  devbox:")
        end = next(
            (i for i in range(start + 1, len(lines))
             if lines[i][:3].strip() and not lines[i].startswith("   ")),
            len(lines),
        )
        declared = self._tmpfs_destinations(lines[start:end])
        assert declared == set(devbox._CONTAINER_TMPFS_MOUNTS)

    def test_every_listed_mount_is_absolute_and_unslashed(self):
        for mount in devbox._CONTAINER_TMPFS_MOUNTS:
            assert mount.startswith("/")
            assert not mount.endswith("/")

    def test_the_staging_dir_is_not_inside_a_tmpfs(self):
        for mount in devbox._CONTAINER_TMPFS_MOUNTS:
            assert not devbox._EXEC_STAGING_DIR.startswith(mount + "/")
            assert devbox._EXEC_STAGING_DIR != mount
