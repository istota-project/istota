"""A stand-in for the `weasyprint` module, for hosts where its native libraries do not load.

It carries the two names invoice rendering uses: `HTML(string=..., url_fetcher=...)`
and `URLFetcher(**kwargs)`. One copy, so a change to how invoices call the
renderer is a change to one double rather than to each test that stubbed it.
"""

from __future__ import annotations

import sys
import types


def install(monkeypatch, render=lambda string: b"%PDF") -> dict:
    """Put the stub in `sys.modules`; returns the recorded `HTML` and `URLFetcher` calls.

    `render` maps the HTML string to the bytes `write_pdf` returns.
    """
    calls: dict = {"html": [], "fetchers": [], "fetched": []}

    class URLFetcher:
        def __init__(self, **kwargs):
            calls["fetchers"].append(kwargs)

        def __call__(self, url):
            calls["fetched"].append(url)
            return "fetched"

    def HTML(string, **kwargs):
        calls["html"].append(kwargs)
        return types.SimpleNamespace(write_pdf=lambda **_: render(string))

    fake = types.ModuleType("weasyprint")
    fake.HTML = HTML
    fake.URLFetcher = URLFetcher
    monkeypatch.setitem(sys.modules, "weasyprint", fake)
    return calls
