"""An explicit user instance for browser helpers tested outside Flask."""

import pytest


@pytest.fixture(autouse=True)
def browser_instance(monkeypatch):
    import browse_api
    import pool

    inst = pool.BrowserInstance("alice", "", ":100", 9300, 5900, 0)
    monkeypatch.setattr(browse_api, "_instance", inst, raising=False)
    monkeypatch.setattr(browse_api, "_request_instance", lambda: browse_api._instance)
    monkeypatch.setattr(browse_api, "_user_scope", "alice")
    monkeypatch.setattr(pool, "_instances", {"alice": inst})
    yield inst
    pool._instances.clear()
