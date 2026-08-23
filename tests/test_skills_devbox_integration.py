"""Devbox skill against a real container — the tier that would have caught
ISSUE-306 on day one.

Every other devbox test monkeypatches ``_run_docker`` wholesale, so the boundary
where the bug lived (``docker cp`` resolving a container path against the
rootfs rather than through the container's mount namespace) never executes. A
canned ``(0, b"", b"")`` for the copy is exactly the answer a real broken run
cannot produce, which is why three months of green suites said nothing.

Prerequisites: a `docker` CLI, a running devbox container owned by the current
``ISTOTA_USER_ID``, **and a docker socket that will run a raw `docker exec`**.
Every test skips without all three, so the file is inert on a laptop and
meaningful on the deployment:

    ISTOTA_USER_ID=<user> uv run pytest -m integration tests/test_skills_devbox_integration.py -n0

**Run it from a shell on the host, outside the sandbox.** The socket a
sandboxed task sees is `istota-docker-proxy`, which tracks the exec ids it
issued and refuses any other — so a raw `docker exec` from a task is denied at
the exec-inspect step, after the command has already run. The CLI then exits 1
with the command's own status never fetched, and every assertion here that
expects a non-zero exit is satisfied by the refusal rather than by the
container (ISSUE-313). That is the proxy working as designed; the tracking is
what makes the socket safe to expose at all. The `container` fixture probes for
it and skips the file, and `_exec` / `_exec_file` raise rather than hand a
refusal to an assertion.
"""

import os
import shutil
import subprocess
import uuid

import pytest

from istota.docker_proxy import PROXY_ERROR_PREFIX
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


def _proxy_refusal(*streams: str) -> str | None:
    """The docker-proxy refusal carried by any of ``streams``, else None.

    Matched on the prefix the proxy itself writes (`docker_proxy`'s
    ``PROXY_ERROR_PREFIX``) rather than on a copy of the string, so the two
    cannot drift. The reason word is deliberately not part of the match: a
    refusal is a refusal whatever the allowlist called it.

    Returns the matching *line*, not the whole stream — a refused `docker
    exec` has already run the command, so the stream can carry the command's
    own output as well, and the point of the return value is to name what
    happened rather than to reprint it.
    """
    for stream in streams:
        if not stream or PROXY_ERROR_PREFIX not in stream:
            continue
        for line in stream.splitlines():
            if PROXY_ERROR_PREFIX in line:
                return line.strip()
    return None


def _exec_refusal(container: str) -> str | None:
    """Refusal text if this process's `docker exec` is denied, else None.

    Returns None for any *other* failure. An unreachable or broken container
    is not a reason to invent a skip — those already fail loudly, and turning
    them quiet is the shape of defect this probe exists to remove. Which is
    also why `subprocess.TimeoutExpired` is deliberately **not** caught: a
    probe that never finished is not evidence that exec is permitted, and
    letting it out errors the module rather than reading silence as a yes.
    """
    try:
        proc = subprocess.run(
            [devbox._docker_cli(), "exec", "-u", "dev", container, "true"],
            capture_output=True, timeout=30, check=False,
        )
    except OSError:
        return None
    if proc.returncode == 0:
        return None
    return _proxy_refusal(proc.stderr.decode("utf-8", "replace"))


def _skip_if_exec_is_refused(name: str) -> None:
    """Skip the file when the proxy will refuse its `docker exec` calls.

    A separate function rather than four lines inside the fixture so the
    branch can be exercised without a container: it is the one that decides
    between a skip and ten results, and a gate nothing can call is a gate
    nothing has checked. See
    `tests/test_skills_devbox.py::TestTheIntegrationTierCannotReadARefusalAsAnAnswer`.
    """
    refusal = _exec_refusal(name)
    if refusal:
        pytest.skip(
            f"docker exec is refused for this process: {refusal}. "
            "istota-docker-proxy tracks only the exec ids it issued, so a raw "
            "`docker exec` from a sandboxed task is denied at the exec-inspect "
            "step and its exit status says nothing about the command. Run this "
            "file from a shell on the host."
        )


