"""Driving a compose stack from a test.

Plain functions over an explicit argument list, no fixture logic and no hidden
global state — `integration-test-framework.md` will absorb this module, and a
helper that reached for a pytest fixture would not survive that move.

The argument list is threaded through every call rather than wrapped in an
object because `docker compose` genuinely needs it on every invocation: the
project name and the file are what tie `up`, `ps`, `logs` and `down` to the same
stack. A stack torn down with a different `-p` than it was brought up with
silently does nothing, and the containers survive the test run.

**Compose variables belong in an `--env-file`, not in `env=`.** Compose
interpolates the compose file on *every* subcommand, so a variable supplied to
one call and not the others makes the rest fail during interpolation, before
they touch a container. That failure is quiet in both directions: `_service_state`
reports "no container yet" and `wait_ready` sits out its whole timeout, while
`down` swallows it and leaves the stack running. An `--env-file` rides in the
argument list, so every subcommand gets it and no caller has to remember.
`env=` remains for the *process* environment — `DOCKER_DEFAULT_PLATFORM` and
the like — and is merged over `os.environ`, never substituted for it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _child_env(env: dict | None) -> dict:
    """The caller's overrides layered *over* the real environment.

    Never a replacement for it. `subprocess.run(env={...})` substitutes rather
    than extends, so passing a one-key dict leaves the child with no `PATH` —
    and `docker` is then not found at all, which reads as "Docker is not
    installed" rather than as a harness bug. `HOME` matters too: it is where the
    Docker CLI finds its context and therefore the daemon socket.
    """
    return {**os.environ, **(env or {})}


def _describe(args: list[str]) -> str:
    """The subcommand, for an error header.

    `args[:4]` would always be `docker compose -f <file>`, so every ComposeError
    read identically and none of them said which call had failed.
    """
    flagged = {"-f", "--project-name", "--env-file"}
    parts, skip = [], False
    for token in args:
        if skip:
            skip = False
            continue
        if token in flagged:
            skip = True
            continue
        parts.append(token)
    return " ".join(parts)


def _run(args: list[str], *, timeout: int, env: dict | None = None) -> str:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env=_child_env(env)
    )
    if result.returncode != 0:
        raise ComposeError(
            f"`{_describe(args)}` exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def up(args: list[str], *, platform: str = "", env: dict | None = None) -> None:
    """Build and start the stack, detached.

    `--platform` is not a compose flag — compose has no per-invocation platform
    option — so it is passed through `DOCKER_DEFAULT_PLATFORM` instead. (The
    image tier one layer down uses a real `docker build --platform` flag; the
    two mechanisms differ, and only the effect is shared.)
    """
    overrides = dict(env or {})
    if platform:
        overrides.setdefault("DOCKER_DEFAULT_PLATFORM", platform)
    _run(args + ["up", "--build", "--detach"], timeout=UP_TIMEOUT, env=overrides)


def down(args: list[str], *, volumes: bool = False, env: dict | None = None) -> None:
    """Stop and remove the stack.

    Never raises: this is the teardown path, and an exception here would replace
    a real test failure with an error about cleanup while leaving the containers
    behind either way.

    It does *log* a non-zero exit, though, which is the part that was missing.
    Swallowing the status as well as the exception is how the `--env-file` bug
    stayed invisible for two leaked stacks — compose exited non-zero during
    interpolation, nothing was raised, nothing was said, and the containers
    survived the run.
    """
    command = args + ["down", "--remove-orphans", "--timeout", "5"]
    if volumes:
        command.append("--volumes")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DOWN_TIMEOUT,
            env=_child_env(env),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("compose down raised %s; the stack may still be running", exc)
        return
    if result.returncode != 0:
        logger.warning(
            "compose down exited %s — the stack is probably still running.\n%s",
            result.returncode,
            result.stderr or result.stdout,
        )


def logs(
    args: list[str], service: str, *, tail: int = 40, env: dict | None = None
) -> str:
    """Recent output from one service, for a failure message."""
    try:
        result = subprocess.run(
            args + ["logs", "--no-color", "--tail", str(tail), service],
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(env),
        )
        return result.stdout or result.stderr
    except (subprocess.SubprocessError, OSError) as exc:  # pragma: no cover
        return f"(could not read logs: {exc})"


def _service_state(
    args: list[str], service: str, *, env: dict | None = None
) -> tuple[str, str]:
    """`(state, health)` for one service, both "" when it has no container yet.

    `--all` is load-bearing. Without it `compose ps` omits stopped containers,
    so a service that crashed at boot reads as absent rather than as `exited` —
    and `wait_ready`'s fast-fail on a dead container can then never fire. The
    symptom is a 120-second wait ending in `state='' health=''`, which is the
    least informative possible report of "it exited immediately".

    `--format json` emits either a JSON array or one object per line depending
    on the compose version, so both are parsed, and a payload that is neither
    reads as "not started yet" rather than raising a decode error out of a
    polling loop.
    """
    try:
        raw = _run(
            args + ["ps", "--all", "--format", "json", service], timeout=30, env=env
        ).strip()
    except ComposeError as exc:
        # Indistinguishable from "no container yet" to the caller by design —
        # the polling loop must not die on one bad `ps` — but logged, because
        # that ambiguity is exactly what hid the interpolation bug.
        logger.debug("compose ps failed: %s", exc)
        return "", ""
    if not raw:
        return "", ""

    records: list[dict] = []
    try:
        parsed = json.loads(raw)
        records = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        try:
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError:
            logger.debug("compose ps emitted neither JSON nor JSON-lines: %r", raw[:200])
            return "", ""

    for record in records:
        # Matched by name only. An earlier version accepted a lone record
        # whatever its service, which is inert on a one-service stack and wrong
        # the moment Layer 4 adds a second.
        if record.get("Service") == service:
            return record.get("State", ""), record.get("Health", "")
    return "", ""


def wait_ready(
    args: list[str], service: str, timeout: int = 120, *, env: dict | None = None
) -> None:
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
        state, health = _service_state(args, service, env=env)
        if health == "healthy":
            return
        if state == "running" and not health:
            return
        if state in ("exited", "dead"):
            break
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"{service} was not ready within {timeout}s (state={state!r}, "
        f"health={health!r})\n--- last logs ---\n{logs(args, service, env=env)}"
    )


def sweep_projects(prefix: str) -> None:
    """Tear down leftover compose projects whose name starts with `prefix`.

    Each test gets a unique project name so an interrupted run is never adopted
    mid-flight by the next one — but that trades one failure mode for another:
    nothing then reclaims the leftovers, and a killed session leaves a container
    and a named volume behind permanently. This is the sweep that closes it, run
    once at session start.

    Never raises. A failed sweep must not stop the run that would otherwise
    clean up after itself.
    """
    try:
        listing = subprocess.run(
            ["docker", "compose", "ls", "--all", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if listing.returncode != 0:
            return
        projects = json.loads(listing.stdout or "[]")
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return

    for project in projects:
        name = project.get("Name", "")
        if not name.startswith(prefix):
            continue
        logger.warning("sweeping leftover compose project %s", name)
        try:
            subprocess.run(
                [
                    "docker", "compose", "--project-name", name,
                    "down", "--volumes", "--remove-orphans", "--timeout", "5",
                ],
                capture_output=True,
                text=True,
                timeout=DOWN_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            continue
