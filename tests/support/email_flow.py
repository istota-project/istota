"""Correspondents, mail on the wire, and the eight things every email scenario asserts.

The email suite on the lean stack (`tests/smoke/test_email_*.py`) drives the
deployed daemon with real mail and a scripted model, and reads the outcome back
out of the database, the catch-all mailbox and the ntfy stub. This module is
the vocabulary those files share, so no scenario spells a stamp, a Message-ID
chain or the email answer envelope by hand:

- `person` names a correspondent by kind, with an address carrying the test's
  nonce, so no sender is already on a thread, under a prompt cap, or trusted
  because of an earlier test on the same session stack.
- `send` puts one mail on the wire with an `[e2e:<marker>]` in its body, which
  is what a routed script turn (`route`) keys on.
- `Expected` names a value for each of the eight dimensions, with no defaults,
  and `assert_outcome` checks all of them and reports every mismatch.

The note's outcome lines and its push pointer are imported from the product
rather than restated, so an expectation cannot drift from the code it
describes; the probe's restated strings are held equal by
`tests/test_testbed_probe_email.py`. Plain functions and dataclasses; no pytest import, so a
failure is an `AssertionError` or a `TimeoutError` naming what it last saw.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr

from istota.confirmations import request_subject
from istota.notifications.resolvers.task_alert import PRIVATE_NOTE_POINTER, flatten_body
from istota.rooms.private_replies import NOTE_OUTCOMES
from testbed.services import mail

#: The stack's own user and the second istota user `email_people` seeds.
HOST_ID = "testuser"
HOST_ADDRESS = "testuser@ext.test"
ALICE_ID = "alice"
ALICE_ADDRESS = "alice@ext.test"

#: The domains `email_people` gives a standing to, through testuser's profile
#: patterns (`--trusted-sender`, `--quiet-sender`). A stranger's domain is on
#: no list.
TRUSTED_DOMAIN = "trusted.test"
QUIET_DOMAIN = "quiet.test"
STRANGER_DOMAIN = "stranger.test"

#: The bot's own mail domain. Everything sent there lands in the bot mailbox;
#: the bot's mail to anyone else lands in the catch-all, from this domain.
BOT_DOMAIN = mail.BOT_ADDRESS.rsplit("@", 1)[1]

#: The `authserv_id` `MailService.config_env` gives the daemon. A stamp naming
#: any other server is ignored under `verify`, which is the point of setting it.
AUTHSERV_ID = mail.SERVICE_NAME

#: What the scheduler logs when it writes an email note
#: (`scheduler._write_email_note`).
NOTE_LOG_LINE = "Private note to the host: "

#: What `UserWorker.run` logs once `process_one_task` has returned, which is
#: after the reply, the note and the note's push. The one marker a test can
#: read that comes after the note step: nothing in the database does. It is in
#: the daemon's log, not a table, so `worker_done` reads `Stack.logs`.
WORKER_DONE_RE = r"Worker [^/\s]+/[a-z]+: task {task_id} (completed|failed)\b"

DEFAULT_TIMEOUT = 90.0
POLL_INTERVAL = 2.0

#: How long a "no reply" claim watches the mailbox, and a "nothing was pushed"
#: claim waits on a mail no worker ran (held at the gate): two of the lean
#: profiles' five-second mail polls. A bounded settle, not an event, because
#: a reply may still be passing through the mail server after the worker's
#: line, and nothing the probe can read follows the gate's prompt delivery.
NEGATIVE_SETTLE = 10.0

#: How long the exact push and alert-mail sets are watched past the expected
#: count, so one extra push sent right behind them is still counted.
POST_COUNT_SETTLE = 3.0


def stamp(address: str, verdict: str = "pass") -> str:
    """An `Authentication-Results` value for `address`'s domain.

    `pass` is the full SPF, DKIM and DMARC pass a receiving MTA would write;
    `fail` is a DMARC fail. Built per address, because `header.from` has to
    name the domain the `From:` claims.
    """
    domain = address.rsplit("@", 1)[-1]
    if verdict == "pass":
        return (
            f"{AUTHSERV_ID}; spf=pass smtp.mailfrom={domain}; dkim=pass; "
            f"dmarc=pass header.from={domain}"
        )
    if verdict == "fail":
        return f"{AUTHSERV_ID}; dmarc=fail header.from={domain}"
    raise ValueError(f"unknown verdict {verdict!r}; expected 'pass' or 'fail'")


#: A clean and a failing verdict for the stack user's own domain, as header
#: dicts for `MailService.send(headers=...)`. Moved here from
#: `tests/full/test_email_attachments.py`, which imports them.
PASSING_STAMP = {"Authentication-Results": stamp(HOST_ADDRESS, "pass")}
FAILING_STAMP = {"Authentication-Results": stamp(HOST_ADDRESS, "fail")}


def new_nonce() -> str:
    """A short per-test token for addresses, subjects and markers."""
    return uuid.uuid4().hex[:10]


def marker_text(marker: str) -> str:
    """The marker as it appears in a mail body, and what a route keys on."""
    return f"[e2e:{marker}]"


def route(marker: str, turns: list[dict]) -> dict:
    """A routed script item: the request carrying `marker` gets `turns`."""
    return {"when": marker_text(marker), "turns": list(turns)}


# -- correspondents -----------------------------------------------------------


@dataclass(frozen=True)
class Correspondent:
    address: str
    name: str | None = None
    #: An `Authentication-Results` value, or None to send no such header.
    stamp: str | None = None


PERSON_KINDS = ("host", "alice", "trusted", "quiet", "stranger", "spoofed_host")


def person(kind: str, nonce: str) -> Correspondent:
    """A correspondent of one standing, unique to `nonce` where it can be.

    `host` and `alice` are fixed addresses, since they are configured users;
    `spoofed_host` is the host's address with a failing stamp, which is what a
    forged self-claim looks like under `verify`. Everyone else's local part is
    the nonce, at a domain whose standing `email_people` set up.
    """
    if kind == "host":
        return Correspondent(HOST_ADDRESS, "Test User", stamp(HOST_ADDRESS))
    if kind == "alice":
        return Correspondent(ALICE_ADDRESS, "Alice", stamp(ALICE_ADDRESS))
    if kind == "spoofed_host":
        return Correspondent(HOST_ADDRESS, "Test User", stamp(HOST_ADDRESS, "fail"))
    domains = {"trusted": TRUSTED_DOMAIN, "quiet": QUIET_DOMAIN,
               "stranger": STRANGER_DOMAIN}
    if kind not in domains:
        raise ValueError(f"unknown kind {kind!r}; expected one of {PERSON_KINDS}")
    address = f"{kind}-{nonce}@{domains[kind]}"
    return Correspondent(address, None, stamp(address))


# -- the wire -------------------------------------------------------------------


@dataclass(frozen=True)
class Sent:
    message_id: str
    marker: str
    subject: str
    sender: str
    #: The text the sender wrote, without the marker: what must never reach a
    #: push or a room it was not addressed to.
    text: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    #: The `References` this mail carried, so a reply to it can extend the chain.
    references: str | None
    #: The catch-all's highest UID just before this mail went out. Everything
    #: the bot sent because of it is above this.
    outbox_uid: int


def _mail(stack) -> mail.MailService:
    return stack.service("mail")


def outbox_uid(stack) -> int:
    with _mail(stack).session(mail.EXTERNAL_ADDRESS) as outbox:
        return outbox.latest_uid()


def sent_markers(stack) -> set[str]:
    """The markers `send` put on the wire since the set was last cleared.

    Kept on the stack so the unmatched-request check can tell this test's own
    requests from a daemon job that quotes an earlier test's mail: the nightly
    memory extraction reads a day of conversation, markers included.
    """
    markers = getattr(stack, "_e2e_sent_markers", None)
    if markers is None:
        markers = set()
        stack._e2e_sent_markers = markers
    return markers


def send(
    stack,
    sender: Correspondent,
    *,
    to: list[str],
    cc: list[str] | tuple[str, ...] = (),
    subject: str,
    text: str,
    reply_to_msg: Sent | mail.ReceivedMessage | None = None,
    with_references: bool = True,
    marker: str,
) -> Sent:
    """Send one mail through the stack's mail server, and say what was sent.

    The body is `text` followed by the marker on its own line. A reply carries
    `In-Reply-To` naming `reply_to_msg` (a mail this test sent, or one it read
    back off the wire) and, unless `with_references` is False, `References`
    extending its chain. The stamp, when the correspondent has one, goes on the
    wire verbatim.
    """
    sent_markers(stack).add(marker_text(marker))
    references = None
    in_reply_to = None
    if reply_to_msg is not None:
        in_reply_to = reply_to_msg.message_id
        if with_references:
            chain = (reply_to_msg.references or "").split()
            references = " ".join([*chain, reply_to_msg.message_id])
    before = outbox_uid(stack)
    message_id = _mail(stack).send(
        from_addr=sender.address,
        from_name=sender.name,
        to_addr=list(to),
        cc=list(cc),
        subject=subject,
        body=f"{text}\n\n{marker_text(marker)}\n",
        in_reply_to=in_reply_to,
        references=references,
        headers=(
            {"Authentication-Results": sender.stamp} if sender.stamp else None
        ),
    )
    return Sent(
        message_id=message_id, marker=marker, subject=subject,
        sender=sender.address, text=text, to=tuple(to), cc=tuple(cc),
        references=references, outbox_uid=before,
    )


def bot_mail_since(stack, after_uid: int) -> list[mail.ReceivedMessage]:
    """Every message in the catch-all above `after_uid` that the bot sent.

    By the `From:` domain, because a multi-party mail's human copies land in
    the catch-all too (`mail.send` delivers to every recipient).
    """
    with _mail(stack).session(mail.EXTERNAL_ADDRESS) as outbox:
        found = outbox.fetch_new_since(after_uid)
    return [
        message for message in found
        if parseaddr(message.sender)[1].lower().endswith("@" + BOT_DOMAIN)
    ]


def wait_for_reply(
    stack, *, after_uid: int, subject: str, timeout: float = DEFAULT_TIMEOUT,
) -> mail.ReceivedMessage:
    """The first mail from the bot above `after_uid` with this subject.

    Raises `TimeoutError` with both mailboxes and the endpoint's counts.
    """
    deadline = time.monotonic() + timeout
    while True:
        for message in bot_mail_since(stack, after_uid):
            if message.subject == subject:
                return message
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no mail from the bot with subject {subject!r} within "
                f"{timeout}s\n{_mail(stack).describe()}\n"
                f"model:{stack.endpoint.describe()}"
            )
        time.sleep(POLL_INTERVAL)


def reply_to(stack, sent: Sent) -> mail.ReceivedMessage | None:
    """The bot's mail threaded on `sent`, read once, or None."""
    for message in bot_mail_since(stack, sent.outbox_uid):
        if (message.in_reply_to or "").strip() == sent.message_id:
            return message
    return None


