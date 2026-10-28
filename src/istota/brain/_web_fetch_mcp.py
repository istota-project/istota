"""The CLI tool harness's WebFetch, served as its own MCP server (#862, Stage 6).

``python -I -m istota.brain._web_fetch_mcp --config PATH`` is the harness's
second ``mcpServers`` entry, ``istota_web``. The ``claude`` CLI spawns it as it
spawns the tool server's launcher, but nothing wraps it: it runs outside the
sandbox, in the daemon's network namespace, as native WebFetch does, because
that tool's boundary is the address rule it applies to every hop rather than a
namespace. It holds no credential and reaches neither proxy, so it is not
reported to them.

In order: ``os.environ.clear()``, since the CLI hands every MCP server its
whole environment, credential included; read the config file (``O_NOFOLLOW``,
a regular file, a JSON object); build one ``ToolDispatcher`` over WebFetch
alone, at the harness's text cap; serve it through the tool server's MCP front
end (``sandbox.tool_server.serve_mcp``), holding calls behind the config's
``gate`` FIFO until the brain's init check has passed. WebFetch spawns nothing
and needs no environment: its client is built with ``trust_env=False``.

This module owns both halves of the config file: ``config_document``, which
the harness writes 0600 into its own directory, and the readers below. Every
refusal exits 2 with one line on stderr and nothing on stdout.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import fields
from typing import Any

_MAX_CONFIG_BYTES = 1024 * 1024
_TOOL_NAME = "WebFetch"


class ConfigError(Exception):
    """The config file is missing, unreadable or not what the harness writes."""


def config_document(policy, *, cwd, url_corpus, gate) -> dict[str, Any]:
    """What the harness writes for one attempt: the ``WebFetchPolicy`` field by
    field, the provenance corpus (``None`` when the policy does not ask for
    one), the workspace the tool is bound to and the gate FIFO's path."""
    values = {}
    for f in fields(policy):
        value = getattr(policy, f.name)
        values[f.name] = list(value) if isinstance(value, tuple) else value
    return {
        "cwd": str(cwd),
        "policy": values,
        "url_corpus": sorted(url_corpus) if url_corpus is not None else None,
        "gate": str(gate) if gate is not None else None,
    }


def read_config(path: str) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise ConfigError(f"cannot open the config: {exc.strerror or exc}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfigError("the config is not a regular file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_CONFIG_BYTES:
                raise ConfigError("the config is too large")
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        document = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        raise ConfigError("the config is not JSON") from None
    if not isinstance(document, dict):
        raise ConfigError("the config is not a JSON object")
    return document


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def policy_from_document(document: dict[str, Any]):
    """The ``WebFetchPolicy`` in ``document``, every field checked against the
    type of its default. A field of the wrong shape or one the policy does not
    have is refused, never guessed at: a wrong guess could widen egress."""
    from istota.session.tools import WebFetchPolicy

    raw = document.get("policy")
    if not isinstance(raw, dict):
        raise ConfigError("config field 'policy' must be an object")
    known = {f.name: f for f in fields(WebFetchPolicy)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"config field 'policy' has unknown keys {unknown}")
    defaults = WebFetchPolicy()
    values = {}
    for name, value in raw.items():
        default = getattr(defaults, name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            ok = _is_number(value)
            value = float(value) if ok else value
        elif isinstance(default, str):
            ok = isinstance(value, str)
        elif isinstance(default, tuple):
            item_type = int if name == "allowed_ports" else str
            ok = isinstance(value, list) and all(
                isinstance(item, item_type) and not isinstance(item, bool) for item in value
            )
            value = tuple(value) if ok else value
        else:
            ok = False
        if not ok:
            raise ConfigError(f"config field 'policy.{name}' has the wrong type")
        values[name] = value
    return WebFetchPolicy(**values)


def corpus_from_document(document: dict[str, Any]) -> frozenset[str] | None:
    corpus = document.get("url_corpus")
    if corpus is None:
        return None
    if not isinstance(corpus, list) or not all(isinstance(url, str) for url in corpus):
        raise ConfigError("config field 'url_corpus' must be a list of URLs or null")
    return frozenset(corpus)


def build_dispatcher(document: dict[str, Any]):
    """One ``ToolDispatcher`` over WebFetch alone, bound as
    ``_tool_harness.request_web_fetch_tool`` binds native's."""
    from pathlib import Path

    from istota.session.tools import ToolEnv, make_web_fetch_tool
    from istota.session.tools.dispatch import HARNESS_TEXT_CAP_CHARS, ToolDispatcher

    cwd = document.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ConfigError("config field 'cwd' must be a path")
    policy = policy_from_document(document)
    if not policy.enabled:
        raise ConfigError("the policy is disabled")
    tool = make_web_fetch_tool(ToolEnv(
        cwd=Path(cwd),
        web_fetch=policy,
        web_fetch_url_corpus=corpus_from_document(document),
    ))
    return ToolDispatcher(
        {_TOOL_NAME: tool}, offered=[_TOOL_NAME], text_cap=HARNESS_TEXT_CAP_CHARS,
    )


def serve_mcp(dispatcher, gate) -> int:
    from istota.sandbox.tool_server import serve_mcp as serve

    return serve(dispatcher, gate, label="web fetch server")


def main(argv: list[str] | None = None) -> int:
    os.environ.clear()
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "--config":
        print("web fetch server: usage: --config PATH", file=sys.stderr)
        return 2
    try:
        document = read_config(args[1])
        dispatcher = build_dispatcher(document)
        gate = document.get("gate")
        if gate is not None and not isinstance(gate, str):
            raise ConfigError("config field 'gate' must be a path or null")
    except ConfigError as exc:
        print(f"web fetch server: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"web fetch server: cannot start: {type(exc).__name__}", file=sys.stderr)
        return 2
    return serve_mcp(dispatcher, gate)


if __name__ == "__main__":
    raise SystemExit(main())
