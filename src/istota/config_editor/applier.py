"""Whether this install can apply a web config change, and handing one over.

The web process cannot write `config/` (it is bound read-only into every
service) or restart the stack. On the dedicated VM a root-side host agent does
both: `istota-stack config-apply`, started by a systemd path unit watching the
trigger file this module writes. The compose override that binds the requests
directory into the web container also sets `ISTOTA_CONFIG_APPLIER=host`, so
the variable and a writable directory together are what "an applier exists"
means. Anywhere else (`istota serve` on macOS, a deprecated standalone install)
the editor is read-only, and `detect` says why.

The trigger file carries only a request id; the host reads nothing else from
it, and takes the request itself from the database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

APPLIER_ENV = "ISTOTA_CONFIG_APPLIER"
REQUESTS_DIR_ENV = "ISTOTA_CONFIG_REQUESTS_DIR"
DEFAULT_REQUESTS_DIR = "/data/requests"
TRIGGER_NAME = "config-apply"

NO_APPLIER_REASON = (
    "This install has no config applier; changes are made with "
    "`istota-stack apply` on the host."
)
NOT_RESTARTED_REASON = (
    "The config applier is configured but its requests directory is missing or "
    "not writable. Restart the stack on the host (`istota-stack up`) after "
    "`provision.sh` has installed the config applier."
)

#: What the view shows when nobody asked `detect`.
NO_APPLIER = {"kind": "none", "available": False, "reason": NO_APPLIER_REASON}


def requests_dir(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get(REQUESTS_DIR_ENV, "").strip() or DEFAULT_REQUESTS_DIR)


def detect(config: Any = None, *, environ: Mapping[str, str] | None = None) -> dict:
    """`{"kind": "host" | "none", "available": bool, "reason": str}`.

    `host` needs both the variable the compose override sets and a writable
    requests directory; the variable alone means the stack has not been
    restarted since provisioning added the override.
    """
    environ = os.environ if environ is None else environ
    if environ.get(APPLIER_ENV, "").strip() != "host":
        return dict(NO_APPLIER)
    directory = requests_dir(environ)
    if directory.is_symlink() or not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
        return {"kind": "none", "available": False, "reason": NOT_RESTARTED_REASON}
    return {"kind": "host", "available": True, "reason": ""}


def trigger(request_id: int, *, environ: Mapping[str, str] | None = None) -> None:
    """Write `request_id` to the trigger file the host's path unit watches.

    Created or truncated, mode 0600, never through a link. Raises `OSError`;
    the host's timer takes a pending request within five minutes regardless.
    """
    path = requests_dir(environ) / TRIGGER_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{int(request_id)}\n".encode("ascii"))
    finally:
        os.close(fd)


def read_managed(config_dir: Path) -> dict:
    """`config/managed.json`, read fail-closed: a damaged file manages everything."""
    from istota import apply

    return apply.read_managed(Path(config_dir), fail_closed=True)
