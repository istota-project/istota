"""Child process for the ``istota serve`` shutdown tests.

A real uvicorn serving a real SSE stream — the shape the launcher actually has,
and the one where the shutdown hang lives. Argv: ``<port> <graceful_seconds>``.
Nothing here imports the web app; the two mechanisms under test are the
``timeout_graceful_shutdown`` value and :func:`istota.serve.install_force_quit`.
"""

import asyncio
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from istota import serve


async def _forever():
    """The shape of every SSE endpoint in the web app: polls until the client
    goes away, so nothing on the server side ever ends it."""
    while True:
        yield b": ping\n\n"
        await asyncio.sleep(0.2)


async def _stream(request):
    return StreamingResponse(_forever(), media_type="text/event-stream")


def main() -> None:
    port = int(sys.argv[1])
    graceful = int(sys.argv[2])
    app = Starlette(routes=[Route("/stream", _stream)])
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        access_log=False, timeout_graceful_shutdown=graceful,
    )
    server = uvicorn.Server(config)
    serve.install_force_quit(server)
    server.run()


if __name__ == "__main__":
    main()
