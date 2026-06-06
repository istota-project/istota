"""Tests for EmailTransport.

Email self-creates its tasks inside ``poll_emails`` (the confirmation gate and
processed-email linkage need the task id mid-loop), so the transport's ``poll``
delegates there and returns an empty IncomingMessage list. The routing
precedence / gate behaviour itself is covered by ``test_email_poller.py``.
"""

from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, NextcloudConfig, UserConfig
from istota.transport.email import EmailTransport


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _config(db_path, **overrides):
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(url="https://nc.example.com"),
        email=EmailConfig(enabled=True, bot_email="bot@example.com"),
        **overrides,
    )


def _task(**overrides):
    defaults = dict(
        id=1, status="completed", source_type="email",
        user_id="alice", prompt="hi",
    )
    defaults.update(overrides)
    return db.Task(**defaults)


class TestCapabilities:
    def test_email_capabilities(self, db_path):
        t = EmailTransport(_config(db_path))
        assert t.name == "email"
        assert t.capabilities.supports_edit is False
        assert t.capabilities.supports_progress_ack is False
        assert t.capabilities.supports_threading is True
        assert t.capabilities.max_message_length is None


class TestPoll:
    @pytest.mark.asyncio
    async def test_poll_delegates_to_poll_emails(self, db_path):
        t = EmailTransport(_config(db_path))
        with patch("istota.email_poller.poll_emails", return_value=[7, 8]) as mock_poll:
            result = await t.poll()
        # Email self-creates; nothing to ingest downstream.
        assert result == []
        mock_poll.assert_called_once_with(t._config)


class TestDeliver:
    @pytest.mark.asyncio
    async def test_deliver_delegates_to_post_result_to_email(self, db_path):
        t = EmailTransport(_config(db_path))
        task = _task()
        with patch(
            "istota.scheduler.post_result_to_email",
            new_callable=MagicMock,
        ) as mock_send:
            async def _ok(*a, **k):
                return True
            mock_send.side_effect = _ok
            result = await t.deliver("alice@example.com", "body", task=task)
        assert result is None
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_deliver_without_task_is_noop(self, db_path):
        t = EmailTransport(_config(db_path))
        result = await t.deliver("alice@example.com", "body")
        assert result is None


class TestEditAndAttachment:
    @pytest.mark.asyncio
    async def test_edit_is_noop(self, db_path):
        t = EmailTransport(_config(db_path))
        assert await t.edit("x", 1, "y") is None

    @pytest.mark.asyncio
    async def test_download_attachment_is_noop(self, db_path):
        t = EmailTransport(_config(db_path))
        assert await t.download_attachment("ref", "/tmp/x") is None


class TestResolveTarget:
    def test_resolves_sender_for_reply(self, db_path):
        t = EmailTransport(_config(db_path))
        with patch.object(db, "get_email_for_task") as mock_get:
            mock_get.return_value = MagicMock(sender_email="ext@example.com")
            target = t.resolve_target(_task())
        assert target == "ext@example.com"

    def test_falls_back_to_user_address(self, db_path):
        config = _config(
            db_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        t = EmailTransport(config)
        with patch.object(db, "get_email_for_task", return_value=None):
            target = t.resolve_target(_task(user_id="alice"))
        assert target == "alice@example.com"

    def test_returns_none_when_no_address(self, db_path):
        config = _config(db_path, users={"alice": UserConfig()})
        t = EmailTransport(config)
        with patch.object(db, "get_email_for_task", return_value=None):
            target = t.resolve_target(_task(user_id="alice"))
        assert target is None
