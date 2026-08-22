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

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import probe as probe_support
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

# The same three, as SQL, because `Stack.in_flight` also has to reason about
# `scheduled_for` and that comparison has to happen in the database.
_IN_FLIGHT_SQL = "'pending', 'locked', 'running'"

#: The interface a host-side stub binds so a container can reach it. Every stub
#: bound here has to name a credential; `HttpStub.start` is what enforces that.
PUBLIC_BIND = "0.0.0.0"

READY_TIMEOUT = 120

#: The full shape's budget, and it is not the lean shape's with a margin. A cold
#: volume set spends most of it on Nextcloud installing itself and on the two
#: app-store downloads `provision-nc.sh` triggers, and `entrypoint.sh` then
#: allows itself 600 seconds waiting on the provisioning flag before it gives
#: up. A timeout shorter than that would report the harness's impatience as a
#: deployment failure.
FULL_READY_TIMEOUT = 1500

#: The compose services readiness means, per shape.
#:
#: `web` and `nginx` are deliberately absent from the full shape's tuple, and
#: not because they do not matter — because they restart-loop through a cold
#: boot *by design*. `web` polls for `config.toml` for 120 seconds and exits 1
#: (`docker-compose.yml:422-425`) while `istota` may take up to 600 seconds to
#: write it, and `nginx` starts before `web` is serving (`depends_on` there is
#: `service_started`, not `service_healthy`) so its startup resolution of the
#: `web` upstream can fail and take it round again.
#:
#: How waiting on one would fail is worth being exact about, because the first
#: draft of this comment said "would time out" and that is wrong: `wait_ready`
#: breaks immediately on `exited`, so it would *fast-fail* mid-loop on a stack
#: that was coming up correctly. That is worse than a timeout, not better.
#:
#: `nginx` is the one every `NextcloudService` HTTP read goes through, so not
#: waiting on it has a cost — an unretried connection refused, arriving as
#: whichever provisioning assertion ran first. That is paid for in
#: `NextcloudService._ocs`, which retries a connection error, rather than here.
#: The substring that marks a project holding kept volumes. Named rather than
#: spelled twice, because `sweep_projects` refuses to reap a project carrying it
#: and `_compose_args_full` is what puts it there.
KEEP_PROJECT_MARKER = "-full-keep-"

READY_SERVICES: dict[str, tuple[str, ...]] = {
    "lean": (ISTOTA_SERVICE,),
    "full": ("nextcloud", ISTOTA_SERVICE),
}

# The three pieces of framework state a reset has to *write*, all through the
# daemon's own functions rather than hand-written SQL — the harness should not
# be a second implementation of a status transition.
#
# A **parked confirmation** wedges a room: `db.py` blocks any foreground task
# in a room that holds a `locked`, `running` or `pending_confirmation` task,
# and `confirmation_timeout_minutes` is 120. It is deliberately *not* counted
# as in-flight, because a suspended task will not move on its own and treating
# it as busy would make every reset wait out its whole timeout.
#
# A **retry row** is a previous test's failed task waiting on the scheduler's
# backoff (`db.set_task_pending_retry`: `pending`, `scheduled_for` one, four or
# sixteen minutes out). Cancelled rather than waited on, because a retry of a
# task some earlier test submitted can never be work this test wants — and if
# it fires mid-test it consumes a scripted turn, which is the exact failure the
# barrier exists to prevent, arriving by a route the barrier cannot see.
#
# **Trusted senders** are cleared rather than watermarked. A test that trusts
# `catchall@ext.test` does not add a row a later test can filter past; it
# changes what every later scenario *means*, silently converting each
# untrusted-sender case into a trusted one.
_RESET_FRAMEWORK_STATE = """
import sys
from istota import db
released = retries = trusted = 0
with db.get_db(sys.argv[1]) as conn:
    for (task_id,) in conn.execute(
        "SELECT id FROM tasks WHERE status = 'pending_confirmation'"
    ).fetchall():
        db.cancel_task(conn, task_id)
        released += 1
    for (task_id,) in conn.execute(
        "SELECT id FROM tasks WHERE status = 'pending' AND attempt_count > 0"
    ).fetchall():
        db.cancel_task(conn, task_id)
        retries += 1
    for user_id, sender in conn.execute(
        "SELECT user_id, sender_email FROM trusted_email_senders"
    ).fetchall():
        db.remove_trusted_sender(conn, user_id, sender)
        trusted += 1
print(released, retries, trusted)
"""

