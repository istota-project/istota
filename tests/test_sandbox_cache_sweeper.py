"""ISSUE-317 — nothing prunes the package caches `security.sandbox_cache_dir` creates.

The key is what moves a task's uv and npm caches off bubblewrap's root tmpfs
and onto disk, which is the whole point of ISSUE-305. Turning it on without a
sweeper behind it swaps a bounded RAM burn for an unbounded disk leak: the
caches stop dying with the task's tmpfs and nothing else removes them, on the
same volume the worktree reaper is already fighting for.

The risk here is entirely on the delete side, so most of this file is the
*refuse* cases rather than the reclaim ones. A sweeper that only proved it can
bring a cache under its ceiling passes just as well while deleting a directory
outside its root, or while wiping the cache a `uv sync` is mid-way through
hardlinking out of.

Three separate guards stand between a running task and a wipe, and each is
tested on its own because each fails differently:

  * the caller's set of users with a task in flight — the primary one, and the
    only one that sees a warm-cache sync doing nothing but reading;
  * an idle window on the cache tree's own newest mtime, which catches a writer
    the task table never knew about;
  * uv's own in-use check, which the sweeper preserves by never passing
    ``--force``. ``test_force_is_never_passed_to_uv`` is what holds that, and
    it is an argv assertion because the flag's absence is the whole mechanism.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from istota.sandbox_cache_sweeper import (
    ACTION_BUSY,
    ACTION_FUTURE_MTIME,
    ACTION_NO_TOOLS,
    ACTION_OUTSIDE,
    ACTION_RECENT,
    ACTION_RECLAIMED,
    ACTION_STILL_OVER,
    ACTION_WIPED,
    MIN_MAX_BYTES,
    CACHE_NPM,
    CACHE_UV,
    measure_cache,
    sweep_and_report,
    sweep_caches,
)

MB = 1024 * 1024


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_FAKE = """#!{python}
import json, os, shutil, sys

argv = sys.argv[1:]
here = os.path.dirname(os.path.abspath(sys.argv[0]))
# The log path is derived from where the script lives, not read from the
# environment: `_tool_env` builds an allowlisted environment, so nothing the
# test exports reaches this process. That is the property under test in
# `TestTheTools::test_the_daemons_environment_is_not_handed_to_the_tools`, and
# a fixture that needed a variable of its own would have to punch a hole in it.
with open(os.path.join(here, "tool-calls.log"), "a") as fh:
    fh.write({name!r} + chr(9) + chr(9).join(argv) + chr(10))
with open(os.path.join(here, "tool-env.log"), "a") as fh:
    fh.write(json.dumps(dict(os.environ)) + chr(10))

if {fails!r}:
    sys.stderr.write("fake failure" + chr(10))
    sys.exit(1)

target = None
for i, arg in enumerate(argv):
    if arg in ("--cache-dir", "--cache") and i + 1 < len(argv):
        target = argv[i + 1]

# `prune` reclaims `prunes` entries; `clean` empties the tree, as the real
# verbs do.
wipes = any(a == "clean" for a in argv)
if target and os.path.isdir(target):
    entries = sorted(os.listdir(target))
    chosen = entries if wipes else entries[:{prunes!r}]
    for entry in chosen:
        path = os.path.join(target, entry)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
sys.exit(0)
"""


@pytest.fixture
def toolbox(tmp_path, monkeypatch):
    """A PATH holding fake `uv` and `npm`, and a log of how they were called.

    Real subprocesses rather than a patched ``subprocess.run``: the argv and the
    environment the sweeper builds are the security-relevant part of this
    module, and a mock records what the caller *meant* to run.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))

    def install(name: str, *, fails: bool = False, prunes: int = 0) -> Path:
        script = bindir / name
        script.write_text(
            _FAKE.format(python=sys.executable, name=name, fails=fails, prunes=prunes)
        )
        script.chmod(0o755)
        return script

    def calls() -> list[list[str]]:
        log = bindir / "tool-calls.log"
        if not log.exists():
            return []
        return [line.split("\t") for line in log.read_text().splitlines()]

    def envs() -> list[dict]:
        log = bindir / "tool-env.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    install.calls = calls  # type: ignore[attr-defined]
    install.envs = envs  # type: ignore[attr-defined]
    install.bindir = bindir  # type: ignore[attr-defined]
    return install


