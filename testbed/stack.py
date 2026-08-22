"""Driving a compose stack, and the object a scenario is handed.

Two halves. The plain functions — `compose_args`, `up`, `down`, `logs`,
`wait_ready`, `sweep_projects` — take an explicit argument list and hold no
state; `Stack` at the bottom of the file is the thing a scenario talks to, and
it is those functions plus the services the stack was pointed at.

Nothing here imports pytest. It used to: `LeanStack.submit` and `ForgeStack.doctor`
called `pytest.fail`, which was fine while they lived in a conftest and is not
fine in an installable package that istota-demo and istota-redteam consume. They
raise `StackError` instead, which pytest renders perfectly well.

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
import re
import shutil
import subprocess
import time
from pathlib import Path

from .probe import Probe
from .profiles import Profile
from .services import Service

logger = logging.getLogger(__name__)

# `up` covers a build on a cold cache, which on an emulated platform is the
# slowest thing in this tier.
UP_TIMEOUT = 900
DOWN_TIMEOUT = 120
POLL_INTERVAL = 0.5

#: The compose service the daemon runs as, on both shapes.
ISTOTA_SERVICE = "istota"

#: Where the entrypoint and the lean fixture both put the rendered config.
CONTAINER_CONFIG = "/data/config/config.toml"

# The non-terminal task statuses, from AGENTS.md's "Task Status" ladder
# (pending -> locked -> running -> completed / failed / pending_confirmation /
# cancelled). A task in one of these may still call the model.
# `pending_confirmation` is deliberately absent: it is suspended waiting for a
# human and will not move on its own, so treating it as in-flight would make
# `script` wait out its whole timeout. `Probe.wait_for_task` draws the same line
# for the same reason.
IN_FLIGHT = frozenset({"pending", "locked", "running"})


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
    compose_file: Path,
    *,
    project: str,
    env_file: Path | None = None,
    overlays: list[Path] | None = None,
) -> list[str]:
    """The invariant prefix for every compose call against one stack.

    `--project-name` is not optional here even though compose defaults it from
    the directory name: every stack in this repo's `docker/` directory would
    otherwise share one project, so a smoke run would adopt (and then tear down)
    a developer's running full stack.

    `overlays` are extra `-f` files merged over the base, in order. Like the
    base file they ride in the argument list, so every subcommand sees the same
    merged model — an overlay applied only to `up` would leave `ps`, `logs` and
    `down` reasoning about a different stack than the one running.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    for overlay in overlays or []:
        args += ["-f", str(overlay)]
    args += ["--project-name", project]
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


class StackError(RuntimeError):
    """A stack was asked for something and answered wrongly.

    Distinct from `ComposeError`, which is compose itself failing. This one is
    the daemon inside a running stack: a task that would not submit, a `doctor`
    that printed something other than JSON.
    """


