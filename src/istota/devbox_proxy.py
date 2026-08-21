"""Devbox credential proxy daemon.

Per-user asyncio daemon that lives on the host, listens on a Unix socket
bind-mounted into the user's devbox container, and answers structured
requests for git credentials and forge tokens.

See `.claude/skills/spec/devbox-credential-proxy` (in the user's notes
vault) for the full design. The protocol is in
``src/istota/devbox_proxy_protocol.py``.

Three actions: ``ping``, ``git_credential`` (get/store/erase), and
``forge_token``. Every one of them answers out of the context held in
memory — the daemon makes no outbound requests of its own.

``forge_token`` replaced the ``gitlab_api`` / ``github_api`` actions,
which had the proxy make the REST call itself against an endpoint
allowlist. That allowlist could not describe what a real ``gh``
invocation does (``gh pr create`` is several calls, ``gh pr checks``
paginates), so the container now runs the real ``gh`` / ``glab`` behind
``forge_cli.py`` and asks here only for the token. The container
consequently *does* see the token, which it did not before; the git
path already handed it one on every push, and the boundary that does
the work is the token's own scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from istota.devbox_proxy_protocol import (
    ACTION_FORGE_TOKEN,
    ACTION_GIT_CREDENTIAL,
    ACTION_PING,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NO_TOKEN,
    ERR_UNKNOWN_ACTION,
    ERR_UNKNOWN_PROVIDER,
    MAX_REQUEST_BYTES,
    ProtocolError,
    decode_request,
    encode_error,
    encode_response,
)

logger = logging.getLogger("istota.devbox_proxy")
audit_logger = logging.getLogger("istota.devbox_proxy.audit")
# Detach the audit logger from root so the journal handler on the
# regular module logger doesn't double up; we still emit at INFO level
# so a configured file handler picks lines up.
audit_logger.propagate = True
audit_logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class DevboxProxyContext:
    """Per-user runtime context for the proxy daemon.

    Held in memory for the lifetime of the unit. Tokens are loaded once
    at startup; rotation is handled by restarting the systemd unit.

    No HTTP client and no timeout: since ``forge_token`` replaced the two
    REST actions the daemon answers every request out of these fields and
    never leaves the host.
    """

    user_id: str
    gitlab_token: str
    github_token: str
    gitlab_url: str
    github_url: str

    @property
    def providers(self) -> list[str]:
        names: list[str] = []
        if self.github_token:
            names.append("github")
        if self.gitlab_token:
            names.append("gitlab")
        return names


# ---- Audit logging ---------------------------------------------------------


def configure_audit_log(path: str | None) -> logging.Handler | None:
    """Attach a FileHandler to the audit logger when ``path`` is set.

    Idempotent — call it once at daemon startup. Returns the added
    handler (or ``None`` if no file logging was requested) so callers
    that need clean teardown (tests) can remove it.
    """
    if not path:
        return None
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    handler.setLevel(logging.INFO)
    audit_logger.addHandler(handler)
    return handler


def _audit(
    *,
    user_id: str,
    action: str,
    result: str,
    dur_ms: int,
    **extra: Any,
) -> None:
    """Emit one structured audit line in key=value form.

    Values that are not plain are single-quoted and escaped by
    ``_audit_value``; anything ``None`` is dropped, which keeps the line
    compact. Q3 of the spec's open questions resolved on 2026-05-15:
    key-value text in both the journal and the optional file sink.
    """
    parts = [
        f"user={user_id}",
        f"action={action}",
        f"result={result}",
        f"dur_ms={dur_ms}",
    ]
    for key, value in extra.items():
        if value is None:
            continue
        parts.append(f"{key}={_audit_value(value)}")
    audit_logger.info("devbox_proxy " + " ".join(parts))


# Characters that may appear in an audit value unquoted. An allowlist rather
# than a denylist because several of these values are caller-controlled — the
# git-credential ``host`` field and ``forge_token``'s ``provider`` both come
# straight off the wire. The previous denylist (space, ``=``, quote,
# backslash) let a newline through unquoted, which forges a whole second
# audit line: one request writing ``result=ok`` for a call that never
# happened. Common host and provider spellings stay unquoted, so existing
# log lines are unchanged.
_AUDIT_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-/:@+,"
)

# Caller-controlled values are truncated before they reach the log. Without
# this a 16 MiB ``provider`` string (the wire cap) becomes a 16 MiB log line.
_AUDIT_MAX_VALUE_CHARS = 200


def _audit_value(value: Any) -> str:
    """Render one audit value, quoted and escaped when it isn't plain."""
    s = str(value)
    if len(s) > _AUDIT_MAX_VALUE_CHARS:
        s = s[:_AUDIT_MAX_VALUE_CHARS] + "…truncated"
    if s and all(c in _AUDIT_SAFE_CHARS for c in s):
        return s
    # Backslash first, else an escape inserted below gets escaped again.
    escaped = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # Anything else non-printable (NUL, other control bytes) would still
    # break a line-oriented parser; render it as an escape too. Above 0xFF the
    # escape is \uXXXX rather than \xNN: "%02x" is a *minimum* width, so
    # U+2028 (a line separator, and the reason this branch exists) would
    # otherwise render as \x2028, which a parser reads as \x20 followed by a
    # literal "28".
    escaped = "".join(
        c if c.isprintable() or c == " "
        else ("\\x%02x" % ord(c) if ord(c) <= 0xFF else "\\u%04x" % ord(c))
        for c in escaped
    )
    return "'" + escaped + "'"


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