@pytest.fixture(scope="module", autouse=True)
def container() -> str:
    """The devbox under test, and the reachability gate for the whole file.

    Autouse on purpose: a guard a new test can forget to request is the same
    hole ISSUE-313 was filed for. Every test here goes through the probe
    whether or not it names the fixture.
    """
    name = _running_container()
    if not name:
        pytest.skip("no running devbox container for this user")
    _skip_if_exec_is_refused(name)
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
    made = subprocess.run(
        [devbox._docker_cli(), "exec", "-u", "dev", container, "mkdir", "-p", path],
        check=False, capture_output=True, timeout=30,
    )
    if made.returncode != 0:
        # `check=True` would raise a CalledProcessError whose str() is the
        # return code and the argv, with the daemon's own explanation only
        # reachable from the exception object. Reporting, not a guard, and
        # untested by construction — it needs a container, and factoring two
        # lines out to unit-test a failure message would buy nothing.
        pytest.fail(
            f"could not create {path} in {container}: "
            f"{made.stderr.decode('utf-8', 'replace').strip()}"
        )
    yield path
    subprocess.run(
        [devbox._docker_cli(), "exec", "-u", "dev", container, "rm", "-rf", path],
        check=False, capture_output=True, timeout=30,
    )


def _args(**kw):
    return type("A", (), kw)()


def _refuse_a_refusal(result: dict, what: str) -> None:
    """Raise rather than let a proxy refusal be read as a command's result.

    `cmd_exec` and `cmd_exec_file` report the *docker CLI's* exit status as
    ``exit_code``, inside an envelope whose ``status: "ok"`` means only that
    the CLI ran. A refused exec exits 1 having never fetched the command's own
    status, so the envelope says `ok` / `1` for a container that may have done
    anything at all — which is what every negative assertion in this file was
    reading (ISSUE-313). The `container` fixture already skips the file when
    exec is refused up front; this catches a refusal that starts mid-run, and
    keeps the guarantee at the call site rather than in a fixture's memory.

    Both fields, because a refusal reaches the envelope by two routes: `exec`
    and `exec-file` put the CLI's stderr in ``stderr`` and report its status
    as ``exit_code``, while the copy verbs and `exec-file`'s staging legs fold
    it into ``error``.

    One hole, stated rather than papered over: ``stderr`` is capped by
    ``_max_output_bytes`` (100 KB by default) and `_truncate` keeps the head,
    while the CLI writes its refusal *after* the command's own output. A
    command emitting more than the cap on stderr therefore hands this function
    a string with the refusal cut off. Nothing here produces that volume, and
    the up-front probe reads the raw stream, so the primary gate is unaffected.
    """
    refusal = _proxy_refusal(result.get("stderr") or "", result.get("error") or "")
    if refusal:
        raise AssertionError(
            f"{what} was refused, not answered: {refusal}. `exit_code` here is "
            "the docker CLI's, not the command's — this result says nothing "
            "about the container."
        )


def _exec(command: str) -> dict:
    result = devbox.cmd_exec(_args(command=command, timeout=60))
    _refuse_a_refusal(result, "`docker exec`")
    return result


def _exec_file(path, interpreter: str | None = None, timeout: int = 60) -> dict:
    result = devbox.cmd_exec_file(
        _args(path=str(path), interpreter=interpreter, timeout=timeout),
    )
    _refuse_a_refusal(result, "`exec-file`")
    return result


def _cp_in(src, dest: str) -> dict:
    """`cp-in`, guarded. Its arrival check is a `docker exec`, so a refusal
    reaches it as a failure to read the destination back — which without the
    guard reads as the ISSUE-306 symptom the check exists to detect."""
    result = devbox.cmd_cp_in(_args(src=str(src), dest=dest))
    _refuse_a_refusal(result, "`cp-in`")
    return result


def _cp_out(src: str, dest) -> dict:
    result = devbox.cmd_cp_out(_args(src=src, dest=str(dest)))
    _refuse_a_refusal(result, "`cp-out`")
    return result


class TestCopyRoundTrip:
    def test_a_file_copied_in_is_visible_inside_the_container(
        self, allowlisted_dir, remote_scratch,
    ):
        src = allowlisted_dir / "probe.txt"
        src.write_text("round-trip marker\n")
        dest = f"{remote_scratch}/probe.txt"

        result = _cp_in(src, dest)
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
        result = _cp_out(remote, dest)
        assert result["status"] == "ok", result
        assert dest.read_text() == "from inside\n"

    def test_cp_in_reports_an_error_rather_than_losing_the_file(self, container, allowlisted_dir):
        """The `/workspace` tmpfs used to take a copy, report ok, and drop it."""
        src = allowlisted_dir / "probe.txt"
        src.write_text("hello\n")
        result = _cp_in(src, "/workspace/probe.txt")
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
        result = _exec_file(script)
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
        result = _exec_file(script)
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
        assert _exec_file(script)["exit_code"] == 0

        staged = f"{devbox._EXEC_STAGING_DIR}/exec_{os.getpid()}_probe.sh"
        assert _exec(f"test -e {staged}")["exit_code"] != 0, staged
