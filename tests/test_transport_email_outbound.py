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
    """A Config whose default user trusts the correspondents these tests use.

    The default matters: `deliver_email_result` runs every recipient through the
    outbound approval gate, whose floor is `untrusted`, so a config naming no
    user at all holds every reply rather than sending it. The branch tests below
    are about send mechanics, not the gate, so the default user trusts
    `*@example.com` and their sends go through — which also means each of them
    exercises the gate's pass-through. The gate's *holding* behaviour has its own
    class at the end of this file.
    """
    base = dict(
        db_path=db_path,
        temp_dir=tmp_path,
        bot_name="Istota",
        nextcloud=NextcloudConfig(url="https://nc.example.com"),
        email=EmailConfig(enabled=True, bot_email="bot@example.com"),
        users={
            "alice": UserConfig(
                email_addresses=["alice@example.com"],
                trusted_email_senders=["*@example.com"],
            ),
        },
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
        # No caller-supplied title: fall back to deriving one from the prompt.
        assert kwargs["subject"] == "Morning Briefing"
        assert kwargs["content_type"] == "plain"
        # Markdown emphasis / link syntax must be stripped for plain-text email.
        assert "**" not in kwargs["body"]
        assert "[link]" not in kwargs["body"]
        assert "http://x" not in kwargs["body"]
        assert "Markets are up. See link." in kwargs["body"]

    @pytest.mark.asyncio
    async def test_supplied_subject_wins_over_the_prompt_derivation(
        self, db_path, tmp_path,
    ):
        """The scheduler's deterministic title overrides the prompt scrape.

        The prompt says "morning" (clock-derived), the briefing is the evening
        one — the caller's title is what the reader should see.
        """
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(
            db_path, source_type="briefing",
            prompt="Generate a morning briefing for the user",
        )

        with (
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(
                config, task, "body text", subject="Evening Wrap — Monday, 27 July",
            )

        assert ok is True
        assert mock_send.call_args.kwargs["subject"] == "Evening Wrap — Monday, 27 July"

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
        """With HTML briefing email off, a `format=="html"` body is sent verbatim.

        The HTML-on path instead passes it through as the ``html_body``
        alternative — see ``TestBriefingHtmlEmail``.
        """
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(
                email_addresses=["alice@example.com"], briefing_email_html=False,
            )},
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


# ---------------------------------------------------------------------------
# HTML briefing email (multipart/alternative) + the per-user opt-out
# ---------------------------------------------------------------------------


_BRIEFING_MD = (
    "\U0001f4f0 World News\n"
    "**IRAN:** Tensions escalate. "
    "[[Semafor](https://semafor.com/a/iran), NYT]\n"
    "\n"
    "- **10:00 Standup** (30 min)"
)


class TestBriefingHtmlEmail:
    @pytest.mark.asyncio
    async def test_legacy_briefing_path_sends_multipart(self, db_path, tmp_path):
        """Unstructured briefing output: plain part stripped, HTML part rendered."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(
            db_path, source_type="briefing",
            prompt="Generate a morning briefing for the user",
        )

        with (
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _BRIEFING_MD)

        assert ok is True
        kwargs = mock_send.call_args.kwargs
        # Plain part: markdown flattened, URL gone (today's behaviour).
        assert "**" not in kwargs["body"]
        assert "https://semafor.com" not in kwargs["body"]
        # HTML part: the article link is a real anchor.
        assert '<a href="https://semafor.com/a/iran">Semafor</a>' in kwargs["html_body"]
        assert "<strong>IRAN:</strong>" in kwargs["html_body"]
        assert "<li><strong>10:00 Standup</strong> (30 min)</li>" in kwargs["html_body"]
        assert "style=" not in kwargs["html_body"]

    @pytest.mark.asyncio
    async def test_structured_plain_briefing_sends_multipart(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="briefing", prompt="Generate a briefing")

        with (
            patch(
                "istota.transport.email.outbound.send_email", return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Morning", body=_BRIEFING_MD, fmt="plain",
            ))

        assert ok is True
        kwargs = mock_send.call_args.kwargs
        assert "**" not in kwargs["body"]
        assert '<a href="https://semafor.com/a/iran">' in kwargs["html_body"]

    @pytest.mark.asyncio
    async def test_preference_off_is_single_part_plain(self, db_path, tmp_path):
        """Opt-out restores today's exact plain-only delivery."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(
                email_addresses=["alice@example.com"], briefing_email_html=False,
            )},
        )
        task = _make_task(db_path, source_type="briefing", prompt="Generate a briefing")

        with (
            patch(
                "istota.transport.email.outbound.send_email", return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _BRIEFING_MD)

        assert ok is True
        kwargs = mock_send.call_args.kwargs
        assert kwargs.get("html_body") is None
        assert kwargs["content_type"] == "plain"
        assert "**" not in kwargs["body"]

    @pytest.mark.asyncio
    async def test_non_briefing_task_gets_no_html(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="scheduled", prompt="job")

        with (
            patch(
                "istota.transport.email.outbound.send_email", return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Job", body="**not** stripped", fmt="plain",
            ))

        assert ok is True
        kwargs = mock_send.call_args.kwargs
        assert kwargs.get("html_body") is None
        # Non-briefing bodies are not markdown-stripped either (unchanged).
        assert kwargs["body"] == "**not** stripped"

    @pytest.mark.asyncio
    async def test_reply_thread_briefing_gets_html_body(self, db_path, tmp_path):
        """A briefing landing in an existing thread still goes multipart."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(
                email_addresses=["alice@example.com"],
                # The reply goes to the correspondent, not to the user, so the
                # approval gate has an opinion about it. This test is about the
                # multipart body.
                trusted_email_senders=["*@example.com"],
            )},
        )
        task = _make_task(db_path, source_type="briefing", prompt="Generate a briefing")
        _link_inbound_email(db_path, task.id)

        with (
            patch("istota.transport.email.outbound.send_email"),
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<r@bot>",
            ) as mock_reply,
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Morning", body=_BRIEFING_MD, fmt="plain",
            ))

        assert ok is True
        kwargs = mock_reply.call_args.kwargs
        assert '<a href="https://semafor.com/a/iran">' in kwargs["html_body"]
        assert "**" not in kwargs["body"]
        assert kwargs["in_reply_to"] == "<orig@example.com>"

    @pytest.mark.asyncio
    async def test_structured_html_format_passes_through_as_html_part(
        self, db_path, tmp_path,
    ):
        """A hand-authored `format=="html"` briefing: HTML part verbatim, plain derived."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="briefing", prompt="Generate a briefing")

        html = "<p><strong>Bold</strong> and a <a href='http://x'>link</a></p>"
        with (
            patch(
                "istota.transport.email.outbound.send_email", return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured(
                subject="Weekly", body=html, fmt="html",
            ))

        assert ok is True
        kwargs = mock_send.call_args.kwargs
        assert kwargs["html_body"] == html
        # Plain fallback is a tag-stripped copy (the rare hand-authored path).
        assert "<p>" not in kwargs["body"]
        assert "Bold" in kwargs["body"]
        assert kwargs["content_type"] == "plain"

    @pytest.mark.asyncio
    async def test_renderer_failure_degrades_to_plain(self, db_path, tmp_path):
        """An empty render (renderer's failure signal) must not send an empty part."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="briefing", prompt="Generate a briefing")

        with (
            patch(
                "istota.transport.email.outbound.send_email", return_value="<b@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
            patch(
                "istota.skills.briefing.render_briefing_html", return_value="",
            ),
        ):
            ok = await deliver_email_result(config, task, _BRIEFING_MD)

        assert ok is True
        assert mock_send.call_args.kwargs.get("html_body") is None


# ---------------------------------------------------------------------------
# The outbound approval gate on the delivery leg (ISSUE-246)
# ---------------------------------------------------------------------------


def _write_deferred_output(tmp_path, task, *, subject="Re: Hey", body="The answer."):
    """The `istota-skill email output` file, as the executor would leave it."""
    user_dir = tmp_path / task.user_id
    user_dir.mkdir(exist_ok=True)
    (user_dir / f"task_{task.id}_email_output.json").write_text(json.dumps({
        "subject": subject, "body": body, "format": "plain",
    }))


def _drafts(db_path):
    with db.get_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM outbound_drafts ORDER BY id"
        ).fetchall()


class TestDeliveryLegApprovalGate:
    """The gate has to fire where the mail actually leaves, not only in the
    CLI verbs.

    ISSUE-246: `_outbound_gate` was wired into `email send` / `email reply`
    only, and the model replies with `email output` — which writes a deferred
    file that the *scheduler* mails through `deliver_email_result`, a path with
    no check at all. Two messages reached an address the user had declined to
    trust. These drive that exact shape: an email-origin task, a deferred output
    file, delivery by the scheduler.
    """

    @pytest.mark.asyncio
    async def test_deferred_reply_to_untrusted_address_is_held(self, db_path, tmp_path):
        """The reported trace. `email output` to an untrusted correspondent."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id,
            sender="stranger@protonmail.test",
            subject="Hey zorg",
            message_id="<inbound-1@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task, body="Here is the todo list.")

        with (
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
            patch("istota.transport.email.outbound.send_email") as mock_send,
        ):
            ok = await deliver_email_result(config, task, "")

        # Nothing left the process.
        mock_reply.assert_not_called()
        mock_send.assert_not_called()
        # A hold is not a task failure — the task already reported success.
        assert ok is True

        rows = _drafts(db_path)
        assert len(rows) == 1
        draft = rows[0]
        assert json.loads(draft["to_addrs"]) == ["stranger@protonmail.test"]
        assert draft["body"] == "Here is the todo list."
        assert draft["status"] == "pending"
        assert draft["hold_reason"] == "untrusted_recipient"
        assert draft["task_id"] == task.id

    @pytest.mark.asyncio
    async def test_held_draft_snapshots_threading_headers(self, db_path, tmp_path):
        """`release` sends from the row, so the row must carry the thread."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id,
            sender="stranger@protonmail.test",
            message_id="<inbound-2@protonmail.test>",
            references="<root@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task, subject="Re: Plan")

        with (
            patch("istota.transport.email.outbound.reply_to_email"),
            patch("istota.transport.email.outbound.send_email"),
        ):
            await deliver_email_result(config, task, "")

        draft = _drafts(db_path)[0]
        assert draft["in_reply_to"] == "<inbound-2@protonmail.test>"
        assert draft["references"] == "<root@protonmail.test> <inbound-2@protonmail.test>"
        assert draft["subject"] == "Re: Plan"

    @pytest.mark.asyncio
    async def test_deferred_reply_to_trusted_address_is_sent(self, db_path, tmp_path):
        """The gate keys on the recipient, so a trusted one still goes out."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(
                email_addresses=["alice@example.com"],
                trusted_email_senders=["*@partner.test"],
            )},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id,
            sender="colleague@partner.test",
            message_id="<inbound-3@partner.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<sent@bot.example.com>",
            ) as mock_reply,
            patch("istota.transport.email.outbound.send_email"),
        ):
            ok = await deliver_email_result(config, task, "")

        assert ok is True
        mock_reply.assert_called_once()
        assert mock_reply.call_args.kwargs["to_addr"] == "colleague@partner.test"
        assert _drafts(db_path) == []

    @pytest.mark.asyncio
    async def test_all_policy_holds_reply_to_trusted_correspondent(self, db_path, tmp_path):
        """The residual the spec recorded at `:76`, closed by moving the check.

        Under `all` only the user's own addresses go out unapproved. A deferred
        reply to a *trusted* correspondent was sent anyway, because this leg had
        no check to consult the policy at all.
        """
        config = _config(
            db_path, tmp_path,
            email=EmailConfig(
                enabled=True, bot_email="bot@example.com",
                outbound_approval_floor="all",
            ),
            users={"alice": UserConfig(
                email_addresses=["alice@example.com"],
                trusted_email_senders=["*@partner.test"],
            )},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id,
            sender="colleague@partner.test",
            message_id="<inbound-4@partner.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
            patch("istota.transport.email.outbound.send_email"),
        ):
            ok = await deliver_email_result(config, task, "")

        assert ok is True
        mock_reply.assert_not_called()
        assert _drafts(db_path)[0]["hold_reason"] == "all_mode"

    @pytest.mark.parametrize("floor", ["untrusted", "all"])
    @pytest.mark.asyncio
    async def test_self_addressed_briefing_is_never_held(self, db_path, tmp_path, floor):
        """Briefings and notifications pass through this function too.

        Both live policies clear the user's own address, so neither holds a
        self-addressed briefing. The entry asked for a test rather than the
        argument.
        """
        config = _config(
            db_path, tmp_path,
            email=EmailConfig(
                enabled=True, bot_email="bot@example.com",
                outbound_approval_floor=floor,
            ),
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path, source_type="briefing")

        with (
            patch(
                "istota.transport.email.outbound.send_email",
                return_value="<brief@bot>",
            ) as mock_send,
            patch("istota.transport.email.outbound.reply_to_email"),
        ):
            ok = await deliver_email_result(config, task, _structured())

        assert ok is True
        assert mock_send.call_args.kwargs["to"] == "alice@example.com"
        assert _drafts(db_path) == []

    @pytest.mark.asyncio
    async def test_policy_off_sends_without_touching_drafts(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            email=EmailConfig(
                enabled=True, bot_email="bot@example.com",
                outbound_approval_floor="off",
            ),
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            message_id="<inbound-5@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch(
                "istota.transport.email.outbound.reply_to_email",
                return_value="<sent@bot>",
            ) as mock_reply,
            patch("istota.transport.email.outbound.send_email"),
        ):
            ok = await deliver_email_result(config, task, "")

        assert ok is True
        mock_reply.assert_called_once()
        assert _drafts(db_path) == []

    @pytest.mark.asyncio
    async def test_gate_that_cannot_run_refuses_to_send(self, db_path, tmp_path):
        """A gate that fails open on a broken check is not a gate.

        Reported as a delivery failure, because nothing was sent *and* nothing
        was held — unlike a hold, there is no draft to recover from.
        """
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            message_id="<inbound-6@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
            patch("istota.transport.email.outbound.send_email") as mock_send,
            patch(
                "istota.outbound_policy.recipients_require_hold",
                side_effect=RuntimeError("database is locked"),
            ),
        ):
            ok = await deliver_email_result(config, task, "")

        assert ok is False
        mock_reply.assert_not_called()
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_send_keeps_the_composed_body_on_disk(self, db_path, tmp_path):
        """The failing check must not also destroy the message.

        Nothing was sent and no draft was written, so the deferred file is the
        only surviving copy — `task.result` holds the model's prose, not this
        envelope. Deleting it would turn a transient database fault into
        permanent message loss.
        """
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            message_id="<inbound-8@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task, body="Do not lose me.")
        path = tmp_path / "alice" / f"task_{task.id}_email_output.json"

        with (
            patch("istota.transport.email.outbound.reply_to_email"),
            patch("istota.transport.email.outbound.send_email"),
            patch(
                "istota.outbound_policy.recipients_require_hold",
                side_effect=RuntimeError("database is locked"),
            ),
        ):
            await deliver_email_result(config, task, "")

        assert path.exists()
        assert "Do not lose me." in path.read_text()

    @pytest.mark.asyncio
    async def test_a_held_reply_consumes_the_file(self, db_path, tmp_path):
        """Once the body is in the draft, the file has done its job."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            message_id="<inbound-9@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task)
        path = tmp_path / "alice" / f"task_{task.id}_email_output.json"

        with (
            patch("istota.transport.email.outbound.reply_to_email"),
            patch("istota.transport.email.outbound.send_email"),
        ):
            await deliver_email_result(config, task, "")

        assert not path.exists()
        assert len(_drafts(db_path)) == 1

    @pytest.mark.asyncio
    async def test_hold_notifies_the_user_immediately(self, db_path, tmp_path):
        """A hold here has no other voice.

        The CLI verbs return a `held` envelope the model reports in its own
        answer. By the time this leg runs the task has finished and its "I have
        replied" answer is already in the room, and a first-contact thread has
        no room for a draft card — so without this notice the hold is invisible
        until the 24-hour stale-draft nag.
        """
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            subject="Hey zorg",
            message_id="<inbound-10@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task, subject="Re: Hey zorg")

        with (
            patch("istota.transport.email.outbound.reply_to_email"),
            patch("istota.transport.email.outbound.send_email"),
            patch("istota.notifications.send_notification") as mock_notify,
        ):
            await deliver_email_result(config, task, "")

        mock_notify.assert_called_once()
        body = mock_notify.call_args[0][2]
        assert "stranger@protonmail.test" in body
        assert "waiting for your approval" in body
        assert "Nothing was sent" in body
        # Routed as an alert: it is asking the user to do something.
        assert mock_notify.call_args[1]["purpose"] == "alert"

    @pytest.mark.asyncio
    async def test_a_notification_failure_does_not_lose_the_hold(self, db_path, tmp_path):
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            message_id="<inbound-11@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
            patch("istota.transport.email.outbound.send_email"),
            patch(
                "istota.notifications.send_notification",
                side_effect=RuntimeError("talk is down"),
            ),
        ):
            ok = await deliver_email_result(config, task, "")

        assert ok is True
        mock_reply.assert_not_called()
        assert len(_drafts(db_path)) == 1

    @pytest.mark.asyncio
    async def test_held_reply_subject_carries_the_re_prefix(self, db_path, tmp_path):
        """`release` sends through `send_email`, which adds no `Re:` of its own."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            subject="Question about X",
            message_id="<inbound-12@protonmail.test>",
        )
        # No structured subject, so the branch keeps the inbound one.
        user_dir = tmp_path / "alice"
        user_dir.mkdir(exist_ok=True)
        (user_dir / f"task_{task.id}_email_output.json").write_text(json.dumps({
            "subject": None, "body": "An answer.", "format": "plain",
        }))

        with (
            patch("istota.transport.email.outbound.reply_to_email"),
            patch("istota.transport.email.outbound.send_email"),
        ):
            await deliver_email_result(config, task, "")

        assert _drafts(db_path)[0]["subject"] == "Re: Question about X"

    @pytest.mark.asyncio
    async def test_an_unrecordable_recipient_refuses_rather_than_sends(self, db_path, tmp_path):
        """The decision was made; only the recording failed. Still no send."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        task = _make_task(db_path)
        # A header-injection shape `normalize_addresses` refuses at hold time.
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test\nBcc: x@y.test",
            message_id="<inbound-13@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch("istota.transport.email.outbound.reply_to_email") as mock_reply,
            patch("istota.transport.email.outbound.send_email") as mock_send,
        ):
            ok = await deliver_email_result(config, task, "")

        assert ok is False
        mock_reply.assert_not_called()
        mock_send.assert_not_called()
        assert _drafts(db_path) == []

    @pytest.mark.asyncio
    async def test_hold_is_attributed_to_the_task_room(self, db_path, tmp_path):
        """`room_token` is what makes the draft card render inline."""
        config = _config(
            db_path, tmp_path,
            users={"alice": UserConfig(email_addresses=["alice@example.com"])},
        )
        with db.get_db(db_path) as conn:
            db.register_room(conn, token="room-abc", user_id="alice", origin="web")
            tid = db.create_task(
                conn, prompt="reply to them", user_id="alice",
                source_type="email", conversation_token="room-abc",
            )
            task = db.get_task(conn, tid)
        _link_inbound_email(
            db_path, task.id, sender="stranger@protonmail.test",
            message_id="<inbound-7@protonmail.test>",
        )
        _write_deferred_output(tmp_path, task)

        with (
            patch("istota.transport.email.outbound.reply_to_email"),
            patch("istota.transport.email.outbound.send_email"),
        ):
            await deliver_email_result(config, task, "")

        draft = _drafts(db_path)[0]
        assert draft["room_token"] == "room-abc"
        assert draft["origin_target"] == "room:room-abc"
