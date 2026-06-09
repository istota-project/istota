"""End-to-end tests for the EmailTransport send body
(``transport/email/outbound.py:deliver_email_result``).

The scheduler-level suite mocks ``post_result_to_email`` wholesale, so the four
real send branches — reply-to-thread, fresh-send, briefing legacy fallback, and
the briefing markdown-strip safety net — were never exercised against the actual
``send_email`` / ``reply_to_email`` calls. These tests drive
``deliver_email_result`` directly with those two SMTP entry points mocked at the
outbound module, asserting the call arguments, the True/False return contract,
and the ``sent_emails`` recording side effect.

``_parse_email_output`` / ``_load_deferred_email_output`` / ``_record_sent_email``
have their own unit coverage in ``test_scheduler.py``; this file is about the
orchestration in ``deliver_email_result``.
"""

import json
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, NextcloudConfig, UserConfig
from istota.transport.email.outbound import deliver_email_result


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _config(db_path, tmp_path, **overrides):
    base = dict(
        db_path=db_path,
        temp_dir=tmp_path,
        bot_name="Istota",
        nextcloud=NextcloudConfig(url="https://nc.example.com"),
        email=EmailConfig(enabled=True, bot_email="bot@example.com"),
    )
    base.update(overrides)
    return Config(**base)


def _make_task(db_path, *, source_type="email", prompt="do a thing", user_id="alice"):
    """Create a real task row and return the hydrated Task."""
    with db.get_db(db_path) as conn:
        tid = db.create_task(
            conn, prompt=prompt, user_id=user_id, source_type=source_type,
        )
        return db.get_task(conn, tid)


def _link_inbound_email(
    db_path, task_id, *,
    sender="ext@example.com", subject="Original subject",
    message_id="<orig@example.com>", references=None,
):
    """Attach a processed_emails row to a task so it is treated as a reply."""
    with db.get_db(db_path) as conn:
        db.mark_email_processed(
            conn,
            email_id=f"imap-{task_id}",
            sender_email=sender,
            subject=subject,
            message_id=message_id,
            references=references,
            user_id="alice",
            task_id=task_id,
            routing_method="plus_address",
        )


def _structured(subject="Re: Plan", body="Here is the plan.", fmt="plain"):
    return json.dumps({"subject": subject, "body": body, "format": fmt})


# ---------------------------------------------------------------------------
# Reply-to-thread branch
# ---------------------------------------------------------------------------


