"""Run inside the browser image: a PDF in real Chrome, read by the shipped pdf_text (#850).

Chrome opens a routed PDF in its viewer; `pdf_text.probe` must see it, and an
HTML page must not read as one. The document is then fetched by the shipped
client from a loopback server in the container, with the context's cookies,
and parsed by the shipped child on Linux, where its limits are enforced: a text
PDF and an AES-locked one read, and an input larger than a deliberately small
address-space limit is refused rather than read. The last stdout line is a JSON
report for the test on the host.

`PDF_TEXT_CONTROL=html` serves the PDF route as HTML, which the host-side
negative control uses to prove the viewer assertion can fail.
"""

import json
import os
import time

from patchright.sync_api import sync_playwright

import pdf_text

CONTROL = os.environ.get("PDF_TEXT_CONTROL", "")


def make_pdf(line, pad=0):
    stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    if pad:
        out += b"%" + b"0" * pad + b"\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


PDF = make_pdf("Programme deadline 30 June")


def locked_pdf():
    """PDF, locked by an owner password alone with AES-128: needs the crypto provider."""
    import io

    import pypdf

    writer = pypdf.PdfWriter(clone_from=io.BytesIO(make_pdf("Locked for editing only")))
    writer.encrypt(user_password="", owner_password="owner", algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def serve(documents):
    """A loopback server in the container; returns it and the Cookie headers it saw."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    cookies = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            cookies.append(self.headers.get("Cookie"))
            body = documents.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, cookies


def run():
    report = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True,
            args=["--no-sandbox"],
        )
        context = browser.new_context()
        if CONTROL == "html":
            context.route("https://docs.example/**", lambda r: r.fulfill(
                body="<html><body></body></html>", content_type="text/html"))
        else:
            context.route("https://docs.example/**", lambda r: r.fulfill(
                body=PDF, content_type="application/pdf"))
        context.route("https://www.example/**", lambda r: r.fulfill(
            body="<html><body><p>An ordinary page</p></body></html>", content_type="text/html"))
        page = context.new_page()

        page.goto("https://www.example/")
        report["html_probe"] = pdf_text.probe(page)[0]

        page.goto("https://docs.example/programme.pdf")
        url = None
        for _ in range(20):
            url, user_agent = pdf_text.probe(page)
            if url:
                break
            time.sleep(0.5)
        report["pdf_probe"] = url
        report["frames"] = [frame.url for frame in page.frames]

        # The real pinned client and the context's real cookies, against a
        # loopback server admitted by the guard's listed-name rule.
        httpd, seen_cookies = serve({"/programme.pdf": PDF, "/locked.pdf": locked_pdf()})
        port = httpd.server_address[1]
        context.add_cookies([{"name": "sid", "value": "tab-cookie", "domain": "docs.example", "path": "/"}])
        options = {
            "resolve": lambda host, port: ["127.0.0.1"] if host == "docs.example" else [],
            "private_hosts": ("docs.example",),
            "cookies_for": lambda url: context.cookies([url]),
        }
        read = pdf_text.read_pdf(
            f"http://docs.example:{port}/programme.pdf",
            deadline=time.monotonic() + 60, user_agent=user_agent, **options,
        )
        report["read"] = type(read).__name__
        report["text"] = getattr(read, "text", None)
        report["cookie"] = seen_cookies[0] if seen_cookies else None
        locked = pdf_text.read_pdf(
            f"http://docs.example:{port}/locked.pdf", deadline=time.monotonic() + 60, **options,
        )
        report["locked"] = getattr(locked, "text", None) or getattr(locked, "reason", None)
        httpd.shutdown()
        browser.close()

    # The address-space limit, enforced: a valid PDF padded with a 96 MiB
    # comment is refused under a 64 MiB limit, whether the child raises
    # MemoryError or dies on a signal, and reads under the shipped limit.
    padded = make_pdf("Padded but readable", pad=96 * 1024 * 1024)
    shipped = pdf_text.CHILD_MEMORY_BYTES
    pdf_text.CHILD_MEMORY_BYTES = 64 * 1024 * 1024
    report["bomb"] = pdf_text.extract_text(padded, deadline=time.monotonic() + 60)
    pdf_text.CHILD_MEMORY_BYTES = shipped
    unbombed = pdf_text.extract_text(padded, deadline=time.monotonic() + 60)
    unbombed["text"] = unbombed.get("text", "")[:200]
    report["unbombed"] = unbombed
    print(json.dumps(report))


if __name__ == "__main__":
    run()
