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
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import services as service_support
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

#: The interface a host-side stub binds so a container can reach it. Every stub
#: bound here has to name a credential; `HttpStub.start` is what enforces that.
PUBLIC_BIND = "0.0.0.0"

READY_TIMEOUT = 120

# Releasing a parked confirmation, through the daemon's own `cancel_task`
# rather than a hand-written UPDATE — the harness should not be the second
# implementation of a status transition.
#
# Why release at all: `db.py` blocks any foreground task in a room that holds a
# `locked`, `running` or `pending_confirmation` task, and
# `confirmation_timeout_minutes` is 120. Under a session-scoped stack a
# scenario that parks one deliberately would wedge that room for every later
# test in the profile. It is *not* in-flight — the quiesce loop excludes it on
# purpose, because a suspended task will not move on its own and treating it as
# busy would make every reset wait out its whole timeout.
_RELEASE_CONFIRMATIONS = """
import sys
from istota import db
with db.get_db(sys.argv[1]) as conn:
    parked = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE status = 'pending_confirmation'"
    ).fetchall()]
    for task_id in parked:
        db.cancel_task(conn, task_id)
print(len(parked))
"""

# Emptying a container-side scratch directory without removing it — the ones in
# question are tmpfs mount points the compose file declares, so removing the
# directory itself would take the mount with it.
#
# `find -mindepth 1 -maxdepth 1 -exec rm -rf` rather than `rm -rf "$d"/*`,
# because a glob misses dotfiles and the thing most likely to be left behind in
# a checkout is `.git`.
_CLEAR_SCRATCH = (
    'set -eu; for d in "$@"; do '
    'if [ -d "$d" ]; then find "$d" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; fi; '
    "done"
)


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


class StackError(RuntimeError):
    """A stack was asked for something and answered wrongly.

    Distinct from `ComposeError`, which is compose itself failing. This one is
    the daemon inside a running stack: a task that would not submit, a `doctor`
    that printed something other than JSON, a render script that exited 2.
    """


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


# -- rendering the lean shape's config -------------------------------------

#: What every lean profile's config is rendered from, before any service adds
#: its own variables.
#:
#: **`NC_URL` and `APP_PASSWORD` are set, and empty.** Not `http://nextcloud`,
#: which is what this used to be: `Config.storage_is_nextcloud` is
#: `bool(nextcloud.url)`, so that rendered a config claiming Nextcloud-backed
#: storage and pointed it at a hostname `docker-compose.test.yml` resolves to
#: nothing. That is a third configuration — Nextcloud configured but absent —
#: and nobody ships it. Empty makes the lean daemon local-backed, which *is* a
#: shipped install shape and is truthful for every scenario the lean shape
#: runs, none of which touches storage.
#:
#: Set-but-empty rather than unset, and the difference is load-bearing:
#: `render-config.sh:68` preflights with `[ -n "${NC_URL+x}" ]`, which tests
#: whether the variable is *set*. Unset fails the render with exit 2 and a
#: "missing required input" message. `APP_PASSWORD` is required by the same
#: preflight and takes the same treatment.
#:
#: One measured consequence: `/mnt/shared` is a tmpfs on the lean stack, so
#: `runtime.mount_liveness` reported `ok` under the old value and reports
#: `skip` under this one. Any assertion comparing a whole `doctor` payload has
#: to become an assertion on named checks.
DEFAULT_RENDER_ENV: dict[str, str] = {
    "USER_NAME": "testuser",
    "BOT_USER": "istota",
    "USER_TIMEZONE": "UTC",
    "NC_URL": "",
    "APP_PASSWORD": "",
}


