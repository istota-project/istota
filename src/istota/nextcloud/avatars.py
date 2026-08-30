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
    placeholder for every user, every tick, forever.

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
) -> RemoteAvatar | NoCustomAvatar | None:
    """The user's custom Nextcloud avatar, a negative result, or nothing learned.

    `RemoteAvatar` is an image to import. `NoCustomAvatar` means Nextcloud
    answered with something it generated. `None` means nothing at all was
    learned — a 304, or a user Nextcloud does not know — and the caller should
    leave the stored row exactly as it is.

    Raises `OcsError` on a transport failure or a server error, so the caller
    can log it apart from a no-op.
    """
    endpoint = f"/index.php/avatar/{uid}/{size}"
    if not nc_configured(config):
        raise OcsError(
            "Nextcloud is not configured (nextcloud.url / nextcloud.username "
            "are unset)",
            None,
            None,
            endpoint,
        )

    url = (
        f"{nc_base_url(config)}/index.php/avatar/"
        f"{quote(uid, safe='')}/{int(size)}"
    )
    headers = {"If-None-Match": etag} if etag else {}

    try:
        resp = httpx.get(
            url, auth=nc_auth(config), headers=headers, timeout=timeout,
        )
    except Exception as e:
        raise OcsError(f"Could not reach Nextcloud: {e}", None, None, endpoint) from e

    status = getattr(resp, "status_code", None)
    if status == 304:
        return None
    if status == 404:
        # A user Nextcloud does not know about. Not a failure to log every tick
        # — a deployment can legitimately carry an istota user with no Nextcloud
        # account, and nothing here can fix that.
        return None
    if not isinstance(status, int) or not (200 <= status < 300):
        detail = (getattr(resp, "text", "") or "").strip()[:200]
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

    image = resp.content
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
