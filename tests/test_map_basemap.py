"""The basemap provider seam: config in, a resolved MapLibre source out.

The reported failure (ISSUE-334) is that the location maps rendered on CARTO's
keyless raster tiles, which CARTO now watermarks with "API KEY REQUIRED". The
regression these tests hold is not "CARTO works again" — it is that the tile
source is *deployment config* rather than a literal, and that a fresh install
with no configuration at all gets a basemap that needs no credential.
"""

from __future__ import annotations

import pytest

from istota.webui.map_basemap import (
    PROVIDERS,
    BasemapSpec,
    resolve_basemap,
)


class TestTheDefault:
    """A fresh install must render a map with no key and no setup."""

    def test_the_default_provider_needs_no_credential(self):
        spec = resolve_basemap()
        assert spec.provider == "openfreemap"
        assert spec.needs_key is False
        assert spec.warning == ""

    def test_the_default_resolves_a_distinct_style_per_theme(self):
        spec = resolve_basemap()
        assert spec.kind == "style"
        assert spec.dark and spec.light
        assert spec.dark != spec.light

    def test_no_resolved_url_points_at_carto_unless_carto_was_asked_for(self):
        """The bug was a hardcoded vendor. The default must not reintroduce one."""
        spec = resolve_basemap()
        for url in (spec.dark, spec.light):
            assert "cartocdn" not in url


class TestCarto:
    """The stopgap branch: an operator with a key gets the old look back."""

    def test_a_key_is_carried_into_both_tile_urls(self):
        spec = resolve_basemap(provider="carto", api_key="tok_abc123")
        assert spec.kind == "raster"
        for url in (spec.dark, spec.light):
            assert "api_key=tok_abc123" in url
            assert "basemaps.cartocdn.com" in url

    def test_dark_and_light_are_different_carto_flavours(self):
        spec = resolve_basemap(provider="carto", api_key="k")
        assert "dark_all" in spec.dark
        assert "light_all" in spec.light

    def test_carto_without_a_key_falls_back_rather_than_serving_watermarks(self):
        """The reported bug. A flag the browser cannot act on is not a fix."""
        spec = resolve_basemap(provider="carto", api_key="")
        assert spec.provider == "openfreemap"
        assert "cartocdn" not in spec.dark
        assert "cartocdn" not in spec.light

    def test_the_fallback_still_carries_the_reason(self):
        """`needs_key` survives as the reason, so doctor can name it exactly."""
        spec = resolve_basemap(provider="carto", api_key="")
        assert spec.needs_key is True
        assert spec.fell_back is True
        assert spec.warning
        assert "watermark" in spec.warning.lower()

    def test_a_key_is_url_encoded_rather_than_interpolated_raw(self):
        """The key reaches a URL. A stray & would silently truncate it."""
        spec = resolve_basemap(provider="carto", api_key="a&b=c d")
        assert "a&b=c d" not in spec.dark
        assert "a%26b%3Dc%20d" in spec.dark

    def test_a_whitespace_only_key_counts_as_no_key(self):
        """Otherwise `api_key=%20%20%20` is reported as a healthy basemap."""
        spec = resolve_basemap(provider="carto", api_key="   ")
        assert spec.needs_key is True
        assert spec.fell_back is True
        assert "%20" not in spec.dark

    def test_a_key_is_stripped_before_it_travels(self):
        spec = resolve_basemap(provider="carto", api_key="  tok  ")
        assert "api_key=tok" in spec.dark

    def test_the_template_placeholders_survive_encoding(self):
        """MapLibre substitutes {z}/{x}/{y}; percent-encoding them breaks tiles."""
        spec = resolve_basemap(provider="carto", api_key="k")
        assert "{z}/{x}/{y}" in spec.dark


class TestOsm:
    def test_osm_needs_no_key_and_is_raster(self):
        spec = resolve_basemap(provider="osm")
        assert spec.needs_key is False
        assert spec.kind == "raster"

    def test_osm_serves_the_same_light_tiles_to_both_themes_and_says_so(self):
        spec = resolve_basemap(provider="osm")
        assert spec.dark == spec.light
        assert spec.warning


class TestCustom:
    def test_custom_takes_the_operators_own_style_urls(self):
        spec = resolve_basemap(
            provider="custom",
            dark_style="https://tiles.example.test/dark.json",
            light_style="https://tiles.example.test/light.json",
            attribution="&copy; me",
        )
        assert spec.kind == "style"
        assert spec.dark == "https://tiles.example.test/dark.json"
        assert spec.light == "https://tiles.example.test/light.json"
        assert spec.attribution == "&copy; me"

    def test_one_style_url_is_enough_and_covers_both_themes(self):
        spec = resolve_basemap(
            provider="custom", dark_style="https://tiles.example.test/only.json"
        )
        assert spec.dark == spec.light == "https://tiles.example.test/only.json"

    def test_custom_with_no_url_falls_back_rather_than_rendering_nothing(self):
        """A misconfigured custom provider must not leave a blank page."""
        spec = resolve_basemap(provider="custom")
        assert spec.provider == "openfreemap"
        assert spec.warning
        assert spec.fell_back is True

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/shadow",
            "ftp://example.test/s.json",
            "javascript:alert(1)",
            "/etc/passwd",
            "example.test/s.json",
        ],
    )
    def test_a_non_http_style_url_is_refused(self, url):
        """The daemon opens this URL, and the browser loads it."""
        spec = resolve_basemap(provider="custom", dark_style=url)
        assert spec.provider == "openfreemap"
        assert spec.fell_back is True
        assert url not in (spec.dark, spec.light)

    def test_one_bad_url_refuses_the_pair_rather_than_half_working(self):
        spec = resolve_basemap(
            provider="custom",
            dark_style="https://ok.example.test/s.json",
            light_style="file:///etc/shadow",
        )
        assert spec.provider == "openfreemap"

    def test_an_unparseable_url_is_refused_not_raised(self):
        spec = resolve_basemap(provider="custom", dark_style="http://[::1")
        assert spec.provider == "openfreemap"

    def test_plain_http_is_allowed(self):
        """A self-hosted tile server on a LAN is a legitimate deployment."""
        spec = resolve_basemap(provider="custom", dark_style="http://tiles.lan/s.json")
        assert spec.provider == "custom"


