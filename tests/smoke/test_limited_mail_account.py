"""A limited job's mail from a connected account, on the wire.

`tests/test_egress_closure.py` and `tests/test_skill_proxy.py` call the CLI and
read its answer. Only here does the shipped scheduler run the job, the skill
proxy serve the account's password over its private channel, and a real SMTP
server show what left. A CRON.md job with no approval is the limited task; the
witness for each send is the mailbox it would land in, not the envelope alone,
since a send that answered `held` and also went out would read the same.

Three recipients, one task. The user's own address goes out, From the account:
that shows the job really held a working account, so the other two are stopped
by the egress rule and not by a dead connection. A correspondent the user trusts
is held as a draft and nothing arrives. It has to be a trusted one, since the
default outbound gate holds a stranger for any task. The bot's address is refused
outright and nothing arrives, because that mail would come back as the user's own
attended turn.

The control sends the same two on an attended turn. The trusted correspondent gets
the mail, and the mail to the bot is held for the user rather than refused, so
both answers above are the limit's doing.

Recorded with `egress_limited` answering False: the trusted correspondent's send
answers `ok`, where the job requires `held`.
"""

from __future__ import annotations

import time

import pytest

from testbed.services import mail
from testbed.stack import CONTAINER_CONFIG

from ..support import email_flow
from .test_multi_account_email import (
    PROFILE,
    SLUG,
    USER_ADDRESS,
    USER_ID,
    _bash,
    _connect,
    _envelope,
    _run,
    _wait_for_subject,
)

pytestmark = pytest.mark.smoke

CONFIG_DIR = f"/data/workspace/Users/{USER_ID}/istota/config"
CRON_PATH = f"{CONFIG_DIR}/CRON.md"
TRUSTED = "trusted-friend@ext.test"
#: Long enough that mail really sent would have been delivered to Maddy.
NOT_ARRIVED_SETTLE = 15


def _send(to: str, subject: str) -> str:
    return (
        f"istota-skill email send --account {SLUG} --to {to} "
        f"--subject '{subject}' --body 'the body of {subject}'"
    )


def _latest(service: mail.MailService, mailbox: str) -> int:
    with service.session(mailbox) as box:
        return box.latest_uid()