def recipients(message: mail.ReceivedMessage, header: str) -> tuple[str, ...]:
    """The addresses in one of a received message's address headers, folded."""
    value = message.headers.get(header, "")
    return tuple(
        address.strip().lower()
        for _, address in getaddresses([value]) if address.strip()
    )


# -- the model's turns ----------------------------------------------------------


def email_answer(text: str) -> dict:
    """The structured answer the email surface parses and mails.

    No `subject`, so the reply keeps the inbound subject with `Re:` in front.
    See `tests/smoke/test_email_e2e.py` for what a prose turn does instead.
    """
    return {"text": json.dumps({"body": text, "format": "plain"})}


def email_output_then(body: str, remark: str, *, subject: str | None = None) -> list[dict]:
    """The two turns the production model uses to mail `body` and tell the
    host `remark`: `istota-skill email output`, then a final text turn.

    Since #636 the envelope alone cannot carry a remark
    (`private_replies.email_note_remark` strips it), and this is the route that
    lets a scenario tell the mailed body from the report.
    """
    command = f"istota-skill email output --body {shlex.quote(body)}"
    if subject is not None:
        command += f" --subject {shlex.quote(subject)}"
    return [
        {"tool_calls": [{
            "id": "call-email-output", "name": "Bash",
            "arguments": {"command": command},
        }]},
        {"text": remark},
    ]