# What decides whether the write above is worth an exec at all. `uv run python
# -c` importing `istota.db` is one to two seconds, on a tier whose per-test
# cost is now six-tenths of a second; this query is tens of milliseconds and
# answers "no" on every profile with no mail and no failures in it.
_DIRTY_STATE_SQL = """
SELECT
  (SELECT COUNT(*) FROM tasks WHERE status = 'pending_confirmation') AS parked,
  (SELECT COUNT(*) FROM tasks WHERE status = 'pending' AND attempt_count > 0) AS retries,
  (SELECT COUNT(*) FROM trusted_email_senders) AS trusted
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

#: Container paths the clearing above must never be pointed at, nor at any
#: ancestor of. Everything the tier reads its assertions out of lives under one
#: of them: the framework DB (`Probe.DEFAULT_DB_PATH`) and the rendered config
#: the daemon booted from. A declaration is code-owned rather than
#: model-supplied, so this is not an attack surface — it is the typo that would
#: turn "the second scenario saw a stale checkout" into "the stack stopped
#: answering", diagnosed as something else entirely.
PROTECTED_CONTAINER_PATHS = ("/data/db", "/data/config")

# "Has `entrypoint.sh` reached its last line" — asked of **pid 1 only**.
#
# `entrypoint.sh` ends in `exec uv run istota-scheduler`, so on the full shape
# pid 1 *is* the answer, and a `docker compose exec` shell is never pid 1.
#
# The first version of this globbed `/proc/[0-9]*/cmdline`, which is unsound in
# a way that is invisible from reading it: the probe runs as
# `sh -c '<this script>'`, so the probing shell's own command line contains the
# literal `istota-scheduler` and matches itself. It returned 0 on the first poll
# of any container — measured in a bare `alpine`, which has no istota in it at
# all — so `wait_healthy` waited for nothing and the idempotence assertions read
# pre-restart state. The same self-match hazard is why this probe is not used on
# the lean shape, whose entrypoint is a `sh -c` carrying the string from the
# moment it starts; the reasoning was written down there and not applied here.
#
# `/proc` rather than `pgrep`, which lives in `procps` and is not guaranteed to
# be in the image. `tr` rather than `grep -a`, because `cmdline` is
# NUL-separated and a plain `grep` treats the file as binary.
_SCHEDULER_RUNNING = (
    "tr '\\0' ' ' < /proc/1/cmdline 2>/dev/null | grep -q istota-scheduler"
)


def _check_container_state_path(service: str, path: str) -> None:
    """Refuse a path that `rm -rf` inside a container must not be aimed at."""
    parts = [part for part in path.split("/") if part]
    if not path.startswith("/") or len(parts) < 2 or ".." in parts:
        raise StackError(
            f"{service} declares container state path {path!r}; it must be "
            "absolute, below a top-level directory, and free of '..', because "
            "this is emptied with rm -rf inside a container"
        )
    normalized = "/" + "/".join(parts)
    for protected in PROTECTED_CONTAINER_PATHS:
        if normalized == protected or normalized.startswith(protected + "/"):
            raise StackError(
                f"{service} declares container state path {path!r}, which is "
                f"inside {protected} — the tier reads its assertions out of it"
            )
        if protected.startswith(normalized + "/"):
            raise StackError(
                f"{service} declares container state path {path!r}, which "
                f"contains {protected} — emptying it would take the tier's own "
                "database or config with it"
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


def _project_of(args: list[str]) -> str:
    """The `--project-name` value out of a compose argument list.

    Read back rather than remembered, because the argument list is the one
    thing that definitively ties a call to a stack — a project name held
    separately is a second source of truth for the same fact, and the failure
    when they disagree is `docker volume rm` silently removing nothing.
    """
    try:
        return args[args.index("--project-name") + 1]
    except (ValueError, IndexError):  # pragma: no cover - assembled by us
        raise StackError(f"no --project-name in {args!r}") from None


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


def up(
    args: list[str],
    *,
    platform: str = "",
    env: dict | None = None,
    build: bool = True,
) -> None:
    """Build and start the stack, detached.

    `--platform` is not a compose flag — compose has no per-invocation platform
    option — so it is passed through `DOCKER_DEFAULT_PLATFORM` instead. (The
    image tier one layer down uses a real `docker build --platform` flag; the
    two mechanisms differ, and only the effect is shared.)

    `build=False` is for a caller that has already built this session's image.
    Every stack in a session shares one tag, so a second `up --build` moves
    that tag while the first stack's containers are running — they hold the
    image *id* and are unaffected, but a third stack booted later could run a
    different artifact than the first two with nothing recording it. That is
    the moving-tag failure `docker-compose.test.yml` guards against across
    worktrees, and there is no reason to reintroduce it within one session.
    """
    overrides = dict(env or {})
    if platform:
        overrides.setdefault("DOCKER_DEFAULT_PLATFORM", platform)
    command = args + ["up", "--detach"]
    if build:
        command.insert(len(args) + 1, "--build")
    _run(command, timeout=UP_TIMEOUT, env=overrides)


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


def wait_all_ready(
    args: list[str],
    services: tuple[str, ...],
    *,
    timeout: int,
    env: dict | None = None,
    on_ready=None,
) -> None:
    """Wait on several services against one shared budget.

    Not `timeout` each. The full shape waits on `nextcloud` and then on
    `istota`, and the second wait is only interesting once the first has
    finished — giving each the whole budget would let a stack spend fifty
    minutes before reporting a failure that was visible in ten.

    `on_ready(service, seconds)` is called as each one lands, so a caller can
    record where a cold boot actually went. A ten-minute wait that ends in a
    bare timeout is the failure mode most likely to make someone stop running
    the tier, and a ten-minute wait that *succeeds* and says nothing is how
    "roughly ten minutes" stays an impression instead of a number.
    """
    started = time.monotonic()
    for service in services:
        remaining = int(timeout - (time.monotonic() - started))
        if remaining <= 0:
            raise TimeoutError(
                f"the budget of {timeout}s was spent before {service} was "
                f"waited on at all (reached: {services[:services.index(service)]})"
            )
        at = time.monotonic()
        wait_ready(args, service, timeout=remaining, env=env)
        if on_ready is not None:
            on_ready(service, time.monotonic() - at)


def sweep_projects(prefix: str) -> None:
    """Tear down leftover compose projects whose name starts with `prefix`.

    Each test gets a unique project name so an interrupted run is never adopted
    mid-flight by the next one — but that trades one failure mode for another:
    nothing then reclaims the leftovers, and a killed session leaves a container
    and a named volume behind permanently. This is the sweep that closes it, run
    once at session start.

    A project named `…-full-keep-…` is skipped, and skipping it is what makes
    `ISTOTA_TESTBED_KEEP` mean anything. A clean kept teardown removes the
    containers, so `compose ls` does not report the project and the sweep never
    sees it — but a *killed* kept session leaves them, and the sweep's
    `down --volumes` would then destroy `nextcloud_html`, `nextcloud_data` and
    `postgres_data`, which are the entire point. Compose adopts and recreates
    the leftover containers on the next `up` under the same project name, so
    leaving them is not a leak.

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
        if KEEP_PROJECT_MARKER in name:
            logger.warning(
                "leaving %s alone: it holds the volumes ISTOTA_TESTBED_KEEP "
                "exists to keep, and the next boot adopts its containers",
                name,
            )
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

    Nothing carries `HOME`, `LANG` or `TMPDIR`, and that is checked rather than
    assumed: `render-config.sh` references none of them and expands no `~`.
    `gitlab._base_env` does pass them, because it runs `git`, which reads all
    three. If the generator ever grows a tool that does, it goes here.
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


# -- the full shape's environment ------------------------------------------

#: Services the spec's later stages add, named here so `FULL_MODULE_SWITCHES`
#: can point at them before they exist.
#:
#: Without this the guard on that map would have to accept any string, which is
#: the same as not checking for a typo at all. It is also a ratchet: a unit test
#: asserts this set and `REGISTRY` stay disjoint, so registering `mail` fails
#: until the name is removed from here.
PLANNED_SERVICES = frozenset({"feeds"})

#: Every module `docker-compose.yml` turns on by default, mapped to the service
#: whose presence in a profile is what turns it back on. Empty means nothing in
#: this tier turns it on.
#:
#: This map is what makes `Profile` mean anything on the full shape. The shipped
#: file defaults them all on — Talk, email, feeds, money, location, both sleep
#: cycles and the browser (with no browser container in the tier) — so a `full`
#: profile declaring `services=("model", "nextcloud")` would boot a daemon
#: polling every subsystem, which is exactly what the profile mechanism exists
#: to prevent.
#:
#: One module compose defaults on is deliberately absent:
#: `ISTOTA_MEMORY_SEARCH_ENABLED` is in-process indexing rather than a poller,
#: and switching it off would change what the assembled prompt contains for
#: every full-shape scenario.
#:
#: `ISTOTA_TALK_ENABLED` is *present* rather than being left to
#: `nextcloud.config_env()`, because that service's `config_env()` is empty by
#: design — the shipped compose file already points the daemon at its own
#: `nextcloud`, and a service inventing a variable to say "I am present" would
#: be the fixture side-loading config.
FULL_MODULE_SWITCHES: dict[str, str] = {
    "ISTOTA_TALK_ENABLED": "nextcloud",
    "ISTOTA_EMAIL_ENABLED": "mail",
    "ISTOTA_FEEDS_ENABLED": "feeds",
    "ISTOTA_DEVELOPER_ENABLED": "gitlab",
    "ISTOTA_MONEY_ENABLED": "",
    "ISTOTA_LOCATION_ENABLED": "",
    "ISTOTA_BROWSER_ENABLED": "",
    "ISTOTA_SLEEP_CYCLE_ENABLED": "",
    "ISTOTA_CHANNEL_SLEEP_CYCLE_ENABLED": "",
}

