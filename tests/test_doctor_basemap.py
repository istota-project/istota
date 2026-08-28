"""`web.basemap`: is the configured basemap one that will actually render?

The watermark that prompted ISSUE-334 ran unnoticed because nothing automated
could see it — CARTO returns 200 with a byte-identical PNG whether the key is
good, bad or absent. The check therefore answers from configuration and opens
no socket, and `TestItOpensNoSocket` below is what holds that: a probe could
not see the watermark, and the daemon is not the host that fetches tiles.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from istota.config import Config, WebConfig, WebMapConfig
from istota.doctor import CHECK_SCOPES, CHECKS, FAIL, OK, SKIP, WARN, check_basemap


def _config(**map_kwargs) -> Config:
    cfg = Config()
    cfg.web = replace(WebConfig(enabled=True), map=WebMapConfig(**map_kwargs))
    return cfg


class TestRegistration:
    def test_the_check_is_in_the_registry(self):
        assert "web.basemap" in dict(CHECKS)

    def test_it_declares_a_scope(self):
        assert CHECK_SCOPES["web.basemap"] == "deployment"

    def test_every_result_carries_the_registry_scope(self):
        result = check_basemap(_config(), probe=False)
        assert result.scope == CHECK_SCOPES["web.basemap"]


class TestTheWebGate:
    def test_it_skips_when_the_web_interface_is_off(self):
        cfg = Config()
        cfg.web = WebConfig(enabled=False)
        result = check_basemap(cfg, probe=False)
        assert result.status == SKIP


class TestTheReportedBug:
    """carto with no key is the failure as filed, and it is decided offline."""

    def test_carto_without_a_key_warns_even_with_no_network(self):
        result = check_basemap(_config(provider="carto"), probe=False)
        assert result.status == WARN
        assert "watermark" in result.detail.lower()
        assert result.remedy

    def test_the_remedy_names_both_ways_out(self):
        result = check_basemap(_config(provider="carto"), probe=False)
        assert "carto.com" in result.remedy
        assert "openfreemap" in result.remedy.lower()

    def test_carto_with_a_key_does_not_warn_about_the_watermark(self):
        result = check_basemap(_config(provider="carto", api_key="k"), probe=False)
        assert result.status == OK

    def test_carto_with_a_key_says_the_key_cannot_be_verified(self):
        """Honesty about what the check does not know is the point."""
        result = check_basemap(_config(provider="carto", api_key="k"), probe=True)
        assert result.status == OK
        assert "cannot" in result.detail.lower() or "not verif" in result.detail.lower()

    def test_a_keyless_carto_is_reported_but_not_served(self):
        """WARN names the misconfiguration; the map runs on the fallback."""
        result = check_basemap(_config(provider="carto"), probe=False)
        assert result.status == WARN
        assert "openfreemap" in result.detail


class TestMisconfiguration:
    def test_an_unknown_provider_warns_that_it_fell_back(self):
        result = check_basemap(_config(provider="maptiler"), probe=False)
        assert result.status == WARN
        assert "maptiler" in result.detail

    def test_custom_with_no_url_warns(self):
        result = check_basemap(_config(provider="custom"), probe=False)
        assert result.status == WARN

    def test_a_deliberate_provider_with_a_caveat_is_not_a_warning(self):
        """osm is a documented last resort; nagging every run is noise."""
        result = check_basemap(_config(provider="osm"), probe=False)
        assert result.status == OK
        assert "dark" in result.detail.lower()


class TestNoCredentialInOutput:
    def test_the_key_never_appears_in_the_detail_or_remedy(self):
        key = "supersecretkey123"
        result = check_basemap(_config(provider="carto", api_key=key), probe=False)
        assert key not in result.detail
        assert key not in result.remedy

    def test_a_custom_style_url_is_never_echoed_into_output(self):
        """It can carry a token in its query. Nothing needs to print it."""
        result = check_basemap(
            _config(
                provider="custom",
                dark_style="https://tiles.example.test/s.json?key=abc123secret",
            ),
            probe=False,
        )
        assert "abc123secret" not in result.detail
        assert "abc123secret" not in result.remedy


class TestItOpensNoSocket:
    """The check must not reach the network. See its docstring for why.

    A probe cannot see the watermark, and the daemon is not the host that
    fetches tiles — so a fetch here would add a third-party request to the
    daemon boot path and the hourly sweep in exchange for an answer about the
    wrong machine.
    """

    def test_no_provider_causes_a_network_call(self, monkeypatch):
        import socket

        def _boom(*args, **kwargs):
            raise AssertionError("check_basemap opened a socket")

        monkeypatch.setattr(socket, "create_connection", _boom)
        monkeypatch.setattr(socket.socket, "connect", _boom)
        for provider in ("openfreemap", "carto", "osm", "custom"):
            check_basemap(
                _config(
                    provider=provider,
                    api_key="k",
                    dark_style="https://tiles.example.test/s.json",
                ),
                probe=True,
            )

    def test_probe_true_and_probe_false_agree(self):
        """`probe` is accepted for the Check protocol and changes nothing."""
        for provider in ("openfreemap", "carto", "osm"):
            cfg = _config(provider=provider)
            assert (
                check_basemap(cfg, probe=True).status
                == check_basemap(cfg, probe=False).status
            )


@pytest.mark.parametrize("provider", ["openfreemap", "carto", "osm", "custom"])
def test_no_provider_makes_the_check_raise(provider):
    result = check_basemap(
        _config(provider=provider, dark_style="https://x.test/s.json"), probe=False
    )
    assert result.name == "web.basemap"
    assert result.status in (OK, WARN, FAIL, SKIP)
