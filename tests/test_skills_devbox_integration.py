"""Devbox skill against a real container — the tier that would have caught
ISSUE-306 on day one.

Every other devbox test monkeypatches ``_run_docker`` wholesale, so the boundary
where the bug lived (``docker cp`` resolving a container path against the
rootfs rather than through the container's mount namespace) never executes. A
canned ``(0, b"", b"")`` for the copy is exactly the answer a real broken run
cannot produce, which is why three months of green suites said nothing.

Prerequisites: a `docker` CLI, and a running devbox container owned by the
current ``ISTOTA_USER_ID``. Every test skips without one, so the file is inert
on a laptop and meaningful on the deployment:

    ISTOTA_USER_ID=<user> uv run pytest -m integration tests/test_skills_devbox_integration.py -n0
"""

import os
import shutil
import subprocess
import uuid

import pytest

from istota.skills import devbox

pytestmark = pytest.mark.integration


def _running_container() -> str | None:
    """The devbox this process is allowed to talk to, or None."""
    if not shutil.which(os.environ.get("ISTOTA_DEVBOX_DOCKER_CLI") or "docker"):
        return None
    container = devbox._container_name()
    if not container:
        return None
    if devbox._check_owned(container) is not None:
        return None
    return container


@pytest.fixture(scope="module")
def container() -> str:
    name = _running_container()
    if not name:
        pytest.skip("no running devbox container for this user")
    return name


@pytest.fixture
def allowlisted_dir(tmp_path, monkeypatch):
    """cp-in / cp-out refuse a host path outside the skill's allowed roots."""
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def remote_scratch(container):
    """A per-run directory on the persistent volume, removed afterwards."""
    path = f"/home/dev/.istota-it-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [devbox._docker_cli(), "exec", "-u", "dev", container, "mkdir", "-p", path],
        check=True, capture_output=True, timeout=30,
    )
    yield path
    subprocess.run(
        [devbox._docker_cli(), "exec", "-u", "dev", container, "rm", "-rf", path],
        check=False, capture_output=True, timeout=30,
    )


def _args(**kw):
    return type("A", (), kw)()


def _exec(command: str) -> dict:
    return devbox.cmd_exec(_args(command=command, timeout=60))


class TestCopyRoundTrip:
    def test_a_file_copied_in_is_visible_inside_the_container(
        self, allowlisted_dir, remote_scratch,
    ):
        src = allowlisted_dir / "probe.txt"
        src.write_text("round-trip marker\n")
        dest = f"{remote_scratch}/probe.txt"

        result = devbox.cmd_cp_in(_args(src=str(src), dest=dest))
        assert result["status"] == "ok", result

        # The whole point: ask the container, not the daemon.
        seen = _exec(f"cat {dest}")
        assert seen["exit_code"] == 0, seen
        assert seen["stdout"] == "round-trip marker\n"

    def test_a_file_written_inside_the_container_copies_out(
        self, allowlisted_dir, remote_scratch,
    ):
        remote = f"{remote_scratch}/out.txt"
        written = _exec(f"printf 'from inside\\n' > {remote}")
        assert written["exit_code"] == 0, written

        dest = allowlisted_dir / "out.txt"
        result = devbox.cmd_cp_out(_args(src=remote, dest=str(dest)))
        assert result["status"] == "ok", result
        assert dest.read_text() == "from inside\n"

    def test_cp_in_reports_an_error_rather_than_losing_the_file(self, container, allowlisted_dir):
        """The `/workspace` tmpfs used to take a copy, report ok, and drop it."""
        src = allowlisted_dir / "probe.txt"
        src.write_text("hello\n")
        result = devbox.cmd_cp_in(_args(src=str(src), dest="/workspace/probe.txt"))
        assert result["status"] == "error"
        assert "tmpfs" in result["error"]
        # And nothing arrived under that name either way.
        assert _exec("test -e /workspace/probe.txt")["exit_code"] != 0


class TestExecPipelineStatus:
    """ISSUE-307, in the shell that actually runs the command.

    The unit tier fakes `_run_docker`, so the shell whose options are the whole
    question never exists there; it compensates by running the argv locally,
    which proves bash honours the flag but not that this image's `bash` is the
    one being reached. These two are the end of that chain.
    """

    def test_a_failing_pipeline_is_not_reported_as_success(self, container):
        result = _exec("false | tail -1")
        assert result["status"] == "ok", result
        assert result["exit_code"] != 0, (
            "the container's shell reported success for a failing pipeline — "
            "`exit_code` in this envelope does not mean what it says"
        )

    def test_a_succeeding_pipeline_is_still_success(self, container):
        """Control: the option must not turn every pipeline red."""
        result = _exec("echo hi | tail -1")
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "hi\n"

    def test_the_option_is_on_inside_the_container(self, container):
        result = _exec("set -o | grep pipefail")
        assert result["exit_code"] == 0, result
        assert result["stdout"].split() == ["pipefail", "on"], result["stdout"]


class TestExecFile:
    @pytest.mark.parametrize("name,body,expected", [
        ("probe.sh", "#!/bin/bash\necho shell-ok\n", "shell-ok\n"),
        ("probe.py", "print('python-ok')\n", "python-ok\n"),
    ])
    def test_runs_a_script_through_its_interpreter(
        self, container, allowlisted_dir, name, body, expected,
    ):
        script = allowlisted_dir / name
        script.write_text(body)
        result = devbox.cmd_exec_file(_args(path=str(script), interpreter=None, timeout=60))
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
        assert result["stdout"] == expected

    def test_runs_a_script_with_no_extension_via_its_shebang(
        self, container, allowlisted_dir,
    ):
        """The staging dir used to be a `noexec` tmpfs, so this path could not
        work even once the file was reachable."""
        script = allowlisted_dir / "probe"
        script.write_text("#!/bin/bash\necho shebang-ok\n")
        result = devbox.cmd_exec_file(_args(path=str(script), interpreter=None, timeout=60))
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "shebang-ok\n"

    def test_leaves_no_staged_copy_behind(self, container, allowlisted_dir):
        """The cleanup `rm -f` used to run against a path the file was never
        written to, so it removed nothing and exited 0.

        Scoped to this process's own staged name: the staging directory is
        shared by every caller and keyed on pid, so asserting it is globally
        empty would fail on any concurrent `exec-file` — another test, or a
        real task on the deployment.
        """
        script = allowlisted_dir / "probe.sh"
        script.write_text("#!/bin/bash\necho ok\n")
        assert devbox.cmd_exec_file(
            _args(path=str(script), interpreter=None, timeout=60),
        )["exit_code"] == 0

        staged = f"{devbox._EXEC_STAGING_DIR}/exec_{os.getpid()}_probe.sh"
        assert _exec(f"test -e {staged}")["exit_code"] != 0, staged
