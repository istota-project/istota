"""The users and standings the email suite assumes, seeded into a running stack.

Shared by the lean email files (`tests/smoke/conftest.py`) and the full-shape
one (`tests/full/conftest.py`), whose conftests cannot see each other's
fixtures; each wraps these functions in its own. The two shapes differ in one
value, the host's alert route: on lean it is `email,ntfy`, since there is no
Talk, and on full it names the provisioned Talk alerts room beside ntfy, so a
room-free push (ntfy and email only) can be told apart from one on the whole
route.

No pytest import: a failure is a `StackError`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from testbed import stack as stack_support
from testbed.services import mail, ntfy
from testbed.stack import CONTAINER_CONFIG

from . import email_flow

#: How long the readiness proof waits for the seeded profiles to reach the
#: running daemon. They arrive through `config.refresh_user_profiles_if_changed`
#: on a scheduler tick, and the mail poller runs every five seconds.
READINESS_TIMEOUT = 60.0

#: The lean alert route. `email` alone resolves to the email destination only
#: (`notifications.delivery.resolve_destinations`); an ntfy secret adds no
#: destination by itself, so the stub hears nothing unless the route names it.
LEAN_ALERT_ROUTE = "alert=email,ntfy"


@dataclass(frozen=True)
class EmailPeople:
    """Who `seed_email_people` seeded, by user id and address."""

    host_id: str
    host_address: str
    alice_id: str
    alice_address: str


def run_istota(stack, *argv: str) -> str:
    """Run the shipped `istota` CLI in the stack's daemon container.

    The full shape's key lives in `/data/.secret_key`, which `entrypoint.sh`
    exports into the daemon's environment and not into an `exec` session's,
    so it is read the same way here. The lean shape sets the variable in
    compose, and the shell leaves it alone.
    """
    script = (
        'if [ -z "${ISTOTA_SECRET_KEY:-}" ] && [ -r /data/.secret_key ]; then '
        'ISTOTA_SECRET_KEY=$(cat /data/.secret_key); export ISTOTA_SECRET_KEY; fi; '
        f'exec uv run istota -c {CONTAINER_CONFIG} "$@"'
    )
    result = stack.exec(["sh", "-c", script, "istota", *argv], timeout=120)
    if result.returncode != 0:
        raise stack_support.StackError(
            f"`istota {' '.join(argv)}` exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def seed_email_people(stack, *, alert_route: str) -> EmailPeople:
    """Seed the users and standings the email suite assumes, then prove the
    running daemon routes by them.

    Through the shipped CLI, as an operator would. Profile patterns rather than
    the runtime trust table, because `Stack.reset_framework_state` clears only
    the table, so a pattern survives every reset and the table stays what
    `yes trust` writes. alice is written last, so the readiness proof, which
    waits for her, also covers the host's profile written before her.
    """
    run_istota(stack, "user", "ensure", "--name", email_flow.HOST_ID,
               "--email", email_flow.HOST_ADDRESS,
               "--trusted-sender", f"*@{email_flow.TRUSTED_DOMAIN}",
               "--quiet-sender", f"*@{email_flow.QUIET_DOMAIN}",
               "--route", alert_route)
    ntfy_service = stack.service("ntfy")
    for key, value in (
        ("server_url", ntfy_service.container_url),
        ("topic", ntfy_service.topic),
        ("token", ntfy.NTFY_TOKEN),
    ):
        run_istota(stack, "secret", "ensure", "-u", email_flow.HOST_ID,
                   "--service", "ntfy", "--key", key, "--value", value)
    run_istota(stack, "user", "ensure", "--name", email_flow.ALICE_ID,
               "--email", email_flow.ALICE_ADDRESS)

    # The readiness proof: a mail only the seeded profile can route. alice
    # exists nowhere but in the row just written, so `sender_match` naming her
    # means the daemon's config has the overlay. Answered with a no-op through
    # its own route, so it takes no turn of the test's script.
    # A mail read before the overlay landed is routed some other way and is
    # spent, so a wrong answer sends a fresh one rather than failing.
    turns = list(stack.endpoint.turns)
    markers: list[str] = []
    deadline = time.monotonic() + READINESS_TIMEOUT
    row = None
    while time.monotonic() < deadline:
        nonce = email_flow.new_nonce()
        marker = f"ready-{nonce}"
        markers.append(marker)
        stack.script([email_flow.route(m, [{"text": "NO_ACTION: ready"}])
                      for m in markers])
        sent = email_flow.send(
            stack, email_flow.person("alice", nonce), to=[mail.BOT_ADDRESS],
            subject=f"readiness {nonce}", text="checking the seeded profiles",
            marker=marker,
        )
        row = None
        while time.monotonic() < deadline and row is None:
            row = stack.probe.processed(sent.message_id)
            if row is None:
                time.sleep(2)
        if (row is not None and row.get("routing_method") == "sender_match"
                and row.get("user_id") == email_flow.ALICE_ID):
            break
    if (row is None or row.get("routing_method") != "sender_match"
            or row.get("user_id") != email_flow.ALICE_ID):
        raise stack_support.StackError(
            f"the readiness mail was not routed to alice by sender within "
            f"{READINESS_TIMEOUT}s (saw {row!r}). The seeded profiles have not "
            "reached the running daemon; `refresh_user_profiles_if_changed` on "
            "the scheduler tick is what carries them."
        )
    if row.get("task_id") is not None:
        stack.probe.wait_for_task(status="completed", task_id=row["task_id"],
                                  timeout=READINESS_TIMEOUT)
    # The proof made rows and maybe mail; reset again so the test's watermark
    # and mailboxes start after it, and put the test's own script back.
    stack.mark = stack.reset(turns)
    for marker in markers:
        email_flow.sent_markers(stack).discard(email_flow.marker_text(marker))
    return EmailPeople(
        host_id=email_flow.HOST_ID, host_address=email_flow.HOST_ADDRESS,
        alice_id=email_flow.ALICE_ID, alice_address=email_flow.ALICE_ADDRESS,
    )


def unmatched_marked_requests(stack, ours: set[str]) -> list[dict]:
    """The endpoint's marked requests whose current marker is one of `ours`
    and which matched no route or ran past its turns."""
    return [
        entry for entry in stack.endpoint.marked_unmatched()
        if entry["markers"][-1] in ours
    ]


def describe_unmatched(entries: list[dict]) -> str:
    return "marked model requests found no scripted turn:\n" + "\n".join(
        f"  {entry['reason']} when={entry['when']!r} markers={entry['markers']} "
        f"excerpt={entry['excerpt']!r}"
        for entry in entries
    )
