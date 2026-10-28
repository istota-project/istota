"""ISSUE-880: no task reaches the flat `{mount}/Talk` folder.

That folder is the bot account's Nextcloud `Talk/`, which holds the files of
every conversation the bot is in. A task gets copies of its own attachments in
its control directory instead (`executor.stage_attachments`), and a skill CLI
reads them through `ISTOTA_ATTACHMENTS_DIR`.
"""

from pathlib import Path

import pytest

from istota.config import Config
from istota.sandbox.host_paths import (
    ATTACHMENTS_DIR_VAR,
    env_host_roots,
    resolve_host_path,
)
from istota.sandbox.plan import user_data_mounts


@pytest.fixture
def mount(tmp_path):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    victim = mount / "Talk" / "bobs-private-room" / "private.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("bobs-synthetic-canary")
    return mount


def _nextcloud_config(mount: Path) -> Config:
    config = Config(workspace_path=mount, workspace_mount_path=mount)
    config.nextcloud.url = "https://cloud.example.com"
    assert config.storage_is_nextcloud
    return config


class TestTheMountPlan:
    def test_an_ordinary_task_binds_nothing_under_talk(self, mount):
        from istota.session.tools.env import ToolEnv, ToolPathError

        mounts = user_data_mounts(
            _nextcloud_config(mount), user_id="alice", conversation_token="alice-room",
            user_resources=[], withheld_scopes=frozenset(), group_ids=[],
        )
        talk = mount / "Talk"
        assert not [m for m in mounts if m.source == talk or talk in m.source.parents]
        env = ToolEnv(cwd=mount / "Users" / "alice",
                      read_roots=tuple(m.source for m in mounts), write_roots=())
        with pytest.raises(ToolPathError):
            env.resolve(str(talk / "bobs-private-room" / "private.txt"))


class TestStaging:
    def test_an_over_cap_talk_file_is_not_copied(self, mount, tmp_path):
        from istota.executor import stage_attachments

        config = _nextcloud_config(mount)
        config.talk.attachment_max_mb = 0
        big = mount / "Talk" / "video.mp4"
        big.write_bytes(b"x")
        inbox = mount / "Users" / "alice" / "inbox"
        inbox.mkdir()
        (inbox / "a.pdf").write_bytes(b"%PDF")
        dest = tmp_path / "control" / "room-attachments"
        dest.parent.mkdir()

        out = stage_attachments(
            config, [str(big), str(inbox / "a.pdf")], dest,
            user_id="alice", files_withheld=True,
        )

        # The cap is Talk's; an inbox copy for a restricted task is not held to it.
        assert out == [str(big), str(dest / "a.pdf")]
        assert not (dest / "video.mp4").exists()

    def test_a_talk_file_under_the_cap_is_copied(self, mount, tmp_path):
        from istota.executor import stage_attachments

        config = _nextcloud_config(mount)
        own = mount / "Talk" / "note.txt"
        own.write_bytes(b"hello")
        dest = tmp_path / "control" / "room-attachments"
        dest.parent.mkdir()

        out = stage_attachments(config, [str(own)], dest, user_id="alice")

        assert out == [str(dest / "note.txt")]
        assert (dest / "note.txt").read_bytes() == b"hello"


class TestTheSkillCliRoots:
    @pytest.fixture
    def task_env(self, mount, tmp_path, monkeypatch):
        attachments = tmp_path / ".control" / "alice" / "task_1" / "room-attachments"
        attachments.mkdir(parents=True)
        (attachments / "ticket.pdf").write_bytes(b"%PDF-1.4 ticket")
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "alice-room")
        monkeypatch.setenv(ATTACHMENTS_DIR_VAR, str(attachments))
        return attachments

    def test_another_rooms_talk_file_is_refused(self, mount, task_env):
        resolved, err = resolve_host_path(
            mount / "Talk" / "bobs-private-room" / "private.txt",
            writable=False, operation="read",
        )
        assert resolved is None
        assert err
        assert (mount / "Talk").resolve() not in env_host_roots()

    def test_the_tasks_own_attachment_is_readable(self, task_env):
        resolved, err = resolve_host_path(
            task_env / "ticket.pdf", writable=False, operation="read",
        )
        assert resolved == (task_env / "ticket.pdf").resolve(), err

    def test_the_attachments_dir_is_never_a_write_root(self, task_env):
        assert task_env.resolve() not in env_host_roots(writable=True)
        resolved, _ = resolve_host_path(
            task_env / "new.txt", writable=True, operation="write",
        )
        assert resolved is None

    def test_a_read_whose_bytes_leave_does_not_get_it(self, task_env):
        assert task_env.resolve() not in env_host_roots(attachments=False)
