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
import threading
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext
from pathlib import Path
from time import sleep, time

from istota import skill_client
from istota.sandbox import peer_process
from istota.credentials.vault import label_for_display
from istota.sandbox.unix_server import UnixSocketServer

logger = logging.getLogger("istota.sandbox.skill_proxy")

#: The three paths that may ask for a shared credential, as the request
#: declares itself: a skill CLI resolving a stamped argument, the shim
#: injecting into a child process, or a value handed back to the caller.
#:
#: It is a **claim by the caller and is never verified** — the proxy knows the
#: caller is in its task's process tree, not which program inside it is asking
#: — so it decides a log level and nothing else. Anything absent or
#: unrecognised is recorded as ``read``, which is the direction that does not
#: under-report.
#:
#: All three have producers: ``skill`` is the credential stamp in
#: ``skills/_credref``, resolving a stamped argument host-side before the
#: handler runs (`browse interact --fill-credential`), and ``inject`` and
#: ``read`` are the shim's ``run`` and ``get``.
VAULT_MODES = frozenset({"skill", "inject", "read"})
VAULT_MODE_DEFAULT = "read"
OTP_MIN_REMAINING_SECONDS = 10

#: What a task is told when it asks for a value only the daemon may read.
_DAEMON_ONLY_MESSAGES = {
    "credential_is_otp_seed": "Credential is an OTP seed; use browse --fill-otp",
    "credential_is_recovery": ("Credential holds recovery codes, which only the user can read, "
                               "in Settings, Credentials"),
}


def entry_fields(entry: str, values: dict[str, str]) -> dict[str, str]:
    """An entry's member names as field keys: `password`, `username`, `url`, custom.

    The entry's own name is its password, and every other member is
    `<entry>_<field>` because the vault parser named it so, so its key is the
    part after the prefix. That short key is used only when nothing else could
    land on it: not `password`, not another member's short key, and not any
    member's full name. Otherwise the member keeps its full name, which is
    unique, so no field is dropped, overwritten or mistaken for the password.
    """
    prefix = entry + "_"
    short = {m: m[len(prefix):] if m.startswith(prefix) else m
             for m in values if m != entry}
    claimed: dict[str, int] = {}
    for key in short.values():
        claimed[key] = claimed.get(key, 0) + 1
    fields: dict[str, str] = {}
    if entry in values:
        fields["password"] = values[entry]
    for member in sorted(short):
        key = short[member]
        if key == "password" or claimed[key] > 1 or key in values:
            key = member
        fields[key] = values[member]
    return fields


# Owner-only, so no other local user can ask this proxy for a credential. This
# proxy's own decision, stated here rather than inherited from the server
# lifecycle it is handed to. It keeps out no other *task*, since they all run
# as this uid; the peer check in `_handle_connection` is what does that.
SOCKET_MODE = 0o600

# How long a connection from an unrecognised peer waits for a registration
# before it is refused. `on_pid` is called after the spawn returns and the pid
# of a skill subprocess arrives down a pipe, so a child can reach the socket a
# moment before its root is known. A sibling task pays this once per refused
# connection, which is the cost to it of trying.
PEER_REGISTRATION_GRACE_SECONDS = 2.0

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


