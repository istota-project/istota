"""Unix socket proxy for skill CLI commands.

Runs skill CLI commands with credentials injected server-side, so the
Claude subprocess never sees secret env vars. The protocol is one JSON
request/response per connection, newline-terminated.
"""

import json
import logging
import socket
import subprocess
import sys
from pathlib import Path

from istota import skill_client
from istota.unix_server import UnixSocketServer

logger = logging.getLogger("istota.skill_proxy")

# Owner-only, so no other local user can ask this proxy for a credential. This
# proxy's own decision, stated here rather than inherited from the server
# lifecycle it is handed to.
SOCKET_MODE = 0o600

# Listen backlog. Connections are skill calls from one sandboxed task.
LISTEN_BACKLOG = 8

# How much longer than the subprocess budget a connection stays armed, so the
# handler outlives the command it is waiting on and can report the timeout
# rather than dropping the connection under it.
CONNECTION_SLACK_SECONDS = 10

# Headroom between the largest skill budget and the client's wait: the
# connection slack above, plus room for the proxy to serialize and send a
# response the size of a skill's whole stdout after the subprocess has already
# spent its full budget.
CLIENT_WAIT_MARGIN_SECONDS = 30

# The ceiling on any single skill's timeout, the global and a per-skill entry
# alike, at the *default* client wait. Derived from what `skill_client` waits
# rather than chosen: the client arms its socket before it sends, so a server
# budget past that wait means the client gives up first and the model reads a
# completed call as no answer at all. A deployment that raises
# `security.skill_client_wait_seconds` raises the ceiling with it — the config
# field is what the resolver takes, never `ISTOTA_SKILL_CLIENT_WAIT`, which
# lives in the model's own environment where a task can rewrite it
# (ISSUE-450).
MAX_SKILL_TIMEOUT_SECONDS = (
    skill_client.SKILL_CLIENT_WAIT_SECONDS - CLIENT_WAIT_MARGIN_SECONDS
)


