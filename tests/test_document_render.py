"""`istota.browser.document_render`: the bundle, the envelope, the call, the reasons.

The browser is mocked at `browser_call`, the one function that sends a request
to the browser service, so the admission lock, the headers, the envelope and
the answer handling all run as shipped. The bundle is a real file in a render
directory named by `ISTOTA_WEB_RENDER_DIR`, as the image sets it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

import httpx
import pytest

from istota import db
from istota.browser import document_render
from istota.browser.admission import BrowserQueueTimeout
from istota.config import BrowserConfig, Config

DOCUMENT = "<!doctype html><html><body><div data-render-frame></div></body></html>"
PAYLOAD = {"observation": {"lat": 48.2}, "viewport": {"width": 640, "height": 360}}
VIEWPORT = {"width": 640, "height": 360}


def _write_bundle(directory, name="share-map", text=DOCUMENT, digest=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.html").write_text(text, encoding="utf-8")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest[name] = digest or hashlib.sha256(text.encode()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


@pytest.fixture
def render_dir(tmp_path, monkeypatch):
    directory = tmp_path / "build-render"
    _write_bundle(directory)
    monkeypatch.setenv("ISTOTA_WEB_RENDER_DIR", str(directory))
    document_render.reset_cache()
    yield directory
    document_render.reset_cache()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    cfg.browser = BrowserConfig(enabled=True, api_url="http://browser.test:9223")
    return cfg


class _Browser:
    def __init__(self, response=None, raises=None):
        self.calls = []
        self.response = response
        self.raises = raises

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response


def _answer(body=b"\x89PNG\r\n\x1a\nrest", *, status=200, content_type="image/png", json=None):
    request = httpx.Request("POST", "http://browser.test:9223/render-document")
    if json is not None:
        return httpx.Response(status, json=json, request=request)
    return httpx.Response(status, content=body, headers={"content-type": content_type},
                          request=request)


@pytest.fixture
def browser(monkeypatch):
    def install(**kwargs):
        fake = _Browser(**kwargs)
        monkeypatch.setattr(document_render, "browser_call", fake)
        return fake
    return install


def _render(config, **changes):
    arguments = {"actor_user_id": "bob", "bundle": "share-map", "payload": PAYLOAD,
                 "viewport": VIEWPORT, "timeout_s": 10.0}
    arguments.update(changes)
    return document_render.render_document(config, **arguments)


class TestTheEnvelope:
    def test_it_carries_the_bundle_its_digest_and_the_callers_payload(
            self, config, render_dir, browser):
        fake = browser(response=_answer())

        outcome = _render(config)

        assert outcome == document_render.DocumentRender(b"\x89PNG\r\n\x1a\nrest", None)
        method, url, kwargs = fake.calls[0]
        assert (method, url) == ("post", "http://browser.test:9223/render-document")
        assert kwargs["headers"]["X-Istota-User"] == "bob"
        body = kwargs["json"]
        assert set(body) == {"document", "document_sha256", "payload", "viewport", "output",
                             "timeout_ms"}
        assert body["document"] == DOCUMENT
        assert body["document_sha256"] == hashlib.sha256(DOCUMENT.encode()).hexdigest()
        assert body["payload"] == PAYLOAD
        assert body["viewport"] == VIEWPORT
        assert body["output"] == "png"

    def test_the_page_budget_ends_before_the_http_one(self, config, render_dir, browser):
        fake = browser(response=_answer())

        _render(config, timeout_s=10.0)

        kwargs = fake.calls[0][2]
        assert kwargs["timeout"] <= 10.0
        assert kwargs["json"]["timeout_ms"] < kwargs["timeout"] * 1000
        assert kwargs["json"]["timeout_ms"] <= 15000

    def test_assets_go_base64_under_their_paths(self, config, render_dir, browser):
        fake = browser(response=_answer())

        _render(config, assets={"tiles/tile.png": ("image/png", b"\x89PNG tile")})

        assets = fake.calls[0][2]["json"]["assets"]
        assert assets == {"tiles/tile.png": {
            "content_type": "image/png",
            "data_base64": base64.b64encode(b"\x89PNG tile").decode()}}

    def test_admission_is_short_and_bounded_by_the_total(self, config, render_dir, browser,
                                                         monkeypatch):
        waits = []
        real = document_render.browser_admission

        def admission(**kwargs):
            waits.append(kwargs["queue_timeout"])
            return real(**kwargs)

        monkeypatch.setattr(document_render, "browser_admission", admission)
        browser(response=_answer())

        _render(config, admission_wait_s=2.0)

        assert waits == [2.0]


class TestNoBrowserCall:
    @pytest.mark.parametrize("bundle", ["Share-Map", "../share-map", "share map", "", "x" * 41,
                                        "share-map.html", "a/b"])
    def test_a_malformed_bundle_name(self, config, render_dir, browser, bundle):
        fake = browser(response=_answer())
        assert _render(config, bundle=bundle).reason == "bad_request"
        assert fake.calls == []

    def test_an_unknown_bundle(self, config, render_dir, browser):
        fake = browser(response=_answer())
        assert _render(config, bundle="chart").reason == "render_unavailable"
        assert fake.calls == []

    def test_a_bundle_whose_bytes_are_not_the_manifests(self, config, render_dir, browser, caplog):
        fake = browser(response=_answer())
        (render_dir / "share-map.html").write_text(DOCUMENT + "<!-- changed -->")

        with caplog.at_level(logging.WARNING, logger="istota.browser.document_render"):
            assert _render(config).reason == "render_unavailable"
            assert _render(config).reason == "render_unavailable"

        assert fake.calls == []
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, "logged once per process, not once per share"

    def test_no_render_directory(self, config, browser, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_WEB_RENDER_DIR", str(tmp_path / "nowhere"))
        document_render.reset_cache()
        fake = browser(response=_answer())
        assert _render(config).reason == "render_unavailable"
        assert fake.calls == []

    def test_an_oversized_payload(self, config, render_dir, browser):
        fake = browser(response=_answer())
        payload = {"blob": "x" * document_render.MAX_PAYLOAD_BYTES, "viewport": VIEWPORT}
        assert _render(config, payload=payload).reason == "bad_request"
        assert fake.calls == []

    @pytest.mark.parametrize("viewport", [{"width": 0, "height": 360}, {"width": 640},
                                          {"width": 2001, "height": 1}, {"width": 1.5, "height": 1}])
    def test_a_viewport_out_of_range(self, config, render_dir, browser, viewport):
        fake = browser(response=_answer())
        assert _render(config, viewport=viewport).reason == "bad_request"
        assert fake.calls == []

    def test_assets_over_the_cap(self, config, render_dir, browser):
        fake = browser(response=_answer())
        big = b"\0" * (document_render.MAX_ASSET_BYTES + 1)
        assert _render(config, assets={"a.png": ("image/png", big)}).reason == "bad_request"
        assert fake.calls == []

    def test_a_disabled_browser(self, config, render_dir, browser):
        fake = browser(response=_answer())
        config.browser = BrowserConfig(enabled=False)
        assert _render(config).reason == "render_unavailable"
        assert fake.calls == []


class TestTheCache:
    def test_the_bundle_is_read_once_until_it_changes(self, config, render_dir, browser,
                                                      monkeypatch):
        reads = []
        real = document_render._read_bytes

        def counting(path):
            reads.append(path.name)
            return real(path)

        monkeypatch.setattr(document_render, "_read_bytes", counting)
        fake = browser(response=_answer())

        _render(config)
        _render(config)
        assert reads.count("share-map.html") == 1

        _write_bundle(render_dir, text=DOCUMENT + "<p>new</p>")
        stat = (render_dir / "share-map.html").stat()
        os.utime(render_dir / "share-map.html", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        _render(config)

        assert reads.count("share-map.html") == 2
        assert fake.calls[-1][2]["json"]["document"].endswith("<p>new</p>")


class TestTheAnswer:
    @pytest.mark.parametrize("answer,reason,detail", [
        (_answer(status=503, json={"status": "error", "error": "capacity"}), "capacity", None),
        # The pool's own refusal, before the endpoint runs (browse_api._capacity_response).
        (_answer(status=503, json={"status": "error", "error": "Browser pool is full",
                                   "retry_after_seconds": 30}), "capacity", None),
        (_answer(status=504, json={"status": "error", "error": "render_timeout"}),
         "render_timeout", None),
        (_answer(status=502, json={"status": "error", "error": "destination_blocked"}),
         "render_failed", None),
        (_answer(status=502, json={"status": "error", "error": "render_failed",
                                   "detail": "tile_error"}), "render_failed", "tile_error"),
        (_answer(status=502, json={"status": "error", "error": "render_failed",
                                   "detail": "https://x.example/?k=secret"}), "render_failed", None),
        (_answer(status=400, json={"status": "error", "error": "bad_request"}),
         "render_failed", None),
        (_answer(status=404, json={"status": "error", "error": "Not Found"}),
         "render_unavailable", None),
        (_answer(b"<html>", content_type="text/html"), "render_failed", None),
    ])
    def test_an_answer_that_is_not_a_picture(self, config, render_dir, browser, answer, reason,
                                             detail):
        browser(response=answer)
        outcome = _render(config)
        assert (outcome.data, outcome.reason, outcome.detail) == (None, reason, detail)

    @pytest.mark.parametrize("error,reason", [
        (httpx.ReadTimeout("slow"), "render_timeout"),
        (httpx.ConnectError("refused"), "render_unavailable"),
        (BrowserQueueTimeout("busy"), "capacity"),
        (RuntimeError("anything"), "render_failed"),
    ])
    def test_a_failed_call(self, config, render_dir, browser, error, reason):
        browser(raises=error)
        assert _render(config).reason == reason

    def test_the_log_carries_neither_payload_nor_document(self, config, render_dir, browser,
                                                         caplog):
        browser(response=_answer(status=504, json={"error": "render_timeout"}))
        payload = {"marker": "payload-marker-48.2", "viewport": VIEWPORT}

        with caplog.at_level(logging.DEBUG, logger="istota.browser.document_render"):
            _render(config, payload=payload)

        assert "render_timeout" in caplog.text
        assert "payload-marker" not in caplog.text
        assert "data-render-frame" not in caplog.text
