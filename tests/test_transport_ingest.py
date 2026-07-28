"""Tests for ingest_message — IncomingMessage → task."""

import pytest

from istota import db
from istota.config import Config
from istota.transport import IncomingMessage, ingest_message


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


class TestIngestMessage:
    def test_maps_all_fields_to_task(self, config, db_path):
        msg = IncomingMessage(
            user_id="alice",
            text="do the thing",
            source_type="talk",
            surface="talk",
            channel_token="room42",
            delivery_token="deliver42",
            platform_message_id=1001,
            reply_to_message_id=999,
            reply_to_content="parent text",
            attachments=["Talk/a.png"],
            is_group_chat=True,
            output_target="both",
            model="claude-opus-4-8",
            effort="high",
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)

        assert task.prompt == "do the thing"
        assert task.user_id == "alice"
        assert task.source_type == "talk"
        assert task.conversation_token == "room42"
        assert task.talk_delivery_token == "deliver42"
        assert task.talk_message_id == 1001
        assert task.reply_to_talk_id == 999
        assert task.reply_to_content == "parent text"
        assert task.attachments == ["Talk/a.png"]
        assert task.is_group_chat is True
        assert task.output_target == "both"
        assert task.model == "claude-opus-4-8"
        assert task.effort == "high"

    def test_sender_inbound_clears_their_hide_tombstone(self, config, db_path):
        """Re-engagement un-hides: a user posting in a room they'd hidden clears
        their dismissal tombstone, so it resurfaces in their web list. Only the
        sender's tombstone — a co-member's hide is untouched."""
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room42", "alice", origin="talk", name="#x")
            db.add_room_member(conn, "room42", "bob")
            db.dismiss_room(conn, "room42", "alice")
            db.dismiss_room(conn, "room42", "bob")

        msg = IncomingMessage(
            user_id="alice", text="back again", source_type="talk",
            surface="talk", channel_token="room42",
        )
        with db.get_db(db_path) as conn:
            ingest_message(conn, config, msg)

        with db.get_db(db_path) as conn:
            assert not db.is_room_dismissed(conn, "room42", "alice")
            assert db.is_room_dismissed(conn, "room42", "bob")  # untouched

    def test_minimal_message(self, config, db_path):
        msg = IncomingMessage(
            user_id="bob",
            text="hello",
            source_type="email",
            surface="email",
            channel_token="thread1",
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
            task = db.get_task(conn, task_id)
        assert task.source_type == "email"
        assert task.conversation_token == "thread1"
        assert task.attachments is None

    def test_empty_attachments_become_none(self, config, db_path):
        msg = IncomingMessage(
            user_id="bob", text="x", source_type="talk",
            surface="talk", channel_token="c", attachments=[],
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
            task = db.get_task(conn, task_id)
        assert task.attachments is None

    def test_duplicate_talk_message_returns_existing_id(self, config, db_path):
        msg = IncomingMessage(
            user_id="alice", text="hi", source_type="talk",
            surface="talk", channel_token="room1", platform_message_id=555,
        )
        with db.get_db(db_path) as conn:
            first = ingest_message(conn, config, msg)
            second = ingest_message(conn, config, msg)
        assert first == second


class TestWorkspaceAttachmentPaths:
    """A turn's attachment chips become links only when the file sits in the
    sender's own workspace, so the web UI can serve it back through the
    session-scoped `/chat/files` endpoint (never a public share)."""

    def _config(self, tmp_path, db_path):
        cfg = Config()
        cfg.db_path = db_path
        cfg.nextcloud_mount_path = tmp_path / "mount"
        return cfg

    def test_maps_a_workspace_file_to_its_workspace_path(self, tmp_path, db_path):
        from istota.transport.ingest import workspace_attachment_paths

        cfg = self._config(tmp_path, db_path)
        host = str(cfg.nextcloud_mount_path / "Users" / "alice" / "inbox" / "web-chat" / "n-1.txt")
        assert workspace_attachment_paths(cfg, "alice", [host]) == [
            "/Users/alice/inbox/web-chat/n-1.txt"
        ]

    def test_file_outside_the_workspace_has_no_path(self, tmp_path, db_path):
        """A Talk attachment lives under /Talk, and a mountless deployment
        stores uploads in temp — neither is servable, so the chip stays inert
        rather than becoming a dead link."""
        from istota.transport.ingest import workspace_attachment_paths

        cfg = self._config(tmp_path, db_path)
        outside = str(cfg.nextcloud_mount_path / "Talk" / "shared.png")
        own = str(cfg.nextcloud_mount_path / "Users" / "alice" / "b.png")
        assert workspace_attachment_paths(cfg, "alice", [outside, own]) == [
            None, "/Users/alice/b.png",
        ]

    def test_another_users_file_has_no_path(self, tmp_path, db_path):
        from istota.transport.ingest import workspace_attachment_paths

        cfg = self._config(tmp_path, db_path)
        host = str(cfg.nextcloud_mount_path / "Users" / "bob" / "inbox" / "x.txt")
        own = str(cfg.nextcloud_mount_path / "Users" / "alice" / "b.png")
        assert workspace_attachment_paths(cfg, "alice", [host, own]) == [
            None, "/Users/alice/b.png",
        ]

    def test_entries_stay_positional(self, tmp_path, db_path):
        """The list is parallel to the display names, so an unresolvable file
        holds its slot instead of shifting a link onto the wrong chip."""
        from istota.transport.ingest import workspace_attachment_paths

        cfg = self._config(tmp_path, db_path)
        root = cfg.nextcloud_mount_path / "Users" / "alice"
        paths = [str(cfg.nextcloud_mount_path / "Talk" / "a.png"), str(root / "b.png")]
        assert workspace_attachment_paths(cfg, "alice", paths) == [
            None, "/Users/alice/b.png",
        ]

    def test_all_unresolvable_returns_none(self, tmp_path, db_path):
        from istota.transport.ingest import workspace_attachment_paths

        cfg = self._config(tmp_path, db_path)
        outside = str(cfg.nextcloud_mount_path / "Talk" / "a.png")
        assert workspace_attachment_paths(cfg, "alice", [outside]) is None

    def test_no_attachments_returns_none(self, tmp_path, db_path):
        from istota.transport.ingest import workspace_attachment_paths

        cfg = self._config(tmp_path, db_path)
        assert workspace_attachment_paths(cfg, "alice", None) is None

    def test_no_mount_returns_none(self, db_path):
        """An rclone deployment has no local workspace, so nothing is linkable."""
        from istota.transport.ingest import workspace_attachment_paths

        cfg = Config()
        cfg.db_path = db_path
        cfg.nextcloud_mount_path = None
        assert workspace_attachment_paths(cfg, "alice", ["/tmp/x.png"]) is None
