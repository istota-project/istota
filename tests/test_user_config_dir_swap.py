"""The user's `{bot_dir}/config` is opened by descriptor, not resolved then named (ISSUE-981).

`{mount}/Users/{uid}` is bound read-write into that user's own sandbox, so a
task can rename `config` (or the bot dir above it) and leave a link to another
user's directory in its place. The daemon used to realpath-check the directory,
return the path, and then open `CRON.md`, `TASKS.md` or `USER.md` by name: a swap
in between wrote one user's jobs over another's and read another user's memory.

Every case swaps at three points: right after `resolve_user_config_dir`
returns (the audit's witness, which the old code fails at), just before the
descriptor walk, and just after it. The two walk arms must have fired, or the
case proves nothing. Bob's files must stay unchanged and unread at all three.

Synthetic users only: alice is the one whose task swaps, bob the other user.
"""

import os
from pathlib import Path

import pytest

from istota import storage
from istota.config import Config, SecurityConfig

BOB_CANARY = "bob-synthetic-private-canary"
BOB_CRON = "# bob's jobs\n"


def _swap_for_link(path: Path, target: Path) -> None:
    os.rename(path, path.with_name(path.name + ".real"))
    path.symlink_to(target, target_is_directory=True)


@pytest.fixture
def ws(tmp_path):
    mount = tmp_path / "mount"
    for user in ("alice", "bob"):
        (mount / "Users" / user / "istota" / "config").mkdir(parents=True)
    bob_cfg = mount / "Users" / "bob" / "istota" / "config"
    (bob_cfg / "CRON.md").write_text(BOB_CRON)
    (bob_cfg / "USER.md").write_text(BOB_CANARY)
    (bob_cfg / "HEARTBEAT.md").write_text(BOB_CANARY)
    (bob_cfg / "BRIEFINGS.md").write_text(
        '```toml\n[[briefings]]\nname = "bob-canary"\ncron = "0 8 * * *"\n```\n')
    dbf = tmp_path / "data" / "istota.db"
    dbf.parent.mkdir(parents=True)
    dbf.touch()
    config = Config(
        db_path=dbf, temp_dir=tmp_path / "temp", workspace_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(sandbox_enabled=True),
    )
    return config, mount


def _paths(mount, which):
    if which == "config":
        return (mount / "Users" / "alice" / "istota" / "config",
                mount / "Users" / "bob" / "istota" / "config")
    return (mount / "Users" / "alice" / "istota", mount / "Users" / "bob" / "istota")


_WALKERS = (
    ("istota.sandbox.confined", "open_dir"),
    ("istota.sandbox.confined", "open_or_create_dirs"),
    ("istota.workspace.config_files", "open_or_create_dirs"),
)


def _arm_swap(monkeypatch, point, alice, bob, resolver=(storage, "resolve_user_config_dir")):
    """Swap ``alice`` for a link to ``bob`` once, at ``point``. Returns a
    callable that fails the test if a walk arm never fired."""
    import importlib

    state = {"fired": False}

    def swap():
        if not state["fired"] and not alice.is_symlink():
            _swap_for_link(alice, bob)
            state["fired"] = True

    def wrap(real, before):
        def wrapped(*args, **kwargs):
            if before:
                swap()
            out = real(*args, **kwargs)
            if not before:
                swap()
            return out
        return wrapped

    if point == "after_resolve":
        # The old window. A resolver the fixed code no longer calls, or no
        # longer imports, leaves this arm a no-op, which is the point.
        module, name = resolver
        real = getattr(module, name, None)
        if real is not None:
            monkeypatch.setattr(module, name, wrap(real, False))
    else:
        for module_name, name in _WALKERS:
            module = importlib.import_module(module_name)
            monkeypatch.setattr(module, name, wrap(getattr(module, name), point == "before_walk"))

    def check():
        if point != "after_resolve":
            assert state["fired"], "the walk was never reached, so nothing was swapped"

    return check


