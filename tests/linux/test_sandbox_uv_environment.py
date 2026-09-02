"""What a Python build environment sees inside the sandbox, on a real kernel.

Two properties the `developer-work-in-the-devbox` design rests on, both
measured by *executing* the real bwrap argv rather than by reading it:

1. **`$HOME/.cache` inside the sandbox is on bwrap's own root tmpfs.** The
   builder is a whitelist — `--ro-bind /usr`, a named list of `/etc` files,
   specific home subdirectories — and the only `~/.cache` entry is huggingface.
   `HOME` is set as an environment variable to a directory that exists inside
   the namespace only as the mount points bwrap created for those binds, and
   those live on the root tmpfs. So a `uv sync` in a sandboxed developer task
   downloads and unpacks into RAM. The tmpfs is in the task's own mount
   namespace, so `host_pressure.read_tmpfs_usage` — which reads
   `/proc/self/mounts` — cannot see it, and every byte lands in
   `shmem_unaccounted`.

   This was reasoned from the bind list before it was ever observed. These
   tests are the observation, and they are what a fix has to change: when a
   disk-backed cache directory is bound and `UV_CACHE_DIR` points at it, the
   *cache* moves off the tmpfs while `$HOME/.cache` itself stays where it is.

2. **A virtualenv is pinned to the absolute path it was built at.** Console
   scripts bake that path into their shebang, so the same bytes seen at a
   second path are not the same environment — `bin/python` survives and
   `bin/pip` does not. That is why any second mount namespace sharing the
   repos directory (the devbox container) has to bind it at the *same*
   absolute path the daemon uses, and it is why the sandbox already does.

Run them with `scripts/test-linux.sh`. They carry the `linux` marker, which
pyproject's addopts deselects, so `uv run pytest` on a host without Docker or
bubblewrap is unaffected.

Neither test needs the network: the venv is built with the stdlib, from an
interpreter that is already on disk.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import DeveloperConfig, SecurityConfig
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd

pytestmark = pytest.mark.linux


def _unavailable(reason):
    """Skip — unless we are inside the runner, where a skip is the bug.

    Same rule as `test_sandbox_real.py`: `scripts/test-linux.sh` sets
    ISTOTA_LINUX_TIER=1 and checks bwrap with its own probes before starting
    pytest, so a skip in there means the driver exited 0 having executed
    nothing. Outside the runner a skip is the right answer.
    """
    if os.environ.get("ISTOTA_LINUX_TIER") == "1":
        pytest.fail(f"running under scripts/test-linux.sh, where this must not skip: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def _q(path):
    return shlex.quote(str(path))


@pytest.fixture
def repos_dir(tmp_path):
    """`developer.repos_dir` — the *root* of the per-user subtrees.

    Nothing binds this. It is the configured value only; the bind is the
    per-user subtree below.
    """
    d = tmp_path / "repos"
    d.mkdir()
    return d


@pytest.fixture
def user_repos(repos_dir):
    """`{repos_dir}/alice` — the directory actually bound RW for an admin task.

    Since the per-user split, `build_bwrap_cmd` binds `get_user_repos_dir`'s
    answer and never `developer.repos_dir` itself, so anything a test wants to
    see from inside the sandbox has to live here. Three tests in this file were
    still writing into the root and asserting they could read it back, which
    the split turned from a real assertion into a permanent failure.
    """
    d = repos_dir / "alice"
    d.mkdir()
    return d


@pytest.fixture
def layout(tmp_path, repos_dir, make_config):
    data = tmp_path / "data"
    data.mkdir()
    (data / "istota.db").write_text("framework-db-contents")
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    return make_config(
        db_path=data / "istota.db",
        module_data_dir=data / "modules",
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True),
        developer=DeveloperConfig(enabled=True, repos_dir=str(repos_dir)),
    )


@pytest.fixture
def user_temp(layout):
    d = layout.temp_dir / "alice"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


def run_probe(script, config, task, user_temp, *, is_admin=True, **kwargs):
    """Run `sh -c script` inside the real sandbox and return the result.

    `is_admin` defaults True here, unlike the masks file: the developer repos
    bind is admin-gated, and every test below is about what happens inside
    that bind.

    Fails rather than returns when bwrap declined to build a command —
    `build_bwrap_cmd` passes *cmd through unchanged* when the sandbox is
    unavailable, so an unsandboxed probe would report the host's filesystems
    and quietly pass the assertions that matter least.
    """
    cmd = build_bwrap_cmd(
        ["/bin/sh", "-c", script], config, task, is_admin, [], user_temp,
        profile=SandboxProfile.CLAUDE, **kwargs,
    )
    assert cmd[0] == "bwrap", "sandbox unavailable — probe would have run unsandboxed"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _base_interpreter():
    """A python that exists under `/usr`, which the sandbox `--ro-bind`s.

    `sys.executable` is the wrong answer inside the test runner: pytest runs
    from `/venv`, which is bound nowhere, so a venv built from it would have a
    `bin/python` symlink that dangles in the sandbox and the positive test
    would fail for a reason it is not about.
    """
    candidate = Path(getattr(sys, "_base_executable", "") or sys.executable).resolve()
    if candidate.is_file() and str(candidate).startswith("/usr/"):
        return candidate
    for fallback in ("/usr/bin/python3", "/usr/local/bin/python3"):
        p = Path(fallback)
        if p.is_file():
            return p.resolve()
    _unavailable("no interpreter under /usr to build a venv from")


class TestTheCacheIsRamBacked:
    """Stage 1's hypothesis, executed rather than reasoned."""

    def test_home_cache_is_on_the_bwrap_root_tmpfs(self, layout, task, user_temp):
        """`$HOME/.cache` is tmpfs, and it is the *root's* tmpfs.

        The filesystem type alone would not settle it — `--tmpfs /tmp` means a
        sandbox has more than one — so the device number is compared against
        `/`. Equal device is the whole finding: the cache is not on a mount
        anybody chose, it is on the namespace's ephemeral root.
        """
        result = run_probe(
            'mkdir -p "$HOME/.cache"; '
            'echo "cache_type=$(stat -f -c %T "$HOME/.cache")"; '
            'echo "root_type=$(stat -f -c %T /)"; '
            'echo "cache_dev=$(stat -c %d "$HOME/.cache")"; '
            'echo "root_dev=$(stat -c %d /)"',
            layout, task, user_temp,
        )
        out = dict(
            line.split("=", 1) for line in result.stdout.split() if "=" in line
        )
        assert out.get("cache_type") == "tmpfs", f"{result.stdout}{result.stderr}"
        assert out.get("root_type") == "tmpfs", result.stdout
        assert out["cache_dev"] == out["root_dev"], (
            f"expected the cache on the root tmpfs, got {out}"
        )

    def test_bytes_written_to_the_cache_stay_on_that_tmpfs(self, layout, task, user_temp):
        """The mechanism, not just the mount: a write there is a write to RAM.

        Small (4 MiB) because the size is not the point — `uv sync
        --all-extras` unpacking torch is the real case, and it is the same
        mount.
        """
        result = run_probe(
            'mkdir -p "$HOME/.cache/uv" && '
            'dd if=/dev/zero of="$HOME/.cache/uv/blob" bs=1M count=4 2>/dev/null && '
            'echo "written=$(stat -c %s "$HOME/.cache/uv/blob")" && '
            'echo "blob_dev=$(stat -c %d "$HOME/.cache/uv/blob")" && '
            'echo "root_dev=$(stat -c %d /)"',
            layout, task, user_temp,
        )
        out = dict(
            line.split("=", 1) for line in result.stdout.split() if "=" in line
        )
        assert out.get("written") == str(4 * 1024 * 1024), f"{result.stdout}{result.stderr}"
        assert out["blob_dev"] == out["root_dev"], out

    def test_a_bound_directory_is_not_on_the_root_tmpfs(
        self, layout, task, repos_dir, user_repos, user_temp,
    ):
        """The control: a fix has somewhere to land.

        Without this the first two tests could pass in a sandbox where
        *everything* is the root tmpfs, and "point `UV_CACHE_DIR` at a bound
        directory" would be no fix at all. The comparison is device against
        device rather than filesystem type, because under the test runner
        `tmp_path` is itself on a tmpfs — a different mount, which is the
        property that matters, and asserting `!= tmpfs` would fail there for a
        reason that says nothing about the deployment.

        **The comparison is against the bind's own parent, not against `/`, and
        that is the whole assertion.** `build_bwrap_cmd` emits `--tmpfs /tmp`,
        and `tmp_path` is under `/tmp`, so every unbound directory in this
        layout sits on a tmpfs that is already not the root one — against `/`
        the check passed for a directory nothing had bound, which is what it
        was doing here until the per-user split made the difference visible.
        The parent is the discriminating comparison: a bind is its own mount
        and differs from the directory it was mounted inside, while an unbound
        child shares its parent's device.

        Inside the sandbox that parent is on bwrap's `--tmpfs /tmp`, not on the
        host filesystem — bwrap creates the intermediate mountpoint directories
        there. So the three devices are distinct (bind source, sandbox `/tmp`,
        sandbox root), and the assertion holds for the right reason only while
        pytest's basetemp is under `/tmp`. Move basetemp elsewhere and
        `parent_dev` becomes `root_dev`, at which point this collapses back into
        the vacuous check it replaced — hence both comparisons, not just the
        new one.
        """
        result = run_probe(
            f'test -d {_q(user_repos)} && echo bound=PRESENT || echo bound=ABSENT; '
            f'echo "repos_dev=$(stat -c %d {_q(user_repos)} 2>/dev/null)"; '
            f'echo "parent_dev=$(stat -c %d {_q(repos_dir)} 2>/dev/null)"; '
            'echo "root_dev=$(stat -c %d /)"',
            layout, task, user_temp,
        )
        out = dict(
            line.split("=", 1) for line in result.stdout.split() if "=" in line
        )
        # `stat` on a path that is not there prints nothing and the empty
        # string differs from every device number, so the comparisons below
        # would pass on an unbound directory. Both guards exist because the
        # test did exactly that when it was first run against `is_admin=False`.
        assert out.get("bound") == "PRESENT", (
            f"repos dir not bound: {result.stdout}{result.stderr}"
        )
        assert out.get("repos_dev", "").isdigit(), (
            f"no device number for the repos bind: {result.stdout}{result.stderr}"
        )
        assert out.get("parent_dev", "").isdigit(), (
            f"no device number for the repos root: {result.stdout}{result.stderr}"
        )
        assert out["repos_dev"] != out["parent_dev"], (
            f"the developer repos bind shares a device with the unbound root "
            f"above it, so it was not bound at all: {out}"
        )
        assert out["repos_dev"] != out["root_dev"], (
            f"the developer repos bind is on the root tmpfs: {out}"
        )


