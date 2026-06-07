"""Brain routing for conversation-context triage (``_build_triage_completer``).

The native-brain migration ported Pass-2 skill routing to a brain-aware
completer but left the two conversation-context triage calls shelling out to the
``claude`` CLI — which, under ``kind="native"`` (no ``CLAUDE_CODE_OAUTH_TOKEN``),
exits "Not logged in" and silently collapsed context to the recent-5 window.
``_build_triage_completer`` mirrors the Pass-2 routing so native tasks triage
through their own provider, and never falls back to the wrong CLI.
"""

from types import SimpleNamespace
from unittest.mock import patch

from istota.config import BrainConfig, Config, NativeBrainConfig
from istota.executor import _build_triage_completer


def _cfg(tmp_path, kind="claude_code", overrides=None):
    c = Config()
    c.db_path = tmp_path / "istota.db"
    c.brain = BrainConfig(
        kind=kind,
        native=NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
        source_type_overrides=overrides or {},
    )
    return c


def _task(source_type="talk", user_id="alice"):
    return SimpleNamespace(source_type=source_type, user_id=user_id)


def test_claude_code_returns_none(tmp_path):
    """claude_code → None, so context triage keeps using the `claude` CLI."""
    completer = _build_triage_completer(_task(), _cfg(tmp_path, "claude_code"))
    assert completer is None


def test_native_returns_provider_completer(tmp_path):
    """native → the native provider completer (not the CLI)."""
    sentinel = lambda _p: '{"relevant_ids": []}'  # noqa: E731
    with patch("istota.executor._build_native_completer", return_value=sentinel):
        completer = _build_triage_completer(_task(), _cfg(tmp_path, "native"))
    assert completer is sentinel


def test_native_build_failure_fails_open_without_cli(tmp_path):
    """If the native completer can't be built, return a callable that yields None
    (so triage fails open) rather than None (which would re-enable the CLI)."""
    with patch("istota.executor._build_native_completer", return_value=None):
        completer = _build_triage_completer(_task(), _cfg(tmp_path, "native"))
    assert completer is not None
    assert completer("any prompt") is None


def test_per_source_type_override_routes_to_native(tmp_path):
    """A scheduled task routed to native via overrides gets the native completer,
    while an interactive task on the same config stays on the CLI (None)."""
    sentinel = lambda _p: "[]"  # noqa: E731
    cfg = _cfg(tmp_path, "claude_code", overrides={"scheduled": "native"})
    with patch("istota.executor._build_native_completer", return_value=sentinel):
        assert _build_triage_completer(_task("scheduled"), cfg) is sentinel
    assert _build_triage_completer(_task("talk"), cfg) is None
