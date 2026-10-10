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
from dataclasses import dataclass
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

#: The setpriv wrapper the istota image ships for every exec path.
DROP = "istota-drop"

#: Where the config the testbed writes is bound, which is where `istota setup`
#: writes it.
CONTAINER_CONFIG = "/data/config/config.toml"

#: This package's own compose files: `testbed.yml`, the Nextcloud fixture and
#: its provisioning script. Handed to compose as an absolute path, because
#: compose resolves a relative one against the first `-f` file's directory.
COMPOSE_DIR = Path(__file__).resolve().parent / "compose"
TESTBED_OVERLAY = COMPOSE_DIR / "testbed.yml"
NEXTCLOUD_OVERLAY = COMPOSE_DIR / "nextcloud.yml"

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
#: volume set spends most of it on the Nextcloud fixture installing itself and
#: on the two app-store downloads `provision-nc.sh` triggers, which `istota`
#: waits on. A timeout shorter than that would report the harness's impatience
#: as a deployment failure.
FULL_READY_TIMEOUT = 1500

#: The compose services readiness means, per shape.
#:
#: `web` and `nginx` are deliberately absent from the full shape's tuple. `web`
#: starts only once `istota` is healthy, and `nginx` starts before `web` is
#: serving (`depends_on` there is `service_started`, not `service_healthy`) so
#: its startup resolution of the `web` upstream can fail and take it round
#: again. Waiting on `istota` is waiting on the part that decides.
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
#: and `StackPool._compose_args` is what puts it there.
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
# The root phase execs the drop, which execs `entrypoint.sh`, which ends in
# `exec istota-scheduler`, so pid 1 *is* the answer, and a `docker compose
# exec` shell is never pid 1.
#
# The first version of this globbed `/proc/[0-9]*/cmdline`, which is unsound in
# a way that is invisible from reading it: the probe runs as
# `sh -c '<this script>'`, so the probing shell's own command line contains the
# literal `istota-scheduler` and matches itself. It returned 0 on the first poll
# of any container — measured in a bare `alpine`, which has no istota in it at
# all — so `wait_healthy` waited for nothing and the idempotence assertions read
# pre-restart state.
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
    compose_profiles: tuple[str, ...] | list[str] = (),
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

    `compose_profiles` rides here for the same reason, and the failure it avoids
    is worse than a confusing `ps`: a service started under a profile that
    `down` was not told about is not torn down, so an interrupted session leaves
    a container holding a published port and the next one collides with it.
    """
    args = ["docker", "compose", "-f", str(compose_file)]
    for overlay in overlays or []:
        args += ["-f", str(overlay)]
    for compose_profile in compose_profiles:
        args += ["--profile", compose_profile]
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
    skip: tuple[str, ...] = (),
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
    the moving-tag failure the per-checkout tag guards against across
    worktrees, and there is no reason to reintroduce it within one session.
    """
    overrides = dict(env or {})
    if platform:
        overrides.setdefault("DOCKER_DEFAULT_PLATFORM", platform)
    command = args + ["up", "--detach"]
    if build:
        command.insert(len(args) + 1, "--build")
    # `--scale <service>=0` rather than naming the services to start, so every
    # profile service and overlay addition still starts as declared.
    for service in skip:
        command += ["--scale", f"{service}=0"]
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
            health = record.get("Health", "")
            if not health:
                # Compose leaves `Health` empty on a container whose first
                # check has not run, and says so only in `Status`. Read as "no
                # healthcheck", that let `wait_ready` return on a container
                # about to exit.
                match = re.search(
                    r"\((?:health: )?(starting|healthy|unhealthy)\)", record.get("Status", "")
                )
                health = match.group(1) if match else ""
            return record.get("State", ""), health
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


# -- writing the stack's config ----------------------------------------------
#
# config.toml is an input to the container: `istota setup` writes it once and
# nothing renders it from the environment. The testbed writes it too, for every
# profile, from layers merged in order: the base (what `istota setup` writes
# for the stack's one user), the concessions this tier needs, each service's
# `config()`, then the profile's own `config`.

#: The container's layout and what `istota setup` writes for a stack's one
#: user, local storage, no Nextcloud. `tests/test_testbed_config.py` holds
#: this equal to `setup_wizard.render_container_config` for the same answers,
#: since this package may not import istota. `session_secret_key` is filled in
#: per write.
LEAN_BASE_CONFIG: dict = {
    "bot_name": "Istota",
    "db_path": "/data/db/istota.db",
    "workspace_path": "/data/workspace",
    "temp_dir": "/data/tmp",
    "security": {"sandbox_enabled": True, "skill_proxy_enabled": True},
    "brain": {"kind": "claude_code"},
    "talk": {"enabled": False},
    "email": {"enabled": False},
    "location": {"enabled": False},
    "web": {
        "enabled": True,
        "port": 8766,
        "auth": ["email"],
        "trusted_proxy_hops": 1,
        "token_storage": "encrypted",
    },
    "site": {"hostname": "localhost"},
    "users": {"testuser": {"display_name": "testuser", "timezone": "UTC"}},
}

#: Where every stack this tier boots departs from `istota setup`'s output, each
#: for a reason of the harness rather than of the product. The network sandbox
#: is *not* among them: a task reaches a host-side stub only through the
#: CONNECT proxy, and only where the stack's config allowlists it (the forge
#: stub's URL is allowlisted by the developer skill, as any forge's is).
#:
#: - `memory_search`: off, as the old lean render had it, so the assembled
#:   prompt does not depend on indexing no scenario asserts on.
#: - `web.auth`: `nextcloud`, the dataclass default the old lean render wrote.
#:   No lean profile runs the web app, and doctor's email-auth checks then skip.
#:   The full profile sets its own.
CONCESSIONS: dict = {
    "memory_search": {"enabled": False},
    "web": {"auth": ["nextcloud"]},
}

