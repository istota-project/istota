"""CRON `command:` jobs and HEARTBEAT `shell-command` checks run confined (ISSUE-886).

CRON.md and HEARTBEAT.md are written by the model, so a command in either has
the authority of whatever wrote it: an injected admin task, not the operator.
The default suite checks argv and env, and runs on shapes with no bwrap. The
`linux` tests at the end execute the namespace.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SecurityConfig
from istota.heartbeat import HeartbeatCheck, _check_shell_command
from istota.scheduler import _execute_command_task


def _config(db_path, tmp_path, **kwargs) -> Config:
    (tmp_path / "mount" / "Users" / "alice").mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=db_path,
        workspace_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
        **kwargs,
    )


def _task(command: str, **kwargs) -> db.Task:
    defaults = dict(
        id=7, status="running", source_type="scheduled", user_id="alice",
        prompt="", conversation_token="", command=command,
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


def _check(command: str) -> HeartbeatCheck:
    return HeartbeatCheck(
        name="probe", type="shell-command", config={"command": command},
    )


def _on_daemon_path(name):
    return f"/usr/bin/{name}"


def _capture_spawn():
    """What the runner hands the kernel, in place of running it."""
    seen: dict = {}

    def fake(cmd, **kwargs):
        seen["argv"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    return seen, fake


class TestWrappedInTheTaskSandbox:
    def _assert_confined(self, seen, config):
        argv = seen["argv"]
        assert Path(argv[0]).name == "bwrap", argv[:3]
        assert "--unshare-pid" in argv
        assert "--die-with-parent" in argv
        # The daemon's filesystem view is gone: the user's own home is bound,
        # and the mount as a whole is not.
        users = str(config.workspace_path.resolve() / "Users")
        bound = [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
        assert str(config.workspace_path.resolve()) not in bound
        assert f"{users}/alice" in bound
        tail = argv[argv.index("--") + 1:]
        assert any("pipefail" in part for part in tail), tail

    def test_a_command_job(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        seen, fake = _capture_spawn()
        with (
            patch("istota.executor._bwrap_available", return_value=True),
            patch("istota.sandbox.confined_command.on_daemon_path", _on_daemon_path),
            patch("istota.sandbox.confined_command.run_capture", side_effect=fake),
            patch("istota.scheduler.discover_calendars_for_task", return_value=[]),
        ):
            success, result = _execute_command_task(_task("cat /etc/hostname"), config)
        assert success, result
        self._assert_confined(seen, config)

    def test_a_heartbeat_shell_check(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        seen, fake = _capture_spawn()
        with (
            patch("istota.executor._bwrap_available", return_value=True),
            patch("istota.sandbox.confined_command.on_daemon_path", _on_daemon_path),
            patch("istota.sandbox.confined_command.run_capture", side_effect=fake),
        ):
            result = _check_shell_command(_check("cat /etc/hostname"), config, "alice")
        assert result.healthy, result.message
        self._assert_confined(seen, config)

    def test_sandbox_off_is_the_operator_s_choice(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path, security=SecurityConfig(sandbox_enabled=False),
        )
        seen, fake = _capture_spawn()
        with (
            patch("istota.executor._bwrap_available", return_value=True),
            patch("istota.sandbox.confined_command.on_daemon_path", _on_daemon_path),
            patch("istota.sandbox.confined_command.run_capture", side_effect=fake),
        ):
            _execute_command_task(_task("true"), config)
        assert Path(seen["argv"][0]).name != "bwrap"


class TestTheBinaryIsTheDaemons:
    def test_a_bwrap_planted_on_the_task_s_path_is_not_what_runs(self, db_path, tmp_path):
        """The env's PATH starts under the temp dir a model can write."""
        config = _config(db_path, tmp_path)
        seen, fake = _capture_spawn()
        with (
            patch("istota.executor._bwrap_available", return_value=True),
            patch("istota.sandbox.confined_command.on_daemon_path", _on_daemon_path),
            patch("istota.sandbox.confined_command.run_capture", side_effect=fake),
        ):
            _execute_command_task(_task("true"), config)
        assert seen["argv"][0] == "/usr/bin/bwrap"

    def test_a_missing_binary_fails_the_run(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        marker = tmp_path / "ran"
        with (
            patch("istota.executor._bwrap_available", return_value=True),
            patch("istota.sandbox.confined_command.on_daemon_path", return_value=None),
        ):
            success, result = _execute_command_task(_task(f"touch {marker}"), config)
        assert success is False
        assert "PATH" in result
        assert not marker.exists()


class TestTheEnvironment:
    """The split a task gets: a command sees neither a credential nor a DB path."""

    def test_a_command_job_gets_no_database_path(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        success, result = _execute_command_task(
            _task('echo "db=[$ISTOTA_DB_PATH]:user=[$ISTOTA_USER_ID]"'), config,
        )
        assert success, result
        assert result == "db=[]:user=[alice]"

    def test_a_heartbeat_check_gets_no_database_path(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        result = _check_shell_command(
            _check('echo "db=[$ISTOTA_DB_PATH]:user=[$ISTOTA_USER_ID]"'),
            config, "alice",
        )
        assert result.details["value"] == "db=[]:user=[alice]"

    def test_a_pipeline_in_a_nested_script_reports_its_failing_stage(
        self, db_path, tmp_path,
    ):
        """`SHELLOPTS` from `build_clean_env`; `-o pipefail` stops at one shell."""
        config = _config(db_path, tmp_path)
        success, result = _execute_command_task(
            _task("bash -c 'false | true; echo status=$?'"), config,
        )
        assert success, result
        assert result == "status=1"

    def test_doctor_reads_a_command_job_as_a_task(self, db_path, tmp_path):
        """`doctor._is_task_process` keys on this: `config/` is out of the
        namespace, so a doctor run in here answers about defaults."""
        config = _config(db_path, tmp_path)
        success, result = _execute_command_task(_task('echo "$ISTOTA_TASK_ID"'), config)
        assert success, result
        assert result == "7"

    def test_doctor_reads_a_heartbeat_check_as_a_task(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        result = _check_shell_command(_check('echo "$ISTOTA_TASK_ID"'), config, "alice")
        assert int(result.details["value"]) < 0

    def test_nothing_credential_shaped_reaches_a_command(
        self, db_path, tmp_path, monkeypatch,
    ):
        """The names the old stripped env's tests held, through the real seam.
        By value, so a renamed copy counts as a leak."""
        from istota.setup_wizard import SECRET_NAMES

        names = {
            "DB_PASSWORD", "IMAP_PASSWORD", "SMTP_PASSWORD", "GITLAB_TOKEN",
            "API_TOKEN", "MY_SECRET", "SERVICE_API_KEY", "NC_PASS",
            "ISTOTA_NEXTCLOUD_APP_PASSWORD", "SSH_PRIVATE_KEY",
            "ISTOTA_SECRET_KEY", "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS", "BASH_ENV",
            *(name.upper() for name in SECRET_NAMES),
        }
        for name in names:
            monkeypatch.setenv(name, f"planted-{name}")
        # An allowlist, so a name no pattern matches is gone too.
        monkeypatch.setenv("NOT_A_SECRET", "planted-plain")
        config = _config(db_path, tmp_path)

        success, job = _execute_command_task(_task("env"), config)
        check = _check_shell_command(_check("env"), config, "alice")

        assert success, job
        for dump in (job, check.details["value"]):
            assert "HOME=" in dump
            assert "planted-" not in dump

    def test_the_skill_proxy_socket_is_live(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        success, result = _execute_command_task(
            _task('test -S "$ISTOTA_SKILL_PROXY_SOCK" && echo live'), config,
        )
        assert success, result
        assert result == "live"

    def test_a_setup_env_value_goes_to_the_proxy_only(self, db_path, tmp_path):
        """`LOCATION_DB_PATH` is what a heartbeat's `istota-skill location
        current` needs. It belongs to the host-side CLI, not the shell."""
        config = _config(db_path, tmp_path)
        seen = {}
        from istota.sandbox import skill_proxy

        real_init = skill_proxy.SkillProxy.__init__

        def spy(self, sock, credential_env, base_env, **kwargs):
            seen["base_env"] = dict(base_env)
            real_init(self, sock, credential_env, base_env, **kwargs)

        with (
            patch.object(skill_proxy.SkillProxy, "__init__", spy),
            patch(
                "istota.skills._env.dispatch_setup_env_hooks",
                return_value={"LOCATION_DB_PATH": "/srv/data/alice/location.db"},
            ),
        ):
            result = _check_shell_command(
                _check('echo "loc=[$LOCATION_DB_PATH]"'), config, "alice",
            )
        assert result.details["value"] == "loc=[]"
        assert seen["base_env"]["LOCATION_DB_PATH"] == "/srv/data/alice/location.db"


class TestAHeartbeatCheckFailsClosed:
    def test_no_user_is_refused(self, db_path, tmp_path):
        marker = tmp_path / "ran"
        result = _check_shell_command(
            _check(f"touch {marker}"), _config(db_path, tmp_path),
        )
        assert result.healthy is False
        assert not marker.exists()

    def test_a_runtime_that_cannot_be_built_does_not_run_it_unconfined(
        self, db_path, tmp_path,
    ):
        config = _config(db_path, tmp_path)
        marker = tmp_path / "ran"
        with patch(
            "istota.sandbox.task_env.build_task_runtime",
            side_effect=RuntimeError("kaboom"),
        ):
            result = _check_shell_command(_check(f"touch {marker}"), config, "alice")
        assert result.healthy is False
        assert not marker.exists()


class TestAHeartbeatCheckWritesDirectly:
    def test_no_deferred_dir_reaches_the_shell_or_the_proxy(self, db_path, tmp_path):
        """Nothing drains a check's deferred ops, so a skill's write would answer
        ok and never land."""
        config = _config(db_path, tmp_path)
        seen = {}
        from istota.sandbox import skill_proxy

        real_init = skill_proxy.SkillProxy.__init__

        def spy(self, sock, credential_env, base_env, **kwargs):
            real_init(self, sock, credential_env, base_env, **kwargs)
            seen["proxy"] = self

        with patch.object(skill_proxy.SkillProxy, "__init__", spy):
            result = _check_shell_command(
                _check('echo "dir=[$ISTOTA_DEFERRED_DIR]"'), config, "alice",
            )
        assert result.details["value"] == "dir=[]"
        assert "ISTOTA_DEFERRED_DIR" not in seen["proxy"].base_env

    def test_a_command_job_still_defers(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        success, result = _execute_command_task(
            _task('echo "dir=[$ISTOTA_DEFERRED_DIR]"'), config,
        )
        assert success, result
        assert result != "dir=[]"


class TestTheReachOfAJob:
    def test_nothing_is_selected(self, db_path, tmp_path):
        """Selection decides the allowlist and the devbox bind. A file edit must
        not buy what a turn of the same user does not have."""
        config = _config(db_path, tmp_path)
        from istota.sandbox import task_env

        real = task_env.build_task_runtime
        seen = {}

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        with patch.object(task_env, "build_task_runtime", spy):
            _execute_command_task(_task("true"), config)
        assert seen["selected_skills"] == []

    def test_a_commit_from_a_job_requires_the_scans(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        success, result = _execute_command_task(
            _task("echo $PRECOMMIT_SCANS_REQUIRED"), config,
        )
        assert success, result
        assert result == "1"


class TestTheBridgeIsWaitedFor:
    def test_the_command_opens_with_the_wait_when_the_network_is_proxied(
        self, db_path, tmp_path,
    ):
        config = _config(db_path, tmp_path)
        config.security.network.enabled = True
        seen, fake = _capture_spawn()
        with (
            patch("istota.executor._bwrap_available", return_value=True),
            patch("istota.sandbox.confined_command.on_daemon_path", _on_daemon_path),
            patch("istota.sandbox.confined_command.run_capture", side_effect=fake),
        ):
            _execute_command_task(_task("curl https://example.com/x"), config)
        script = seen["argv"][-1]
        assert script.index("/dev/tcp/127.0.0.1/") < script.index("curl")


class TestTheCgroup:
    def test_a_proxy_that_cannot_start_does_not_leak_the_group(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        group = tmp_path / "task-7-0"
        group.mkdir()
        from istota.sandbox import skill_proxy

        with (
            patch("istota.sandbox.cgroup.create", return_value=group),
            patch("istota.executor._release_task_cgroup") as release,
            patch.object(skill_proxy.SkillProxy, "__enter__", side_effect=OSError("no")),
        ):
            success, _ = _execute_command_task(_task("true"), config)
        assert success is False
        release.assert_called_once_with(7, group)

    def test_the_child_places_itself_before_it_execs(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        group = tmp_path / "task-7-0"
        group.mkdir()
        (group / "cgroup.procs").write_text("")
        with (
            patch("istota.sandbox.cgroup.create", return_value=group),
            patch("istota.executor._release_task_cgroup") as release,
            patch("istota.sandbox.cgroup.place"),
        ):
            success, result = _execute_command_task(_task("echo hi"), config)
        assert success, result
        assert (group / "cgroup.procs").read_text() == "0\n"
        release.assert_called_once_with(7, group)


class TestTheWorkerPid:
    def test_a_command_job_records_and_clears_its_pid(self, db_path, tmp_path):
        """So `!stop` reaches it: the watch signals the row's `worker_pid`."""
        config = _config(db_path, tmp_path)
        recorded = []
        with patch(
            "istota.db.update_task_pid",
            side_effect=lambda conn, task_id, pid: recorded.append((task_id, pid)),
        ):
            success, result = _execute_command_task(_task("echo hi"), config)
        assert success, result
        assert recorded[0][0] == 7 and recorded[0][1]
        assert recorded[-1] == (7, None)


@pytest.mark.linux
@pytest.mark.skipif(sys.platform != "linux", reason="needs a real namespace")
class TestTheNamespace:
    """The closure test: editing the file buys no host access."""

    def _sandboxed(self, db_path, tmp_path):
        if shutil.which("bwrap") is None:
            pytest.skip("bwrap not installed")
        return _config(db_path, tmp_path)

    def test_a_command_job_cannot_read_a_host_file(self, db_path, tmp_path):
        config = self._sandboxed(db_path, tmp_path)
        canary = tmp_path / "host-secret"
        canary.write_text("canary-value")
        success, result = _execute_command_task(
            _task(f"cat {canary} || echo refused"), config,
        )
        assert success, result
        assert "canary-value" not in result
        assert "refused" in result

    def test_a_command_job_cannot_read_another_user_s_home(self, db_path, tmp_path):
        config = self._sandboxed(db_path, tmp_path)
        other = config.workspace_path / "Users" / "bob"
        other.mkdir(parents=True)
        (other / "USER.md").write_text("bob-private")
        success, result = _execute_command_task(
            _task(f"cat {other}/USER.md || echo refused"), config,
        )
        assert success, result
        assert "bob-private" not in result

    def test_a_command_job_cannot_see_the_database(self, db_path, tmp_path):
        config = self._sandboxed(db_path, tmp_path)
        success, result = _execute_command_task(
            _task(f"test -e {db_path} && echo there || echo gone"), config,
        )
        assert success, result
        assert result == "gone"

    def test_a_command_job_reaches_its_own_home(self, db_path, tmp_path):
        config = self._sandboxed(db_path, tmp_path)
        home = config.workspace_path / "Users" / "alice"
        (home / "note.md").write_text("mine")
        success, result = _execute_command_task(_task(f"cat {home}/note.md"), config)
        assert success, result
        assert result == "mine"

    def test_the_skill_proxy_socket_is_in_the_namespace(self, db_path, tmp_path):
        config = self._sandboxed(db_path, tmp_path)
        success, result = _execute_command_task(
            _task('test -S "$ISTOTA_SKILL_PROXY_SOCK" && echo bound'), config,
        )
        assert success, result
        assert result == "bound"

    def test_the_bridge_listens_before_the_command_starts(self, db_path, tmp_path):
        from istota.sandbox.network_proxy import BRIDGE_PORT

        config = self._sandboxed(db_path, tmp_path)
        config.security.network.enabled = True
        success, result = _execute_command_task(
            _task(f"(exec 3<>/dev/tcp/127.0.0.1/{BRIDGE_PORT}) && echo up"), config,
        )
        assert success, result
        assert result == "up"

    def test_a_heartbeat_check_cannot_read_a_host_file(self, db_path, tmp_path):
        config = self._sandboxed(db_path, tmp_path)
        canary = tmp_path / "host-secret"
        canary.write_text("canary-value")
        result = _check_shell_command(
            _check(f"cat {canary} || echo refused"), config, "alice",
        )
        assert "canary-value" not in result.details["value"]