def _usable_seconds(raw) -> int | None:
    """A usable positive whole number of seconds, or None.

    `bool` is excluded before `int()` because `int(True)` is 1 and `= true` is
    a plausible typo that would otherwise resolve to a one-second value rather
    than falling through.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        candidate = int(raw)
    except (TypeError, ValueError):
        return None
    return candidate if candidate > 0 else None


def effective_client_wait(client_wait) -> int:
    """The wait to derive the ceiling from: a usable configured value, or the
    client's shipped default. Same robustness rule as the per-skill entries —
    the value comes off a loaded config nothing validates, and this runs on
    the path of every skill call.

    Clamped to `skill_client.MAX_CLIENT_WAIT_SECONDS`, matching the client's
    own clamp, so an absurd configured value neither overflows
    `conn.settimeout` nor leaves the two ends deriving different waits.
    Public because `task_env` builds the client's export from this same
    function — one parse feeding both ends is what keeps a value the loader
    did not coerce (a float set programmatically, say) from giving the proxy
    a longer wait than the client arms."""
    usable = _usable_seconds(client_wait)
    if usable is None:
        return skill_client.SKILL_CLIENT_WAIT_SECONDS
    return min(usable, skill_client.MAX_CLIENT_WAIT_SECONDS)


def _ceiling_seconds(client_wait) -> int:
    """The largest budget any skill may be given under this client wait.

    Floored at one second: a wait at or under the margin is a
    misconfiguration, and a non-positive number here would reach
    `subprocess.run(timeout=...)` and `conn.settimeout(...)`, turning it into
    a raise on every call rather than a call that fails fast and is reported
    by `describe_skill_timeouts`."""
    return max(1, effective_client_wait(client_wait) - CLIENT_WAIT_MARGIN_SECONDS)

# The shipped per-skill policy, in code rather than in the config default, and
# that placement is the point (ISSUE-448). `config_mapper` maps a `dict` field
# through `coerce_dict`, which passes the operator's table through **verbatim**
# — a dict field replaces its default, it does not merge — and Ansible's own
# hash behaviour is replace too. So a `default_factory` carrying `code_review`
# would be silently dropped by an operator who wrote
# `[security.skill_proxy_timeouts]` to set *some other* skill, taking the
# review's ceiling back to the global and reintroducing the exact bug this map
# exists to fix, with only a log line to say so. Here it is not something an
# operator's table can clobber: `security.skill_proxy_timeouts` defaults to
# empty and is consulted first, so naming `code_review` there still overrides
# this, and naming anything else leaves it alone.
DEFAULT_SKILL_TIMEOUTS: dict[str, int] = {
    # The only skill that drives model calls of its own, so the only one whose
    # work is measured in minutes. 540 leaves room for the 480s per-agent budget
    # plus the review's own assembly reserve.
    "code_review": 540,
}


def resolve_skill_timeout(default: int, overrides, skill: str, client_wait=None) -> int:
    """Seconds this skill's subprocess gets: operator entry, shipped policy, global.

    `security.skill_proxy_timeout` is one number applied to every proxied call,
    and `code_review` is the only skill that drives model calls of its own — so
    the only lever on a review's budget was a limit on every other skill too,
    and the review's ceiling was that global minus an assembly reserve (240s at
    the shipped default). A per-skill entry is what lets one skill have minutes
    without handing them to the rest (ISSUE-448).

    An entry **replaces** the value below it rather than raising it. Narrowing
    one chatty skill is the same mechanism as widening the review, and reading
    the value as a floor would silently ignore half of what the map is for.

    Pure and silent. Nothing raises and nothing is trusted — `config_mapper`
    passes a table's values through uncoerced, so what arrives is whatever the
    TOML said, and this runs on the path of every skill call the deployment
    makes, where one malformed line must not be able to break them all. It does
    not log, because per-connection is the wrong cadence for a fact about the
    configuration: `describe_skill_timeouts` reports the same judgements once,
    at proxy construction, by asking this function rather than restating it.

    `client_wait` is `security.skill_client_wait_seconds` — the operator's
    statement of what the sandboxed client arms — and moves the ceiling with
    it (ISSUE-450). It must come from the loaded config, never from
    `ISTOTA_SKILL_CLIENT_WAIT`: the variable lives in the model's environment,
    and deriving the server-side cap from it would let a task widen a bound
    the operator set. `None` (or anything unusable) is the shipped 600.
    """
    resolved = default
    entry = _entry_seconds(overrides, skill)
    if entry is None:
        entry = _entry_seconds(DEFAULT_SKILL_TIMEOUTS, skill)
    if entry is not None:
        resolved = entry
    return min(resolved, _ceiling_seconds(client_wait))


def _entry_seconds(overrides, skill) -> int | None:
    """One usable positive entry from a mapping, or None for absent or unusable."""
    try:
        raw = overrides.get(skill) if overrides is not None else None
    except AttributeError:
        return None
    return _usable_seconds(raw)


def describe_skill_timeouts(default: int, overrides, client_wait=None) -> list[str]:
    """Every configured timeout whose resolved value is not what was written.

    Reported once, at proxy construction, rather than from the resolver — that
    runs per connection, so a warning there repeats for the life of the
    deployment on every skill call, which is noise rather than a diagnosis. And
    it is computed by *calling* `resolve_skill_timeout` rather than restating
    its rules, so a message here can never describe a decision the resolver did
    not make.

    Covers the unusable entry (a string, a bool, a zero) that silently falls
    through, any value clamped by the client-wait ceiling — the global
    included, which is the one an operator who never wrote a per-skill table can
    still trip — and a `skill_client_wait_seconds` that is not a positive
    number of seconds, which silently falls back to the client's shipped
    default.
    """
    notes: list[str] = []
    effective_wait = effective_client_wait(client_wait)
    ceiling = _ceiling_seconds(client_wait)
    if client_wait is not None and _usable_seconds(client_wait) is None:
        notes.append(
            f"skill_client_wait_seconds is {client_wait!r}, which is not a "
            f"positive number of seconds, so the client wait is the default "
            f"{skill_client.SKILL_CLIENT_WAIT_SECONDS}s"
        )
    elif (
        client_wait is not None
        and _usable_seconds(client_wait) > skill_client.MAX_CLIENT_WAIT_SECONDS
    ):
        notes.append(
            f"skill_client_wait_seconds of {client_wait!r} is past the "
            f"{skill_client.MAX_CLIENT_WAIT_SECONDS}s either end will arm, so "
            f"the client wait is {effective_wait}s"
        )
    if default > ceiling:
        notes.append(
            f"skill_proxy_timeout of {default}s is past the "
            f"{effective_wait}s the sandboxed client "
            f"waits, so every skill is being given "
            f"{ceiling}s instead"
        )
    try:
        written = dict(overrides) if overrides is not None else {}
    except (TypeError, ValueError):
        notes.append(
            f"skill_proxy_timeouts is {type(overrides).__name__}, not a table, "
            f"so every skill is being given the {default}s global"
        )
        return notes
    for skill in sorted(written, key=str):
        resolved = resolve_skill_timeout(default, written, skill, client_wait)
        if _entry_seconds(written, skill) is None:
            notes.append(
                f"skill_proxy_timeouts[{skill!r}] is {written[skill]!r}, which "
                f"is not a positive number of seconds, so {skill!r} is being "
                f"given {resolved}s"
            )
        elif resolved != written[skill]:
            notes.append(
                f"skill_proxy_timeouts[{skill!r}] of {written[skill]!r} is past "
                f"the {effective_wait}s the sandboxed "
                f"client waits, so {skill!r} is being given {resolved}s"
            )
    return notes


class SkillProxy:
    """Unix socket server that proxies skill CLI commands with credentials.

    Usage::

        with SkillProxy(sock_path, credential_env, base_env) as proxy:
            # Claude subprocess runs here — calls istota-skill client
            ...

    The server accepts connections, reads a JSON request, runs the skill
    CLI with merged env (base_env + credential_env), and returns the result.
    """

    def __init__(
        self,
        socket_path: Path,
        credential_env: dict[str, str],
        base_env: dict[str, str],
        timeout: int = 300,
        skill_timeouts: dict | None = None,
        client_wait_seconds: int | None = None,
        allowed_credentials: set[str] | None = None,
        skill_credential_map: dict[str, set[str]] | None = None,
        allowed_skills: frozenset[str] | None = None,
        authorized_skills: frozenset[str] | None = None,
        task_id: int | None = None,
    ):
        self.credential_env = credential_env
        self.base_env = base_env
        self.timeout = timeout
        # Per-skill overrides of the timeout above. Resolved per *connection*
        # rather than per proxy: one proxy serves every skill a task can call,
        # so `code_review`'s minutes and `email`'s seconds have to come apart
        # inside the handler.
        self.skill_timeouts = skill_timeouts
        # `security.skill_client_wait_seconds`, from the loaded config and
        # nowhere else. Never read back from ISTOTA_SKILL_CLIENT_WAIT: that
        # export is in the model's environment, and deriving the server-side
        # ceiling from it would let a task widen a bound the operator set
        # (ISSUE-450).
        self.client_wait_seconds = client_wait_seconds
        for note in describe_skill_timeouts(
            timeout, skill_timeouts, client_wait_seconds,
        ):
            logger.warning("skill proxy: %s", note)
        self.allowed_credentials = allowed_credentials
        self.skill_credential_map = skill_credential_map
        self.allowed_skills = allowed_skills
        # Skills authorized for credential access this task. None = no filter
        # (back-compat for callers that don't pass it). Used purely for the
        # informative-rejection list returned to the client.
        self.authorized_skills = authorized_skills
        self.task_id = task_id
        self._server = UnixSocketServer(
            socket_path,
            # Resolved per connection, not captured here: the accept loop
            # used to call `self._handle_connection` at accept time, and a
            # test substitutes it on the instance.
            lambda conn: self._handle_connection(conn),
            name="skill-proxy",
            label="Skill proxy",
            socket_mode=SOCKET_MODE,
            backlog=LISTEN_BACKLOG,
            logger=logger,
        )

    @property
    def socket_path(self) -> Path:
        """The path the server is bound to. One copy, held by the server."""
        return self._server.socket_path

    def start(self) -> None:
        self._server.start()
        logger.debug("Skill proxy started on %s", self.socket_path)

    def stop(self) -> None:
        self._server.stop()
        logger.debug("Skill proxy stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            # `settimeout` bounds each *blocking operation*, not the connection,
            # so this is the budget for the request read below and — after the
            # re-arm further down — for sending the response back. It is not an
            # end-to-end deadline and cannot expire while the handler sits in
            # `subprocess.run`, which is not a socket operation.
            #
            # Armed at the global here because the skill is not known until the
            # request is parsed.
            conn.settimeout(self.timeout + CONNECTION_SLACK_SECONDS)
            data = self._recv_all(conn)
            if not data:
                return

            try:
                request = json.loads(data)
            except json.JSONDecodeError as e:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Invalid JSON request: {e}",
                    "returncode": 1,
                })
                return

            # Route by request type: "credential" for lookups, default for skill calls
            req_type = request.get("type")

            if req_type == "credential":
                name = request.get("name", "")
                # Scope check: if allowed_credentials is set, only return
                # credentials authorized for this task.
                if self.allowed_credentials is not None and name not in self.allowed_credentials:
                    logger.warning(
                        "proxy_rejected task_id=%s type=credential name=%s reason=not_authorized",
                        self.task_id, name,
                    )
                    self._send_response(conn, {
                        "error": f"Credential not authorized for this task: {name!r}",
                        "reason": "not_authorized_credential",
                        "name": name,
                    })
                    return
                if name not in self.credential_env:
                    logger.warning(
                        "proxy_rejected task_id=%s type=credential name=%s reason=not_present",
                        self.task_id, name,
                    )
                    self._send_response(conn, {
                        "error": f"Credential not present in environment: {name!r}",
                        "reason": "credential_not_present",
                        "name": name,
                    })
                    return
                self._send_response(conn, {"value": self.credential_env[name]})
                return

            skill = request.get("skill", "")
            args = request.get("args", [])

            # Validate skill name against CLI-capable skills from skill index
            if self.allowed_skills is not None and skill not in self.allowed_skills:
                logger.warning(
                    "proxy_rejected task_id=%s type=skill skill=%s reason=unknown_skill",
                    self.task_id, skill,
                )
                authorized_list = (
                    sorted(self.authorized_skills)
                    if self.authorized_skills is not None
                    else sorted(self.allowed_skills)
                )
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": (
                        f"Unknown skill: {skill!r}.\n"
                        f"Authorized skills for this task: {', '.join(authorized_list)}"
                    ),
                    "returncode": 1,
                    "reason": "unknown_skill",
                    "skill": skill,
                    "authorized_skills": authorized_list,
                })
                return


            # Scales the *response send* with this skill's own budget: a skill
            # allowed nine minutes may also take longer to hand back its stdout
            # than one allowed five, and the global is an unrelated number to
            # bound that by. The credential branch and the two rejections above
            # return before this deliberately — each answers from memory, so the
            # global is already more than any of them can need.
            skill_timeout = resolve_skill_timeout(
                self.timeout, self.skill_timeouts, skill,
                self.client_wait_seconds,
            )
            if skill_timeout != self.timeout:
                conn.settimeout(skill_timeout + CONNECTION_SLACK_SECONDS)

            # Build command
            cmd = [sys.executable, "-m", f"istota.skills.{skill}"] + args

            # Merge envs: base gets only the credentials this skill needs
            merged_env = dict(self.base_env)
            if self.skill_credential_map is not None:
                allowed_vars = self.skill_credential_map.get(skill, set())
                for var in allowed_vars:
                    if var in self.credential_env:
                        merged_env[var] = self.credential_env[var]
            else:
                # Backward compat: no map means all credentials
                merged_env.update(self.credential_env)

            try:
                result = subprocess.run(
                    cmd,
                    env=merged_env,
                    capture_output=True,
                    text=True,
                    timeout=skill_timeout,
                )
                self._send_response(conn, {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                })
            except subprocess.TimeoutExpired:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Skill command timed out after {skill_timeout}s",
                    "returncode": 124,
                })
            except Exception as e:
                self._send_response(conn, {
                    "stdout": "",
                    "stderr": f"Failed to run skill: {e}",
                    "returncode": 1,
                })

        except Exception:
            logger.debug("Error handling proxy connection", exc_info=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _recv_all(conn: socket.socket) -> str:
        """Read until newline (protocol delimiter)."""
        chunks = []
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    @staticmethod
    def _send_response(conn: socket.socket, response: dict) -> None:
        """Send JSON response terminated by newline."""
        data = json.dumps(response) + "\n"
        conn.sendall(data.encode("utf-8"))
