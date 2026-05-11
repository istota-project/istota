"""Experimental-flag gating on money CLI subcommands (lots, wash-sales).

These tests invoke the Click ``cli`` group directly via ``CliRunner`` so
the full decorator stack runs (Click parsing + ``@requires_feature`` +
``@pass_ctx``). The decorator should short-circuit before any ledger
machinery loads, so we don't need a real money workspace.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from istota.money.cli import cli


class TestLotsGating:
    def test_lots_blocked_without_flag(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        runner = CliRunner()
        result = runner.invoke(cli, ["lots", "AAPL"])
        assert result.exit_code == 1
        payload = json.loads(result.output.strip())
        assert payload["status"] == "error"
        assert "money_tax" in payload["error"]

    # The on-path (flag enabled → command passes through) is covered by
    # tests/test_experimental.py::TestRequiresFeature::test_on_passes_through,
    # which exercises the decorator directly without needing money-CLI
    # fixtures. No on-path test here.


class TestWashSalesGating:
    def test_wash_sales_blocked_without_flag(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        runner = CliRunner()
        result = runner.invoke(cli, ["wash-sales"])
        assert result.exit_code == 1
        payload = json.loads(result.output.strip())
        assert payload["status"] == "error"
        assert "money_wash_sales" in payload["error"]

    def test_wash_sales_blocked_with_only_money_tax(self, monkeypatch):
        # money_tax alone doesn't unlock wash-sales — flags are per-command.
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "money_tax")
        runner = CliRunner()
        result = runner.invoke(cli, ["wash-sales"])
        assert result.exit_code == 1
        payload = json.loads(result.output.strip())
        assert payload["status"] == "error"
        assert "money_wash_sales" in payload["error"]


class TestFacadeUnwrapsGatingEnvelope:
    """When the money skill facade calls the inner Click CLI and the
    inner call hits the experimental gate, the facade must unwrap the
    inner error envelope so the user-visible message stays readable
    instead of becoming escaped JSON nested inside an outer envelope.
    """

    def test_unwrap_flattens_gating_envelope(self):
        from istota.skills.money import _unwrap_inner_error

        raw = json.dumps({
            "status": "error",
            "error": "feature 'money_tax' is experimental and not enabled on this instance",
        })
        msg = _unwrap_inner_error(raw)
        assert "money_tax" in msg
        assert "experimental" in msg
        # The unwrapped string must not itself look like a JSON envelope.
        assert not msg.lstrip().startswith("{")

    def test_unwrap_passes_non_envelope_through(self):
        from istota.skills.money import _unwrap_inner_error

        assert _unwrap_inner_error("No ledgers configured") == "No ledgers configured"
        assert _unwrap_inner_error("") == ""

    def test_unwrap_ignores_non_error_envelope(self):
        from istota.skills.money import _unwrap_inner_error

        # A success envelope or non-string error field should not be unwrapped —
        # we'd lose the structure. Keep the raw text.
        raw = json.dumps({"status": "ok", "value": 42})
        assert _unwrap_inner_error(raw) == raw
        raw2 = json.dumps({"status": "error", "error": 42})
        assert _unwrap_inner_error(raw2) == raw2

    def test_unwrap_ignores_malformed_json(self):
        from istota.skills.money import _unwrap_inner_error

        assert _unwrap_inner_error("{not valid json") == "{not valid json"
