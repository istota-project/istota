"""Serve status concurrently while browser operations stay on the main thread."""

from concurrent.futures import Future
import queue
import threading

from werkzeug.serving import make_server
from werkzeug.wrappers import Response


def make_browser_server(app, host="0.0.0.0", port=9223):
    pending = queue.Queue(maxsize=32)

    def dispatch(environ, start_response):
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