class TestAnEnvironmentIsPinnedToItsPath:
    """Why a second namespace must bind the repos directory at the same path."""

    @pytest.fixture
    def venv(self, user_repos):
        """A virtualenv built on the host, inside the bound repos subtree.

        Built with the stdlib rather than uv so the test needs no network and
        no populated cache. What it shares with a uv-built environment is the
        thing under test: an absolute interpreter path baked into every
        console script's shebang.
        """
        target = user_repos / "proj" / ".venv"
        subprocess.run(
            [str(_base_interpreter()), "-m", "venv", str(target)],
            check=True, capture_output=True, timeout=180,
        )
        if not (target / "bin" / "pip").exists():
            _unavailable("stdlib venv produced no console script to test against")
        return target

    def test_the_repos_directory_is_bound_at_its_host_path(
        self, layout, task, user_repos, user_temp,
    ):
        """The invariant the container has to match.

        The sandbox already satisfies it, because `build_bwrap_cmd` binds the
        task's repos subtree at its own path. A devbox that mounted the same
        tree at `/home/dev/repos` would not, and every environment built on one
        side would be unusable from the other.
        """
        (user_repos / "marker").write_text("written-on-the-host")
        result = run_probe(
            f'cat {_q(user_repos / "marker")} 2>/dev/null || echo MISSING',
            layout, task, user_temp,
        )
        assert "written-on-the-host" in result.stdout, f"{result.stdout}{result.stderr}"

    def test_a_venv_built_outside_runs_inside_the_sandbox(
        self, layout, task, user_temp, venv,
    ):
        """Tier two: an environment the sandbox did not build, it can still run.

        This is the fallback the two-tier design depends on — a test suite run
        on the host against an environment populated elsewhere. It needs the
        interpreter the venv points at to be reachable, which is why the venv
        is built from a `/usr` interpreter and why a devbox managed
        interpreter has to sit on the shared mount rather than in the
        container's own volume.
        """
        result = run_probe(
            f'{_q(venv / "bin" / "python")} -c "import sys; print(\'PY_OK\', sys.version_info[0])" '
            f'2>&1 || echo PY_FAIL; '
            f'{_q(venv / "bin" / "pip")} --version >/dev/null 2>&1 '
            f'&& echo SCRIPT_OK || echo SCRIPT_FAIL',
            layout, task, user_temp,
        )
        assert "PY_OK 3" in result.stdout, f"{result.stdout}{result.stderr}"
        assert "SCRIPT_OK" in result.stdout, f"{result.stdout}{result.stderr}"

    def test_a_console_script_bakes_the_absolute_path(self, venv):
        """Read the shebang. This is the mechanism the next test exercises."""
        shebang = (venv / "bin" / "pip").read_text().splitlines()[0]
        assert shebang.startswith("#!"), shebang
        assert str(venv) in shebang, (
            f"expected the venv's absolute path in the shebang, got {shebang!r}"
        )

    def test_the_same_venv_at_another_path_is_a_broken_environment(
        self, layout, task, user_repos, user_temp, venv,
    ):
        """The negative control for path equality.

        Moving the tree is what a second namespace at a different mount point
        looks like from the environment's point of view. `bin/python` survives
        — it is a symlink to an interpreter that did not move — and every
        console script dies on its shebang. A design that reads "one
        filesystem, two namespaces, nothing diverges" is true of the bytes and
        false of the environment, and this is the difference.
        """
        moved = user_repos / "moved" / ".venv"
        moved.parent.mkdir()
        venv.rename(moved)
        result = run_probe(
            f'{_q(moved / "bin" / "python")} -c "print(\'PY_OK\')" 2>/dev/null || echo PY_FAIL; '
            f'{_q(moved / "bin" / "pip")} --version >/dev/null 2>&1 '
            f'&& echo SCRIPT_OK || echo SCRIPT_FAIL',
            layout, task, user_temp,
        )
        assert "PY_OK" in result.stdout, (
            f"the interpreter symlink should survive a move: {result.stdout}{result.stderr}"
        )
        assert "SCRIPT_FAIL" in result.stdout, (
            f"expected the baked shebang to break: {result.stdout}{result.stderr}"
        )
