"""A restricted task's temp dir lives where no task can write (ISSUE-980).

A guest's turn, a correspondent's turn and a task nobody asked in a shared room
each get a temp dir of their own. It used to be `{temp}/{user}/room-task-<id>`,
inside the per-user directory every ordinary task of that user binds read-write,
and it was checked with `is_symlink()`, made with `mkdir` and bound by name
seconds later. A concurrent ordinary task could swap it for a link to another
user's temp dir in between and have that directory bound into the restricted
task. It now lives under `{temp}/.restricted/{user}/`, which no namespace binds.

Synthetic users only: alice is the one with two tasks, bob the other user.
The `linux` tier runs the swap against a real bwrap:
`tests/linux/test_restricted_temp_dir_swap.py`.
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SecurityConfig


@pytest.fixture
def config(tmp_path):
    mount = tmp_path / "mount"
    for user in ("alice", "bob"):
        (mount / "Users" / user).mkdir(parents=True)
    (mount / "Channels" / "room123").mkdir(parents=True)
    dbf = tmp_path / "data" / "istota.db"
    dbf.parent.mkdir(parents=True)
    dbf.touch()
    return Config(
        db_path=dbf, temp_dir=tmp_path / "temp", workspace_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(sandbox_enabled=True),
    )


def _task(task_id=42, *, user="alice", guest=None, source_type="email"):
    return db.Task(
        id=task_id, prompt="x", user_id=user, source_type=source_type,
        status="running", conversation_token="room123",
        guest_participant_id=guest,
    )


def _argv(config, task, temp, withheld=frozenset()):
    from istota.executor import SandboxProfile, build_bwrap_cmd

    with patch("istota.executor._bwrap_available", return_value=True), \
         patch("istota.executor._bwrap_fd_bind_binary", return_value="/usr/bin/bwrap"):
        return build_bwrap_cmd(
            ["true"], config, task, False, [], temp,
            profile=SandboxProfile.NATIVE, withheld_scopes=withheld,
        )


def _rw_sources(argv):
    return [Path(argv[i + 1]) for i, tok in enumerate(argv) if tok == "--bind"]


def _bob_temp(config):
    bob = config.temp_dir / "bob"
    bob.mkdir(parents=True)
    (bob / "task_9001_kv_ops.json").write_text("[]")
    return bob


@pytest.mark.parametrize("guest", [None, 5], ids=["restricted", "guest"])
class TestWhereTheDirectoryLives:
    def test_no_ordinary_task_of_the_same_user_can_write_its_parent(self, config, guest):
        from istota.executor import get_user_temp_dir, task_temp_dir

        own = task_temp_dir(config, _task(guest=guest), restricted=True)
        get_user_temp_dir(config, "alice").mkdir(parents=True)
        ordinary = _argv(config, _task(41, source_type="talk"), get_user_temp_dir(config, "alice"))

        for source in _rw_sources(ordinary):
            assert not own.resolve().is_relative_to(source), (
                f"{own} is under {source}, which alice's ordinary task binds read-write"
            )

    def test_a_link_planted_at_the_old_name_is_not_what_gets_bound(self, config, guest):
        """The audit's witness, turned round: alice's other task swaps the
        directory it can write for a link to bob's temp dir after the check."""
        from istota.executor import ensure_task_temp_dir, get_user_temp_dir

        bob = _bob_temp(config)
        task = _task(guest=guest)
        own = ensure_task_temp_dir(config, task, restricted=True)
        prefix = "emissary-task-" if guest else "room-task-"
        planted = get_user_temp_dir(config, "alice") / f"{prefix}{task.id}"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.symlink_to(bob, target_is_directory=True)

        argv = _argv(config, task, own, withheld=frozenset({"files", "memory"}))

        assert str(bob.resolve()) not in argv
        assert own.resolve() in _rw_sources(argv)


class TestTheOldNameIsIgnored:
    def test_a_directory_planted_there_does_not_become_an_ordinary_tasks_temp_dir(self, config):
        """Before, a `room-task-<id>` there made the next unrestricted task with
        that id bind it, by name, after the same check-then-mkdir."""
        from istota.executor import get_user_temp_dir, task_deferred_dir, task_temp_dir

        base = get_user_temp_dir(config, "alice")
        (base / "room-task-43").mkdir(parents=True)
        task = _task(43, source_type="talk")

        assert task_temp_dir(config, task, restricted=False) == base
        assert task_deferred_dir(config, task) == base

    def test_a_restricted_run_is_still_found_by_a_later_unrestricted_attempt(self, config):
        from istota.executor import ensure_task_temp_dir, task_deferred_dir, task_temp_dir

        task = _task(44)
        own = ensure_task_temp_dir(config, task, restricted=True)

        assert task_temp_dir(config, task, restricted=False) == own
        assert task_deferred_dir(config, task) == own


