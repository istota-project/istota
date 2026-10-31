"""The origin comparison the two web apps and the mobile handoff share."""

import pytest

from istota.config import Config, SiteConfig
from istota.webui.origin import external_origin
from istota.webui.request_guards import origin_key, origin_refusal, published_authority


class TestOriginRefusal:
    @pytest.mark.parametrize("origin", [
        "https://app.example.com",
        "https://app.example.com:443",
        "https://APP.example.com",
    ])
    def test_the_configured_origin_passes(self, origin):
        assert origin_refusal({"origin": origin}, "app.example.com", "https") is None

    @pytest.mark.parametrize("origin", [
        "http://app.example.com",
        "http://app.example.com:443",
        "https://app.example.com:8088",
        "http://app.example.com:8088",
        "https://attacker.example",
        "https://app.example.com.attacker.example",
        "https://app.example.com:notaport",
        "ftp://app.example.com",
        "null",
    ])
    def test_a_sibling_or_foreign_origin_is_refused(self, origin):
        assert origin_refusal({"origin": origin}, "app.example.com", "https") == "origin mismatch"

    def test_a_port_in_the_hostname_is_the_port_expected(self):
        assert origin_refusal({"origin": "http://localhost:8181"}, "localhost:8181", "http") is None
        assert origin_refusal({"origin": "http://localhost:8182"}, "localhost:8181", "http") == "origin mismatch"
        assert origin_refusal({"origin": "http://localhost"}, "localhost:8181", "http") == "origin mismatch"

    def test_a_referer_is_compared_the_same_way(self):
        assert origin_refusal({"referer": "https://app.example.com/istota/x?y=1"}, "app.example.com", "https") is None
        assert origin_refusal({"referer": "https://app.example.com:8088/x"}, "app.example.com", "https") == "origin mismatch"

    def test_no_origin_is_its_own_reason(self):
        assert origin_refusal({}, "app.example.com", "https") == "missing origin"

    def test_with_no_scheme_named_either_scheme_passes_on_its_default_port(self):
        assert origin_refusal({"origin": "https://setup.example"}, "setup.example") is None
        assert origin_refusal({"origin": "http://setup.example"}, "setup.example") is None
        assert origin_refusal({"origin": "http://setup.example:8080"}, "setup.example") == "origin mismatch"
        assert origin_refusal({"origin": "http://setup.example:8080"}, "setup.example:8080") is None

    @pytest.mark.parametrize("authority", ["", "["])
    def test_an_authority_with_no_name_matches_nothing(self, authority):
        assert origin_refusal({"origin": "null"}, authority) == "origin mismatch"
        assert origin_refusal({"origin": "http://x"}, authority) == "origin mismatch"


class TestOriginKey:
    def test_the_default_port_is_filled_in(self):
        assert origin_key("https://Example.com") == ("https", "example.com", 443)
        assert origin_key("http://example.com") == ("http", "example.com", 80)
        assert origin_key("http://[::1]:8080") == ("http", "::1", 8080)

    @pytest.mark.parametrize("url", ["null", "", "ftp://x", "http://", "http://x:99999", "http://["])
    def test_what_is_not_a_web_origin_has_no_key(self, url):
        assert origin_key(url) is None


class TestPublishedAuthority:
    def test_a_portless_hostname_takes_the_port_of_a_host_with_its_name(self):
        assert published_authority("localhost", "localhost:8080") == "localhost:8080"
        assert published_authority("localhost", "LOCALHOST:8080") == "LOCALHOST:8080"

    @pytest.mark.parametrize("hostname, host, expected", [
        ("localhost:8181", "localhost:9999", "localhost:8181"),
        ("localhost", "evil.example:8080", "localhost"),
        ("localhost", "127.0.0.1:8080", "localhost"),
        ("localhost", "", "localhost"),
        ("localhost", "[", "localhost"),
    ])
    def test_everything_else_keeps_the_hostname(self, hostname, host, expected):
        assert published_authority(hostname, host) == expected


class TestExternalOriginScheme:
    @pytest.mark.parametrize("hostname, scheme", [
        ("localhost", "http"),
        ("localhost:8181", "http"),
        ("127.0.0.1:8080", "http"),
        ("[::1]:8080", "http"),
        ("::1", "http"),
        ("LOCALHOST:8181", "http"),
        ("app.example.com", "https"),
        ("app.example.com:8443", "https"),
    ])
    def test_only_a_loopback_name_is_plain_http(self, hostname, scheme):
        config = Config(site=SiteConfig(hostname=hostname))
        assert external_origin(config) == (hostname, scheme)
