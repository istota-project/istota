"""Tests for the devbox skill CLI."""

import json
import subprocess
from pathlib import Path

import pytest

from istota.skills import devbox


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ISTOTA_USER_ID", "stefan")
    monkeypatch.setenv("ISTOTA_DEVBOX_CONTAINER", "devbox-stefan")
    monkeypatch.setenv("ISTOTA_DEVBOX_DOCKER_CLI", "/usr/bin/docker")
    monkeypatch.delenv("ISTOTA_DEVBOX_EXEC_TIMEOUT", raising=False)
    monkeypatch.delenv("ISTOTA_DEVBOX_MAX_OUTPUT_BYTES", raising=False)
    # cp-in / cp-out require an allowlist; point at tmp_path so tests
    # can build host paths inside it.
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)


def _ownership_sequence(*, owner: str = "stefan", running: bool = True) -> list[tuple[int, bytes, bytes]]:
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
        assert devbox._container_name() == "devbox-stefan"

    def test_falls_back_to_user_id(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_DEVBOX_CONTAINER")
        assert devbox._container_name() == "devbox-stefan"

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_DEVBOX_CONTAINER")
        monkeypatch.delenv("ISTOTA_USER_ID")
        assert devbox._container_name() is None

    @pytest.mark.parametrize("bad", [
        "devbox stefan",          # space
        "devbox-stefan;rm -rf /", # shell metachars
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
        assert devbox._validate_command("dig MX cynium.com") is None

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
        candidate = nc / "Users" / "stefan" / "f.txt"
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
        assert "devbox-stefan" in exec_argv
        assert exec_argv[-3] == "bash"
        assert exec_argv[-2] == "-c"
        assert exec_argv[-1] == "echo hi"

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
            (0, b"", b""),                # cp into container
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
            (0, b"", b""),                # cp into container
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


class TestCp:
    def test_cp_in_missing_source(self, tmp_path):
        args = type("A", (), {"src": str(tmp_path / "no-such"), "dest": "/workspace/x"})()
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
        ])
        def fake_run(argv, timeout):
            calls.append(argv)
            return next(seq)
        monkeypatch.setattr(devbox, "_run_docker", fake_run)
        args = type("A", (), {"src": str(src), "dest": "/workspace/a.txt"})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "ok"
        assert calls[-1][0] == "cp"
        assert calls[-1][2] == "devbox-stefan:/workspace/a.txt"

    def test_cp_in_rejects_path_outside_allowlist(self):
        args = type("A", (), {"src": "/etc/passwd", "dest": "/workspace/p"})()
        result = devbox.cmd_cp_in(args)
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]

    def test_cp_out_creates_parent(self, monkeypatch, tmp_path):
        dest = tmp_path / "nested" / "out.json"
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (0, b"", b""),  # docker cp
        ]))
        args = type("A", (), {"src": "/workspace/out.json", "dest": str(dest)})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "ok"
        assert dest.parent.exists()

    def test_cp_out_rejects_path_outside_allowlist(self, tmp_path_factory):
        # Build a *separate* tmp dir outside ISTOTA_DEFERRED_DIR. Writable by
        # the test user, but not on the allowlist — the right rejection path.
        outside = tmp_path_factory.mktemp("outside")
        args = type("A", (), {"src": "/workspace/x", "dest": str(outside / "payload")})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "error"
        assert "outside allowed roots" in result["error"]

    def test_cp_out_propagates_docker_failure(self, monkeypatch, tmp_path):
        dest = tmp_path / "out.txt"
        monkeypatch.setattr(devbox, "_run_docker", _drain([
            *_ownership_sequence(),
            (1, b"", b"Error: no such file"),
        ]))
        args = type("A", (), {"src": "/workspace/missing", "dest": str(dest)})()
        result = devbox.cmd_cp_out(args)
        assert result["status"] == "error"
        assert "no such file" in result["error"]


class TestStatus:
    def test_status_parses_inspect_output(self, monkeypatch):
        # status now also surfaces the owner label as the 6th field.
        seq = iter([
            (0, b"true|2026-05-13T10:00:00Z|istota-devbox:latest|deadbeef1234abcd|0|stefan", b""),
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
        assert result["owner"] == "stefan"

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
        assert calls[-1] == ["restart", "devbox-stefan"]


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