#: The credential files `docker-compose.yml` declares, which compose refuses to
#: start without. The same list as `setup_wizard.SECRET_NAMES`, restated
#: because this package does not import istota; `tests/test_testbed_config.py`
#: holds the two equal.
SECRET_NAMES: tuple[str, ...] = (
    "anthropic_api_key",
    "claude_code_oauth_token",
    "istota_brain_native_api_key",
    "istota_nextcloud_app_password",
    "istota_web_oauth2_client_secret",
    "istota_web_session_secret_key",
    "istota_email_imap_password",
    "istota_caldav_password",
    "istota_developer_gitlab_token",
    "istota_developer_github_token",
)

#: What the scripted endpoint is sent as a key. Nothing in this tier can use a
#: real one: the endpoint ignores the Authorization header entirely.
SCRIPTED_ENDPOINT_KEY = "unused-by-the-scripted-endpoint"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_string(value: str) -> str:
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif (char < " " and char != "\t") or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise StackError(f"the testbed TOML writer has no form for {type(value).__name__}")


def toml_dumps(document: dict) -> str:
    """A TOML document from nested dicts of strings, numbers, bools and lists.

    Small on purpose: the testbed's dependency set is the standard library plus
    `cryptography`, and `tomllib` reads but does not write. Arrays of tables are
    not supported, since no config the tier writes has one.
    """
    lines: list[str] = []

    def emit(table: dict, path: tuple[str, ...]) -> None:
        scalars = [(key, value) for key, value in table.items() if not isinstance(value, dict)]
        tables = [(key, value) for key, value in table.items() if isinstance(value, dict)]
        if path and (scalars or not tables):
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        if path and (scalars or not tables):
            lines.append("")
        elif scalars:
            lines.append("")
        for key, value in tables:
            emit(value, (*path, key))

    emit(document, ())
    return "\n".join(lines).rstrip("\n") + "\n"


def merge_config(
    target: dict,
    fragment: dict,
    *,
    owner: str,
    owners: dict[tuple[str, ...], str] | None = None,
    path: tuple[str, ...] = (),
) -> dict:
    """Merge `fragment` into `target`, refusing a leaf two services both set.

    `owners` maps each leaf a *service* has set to that service's name. Two
    services claiming one key is refused rather than resolved: silent last-wins
    would boot a stack whose config names the wrong service's port, and dict
    order would decide which. `owners=None` is a layer that may override.
    """
    for key, value in fragment.items():
        here = (*path, key)
        if isinstance(value, dict):
            existing = target.get(key)
            if existing is not None and not isinstance(existing, dict):
                raise StackError(f"{owner} sets {'.'.join(here)} as a table over a value")
            merge_config(
                target.setdefault(key, {}), value, owner=owner, owners=owners, path=here,
            )
            continue
        if owners is not None:
            claimant = owners.get(here)
            if claimant is not None and claimant != owner:
                raise StackError(
                    f"{owner} and {claimant} both set {'.'.join(here)}; one of them "
                    "would silently win and the stack would boot pointing at the other"
                )
            owners[here] = owner
        target[key] = value
    return target


def _copy(document: dict) -> dict:
    return json.loads(json.dumps(document))


def assemble_config(
    services: dict[str, Service], *, base: dict, extra: dict | None = None,
) -> dict:
    """The base, the concessions, each service's `config()`, the profile's own."""
    document = _copy(base)
    merge_config(document, CONCESSIONS, owner="the testbed")
    owners: dict[tuple[str, ...], str] = {}
    for name, service in services.items():
        merge_config(document, service.config(), owner=name, owners=owners)
    merge_config(document, extra or {}, owner="the profile")
    return document


def lean_config(profile: Profile, services: dict[str, Service]) -> dict:
    """The document a lean stack boots from, with a fresh session key."""
    document = assemble_config(services, base=LEAN_BASE_CONFIG, extra=profile.config)
    document["web"]["session_secret_key"] = secrets.token_hex(32)
    return document


def lean_secrets() -> dict[str, str]:
    """The lean stack's secret files: the scripted endpoint's placeholder key.

    Through a file like any credential, so the daemon reads it the way an
    operator's does (`istota-secrets`), and never from a developer's exported
    `ISTOTA_BRAIN_NATIVE_API_KEY`.
    """
    return {"istota_brain_native_api_key": SCRIPTED_ENDPOINT_KEY}


def write_config(
    directory: Path, document: dict, *, admins: tuple[str, ...] = ("testuser",),
) -> Path:
    """Write `config.toml` and the admins file into `directory`, as setup does.

    0644 rather than setup's 0600: the container reads them as uid 10001
    through a bind mount, and on a Linux host the files keep the harness's
    owner. The directory is the harness's own scratch.
    """
    directory.mkdir(parents=True, exist_ok=True)
    config_file = directory / "config.toml"
    config_file.write_text(toml_dumps(document))
    config_file.chmod(0o644)
    (directory / "admins").write_text("".join(f"{user}\n" for user in admins))
    return config_file


def write_secrets(directory: Path, values: dict[str, str]) -> Path:
    """One file per declared secret, as `istota setup --vm-dir` writes them.

    Every declared name gets a file, empty when unused, because compose refuses
    to start a service whose secret file is missing. 0444 rather than the
    shipped 0400, for the bind-mount reason `write_config` gives.
    """
    unknown = sorted(set(values) - set(SECRET_NAMES))
    if unknown:
        raise StackError(f"no compose secret is declared for {unknown}")
    directory.mkdir(parents=True, exist_ok=True)
    for name in SECRET_NAMES:
        path = directory / name
        if path.exists():
            path.chmod(0o644)
        path.write_text(values.get(name, ""))
        path.chmod(0o444)
    return directory


# -- the stack's environment -------------------------------------------------