# ---- Helpers ---------------------------------------------------------------


def _parse_git_credential_input(raw: str) -> dict[str, str]:
    """Parse git's framed credential-helper stdin into a dict.

    Format is ``key=value\\n`` lines, terminated by an empty line. We're
    lenient about the trailing empty line — git always sends it but we
    don't enforce its presence.
    """
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value
    return out


def _provider_for_host(host: str, ctx: DevboxProxyContext) -> str | None:
    """Map a hostname to ``"github"`` / ``"gitlab"`` / ``None``.

    Comparison is case-insensitive and strips any ``:port`` suffix —
    git can send mixed case (``GitHub.com``) and some setups append
    ports to the host field.
    """
    if not host:
        return None
    host = host.strip().lower().split(":", 1)[0]
    gh_host = (urllib.parse.urlparse(ctx.github_url).hostname or "github.com").lower()
    gl_host = (urllib.parse.urlparse(ctx.gitlab_url).hostname or "gitlab.com").lower()
    if host == gh_host or host == "github.com":
        return "github"
    if host == gl_host or host == "gitlab.com":
        return "gitlab"
    return None


# ---- Action handlers -------------------------------------------------------


async def handle_ping(request: dict, ctx: DevboxProxyContext) -> str:
    start = time.perf_counter()
    response = encode_response(
        ok=True, user_id=ctx.user_id, providers=ctx.providers,
    )
    _audit(
        user_id=ctx.user_id, action=ACTION_PING,
        result="ok", dur_ms=_elapsed_ms(start),
    )
    return response


