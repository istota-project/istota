"""Inbox attachments reach the executor as files it can open.

WhatsApp media and email attachments are copied into the user's inbox and the
task row names them the way the user sees them, `/Users/<uid>/inbox/<name>`.
That is a path in the workspace tree, not on the host, so the executor's two
attachment passes (audio pre-transcription and image preparation) found
nothing at it and skipped every one. The scheduler maps it onto the mount
before execution, for the task's own user only.
"""

import logging
import os
from pathlib import Path
from unittest.mock import patch

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
)
from istota.scheduler import localize_workspace_attachments, process_one_task


def _inbox_file(mount: Path, user_id: str, name: str, data: bytes = b"OggS") -> Path:
    inbox = mount / "Users" / user_id / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_bytes(data)
    return path


def _config(db_path: Path, tmp_path: Path) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="secret",
        ),
        talk=TalkConfig(enabled=False, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        workspace_path=mount,
        temp_dir=tmp_path / "temp",
    )


class TestTheTaskReachesTheExecutorWithAReadableFile:
    def _run(self, config, db_path, *, source_type, attachments):
        seen = {}

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, **kw):
            seen["attachments"] = list(task.attachments or [])
            return (True, "ok", None, None)

        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="The user sent an image with no caption.",
                user_id="alice", source_type=source_type,
                attachments=attachments,
            )
        with patch("istota.scheduler.execute_task", side_effect=fake_exec), \
                patch("istota.scheduler.asyncio.run", return_value=None):
            process_one_task(config)
        return seen["attachments"]

    def test_a_whatsapp_inbox_path_is_mapped_onto_the_mount(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        local = _inbox_file(config.workspace_path, "alice", "whatsapp_x.ogg")

        got = self._run(
            config, db_path, source_type="whatsapp",
            attachments=["/Users/alice/inbox/whatsapp_x.ogg"],
        )

        assert len(got) == 1
        assert os.path.isfile(got[0]), got
        assert Path(got[0]) == local.resolve()

    def test_an_email_inbox_path_is_mapped_too(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        local = _inbox_file(config.workspace_path, "alice", "123_report.pdf", b"%PDF")

        got = self._run(
            config, db_path, source_type="email",
            attachments=["/Users/alice/inbox/123_report.pdf"],
        )

        assert got == [str(local.resolve())]

    def test_a_talk_task_still_goes_through_the_talk_download(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with patch(
            "istota.scheduler.download_talk_attachments",
            return_value=["/downloaded/photo.jpg"],
        ) as talk_dl, patch(
            "istota.scheduler.localize_workspace_attachments",
        ) as localize:
            got = self._run(
                config, db_path, source_type="talk",
                attachments=["Talk/photo.jpg"],
            )

        talk_dl.assert_called_once()
        localize.assert_not_called()
        assert got == ["/downloaded/photo.jpg"]


class TestLocalizeWorkspaceAttachments:
    def test_maps_the_users_own_inbox_file(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        local = _inbox_file(tmp_path, "alice", "whatsapp_a.jpg")

        got = localize_workspace_attachments(
            config, "alice", ["/Users/alice/inbox/whatsapp_a.jpg"],
        )

        assert got == [str(local.resolve())]

    def test_another_users_path_is_never_mapped(self, tmp_path, caplog):
        config = Config(workspace_path=tmp_path)
        _inbox_file(tmp_path, "bob", "secret.pdf")

        entry = "/Users/bob/inbox/secret.pdf"
        got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]

    def test_a_dotdot_path_into_another_user_is_not_mapped(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        _inbox_file(tmp_path, "bob", "secret.pdf")
        (tmp_path / "Users" / "alice" / "inbox").mkdir(parents=True)

        entry = "/Users/alice/../bob/inbox/secret.pdf"
        got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]

    def test_a_symlink_in_the_inbox_pointing_outside_is_not_mapped(
        self, tmp_path, caplog,
    ):
        config = Config(workspace_path=tmp_path)
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("not yours")
        inbox = tmp_path / "Users" / "alice" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "link.txt").symlink_to(outside)

        entry = "/Users/alice/inbox/link.txt"
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]
        assert "link.txt" in caplog.text
        assert str(outside) not in caplog.text

    def test_a_symlink_to_a_file_inside_the_users_tree_is_not_mapped_either(
        self, tmp_path,
    ):
        config = Config(workspace_path=tmp_path)
        target = _inbox_file(tmp_path, "alice", "real.txt")
        (target.parent / "link.txt").symlink_to(target)

        entry = "/Users/alice/inbox/link.txt"
        got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]

    def test_a_symlinked_directory_out_of_the_users_tree_is_not_mapped(
        self, tmp_path,
    ):
        config = Config(workspace_path=tmp_path)
        _inbox_file(tmp_path, "bob", "secret.pdf")
        alice = tmp_path / "Users" / "alice"
        alice.mkdir(parents=True)
        (alice / "inbox").symlink_to(tmp_path / "Users" / "bob" / "inbox")

        entry = "/Users/alice/inbox/secret.pdf"
        got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]

    def test_a_missing_file_is_left_as_is_with_a_warning(self, tmp_path, caplog):
        config = Config(workspace_path=tmp_path)
        (tmp_path / "Users" / "alice" / "inbox").mkdir(parents=True)

        entry = "/Users/alice/inbox/gone.ogg"
        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]
        assert "gone.ogg" in caplog.text
        assert "/Users/alice/inbox" not in caplog.text

    def test_a_directory_is_not_mapped(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        (tmp_path / "Users" / "alice" / "inbox" / "folder").mkdir(parents=True)

        entry = "/Users/alice/inbox/folder"
        got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]

    def test_no_workspace_leaves_entries_untouched(self, tmp_path):
        config = Config(workspace_path=None)
        entry = "/Users/alice/inbox/whatsapp_x.ogg"

        assert localize_workspace_attachments(config, "alice", [entry]) == [entry]

    def test_entries_outside_users_pass_through(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        entries = ["Talk/photo.jpg", "/tmp/some/file.txt"]

        assert localize_workspace_attachments(config, "alice", entries) == entries

    def test_order_is_kept_across_mapped_and_unmapped(self, tmp_path):
        config = Config(workspace_path=tmp_path)
        a = _inbox_file(tmp_path, "alice", "a.jpg")
        b = _inbox_file(tmp_path, "alice", "b.jpg")

        got = localize_workspace_attachments(config, "alice", [
            "/Users/alice/inbox/a.jpg",
            "/Users/alice/inbox/missing.jpg",
            "/Users/alice/inbox/b.jpg",
        ])

        assert got == [
            str(a.resolve()), "/Users/alice/inbox/missing.jpg", str(b.resolve()),
        ]
