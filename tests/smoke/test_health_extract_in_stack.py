"""Health document extraction, handed from the web container to the daemon.

ISSUE-697 moved the three extract routes' model call out of the web process,
because the web container cannot build a namespace and the extractor grants the
Claude CLI a ``Read`` tool on an uploaded document. Two claims follow, and the
default suite can witness neither: it runs one process and patches out bwrap.

**The hand-off crosses the containers.** The web container stages the upload
under ``{temp_dir}/.health-extract`` and queues a request; the daemon container
has to find both through the shared ``/data`` volume, run the extractor and
record the answer. A ``temp_dir`` on a private tmpfs would pass every default
test and answer "the uploaded file is no longer available" here.

The web half runs as the two calls the route makes, inside the web container
with its own config and its own filesystem, rather than over HTTP: the lean
stack's web app signs in through Nextcloud, which the lean shape does not run.
The route itself is covered in ``tests/test_health_extract_requests.py``.

**The daemon builds the wrap the extractor uses, and the web container does
not.** The lean stack's model is the scripted OpenAI-compatible endpoint, so its
extractions run on the native brain, which confines ``Read`` by its roots rather
than by bubblewrap. The wrap the Claude brains depend on is witnessed directly
instead: ``build_daemon_sandbox`` for this user, run in each container, around a
probe only a real namespace can satisfy (the database mask, as in
``test_sandbox_in_stack.py``).
"""

from __future__ import annotations

import json
import textwrap

import pytest

# `browser` is the one lean profile that starts the `web` container; a profile
# of its own would add a stack to every session for three tests.
pytestmark = [pytest.mark.smoke, pytest.mark.profile("browser")]

USER = "testuser"
CONTAINER_CONFIG = "/data/config/config.toml"
DROP = "istota-drop"
WEB_SERVICE = "web"

ENCOUNTER_JSON = json.dumps({"encounters": [{
    "encounter_date": "2026-01-02",
    "encounter_type": "visit",
    "provider": "Dr. Placeholder",
    "facility": "Example Clinic",
    "specialty": "primary care",
    "reason": "Smoke-tier checkup",
    "notes": None,
    "diagnoses": [],
    "confidence": "high",
}]})

# Routed on the vision prompt's opening line, so a daemon task of its own that
# reaches the endpoint meanwhile cannot take the turn.
EXTRACT_SCRIPT = [
    {"when": "Read the clinical document at the following absolute path",
     "turns": [{"text": ENCOUNTER_JSON}]},
]


def _python(stack, service: str, code: str, *, timeout: int = 60):
    """Run ``code`` with the image's interpreter in ``service``, as the uid the
    service itself runs as."""
    argv = ["uv", "run", "python", "-c", textwrap.dedent(code)]
    if service == WEB_SERVICE:
        # `Stack.exec` drops only for the daemon; the web process runs as the
        # same uid, and a root-owned staging directory would lock the daemon out.
        argv = [DROP, *argv]
    result = stack.exec(argv, service=service, timeout=timeout)
    assert result.returncode == 0, (
        f"python in {service} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _marked(output: str, marker: str) -> str:
    for line in output.splitlines():
        if line.startswith(marker + " "):
            return line[len(marker) + 1:]
    raise AssertionError(f"no {marker} line in:\n{output}")


class TestTheHandOffCrossesTheContainers:
    @pytest.mark.script(EXTRACT_SCRIPT)
    def test_an_upload_staged_by_web_is_answered_by_the_daemon(self, stack):
        queued = _python(stack, WEB_SERVICE, f"""
            from pathlib import Path
            from istota.config import load_config
            from istota.health import extract_requests as er
            config = load_config(Path({CONTAINER_CONFIG!r}))
            name = er.stage_upload(config, b"\\x89PNG\\r\\n\\x1a\\nsmoke", ".png")
            request_id = er.request_extract(
                config, {USER!r}, er.ENCOUNTER, staged=name, mime="image/png",
                filename="visit.png", document_mime="image/png",
            )
            print("STAGED", name)
            print("REQUEST", request_id)
        """)
        staged = _marked(queued, "STAGED")
        request_id = _marked(queued, "REQUEST")

        answered = _python(stack, WEB_SERVICE, f"""
            import json, time
            from pathlib import Path
            from istota.config import load_config
            from istota.health import extract_requests as er
            config = load_config(Path({CONTAINER_CONFIG!r}))
            deadline = time.monotonic() + 150
            state = {{"state": "pending"}}
            while time.monotonic() < deadline:
                state = er.extract_state(config, {USER!r}, {request_id!r})
                if state["state"] != "pending":
                    break
                time.sleep(1)
            print("STATE", json.dumps(state))
        """, timeout=180)
        state = json.loads(_marked(answered, "STATE"))

        assert state["state"] == "answered", (
            f"the daemon never answered request {request_id}: {state}\n"
            f"--- daemon logs ---\n{stack.logs()}"
        )
        result = state["result"]
        assert "error" not in result, result
        assert [r["reason"] for r in result["rows"]] == ["Smoke-tier checkup"], result
        assert isinstance(result["document_id"], int), result

        # The daemon removed its staged copy, and the extractor's prompt named
        # the file where the web container wrote it.
        gone = stack.exec(["test", "-e", f"/data/tmp/.health-extract/{staged}"])
        assert gone.returncode != 0, "the staged upload outlived its extraction"
        assert staged in stack.endpoint.transcript()


WRAP_PROBE = f"""
    import json, subprocess
    from pathlib import Path
    from istota.config import load_config
    from istota.executor import build_daemon_sandbox
    config = load_config(Path({CONTAINER_CONFIG!r}))
    sandbox = build_daemon_sandbox(config, {USER!r})
    probe = ["stat", "-f", "-c", "%T", "/data/db"]
    argv = sandbox.wrap(probe) if sandbox.wrap else probe
    run = subprocess.run(argv, capture_output=True, text=True)
    print("WRAP", json.dumps({{
        "refused": sandbox.refused,
        "wrapped": argv != probe,
        "fstype": run.stdout.strip(),
        "exit": run.returncode,
        "stderr": run.stderr[-500:],
    }}))
"""


class TestTheExtractorsWrap:
    def test_the_daemon_builds_a_real_namespace(self, stack):
        """What a Claude-brain extraction runs in, built by the process that
        now runs it. ``tmpfs`` at the database directory is the mask
        ``build_bwrap_cmd`` ends with; an unconfined run sees the volume."""
        wrap = json.loads(_marked(_python(stack, "daemon", WRAP_PROBE), "WRAP"))
        assert wrap["refused"] is False, wrap
        assert wrap["wrapped"] is True, wrap
        assert wrap["exit"] == 0, wrap
        assert wrap["fstype"] == "tmpfs", wrap

    def test_the_web_container_cannot(self, stack):
        """The premise of the hand-off, pinned so it is noticed if it changes:
        in the web container the same call hands the command back unwrapped
        and the probe sees the real volume. If the web container is ever given
        the sandbox grant this goes red, and the hand-off can be revisited."""
        wrap = json.loads(_marked(_python(stack, WEB_SERVICE, WRAP_PROBE), "WRAP"))
        assert wrap["wrapped"] is False, wrap
        assert wrap["fstype"] != "tmpfs", wrap
