"""The lean stack, brought up once per profile and shared for the session.

Everything the lean shape exists for is getting from "a checkout" to "a running
daemon that will answer a task" in under thirty seconds, with no Nextcloud and
no API key. Three pieces make that possible, and each replaces something the
full stack does slowly:

- the config is rendered **on the host** by the same `render-config.sh` the
  image ships, so the container never enters the provisioning branch and its
  120-second Nextcloud polling loop;
- the model is a scripted HTTP endpoint in the pytest process, reached through
  `[brain.native] base_url`, so no credential and no network are involved;
- the stack is one service.

A test declares what it needs and is handed a stack that already has it:

    @pytest.mark.profile("forge")
    @pytest.mark.script([{"text": "done"}])
    def test_something(stack): ...

`profile` defaults to `"base"` and `script` to one plain answer. Both are
optional, and a scenario whose script depends on something only known at run
time — a stub's port, say — calls `stack.script(...)` inside the test instead.

**Stacks are session-scoped, one per profile.** The fixture that used to boot
one per test argued that the endpoint's `base_url` is baked into the rendered
config, so a shared stack would need reconfiguring between tests anyway. That
held only because the endpoint was started immediately before the render. Here
the services start once per profile, before that profile's config is rendered,
and live as long as the stack — so the address stays valid and `rescript`
handles the per-test script, which is what it was written for. `Stack.reset` is
what makes the sharing safe; read its docstring before adding a scenario that
mutates something.

**Almost nothing is left in this file.** The `stacks` and `stack` fixtures, the
xdist guards, `require_docker`, the session sweep and the exec measurement moved
up to `tests/conftest.py` in Stage 3, because `tests/full/` needs the same ones
and a fixture in a sibling package's conftest is invisible to another. What
stays is what is specific to the lean *shape*: the negative control's image,
which is a lean-profile concern and which nothing else should be able to build
by accident.

The machinery underneath lives in `testbed/` — `StackPool`, `Stack`, the
`Service` protocol, the compose helpers, the probe.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

import pytest

from testbed import stack as stack_support
from testbed.services import mail, ntfy

from ..conftest import REPO, _require_no_xdist, require_docker, resolve_platform
from ..image import conftest as image_support
from ..support import email_flow

NO_FORGE_DOCKERFILE = REPO / "docker" / "test" / "Dockerfile.no-forge"


@pytest.fixture(scope="session")
def no_forge_image(pytestconfig) -> str:
    """The shipped image with the forge binaries removed.

    Built here rather than imported as a fixture from `tests/image/conftest.py`,
    because a fixture defined in a sibling package's conftest is not visible to
    this one — the *functions* are, and those are what this uses.

    Two builds: the real image (usually a cache hit, since the compose stack in
    this same session just built it from the same context) and then the control
    on top of it. `Dockerfile.no-forge` takes the real tag as `BASE` precisely
    so the second is one `rm -rf` layer.
    """
    _require_no_xdist(pytestconfig)
    require_docker()
    platform = resolve_platform(pytestconfig)

    # `ISTOTA_IMAGE_TAG` first, exactly as `image_support.istota_image` does.
    # Without it the control is built from the local checkout while the
    # correct-image half of the pair is whatever tag the environment named — so
    # the two differ by more than the forge binaries, and the control measures
    # the difference between two builds rather than the thing it exists for.
    preexisting = os.environ.get("ISTOTA_IMAGE_TAG")
    if preexisting:
        base_tag = preexisting
    else:
        base_tag = image_support.build_image(
            image_support.ISTOTA_DOCKERFILE, REPO, platform=platform, prefix="istota"
        ).tag
    tag = f"istota-test/no-forge:{base_tag.rsplit(':', 1)[-1]}"
    argv = [
        "docker", "build",
        "-f", str(NO_FORGE_DOCKERFILE),
        "--build-arg", f"BASE={base_tag}",
        "-t", tag,
    ]
    if platform:
        argv += ["--platform", platform]
    argv.append(str(NO_FORGE_DOCKERFILE.parent))

    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=image_support.BUILD_TIMEOUT
    )
    if result.returncode != 0:
        # `fail`, not `exit`. A Docker hiccup building the control must not
        # terminate the whole session and take any other tier queued behind it
        # with it; every other failure path in this file uses `fail` too.
        pytest.fail(
            "could not build the no-forge control image:\n"
            + "\n".join((result.stderr or result.stdout or "").splitlines()[-40:]),
            pytrace=False,
        )
    return tag


# -- the email suite ---------------------------------------------------------


@dataclass(frozen=True)
class EmailPeople:
    """Who `email_people` seeded, by user id and address."""

    host_id: str
    host_address: str
    alice_id: str
    alice_address: str


#: How long the readiness proof waits for the seeded profiles to reach the
#: running daemon. They arrive through `config.refresh_user_profiles_if_changed`
#: on a scheduler tick, and the mail poller runs every five seconds.
READINESS_TIMEOUT = 60.0

#: The alert route `email_people` gives testuser. `email` alone resolves to the
#: email destination only (`notifications.delivery.resolve_destinations`); an
#: ntfy secret adds no destination by itself, so the stub hears nothing unless
#: the route names it. Both, so the suite can read a push on either surface.
HOST_ALERT_ROUTE = "alert=email,ntfy"


def _istota(stack, *argv: str) -> None:
    result = stack.exec(
        ["uv", "run", "istota", "-c", "/data/config/config.toml", *argv],
        timeout=120,
    )
    if result.returncode != 0:
        raise stack_support.StackError(
            f"`istota {' '.join(argv)}` exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def _seed_email_people(stack) -> EmailPeople:
    """Seed the users and standings the email suite assumes, then prove the
    running daemon routes by them.

    Through the shipped CLI, as an operator would. Profile patterns rather than
    the runtime trust table, because `Stack.reset_framework_state` clears only
    the table, so a pattern survives every reset and the table stays what
    `yes trust` writes.
    """
    _istota(stack, "user", "ensure", "--name", email_flow.ALICE_ID,
            "--email", email_flow.ALICE_ADDRESS)
    _istota(stack, "user", "ensure", "--name", email_flow.HOST_ID,
            "--email", email_flow.HOST_ADDRESS,
            "--trusted-sender", f"*@{email_flow.TRUSTED_DOMAIN}",
            "--quiet-sender", f"*@{email_flow.QUIET_DOMAIN}",
            "--route", HOST_ALERT_ROUTE)
    ntfy_service = stack.service("ntfy")
    for key, value in (
        ("server_url", ntfy_service.container_url),
        ("topic", ntfy_service.topic),
        ("token", ntfy.NTFY_TOKEN),
    ):
        _istota(stack, "secret", "ensure", "-u", email_flow.HOST_ID,
                "--service", "ntfy", "--key", key, "--value", value)

    # The readiness proof: a mail only the seeded profile can route. alice
    # exists nowhere but in the row just written, so `sender_match` naming her
    # means the daemon's config has the overlay. Answered with a no-op through
    # its own route, so it takes no turn of the test's script.
    nonce = email_flow.new_nonce()
    marker = f"ready-{nonce}"
    turns = list(stack.endpoint.turns)
    stack.script([email_flow.route(marker, [{"text": "NO_ACTION: ready"}])])
    sent = email_flow.send(
        stack, email_flow.person("alice", nonce), to=[mail.BOT_ADDRESS],
        subject=f"readiness {nonce}", text="checking the seeded profiles",
        marker=marker,
    )
    deadline = time.monotonic() + READINESS_TIMEOUT
    row = None
    while time.monotonic() < deadline:
        row = stack.probe.processed(sent.message_id)
        if row is not None:
            break
        time.sleep(2)
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
    email_flow.sent_markers(stack).discard(email_flow.marker_text(marker))
    return EmailPeople(
        host_id=email_flow.HOST_ID, host_address=email_flow.HOST_ADDRESS,
        alice_id=email_flow.ALICE_ID, alice_address=email_flow.ALICE_ADDRESS,
    )


@pytest.fixture
def email_people(stack) -> EmailPeople:
    """The email suite's users, seeded once per stack and proven live.

    Once per stack rather than per test, and kept on the stack object rather
    than in a session-scoped fixture, because `stack` is per test while the
    stack it hands out lives for the session. A fresh stack is a new object
    and seeds again.
    """
    seeded = getattr(stack, "_email_people", None)
    if seeded is None:
        try:
            seeded = _seed_email_people(stack)
        except (TimeoutError, stack_support.StackError) as exc:
            pytest.fail(str(exc), pytrace=False)
        stack._email_people = seeded
    return seeded


@pytest.fixture(autouse=True)
def _no_unmatched_marked_requests(request):
    """Fail an email scenario whose own marked request found no turn.

    A request whose current marker, the rightmost `[e2e:...]` in it, is one this
    test sent, and which matched no route or ran past its route's turns, is a
    task the scenario did not describe; the endpoint answers it with
    `NO_ACTION` or the exhausted frame, and without this it would pass
    silently. A request with no marker is a daemon poller's. One whose
    rightmost marker this test did not send is a daemon job quoting earlier
    mail (the memory extraction reads a day of conversation), and is ignored
    too. Only in `test_email_*` files that use a stack.
    """
    module = request.module.__name__.rsplit(".", 1)[-1]
    if not module.startswith("test_email_") or "stack" not in request.fixturenames:
        yield
        return
    stack = request.getfixturevalue("stack")
    ours = email_flow.sent_markers(stack)
    ours.clear()
    yield
    unmatched = [
        entry for entry in stack.endpoint.marked_unmatched()
        if entry["markers"][-1] in ours
    ]
    assert not unmatched, (
        "marked model requests found no scripted turn:\n"
        + "\n".join(
            f"  {entry['reason']} when={entry['when']!r} markers={entry['markers']} "
            f"excerpt={entry['excerpt']!r}"
            for entry in unmatched
        )
    )