async def handle_git_credential(request: dict, ctx: DevboxProxyContext) -> str:
    start = time.perf_counter()
    op = request.get("op", "")

    # store / erase are intentional no-ops. We never persist the token on
    # the container side, and an in-container erase request should never
    # be able to invalidate host-side state.
    if op in ("store", "erase"):
        _audit(
            user_id=ctx.user_id, action=ACTION_GIT_CREDENTIAL,
            result="noop", dur_ms=_elapsed_ms(start), op=op,
        )
        return encode_response(ok=True)

    if op != "get":
        _audit(
            user_id=ctx.user_id, action=ACTION_GIT_CREDENTIAL,
            result="bad_request", dur_ms=_elapsed_ms(start), op=op,
        )
        return encode_error(
            ERR_BAD_REQUEST,
            f"unknown git_credential op: {op!r}",
        )

    raw_input = request.get("input", "") or ""
    fields = _parse_git_credential_input(raw_input)
    host = fields.get("host", "")
    protocol = fields.get("protocol", "https")

    provider = _provider_for_host(host, ctx)
    if provider == "github":
        token = ctx.github_token
    elif provider == "gitlab":
        token = ctx.gitlab_token
    else:
        # Q2 resolution (2026-05-15): cross-host attempts emit a
        # no_token audit line — it's the only signal we have that the
        # agent reached for a third-party host.
        _audit(
            user_id=ctx.user_id, action=ACTION_GIT_CREDENTIAL,
            result="no_token", dur_ms=_elapsed_ms(start),
            op="get", host=host,
        )
        return encode_error(
            ERR_NO_TOKEN,
            f"no token configured for host {host!r}",
        )

    if not token:
        _audit(
            user_id=ctx.user_id, action=ACTION_GIT_CREDENTIAL,
            result="no_token", dur_ms=_elapsed_ms(start),
            op="get", host=host, provider=provider,
        )
        return encode_error(
            ERR_NO_TOKEN,
            f"no token configured for {provider}",
        )

    stdout = (
        f"protocol={protocol}\n"
        f"host={host}\n"
        f"username=x-access-token\n"
        f"password={token}\n"
    )
    _audit(
        user_id=ctx.user_id, action=ACTION_GIT_CREDENTIAL,
        result="ok", dur_ms=_elapsed_ms(start),
        op="get", host=host, provider=provider,
    )
    return encode_response(ok=True, stdout=stdout)


async def handle_forge_token(request: dict, ctx: DevboxProxyContext) -> str:
    """Hand the in-container ``gh`` / ``glab`` wrapper its forge token.

    The wrapper (``src/istota/forge_cli.py``, vendored into the image)
    sends ``{"action": "forge_token", "provider": "github"|"gitlab"}`` and
    execs the real binary with the token in its environment. There is no
    policy check here: the policy is the wrapper's, read from a root-owned
    file in the image, and re-deciding it on this side would mean parsing
    an argv the proxy never sees.

    The configured URL rides along with the token. The devbox image is built
    once and shared by every user, so its baked policy cannot name a per-user
    ``gitlab_url``; without this the wrapper leaves ``GITLAB_HOST`` unset and
    glab sends a self-hosted token to gitlab.com. This daemon is per-user and
    already hands over the token, so it is the right place to answer from.

    The audit line records the provider and never the token.
    """
    start = time.perf_counter()
    provider = str(request.get("provider") or "").strip().lower()

    if provider == "github":
        token, url = ctx.github_token, ctx.github_url
    elif provider == "gitlab":
        token, url = ctx.gitlab_token, ctx.gitlab_url
    else:
        _audit(
            user_id=ctx.user_id, action=ACTION_FORGE_TOKEN,
            result="unknown_provider", dur_ms=_elapsed_ms(start),
            provider=provider or "(missing)",
        )
        return encode_error(
            ERR_UNKNOWN_PROVIDER,
            f"unknown forge provider {provider!r} — expected 'github' or 'gitlab'",
        )

    if not token:
        _audit(
            user_id=ctx.user_id, action=ACTION_FORGE_TOKEN,
            result="no_token", dur_ms=_elapsed_ms(start), provider=provider,
        )
        return encode_error(ERR_NO_TOKEN, f"no token configured for {provider}")

    _audit(
        user_id=ctx.user_id, action=ACTION_FORGE_TOKEN,
        result="ok", dur_ms=_elapsed_ms(start), provider=provider,
    )
    return encode_response(ok=True, token=token, url=url)


# ---- Connection dispatch ---------------------------------------------------


_ACTION_HANDLERS = {
    ACTION_PING: handle_ping,
    ACTION_GIT_CREDENTIAL: handle_git_credential,
    ACTION_FORGE_TOKEN: handle_forge_token,
}

