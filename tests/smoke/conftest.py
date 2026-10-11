"""The lean stack, brought up once per profile and shared for the session.

The lean shape is the shipped compose file plus `testbed/compose/testbed.yml`,
booted through the shipped entrypoint, with local storage and no Nextcloud:
the deployment an operator runs by default. It gets from "a checkout" to "a
running daemon that will answer a task" in seconds because the config is an
input the testbed writes before boot, and the model is a scripted HTTP
endpoint in the pytest process, reached through `[brain.native] base_url`, so
no credential and no external network are involved.

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

import pytest

from testbed import stack as stack_support

from ..conftest import REPO, _require_no_xdist, require_docker, resolve_platform
from ..image import conftest as image_support
from ..support import email_flow
from ..support import email_people as email_people_support

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


@pytest.fixture(scope="session")
def testbed_browser_image(pytestconfig) -> str:
    """The shipped browser image, built by the image tier's rule and tag.

    `ISTOTA_BROWSER_IMAGE_TAG` first, as the image tier honours it. Otherwise a
    `docker build` with that tier's tag, which is a no-op when an image tier run
    already built this tree's browser.
    """
    _require_no_xdist(pytestconfig)
    require_docker()
    preexisting = os.environ.get("ISTOTA_BROWSER_IMAGE_TAG")
    if preexisting:
        return preexisting
    dockerfile = REPO / "docker" / "browser" / "Dockerfile"
    return image_support.build_image(
        dockerfile, dockerfile.parent, platform="linux/amd64", prefix="browser",
    ).tag


# -- the email suite ---------------------------------------------------------

#: The lean shape's seeding: no Talk, so the host's alerts go to email and ntfy.
EmailPeople = email_people_support.EmailPeople
HOST_ALERT_ROUTE = email_people_support.LEAN_ALERT_ROUTE


@pytest.fixture
def email_people(stack) -> EmailPeople:
    """The email suite's users, seeded once per stack and proven live.

    Once per stack rather than per test, and kept on the stack object rather
    than in a session-scoped fixture, because `stack` is per test while the
    stack it hands out lives for the session. A fresh stack is a new object
    and seeds again. The seeding itself is `tests/support/email_people.py`,
    shared with the full shape.
    """
    seeded = getattr(stack, "_email_people", None)
    if seeded is None:
        try:
            seeded = email_people_support.seed_email_people(
                stack, alert_route=HOST_ALERT_ROUTE,
            )
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
    unmatched = email_people_support.unmatched_marked_requests(stack, ours)
    assert not unmatched, email_people_support.describe_unmatched(unmatched)
