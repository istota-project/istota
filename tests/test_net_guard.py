"""The resolve-and-check half of the SSRF rule in `sandbox.net_guard`.

Three host-side callers connect to a host somebody typed: `mail.connect`, the
`wordpress` client and the `servarr` client. They resolve it, refuse it when
any address is not public unless the operator listed the exact name, and
connect to an address they checked. The resolver, the listed-name normaliser
and the check live here once, so a fix to one cannot miss the others.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from istota.sandbox import net_guard
from tests.support.drift import source_of


def _infos(*addresses):
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses]


class TestNormalHost:
    @pytest.mark.parametrize("raw", ["Mail.Example.", " mail.example ", "MAIL.EXAMPLE"])
    def test_case_space_and_the_root_dot(self, raw):
        assert net_guard.normal_host(raw) == "mail.example"

    def test_the_listed_set_drops_blanks(self):
        assert net_guard.host_set(["Mail.Example.", "  ", ""]) == frozenset(
            {"mail.example"},
        )

    def test_no_hosts_is_empty(self):
        assert net_guard.host_set(None) == frozenset()


class TestHostAddresses:
    def test_a_literal_is_not_resolved(self):
        def resolve(host, port):
            raise AssertionError("a literal went to the resolver")
        assert net_guard.host_addresses("10.0.0.5", 993, resolve=resolve) == [
            ipaddress.ip_address("10.0.0.5"),
        ]

    def test_a_zone_is_dropped_and_repeats_collapse(self):
        found = net_guard.host_addresses(
            "mail.example", 993,
            resolve=lambda h, p: ["fe80::1%eth0", "fe80::1%eth1", "203.0.113.7", "203.0.113.7"],
        )
        assert found == [ipaddress.ip_address("fe80::1"), ipaddress.ip_address("203.0.113.7")]

    def test_garbage_from_the_resolver_is_skipped(self):
        assert net_guard.host_addresses("x.example", 1, resolve=lambda h, p: ["nope"]) == []

    def test_the_system_resolver_fails_to_empty(self, monkeypatch):
        def broken(*a, **k):
            raise socket.gaierror(-2, "Name or service not known")
        monkeypatch.setattr(socket, "getaddrinfo", broken)
        assert net_guard.resolve_host("nowhere.example", 443) == []
        assert net_guard.host_addresses("nowhere.example", 443) == []

    def test_the_system_resolver_keeps_order(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *a, **k: _infos("203.0.113.7", "10.0.0.5", "203.0.113.7"))
        assert net_guard.resolve_host("mail.example", 993) == ["203.0.113.7", "10.0.0.5"]


class TestFirstNonPublic:
    def test_any_private_address_is_named(self):
        addresses = [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("192.168.1.9")]
        assert str(net_guard.first_non_public(addresses)) == "192.168.1.9"

    def test_public_only_is_none(self):
        assert net_guard.first_non_public([ipaddress.ip_address("93.184.216.34")]) is None


class TestOneCopy:
    """The callers hold no resolver or private-host normaliser of their own."""

    @pytest.mark.parametrize("module_name", [
        "istota.mail.connect",
        "istota.skills.wordpress.client",
        "istota.skills.servarr.client",
    ])
    def test_no_caller_resolves_for_itself(self, module_name):
        import importlib
        module = importlib.import_module(module_name)
        text = source_of(module)
        assert "getaddrinfo" not in text
        assert ".rstrip(\".\")" not in text
        assert "split(\"%\"" not in text


class TestCheckedAddresses:
    """The composed rule: resolve, then refuse any non-public answer unless listed."""

    def test_a_public_answer_passes_as_strings(self):
        addresses, refused = net_guard.checked_addresses(
            "site.example", 443, private_hosts=[], resolve=lambda h, p: ["93.184.216.34"],
        )
        assert (addresses, refused) == (["93.184.216.34"], None)

    def test_a_private_address_is_refused_unlisted(self):
        addresses, refused = net_guard.checked_addresses(
            "site.example", 443, private_hosts=[], resolve=lambda h, p: ["10.0.0.5"],
        )
        assert addresses == []
        assert str(refused) == "10.0.0.5"

    def test_a_private_address_is_allowed_when_the_exact_name_is_listed(self):
        addresses, refused = net_guard.checked_addresses(
            "Site.Example.", 443, private_hosts=["site.example"],
            resolve=lambda h, p: ["10.0.0.5"],
        )
        assert (addresses, refused) == (["10.0.0.5"], None)

    def test_one_public_and_one_private_answer_is_refused(self):
        addresses, refused = net_guard.checked_addresses(
            "site.example", 443, private_hosts=["other.example"],
            resolve=lambda h, p: ["93.184.216.34", "192.168.1.9"],
        )
        assert addresses == []
        assert str(refused) == "192.168.1.9"

    def test_nothing_resolved_is_empty_with_nothing_refused(self):
        assert net_guard.checked_addresses(
            "nowhere.example", 443, private_hosts=[], resolve=lambda h, p: [],
        ) == ([], None)

    def test_a_private_literal_is_refused_unlisted(self):
        addresses, refused = net_guard.checked_addresses("127.0.0.1", 80, private_hosts=[])
        assert addresses == [] and str(refused) == "127.0.0.1"
