"""A location share's map as a PNG: the browser call, the PNG checks and the fallbacks.

The browser is mocked at `browser_call`, the one function that sends a request
to the browser service, so the admission lock, the headers, the payload and the
answer handling all run as shipped.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import stat

import httpx
import pytest
from PIL import Image

from istota import db
from istota.browser.admission import BrowserQueueTimeout
from istota.config import BrowserConfig, Config
from istota.location import share_maps
from istota.location.logic import share_map_url


OBSERVATION = {
    "owner_user_id": "alice",
    "owner_label": "Alice",
    "precision": "exact",
    "lat": 48.208176,
    "lon": 16.373819,
    "accuracy_m": 12.0,
    "observed_at": "2026-10-16T14:02:00+00:00",
    "freshness": "fresh",
    "map_url": share_map_url("exact", 48.208176, 16.373819),
}


def _png(width=640, height=360, color=(120, 160, 200), **info) -> bytes:
    buffer = io.BytesIO()
    picture = Image.new("RGB", (width, height), color)
    picture.save(buffer, "PNG", **info)
    return buffer.getvalue()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    db.init_db(cfg.db_path)
    cfg.browser = BrowserConfig(enabled=True, api_url="http://browser.test:9223")
    return cfg


class _Browser:
    """Records what reached the browser service and answers with a fixed response."""

    def __init__(self, response=None, raises=None):
        self.calls = []
        self.response = response
        self.raises = raises

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response


def _answer(body: bytes = b"", *, status=200, content_type="image/png", json=None):
    request = httpx.Request("POST", "http://browser.test:9223/map-snapshot")
    if json is not None:
        return httpx.Response(status, json=json, request=request)
    return httpx.Response(status, content=body, headers={"content-type": content_type},
                          request=request)


@pytest.fixture
def browser(monkeypatch):
    def install(**kwargs):
        fake = _Browser(**kwargs)
        monkeypatch.setattr(share_maps, "browser_call", fake)
        return fake
    return install


class TestTheRender:
    def test_a_good_png_comes_back_reencoded_with_its_digest(self, config, browser):
        source = _png(pnginfo=_text_chunk("Comment", "from the browser"))
        fake = browser(response=_answer(source))

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation=OBSERVATION)

        assert outcome.reason is None
        rendered = outcome.rendered
        assert (rendered.width, rendered.height) == (640, 360)
        assert rendered.sha256 == hashlib.sha256(rendered.data).hexdigest()
        assert rendered.data.startswith(b"\x89PNG\r\n\x1a\n")
        # Re-encoded through the WhatsApp path, which drops every text chunk.
        assert b"from the browser" not in rendered.data
        assert len(fake.calls) == 1

    def test_the_request_carries_the_actor_and_only_the_projection(self, config, browser):
        fake = browser(response=_answer(_png()))

        share_maps.render_location_png(config, actor_user_id="bob", observation=OBSERVATION)

        method, url, kwargs = fake.calls[0]
        assert method == "post"
        assert url == "http://browser.test:9223/map-snapshot"
        assert kwargs["headers"]["X-Istota-User"] == "bob"
        body = kwargs["json"]
        assert set(body) == {"observation", "basemap", "owner"}
        assert body["owner"] == "Alice"
        assert body["observation"] == {
            "precision": "exact", "lat": 48.208176, "lon": 16.373819, "accuracy_m": 12.0,
            "observed_at": "2026-10-16T14:02:00+00:00", "freshness": "fresh",
            "owner_user_id": "alice",
        }
        assert set(body["basemap"]) == {"provider", "kind", "light", "dark", "attribution"}
        assert body["basemap"]["light"].startswith("https://")
        assert kwargs["timeout"] <= share_maps.TOTAL_TIMEOUT_S

    def test_the_basemap_is_the_actors_own(self, config, browser, monkeypatch):
        seen = []

        def basemap_for_user(cfg, user_id):
            seen.append(user_id)
            from istota.webui.map_basemap import resolve_basemap
            return resolve_basemap("osm")

        monkeypatch.setattr(share_maps, "basemap_for_user", basemap_for_user)
        fake = browser(response=_answer(_png()))

        share_maps.render_location_png(config, actor_user_id="bob", observation=OBSERVATION)

        assert seen == ["bob"]
        assert fake.calls[0][2]["json"]["basemap"]["provider"] == "osm"

    def test_an_area_share_sends_no_accuracy(self, config, browser):
        fake = browser(response=_answer(_png()))
        area = {**OBSERVATION, "precision": "area", "lat": 48.21, "lon": 16.37,
                "accuracy_m": None, "map_url": share_map_url("area", 48.21, 16.37)}

        share_maps.render_location_png(config, actor_user_id="bob", observation=area)

        sent = fake.calls[0][2]["json"]["observation"]
        assert sent["precision"] == "area"
        assert sent["accuracy_m"] is None


class TestNoBrowserCall:
    def test_a_latitude_outside_web_mercator_is_skipped(self, config, browser):
        fake = browser(response=_answer(_png()))
        polar = {**OBSERVATION, "lat": 86.0}

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation=polar)

        assert outcome.rendered is None
        assert outcome.reason == "not_mercator_safe"
        assert outcome.reason in share_maps.SKIP_REASONS
        assert fake.calls == []

    def test_a_disabled_browser_is_skipped(self, config, browser):
        fake = browser(response=_answer(_png()))
        config.browser = BrowserConfig(enabled=False)

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation=OBSERVATION)

        assert outcome.reason == "render_unavailable"
        assert outcome.reason in share_maps.SKIP_REASONS
        assert fake.calls == []

    @pytest.mark.parametrize("change", [
        {"precision": "street"},
        {"lat": float("nan")},
        {"lon": 181.0},
        {"lat": True},
        {"accuracy_m": -1},
        {"observed_at": ""},
    ])
    def test_an_observation_out_of_shape_is_not_sent(self, config, browser, change):
        fake = browser(response=_answer(_png()))

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation={**OBSERVATION, **change})

        assert outcome.reason == "bad_observation"
        assert fake.calls == []


class TestTheFallbacks:
    @pytest.mark.parametrize("answer,reason", [
        (_answer(status=503, json={"status": "error", "error": "render_unavailable"}),
         "render_unavailable"),
        (_answer(status=503, json={"status": "error", "error": "capacity"}), "capacity"),
        (_answer(status=504, json={"status": "error", "error": "render_timeout"}),
         "render_timeout"),
        (_answer(status=502, json={"status": "error", "error": "destination_blocked"}),
         "render_failed"),
        (_answer(status=404, json={"status": "error", "error": "Not Found"}),
         "render_unavailable"),
        (_answer(b"<html>", content_type="text/html"), "bad_png"),
    ])
    def test_an_answer_that_is_not_a_map(self, config, browser, answer, reason):
        browser(response=answer)

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation=OBSERVATION)

        assert (outcome.rendered, outcome.reason) == (None, reason)

    @pytest.mark.parametrize("body", [
        b"GIF89a" + b"\0" * 64,
        _png(width=1280, height=720),
        _png(width=640, height=361),
        b"\x89PNG\r\n\x1a\n" + b"\0" * (share_maps.MAX_PNG_BYTES + 1),
        b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR\0\0\x02\x80\0\0\x01h" + b"not a png body",
    ], ids=["signature", "dimensions", "one_row_more", "oversize", "truncated"])
    def test_a_png_that_fails_the_checks(self, config, browser, body):
        browser(response=_answer(body))

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation=OBSERVATION)

        assert (outcome.rendered, outcome.reason) == (None, "bad_png")

    @pytest.mark.parametrize("error,reason", [
        (httpx.ReadTimeout("slow"), "render_timeout"),
        (httpx.ConnectError("refused"), "render_unavailable"),
        (BrowserQueueTimeout("busy"), "capacity"),
    ])
    def test_a_failed_call(self, config, browser, error, reason):
        browser(raises=error)

        outcome = share_maps.render_location_map(
            config, actor_user_id="bob", observation=OBSERVATION)

        assert (outcome.rendered, outcome.reason) == (None, reason)

    def test_the_admission_wait_is_short(self, config, browser, monkeypatch):
        waits = []
        real = share_maps.browser_admission

        def admission(**kwargs):
            waits.append(kwargs["queue_timeout"])
            return real(**kwargs)

        monkeypatch.setattr(share_maps, "browser_admission", admission)
        browser(response=_answer(_png()))

        share_maps.render_location_png(config, actor_user_id="bob", observation=OBSERVATION)

        assert waits == [share_maps.ADMISSION_WAIT_S]

    def test_the_log_names_the_reason_and_never_the_place(self, config, browser, caplog):
        browser(response=_answer(status=504, json={"error": "render_timeout"}))

        with caplog.at_level(logging.DEBUG, logger="istota.location.share_maps"):
            assert share_maps.render_location_png(
                config, actor_user_id="bob", observation=OBSERVATION) is None

        text = caplog.text
        assert "render_timeout" in text
        for leak in ("48.20", "16.37", "openstreetmap", "tiles.openfreemap"):
            assert leak not in text


class TestThePrivateFile:
    def test_written_private_under_an_opaque_name_and_removed(self, config, browser, tmp_path):
        browser(response=_answer(_png()))
        rendered = share_maps.render_location_png(
            config, actor_user_id="bob", observation=OBSERVATION)
        directory = tmp_path / "task-temp"
        directory.mkdir()

        with share_maps.private_file(rendered, directory) as path:
            assert path.parent == directory
            assert path.read_bytes() == rendered.data
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert "alice" not in path.name and "48" not in path.name
        assert not path.exists()

    def test_removed_when_the_delivery_raises(self, config, browser, tmp_path):
        browser(response=_answer(_png()))
        rendered = share_maps.render_location_png(
            config, actor_user_id="bob", observation=OBSERVATION)

        with pytest.raises(RuntimeError):
            with share_maps.private_file(rendered, tmp_path) as path:
                raise RuntimeError("upload failed")
        assert not path.exists()


def _text_chunk(key, value):
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    return info
