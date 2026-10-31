"""Chrome's only way out of the container: a forward proxy holding the SSRF rule.

A page runs attacker-chosen code, and a task chooses where the browser goes, so
what Chrome may connect to cannot be decided by checking the URL a caller asked
for: redirects, subresources, WebSockets and a DNS answer that changes all
happen later, inside Chrome. Every one of them reaches this process as a request
to connect, with the name still unresolved (Chrome resolves nothing itself when
it has a proxy), so the check sits where the connection is made. It is the rule
WebFetch uses, `lib/istota_net_guard.py`, applied to every resolved address, and
the connection goes to an address it checked.

That covers the rest of the container too. The API on 9223, the liveness port,
the debugging ports and VNC all answer on loopback, which a page would otherwise
reach directly, since Chrome sends loopback past a proxy unless told otherwise
(`chrome_args`). WebRTC's UDP is the one route a proxy does not see, and the
managed policy in `chrome-policy.json` turns it off.

`BROWSER_PRIVATE_HOSTS` is the operator's grant, `[browser] private_hosts` in
istota's config: exact names that may resolve to a private address. No grant
reaches loopback. A granted name that resolves to the
container's own network address would reach the API, which is why a grant is
exact names chosen by the operator.

This is not a sandbox. Code that escapes Chrome's sandbox could open its own
sockets; only a firewall outside the container stops that.
TLS is tunnelled untouched, so a site sees Chrome's own fingerprint.

stdlib and the vendored guard only, one process, started by `entrypoint.sh`. If
it is down Chrome reaches nothing, which is the side to fail on.
"""

import asyncio
import ipaddress
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import socket
import time
from urllib.parse import urlsplit

from lib.istota_net_guard import checked_addresses

log = logging.getLogger("egress_proxy")

PROXY_PORT = 9225
MAX_HEAD_BYTES = 64 * 1024
MAX_CONNECTIONS = 1024
HEAD_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 15
DNS_TIMEOUT_S = 15
PUMP_BUFFER_BYTES = 64 * 1024
# A far end can ignore a close, and a slot it holds is every user's.
RELAY_TICK_S = 5
CLOSE_GRACE_S = 30
TUNNEL_IDLE_S = 600

# A black-hole nameserver stalls a lookup past its deadline; the thread stays
# busy, so this pool is its own and lookups cannot take the loop's default one.
_dns_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="egress-dns")

REFUSED_BODY = b"Refused: this is a private address, which the browser may not reach.\n"


def chrome_args():
    """The flags that put every Chrome connection through this proxy."""
    return [
        f"--proxy-server=http://127.0.0.1:{PROXY_PORT}",
        "--proxy-bypass-list=<-loopback>",
    ]


def private_hosts_from(value):
    return [name.strip() for name in (value or "").split(",") if name.strip()]


def granted_hosts():
    return private_hosts_from(os.environ.get("BROWSER_PRIVATE_HOSTS", ""))


def is_up(timeout=1):
    """Whether the proxy accepts a connection, for the deep liveness probe."""
    try:
        socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=timeout).close()
        return True
    except OSError:
        return False


def is_loopback(address):
    """Loopback in any spelling, and the unspecified address, which connects to it."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _split_authority(authority):
    """``(host, port)`` from ``host:port`` or ``[v6]:port``, or None."""
    try:
        parts = urlsplit(f"//{authority}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or port is None or parts.path or parts.query or parts.username:
        return None
    return host, port


def _parse_head(head):
    """``(method, host, port, outgoing head or None)``, or None if it cannot be placed.

    The outgoing head is None for CONNECT, which sends nothing of its own.
    """
    try:
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ")
    except ValueError:
        return None
    if not version.startswith("HTTP/"):
        return None
    if method == "CONNECT":
        authority = _split_authority(target)
        return None if authority is None else (method, *authority, None)
    try:
        parts = urlsplit(target)
        host, port = parts.hostname, parts.port or 80
    except ValueError:
        return None
    if parts.scheme != "http" or not host:
        return None
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    kept = [f"{method} {path} {version}"]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name == "connection" or name.startswith("proxy-"):
            continue
        kept.append(line)
    # One request per connection, with the response saying so too: the relay does
    # not read framing, so a second request on this socket would go to this host.
    kept.append("Connection: close")
    return method, host, port, ("\r\n".join(kept) + "\r\n\r\n").encode("latin-1")


async def _answer(writer, status, reason, body=b""):
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    )
    await writer.drain()


async def _forward_response_head(reader, writer):
    """Pass the response head on saying the connection closes, as the request did."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        writer.write(exc.partial)
        return
    lines = head.decode("latin-1").split("\r\n")
    kept = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name in ("connection", "keep-alive", "proxy-connection"):
            continue
        kept.append(line)
    kept.append("Connection: close")
    writer.write(("\r\n".join(kept) + "\r\n\r\n").encode("latin-1"))


