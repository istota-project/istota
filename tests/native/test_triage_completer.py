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

from istota import db
from istota.config import BrainConfig, Config, NativeBrainConfig
from istota.executor import _build_triage_completer, build_oneshot_completer
from istota.llm.types import AssistantMessage, TextContent, Usage

from ._mock_provider import MockProvider


def _cfg(tmp_path, kind="claude_code", overrides=None):
    c = Config()
    c.db_path = tmp_path / "istota.db"
    c.brain = BrainConfig(
        kind=kind,
        native=NativeBrainConfig(model="claude-sonnet-4-6", api_key="k"),
        source_type_overrides=overrides or {},
    )
    return c


def _task(source_type="talk", user_id="alice", brain=None):
    # `brain` is on the double because it is on `db.Task`: the routing reads a
    # task's own pinned kind ahead of the source-type layer, and a stand-in
    # missing the attribute would hide that read rather than exercise it.
    return SimpleNamespace(
        source_type=source_type, user_id=user_id, brain=brain,
    )


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


def test_a_room_pinned_brain_routes_triage(tmp_path):
    """The task's own pinned kind outranks the source-type layer here too.

    Both directions, because a completer that only ever read ``[brain] kind``
    would pass the first of these by accident: a ``native`` pin on a claude_code
    deployment must reach the provider completer, and a ``claude_code`` pin must
    keep the CLI even where the lane rule says native.
    """
    sentinel = lambda _p: "[]"  # noqa: E731
    cfg = _cfg(tmp_path, "claude_code", overrides={"talk": "claude_code"})
    cfg.brain.room_selectable = ["native", "claude_code"]
    with patch("istota.executor._build_native_completer", return_value=sentinel):
        assert _build_triage_completer(_task(brain="native"), cfg) is sentinel

    cfg2 = _cfg(tmp_path, "claude_code", overrides={"talk": "native"})
    cfg2.brain.room_selectable = ["claude_code"]
    assert _build_triage_completer(_task(brain="claude_code"), cfg2) is None


def test_an_unallowlisted_pin_falls_through_to_the_source_type_layer(tmp_path):
    """The refusal `resolve_brain_kind` makes, seen from a call site: the pin is
    dropped and the lane rule answers instead."""
    sentinel = lambda _p: "[]"  # noqa: E731
    cfg = _cfg(tmp_path, "claude_code", overrides={"talk": "native"})
    cfg.brain.room_selectable = []
    with patch("istota.executor._build_native_completer", return_value=sentinel):
        assert _build_triage_completer(_task(brain="claude_code"), cfg) is sentinel


class TestTheTaskFreeFactory:
    """``build_oneshot_completer`` takes the fields, never a ``db.Task``.

    The speech gate runs before any task exists, so what it builds from has to
    be ``(config, user_id, source_type)`` alone.
    """

    def test_it_routes_by_source_type_and_pin_with_no_task(self, tmp_path):
        sentinel = lambda _p: "{}"  # noqa: E731
        cfg = _cfg(tmp_path, "claude_code", overrides={"talk": "native"})
        cfg.brain.room_selectable = ["claude_code"]
        kw = dict(user_id="alice", timeout=5.0, origin="speech_gate")
        with patch("istota.executor._build_native_completer", return_value=sentinel):
            assert build_oneshot_completer(cfg, source_type="talk", **kw) is sentinel
            assert build_oneshot_completer(cfg, source_type="web", **kw) is None
            assert build_oneshot_completer(
                cfg, source_type="talk", brain_kind="claude_code", **kw,
            ) is None

    def test_a_native_call_writes_a_task_less_row_under_the_callers_origin(
        self, tmp_path,
    ):
        cfg = _cfg(tmp_path, "native")
        db.init_db(cfg.db_path)
        provider = MockProvider([
            AssistantMessage(
                content=[TextContent(text='{"speak": false}')],
                usage=Usage(input_tokens=50, output_tokens=7),
                model="claude-sonnet-4-6",
            )
        ])
        with patch("istota.llm.make_provider", return_value=provider), \
                patch("istota.executor._native_with_user_key",
                      side_effect=lambda nc, *a, **k: nc):
            completer = build_oneshot_completer(
                cfg, user_id="bob", source_type="web",
                timeout=5.0, origin="speech_gate",
            )
            assert completer("is this for the bot?") == '{"speak": false}'

        with db.get_db(cfg.db_path) as conn:
            rows = conn.execute(
                "SELECT origin, user_id, source_type, task_id, output_tokens "
                "FROM task_usage"
            ).fetchall()
        assert [tuple(r) for r in rows] == [("speech_gate", "bob", "web", None, 7)]