#: The interpolation variables every stack's env-file owns, whatever the shape.
#:
#: Reserved against a service's `compose_env()`, since both are written into one
#: env-file where a later assignment wins: a service naming one would silently
#: redirect the config directory, run somebody else's image, or publish nginx on
#: a port another stack holds.
STACK_ENV_KEYS = (
    "ISTOTA_TEST_IMAGE",
    "ISTOTA_TEST_CONFIG_DIR",
    "ISTOTA_SECRETS_DIR",
    "ISTOTA_TESTBED_COMPOSE_DIR",
    "NGINX_PUBLISH",
    "NGINX_PUBLISH_TLS",
    "ISTOTA_TALK_SIGNALING_PORT",
    "BROWSER_API_PORT",
    "BROWSER_VNC_PORT",
    "ISTOTA_TEST_BROWSER_IMAGE",
)


def stack_env(
    *,
    image: str,
    config_dir: Path,
    secrets_dir: Path,
    nginx_port: int = 0,
    signaling_port: int = 0,
    browser_image: str = "",
) -> dict[str, str]:
    """What every stack's env-file carries: where its inputs are, which image
    runs, and host ports that cannot collide with another stack's.

    The shipped file publishes nginx on `127.0.0.1:8080` and the browser and
    signaling servers on fixed loopback ports by default, which a developer's
    own stack or a second worktree also holds. So every published port is
    ephemeral (`127.0.0.1::<port>`, or port 0) unless the shape needs to know
    it in advance: the full shape's nginx port is reserved, because the
    fixture's OAuth2 redirect URI is baked with it at first install.
    """
    return {
        "ISTOTA_TEST_IMAGE": image,
        "ISTOTA_TEST_CONFIG_DIR": str(config_dir),
        "ISTOTA_SECRETS_DIR": str(secrets_dir),
        "ISTOTA_TESTBED_COMPOSE_DIR": str(COMPOSE_DIR),
        "NGINX_PUBLISH": f"127.0.0.1:{nginx_port}:80" if nginx_port else "127.0.0.1::80",
        "NGINX_PUBLISH_TLS": "127.0.0.1::443",
        "ISTOTA_TALK_SIGNALING_PORT": str(signaling_port),
        "BROWSER_API_PORT": "0",
        "BROWSER_VNC_PORT": "0",
        "ISTOTA_TEST_BROWSER_IMAGE": browser_image or "istota-test/browser:unbuilt",
    }


def lean_env(
    services: dict[str, Service],
    *,
    image: str,
    config_dir: Path,
    secrets_dir: Path,
    browser_image: str = "",
) -> dict[str, str]:
    """The lean stack's env-file: the shared keys and each service's own.

    `ISTOTA_TALK_SIGNALING_BACKEND_URLS` names a Nextcloud the lean shape does
    not have. The signaling server only ever matches it against the backend a
    client names, and the round trip that would resolve it is on auth paths
    this shape cannot reach; it has to be non-empty for the server to start.
    """
    environment = stack_env(
        image=image, config_dir=config_dir, secrets_dir=secrets_dir, browser_image=browser_image,
    )
    environment["ISTOTA_TALK_SIGNALING_BACKEND_URLS"] = "http://nextcloud"
    environment.update(compose_env(services, reserved=set(environment)))
    return environment


#: Services the spec's later stages add, named here so `FULL_MODULE_SWITCHES`
#: can point at them before they exist.
#:
#: Without this the guard on that map would have to accept any string, which is
#: the same as not checking for a typo at all. It is also a ratchet: a unit test
#: asserts this set and `REGISTRY` stay disjoint, so registering a name fails
#: until the name is removed from here. Empty now; kept for the next one.
PLANNED_SERVICES: frozenset[str] = frozenset()

#: Each subsystem switch the full config writes, mapped to the service whose
#: presence in a profile turns it on. Empty means nothing in this tier does.
#:
#: This is what makes `Profile` mean anything on the full shape: without it a
#: `full` profile declaring `services=("model", "nextcloud")` would boot a daemon
#: polling every subsystem the dataclass defaults leave on (Talk, both sleep
#: cycles), which is what the profile mechanism exists to prevent. Email and the
#: developer skill are switched on by their own services' `config()`, so they
#: are not here. `memory_search` is the testbed's concession, off on both shapes.
FULL_MODULE_SWITCHES: dict[tuple[str, ...], str] = {
    ("talk", "enabled"): "nextcloud",
    ("talk", "signaling", "enabled"): "signaling",
    ("browser", "enabled"): "",
    ("location", "enabled"): "",
    ("sleep_cycle", "enabled"): "",
    ("channel_sleep_cycle", "enabled"): "",
}

#: Identity the full stack requires by name. `nextcloud.yml` preflights
#: `USER_NAME` with `${USER_NAME:?}` for the fixture's provisioning, and the
#: config the testbed writes names the same user and bot.
FULL_IDENTITY: dict[str, str] = {
    "USER_NAME": "testuser",
    "BOT_USER": "istota",
    "ISTOTA_BOT_NAME": "Istota",
}

#: The four `${…:?}` credentials `nextcloud.yml` refuses to start without.
CREDENTIAL_KEYS = (
    "POSTGRES_PASSWORD",
    "ADMIN_PASSWORD",
    "BOT_PASSWORD",
    "USER_PASSWORD",
)

#: The fixture's name for the files_external mount it gives the bot over the
#: shared volume (`nextcloud.yml`, `provision-nc.sh`). The daemon prefixes every
#: DAV and OCS path with it.
SHARED_MOUNT_NAME = "Shared Files"


