"""Stage 4 — the gate fires.

The predicate and the store were built and tested in stages 2 and 3 with
nothing calling them. This file covers the wiring: the email skill's outward
verbs consult the predicate, a hold writes a row and reaches no SMTP server, the
`!drafts` command answers a draft from a push surface, and the scheduler tells
the user about one they have forgotten.

The load-bearing assertion in most of these is the *negative* one — that
`_send_smtp` was never called. A gate that writes a draft row and also sends the
mail is worse than no gate: it produces the paperwork of a decision the user was
never given.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from istota import db, outbound_drafts as drafts
from istota.config import Config, EmailConfig, UserConfig
from istota.skills.email import Email, cmd_reply, cmd_send

OWN = "alice@example.com"
TRUSTED = "colleague@partner.example.org"
STRANGER = "stranger@example.invalid"
OTHER_STRANGER = "someone-else@example.invalid"
BOT = "bot@test.invalid"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "mount" / "Users" / "alice"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def config(tmp_path, workspace, monkeypatch):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        email=EmailConfig(
            enabled=True,
            imap_host="imap.test", imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email=BOT,
            outbound_approval_floor="untrusted",
        ),
        nextcloud_mount_path=tmp_path / "mount",
        users={
            "alice": UserConfig(
                display_name="Alice",
                email_addresses=[OWN],
                trusted_email_senders=["*@partner.example.org"],
            ),
            "bob": UserConfig(display_name="Bob"),
        },
    )
    db.init_db(cfg.db_path)
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    return cfg


@pytest.fixture
def skill_env(config, monkeypatch, tmp_path):
    """What the skill proxy hands the CLI: identity, credentials, host roots."""
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(tmp_path / "mount"))
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    for k, v in {
        "SMTP_HOST": "smtp.test", "SMTP_FROM": BOT,
        "IMAP_HOST": "imap.test", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(k, v)
    return config


class _Args:
    """argparse namespace stand-in — the CLI verbs read attributes, not a dict."""

    def __init__(self, **kw):
        defaults = {
            "to": STRANGER, "subject": "Re: Invite", "body": "Tuesday at four.",
            "body_file": None, "html": False, "cc": None, "bcc": None,
            "attach": None, "reply_to": None, "command": "send",
            "scope": "all", "id": "1", "all": False,
        }
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _pending(config):
    with db.get_db(config.db_path) as conn:
        return drafts.pending_for_user(conn, "alice")


# ---------------------------------------------------------------------------
# 1. `send` — the gate decides, and a hold reaches no SMTP server
# ---------------------------------------------------------------------------


class TestSendGate:
    def test_an_untrusted_recipient_is_held_and_nothing_is_sent(self, skill_env):
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args())

        smtp.assert_not_called()
        assert result["status"] == "held"
        assert result["needs_confirmation"] is True
        assert result["reason"] == "untrusted_recipient"
        assert result["held_recipients"] == [STRANGER]
        assert "message_id" not in result

        held = _pending(skill_env)
        assert len(held) == 1
        assert held[0].to_addrs == [STRANGER]
        assert held[0].subject == "Re: Invite"
        assert held[0].body == "Tuesday at four."
        assert held[0].hold_reason == "untrusted_recipient"

    def test_a_trusted_recipient_sends_and_writes_no_row(self, skill_env):
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED))

        smtp.assert_called_once()
        assert result["status"] == "ok"
        assert result["message_id"]
        assert _pending(skill_env) == []

    def test_the_users_own_address_sends(self, skill_env):
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=OWN))

        smtp.assert_called_once()
        assert result["status"] == "ok"
        assert _pending(skill_env) == []

    def test_one_untrusted_recipient_holds_the_whole_message(self, skill_env):
        """Never the trusted subset. A partial send delivers something the user
        never read, under a subject line implying everyone got it."""
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED, cc=STRANGER))

        smtp.assert_not_called()
        assert result["status"] == "held"
        assert result["held_recipients"] == [STRANGER]
        held = _pending(skill_env)[0]
        assert held.to_addrs == [TRUSTED]
        assert held.cc_addrs == [STRANGER]

    def test_a_bcc_recipient_is_checked_too(self, skill_env):
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED, bcc=STRANGER))

        smtp.assert_not_called()
        assert _pending(skill_env)[0].bcc_addrs == [STRANGER]
        assert result["held_recipients"] == [STRANGER]

    def test_every_held_recipient_is_named(self, skill_env):
        with patch("istota.skills.email._send_smtp"):
            result = cmd_send(
                _Args(to=STRANGER, cc=f"{TRUSTED},{OTHER_STRANGER}"),
            )
        assert result["held_recipients"] == [STRANGER, OTHER_STRANGER]
        assert STRANGER in result["message"]
        assert OTHER_STRANGER in result["message"]

    def test_the_reply_to_header_survives_the_hold(self, skill_env):
        """It decides where the recipient's answer lands. Dropping it on the way
        through the hold would silently reroute the conversation."""
        with patch("istota.skills.email._send_smtp"):
            cmd_send(_Args(reply_to="desk@example.com"))

        assert _pending(skill_env)[0].reply_to == "desk@example.com"

    def test_an_html_send_is_held_as_html(self, skill_env):
        with patch("istota.skills.email._send_smtp"):
            cmd_send(_Args(html=True, body="<p>Tuesday.</p>"))

        held = _pending(skill_env)[0]
        assert held.html is True
        assert held.body == "<p>Tuesday.</p>"

    def test_an_all_policy_holds_a_trusted_recipient_too(self, skill_env):
        skill_env.users["alice"].outbound_approval = "all"
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED))

        smtp.assert_not_called()
        assert result["reason"] == "all_mode"
        assert "policy is 'all'" in result["message"]

    def test_an_off_policy_sends_to_anyone(self, skill_env):
        skill_env.email.outbound_approval_floor = "off"
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=STRANGER))

        smtp.assert_called_once()
        assert result["status"] == "ok"
        assert _pending(skill_env) == []


# ---------------------------------------------------------------------------
# 2. `reply` / `reply-all` — held with the threading headers already snapshotted
# ---------------------------------------------------------------------------


def _fetched(sender=STRANGER, **kw):
    return Email(
        id="7", subject="Invite", sender=sender,
        date="Mon, 10 Aug 2026 00:00:00 +0000", body="Are we still on?",
        attachments=[], message_id="<parent@example.invalid>",
        references="<root@example.invalid>",
        to=(BOT,), cc=(), body_text="Are we still on?", **kw
    )


class TestReplyGate:
    def test_a_reply_to_an_untrusted_sender_is_held(self, skill_env):
        with patch("istota.skills.email._read_scoped",
                   return_value=(_fetched(), None)), \
             patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_reply(_Args(command="reply", body="Yes, Tuesday."))

        smtp.assert_not_called()
        assert result["status"] == "held"
        assert result["held_recipients"] == [STRANGER]

    def test_the_threading_headers_are_snapshotted_at_hold_time(self, skill_env):
        """Re-deriving them at release would be a second IMAP round trip against
        a message that may have moved, been refiled or been deleted."""
        with patch("istota.skills.email._read_scoped",
                   return_value=(_fetched(), None)), \
             patch("istota.skills.email._send_smtp"):
            cmd_reply(_Args(command="reply", body="Yes, Tuesday."))

        held = _pending(skill_env)[0]
        assert held.in_reply_to == "<parent@example.invalid>"
        assert held.references == "<root@example.invalid> <parent@example.invalid>"
        assert held.subject == "Re: Invite"
        assert held.to_addrs == [STRANGER]

    def test_a_reply_to_a_trusted_sender_still_sends(self, skill_env):
        with patch("istota.skills.email._read_scoped",
                   return_value=(_fetched(sender=TRUSTED), None)), \
             patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_reply(_Args(command="reply", body="Yes."))

        smtp.assert_called_once()
        assert result["status"] == "ok"
        assert _pending(skill_env) == []

    def test_reply_all_holds_on_an_untrusted_cc(self, skill_env):
        mail = _fetched(sender=TRUSTED)
        mail.cc = (OTHER_STRANGER,)
        with patch("istota.skills.email._read_scoped", return_value=(mail, None)), \
             patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_reply(_Args(command="reply-all", body="Yes."))

        smtp.assert_not_called()
        assert result["held_recipients"] == [OTHER_STRANGER]
        assert _pending(skill_env)[0].cc_addrs == [OTHER_STRANGER]


# ---------------------------------------------------------------------------
# 3. The gate must not be reachable around, and must never fail open
# ---------------------------------------------------------------------------


class TestGateFailsClosed:
    def test_an_unreachable_database_refuses_to_send(self, skill_env):
        """A gate that answers with half its inputs missing is a gate that fails
        open for exactly the caller whose database was unavailable."""
        skill_env.db_path = skill_env.db_path.parent / "nonexistent" / "istota.db"
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args())

        smtp.assert_not_called()
        assert result["status"] == "error"
        assert "Refusing to send" in result["error"]

    def test_an_off_policy_does_not_need_the_database_at_all(self, skill_env):
        """`off` is the setting an operator chooses to keep the old behaviour,
        and the old behaviour never opened the framework DB on a send. Reading
        the policy after opening the connection made an unreachable — or merely
        busy — database fail sends on an instance that had switched the gate
        off."""
        skill_env.email.outbound_approval_floor = "off"
        skill_env.db_path = skill_env.db_path.parent / "nonexistent" / "istota.db"
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=STRANGER))

        smtp.assert_called_once()
        assert result["status"] == "ok"

    def test_no_user_identity_refuses_to_send(self, skill_env, monkeypatch):
        monkeypatch.delenv("ISTOTA_USER_ID")
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args())

        smtp.assert_not_called()
        assert result["status"] == "error"
        assert "ISTOTA_USER_ID" in result["error"]

    def test_an_unloadable_config_refuses_to_send(self, skill_env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("config.toml is unreadable")

        monkeypatch.setattr("istota.config.load_config", boom)
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args())

        smtp.assert_not_called()
        assert result["status"] == "error"

    def test_a_hold_exits_zero(self, skill_env):
        """A hold is a successful outcome of the verb. A non-zero exit invites
        the model to retry with different arguments, which is the one thing it
        must not do with a message the user is deciding about."""
        from istota.skills.email import main

        with patch("istota.skills.email._send_smtp"):
            main([
                "send", "--to", STRANGER, "--subject", "Re: Invite",
                "--body", "Tuesday at four.",
            ])  # no SystemExit

        assert len(_pending(skill_env)) == 1

    def test_a_refused_send_exits_non_zero(self, skill_env, monkeypatch, capsys):
        """The other half of the contract: a gate that could not run is a
        failure of the verb, so the task fails rather than reporting success."""
        from istota.skills.email import main

        monkeypatch.delenv("ISTOTA_USER_ID")
        with patch("istota.skills.email._send_smtp") as smtp, \
             pytest.raises(SystemExit) as exc:
            main([
                "send", "--to", STRANGER, "--subject", "Re: Invite",
                "--body", "Tuesday at four.",
            ])

        smtp.assert_not_called()
        assert exc.value.code == 1
        import json
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_an_unstorable_address_refuses_rather_than_sends(self, skill_env):
        """The gate's recipient expansion and the store's are two parsers over
        the same string, and the store's is the stricter (`@x.invalid` has a
        domain but no local part). Where they disagree the answer must be a
        refusal, never a send."""
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to="@example.invalid"))

        smtp.assert_not_called()
        assert result["status"] == "error"
        assert _pending(skill_env) == []

    def test_send_has_no_confirmed_flag(self):
        """A self-supplied flag is not a gate. The failure being fixed is a
        model talking itself past a rule, and it would supply the flag in
        exactly that state."""
        from istota.skills.email import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "send", "--to", STRANGER, "--subject", "S", "--body", "b",
                "--confirmed",
            ])
        with pytest.raises(SystemExit):
            parser.parse_args(["reply", "1", "--body", "b", "--confirmed"])


class TestHeldAttachments:
    def test_an_attachment_in_the_workspace_is_held_by_resolved_path(
        self, skill_env, workspace,
    ):
        f = workspace / "report.txt"
        f.write_text("data")
        with patch("istota.skills.email._send_smtp"):
            cmd_send(_Args(attach=[str(f)]))

        held = _pending(skill_env)[0]
        assert held.attachments == [str(f.resolve())]

    def test_an_attachment_outside_the_workspace_is_refused_not_held(
        self, skill_env, tmp_path,
    ):
        """`release` re-confines to the workspace, so holding this would produce
        a draft the user can approve and never send. Refusing now leaves the
        model able to retry without it."""
        outside = tmp_path / "secret.txt"
        outside.write_text("data")
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(attach=[str(outside)]))

        smtp.assert_not_called()
        assert result["status"] == "error"
        assert _pending(skill_env) == []

    def test_an_unscoped_attachment_is_refused_on_an_ungated_send_too(
        self, skill_env, tmp_path,
    ):
        """The scoping is not part of the approval decision.

        The CLI is spawned host-side by the proxy with the daemon's whole
        filesystem view, so a path the model chose is an arbitrary read unless
        it is scoped — and `_attach_files` does a bare `read_bytes` on whatever
        it is handed. Checking only on the held branch would mean
        `--attach /etc/istota/config.toml` is refused to a stranger and mailed
        to a colleague.
        """
        secret = tmp_path / "config.toml"
        secret.write_text("smtp_password = 'hunter2'")
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED, attach=[str(secret)]))

        smtp.assert_not_called()
        assert result["status"] == "error"

    def test_the_gate_being_off_does_not_unscope_attachments(
        self, skill_env, tmp_path,
    ):
        skill_env.email.outbound_approval_floor = "off"
        secret = tmp_path / "config.toml"
        secret.write_text("smtp_password = 'hunter2'")
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=STRANGER, attach=[str(secret)]))

        smtp.assert_not_called()
        assert result["status"] == "error"

    def test_a_workspace_attachment_sends_on_the_ungated_path(
        self, skill_env, workspace,
    ):
        f = workspace / "report.txt"
        f.write_text("data")
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED, attach=[str(f)]))

        smtp.assert_called_once()
        assert result["status"] == "ok"
        assert result["attachments"] == ["report.txt"]

    def test_a_symlinked_attachment_is_refused(self, skill_env, workspace, tmp_path):
        target = tmp_path / "secret.txt"
        target.write_text("data")
        link = workspace / "innocent.txt"
        link.symlink_to(target)
        with patch("istota.skills.email._send_smtp") as smtp:
            result = cmd_send(_Args(to=TRUSTED, attach=[str(link)]))

        smtp.assert_not_called()
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# 4. The draft knows where it came from
# ---------------------------------------------------------------------------


class TestTaskAttribution:
    def _task_in_room(self, config, user_id="alice"):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room-1", user_id, origin="web", name="General")
            return db.create_task(
                conn, prompt="reply to them", user_id=user_id,
                source_type="web", conversation_token="room-1",
            )

    def test_the_draft_carries_the_room_and_the_origin_descriptor(
        self, skill_env, monkeypatch,
    ):
        task_id = self._task_in_room(skill_env)
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        with patch("istota.skills.email._send_smtp"):
            result = cmd_send(_Args())

        held = _pending(skill_env)[0]
        assert held.task_id == task_id
        assert held.room_token == "room-1"
        assert held.origin_target == "room:room-1"
        # The room is recorded so the card can render there once stage 6 lands.
        # Until then the message must not send the user looking for it: nothing
        # renders a card and there is no web drafts list, so naming either would
        # have the model confidently point at an empty place.
        assert "General" not in result["message"]
        assert "`!drafts`" in result["message"]

    def test_a_task_belonging_to_another_user_is_not_attributed(
        self, skill_env, monkeypatch,
    ):
        """Identity comes from the task row; the env only chose which row. A
        disagreement means the env is not describing this task."""
        task_id = self._task_in_room(skill_env, user_id="bob")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        with patch("istota.skills.email._send_smtp"):
            cmd_send(_Args())

        held = _pending(skill_env)[0]
        assert held.user_id == "alice"
        assert held.task_id is None
        assert held.room_token is None

    def test_a_draft_with_no_task_is_still_held(self, skill_env):
        with patch("istota.skills.email._send_smtp"):
            result = cmd_send(_Args())

        held = _pending(skill_env)[0]
        assert held.task_id is None
        assert held.room_token is None
        assert "`!drafts`" in result["message"]


# ---------------------------------------------------------------------------
# 5. The stale-draft sweep
# ---------------------------------------------------------------------------


def _age_draft(config, draft_id, hours):
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "UPDATE outbound_drafts SET created_at = datetime('now', ?) "
            "WHERE id = ?",
            (f"-{hours} hours", draft_id),
        )


def _hold_one(config, **overrides):
    kwargs = {
        "user_id": "alice", "task_id": None, "room_token": None,
        "to_addrs": [STRANGER], "cc_addrs": [], "bcc_addrs": [],
        "subject": "Re: Invite", "body": "Tuesday.", "html": False,
        "in_reply_to": None, "references": None, "attachments": [],
        "origin_target": None, "hold_reason": "untrusted_recipient",
    }
    kwargs.update(overrides)
    with db.get_db(config.db_path) as conn:
        return drafts.hold(conn, **kwargs)


def _nagged_at(config, draft_id):
    with db.get_db(config.db_path) as conn:
        return drafts.get(conn, draft_id).nagged_at


class TestStaleDraftSweep:
    def test_a_day_old_draft_is_notified_once(self, config):
        from istota import scheduler

        draft_id = _hold_one(config)
        _age_draft(config, draft_id, 25)

        with patch.object(scheduler, "send_notification", return_value=True) as notify:
            assert scheduler.nag_stale_outbound_drafts(config) == 1
            assert scheduler.nag_stale_outbound_drafts(config) == 0

        assert notify.call_count == 1
        message = notify.call_args.args[2]
        assert STRANGER in message
        assert "Re: Invite" in message
        assert f"!drafts send {draft_id}" in message
        assert notify.call_args.kwargs["purpose"] == "alert"
        assert _nagged_at(config, draft_id) is not None

    def test_a_fresh_draft_is_left_alone(self, config):
        from istota import scheduler

        draft_id = _hold_one(config)
        _age_draft(config, draft_id, 23)

        with patch.object(scheduler, "send_notification", return_value=True) as notify:
            assert scheduler.nag_stale_outbound_drafts(config) == 0
        notify.assert_not_called()
        assert _nagged_at(config, draft_id) is None

    def test_an_answered_draft_is_never_nagged(self, config):
        from istota import scheduler

        draft_id = _hold_one(config)
        _age_draft(config, draft_id, 48)
        with db.get_db(config.db_path) as conn:
            drafts.discard(conn, draft_id)

        with patch.object(scheduler, "send_notification", return_value=True) as notify:
            assert scheduler.nag_stale_outbound_drafts(config) == 0
        notify.assert_not_called()

    def test_an_undeliverable_notice_is_retried_next_sweep(self, config):
        """`send_notification` reports "no destination configured" by returning
        False. Stamping on the decision rather than the delivery would let one
        silent failure swallow the reminder permanently."""
        from istota import scheduler

        draft_id = _hold_one(config)
        _age_draft(config, draft_id, 25)

        with patch.object(scheduler, "send_notification", return_value=False):
            assert scheduler.nag_stale_outbound_drafts(config) == 0
        assert _nagged_at(config, draft_id) is None

        with patch.object(scheduler, "send_notification", return_value=True):
            assert scheduler.nag_stale_outbound_drafts(config) == 1
        assert _nagged_at(config, draft_id) is not None

    def test_a_raising_notification_does_not_block_the_others(self, config):
        from istota import scheduler

        first = _hold_one(config, subject="First")
        second = _hold_one(config, subject="Second")
        _age_draft(config, first, 25)
        _age_draft(config, second, 25)

        def flaky(cfg, user_id, message, **kw):
            if "First" in message:
                raise RuntimeError("transport down")
            return True

        with patch.object(scheduler, "send_notification", side_effect=flaky):
            assert scheduler.nag_stale_outbound_drafts(config) == 1

        assert _nagged_at(config, first) is None
        assert _nagged_at(config, second) is not None


# ---------------------------------------------------------------------------
# 6. `!drafts` — answering the gate from a push surface
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(config):
    """A `CommandContext` per invocation, each on its own connection.

    Its own connection, and committed before the handler returns, because
    `release` opens a second connection and commits its claim before touching
    SMTP — a caller sitting on an open write transaction deadlocks against it.
    """
    from istota.commands import CommandContext

    cms = []

    def make(args, user_id="alice"):
        cm = db.get_db(config.db_path)
        cms.append(cm)
        return CommandContext(
            config=config, conn=cm.__enter__(), user_id=user_id,
            conversation_token="room-1", args=args, surface="talk",
        )

    yield make
    for cm in cms:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


async def _run(ctx_factory, args, user_id="alice"):
    from istota.commands import cmd_drafts

    ctx = ctx_factory(args, user_id=user_id)
    result = await cmd_drafts(ctx)
    ctx.conn.commit()
    return result


@pytest.mark.asyncio
class TestDraftsCommand:
    async def test_nothing_pending(self, ctx_factory):
        assert "No outbound mail" in await _run(ctx_factory, "")

    async def test_bare_drafts_lists_ids_recipients_and_subjects(
        self, config, ctx_factory,
    ):
        first = _hold_one(config, subject="Re: Invite")
        second = _hold_one(config, subject="Re: Quote", to_addrs=[OTHER_STRANGER])

        text = await _run(ctx_factory, "")
        assert f"`#{first}`" in text and f"`#{second}`" in text
        assert STRANGER in text and OTHER_STRANGER in text
        assert "Re: Invite" in text and "Re: Quote" in text

    async def test_send_with_one_pending_needs_no_id(self, config, ctx_factory):
        draft_id = _hold_one(config)
        with patch("istota.skills.email._send_smtp"):
            text = await _run(ctx_factory, "send")

        assert f"Sent #{draft_id}" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, draft_id).status == drafts.STATUS_SENT

    async def test_send_with_two_pending_releases_neither(self, config, ctx_factory):
        first = _hold_one(config)
        second = _hold_one(config, subject="Re: Quote")

        with patch("istota.skills.email._send_smtp") as smtp:
            text = await _run(ctx_factory, "send")

        smtp.assert_not_called()
        assert "say which" in text
        assert f"`#{first}`" in text and f"`#{second}`" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, first).status == drafts.STATUS_PENDING
            assert drafts.get(conn, second).status == drafts.STATUS_PENDING

    async def test_send_by_id_with_several_pending(self, config, ctx_factory):
        first = _hold_one(config)
        second = _hold_one(config, subject="Re: Quote")

        with patch("istota.skills.email._send_smtp"):
            text = await _run(ctx_factory, f"send {second}")

        assert f"Sent #{second}" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, first).status == drafts.STATUS_PENDING
            assert drafts.get(conn, second).status == drafts.STATUS_SENT

    async def test_discard_sends_nothing(self, config, ctx_factory):
        draft_id = _hold_one(config)
        with patch("istota.skills.email._send_smtp") as smtp:
            text = await _run(ctx_factory, "discard")

        smtp.assert_not_called()
        assert f"Discarded #{draft_id}" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, draft_id).status == drafts.STATUS_DISCARDED

    async def test_a_bare_id_says_nothing_about_what_to_do(self, config, ctx_factory):
        """Guessing `send` would mail a message off a typo; guessing `discard`
        would bin the user's own words."""
        draft_id = _hold_one(config)
        with patch("istota.skills.email._send_smtp") as smtp:
            text = await _run(ctx_factory, str(draft_id))

        smtp.assert_not_called()
        assert f"!drafts send {draft_id}" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, draft_id).status == drafts.STATUS_PENDING

    async def test_another_users_draft_is_not_addressable(self, config, ctx_factory):
        """One answer for "no such draft", "not yours" and "already answered" —
        the command must not become an oracle for which ids exist."""
        theirs = _hold_one(config, user_id="bob")
        mine = _hold_one(config)

        with patch("istota.skills.email._send_smtp") as smtp:
            text = await _run(ctx_factory, f"send {theirs}")

        smtp.assert_not_called()
        assert f"Draft #{theirs} isn't waiting" in text
        assert f"`#{mine}`" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, theirs).status == drafts.STATUS_PENDING

    async def test_an_unknown_verb_is_refused(self, config, ctx_factory):
        _hold_one(config)
        text = await _run(ctx_factory, "yeet")
        assert "Don't know what `yeet` means" in text

    async def test_send_works_on_a_caller_holding_an_open_write_transaction(
        self, config, ctx_factory,
    ):
        """The shape Talk actually hands `dispatch`.

        `poll_talk_conversations` passes its own poll connection, which is
        already mid-write when a `!command` is dispatched — the message cache
        upsert and the poll cursor sit in it uncommitted. `release` commits its
        claim on a *second* connection before touching SMTP, so without the
        handler committing first this is second-writer-against-first-writer:
        `database is locked` after the full 30s busy timeout, on the poll loop.
        Every other `!drafts send` test here gives the handler a clean
        connection, which is a shape Talk never produces.
        """
        draft_id = _hold_one(config)
        ctx = ctx_factory("send")
        # Stand in for `upsert_talk_messages` + `set_talk_poll_state`: any write
        # opens the transaction, and the poller has always issued both before it
        # reaches command dispatch.
        ctx.conn.execute(
            "INSERT INTO talk_poll_state (conversation_token, last_known_message_id) "
            "VALUES ('room-1', 7)",
        )
        assert ctx.conn.in_transaction

        from istota.commands import cmd_drafts

        with patch("istota.skills.email._send_smtp"):
            text = await cmd_drafts(ctx)

        assert f"Sent #{draft_id}" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, draft_id).status == drafts.STATUS_SENT

    async def test_an_smtp_failure_leaves_the_draft_pending(self, config, ctx_factory):
        draft_id = _hold_one(config)
        with patch("istota.skills.email._send_smtp",
                   side_effect=OSError("connection refused")):
            text = await _run(ctx_factory, "send")

        assert "still waiting" in text
        with db.get_db(config.db_path) as conn:
            assert drafts.get(conn, draft_id).status == drafts.STATUS_PENDING

    async def test_a_failure_after_the_send_does_not_say_still_waiting(
        self, config, ctx_factory,
    ):
        """The finalize step runs after an irreversible act. A lock or a disk
        fault there leaves the recipient holding the message, so reporting it as
        "failed, try again" tells the user the opposite of what happened — and
        the advised retry is refused, because the row is no longer pending."""
        draft_id = _hold_one(config)
        real_get_db = db.get_db
        sent = {"yet": False}

        def sending(*a, **kw):
            sent["yet"] = True

        def flaky_get_db(path, **kw):
            # Keyed on the send having happened rather than on a call count, so
            # the test pins "any DB failure after SMTP" rather than one
            # particular connection in `release`'s sequence.
            if sent["yet"]:
                raise OSError("disk I/O error")
            return real_get_db(path, **kw)

        with patch("istota.skills.email._send_smtp", side_effect=sending) as smtp, \
             patch("istota.db.get_db", side_effect=flaky_get_db):
            text = await _run(ctx_factory, f"send {draft_id}")

        smtp.assert_called_once()
        assert "was sent" in text
        assert "Do not resend" in text
        assert "still waiting" not in text

    async def test_bcc_is_not_printed_into_the_room(self, config, ctx_factory):
        """`!drafts` is surface-agnostic and works in a multi-user Talk room,
        so the listing must not post the blind-carbon list to everyone in it."""
        _hold_one(config, to_addrs=[STRANGER], bcc_addrs=["secret@example.invalid"])

        text = await _run(ctx_factory, "")
        assert "secret@example.invalid" not in text
        assert "+1 bcc" in text
        assert STRANGER in text


