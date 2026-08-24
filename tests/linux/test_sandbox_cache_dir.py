"""Where a package-manager cache actually lands, inside the real namespace.

`tests/test_sandbox.py` asserts that `build_bwrap_cmd` puts a `--bind` in argv.
That is all it can assert on darwin. The claim ISSUE-305 makes is about a
filesystem — that without `security.sandbox_cache_dir` the sandbox's
`$HOME/.cache` is bwrap's own root tmpfs, so a `uv sync` unpacks into RAM the
host cannot attribute and discards at task exit — and only a real kernel can
answer it. These tests execute the argv around a `/bin/sh` probe and compare
device numbers.

The device comparison is the whole assertion, so it is written to fail loudly
rather than quietly: `stat -c %d` on a path that does not exist prints nothing,
and an empty string compares unequal to every other string. An earlier draft of
this file passed for that reason while proving nothing. Each probe therefore
reports the device of `/` alongside the device under test, and the test asserts
both are non-empty before comparing them.

**The ISSUE-319 sibling masks no longer exist, and neither does the
precondition this paragraph used to describe.** The cache is derived per user
inside `{developer.repos_dir}/{user_id}` now, so there is no other user's cache
in the namespace to mask, and `resolve_sandbox_cache_dir` no longer refuses a
covered cache on a bwrap without `--disable-userns` — with no mask holding the
boundary, the flag is back to being plain hardening for the database masks. So
the reason this tier cannot host the cross-user cache scenario is no longer the
precondition; it is only that `--proc` inside a nested user namespace needs a
fully visible procfs, and `scripts/test-linux.sh` grants CAP_SYS_ADMIN and
unconfined seccomp but not `systempaths=unconfined`.

The cases below use `security.sandbox_cache_dir` with `developer.repos_dir`
unset, which is the fallback branch and is unchanged by that work.

Run with `scripts/test-linux.sh`. Carries the `linux` marker, which pyproject's
addopts deselects.
"""

import os
import shlex
import subprocess

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import _bwrap_available, build_bwrap_cmd

pytestmark = pytest.mark.linux


def _unavailable(reason):
    """Skip — unless we are inside the runner, where a skip is the bug.

    Same contract as `test_sandbox_real.py`: `scripts/test-linux.sh` sets
    ISTOTA_LINUX_TIER=1 and exists to make this path execute, so a silent skip
    there would let the driver exit 0 having run nothing.
    """
    if os.environ.get("ISTOTA_LINUX_TIER") == "1":
        pytest.fail(f"running under scripts/test-linux.sh, where this must not skip: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if not _bwrap_available():
        _unavailable("bwrap is not available")


@pytest.fixture
def cache_layout(tmp_path, make_config):
    """A config whose cache directory is a real directory on a real filesystem.

    Deliberately outside every protected root — `resolve_sandbox_cache_dir`
    refuses a cache overlapping the database directories or the deployment
    tree, and a refusal here would look exactly like the fix not working.
    """
    db_dir = tmp_path / "app" / "data"
    db_dir.mkdir(parents=True)
    (db_dir / "istota.db").write_text("framework-db-contents")

    cache = tmp_path / "cache"
    cache.mkdir()

    config = make_config(
        db_path=db_dir / "istota.db",
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True),
    )
    return config, cache


@pytest.fixture
def user_temp(cache_layout):
    config, _ = cache_layout
    d = config.temp_dir / "alice"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def task():
    return db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )


def _q(path):
    return shlex.quote(str(path))


def _device_probe(path):
    """A probe printing the device number of `/` and of `path`.

    Both, always: a bare `stat` on a missing path prints an empty string, which
    would compare unequal to the root device and pass the test that matters for
    the wrong reason.
    """
    return (
        f'echo "root=$(stat -c %d / 2>/dev/null)"; '
        f'echo "target=$(stat -c %d {_q(path)} 2>/dev/null)"'
    )


def _run_probe(script, config, task, user_temp):
    cmd = build_bwrap_cmd(
        ["/bin/sh", "-c", script], config, task, False, [], user_temp,
    )
    assert cmd[0] == "bwrap", "sandbox unavailable — probe would have run unsandboxed"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _devices(result):
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    root = values.get("root", "")
    target = values.get("target", "")
    assert root, f"could not read the device of / : {result.stdout!r} {result.stderr!r}"
    return root, target