class TestUnknownProviders:
    def test_an_unknown_provider_falls_back_to_the_keyless_default(self):
        spec = resolve_basemap(provider="maptiler")
        assert spec.provider == "openfreemap"
        assert "maptiler" in spec.warning

    def test_the_provider_name_is_matched_case_and_space_insensitively(self):
        assert resolve_basemap(provider="  CARTO  ", api_key="k").provider == "carto"

    def test_an_empty_provider_is_the_default_not_an_error(self):
        assert resolve_basemap(provider="").provider == "openfreemap"


class TestTheSpecIsSafeToSerialize:
    """The spec is returned to the browser, so it must be plain JSON data."""

    def test_every_provider_resolves_to_strings_only(self):
        for name in PROVIDERS:
            spec = resolve_basemap(
                provider=name,
                api_key="k",
                dark_style="https://example.test/s.json",
            )
            payload = spec.as_dict()
            assert isinstance(payload, dict)
            for key, value in payload.items():
                assert isinstance(value, (str, bool)), f"{name}.{key} is {type(value)}"

    def test_the_payload_never_carries_the_key_under_its_own_name(self):
        """The key is disclosed in the tile URL by necessity, not duplicated."""
        payload = resolve_basemap(provider="carto", api_key="tok").as_dict()
        assert "api_key" not in payload

    def test_a_spec_is_frozen(self):
        spec = resolve_basemap()
        with pytest.raises(Exception):
            spec.provider = "carto"  # type: ignore[misc]


class TestBasemapSpecDirectly:
    def test_as_dict_round_trips_the_fields_the_frontend_reads(self):
        spec = BasemapSpec(
            provider="custom",
            kind="style",
            dark="d",
            light="l",
            attribution="a",
        )
        payload = spec.as_dict()
        assert payload["provider"] == "custom"
        assert payload["kind"] == "style"
        assert payload["dark"] == "d"
        assert payload["light"] == "l"
        assert payload["attribution"] == "a"


class TestBasemapForUser:
    """`basemap_for_user`: deployment config and a user's stored key, resolved once.

    The web endpoint and the PNG renderer both read it, so a user's map looks
    the same in their browser and in a snapshot posted on their behalf.
    """

    @pytest.fixture
    def config(self, monkeypatch, tmp_path):
        from dataclasses import replace

        from istota import db
        from istota.config import Config, WebMapConfig

        db_path = tmp_path / "basemap.db"
        db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        cfg = Config()
        cfg.db_path = db_path

        def with_map(**kwargs):
            cfg.web = replace(cfg.web, map=WebMapConfig(**kwargs))
            return cfg

        cfg.with_map = with_map  # type: ignore[attr-defined]
        return cfg

    def test_the_default_deployment_resolves_the_keyless_provider(self, config):
        from istota.webui.map_basemap import basemap_for_user

        spec = basemap_for_user(config, "alice")
        assert spec.provider == "openfreemap"
        assert spec.needs_key is False

    def test_a_stored_key_selects_carto_for_that_user_only(self, config):
        from istota.credentials import store as secrets_store
        from istota.webui.map_basemap import basemap_for_user

        secrets_store.set_secret(config.db_path, "alice", "carto", "api_key", "alicekey")
        alice = basemap_for_user(config, "alice")
        bob = basemap_for_user(config, "bob")
        assert alice.provider == "carto"
        assert "api_key=alicekey" in alice.light
        assert bob.provider == "openfreemap"
        assert "alicekey" not in bob.light

    def test_the_deployment_key_applies_when_the_user_has_none(self, config):
        from istota.webui.map_basemap import basemap_for_user

        config.with_map(provider="carto", api_key="deploykey")
        spec = basemap_for_user(config, "alice")
        assert "api_key=deploykey" in spec.light

    def test_a_broken_store_still_resolves_a_usable_basemap(self, config, monkeypatch):
        import istota.credentials.store as store
        from istota.webui.map_basemap import basemap_for_user

        def _boom(*args, **kwargs):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(store, "get_secret", _boom)
        assert basemap_for_user(config, "alice").provider == "openfreemap"

    def test_a_config_with_no_web_section_resolves_the_default(self):
        from types import SimpleNamespace

        from istota.webui.map_basemap import basemap_for_user

        spec = basemap_for_user(SimpleNamespace(db_path=None), "alice")
        assert spec.provider == "openfreemap"

    def test_no_config_resolves_the_default(self):
        from istota.webui.map_basemap import basemap_for_user

        assert basemap_for_user(None, "alice").provider == "openfreemap"
