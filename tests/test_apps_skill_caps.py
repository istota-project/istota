"""The `skill:` capabilities through the real router: the six allowlisted operations.

Same harness as `test_apps_reads.py`. Each operation runs against the real
library function it wraps, over a seeded database: the health and location
module databases, and the framework database's `knowledge_facts`. The one
double is `yfinance`, the network boundary behind `markets.get_quotes`,
replaced in `sys.modules` so the real `get_quotes` runs over it.

Each operation is checked for its typed arguments, for the owning module
being off for the user (health, location), and for its skill being disabled.
The grant row is the authority, as for the reads: a scope tampered after the
fact to name another operation is refused rather than run.
"""

from __future__ import annotations

import sys
import types

import pytest

from istota import db
from istota.apps import skill_caps
from istota.apps.capabilities import SKILL_OPERATIONS, is_sensitive, parse_capability
from istota.health import db as health_db
from istota.health._loader import resolve_for_user as resolve_health
from istota.location import db as location_db
from istota.location._loader import resolve_for_user as resolve_location
from istota.memory import knowledge_graph as kg
from tests import test_apps_routes as harness
from tests.test_apps_routes import USER, _call, _calls, _conn, _install

apps_dir = harness.apps_dir
config = harness.config
apps_root = harness.apps_root
client = harness.client


def _code(resp):
    return resp.json()["error"]["code"]


