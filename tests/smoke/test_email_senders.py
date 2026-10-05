"""One mail per sender class, through the deployed daemon, every dimension asserted.

The email-on-rooms design decides who is asked, who is held, which room a mail
lands in and what the host is told, by the sender's standing. This file sends
one mail per standing on the `email` profile (`verify` self-claims, an ntfy
stub, the people `email_people` seeds) and checks all eight dimensions of
`tests/support/email_flow.Expected` for it. Expected values cite the function
they come from.
"""

from __future__ import annotations

import time

import pytest

from istota.notifications.resolvers.task_alert import flatten_body
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


def held_task(stack, sent: flow.Sent, timeout: float = flow.DEFAULT_TIMEOUT) -> dict:
    """The task the gate parked for `sent`, once it is parked."""
    deadline = time.monotonic() + timeout
    row = None
    while row is None or row.get("task_id") is None:
        row = stack.probe.processed(sent.message_id)
        if row is not None and row.get("task_id") is not None:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"no held task for {sent.message_id}: {row!r}")
        time.sleep(flow.POLL_INTERVAL)
    task = stack.probe.wait_for_task(
        status="pending_confirmation", task_id=row["task_id"], timeout=timeout,
    )
    # `wait_for_task` also returns on a terminal status.
    assert task["status"] == "pending_confirmation", (
        f"the gate did not hold {sent.message_id}: task {task['id']} is {task['status']}"
    )
    return task


def prompt_push(task: dict) -> str:
    """What the gate prompt's push carries: the composed prompt through
    `flatten_body` (`inbound._deliver_confirmation_prompts`). It names the
    sender, the subject and the task, never the mail's body."""
    return flatten_body(task["confirmation_prompt"])


def _dmarc_push(stack, task: dict, verdict: str) -> str:
    """The DMARC canary's push: its alert message through `flatten_body`
    (`inbound._deliver_dmarc_alerts`), read back off the `dmarc:<verdict>` row,
    which stores the same message (`inbound._write_dmarc_rows`).

    The row is keyed per user and verdict, and the in-process alert dedup per
    sender and verdict, so each verdict is exercised once per session stack.
    """
    rows = stack.probe.notifications(task["user_id"], dedup_key=f"dmarc:{verdict}")
    assert len(rows) == 1, rows
    return flatten_body(rows[0]["body"])


def _assert_self_claim_prompt(task: dict) -> None:
    """A self-claim is offered a plain yes or no, never `yes trust`, which
    would exempt the host's address for a spoofer too (`inbound.poll_emails`,
    the `claims_to_be_user` branch of the prompt)."""
    prompt = task["confirmation_prompt"] or ""
    assert prompt.startswith("Email from unverified sender "), prompt
    assert flow.confirm_command_from(prompt, "yes") == f"!confirm {task['id']}"
    assert flow.confirm_command_from(prompt, "no") == f"!confirm {task['id']} no"
    with pytest.raises(AssertionError, match="no `yes trust`"):
        flow.confirm_command_from(prompt, "yes trust")


def assert_unknown_sender_prompt(task: dict, sender: str) -> None:
    """Anyone not claiming to be the routed user is an `unknown sender`, and
    is offered `yes trust` as well."""
    prompt = task["confirmation_prompt"] or ""
    assert prompt.startswith(f"Email from unknown sender {sender}\n"), prompt
    assert flow.confirm_command_from(prompt, "yes trust") == f"!confirm {task['id']} trust"


def _held_at_the_plus_address(stack, email_people, sent: flow.Sent) -> None:
    """A mail the gate held on the plus-address route from someone not
    claiming to be the host. The prompt is the one alert, on both surfaces of
    the alert route; the `confirmation` row is written and not pushed besides,
    because the prompt reached someone (`inbound._deliver_confirmation_prompts`
    delivers the row only where the prompt did not)."""
    task = held_task(stack, sent)
    assert_unknown_sender_prompt(task, sent.sender)
    flow.assert_outcome(stack, sent, flow.Expected(
        processed=flow.Processed(
            routing_method="plus_address", user_id=email_people.host_id,
            host_asked=False, sender_check="verified",
        ),
        task=flow.TaskState(status="pending_confirmation", host_absent=False),
        room="none", participants=None,
        transcript=flow.Transcript(incoming=None, bot=None),
        reply=None, note=None,
        notices=flow.Notices(
            rows=frozenset({("confirmation", "task:{task}")}),
            pushes=(prompt_push(task),),
            alert_mails=1,
        ),
    ))


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

    def test_a_failing_stamp_on_the_hosts_address_is_held(self, stack, email_people):
        """The host's address under a DMARC fail is a self-claim the gate holds.

        Under `verify`, `_own_address_claim_counts` accepts only a pass under
        our own authserv-id, so `is_trusted_email_sender(...,
        include_own_addresses=False)` decides and finds nothing. A held mail is
        in no room until it is approved (`suppress_transcript_mirror`, then
        `threads.admit_approved_mail`). The alert route (email and ntfy)
        carries the prompt and the DMARC canary, each by its own composition.
        """
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.person("spoofed_host", nonce), to=[mail.BOT_ADDRESS],
            subject=f"spoofed {nonce}", text=f"a forged question {nonce}",
            marker=nonce,
        )
        task = held_task(stack, sent)
        _assert_self_claim_prompt(task)
        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="sender_match", user_id=email_people.host_id,
                host_asked=False, sender_check="failed",
            ),
            task=flow.TaskState(status="pending_confirmation", host_absent=False),
            room="none", participants=None,
            transcript=flow.Transcript(incoming=None, bot=None),
            reply=None, note=None,
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}"),
                                ("task_alert", "dmarc:fail")}),
                pushes=(prompt_push(task), _dmarc_push(stack, task, "fail")),
                alert_mails=2,
            ),
        ))

    def test_no_stamp_on_the_hosts_address_is_held(self, stack, email_people):
        """No `Authentication-Results` at all: held the same way, with
        `sender_check = "none"` (`inbound.sender_check_for`) and the canary's
        `unstamped` verdict."""
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.Correspondent(email_people.host_address, "Test User", None),
            to=[mail.BOT_ADDRESS],
            subject=f"unstamped {nonce}", text=f"an unstamped question {nonce}",
            marker=nonce,
        )
        task = held_task(stack, sent)
        _assert_self_claim_prompt(task)
        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="sender_match", user_id=email_people.host_id,
                host_asked=False, sender_check="none",
            ),
            task=flow.TaskState(status="pending_confirmation", host_absent=False),
            room="none", participants=None,
            transcript=flow.Transcript(incoming=None, bot=None),
            reply=None, note=None,
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}"),
                                ("task_alert", "dmarc:unstamped")}),
                pushes=(prompt_push(task), _dmarc_push(stack, task, "unstamped")),
                alert_mails=2,
            ),
        ))