def _fill(path: Path, name: str, size: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    blob = path / name
    blob.write_bytes(b"\0" * size)
    return blob


def _cache(root: Path, user: str, *, uv: int = 0, npm: int = 0, other: int = 0) -> Path:
    """A per-user cache in the layout `resolve_sandbox_cache_dir` creates."""
    user_dir = root / user
    user_dir.mkdir(parents=True, exist_ok=True)
    if uv:
        _fill(user_dir / CACHE_UV / "archive-v0", "wheel", uv)
    if npm:
        _fill(user_dir / CACHE_NPM / "_cacache", "content", npm)
    if other:
        _fill(user_dir / "huggingface", "model", other)
    return user_dir


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames + dirnames:
            os.utime(os.path.join(dirpath, name), (stamp, stamp))
    os.utime(path, (stamp, stamp))


CEILING = 2 * MB


def sweep(root, **kwargs):
    """`sweep_caches` with a ceiling these tests can actually exceed.

    `floor_bytes` is the module's clamp and the real floor is a gibibyte — see
    `MIN_MAX_BYTES`. Dropping it here is what lets every case below write four
    megabytes instead of four gigabytes. The clamp itself is tested on its own
    in `test_a_ceiling_below_the_floor_is_clamped`, which calls `sweep_caches`
    directly and therefore gets the real floor.
    """
    kwargs.setdefault("max_bytes", CEILING)
    kwargs.setdefault("floor_bytes", 0)
    return sweep_caches(root, **kwargs)


def _by_user(outcomes) -> dict[str, object]:
    return {o.user_id: o for o in outcomes}


# ---------------------------------------------------------------------------
# Containment — nothing outside the configured root is ever reached
# ---------------------------------------------------------------------------

class TestContainment:
    def test_a_symlinked_per_user_entry_is_refused_and_its_target_untouched(
        self, tmp_path, toolbox
    ):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        _fill(elsewhere / CACHE_UV / "archive-v0", "wheel", 4 * MB)
        _age(elsewhere, 86400)
        (root / "alice").symlink_to(elsewhere)

        outcomes = sweep(root)

        assert _by_user(outcomes)["alice"].action == ACTION_OUTSIDE
        assert (elsewhere / CACHE_UV / "archive-v0" / "wheel").exists()
        assert toolbox.calls() == []

    def test_the_root_and_the_per_user_directory_survive_a_wipe(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)

        sweep(root)

        assert root.is_dir()
        assert user_dir.is_dir()

    def test_a_symlink_to_a_sibling_cache_is_refused(self, tmp_path, toolbox):
        """The case the weaker containment rule let through.

        `{root}/zzz -> {root}/bob` resolves to a path whose *parent* is the
        root, so a rule phrased as "the resolved parent is the root" accepts it
        — and `user_id` then comes from the entry, so the busy check asks about
        `zzz` while the reclaim verb runs against bob's real cache. Guard 1
        defeated by a name.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        victim = _cache(root, "bob", uv=4 * MB)
        _age(root, 86400)
        (root / "zzz").symlink_to(victim)

        outcomes = _by_user(sweep(root, busy_users={"bob"}))

        assert outcomes["zzz"].action == ACTION_OUTSIDE
        assert outcomes["bob"].action == ACTION_BUSY
        assert (victim / CACHE_UV / "archive-v0" / "wheel").exists()
        assert toolbox.calls() == []

    def test_only_a_direct_child_of_the_root_is_a_candidate(self, tmp_path, toolbox):
        """`resolve_sandbox_cache_dir` creates `{root}/{user_id}` and nothing deeper."""
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root / "nested", "alice", uv=4 * MB)
        (root / "stray-file").write_bytes(b"\0" * 16)
        _age(root, 86400)

        outcomes = sweep(root)

        assert [o.user_id for o in outcomes] == ["nested"]
        assert (root / "stray-file").exists()

    def test_a_missing_root_is_a_no_op(self, tmp_path, toolbox):
        toolbox("uv")
        assert sweep(tmp_path / "nope") == []
        assert toolbox.calls() == []

    def test_a_root_that_is_a_file_is_a_no_op(self, tmp_path, toolbox):
        toolbox("uv")
        target = tmp_path / "caches"
        target.write_text("not a directory")
        assert sweep(target) == []

    def test_an_unreadable_root_never_raises(self, tmp_path, toolbox):
        toolbox("uv")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        root.chmod(0o000)
        try:
            assert sweep(root) == []
        finally:
            root.chmod(0o755)


# ---------------------------------------------------------------------------
# Liveness — a cache in use is not swept
# ---------------------------------------------------------------------------

class TestLiveness:
    def test_a_user_with_a_task_in_flight_is_skipped_entirely(self, tmp_path, toolbox):
        """Not even the cheap reclaim: `uv cache prune` unlinks too."""
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _cache(root, "bob", uv=4 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root, busy_users={"alice"}))

        assert outcomes["alice"].action == ACTION_BUSY
        assert outcomes["bob"].action in (ACTION_WIPED, ACTION_RECLAIMED)
        for call in toolbox.calls():
            assert "alice" not in "\t".join(call)

    def test_a_cache_written_moments_ago_is_left_alone(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)
        # One fresh file, as a download landing mid-sweep would leave.
        _fill(root / "alice" / CACHE_UV / "archive-v0", "fresh", 1024)

        outcomes = _by_user(sweep(root, min_idle_seconds=900))

        assert outcomes["alice"].action == ACTION_RECENT
        assert toolbox.calls() == []

    def test_a_cache_stamped_in_the_future_is_reported_not_pinned(self, tmp_path, toolbox):
        """The mtime is model-controlled — the tree is bound RW into that user's
        own sandbox — so `touch -d '+10 years'` would otherwise make the idle
        window negative, below any threshold, and pin the cache for good. That
        is the unbounded disk leak this module exists to prevent, restored by
        one command, and reported as `recent` it would never be warned about.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)
        ahead = time.time() + 10 * 365 * 86400
        os.utime(user_dir / CACHE_UV / "archive-v0" / "wheel", (ahead, ahead))

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_FUTURE_MTIME
        assert toolbox.calls() == []

    def test_a_future_mtime_is_warned_about_rather_than_counted(self, tmp_path, toolbox, caplog):
        toolbox("uv")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)
        ahead = time.time() + 86400
        os.utime(user_dir / CACHE_UV / "archive-v0" / "wheel", (ahead, ahead))

        with caplog.at_level("WARNING", logger="istota.sandbox_cache_sweeper"):
            sweep_and_report(root, max_bytes=MIN_MAX_BYTES)

        assert "alice" in caplog.text
        assert "future" in caplog.text

    def test_the_idle_window_is_measured_against_the_supplied_clock(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 600)

        recent = _by_user(sweep(root, min_idle_seconds=900))
        assert recent["alice"].action == ACTION_RECENT

        later = _by_user(
            sweep(root, min_idle_seconds=900, now=time.time() + 3600)
        )
        assert later["alice"].action != ACTION_RECENT


