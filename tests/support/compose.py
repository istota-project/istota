"""Driving a compose stack from a test.

Plain functions over an explicit argument list, no fixture logic and no hidden
global state — `integration-test-framework.md` will absorb this module, and a
helper that reached for a pytest fixture would not survive that move.

The argument list is threaded through every call rather than wrapped in an
object because `docker compose` genuinely needs it on every invocation: the
project name and the file are what tie `up`, `ps`, `logs` and `down` to the same
stack. A stack torn down with a different `-p` than it was brought up with
silently does nothing, and the containers survive the test run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

# `up` covers a build on a cold cache, which on an emulated platform is the
# slowest thing in this tier.
UP_TIMEOUT = 900
DOWN_TIMEOUT = 120
POLL_INTERVAL = 0.5


def docker_available() -> bool:
    """Whether a daemon is actually reachable, not merely whether the CLI exists.

    `docker` is installed and `docker compose` resolves on plenty of machines
    where Desktop is not running, and every command then fails several seconds
    in with a socket error. Asking `info` once turns that into a skip.
    """
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=15
            ).returncode
            == 0
        )
    except (subprocess.SubprocessError, OSError):
        return False


class ComposeError(RuntimeError):
    """A compose command failed, or a service never became ready."""


def compose_args(
    compose_file: Path, *, project: str, env_file: Path | None = None
) -> list[str]:
    """The invariant prefix for every compose call against one stack.

    `--project-name` is not optional here even though compose defaults it from
    the directory name: every stack in this repo's `docker/` directory would
    otherwise share one project, so a smoke run would adopt (and then tear down)
    a developer's running full stack.
    """
    args = ["docker", "compose", "-f", str(compose_file), "--project-name", project]
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    return args


def _run(args: list[str], *, timeout: int, env: dict | None = None) -> str:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env=env
    )
    if result.returncode != 0:
        raise ComposeError(
            f"{' '.join(args[:4])}… exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def up(args: list[str], *, platform: str = "", env: dict | None = None) -> None:
    """Build and start the stack, detached.

    `--platform` is not a compose flag; it reaches the build through
    `DOCKER_DEFAULT_PLATFORM`, which is also what the image tier's `--platform`
    option ends up meaning. Passed through the environment so the two tiers
    cannot disagree about what selecting a platform does.
    """
    environment = dict(env) if env is not None else None
    if platform:
        environment = environment if environment is not None else {}
        environment.setdefault("DOCKER_DEFAULT_PLATFORM", platform)
    _run(args + ["up", "--build", "--detach"], timeout=UP_TIMEOUT, env=environment)


def down(args: list[str], *, volumes: bool = False, env: dict | None = None) -> None:
    """Stop and remove the stack.

    `env` is not optional in practice, and forgetting it is silent. Compose
    interpolates the file on *every* subcommand, including `down`, so a
    `${VAR:?}` the harness only exported for `up` makes the teardown exit
    non-zero before it touches a container — and because this function
    deliberately swallows failures, the stack simply survives the run. Measured:
    two leaked stacks, each holding a named volume, before `env` was threaded
    through. `tests/test_smoke_tier.py` covers it.

    Never raises otherwise. This is the teardown path, and a failure here would
    replace a real test failure with an error about cleanup.
    """
    command = args + ["down", "--remove-orphans", "--timeout", "5"]
    if volumes:
        command.append("--volumes")
    try:
        subprocess.run(
            command, capture_output=True, text=True, timeout=DOWN_TIMEOUT, env=env
        )
    except (subprocess.SubprocessError, OSError):
        pass


def logs(args: list[str], service: str, *, tail: int = 40) -> str:
    """Recent output from one service, for a failure message."""
    try:
        result = subprocess.run(
            args + ["logs", "--no-color", "--tail", str(tail), service],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout or result.stderr
    except (subprocess.SubprocessError, OSError) as exc:  # pragma: no cover
        return f"(could not read logs: {exc})"


def _service_state(args: list[str], service: str) -> tuple[str, str]:
    """`(state, health)` for one service, both "" when it has no container yet.

    `compose ps --format json` emits either a JSON array or one object per line
    depending on the compose version, so both are parsed. Guessing wrong reads
    as "the service has not started" forever, which surfaces as a timeout with
    no explanation.
    """
    try:
        raw = _run(args + ["ps", "--format", "json", service], timeout=30).strip()
    except ComposeError:
        return "", ""
    if not raw:
        return "", ""

    records: list[dict] = []
    try:
        parsed = json.loads(raw)
        records = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in raw.splitlines():
            if line.strip():
                records.append(json.loads(line))

    for record in records:
        if record.get("Service") == service or len(records) == 1:
            return record.get("State", ""), record.get("Health", "")
    return "", ""


def wait_ready(args: list[str], service: str, timeout: int = 120) -> None:
    """Block until `service` is healthy, or running when it declares no health check.

    Accepting bare `running` matters: a service with no `healthcheck` never
    reports a health status at all, so waiting for "healthy" would hang for the
    whole timeout on a stack that came up correctly.

    Raises `TimeoutError` carrying the service's last log lines. A bare timeout
    here is close to useless — the reason the service did not start is in its
    output, and by the time the caller could look, teardown has removed it.
    """
    deadline = time.monotonic() + timeout
    state, health = "", ""
    while time.monotonic() < deadline:
        state, health = _service_state(args, service)
        if health == "healthy":
            return
        if state == "running" and not health:
            return
        if state in ("exited", "dead"):
            break
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"{service} was not ready within {timeout}s (state={state!r}, "
        f"health={health!r})\n--- last logs ---\n{logs(args, service)}"
    )