_CONFIRM_RE = re.compile(r"!confirm (\d+) to process")


def confirm_command_from(prompt: mail.ReceivedMessage | str, answer: str = "yes") -> str:
    """The `!confirm` line the gate prompt tells its user to send.

    Read off the prompt rather than spelled from a task id, so a prompt that
    stops saying how to answer fails here. `answer` is `yes`, `no` or
    `yes trust`; the last only where the prompt offers it (a self-claim is
    offered a plain yes or no, `inbound.py`).
    """
    body = prompt if isinstance(prompt, str) else prompt.body_text
    found = _CONFIRM_RE.search(body)
    if found is None:
        raise AssertionError(
            f"the gate prompt names no `!confirm N` command:\n{body}"
        )
    task_id = found.group(1)
    if answer == "yes":
        return f"!confirm {task_id}"
    if answer == "no":
        if f"!confirm {task_id} no" not in body:
            raise AssertionError(f"the gate prompt offers no `no` answer:\n{body}")
        return f"!confirm {task_id} no"
    if answer == "yes trust":
        if "'yes trust'" not in body:
            raise AssertionError(f"the gate prompt offers no `yes trust`:\n{body}")
        return f"!confirm {task_id} trust"
    raise ValueError(f"unknown answer {answer!r}; expected yes, no or 'yes trust'")


