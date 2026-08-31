"""Read a user's Nextcloud avatar, and tell a real one from a generated one.

**Nextcloud always answers this endpoint with an image.** With no custom avatar
set it generates a coloured letter from the display name, which is Nextcloud's
version of the initial chip the web UI already renders. Importing that would
replace our own chip with a picture indistinguishable at the call site from a
real photograph, and nothing downstream could ever tell them apart again. The
only thing that distinguishes them is a response header, so this module's whole
job is reading it correctly and refusing to guess when it is not there.

`_http.py` has no plain-GET helper to build on — `ocs_request_full` hardcodes
`/ocs/v2.php` into the URL and `dav_request` raises `OcsError` with WebDAV
wording — so this builds its own request against `nc_base_url` with `nc_auth`,
which is what the two existing ad-hoc `index.php` callers already do
(`web_app.py`, `web_tokens.py`). It raises `OcsError` like everything else in
the package, so the import job can log a transport failure apart from a no-op.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from ..config import Config
from ._http import DEFAULT_TIMEOUT, OcsError, nc_auth, nc_base_url, nc_configured

logger = logging.getLogger("istota.nextcloud.avatars")

# **Observed, not assumed.** Against a real `nextcloud:30-apache` on the `full`
# testbed profile, `GET /index.php/avatar/{uid}/{size}` answered 200 with
# `X-NC-IsCustomAvatar: 0` for a user who had set no picture and
# `X-NC-IsCustomAvatar: 1` for one whose avatar had just been set through
# Nextcloud's own `IAvatar::set` — that spelling, that casing, those values, and
# an `ETag` on both. `tests/full/test_nextcloud_avatar_header.py` is what holds
# it: every unit test in `tests/test_nextcloud_avatar_import.py` scripts the
# header using this same constant on both sides, so a wrong name passes all of
# them while the import silently classifies every user on the deployment as
# having no custom avatar.
CUSTOM_AVATAR_HEADER = "X-NC-IsCustomAvatar"

# Read generously rather than requiring `"1"`. The header is an int today, but a
# server behind a rewriting proxy — or a future Nextcloud — spelling it `true`
# should import the picture rather than silently record a negative, and the
# error made by being generous here is one a user can correct by removing their
# Nextcloud avatar. Anything not in this set is falsy, including the empty
# string a header stripped to nothing leaves behind.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The one size we ask for. `avatars.AVATAR_EDGE` is not imported: this module
# would then pull the store — and Pillow's neighbourhood — onto the import graph
# of a pure HTTP leaf, and the two numbers agreeing is a convenience rather than
# a contract. Nextcloud resizes to whatever is asked and `normalize` re-crops
# regardless, so a disagreement costs a resample, not a defect.
DEFAULT_SIZE = 192

# What one avatar may weigh before the fetch gives up. A ceiling rather than a
# guess at what Nextcloud sends: the answer to this request goes into RAM on a
# daemon that instruments its own memory pressure, once per configured user per
# tick, and the spec's D15 states the rule for the upload route in as many words
# — the cap has to be enforced before the body exists in memory, not on `len()`
# afterwards. Nothing makes an image from Nextcloud different. The caller passes
# `web.max_avatar_kb`; this default is only for a caller that names none.
DEFAULT_MAX_BYTES = 4096 * 1024

# How much longer than one socket operation the whole transfer may take.
# `timeout` is httpx's per-operation bound, so a peer emitting one byte just
# inside it holds the call open for as long as it likes — and this runs on the
# thread `_spawn_background_check` owns, which refuses to start a second run
# while the first is alive. One drip-feeding response would therefore disable
# the import for the life of the process, silently, with a skip logged every six
# hours. Six socket timeouts is generous for a 10 KB picture and finite.
TRANSFER_DEADLINE_MULTIPLE = 6.0

# How much of a non-2xx body goes into the error message.
_ERROR_SNIPPET_BYTES = 2048

# The read granularity, and therefore the overshoot the ceiling permits: peak
# memory is `max_bytes` plus at most one of these.
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class RemoteAvatar:
    """A user-set Nextcloud avatar: bytes to import, and the ETag to remember."""

    image: bytes
    etag: str


@dataclass(frozen=True)
class NoCustomAvatar:
    """Nextcloud answered, and the answer was a picture it generated itself.

    Worth recording rather than discarding: with the ETag stored, the next tick
    revalidates with `If-None-Match` and gets a 304 instead of re-downloading a
    placeholder for every user, every tick, forever. That applies to the
    `header_seen=True` case only — the caller deliberately stores no validator
    when the header was absent, because a 304 on the tick after would record
    "unobserved", which reads as an OK, and the `absent` verdict would erase
    itself one interval after it was written. `etag` is ignored in that case.

    `header_seen` says whether that judgement came from the server or from this
    module degrading safely. A tick that never saw the header is a deployment
    where nothing will ever be imported, which is a fact an operator needs and
    which no count of stored rows can distinguish from "nobody has set one".
    """

    etag: str
    header_seen: bool = True


# One line per process, not one per user per tick: on a deployment whose
# Nextcloud does not send the header this fires for every configured user every
# six hours, which buries the log it is meant to stand out in.
_absent_header_logged = False


def reset_absent_header_log() -> None:
    """Re-arm the once-per-process log line. For tests, which share a process."""
    global _absent_header_logged
    _absent_header_logged = False


def fetch_avatar(
    config: Config,
    uid: str,
    *,
    size: int = DEFAULT_SIZE,
    etag: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> RemoteAvatar | NoCustomAvatar | None:
    """The user's custom Nextcloud avatar, a negative result, or nothing learned.

    `RemoteAvatar` is an image to import. `NoCustomAvatar` means Nextcloud
    answered with something it generated. `None` means nothing at all was
    learned — a 304, or a user Nextcloud does not know — and the caller should
    leave the stored row exactly as it is.

    Raises `OcsError` on a transport failure or a server error, so the caller
    can log it apart from a no-op.

    **The body is streamed under two bounds, and only where there is one worth
    reading.** A generated avatar is decided from the response header alone, so
    the placeholder Nextcloud drew is never pulled down at all; a picture worth
    importing is read chunk by chunk and abandoned the moment it passes
    `max_bytes` or `TRANSFER_DEADLINE_MULTIPLE` socket timeouts. Both bounds are
    needed and neither is the other — the byte cap is what stops the daemon
    buffering an arbitrary body once per configured user per tick, and the
    deadline is what stops a peer that drips one byte inside every socket
    timeout from holding this call, and with it the whole import, open for the
    life of the process (`_spawn_background_check` will not start a second run
    while the first is alive, so a hung fetch is not a slow tick, it is no more
    ticks).

    Redirects are deliberately not followed. This request carries the bot's app
    password as HTTP Basic on every hop, so following a 3xx would hand that
    credential to whatever the redirect names; a redirect here means the
    configured URL is wrong, and saying so is the right answer.
    """
    url = (
        f"{nc_base_url(config)}/index.php/avatar/"
        f"{quote(uid, safe='')}/{int(size)}"
    )
    # Built from the same quoted, coerced values the URL is. This string reaches
    # a log line and a per-user outcome record, and a uid carrying a newline
    # would otherwise break one entry into two.
    endpoint = f"/index.php/avatar/{quote(uid, safe='')}/{int(size)}"
    if not nc_configured(config):
        raise OcsError(
            "Nextcloud is not configured (nextcloud.url / nextcloud.username "
            "are unset)",
            None,
            None,
            endpoint,
        )

    # **`identity`, and it is what makes the byte ceiling mean what it says.**
    # httpx offers `gzip, deflate, br, zstd` by default and decodes each raw
    # network read before `read_bounded` can weigh it, so a compressed body
    # amplifies inside one chunk and the ceiling sees the decoded size only
    # after it is already in memory. An avatar is an already-compressed raster;
    # content-encoding buys nothing here and costs the guarantee.
    headers = {"Accept-Encoding": "identity"}
    if etag:
        headers["If-None-Match"] = etag
    deadline = time.monotonic() + max(float(timeout), 0.0) * TRANSFER_DEADLINE_MULTIPLE

    try:
        with httpx.stream(
            "GET", url,
            auth=nc_auth(config),
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        ) as resp:
            return _classify(
                resp, uid=uid, endpoint=endpoint, url=url,
                max_bytes=max_bytes, deadline=deadline,
            )
    except OcsError:
        raise
    except Exception as e:
        raise OcsError(f"Could not reach Nextcloud: {e}", None, None, endpoint) from e


def _classify(
    resp,
    *,
    uid: str,
    endpoint: str,
    url: str,
    max_bytes: int,
    deadline: float,
) -> RemoteAvatar | NoCustomAvatar | None:
    """Read the answer, downloading a body only where one is worth having."""
    status = getattr(resp, "status_code", None)
    if status == 304:
        return None
    if status == 404:
        # A user Nextcloud does not know about. Not a failure to log every tick
        # — a deployment can legitimately carry an istota user with no Nextcloud
        # account, and nothing here can fix that.
        return None
    if not isinstance(status, int) or not (200 <= status < 300):
        try:
            raw_detail = read_bounded(
                resp,
                max_bytes=_ERROR_SNIPPET_BYTES,
                deadline=deadline,
                endpoint=endpoint,
                status=status if isinstance(status, int) else None,
                truncate=True,
            ).decode("utf-8", "replace")
        except Exception:
            # The status is the useful half and it is already in hand. Letting a
            # reset mid-error-body propagate turned a 500 or a 401 into "could
            # not reach Nextcloud" with no status at all, which reads to an
            # operator as a connectivity fault rather than a rejection.
            raw_detail = ""
        # Whitespace collapsed, not just stripped. This lands in a log line as
        # `avatar_import_failed user=%s err=%s`, and an interior newline breaks
        # one entry into two — the same hazard the quoting of `endpoint` above
        # exists to prevent, except this text comes off the wire.
        detail = " ".join(raw_detail.split())[:200]
        message = f"HTTP {status} from Nextcloud"
        if detail:
            message = f"{message}: {detail}"
        raise OcsError(message, status if isinstance(status, int) else None,
                       None, endpoint)

    observed_etag = (resp.headers.get("ETag") or "").strip()
    raw = resp.headers.get(CUSTOM_AVATAR_HEADER)

    if raw is None:
        _log_absent_header(url)
        return NoCustomAvatar(etag=observed_etag, header_seen=False)

    if raw.strip().lower() not in _TRUTHY:
        return NoCustomAvatar(etag=observed_etag)

    image = read_bounded(
        resp, max_bytes=max_bytes, deadline=deadline,
        endpoint=endpoint, status=status,
    )
    if not image:
        # The server said there is a custom avatar and sent no picture. Recording
        # a negative here would write a lie into the store *and* park an ETag
        # beside it that stops the next tick looking again, so this is a failure
        # rather than a quiet degradation.
        raise OcsError(
            f"Nextcloud reported a custom avatar for {uid!r} and sent an empty body",
            status,
            None,
            endpoint,
        )
    return RemoteAvatar(image=image, etag=observed_etag)


def read_bounded(
    resp,
    *,
    max_bytes: int,
    deadline: float,
    endpoint: str,
    status: int | None,
    truncate: bool = False,
) -> bytes:
    """Read a response body under a byte ceiling and a wall-clock deadline.

    Public because it is the half of `fetch_avatar` worth testing on its own: a
    deadline is otherwise only reachable by waiting for one, and a test that
    waits is a test nobody runs.

    `truncate` picks what happens at the ceiling, and the two callers want
    opposite things. An oversized *image* is a refusal — importing a prefix of a
    JPEG would store a corrupt picture under a hash that claims to identify it.
    An oversized *error body* is not the subject; it is context for a message
    that is about to be truncated to 200 characters anyway, so it stops reading
    and keeps what it has.

    The deadline is checked before each chunk is appended rather than only at
    the end, because the failure it exists for is a stream that never ends.
    """
    chunks: list[bytes] = []
    total = 0
    # An explicit chunk size rather than httpx's default of "whatever one
    # network read decoded to". The ceiling is checked between chunks, so the
    # real bound on peak memory is `max_bytes` plus one chunk, and leaving the
    # chunk unbounded left that second term unbounded too.
    for chunk in resp.iter_bytes(chunk_size=_CHUNK_BYTES):
        if time.monotonic() > deadline:
            raise OcsError(
                "Nextcloud took too long to send the body "
                f"(gave up after {total} bytes)",
                status,
                None,
                endpoint,
            )
        total += len(chunk)
        if total > max_bytes:
            if truncate:
                chunks.append(chunk)
                break
            raise OcsError(
                f"the avatar is larger than the {max_bytes}-byte ceiling",
                status,
                None,
                endpoint,
            )
        chunks.append(chunk)
    return b"".join(chunks)[:max_bytes] if truncate else b"".join(chunks)


def _log_absent_header(url: str) -> None:
    global _absent_header_logged
    if _absent_header_logged:
        return
    _absent_header_logged = True
    logger.warning(
        "nextcloud_avatar_header_absent header=%s url=%s — this Nextcloud does "
        "not say whether an avatar is user-set or generated, so nothing will be "
        "imported. Every user would otherwise get Nextcloud's coloured-letter "
        "placeholder as their profile picture. Set [web] "
        "avatar_import_from_nextcloud = false to stop asking.",
        CUSTOM_AVATAR_HEADER, url,
    )