class TestReplyBranch:
    @pytest.mark.asyncio
    async def test_reply_uses_reply_to_email_with_thread_headers(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id,
            sender="contact@example.com",
            subject="Project X",
            message_id="<msg-1@example.com>",
            references="<root@example.com>",
        )

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<reply-1@bot.example.com>",
            ) as mock_reply,
            patch("istota.transport.email.outbound.send_email") as mock_send,
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Re: Project X", body="Done.", fmt="plain",
            ))

        assert ok is True
        mock_send.assert_not_called()
        mock_reply.assert_called_once()
        kwargs = mock_reply.call_args.kwargs
        assert kwargs["to_addr"] == "contact@example.com"
        assert kwargs["subject"] == "Re: Project X"
        assert kwargs["body"] == "Done."
        assert kwargs["in_reply_to"] == "<msg-1@example.com>"
        # References = parent.references + parent.message_id (RFC 5322 chain)
        assert kwargs["references"] == "<root@example.com> <msg-1@example.com>"
        assert kwargs["from_addr"] == "bot@example.com"
        assert kwargs["content_type"] == "plain"

    @pytest.mark.asyncio
    async def test_reply_records_sent_email(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        task = _make_task(db_path)
        _link_inbound_email(db_path, task.id, message_id="<msg-2@example.com>")

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<sent-99@bot.example.com>",
            ),
            patch("istota.transport.email.outbound.send_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is True
        # The reply must be recorded for emissary thread matching.
        with db.get_db(db_path) as conn:
            recorded = db.find_sent_email_by_message_id(conn, "<sent-99@bot.example.com>")
        assert recorded is not None
        assert recorded.task_id == task.id
        assert recorded.in_reply_to == "<msg-2@example.com>"

    @pytest.mark.asyncio
    async def test_web_continuation_mirror_carries_origin_forward(self, db_path, tmp_path):
        # Multi-round regression: a web-origin email reply mirrors its result to
        # the thread. The mirror's sent_emails row must carry the web origin so
        # the *next* reply still routes back to the room (not misroute to Talk).
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="reply", user_id="alice", source_type="email",
                conversation_token="web-alice-rm1",
                output_target="web:web-alice-rm1,email",
            )
            task = db.get_task(conn, tid)
        _link_inbound_email(db_path, task.id, message_id="<round1@x.com>")

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<mirror@bot.example.com>",
            ),
            patch("istota.transport.email.outbound.send_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is True
        with db.get_db(db_path) as conn:
            recorded = db.find_sent_email_by_message_id(conn, "<mirror@bot.example.com>")
        assert recorded is not None
        assert recorded.origin_target == "web:web-alice-rm1"

    @pytest.mark.asyncio
    async def test_reply_falls_back_to_original_subject(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        task = _make_task(db_path)
        _link_inbound_email(db_path, task.id, subject="Kept subject", message_id="<m@x>")

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<r@bot>",
            ) as mock_reply,
            patch("istota.transport.email.outbound.send_email"),
        ):
            # parsed subject is None -> keep the original email subject
            ok = await deliver_email_result(config, task, json.dumps(
                {"subject": None, "body": "b", "format": "plain"},
            ))

        assert ok is True
        assert mock_reply.call_args.kwargs["subject"] == "Kept subject"

    @pytest.mark.asyncio
    async def test_reply_returns_false_on_send_error(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        task = _make_task(db_path)
        _link_inbound_email(db_path, task.id)

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                side_effect=RuntimeError("smtp down"),
            ),
            patch("istota.transport.email.outbound.send_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is False


# ---------------------------------------------------------------------------
# Fresh-send branch (no inbound email linked, e.g. scheduled job)
# ---------------------------------------------------------------------------


class TestFreshSendBranch:
    @pytest.mark.asyncio
    async def test_fresh_send_to_user_address(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="scheduled")

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<fresh-1@bot.example.com>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Your digest", body="content", fmt="plain",
            ))

        assert ok is True
        mock_reply.assert_not_called()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to"] == "alice@example.com"
        assert kwargs["subject"] == "Your digest"
        assert kwargs["body"] == "content"
        assert kwargs["from_addr"] == "bot@example.com"

    @pytest.mark.asyncio
    async def test_fresh_send_subject_falls_back_to_prompt(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(
            db_path, source_type="scheduled",
            prompt="A rather long prompt that should be excerpted into the subject line nicely",
        )

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<fresh-2@bot.example.com>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, json.dumps(
                {"subject": None, "body": "b", "format": "plain"},
            ))

        assert ok is True
        subject = mock_send.call_args.kwargs["subject"]
        assert subject == f"[Istota] {task.prompt[:80]}"

    @pytest.mark.asyncio
    async def test_fresh_send_records_sent_email(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="scheduled")

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<fresh-3@bot.example.com>",
            ),
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is True
        with db.get_db(db_path) as conn:
            recorded = db.find_sent_email_by_message_id(conn, "<fresh-3@bot.example.com>")
        assert recorded is not None
        assert recorded.to_addr == "alice@example.com"

    @pytest.mark.asyncio
    async def test_fresh_send_no_address_returns_false(self, db_path, tmp_path):
        config = _config(db_path, tmp_path, users={"alice": UserConfig()})
        task = _make_task(db_path, source_type="scheduled")

        with (
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is False
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_send_returns_false_on_send_error(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="scheduled")

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                side_effect=RuntimeError("smtp down"),
            ),
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is False


# ---------------------------------------------------------------------------
# Briefing legacy fallback (no structured output, source_type == "briefing")
# ---------------------------------------------------------------------------


