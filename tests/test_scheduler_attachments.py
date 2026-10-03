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

import pytest

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

    def test_a_host_path_under_a_users_shaped_workspace_passes_untouched(
        self, tmp_path, caplog,
    ):
        # A macOS standalone install keeps its workspace under the OS user's
        # home, so a web upload's real path also starts with /Users/<uid>/.
        workspace = tmp_path / "Users" / "alice" / ".istota"
        upload = workspace / "Users" / "alice" / "inbox" / "web-chat" / "x.png"
        upload.parent.mkdir(parents=True)
        upload.write_bytes(b"png")
        config = Config(workspace_path=workspace, temp_dir=tmp_path / "temp")
        entry = str(upload)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), \
                patch("istota.scheduler.BOT_USER_BASE", str(tmp_path / "Users")):
            got = localize_workspace_attachments(config, "alice", [entry])

        assert got == [entry]
        assert "x.png" not in caplog.text


class TestATranscribedWhatsAppVoiceNoteLeavesTheInbox:
    """ISSUE-611: a WhatsApp voice note is deleted once it has done its job.

    The executor reports which audio files produced a transcript on
    `task.transcribed_audio`; the scheduler deletes those inbox copies after
    the task completes, and only those. `fake_exec` stands in for the
    executor's half, which `test_executor.py` pins on its own.
    """

    def _run(self, config, db_path, *, attachments, source_type="whatsapp",
             transcribed=lambda paths: paths, success=True, during=None):
        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, **kw):
            if during is not None:
                during(list(task.attachments or []))
            task.transcribed_audio = tuple(transcribed(list(task.attachments or [])))
            if task.transcribed_audio:
                task.prompt = f"{task.prompt}\n\nTranscribed voice message: hello"
            return (success, "ok" if success else "boom", None, None)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Process the attached file(s)",
                user_id="alice", source_type=source_type,
                attachments=attachments,
            )
        with patch("istota.scheduler.execute_task", side_effect=fake_exec), \
                patch("istota.scheduler.asyncio.run", return_value=None):
            process_one_task(config)
        with db.get_db(db_path) as conn:
            return db.get_task(conn, task_id)

    def test_the_note_is_there_during_the_task_and_gone_after(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        note = _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")
        seen = []

        task = self._run(
            config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            during=lambda paths: seen.append(all(os.path.isfile(p) for p in paths)),
        )

        assert seen == [True]
        assert task.status == "completed"
        assert not note.exists()

    def test_a_note_with_no_transcript_is_kept(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        note = _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")

        task = self._run(
            config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            transcribed=lambda paths: [],
        )

        assert task.status == "completed"
        assert note.exists()

    def test_of_two_notes_only_the_transcribed_one_goes(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        ok = _inbox_file(config.workspace_path, "alice", "whatsapp_ok.ogg")
        bad = _inbox_file(config.workspace_path, "alice", "whatsapp_bad.ogg")

        self._run(
            config, db_path,
            attachments=[
                "/Users/alice/inbox/whatsapp_ok.ogg",
                "/Users/alice/inbox/whatsapp_bad.ogg",
            ],
            transcribed=lambda paths: [p for p in paths if "whatsapp_ok" in p],
        )

        assert not ok.exists()
        assert bad.exists()

    def test_a_reshuffled_attachment_list_does_not_shift_the_delete(self, db_path, tmp_path):
        # The executor reassigns `task.attachments` in place (image renditions
        # are inserted ahead of the rest); the match must be by value.
        config = _config(db_path, tmp_path)
        first = _inbox_file(config.workspace_path, "alice", "whatsapp_first.ogg")
        second = _inbox_file(config.workspace_path, "alice", "whatsapp_second.ogg")

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, **kw):
            seen = list(task.attachments or [])
            task.attachments = ["/control/rendition.jpg"] + seen
            task.transcribed_audio = (seen[0],)
            return (True, "ok", None, None)

        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="x", user_id="alice", source_type="whatsapp",
                attachments=[
                    "/Users/alice/inbox/whatsapp_first.ogg",
                    "/Users/alice/inbox/whatsapp_second.ogg",
                ],
            )
        with patch("istota.scheduler.execute_task", side_effect=fake_exec), \
                patch("istota.scheduler.asyncio.run", return_value=None):
            process_one_task(config)

        assert not first.exists()
        assert second.exists()

    def test_a_failed_task_keeps_its_note(self, db_path, tmp_path):
        # A retry re-transcribes from the file, so it has to still be there.
        config = _config(db_path, tmp_path)
        note = _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")

        self._run(
            config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            success=False,
        )

        assert note.exists()

    def test_a_web_chat_memo_is_untouched(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        memo = config.workspace_path / "Users" / "alice" / "inbox" / "web-chat" / "memo.webm"
        memo.parent.mkdir(parents=True)
        memo.write_bytes(b"webm")

        self._run(config, db_path, source_type="web", attachments=[str(memo)])

        assert memo.exists()

    def test_only_whatsapp_named_inbox_files_are_deleted(self, db_path, tmp_path):
        # An email attachment in the inbox, even on a WhatsApp-sourced row,
        # is not a voice note this feature owns.
        config = _config(db_path, tmp_path)
        other = _inbox_file(config.workspace_path, "alice", "123_voicemail.ogg")

        self._run(config, db_path, attachments=["/Users/alice/inbox/123_voicemail.ogg"])

        assert other.exists()

    def test_a_note_already_gone_still_completes_with_a_warning(
        self, db_path, tmp_path, caplog,
    ):
        # Gone between the re-localization and the unlink, so the delete
        # itself is what fails.
        from istota.skills import _loader

        config = _config(db_path, tmp_path)
        note = _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")
        real_open = _loader.open_overlay_dir

        def open_then_vanish(root, *parts):
            note.unlink()
            return real_open(root, *parts)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), \
                patch.object(_loader, "open_overlay_dir", side_effect=open_then_vanish):
            task = self._run(
                config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            )

        assert task.status == "completed"
        assert "Could not delete transcribed voice note 'whatsapp_abc.ogg'" in caplog.text

    @pytest.mark.requires_dac
    def test_an_undeletable_note_still_completes_with_a_warning(
        self, db_path, tmp_path, caplog,
    ):
        config = _config(db_path, tmp_path)
        note = _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")
        note.parent.chmod(0o500)
        try:
            with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
                task = self._run(
                    config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
                )
        finally:
            note.parent.chmod(0o700)

        assert task.status == "completed"
        assert note.exists()
        assert "whatsapp_abc.ogg" in caplog.text

    def test_a_symlinked_inbox_is_never_deleted_through(self, db_path, tmp_path):
        # The inbox is model-writable: swapped for a link to somewhere else
        # after the re-localization matched, nothing there may be unlinked.
        from istota import scheduler

        config = _config(db_path, tmp_path)
        _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "whatsapp_abc.ogg"
        victim.write_bytes(b"keep me")
        inbox = config.workspace_path / "Users" / "alice" / "inbox"
        real_localize = scheduler.localize_workspace_attachments
        calls = []

        def localize_then_swap(*args, **kwargs):
            got = real_localize(*args, **kwargs)
            calls.append(got)
            if len(calls) == 2:
                os.rename(inbox, inbox.with_name("inbox.real"))
                os.symlink(elsewhere, inbox)
            return got

        with patch.object(
            scheduler, "localize_workspace_attachments", side_effect=localize_then_swap,
        ):
            self._run(
                config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            )

        assert len(calls) == 2
        assert victim.exists()

    def test_the_transcript_is_written_to_the_row_before_the_file_goes(
        self, db_path, tmp_path,
    ):
        config = _config(db_path, tmp_path)
        _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")

        task = self._run(
            config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
        )

        assert "Transcribed voice message: hello" in task.prompt

    def test_a_raising_delete_does_not_fail_the_task(self, db_path, tmp_path, caplog):
        config = _config(db_path, tmp_path)
        _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"), patch(
            "istota.scheduler.remove_transcribed_voice_notes",
            side_effect=RuntimeError("boom"),
        ):
            task = self._run(
                config, db_path, attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            )

        assert task.status == "completed"
        assert "Could not remove transcribed voice notes" in caplog.text

    def test_a_dry_run_deletes_nothing(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        note = _inbox_file(config.workspace_path, "alice", "whatsapp_abc.ogg")

        def fake_exec(task, config, user_resources, *, dry_run=False,
                      event_writer=None, **kw):
            task.transcribed_audio = tuple(task.attachments or [])
            return (True, "ok", None, None)

        with db.get_db(db_path) as conn:
            db.create_task(
                conn, prompt="x", user_id="alice", source_type="whatsapp",
                attachments=["/Users/alice/inbox/whatsapp_abc.ogg"],
            )
        with patch("istota.scheduler.execute_task", side_effect=fake_exec), \
                patch("istota.scheduler.asyncio.run", return_value=None):
            process_one_task(config, dry_run=True)

        assert note.exists()
