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

That is a statement about the ISSUE-319 *mask* scenario, which needs the flag
on. `TestTheCacheBindSymlinkRace` at the bottom of this file is the other
cross-user cache question and needs the flag **off**, which is what this
runner's bwrap reports — so ISSUE-320 was answered here rather than blocked
here. That class's two flag arms skip each other, so exactly one runs per host
and each asserts which state it measured.

`TestSandboxCacheDirDevice` uses `security.sandbox_cache_dir` with
`developer.repos_dir` unset, which is the fallback branch and is unchanged by
that work. `TestTheCacheBindSymlinkRace` uses the derived branch, which is the
only one where the cache's parent is model-writable.

Run with `scripts/test-linux.sh`. Carries the `linux` marker, which pyproject's
addopts deselects.
"""

import os
import select
import shlex
import subprocess

import pytest

from istota import db
from istota.config import DeveloperConfig, SecurityConfig
from istota.executor import (
    SANDBOX_CACHE_ROOT_NAME,
    _bwrap_available,
    _bwrap_supports_disable_userns,
    build_bwrap_cmd,
)

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


class TestTheCacheBindSymlinkRace:
    """ISSUE-320: does the window between the resolver and bwrap reach anything?

    The derived branch puts the cache at ``{repos_dir}/{user_id}/.package-caches``,
    inside a directory bound read-write into that user's own admin tasks. So the
    name is model-writable, and ``user_max_foreground_workers`` defaults to 2 —
    two tasks for one user can run at once. The claim ISSUE-320 raises is that
    the second one swaps the directory for a symlink after
    ``resolve_sandbox_cache_dir`` validated it, and the sandbox then binds the
    link's target read-write.

    **The answer was yes**, and the fix is the gate in
    ``sandbox_cache_is_derived``: the cache is derived inside the repos subtree
    only where the covering repos bind is also emitted. Before that, a
    non-admin derived a cache inside ``{repos_dir}/{user_id}`` — a directory
    their own devbox container mounts read-write, with no admin gate on that
    mount or on the exec-socket bind that reaches it — and took the cache bind
    with nothing above it.

    **The swap is performed here rather than raced for, deliberately.** A loop
    that lost the race would report exactly the green a closed window reports,
    which is this tier's oldest failure mode. Planting between
    ``build_bwrap_cmd`` returning and ``subprocess.run`` starting *is* a won
    race: the argv is written, the resolver's ``O_NOFOLLOW`` check is done, and
    nothing but bwrap's own path walk is left.

    The first two tests are a pair and neither means anything alone. The first
    is the exposure, constructed by calling ``build_bwrap_cmd`` with the
    pre-fix combination directly, so what it produced stays measured after the
    gate makes it unreachable. The second is the covered shape. Without the
    first, the second proves only that the probe is blind.
    ``tests/test_sandbox.py::TestTheDerivationIsGatedOnAdmin`` is what holds the
    gate itself, in the default suite.
    """

    @pytest.fixture
    def derived_layout(self, tmp_path, make_config):
        """The derived-cache shape: a repos root with a per-user subtree.

        ``security.sandbox_cache_dir`` is left blank on purpose — on the derived
        branch it is not consulted at all, and setting it would test the other
        branch while looking like it tested this one.

        ``admin_users`` is left empty, which `Config.is_admin` reads as everyone
        being an admin, so `sandbox_cache_is_derived` is true here for whichever
        `is_admin` a test passes to `build_bwrap_cmd`. That is what lets the
        first test below construct the pre-fix shape — a derived cache with no
        covering bind — which the fix makes unreachable and which is otherwise
        not expressible.
        """
        db_dir = tmp_path / "app" / "data"
        db_dir.mkdir(parents=True)
        (db_dir / "istota.db").write_text("framework-db-contents")

        repos = tmp_path / "repos"
        repos.mkdir()

        config = make_config(
            db_path=db_dir / "istota.db",
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(sandbox_enabled=True),
            developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
        )
        return config, repos

    @pytest.fixture
    def user_temp(self, derived_layout):
        config, _ = derived_layout
        d = config.temp_dir / "alice"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @pytest.fixture
    def victim(self, derived_layout):
        """Another user's subtree, holding a byte string only it can supply."""
        _, repos = derived_layout
        bob = repos / "bob"
        bob.mkdir()
        (bob / "planted").write_text("bobs-private-bytes")
        return bob

    @staticmethod
    def _build(config, task, user_temp, script, *, is_admin):
        cmd = build_bwrap_cmd(
            ["/bin/sh", "-c", script], config, task, is_admin, [], user_temp,
        )
        assert cmd[0] == "bwrap", "sandbox unavailable — probe would have run unsandboxed"
        return cmd

    @staticmethod
    def _assert_cache_bound(cmd, cache):
        """The cache bind is in the argv.

        Without this the tests below pass unchanged on a build that stopped
        emitting the bind altogether, which is the difference between "the
        window is closed" and "there is nothing in the window".
        """
        pairs = [
            (cmd[i + 1], cmd[i + 2])
            for i, arg in enumerate(cmd)
            if arg == "--bind" and i + 2 < len(cmd)
        ]
        # `_bind` emits the *resolved* source against the destination *as
        # written*, and the two differ whenever pytest's basetemp has a
        # symlinked ancestor. Comparing the written path on both sides turned
        # that environment into "no cache bind in the argv", which reads as a
        # product failure.
        assert (str(cache.resolve()), str(cache)) in pairs, (
            f"no cache bind in the argv, so this test proves nothing: {pairs}"
        )

    @staticmethod
    def _win_the_race(repos, victim):
        """Replace the validated cache directory with a symlink to ``victim``."""
        cache = repos / "alice" / SANDBOX_CACHE_ROOT_NAME
        cache.rename(repos / "alice" / ".package-caches.evicted")
        cache.symlink_to(victim)
        return cache

    def test_the_kernel_follows_a_symlink_planted_after_the_argv_was_built(
        self, derived_layout, task, user_temp, victim,
    ):
        """The window is real. This is the exposure ISSUE-320 asked about.

        ``_bind`` emits ``src.resolve()`` as the source, which on a real
        directory is the written name — and bwrap's ``mount`` walks that name
        again, in the kernel, after the swap.

        **Run with ``is_admin=False``, which was a reachable shape before the
        fix and is what the fix removes.** The derivation used to be gated on
        `developer.enabled` alone, so a non-admin derived a cache inside
        `{repos_dir}/{user_id}` and took this bind with no repos bind above it
        — while the devbox mounts that same directory read-write into their own
        container. `build_bwrap_cmd` is called directly here, so the pre-fix
        argv is still constructible and the exposure it produced stays
        measured; `TestTheDerivationIsGatedOnAdmin` in `tests/test_sandbox.py`
        is what holds the gate that stops it being reachable.
        """
        config, repos = derived_layout
        cache = repos / "alice" / SANDBOX_CACHE_ROOT_NAME
        cmd = self._build(
            config, task, user_temp,
            f'cat {_q(cache / "planted")} 2>/dev/null || echo READ_FAIL',
            is_admin=False,
        )
        self._assert_cache_bound(cmd, cache)
        self._win_the_race(repos, victim)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert "bobs-private-bytes" in result.stdout, (
            "the swapped symlink was not followed, so the window this class is "
            f"about does not exist on this kernel: {result.stdout!r} {result.stderr!r}"
        )

    def test_the_repos_bind_covers_the_swapped_cache_bind(
        self, derived_layout, task, user_temp, victim,
    ):
        """The question as filed, on the shape where the swap is reachable.

        Only an admin task for user U can write ``{repos_dir}/U``, so only an
        admin task can plant the symlink — and an admin task is exactly the one
        that gets the repos bind, which is emitted *after* the cache bind and
        is an ancestor of it. The swapped mount lands underneath and is
        unreachable. The attacker and the shadow arrive together.
        """
        config, repos = derived_layout
        cache = repos / "alice" / SANDBOX_CACHE_ROOT_NAME
        cmd = self._build(
            config, task, user_temp,
            f'test -L {_q(cache)} && echo IS_SYMLINK || echo IS_NOT_SYMLINK; '
            f'cat {_q(cache / "planted")} 2>/dev/null || echo READ_FAIL',
            is_admin=True,
        )
        self._assert_cache_bound(cmd, cache)
        self._win_the_race(repos, victim)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert "bobs-private-bytes" not in result.stdout, (
            "the sandbox reached another user's subtree through the swapped "
            f"cache bind — ISSUE-320 holds: {result.stdout!r} {result.stderr!r}"
        )
        assert "READ_FAIL" in result.stdout, f"{result.stdout!r} {result.stderr!r}"
        # The discriminating half. Reading nothing is also what a bwrap that
        # failed to start reports, and what an empty mount reports. Seeing a
        # *symlink* at the cache path says the repos bind is what is on top:
        # the swapped mount is a directory, and the host entry carried in by
        # the repos bind is the link.
        assert "IS_SYMLINK" in result.stdout, (
            "the cache path is not the host symlink, so the repos bind is not "
            f"what covered it: {result.stdout!r} {result.stderr!r}"
        )

    def test_a_bound_cache_is_not_a_mountpoint_on_the_host(
        self, derived_layout, task, user_temp,
    ):
        """The entry's stated mechanism for why the window used to be closed.

        ISSUE-320 records that passing ``--disable-userns`` "pins the cache
        directory as a mountpoint, and ``rename(2)`` on a mountpoint returns
        EBUSY". bwrap mounts inside its own mount namespace, so the host
        directory never becomes a mountpoint and the rename that walks this
        window is not one the kernel would refuse.

        **This is the flag-*off* arm**, pinned rather than left to the host:
        without the assertion below, a bwrap that has the flag would run this
        and `test_disable_userns_does_not_pin_it_either` on identical argv and
        the pair would cover one state twice while claiming two. Skipped where
        the flag is present, so the two arms never overlap and each says which
        state it measured.
        """
        if _bwrap_supports_disable_userns():
            pytest.skip("this bwrap has --disable-userns; the flag-on arm covers it")

        config, repos = derived_layout
        cache = repos / "alice" / SANDBOX_CACHE_ROOT_NAME
        cmd = self._build(
            config, task, user_temp, "echo SANDBOX_UP; sleep 10", is_admin=False,
        )
        assert "--disable-userns" not in cmd, (
            "the flag probes unsupported but was emitted anyway; this test is "
            "not measuring the state it says"
        )
        self._assert_cache_bound(cmd, cache)
        _rename_while_bound(cmd, cache, repos)

    def test_disable_userns_does_not_pin_it_either(
        self, derived_layout, task, user_temp,
    ):
        """The same measurement with the flag actually present.

        Skipped rather than failed where bwrap lacks it: this runner's bwrap is
        one of those, which is precisely the environment ISSUE-320 asked for,
        and the tests above are the ones that answer the filed question. This
        one settles the entry's account of what the flag used to be doing, on a
        bwrap that has it.
        """
        # The same predicate `build_bwrap_cmd` gates the flag on, not a
        # re-spelling of it: the probe argv has to carry `--unshare-user` or
        # bwrap rejects the pair and every host reports the flag missing, which
        # is a bug that already happened once here.
        if not _bwrap_supports_disable_userns():
            pytest.skip("this bwrap does not support --disable-userns")

        config, repos = derived_layout
        cache = repos / "alice" / SANDBOX_CACHE_ROOT_NAME
        cmd = self._build(
            config, task, user_temp, "echo SANDBOX_UP; sleep 10", is_admin=False,
        )
        assert "--disable-userns" in cmd, (
            "the flag probes supported but was not emitted; this test is not "
            "measuring what it says"
        )
        self._assert_cache_bound(cmd, cache)
        _rename_while_bound(cmd, cache, repos)


def _rename_while_bound(cmd, cache, repos):
    """Start the sandbox, then rename the bound cache directory on the host.

    The child announces itself on stdout first, so the rename is measured
    against a live bind rather than against a sandbox that has not started its
    mounts yet or has already exited — either of which would let the rename
    succeed for a reason that says nothing about mountpoints.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        # Bounded, like every `subprocess.run` in this file: a bwrap that gets
        # part-way through its mounts and then hangs would otherwise block the
        # run forever with the `finally` never reached.
        if not select.select([proc.stdout], [], [], 30)[0]:
            raise AssertionError("the sandbox produced no output within 30s")
        first = proc.stdout.readline()
        assert "SANDBOX_UP" in first, f"the sandbox never started: {first!r}"

        renamed = repos / "alice" / ".package-caches.moved"
        cache.rename(renamed)
        assert renamed.is_dir(), "the rename reported success but nothing moved"
    finally:
        proc.kill()
        proc.wait(timeout=10)