class TestMakingIt:
    def test_every_level_is_the_daemons_and_0700(self, config):
        from istota.executor import RESTRICTED_TEMP_DIR_NAME, ensure_task_temp_dir

        own = ensure_task_temp_dir(config, _task(), restricted=True)

        assert own.parent.parent == config.temp_dir.resolve() / RESTRICTED_TEMP_DIR_NAME
        for level in (own, own.parent, own.parent.parent):
            st = os.lstat(level)
            assert stat.S_ISDIR(st.st_mode)
            assert stat.S_IMODE(st.st_mode) == 0o700
            assert st.st_uid == os.geteuid()

    def test_a_link_at_the_leaf_is_refused(self, config):
        from istota.executor import ensure_task_temp_dir, task_temp_dir

        bob = _bob_temp(config)
        task = _task()
        own = task_temp_dir(config, task, restricted=True)
        own.parent.mkdir(parents=True)
        own.symlink_to(bob, target_is_directory=True)

        with pytest.raises(RuntimeError):
            ensure_task_temp_dir(config, task, restricted=True)

    def test_an_unrestricted_task_gets_the_per_user_dir(self, config):
        from istota.executor import ensure_task_temp_dir, get_user_temp_dir

        own = ensure_task_temp_dir(config, _task(source_type="talk"), restricted=False)

        assert own == get_user_temp_dir(config, "alice")
        assert own.is_dir()

    @pytest.mark.parametrize("user", [".restricted", ".Restricted"])
    def test_a_user_id_naming_the_root_is_refused(self, config, user):
        from istota.executor import ensure_task_temp_dir, get_task_control_dir

        with pytest.raises((RuntimeError, ValueError)):
            ensure_task_temp_dir(config, _task(user=user), restricted=True)
        # And that user's ordinary temp dir would be the root itself, so the
        # control dir every task needs refuses them too.
        assert get_task_control_dir(config, user, 1) is None


class TestWhenTheRootIsNotTheDaemons:
    def test_a_guests_drain_never_reads_the_hosts_per_user_dir(self, config):
        """A `.restricted` root that became a link between the run and the drain
        names nothing for a guest, rather than falling back to the host's dir."""
        from istota.executor import get_user_temp_dir, task_deferred_dir

        elsewhere = config.temp_dir.parent / "elsewhere"
        elsewhere.mkdir()
        config.temp_dir.mkdir()
        (config.temp_dir / ".restricted").symlink_to(elsewhere, target_is_directory=True)

        found = task_deferred_dir(config, _task(guest=5))

        assert found != get_user_temp_dir(config, "alice")
        assert not found.is_relative_to(get_user_temp_dir(config, "alice"))


class TestTheConfigWarning:
    @pytest.fixture(autouse=True)
    def _clear_the_latch(self):
        from istota.config import _RO_PATH_CONTROL_TREE_WARNED

        _RO_PATH_CONTROL_TREE_WARNED.clear()
        yield
        _RO_PATH_CONTROL_TREE_WARNED.clear()

    def test_a_sandbox_ro_path_naming_the_restricted_root_is_warned_about(self, tmp_path, caplog):
        from istota.config import load_config

        p = tmp_path / "config.toml"
        p.write_text(
            f'temp_dir = "{tmp_path}/tmp"\n'
            "[security]\n"
            "sandbox_enabled = true\n"
            f'sandbox_ro_paths = ["{tmp_path}/tmp/.restricted"]\n'
        )
        with caplog.at_level("WARNING", logger="istota.config"):
            load_config(p)

        assert any("restricted task temp dirs" in r.getMessage() for r in caplog.records)

    def test_the_config_copy_matches_the_executor_constant(self):
        from istota.config import _RESTRICTED_TEMP_DIR_NAME
        from istota.executor import RESTRICTED_TEMP_DIR_NAME

        assert _RESTRICTED_TEMP_DIR_NAME == RESTRICTED_TEMP_DIR_NAME
