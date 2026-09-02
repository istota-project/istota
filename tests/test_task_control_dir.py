"""The daemon-owned per-task control directory resolver.

`get_task_control_dir` names `{temp_dir}/.control/{user_id}/task_{id}` and
`ensure_task_control_dir` creates it 0700 the whole way down. Stage 1 of the
task-control-dir spec: no caller yet, so everything asserted here is asserted
against the two functions directly.

The refusals are the point. A control directory that resolves somewhere else
is the exposure this whole change exists to close, so each of the four ways a
`user_id` can escape the root gets its own case, and each of the two ways a
level can fail to be a real directory gets its own case as well.
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import executor
from istota.config import Config
from istota.executor import (
    CONTROL_DIR_NAME,
    ensure_task_control_dir,
    get_task_control_dir,
    get_user_temp_dir,
)


@pytest.fixture
def control_config(tmp_path):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    return Config(
        db_path=tmp_path / "data" / "istota.db",
        temp_dir=temp_dir,
        skills_dir=tmp_path / "skills",
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _refused_by(error: RuntimeError) -> str:
    """Which of the two mechanisms produced this ``RuntimeError``.

    ``ensure_task_control_dir`` raises for two unrelated reasons and the
    distinction is the whole point of several cases below: the resolver
    returning ``None`` (its containment equality resolved through a bad level)
    versus ``_ensure_control_level`` refusing an inode it opened. A bare
    ``pytest.raises(RuntimeError)`` cannot tell them apart, which is how a
    parametrization ends up claiming to exercise a guard that ran in one of
    its three cases.
    """
    text = str(error)
    if text.startswith("cannot name a task control directory"):
        return "resolver"
    return "ensure"


class TestThePathShape:
    def test_it_names_the_documented_path(self, control_config):
        control_dir = get_task_control_dir(control_config, "alice", 42)

        assert control_dir == (
            control_config.temp_dir.resolve() / CONTROL_DIR_NAME / "alice" / "task_42"
        )

    def test_the_returned_path_is_resolved(self, tmp_path):
        # The host path and the in-namespace path must be one string, and
        # `_ro_bind` uses the string it is handed as the destination. So a
        # deployment whose temp root sits behind a symlink must still get a
        # resolved answer.
        real = tmp_path / "real-temp"
        real.mkdir()
        link = tmp_path / "temp"
        link.symlink_to(real)
        config = Config(
            db_path=tmp_path / "data" / "istota.db",
            temp_dir=link,
            skills_dir=tmp_path / "skills",
        )

        control_dir = get_task_control_dir(config, "alice", 7)

        assert control_dir == real / CONTROL_DIR_NAME / "alice" / "task_7"

    def test_it_creates_nothing(self, control_config):
        get_task_control_dir(control_config, "alice", 1)

        assert not (control_config.temp_dir / CONTROL_DIR_NAME).exists()

    def test_it_is_not_under_the_user_temp_dir(self, control_config):
        # An equality on resolved paths rather than a `startswith`: the claim
        # is that the control root is a *sibling* of every per-user directory,
        # which is what makes it unreachable from a model-writable parent.
        for user_id in ("alice", "bob", "a.control"):
            control_dir = get_task_control_dir(control_config, user_id, 3)
            user_temp = get_user_temp_dir(control_config, user_id).resolve()

            assert control_dir is not None
            assert user_temp not in control_dir.parents
            assert control_dir != user_temp

    def test_task_zero_is_a_legal_name(self, control_config):
        # The heartbeat's synthetic task. No special case, deliberately.
        control_dir = get_task_control_dir(control_config, "alice", 0)

        assert control_dir is not None
        assert control_dir.name == "task_0"


class TestTheFourRefusals:
    @pytest.mark.parametrize(
        "user_id, why",
        [
            ("", "empty"),
            ("..", "parent"),
            ("/etc", "absolute"),
            (CONTROL_DIR_NAME, "the control root's own name"),
        ],
    )
    def test_it_refuses(self, control_config, user_id, why):
        assert get_task_control_dir(control_config, user_id, 1) is None, why

    def test_it_refuses_a_nested_user_id(self, control_config):
        assert get_task_control_dir(control_config, "alice/bob", 1) is None

    def test_it_refuses_a_dot_user_id(self, control_config):
        # `PurePath` drops `.` outright, so the join would collapse to the
        # control root itself.
        assert get_task_control_dir(control_config, ".", 1) is None

    @pytest.mark.parametrize("user_id", [".Control", ".CONTROL"])
    def test_it_refuses_the_control_name_whatever_its_case(
        self, control_config, user_id
    ):
        # A case-sensitive equality is defeated by the shift key on any
        # case-insensitive filesystem — where `{temp}/.CONTROL`, the
        # model-writable scratch directory `get_user_temp_dir` would hand that
        # user, *is* the control root.
        assert get_task_control_dir(control_config, user_id, 1) is None

    @pytest.mark.parametrize("task_id", ["1/../../..", "../escape", "1/nested"])
    def test_it_refuses_a_task_id_that_would_escape(self, control_config, task_id):
        # The containment equality covers the `user_id` component and stops
        # there, `PurePath` does not collapse `..`, and the kernel resolves it
        # at `mkdir`. `int()` is what keeps the last component a leaf.
        assert get_task_control_dir(control_config, "alice", task_id) is None

    def test_a_task_id_that_is_a_numeric_string_is_accepted_as_a_number(
        self, control_config
    ):
        # The coercion is a containment check, not a type check: a caller
        # handing over "42" means task 42 and gets task_42, not task_"42".
        assert get_task_control_dir(control_config, "alice", "42") == (
            get_task_control_dir(control_config, "alice", 42)
        )

    @pytest.mark.parametrize("user_id", [5, b"alice", object()])
    def test_it_never_raises_on_a_user_id_of_the_wrong_type(
        self, control_config, user_id
    ):
        # "Never raises" is the contract, and `Path.__truediv__` raises
        # `TypeError` rather than one of the two the join used to catch.
        assert get_task_control_dir(control_config, user_id, 1) is None

    def test_it_never_raises_on_a_user_id_carrying_a_nul(self, control_config):
        assert get_task_control_dir(control_config, "ali\x00ce", 1) is None

    def test_it_refuses_a_user_id_that_is_a_symlink_out_of_the_root(
        self, control_config
    ):
        root = control_config.temp_dir / CONTROL_DIR_NAME
        root.mkdir()
        elsewhere = control_config.temp_dir.parent / "elsewhere"
        elsewhere.mkdir()
        (root / "alice").symlink_to(elsewhere)

        assert get_task_control_dir(control_config, "alice", 1) is None

    def test_a_relative_temp_root_is_resolved_rather_than_refused(self, tmp_path):
        # `Path("")` is `Path(".")`, which is truthy, and a relative
        # `temp_dir` is resolved against the daemon's cwd — the same thing
        # `get_user_temp_dir` does with the same value. Asserted so that
        # anyone who later decides a relative root should be refused finds
        # this case rather than a silent behaviour change. Never raises
        # either way, which is the contract.
        config = Config(
            db_path=tmp_path / "data" / "istota.db",
            temp_dir=Path(""),
            skills_dir=tmp_path / "skills",
        )

        control_dir = get_task_control_dir(config, "alice", 1)

        assert control_dir == (
            Path.cwd() / CONTROL_DIR_NAME / "alice" / "task_1"
        )


class TestEnsure:
    def test_it_creates_all_three_levels_at_0700(self, control_config):
        control_dir = ensure_task_control_dir(control_config, "alice", 42)

        assert control_dir == get_task_control_dir(control_config, "alice", 42)
        assert control_dir.is_dir()
        assert _mode(control_dir) == 0o700
        assert _mode(control_dir.parent) == 0o700
        assert _mode(control_dir.parent.parent) == 0o700
        assert control_dir.parent.parent.name == CONTROL_DIR_NAME

    def test_it_is_idempotent(self, control_config):
        first = ensure_task_control_dir(control_config, "alice", 42)
        (first / "kept").write_text("x")

        second = ensure_task_control_dir(control_config, "alice", 42)

        assert second == first
        assert (second / "kept").read_text() == "x"

    def test_it_is_idempotent_under_real_concurrency(self, control_config):
        """Idempotent *concurrently*, not merely when called twice in a row.

        Stage 1 asserted the claim sequentially, which is the cheap half:
        every level already existed by the time the second call ran, so no
        two callers were ever inside `mkdir` at once. Stage 2b is where the
        second caller arrives — `_build_module_briefing_prompt` calls this
        again underneath `execute_task` — and the shape that matters is two
        *tasks of one user* on two worker threads, which share the control
        root and the per-user level and race to create both.

        A barrier rather than a plain thread start: without it the threads
        serialize on interpreter startup and the test passes against an
        implementation that is not safe at all.
        """
        import threading

        users = ("alice", "bob")
        task_ids = (1, 2, 3, 4)
        workers = [(u, t) for u in users for t in task_ids for _ in range(3)]
        barrier = threading.Barrier(len(workers))
        results: dict[int, Path] = {}
        failures: list[BaseException] = []
        lock = threading.Lock()

        def _run(index: int, user_id: str, task_id: int) -> None:
            try:
                barrier.wait(timeout=10)
                path = ensure_task_control_dir(control_config, user_id, task_id)
                # Write from inside the race, so a level re-created by a
                # neighbour after this one returned would lose the file.
                (path / f"witness_{index}").write_text("x")
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    failures.append(exc)
            else:
                with lock:
                    results[index] = path

        threads = [
            threading.Thread(target=_run, args=(i, u, t))
            for i, (u, t) in enumerate(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not failures, f"concurrent callers raised: {failures!r}"
        assert len(results) == len(workers)
        for index, (user_id, task_id) in enumerate(workers):
            expected = get_task_control_dir(control_config, user_id, task_id)
            assert results[index] == expected
            assert (expected / f"witness_{index}").read_text() == "x"
            assert _mode(expected) == 0o700
            assert _mode(expected.parent) == 0o700
        assert _mode(control_config.temp_dir / CONTROL_DIR_NAME) == 0o700

    @pytest.mark.parametrize("level", ["control", "user", "task"])
    def test_it_re_asserts_the_mode_on_an_existing_directory(
        self, control_config, level
    ):
        # `mkdir(exist_ok=True)` leaves an existing directory's mode alone, so
        # a directory widened by hand (or by a umask the daemon did not have
        # when it first ran) would stay widened for ever.
        control_dir = ensure_task_control_dir(control_config, "alice", 42)
        widened = {
            "control": control_dir.parent.parent,
            "user": control_dir.parent,
            "task": control_dir,
        }[level]
        os.chmod(widened, 0o755)

        ensure_task_control_dir(control_config, "alice", 42)

        assert _mode(widened) == 0o700

    def test_it_raises_when_the_user_id_does_not_resolve(self, control_config):
        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "..", 1)

        assert _refused_by(excinfo.value) == "resolver"

    def test_it_leaves_the_shared_temp_root_alone(self, control_config):
        # `mkdir` applies its mode to the leaf only, so 0700 lands on
        # `.control` and the shared root keeps whatever mode it had. Pinned
        # because nothing else in this file asserts anything about the root,
        # so a later change that tightened it would go unnoticed either way.
        os.chmod(control_config.temp_dir, 0o755)

        ensure_task_control_dir(control_config, "alice", 1)

        assert _mode(control_config.temp_dir) == 0o755

    def test_it_creates_the_shared_temp_root_when_it_is_missing(self, tmp_path):
        # `parents=True` on the first level exists for this: nothing
        # guarantees the daemon has written to `temp_dir` before the first
        # task of a boot.
        config = Config(
            db_path=tmp_path / "data" / "istota.db",
            temp_dir=tmp_path / "temp" / "nested",
            skills_dir=tmp_path / "skills",
        )

        control_dir = ensure_task_control_dir(config, "alice", 1)

        assert control_dir.is_dir()
        assert _mode(control_dir.parent.parent) == 0o700

    @pytest.mark.parametrize("level", ["control", "user", "task"])
    def test_it_raises_when_a_level_is_a_symlink(self, control_config, level):
        root = control_config.temp_dir / CONTROL_DIR_NAME
        elsewhere = control_config.temp_dir.parent / "elsewhere"
        elsewhere.mkdir()
        if level == "control":
            root.symlink_to(elsewhere)
        elif level == "user":
            root.mkdir()
            (root / "alice").symlink_to(elsewhere)
        else:
            (root / "alice").mkdir(parents=True)
            (root / "alice" / "task_1").symlink_to(elsewhere)

        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "alice", 1)

        # Which mechanism refused matters, and a bare `raises(RuntimeError)`
        # cannot tell them apart. The resolver's containment equality resolves
        # *through* the control root and the user level, so those two never
        # reach `_ensure_control_level` at all; only `task_1` gets that far,
        # because the resolver deliberately leaves the last component
        # unresolved. Pinning the producer is what stops this file reporting
        # three cases of a guard that runs in one of them.
        assert _refused_by(excinfo.value) == (
            "resolver" if level in ("control", "user") else "ensure"
        )

    def test_only_the_task_level_depends_on_o_nofollow(self, control_config):
        # The companion to the case above, stated as a property rather than
        # left implicit in the parametrization: the negative control that
        # deletes `O_NOFOLLOW` turns the task level red and the other two
        # stay green, because a different mechanism already covered them.
        root = control_config.temp_dir / CONTROL_DIR_NAME
        (root / "alice").mkdir(parents=True)
        elsewhere = control_config.temp_dir.parent / "elsewhere"
        elsewhere.mkdir()
        (root / "alice" / "task_1").symlink_to(elsewhere)

        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "alice", 1)

        assert _refused_by(excinfo.value) == "ensure"
        # And the symlink target was not touched: `fchmod` acts on the
        # descriptor, and the open never happened.
        assert _mode(elsewhere) != 0o700

    def test_it_raises_on_a_dangling_symlink_at_the_task_level(self, control_config):
        # A different path through `_ensure_control_level` from the
        # symlink-to-a-directory case: `mkdir` still raises `FileExistsError`
        # (the name exists), and the open fails ELOOP rather than ENOTDIR.
        root = control_config.temp_dir / CONTROL_DIR_NAME
        (root / "alice").mkdir(parents=True)
        (root / "alice" / "task_1").symlink_to(
            control_config.temp_dir / "nothing-here"
        )

        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "alice", 1)

        assert _refused_by(excinfo.value) == "ensure"

    def test_it_raises_on_a_fifo_at_the_task_level(self, control_config):
        # O_DIRECTORY is what keeps this from blocking on the O_RDONLY open
        # of a FIFO with no writer.
        root = control_config.temp_dir / CONTROL_DIR_NAME
        (root / "alice").mkdir(parents=True)
        os.mkfifo(root / "alice" / "task_1")

        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "alice", 1)

        assert _refused_by(excinfo.value) == "ensure"

    @pytest.mark.parametrize("level", ["control", "user", "task"])
    def test_it_raises_when_a_level_is_a_regular_file(self, control_config, level):
        root = control_config.temp_dir / CONTROL_DIR_NAME
        if level == "control":
            root.write_text("not a directory")
        elif level == "user":
            root.mkdir()
            (root / "alice").write_text("not a directory")
        else:
            (root / "alice").mkdir(parents=True)
            (root / "alice" / "task_1").write_text("not a directory")

        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "alice", 1)

        # Every level, unlike the symlink case above. `Path.resolve()` is
        # non-strict, so it returns the lexical path even when an ancestor is
        # a regular file — the containment equality holds and the resolver
        # passes it through. `O_DIRECTORY` is what refuses it, at all three
        # levels, with ENOTDIR.
        assert _refused_by(excinfo.value) == "ensure"

    def test_it_refuses_a_level_owned_by_another_uid(self, control_config):
        # A type check says the level is a directory, not that it is ours.
        # `Config.temp_dir` defaults under world-writable `/tmp`, so a
        # pre-created level is a reachable shape on the standalone install.
        # The uid is faked rather than the directory, because the suite does
        # not run as root and cannot chown one away.
        real_fstat = os.fstat

        def _alien_owner(fd):
            st = real_fstat(fd)
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                 os.geteuid() + 1, st.st_gid, st.st_size,
                 int(st.st_atime), int(st.st_mtime), int(st.st_ctime))
            )

        with patch("istota.executor.os.fstat", _alien_owner):
            with pytest.raises(RuntimeError) as excinfo:
                ensure_task_control_dir(control_config, "alice", 1)

        assert _refused_by(excinfo.value) == "ensure"
        assert "owned by uid" in str(excinfo.value)

    def test_it_retries_once_when_the_sweep_collects_a_level_underneath_it(
        self, control_config
    ):
        # `cleanup_old_temp_files` recurses into every subdirectory of
        # `temp_dir` — `.control` included — and rmdirs an empty directory
        # past the retention window. Neither `mkdir` on an existing directory
        # nor `fchmod` updates an mtime, so a per-user level can be collected
        # while this function is walking down through it, and the next
        # `mkdir(parents=False)` would fail a task for it.
        root = control_config.temp_dir / CONTROL_DIR_NAME
        real_ensure = executor._ensure_control_level
        collected = []

        def _sweeping(path, *, parents):
            real_ensure(path, parents=parents)
            # Collect the user level exactly once, immediately after it is
            # created and before the task level is made inside it.
            if path == root / "alice" and not collected:
                collected.append(path)
                path.rmdir()

        with patch("istota.executor._ensure_control_level", _sweeping):
            control_dir = ensure_task_control_dir(control_config, "alice", 1)

        assert collected, "the sweep stand-in never fired; the test proves nothing"
        assert control_dir.is_dir()
        assert _mode(control_dir) == 0o700
        assert _mode(control_dir.parent) == 0o700

    def test_it_gives_up_when_a_level_keeps_disappearing(self, control_config):
        # One retry, not a loop. A level that vanishes every time is not a
        # sweep race and must not spin.
        root = control_config.temp_dir / CONTROL_DIR_NAME
        real_ensure = executor._ensure_control_level

        def _always_sweeping(path, *, parents):
            real_ensure(path, parents=parents)
            if path == root / "alice":
                path.rmdir()

        with patch("istota.executor._ensure_control_level", _always_sweeping):
            with pytest.raises(RuntimeError) as excinfo:
                ensure_task_control_dir(control_config, "alice", 1)

        assert "kept disappearing" in str(excinfo.value)

    def test_the_error_names_the_path_and_carries_no_contents(self, control_config):
        root = control_config.temp_dir / CONTROL_DIR_NAME
        root.write_text("a secret the daemon wrote")

        with pytest.raises(RuntimeError) as excinfo:
            ensure_task_control_dir(control_config, "alice", 1)

        assert str(root) in str(excinfo.value)
        assert "a secret the daemon wrote" not in str(excinfo.value)


class TestItIsNotReachableFromTheModelsDirectory:
    def test_the_control_root_is_a_sibling_of_the_per_user_directories(
        self, control_config
    ):
        control_dir = ensure_task_control_dir(control_config, "alice", 42)
        user_temp = get_user_temp_dir(control_config, "alice")
        user_temp.mkdir(parents=True)

        # The whole design in one assertion: no ancestor of the control
        # directory is a directory bound read-write into the sandbox.
        assert user_temp.resolve() not in control_dir.parents
        assert control_dir.parents[2] == control_config.temp_dir.resolve()


class TestTheNameIsRestatedInOnePlaceAndHeldEqual:
    """`config.py` cannot import the executor, so it restates the name.

    `load_config` runs in the daemon, the web app, the webhook receiver, every
    CLI invocation and every host-side skill CLI the proxy spawns per call —
    importing `istota.executor` there would put that whole graph on all of
    them for one string. The copy is the same trade `sandbox_cache_sweeper`
    makes for the cache subdirectory names, and it is only safe with the two
    held equal: a drift would leave `_warn_ro_paths_over_control_tree`
    checking a directory nothing writes to, silently passing every broad
    entry it exists to catch.
    """

    def test_the_config_copy_matches_the_executor_constant(self):
        from istota.config import _CONTROL_DIR_NAME

        assert _CONTROL_DIR_NAME == CONTROL_DIR_NAME