# -- the gate, and answering it by mail -----------------------------------------


def held_task(stack, sent: Sent, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """The task the gate parked for `sent`, once it is parked."""
    deadline = time.monotonic() + timeout
    row = None
    while row is None or row.get("task_id") is None:
        row = stack.probe.processed(sent.message_id)
        if row is not None and row.get("task_id") is not None:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"no held task for {sent.message_id}: {row!r}")
        time.sleep(POLL_INTERVAL)
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


def assert_unknown_sender_prompt(task: dict, sender: str) -> None:
    """Anyone not claiming to be the routed user is an `unknown sender`, and
    is offered `yes trust` as well."""
    prompt = task["confirmation_prompt"] or ""
    assert prompt.startswith(f"Email from unknown sender {sender}\n"), prompt
    assert confirm_command_from(prompt, "yes trust") == f"!confirm {task['id']} trust"


def request_mail(stack, sent: Sent, task: dict,
                 timeout: float = DEFAULT_TIMEOUT) -> mail.ReceivedMessage:
    """The confirmation request the bot mailed the host for `task`.

    Read out of the catch-all by the subject the request goes out under
    (`confirmations.request_subject`, ISSUE-649) and checked to be addressed to
    the task's user, since the alert route is what carries it.
    """
    message = wait_for_reply(stack, after_uid=sent.outbox_uid,
                             subject=request_subject(task["id"]), timeout=timeout)
    assert user_id_address(task["user_id"]) in recipients(message, "To"), message.headers
    return message


def answer_by_mail(stack, request: mail.ReceivedMessage, answer: str, *,
                   sender: Correspondent | None = None, nonce: str) -> Sent:
    """Answer a mailed request with the `!confirm` line it names.

    From the host's own address with a passing stamp unless `sender` says
    otherwise, to the bot's bare address, under a subject of its own: the line
    is what makes it an answer (`transport/email/answers.read_answer`). The
    answer runs no task, so its marker takes no scripted turn.
    """
    return send(
        stack, sender or person("host", nonce), to=[mail.BOT_ADDRESS],
        subject=f"answer {nonce}", text=confirm_command_from(request, answer),
        marker=f"answer-{nonce}",
    )