@EMAIL
class TestCorrespondents:
    def test_a_trusted_sender_mints_a_thread_and_is_answered(self, stack, email_people):
        """A sender on the host's `--trusted-sender` pattern, at the plus-address.

        Trusted, so not held (`config.is_trusted_email_sender`, the pattern
        branch), and the card says so. Not private mail, so its thread room is
        minted on receipt with the sender as a guest of the host; the host is
        not on the mail, so is no participant. The reply goes out with no
        draft because every recipient is trusted (`mail.outbound_policy`
        under `untrusted`). The host was not on the mail, so `host_absent`
        holds and a note is due (`private_replies.email_note_due`); lean has
        no private room, so it is the `private-note:<task>` bell row, pushed
        with the fixed pointer (`private_replies._bell_note`, #638).
        """
        nonce = flow.new_nonce()
        answer = f"the trusted answer {nonce}"
        stack.script([flow.route(nonce, [flow.email_answer(answer)])])
        sender = flow.person("trusted", nonce)
        plus = mail.tagged(email_people.host_id)
        sent = flow.send(
            stack, sender, to=[plus],
            subject=f"trusted {nonce}", text=f"a trusted question {nonce}",
            marker=nonce,
        )
        seen = flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(sender.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(
                    to=(plus,), cc=(), sender_check="verified", trusted=True,
                ),
                bot=flow.BotRow(body=answer, mail_state="sent"),
            ),
            reply=flow.Reply(to=(sender.address,), cc=()),
            note=flow.Note(outcome="sent", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("task_alert", "private-note:{task}")}),
                pushes=(flow.PRIVATE_NOTE_POINTER,),
                alert_mails=1,
            ),
        ))
        # The note quotes the sender's words; the bell row is the one place.
        assert sent.text in seen.note_body
        assert stack.probe.drafts(
            email_people.host_id, id_above=stack.mark.get("outbound_drafts"),
        ) == []

    def test_a_stranger_at_the_plus_address_is_held(self, stack, email_people):
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.person("stranger", nonce), to=[mail.tagged(email_people.host_id)],
            subject=f"stranger {nonce}", text=f"a stranger's question {nonce}",
            marker=nonce,
        )
        _held_at_the_plus_address(stack, email_people, sent)

    def test_a_stranger_at_the_bare_address_is_discarded(self, stack, email_people):
        """No plus-address, no configured sender, no thread: nothing routes it,
        and the ledger row is all there is (`inbound.poll_emails`, step 4)."""
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.person("stranger", nonce), to=[mail.BOT_ADDRESS],
            subject=f"discarded {nonce}", text=f"nobody's question {nonce}",
            marker=nonce,
        )
        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="discarded", user_id=None,
                host_asked=False, sender_check=None,
            ),
            task=None, room="none", participants=None,
            transcript=flow.Transcript(incoming=None, bot=None),
            reply=None, note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))

    def test_a_quiet_sender_is_filed_without_a_task(self, stack, email_people):
        """A `--quiet-sender` match is filed after routing and before the gate
        (`config.is_quiet_email_sender`): no task, no room, no alert."""
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.person("quiet", nonce), to=[mail.tagged(email_people.host_id)],
            subject=f"quiet {nonce}", text=f"a quiet newsletter {nonce}",
            marker=nonce,
        )
        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="quiet", user_id=email_people.host_id,
                host_asked=False, sender_check=None,
            ),
            task=None, room="none", participants=None,
            transcript=flow.Transcript(incoming=None, bot=None),
            reply=None, note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))

    def test_alice_at_the_hosts_plus_address_is_a_stranger_to_the_host(
        self, stack, email_people,
    ):
        """Another istota user is not a trusted sender for the host.

        `config.is_trusted_email_sender(user_id, ...)` asks about the routed
        user only: the host's own addresses, the host's patterns and the
        host's runtime trust rows. alice's address is in her own profile and
        in none of the host's, so her mail at the host's plus-address is held
        exactly as a stranger's is. `mail.support.sender_claims_to_be_user`
        checks the routed user's addresses too, so she is an `unknown sender`
        offered `yes trust`, not a self-claim. Her stamp passes, so the card's
        check reads `verified` (`inbound.sender_check_for`).
        """
        nonce = flow.new_nonce()
        sent = flow.send(
            stack, flow.person("alice", nonce), to=[mail.tagged(email_people.host_id)],
            subject=f"alice {nonce}", text=f"alice's question {nonce}",
            marker=nonce,
        )
        _held_at_the_plus_address(stack, email_people, sent)