class TestBriefingLegacyFallback:
    @pytest.mark.asyncio
    async def test_briefing_unstructured_sends_stripped_markdown(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(
            db_path, source_type="briefing",
            prompt="Generate a morning briefing for the user",
        )

        # Raw Talk-formatted text, no JSON -> legacy briefing path.
        message = "# Morning\n\n**Markets** are up. See [link](http://x)."
        with (
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
        ):
            ok = await deliver_email_result(config, task, message)

        assert ok is True
        mock_reply.assert_not_called()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to"] == "alice@example.com"
        # Subject derived from the "Generate a X briefing" prompt.
        assert kwargs["subject"] == "Morning Briefing"
        assert kwargs["content_type"] == "plain"
        # Markdown emphasis / link syntax must be stripped for plain-text email.
        assert "**" not in kwargs["body"]
        assert "[link]" not in kwargs["body"]
        assert "http://x" not in kwargs["body"]
        assert "Markets are up. See link." in kwargs["body"]

    @pytest.mark.asyncio
    async def test_briefing_unstructured_no_address_returns_false(self, db_path, tmp_path):
        config = _config(db_path, tmp_path, users={"alice": UserConfig()})
        task = _make_task(
            db_path, source_type="briefing",
            prompt="Generate a daily briefing",
        )

        with (
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, "raw text")

        assert ok is False
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Briefing markdown-strip safety net (structured output, plain format)
# ---------------------------------------------------------------------------


class TestBriefingMarkdownStripSafetyNet:
    @pytest.mark.asyncio
    async def test_structured_briefing_plain_body_is_stripped(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(
            db_path, source_type="briefing",
            prompt="Generate a weekly briefing",
        )

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Weekly", body="**Bold** and _italic_ text", fmt="plain",
            ))

        assert ok is True
        body = mock_send.call_args.kwargs["body"]
        assert "**" not in body
        assert "Bold" in body

    @pytest.mark.asyncio
    async def test_structured_briefing_html_body_not_stripped(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(
            db_path, source_type="briefing",
            prompt="Generate a weekly briefing",
        )

        html = "<p><strong>Bold</strong></p>"
        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Weekly", body=html, fmt="html",
            ))

        assert ok is True
        # html format -> safety-net strip does not run; body preserved verbatim.
        assert mock_send.call_args.kwargs["body"] == html
        assert mock_send.call_args.kwargs["content_type"] == "html"


# ---------------------------------------------------------------------------
# No-structured-output skip (non-briefing): nothing sent, returns True
# ---------------------------------------------------------------------------


class TestNoStructuredOutputSkip:
    @pytest.mark.asyncio
    async def test_no_json_non_briefing_skips_send(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        task = _make_task(db_path, source_type="email")
        _link_inbound_email(db_path, task.id)

        with (
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
        ):
            # Plain prose, no JSON -> assume the agent already sent via `email send`.
            ok = await deliver_email_result(config, task, "I sent the email already.")

        assert ok is True
        mock_send.assert_not_called()
        mock_reply.assert_not_called()


# ---------------------------------------------------------------------------
# Deferred-file output takes precedence over inline JSON
# ---------------------------------------------------------------------------


class TestDeferredOutputPrecedence:
    @pytest.mark.asyncio
    async def test_deferred_file_wins_over_inline_json(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="scheduled")

        # Write the deferred email-output file the executor would have produced.
        user_dir = tmp_path / "alice"
        user_dir.mkdir()
        (user_dir / f"task_{task.id}_email_output.json").write_text(json.dumps({
            "subject": "From deferred file",
            "body": "deferred body",
            "format": "plain",
        }))

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<d@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            # Inline message has different content; deferred file must win.
            ok = await deliver_email_result(config, task, _structured(
                subject="From inline", body="inline body",
            ))

        assert ok is True
        kwargs = mock_send.call_args.kwargs
        assert kwargs["subject"] == "From deferred file"
        assert kwargs["body"] == "deferred body"
