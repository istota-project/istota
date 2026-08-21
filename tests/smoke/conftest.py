"""The lean stack, brought up around one test.

Everything here exists to get from "a checkout" to "a running daemon that will
answer a task" in under thirty seconds, with no Nextcloud and no API key. Three
pieces make that possible, and each replaces something the full stack does
slowly:

- the config is rendered **on the host** by the same `render-config.sh` the
  image ships, so the container never enters the provisioning branch and its
  120-second Nextcloud polling loop;
- the model is a scripted HTTP endpoint in the pytest process, reached through
  `base_url`, so no credential and no network are involved;
- the stack is one service.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from ..support import compose as compose_support
from ..support.model_endpoint import serve_script
from ..support.probe import Probe

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "docker" / "docker-compose.test.yml"
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"

READY_TIMEOUT = 120

_XDIST_MESSAGE = (
    "the smoke tier must run with -n0. The stack is a single compose project "
    "with a fixed name, so N workers would bring up, exec against and tear "
    "down each other's containers."
)


def _require_no_xdist(config) -> None:
    """Refuse inside an xdist worker.

    The same shape, and for the same reason, as `tests/image/conftest.py`:
    `pytest_collection_modifyitems` cannot see a real parallel run at all — the
    controller holds no items and the workers have `numprocesses` cleared — so
    the check that binds has to live where the damage would happen, keyed on
    `config.workerinput`.
    """
    if hasattr(config, "workerinput"):
        worker = config.workerinput.get("workerid", "?")
        pytest.fail(f"{_XDIST_MESSAGE} (running in xdist worker {worker})", pytrace=False)


def require_docker() -> None:
    if not compose_support.docker_available():
        pytest.skip("no Docker daemon available")


def _render_config(destination: Path, base_url: str) -> Path:
    """Run the shipped render script on the host.

    This is the property that makes the shortcut legitimate: the file the lean
    stack boots from is produced by the same script the container would have
    run, not by a fixture that approximates it.
    """
    config_file = destination / "config.toml"
    # An explicit environment, NOT `**os.environ`. render-config.sh reads
    # dozens of `ISTOTA_*` variables, so inheriting the developer's shell would
    # make the config the lean stack boots from depend on whatever happens to be
    # exported in the terminal that started pytest — the same run passing on one
    # machine and failing on another, with nothing in the repo to explain it.
    #
    # This is reproducibility, not test isolation: it does *not* stop the daemon
    # queueing work of its own. The scheduler seeds `_module.feeds.run_scheduled`
    # and polls it at startup regardless of this environment, so the smoke tests
    # filter on the submitted task's id rather than on its user.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CONFIG_FILE": str(config_file),
        "USER_NAME": "testuser",
        "NC_URL": "http://nextcloud",
        "APP_PASSWORD": "app-password-value",
        "BOT_USER": "istota",
        "USER_TIMEZONE": "UTC",
        "ISTOTA_BRAIN_KIND": "native",
        "ISTOTA_BRAIN_NATIVE_BASE_URL": base_url,
        "ISTOTA_BRAIN_NATIVE_MODEL": "scripted-test-model",
        # One turn is all the scripted endpoint has; a loop that asked for more
        # should fail loudly rather than grind through a hundred attempts.
        "ISTOTA_BRAIN_NATIVE_MAX_TURNS": "4",
    }
    result = subprocess.run(
        ["bash", str(RENDER_CONFIG)],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if result.returncode != 0:
        pytest.fail(
            f"render-config.sh exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
            pytrace=False,
        )
    assert config_file.exists(), "render-config.sh reported success but wrote nothing"
    return config_file


@pytest.fixture
def lean_stack(pytestconfig, tmp_path, request):
    """A running daemon and the endpoint it talks to.

    Function-scoped on purpose. The scripted turns differ per test, and the
    endpoint's `base_url` is baked into the rendered config, so a shared stack
    would have to be reconfigured and restarted between tests anyway — at which
    point the sharing saves nothing and couples the tests to each other's
    scripts.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    turns = getattr(request, "param", None) or [{"text": "the scripted answer"}]
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    endpoint = serve_script(turns)
    # A fresh project name per test, so a stack left behind by an interrupted
    # run is never adopted (and then torn down) by the next one.
    project = f"istota-smoke-{uuid.uuid4().hex[:8]}"

    # The config directory travels in an --env-file, not in our environment.
    # Compose interpolates the compose file on *every* subcommand, so a
    # `${VAR:?}` supplied only to `up` makes `ps`, `exec`, `logs` and `down`
    # fail before they touch a container. `down` swallows its failures, so the
    # visible symptom was a stack that survived the run holding a named volume,
    # while `wait_ready` sat out its whole timeout reading "no container yet".
    # An --env-file rides along in the argument list, so every subcommand gets
    # it and no caller has to remember.
    env_file = tmp_path / "compose.env"
    env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={config_dir}\n")
    args = compose_support.compose_args(
        COMPOSE_FILE, project=project, env_file=env_file
    )

    _render_config(config_dir, endpoint.container_base_url)

    try:
        compose_support.up(args)
        compose_support.wait_ready(args, "istota", timeout=READY_TIMEOUT)
        yield LeanStack(args=args, endpoint=endpoint, config_dir=config_dir)
    finally:
        # Volumes too: the DB is a named volume, and leaving it behind would
        # make the next run's assertions depend on this one's rows.
        compose_support.down(args, volumes=True)
        endpoint.close()


class LeanStack:
    """What a smoke test is handed."""

    def __init__(self, *, args: list[str], endpoint, config_dir: Path):
        self.args = args
        self.endpoint = endpoint
        self.config_dir = config_dir
        self.probe = Probe(compose_args=args, service="istota")

    def submit(self, prompt: str, *, user_id: str = "testuser") -> int:
        """Enqueue a task through the shipped CLI and return its id.

        Through `istota task` rather than by writing a row directly: inserting
        into `tasks` would assert nothing about the image, and the point of this
        tier is that the artifact works.

        The id is parsed out and returned because the caller needs it: the
        daemon queues tasks of its own for the same user at startup, so an
        assertion filtered on `user_id` alone can land on the wrong row.
        """
        result = subprocess.run(
            self.args
            + [
                "exec",
                "-T",
                "istota",
                "uv",
                "run",
                "istota",
                "-c",
                "/data/config/config.toml",
                "task",
                prompt,
                "-u",
                user_id,
                "--source-type",
                "cli",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.fail(
                f"submitting a task exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
                pytrace=False,
            )
        match = re.search(r"Task created:\s*(\d+)", result.stdout)
        if not match:
            pytest.fail(
                "could not read a task id out of `istota task` output; the CLI "
                f"prints 'Task created: N'\n--- stdout ---\n{result.stdout}",
                pytrace=False,
            )
        return int(match.group(1))

    def logs(self, tail: int = 60) -> str:
        return compose_support.logs(self.args, "istota", tail=tail)
