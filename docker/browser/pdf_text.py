"""A PDF the tab landed on, read as text without trusting the file (#850).

Chrome shows a PDF in its built-in viewer, a plugin frame with no DOM text, so
`/browse` and `/render` read nothing there and answered an empty `ok`, which a
caller could only take for a page with nothing on it. `read_shown_pdf` is the
other route: notice the viewer, fetch the document's bytes with the tab's
cookies, and extract the text in a child process.

**Detection asks the document, and the bytes have the last word.** The main
document's `contentType` is `application/pdf` in the viewer, and a page that is
nothing but one same-origin `<embed type="application/pdf">` counts too. A PDF
at an address that cannot be fetched again (`blob:`, `data:`) is an error, not
an empty page. Whatever the page says, the fetched bytes must start with
`%PDF-` within their first kilobyte.

**The fetch is ours, not Playwright's.** `APIRequestContext` buffers a whole
response, chunked or decompressed, before anything can measure it, so a server
could answer the refetch with gigabytes. `fetch` is a small stdlib client
instead: each hop's host is resolved and every address checked by the vendored
net guard (http or https, no credentials in the URL, public addresses only),
the connection is made to the checked address (so a DNS answer that changes
afterwards is not followed), no redirect is followed blind (each hop is checked
again, at most `MAX_REDIRECTS`), and the body is read in chunks against
`MAX_PDF_BYTES`, after gzip or deflate is undone under the same cap. Any other
content encoding is refused. The cookies are the tab's for each hop's URL
(`context.cookies`), and none it sets are kept. It leaves directly rather than
through `egress_proxy.py`, applying that proxy's rule and grant itself.

Residual: this is a second GET, not the document Chrome showed, so a PDF that
answered a POST, or a single-use link, may not come back.

**The parse.** `pdf_child.py` in a fresh interpreter (`-I -B`, an empty
environment), which sets its own address space, CPU, file-size and descriptor
limits before reading the document, under a wall-clock timeout that kills it.
The parser is pypdf, which is pure Python. The child's output is capped before
it is decoded.

**The time.** Everything here runs inside one Flask request, which the browse
watchdog ends by relaunching Chrome at `BROWSE_WATCHDOG_DEADLINE_S`, taking
every tab with it. The caller passes the request's deadline, and the fetch and
the parse share what is left of it; too little left is an error, not a gamble.

Nothing here logs a URL, a cookie, the text or an exception's message.
"""

from __future__ import annotations

import http.client
import json
import logging
import signal
import socket
import ssl
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lib.istota_net_guard import checked_addresses

log = logging.getLogger(__name__)

PDF_CONTENT_TYPE = "application/pdf"
PDF_MAGIC = b"%PDF-"
#: The spec lets a file carry junk before its header; readers look this far.
PDF_MAGIC_WINDOW = 1024

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
READ_CHUNK = 64 * 1024
MAX_PAGES = 1000
#: Characters extracted per document; the caller pages through them by offset.
MAX_EXTRACTED_CHARS = 2_000_000
#: The child's stdout, UTF-8, before decoding: four bytes a character at most.
MAX_CHILD_OUTPUT = MAX_EXTRACTED_CHARS * 4 + 64 * 1024
CHILD_MEMORY_BYTES = 768 * 1024 * 1024
CHILD_CPU_SECONDS = 45
FETCH_TIMEOUT_S = 30
PARSE_TIMEOUT_S = 50
#: Below this much request time a step is not started.
MIN_FETCH_S = 5
MIN_PARSE_S = 5

CHILD = Path(__file__).resolve().parent / "pdf_child.py"

# The page is asked once, after the caller's challenge checks have passed, the
# same window every other read in browse_api runs in.
_PDF_PROBE_JS = """() => {
  const body = document.body;
  let pdfUrl = null;
  if ((document.contentType || "").toLowerCase() === "application/pdf") {
    pdfUrl = location.href;
  } else if (body) {
    const kids = Array.from(body.children);
    const only = kids.length === 1 ? kids[0] : null;
    if (only && only.localName === "embed"
        && (only.getAttribute("type") || "").toLowerCase() === "application/pdf") {
      let src = null;
      try { src = new URL(only.src, location.href); } catch (e) { src = null; }
      pdfUrl = src && src.origin === location.origin ? src.href : location.href;
    }
  }
  return {pdfUrl, userAgent: navigator.userAgent};
}"""