def filed_row(stack, sent: Sent, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """`sent`'s `processed_emails` row, once the poller has filed it.

    An answer to a held task is filed without its Message-ID, like the other
    ledger-only rows (`inbound.poll_emails`, the ISSUE-649 branch), so this
    falls back to sender and subject as `assert_outcome` does.
    """
    row = _wait(lambda: stack.probe.processed(sent.message_id)
                or ledger_only_row(stack, sent), timeout=timeout)
    if row is None:
        raise AssertionError(
            f"{sent.message_id} was not filed within {timeout}s"
            f"\n{_mail(stack).describe()}"
        )
    return row


def held_draft(stack, task_id: int, since: Checkpoint,
               user_id: str = HOST_ID) -> tuple[dict, str, str]:
    """The one draft a held reply of `task_id` made, its notice's dedup key,
    and what that notice pushes.

    The push is the stored body as written, code spans included
    (`outbound_draft.delivery_body_for`), unlike the gate prompt, whose push
    goes through `flatten_body`. Equal to the row by construction, so the
    content is pinned by fragments the producer composes and by
    `assert_outcome`'s check that the sender's words are absent.
    """
    from istota.notifications.resolvers import outbound_draft

    drafts = [
        d for d in stack.probe.drafts(user_id, id_above=since.mark.get("outbound_drafts"))
        if d.get("task_id") == task_id
    ]
    assert len(drafts) == 1, drafts
    key = outbound_draft.dedup_key(drafts[0]["id"])
    rows = stack.probe.notifications(user_id, dedup_key=key)
    assert len(rows) == 1, rows
    push = rows[0]["body"]
    for fragment in ("Nothing was sent.", f"`!drafts send {drafts[0]['id']}`"):
        assert fragment in push, (fragment, push)
    return drafts[0], key, push


# -- what every scenario asserts ------------------------------------------------


@dataclass(frozen=True)
class Processed:
    """Dimension 1: the mail's `processed_emails` row."""
    routing_method: str
    user_id: str | None
    host_asked: bool
    #: `mail_meta.sender_check`, or None for a row with no `mail_meta`.
    sender_check: str | None


@dataclass(frozen=True)
class TaskState:
    """Dimension 2: the task the mail made, when it made one."""
    status: str
    host_absent: bool


@dataclass(frozen=True)
class Incoming:
    """The incoming row's `received_mail`. Bcc is never stored."""
    to: tuple[str, ...]
    cc: tuple[str, ...]
    sender_check: str
    trusted: bool


@dataclass(frozen=True)
class BotRow:
    """The bot's row in the room: its body, and its outgoing card's state
    (None for a row with no card)."""
    body: str
    mail_state: str | None


@dataclass(frozen=True)
class Transcript:
    """Dimension 5: the rows this mail added to its room."""
    incoming: Incoming | None
    bot: BotRow | None


@dataclass(frozen=True)
class Reply:
    """Dimension 6: the reply on the wire. Its `In-Reply-To` is always checked
    against the mail that was sent."""
    to: tuple[str, ...]
    cc: tuple[str, ...]


@dataclass(frozen=True)
class Note:
    """Dimension 7: the host's email note.

    `outcome` is a key of `private_replies.NOTE_OUTCOMES`; `remark` is the
    bot's words after the outcome line, or None for none.
    """
    outcome: str
    without_you: bool
    remark: str | None


@dataclass(frozen=True)
class Notices:
    """Dimension 8: notifications and pushes.

    `rows` is the exact set of `(source, dedup_key)` the user gained above the
    watermark; `{task}` in a key is the mail's task id. `pushes` is the exact
    sequence of ntfy push bodies this test caused, with the same placeholder.
    `alert_mails` is how many mails the bot sent the host on the alert route
    because of this mail, the reply excluded.
    """
    rows: frozenset[tuple[str, str]]
    pushes: tuple[str, ...]
    alert_mails: int


@dataclass(frozen=True)
class Expected:
    """One mail's outcome on every dimension. No field has a default."""
    processed: Processed
    task: TaskState | None
    #: `private` (the user's private email room), `thread`, or `none`.
    room: str
    #: Present participants as `(surface_ref, kind, user_id)`; None when
    #: `room` is `none`.
    participants: frozenset[tuple[str, str, str | None]] | None
    transcript: Transcript
    reply: Reply | None
    note: Note | None
    notices: Notices


@dataclass(frozen=True)
class Checkpoint:
    """Where a later step of one test starts reading: the watermark and the
    number of ntfy pushes already seen. `assert_outcome(..., since=)` reads
    rows and pushes above it, so a test sending several mails can assert each
    one's own."""
    mark: dict
    pushes: int


def checkpoint(stack) -> Checkpoint:
    ntfy = stack.service("ntfy") if "ntfy" in stack.services else None
    return Checkpoint(mark=stack.probe.watermark(),
                      pushes=len(ntfy.pushes()) if ntfy is not None else 0)


@dataclass
class Outcome:
    """What `assert_outcome` read, for a scenario that asserts further."""
    processed: dict | None = None
    task: dict | None = None
    room: str | None = None
    rows: list[dict] = field(default_factory=list)
    reply: mail.ReceivedMessage | None = None
    note_body: str | None = None
    pushes: list = field(default_factory=list)


def _wait(read, *, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        found = read()
        if found:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL)


def worker_done(stack, task_id: int, *, attempts: int = 1,
                timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Wait for the worker's line for this task, which follows the note step.

    True once `attempts` lines are seen: the scheduler logs one per attempt,
    so a retried task's first `failed` line must not stand for its last
    attempt. The line is in the daemon's log, which has no watermark; the task
    id is what makes the match this test's.
    """
    pattern = re.compile(WORKER_DONE_RE.format(task_id=task_id))
    deadline = time.monotonic() + timeout
    while True:
        if len(pattern.findall(stack.logs(2000))) >= attempts:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def parse_note(body: str) -> tuple[str | None, bool, str | None]:
    """`(outcome key, without_you, remark)` read off a note's text body.

    The body is `email_note`'s: a header line, the quote, the outcome line,
    then the remark. The outcome is the last line that is exactly one of
    `NOTE_OUTCOMES`; everything after it is the remark.
    """
    lines = body.splitlines()
    header = lines[0] if lines else ""
    by_text = {text: key for key, text in NOTE_OUTCOMES.items()}
    found = None
    for index, line in enumerate(lines):
        if line.strip() in by_text:
            found = index
    if found is None:
        return None, "without you on the message" in header, None
    remark = "\n".join(lines[found + 1:]).strip() or None
    return (by_text[lines[found].strip()],
            "without you on the message" in header, remark)


def _people(entries) -> tuple[str, ...]:
    return tuple(
        str(entry.get("address", "")).lower()
        for entry in entries or () if isinstance(entry, dict)
    )


def assert_outcome(
    stack, sent: Sent, expected: Expected, *, user_id: str = HOST_ID,
    timeout: float = DEFAULT_TIMEOUT, since: Checkpoint | None = None,
) -> Outcome:
    """Check every dimension of `expected` against what the stack did with `sent`.

    In the order the dimensions are listed, collecting every mismatch before
    failing, so a broken run says everything that is wrong with it. Waits are
    bounded. A negative claim about something that happens after delivery (no
    reply, no note) is read after the worker's completion line for the task,
    the one marker that follows the note step, and "no reply" is then watched
    for `NEGATIVE_SETTLE`, since a sent mail reaches IMAP after the line.

    Rows, transcript and pushes are read above the test's watermark, or above
    `since` for a later step of a test that has already sent mail. Alert mails
    are always read above `sent.outbox_uid`, so a step must let an earlier
    step's alert mail land before it sends.
    """
    probe = stack.probe
    mark = since.mark if since is not None else stack.mark
    pushes_before = since.pushes if since is not None else 0
    misses: list[str] = []
    seen = Outcome()

    def check(dimension: str, want, got) -> None:
        if want != got:
            misses.append(f"{dimension}: expected {want!r}, observed {got!r}")

    # 1. processed_emails
    row = _wait(lambda: probe.processed(sent.message_id) or ledger_only_row(stack, sent),
                timeout=timeout)
    seen.processed = row
    if row is None:
        raise AssertionError(
            f"no processed_emails row for {sent.message_id} within {timeout}s"
            f"\n{_mail(stack).describe()}\nmodel:{stack.endpoint.describe()}"
        )
    meta = row.get("mail_meta") if isinstance(row.get("mail_meta"), dict) else None
    check("processed.routing_method", expected.processed.routing_method,
          row.get("routing_method"))
    check("processed.user_id", expected.processed.user_id, row.get("user_id"))
    check("processed.host_asked", expected.processed.host_asked,
          bool(row.get("host_asked")))
    check("processed.mail_meta.sender_check", expected.processed.sender_check,
          meta.get("sender_check") if meta else None)

    # 2. the task
    task = None
    task_id = row.get("task_id")
    if expected.task is None:
        check("task", None, task_id)
    elif task_id is None:
        misses.append(f"task: expected {expected.task!r}, observed no task")
    else:
        try:
            task = probe.wait_for_task(status=expected.task.status,
                                       task_id=task_id, timeout=timeout)
        except TimeoutError as exc:
            misses.append(f"task: {exc}")
            task = (probe.tasks(task_id=task_id) or [None])[0]
        if task is not None:
            check("task.status", expected.task.status, task.get("status"))
            check("task.host_absent", expected.task.host_absent,
                  bool(task.get("host_absent")))
    seen.task = task
    ran = task is not None and bool(task.get("started_at"))
    attempts = int(task.get("attempt_count") or 0) + 1 if task else 1
    settled = (worker_done(stack, task_id, attempts=attempts, timeout=timeout)
               if ran else True)
    if ran and not settled:
        misses.append(f"worker: no completion line for task {task_id} in the log")

    # 3. the room
    thread = probe.email_room(sent.message_id)
    private = probe.private_email_room(user_id)
    stored = row.get("thread_id")
    if thread is not None:
        room, kind = thread, "thread"
    elif private is not None and stored and probe._canonical_room(stored) == private:
        room, kind = private, "private"
    else:
        room, kind = None, "none"
    seen.room = room
    check("room", expected.room, kind)

    # 4. participants
    if room is None:
        check("participants", expected.participants, None)
    else:
        present = frozenset(
            (p["surface_ref"], p["kind"], p["user_id"])
            for p in probe.participants(room) if p.get("left_at") is None
        )
        check("participants", expected.participants, present)

    # 5. the transcript
    rows = probe.room_messages(room, id_above=mark.get("messages")) if room else []
    seen.rows = rows
    incoming = next(
        (r for r in rows if r["role"] == "user" and isinstance(r.get("received_mail"), dict)
         and r["received_mail"].get("message_id") == sent.message_id),
        None,
    ) or next(
        (r for r in rows if r["role"] == "user" and task_id is not None
         and r.get("task_id") == task_id),
        None,
    )
    if expected.transcript.incoming is None:
        check("transcript.incoming", None, incoming and incoming.get("id"))
    elif incoming is None:
        misses.append("transcript.incoming: expected a row, observed none")
    else:
        received = incoming.get("received_mail") or {}
        got = Incoming(
            to=_people(received.get("to")), cc=_people(received.get("cc")),
            sender_check=received.get("sender_check"),
            trusted=received.get("trusted"),
        )
        check("transcript.incoming.received_mail", expected.transcript.incoming, got)
    bot = next(
        (r for r in rows if r["role"] == "assistant" and task_id is not None
         and r.get("task_id") == task_id),
        None,
    )
    if expected.transcript.bot is None:
        check("transcript.bot", None, bot and bot.get("body"))
    elif bot is None:
        misses.append("transcript.bot: expected a row, observed none")
    else:
        card = bot.get("outgoing_mail") if isinstance(bot.get("outgoing_mail"), dict) else None
        check("transcript.bot", expected.transcript.bot,
              BotRow(body=(bot.get("body") or "").strip(),
                     mail_state=card.get("state") if card else None))

    # 6. the wire
    absence_watched = False
    if expected.reply is None:
        # A reply still passing through the mail server is not in IMAP yet,
        # so one read would miss it: watch for a bounded settle instead.
        reply = _wait(lambda: reply_to(stack, sent), timeout=NEGATIVE_SETTLE)
        absence_watched = True
        check("reply", None, reply and reply.subject)
    else:
        reply = _wait(lambda: reply_to(stack, sent), timeout=timeout)
        if reply is None:
            misses.append(f"reply: expected {expected.reply!r}, observed none")
        else:
            check("reply.to", expected.reply.to, recipients(reply, "To"))
            check("reply.cc", expected.reply.cc, recipients(reply, "Cc"))
    seen.reply = reply

    # 7. the note
    note_key = f"private-note:{task_id}"

    def read_note() -> str | None:
        if task_id is None:
            return None
        room_row = probe.email_note(task_id)
        if room_row is not None:
            return room_row.get("body")
        bell = probe.notifications(user_id, dedup_key=note_key)
        return bell[0]["body"] if bell else None

    if expected.note is None:
        body = read_note()
        check("note", None, body)
        if task_id is not None:
            check("note.task_log", [], [
                r["message"] for r in probe.task_log(task_id, contains=NOTE_LOG_LINE)
            ])
    else:
        body = _wait(read_note, timeout=timeout)
        if body is None:
            misses.append(f"note: expected {expected.note!r}, observed none")
        else:
            outcome, without_you, remark = parse_note(body)
            check("note", expected.note, Note(outcome, without_you, remark))
            check("note.task_log", [NOTE_LOG_LINE + str(expected.note.outcome)], [
                r["message"] for r in probe.task_log(task_id, contains=NOTE_LOG_LINE)
            ])
    seen.note_body = body

    # 8. notifications and pushes
    def fill(text: str) -> str:
        return text.replace("{task}", str(task_id))

    got_rows = frozenset(
        (n["source"], n["dedup_key"])
        for n in probe.notifications(user_id, id_above=mark.get("notifications"))
    )
    check("notices.rows",
          frozenset((source, fill(key)) for source, key in expected.notices.rows),
          got_rows)
    ntfy = stack.service("ntfy") if "ntfy" in stack.services else None

    def read_pushes() -> list:
        return ntfy.pushes()[pushes_before:] if ntfy is not None else []

    def read_alerts() -> list:
        return [
            m for m in bot_mail_since(stack, sent.outbox_uid)
            if (m.in_reply_to or "").strip() != sent.message_id
            and user_id_address(user_id) in recipients(m, "To")
        ]

    # A gate prompt is sent after the poll's transaction commits, and an alert
    # mail passes through the mail server before IMAP shows it, so neither is
    # read once. A positive count is waited for; a zero on a task no worker
    # ran has nothing later to wait behind, so it gets a bounded settle.
    want_pushes = len(expected.notices.pushes)
    want_alerts = expected.notices.alert_mails
    if want_pushes or want_alerts:
        _wait(lambda: len(read_pushes()) >= want_pushes
              and len(read_alerts()) >= want_alerts, timeout=timeout)
        # The count is a floor: a push past it would otherwise go unseen.
        time.sleep(POST_COUNT_SETTLE)
    elif not ran and not absence_watched:
        # The reply watch above has already waited out the same settle.
        time.sleep(NEGATIVE_SETTLE)
    pushes = read_pushes()
    seen.pushes = pushes
    check("notices.pushes", tuple(fill(text) for text in expected.notices.pushes),
          tuple(call.body.decode("utf-8", "replace") for call in pushes))
    alerts = read_alerts()
    check("notices.alert_mails", expected.notices.alert_mails, len(alerts))

    # Whatever went out on the alert route is a title and fixed text (#638):
    # never the sender's words, never the bot's remark.
    forbidden = [sent.text]
    if expected.note is not None and expected.note.remark:
        forbidden.append(expected.note.remark)
    for call in pushes:
        carried = call.body.decode("utf-8", "replace") + " " + " ".join(call.headers.values())
        for text in forbidden:
            if text and text in carried:
                misses.append(f"notices: a push carried {text!r}")
    for message in alerts:
        for text in forbidden:
            if text and text in message.body_text:
                misses.append(f"notices: an alert mail carried {text!r}")

    if misses:
        raise AssertionError(
            f"{len(misses)} dimension(s) differ for {sent.message_id}:\n  "
            + "\n  ".join(misses)
            + f"\n\nmodel:{stack.endpoint.describe()}\n"
            + (stack.diagnostics(task) if task else _mail(stack).describe())
        )
    return seen


def ledger_only_row(stack, sent: Sent) -> dict | None:
    """The ledger row of a mail filed without its Message-ID, or None.

    The poller writes a `discarded`, `quiet`, `throttled`, `read_error`,
    `confirm_answer` or `answer_refused:<reason>` row with the sender
    and subject and no `message_id` (the `mark_email_processed` calls on those
    branches of `inbound.poll_emails`), so `Probe.processed` cannot find one.
    Every subject in the suite carries the test's nonce or a session-unique
    task id (a reply to a request is `Re: Confirm task #N`), which is what
    makes sender plus subject this mail's.
    """
    rows = stack.probe.query(
        "SELECT * FROM processed_emails WHERE message_id IS NULL "
        "AND lower(sender_email) = lower(?) AND subject = ? ORDER BY id DESC LIMIT 1",
        [sent.sender, sent.subject],
    )
    if not rows:
        return None
    row = dict(rows[0])
    meta = row.get("mail_meta")
    row["mail_meta"] = json.loads(meta) if isinstance(meta, str) and meta else None
    return row


def user_id_address(user_id: str) -> str:
    """The address the alert route mails, for the two seeded users."""
    return {HOST_ID: HOST_ADDRESS, ALICE_ID: ALICE_ADDRESS}.get(user_id, "")


__all__ = [
    "PRIVATE_NOTE_POINTER", "NOTE_OUTCOMES", "Correspondent", "Sent", "Expected",
    "Processed", "TaskState", "Transcript", "Incoming", "BotRow", "Reply", "Note",
    "Notices", "Outcome", "Checkpoint", "checkpoint", "PASSING_STAMP", "FAILING_STAMP", "assert_outcome",
    "confirm_command_from", "email_answer", "email_output_then", "person", "route",
    "send", "stamp", "wait_for_reply", "worker_done", "held_task", "prompt_push",
    "request_mail", "answer_by_mail", "filed_row", "assert_unknown_sender_prompt",
    "held_draft",
]