@pytest.fixture(params=["after_resolve", "before_walk", "after_walk"])
def swap_at(request, ws, monkeypatch):
    """Arm a swap of alice's `config` or bot dir for a link to bob's."""
    config, mount = ws
    checks = []

    def arm(which):
        alice, bob = _paths(mount, which)
        checks.append(_arm_swap(monkeypatch, request.param, alice, bob))
        return alice

    arm.point = request.param
    yield arm
    for check in checks:
        check()


def _bob(mount):
    return mount / "Users" / "bob" / "istota" / "config"


@pytest.mark.parametrize("which", ["config", "bot_dir"])
class TestAWriteNeverLandsInAnotherUsersConfig:
    def test_the_cron_md_write(self, ws, swap_at, which):
        from istota import cron_loader

        config, mount = ws
        alice = swap_at(which)

        cron_loader._write_cron_md(config, "alice", [], None, externalize_prompts=False)

        assert (_bob(mount) / "CRON.md").read_text() == BOB_CRON
        if swap_at.point == "after_walk":
            # The descriptor held the directory that passed the walk.
            real = alice.with_name(alice.name + ".real")
            written = real / "CRON.md" if which == "config" else real / "config" / "CRON.md"
            assert written.is_file()

    def test_the_tasks_md_write(self, ws, swap_at, which):
        from istota import tasks_file_poller

        config, mount = ws
        swap_at(which)

        try:
            tasks_file_poller._write_tasks_file(
                config, "/Users/alice/istota/config/TASKS.md",
                "- [ ] synthetic attacker task\n", user_id="alice")
        except PermissionError:
            pass

        assert not (_bob(mount) / "TASKS.md").exists()

    def test_the_seeded_user_md(self, ws, swap_at, which):
        config, mount = ws
        swap_at(which)

        storage.init_user_memory_v2(config, "alice")

        assert (_bob(mount) / "USER.md").read_text() == BOB_CANARY

    def test_the_web_config_editor_save(self, ws, swap_at, which):
        from istota.workspace import config_files, text_files

        config, mount = ws
        file = config_files.lookup(config, "alice", "CRON.md", skills=[])
        swap_at(which)

        config_files.save(config, "alice", file, "# alice's jobs\n", text_files.revision(""))

        assert (_bob(mount) / "CRON.md").read_text() == BOB_CRON


@pytest.mark.parametrize("which", ["config", "bot_dir"])
class TestAReadNeverReturnsAnotherUsersFile:
    @pytest.mark.parametrize("name", ["USER.md", "CRON.md", "HEARTBEAT.md"])
    def test_read_user_config_file(self, ws, swap_at, which, name):
        config, _ = ws
        swap_at(which)

        text = storage.read_user_config_file(config, "alice", name)

        assert text in ("", None)

    def test_the_briefings_import(self, ws, swap_at, which):
        from istota import user_briefings

        config, mount = ws
        swap_at(which)

        pending = user_briefings._read_one_user(config, mount, "istota", "alice")

        assert pending is None or all(e.get("name") != "bob-canary" for e in pending.entries)

    def test_the_web_config_editor_read(self, ws, swap_at, which):
        from istota.workspace import config_files

        config, _ = ws
        file = config_files.lookup(config, "alice", "USER.md", skills=[])
        swap_at(which)

        assert config_files.read(config, "alice", file) in ("", None)

    def test_the_absent_file_check(self, ws, swap_at, which):
        """`job_file_writable` with no document asks whether CRON.md is absent:
        bob's file must not make alice's read as present, nor the reverse."""
        from istota import cron_loader

        config, _ = ws
        swap_at(which)

        # Alice's own folder has no CRON.md; refused before the walk is "not
        # known to be absent", which is the safe answer.
        expected = swap_at.point != "before_walk"
        assert cron_loader.job_file_writable(config, "alice", None) is expected


