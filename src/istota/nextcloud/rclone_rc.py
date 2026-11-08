"""The rclone mount's remote control, as the daemon may use it (#939).

On the dedicated VM, `mount-nextcloud.service` runs rclone with `--rc` on a
Unix socket. The rc can do anything to the bot's whole Nextcloud tree
(`operations/*`, `core/command`), so the daemon sends three calls and nothing
else: `vfs/forget` and `vfs/refresh`, which only drop or reload rclone's own
directory cache, and `rc/noop`, which doctor sends to check the socket answers
and authenticates.

Where it is, inside the daemon container: `docker/docker-compose.rclone-rc.yml`
binds the socket's host directory (`/srv/app/istota/rclone-rc`, 0700, uid
10001) at `RC_DIR/rc` and the credentials file (`/srv/app/istota/rclone-rc.env`,
root's, 0640 group 10001, which `host/provision.sh` writes) at `RC_DIR/rc.env`,
and sets `RC_ENV_VAR`. Nothing else names the directory, and
`sandbox.plan.plan_masks` covers `RC_DIR` with an empty tmpfs in every sandbox,
so no task reaches it whatever `sandbox_ro_paths` says.

Never raises: every call answers `RcResult`, and a failure carries a fixed
reason, never the response body or the password.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from istota.lib.unix_connect import connect_no_follow

logger = logging.getLogger("istota.nextcloud.rclone_rc")

#: Set (to any non-empty value) by the compose overlay when the rc is bound in.
RC_ENV_VAR = "ISTOTA_RCLONE_RC"
RC_DIR = Path("/run/istota-rclone")
RC_SOCKET = RC_DIR / "rc" / "rc.sock"
RC_CREDENTIALS = RC_DIR / "rc.env"

#: The three calls the daemon may make. Anything else is refused here.
ALLOWED_CALLS = frozenset({"vfs/forget", "vfs/refresh", "rc/noop"})

TIMEOUT = 10.0
_CREDENTIALS_MAX_BYTES = 4096


@dataclass(frozen=True)
class RcResult:
    ok: bool
    reason: str = ""  # "", "not_configured", "no_credentials", "unreachable",
    #                   "unauthorized", "refused_call", "http_<status>", "bad_answer"
    body: dict | None = None


def configured() -> bool:
    """Whether this process was given the rc at all."""
    return bool(os.environ.get(RC_ENV_VAR))


def read_credentials(path: Path = RC_CREDENTIALS) -> tuple[str, str] | None:
    """`RCLONE_RC_USER` and `RCLONE_RC_PASS` from the env file rclone reads.

    The same file systemd hands rclone through `EnvironmentFile=`, so there is
    one copy of the password. Opened with no link followed and bounded.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_CREDENTIALS_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _CREDENTIALS_MAX_BYTES:
        return None
    values: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    user = values.get("RCLONE_RC_USER", "")
    password = values.get("RCLONE_RC_PASS", "")
    if not user or not password:
        return None
    return user, password


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("rclone", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            connect_no_follow(sock, self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def call(
    command: str,
    params: dict[str, str] | None = None,
    *,
    authenticate: bool = True,
    socket_path: Path = RC_SOCKET,
    credentials_path: Path = RC_CREDENTIALS,
    timeout: float = TIMEOUT,
) -> RcResult:
    """POST one rc command. `authenticate=False` is doctor's check that the rc
    refuses a caller without the password."""
    if command not in ALLOWED_CALLS:
        return RcResult(False, "refused_call")
    headers = {"Content-Type": "application/json"}
    if authenticate:
        creds = read_credentials(credentials_path)
        if creds is None:
            return RcResult(False, "no_credentials")
        token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    conn = _UnixHTTPConnection(str(socket_path), timeout)
    try:
        conn.request("POST", f"/{command}", body=json.dumps(params or {}), headers=headers)
        response = conn.getresponse()
        raw = response.read(1024 * 1024)
        status = response.status
    except (OSError, http.client.HTTPException):
        return RcResult(False, "unreachable")
    finally:
        conn.close()
    if status in (401, 403):
        return RcResult(False, "unauthorized")
    if status != 200:
        return RcResult(False, f"http_{status}")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return RcResult(False, "bad_answer")
    if not isinstance(body, dict):
        return RcResult(False, "bad_answer")
    return RcResult(True, body=body)


def _numbered(key: str, paths: list[str]) -> dict[str, str]:
    """rclone reads every parameter starting with `dir` as one directory."""
    return {(key if i == 0 else f"{key}{i + 1}"): p for i, p in enumerate(paths)}


def forget_dirs(paths: list[str]) -> RcResult:
    """Drop each directory and everything under it from rclone's cache. The
    next read lists it again from Nextcloud, lazily."""
    if not paths:
        return RcResult(True)
    if not configured():
        return RcResult(False, "not_configured")
    return call("vfs/forget", _numbered("dir", paths))


def forget_all() -> RcResult:
    """Drop rclone's whole directory cache: `vfs/forget` with nothing named."""
    if not configured():
        return RcResult(False, "not_configured")
    return call("vfs/forget", {})


def refresh_dirs(paths: list[str]) -> RcResult:
    """List each directory again now, keeping what is cached below it, one
    call each and in the order given (parents first), since rclone finds a
    directory through its parent's listing and walks one call's parameters in
    no fixed order. The root is an empty parameter list, which is how rclone
    names it. Stops at the first failure."""
    if not paths:
        return RcResult(True)
    if not configured():
        return RcResult(False, "not_configured")
    result = RcResult(True)
    for path in paths:
        result = call("vfs/refresh", {"dir": path} if path else {})
        if not result.ok:
            return result
        answers = result.body.get("result") if isinstance(result.body, dict) else None
        if isinstance(answers, dict) and any(v != "OK" for v in answers.values()):
            return RcResult(False, "bad_answer", result.body)
    return result
