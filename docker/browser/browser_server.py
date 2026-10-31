"""Serve status concurrently while browser operations stay on the main thread."""

from concurrent.futures import Future
import hmac
import json
import queue
import threading

from werkzeug.serving import make_server
from werkzeug.wrappers import Response


#: Set by the dispatcher on every request, and only there: a header cannot reach it.
TOKEN_MATCHED = "istota.browser_api_token_matched"


def _refusal(status, error):
    return Response(json.dumps({"status": "error", "error": error}), status=status,
                    mimetype="application/json")


def _refused(environ, token):
    """Why a request may not reach the app. No token means nobody gets in.

    `/health` is open, and the route answers a caller without the token with no
    user's data: the doctor and the version gate read its capability flags.
    """
    # Compared as bytes: compare_digest refuses a str outside ASCII, and a
    # header is whatever a peer sent.
    sent = environ.get("HTTP_AUTHORIZATION", "").encode("latin-1", "replace")
    matched = bool(token) and hmac.compare_digest(sent, f"Bearer {token}".encode())
    environ[TOKEN_MATCHED] = matched
    if matched or environ["PATH_INFO"] == "/health":
        return None
    if not token:
        return _refusal(503, "browser_api_token_missing")
    return _refusal(401, "browser_api_unauthorized")


def make_browser_server(app, host="0.0.0.0", port=9223, token=""):
    pending = queue.Queue(maxsize=32)

    def dispatch(environ, start_response):
        # In front of the queue, so only `/health` lets a tokenless peer use it.
        refusal = _refused(environ, token)
        if refusal is not None:
            return refusal(environ, start_response)
        # Only this read-only route may run outside the browser's owning thread.
        if (environ["PATH_INFO"] == "/instances"
                and environ["REQUEST_METHOD"] in {"GET", "HEAD"}):
            return app(environ, start_response)
        result = Future()
        try:
            pending.put_nowait((environ, result))
        except queue.Full:
            return Response("Browser request queue is full", status=503)(environ, start_response)
        response = result.result()
        return response(environ, start_response)

    return make_server(host, port, dispatch, threaded=True), (app, pending)


def serve_browser_requests(server, work, stopping=None):
    """Run on the main thread, including response iteration and Flask teardown."""
    app, pending = work
    stopping = stopping if stopping is not None else threading.Event()
    listener = threading.Thread(target=server.serve_forever, daemon=True, name="browser-http")
    listener.start()
    try:
        while not stopping.is_set():
            try:
                environ, result = pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                # Browser endpoints return finite JSON or image bodies. Consume
                # them here so no browser work escapes onto an HTTP thread.
                response = Response.from_app(app, environ, buffered=True)
            except Exception as error:
                result.set_exception(error)
            else:
                result.set_result(response)
    finally:
        server.shutdown()
        server.server_close()
        listener.join()
        while True:
            try:
                _, result = pending.get_nowait()
            except queue.Empty:
                break
            result.set_result(Response("Browser service is stopping", status=503))
