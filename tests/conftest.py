"""Shared test fixtures for istota tests."""

import os
from pathlib import Path

import pytest


def _load_dotenv():
    """Load .env file from project root into os.environ (simple key=value parser)."""
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            os.environ.setdefault(key, value)


_load_dotenv()

# Default-off in tests: most feeds tests expect an empty DB. The seed
# tests in test_feeds_migrate.py monkeypatch this var off explicitly.
os.environ.setdefault("ISTOTA_FEEDS_SKIP_DEFAULT_SEED", "1")
# Same pattern for the money default-ledger seed: most money tests
# expect a clean ledgers/ dir. The seed tests in test_migrate.py
# monkeypatch this var off explicitly.
os.environ.setdefault("ISTOTA_MONEY_SKIP_DEFAULT_SEED", "1")
# web_app's session middleware fails closed without a signing secret
# (ISSUE-124). Tests don't configure one, so opt into the random per-process
# dev secret. Tests that assert the fail-closed behaviour clear this explicitly.
os.environ.setdefault("ISTOTA_WEB_ALLOW_INSECURE_SESSION", "1")

from istota import db
from istota.config import Config, UserConfig


@pytest.fixture(autouse=True)
def _no_network_symbol_lookups(monkeypatch):
    """Portfolio auto-classification's default fetch is a live yfinance
    lookup, and an import triggers it — so any test that reaches a portfolio
    import would otherwise hit the network. Root-level rather than scoped to
    ``tests/money/``, since the import path is reachable from the web-route
    and skill tests too. Tests exercising the lookup path inject their own
    fetch; ``TestFetchSymbolInfo`` captures the real function at import time.
    """
    try:
        from istota.money import portfolio_autoclass
    except Exception:
        # Money extra absent, or its import chain unhappy. Broad on purpose:
        # this fixture is purely defensive and runs before every test in the
        # suite, so anything it raises fails thousands of unrelated tests
        # with a traceback pointing at the wrong place.
        return

    monkeypatch.setattr(
        portfolio_autoclass, "fetch_symbol_info", lambda symbol, **kwargs: None
    )


@pytest.fixture
def db_path(tmp_path):
    """Initialize a real SQLite database using schema.sql and return its path."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def db_conn(db_path):
    """Yield a database connection with row factory set."""
    with db.get_db(db_path) as conn:
        yield conn


@pytest.fixture
def make_task():
    """Factory fixture that creates Task dataclass instances with defaults."""
    def _make_task(**overrides):
        defaults = {
            "id": 1,
            "prompt": "test prompt",
            "user_id": "testuser",
            "source_type": "cli",
            "status": "pending",
        }
        defaults.update(overrides)
        return db.Task(**defaults)
    return _make_task


@pytest.fixture
def make_config(tmp_path):
    """Factory fixture that creates Config instances with tmp paths."""
    def _make_config(**overrides):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        index_file = skills_dir / "_index.toml"
        if not index_file.exists():
            index_file.write_text("")

        mount_path = tmp_path / "mount"
        mount_path.mkdir(exist_ok=True)

        defaults = {
            "db_path": tmp_path / "test.db",
            "temp_dir": tmp_path / "temp",
            "skills_dir": skills_dir,
            "nextcloud_mount_path": mount_path,
        }
        defaults.update(overrides)
        return Config(**defaults)
    return _make_config


@pytest.fixture
def make_user_config():
    """Factory fixture that creates UserConfig instances with defaults."""
    def _make_user_config(**overrides):
        defaults = {
            "display_name": "Test User",
            "email_addresses": [],
            "timezone": "UTC",
            "briefings": [],
        }
        defaults.update(overrides)
        return UserConfig(**defaults)
    return _make_user_config


@pytest.fixture(autouse=True)
def _reset_async_runtime_singletons():
    """Isolate the process-global persistent asyncio runtime + TalkClient.

    These singletons (``istota.async_runtime._RUNTIME`` / ``_TALK_CLIENT``)
    persist across tests within an xdist worker. A test that lazily starts the
    runtime or opens the shared client and doesn't reset it would leak that
    state into the next test on the same worker (e.g. a returned-singleton whose
    httpx pool is already open). Reset before and after every test so isolation
    doesn't depend on each Talk-touching test remembering to clean up. Cheap for
    the vast majority of tests that never touch the runtime: the reset helpers
    early-return when the globals are still ``None``.
    """
    from istota.async_runtime import reset_async_runtime, reset_talk_client

    reset_talk_client()
    reset_async_runtime()
    yield
    reset_talk_client()
    reset_async_runtime()


@pytest.fixture(autouse=True)
def _reset_expunge_warning_latch():
    """Isolate ``skills.email._expunge_warned_hosts``.

    A process-global "warned about this host already" set, so any test that
    drives a mailbox without UIDPLUS seeds it for the rest of that xdist
    worker. Tests must be order-independent, and a test asserting on that
    warning would otherwise pass or fail on who ran first.
    """
    from istota.skills import email as email_skill

    email_skill._expunge_warned_hosts.clear()
    yield
    email_skill._expunge_warned_hosts.clear()