# ---------------------------------------------------------------------------
# Policy — a size ceiling, with the cheap reclaim tried first
# ---------------------------------------------------------------------------

class TestPolicy:
    def test_the_cheap_reclaim_runs_even_on_a_cache_under_the_ceiling(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=64 * 1024, npm=64 * 1024)
        _age(root, 86400)

        outcomes = _by_user(sweep(root, max_bytes=512 * MB))

        assert outcomes["alice"].action == ACTION_RECLAIMED
        verbs = {tuple(c) for c in toolbox.calls()}
        assert any("prune" in c for c in verbs)
        assert any("verify" in c for c in verbs)
        assert not any("clean" in c for c in verbs)

    def test_a_cache_the_reclaim_brings_under_the_ceiling_is_not_wiped(self, tmp_path, toolbox):
        # `prunes=2` removes both entries under `uv/`, which is the whole overage.
        toolbox("uv", prunes=2)
        toolbox("npm")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice")
        _fill(user_dir / CACHE_UV, "a", 3 * MB)
        _fill(user_dir / CACHE_UV, "b", 3 * MB)
        _fill(user_dir / CACHE_NPM, "keep", 16 * 1024)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_RECLAIMED
        assert (user_dir / CACHE_NPM / "keep").exists()
        assert not any("clean" in c for c in toolbox.calls())

    def test_a_cache_still_over_the_ceiling_escalates_to_a_wipe(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_WIPED
        assert outcomes["alice"].after_bytes < outcomes["alice"].before_bytes
        assert user_dir.is_dir()
        calls = toolbox.calls()
        assert any("clean" in c for c in calls)

    def test_a_write_during_the_reclaim_cancels_the_escalation(self, tmp_path, toolbox):
        """The prune round stalls for exactly as long as a task is syncing.

        uv holds an exclusive lock on the cache for the whole of an install and
        `cache prune` blocks on it rather than refusing, so carrying the earlier
        liveness reading into the wipe fires it on evidence gathered before that
        task existed. The fake writes into the cache while it runs, which is
        what a real sync racing the sweep does.
        """
        script = toolbox("uv")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)
        toolbox("npm")
        # Replace uv with one that writes a fresh file, as a sync arriving
        # mid-reclaim would.
        script.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            f"open({str(user_dir / CACHE_UV / 'incoming')!r}, 'wb').write(b'x')\n"
        )
        script.chmod(0o755)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_RECENT
        assert (user_dir / CACHE_NPM / "_cacache" / "content").exists()
        assert not any("clean" in c for c in toolbox.calls())

    def test_a_ceiling_below_the_floor_is_clamped(self, tmp_path, toolbox):
        """A ceiling under one sync's working set would wipe on every sweep."""
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=64 * 1024)
        _age(root, 86400)

        outcomes = _by_user(sweep_caches(root, max_bytes=1024))

        assert outcomes["alice"].action == ACTION_RECLAIMED
        assert not any("clean" in c for c in toolbox.calls())

    def test_the_ceiling_counts_the_whole_per_user_directory(self, tmp_path, toolbox):
        """XDG_CACHE_HOME points at the user root, so more than uv and npm land there.

        Neither tool can reclaim a third tool's cache, so this reports
        `still-over` and names it rather than reaching for the filesystem.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", other=8 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_STILL_OVER
        assert (user_dir / "huggingface" / "model").exists()
        assert "huggingface" in outcomes["alice"].detail


    def test_an_overage_no_verb_can_reach_does_not_wipe_the_working_caches(
        self, tmp_path, toolbox
    ):
        """Otherwise the wipe repeats every sweep, forever, and never succeeds.

        `XDG_CACHE_HOME` points at the per-user root, which is bound read-write
        into that user's own sandbox, so bytes can sit beside `uv/` and `npm/`
        where neither `clean` verb reaches. Escalating on the whole directory
        means one large file in a third subdirectory throws away both real
        caches on every pass while the total never comes under the ceiling —
        every task re-downloading every time, which is the pre-ISSUE-305
        behaviour the floor exists to prevent, arriving by another road.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=2 * MB, npm=2 * MB, other=8 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_STILL_OVER
        # The working caches are still there; only a `clean` would have taken them.
        assert (user_dir / CACHE_UV / "archive-v0" / "wheel").exists()
        assert (user_dir / CACHE_NPM / "_cacache" / "content").exists()
        assert not any("clean" in c for c in toolbox.calls())
        assert "huggingface" in outcomes["alice"].detail


