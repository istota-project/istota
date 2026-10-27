"""A PDF the browser lands on: detection, the guarded fetch, the confined parse, the endpoints (#850).

`/browse` and `/render` used to answer an empty `ok` on a PDF, because Chrome
draws one in a plugin frame with no DOM text. The parse runs the real child
over a real (hand-built) PDF. The fetch runs over a real socket to a loopback
server, admitted through the net guard's own listed-name rule, which nothing
in production passes. Chrome's own viewer is the image tier's to witness.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import signal
import socket
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_browser_user_scope import api, post  # noqa: F401 - the Flask fixture

BROWSER = Path(__file__).resolve().parents[1] / "docker" / "browser"
PDF_URL = "https://93.184.216.34/programme.pdf"


def make_pdf(pages, title=None) -> bytes:
    """A minimal valid PDF whose pages each draw one line of text."""
    objects = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    pages_id = len(objects) + 1 + 2 * len(pages)
    kids = []
    for line in pages:
        text = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content)
        ))
    assert add(
        b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
        + b"] /Count %d >>" % len(kids)
    ) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info = add(b"<< /Title (" + title.encode("latin-1") + b") >>") if title else None

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if info:
        trailer += b" /Info %d 0 R" % info
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


@pytest.fixture
def pdf_text(monkeypatch):
    monkeypatch.syspath_prepend(str(BROWSER))
    for name in ("lib", "lib.istota_net_guard"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("_pdf_text", BROWSER / "pdf_text.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_pdf_text", module)
    spec.loader.exec_module(module)
    return module


def _later(seconds=120):
    return time.monotonic() + seconds


class _Server:
    """A real HTTP server on loopback, answering from a table of handlers by path."""

    def __init__(self):
        routes = self.routes = {}
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                route = routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    route(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path, host="pdf.test"):
        return f"http://{host}:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _send(body=b"", status=200, headers=None):
    headers = dict(headers or {})

    def route(handler):
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        if "Content-Length" not in headers:
            handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    return route


def _chunked(chunks):
    def route(handler):
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for chunk in chunks():
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        handler.wfile.write(b"0\r\n\r\n")
    return route


@pytest.fixture
def server():
    running = _Server()
    yield running
    running.close()


def _local(extra=None):
    """What admits the test server: its names, listed, and resolved to loopback."""
    table = {"pdf.test": ["127.0.0.1"], "other.test": ["127.0.0.1"], **(extra or {})}
    return {
        "resolve": lambda host, port: table.get(host, []),
        "private_hosts": ("pdf.test", "other.test"),
    }


class _Page:
    def __init__(self, probe, cookies=()):
        self._probe = probe
        self.cookie_calls = []
        cookie_list = list(cookies)

        def context_cookies(urls):
            self.cookie_calls.append(urls)
            return cookie_list

        self.context = SimpleNamespace(cookies=context_cookies)

    def evaluate(self, script, *args):
        if isinstance(self._probe, Exception):
            raise self._probe
        return self._probe


# --------------------------------------------------------------------------- #
# The parse, in the real child
# --------------------------------------------------------------------------- #

class TestTheChild:
    def test_text_comes_back_with_page_markers_count_and_title(self, pdf_text):
        data = make_pdf(["Programme one: documentary", "Programme two: animation"], title="Programy 2026")
        result = pdf_text.extract_text(data, deadline=_later())
        assert result["ok"] is True
        assert result["pages"] == 2 and result["pages_read"] == 2
        assert result["title"] == "Programy 2026"
        assert result["has_text"] is True
        assert result["capped"] is False
        text = result["text"]
        assert text.index("[page 1]") < text.index("Programme one: documentary")
        assert text.index("[page 2]") < text.index("Programme two: animation")

    def test_text_beyond_ascii_comes_back(self, pdf_text):
        result = pdf_text.extract_text(make_pdf(["caf\xe9 na\xefve"]), deadline=_later())
        assert result["ok"] is True
        assert "caf\xe9 na\xefve" in result["text"]

    def test_the_page_ceiling_caps_and_says_so(self, pdf_text, monkeypatch):
        monkeypatch.setattr(pdf_text, "MAX_PAGES", 2)
        result = pdf_text.extract_text(make_pdf(["a1", "b2", "c3"]), deadline=_later())
        assert result["ok"] is True
        assert result["pages"] == 3 and result["pages_read"] == 2
        assert result["capped"] is True
        assert "c3" not in result["text"]

    def test_garbage_after_the_magic_is_unreadable_not_a_crash(self, pdf_text):
        result = pdf_text.extract_text(b"%PDF-1.7\n" + b"\x00\xff" * 4096, deadline=_later())
        assert result == {"ok": False, "error": "pdf_unreadable"}

    def test_a_page_with_no_text_is_reported_as_such(self, pdf_text):
        result = pdf_text.extract_text(make_pdf([""]), deadline=_later())
        assert result["ok"] is True
        assert result["has_text"] is False

    @pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
    def test_an_owner_password_only_pdf_is_read(self, pdf_text, algorithm):
        import pypdf

        writer = pypdf.PdfWriter(clone_from=io.BytesIO(make_pdf(["Locked for editing only"])))
        writer.encrypt(user_password="", owner_password="owner", algorithm=algorithm)
        out = io.BytesIO()
        writer.write(out)
        result = pdf_text.extract_text(out.getvalue(), deadline=_later())
        assert result["ok"] is True, result
        assert "Locked for editing only" in result["text"]

    def test_the_child_runs_isolated_with_an_empty_environment(self, pdf_text):
        seen = {}

        def runner(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok": false, "error": "pdf_unreadable"}')

        pdf_text.extract_text(b"%PDF-1.4", deadline=_later(), runner=runner)
        assert seen["argv"][:4] == [sys.executable, "-I", "-B", str(BROWSER / "pdf_child.py")]
        assert seen["env"] == {}
        assert seen["input"] == b"%PDF-1.4"
        assert seen["timeout"] <= pdf_text.PARSE_TIMEOUT_S

    def test_a_wall_clock_timeout_is_a_parse_timeout(self, pdf_text):
        def runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        assert pdf_text.extract_text(b"%PDF-", deadline=_later(), runner=runner) == {
            "ok": False, "error": "pdf_parse_timeout",
        }

    @pytest.mark.parametrize("sig, reason", [
        (signal.SIGXCPU, "pdf_parse_timeout"),
        (signal.SIGKILL, "pdf_parse_timeout"),
        (signal.SIGSEGV, "pdf_unreadable"),
        (signal.SIGTRAP, "pdf_unreadable"),
    ])
    def test_a_child_killed_by_a_signal_is_never_read(self, pdf_text, sig, reason):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, -sig, stdout=b'{"ok": true, "text": "x"}')

        assert pdf_text.extract_text(b"%PDF-", deadline=_later(), runner=runner) == {
            "ok": False, "error": reason,
        }

    @pytest.mark.parametrize("stdout", [b"", b"not json", b"[1]", b'{"text": "x"}', b"\xff\xfe"])
    def test_output_out_of_shape_is_unreadable(self, pdf_text, stdout):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=stdout)

        assert pdf_text.extract_text(b"%PDF-", deadline=_later(), runner=runner)["error"] == "pdf_unreadable"

    def test_oversized_output_is_refused_before_decoding(self, pdf_text, monkeypatch):
        monkeypatch.setattr(pdf_text, "MAX_CHILD_OUTPUT", 10)

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok": true, "text": "far too long"}')

        assert pdf_text.extract_text(b"%PDF-", deadline=_later(), runner=runner)["error"] == "pdf_unreadable"

    def test_no_time_left_starts_no_child(self, pdf_text):
        runner = MagicMock()
        result = pdf_text.extract_text(b"%PDF-", deadline=time.monotonic() + 1, runner=runner)
        assert result == {"ok": False, "error": "pdf_no_time"}
        runner.assert_not_called()


# --------------------------------------------------------------------------- #
# The fetch, through a real socket
# --------------------------------------------------------------------------- #

class TestTheFetch:
    def test_the_document_comes_back_with_the_tabs_cookies(self, pdf_text, server):
        data = make_pdf(["x"])
        server.routes["/a.pdf"] = _send(data, headers={"Content-Type": "application/pdf"})
        body, failure = pdf_text.fetch(
            server.url("/a.pdf"), deadline=_later(), user_agent="UA/1",
            cookies_for=lambda url: [{"name": "sid", "value": "abc"}, {"name": "x", "value": "1"}],
            **_local(),
        )
        assert failure is None and body == data
        (path, headers), = server.seen
        assert path == "/a.pdf"
        assert headers["User-Agent"] == "UA/1"
        assert headers["Cookie"] == "sid=abc; x=1"
        assert headers["Host"] == f"pdf.test:{server.port}"

    def test_a_cookie_that_would_split_the_header_is_dropped(self, pdf_text, server):
        server.routes["/a.pdf"] = _send(make_pdf(["x"]))
        pdf_text.fetch(
            server.url("/a.pdf"), deadline=_later(),
            cookies_for=lambda url: [{"name": "a", "value": "1\r\nX-Injected: 1"}, {"name": "b", "value": "2"}],
            **_local(),
        )
        (_path, headers), = server.seen
        assert headers["Cookie"] == "b=2"
        assert "X-Injected" not in headers

    def test_a_chunked_body_is_read_to_its_end(self, pdf_text, server):
        data = make_pdf(["chunked"])
        server.routes["/c.pdf"] = _chunked(lambda: [data[:100], data[100:]])
        body, failure = pdf_text.fetch(server.url("/c.pdf"), deadline=_later(), **_local())
        assert failure is None and body == data

    def test_a_chunked_body_over_the_cap_is_cut_off_while_reading(self, pdf_text, server, monkeypatch):
        monkeypatch.setattr(pdf_text, "MAX_PDF_BYTES", 256 * 1024)

        def endless():
            yield b"%PDF-1.4\n"
            for _ in range(10_000):
                yield b"0" * 65536

        server.routes["/big.pdf"] = _chunked(endless)
        body, failure = pdf_text.fetch(server.url("/big.pdf"), deadline=_later(), **_local())
        assert body is None and failure.reason == "pdf_too_large"

    def test_a_gzip_bomb_is_cut_off_at_the_cap_after_inflating(self, pdf_text, server, monkeypatch):
        monkeypatch.setattr(pdf_text, "MAX_PDF_BYTES", 1024 * 1024)
        compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
        bomb = compressor.compress(b"%PDF-1.4\n" + b"\0" * (64 * 1024 * 1024)) + compressor.flush()
        assert len(bomb) < 128 * 1024
        server.routes["/bomb.pdf"] = _send(bomb, headers={"Content-Encoding": "gzip"})
        body, failure = pdf_text.fetch(server.url("/bomb.pdf"), deadline=_later(), **_local())
        assert body is None and failure.reason == "pdf_too_large"

    def test_a_gzip_body_under_the_cap_is_inflated(self, pdf_text, server):
        data = make_pdf(["zipped"])
        server.routes["/z.pdf"] = _send(gzip.compress(data), headers={"Content-Encoding": "gzip"})
        body, failure = pdf_text.fetch(server.url("/z.pdf"), deadline=_later(), **_local())
        assert failure is None and body == data

    def test_a_raw_deflate_body_is_inflated(self, pdf_text, server):
        data = make_pdf(["deflated"])
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
        raw = compressor.compress(data) + compressor.flush()
        server.routes["/d.pdf"] = _send(raw, headers={"Content-Encoding": "deflate"})
        body, failure = pdf_text.fetch(server.url("/d.pdf"), deadline=_later(), **_local())
        assert failure is None and body == data

    def test_an_encoding_it_cannot_undo_is_refused(self, pdf_text, server):
        server.routes["/br.pdf"] = _send(b"\x0b\x02", headers={"Content-Encoding": "br"})
        body, failure = pdf_text.fetch(server.url("/br.pdf"), deadline=_later(), **_local())
        assert failure.reason == "pdf_fetch_failed"

    def test_a_declared_length_over_the_cap_is_refused_unread(self, pdf_text, server, monkeypatch):
        monkeypatch.setattr(pdf_text, "MAX_PDF_BYTES", 10)
        server.routes["/l.pdf"] = _send(b"%PDF-1.4 and more", headers={"Content-Length": "999999"})
        body, failure = pdf_text.fetch(server.url("/l.pdf"), deadline=_later(), **_local())
        assert failure.reason == "pdf_too_large"
        assert failure.detail == {"bytes": 999999}

    def test_each_redirect_hop_is_checked_before_it_is_fetched(self, pdf_text, server):
        server.routes["/r.pdf"] = _send(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
        body, failure = pdf_text.fetch(server.url("/r.pdf"), deadline=_later(), **_local())
        assert failure.reason == "pdf_fetch_refused"
        assert [path for path, _ in server.seen] == ["/r.pdf"]

    def test_a_redirect_to_a_name_resolving_to_a_private_address_is_refused(self, pdf_text, server):
        server.routes["/r.pdf"] = _send(status=302, headers={"Location": "http://intranet.example/x.pdf"})
        options = _local({"intranet.example": ["10.0.0.7"]})
        body, failure = pdf_text.fetch(server.url("/r.pdf"), deadline=_later(), **options)
        assert failure.reason == "pdf_fetch_refused"

    def test_an_allowed_redirect_is_followed_with_the_new_urls_cookies(self, pdf_text, server):
        data = make_pdf(["moved"])
        moved = server.url("/new.pdf", host="other.test")
        server.routes["/old.pdf"] = _send(status=301, headers={"Location": moved})
        server.routes["/new.pdf"] = _send(data)
        asked = []
        body, failure = pdf_text.fetch(
            server.url("/old.pdf"), deadline=_later(),
            cookies_for=lambda url: asked.append(url) or [], **_local(),
        )
        assert failure is None and body == data
        assert asked == [server.url("/old.pdf"), moved]

    def test_a_redirect_loop_ends(self, pdf_text, server):
        server.routes["/loop.pdf"] = _send(status=302, headers={"Location": "/loop.pdf"})
        body, failure = pdf_text.fetch(server.url("/loop.pdf"), deadline=_later(), **_local())
        assert failure.reason == "pdf_fetch_refused"
        assert len(server.seen) == pdf_text.MAX_REDIRECTS + 1

    @pytest.mark.parametrize("url, reason", [
        ("http://127.0.0.1/a.pdf", "pdf_fetch_refused"),
        ("http://10.0.0.5/a.pdf", "pdf_fetch_refused"),
        ("http://[::1]/a.pdf", "pdf_fetch_refused"),
        ("https://user:pw@93.184.216.34/a.pdf", "pdf_fetch_refused"),
        ("file:///etc/passwd", "pdf_fetch_refused"),
        ("about:blank", "pdf_fetch_refused"),
        ("blob:https://example.com/0f0e", "pdf_not_fetchable"),
        ("data:application/pdf;base64,JVBERi0=", "pdf_not_fetchable"),
    ])
    def test_the_first_hop_needs_a_public_http_address(self, pdf_text, url, reason):
        body, failure = pdf_text.fetch(url, deadline=_later())
        assert failure.reason == reason

    def test_the_name_is_resolved_once_and_the_connection_pinned(self, pdf_text, server):
        calls = []
        options = _local()
        resolve = options["resolve"]
        options["resolve"] = lambda host, port: calls.append(host) or resolve(host, port)
        server.routes["/p.pdf"] = _send(make_pdf(["pinned"]))
        body, failure = pdf_text.fetch(server.url("/p.pdf"), deadline=_later(), **options)
        assert failure is None
        assert calls == ["pdf.test"]

    def test_bytes_that_are_not_a_pdf_are_refused(self, pdf_text, server):
        server.routes["/h.pdf"] = _send(b"<html>login</html>")
        body, failure = pdf_text.fetch(server.url("/h.pdf"), deadline=_later(), **_local())
        assert failure.reason == "pdf_not_pdf"

    def test_an_error_status_is_reported(self, pdf_text, server):
        server.routes["/f.pdf"] = _send(status=403)
        body, failure = pdf_text.fetch(server.url("/f.pdf"), deadline=_later(), **_local())
        assert failure.reason == "pdf_fetch_status"
        assert failure.detail == {"http_status": 403}

    def test_a_host_that_does_not_answer_is_a_fetch_failure(self, pdf_text):
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
        body, failure = pdf_text.fetch(f"http://pdf.test:{port}/a.pdf", deadline=_later(), **_local())
        assert failure.reason == "pdf_fetch_failed"

    def test_no_time_left_fetches_nothing(self, pdf_text, server):
        body, failure = pdf_text.fetch(server.url("/a.pdf"), deadline=time.monotonic() + 1, **_local())
        assert failure.reason == "pdf_no_time"
        assert server.seen == []


# --------------------------------------------------------------------------- #
# Detection and the read
# --------------------------------------------------------------------------- #

class TestDetection:
    def test_a_page_that_is_not_a_pdf_reads_as_none(self, pdf_text):
        assert pdf_text.read_shown_pdf(_Page({"pdfUrl": None, "userAgent": "UA"}), deadline=_later()) is None

    def test_a_probe_that_fails_reads_as_none(self, pdf_text):
        assert pdf_text.read_shown_pdf(_Page(RuntimeError("detached")), deadline=_later()) is None

    def test_the_probe_reads_the_content_type_and_a_same_origin_lone_embed(self, pdf_text):
        assert "document.contentType" in pdf_text._PDF_PROBE_JS
        assert 'only.localName === "embed"' in pdf_text._PDF_PROBE_JS
        assert "src.origin === location.origin" in pdf_text._PDF_PROBE_JS

    def test_a_shown_pdf_is_read_with_the_tabs_cookies(self, pdf_text, server):
        data = make_pdf(["Deadline: 30 June"], title="Call")
        server.routes["/call.pdf"] = _send(data)
        page = _Page({"pdfUrl": server.url("/call.pdf"), "userAgent": "UA"},
                     cookies=[{"name": "s", "value": "1"}])
        result = pdf_text.read_shown_pdf(page, deadline=_later(), **_local())
        assert isinstance(result, pdf_text.PdfText)
        assert "Deadline: 30 June" in result.text
        assert result.title == "Call" and result.pages == 1 and result.size == len(data)
        assert page.cookie_calls == [[server.url("/call.pdf")]]
        assert server.seen[0][1]["Cookie"] == "s=1"

    def test_a_pdf_the_page_made_itself_is_a_failure_not_an_empty_page(self, pdf_text):
        result = pdf_text.read_shown_pdf(_Page({"pdfUrl": "blob:https://example.com/1"}), deadline=_later())
        assert isinstance(result, pdf_text.PdfFailure)
        assert result.reason == "pdf_not_fetchable"

    def test_a_scanned_pdf_is_a_failure_not_an_empty_text(self, pdf_text, server):
        server.routes["/scan.pdf"] = _send(make_pdf([""]))
        result = pdf_text.read_shown_pdf(_Page({"pdfUrl": server.url("/scan.pdf")}), deadline=_later(), **_local())
        assert result.reason == "pdf_no_text"
        assert "screenshot" in result.message


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

@pytest.fixture
def pdf_api(api, monkeypatch):  # noqa: F811
    """The real API whose every new tab shows the PDF at PDF_URL.

    `pdf_api.pdf_state["body"]` is what the fetch returns, and
    `pdf_api.pdf_state["fetches"]` counts the fetches made.
    """
    state = {"body": make_pdf(["Programme one", "Programme two"], title="Programy"), "fetches": 0}
    launch = api.chrome.launch_chrome

    def launch_pdf(inst):
        launch(inst)
        page = inst.pw_context.pages[0]
        page.url = PDF_URL
        page.title.return_value = "programme.pdf"

        def evaluate(script, *args):
            if "document.contentType" in script:
                return {"pdfUrl": PDF_URL, "userAgent": "UA"}
            return []

        page.evaluate.side_effect = evaluate
        page.context.cookies.return_value = []

    def fetch(url, **kwargs):
        state["fetches"] += 1
        body = state["body"]
        if not api.pdf_text.looks_like_pdf(body):
            return None, api.pdf_text.PdfFailure(url, "pdf_not_pdf")
        return body, None

    monkeypatch.setattr(api.chrome, "launch_chrome", launch_pdf)
    monkeypatch.setattr(api.chrome, "ensure_chrome", lambda inst: launch_pdf(inst) if inst.pw_context is None else None)
    monkeypatch.setattr(api.pdf_text, "fetch", fetch)
    # What the real extractors return on Chrome's viewer: nothing.
    monkeypatch.setattr(api.browsing, "extract_page_content", lambda page, **kw: {
        "title": "", "url": page.url, "text": "", "links": [],
    })
    monkeypatch.setattr(api.render, "to_markdown", lambda *a, **kw: {
        "markdown": "", "mode": "full", "requested_mode": "full", "chars": 0, "truncated": False,
        "frames": {"found": 1, "included": 0, "capped": False, "urls": []}, "notes": [],
    })
    api.pdf_state = state
    return api


class TestTheEndpoints:
    def test_get_on_a_pdf_returns_its_text(self, pdf_api):
        response = post(pdf_api, "/browse", url=PDF_URL)
        assert response.status_code == 200
        body = response.json
        assert body["status"] == "ok"
        assert body["content_type"] == "application/pdf"
        assert "Programme one" in body["text"] and "Programme two" in body["text"]
        assert body["title"] == "Programy"
        assert body["pdf"]["pages"] == 2 and body["pdf"]["pages_read"] == 2
        assert body["pdf"]["text_capped"] is False
        assert body["links"] == []

    def test_render_on_a_pdf_returns_its_text_as_the_markdown(self, pdf_api):
        response = post(pdf_api, "/render", url=PDF_URL)
        assert response.status_code == 200
        body = response.json
        assert body["status"] == "ok"
        assert body["mode"] == "pdf"
        assert "Programme one" in body["markdown"]
        assert body["chars"] == len(body["markdown"])
        assert body["truncated"] is False
        assert body["pdf"]["pages"] == 2
        assert body["notes"]

    def test_the_text_pages_by_offset(self, pdf_api):
        first = post(pdf_api, "/render", url=PDF_URL, max_chars=12).json
        assert first["truncated"] is True and first["next_offset"] == 12
        second = post(pdf_api, "/render", url=PDF_URL, max_chars=12, offset=12).json
        whole = post(pdf_api, "/render", url=PDF_URL).json["markdown"]
        assert first["markdown"] + second["markdown"] == whole[:24]
        browse = post(pdf_api, "/browse", url=PDF_URL, max_chars=12, offset=12).json
        assert browse["text"] == second["markdown"]

    def test_a_kept_session_reads_the_pdf_once_while_paging(self, pdf_api):
        first = post(pdf_api, "/render", url=PDF_URL, max_chars=12, keep_session=True).json
        sid = first["session_id"]
        second = post(pdf_api, "/render", session_id=sid, max_chars=12, offset=12).json
        third = post(pdf_api, "/browse", session_id=sid, max_chars=12, offset=24).json
        assert pdf_api.pdf_state["fetches"] == 1
        assert first["markdown"] and second["markdown"] and third["text"]
        assert second["text_offset"] == 12 and third["text_offset"] == 24

    @pytest.mark.parametrize("path", ["/browse", "/render"])
    def test_an_unreadable_pdf_is_an_error_never_an_empty_ok(self, pdf_api, path):
        pdf_api.pdf_state["body"] = b"<html>login wall</html>"
        response = post(pdf_api, path, url=PDF_URL)
        body = response.json
        assert body["status"] == "error"
        assert body["error"] == "pdf_document"
        assert body["reason"] == "pdf_not_pdf"
        assert body["message"]
        assert response.status_code == 422

    def test_extract_on_a_pdf_names_the_verbs_that_read_it(self, pdf_api):
        response = post(pdf_api, "/extract", url=PDF_URL, selector="body")
        body = response.json
        assert body["status"] == "error"
        assert body["error"] == "pdf_document"
        assert "render" in body["message"]
        assert pdf_api.pdf_state["fetches"] == 0

    def test_the_text_is_scrubbed_of_credential_values(self, pdf_api, monkeypatch):
        pdf_api.pdf_state["body"] = make_pdf(["token hunter2 here"])
        monkeypatch.setattr(pdf_api, "_credential_values", {"hunter2"})
        body = post(pdf_api, "/browse", url=PDF_URL).json
        assert "hunter2" not in body["text"]
        assert "[REDACTED]" in body["text"]

    def test_a_disabled_watchdog_still_leaves_time_to_read(self, pdf_api, monkeypatch):
        monkeypatch.setattr(pdf_api, "BROWSE_WATCHDOG_DEADLINE_S", 0)
        body = post(pdf_api, "/browse", url=PDF_URL).json
        assert body["status"] == "ok"
        assert "Programme one" in body["text"]

    def test_an_html_page_is_untouched(self, api):  # noqa: F811
        body = post(api, "/browse", url="https://example.com/").json
        assert body["status"] == "ok"
        assert "pdf" not in body


def test_the_test_pin_is_the_images_pin():
    """The suite parses with the pypdf the image ships, not a neighbour of it."""
    import re
    import tomllib

    repo = Path(__file__).resolve().parents[1]
    image = re.search(r"^pypdf==(\S+)$", (BROWSER / "requirements.txt").read_text(), re.M)
    dev = tomllib.loads((repo / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    assert image and f"pypdf=={image.group(1)}" in dev


def test_the_image_can_decrypt_aes():
    """pypdf reads AES only through a crypto provider; the image must ship one."""
    requirements = (BROWSER / "requirements.txt").read_text()
    assert any(line.startswith("cryptography==") for line in requirements.splitlines())


def test_the_hand_built_pdf_is_one_pypdf_reads():
    """Every test above trusts `make_pdf`; this is where it is checked directly."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(make_pdf(["one", "two"], title="T")))
    assert len(reader.pages) == 2
    assert reader.metadata.title == "T"
    assert "two" in reader.pages[1].extract_text()