class TestALinkInsideTheUsersOwnTreeIsRefusedToo:
    """The old rule followed a link that stayed inside the user's tree. A walk
    cannot tell that link from one just repointed, so it refuses both, as the
    overlay directory has since ISSUE-344."""

    def test_an_in_tree_config_link_reads_nothing(self, ws):
        config, mount = ws
        base = mount / "Users" / "alice" / "istota"
        os.rename(base / "config", base / "real_config")
        (base / "real_config" / "USER.md").write_text("alice's own")
        (base / "config").symlink_to(base / "real_config", target_is_directory=True)

        assert storage.read_user_config_file(config, "alice", "USER.md") is None

    def test_a_missing_config_dir_reads_as_absent(self, ws):
        config, mount = ws
        (mount / "Users" / "alice" / "istota" / "config").rmdir()

        assert storage.read_user_config_file(config, "alice", "USER.md") == ""


@pytest.fixture(params=["after_resolve", "before_walk", "after_walk"])
def walk_swap(request, monkeypatch):
    """`swap_at` for a directory other than `config`, and another resolver."""
    checks = []

    def arm(module, resolver, alice, bob):
        checks.append(_arm_swap(monkeypatch, request.param, alice, bob, (module, resolver)))

    arm.point = request.param
    yield arm
    for check in checks:
        check()


class TestTheMemoryCli:
    """The runtime `memory` CLI runs host-side while the task that called it can
    run a background shell, so it is the closest an attacker gets to the window."""

    @pytest.fixture
    def env(self, ws, monkeypatch):
        from istota import db

        config, mount = ws
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        db.init_db(config.db_path)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        (mount / "Users" / "alice" / "istota" / "config" / "USER.md").write_text("## Notes\n")
        (_bob(mount) / "USER.md").write_text("## Notes\n- " + BOB_CANARY + "\n")
        return mount

    @pytest.mark.parametrize("verb", [["show"], ["append", "--heading", "Notes", "--line", "attacker"]])
    def test_neither_read_nor_write_reaches_another_users_user_md(
        self, env, walk_swap, monkeypatch, capsys, verb,
    ):
        import istota.skills.memory as mem

        mount = env
        walk_swap(mem, "_user_md_path",
                  mount / "Users" / "alice" / "istota" / "config", _bob(mount))

        try:
            mem.main(verb)
        except SystemExit:
            pass

        assert BOB_CANARY not in capsys.readouterr().out
        assert (_bob(mount) / "USER.md").read_text() == "## Notes\n- " + BOB_CANARY + "\n"


class TestThePlaybookPrune:
    def test_another_users_old_playbook_is_never_deleted(self, ws, walk_swap):
        """Before ISSUE-981 the window was between `_contained_under_user_root`
        and the unlink by name; the prune calls no such check now."""
        import time as _time

        from istota.memory import sleep_cycle

        config, mount = ws
        alice_pb = mount / "Users" / "alice" / "istota" / "playbooks"
        bob_pb = mount / "Users" / "bob" / "istota" / "playbooks"
        for directory in (alice_pb, bob_pb):
            directory.mkdir(parents=True)
            (directory / sleep_cycle._RETENTION_SENTINEL).write_text("2026-01-01")
        victim = bob_pb / "old.md"
        victim.write_text("bob's playbook\n")
        past = _time.time() - 400 * 86400
        os.utime(victim, (past, past))
        walk_swap(sleep_cycle, "_contained_under_user_root", alice_pb, bob_pb)

        sleep_cycle.cleanup_old_playbooks(config, "alice", 30)

        assert victim.exists()