# ---------------------------------------------------------------------------
# The tools — how they are invoked, and what happens when they are not there
# ---------------------------------------------------------------------------

class TestTheTools:
    def test_force_is_never_passed_to_uv(self, tmp_path, toolbox):
        """uv's `--force` bypasses its own in-use check, which is our last guard.

        Both `uv cache prune` and `uv cache clean` refuse to touch a cache
        another uv process holds unless `--force` is given. That refusal is the
        only guard that sees a `uv sync` which is doing nothing but hardlinking
        out of a warm cache — no bytes are written, so the idle window cannot
        see it, and the task table can lose a race the kernel cannot.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)

        sweep(root)

        uv_calls = [c for c in toolbox.calls() if c[0] == "uv"]
        assert uv_calls
        for call in uv_calls:
            assert "--force" not in call

    def test_each_tool_is_pointed_at_its_own_subdirectory(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)

        sweep(root)

        for call in toolbox.calls():
            tool, args = call[0], call[1:]
            flag = "--cache-dir" if tool == "uv" else "--cache"
            assert flag in args, call
            target = args[args.index(flag) + 1]
            assert target == str(root / "alice" / (CACHE_UV if tool == "uv" else CACHE_NPM))

    def test_a_missing_tool_never_becomes_a_filesystem_delete(self, tmp_path, toolbox):
        """Neither binary on PATH: report it, remove nothing."""
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_NO_TOOLS
        assert (user_dir / CACHE_UV / "archive-v0" / "wheel").exists()
        assert (user_dir / CACHE_NPM / "_cacache" / "content").exists()

    def test_one_missing_tool_still_runs_the_other(self, tmp_path, toolbox):
        toolbox("uv")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert {c[0] for c in toolbox.calls()} == {"uv"}
        assert "npm" in outcomes["alice"].detail

    def test_a_failing_tool_does_not_escalate_to_a_filesystem_delete(self, tmp_path, toolbox):
        toolbox("uv", fails=True)
        toolbox("npm", fails=True)
        root = tmp_path / "caches"
        user_dir = _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)

        outcomes = _by_user(sweep(root))

        assert outcomes["alice"].action == ACTION_STILL_OVER
        assert (user_dir / CACHE_UV / "archive-v0" / "wheel").exists()

    def test_the_tools_run_outside_the_directory_the_model_can_write(self, tmp_path, toolbox):
        """A `uv.toml` or an `.npmrc` in the cache is model-written input.

        The per-user cache is bound RW into that user's sandbox, so running a
        host-side tool with its cwd anywhere under the root would let the model
        hand configuration to a process running as the daemon.
        """
        probe = tmp_path / "bin" / "uv"
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)
        toolbox("npm")
        cwd_log = tmp_path / "cwd.log"
        probe.parent.mkdir(exist_ok=True)
        probe.write_text(
            f"#!{sys.executable}\nimport os\n"
            f"open({str(cwd_log)!r}, 'a').write(os.getcwd() + chr(10))\n"
        )
        probe.chmod(0o755)

        sweep(root)

        seen = [Path(line) for line in cwd_log.read_text().split()]
        assert seen
        for cwd in seen:
            assert root.resolve() not in cwd.resolve().parents
            assert cwd.resolve() != root.resolve()

    def test_the_daemons_environment_is_not_handed_to_the_tools(self, tmp_path, toolbox):
        """The daemon carries the secret key and every module credential.

        A subprocess whose job is to unlink files needs none of it, and the
        allowlist is also the only way to be sure of the two variables that
        would quietly break the sweep — an inherited `npm_config_cache`
        redirects the reclaim, and `UV_NO_CACHE` makes uv work out of a
        temporary directory so the prune reclaims nothing.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB, npm=4 * MB)
        _age(root, 86400)
        os.environ["ISTOTA_SECRET_KEY"] = "not-a-real-key"
        os.environ["npm_config_cache"] = "/somewhere/else"
        os.environ["NPM_CONFIG_CACHE"] = "/somewhere/else"
        os.environ["UV_NO_CACHE"] = "1"
        try:
            sweep(root)
        finally:
            for key in ("ISTOTA_SECRET_KEY", "npm_config_cache",
                        "NPM_CONFIG_CACHE", "UV_NO_CACHE"):
                os.environ.pop(key, None)

        seen = toolbox.envs()
        assert seen
        for env in seen:
            assert "ISTOTA_SECRET_KEY" not in env
            assert "UV_NO_CACHE" not in env
            assert "NPM_CONFIG_CACHE" not in env
            assert env["npm_config_cache"] == str(root / "alice" / CACHE_NPM)
            assert env["UV_CACHE_DIR"] == str(root / "alice" / CACHE_UV)

    def test_uv_is_told_to_ignore_configuration_files(self, tmp_path, toolbox):
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)

        sweep(root)

        for call in toolbox.calls():
            if call[0] == "uv":
                assert "--no-config" in call


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

