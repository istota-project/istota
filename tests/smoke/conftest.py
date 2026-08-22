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

import pytest

from ..conftest import REPO, _require_no_xdist, require_docker, resolve_platform
from ..image import conftest as image_support

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