@dataclass(frozen=True)
class FullCredentials:
    """This session's generated passwords and OAuth2 client, plus their ports.

    Generated rather than read from `docker/.env`, which on a developer machine
    is a gitignored file holding real ones. Nothing in this tier reads it.

    `nc_port` travels with them because it is credential-shaped state in one
    specific sense: `provision-nc.sh` bakes `ISTOTA_WEB_CALLBACK_URL`, which is
    derived from the port, into the `oauth2_clients` row at first install and
    never revisits it. A kept volume set and a different port is a stale
    registration, so the port is persisted alongside the passwords.

    `__repr__` is redacted, and for the same reason `ServiceCall`'s is: pytest's
    assertion rewriting renders the repr of whatever a failing comparison
    touched, and this object reaches a `Stack`.
    """

    postgres_password: str
    admin_password: str
    bot_password: str
    user_password: str
    nc_port: int

    signaling_port: int = 0
    """Where the signaling server is published, reserved beside `nc_port`.

    Defaulted rather than required, because it arrived after the keep file did
    and a persisted set from an earlier session has no such key —
    `_full_credentials` reserves one in that case rather than accepting the
    default, since `0` hands the choice back to Docker and that is the
    collision `reserve_ports` exists to prevent.

    **What KEEP does not carry is the signaling *secret*.** `provision-nc.sh`
    registers it with Talk at first install and never revisits it, while
    `signaling.serve()` generates a fresh one per session. `tests/full/` refuses
    to run under KEEP for its own reasons, which is what masks this today.
    """

    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    """The web login's OAuth2 client. `provision-nc.sh` registers exactly this
    pair at first install, and the config the testbed writes names it, which is
    the route an operator takes with `istota setup`. Persisted under KEEP for
    the reason `nc_port` is; a keep file from before it gets a new pair, which a
    kept Nextcloud does not hold, so `tests/full/` refusing KEEP covers it."""

    def as_env(self) -> dict[str, str]:
        return {
            "POSTGRES_PASSWORD": self.postgres_password,
            "ADMIN_PASSWORD": self.admin_password,
            "BOT_PASSWORD": self.bot_password,
            "USER_PASSWORD": self.user_password,
            "ISTOTA_WEB_OAUTH2_CLIENT_ID": self.oauth_client_id,
            "ISTOTA_WEB_OAUTH2_CLIENT_SECRET": self.oauth_client_secret,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return (
            f"FullCredentials(<6 redacted>, nc_port={self.nc_port}, "
            f"signaling_port={self.signaling_port})"
        )


def reserve_ports(count: int = 1) -> tuple[int, ...]:
    """Bind `count` ephemeral ports at once, read them back, release them all.

    `docker-compose.yml` binds `${NC_PORT:-8080}:80` on nginx and
    `${ISTOTA_TALK_SIGNALING_PORT:-8081}:8080` on the signaling server — *fixed*
    host ports, unlike the lean stack which publishes nothing — so a developer's
    own demo stack or a second worktree collides and `up` fails.

    **All the sockets are held until every port has been read**, which is what
    makes two reservations distinct rather than merely probable. One at a time
    the kernel is free to hand the second call the port the first just released.
    Letting the signaling service publish on `:0` was measured colliding with
    the reserved `NC_PORT` on the first full boot that ran both.

    Racy against the rest of the machine by construction, and knowingly so: the
    kernel can hand one of these to something else between the release here and
    compose's bind. A lost race fails `up` loudly with "address already in use".
    """
    probes = []
    try:
        for _ in range(count):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            probes.append(probe)
        return tuple(probe.getsockname()[1] for probe in probes)
    finally:
        for probe in probes:
            probe.close()


def reserve_port() -> int:
    """One port, for a caller that needs no second one."""
    return reserve_ports(1)[0]


def _oauth_token(length: int = 64) -> str:
    """The alphabet Nextcloud's own admin UI mints OAuth2 clients from."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_credentials(nc_port: int, signaling_port: int = 0) -> FullCredentials:
    """Fresh passwords and an OAuth2 client for one session.

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
        signaling_port=signaling_port,
        oauth_client_id=_oauth_token(),
        oauth_client_secret=_oauth_token(),
    )


def full_env(
    services: dict[str, Service],
    credentials: FullCredentials,
    *,
    config_dir: Path,
    secrets_dir: Path,
    image: str = "",
) -> dict[str, str]:
    """Everything the full shape's compose env-file carries.

    None of it is istota configuration: that is `config.toml`, which
    `full_config` builds and `write_config` writes into `config_dir`. What is
    left configures the *stack*:

    1. **What every stack carries** (`stack_env`), with nginx on the reserved
       `NC_PORT` rather than an ephemeral one.
    2. **Identity and credentials** for the Nextcloud fixture's provisioning,
       which `nextcloud.yml` preflights with `${…:?}`, plus the OAuth2 pair
       `provision-nc.sh` registers as given.
    3. **`NC_PORT` and an explicit `ISTOTA_WEB_CALLBACK_URL`**, which
       `provision-nc.sh` bakes irreversibly into the `oauth2_clients` row at
       first install.
    4. Each service's `compose_env()`.

    A service claiming a key this function owns is refused: one that quietly
    renamed `USER_NAME` would leave `NextcloudService` authenticating as a user
    the stack never created, and one that moved `NC_PORT` would leave the OAuth2
    redirect URI baked at a port nothing publishes.
    """
    # The signaling port beside `NC_PORT` and reserved for the same reason:
    # both are fixed host ports on one machine and something has to hold them
    # apart.
    environment = stack_env(
        image=image, config_dir=config_dir, secrets_dir=secrets_dir,
        nginx_port=credentials.nc_port, signaling_port=credentials.signaling_port,
    )
    environment.update(FULL_IDENTITY)
    environment.update(credentials.as_env())
    environment["NC_PORT"] = str(credentials.nc_port)
    environment["ISTOTA_WEB_CALLBACK_URL"] = (
        f"http://localhost:{credentials.nc_port}/istota/callback"
    )
    # The two URLs the signaling server must match a backend request against:
    # the one Talk stamps (the browser's) and the one the daemon names.
    environment["ISTOTA_TALK_SIGNALING_BACKEND_URLS"] = (
        f"http://nextcloud,http://localhost:{credentials.nc_port}"
    )

    reserved = set(environment)
    environment.update(compose_env(services, reserved=reserved))
    return environment