def _result(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _grant_scope(config, capability, scope, *, app_id="board", version=1):
    from istota.apps import db as apps_db

    with _conn(config) as conn:
        apps_db.put_grant(conn, app_id, version, capability, scope)
        conn.commit()


# -- the allowlist itself -------------------------------------------------------


class TestAllowlist:
    def test_the_handlers_are_exactly_the_operations_apps_may_name(self):
        assert set(skill_caps.SKILL_CAPS) == set(SKILL_OPERATIONS)

    def test_sensitivity_is_the_capabilitys_own(self):
        for operation, cap in skill_caps.SKILL_CAPS.items():
            parsed = parse_capability(f"skill:{operation}", bot_dir="Istota")
            assert cap.sensitive is is_sensitive(parsed), operation
        assert skill_caps.SKILL_CAPS["health.trend"].sensitive is True

    def test_an_operation_off_the_list_is_refused_at_install(self, config, apps_root):
        from istota.apps.manifest import ManifestError

        with pytest.raises(ManifestError):
            _install(config, apps_root, capabilities=["skill:shell.run"])


class TestDispatch:
    def test_without_the_grant_it_is_not_granted(self, config, apps_root, client):
        _install(config, apps_root)
        resp = _call(client, "skill:location.places", "call", {})
        assert resp.status_code == 403
        assert _code(resp) == "not_granted"

    def test_the_op_is_call(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.places"])
        resp = _call(client, "skill:location.places", "places", {})
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"

    @pytest.mark.parametrize("stored", ["health.trend", "shell.run", "", None, 7, "location.places "])
    def test_a_stored_scope_other_than_the_capabilitys_is_not_granted(
        self, config, apps_root, client, stored,
    ):
        _install(config, apps_root, capabilities=["skill:location.places"])
        _grant_scope(config, "skill:location.places", stored)
        resp = _call(client, "skill:location.places", "call", {})
        assert resp.status_code == 403
        assert _code(resp) == "not_granted"

    def test_unknown_arguments_are_refused(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.places"])
        resp = _call(client, "skill:location.places", "call", {"sql": "select 1"})
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"

    def test_the_call_is_recorded(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.places"])
        _call(client, "skill:location.places", "call", {})
        [row] = _calls(config)
        assert (row["capability"], row["op"]) == ("skill:location.places", "call")

    @pytest.mark.parametrize("operation, skill", [
        ("markets.quote", "markets"),
        ("health.trend", "health"),
        ("location.places", "location"),
        ("memory_search.facts", "memory_search"),
    ])
    def test_a_skill_disabled_for_the_user_is_not_found(
        self, config, apps_root, client, operation, skill,
    ):
        _install(config, apps_root, capabilities=[f"skill:{operation}"])
        config.users[USER].disabled_skills = [skill]
        resp = _call(client, f"skill:{operation}", "call", _VALID_ARGS[operation])
        assert resp.status_code == 404
        assert _code(resp) == "not_found"

    def test_a_skill_disabled_for_the_instance_is_not_found(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:memory_search.facts"])
        config.disabled_skills = ["memory_search"]
        resp = _call(client, "skill:memory_search.facts", "call", {})
        assert _code(resp) == "not_found"


_VALID_ARGS = {
    "markets.quote": {"symbols": ["AAPL"]},
    "health.trend": {"name": "LDL"},
    "location.current": {},
    "location.places": {},
    "memory_search.facts": {},
    "memory_search.timeline": {"subject": "user"},
}


# -- markets.quote ----------------------------------------------------------------


class _FastInfo:
    def __init__(self, last, previous):
        self.last_price = last
        self.previous_close = previous


@pytest.fixture
def yfinance(monkeypatch):
    prices = {"AAPL": (110.0, 100.0), "^GSPC": (5000.0, 5050.0)}
    asked = []

    class Ticker:
        def __init__(self, symbol):
            asked.append(symbol)
            if symbol not in prices:
                raise RuntimeError("no such symbol")
            self.fast_info = _FastInfo(*prices[symbol])

    module = types.ModuleType("yfinance")
    module.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return asked


class TestMarketsQuote:
    def test_quotes_through_get_quotes(self, config, apps_root, client, yfinance):
        _install(config, apps_root, capabilities=["skill:markets.quote"])
        result = _result(_call(client, "skill:markets.quote", "call", {"symbols": ["AAPL", "^GSPC", "NOPE"]}))
        assert yfinance == ["AAPL", "^GSPC", "NOPE"]
        quotes = {q["symbol"]: q for q in result["quotes"]}
        assert set(quotes) == {"AAPL", "^GSPC"}
        assert quotes["AAPL"]["price"] == 110.0
        assert quotes["AAPL"]["change_percent"] == pytest.approx(10.0)
        assert quotes["^GSPC"]["name"] == "S&P 500"
        assert isinstance(quotes["AAPL"]["timestamp"], str)

    @pytest.mark.parametrize("args", [
        {},
        {"symbols": []},
        {"symbols": "AAPL"},
        {"symbols": [1]},
        {"symbols": ["AAPL; rm -rf"]},
        {"symbols": ["A" * 21]},
        {"symbols": ["AAPL"] * 21},
    ])
    def test_symbols_are_typed(self, config, apps_root, client, yfinance, args):
        _install(config, apps_root, capabilities=["skill:markets.quote"])
        resp = _call(client, "skill:markets.quote", "call", args)
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"
        assert yfinance == []

    def test_without_yfinance_it_is_not_found(self, config, apps_root, client, monkeypatch):
        _install(config, apps_root, capabilities=["skill:markets.quote"])
        monkeypatch.setitem(sys.modules, "yfinance", None)
        resp = _call(client, "skill:markets.quote", "call", {"symbols": ["AAPL"]})
        assert resp.status_code == 404
        assert _code(resp) == "not_found"


# -- health.trend -------------------------------------------------------------------


def _health_path(config):
    path = resolve_health(USER, config).db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    health_db.init_db(path)
    return path


def _seed_health(config):
    path = _health_path(config)
    with health_db.connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO biomarker_refs(name, display_name, category, default_unit, aliases) "
            "VALUES (?, ?, ?, ?, ?)",
            ("LDL", "LDL cholesterol", "lipids", "mg/dL", '["LDL-C"]'),
        )
        first = health_db.insert_panel(conn, drawn_at="2026-01-10")
        health_db.insert_biomarker(conn, panel_id=first, name="LDL", value=130, unit="mg/dL", flag="H")
        second = health_db.insert_panel(conn, drawn_at="2026-06-10")
        health_db.insert_biomarker(conn, panel_id=second, name="LDL", value=95, unit="mg/dL")
        draft = health_db.insert_panel(conn, drawn_at="2026-07-10", draft=True)
        health_db.insert_biomarker(conn, panel_id=draft, name="LDL", value=999, unit="mg/dL")
        conn.commit()


class TestHealthTrend:
    def test_the_trend_by_alias_without_drafts(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:health.trend"])
        _seed_health(config)
        result = _result(_call(client, "skill:health.trend", "call", {"name": "ldl-c"}))
        assert result["name"] == "LDL"
        assert result["display_name"] == "LDL cholesterol"
        assert result["unit"] == "mg/dL"
        assert result["points"] == [
            {"drawn_at": "2026-01-10", "value": 130, "unit": "mg/dL", "flag": "H"},
            {"drawn_at": "2026-06-10", "value": 95, "unit": "mg/dL", "flag": None},
        ]

    def test_since_and_until_bound_the_trend(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:health.trend"])
        _seed_health(config)
        result = _result(_call(
            client, "skill:health.trend", "call",
            {"name": "LDL", "since": "2026-02-01", "until": "2026-12-31"},
        ))
        assert [p["drawn_at"] for p in result["points"]] == ["2026-06-10"]

    def test_no_health_database_is_an_empty_trend(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:health.trend"])
        result = _result(_call(client, "skill:health.trend", "call", {"name": "LDL"}))
        assert result["points"] == []
        assert not resolve_health(USER, config).db_path.exists()

    @pytest.mark.parametrize("args", [
        {},
        {"name": ""},
        {"name": 7},
        {"name": "x" * 201},
        {"name": "LDL", "since": "last year"},
        {"name": "LDL", "until": 20260101},
        {"name": "LDL", "since": "2026-13-01"},
    ])
    def test_arguments_are_typed(self, config, apps_root, client, args):
        _install(config, apps_root, capabilities=["skill:health.trend"])
        resp = _call(client, "skill:health.trend", "call", args)
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"

    def test_the_health_module_off_is_not_found(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:health.trend"])
        _seed_health(config)
        config.users[USER].disabled_modules = ["health"]
        resp = _call(client, "skill:health.trend", "call", {"name": "LDL"})
        assert resp.status_code == 404
        assert _code(resp) == "not_found"
        assert "130" not in resp.text


# -- location.current and location.places ------------------------------------------


def _seed_location(config):
    path = resolve_location(USER, config).db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    location_db.init_db(path)
    with location_db.connect(path) as conn:
        place_id = conn.execute(
            "INSERT INTO places(name, lat, lon, radius_meters, category, notes) VALUES (?, ?, ?, ?, ?, ?)",
            ("Home", 12.5, -45.25, 80, "home", "front door"),
        ).lastrowid
        conn.execute(
            "INSERT INTO places(name, lat, lon) VALUES (?, ?, ?)", ("Airfield", 13.0, -44.0),
        )
        location_db.insert_ping(conn, "2026-03-08T11:59:00Z", 12.5, -45.25, accuracy=10, place_id=place_id)
        conn.commit()


class TestLocation:
    def test_places(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.places"])
        _seed_location(config)
        result = _result(_call(client, "skill:location.places", "call", {}))
        assert [p["name"] for p in result["places"]] == ["Airfield", "Home"]
        home = result["places"][1]
        assert (home["lat"], home["lon"], home["radius_meters"]) == (12.5, -45.25, 80)

    def test_current(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.current"])
        _seed_location(config)
        result = _result(_call(client, "skill:location.current", "call", {}))
        assert result["last_ping"]["lat"] == 12.5
        assert result["last_ping"]["place"] == "Home"

    def test_no_location_database_is_empty(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.current", "skill:location.places"])
        assert _result(_call(client, "skill:location.current", "call", {})) == {
            "last_ping": None, "current_visit": None,
        }
        assert _result(_call(client, "skill:location.places", "call", {})) == {"places": []}
        assert not resolve_location(USER, config).db_path.exists()

    def test_arguments_are_refused(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:location.current"])
        resp = _call(client, "skill:location.current", "call", {"precision": "exact"})
        assert _code(resp) == "bad_args"

    @pytest.mark.parametrize("operation", ["location.current", "location.places"])
    def test_the_location_module_off_is_not_found(self, config, apps_root, client, operation):
        _install(config, apps_root, capabilities=[f"skill:{operation}"])
        _seed_location(config)
        config.users[USER].disabled_modules = ["location"]
        resp = _call(client, f"skill:{operation}", "call", {})
        assert resp.status_code == 404
        assert _code(resp) == "not_found"
        assert "12.5" not in resp.text


# -- memory_search.facts and memory_search.timeline ---------------------------------


def _seed_facts(config, user=USER):
    with db.get_db(config.db_path) as conn:
        kg.ensure_table(conn)
        kg.add_fact(conn, user, "user", "lives_in", "Lisbon", valid_from="2024-01-01")
        kg.add_fact(conn, user, "user", "flies", "a Cessna 172")
        kg.add_fact(conn, user, "user", "worked_at", "Old Co", valid_from="2019-01-01", valid_until="2022-01-01")
        kg.add_fact(conn, user, "lisbon", "is_in", "Portugal")
        conn.commit()


class TestMemorySearch:
    def test_facts_are_the_current_ones(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:memory_search.facts"])
        _seed_facts(config)
        result = _result(_call(client, "skill:memory_search.facts", "call", {}))
        triples = {(f["subject"], f["predicate"], f["object"]) for f in result["facts"]}
        assert triples == {
            ("user", "lives_in", "lisbon"), ("user", "flies", "a cessna 172"),
            ("lisbon", "is_in", "portugal"),
        }
        assert result["truncated"] is False

    def test_facts_filter_by_subject_and_predicate(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:memory_search.facts"])
        _seed_facts(config)
        result = _result(_call(
            client, "skill:memory_search.facts", "call", {"subject": "User", "predicate": "flies"},
        ))
        assert [f["object"] for f in result["facts"]] == ["a cessna 172"]

    def test_only_the_users_own_facts(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:memory_search.facts", "skill:memory_search.timeline"])
        _seed_facts(config, user="bob")
        assert _result(_call(client, "skill:memory_search.facts", "call", {}))["facts"] == []
        timeline = _result(_call(client, "skill:memory_search.timeline", "call", {"subject": "user"}))
        assert timeline["facts"] == []

    def test_the_timeline_includes_what_was_invalidated(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["skill:memory_search.timeline"])
        _seed_facts(config)
        result = _result(_call(client, "skill:memory_search.timeline", "call", {"subject": "user"}))
        assert result["subject"] == "user"
        objects = [f["object"] for f in result["facts"]]
        assert objects[0] == "old co"
        assert set(objects) == {"old co", "lisbon", "a cessna 172"}
        old = result["facts"][0]
        assert (old["valid_from"], old["valid_until"]) == ("2019-01-01", "2022-01-01")

    @pytest.mark.parametrize("operation, args", [
        ("memory_search.facts", {"subject": 3}),
        ("memory_search.facts", {"predicate": "x" * 201}),
        ("memory_search.facts", {"as_of": "2020-01-01"}),
        ("memory_search.timeline", {}),
        ("memory_search.timeline", {"subject": ""}),
        ("memory_search.timeline", {"subject": ["user"]}),
    ])
    def test_arguments_are_typed(self, config, apps_root, client, operation, args):
        _install(config, apps_root, capabilities=[f"skill:{operation}"])
        resp = _call(client, f"skill:{operation}", "call", args)
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"
