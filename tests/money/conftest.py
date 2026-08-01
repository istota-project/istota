"""Money test-package fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_network_symbol_lookups(monkeypatch):
    """Auto-classification's default fetch is a live yfinance lookup; imports
    trigger it, so every import-touching test would otherwise hit the network.
    Tests that exercise the lookup path inject their own fetch."""
    from istota.money import portfolio_autoclass

    monkeypatch.setattr(
        portfolio_autoclass, "fetch_symbol_info", lambda symbol: None
    )