class Stack:
    """A running stack and everything pointed at it.

    `LeanStack` and `ForgeStack` collapsed into this one class. `script`,
    `doctor` and `diagnostics` came off `ForgeStack`, where none of the three
    was about a forge — a scenario for any subsystem needs all three, and a
    second subclass per subsystem is how the first copy of `submit` gets made.

    The forge-shaped members went the other way, onto the service that owns
    them: `clone_url` and `branches` are `services["gitlab"]`'s, and a scenario
    reaches them through `stack.service("gitlab")`.
    """

    def __init__(
        self,
        *,
        profile: Profile,
        args: list[str],
        services: dict[str, Service],
        config_dir: Path | None = None,
    ) -> None:
        self.profile = profile
        self.args = args
        self.services = services
        self.config_dir = config_dir
        self.probe = Probe(compose_args=args, service=ISTOTA_SERVICE)

    # -- the services -----------------------------------------------------

    def service(self, name: str) -> Service:
        """One of the profile's services, by registry name."""
        try:
            return self.services[name]
        except KeyError:
            raise KeyError(
                f"profile {self.profile.name!r} runs no {name!r} service; it "
                f"has {sorted(self.services)}"
            ) from None

    @property
    def endpoint(self):
        """`services["model"]`, narrowed.

        Named because two things reach for it directly — the rewind step of a
        reset, and any scenario asserting on `transcript()` — and
        `service("model")` returns the protocol rather than the class that has
        `rescript` and `transcript` on it.
        """
        return self.service("model")

    # -- driving the daemon -----------------------------------------------

    def exec(
        self,
        argv: list[str],
        *,
        service: str = ISTOTA_SERVICE,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        """Run one command inside a service, capturing both streams.

        Named because four call sites were each rebuilding the
        `docker compose exec -T` prefix, and `-T` is the part that is easy to
        forget: without it compose allocates a TTY and the call hangs when
        stdin is not one, which under pytest it never is.

        The exit status is returned rather than raised on. Two callers depend on
        that — `doctor` exits non-zero whenever a check FAILs, which the
        negative control exists to produce, and a scenario probing the
        container's environment expects a non-zero grep.
        """
        return subprocess.run(
            self.args + ["exec", "-T", service, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def submit(self, prompt: str, *, user_id: str = "testuser") -> int:
        """Enqueue a task through the shipped CLI and return its id.

        Through `istota task` rather than by writing a row directly: inserting
        into `tasks` would assert nothing about the image, and the point of this
        tier is that the artifact works.

        The id is parsed out and returned because the caller needs it: the
        daemon queues tasks of its own for the same user at startup, so an
        assertion filtered on `user_id` alone can land on the wrong row.
        """
        result = self.exec(
            [
                "uv", "run", "istota", "-c", CONTAINER_CONFIG,
                "task", prompt, "-u", user_id, "--source-type", "cli",
            ],
            timeout=120,
        )
        if result.returncode != 0:
            raise StackError(
                f"submitting a task exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        match = re.search(r"Task created:\s*(\d+)", result.stdout)
        if not match:
            raise StackError(
                "could not read a task id out of `istota task` output; the CLI "
                f"prints 'Task created: N'\n--- stdout ---\n{result.stdout}"
            )
        return int(match.group(1))

    def script(self, turns: list[dict], *, timeout: float = 60) -> None:
        """Install a script, once the daemon is not going to consume it.

        `rescript` rewinds the endpoint, and the endpoint routes by call order
        alone — it has no notion of which task a request belongs to. The daemon
        queues work of its own at startup (`submit` says as much), so a task
        still in flight when a scenario rewinds would take turn 0, and the
        submitted task would get turn 1. The symptom is either an assertion
        about a merge request opened on behalf of a different task, or the
        exhausted-script error frame that rewinding exists to prevent — and both
        read as subsystem problems.

        So: wait for the task table to hold nothing non-terminal, then rewind.
        `Probe.wait_for_task` cannot express this — it waits for *a* task to
        reach a status, and what is wanted here is the absence of any that have
        not.
        """
        deadline = time.monotonic() + timeout
        busy: list = []
        while time.monotonic() < deadline:
            busy = [
                task
                for task in self.probe.tasks()
                if task.get("status") in IN_FLIGHT
            ]
            if not busy:
                self.endpoint.rescript(turns)
                return
            time.sleep(POLL_INTERVAL)

        raise TimeoutError(
            "the daemon still had work in flight after "
            f"{timeout}s, so a scripted turn would have gone to it rather than "
            f"to this scenario: {[(t.get('id'), t.get('status')) for t in busy]}"
        )

    def restart(self, service: str = ISTOTA_SERVICE) -> None:
        """Restart one service in place, keeping its volumes.

        The idempotence half of a provisioning scenario is "boot it twice", and
        `down` then `up` is not that — it would also be a different project's
        worth of teardown risk.
        """
        _run(self.args + ["restart", service], timeout=DOWN_TIMEOUT + UP_TIMEOUT)

    # -- reading it back --------------------------------------------------

    def doctor(self, *, scope: str = "") -> list[dict]:
        """`istota doctor --json` inside the running container.

        Through the shipped CLI in the shipped image, which is the whole point:
        a doctor run on the host would be asking about the developer's laptop.

        The exit code is deliberately ignored — `doctor.exit_code` is non-zero
        when a check FAILs, and the negative control exists to produce exactly
        that. What matters is the payload, and it is valid JSON either way by
        construction (`render_json`).

        Statuses arrive lowercase. `.claude/rules/deployment.md` records a
        consumer that filtered on `"FAIL"`, matched nothing, and shipped, so
        this normalizes once and every scenario compares against the normalized
        form rather than each learning the convention.
        """
        argv = ["uv", "run", "istota", "-c", CONTAINER_CONFIG, "doctor", "--json"]
        if scope:
            argv += ["--scope", scope]
        result = self.exec(argv, timeout=180)
        try:
            report = json.loads(result.stdout or "[]")
        except ValueError:
            raise StackError(
                f"`istota doctor --json` did not print JSON (exit "
                f"{result.returncode})\n--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            ) from None
        for check in report:
            if isinstance(check.get("status"), str):
                check["status"] = check["status"].lower()
        return report

    def logs(self, tail: int = 60, service: str = ISTOTA_SERVICE) -> str:
        return logs(self.args, service, tail=tail)

    def diagnostics(self, task: dict) -> str:
        """One string carrying everything a failed scenario needs.

        Assembled in one place because the useful context is spread over three
        sources — the task row, the daemon log, and whatever each service saw —
        and a scenario that printed only the first reports "the task failed" for
        a wrapper that was denied, a token that never arrived and a stub
        endpoint that answered 501, all identically.

        Each service renders *itself*, through `describe()`. The forge version
        of this reached into `stub.calls` and `stub.git_calls` directly, which
        is why it could only ever diagnose a forge.
        """
        seen = "\n".join(
            f"[{name}]\n{service.describe()}"
            for name, service in sorted(self.services.items())
            if hasattr(service, "describe")
        )
        return (
            f"task {task.get('id')} ended {task.get('status')!r}: "
            f"{task.get('error')!r}\n"
            f"--- result ---\n{task.get('result')}\n"
            f"--- services ---\n{seen}\n"
            f"--- daemon logs ---\n{self.logs(150)}"
        )