def _old_dated_memory(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2020-01-01.md"
    path.write_text(text)
    return path


class TestDatedMemories:
    """`memories/` sits in the same task-writable tree; a swap there put another
    user's dated memories into this user's prompt and index, and the nightly
    cleanup deleted theirs."""

    def _dirs(self, mount):
        return mount / "Users" / "alice" / "memories", mount / "Users" / "bob" / "memories"

    def test_the_prompt_never_loads_another_users(self, ws, walk_swap, monkeypatch):
        from datetime import datetime

        config, mount = ws
        alice, bob = self._dirs(mount)
        alice.mkdir(parents=True)
        bob.mkdir(parents=True)
        (bob / f"{datetime.now():%Y-%m-%d}.md").write_text(BOB_CANARY)
        monkeypatch.setattr(config, "resolve_user_timezone", lambda _u: "UTC", raising=False)
        walk_swap(storage, "_get_mount_path", alice, bob)

        text = storage.read_dated_memories(config, "alice", max_days=7)

        assert not text or BOB_CANARY not in text

    def test_the_cleanup_never_deletes_another_users(self, ws, walk_swap, monkeypatch):
        from istota.memory import sleep_cycle

        config, mount = ws
        alice, bob = self._dirs(mount)
        _old_dated_memory(alice, "alice's old")
        victim = _old_dated_memory(bob, BOB_CANARY)
        monkeypatch.setattr(config, "resolve_user_timezone", lambda _u: "UTC", raising=False)
        walk_swap(sleep_cycle, "_get_mount_path", alice, bob)

        sleep_cycle.cleanup_old_memory_files(config, "alice", 30)

        assert victim.read_text() == BOB_CANARY

    def test_a_rooms_cleanup_never_deletes_another_rooms(self, ws, walk_swap):
        from istota.memory import sleep_cycle

        config, mount = ws
        mine = mount / "Channels" / "room123" / "memories"
        other = mount / "Channels" / "room456" / "memories"
        _old_dated_memory(mine, "this room's old")
        victim = _old_dated_memory(other, BOB_CANARY)
        walk_swap(sleep_cycle, "_get_mount_path", mine, other)

        sleep_cycle.cleanup_old_channel_memory_files(config, "room123", 30)

        assert victim.read_text() == BOB_CANARY

    def test_the_reindex_never_indexes_another_users(self, ws, walk_swap):
        from istota import db
        from istota.memory import search

        config, mount = ws
        alice, bob = self._dirs(mount)
        _old_dated_memory(alice, "alice's own")
        _old_dated_memory(bob, BOB_CANARY)
        db.init_db(config.db_path)
        walk_swap(search, "scoped_user_dir", alice, bob)

        from unittest.mock import patch

        with db.get_db(config.db_path) as conn, \
                patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.enable_vec_extension", return_value=False):
            search.reindex_all(conn, config, "alice", lookback_days=1)
            rows = conn.execute(
                "SELECT content FROM memory_chunks WHERE user_id = 'alice'"
            ).fetchall()

        assert not any(BOB_CANARY in r[0] for r in rows)


class TestTheMemoryCliCreatePath:
    def test_a_directory_renamed_away_mid_walk_writes_nothing_anywhere(
        self, ws, monkeypatch, tmp_path,
    ):
        """A `config` renamed away between the `mkdir` and the open answers
        FileNotFoundError; that must refuse, not fall back to writing relative
        to the CLI's working directory."""
        import istota.skills.memory as mem
        from istota import db
        from istota.sandbox import confined

        config, mount = ws
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        db.init_db(config.db_path)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        def vanished(*_a, **_k):
            raise FileNotFoundError("renamed away")

        monkeypatch.setattr(confined, "open_or_create_dirs", vanished)
        with pytest.raises(SystemExit):
            mem.main(["append", "--heading", "Notes", "--line", "x"])

        assert not (cwd / "USER.md").exists()


class TestARefusedDirectoryIsNotAnAbsentFile:
    def test_a_job_is_not_called_gone_when_the_directory_cannot_be_opened(self, ws):
        """`remove_job_definition`'s fallback: only a CRON.md known to be absent
        means the file no longer names the job."""
        from istota import cron_loader

        config, mount = ws
        base = mount / "Users" / "alice" / "istota"
        (base / "config" / "CRON.md").write_text(
            '```toml\n[[jobs]]\nname = "daily"\ncron = "0 9 * * *"\nprompt = "hi"\n```\n')
        os.rename(base / "config", base / "real_config")
        (base / "config").symlink_to(base / "real_config", target_is_directory=True)

        assert cron_loader.remove_job_definition(config, "alice", "daily") is False