class TestSandboxCacheDirDevice:
    def test_without_the_key_the_cache_home_is_the_root_tmpfs(self, cache_layout, task, user_temp):
        """The pre-fix state, kept as the control the fix is measured against.

        `$HOME/.cache` is not bound at all, so it exists inside the namespace
        only as a directory bwrap created on its own root tmpfs — same device as
        `/`, and therefore RAM.
        """
        config, _ = cache_layout
        home = os.environ.get("HOME", "/tmp")
        result = _run_probe(
            f'mkdir -p {_q(f"{home}/.cache")} 2>/dev/null; ' + _device_probe(f"{home}/.cache"),
            config, task, user_temp,
        )
        root, target = _devices(result)
        assert target, f"could not read the device of the cache home: {result.stdout!r}"
        assert target == root, (
            f"$HOME/.cache is on device {target}, / is on {root} — something already "
            "binds it, and the control this file's real assertion rests on is gone"
        )

    def test_with_the_key_the_cache_is_off_the_root_tmpfs(self, cache_layout, task, user_temp):
        """The fix: the configured directory is a bind, so it is real disk."""
        config, cache = cache_layout
        config.security.sandbox_cache_dir = str(cache)
        result = _run_probe(_device_probe(cache / "alice"), config, task, user_temp)
        root, target = _devices(result)
        assert target, (
            f"the cache directory is not in the namespace at all: {result.stdout!r} "
            f"{result.stderr!r}"
        )
        assert target != root, (
            f"the cache directory is on device {target}, the same as / — it was not bound, "
            "so a uv cache written there is still RAM"
        )

    def test_the_bound_cache_is_writable_and_persists_on_the_host(self, cache_layout, task, user_temp):
        """A read-only bind would satisfy the device check and still be useless.

        The point of the directory is that what a task writes is there for the
        next task, which is exactly what the root tmpfs cannot do.
        """
        config, cache = cache_layout
        config.security.sandbox_cache_dir = str(cache)
        per_user = cache / "alice"
        result = _run_probe(
            f'echo cached > {_q(per_user / "probe")} 2>/dev/null && echo WRITE_OK || echo WRITE_FAIL',
            config, task, user_temp,
        )
        assert "WRITE_OK" in result.stdout, f"{result.stdout!r} {result.stderr!r}"
        assert (per_user / "probe").read_text().strip() == "cached", \
            "the write did not reach the host directory — the bind is not the host's"

    def test_another_users_cache_is_not_in_this_namespace(self, cache_layout, task, user_temp):
        """Per user, for the same reason every other RW bind is. uv trusts its
        unpacked wheels on read — it does not re-verify them against a hash — so
        one shared directory would let any task plant an archive that the next
        user's `uv sync` hardlinks straight into a venv.
        """
        config, cache = cache_layout
        config.security.sandbox_cache_dir = str(cache)
        bob = cache / "bob"
        bob.mkdir()
        (bob / "planted").write_text("bob's bytes")

        result = _run_probe(
            f'cat {_q(bob / "planted")} 2>/dev/null && echo READ_OK || echo READ_FAIL',
            config, task, user_temp,
        )
        assert "READ_FAIL" in result.stdout, \
            f"alice's namespace reached bob's cache: {result.stdout!r}"

    def test_a_cache_above_the_workspace_cannot_overmount_the_credential_helpers(
        self, cache_layout, task, user_temp,
    ):
        """bwrap applies argv in order, so a bind whose destination is an
        *ancestor* of an earlier mount covers it — the `.developer` read-only
        re-bind used the wrong way round. `config.temp_dir` is the worst case:
        every user's workspace sits under it, and `.developer` inside each one
        holds the credential-fetch helper. `_sandbox_bind_targets` is what
        refuses the configuration; this is that refusal from inside the kernel.
        """
        config, _ = cache_layout
        dev_dir = user_temp / ".developer"
        dev_dir.mkdir(exist_ok=True)
        original = "#!/bin/sh\nreal helper\n"
        (dev_dir / "credential-fetch").write_text(original)
        config.security.sandbox_cache_dir = str(config.temp_dir)

        result = _run_probe(
            f'echo tampered > {_q(dev_dir / "credential-fetch")} 2>/dev/null '
            f'&& echo WRITE_OK || echo WRITE_FAIL',
            config, task, user_temp,
        )
        assert "WRITE_FAIL" in result.stdout, \
            f"the credential helper was writable: {result.stdout!r} {result.stderr!r}"
        assert (dev_dir / "credential-fetch").read_text() == original
