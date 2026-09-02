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

import pytest

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
        with pytest.raises(RuntimeError):
            ensure_task_control_dir(control_config, "..", 1)

    @pytest.mark.parametrize("level", ["control", "user", "task"])
    def test_it_raises_when_a_level_is_a_symlink(self, control_config, level):
        # `Path.mkdir(exist_ok=True)` swallows `FileExistsError` whenever
        # `is_dir()` says yes, and `is_dir()` follows a symlink — so a symlink
        # to a directory sails through the create and only `O_NOFOLLOW`
        # catches it.
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

        with pytest.raises(RuntimeError):
            ensure_task_control_dir(control_config, "alice", 1)

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

        with pytest.raises(RuntimeError):
            ensure_task_control_dir(control_config, "alice", 1)

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