def full_config(
    services: dict[str, Service], credentials: FullCredentials, profile: Profile,
) -> tuple[dict, dict[str, str]]:
    """The full stack's `config.toml` and its secret files.

    Full Nextcloud integration against the fixture, laid out the way an install
    moved off the old bundled Nextcloud runs it: the workspace is the shared
    volume, which is the bot's `Shared Files` mount, so `dav_prefix` names it
    and the boot-time OCS share-back is off (the user already has the directory
    as a mount of their own). The workspace and mount keys are what `istota
    setup` writes for full integration. Credentials go to the secret files, as
    `istota setup --vm-dir` writes them, never into the config.
    """
    public = f"http://localhost:{credentials.nc_port}"
    base = _copy(LEAN_BASE_CONFIG)
    base["workspace_path"] = "/mnt/shared"
    base["nextcloud_mount_path"] = "/mnt/shared"
    base["nextcloud"] = {
        "url": "http://nextcloud",
        "username": FULL_IDENTITY["BOT_USER"],
        "dav_prefix": SHARED_MOUNT_NAME,
        "auto_share_bot_dir": False,
    }
    base["talk"] = {"enabled": True, "bot_username": FULL_IDENTITY["BOT_USER"]}
    base["web"].update({
        "oauth2_provider": public,
        "oauth2_client_id": credentials.oauth_client_id,
        "oauth2_token_endpoint": "http://nextcloud/index.php/apps/oauth2/api/v1/token",
        "oauth2_userinfo_endpoint": "http://nextcloud/ocs/v2.php/cloud/user?format=json",
        "oauth2_redirect_uri": f"{public}/istota/callback",
    })
    base["site"] = {"hostname": f"localhost:{credentials.nc_port}"}
    document = assemble_config(services, base=base)
    for path, owner in FULL_MODULE_SWITCHES.items():
        table = document
        for part in path[:-1]:
            table = table.setdefault(part, {})
        table[path[-1]] = bool(owner and owner in services)
    merge_config(document, profile.config, owner="the profile")
    secret_values = {
        "istota_brain_native_api_key": SCRIPTED_ENDPOINT_KEY,
        "istota_nextcloud_app_password": credentials.bot_password,
        "istota_web_oauth2_client_secret": credentials.oauth_client_secret,
        "istota_web_session_secret_key": secrets.token_hex(32),
    }
    return document, secret_values


