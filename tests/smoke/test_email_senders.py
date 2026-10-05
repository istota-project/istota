"""One mail per sender class, through the deployed daemon, every dimension asserted.

The email-on-rooms design decides who is asked, who is held, which room a mail
lands in and what the host is told, by the sender's standing. This file sends
one mail per standing on the `email` profile (`verify` self-claims, an ntfy
stub, the people `email_people` seeds) and checks all eight dimensions of
`tests/support/email_flow.Expected` for it. Expected values cite the function
they come from.
"""

from __future__ import annotations

import pytest

from testbed.services import mail

from ..support import email_flow as flow

pytestmark = pytest.mark.smoke

EMAIL = pytest.mark.profile("email")


#: Prints the descriptors testuser's `alert` purpose resolves to, read through
#: the daemon's own config loader inside the container.
_ALERT_DESTINATIONS = (
    "from pathlib import Path\n"
    "from istota.config import load_config\n"
    "from istota.notifications.delivery import resolve_destinations\n"
    "config = load_config(Path('/data/config/config.toml'))\n"
    "print(','.join(d.surface + (':' + d.channel if d.channel else '')"
    " for d in resolve_destinations(config, 'testuser', 'alert')))\n"
)


@EMAIL
class TestTheSeededPeople:
    def test_the_alert_route_reaches_email_and_ntfy(self, stack, email_people):
        """Which surfaces a push on testuser's alert route reaches.

        `email_people` passes `--route alert=email,ntfy`: `email` alone
        resolves to the email destination only, and the ntfy secret adds no
        destination (`notifications.delivery.resolve_destinations`). The suite
        reads pushes from both, so both have to be named.
        """
        result = stack.exec(
            ["uv", "run", "python", "-c", _ALERT_DESTINATIONS], timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "email,ntfy"


@EMAIL
class TestTheHostsOwnAddress:
    def test_a_verified_mail_to_the_bare_address_is_answered_in_the_private_room(
        self, stack, email_people,
    ):
        """The host mails the bot alone, from their own address, stamped.

        `sender_match` routes it to testuser (`inbound.py`), `is_private_mail`
        puts it in their private email room rather than a thread, and the
        answer is a reply to the sender alone. `host_asked` is only ever set on
        a thread room (`inbound.py`, ISSUE-607). No note: a note is written for
        a thread room only (`scheduler.process_one_task` gates
        `_write_email_note` on `_thread_token`).

        The room's one participant is the host as its principal, keyed by user
        id (`ingest.record_phone_turn` through `record_inbound`), and the bot's
        row carries an outgoing card like a thread's, at `sent`.
        """
        nonce = flow.new_nonce()
        answer = f"the private answer {nonce}"
        stack.script([flow.route(nonce, [flow.email_answer(answer)])])
        sent = flow.send(
            stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS],
            subject=f"own address {nonce}", text=f"a question of my own {nonce}",
            marker=nonce,
        )

        host = email_people.host_address
        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="sender_match", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="private",
            participants=frozenset({
                (email_people.host_id, "principal", email_people.host_id),
            }),
            transcript=flow.Transcript(
                incoming=flow.Incoming(
                    to=(mail.BOT_ADDRESS,), cc=(), sender_check="verified",
                    trusted=False,
                ),
                bot=flow.BotRow(body=answer, mail_state="sent"),
            ),
            reply=flow.Reply(to=(host,), cc=()),
            note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))