def _default_trusted_roots() -> frozenset[int]:
    """Roots a proxy starts with when the caller names none: none at all.

    A function rather than a constant so the test suite can stand the pytest
    process in for a task's root in one place, rather than in every test that
    talks to a proxy from its own process.
    """
    return frozenset()


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
        vault_credentials: dict[str, str] | None = None,
        vault_fetch_limit: int = 0,
        vault_write_limit: int = 0,
        config=None,
        user_id: str | None = None,
        trusted_roots: Iterable[int] | None = None,
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
        # The user's shared-credential namespace (`vault_entries`), resolved
        # once at task build. A **separate dict from `credential_env`**, and
        # that separation is structural rather than tidiness: the skill
        # dispatch below merges `credential_env` wholesale into a skill
        # subprocess's environment when no per-skill map is given, so a vault
        # value living in that dict would be handed to skill CLIs that never
        # asked for it. The two name spaces also answer different questions —
        # one is a union over skill manifests, the other is whatever the user
        # put in their KDBX file — and only the vault branches read this one.
        self.vault_credentials = vault_credentials or {}
        # `security.vault_fetch_limit_per_task`; 0 is unlimited. Counted here
        # rather than in the shim, because the shim is a program in a directory
        # the model can overwrite and a hand-rolled five-line client speaks the
        # same protocol — the proxy is the only thing that sees every request.
        self.vault_fetch_limit = vault_fetch_limit
        self.vault_write_limit = vault_write_limit
        self.config = config
        self.user_id = user_id
        self._vault_writes = 0
        self._vault_write_lock = threading.Lock()
        # Per task attempt, and that is the whole lifetime: `build_task_runtime`
        # constructs one proxy per attempt, so the counter starts at zero with
        # the attempt and is gone with it. A retry gets a fresh budget, which is
        # correct — it re-executes the prompt from a fresh message list.
        #
        # Locked because `unix_server` runs one thread per connection.
        self._vault_fetches = 0
        # Names this attempt created with vault_create. They have no grant by
        # design (later tasks start narrow), but the task that made one may
        # fill it, which is the documented create-then-sign-up flow.
        self._created_names: set[str] = set()
        self._vault_fetch_lock = threading.Lock()
        # The processes whose descendants this proxy serves (ISSUE-550): the
        # brain's child, reported through `on_pid`, and each skill subprocess
        # this proxy spawns, for as long as it runs. Empty until the first
        # registration, so nothing is served before the task has a process.
        # Each pid maps to its start time, so a recycled number matches nothing.
        self._peer_roots = peer_process.PeerRoots()
        for pid in (
            _default_trusted_roots() if trusted_roots is None else trusted_roots
        ):
            self.authorize_pid(pid)
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

    def authorize_pid(self, pid: int) -> None:
        """Serve ``pid`` and its descendants from now on.

        Pinned to the process's start time as read now. A pid that is already
        gone has no start time and is not registered: it has no descendants
        that could still ask, and registering the bare number would hand its
        authority to whatever holds it next.
        """
        pid = int(pid)
        if not self._peer_roots.authorize(pid):
            logger.warning(
                "proxy_root_unregistered task_id=%s pid=%s reason=unreadable",
                self.task_id, pid,
            )
            return

    def revoke_pid(self, pid: int) -> None:
        """Stop serving ``pid``'s tree."""
        self._peer_roots.revoke(pid)

    @property
    def trusted_roots(self) -> frozenset[int]:
        return self._peer_roots.pids

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

    def _peer_in_task(self, pid: int | None) -> bool:
        """Whether ``pid`` descends from a root, waiting briefly for one."""
        return self._peer_roots.contains(
            pid, grace_seconds=PEER_REGISTRATION_GRACE_SECONDS,
        )

    def _refuse_peer(self, conn: socket.socket, pid: int | None) -> None:
        # Drain the request, bounded, without acting on it. Closing a socket
        # with unread data makes Linux send a reset, which discards the refusal
        # before the client reads it and leaves it an empty answer.
        try:
            conn.settimeout(1.0)
            received = 0
            while received < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                received += len(chunk)
                if b"\n" in chunk:
                    break
        except OSError:
            pass
        logger.warning(
            "proxy_rejected task_id=%s peer_pid=%s reason=peer",
            self.task_id, pid,
        )
        # Shaped for both clients: `error`/`reason` for a credential lookup,
        # `stderr`/`returncode` for a skill call, so neither reads it as a
        # malformed answer.
        self._send_response(conn, {
            "error": "Connection is not from this task's processes",
            "reason": "peer_not_in_task",
            "stdout": "",
            "stderr": (
                "Skill proxy refused the connection: it is not from this "
                "task's processes\n"
            ),
            "returncode": 1,
        })

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            # Before the request is parsed (ISSUE-550). The socket's 0600 mode keeps
            # out other OS users and no other task, since every task runs as
            # the daemon's uid; this is what keeps out the rest. The kernel
            # names the peer, so nothing in the request is consulted.
            peer = peer_process.peer_pid(conn)
            if not self._peer_in_task(peer):
                self._refuse_peer(conn, peer)
                return

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

            if req_type == "wallet_card":
                self._send_response(conn, {"ok": False, "reason": "wallet_channel_unavailable"})
                return

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
                # Manifest values have no user-controlled reveal marker. Skills
                # receive them in their host-side env, never through this read.
                if self._refuse_brokered_credential(conn, name, "credential", "read"):
                    return
                self._send_response(conn, {"value": self.credential_env[name]})
                return

            if req_type == "vault_list":
                # Not counted against the fetch limit: it returns names and no
                # values, it is the discovery step the prompt tells the model to
                # take, and one call answers what a hundred `vault_credential`
                # probes would.
                names = sorted(self.vault_credentials)
                logger.info(
                    "vault_list task_id=%s count=%d", self.task_id, len(names),
                )
                reply = {"names": names}
                if self.config is not None and self.user_id:
                    from istota import db
                    from istota.credentials.broker.bindings import get_binding
                    from istota.credentials.broker.grants import get_grant
                    with db.get_db(self.config.db_path) as database:
                        metadata = {name: get_binding(database, self.user_id, name)
                                    for name in names}
                        # Forge names are visible only when this task already
                        # has access to the corresponding deployment token.
                        from istota.credentials.broker.bindings import forge_bindings
                        for name, binding in forge_bindings(self.config.developer).items():
                            env_name = name.split(".")[1].upper() + "_TOKEN"
                            if env_name in self.credential_env:
                                metadata[name] = binding
                        granted = {name for name in metadata if get_grant(database, self.user_id, name)}
                        # Entry names, for a caller that addresses whole entries
                        # (`wordpress --site`). Membership is the vault parser's,
                        # never a name suffix, and an entry is listed only when a
                        # member is in this task's snapshot.
                        from istota.credentials.broker.bindings import credential_groups
                        entries = sorted(
                            entry for entry, members in credential_groups(database, self.user_id).items()
                            if any(member in self.vault_credentials for member in members)
                        )
                    reply["names"] = sorted(metadata)
                    reply["entries"] = entries
                    reply["credentials"] = [
                        {"name": name, "bound_hosts": (binding or {}).get("hosts", []),
                         "revealable": (binding or {}).get("revealable", False),
                         "kind": (binding or {}).get("kind", "value"),
                         "grant": "granted" if name in granted else "ungranted"}
                        for name, binding in sorted(metadata.items())
                    ]
                self._send_response(conn, reply)
                return

            if req_type == "vault_otp":
                self._send_response(conn, {
                    "error": "OTP reads require the private credential channel",
                    "reason": "invalid_credential_request",
                })
                return

            if req_type == "vault_credential":
                self._serve_vault_credential(conn, request)
                return

            if req_type == "vault_entry":
                self._serve_vault_entry(conn, request)
                return

            if req_type == "vault_otp_set":
                self._serve_vault_otp_set(conn, request)
                return

            if req_type == "vault_recovery_set":
                self._serve_vault_recovery_set(conn, request)
                return

            if req_type == "vault_create":
                self._serve_vault_create(conn, request)
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
                # Legacy skill callers can still connect back for other proxy
                # operations, and they descend from the daemon rather
                # than from the task's brain, so it is registered here for as
                # long as it runs and revoked after, so a reused pid inherits
                # nothing. The reader is joined before the revoke, so a late
                # registration cannot land after it. The cost is a `preexec_fn`,
                # which takes CPython off its vfork fast path, so each skill call
                # is a full fork of the daemon; `Popen` and a registration after
                # it returns would avoid that, at the price of moving the nine
                # tests that patch `subprocess.run` here.
                spawned: list[int] = []

                def _register(pid: int) -> None:
                    spawned.append(pid)
                    self.authorize_pid(pid)

                try:
                    with self._credential_channel(skill_timeout) as credential_fd, \
                            peer_process.reporting_pid(_register) as preexec:
                        merged_env["ISTOTA_CRED_FD"] = str(credential_fd)
                        result = subprocess.run(
                            cmd,
                            env=merged_env,
                            capture_output=True,
                            text=True,
                            timeout=skill_timeout,
                            preexec_fn=preexec,
                            pass_fds=(credential_fd,),
                        )
                finally:
                    for pid in spawned:
                        self.revoke_pid(pid)
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

    @contextmanager
    def _credential_channel(self, timeout: int):
        """A private endpoint owned by one skill invocation, never a task."""
        server, child = socket.socketpair()
        server.settimeout(timeout + CONNECTION_SLACK_SECONDS)
        worker = threading.Thread(
            target=self._serve_credential_channel, args=(server,), daemon=True,
            name="skill-credential-fd",
        )
        try:
            worker.start()
            yield child.fileno()
        finally:
            # Shutdown wakes the reader even if the skill leaked a duplicate.
            # Join before returning the skill result, on failures and timeouts too.
            try:
                server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            child.close()
            if worker.ident is not None:
                worker.join()
            server.close()

    def _serve_credential_channel(self, conn: socket.socket) -> None:
        """Only credential reads, with server-owned skill provenance."""
        try:
            with conn.makefile("rb") as reader:
                while True:
                    # Requests contain a name, not values. Bound malformed input
                    # and retain framing when several requests arrive together.
                    line = reader.readline(65537)
                    if not line:
                        return
                    if len(line) > 65536 or not line.endswith(b"\n"):
                        return
                    try:
                        request = json.loads(line)
                    except (ValueError, UnicodeError):
                        return
                    req_type = request.get("type") if isinstance(request, dict) else None
                    if req_type not in ("vault_credential", "vault_entry", "vault_otp", "wallet_card",
                                        "vault_recovery_target", "vault_recovery_set"):
                        self._send_response(conn, {
                            "error": "Private channel accepts credential reads only",
                            "reason": "invalid_credential_request",
                        })
                        return
                    if req_type == "vault_recovery_target":
                        self._serve_vault_recovery_target(conn, request)
                    elif req_type == "vault_recovery_set":
                        self._serve_vault_recovery_set(conn, request)
                    elif req_type == "wallet_card":
                        self._serve_wallet_card(conn, request)
                    elif req_type == "vault_otp":
                        self._serve_vault_otp(conn, request)
                    elif req_type == "vault_entry":
                        self._serve_vault_entry(conn, request, trusted_skill=True)
                    else:
                        self._serve_vault_credential(conn, request, trusted_skill=True)
        except OSError:
            # The owning invocation closed, timed out, or stopped reading.
            pass
        except Exception:
            logger.warning("Private credential channel failed task_id=%s", self.task_id)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _serve_wallet_card(self, conn: socket.socket, request: dict) -> None:
        from istota import db
        from istota.wallet.purchases import WalletRefusal, claim_fill

        purchase_id = request.get("purchase_id")
        if type(purchase_id) is not int or not 0 < purchase_id <= 2**63 - 1:
            self._send_response(conn, {"ok": False, "reason": "purchase_not_found"})
            return
        try:
            if self.config is None or not self.user_id or self.task_id is None:
                raise WalletRefusal("wallet_unavailable")
            with db.get_db(self.config.db_path) as database:
                grant = claim_fill(database, self.config, user_id=self.user_id,
                                   task_id=self.task_id, purchase_id=purchase_id)
        except WalletRefusal as exc:
            logger.info("wallet fill refused purchase_id=%s reason=%s", purchase_id, exc.reason)
            self._send_response(conn, {"ok": False, "reason": exc.reason})
            return
        except Exception as exc:
            logger.error("wallet fill failed purchase_id=%s exception=%s", purchase_id, type(exc).__name__)
            self._send_response(conn, {"ok": False, "reason": "wallet_error"})
            return
        # Commit before sending: a disconnected reader still spends this fill.
        self._send_response(conn, {"fields": grant.fields, "bound_hosts": grant.bound_hosts})

    def _spend_vault_fetch(self) -> tuple[int, bool]:
        """Charge one fetch to this attempt's budget. ``(count, within)``.

        **Counted per request, not per distinct name, and counted whether or
        not the name resolves.** Counting only successful lookups makes probing
        for absent names free, which is the enumeration the cap exists to
        bound; counting distinct names lets a loop over one name run for ever,
        which is the case it does not need to bound and cannot distinguish.
        """
        with self._vault_fetch_lock:
            self._vault_fetches += 1
            count = self._vault_fetches
        limit = self.vault_fetch_limit
        return count, not limit or count <= limit

    def _refuse_fetch_limit(self, conn: socket.socket, request_type: str, count: int) -> None:
        """The over-budget answer, the same for every name and both read types.

        It names no credential, so a present and an absent name get one reply
        and the cap cannot be used to enumerate the namespace.
        """
        logger.warning(
            "proxy_rejected task_id=%s type=%s "
            "count=%d limit=%d reason=vault_credential_limit",
            self.task_id, request_type, count, self.vault_fetch_limit,
        )
        self._send_response(conn, {
            "error": (
                f"Shared credential fetch limit reached for this task "
                f"({self.vault_fetch_limit})"
            ),
            "reason": "vault_credential_limit",
        })

    def _spend_vault_write(self, conn: socket.socket) -> bool:
        """Charge one write to this attempt's budget; refuse past it. Refusals count."""
        with self._vault_write_lock:
            self._vault_writes += 1
            count = self._vault_writes
        if self.vault_write_limit <= 0 or count > self.vault_write_limit:
            self._send_response(conn, {
                "error": "Vault write limit reached or writes disabled",
                "reason": "vault_write_limit",
            })
            return False
        return True

    def _mirror_generated(self, name: str) -> str:
        """Write a new generated credential to the user's KeePass file, if mirrored.

        A write that does not land leaves the credential stored and the mirror
        pending; the sync retries it. Never turns a stored credential into a
        refusal that would invite the model to create a second one.
        """
        from istota.credentials import vault as secrets_vault
        try:
            return secrets_vault.mirror_generated(self.config, self.user_id, name)
        except Exception:
            logger.exception("vault mirror task_id=%s: write failed", self.task_id)
            return "pending"

    @staticmethod
    def _mirror_sentence(state: str) -> str:
        if state == "mirrored":
            return " A copy is in your KeePass file under generated/."
        if state == "pending":
            return (" The copy in your KeePass file has not been written yet; Istota "
                    "retries on its next vault sync.")
        return ""

    def _serve_vault_create(self, conn: socket.socket, request: dict) -> None:
        """Generate and store one credential host-side; return names only (ISSUE-686).

        The credential lives in the secrets table as ``source="generated"``;
        the user's KeePass file, when there is one and mirroring is on, gets a
        one-way copy. No vault is needed.
        """
        from istota import db
        from istota.mail import support as email_support
        from istota.credentials import generated
        from istota.credentials import vault as secrets_vault
        from istota.notifications.resolvers import task_alert
        from istota.notifications.store import deliver_pending

        if not self._spend_vault_write(conn):
            return

        def refuse(reason: str, message: str) -> None:
            self._send_response(conn, {"error": message, "reason": reason})

        config, user_id = self.config, self.user_id
        if config is None or not user_id:
            refuse("vault_not_configured", "No credential store is available to this task")
            return
        refusal = secrets_vault.vault_isolation_refusal(config, user_id)
        if refusal:
            refuse("vault_isolation_required", refusal)
            return
        slug = request.get("slug")
        username = request.get("username")
        url = request.get("url", "")
        length = request.get("length", 24)
        symbols = request.get("symbols", True)
        if not isinstance(slug, str) or not slug or not isinstance(url, str):
            refuse("vault_write_refused", "A slug and string URL are required")
            return
        names = secrets_vault.generated_entry_names(slug)
        if (secrets_vault.slug_name((slug,)) != slug or any(name is None for name in names)
                or secrets_vault.slug_name((secrets_vault.VAULT_WRITE_GROUP, slug, "totp")) is None):
            refuse("vault_write_refused",
                   "The slug must be lowercase letters, digits and underscores, and short enough "
                   "for its field names")
            return
        name = names[0]
        if any(value != value.strip() for value in (url, username or "")):
            refuse("vault_write_refused", "Credential fields cannot have surrounding whitespace")
            return
        signup_address = None
        if username is None and config.email.enabled and config.email.bot_email:
            bot_address = email_support.per_user_address(config, user_id)
            if bot_address:
                if f"{user_id}+{slug}" in config.users:
                    refuse("vault_write_refused", "Signup address belongs to another user")
                    return
                local, domain = bot_address.rsplit("@", 1)
                signup_address = f"{local}+{slug}@{domain}"
                username = signup_address
        if username is None:
            user = config.users.get(user_id)
            addresses = getattr(user, "email_addresses", ()) if user else ()
            username = next((address for address in addresses if address), None)
        if not isinstance(username, str) or not username:
            refuse("username_required", "Give a username or configure the user's email address")
            return
        if isinstance(length, bool) or not isinstance(length, int) or not isinstance(symbols, bool):
            refuse("vault_write_refused", "Invalid password policy")
            return
        try:
            password = secrets_vault.generate_password(secrets_vault.PasswordPolicy(
                length=length, require_symbols=symbols, allow_symbols=symbols,
            ))
        except secrets_vault.VaultError as exc:
            refuse(type(exc).__name__, str(exc))
            return
        for value in (password, username, url):
            if len(value.encode("utf-8", "surrogatepass")) > secrets_vault.VAULT_MAX_VALUE_BYTES:
                refuse("vault_write_refused", "A credential field is too large")
                return

        reserved_tag = False
        try:
            if signup_address:
                with db.get_db(config.db_path) as db_conn:
                    reserved_tag = db.reserve_signup_tag(db_conn, user_id, slug)
                if not reserved_tag:
                    # A killed task can leave the reservation pending. After five
                    # minutes it is reclaimed unless the credential was stored.
                    with db.get_db(config.db_path) as db_conn:
                        present = db_conn.execute(
                            "SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=?",
                            (user_id, secrets_vault.VAULT_ENTRY_SERVICE, name),
                        ).fetchone() is not None
                        reserved_tag = db.reconcile_stale_signup_tag(
                            db_conn, user_id, slug, present_in_vault=present,
                        )
                    if not reserved_tag:
                        raise generated.GeneratedCredentialError(
                            "VaultWriteRefused", "signup address already used")
            with db.get_db(config.db_path) as db_conn:
                db_conn.execute("BEGIN IMMEDIATE")
                mirror = (generated.default_mirror(db_conn, user_id)
                          and secrets_vault._vault_is_enabled(config, user_id))
                generated.create(db_conn, user_id, name=name, username=username,
                                 password=password, url=url, mirror=mirror)
        except generated.GeneratedCredentialError as exc:
            if reserved_tag:
                with db.get_db(config.db_path) as db_conn:
                    db.cancel_signup_tag(db_conn, user_id, slug)
            refuse("VaultWriteRefused", str(exc))
            return
        except Exception:
            if reserved_tag:
                with db.get_db(config.db_path) as db_conn:
                    db.cancel_signup_tag(db_conn, user_id, slug)
            raise

        # Stored. Nothing below may turn this into a refusal inviting a retry.
        username_name, url_name = names[1], names[2]
        self.vault_credentials.update({name: password, username_name: username})
        if url:
            self.vault_credentials[url_name] = url
        self._created_names.update((name, username_name, url_name))
        granted = None
        if self.task_id:
            from istota.credentials.broker.grants import grant_created_entry
            try:
                with db.get_db(config.db_path) as db_conn:
                    granted = grant_created_entry(db_conn, user_id, name, int(self.task_id))
            except Exception:
                logger.exception("vault_create task_id=%s: conversation grant failed", self.task_id)
        mirrored = self._mirror_generated(name) if mirror else "off"
        confirmation_readable = False
        if signup_address:
            try:
                with db.get_db(config.db_path) as db_conn:
                    confirmation_readable = db.activate_signup_tag(db_conn, user_id, slug)
            except Exception:
                logger.exception("vault_create task_id=%s: signup tag could not be opened", self.task_id)
        try:
            with db.get_db(config.db_path) as db_conn:
                raised = task_alert.write(
                    db_conn, user_id,
                    dedup_key=f"vault-created:{task_alert._slug(name, limit=64)}",
                    title=f"Istota created {name}",
                    body="A credential was generated and stored in Istota." + (
                        " Only the task that created it can use it until you grant it "
                        "in Settings, Credentials." if granted is None else
                        " Interactive turns in the conversation that created it can use it, "
                        "but scheduled runs, including the job that created it, cannot "
                        "until you widen it in Settings, Credentials." if granted[1] else
                        " Later tasks in the conversation that created it can use it; "
                        "widen or revoke that in Settings, Credentials."
                    ) + self._mirror_sentence(mirrored),
                    severity="warning", actionable=True,
                    params={"task_id": self.task_id, "status": "vault_created"},
                )
            if raised is not None:
                deliver_pending(config, [raised])
        except Exception:
            logger.warning("vault_create task_id=%s: notice could not be sent", self.task_id)
        self._send_response(conn, {
            "name": name, "username_name": username_name,
            "url_name": url_name, "username": username,
            "confirmation_readable": confirmation_readable,
        })

    def _serve_vault_otp_set(self, conn: socket.socket, request: dict) -> None:
        """Attach a factor once to a generated credential, in the table (ISSUE-686)."""
        from istota import db
        from istota.credentials import generated
        from istota.credentials import vault
        from istota.lib import totp
        from istota.notifications.resolvers import task_alert
        from istota.notifications.store import deliver_pending

        def refuse(reason, message):
            self._send_response(conn, {"error": message, "reason": reason})

        if not self._spend_vault_write(conn):
            return
        config, user_id = self.config, self.user_id
        if config is None or not user_id:
            refuse("vault_not_configured", "No credential store is available to this task")
            return
        refusal = vault.vault_isolation_refusal(config, user_id)
        if refusal:
            refuse("vault_isolation_required", refusal)
            return
        name, otp = request.get("name"), request.get("otp")
        if not isinstance(name, str) or not name or not isinstance(otp, str):
            refuse("invalid_otp", "An entry name and OTP text are required")
            return
        try:
            canonical = totp.to_uri(totp.parse_user_input(otp))
        except totp.TotpError as exc:
            refuse("invalid_otp", f"Invalid OTP ({exc.code})")
            return
        try:
            with db.get_db(config.db_path) as db_conn:
                db_conn.execute("BEGIN IMMEDIATE")
                seed_name = generated.set_otp(db_conn, user_id, name, canonical)
                mirror = generated.mirror_state(db_conn, user_id, name)["mirror"]
        except generated.GeneratedCredentialError as exc:
            refuse(exc.reason, str(exc))
            return

        self.vault_credentials[seed_name] = canonical
        # Enrollment does not grant an existing credential to this task.
        if name in self._created_names:
            self._created_names.add(seed_name)
        mirrored = self._mirror_generated(name) if mirror else "off"
        try:
            with db.get_db(config.db_path) as db_conn:
                raised = task_alert.write(
                    db_conn, user_id,
                    dedup_key=f"vault-otp-set:{task_alert._slug(name, limit=64)}",
                    title=f"Istota added two-factor to {name}",
                    body="Two-factor enrollment was saved in Istota." + self._mirror_sentence(mirrored),
                    severity="warning", actionable=True,
                    params={"task_id": self.task_id, "status": "vault_otp_set"},
                )
            if raised is not None:
                deliver_pending(config, [raised])
        except Exception:
            logger.warning("vault_otp_set task_id=%s: notice could not be sent", self.task_id)
        self._send_response(conn, {"name": name, "otp": True})

    def _recovery_reach_refusal(self, name: str) -> tuple[str, str] | None:
        """Whether this task may touch ``name``'s recovery codes; ``(reason, message)`` if not.

        A save replaces the stored set, so it is held to what a read of the
        entry needs: in this task's snapshot, and granted to it when the
        broker is on, unless this attempt created it.
        """
        label = label_for_display(name)
        if name not in self.vault_credentials:
            return "vault_credential_not_present", f"No shared credential named {label!r}"
        if (self.config is not None and self.config.security.credential_broker.enabled
                and name not in self._created_names):
            reason = self._skill_grant_refusal(name)
            if reason:
                return reason, f"Credential {label!r} is not granted to this task"
        return None

    def _serve_vault_recovery_target(self, conn: socket.socket, request: dict) -> None:
        """Where ``browse --save-recovery`` may read codes for ``name`` (ISSUE-688).

        Hosts for a name in this task's reach, which ``vault_list`` already
        shows, so it spends no budget; the save that follows spends the write.
        """
        from istota import db
        from istota.credentials import generated
        from istota.credentials import vault
        from istota.credentials.broker.bindings import get_binding

        def refuse(reason, message):
            self._send_response(conn, {"error": f"{message} ({reason})", "reason": reason})

        name = request.get("name")
        if self.config is None or not self.user_id:
            refuse("vault_not_configured", "No credential store is available to this task")
            return
        if vault.vault_isolation_refusal(self.config, self.user_id):
            refuse("vault_isolation_required", "The credential store is not isolated")
            return
        if not isinstance(name, str) or not name:
            refuse("recovery_set_not_generated", "An entry name is required")
            return
        reach = self._recovery_reach_refusal(name)
        if reach:
            refuse(*reach)
            return
        with db.get_db(self.config.db_path) as database:
            if not generated.is_generated(database, self.user_id, name):
                refuse("recovery_set_not_generated",
                       "Recovery codes can be saved only for a credential Istota generated")
                return
            hosts = (get_binding(database, self.user_id, name) or {}).get("hosts", [])
        if not hosts:
            refuse("credential_unbound", "The credential has no site to read the codes from")
            return
        self._send_response(conn, {"name": name, "bound_hosts": hosts})

    def _serve_vault_recovery_set(self, conn: socket.socket, request: dict) -> None:
        """Store or replace a generated credential's recovery codes (ISSUE-688).

        The reply carries a line count and whether an earlier set was replaced;
        the codes go to the table and the mirror, never back to the caller and
        never into this task's credential snapshot.
        """
        from istota import db
        from istota.credentials import generated
        from istota.credentials import vault
        from istota.notifications.resolvers import task_alert
        from istota.notifications.store import deliver_pending

        def refuse(reason, message):
            self._send_response(conn, {"error": f"{message} ({reason})", "reason": reason})

        if not self._spend_vault_write(conn):
            return
        config, user_id = self.config, self.user_id
        if config is None or not user_id:
            refuse("vault_not_configured", "No credential store is available to this task")
            return
        refusal = vault.vault_isolation_refusal(config, user_id)
        if refusal:
            self._send_response(conn, {"error": refusal, "reason": "vault_isolation_required"})
            return
        name, text = request.get("name"), request.get("text")
        if not isinstance(name, str) or not name or not isinstance(text, str):
            refuse("recovery_empty", "An entry name and the codes are required")
            return
        reach = self._recovery_reach_refusal(name)
        if reach:
            refuse(*reach)
            return
        try:
            with db.get_db(config.db_path) as db_conn:
                db_conn.execute("BEGIN IMMEDIATE")
                _, count, replaced = generated.set_recovery(db_conn, user_id, name, text)
                mirror = generated.mirror_state(db_conn, user_id, name)["mirror"]
        except generated.GeneratedCredentialError as exc:
            refuse(exc.reason, {
                "recovery_set_not_generated":
                    "Recovery codes can be saved only for a credential Istota generated",
                "recovery_empty": "No recovery codes were given",
                "recovery_too_large": "That is more text than a set of recovery codes",
                "recovery_unusable": "The codes contain control characters",
            }.get(exc.reason, "Recovery codes could not be saved"))
            return

        mirrored = self._mirror_generated(name) if mirror else "off"
        logger.info("vault_recovery_set task_id=%s name=%s count=%d replaced=%s",
                    self.task_id, label_for_display(name), count, replaced)
        try:
            with db.get_db(config.db_path) as db_conn:
                raised = task_alert.write(
                    db_conn, user_id,
                    dedup_key=(f"vault-recovery-{'replaced' if replaced else 'saved'}:"
                               f"{task_alert._slug(name, limit=64)}"),
                    title=(f"Istota replaced the recovery codes for {name}" if replaced
                           else f"Istota saved recovery codes for {name}"),
                    body=(("A new set of recovery codes replaced the earlier one, which the site "
                           "no longer accepts." if replaced else
                           "The site's recovery codes were saved in Istota.")
                          + " Only you can read them, in Settings, Credentials."
                          + self._mirror_sentence(mirrored)),
                    severity="warning", actionable=True,
                    params={"task_id": self.task_id, "status": "vault_recovery_set"},
                )
            if raised is not None:
                deliver_pending(config, [raised])
        except Exception:
            logger.warning("vault_recovery_set task_id=%s: notice could not be sent", self.task_id)
        self._send_response(conn, {"name": name, "count": count, "replaced": replaced})

    def _skill_grant_refusal(self, name: str) -> str | None:
        """Live grant check for the private skill channel; fails closed."""
        if not self.user_id or not self.task_id:
            return "credential_not_granted"
        from istota import db
        from istota.credentials.broker.grants import check_credential_use
        try:
            with db.get_db(self.config.db_path) as database:
                return check_credential_use(database, int(self.task_id), self.user_id, name)
        except Exception:
            logger.warning("skill grant check failed task_id=%s", self.task_id, exc_info=True)
            return "credential_not_granted"

    def _refuse_brokered_credential(
        self, conn: socket.socket, name: str, request_type: str, mode: str,
    ) -> bool:
        """Audit a public value read, or refuse it when reveal enforcement is on."""
        enforce = bool(self.config and self.config.security.credential_broker.reveal_enforced)
        label = label_for_display(name)
        logger.warning(
            "credential_reveal task_id=%s type=%s name=%s mode=%s "
            "action=%s reason=credential_brokered",
            self.task_id, request_type, label, mode,
            "refused" if enforce else "would_refuse",
        )
        if not enforce:
            return False
        self._send_response(conn, {
            "error": "Credential is brokered; use a placeholder or a host-side skill (credential_brokered)",
            "reason": "credential_brokered", "name": label,
        })
        return True

    def _serve_vault_credential(
        self, conn: socket.socket, request: dict, *, trusted_skill: bool = False,
    ) -> None:
        """One shared credential, by name, under the per-attempt cap.

        Order is load-bearing: charge, then the limit, then presence. A refusal
        past the cap must be identical for a present and an absent name, or the
        cap itself becomes the enumeration oracle it exists to close — which is
        also why it names no credential at all.
        """
        name = str(request.get("name", ""))
        # Coerced like `name`, and for a sharper reason than tidiness: `in` on
        # a frozenset hashes its left operand, so an unhashable `mode` off the
        # socket — a list, a dict — would raise `TypeError` here, past the
        # charge and past the log line. The outer handler then answers nothing
        # at all, so a malformed request would be the one shape that is neither
        # counted against the cap nor recorded in the audit trail.
        # Only the private endpoint supplies this flag. A request's mode is
        # still just an audit label on the model-facing socket.
        mode = "skill" if trusted_skill else str(request.get("mode", ""))
        if mode not in VAULT_MODES:
            mode = VAULT_MODE_DEFAULT
        # Bounded and flattened before it reaches a log line. The name came off
        # a socket any process in the sandbox can speak to, so it is
        # attacker-chosen outright rather than merely KDBX-sourced, and an
        # unflattened one can forge a record in the daemon's own log.
        label = label_for_display(name)

        count, within = self._spend_vault_fetch()
        if not within:
            self._refuse_fetch_limit(conn, "vault_credential", count)
            return

        if name not in self.vault_credentials:
            logger.warning(
                "proxy_rejected task_id=%s type=vault_credential name=%s "
                "mode=%s reason=vault_credential_not_present",
                self.task_id, label, mode,
            )
            self._send_response(conn, {
                "error": f"No shared credential named {label!r}",
                "reason": "vault_credential_not_present",
                "name": label,
            })
            return

        from istota import db
        from istota.credentials import store as secrets_store
        from istota.credentials.broker.bindings import daemon_only_refusal, get_binding

        live = self.config is not None and bool(self.user_id)
        with db.get_db(self.config.db_path) if live else nullcontext(None) as database:
            if live:
                # The kind must describe the value returned, including across a
                # concurrent rotation from a plain field to a seed.
                database.execute("BEGIN IMMEDIATE")
                refusal = daemon_only_refusal(database, self.user_id, name)
                if refusal:
                    self._send_response(conn, {
                        "error": _DAEMON_ONLY_MESSAGES[refusal] + f" ({refusal})",
                        "reason": refusal, "name": label,
                    })
                    return
            if (trusted_skill and self.config is not None
                    and self.config.security.credential_broker.enabled
                    and name not in self._created_names):
                reason = self._skill_grant_refusal(name)
                if reason:
                    logger.warning(
                        "proxy_rejected task_id=%s type=vault_credential name=%s "
                        "mode=%s reason=%s", self.task_id, label, mode, reason,
                    )
                    self._send_response(conn, {
                        "error": f"Credential {label!r} is not granted to this task ({reason})",
                        "reason": reason, "name": label,
                    })
                    return
            if not trusted_skill:
                metadata = get_binding(database, self.user_id, name) if live else None
                if not (metadata or {}).get("revealable") and self._refuse_brokered_credential(
                    conn, name, "vault_credential", mode,
                ):
                    return

            reply = {"value": self.vault_credentials[name], "bound_hosts": []}
            if live:
                reply = secrets_store.get_secret(
                    self.config.db_path, self.user_id, "vault_entries", name,
                    binding=True, connection=database,
                )
                if reply is None:
                    self._send_response(conn, {"error": "Credential no longer available",
                                               "reason": "vault_credential_not_present"})
                    return

        logger.log(
            logging.INFO if mode in ("skill", "inject") else logging.WARNING,
            "vault_credential task_id=%s name=%s mode=%s count=%d",
            self.task_id, label, mode, count,
        )
        if request.get("binding") is not True:
            reply.pop("bound_hosts", None)
        self._send_response(conn, reply)

    def _serve_vault_otp(self, conn: socket.socket, request: dict) -> None:
        """Compute a bound code for a private skill read; never return the seed."""
        count, within = self._spend_vault_fetch()
        if not within:
            self._refuse_fetch_limit(conn, "vault_otp", count)
            return
        name = str(request.get("name", ""))
        label = label_for_display(name)

        def refuse(reason: str, message: str | None = None) -> None:
            logger.warning(
                "proxy_rejected task_id=%s type=vault_otp name=%s reason=%s",
                self.task_id, label, reason,
            )
            self._send_response(conn, {"error": message or f"OTP credential refused ({reason})",
                                       "reason": reason})

        # An unknown name answers as `vault_credential` does, so a misspelling
        # never reads as "this entry has no OTP", which invites `otp-set`.
        def not_present(message: str | None = None) -> None:
            refuse("vault_credential_not_present",
                   message or f"No shared credential named {label!r}")

        if not name or self.config is None or not self.user_id:
            not_present()
            return
        from istota import db
        from istota.credentials import store as secrets_store
        from istota.credentials.broker.bindings import (
            credential_groups, credential_name, get_binding, is_otp_seed,
        )
        from istota.lib.totp import TotpError, code_at, parse_otpauth, window

        with db.get_db(self.config.db_path) as database:
            # Membership, kind and the live value must describe the same seed.
            database.execute("BEGIN IMMEDIATE")
            if name in self.vault_credentials and is_otp_seed(database, self.user_id, name):
                seed_name = name
            else:
                group = credential_groups(database, self.user_id).get(name, [])
                members = [member for member in group if member in self.vault_credentials]
                # The snapshot can outlive the row: a sync mid-attempt deletes it.
                if not members and (name not in self.vault_credentials
                                    or get_binding(database, self.user_id, name) is None):
                    not_present()
                    return
                seeds = [member for member in group
                         if (get_binding(database, self.user_id, member) or {}).get("kind") == "totp"]
                if not seeds:
                    refuse("credential_has_no_otp")
                    return
                # A seed this task's snapshot lacks is not the entry having no
                # OTP, which invites a second enrollment (ISSUE-685). The entry
                # is already shared, and `list` shows the member and its grant.
                seeds = [member for member in seeds if member in self.vault_credentials]
                if not seeds:
                    refuse("credential_otp_not_granted",
                           f"Credential {label!r} has a two-factor field that is not shared "
                           "with this task; the user can grant it in Settings, Credentials "
                           "(credential_otp_not_granted)")
                    return
                if len(seeds) > 1:
                    refuse("credential_otp_ambiguous",
                           f"Credential {label!r} has more than one OTP field; pass the OTP "
                           "field name from istota-credential list (credential_otp_ambiguous)")
                    return
                seed_name = seeds[0]
            owner = credential_name(database, self.user_id, seed_name)
            if (self.config.security.credential_broker.enabled
                    and seed_name not in self._created_names):
                reason = self._skill_grant_refusal(seed_name)
                if reason:
                    refuse(reason)
                    return
            if (get_binding(database, self.user_id, seed_name) or {}).get("kind") != "totp":
                refuse("credential_otp_unusable")
                return
            seed = secrets_store.get_secret(
                self.config.db_path, self.user_id, "vault_entries", seed_name,
                binding=True, connection=database,
            )
            if seed is None:
                not_present("Credential no longer available")
                return
            hosts = seed["bound_hosts"]
            if not hosts:
                refuse("credential_unbound")
                return
            try:
                params = parse_otpauth(seed["value"])
            except TotpError as exc:
                logger.warning("credential_otp_unusable code=%s", exc.code)
                refuse("credential_otp_unusable")
                return

        now = time()
        _, end = window(params, now)
        if end - now < OTP_MIN_REMAINING_SECONDS:
            sleep(end - now)
            now = time()
            _, end = window(params, now)
        code = code_at(params, now)
        logger.info("credential_otp name=%s", label_for_display(owner))
        self._send_response(conn, {"code": code, "expires_at": end, "bound_hosts": hosts})

    def _serve_vault_entry(
        self, conn: socket.socket, request: dict, *, trusted_skill: bool = False,
    ) -> None:
        """Every field of one vault entry, charged once against the cap.

        The per-field read charges a login, its password and its URL as three
        fetches; this charges the entry once, so for an entry read the budget
        counts credentials rather than fields. Every rule that read applies to
        one field applies here to each member, and one refusal refuses the
        whole entry: a field the per-field path would withhold must not come
        back through this one. Same order as there: charge, limit, presence.

        Membership is the vault parser's (`credential_groups`), never a name
        suffix, and only members in this task's snapshot are read, so a
        withheld namespace has no entries at all.
        """
        name = str(request.get("name", ""))
        mode = "skill" if trusted_skill else str(request.get("mode", ""))
        if mode not in VAULT_MODES:
            mode = VAULT_MODE_DEFAULT
        label = label_for_display(name)

        count, within = self._spend_vault_fetch()
        if not within:
            self._refuse_fetch_limit(conn, "vault_entry", count)
            return

        def refuse(reason: str, message: str, *, named: bool = True) -> None:
            logger.warning(
                "proxy_rejected task_id=%s type=vault_entry name=%s mode=%s reason=%s",
                self.task_id, label, mode, reason,
            )
            reply = {"error": message, "reason": reason}
            if named:
                reply["name"] = label
            self._send_response(conn, reply)

        def not_present() -> None:
            refuse("vault_credential_not_present", f"No shared credential named {label!r}")

        if not name or self.config is None or not self.user_id:
            not_present()
            return
        from istota import db
        from istota.credentials import store as secrets_store
        from istota.credentials.broker.bindings import credential_groups, get_binding, get_entry_binding, is_otp_seed

        with db.get_db(self.config.db_path) as database:
            members = [m for m in credential_groups(database, self.user_id).get(name, [])
                       if m in self.vault_credentials]
        if not members:
            not_present()
            return

        with db.get_db(self.config.db_path) as database:
            # Kind, permission, values and hosts come from one view.
            database.execute("BEGIN IMMEDIATE")
            seeds = {m for m in members if is_otp_seed(database, self.user_id, m)}
            members = [m for m in members if m not in seeds]
            has_otp = any((get_binding(database, self.user_id, m) or {}).get("kind") == "totp"
                          for m in seeds)
            if trusted_skill and self.config.security.credential_broker.enabled:
                for member in members:
                    if member in self._created_names:
                        continue
                    reason = self._skill_grant_refusal(member)
                    if reason:
                        refuse(reason, f"Credential {label!r} is not granted to this task ({reason})")
                        return
            if not trusted_skill:
                hidden = [m for m in members
                          if not (get_binding(database, self.user_id, m) or {}).get("revealable")]
                if hidden and self._refuse_brokered_credential(conn, name, "vault_entry", mode):
                    return
            values = {}
            for member in members:
                value = secrets_store.get_secret(
                    self.config.db_path, self.user_id, "vault_entries", member,
                    connection=database,
                )
                if value is None:
                    refuse("vault_credential_not_present", "Credential no longer available",
                           named=False)
                    return
                values[member] = value
            hosts = (get_entry_binding(database, self.user_id, name) or {}).get("hosts", [])

        logger.log(
            logging.INFO if mode in ("skill", "inject") else logging.WARNING,
            "vault_entry task_id=%s name=%s mode=%s fields=%d count=%d",
            self.task_id, label, mode, len(values), count,
        )
        reply = {"fields": entry_fields(name, values), "bound_hosts": hosts}
        if has_otp:
            reply["otp"] = True
        self._send_response(conn, reply)

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
