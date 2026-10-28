"""The `web` service's own INFO lines reach its container output (ISSUE-872).

The shipped entrypoint runs uvicorn on `istota.webui.stack_app`, and uvicorn
configures only its own loggers. The default suite reads records through
`caplog`, which captures them whether or not the process has a handler, so only
a running container can show that `istota.webui.*` INFO lines are written at
all. The lifespan's `STARTUP Running` line is the probe: every boot writes it.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.profile("group-memory")]

PROBE = "[istota.webui.app"


def test_the_web_startup_line_is_in_the_web_containers_output(stack):
    output = ""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        output = stack.logs(tail=2000, service="web")
        if any(PROBE in ln and "STARTUP Running" in ln for ln in output.splitlines()):
            return
        time.sleep(2)
    pytest.fail(f"no {PROBE} STARTUP line in the web service's output:\n{output[-4000:]}")