def render_config(
    render_script: Path,
    destination: Path,
    services: dict[str, Service],
    *,
    extra: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> Path:
    """Run the shipped render script on the host, into `destination`.

    This is the property that makes the lean shortcut legitimate: the file the
    stack boots from is produced by the same script the container would have
    run, not by a fixture that approximates it.

    Each service contributes its own `config_env()` — the variables that point
    the daemon at it — merged over the base, with `extra` (the profile's own
    `config`) last. Every one of those is a variable the shipped generator
    already reads, so a block it would not have produced cannot be smuggled in
    here, and the base environment is left with nothing subsystem-specific in
    it.

    Two services claiming the same variable is refused rather than resolved.
    Silent last-wins would boot a stack from a config naming the wrong
    service's port, and dict order is what would decide which — a diagnosis
    that starts from "the daemon never reached the feeds stub" and ends
    somewhere else entirely.

    The environment is explicit and **not** `os.environ`. The generator reads
    dozens of `ISTOTA_*` variables, so inheriting the developer's shell would
    make the config a stack boots from depend on whatever happens to be
    exported in the terminal that started the run — the same run passing on one
    machine and failing on another, with nothing in the repo to explain it.
    That is reproducibility, not test isolation: it does not stop the daemon
    queueing work of its own.
    """
    config_file = destination / "config.toml"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CONFIG_FILE": str(config_file),
        **DEFAULT_RENDER_ENV,
        **(base_env or {}),
    }
    claimed: dict[str, str] = {}
    for name, service in services.items():
        for variable, value in service.config_env().items():
            if variable in claimed:
                raise StackError(
                    f"{name} and {claimed[variable]} both set {variable}; one "
                    "of them would silently win and the stack would boot "
                    "pointing at the other"
                )
            claimed[variable] = name
            environment[variable] = value
    environment.update(extra or {})

    result = subprocess.run(
        ["bash", str(render_script)],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if result.returncode != 0:
        raise StackError(
            f"render-config.sh exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    if not config_file.exists():
        raise StackError("render-config.sh reported success but wrote nothing")
    return config_file


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

    def in_flight(self) -> list[dict]:
        """Task rows in a status the daemon may still be working on."""
        return [
            task for task in self.probe.tasks() if task.get("status") in IN_FLIGHT
        ]

    def _quiesce(self, deadline: float) -> None:
        busy: list = []
        while time.monotonic() < deadline:
            busy = self.in_flight()
            if not busy:
                return
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(
            "the daemon still had work in flight, so a scripted turn would "
            "have gone to it rather than to this scenario: "
            f"{[(task.get('id'), task.get('status')) for task in busy]}"
        )

    def script(self, turns: list[dict], *, timeout: float = 60) -> None:
        """Install a script, once the daemon is not going to consume it.

        `rescript` rewinds the endpoint, and the endpoint routes by call order
        alone — it has no notion of which task a request belongs to. So a task
        still in flight when a scenario rewinds takes turn 0, and the submitted
        task gets turn 1. The symptom is either an assertion about a merge
        request opened on behalf of a different task, or the exhausted-script
        error frame that rewinding exists to prevent — and both read as
        subsystem problems.

        Waiting for the table to go quiescent is most of the answer and was all
        of it while every test got its own stack. It is not all of it under a
        session-scoped pool, because the daemon's pollers run on their own
        threads for the whole session — Talk every 10 seconds, the tasks file
        every 30, eleven of them in total, all seeded to fire at boot. Any of
        them can create a task in the window between "the table read quiescent"
        and "the script is installed".

        Two mechanisms close that, and they cover different halves. The
        endpoint's `barrier()` refuses a request that arrives *during* the swap,
        which turns a stolen turn into a task that failed saying why. Re-reading
        the table afterwards catches the row that appeared but has not called
        yet, which the barrier structurally cannot see. Either one firing means
        going round again, and the loop is bounded by the same deadline as the
        quiesce so a busy daemon fails with a list of ids rather than hanging.
        """
        deadline = time.monotonic() + timeout
        while True:
            self._quiesce(deadline)
            before = self.endpoint.refused
            with self.endpoint.barrier():
                self.endpoint.rescript(turns)
            stolen = self.endpoint.refused - before
            busy = self.in_flight()
            if not stolen and not busy:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "a task kept appearing between quiescing and rescripting, "
                    f"so this scenario's script could not be installed cleanly "
                    f"({stolen} request(s) refused at the barrier, still in "
                    f"flight: {[(t.get('id'), t.get('status')) for t in busy]})"
                )

    def release_parked_confirmations(self) -> int:
        """Cancel every `pending_confirmation` task, and say how many.

        A parked confirmation is not in flight — it is suspended waiting for a
        human — but it wedges the room it sits in: `db.py` refuses any
        foreground task in a room already holding one, for the two hours
        `confirmation_timeout_minutes` allows. Under a session-scoped stack the
        next test in that room would fail for a reason that has nothing to do
        with it.

        Through the daemon's own `cancel_task` inside the container, rather
        than a hand-written UPDATE from the host: the harness should not be a
        second implementation of a status transition, and the DB lives on a
        named volume with no host path anyway.

        Guarded by a read first, and the guard is worth more than it looks.
        This exec is `uv run python -c` importing `istota.db`, which is one to
        two seconds — every test, on a tier whose whole point is that the
        per-test cost is now small. A `Probe` query answering "is there one" is
        tens of milliseconds, and on every run but the mail scenarios the
        answer is no.
        """
        if not self.probe.tasks(status="pending_confirmation"):
            return 0
        result = self.exec(
            ["uv", "run", "python", "-c", _RELEASE_CONFIRMATIONS, self.probe.db_path],
            timeout=120,
        )
        if result.returncode != 0:
            raise StackError(
                f"releasing parked confirmations exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        return int((result.stdout or "0").strip().splitlines()[-1])

    def container_state_paths(self) -> list[str]:
        """Container-side directories the profile's services say are per-test.

        Read off the services rather than off the profile, so a path cannot
        drift from the `config_env()` variable that pointed the daemon at it.
        """
        paths: list[str] = []
        for service in self.services.values():
            for path in getattr(service, "container_state_paths", ()):
                if not path.startswith("/") or path.count("/") < 2:
                    raise StackError(
                        f"{service.name} declares container state path {path!r}; "
                        "it must be absolute and below a top-level directory, "
                        "because this is emptied with rm -rf inside a container"
                    )
                paths.append(path)
        return paths

    def clear_container_state(self) -> None:
        """Empty what the profile's services declared, inside the container.

        The half of "reset" that lives on the far side of the process boundary.
        A host-side stub can clear its own recorded calls and rebuild its own
        repositories; it cannot reach the *checkout* the daemon made, and that
        checkout is state too. The forge is the worked example and the reason
        this exists: with `/data/repos` left alone, the second scenario's
        `git clone <url> project` fails on a directory that already exists,
        never reaches the listener, and reports itself as a forge that was
        never called.
        """
        paths = self.container_state_paths()
        if not paths:
            return
        result = self.exec(["sh", "-c", _CLEAR_SCRATCH, "sh", *paths], timeout=60)
        if result.returncode != 0:
            raise StackError(
                f"clearing {paths} exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )

    def reset(self, turns: list[dict] | None = None, *, timeout: float = 60) -> dict:
        """Put the stack back in the state a fresh test expects, and watermark it.

        Called before each test rather than after, so a failed test's state is
        still there to inspect and the next test is still clean.

        The order is forced. Parked confirmations go first, because releasing
        one produces a `cancelled` row and nothing else — it cannot make the
        table busy, and leaving it until after the quiesce would leave a wedged
        room for the test about to run. Then `script`, which is the quiesce, the
        barrier and the rewind. Then every *other* service, once nothing is
        using them — the model is excluded because `script` has just installed
        this test's turns and `ScriptedEndpoint.reset()` would throw them away
        — and alongside them the container-side directories those services
        declared, which no host-side `reset()` can reach. Then the watermark,
        last, so it is taken after every row this reset itself produced.

        It deliberately does **not** truncate `tasks` or any other table. The
        daemon is running, and deleting rows underneath its dispatcher is a
        race. Two exceptions, both narrow and both forced, and both above: the
        parked confirmation, and whatever a service's own `reset()` restores.

        The returned watermark is what negative assertions scope to — see
        `Probe.rows_above`, which will not let one be written with the
        watermark alone.
        """
        self.release_parked_confirmations()
        self.script(list(turns or []), timeout=timeout)
        for name, service in self.services.items():
            if name == "model":
                continue
            service.reset()
        self.clear_container_state()
        return self.probe.watermark()

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


@dataclass(frozen=True)
class LeanShape:
    """Everything booting a lean stack needs that the profile does not carry.

    One object rather than six constructor arguments on `StackPool`, because
    all six are properties of the *shape* — which compose file, which generator,
    which image — and the full shape in the next stage brings a different six.
    """

    compose_file: Path
    """`docker/docker-compose.test.yml`."""

    render_script: Path
    """`docker/istota/render-config.sh`, run on the host."""

    image: str
    """The tag `up --build` writes, shared by every stack in the session."""

    prebuilt_overlay: Path
    """Applied when a profile names its own `image`, so nothing is rebuilt."""

    ready_timeout: int = READY_TIMEOUT

    render_env: dict[str, str] = field(default_factory=dict)
    """Merged over `DEFAULT_RENDER_ENV`, for a caller with a house value."""


class StackPool:
    """Lazily-started stacks, keyed by profile name, for the length of a session.

    The arithmetic is the whole argument. A per-test `up` / `down --volumes` is
    about twelve seconds on the lean shape and minutes on the full one, and six
    subsystems on that model produces a tier nobody runs — which is the same as
    no tier. One boot per *profile* amortizes it across every test that declares
    the same one, and `Stack.reset` is what makes the sharing safe.

    The objection the per-test fixture was written against dissolves rather than
    being overridden: it held that the endpoint's `base_url` is baked into the
    rendered config, so a shared stack would need reconfiguring anyway. That is
    only true because the endpoint was started immediately before the render.
    Here the services start once per profile, *before* that profile's config is
    rendered, and live as long as the stack — so the address baked in stays
    valid, and `rescript` handles the per-test script, which is what it was
    written for.

    Two things stay outside the sharing. A test needing a different image is a
    different profile, because the image is a compose-level property. And a test
    asserting on start-up behaviour needs its own stack by construction; that is
    `fresh=True`, and the cost is visible at the point that asks for it.
    """

    def __init__(
        self,
        *,
        workdir: Path,
        lean: LeanShape,
        platform: str = "",
        project_prefix: str = "istota-testbed-",
    ) -> None:
        self.workdir = workdir
        self.lean = lean
        self.platform = platform
        self.project_prefix = project_prefix
        self._cached: dict[str, Stack] = {}
        self._private: list[Stack] = []
        self._booted = 0

    # -- the pool ---------------------------------------------------------

    def get(self, profile: Profile, *, fresh: bool = False) -> Stack:
        """The stack for `profile`, booting one if none is running.

        Keyed by `profile.name`, which is why `profiles.py` guards against two
        profiles sharing a name: the second would silently get the first's
        services.

        `fresh` bypasses the cache in both directions — it neither adopts a
        running stack nor leaves this one behind for the next caller. Hand it
        back to `release()` when the test is done.
        """
        if not fresh:
            running = self._cached.get(profile.name)
            if running is not None:
                return running
        stack = self._boot(profile)
        if fresh:
            self._private.append(stack)
        else:
            self._cached[profile.name] = stack
        return stack

    def release(self, stack: Stack) -> None:
        """Tear down a `fresh=True` stack. A cached one is ignored."""
        if stack in self._private:
            self._private.remove(stack)
            self._teardown(stack)

    def close_all(self) -> None:
        """Tear down every stack this pool started.

        Private stacks first, then cached ones, and each in its own `try` — a
        teardown that raised partway through would leave the rest running with
        their named volumes, which is the failure the session sweep exists to
        clean up after and should not have to.
        """
        for stack in list(self._private) + list(self._cached.values()):
            try:
                self._teardown(stack)
            except Exception as exc:  # pragma: no cover - teardown is best effort
                logger.warning("tearing down %s raised %s", stack.profile.name, exc)
        self._private.clear()
        self._cached.clear()

    # -- booting ----------------------------------------------------------

    def _boot(self, profile: Profile) -> Stack:
        """Start the profile's services, render, bring the stack up, wait ready.

        The order is not arrangeable: the services have to be listening before
        the config that names their ports is rendered, and the config has to
        exist before the container that reads it starts.
        """
        if profile.shape != "lean":
            raise StackError(
                f"profile {profile.name!r} declares shape {profile.shape!r}; "
                "only the lean shape is implemented"
            )

        self._booted += 1
        scratch = self.workdir / f"{profile.name}-{self._booted}"
        config_dir = scratch / "config"
        config_dir.mkdir(parents=True)

        services: dict[str, Service] = {}
        args: list[str] = []
        try:
            for name in profile.services:
                # Every host-side stub binds all interfaces, because the daemon
                # that reaches it lives in a container. `HttpStub.start` is what
                # makes each of them name the credential it is publishing.
                services[name] = service_support.build(
                    name, scratch=scratch, host=PUBLIC_BIND
                )
            args = self._compose_args(profile, scratch, config_dir)
            render_config(
                self.lean.render_script,
                config_dir,
                services,
                extra=profile.config,
                base_env=self.lean.render_env,
            )
            up(args, platform=self.platform)
            wait_ready(args, ISTOTA_SERVICE, timeout=self.lean.ready_timeout)
        except BaseException:
            # Both halves, and in this order. A stack that came up before
            # `wait_ready` timed out is holding a named volume; a stub that
            # bound before a later one raised is holding a publicly-bound
            # socket and a live thread for the rest of the session.
            if args:
                down(args, volumes=True)
            for service in services.values():
                try:
                    service.close()
                except Exception:  # pragma: no cover - cleanup is best effort
                    logger.debug("closing a service during a failed boot raised")
            raise

        return Stack(
            profile=profile, args=args, services=services, config_dir=config_dir
        )

    def _compose_args(
        self, profile: Profile, scratch: Path, config_dir: Path
    ) -> list[str]:
        """The compose prefix, and the env-file everything in it rides in.

        Compose interpolates the compose file on *every* subcommand, so a
        variable supplied only to `up` makes `ps`, `exec`, `logs` and `down`
        fail during interpolation, before they touch a container. `down`
        swallows its failures, so the visible symptom was a stack that survived
        the run holding a named volume while `wait_ready` sat out its whole
        timeout reading "no container yet". An `--env-file` rides in the
        argument list, so every subcommand gets it and no caller has to
        remember.
        """
        # A fresh project name per stack, so one left behind by an interrupted
        # run is never adopted (and then torn down) by the next session. The
        # session-start sweep is what reclaims those.
        project = f"{self.project_prefix}{uuid.uuid4().hex[:8]}"
        env_file = scratch / "compose.env"
        lines = [
            f"ISTOTA_TEST_CONFIG_DIR={config_dir}",
            f"ISTOTA_TEST_LEAN_IMAGE={self.lean.image}",
        ]
        overlays = list(profile.compose_overlays)
        if profile.image:
            lines.append(f"ISTOTA_TEST_IMAGE={profile.image}")
            overlays.append(self.lean.prebuilt_overlay)
        env_file.write_text("\n".join(lines) + "\n")
        return compose_args(
            self.lean.compose_file,
            project=project,
            env_file=env_file,
            overlays=overlays,
        )

    def _teardown(self, stack: Stack) -> None:
        # Volumes too: the DB is a named volume, and leaving it behind would
        # make the next session's assertions depend on this one's rows.
        down(stack.args, volumes=True)
        for service in stack.services.values():
            try:
                service.close()
            except Exception:  # pragma: no cover - teardown is best effort
                logger.debug("closing a service during teardown raised")