# ---------------------------------------------------------------------------
# 7. End to end — held, then released, with exactly one send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_held_then_approved_sends_the_bytes_the_user_read(
    skill_env, ctx_factory,
):
    """The whole point of holding the artifact rather than the task: approve is
    exactly what was shown, and the model never re-enters the decision."""
    with patch("istota.skills.email._send_smtp") as smtp:
        held = cmd_send(_Args(body="Tuesday at four, at the usual place."))
    smtp.assert_not_called()

    sent = MagicMock()
    with patch("istota.skills.email._send_smtp", sent):
        text = await _run(ctx_factory, f"send {held['draft_id']}")

    sent.assert_called_once()
    message = sent.call_args.args[0]
    assert message["To"] == STRANGER
    assert message["Subject"] == "Re: Invite"
    assert "Tuesday at four, at the usual place." in message.get_content()
    assert f"Sent #{held['draft_id']}" in text

    with db.get_db(skill_env.db_path) as conn:
        row = drafts.get(conn, held["draft_id"])
        assert row.status == drafts.STATUS_SENT
        assert row.sent_message_id
        # The provenance row the direct send path writes, so a reply to the
        # released mail routes back rather than falling to the alerts ladder.
        recorded = conn.execute(
            "SELECT message_id FROM sent_emails WHERE user_id = 'alice'",
        ).fetchall()
        assert [r["message_id"] for r in recorded] == [row.sent_message_id]
