"""A job grant naming `host:port`: what parses, what the operator side has to say,
and what reaches the proxy (#924)."""

from __future__ import annotations

import json

import pytest

from istota import db
from istota.config import Config, EgressConfig, NetworkConfig, NextcloudConfig, SecurityConfig
from istota.credentials.broker.bindings import parse_binding, put_binding
from istota.executor import (
    _build_network_allowlist,
    _egress_grants_in_force,
    _network_private_hosts,
)
from istota.rooms import egress
from istota.sandbox import task_env

USER = "alice"
SUBJECT = "cron:logs"
ENDPOINT = "192.168.1.10:8080"
ADDRESS = "192.168.1.10"
UNLISTED = "192.168.1.11:8080"


def _config(tmp_path, *, private=(), extra=(), admins=frozenset({USER})):
    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://nc.example.com"),
        security=SecurityConfig(
            sandbox_enabled=True, skill_proxy_enabled=True,
            network=NetworkConfig(enabled=True, extra_hosts=list(extra)),
        ),
        egress=EgressConfig(private_hosts=list(private)),
        admin_users=set(admins),
    )
    db.init_db(config.db_path)
    return config


def _bind(config, url, *, source="local", user=USER):
    with db.get_db(config.db_path) as conn:
        put_binding(conn, user, "tower", parse_binding(url, {}, [], source=source))
        conn.commit()


def _allowed(config, hosts, user=USER):
    with db.get_db(config.db_path) as conn:
        return egress.operator_endpoints(conn, config, user).allows(hosts)


class TestParse:
    @pytest.mark.parametrize("text,kind,value", [
        (ENDPOINT, "endpoint", ENDPOINT),
        ("Tower.Example.COM:8080", "endpoint", "tower.example.com:8080"),
        ("10.0.0.5:443", "endpoint", "10.0.0.5:443"),
        ("[FD00::1]:8080", "endpoint", "[fd00::1]:8080"),
        ("[fd00:0:0::1]:8080", "endpoint", "[fd00::1]:8080"),
        # A name at 443 is what a plain grant already is, with one spelling.
        ("example.com:443", "host", "example.com"),
    ])
    def test_accepts(self, text, kind, value):
        entry = egress.parse_entry(text)
        assert (entry.kind, entry.value) == (kind, value)
        assert entry.text == value
        assert egress.parse_entry(entry.text) == entry

    @pytest.mark.parametrize("text", [
        f"{ENDPOINT}/path",
        f"http://{ENDPOINT}",
        f"user@{ENDPOINT}",
        "*.example.com:8080",
        "example.com:",
        "example.com:0",
        "example.com:65536",
        "example.com:12a",
        "example.com:+80",
        "example.com:٣",
        "example.com:80:80",
        "example.com:123456",
        # `int` raises a plain ValueError past 4300 digits, which no caller catches.
        "example.com:" + "9" * 5000,
        "10.0.0.1:" + "9" * 5000,
        "localhost:8080",
        "1.2.3:8080",
        "127.1:8080",
        "0x7f.0.0.1:8080",
        "fd00::1:8080",
        "[fd00::1]",
        "[fd00::1]8080",
        "[not-an-address]:8080",
        # No grant names the daemon's own loopback, or the cloud metadata address.
        "127.0.0.1:8080",
        "[::1]:8080",
        "169.254.169.254:80",
        "0.0.0.0:80",
        "224.0.0.1:80",
        "[::ffff:127.0.0.1]:80",
    ])
    def test_refuses(self, text):
        with pytest.raises(egress.EntryRefused) as caught:
            egress.parse_entry(text)
        assert str(caught.value)

    def test_a_bare_address_says_it_needs_a_port(self):
        with pytest.raises(egress.EntryRefused, match="port"):
            egress.parse_entry(ADDRESS)

    def test_a_network_seam_cannot_file_an_endpoint(self):
        """A refused CONNECT logs a host. Only a person or the job's text names a port."""
        assert egress.entries_from_log([("connect", ENDPOINT), ("web_fetch", ENDPOINT)]) == set()