class TestMeasurement:
    def test_a_hardlinked_inode_is_counted_once(self, tmp_path):
        tree = tmp_path / "tree"
        blob = _fill(tree, "a", 2 * MB)
        os.link(blob, tree / "b")

        once = measure_cache(tree).bytes
        assert once < 3 * MB

    def test_a_symlink_out_of_the_tree_is_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        _fill(outside, "big", 8 * MB)
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "escape").symlink_to(outside)

        assert measure_cache(tree).bytes < MB

    def test_the_newest_mtime_is_the_newest_in_the_tree(self, tmp_path):
        tree = tmp_path / "tree"
        _fill(tree / "old", "a", 1024)
        _age(tree, 86400)
        fresh = _fill(tree / "new", "b", 1024)

        assert measure_cache(tree).newest_mtime >= fresh.stat().st_mtime - 1

    def test_measuring_a_missing_tree_never_raises(self, tmp_path):
        assert measure_cache(tmp_path / "nope").bytes == 0


# ---------------------------------------------------------------------------
# The reporting wrapper
# ---------------------------------------------------------------------------

class TestReporting:
    def test_a_cache_it_took_nothing_from_is_counted_in_the_summary(self, tmp_path, toolbox, caplog):
        """The default ceiling is a gibibyte, so a four-megabyte cache is fine.

        The summary counts those rather than logging a line each: the number an
        operator needs to see growing is how many caches the sweep got nothing
        out of, and a line per user would bury it.
        """
        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _cache(root, "bob", uv=4 * MB)
        _age(root, 86400)

        with caplog.at_level("INFO", logger="istota.sandbox_cache_sweeper"):
            outcomes = sweep_and_report(root, max_bytes=MIN_MAX_BYTES)

        assert {o.action for o in outcomes} == {ACTION_RECLAIMED}
        assert "took no bytes from 2 cache(s)" in caplog.text

    def test_it_never_raises(self, tmp_path, toolbox, monkeypatch):
        monkeypatch.setattr(
            "istota.sandbox_cache_sweeper.sweep_caches",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert sweep_and_report(tmp_path, max_bytes=MIN_MAX_BYTES) == []


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def _load(self, tmp_path, body: str):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(body)
        return load_config(path)

    def test_the_keys_load_from_toml(self, tmp_path):
        cfg = self._load(
            tmp_path,
            "[security]\n"
            'sandbox_cache_dir = "/srv/x/repos/.package-caches"\n'
            "sandbox_cache_sweep_enabled = false\n"
            "sandbox_cache_max_gb = 4.5\n"
            "\n[scheduler]\n"
            "sandbox_cache_sweep_interval = 900\n",
        )
        assert cfg.security.sandbox_cache_dir == "/srv/x/repos/.package-caches"
        assert cfg.security.sandbox_cache_sweep_enabled is False
        assert cfg.security.sandbox_cache_max_gb == 4.5
        assert cfg.scheduler.sandbox_cache_sweep_interval == 900

    def test_the_defaults_leave_the_sweep_armed_but_inert(self, tmp_path):
        cfg = self._load(tmp_path, 'db_path = "d.sqlite"\n')
        assert cfg.security.sandbox_cache_dir == ""
        assert cfg.security.sandbox_cache_sweep_enabled is True
        assert cfg.security.sandbox_cache_max_gb == 10.0
        assert cfg.scheduler.sandbox_cache_sweep_interval == 21600

    def test_a_quoted_false_switches_the_sweep_off(self, tmp_path):
        """`bool("false")` is True, and this key decides whether a delete path runs."""
        cfg = self._load(
            tmp_path, '[security]\nsandbox_cache_sweep_enabled = "false"\n'
        )
        assert cfg.security.sandbox_cache_sweep_enabled is False

    def test_a_nonsense_switch_warns_and_leaves_the_sweep_on(self, tmp_path, caplog):
        with caplog.at_level("WARNING", logger="istota.config"):
            cfg = self._load(
                tmp_path, '[security]\nsandbox_cache_sweep_enabled = "maybe"\n'
            )
        assert cfg.security.sandbox_cache_sweep_enabled is True
        assert "sandbox_cache_sweep_enabled" in caplog.text

    @pytest.mark.parametrize("literal", ["nan", "inf", "0", "-3", '"big"'])
    def test_a_ceiling_that_is_not_a_positive_number_takes_the_default(
        self, tmp_path, literal
    ):
        """NaN compares false against everything, so it would disable the ceiling
        rather than the sweep — every cache would read as over budget."""
        cfg = self._load(tmp_path, f"[security]\nsandbox_cache_max_gb = {literal}\n")
        assert cfg.security.sandbox_cache_max_gb == 10.0


# ---------------------------------------------------------------------------
# The scheduler's call site
# ---------------------------------------------------------------------------

class TestSchedulerIntegration:
    """A scheduler job rather than a task setup hook, for the reason the worktree
    reaper is: `dispatch_setup_env_hooks` calls every skill's hook whatever the
    task selected, so on the setup path a sweep runs before every Talk reply and
    every heartbeat tick."""

    def _config(self, root, tmp_path, **security):
        from istota import db
        from istota.config import Config, SecurityConfig

        config = Config()
        config.db_path = tmp_path / "istota.db"
        db.init_db(config.db_path)
        config.security = SecurityConfig(sandbox_cache_dir=str(root), **security)
        return config

    def test_it_sweeps_the_configured_root(self, tmp_path, toolbox):
        from istota.scheduler import check_sandbox_cache_sweep

        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)

        outcomes = check_sandbox_cache_sweep(self._config(root, tmp_path))

        assert [o.user_id for o in outcomes] == ["alice"]
        assert any("prune" in c for c in toolbox.calls())

    def test_it_honours_the_off_switch(self, tmp_path, toolbox):
        from istota.scheduler import check_sandbox_cache_sweep

        toolbox("uv")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)

        config = self._config(root, tmp_path, sandbox_cache_sweep_enabled=False)

        assert check_sandbox_cache_sweep(config) == []
        assert toolbox.calls() == []

    def test_no_configured_root_means_no_sweep(self, tmp_path, toolbox):
        from istota.scheduler import check_sandbox_cache_sweep

        toolbox("uv")
        assert check_sandbox_cache_sweep(self._config("", tmp_path)) == []
        assert toolbox.calls() == []

    def test_a_user_with_a_running_task_is_skipped(self, tmp_path, toolbox):
        """The busy set has to reach the sweeper, not stop at the query."""
        from istota import db
        from istota.scheduler import check_sandbox_cache_sweep

        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)
        config = self._config(root, tmp_path)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice")
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
            conn.commit()

        outcomes = _by_user(check_sandbox_cache_sweep(config))

        assert outcomes["alice"].action == ACTION_BUSY
        assert toolbox.calls() == []

    def test_an_unreadable_task_table_refuses_to_sweep(self, tmp_path, toolbox, caplog):
        """Fail closed. An empty busy set reads as 'nobody is working', which is
        the one wrong answer that costs a running task its cache."""
        from istota.config import Config, SecurityConfig
        from istota.scheduler import check_sandbox_cache_sweep

        toolbox("uv")
        toolbox("npm")
        root = tmp_path / "caches"
        _cache(root, "alice", uv=4 * MB)
        _age(root, 86400)

        config = Config()
        # A database file with no schema in it: opening succeeds, the query does
        # not — the shape a half-provisioned or corrupted deployment is in.
        config.db_path = tmp_path / "empty.db"
        config.db_path.write_bytes(b"")
        config.security = SecurityConfig(sandbox_cache_dir=str(root))

        with caplog.at_level("WARNING", logger="istota.scheduler"):
            assert check_sandbox_cache_sweep(config) == []

        assert toolbox.calls() == []
        assert "sandbox_cache_sweep_skipped" in caplog.text


# ---------------------------------------------------------------------------
# The one literal shared with another module
# ---------------------------------------------------------------------------

def test_the_cache_subdirectory_names_match_the_executors():
    """The sweeper is a leaf and does not import `executor`; this holds them equal."""
    from istota.executor import SANDBOX_CACHE_NPM, SANDBOX_CACHE_UV

    assert CACHE_UV == SANDBOX_CACHE_UV
    assert CACHE_NPM == SANDBOX_CACHE_NPM
