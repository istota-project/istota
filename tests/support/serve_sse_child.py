"""Child process for the ``istota serve`` shutdown tests.

A real uvicorn serving a real SSE stream — the shape the launcher actually has,
and the one where the shutdown hang lives. Argv:
``<port> <graceful_seconds> [shutdown_aware]``.

Nothing here imports the web app. The three mechanisms under test are the
``timeout_graceful_shutdown`` value, :func:`istota.serve.install_force_quit`,
and — with ``shutdown_aware`` set — the pair the web app itself uses:
:func:`istota.web_shutdown.install_signal_hook` from the lifespan, and a
generator that sleeps through
:func:`istota.web_shutdown.sleep_unless_shutdown`. The aware and unaware
generators are otherwise identical, so a test can run the same stream both ways.
"""

import asyncio
import contextlib
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from istota import serve, web_shutdown

_POLL = 0.2


async def _forever():
    """The shape of every SSE endpoint in the web app: polls until the client
    goes away, so nothing on the server side ever ends it."""
    while True:
        yield b": ping\n\n"
        await asyncio.sleep(_POLL)


async def _forever_shutdown_aware():
    """The same stream, ending itself when the process is told to stop."""
    while True:
        if web_shutdown.is_shutting_down():
            return
        yield b": ping\n\n"
        if not await web_shutdown.sleep_unless_shutdown(_POLL):
            return


def main() -> None:
    port = int(sys.argv[1])
    graceful = int(sys.argv[2])
    aware = len(sys.argv) > 3 and sys.argv[3] == "1"

    body = _forever_shutdown_aware if aware else _forever

    async def _stream(request):
        return StreamingResponse(body(), media_type="text/event-stream")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Where the web app installs it, and for the same reason: uvicorn has
        # installed the handler being wrapped by the time startup runs.
        if aware:
            web_shutdown.install_signal_hook()
        yield

    app = Starlette(routes=[Route("/stream", _stream)], lifespan=lifespan)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        access_log=False, timeout_graceful_shutdown=graceful,
    )
    server = uvicorn.Server(config)
    serve.install_force_quit(server)
    server.run()


if __name__ == "__main__":
    main()