class TestWhatTheOperatorAllows:
    def test_nothing_written_allows_nothing(self, tmp_path):
        assert not _allowed(_config(tmp_path), ENDPOINT)

    def test_extra_hosts_allow_that_exact_endpoint(self, tmp_path):
        config = _config(tmp_path, extra=[ENDPOINT])
        assert _allowed(config, ENDPOINT)
        assert not _allowed(config, f"{ADDRESS}:8081")

    def test_an_egress_grant_in_the_table_allows_any_port_on_that_host(self, tmp_path):
        config = _config(tmp_path, private=[ADDRESS])
        assert _allowed(config, ENDPOINT)
        assert _allowed(config, f"{ADDRESS}:22")
        assert not _allowed(config, UNLISTED)

    def test_the_table_compares_an_ipv6_address_by_value(self, tmp_path):
        assert _allowed(_config(tmp_path, private=["FD00:0::1"]), "[fd00::1]:8080")

    def test_the_writer_of_the_file_is_held_to_the_same_rule(self, tmp_path):
        """A `once` job asking for what nobody can add would wait on a button that refuses."""
        from istota.cron_loader import _validate_egress

        errors = []
        requests, kept = _validate_egress("j", _config(tmp_path), USER, [ENDPOINT], errors)
        assert requests == ()
        assert kept == (ENDPOINT,)
        assert "not one this deployment allows" in errors[0]
        assert _validate_egress("j", _config(tmp_path, private=[ADDRESS]), USER, [ENDPOINT]) == (
            (ENDPOINT,), (),
        )

    @pytest.mark.parametrize("consumer", ["browser", "feeds", "heartbeat", "servarr"])
    def test_another_consumers_grant_allows_nothing(self, tmp_path, consumer):
        """The table's rule: a grant reaches only the consumers it names."""
        config = _config(tmp_path)
        getattr(config, consumer).private_hosts = [ADDRESS]
        assert not _allowed(config, ENDPOINT)

    @pytest.mark.parametrize("url,endpoint", [
        (f"http://{ENDPOINT}", ENDPOINT),
        (f"https://{ENDPOINT}", ENDPOINT),
        (f"http://{ADDRESS}", f"{ADDRESS}:80"),
        (f"https://{ADDRESS}", f"{ADDRESS}:443"),
        ("http://[fd00::1]:8080", "[fd00::1]:8080"),
    ])
    def test_a_binding_a_person_wrote_allows_that_exact_endpoint(self, tmp_path, url, endpoint):
        config = _config(tmp_path)
        _bind(config, url)
        assert _allowed(config, endpoint)
        assert not _allowed(config, UNLISTED)

    def test_a_binding_on_the_other_port_allows_nothing(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, f"http://{ADDRESS}:8081")
        assert not _allowed(config, ENDPOINT)

    def test_a_binding_a_task_wrote_allows_nothing(self, tmp_path):
        """`istota-credential new` takes a URL from the model, so that binding is the
        model choosing a LAN target."""
        config = _config(tmp_path)
        _bind(config, f"http://{ENDPOINT}", source="generated")
        assert not _allowed(config, ENDPOINT)

    def test_a_non_admins_binding_allows_nothing(self, tmp_path):
        """Anyone can add a credential, and only the operator decides what is on the LAN."""
        config = _config(tmp_path, admins={"root-user"})
        _bind(config, f"http://{ENDPOINT}")
        assert not _allowed(config, ENDPOINT)

    def test_an_accepted_shares_binding_allows_nothing(self, tmp_path):
        """A `local` row, whose URL the share's sender chose."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            put_binding(conn, USER, "ocm_share_abc", parse_binding(f"https://{ENDPOINT}", {}, []))
            conn.commit()
            assert not egress.operator_endpoints(conn, config, USER).allows(ENDPOINT)

    def test_another_users_binding_allows_nothing(self, tmp_path):
        config = _config(tmp_path, admins={USER, "bob"})
        _bind(config, f"http://{ENDPOINT}", user="bob")
        assert not _allowed(config, ENDPOINT)


class TestGrantTime:
    def _add(self, config, entry):
        with db.get_db(config.db_path) as conn:
            allowed = egress.operator_endpoints(conn, config, USER)
            egress.add_grant(conn, USER, SUBJECT, entry, decided_via="web", operator=allowed)
            conn.commit()
            return egress.approved_hosts(conn, USER, SUBJECT)

    def test_an_endpoint_the_operator_allows_is_stored(self, tmp_path):
        assert self._add(_config(tmp_path, private=[ADDRESS]), ENDPOINT) == {ENDPOINT}

    def test_one_the_operator_does_not_allow_is_refused_in_words(self, tmp_path):
        config = _config(tmp_path)
        with pytest.raises(egress.EntryRefused, match="not one this deployment allows"):
            self._add(config, ENDPOINT)
        with db.get_db(config.db_path) as conn:
            assert egress.grants_for(conn, USER, SUBJECT) == frozenset()

    def test_a_caller_naming_no_operator_side_cannot_add_one(self, tmp_path):
        config = _config(tmp_path, private=[ADDRESS])
        with db.get_db(config.db_path) as conn, pytest.raises(egress.EntryRefused):
            egress.add_grant(conn, USER, SUBJECT, ENDPOINT, decided_via="cli")

    def test_a_plain_name_needs_nothing_from_the_operator(self, tmp_path):
        assert self._add(_config(tmp_path), "example.com") == {"example.com"}

    def test_the_page_shows_it_among_the_hosts(self, tmp_path):
        config = _config(tmp_path, private=[ADDRESS])
        self._add(config, ENDPOINT)
        with db.get_db(config.db_path) as conn:
            assert egress.describe_subject(conn, USER, SUBJECT)["hosts"] == [ENDPOINT]


class TestWhatTheTaskGets:
    def _in_force(self, config, granted):
        return _egress_grants_in_force(config, USER, frozenset(granted))

    def _lists(self, config, granted):
        hosts, private = self._in_force(config, granted)
        return (
            _build_network_allowlist(config, [], egress_hosts=hosts),
            _network_private_hosts(config, [], egress_hosts=hosts, private_grants=private),
        )

    def test_a_bound_endpoint_is_on_both_lists(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, f"http://{ENDPOINT}")
        allowed, private = self._lists(config, {ENDPOINT})
        assert ENDPOINT in allowed
        assert f"{ADDRESS}:443" not in allowed
        assert ADDRESS in private

    def test_without_the_operator_side_it_is_on_neither(self, tmp_path):
        allowed, private = self._lists(_config(tmp_path), {ENDPOINT})
        assert not [host for host in allowed if host.startswith(ADDRESS)]
        assert ADDRESS not in private

    def test_withdrawing_the_operator_side_withdraws_a_stored_grant(self, tmp_path):
        config = _config(tmp_path, private=[ADDRESS])
        assert ENDPOINT in self._lists(config, {ENDPOINT})[0]
        config.egress.private_hosts = []
        assert ENDPOINT not in self._lists(config, {ENDPOINT})[0]

    def test_a_plain_name_is_still_443_only_and_public_only(self, tmp_path):
        allowed, private = self._lists(_config(tmp_path), {"example.com"})
        assert [host for host in allowed if host.startswith("example.com:")] == ["example.com:443"]
        assert "example.com" not in private

    def test_a_binding_on_a_plain_name_leaves_it_public_only(self, tmp_path):
        """A listed name skips the proxy's address check, so its DNS could reach loopback."""
        config = _config(tmp_path)
        _bind(config, "https://api.vendor.example")
        allowed, private = self._lists(config, {"api.vendor.example"})
        assert "api.vendor.example:443" in allowed
        assert "api.vendor.example" not in private

    def test_a_binding_on_a_named_endpoint_leaves_it_public_only(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, "https://tower.example.com:8080")
        allowed, private = self._lists(config, {"tower.example.com:8080"})
        assert "tower.example.com:8080" in allowed
        assert "tower.example.com" not in private

    def test_a_plain_name_the_operator_allows_may_resolve_private(self, tmp_path):
        allowed, private = self._lists(_config(tmp_path, private=["wiki.example.com"]), {"wiki.example.com"})
        assert "wiki.example.com:443" in allowed
        assert "wiki.example.com" in private

    def test_a_named_endpoint_private_on_its_own_name(self, tmp_path):
        allowed, private = self._lists(
            _config(tmp_path, private=["tower.example.com"]), {"tower.example.com:8080"},
        )
        assert "tower.example.com:8080" in allowed
        assert "tower.example.com:443" not in allowed
        assert "tower.example.com" in private

    def test_an_ipv6_endpoint_is_private_without_its_brackets(self, tmp_path):
        """The proxy unbrackets the CONNECT host before it compares."""
        allowed, private = self._lists(_config(tmp_path, private=["fd00::1"]), {"[fd00::1]:8080"})
        assert "[fd00::1]:8080" in allowed
        assert "fd00::1" in private

    def test_a_grant_with_nothing_to_read_narrows(self, tmp_path):
        config = _config(tmp_path)
        config.db_path = tmp_path / "missing" / "istota.db"
        hosts, private = self._in_force(config, {ENDPOINT, "example.com"})
        assert hosts == {"example.com"}
        assert private == frozenset()

    def test_open_egress_is_left_alone(self, tmp_path):
        assert _egress_grants_in_force(_config(tmp_path), USER, None) == (None, frozenset())