def compose_env(
    services: dict[str, Service],
    *,
    claimed: dict[str, str] | None = None,
    reserved: set[str] | None = None,
) -> dict[str, str]:
    """Interpolation variables the profile's overlays need, from the services.

    Distinct from `config()`, which points the *daemon* at a service through
    `config.toml`. These configure the compose *stack* instead: host paths and
    image tags an overlay binds, and the container secrets and ports a shipped
    service is declared from (`signaling.compose_env()` names
    `ISTOTA_TALK_SIGNALING_SECRET` and its neighbours, which configure the
    signaling container and `provision-nc.sh`, not the daemon). Nothing istota
    reads as configuration goes through here.

    Compose resolves a relative bind against the first `-f` file's directory,
    which is `docker/` rather than this package, so an overlay living here can
    only name an absolute path handed to it. That is how both shapes receive
    the config directory.

    Optional on the protocol, read by `getattr`: most services need no overlay
    and would otherwise carry an empty method apiece. Two services claiming one
    variable, or one claiming a variable the stack owns, is refused.
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
#: too, through `compose_env()`, and the OAuth2 client secret rides here as
#: well. A list a future service has to remember to extend is a list that will
#: not be extended.
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
    `tests/conftest.py::_load_dotenv` injects a repo-root `.env` into
    `os.environ` before any of this runs, so a key landing there is
    indistinguishable from an exported one — and is caught the same way.

    Since ISSUE-301 the suite also *scrubs* `os.environ` before every test, and
    the boot happens inside the function-scoped `stack` fixture, i.e. after the
    scrub. So every `ISTOTA_*` key and everything credential-shaped —
    `ISTOTA_BRAIN_KIND` and `ADMIN_PASSWORD` among them — is already gone by the
    time this runs, and the exported value genuinely no longer reaches compose.
    What is left for this guard is the rest, `USER_NAME` and the port variables
    included, which is still worth refusing over. Read a failure here as "your
    shell exports this and the scrub does not cover it", not as a `.env` to go
    and clean up.
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
        #: The unredacted credentials, kept **only** to substitute them back out
        #: of a failure report (`_scrub`). Nothing reads this to assert with, and
        #: `self.env` above stays the public view — a scenario that wants a
        #: credential should be given one by its service, which is what the
        #: stub-credential rule already requires.
        #:
        #: Longest first so a value containing another is replaced whole rather
        #: than left with the shorter one already substituted inside it.
        self._secrets: list[tuple[str, str]] = sorted(
            (
                (key, value)
                for key, value in (env or {}).items()
                if is_credential_key(key) and value
            ),
            key=lambda pair: len(pair[1]),
            reverse=True,
        )
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
        # The daemon's container drops to uid 10001 in its root phase, and a
        # `docker compose exec` does not inherit that: it would run as uid 0
        # with the cap_add set and leave root-owned files under /data for the
        # daemon to trip over. So every exec into it goes through the same drop
        # the shipped healthcheck uses.
        if service == ISTOTA_SERVICE and not user:
            argv = [DROP, *argv]
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
        interfaces. **The IPv4 line is preferred rather than the first**, and
        that is not cosmetic: Docker binds v4 and v6 separately and does not
        always give them the same host port, while every caller pairs the answer
        with a hardcoded loopback address. Taking whichever line came first
        would then hand back a port nothing is listening on at `127.0.0.1`.
        """
        result = subprocess.run(
            self.args + ["port", service, str(container_port)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = (result.stdout or "").strip().splitlines()
        if result.returncode != 0 or not lines:
            raise StackError(
                f"compose could not say which host port {service}:"
                f"{container_port} is published on (exit {result.returncode})\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        # A v6 binding is bracketed (`[::1]:54321`); a v4 one is not.
        chosen = next(
            (line for line in lines if not line.strip().startswith("[")), lines[0]
        )
        _, _, port = chosen.rpartition(":")
        # Zero is refused as well as a non-number: compose has printed
        # `0.0.0.0:0` for a port it did not map, which parses cleanly and then
        # fails several layers later as a refused connection to port 0.
        if not port.isdigit() or int(port) == 0:
            raise StackError(
                f"compose answered {chosen!r} for {service}:{container_port}, "
                "which is not a host port anything is listening on"
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
        drift from the `config()` key that pointed the daemon at it.

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
        decision rather than an oversight. The config writes
        `workspace_path` as that literal on every profile, so memory
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

        So it also waits for the entrypoint to reach its last line — `exec
        istota-scheduler` — by reading pid 1's command line. See
        `_SCHEDULER_RUNNING` for why it is pid 1 rather than a scan, which is
        not a detail: the scan matched the probing shell itself.
        """
        if timeout is None:
            timeout = (
                FULL_READY_TIMEOUT if self.profile.shape == "full" else READY_TIMEOUT
            )
        # A floor rather than only the caller's number. `StackPool._boot_stack`
        # passes the remainder of a budget that `up` has already eaten into, and
        # on the full shape `up` blocks on the fixture's health check, which
        # allows 300s of start period plus twenty 15s retries. A slow but
        # entirely correct cold boot would otherwise arrive here with one second
        # and report a timeout on a stack that was fine.
        timeout = max(timeout, READY_TIMEOUT)
        deadline = time.monotonic() + timeout
        wait_ready(self.args, ISTOTA_SERVICE, timeout=timeout)

        while time.monotonic() < deadline:
            if self.exec(["sh", "-c", _SCHEDULER_RUNNING], timeout=30).returncode == 0:
                return
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(
            f"the istota container reported healthy but had not reached "
            f"`exec istota-scheduler` within {timeout}s — it is still "
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

        **The tool output is in here because leaving it out cost ISSUE-338 an
        hour.** A scripted model ignores what a command printed and answers
        anyway, so a task whose Bash call failed still reaches `completed` — and
        the only surviving assertion is then a service-side one reporting an
        empty list with no cause attached. The sentence naming the cause was
        written to stderr inside the sandbox and reached the conversation, which
        is the one place `diagnostics` did not look.
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
            f"--- tool output ---\n{self.tool_output()}\n"
            f"--- services ---\n{seen}\n"
            f"--- daemon logs ---\n{self.logs(150)}"
        )

    def tool_output(self, *, per_call: int = 2000) -> str:
        """What the model's commands printed, one block per call, size-bounded.

        Bounded per call because a build log is not a diagnosis, and
        `transcript()` — the only other view of this — is dominated by the 60KB
        system prompt repeated once per turn.

        **Head and tail, not just the tail**, because the Bash tool has already
        truncated once and it truncates at the *head*: over `max_output_bytes`
        it keeps the first N bytes, drops the rest, and appends an
        `[output truncated…]` notice plus the status suffix. A tail-only window
        on such a result shows the notice and the exit code and nothing that
        produced them. Under the cap the two halves overlap and the whole block
        is shown, which is the common case.

        **Credential-shaped values are scrubbed.** This renders into
        `Stack.diagnostics`, which every failing scenario prints — including
        `test_the_token_is_injected_without_the_model_ever_holding_it`, whose
        assertion fires precisely when a token reached the conversation. Without
        this, the report publishing the leak *is* the leak, on a public repo,
        into a terminal scrollback that gets pasted into an issue. Same reason
        `ServiceCall.__repr__` and `FullCredentials.__repr__` redact, and the
        values come from the same `is_credential_key` shape rule `Stack.env`
        uses rather than from a list somebody has to remember to extend.

        Never raises. This is a failure path, and a report that dies rendering
        replaces the diagnosis with a harness traceback.
        """
        try:
            results = self.endpoint.tool_results()
        except Exception as exc:  # pragma: no cover - diagnostics must not fail
            return f"(unavailable: {exc!r})"
        if not results:
            return "(the model made no tool calls)"
        blocks = []
        for index, text in enumerate(results):
            text = self._scrub(text)
            if len(text) > per_call:
                half = per_call // 2
                elided = len(text) - 2 * half
                text = (
                    text[:half]
                    + f"\n...[{elided} chars elided]...\n"
                    + text[-half:]
                )
            blocks.append(f"[call {index}]\n{text}")
        return "\n".join(blocks)

    def _scrub(self, text: str) -> str:
        """Every credential this stack knows, replaced by its name.

        The name rather than a bare `<redacted>`: "the value of
        ISTOTA_DEVELOPER_GITLAB_TOKEN appeared here" is the diagnosis, and
        blanking it uniformly throws that away.

        **Two sources, because neither covers both shapes.** The compose
        environment carries the credentials on the `full` shape and is empty on
        the lean one, which renders its config on the host — so a lean-shape
        scrub drawing only on it would miss the forge token, which is the exact
        value the scenario this protects asserts about. The other source is each
        service's own `credential`, which every stub bound off loopback is
        required to have (`HttpStub.start`), read by `getattr` like the rest of
        the optional service members.

        Longest first, so a value containing another is replaced whole rather
        than left with the shorter one already substituted inside it.
        """
        secrets = list(self._secrets)
        for name, service in self.services.items():
            credential = getattr(service, "credential", None)
            if isinstance(credential, str) and credential:
                secrets.append((f"{name} credential", credential))
            # A second, plural source, because one service holds four secrets
            # and none of them is "the" credential: the signaling server's
            # internal-client door and its two session keys are the *server's*,
            # generated by the harness and written into a compose env-file, so
            # on the lean shape — which puts nothing credential-shaped in
            # `Stack.env` — they are in no source above.
            extra = getattr(service, "credentials", None)
            if isinstance(extra, dict):
                for label, value in extra.items():
                    if isinstance(value, str) and value:
                        secrets.append((f"{name} {label}", value))
        for key, value in sorted(secrets, key=lambda p: len(p[1]), reverse=True):
            if value in text:
                text = text.replace(value, f"<{key}>")
        return text


def _bind_services(stack: "Stack") -> None:
    """Hand the running stack to any service that cannot exist without one.

    Three so far, for the same reason and on both shapes. `NextcloudService`
    attaches to a container the boot just started and needs a way to run `occ`
    inside it. `MailService` and `SignalingService` need the host ports compose
    published, which are ephemeral and do not exist until `up` returns.

    Duck-typed rather than a protocol member, because four of the seven services
    have nothing to bind and would carry an empty method apiece.
    """
    for service in stack.services.values():
        binder = getattr(service, "bind_stack", None)
        if binder is not None:
            binder(stack)


@dataclass(frozen=True)
class Shape:
    """Everything booting a stack needs that the profile does not carry.

    One shape, the deployment as shipped: `compose_file` with its entrypoint,
    run in full, plus `overlay` (the harness concessions), plus
    `nextcloud_overlay` for a profile on the `full` shape. The config is an
    input on every stack: the testbed writes `config.toml` and the admins file
    into a directory the overlay binds at `/data/config`, and one file per
    credential into the directory the shipped `secrets:` read.
    """

    compose_file: Path
    """`docker/docker-compose.yml` — the production artifact, unedited."""

    image: str
    """The tag every istota-image service runs. `up --build` writes it on the
    session's first boot, shared by every stack after."""

    overlay: Path = TESTBED_OVERLAY
    """`testbed/compose/testbed.yml`, the harness concessions. Read it."""

    nextcloud_overlay: Path = NEXTCLOUD_OVERLAY
    """`testbed/compose/nextcloud.yml`, the full shape's Nextcloud fixture."""

    extra_overlays: tuple[Path, ...] = ()
    """Applied to every stack after everything else: the negative controls'
    way in (`ISTOTA_TESTBED_CONTROL_OVERLAYS`)."""

    ready_timeout: int = READY_TIMEOUT

    full_ready_timeout: int = FULL_READY_TIMEOUT

    keep: bool = False
    """`ISTOTA_TESTBED_KEEP`: persist the fixture's expensive volumes between
    sessions. See `StackPool._down` for what is kept and what is wiped."""

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
        shape: Shape,
        platform: str = "",
        project_prefix: str = "istota-testbed-",
    ) -> None:
        self.workdir = workdir
        self.shape = shape
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
        if profile.shape not in READY_SERVICES:
            raise StackError(
                f"profile {profile.name!r} declares shape {profile.shape!r}; the "
                f"shapes are {sorted(READY_SERVICES)}"
            )
        return self._boot_stack(profile)

    def _scratch(self, profile: Profile) -> Path:
        self._booted += 1
        return self.workdir / f"{profile.name}-{self._booted}"

    def _boot_stack(self, profile: Profile) -> Stack:
        """Start the services, write the inputs, bring the stack up, wait ready.

        The order is not arrangeable: the services have to be listening before
        the config that names their ports is written, and the config, the
        secret files and the env-file have to exist before the containers that
        read them start.

        The shipped entrypoint runs in full on both shapes, which is what makes
        this tier a witness for it. The `full` shape adds the Nextcloud fixture,
        a credential set and reserved host ports, and switches every subsystem
        off except the ones the profile names (`FULL_MODULE_SWITCHES`).

        The checkout's image is built once per session, on the first boot that
        runs it; a profile naming its own `image` (a negative control) builds
        nothing. The health check answers "the tasks table exists", which the
        entrypoint satisfies at `istota init`, several steps before it execs
        the scheduler, so the boot also waits for pid 1 to be the scheduler.
        """
        full = profile.shape == "full"
        scratch = self._scratch(profile)
        scratch.mkdir(parents=True, exist_ok=True)
        config_dir = scratch / "config"
        secrets_dir = scratch / "secrets"
        image = profile.image or self.shape.image
        timeout = self.shape.full_ready_timeout if full else self.shape.ready_timeout

        services: dict[str, Service] = {}
        args: list[str] = []
        credentials = self._full_credentials() if full else None
        if credentials is not None:
            # Checked twice, and the first one is before anything is
            # constructed: `services.build` opens a listening socket on every
            # interface and the boot then builds an image, so a refusal that
            # waited for the full map would pay for both before saying no.
            self._refuse_conflicting_env(full_env(
                {}, credentials, config_dir=config_dir, secrets_dir=secrets_dir, image=image,
            ))

        started = time.monotonic()
        try:
            for name in profile.services:
                # Every host-side stub binds all interfaces, because the daemon
                # that reaches it lives in a container. `HttpStub.start` is what
                # makes each of them name the credential it is publishing.
                services[name] = service_support.build(
                    name, scratch=scratch, host=PUBLIC_BIND, credentials=credentials,
                )
            if credentials is not None:
                document, secret_values = full_config(services, credentials, profile)
                environment = full_env(
                    services, credentials,
                    config_dir=config_dir, secrets_dir=secrets_dir, image=image,
                )
            else:
                document, secret_values = lean_config(profile, services), lean_secrets()
                environment = lean_env(
                    services, image=image, config_dir=config_dir, secrets_dir=secrets_dir,
                    browser_image=profile.browser_image,
                )
            write_config(config_dir, document)
            write_secrets(secrets_dir, secret_values)
            self._refuse_conflicting_env(environment)
            args, env_file = self._compose_args(profile, scratch)
            write_env_file(env_file, environment)

            build = not profile.image and not self._built
            skip = () if full or profile.web else ("web", "nginx")
            up(args, platform=self.platform, build=build, skip=skip)
            if build:
                self._built = True
            wait_all_ready(
                args,
                READY_SERVICES[profile.shape],
                timeout=timeout,
                on_ready=lambda service, seconds: self.boot_times.append(
                    (profile.name, service, seconds)
                ),
            )
            stack = Stack(
                profile=profile, args=args, services=services, env=environment,
                config_dir=config_dir,
            )
            _bind_services(stack)
            remaining = int(timeout - (time.monotonic() - started))
            stack.wait_healthy(timeout=max(1, remaining))
        except BaseException:
            # Both halves, and in this order. A stack that came up before the
            # wait timed out is holding named volumes; a stub that bound before
            # a later one raised is holding a publicly-bound socket and a live
            # thread for the rest of the session.
            if args:
                self._down(args, shape=profile.shape)
            for service in services.values():
                try:
                    service.close()
                except Exception:  # pragma: no cover - cleanup is best effort
                    logger.debug("closing a service during a failed boot raised")
            raise

        self.boot_times.append((profile.name, "total", time.monotonic() - started))
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
            return generate_credentials(*reserve_ports(2))

        if self._credentials is not None:
            return self._credentials
        if keep_file.exists():
            persisted = json.loads(keep_file.read_text())
            # A keep file written before `signaling_port` existed has no such
            # key, and the dataclass default is `0` — which hands the port back
            # to Docker's ephemeral choice and reinstates exactly the collision
            # `reserve_ports` exists to prevent. Reserved rather than defaulted,
            # because unlike `nc_port` nothing has baked this one into the kept
            # volumes: a different port each session costs nothing.
            persisted.setdefault("signaling_port", reserve_port())
            self._credentials = FullCredentials(**persisted)
            return self._credentials

        self._credentials = generate_credentials(*reserve_ports(2))
        keep_file.parent.mkdir(parents=True, exist_ok=True)
        _write_private(keep_file, json.dumps(self._credentials.__dict__))
        return self._credentials

    def _keep_file(self) -> Path | None:
        if not self.shape.keep or self.shape.keep_dir is None:
            return None
        return self.shape.keep_dir / "credentials.json"

    def _compose_args(self, profile: Profile, scratch: Path) -> tuple[list[str], Path]:
        """The compose prefix for one stack, and the env-file it rides in.

        The order of the `-f` files decides what wins: the shipped file, the
        profile's own overlays (a mail server, say), the harness concessions,
        the Nextcloud fixture on the full shape (whose `/mnt/shared` has to win
        over the concessions' empty one), and last the negative controls,
        which exist to override everything.

        The project name is fresh per stack, so one left behind by an
        interrupted run is never adopted (and then torn down) by the next
        session; the session-start sweep reclaims those. Under `KEEP` the full
        shape's name is *stable*, because compose scopes a named volume to the
        project, and a fresh name every session would leave the kept volumes
        attached to a project nothing looks at again.

        Compose interpolates the compose files on *every* subcommand, so a
        variable supplied only to `up` makes `ps`, `exec`, `logs` and `down`
        fail during interpolation. An `--env-file` rides in the argument list,
        so every subcommand gets it and no caller has to remember.
        """
        if profile.shape == "full" and self.shape.keep:
            digest = hashlib.sha256(
                str(self.shape.compose_file.resolve()).encode()
            ).hexdigest()[:8]
            project = f"{self.project_prefix.rstrip('-')}{KEEP_PROJECT_MARKER}{digest}"
        elif profile.shape == "full":
            project = f"{self.project_prefix}full-{uuid.uuid4().hex[:8]}"
        else:
            project = f"{self.project_prefix}{uuid.uuid4().hex[:8]}"
        overlays = [*profile.compose_overlays, self.shape.overlay]
        if profile.shape == "full":
            overlays.append(self.shape.nextcloud_overlay)
        overlays.extend(self.shape.extra_overlays)
        env_file = scratch / "compose.env"
        return (
            compose_args(
                self.shape.compose_file,
                project=project,
                env_file=env_file,
                overlays=overlays,
                compose_profiles=profile.compose_profiles,
            ),
            env_file,
        )

    #: Under `KEEP`, the volumes that are wiped anyway, by unqualified name.
    #:
    #: `istota_data` holds the framework database every assertion is read out
    #: of, so keeping it would make session 2's rows depend on session 1's. It
    #: also holds the `_provisioned_rooms` record, whose absence is what puts
    #: room provisioning back on the find-by-name path. The config is not on it:
    #: the testbed binds a fresh one from the session's scratch directory.
    #: `redis_data` is a cache with nothing in it worth a second of boot time.
    KEEP_WIPES = ("istota_data", "redis_data")

    def _down(self, args: list[str], *, shape: str) -> None:
        """Tear a stack down, keeping the expensive volumes if asked to.

        `shape` rather than `self.shape.keep` alone: one pool serves both shapes,
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
        hook does not run, the flag never appears, the fixture's health check
        never passes, and `istota`, which depends on it, never starts. Wiping `istota_data` alone gets the fresh database and the room
        re-provisioning that wiping `shared_files` was supposed to buy.

        The second correction is in `_compose_args`: the port has to be
        pinned across kept sessions, because `provision-nc.sh` bakes the OAuth2
        redirect URI at first install and — same hook, same reason — does not
        revisit it.

        The consequence for scenarios is that `KEEP` and the provisioning suite
        are mutually exclusive: a suite asserting on first-install state cannot
        run against a volume set whose first install was a previous session's.
        `tests/full/conftest.py` refuses that combination by name rather than
        letting it fail as four unrelated-looking assertions.
        """
        keep = shape == "full" and self.shape.keep
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
                # surviving `istota_data` means the next session reads the
                # previous one's framework database and never re-provisions its
                # rooms, which is a wrong-answer boot a minute later with
                # nothing in the log.
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
