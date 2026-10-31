"""Run inside the browser image: what real Chrome reaches, with and without the egress proxy (#884).

Listeners on loopback and on the container's own private address stand in for
internal services. The unguarded arm is the control: the same probes must reach
them, or a guarded run reaching nothing proves nothing. A granted name reached
through the proxy shows the guarded path carries traffic at all. The last stdout
line is a JSON report for the test on the host.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

from patchright.sync_api import sync_playwright

import egress_proxy

LOOPBACK_PORT = 18080
PRIVATE_PORT = 18081
STUN_PORT = 18082
QUIC_PORT = 18083
GRANTED = "granted.internal"
POLICY = "/etc/opt/chrome/policies/managed/istota-egress.json"
POLICY_ASIDE = "/tmp/istota-egress.json"


def own_address():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


PRIVATE = own_address()
hits = []
udp_hits = []
quic_hits = []


class Recorder(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b"internal")

    def log_message(self, *args):
        pass


def serve(address, port):
    server = http.server.ThreadingHTTPServer((address, port), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def serve_udp(address, port, recorded):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((address, port))

    def run():
        while True:
            recorded.append(sock.recvfrom(2048)[1])

    threading.Thread(target=run, daemon=True).start()


PAGE = f"""<!doctype html><html><body>
<img src="http://{PRIVATE}:{PRIVATE_PORT}/subresource-private">
<img src="http://127.0.0.1:{LOOPBACK_PORT}/subresource-loopback">
</body></html>"""

JS_PROBES = f"""async () => {{
  try {{ await fetch('http://{PRIVATE}:{PRIVATE_PORT}/fetch', {{mode: 'no-cors'}}); }} catch (e) {{}}
  try {{ new WebSocket('ws://{PRIVATE}:{PRIVATE_PORT}/websocket'); }} catch (e) {{}}
  try {{
    const pc = new RTCPeerConnection({{iceServers: [{{urls: 'stun:{PRIVATE}:{STUN_PORT}'}}]}});
    pc.createDataChannel('probe');
    await pc.setLocalDescription(await pc.createOffer());
    window.keep = pc;
  }} catch (e) {{}}
  await new Promise(resolve => setTimeout(resolve, 3000));
}}"""


QUIC_PROBE = f"""async () => {{
  try {{ new WebTransport('https://{PRIVATE}:{QUIC_PORT}/'); }} catch (e) {{ window.quicError = String(e); }}
  await new Promise(resolve => setTimeout(resolve, 2000));
}}"""


def visit(page, url):
    try:
        page.goto(url, timeout=15000)
        return "loaded"
    except Exception as exc:
        return str(exc).splitlines()[0]


def arm(playwright, args, *, policy):
    # Chrome reads managed policy at start, from every file in the directory
    # whatever its name, so the file has to leave it for this arm.
    if not policy:
        shutil.move(POLICY, POLICY_ASIDE)
    del hits[:]
    del udp_hits[:]
    del quic_hits[:]
    # Chrome's own local-network checks would block subresources and fetch in both
    # arms. Off in both, the proxy is the only difference between them.
    browser = playwright.chromium.launch(
        executable_path="/usr/bin/google-chrome", headless=True,
        args=["--no-sandbox",
              "--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests",
              *args],
    )
    context = browser.new_context()
    context.route("http://public.example/page", lambda r: r.fulfill(body=PAGE, content_type="text/html"))
    context.route("https://public.example/secure", lambda r: r.fulfill(body="<p>secure</p>", content_type="text/html"))
    context.route("http://public.example/redirect", lambda r: r.fulfill(
        status=302, headers={"Location": f"http://{PRIVATE}:{PRIVATE_PORT}/redirected"}))
    page = context.new_page()
    visits = {
        "direct_private": visit(page, f"http://{PRIVATE}:{PRIVATE_PORT}/direct"),
        "direct_loopback": visit(page, f"http://127.0.0.1:{LOOPBACK_PORT}/loopback"),
        "localhost_name": visit(page, f"http://localhost:{LOOPBACK_PORT}/localhost"),
        "redirected": visit(page, "http://public.example/redirect"),
        "granted": visit(page, f"http://{GRANTED}:{PRIVATE_PORT}/granted"),
        "https_refused": visit(page, f"https://{PRIVATE}:{PRIVATE_PORT}/https"),
    }
    visit(page, "http://public.example/page")
    page.evaluate(JS_PROBES)
    visit(page, "https://public.example/secure")
    page.evaluate(QUIC_PROBE)
    time.sleep(1)
    browser.close()
    if not policy:
        shutil.move(POLICY_ASIDE, POLICY)
    return {"visits": visits, "hits": sorted(set(hits)), "udp": len(udp_hits), "quic": len(quic_hits)}


def main():
    with open("/etc/hosts", "a") as hosts:
        hosts.write(f"{PRIVATE} {GRANTED}\n")
    serve("127.0.0.1", LOOPBACK_PORT)
    serve(PRIVATE, PRIVATE_PORT)
    serve_udp(PRIVATE, STUN_PORT, udp_hits)
    serve_udp(PRIVATE, QUIC_PORT, quic_hits)

    proxy = subprocess.Popen(
        [sys.executable, "egress_proxy.py"],
        env={**os.environ, "BROWSER_PRIVATE_HOSTS": GRANTED},
    )
    deadline = time.monotonic() + 15
    while True:
        try:
            socket.create_connection(("127.0.0.1", egress_proxy.PROXY_PORT), timeout=1).close()
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    try:
        with sync_playwright() as playwright:
            report = {
                "address": PRIVATE,
                "unguarded": arm(playwright, [], policy=False),
                "guarded": arm(playwright, egress_proxy.chrome_args(), policy=True),
            }
    finally:
        proxy.terminate()
    print(json.dumps(report))


main()