MESSAGES = {
    "pdf_no_time": "The page is a PDF, and too little of the request's time was left to read it. Retry with the same URL.",
    "pdf_fetch_refused": "The page is a PDF, and fetching it was refused: it is not a public http(s) address, or it redirected to one that is not.",
    "pdf_not_fetchable": "The page is a PDF the page made itself (a blob: or data: address), which cannot be fetched again.",
    "pdf_fetch_failed": "The page is a PDF, and fetching its bytes failed.",
    "pdf_fetch_status": "The page is a PDF, and the server refused the fetch of its bytes.",
    "pdf_too_large": f"The page is a PDF larger than {MAX_PDF_BYTES // (1024 * 1024)} MiB, which is not read.",
    "pdf_not_pdf": "The page showed a PDF, but what the server returned on a second request is not a PDF.",
    "pdf_encrypted": "The page is a PDF that needs a password to read.",
    "pdf_too_complex": "The page is a PDF that could not be read within the parser's memory limit.",
    "pdf_parse_timeout": "The page is a PDF that could not be read within the parser's time limit.",
    "pdf_unreadable": "The page is a PDF that could not be parsed.",
    "pdf_limits_unavailable": "The page is a PDF, and the parser could not be confined, so it was not run.",
    "pdf_no_text": "The page is a PDF with no extractable text, probably scanned images. Read it with `browse screenshot`.",
}


@dataclass
class PdfText:
    url: str
    text: str
    title: str
    pages: int
    pages_read: int
    pages_unreadable: int
    capped: bool
    size: int


@dataclass
class PdfFailure:
    url: str
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def message(self):
        return MESSAGES.get(self.reason, MESSAGES["pdf_unreadable"])


def probe(page):
    """`(pdf_url, user_agent)` when the tab shows a PDF, else `(None, None)`.

    `pdf_url` may be any scheme; `fetch` decides what can be fetched.
    """
    try:
        answer = page.evaluate(_PDF_PROBE_JS)
    except Exception:
        return None, None
    if not isinstance(answer, dict):
        return None, None
    pdf_url = answer.get("pdfUrl")
    if not isinstance(pdf_url, str) or not pdf_url:
        return None, None
    user_agent = answer.get("userAgent")
    return pdf_url, user_agent if isinstance(user_agent, str) else None


def looks_like_pdf(data):
    return isinstance(data, (bytes, bytearray)) and PDF_MAGIC in bytes(data[:PDF_MAGIC_WINDOW])


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------