class TestTheRuntime:
    """Through the seam the task runs on, to the proxy object it gets."""

    def _proxy(self, config, tmp_path, hosts):
        control = tmp_path / "control"
        control.mkdir()
        (tmp_path / "t").mkdir()
        task = db.Task(id=5, status="running", source_type="scheduled", user_id=USER,
                       prompt="p", conversation_token="")
        runtime = task_env.build_task_runtime(
            config, task, user_temp_dir=tmp_path / "t", control_dir=control, task_attempt=1,
            selected_skills=[], skill_index={}, is_admin=True, user_resources=[],
            user_config=None, egress_log=control / "egress-refused.jsonl",
            egress_hosts=frozenset(hosts),
        )
        return runtime.net_proxy_ctx

    def test_a_bound_endpoint_reaches_the_proxy(self, tmp_path):
        config = _config(tmp_path)
        _bind(config, f"http://{ENDPOINT}")
        proxy = self._proxy(config, tmp_path, {ENDPOINT, "example.com"})
        assert proxy.allowed_hosts == {"nc.example.com:443", "example.com:443", ENDPOINT}
        assert ADDRESS in proxy.private_hosts
        assert "example.com" not in proxy.private_hosts

    def test_an_unbound_one_does_not(self, tmp_path):
        proxy = self._proxy(_config(tmp_path), tmp_path, {ENDPOINT, "example.com"})
        assert proxy.allowed_hosts == {"nc.example.com:443", "example.com:443"}
        assert ADDRESS not in proxy.private_hosts


class TestWebFetch:
    def test_an_endpoint_grant_does_not_open_webfetch(self):
        """WebFetch runs in the daemon's network and matches a URL's host alone."""
        from istota.config import WebFetchConfig
        from istota.session.tools import web_fetch_policy_for_task
        from istota.session.tools.web_fetch import WebFetchError, _validate_url

        policy = web_fetch_policy_for_task(WebFetchConfig(), frozenset({ENDPOINT}))
        with pytest.raises(WebFetchError):
            _validate_url(f"https://{ENDPOINT}/", policy)


class TestCronDeclaration:
    def test_a_job_can_ask_for_an_endpoint_and_is_granted_nothing(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            egress.sync_declared(conn, USER, SUBJECT, json.dumps([ENDPOINT]), None, ask=True)
            assert [row["entry"] for row in egress.list_refused(conn, USER, SUBJECT)] == [ENDPOINT]
            assert egress.grants_for(conn, USER, SUBJECT) == frozenset()