# Actions this daemon used to answer, kept only as a diagnosis.
#
# The deployment's auto-update path resets to main and restarts the daemon on a
# short cron; it does not rebuild the devbox image, which Ansible does. So
# between this landing and the next Ansible run, a *running* container still
# holds the old curated shims and sends these two names at a daemon that no
# longer has handlers for them. Without this the operator gets a bare
# `unknown_action` and a journal that says nothing happened at all — the shape
# the spec split Stage 2 to avoid, arriving on the devbox path instead.
#
# Delete once no deployed image can still send them.
_RETIRED_ACTIONS = {
    "gitlab_api": "glab",
    "github_api": "gh",
}


# Per-daemon connection cap. asyncio.start_unix_server has no built-in
# concurrency limit, and each in-flight connection can carry up to
# MAX_REQUEST_BYTES (16 MiB) buffered in the StreamReader. A loop of
# fast-open + slow-read connections from inside the container would
# otherwise grow memory linearly. 32 is plenty for the legitimate
# workload (one container, sequential git operations + the occasional
# fan-out from the shims).
MAX_CONCURRENT_CONNECTIONS: int = 32


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ctx: DevboxProxyContext,
) -> None:
    """Read one request, dispatch, write one response, close.

    Errors at any layer collapse into the canonical structured envelope —
    the client always sees JSON.
    """
    try:
        try:
            line_bytes = await reader.readline()
        except (asyncio.LimitOverrunError, ValueError):
            # Single-line payload exceeded the StreamReader limit
            # (MAX_REQUEST_BYTES + 4096). Same outcome as the protocol-
            # layer cap — fail with bad_request.
            await _write_line(
                writer,
                encode_error(ERR_BAD_REQUEST, "request exceeds maximum size"),
            )
            return
        if not line_bytes:
            return

        try:
            request = decode_request(line_bytes.decode("utf-8", errors="replace"))
        except ProtocolError as e:
            await _write_line(writer, encode_error(e.code, e.message))
            return

        action = request.get("action")
        handler = _ACTION_HANDLERS.get(action)
        if handler is None:
            start = time.perf_counter()
            retired = _RETIRED_ACTIONS.get(action)
            if retired:
                message = (
                    f"the {action!r} action is retired — this devbox image is "
                    f"older than the daemon. Rebuild it (run the Ansible role) "
                    f"so the container gets the real {retired!r} behind the "
                    f"forge wrapper."
                )
            else:
                message = f"unknown action: {action!r}"
            # Audited, unlike the other early returns on this path: these two
            # names are what an un-rebuilt image sends, and a silent rejection
            # is indistinguishable from the proxy never being reached.
            _audit(
                user_id=ctx.user_id, action="unknown",
                result="retired_action" if retired else "unknown_action",
                dur_ms=_elapsed_ms(start), requested=action,
            )
            await _write_line(writer, encode_error(ERR_UNKNOWN_ACTION, message))
            return

        response_line = await handler(request, ctx)
        await _write_line(writer, response_line)
    except Exception:
        logger.exception("devbox_proxy connection error")
        try:
            await _write_line(writer, encode_error(ERR_INTERNAL, "internal proxy error"))
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _write_line(writer: asyncio.StreamWriter, line: str) -> None:
    writer.write(line.encode("utf-8"))
    await writer.drain()


# ---- Daemon entry point ----------------------------------------------------


async def build_context(user_id: str, config) -> DevboxProxyContext:
    """Build a DevboxProxyContext from a loaded Config."""
    dev = config.developer
    return DevboxProxyContext(
        user_id=user_id,
        gitlab_token=getattr(dev, "gitlab_token", "") or "",
        github_token=getattr(dev, "github_token", "") or "",
        gitlab_url=getattr(dev, "gitlab_url", "https://gitlab.com") or "https://gitlab.com",
        github_url=getattr(dev, "github_url", "https://github.com") or "https://github.com",
    )