async def _pump(reader, writer, activity, *, head=None):
    try:
        if head is not None:
            await head(reader, writer)
            await writer.drain()
            activity[0] = time.monotonic()
        while True:
            data = await reader.read(PUMP_BUFFER_BYTES)
            if not data:
                break
            activity[0] = time.monotonic()
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        if writer.can_write_eof():
            try:
                writer.write_eof()
            except (OSError, RuntimeError):
                pass


async def _relay(reader, writer, upstream_reader, upstream_writer, *, plain_http):
    """Carry both directions until both end, an end goes unanswered, or nothing moves."""
    activity = [time.monotonic()]
    head = _forward_response_head if plain_http else None
    pending = {
        asyncio.create_task(_pump(reader, upstream_writer, activity)),
        asyncio.create_task(_pump(upstream_reader, writer, activity, head=head)),
    }
    first_end = None
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, timeout=RELAY_TICK_S, return_when=asyncio.FIRST_COMPLETED,
            )
            now = time.monotonic()
            if done and first_end is None:
                first_end = now
            if first_end is not None and now - max(first_end, activity[0]) >= CLOSE_GRACE_S:
                break
            if now - activity[0] >= TUNNEL_IDLE_S:
                break
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _open_checked(addresses, port, connect):
    for address in addresses:
        try:
            return await asyncio.wait_for(connect(address, port), CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            continue
    return None


async def _handle(reader, writer, *, private_hosts, resolve, connect, slots):
    upstream_writer = None
    try:
        if slots.locked():
            await _answer(writer, 503, "Service Unavailable")
            return
        async with slots:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
            except asyncio.LimitOverrunError:
                await _answer(writer, 431, "Request Header Fields Too Large")
                return
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                return
            parsed = _parse_head(head)
            if parsed is None:
                await _answer(writer, 400, "Bad Request")
                return
            method, host, port, outgoing = parsed

            loop = asyncio.get_running_loop()
            try:
                addresses, refused = await asyncio.wait_for(
                    loop.run_in_executor(
                        _dns_pool,
                        lambda: checked_addresses(host, port, private_hosts=private_hosts, resolve=resolve),
                    ),
                    DNS_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                await _answer(writer, 502, "Bad Gateway")
                return
            if refused is None and any(is_loopback(address) for address in addresses):
                refused = next(a for a in addresses if is_loopback(a))
                addresses = []
            if refused is not None:
                log.info("refused %s %s:%s, which resolves to %s", method, host, port, refused)
                await _answer(writer, 403, "Forbidden", REFUSED_BODY)
                return
            opened = await _open_checked(addresses, port, connect) if addresses else None
            if opened is None:
                await _answer(writer, 502, "Bad Gateway")
                return
            upstream_reader, upstream_writer = opened

            if outgoing is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(outgoing)
                await upstream_writer.drain()
            await _relay(reader, writer, upstream_reader, upstream_writer, plain_http=outgoing is not None)
    except (ConnectionError, OSError):
        pass
    finally:
        for stream in (upstream_writer, writer):
            if stream is not None:
                stream.close()


async def _open_connection(address, port):
    return await asyncio.open_connection(address, port)


async def serve(host, port, *, private_hosts=(), resolve=None, connect=_open_connection):
    slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def handler(reader, writer):
        await _handle(reader, writer, private_hosts=private_hosts, resolve=resolve,
                      connect=connect, slots=slots)

    return await asyncio.start_server(handler, host, port, limit=MAX_HEAD_BYTES)


async def _main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    private_hosts = granted_hosts()
    server = await serve("127.0.0.1", PROXY_PORT, private_hosts=private_hosts)
    log.info("listening on 127.0.0.1:%d, %d private host(s) granted", PROXY_PORT, len(private_hosts))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
