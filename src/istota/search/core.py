"""Run independent search sources under bounded concurrency and deadlines."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from typing import Callable

from istota.config import Config
from istota.lib.text_match import Term, parse_query

logger = logging.getLogger(__name__)
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop = None


@dataclass(frozen=True)
class SearchContext:
    config: Config
    user_id: str
    deadline: float


@dataclass
class SearchHit:
    id: str
    kind: str
    title: str
    subtitle: str | None
    snippet: str
    highlights: list[list[int]]
    date: str | None
    link: dict | None
    badges: list[str]
    cursor: dict | None = None


@dataclass
class ProviderResult:
    hits: list[SearchHit]
    has_more: bool


@dataclass(frozen=True)
class Provider:
    source: str
    label: str
    order: int
    module: str | None
    timeout_s: float
    on_demand: bool
    run: Callable[[SearchContext, list[Term], str, int, int], ProviderResult]


@contextmanager
def open_with_deadline(open_fn, deadline):
    """Accept a connection or an existing store's connection context manager."""
    opened = open_fn()
    # sqlite3.Connection.__exit__ commits, but does not close the connection.
    manager = closing(opened) if isinstance(opened, sqlite3.Connection) else opened
    with manager as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            if time.monotonic() > deadline:
                raise TimeoutError
            yield conn
        finally:
            conn.set_progress_handler(None, 0)


def _run_one(provider, ctx, terms, limit, offset):
    start = time.monotonic()
    result = ProviderResult([], False)
    relaxed = False
    error = None
    hits = []
    try:
        result = provider.run(ctx, terms, "strict", limit, offset)
        if time.monotonic() > ctx.deadline:
            raise TimeoutError
        if not result.hits and len(terms) > 1 and provider.source != "money":
            # An empty later page does not mean the strict source has no hits.
            first = provider.run(ctx, terms, "strict", 1, 0) if offset else result
            if not first.hits:
                relaxed = True
                result = provider.run(ctx, terms, "relaxed", limit, offset)
        if time.monotonic() > ctx.deadline:
            raise TimeoutError
        hits = [asdict(hit) for hit in result.hits]
    except Exception as exc:
        timeout = isinstance(exc, TimeoutError) or (
            isinstance(exc, sqlite3.OperationalError)
            and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT
        )
        error = "timeout" if timeout else "failed"
        result = ProviderResult([], False)
        if timeout:
            logger.info("Search source %s timed out after %.0f ms", provider.source, (time.monotonic() - start) * 1000)
        else:
            logger.warning("Search source %s failed (%s)", provider.source, type(exc).__name__)
    return {
        "source": provider.source, "label": provider.label,
        "results": hits, "has_more": result.has_more,
        "relaxed": relaxed, "error": error, "elapsed_ms": round((time.monotonic() - start) * 1000),
    }


async def run_search(config, user_id, q, *, sources=None, limit=5, offset=0):
    from istota.search.registry import providers

    terms = parse_query(q)
    if not any(len(term.text) >= 2 for term in terms):
        return {"query": q, "groups": []}
    available = [p for p in providers() if p.module is None or config.is_module_enabled(user_id, p.module)]
    selected = [p for p in available if (p.source in sources if sources is not None else not p.on_demand)]
    selected.sort(key=lambda p: p.order)
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(8)
        _semaphore_loop = loop

    async def run(provider):
        async with _semaphore:
            ctx = SearchContext(config, user_id, time.monotonic() + provider.timeout_s)
            work = asyncio.to_thread(_run_one, provider, ctx, terms, limit if sources is not None else 5, offset)
            if provider.source == "money":
                try:
                    return await asyncio.wait_for(work, provider.timeout_s + 0.5)
                except TimeoutError:
                    return {"source": provider.source, "label": provider.label, "results": [],
                            "has_more": False, "relaxed": False, "error": "timeout",
                            "elapsed_ms": round((provider.timeout_s + 0.5) * 1000)}
            return await work

    groups = await asyncio.gather(*(run(p) for p in selected))
    return {"query": q, "groups": groups,
            "on_demand": [{"source": p.source, "label": p.label} for p in available if p.on_demand]}