def _default_socket_path(user_id: str, config) -> Path:
    """Per-user subdirectory holds the socket.

    Layout: ``{sock_dir}/{user_id}/sock``. The compose template bind-
    mounts the per-user directory (not the socket file) so that when the
    daemon restarts and unlinks+recreates the socket, the container sees
    the new inode through the same mount. A file bind-mount would pin
    the original inode and break every reconnect after a daemon restart.

    Per-user directories also enforce cross-tenant isolation: container
    alice's bind mount only contains alice's socket, even though the
    sockets are all group-readable by ``istota``.
    """
    sock_dir = getattr(config.developer, "devbox_proxy_socket_dir", "/var/run/istota")
    return Path(sock_dir) / user_id / "sock"


async def serve(
    user_id: str,
    config,
    *,
    socket_path: Path | None = None,
) -> None:
    """Run the devbox proxy daemon for one user.

    Returns when the server is stopped (cancelled). Cleans up the socket
    file on the way out.
    """
    ctx = await build_context(user_id, config)
    audit_log_path = getattr(config.developer, "devbox_proxy_audit_log", "") or ""
    configure_audit_log(audit_log_path or None)
    sock_path = socket_path or _default_socket_path(user_id, config)
    # Per-user parent dir is part of the access boundary. Create it 0o750
    # explicitly (mkdir() defaults respect umask but we don't want to
    # depend on the systemd-set umask here). The container's `dev` user
    # gains traverse access via the istota group membership granted by
    # the compose template's group_add.
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(sock_path.parent), 0o750)
    except OSError:
        # Test harnesses sometimes run with a parent dir we don't own;
        # the chmod is best-effort. Production deploys put the dir under
        # /var/run/istota which the daemon owns.
        pass
    if sock_path.exists():
        sock_path.unlink()

    connection_sem = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)

    async def _client_callback(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        # Bound in-flight connections. If we're already at the cap, reply
        # with the structured envelope and close — the client sees a
        # clean ``internal`` rather than a hang.
        if connection_sem.locked():
            try:
                await _write_line(
                    writer,
                    encode_error(ERR_INTERNAL, "proxy at concurrent-connection cap"),
                )
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            return
        async with connection_sem:
            await handle_connection(reader, writer, ctx)

    # Tighten the umask so start_unix_server() creates the socket inode
    # already at mode 0o660 — closes the window where a fast-reconnecting
    # client could race the chmod and see the default-permission inode.
    # The explicit chmod afterwards is belt-and-suspenders for setups
    # with a wider umask (e.g. test harnesses).
    #
    # Mode 0o660 + adding the devbox container's runtime group to the
    # istota group (via Ansible) is the access boundary. 0o600 would
    # leave the container's `dev` user (uid 1000) unable to connect
    # through the bind-mounted socket since it runs as a different uid
    # than the daemon (the istota system user).
    previous_umask = os.umask(0o117)
    try:
        server = await asyncio.start_unix_server(
            _client_callback,
            path=str(sock_path),
            limit=MAX_REQUEST_BYTES + 4096,
        )
    finally:
        os.umask(previous_umask)
    os.chmod(str(sock_path), 0o660)

    # SIGTERM cleanup: systemd sends SIGTERM on stop. Without an explicit
    # handler asyncio surfaces it as KeyboardInterrupt only on the main
    # thread; cancelling the server task lets the finally-block unlink
    # the socket before the process exits, so we never leave a stale
    # inode that confuses the next start.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Signal handlers aren't supported on every platform (e.g.
            # Windows asyncio); fall through and rely on cancellation.
            pass

    logger.info(
        "devbox_proxy listening user_id=%s socket=%s providers=%s",
        user_id, sock_path, ",".join(ctx.providers) or "none",
    )
    try:
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {serve_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    raise exc
    finally:
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """`python -m istota.devbox_proxy --user <id>` entry point."""
    import argparse

    from istota.config import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="user_id this proxy serves")
    parser.add_argument(
        "--config", default=None, help="optional config.toml path override",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = load_config(Path(args.config) if args.config else None)
    try:
        asyncio.run(serve(args.user, config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
