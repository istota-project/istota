"""One credentialed GET to a fixed URL: no redirect followed, no proxy consulted.

Shared by the two requests istota sends to `api.anthropic.com` with a
credential in a header: the subscription usage poll (`usage/subscription.py`)
and Anthropic's model list (`llm/discovery.py`). Both build their URL from a
module constant, and both need the same two refusals:

- **No redirect.** `urllib`'s default redirect handler copies every header but
  the content ones onto the new request, so a 30x to another host would re-send
  the credential there. Refusing outright needs no host comparison to get right;
  the 30x comes back as the status it was.
- **No proxy from the environment.** `ProxyHandler({})` keeps the request off
  whatever `http_proxy` the daemon's environment holds. On a deployment that
  reaches the internet only through `HTTPS_PROXY`, both requests therefore fail
  as a transport error, which their callers report as one.

`describe_failure` is the other shared half: a transport failure named by its
exception class alone, never its message, which can carry a header value.

stdlib-only leaf, imports nothing from istota.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping

#: The most of a response body either caller reads.
DEFAULT_MAX_BODY_BYTES = 1 << 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so `urllib` raises the 30x as an `HTTPError`."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """An opener that follows no redirect and consults no proxy variable."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def get(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    *,
    max_body: int = DEFAULT_MAX_BODY_BYTES,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """One GET, as `(status, body, headers)`.

    An `HTTPError` is a response, not a transport failure, so it is turned back
    into a status: that is what routes a 401 or 403 into the caller's status
    branch and reports a refused redirect as the 30x it was. A transport
    failure raises.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    director = opener if opener is not None else build_opener()
    try:
        with director.open(request, timeout=timeout) as response:
            return int(response.status), response.read(max_body), dict(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_body)
        except Exception:  # noqa: BLE001 - the status is what was needed
            body = b""
        try:
            response_headers = dict(exc.headers or {})
        except Exception:  # noqa: BLE001 - same
            response_headers = {}
        return int(exc.code), body, response_headers


def describe_failure(exc: BaseException) -> str:
    """A transport failure as its exception class name and nothing else."""
    return type(exc).__name__