def _trust(stack, address: str) -> None:
    """A runtime trust row, which each test's reset clears."""
    script = (
        "from pathlib import Path\n"
        "from istota import db\n"
        "from istota.config import load_config\n"
        f"config = load_config(Path({CONTAINER_CONFIG!r}))\n"
        "with db.get_db(config.db_path) as conn:\n"
        f"    db.add_trusted_sender(conn, {USER_ID!r}, {address!r})\n"
        "    conn.commit()\n"
    )
    result = stack.exec(["uv", "run", "python", "-c", script], timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def _run_job(stack, marker: str, name: str, turns: list[dict], timeout: float = 180) -> dict:
    """Install a one-job CRON.md, and return its first occurrence once it ends."""
    stack.script([{"when": marker, "turns": turns}])
    mark = stack.probe.query("SELECT COALESCE(MAX(id), 0) AS id FROM tasks")[0]["id"]
    stack.exec(["mkdir", "-p", CONFIG_DIR])
    body = (
        "```toml\n[[jobs]]\n"
        f'name = "{name}"\ncron = "* * * * *"\nprompt = "{marker} do the rounds"\n'
        "```\n"
    )
    stack.exec(["sh", "-c", f"cat > {CRON_PATH} <<'EOF'\n{body}EOF\n"])
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = stack.probe.query(
                "SELECT * FROM tasks WHERE id > ? AND source_type = 'scheduled' AND prompt LIKE ? "
                "AND status IN ('completed', 'failed') ORDER BY id LIMIT 1",
                [mark, f"%{marker}%"],
            )
            if rows:
                return rows[0]
            time.sleep(2)
        raise AssertionError(f"job {name} never finished\n{stack.logs(120)}")
    finally:
        stack.exec(["rm", "-f", CRON_PATH])


@PROFILE
class TestALimitedJobsConnectedAccount:
    def test_only_the_users_own_address_receives_it(self, stack):
        nonce = email_flow.new_nonce()
        service = stack.service("mail")
        _connect(stack, send=True)
        _trust(stack, TRUSTED)
        outside_before = _latest(service, mail.EXTERNAL_ADDRESS)
        bot_before = _latest(service, mail.BOT_ADDRESS)
        own, held, refused = (f"{kind} {nonce}" for kind in ("own", "held", "refused"))
        calls = {kind: f"call-{kind}-{nonce}" for kind in ("own", "held", "refused")}
        marker = f"[e2e:{nonce}]"

        task = _run_job(stack, marker, f"limited-mail-{nonce}", [
            _bash(calls["own"], _send(USER_ADDRESS, own)),
            _bash(calls["held"], _send(TRUSTED, held)),
            _bash(calls["refused"], _send(mail.BOT_ADDRESS, refused)),
            {"text": "done"},
        ])
        assert task["status"] == "completed", stack.diagnostics(task)
        assert task["egress_posture"] == "private", task

        sent = _envelope(stack, calls["own"])
        assert sent.get("status") == "ok", f"{sent}\n{stack.diagnostics(task)}"
        arrived = _wait_for_subject(service, mail.EXTERNAL_ADDRESS, outside_before, own)
        assert arrived is not None, f"the user's own mail never arrived\n{stack.diagnostics(task)}"
        assert mail.PERSONAL_ADDRESS in arrived.sender.lower(), arrived.sender

        on_hold = _envelope(stack, calls["held"])
        assert on_hold.get("status") == "held", on_hold
        drafts = [d for d in stack.probe.drafts(USER_ID, id_above=stack.mark.get("outbound_drafts"))
                  if d["subject"] == held]
        assert len(drafts) == 1, drafts
        assert drafts[0]["account_id"] is not None, drafts[0]

        refusal = _envelope(stack, calls["refused"])
        assert refusal.get("status") == "error", refusal
        assert refusal.get("reason") == "egress_limited", refusal

        time.sleep(NOT_ARRIVED_SETTLE)
        assert _wait_for_subject(service, mail.EXTERNAL_ADDRESS, outside_before, held, timeout=0) is None
        assert _wait_for_subject(service, mail.BOT_ADDRESS, bot_before, refused, timeout=0) is None
        assert stack.probe.query(
            "SELECT id FROM outbound_drafts WHERE subject = ?", [refused],
        ) == [], "mail to the bot was held, and approving a draft would send it"

    def test_control_an_attended_turn_sends_to_the_trusted_and_holds_for_the_bot(self, stack):
        nonce = email_flow.new_nonce()
        subject = f"attended {nonce}"
        trusted_subject = f"attended trusted {nonce}"
        service = stack.service("mail")
        _connect(stack, send=True)
        _trust(stack, TRUSTED)
        outside_before = _latest(service, mail.EXTERNAL_ADDRESS)
        call = f"call-attended-{nonce}"
        trusted_call = f"call-attended-trusted-{nonce}"

        stack.script([
            _bash(call, _send(mail.BOT_ADDRESS, subject)),
            _bash(trusted_call, _send(TRUSTED, trusted_subject)),
            {"text": "sent"},
        ])
        task = _run(stack, "send that mail to the assistant")

        trusted = _envelope(stack, trusted_call)
        assert trusted.get("status") == "ok", f"{trusted}\n{stack.diagnostics(task)}"
        assert _wait_for_subject(service, mail.EXTERNAL_ADDRESS, outside_before, trusted_subject) is not None

        envelope = _envelope(stack, call)
        assert envelope.get("status") == "held", f"{envelope}\n{stack.diagnostics(task)}"
        assert [d["subject"] for d in stack.probe.drafts(USER_ID, id_above=stack.mark.get("outbound_drafts"))
                if d["subject"] == subject] == [subject]