class _Refused(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to the checked address; the Host header keeps the name."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """The same, with TLS verified against the name, not the address."""

    def __init__(self, host, port, address, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _target(url, *, resolve, private_hosts, deny_address=None):
    """`(scheme, host, port, address, path)` for a fetchable URL, or raise _Refused."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _Refused("pdf_fetch_refused")
    scheme = (parts.scheme or "").lower()
    if scheme in ("blob", "data"):
        raise _Refused("pdf_not_fetchable")
    if scheme not in ("http", "https") or not parts.hostname:
        raise _Refused("pdf_fetch_refused")
    if parts.username is not None or parts.password is not None:
        raise _Refused("pdf_fetch_refused")
    port = port or (443 if scheme == "https" else 80)
    addresses, refused = checked_addresses(
        parts.hostname, port, private_hosts=private_hosts, resolve=resolve,
    )
    if refused is not None:
        raise _Refused("pdf_fetch_refused")
    if not addresses:
        raise _Refused("pdf_fetch_failed")
    if deny_address is not None and any(deny_address(address) for address in addresses):
        raise _Refused("pdf_fetch_refused")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, parts.hostname, port, addresses[0], path


def _read_capped(response, deadline):
    """The body, decoded from gzip or deflate, never more than MAX_PDF_BYTES."""
    encoding = (response.getheader("content-encoding") or "identity").strip().lower()
    if encoding in ("identity", ""):
        inflate = None
    elif encoding in ("gzip", "x-gzip", "deflate"):
        # 47 reads a gzip or a zlib header; raw deflate is what some servers
        # send for `deflate`, and is tried if that fails.
        inflate = zlib.decompressobj(47)
    else:
        raise _Refused("pdf_fetch_failed")
    out = bytearray()
    raw_deflate_tried = False
    while True:
        if time.monotonic() > deadline:
            raise _Refused("pdf_no_time")
        chunk = response.read(READ_CHUNK)
        if not chunk:
            break
        if inflate is None:
            out += chunk
        else:
            try:
                out += inflate.decompress(chunk, MAX_PDF_BYTES + 1 - len(out))
            except zlib.error:
                if encoding != "deflate" or raw_deflate_tried or out:
                    raise _Refused("pdf_fetch_failed")
                raw_deflate_tried = True
                inflate = zlib.decompressobj(-15)
                out += inflate.decompress(chunk, MAX_PDF_BYTES + 1 - len(out))
            if inflate.unconsumed_tail:
                raise _Refused("pdf_too_large")
        if len(out) > MAX_PDF_BYTES:
            raise _Refused("pdf_too_large")
    return bytes(out)


def _cookie_header(cookies_for, url):
    try:
        cookies = cookies_for(url) or []
    except Exception:
        return None
    pairs = []
    for cookie in cookies:
        name, value = cookie.get("name"), cookie.get("value")
        if isinstance(name, str) and isinstance(value, str) and name:
            if not any(ch in name + value for ch in "\r\n;"):
                pairs.append(f"{name}={value}")
    return "; ".join(pairs) or None


def fetch(url, *, deadline, user_agent=None, cookies_for=None,
          resolve=None, private_hosts=(), deny_address=None, tls_context=None):
    """The document's bytes as `(bytes, None)`, or `(None, PdfFailure)`.

    `resolve` and `private_hosts` are the net guard's, and `deny_address` refuses
    an address even for a listed name; production passes the proxy's grant and
    its loopback rule. `cookies_for(url)` answers Playwright's cookie dicts for a URL.
    """
    target = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            scheme, host, port, address, path = _target(
                target, resolve=resolve, private_hosts=private_hosts, deny_address=deny_address,
            )
            remaining = deadline - time.monotonic()
            if remaining < MIN_FETCH_S:
                raise _Refused("pdf_no_time")
            timeout = min(FETCH_TIMEOUT_S, remaining)
            if scheme == "https":
                connection = _PinnedHTTPSConnection(
                    host, port, address, timeout,
                    tls_context or ssl.create_default_context(),
                )
            else:
                connection = _PinnedHTTPConnection(host, port, address, timeout)
            headers = {
                "Accept": "application/pdf,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            }
            if user_agent:
                headers["User-Agent"] = user_agent
            cookie = _cookie_header(cookies_for, target) if cookies_for else None
            if cookie:
                headers["Cookie"] = cookie
            try:
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                status = response.status
                location = response.getheader("location")
                if 300 <= status < 400 and location:
                    target = urljoin(target, location)
                    continue
                if not 200 <= status < 300:
                    raise _Refused("pdf_fetch_status", {"http_status": status})
                declared = response.getheader("content-length")
                if declared and declared.strip().isdigit() and int(declared) > MAX_PDF_BYTES:
                    raise _Refused("pdf_too_large", {"bytes": int(declared)})
                body = _read_capped(response, min(deadline, time.monotonic() + timeout))
            except (OSError, http.client.HTTPException, ValueError) as error:
                log.info("pdf fetch failed: %s", type(error).__name__)
                raise _Refused("pdf_fetch_failed")
            finally:
                connection.close()
            if not looks_like_pdf(body):
                raise _Refused("pdf_not_pdf")
            return body, None
        raise _Refused("pdf_fetch_refused")
    except _Refused as refused:
        return None, PdfFailure(url, refused.reason, refused.detail)


# ---------------------------------------------------------------------------
# The parse
# ---------------------------------------------------------------------------

def extract_text(data, *, deadline, runner=subprocess.run):
    """Run the child over `data`. Returns its result dict, `ok` true or false."""
    remaining = deadline - time.monotonic()
    if remaining < MIN_PARSE_S:
        return {"ok": False, "error": "pdf_no_time"}
    argv = [
        sys.executable, "-I", "-B", str(CHILD),
        str(MAX_PAGES), str(MAX_EXTRACTED_CHARS),
        str(CHILD_MEMORY_BYTES), str(CHILD_CPU_SECONDS),
    ]
    try:
        finished = runner(
            argv, input=bytes(data), capture_output=True,
            timeout=min(PARSE_TIMEOUT_S, remaining), env={}, cwd="/",
            close_fds=True, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pdf_parse_timeout"}
    except OSError as error:
        log.warning("pdf child could not start: %s", type(error).__name__)
        return {"ok": False, "error": "pdf_unreadable"}
    if finished.returncode != 0:
        # SIGXCPU and SIGKILL are the time limits. Any other signal is a crash,
        # often an allocation the address-space limit refused.
        if -finished.returncode in (signal.SIGXCPU, signal.SIGKILL):
            return {"ok": False, "error": "pdf_parse_timeout"}
        return {"ok": False, "error": "pdf_unreadable"}
    output = finished.stdout or b""
    if len(output) > MAX_CHILD_OUTPUT:
        return {"ok": False, "error": "pdf_unreadable"}
    try:
        result = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"ok": False, "error": "pdf_unreadable"}
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return {"ok": False, "error": "pdf_unreadable"}
    return result


def read_pdf(pdf_url, *, deadline, user_agent=None, cookies_for=None,
             runner=subprocess.run, **fetch_options):
    """`PdfText` or `PdfFailure` for the PDF at `pdf_url`. `deadline` is monotonic."""
    data, failure = fetch(
        pdf_url, deadline=deadline, user_agent=user_agent,
        cookies_for=cookies_for, **fetch_options,
    )
    if failure is not None:
        return failure
    result = extract_text(data, deadline=deadline, runner=runner)
    if not result.get("ok"):
        reason = result.get("error")
        return PdfFailure(pdf_url, reason if reason in MESSAGES else "pdf_unreadable")
    try:
        parsed = PdfText(
            url=pdf_url,
            text=str(result["text"]),
            title=str(result.get("title") or ""),
            pages=int(result["pages"]),
            pages_read=int(result["pages_read"]),
            pages_unreadable=int(result.get("pages_unreadable") or 0),
            capped=bool(result.get("capped")),
            size=len(data),
        )
    except (KeyError, TypeError, ValueError):
        return PdfFailure(pdf_url, "pdf_unreadable")
    if not result.get("has_text"):
        return PdfFailure(pdf_url, "pdf_no_text", {"pages": parsed.pages})
    return parsed


def read_shown_pdf(page, *, deadline, runner=subprocess.run, **fetch_options):
    """None when the tab does not show a PDF, else `PdfText` or `PdfFailure`."""
    pdf_url, user_agent = probe(page)
    if pdf_url is None:
        return None
    return read_pdf(
        pdf_url, deadline=deadline, user_agent=user_agent,
        cookies_for=lambda url: page.context.cookies([url]),
        runner=runner, **fetch_options,
    )


def summary(pdf):
    """The `pdf` block a response carries: what was read, and how much of it."""
    block = {
        "url": pdf.url,
        "pages": pdf.pages,
        "pages_read": pdf.pages_read,
        "bytes": pdf.size,
        "text_capped": pdf.capped,
    }
    if pdf.pages_unreadable:
        block["pages_unreadable"] = pdf.pages_unreadable
    return block


def failure_response(failure):
    """The error body for a PDF that could not be read."""
    body = {
        "status": "error",
        "error": "pdf_document",
        "reason": failure.reason,
        "message": failure.message,
        "url": failure.url,
        "content_type": PDF_CONTENT_TYPE,
    }
    body.update(failure.detail)
    return body
