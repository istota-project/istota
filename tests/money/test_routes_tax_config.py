"""Tests for the tax override API.

The DB tables and half the endpoints existed and worked; nothing in the frontend
called any of them, so the override mechanism was half built and never
surfaced. These cover the parts that make it usable: the state setting, the
resolved-rates endpoint that says where each figure came from, and CRUD keyed
on the three dimensions a bracket actually has.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money import config_store
from istota.money.cli import UserContext
from istota.money.core.models import TaxConfig
from istota.money.routes import get_user_config, require_auth, router

API = "/istota/api/money"


@pytest.fixture
def ctx(tmp_path: Path) -> UserContext:
    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    return UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)


@pytest.fixture
def client(ctx: UserContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix=API)
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_config] = lambda: ctx
    return TestClient(app)


class TestStateSetting:
    def test_state_round_trips(self, ctx, client):
        assert client.put(f"{API}/config/tax", json={"state": "CA"}).status_code == 200
        assert client.get(f"{API}/config/tax").json()["tax"]["state"] == "CA"

    def test_state_is_normalized(self, client):
        client.put(f"{API}/config/tax", json={"state": "ny"})
        assert client.get(f"{API}/config/tax").json()["tax"]["state"] == "NY"

    def test_state_can_be_cleared(self, client):
        # "" is a real choice — no state tax — not an unset field.
        client.put(f"{API}/config/tax", json={"state": "CA"})
        client.put(f"{API}/config/tax", json={"state": ""})
        assert client.get(f"{API}/config/tax").json()["tax"]["state"] == ""

    def test_unknown_state_rejected(self, client):
        resp = client.put(f"{API}/config/tax", json={"state": "ZZ"})
        assert resp.status_code == 400
        assert "ZZ" in resp.json()["error"]

    def test_defaults_to_empty(self, client):
        assert client.get(f"{API}/config/tax").json()["tax"]["state"] == ""


class TestJurisdictionRegistry:
    def test_lists_all_jurisdictions(self, client):
        body = client.get(f"{API}/config/tax/jurisdictions").json()
        assert body["status"] == "ok"
        assert len(body["jurisdictions"]) == 51

    def test_entries_carry_name_and_taxes_income(self, client):
        by_code = {
            j["code"]: j
            for j in client.get(f"{API}/config/tax/jurisdictions").json()["jurisdictions"]
        }
        assert by_code["CA"]["name"] == "California"
        assert by_code["CA"]["taxes_income"] is True
        assert by_code["TX"]["taxes_income"] is False

    def test_flags_which_states_ship_bundled_data(self, client):
        # Drives the "selectable, but you will need to enter brackets" hint on
        # the settings page, so a user is told before they pick rather than
        # after.
        by_code = {
            j["code"]: j
            for j in client.get(f"{API}/config/tax/jurisdictions").json()["jurisdictions"]
        }
        assert by_code["CA"]["has_bundled_data"] is True
        assert by_code["TX"]["has_bundled_data"] is False


class TestResolvedRates:
    def test_reports_the_figures_actually_in_use(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": "CA"})
        body = client.get(f"{API}/config/tax/resolved").json()
        assert body["status"] == "ok"
        fed = body["federal"]
        assert fed["standard_deduction"]["value"] == 32_200
        assert fed["standard_deduction"]["overridden"] is False
        assert fed["brackets"]["value"][0] == [0, 0.10]

    def test_names_the_source_per_jurisdiction(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": "CA"})
        body = client.get(f"{API}/config/tax/resolved").json()
        assert "Rev. Proc. 2025-32" in body["federal"]["provenance"]["source"]
        assert body["federal"]["provenance"]["verified_on"]

    def test_reports_the_year_actually_used(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2031})
        prov = client.get(f"{API}/config/tax/resolved").json()["federal"]["provenance"]
        assert prov["requested_year"] == 2031
        assert prov["year"] == 2026
        assert prov["is_fallback"] is True

    def test_an_override_is_flagged_per_field(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026})
        client.put(
            f"{API}/config/tax/schedules/2026/federal/mfj",
            json={"standard_deduction": 40_000},
        )
        fed = client.get(f"{API}/config/tax/resolved").json()["federal"]
        assert fed["standard_deduction"]["value"] == 40_000
        assert fed["standard_deduction"]["overridden"] is True
        # The neighbouring field is untouched, so it must not read as overridden.
        assert fed["brackets"]["overridden"] is False

    def test_payroll_is_reported_with_its_overrides(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026})
        body = client.get(f"{API}/config/tax/resolved").json()
        assert body["payroll"]["ss_wage_base"]["value"] == 184_500
        assert body["payroll"]["ss_wage_base"]["overridden"] is False

    def test_payroll_override_is_flagged(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026})
        client.put(f"{API}/config/tax/years/2026", json={"ss_wage_base": 200_000})
        body = client.get(f"{API}/config/tax/resolved").json()
        assert body["payroll"]["ss_wage_base"]["value"] == 200_000
        assert body["payroll"]["ss_wage_base"]["overridden"] is True

    def test_no_state_reports_none(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": ""})
        assert client.get(f"{API}/config/tax/resolved").json()["state"] is None

    def test_state_without_data_reports_unavailable(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": "NY"})
        state = client.get(f"{API}/config/tax/resolved").json()["state"]
        assert state["code"] == "NY"
        assert state["available"] is False
        assert state["reason"] == "no_brackets"

    def test_no_income_tax_state_reports_its_own_reason(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": "TX"})
        state = client.get(f"{API}/config/tax/resolved").json()["state"]
        assert state["available"] is False
        assert state["reason"] == "no_income_tax"

    def test_an_override_makes_an_unbundled_state_available(self, client):
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": "NY"})
        client.put(
            f"{API}/config/tax/schedules/2026/NY/mfj",
            json={"brackets": [[0, 0.04]], "standard_deduction": 16_050},
        )
        state = client.get(f"{API}/config/tax/resolved").json()["state"]
        assert state["available"] is True
        assert state["standard_deduction"]["value"] == 16_050
        assert state["standard_deduction"]["overridden"] is True

    def test_year_and_status_are_overridable_by_query(self, client):
        # The settings page edits a year the user is not currently filing for.
        client.put(f"{API}/config/tax", json={"tax_year": 2026, "state": "CA"})
        body = client.get(
            f"{API}/config/tax/resolved?year=2025&filing_status=single"
        ).json()
        assert body["federal"]["standard_deduction"]["value"] == 15_000
        assert body["state"]["standard_deduction"]["value"] == 5_706

    def test_bad_filing_status_rejected(self, client):
        assert client.get(
            f"{API}/config/tax/resolved?filing_status=hoh"
        ).status_code == 400


class TestScheduleCrud:
    def test_upsert_and_list(self, client):
        resp = client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj",
            json={"standard_deduction": 11_000, "brackets": [[0, 0.01]]},
        )
        assert resp.json()["state"] == "created"
        rows = client.get(f"{API}/config/tax/schedules").json()["schedules"]
        assert len(rows) == 1
        assert rows[0]["jurisdiction"] == "CA"
        assert rows[0]["filing_status"] == "mfj"
        assert rows[0]["brackets"] == [[0, 0.01]]

    def test_merge_leaves_the_other_field_alone(self, client):
        client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj", json={"brackets": [[0, 0.01]]},
        )
        client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj", json={"standard_deduction": 11_000},
        )
        rows = client.get(f"{API}/config/tax/schedules").json()["schedules"]
        assert rows[0]["brackets"] == [[0, 0.01]]
        assert rows[0]["standard_deduction"] == 11_000

    def test_delete_reverts_to_bundled(self, client):
        client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj", json={"standard_deduction": 11_000},
        )
        assert client.delete(
            f"{API}/config/tax/schedules/2026/CA/mfj"
        ).json()["removed"] is True
        assert client.get(f"{API}/config/tax/schedules").json()["schedules"] == []

    def test_delete_missing_is_not_an_error(self, client):
        resp = client.delete(f"{API}/config/tax/schedules/2026/CA/mfj")
        assert resp.status_code == 200
        assert resp.json()["removed"] is False

    def test_unknown_jurisdiction_rejected(self, client):
        resp = client.put(
            f"{API}/config/tax/schedules/2026/Narnia/mfj",
            json={"standard_deduction": 1},
        )
        assert resp.status_code == 400

    def test_unknown_filing_status_rejected(self, client):
        resp = client.put(
            f"{API}/config/tax/schedules/2026/CA/hoh", json={"standard_deduction": 1},
        )
        assert resp.status_code == 400

    def test_unknown_body_key_rejected(self, client):
        resp = client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj", json={"nope": 1},
        )
        assert resp.status_code == 400

    def test_malformed_brackets_rejected(self, client):
        # Brackets reach `apply_brackets`, which indexes b[0]/b[1]; a ragged
        # pair would fail there rather than here, mid-estimate.
        for bad in ([[0]], [["x", 0.1]], [0.1], "nope", [[0, 0.1], [0]]):
            resp = client.put(
                f"{API}/config/tax/schedules/2026/CA/mfj", json={"brackets": bad},
            )
            assert resp.status_code == 400, bad

    def test_unsorted_brackets_rejected(self, client):
        # `apply_brackets` walks the pairs in order and would compute a
        # nonsense figure rather than refusing.
        resp = client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj",
            json={"brackets": [[100, 0.1], [0, 0.2]]},
        )
        assert resp.status_code == 400

    def test_negative_standard_deduction_rejected(self, client):
        resp = client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj",
            json={"standard_deduction": -1},
        )
        assert resp.status_code == 400

    def test_out_of_range_rate_rejected(self, client):
        resp = client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj",
            json={"brackets": [[0, 1.5]]},
        )
        assert resp.status_code == 400

    def test_empty_brackets_clears_rather_than_storing_nothing(self, client):
        client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj",
            json={"brackets": [[0, 0.01]], "standard_deduction": 11_000},
        )
        client.put(
            f"{API}/config/tax/schedules/2026/CA/mfj", json={"brackets": None},
        )
        rows = client.get(f"{API}/config/tax/schedules").json()["schedules"]
        assert rows[0]["brackets"] is None
        assert rows[0]["standard_deduction"] == 11_000


class TestOverridesReachTheEstimate:
    def test_a_federal_override_changes_the_estimate(self, ctx, client):
        config_store.save_tax(
            ctx.db_path, TaxConfig(tax_year=2026, filing_status="mfj", state="CA"),
        )
        before = client.get(f"{API}/tax/estimate").json()
        client.put(
            f"{API}/config/tax/schedules/2026/federal/mfj",
            json={"standard_deduction": 100_000},
        )
        after = client.get(f"{API}/tax/estimate").json()
        assert before["federal_standard_deduction"] == 32_200
        assert after["federal_standard_deduction"] == 100_000
