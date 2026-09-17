"""The pairing relay file: the QR's only channel between the two processes.

A pairing QR is the full-account credential for one WhatsApp number — anything
that scans it is linked as a device — so where it may travel is the whole of
this module's design.

**Not the database.** `db_backup` snapshots the framework database into a dated
directory on the Nextcloud mount, so a QR live at snapshot time would be
captured in a durable, replicated backup of a full-account credential. Neither
`istota_kv`, for the same reason: that table is in the snapshot.

**A 0600 file, published atomically, unlinked as soon as the window closes.**
`brain_availability.py` is the same arrangement one subsystem over — the
scheduler writes, the web process reads, `{db_path.parent}`, 0600, atomic, an
absolute `expires_at` the reader validates — and this follows its shape. The
publish is `atomic_write.write_bytes_atomic`, never a hand-rolled temp file:
the staging name has to be unique per *call* rather than per process (that
module's docstring is the record of why), and this is a re-entered writer with
a watchdog task beside it, which is exactly the shape that bit
`health/documents.py`. `write_bytes_atomic` also applies the mode with
`os.fchmod` on the descriptor *before* the content is written, so the file
never exists at its final name carrying `mkstemp`'s default.

`O_NOFOLLOW` is deliberately not claimed for the publish: `os.replace` does not
follow the final component, which is `atomic_write`'s own stated reason for
needing no such care.

**A leaf, because the reader runs in the web process.** Importing the bridge
there would pull in the sidecar supervisor, the asyncio primitives and the
adapter. The convention is `.claude/rules/leaf-modules.md`'s: stdlib only
(plus the neighbouring `atomic_write` leaf), paths are parameters, never
raises. A read failure is `None`, which the endpoint renders as "no pairing in
progress"; a write failure is `False` and a log line carrying the errno and the
path — **never the payload, and never with `exc_info`**, because a traceback
frame here holds the credential.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from pathlib import Path

from ...atomic_write import write_bytes_atomic

logger = logging.getLogger(__name__)

#: The mode the relay is created at. Not narrowed to afterwards — see the
#: module docstring.
RELAY_MODE = 0o600

#: The six states a window can publish. `awaiting_sidecar` is what opening a
#: window writes; `awaiting_scan` is the only one carrying a payload.
STATE_AWAITING_SIDECAR = "awaiting_sidecar"
STATE_AWAITING_SCAN = "awaiting_scan"
STATE_SIDECAR_ABSENT = "sidecar_absent"
STATE_PAIRED = "paired"
STATE_EXPIRED = "expired"
STATE_FAILED = "failed"

STATES = frozenset(
    {
        STATE_AWAITING_SIDECAR,
        STATE_AWAITING_SCAN,
        STATE_SIDECAR_ABSENT,
        STATE_PAIRED,
        STATE_EXPIRED,
        STATE_FAILED,
    }
)

#: Terminal states: a window in one of these is over, however it got there.
TERMINAL_STATES = frozenset({STATE_PAIRED, STATE_EXPIRED, STATE_FAILED})


def build_payload(
    *,
    window_id: str,
    state: str,
    expires_at: float,
    qr: str | None = None,
    qr_seq: int = 0,
    message: str = "",
    updated_at: float | None = None,
) -> dict:
    """The relay's schema, in the one module both processes import.

    **`qr` is dropped unless the state is `awaiting_scan`.** A caller that
    passes one anyway has the state wrong, and publishing a credential under a
    state the reader will not render it for is a file holding a QR nothing ever
    looks at — which is the worst of both. The rule lives here rather than at
    the writer so the reader can apply the same one on the way back.

    `expires_at` is an **absolute** wall-clock instant, never a countdown:
    `subscription_usage`'s `resets_at` is the same rule, and a derived deadline
    would move under a reader whose clock differs from the writer's.
    """
    payload: dict = {
        "window_id": str(window_id),
        "state": str(state),
        "expires_at": float(expires_at),
        "qr_seq": int(qr_seq),
        "message": str(message),
        "updated_at": float(time.time() if updated_at is None else updated_at),
    }
    if state == STATE_AWAITING_SCAN and isinstance(qr, str) and qr:
        payload["qr"] = qr
    return payload


def write_relay(path: Path | str, payload: dict) -> bool:
    """Publish `payload` at `path`, 0600 and atomically. Never raises.

    Returns whether the file now holds it. A failure logs the errno and the
    path and nothing else — not the payload, and not a traceback.
    """
    target = Path(path)
    try:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "whatsapp.pairing.relay_unserializable path=%s reason=%s",
            target, type(exc).__name__,
        )
        return False
    try:
        write_bytes_atomic(target, data, mode=RELAY_MODE)
    except OSError as exc:
        logger.warning(
            "whatsapp.pairing.relay_unwritten path=%s errno=%s (%s): the "
            "pairing code cannot be shown to the admin UI",
            target, exc.errno, errno.errorcode.get(exc.errno or 0, "unknown"),
        )
        return False
    except Exception as exc:  # noqa: BLE001 — never raises, per the contract
        logger.warning(
            "whatsapp.pairing.relay_unwritten path=%s reason=%s",
            target, type(exc).__name__,
        )
        return False
    return True


def read_relay(
    path: Path | str,
    *,
    expected_window_id: str | None = None,
    now: float | None = None,
) -> dict | None:
    """The relay's current contents, or `None` for anything unusable.

    Two optional filters, because the caller holding the durable request row is
    the one that knows which window is current: a payload whose `window_id`
    does not match `expected_window_id` is a leftover from an earlier window
    and is ignored rather than rendered, and one whose absolute deadline has
    passed is ignored the same way. A torn file cannot be observed — the
    publish is an `os.replace` of a fully written file — but a file that fails
    to parse anyway reads as nothing.
    """
    target = Path(path)
    try:
        payload = json.loads(target.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    window_id = payload.get("window_id")
    state = payload.get("state")
    if not isinstance(window_id, str) or not window_id:
        return None
    if state not in STATES:
        return None
    if expected_window_id is not None and window_id != expected_window_id:
        return None
    try:
        expires_at = float(payload["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if now is not None and expires_at <= now:
        return None
    if state != STATE_AWAITING_SCAN:
        # Defence in depth on the way back as well as on the way out: a state
        # the UI will not render a code for must not hand one to the caller.
        payload.pop("qr", None)
    return payload


def clear_relay(path: Path | str) -> bool:
    """Remove the relay. Never raises.

    Returns whether nothing is at the path afterwards, so an absent file is
    success — the caller's question is "is the credential gone", and it is.
    """
    target = Path(path)
    try:
        os.unlink(target)
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning(
            "whatsapp.pairing.relay_unremoved path=%s errno=%s (%s): a pairing "
            "code may still be readable on disk",
            target, exc.errno, errno.errorcode.get(exc.errno or 0, "unknown"),
        )
        return False
    return True