#: Identity the full stack requires by name. `docker-compose.yml` preflights
#: `USER_NAME` with `${USER_NAME:?}`, so an absent value fails `up` during
#: interpolation rather than at boot.
FULL_IDENTITY: dict[str, str] = {
    "USER_NAME": "testuser",
    "BOT_USER": "istota",
    "USER_TIMEZONE": "UTC",
    "ISTOTA_BOT_NAME": "Istota",
}

#: The four `${…:?}` credentials `docker-compose.yml` refuses to start without.
CREDENTIAL_KEYS = (
    "POSTGRES_PASSWORD",
    "ADMIN_PASSWORD",
    "BOT_PASSWORD",
    "USER_PASSWORD",
)


@dataclass(frozen=True)
class FullCredentials:
    """This session's generated passwords, plus the port they were bound to.

    Generated rather than read from `docker/.env`, which on a developer machine
    is a gitignored file holding real ones. Nothing in this tier reads it.

    `nc_port` travels with them because it is credential-shaped state in one
    specific sense: `provision-nc.sh:106` bakes `ISTOTA_WEB_CALLBACK_URL` —
    which is derived from the port — into the `oauth2_clients` row at first
    install and never revisits it. A kept volume set and a different port is a
    stale registration, so the port is persisted alongside the passwords rather
    than re-invented.

    `__repr__` is redacted, and for the same reason `ServiceCall`'s is: pytest's
    assertion rewriting renders the repr of whatever a failing comparison
    touched, and this object reaches a `Stack`. A generated password in a
    failure report on a public repo is a password in a terminal scrollback that
    gets pasted into an issue.
    """

    postgres_password: str
    admin_password: str
    bot_password: str
    user_password: str
    nc_port: int

    def as_env(self) -> dict[str, str]:
        return {
            "POSTGRES_PASSWORD": self.postgres_password,
            "ADMIN_PASSWORD": self.admin_password,
            "BOT_PASSWORD": self.bot_password,
            "USER_PASSWORD": self.user_password,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return f"FullCredentials(<4 redacted>, nc_port={self.nc_port})"


def reserve_port() -> int:
    """Bind an ephemeral port, read it back, release it.

    `docker-compose.yml:456` binds `${NC_PORT:-8080}:80` on nginx — a *fixed*
    host port, unlike the lean stack which publishes nothing — so a developer's
    own demo stack or a second worktree collides and `up` fails. Measured while
    this was written: a demo stack was holding 8080 on the machine.

    Racy by construction, and knowingly so: the kernel can hand the same port to
    something else between the release here and compose's bind. The alternative
    is holding the socket open, which compose then cannot bind at all. A lost
    race fails `up` loudly with "address already in use", which is the right
    failure for a condition this rare.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def generate_credentials(nc_port: int) -> FullCredentials:
    """Four fresh passwords for one session.

    `token_urlsafe` rather than anything with punctuation in it, because these
    are written into a compose `--env-file`, which is parsed as bare
    `KEY=VALUE` with no quoting rules to hide behind, and then handed to
    Nextcloud's `occ user:add --password-from-env`.
    """
    return FullCredentials(
        postgres_password=secrets.token_urlsafe(24),
        admin_password=secrets.token_urlsafe(24),
        bot_password=secrets.token_urlsafe(24),
        user_password=secrets.token_urlsafe(24),
        nc_port=nc_port,
    )


def full_env(
    services: dict[str, Service],
    credentials: FullCredentials,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Everything the full shape's compose env-file carries.

    A pure function of the profile's services and this session's credentials, so
    the tier's central "what does a `full` profile actually boot" question has a
    unit test rather than a stack behind it.

    Four groups, in the order they are layered:

    1. **Identity and credentials.** `docker-compose.yml` preflights five of
       these with `${…:?}` and will not interpolate without them.
    2. **The module switches**, every one off unless the profile names the
       service that owns it. See `FULL_MODULE_SWITCHES`.
    3. **`NC_PORT` and an explicit `ISTOTA_WEB_CALLBACK_URL`.** The port feeds
       `OVERWRITEHOST`, `OVERWRITECLIURL`, `ISTOTA_WEB_NC_EXTERNAL_URL`,
       `ISTOTA_WEB_SITE_HOSTNAME` and the callback URL through four levels of
       nested compose defaults. The callback URL is written out rather than
       left to that chain because `provision-nc.sh` bakes it irreversibly into
       the `oauth2_clients` row at first install, and a value assembled by
       four `${A:-${B:-${C:-D}}}` substitutions is not one a test can assert
       against without re-implementing compose's interpolation.
    4. **Each service's `config_env()`**, then the profile's own `config`.

    The three credential-shaped brain variables are *not* here and must not be:
    compose lets the process environment outrank an `--env-file`, so a developer
    with `ANTHROPIC_API_KEY` exported would win. They are literals in
    `testbed/compose/testbed.yml` instead, which nothing outranks.

    Two claims are refused rather than resolved, on the same reasoning as
    `render_config`: silent last-wins would boot a stack pointing at the wrong
    thing and dict order would be what decided. Two services claiming one
    variable is the obvious one. The other is a service — or a profile's own
    `config` — overwriting the identity, a credential, the port or the callback
    URL, which are this function's to set: a profile that quietly renamed
    `USER_NAME` would leave `NextcloudService` authenticating as a user the
    stack never created, and one that moved `NC_PORT` would leave the OAuth2
    redirect URI baked at a port nothing publishes.

    A service *may* override a module switch, and one does: `gitlab.config_env()`
    returns `ISTOTA_DEVELOPER_ENABLED=true` against a map that defaults it off,
    which is the whole point of the map being a default.
    """
    environment: dict[str, str] = dict(FULL_IDENTITY)
    environment.update(credentials.as_env())

    for variable, owner in FULL_MODULE_SWITCHES.items():
        environment[variable] = "true" if owner and owner in services else "false"

    environment["NC_PORT"] = str(credentials.nc_port)
    environment["ISTOTA_WEB_CALLBACK_URL"] = (
        f"http://localhost:{credentials.nc_port}/istota/callback"
    )

    reserved = set(FULL_IDENTITY) | set(CREDENTIAL_KEYS) | {
        "NC_PORT",
        "ISTOTA_WEB_CALLBACK_URL",
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
            if variable in reserved:
                raise StackError(
                    f"{name} sets {variable}, which the stack itself owns; a "
                    "service cannot rename the users or move the published port"
                )
            claimed[variable] = name
            environment[variable] = value
    for variable, value in (extra or {}).items():
        if variable in reserved:
            raise StackError(
                f"the profile's config sets {variable}, which the stack itself "
                "owns; a profile cannot rename the users or move the port"
            )
        environment[variable] = value
    environment.update(compose_env(services, claimed=claimed, reserved=reserved))
    return environment


def compose_env(
    services: dict[str, Service],
    *,
    claimed: dict[str, str] | None = None,
    reserved: set[str] | None = None,
) -> dict[str, str]:
    """Interpolation variables the profile's overlays need, from the services.

    Distinct from `config_env()`, and held to a different rule. That one points
    the *daemon* at a service and may only name variables the shipped generator
    reads and `docker-compose.yml` passes through — the property that makes the
    whole tier honest. These are host paths and image tags a compose *overlay*
    binds, which configure nothing about istota and appear in no shipped file.

    They exist because compose resolves a relative bind against the first `-f`
    file's directory, which is `docker/` rather than this package, so an overlay
    living here can only name an absolute path handed to it. That is already how
    `docker-compose.test.yml` receives the rendered config directory.

    Optional on the protocol, read by `getattr`: five of the six services need
    no overlay and would otherwise carry an empty method apiece. The same claim
    and reservation guards apply as for `config_env`, so an overlay variable
    cannot silently overwrite a config one or the stack's own identity.
    """
    claimed = {} if claimed is None else claimed
    reserved = set() if reserved is None else reserved
    collected: dict[str, str] = {}
    for name, service in services.items():
        provider = getattr(service, "compose_env", None)
        if provider is None:
            continue
        for variable, value in provider().items():
            if variable in claimed:
                raise StackError(
                    f"{name} and {claimed[variable]} both set {variable}; one "
                    "of them would silently win and the stack would boot "
                    "pointing at the other"
                )
            if variable in reserved:
                raise StackError(
                    f"{name} sets {variable}, which the stack itself owns; a "
                    "service cannot rename the users or move the published port"
                )
            claimed[variable] = name
            collected[variable] = value
    return collected


def write_env_file(path: Path, environment: dict[str, str]) -> Path:
    """Write a compose `--env-file`, refusing a value it cannot represent.

    Compose's env-file parser is line-oriented `KEY=VALUE` with no escaping, so
    three shapes do not survive the round trip: a newline becomes a second,
    malformed entry; an unquoted ` #` starts a comment and truncates the value;
    and leading or trailing whitespace is stripped. All three read downstream as
    a variable that is *unset or wrong*, which on this compose file means a
    `${…:?}` preflight failure blamed on the wrong key. Refused here, by name,
    rather than diagnosed there.

    Created 0600, not chmod-ed to it afterwards. Four of these values are
    passwords, and `write_text` then `chmod` leaves them world-readable for the
    length of the write.
    """
    lines = []
    for key, value in environment.items():
        if "\n" in value or "\r" in value:
            raise StackError(
                f"{key} contains a newline; a compose env-file cannot carry one"
            )
        if " #" in value:
            raise StackError(
                f"{key} contains ' #'; compose reads the rest of the line as a "
                "comment and the value would be silently truncated"
            )
        if value != value.strip():
            raise StackError(
                f"{key} has leading or trailing whitespace, which compose strips"
            )
        lines.append(f"{key}={value}")
    _write_private(path, "\n".join(lines) + "\n")
    return path


def _write_private(path: Path, body: str) -> None:
    """Create a file 0600 and write it, never existing at the process umask."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)
    # Only for a path that already existed: `os.open`'s mode argument applies to
    # a file it creates and is ignored for one it opens.
    path.chmod(0o600)


#: Key suffixes whose value is a credential whoever named it.
#:
#: By shape as well as by name, because `CREDENTIAL_KEYS` is only the four
#: passwords `docker-compose.yml` preflights — and a *service* contributes keys
#: too. `GitLabService.config_env()` already returns
#: `ISTOTA_DEVELOPER_GITLAB_TOKEN`, so the first `full` profile carrying a forge
#: would put a token on `Stack.env` in the clear. A list a future service has to
#: remember to extend is a list that will not be extended.
CREDENTIAL_SUFFIXES = ("_PASSWORD", "_TOKEN", "_SECRET", "_KEY")


def is_credential_key(key: str) -> bool:
    return key in CREDENTIAL_KEYS or key.endswith(CREDENTIAL_SUFFIXES)


def redacted(environment: dict[str, str]) -> dict[str, str]:
    """The env map with every credential-shaped value replaced.

    What a `Stack` exposes to a scenario. A test needs `USER_NAME`,
    `ISTOTA_WEB_CALLBACK_URL` and the module switches; none needs a credential,
    and a dict on a `Stack` is exactly the kind of thing that ends up in a
    pytest failure report on a public repo.
    """
    return {
        key: ("<redacted>" if is_credential_key(key) else value)
        for key, value in environment.items()
    }


def conflicting_process_env(environment: dict[str, str]) -> dict[str, str]:
    """Owned keys that the *process* environment would override, and with what.

    Compose interpolates from its own environment first and from `--env-file`
    only as a fallback, so an exported variable beats anything the harness
    writes into a file. `testbed/compose/testbed.yml` solves that for the three
    credential-shaped brain variables by hardcoding them as compose literals,
    which nothing outranks — but that fix covers three keys and the hazard
    covers all of them.

    What it costs when it bites is worth stating, because none of it is loud.
    An exported `ISTOTA_BRAIN_KIND` boots the tier on `claude_code` against the
    real API rather than the scripted endpoint. An exported `ADMIN_PASSWORD`
    installs Nextcloud with one password while `NextcloudService` authenticates
    with the generated one, which arrives as 401s that read as "Talk is broken".
    An exported *empty* `USER_NAME` fails the `${USER_NAME:?}` preflight on
    every compose subcommand, which `_service_state` reports as "no container
    yet" and `down` swallows — the exact silent-both-ways shape Stage 2 chased.

    So the boot refuses rather than guessing. Only a *differing* value counts: a
    developer who happens to export `USER_NAME=testuser` is not fought with.
    Note that `tests/conftest.py::_load_dotenv` injects a repo-root `.env` into
    `os.environ` before any of this runs, so a key landing there is
    indistinguishable from an exported one — and is caught the same way.
    """
    return {
        key: value
        for key, value in environment.items()
        if key in os.environ and os.environ[key] != value
    }


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
        env: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self.args = args
        self.services = services
        self.config_dir = config_dir
        #: The compose environment this stack booted from, **redacted**. Empty
        #: on the lean shape, which renders on the host and carries nothing a
        #: scenario needs. On the full shape it is what the provisioning
        #: assertions compare against — `ISTOTA_WEB_CALLBACK_URL` above all,
        #: since the whole point is that the value Nextcloud baked into its
        #: `oauth2_clients` row is the one compose was given. Passwords read
        #: `<redacted>`; nothing in the tier asserts on one.
        self.env = redacted(env or {})
        self.probe = Probe(compose_args=args, service=ISTOTA_SERVICE)
        #: The watermark the most recent `reset` returned, for the negative
        #: assertions in `Probe.rows_above`. Set by the fixture that drives the
        #: reset, because the instant it is taken is what makes it useful: a
        #: scenario taking its own would take it after `submit`, which is too
        #: late for the row it wants to prove was never written.
        self.mark: dict[str, int] = {}

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
        user: str = "",
    ) -> subprocess.CompletedProcess:
        """Run one command inside a service, capturing both streams.

        `user` is for the one caller that needs it: Nextcloud's `occ` refuses to
        run as root, and the message it prints then ("Console has to be executed
        with the user that owns the file config/config.php") is not one anybody
        reads as "wrong `-u`".

        Named because four call sites were each rebuilding the
        `docker compose exec -T` prefix, and `-T` is the part that is easy to
        forget: without it compose allocates a TTY and the call hangs when
        stdin is not one, which under pytest it never is.

        The exit status is returned rather than raised on. Two callers depend on
        that — `doctor` exits non-zero whenever a check FAILs, which the
        negative control exists to produce, and a scenario probing the
        container's environment expects a non-zero grep.

        Counted alongside `Probe.query`, because both are the thing Open
        question 4 asks about: this path carries `submit`, `doctor`, the
        framework-state write and the container-state clearing, several of them
        once per test, and a measurement that left them out would report a
        fraction under a label that says `docker compose exec`.
        """
        prefix = ["exec", "-T"] + (["-u", user] if user else [])
        with probe_support.counted_exec():
            return subprocess.run(
                self.args + prefix + [service, *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

    def published_port(self, service: str, container_port: int) -> int:
        """Which host port compose published `service`'s `container_port` on.

        Asked of compose rather than fixed in a file, because the overlays that
        publish anything bind `127.0.0.1::<port>` and let Docker choose — a
        fixed host port collides with a developer's own stack and with a second
        worktree, which `docker-compose.yml`'s `NC_PORT` already taught this
        tier once.

        `docker compose port` answers `0.0.0.0:54321` or `127.0.0.1:54321`, and
        may answer with more than one line when a port is published on several
        interfaces; the first is taken and the address discarded, since the
        caller reaches it on loopback either way.
        """
        result = subprocess.run(
            self.args + ["port", service, str(container_port)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        first = (result.stdout or "").strip().splitlines()
        if result.returncode != 0 or not first:
            raise StackError(
                f"compose could not say which host port {service}:"
                f"{container_port} is published on (exit {result.returncode})\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        _, _, port = first[0].rpartition(":")
        if not port.isdigit():
            raise StackError(
                f"compose answered {first[0]!r} for {service}:{container_port}, "
                "which does not end in a port number"
            )
        return int(port)

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
        """Task rows the daemon may act on *now*.

        Not simply "status in `IN_FLIGHT`", and the difference is what stops a
        session-scoped stack wedging itself. A task that fails goes back on the
        scheduler's retry ladder as `status = 'pending'` with `scheduled_for`
        one, then four, then sixteen minutes out (`db.set_task_pending_retry`).
        Counting that row as busy makes every later reset in the profile wait
        out a backoff it cannot shorten — sixteen minutes outlives the session,
        and the failure surfaces as a setup error on tests that had nothing to
        do with it.

        `scheduled_for` is compared by SQLite rather than in Python, so the
        clock is the database's. The host and the container do not have to
        agree, and a `datetime('now')` written by the daemon is only
        meaningfully comparable to a `datetime('now')` read the same way.
        """
        return self.probe.query(
            f"SELECT * FROM tasks WHERE status IN ({_IN_FLIGHT_SQL}) "
            "AND (scheduled_for IS NULL OR scheduled_for <= datetime('now')) "
            "ORDER BY id"
        )

    def _quiesce(self, deadline: float, *, note: str = "") -> None:
        """Poll until nothing is in flight, or raise saying what still was.

        A `while True` with the deadline checked *after* the read, so an
        already-expired deadline still reports what it saw. The `while
        time.monotonic() < deadline` form raises with an empty list and a
        message claiming work was in flight, which is the least useful thing it
        could say.
        """
        busy: list = []
        while True:
            busy = self.in_flight()
            if not busy:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(
            "the daemon still had work in flight, so a scripted turn would "
            "have gone to it rather than to this scenario: "
            f"{[(task.get('id'), task.get('status')) for task in busy]}"
            + (f"\n{note}" if note else "")
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

        Three mechanisms close that, and each covers a case the others cannot
        see. The endpoint's `barrier()` refuses a request that arrives *during*
        the swap. Re-reading the task table afterwards catches the row that
        appeared but has not called yet. And `endpoint.served` catches the one
        neither of those can: a poller's task created, served and finished
        entirely between the barrier dropping and the table being read, which
        is one `docker compose exec` round trip and therefore a window of
        hundreds of milliseconds. `rescript` sets `served` to zero, so a
        non-zero reading afterwards is exact and free — this scenario has not
        submitted anything yet, so any turn served is not its own.

        Any of the three firing means going round again. The loop is bounded by
        the same deadline as the quiesce, so a busy daemon fails with a list of
        ids rather than hanging, and the refusals seen along the way are
        accumulated into that message — the cause is otherwise lost the moment
        the next iteration recomputes it.
        """
        deadline = time.monotonic() + timeout
        stolen = 0
        while True:
            self._quiesce(
                deadline,
                note=(
                    f"({stolen} request(s) had already been refused at the "
                    "barrier, so a poller was competing for this script)"
                    if stolen
                    else ""
                ),
            )
            before = self.endpoint.refused
            with self.endpoint.barrier():
                self.endpoint.rescript(turns)
            # `this_round` decides whether to loop; `stolen` only accumulates
            # for the message. Testing the running total would make the loop
            # unable to exit once a single refusal had ever happened.
            this_round = self.endpoint.refused - before
            stolen += this_round
            served = self.endpoint.served
            busy = self.in_flight()
            if not this_round and not served and not busy:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "a task kept appearing between quiescing and rescripting, "
                    "so this scenario's script could not be installed cleanly "
                    f"({stolen} request(s) refused at the barrier, {served} "
                    "turn(s) served before the scenario submitted anything, "
                    f"still in flight: "
                    f"{[(t.get('id'), t.get('status')) for t in busy]})"
                )

    def reset_framework_state(self) -> tuple[int, int, int]:
        """Clear the three things a reset has to *write*, and say what it cleared.

        Returns `(confirmations released, retries cancelled, senders untrusted)`.
        Each is explained where `_RESET_FRAMEWORK_STATE` is defined; between
        them they are the whole of what this harness writes to a live database,
        and every one is done through the daemon's own function.

        Guarded by a read first, and the guard is worth more than it looks. The
        write is `uv run python -c` importing `istota.db`, which is one to two
        seconds — every test, on a tier whose whole point is that the per-test
        cost is now small. The read is a single `Probe` query at tens of
        milliseconds, and on a profile with no mail and no failed task in it
        the answer is "nothing to do".
        """
        counts = self.probe.query(_DIRTY_STATE_SQL)
        dirty = counts[0] if counts else {}
        if not any(dirty.get(key) for key in ("parked", "retries", "trusted")):
            return (0, 0, 0)
        result = self.exec(
            ["uv", "run", "python", "-c", _RESET_FRAMEWORK_STATE, self.probe.db_path],
            timeout=120,
        )
        if result.returncode != 0:
            raise StackError(
                f"resetting framework state exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        fields = (result.stdout or "").strip().splitlines()[-1].split()
        return tuple(int(field) for field in fields)  # type: ignore[return-value]

    def container_state_paths(self) -> list[str]:
        """Container-side directories the profile's services say are per-test.

        Read off the services rather than off the profile, so a path cannot
        drift from the `config_env()` variable that pointed the daemon at it.

        The guard is doing real work, because what follows is `rm -rf` inside a
        container running as root. Counting slashes is not enough: `//`,
        `/data/` and `/data/../data/db` all have two, and the last two empty
        the database the tier reads all its assertions out of. So the path is
        split into non-empty components, `..` is refused outright, and anything
        that *is* or *contains* a load-bearing path is refused by name.
        """
        paths: list[str] = []
        for service in self.services.values():
            for path in getattr(service, "container_state_paths", ()):
                _check_container_state_path(service.name, path)
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

        **`/mnt/shared` is knowingly outside this**, and the omission is a
        decision rather than an oversight. `render-config.sh` renders
        `nextcloud_mount_path` as that literal on every profile, so memory
        files, `TASKS.md` and per-user directories accumulate there for a whole
        session — and the tasks-file poller reads one of them every 30 seconds.
        No scenario in this tier writes there yet, and emptying it wholesale
        would remove the tree the daemon built at boot (the seeded money ledger
        among it) with nothing to recreate it. The stage that adds a scenario
        writing under `/mnt/shared` is the one that has to settle what a
        per-test clear of it means; declaring it here first would trade a
        known gap for an unknown breakage.
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

        The order is forced, and **the script goes last**. Every step before it
        is slow — `reset_framework_state` may be a two-second exec,
        `GitLabService.reset` rmtrees and re-seeds a repository,
        `clear_container_state` is another container round trip — and the
        script is only protected while `script` holds the barrier across the
        swap. Installing it first and then spending seconds on the rest leaves
        this test's turn 0 exposed for exactly as long as the rest takes, which
        is the defect the barrier was added to close, moved a few lines later.

        So: clear the framework state that has to be written, quiesce once so
        nothing is using the services, reset every *other* service and the
        container-side directories they declared, and only then quiesce again
        and install the script. The model is excluded from the service loop
        because `script` is what scripts it, and `ScriptedEndpoint.reset()`
        would throw those turns away. The watermark is taken last, after every
        row this reset itself produced.

        It deliberately does **not** truncate `tasks` or any other table. The
        daemon is running, and deleting rows underneath its dispatcher is a
        race. The exceptions are narrow, forced, and all in
        `reset_framework_state`: a parked confirmation, a previous test's retry
        row, and the trusted-sender list.

        The returned watermark is what negative assertions scope to — see
        `Probe.rows_above`, which will not let one be written with the
        watermark alone. The `stack` fixture stashes it as `stack.mark`.
        """
        deadline = time.monotonic() + timeout
        self.reset_framework_state()
        # Once, before touching the services, so nothing is mid-clone when a
        # repository is rebuilt underneath it. `script` quiesces again, which
        # on a quiet daemon is one cheap query.
        self._quiesce(deadline)
        for name, service in self.services.items():
            if name == "model":
                continue
            try:
                service.reset()
            except StackError:
                raise
            except Exception as exc:
                # Translated rather than propagated raw, because of where it
                # lands: the `stack` fixture turns `StackError` into a
                # `pytest.fail(..., pytrace=False)` naming the condition, and
                # anything else into a fixture traceback attributed to whichever
                # test happened to be next. A service that could not clean up
                # after the *previous* test is a harness condition, and the one
                # line worth reading is which service it was.
                raise StackError(
                    f"the {name} service could not reset, so this test would "
                    f"run against the previous one's state: {exc}"
                ) from exc
        self.clear_container_state()
        self.script(
            list(turns or []), timeout=max(1.0, deadline - time.monotonic())
        )
        return self.probe.watermark()

    def restart(self, service: str = ISTOTA_SERVICE) -> None:
        """Restart one service in place, keeping its volumes.

        The idempotence half of a provisioning scenario is "boot it twice", and
        `down` then `up` is not that — it would also be a different project's
        worth of teardown risk.

        It does **not** wait: `compose restart` returns as soon as the container
        is running, which on the full shape is the beginning of a boot that
        polls Nextcloud, re-provisions rooms and only then opens the database.
        `wait_healthy` is the other half, and separate so a caller that restarts
        two services waits once.
        """
        _run(self.args + ["restart", service], timeout=DOWN_TIMEOUT + UP_TIMEOUT)

    def wait_healthy(self, *, timeout: int | None = None) -> None:
        """Block until the daemon has finished booting, after a restart.

        Two conditions, and the second is what makes this usable at all.

        The compose health check answers "can the daemon do work" by looking for
        the `tasks` table. That is exactly right at a *cold* boot, when the
        database does not exist until `istota init` has run. It is nearly
        useless after a restart, because the database is on a named volume and
        survives: the probe passes within seconds while `entrypoint.sh` is still
        polling Nextcloud and re-provisioning rooms. An idempotence assertion
        that trusted it would read the pre-restart state and pass for the wrong
        reason.

        So the full shape also waits for the entrypoint to reach its last line —
        `exec uv run istota-scheduler` — by reading pid 1's command line. See
        `_SCHEDULER_RUNNING` for why it is pid 1 rather than a scan, which is
        not a detail: the scan matched the probing shell itself.

        Only on the full shape. The lean stack's entrypoint is a `sh -c` whose
        own command line contains the string from the moment it starts, so the
        probe would answer "yes" before `istota init` had run.
        """
        if timeout is None:
            timeout = (
                FULL_READY_TIMEOUT if self.profile.shape == "full" else READY_TIMEOUT
            )
        # A floor rather than only the caller's number. `StackPool._boot_full`
        # passes the remainder of a budget that `up` has already eaten into, and
        # `up` blocks on `depends_on: nextcloud: service_healthy`, whose own
        # check allows 300s of start period plus twenty 15s retries. A slow but
        # entirely correct cold boot would otherwise arrive here with one second
        # and report a timeout on a stack that was fine.
        timeout = max(timeout, READY_TIMEOUT)
        deadline = time.monotonic() + timeout
        wait_ready(self.args, ISTOTA_SERVICE, timeout=timeout)
        if self.profile.shape != "full":
            return

        while time.monotonic() < deadline:
            if self.exec(["sh", "-c", _SCHEDULER_RUNNING], timeout=30).returncode == 0:
                return
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(
            f"the istota container reported healthy but had not reached "
            f"`exec uv run istota-scheduler` within {timeout}s — it is still "
            "somewhere in entrypoint.sh\n--- last logs ---\n" + self.logs(60)
        )

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


def _bind_services(stack: "Stack") -> None:
    """Hand the running stack to any service that cannot exist without one.

    Two so far, for the same reason and on both shapes. `NextcloudService`
    attaches to a container the boot just started and needs a way to run `occ`
    inside it. `MailService` needs the host ports compose published, which are
    ephemeral and do not exist until `up` returns.

    Duck-typed rather than a protocol member, because four of the six services
    have nothing to bind and would carry an empty method apiece.
    """
    for service in stack.services.values():
        binder = getattr(service, "bind_stack", None)
        if binder is not None:
            binder(stack)


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


@dataclass(frozen=True)
class FullShape:
    """Everything booting the deployment as shipped needs.

    Where `LeanShape` names a generator to run on the host, this shape names
    none: the container runs `render-config.sh` itself, from the environment
    compose passed it, exactly as in production. That is the whole reason the
    shape exists, and it is why the two-file constraint bites harder here — a
    variable the generator reads but `docker-compose.yml` does not pass through
    is unreachable on this shape.
    """

    compose_file: Path
    """`docker/docker-compose.yml` — the production artifact, unedited."""

    overlay: Path
    """`testbed/compose/testbed.yml`, the harness concessions. Read it."""

    ready_timeout: int = FULL_READY_TIMEOUT

    keep: bool = False
    """`ISTOTA_TESTBED_KEEP`: persist the expensive volumes between sessions.

    See `StackPool._teardown` for what is kept and what is always wiped, and
    for the two corrections the boot path forces on the obvious version of this.
    """

    keep_dir: Path | None = None
    """Where the persisted credentials live when `keep` is set.

    Outside the checkout: these are real generated passwords, and the repo has a
    pre-commit hook that exists because credentials end up in trees.
    """


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
        full: FullShape | None = None,
        platform: str = "",
        project_prefix: str = "istota-testbed-",
    ) -> None:
        self.workdir = workdir
        self.lean = lean
        self.full = full
        self.platform = platform
        self.project_prefix = project_prefix
        self._cached: dict[str, Stack] = {}
        self._private: list[Stack] = []
        self._booted = 0
        self._built = False
        self._credentials: FullCredentials | None = None
        #: `(profile name, service, seconds)` for every readiness wait the pool
        #: has done, so a caller can print where a cold boot went. Open question
        #: 2 asks whether the provisioned volume set needs snapshotting, and it
        #: is meant to be settled against a number rather than an impression.
        self.boot_times: list[tuple[str, str, float]] = []

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
        """Boot the shape the profile declares."""
        if profile.shape == "lean":
            return self._boot_lean(profile)
        if profile.shape == "full":
            return self._boot_full(profile)
        raise StackError(
            f"profile {profile.name!r} declares shape {profile.shape!r}; the "
            f"shapes are {sorted(READY_SERVICES)}"
        )

    def _scratch(self, profile: Profile) -> Path:
        self._booted += 1
        return self.workdir / f"{profile.name}-{self._booted}"

    def _boot_lean(self, profile: Profile) -> Stack:
        """Start the profile's services, render, bring the stack up, wait ready.

        The order is not arrangeable: the services have to be listening before
        the config that names their ports is rendered, and the config has to
        exist before the container that reads it starts.
        """
        scratch = self._scratch(profile)
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
            args = self._compose_args(profile, scratch, config_dir, services)
            render_config(
                self.lean.render_script,
                config_dir,
                services,
                extra=profile.config,
                base_env=self.lean.render_env,
            )
            # Built once per session. A profile naming its own `image` builds
            # nothing regardless — the prebuilt overlay runs a tag someone else
            # made — so it must not be what marks the session as built.
            build = not profile.image and not self._built
            up(args, platform=self.platform, build=build)
            if build:
                self._built = True
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

        stack = Stack(
            profile=profile, args=args, services=services, config_dir=config_dir
        )
        try:
            _bind_services(stack)
        except BaseException:
            down(args, volumes=True)
            for service in services.values():
                try:
                    service.close()
                except Exception:  # pragma: no cover - cleanup is best effort
                    logger.debug("closing a service during a failed boot raised")
            raise
        return stack

    def _boot_full(self, profile: Profile) -> Stack:
        """Bring up the deployment as shipped and wait for it to provision itself.

        Different from the lean boot in one structural way and several
        consequential ones. Structurally: nothing is rendered here. The container
        runs `render-config.sh` itself, from the environment compose passed it,
        which is what makes this shape a witness for `entrypoint.sh` and
        `provision-nc.sh` at all. So the services' `config_env()` goes into the
        compose env-file rather than into a render environment, and the
        constraint that a service may only be wired in through a variable the
        shipped generator reads gains a second half: `docker-compose.yml` has to
        pass it through too.

        Consequentially: the credentials are generated per session, the host
        port is ephemeral because `docker-compose.yml` binds a fixed one on
        nginx, and every module is switched off except the ones the profile
        names. All three are `full_env`'s.

        `--build` is unconditional here rather than once-per-session. The lean
        shape shares one tag across every stack, so a second `up --build` would
        move it under a running container; the full shape's `build:` blocks name
        no `image:`, so compose tags them `<project>-<service>` and each stack
        builds its own. Compose's layer cache makes the second one cheap.
        """
        if self.full is None:
            raise StackError(
                f"profile {profile.name!r} declares the full shape, but this "
                "pool was constructed with no `full=FullShape(...)`"
            )

        scratch = self._scratch(profile)
        scratch.mkdir(parents=True, exist_ok=True)

        services: dict[str, Service] = {}
        args: list[str] = []
        environment: dict[str, str] = {}
        credentials = self._full_credentials()
        # Checked twice, and the first one is before anything is constructed:
        # `services.build` opens a listening socket on every interface and the
        # boot then builds an image, so a refusal that waited for the full map
        # would pay for both before saying no. This pass sees the identity, the
        # credentials, the port and the profile's own config; the pass after
        # `full_env` adds whatever the services contributed.
        self._refuse_conflicting_env(full_env({}, credentials, extra=profile.config))

        started = time.monotonic()
        try:
            for name in profile.services:
                services[name] = service_support.build(
                    name,
                    scratch=scratch,
                    host=PUBLIC_BIND,
                    credentials=credentials,
                )
            environment = full_env(services, credentials, extra=profile.config)
            self._refuse_conflicting_env(environment)
            args, env_file = self._compose_args_full(profile, scratch)
            write_env_file(env_file, environment)

            up(args, platform=self.platform, build=True)
            wait_all_ready(
                args,
                READY_SERVICES["full"],
                timeout=self.full.ready_timeout,
                on_ready=lambda service, seconds: self.boot_times.append(
                    (profile.name, service, seconds)
                ),
            )
            stack = Stack(
                profile=profile, args=args, services=services, env=environment
            )
            _bind_services(stack)
            # The health check answers "the tasks table exists", which
            # `entrypoint.sh` satisfies at `istota init` — several steps before
            # it execs the scheduler. A scenario submitting into that gap gets a
            # row nothing dispatches, and the first symptom is a task that timed
            # out `pending`.
            remaining = int(self.full.ready_timeout - (time.monotonic() - started))
            stack.wait_healthy(timeout=max(1, remaining))
        except BaseException:
            if args:
                self._down(args, shape="full")
            for service in services.values():
                try:
                    service.close()
                except Exception:  # pragma: no cover - cleanup is best effort
                    logger.debug("closing a service during a failed boot raised")
            raise

        self.boot_times.append(
            (profile.name, "total", time.monotonic() - started)
        )
        return stack

    @staticmethod
    def _refuse_conflicting_env(environment: dict[str, str]) -> None:
        """Stop the boot when the process environment would win. See
        `conflicting_process_env` for what each conflict actually costs."""
        conflicts = conflicting_process_env(environment)
        if conflicts:
            raise StackError(
                "these variables are set in this process's environment and would "
                "outrank the compose env-file, so the stack would not boot the "
                f"configuration this profile describes: {sorted(conflicts)}. "
                "Unset them (or remove them from the repo-root .env, which "
                "tests/conftest.py loads into os.environ) and run again."
            )

    def _full_credentials(self) -> FullCredentials:
        """Passwords and a host port for one full stack.

        **Fresh per boot when `KEEP` is off**, and that is not merely tidiness.
        `docker-compose.yml:457` publishes `${NC_PORT:-8080}:80` on nginx, a
        fixed host port, and the pool can legitimately hold two full stacks at
        once — a `fresh=True` one alongside a cached one, or two `fresh=True`
        ones from different modules. Memoizing one port for the session makes
        the second `up` fail on a bind, and the error names a port rather than
        the reason. Each stack is its own Nextcloud with its own users, so there
        is nothing for two of them to share.

        Under `ISTOTA_TESTBED_KEEP` they are memoized *and* persisted, because
        then there is: the Nextcloud users on the kept volumes already have
        these passwords and its OAuth2 client already names this port.
        Regenerating either gives a stack that boots and then authenticates
        against nothing.
        """
        keep_file = self._keep_file()
        if keep_file is None:
            return generate_credentials(reserve_port())

        if self._credentials is not None:
            return self._credentials
        if keep_file.exists():
            self._credentials = FullCredentials(**json.loads(keep_file.read_text()))
            return self._credentials

        self._credentials = generate_credentials(reserve_port())
        keep_file.parent.mkdir(parents=True, exist_ok=True)
        _write_private(keep_file, json.dumps(self._credentials.__dict__))
        return self._credentials

    def _keep_file(self) -> Path | None:
        if self.full is None or not self.full.keep or self.full.keep_dir is None:
            return None
        return self.full.keep_dir / "credentials.json"

    def _compose_args_full(
        self, profile: Profile, scratch: Path
    ) -> tuple[list[str], Path]:
        """The compose prefix for the full shape, and the env-file it rides in.

        The overlay goes on last so its concessions win, and the profile's own
        overlays go between — a `mail` overlay adds a service, and adding one
        must not be able to undo the seccomp grant.

        The project name is *stable* under `KEEP` and random otherwise. That is
        not cosmetic: compose scopes a named volume to the project, so a fresh
        uuid every session would leave the kept volumes attached to a project
        nothing ever looks at again — `KEEP` would silently keep a growing pile
        of orphans and cache nothing.
        """
        assert self.full is not None
        if self.full.keep:
            digest = hashlib.sha256(
                str(self.full.compose_file.resolve()).encode()
            ).hexdigest()[:8]
            project = f"{self.project_prefix.rstrip('-')}{KEEP_PROJECT_MARKER}{digest}"
        else:
            project = f"{self.project_prefix}full-{uuid.uuid4().hex[:8]}"
        env_file = scratch / "compose.env"
        overlays = [*profile.compose_overlays, self.full.overlay]
        return (
            compose_args(
                self.full.compose_file,
                project=project,
                env_file=env_file,
                overlays=overlays,
            ),
            env_file,
        )

    def _compose_args(
        self,
        profile: Profile,
        scratch: Path,
        config_dir: Path,
        services: dict[str, Service] | None = None,
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
        # Whatever the profile's own overlays need to resolve their binds. Empty
        # on every profile that names no overlay, which is most of them.
        for variable, value in compose_env(services or {}).items():
            lines.append(f"{variable}={value}")
        env_file.write_text("\n".join(lines) + "\n")
        return compose_args(
            self.lean.compose_file,
            project=project,
            env_file=env_file,
            overlays=overlays,
        )

    #: Under `KEEP`, the volumes that are wiped anyway, by unqualified name.
    #:
    #: `istota_data` holds `/data/config/config.toml`, and `entrypoint.sh:344`
    #: gates the *entire* render on `[ ! -f "$CONFIG_FILE" ]` — so a kept
    #: `istota_data` means session 2's env-file is never read and the daemon
    #: boots pointing at session 1's now-dead scripted-endpoint port. It also
    #: holds `.api-provisioned`, whose absence is what makes the entrypoint
    #: re-run room provisioning through the find-by-name recovery path.
    #: `redis_data` is a cache with nothing in it worth a second of boot time.
    KEEP_WIPES = ("istota_data", "redis_data")

    def _down(self, args: list[str], *, shape: str) -> None:
        """Tear a stack down, keeping the expensive volumes if asked to.

        `shape` rather than `self.full.keep` alone: one pool serves both shapes,
        and a lean stack in a session that also ran a kept full one must still
        lose its volumes — its named volume is the framework DB every assertion
        is read out of.

        **`shared_files` is kept, and the spec that asked for it to be wiped was
        wrong.** The reasoning there was that wiping it forces the daemon to
        re-provision against the session's own env-file. What it actually does
        is remove `/mnt/shared/.istota-provisioned`, which
        `provision-nc.sh` will never rewrite: it is mounted as a
        `post-installation` hook, and the Nextcloud image runs
        `run_path post-installation` only inside the branch where the installed
        version is `0.0.0.0` (verified by reading `/entrypoint.sh` in
        `nextcloud:30-apache`, not by reasoning). So on a kept volume set the
        hook does not run, the flag never appears, `entrypoint.sh` waits its
        600 seconds and exits 1, and `restart: unless-stopped` does that
        forever. Wiping `istota_data` alone gets the re-render and the room
        re-provisioning that wiping `shared_files` was supposed to buy.

        The second correction is in `_compose_args_full`: the port has to be
        pinned across kept sessions, because `provision-nc.sh` bakes the OAuth2
        redirect URI at first install and — same hook, same reason — does not
        revisit it.

        The consequence for scenarios is that `KEEP` and the provisioning suite
        are mutually exclusive: a suite asserting on first-install state cannot
        run against a volume set whose first install was a previous session's.
        `tests/full/conftest.py` refuses that combination by name rather than
        letting it fail as four unrelated-looking assertions.
        """
        keep = shape == "full" and self.full is not None and self.full.keep
        if not keep:
            # Volumes too: the DB is a named volume, and leaving it behind would
            # make the next session's assertions depend on this one's rows.
            down(args, volumes=True)
            return

        down(args, volumes=False)
        project = _project_of(args)
        for volume in self.KEEP_WIPES:
            name = f"{project}_{volume}"
            try:
                result = subprocess.run(
                    ["docker", "volume", "rm", "-f", name],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (subprocess.SubprocessError, OSError) as exc:  # pragma: no cover
                logger.warning("could not remove %s: %s", name, exc)
                continue
            if result.returncode != 0:
                # Said out loud, for the reason `down` says its own non-zero
                # exit out loud: `-f` already tolerates a missing volume, so a
                # failure here means one that is still *in use* — a container
                # from a crashed run, another project holding it. A silently
                # surviving `istota_data` means the next session's env-file is
                # never read (the render is gated on config.toml existing) and
                # its rooms are never re-provisioned, which is a wrong-answer
                # boot a minute later with nothing in the log.
                logger.warning(
                    "docker volume rm %s exited %s — the next kept session will "
                    "reuse it and may boot a stale configuration.\n%s",
                    name,
                    result.returncode,
                    result.stderr or result.stdout,
                )

    def _teardown(self, stack: Stack) -> None:
        self._down(stack.args, shape=stack.profile.shape)
        for service in stack.services.values():
            try:
                service.close()
            except Exception:  # pragma: no cover - teardown is best effort
                logger.debug("closing a service during teardown raised")
