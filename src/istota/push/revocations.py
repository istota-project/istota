"""The relay revocation queue's sweep: routes of removed or re-enrolled devices, revoked at the relay.

`devices.delete_device`, `devices.delete_session_devices` and a
re-registration that replaces a route write a `push_relay_revocations` row in
the transaction that changes the device, so a logout or a removal never waits
on the relay and never forgets a route. This sweep runs on the push-dispatch
gate's tick, whatever the delivery mode, since a route queued before a switch
to direct delivery is still live at its relay.

Each tick takes up to `MAX_PER_SWEEP` due rows, oldest first, and sends each
one `relay_client.revoke` with the capability its file holds, to the relay URL
stored on the row. A row is dropped, its file deleted first, when the relay
answers 204 or 401, when it is `REVOCATION_LIFETIME_SECONDS` old, or when it
cannot be sent at all (no usable URL or capability); the route then lapses
with its lease. Any other outcome backs the row off by `backoff`, or by the
relay's `retry_after` when that is longer, and ends the tick: a relay that is
down or limiting this source would refuse the rest too, and each attempt can
cost a whole timeout. This is the only durable retry in push; alerts have none.

No write transaction spans a request, and the table holds no capability.
"""

import logging
import threading
import time

from istota import db
from istota.config import Config, normalize_relay_url
from istota.push import devices, relay_client

logger = logging.getLogger(__name__)

MAX_PER_SWEEP = 20
BASE_DELAY_SECONDS = 30
MAX_DELAY_SECONDS = 6 * 3600

_lock = threading.Lock()


def backoff(attempts: int) -> int:
    """Seconds to wait after a failure, given the failures before it: 30 s doubling to 6 h."""
    if attempts >= 20:
        return MAX_DELAY_SECONDS
    return min(2 ** attempts * BASE_DELAY_SECONDS, MAX_DELAY_SECONDS)


def _usable_url(value: str) -> str | None:
    try:
        return normalize_relay_url(value) if value else None
    except ValueError:
        return None


def _drop(config: Config, directory, row: dict, why: str) -> None:
    # The file goes first: a row left behind by a failed delete finds no
    # capability next tick and is dropped then, so no capability outlives its row.
    devices.remove_relay_capability(directory, row["credential_ref"])
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM push_relay_revocations WHERE route_id = ?", (row["route_id"],))
    if why in {"expired", "unsendable"}:
        logger.warning("Push relay revocation dropped (%s) after %s attempt(s); the route lapses with its lease",
                       why, row["attempts"])
    else:
        logger.info("Push relay route revocation finished: %s", why)


def run_relay_revocations(config: Config) -> None:
    """One bounded pass over the due revocations; never raises."""
    if not _lock.acquire(blocking=False):
        return
    try:
        now = int(time.time())
        with db.get_db(config.db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT route_id, credential_ref, relay_url, attempts, expires_at FROM push_relay_revocations"
                " WHERE next_attempt_at <= ? ORDER BY next_attempt_at, created_at LIMIT ?",
                (now, MAX_PER_SWEEP),
            )]
        if not rows:
            return
        directory = devices.relay_credentials_dir(config)
        for row in rows:
            if now >= row["expires_at"]:
                _drop(config, directory, row, "expired")
                continue
            url = _usable_url(row["relay_url"])
            capability = devices.read_relay_capability(directory, row["credential_ref"])
            if url is None or capability is None or not relay_client._is_id(row["route_id"]):
                _drop(config, directory, row, "unsendable")
                continue
            result = relay_client.revoke(config, row["route_id"], capability, relay_url=url)
            if result.done:
                _drop(config, directory, row, result.reason)
                continue
            retry_after = min(result.retry_after or 0, devices.REVOCATION_LIFETIME_SECONDS)
            delay = max(backoff(row["attempts"]), retry_after)
            with db.get_db(config.db_path) as conn:
                conn.execute(
                    "UPDATE push_relay_revocations SET attempts = attempts + 1, next_attempt_at = ?,"
                    " last_error = ? WHERE route_id = ?",
                    (now + delay, f"{result.status} {result.reason}", row["route_id"]),
                )
            break
    except Exception as exc:
        logger.warning("Push relay revocation sweep failed: %s", type(exc).__name__)
    finally:
        _lock.release()
