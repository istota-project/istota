"""Tests for ``istota.subscription_usage``.

Nothing here touches the network, the real macOS Keychain, or the real
``~/.claude/.credentials.json``. Every entry point takes its environment, its
home directory and its transport as a parameter, so the whole module is
exercised against ``tmp_path`` and a stub callable.

The payload fixture is shaped like the real 2026-08-22 capture but carries
**invented** utilization figures and **invented** codenames of the same shape as
the unreleased ones the endpoint really returns (resolved open question 3 in the
spec). The codenames matter: the point of ``test_no_codename_reaches_a_window``
is that the allowlist drops them, and a fixture without them would prove
nothing.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import subscription_usage as su


# ---------------------------------------------------------------------------
# Fixture payload
# ---------------------------------------------------------------------------

# Invented substitutes for the unreleased top-level keys the endpoint returns.
# `cobalt_lantern` is deliberately a *live* window below (utilization 0.0), so a
# test that only checked for null values would not exercise the allowlist.
CODENAMES = (
    "quince",
    "walrus_cravat",
    "cobalt_lantern",
    "frittata_promotional",
    "ember_inlet",
    "topaz_stairway",
    "seven_day_coworking",
)

NOW = datetime(2026, 8, 22, 16, 35, 12, tzinfo=timezone.utc).timestamp()

FIVE_HOUR_RESETS = "2026-08-22T17:40:00.224941+00:00"
WEEKLY_RESETS = "2026-08-28T19:00:00.224961+00:00"

# 17:40:00.224941 − 16:35:12 = 3888.22s
FIVE_HOUR_RESETS_IN = 3888
# 2026-08-28T19:00:00.224961 − 2026-08-22T16:35:12 = 527088.22s
WEEKLY_RESETS_IN = 527088


def _dollars_block() -> dict:
    return {"limit_dollars": None, "used_dollars": None, "remaining_dollars": None}


def payload() -> dict:
    """A fresh copy of the fixture payload (tests mutate it)."""
    return {
        "five_hour": {"utilization": 37.0, "resets_at": FIVE_HOUR_RESETS, **_dollars_block()},
        "seven_day": {"utilization": 12.0, "resets_at": WEEKLY_RESETS, **_dollars_block()},
        "seven_day_sonnet": {"utilization": 4.0, "resets_at": WEEKLY_RESETS, **_dollars_block()},
        "seven_day_opus": None,
        "seven_day_oauth_apps": None,
        # --- invented codenames, one of them live ---
        "seven_day_coworking": None,
        "quince": None,
        "walrus_cravat": None,
        "cobalt_lantern": {"utilization": 0.0, "resets_at": None, **_dollars_block()},
        "frittata_promotional": None,
        "ember_inlet": None,
        "topaz_stairway": None,
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": 2000,
            "used_credits": 0.0,
            "utilization": 0.0,
            "currency": "USD",
            "decimal_places": 2,
            "disabled_reason": "out_of_credits",
        },
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 37,
                "severity": "normal",
                "resets_at": FIVE_HOUR_RESETS,
                "scope": None,
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 12,
                "severity": "normal",
                "resets_at": WEEKLY_RESETS,
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 4,
                "severity": "normal",
                "resets_at": None,
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": False,
            },
        ],
        "spend": {
            "used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 2000, "currency": "USD", "exponent": 2},
            "percent": 0,
            "severity": "normal",
            "enabled": False,
            "disclaimer": "Estimates only.",
        },
        "member_dashboard_available": False,
    }


def _stub_transport(status: int = 200, body: object = None, *, calls: list | None = None):
    """A ``Transport`` returning a canned ``(status, body)`` and recording calls."""
    if body is None:
        body = payload()
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        if calls is not None:
            calls.append((url, headers, timeout))
        return status, raw

    return transport


def _config(tmp_path: Path, **claude_code) -> SimpleNamespace:
    """A minimal stand-in for ``Config``.

    ``subscription_usage.get_snapshot`` reads only ``db_path`` and the
    ``brain.claude_code.*`` fields, and reads them defensively, so a namespace
    with those attributes is a faithful stub. Stage 2 adds the real dataclass.
    """
    settings = {
        "subscription_usage": True,
        "subscription_usage_cache_ttl_seconds": 300,
        "subscription_usage_timeout_seconds": 10.0,
    }
    settings.update(claude_code)
    return SimpleNamespace(
        db_path=tmp_path / "istota.db",
        brain=SimpleNamespace(claude_code=SimpleNamespace(**settings)),
    )


# ---------------------------------------------------------------------------
# resolve_token
# ---------------------------------------------------------------------------


def _write_credentials(home: Path, obj: object) -> Path:
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    p = d / ".credentials.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return p


class TestResolveToken:
    def test_env_wins(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "from-file"}})
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "from-env"}, tmp_path) == (
            "from-env",
            "env",
        )

    def test_empty_env_var_falls_through_to_the_file(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "from-file"}})
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": ""}, tmp_path) == (
            "from-file",
            "file",
        )

    def test_whitespace_env_var_falls_through(self, tmp_path):
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": "from-file"}})
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "  \n"}, tmp_path) == (
            "from-file",
            "file",
        )

    def test_env_token_is_stripped(self, tmp_path):
        assert su.resolve_token({"CLAUDE_CODE_OAUTH_TOKEN": "tok\n"}, tmp_path) == ("tok", "env")

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        assert su.resolve_token({}, tmp_path) is None

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            {"other": {"accessToken": "x"}},
            {"claudeAiOauth": {"accessToken": ""}},
            {"claudeAiOauth": {"accessToken": None}},
            {"claudeAiOauth": "a string"},
            {"claudeAiOauth": {}},
            ["a", "list"],
        ],
        ids=[
            "malformed-json",
            "no-claudeAiOauth",
            "empty-token",
            "null-token",
            "oauth-not-a-dict",
            "no-accessToken",
            "payload-not-a-dict",
        ],
    )
    def test_unusable_file_returns_none(self, tmp_path, monkeypatch, content):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        _write_credentials(tmp_path, content)
        assert su.resolve_token({}, tmp_path) is None

    def test_the_ansible_sentinel_expiry_does_not_raise(self, tmp_path):
        """The literal string the Ansible role and the docker entrypoint write.

        The proof of concept did ``expires_at / 1000 <= time.time()`` on this
        field, which is an ``int`` in the keychain blob and a *string* here —
        a ``TypeError`` on exactly the deployment shape this has to work on.
        This module never looks at expiry at all.
        """
        _write_credentials(
            tmp_path,
            {"claudeAiOauth": {"accessToken": "x", "expiresAt": "9999-12-31T23:59:59.999Z"}},
        )
        assert su.resolve_token({}, tmp_path) == ("x", "file")

    def test_no_subprocess_is_spawned_off_darwin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        calls = []
        monkeypatch.setattr(
            su.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace()
        )
        assert su.resolve_token({}, tmp_path) is None
        assert calls == []

    def test_keychain_branch_on_darwin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            blob = json.dumps({"claudeAiOauth": {"accessToken": "kc-token"}})
            return subprocess.CompletedProcess(argv, 0, stdout=blob, stderr="")

        monkeypatch.setattr(su.subprocess, "run", fake_run)
        assert su.resolve_token({"USER": "someone"}, tmp_path) == ("kc-token", "keychain")
        assert seen["argv"][:2] == ["security", "find-generic-password"]
        assert "Claude Code-credentials" in seen["argv"]
        assert "someone" in seen["argv"]
        assert seen["kwargs"].get("timeout") == su.PROBE_TIMEOUT

    @pytest.mark.parametrize(
        "result",
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="not found"),
            subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="{}", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"claudeAiOauth":{}}', stderr=""),
        ],
        ids=["nonzero-exit", "not-json", "empty-object", "no-token"],
    )
    def test_keychain_failures_return_none(self, tmp_path, monkeypatch, result):
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(su.subprocess, "run", lambda *a, **k: result)
        assert su.resolve_token({"USER": "someone"}, tmp_path) is None

    def test_keychain_timeout_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="security", timeout=1)

        monkeypatch.setattr(su.subprocess, "run", boom)
        assert su.resolve_token({"USER": "someone"}, tmp_path) is None

    def test_resolve_never_writes(self, tmp_path, monkeypatch):
        """The credential file is read-only to this module, on every branch.

        This is the test that would have caught the proof of concept's
        ``--refresh`` behaviour reaching production.
        """
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        path = _write_credentials(
            tmp_path,
            {"claudeAiOauth": {"accessToken": "x", "expiresAt": "9999-12-31T23:59:59.999Z"}},
        )
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        before_listing = sorted(p.name for p in (tmp_path / ".claude").iterdir())

        assert su.resolve_token({}, tmp_path) == ("x", "file")

        assert path.read_bytes() == before_bytes
        assert path.stat().st_mtime_ns == before_mtime
        assert sorted(p.name for p in (tmp_path / ".claude").iterdir()) == before_listing
        assert not list((tmp_path / ".claude").glob("*.tmp"))


# ---------------------------------------------------------------------------
# parse_usage
# ---------------------------------------------------------------------------


class TestParseUsageLimitsPath:
    def test_the_captured_payload(self):
        windows, spend = su.parse_usage(payload(), now_ts=NOW)

        assert [w.key for w in windows] == ["session", "weekly_all", "weekly_scoped:fable"]
        assert [w.label for w in windows] == ["5-hour", "Weekly (all models)", "Weekly (Fable)"]
        assert [w.percent for w in windows] == [37.0, 12.0, 4.0]
        assert windows[0].resets_at == "2026-08-22T17:40:00Z"
        assert windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN
        assert windows[1].resets_at == "2026-08-28T19:00:00Z"
        assert windows[1].resets_in_seconds == WEEKLY_RESETS_IN
        assert windows[2].resets_at is None
        assert windows[2].resets_in_seconds is None
        assert [w.severity for w in windows] == ["normal", "normal", "normal"]
        assert [w.is_active for w in windows] == [True, False, False]
        assert spend is not None

    def test_no_codename_reaches_a_window(self):
        """The allowlist is the whole point: unreleased names must not render.

        The fixture really carries all seven, and one of them
        (``cobalt_lantern``) is a live window rather than a null, so this
        assertion is not satisfied by the payload being empty.
        """
        for name in CODENAMES:
            assert isinstance(payload()[name], (dict, type(None)))

        for source in (payload(), {k: v for k, v in payload().items() if k != "limits"}):
            windows, _ = su.parse_usage(source, now_ts=NOW)
            rendered = " ".join(w.key + " " + w.label for w in windows).lower()
            for name in CODENAMES:
                assert name not in rendered
                assert name.replace("_", " ") not in rendered

    def test_an_unknown_kind_is_kept_and_labelled(self):
        raw = payload()
        raw["limits"].append(
            {"kind": "monthly_all", "percent": 5, "severity": "normal", "resets_at": None}
        )
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows][-1] == "monthly_all"
        assert [w.label for w in windows][-1] == "Monthly all"

    def test_percent_is_clamped(self):
        raw = payload()
        raw["limits"][0]["percent"] = 150
        raw["limits"][1]["percent"] = -3
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].percent == 100.0
        assert windows[1].percent == 0.0

    def test_a_past_reset_floors_at_zero(self):
        raw = payload()
        raw["limits"][0]["resets_at"] = "2020-01-01T00:00:00Z"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].resets_in_seconds == 0
        assert windows[0].resets_at == "2020-01-01T00:00:00Z"

    def test_a_naive_reset_time_is_read_as_utc(self):
        raw = payload()
        raw["limits"][0]["resets_at"] = "2026-08-22T17:40:00"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].resets_at == "2026-08-22T17:40:00Z"
        assert windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN


class TestParseUsageFallbackPath:
    def test_limits_absent(self):
        raw = payload()
        raw.pop("limits")
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]
        assert [w.label for w in windows] == [
            "5-hour",
            "Weekly (all models)",
            "Weekly (Sonnet)",
        ]
        assert [w.percent for w in windows] == [37.0, 12.0, 4.0]
        assert windows[0].resets_in_seconds == FIVE_HOUR_RESETS_IN
        assert windows[0].severity == ""
        assert windows[0].is_active is None

    def test_an_empty_limits_list_is_not_mistaken_for_a_populated_one(self):
        raw = payload()
        raw["limits"] = []
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]

    def test_limits_not_a_list(self):
        raw = payload()
        raw["limits"] = "nope"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day", "seven_day_sonnet"]

    def test_a_null_allowlisted_window_is_skipped(self):
        raw = payload()
        raw.pop("limits")
        raw["seven_day"] = None
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["five_hour", "seven_day_sonnet"]


class TestParseUsageHostileInput:
    @pytest.mark.parametrize("raw", [None, [], ["a"], "a string", 7, True])
    def test_a_non_dict_payload_yields_nothing(self, raw):
        assert su.parse_usage(raw, now_ts=NOW) == ((), None)

    @pytest.mark.parametrize(
        "percent",
        ["40", True, False, None, float("nan"), float("inf"), float("-inf"), {}, []],
        ids=["string", "true", "false", "null", "nan", "inf", "-inf", "dict", "list"],
    )
    def test_an_unusable_percent_drops_the_window(self, percent):
        raw = payload()
        raw["limits"][0]["percent"] = percent
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["weekly_all", "weekly_scoped:fable"]

    def test_an_absent_percent_drops_the_window(self):
        raw = payload()
        raw["limits"][0].pop("percent")
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["weekly_all", "weekly_scoped:fable"]

    def test_a_non_dict_limit_entry_is_skipped(self):
        raw = payload()
        raw["limits"].insert(0, "garbage")
        raw["limits"].insert(0, None)
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["session", "weekly_all", "weekly_scoped:fable"]

    @pytest.mark.parametrize("kind", [None, "", 7, True, {}])
    def test_an_unusable_kind_is_skipped(self, kind):
        raw = payload()
        raw["limits"][0]["kind"] = kind
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["weekly_all", "weekly_scoped:fable"]

    @pytest.mark.parametrize("value", ["not a date", "", 7, True, [], {}, "2026-13-45T99:99:99"])
    def test_an_unparseable_resets_at(self, value):
        raw = payload()
        raw["limits"][0]["resets_at"] = value
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].key == "session"
        assert windows[0].resets_in_seconds is None
        # A string is carried through verbatim; a non-string becomes None.
        assert windows[0].resets_at == (value if isinstance(value, str) and value else None)

    def test_weekly_scoped_with_no_name_at_all_is_dropped(self):
        raw = payload()
        raw["limits"][2]["scope"] = {"model": {"id": None, "display_name": None}, "surface": None}
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["session", "weekly_all"]

    @pytest.mark.parametrize(
        "scope",
        [None, "nope", {}, {"model": None}, {"model": "nope"}, {"model": {}}],
        ids=["null", "string", "empty", "model-null", "model-string", "model-empty"],
    )
    def test_weekly_scoped_with_an_unusable_scope_is_dropped(self, scope):
        raw = payload()
        raw["limits"][2]["scope"] = scope
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["session", "weekly_all"]

    def test_weekly_scoped_falls_back_to_the_model_id(self):
        raw = payload()
        raw["limits"][2]["scope"] = {"model": {"id": "claude-fable-4", "display_name": None}}
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[2].key == "weekly_scoped:claude-fable-4"
        assert windows[2].label == "Weekly (claude-fable-4)"

    def test_two_scoped_windows_do_not_collide(self):
        raw = payload()
        raw["limits"].append(
            {
                "kind": "weekly_scoped",
                "percent": 9,
                "resets_at": None,
                "scope": {"model": {"id": None, "display_name": "Walrus"}},
            }
        )
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows][-2:] == ["weekly_scoped:fable", "weekly_scoped:walrus"]

    @pytest.mark.parametrize("severity", [None, 7, [], {}])
    def test_a_non_string_severity_becomes_empty(self, severity):
        raw = payload()
        raw["limits"][0]["severity"] = severity
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].severity == ""

    @pytest.mark.parametrize("is_active", ["yes", 1, None, {}])
    def test_a_non_bool_is_active_becomes_none(self, is_active):
        raw = payload()
        raw["limits"][0]["is_active"] = is_active
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert windows[0].is_active is None

    def test_a_non_dict_top_level_window_is_skipped(self):
        raw = payload()
        raw.pop("limits")
        raw["five_hour"] = "nope"
        windows, _ = su.parse_usage(raw, now_ts=NOW)
        assert [w.key for w in windows] == ["seven_day", "seven_day_sonnet"]


class TestParseSpend:
    def test_the_spend_block_is_preferred(self):
        _, spend = su.parse_usage(payload(), now_ts=NOW)
        assert spend == su.Spend(
            enabled=False, used_minor=0, limit_minor=2000, currency="USD", exponent=2, percent=0.0
        )

    def test_a_non_default_exponent_is_honoured(self):
        raw = payload()
        raw["spend"]["used"] = {"amount_minor": 1500, "currency": "JPY", "exponent": 0}
        raw["spend"]["limit"] = {"amount_minor": 3000, "currency": "JPY", "exponent": 0}
        raw["spend"]["enabled"] = True
        raw["spend"]["percent"] = 50
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend == su.Spend(
            enabled=True,
            used_minor=1500,
            limit_minor=3000,
            currency="JPY",
            exponent=0,
            percent=50.0,
        )

    def test_extra_usage_is_the_fallback(self):
        raw = payload()
        raw.pop("spend")
        raw["extra_usage"] = {
            "is_enabled": True,
            "monthly_limit": 2000,
            "used_credits": 425.0,
            "utilization": 21.25,
            "currency": "USD",
            "decimal_places": 2,
        }
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend == su.Spend(
            enabled=True,
            used_minor=425,
            limit_minor=2000,
            currency="USD",
            exponent=2,
            percent=21.25,
        )

    def test_a_missing_divisor_defaults_to_two(self):
        raw = payload()
        raw.pop("spend")
        raw["extra_usage"] = {"is_enabled": False, "monthly_limit": 2000, "used_credits": 0}
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is not None
        assert spend.exponent == 2
        assert spend.currency == "USD"

    def test_neither_block_yields_none(self):
        raw = payload()
        raw.pop("spend")
        raw.pop("extra_usage")
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is None

    @pytest.mark.parametrize("bad", [None, "nope", 7, []])
    def test_unusable_blocks_yield_none(self, bad):
        raw = payload()
        raw["spend"] = bad
        raw["extra_usage"] = bad
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend is None

    def test_hostile_spend_fields_do_not_raise(self):
        raw = payload()
        raw["spend"] = {
            "used": {"amount_minor": "nope"},
            "limit": {"amount_minor": float("nan")},
            "percent": "high",
            "enabled": "yes",
        }
        _, spend = su.parse_usage(raw, now_ts=NOW)
        assert spend == su.Spend(
            enabled=False, used_minor=0, limit_minor=0, currency="USD", exponent=2, percent=0.0
        )


# ---------------------------------------------------------------------------
# fetch_snapshot
# ---------------------------------------------------------------------------


class TestFetchSnapshot:
    def test_a_good_response(self):
        calls: list = []
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
        )
        assert snap.error == ""
        assert snap.ok
        assert snap.source == "fetch"
        assert snap.fetched_at == NOW
        assert [w.key for w in snap.windows] == ["session", "weekly_all", "weekly_scoped:fable"]
        assert len(calls) == 1

    def test_the_request_shape(self):
        calls: list = []
        su.fetch_snapshot(
            "sk-ant-oat-SENTINEL", timeout=7.5, now_ts=NOW, transport=_stub_transport(calls=calls)
        )
        url, headers, timeout = calls[0]
        assert url == "https://api.anthropic.com/api/oauth/usage"
        assert headers["Authorization"] == "Bearer sk-ant-oat-SENTINEL"
        assert headers["anthropic-beta"] == "oauth-2025-04-20"
        assert headers["User-Agent"] == su.USER_AGENT
        assert su.USER_AGENT.startswith("istota/")
        assert timeout == 7.5

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502])
    def test_an_http_error_becomes_an_error_snapshot(self, status):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(status, b'{"error":"forbidden"}'),
        )
        assert not snap.ok
        assert str(status) in snap.error
        assert snap.windows == ()
        assert snap.source == "none"

    def test_an_error_body_is_not_echoed(self):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(403, b"you shall not pass, sk-ant-oat-SENTINEL"),
        )
        assert "you shall not pass" not in snap.error

    def test_a_non_json_body(self):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(200, b"<html>nope</html>"),
        )
        assert not snap.ok
        assert snap.error
        assert "<html>" not in snap.error

    def test_a_payload_with_no_recognizable_window(self):
        snap = su.fetch_snapshot(
            "sk-ant-oat-SENTINEL",
            timeout=10.0,
            now_ts=NOW,
            transport=_stub_transport(200, {"quince": {"utilization": 3.0}}),
        )
        assert not snap.ok
        assert snap.error == su.NO_WINDOWS_ERROR
        assert snap.windows == ()

    def test_a_transport_raising_urlerror(self):
        import urllib.error

        def boom(url, headers, timeout):
            raise urllib.error.URLError("connection refused")

        snap = su.fetch_snapshot("t", timeout=10.0, now_ts=NOW, transport=boom)
        assert not snap.ok
        assert snap.error

    def test_a_transport_raising_something_unexpected(self):
        def boom(url, headers, timeout):
            raise RuntimeError("kaboom")

        snap = su.fetch_snapshot("t", timeout=10.0, now_ts=NOW, transport=boom)
        assert not snap.ok
        assert snap.error

    @pytest.mark.parametrize(
        "transport",
        [
            _stub_transport(),
            _stub_transport(403, b"denied"),
            _stub_transport(200, b"not json"),
        ],
        ids=["ok", "denied", "garbage"],
    )
    def test_the_token_never_reaches_the_snapshot(self, transport):
        token = "sk-ant-oat-SENTINEL-TOKEN"
        snap = su.fetch_snapshot(token, timeout=10.0, now_ts=NOW, transport=transport)
        assert token not in repr(snap)

    def test_a_token_inside_an_exception_is_redacted(self):
        token = "sk-ant-oat-SENTINEL-TOKEN"

        def boom(url, headers, timeout):
            raise RuntimeError(f"failed talking to {url} as {token}")

        snap = su.fetch_snapshot(token, timeout=10.0, now_ts=NOW, transport=boom)
        assert token not in snap.error
        assert token not in repr(snap)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _good_snapshot() -> su.UsageSnapshot:
    windows, spend = su.parse_usage(payload(), now_ts=NOW)
    return su.UsageSnapshot(fetched_at=NOW, windows=windows, spend=spend, source="fetch")


class TestCache:
    def test_cache_path(self, tmp_path):
        assert su.cache_path(tmp_path) == tmp_path / "subscription_usage.json"

    def test_round_trip_within_ttl(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        got = su.read_cache(p, 300, now_ts=NOW + 40)
        assert got is not None
        assert got.source == "cache"
        assert got.fetched_at == NOW
        assert got.windows == _good_snapshot().windows
        assert got.spend == _good_snapshot().spend
        assert got.error == ""

    def test_outside_ttl_returns_none_but_any_age_returns_it(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert su.read_cache(p, 300, now_ts=NOW + 301) is None
        stale = su.read_cache_any_age(p)
        assert stale is not None
        assert stale.fetched_at == NOW

    def test_a_future_fetched_at_is_stale(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert su.read_cache(p, 300, now_ts=NOW - 86400) is None

    def test_a_missing_file_returns_none(self, tmp_path):
        p = su.cache_path(tmp_path / "nope")
        assert su.read_cache(p, 300, now_ts=NOW) is None
        assert su.read_cache_any_age(p) is None

    @pytest.mark.parametrize(
        "content",
        [
            "{truncated",
            "[]",
            '"a string"',
            "{}",
            '{"fetched_at": "soon", "windows": []}',
            '{"fetched_at": 1.0, "windows": "nope"}',
            '{"fetched_at": 1.0, "windows": []}',
            '{"fetched_at": 1.0, "windows": [{"key": 7}]}',
        ],
        ids=[
            "truncated",
            "list",
            "string",
            "empty",
            "bad-fetched-at",
            "windows-not-a-list",
            "no-windows",
            "unusable-window",
        ],
    )
    def test_a_corrupt_cache_returns_none_from_both_readers(self, tmp_path, content):
        p = su.cache_path(tmp_path)
        p.write_text(content)
        assert su.read_cache(p, 300, now_ts=1.0) is None
        assert su.read_cache_any_age(p) is None

    def test_the_file_is_written_0600_with_no_tmp_left_behind(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert list(tmp_path.glob("*.tmp")) == []
        assert sorted(x.name for x in tmp_path.iterdir()) == ["subscription_usage.json"]

    def test_a_failed_snapshot_is_never_cached(self, tmp_path):
        p = su.cache_path(tmp_path)
        su.write_cache(p, _good_snapshot())
        before = p.read_bytes()
        su.write_cache(p, su.UsageSnapshot(fetched_at=NOW + 1, source="none", error="boom"))
        assert p.read_bytes() == before

    def test_a_write_into_a_missing_directory_creates_it(self, tmp_path):
        p = su.cache_path(tmp_path / "deep" / "nested")
        su.write_cache(p, _good_snapshot())
        assert su.read_cache_any_age(p) is not None

    def test_an_unwritable_directory_does_not_raise(self, tmp_path):
        target = tmp_path / "ro"
        target.mkdir()
        os.chmod(target, 0o500)
        try:
            su.write_cache(su.cache_path(target), _good_snapshot())
        finally:
            os.chmod(target, 0o700)

    def test_an_unreadable_file_returns_none(self, tmp_path):
        p = su.cache_path(tmp_path)
        p.mkdir()  # a directory where a file belongs
        assert su.read_cache(p, 300, now_ts=NOW) is None
        assert su.read_cache_any_age(p) is None


class TestSnapshotHelpers:
    def test_ok_is_false_whenever_error_is_set(self):
        assert not su.UsageSnapshot(fetched_at=NOW, error="boom").ok
        assert _good_snapshot().ok

    def test_age_seconds(self):
        assert _good_snapshot().age_seconds(NOW + 90) == 90


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    def test_disabled_config_makes_no_call(self, tmp_path):
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path, subscription_usage=False),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "none"
        assert snap.error == "disabled by config"
        assert calls == []

    def test_a_fresh_cache_makes_no_call(self, tmp_path):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 40,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "cache"
        assert snap.ok
        assert calls == []

    def test_a_stale_cache_makes_one_call(self, tmp_path):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 3600,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "fetch"
        assert snap.fetched_at == NOW + 3600
        assert len(calls) == 1

    def test_a_successful_fetch_refreshes_the_cache(self, tmp_path):
        calls: list = []
        su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        cached = su.read_cache(su.cache_path(tmp_path), 300, now_ts=NOW)
        assert cached is not None
        assert cached.fetched_at == NOW
        # And the next call is free.
        su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 10,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert len(calls) == 1

    def test_no_credential_is_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={},
            home=tmp_path,
        )
        assert snap.source == "none"
        assert snap.error == su.NO_CREDENTIAL_ERROR
        assert calls == []
        assert not su.cache_path(tmp_path).exists()

    def test_a_failed_fetch_with_a_stale_cache(self, tmp_path):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        before = su.cache_path(tmp_path).read_bytes()
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW + 3600,
            transport=_stub_transport(403, b"denied"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "stale-cache"
        assert "403" in snap.error
        assert [w.key for w in snap.windows] == ["session", "weekly_all", "weekly_scoped:fable"]
        assert snap.fetched_at == NOW
        assert snap.age_seconds(NOW + 3600) == 3600
        # The failure must not have overwritten the good reading.
        assert su.cache_path(tmp_path).read_bytes() == before

    def test_a_failed_fetch_with_no_cache(self, tmp_path):
        snap = su.get_snapshot(
            _config(tmp_path),
            now_ts=NOW,
            transport=_stub_transport(500, b"oops"),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "none"
        assert "500" in snap.error
        assert not su.cache_path(tmp_path).exists()

    def test_an_absent_claude_code_block_uses_the_documented_defaults(self, tmp_path):
        """Stage 2 adds the dataclass; until then every ``Config`` lacks it.

        The shipping default is on, with a 300s TTL, so an absent block must
        behave exactly like the example config's block.
        """
        config = SimpleNamespace(db_path=tmp_path / "istota.db", brain=SimpleNamespace())
        calls: list = []
        snap = su.get_snapshot(
            config,
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.ok
        assert len(calls) == 1
        assert su.read_cache(su.cache_path(tmp_path), 300, now_ts=NOW + 299) is not None

    def test_a_config_with_no_db_path_still_fetches(self, tmp_path):
        config = SimpleNamespace(db_path=None, brain=SimpleNamespace())
        snap = su.get_snapshot(
            config,
            now_ts=NOW,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.ok
        assert snap.source == "fetch"

    @pytest.mark.parametrize("ttl", [0, -5, "nope", None])
    def test_a_nonsense_ttl_falls_back_to_the_default(self, tmp_path, ttl):
        su.write_cache(su.cache_path(tmp_path), _good_snapshot())
        calls: list = []
        snap = su.get_snapshot(
            _config(tmp_path, subscription_usage_cache_ttl_seconds=ttl),
            now_ts=NOW + 40,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.source == "cache"
        assert calls == []

    def test_the_timeout_reaches_the_transport(self, tmp_path):
        calls: list = []
        su.get_snapshot(
            _config(tmp_path, subscription_usage_timeout_seconds=3.0),
            now_ts=NOW,
            transport=_stub_transport(calls=calls),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert calls[0][2] == 3.0

    def test_nothing_raises_on_a_config_that_is_not_a_config(self, tmp_path):
        snap = su.get_snapshot(
            object(),
            now_ts=NOW,
            transport=_stub_transport(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
            home=tmp_path,
        )
        assert snap.ok

    def test_the_token_never_reaches_the_returned_snapshot(self, tmp_path):
        token = "sk-ant-oat-SENTINEL-TOKEN"
        _write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": token}})
        for transport in (_stub_transport(), _stub_transport(403, b"denied")):
            snap = su.get_snapshot(
                _config(tmp_path),
                now_ts=NOW,
                transport=transport,
                env={"CLAUDE_CODE_OAUTH_TOKEN": token},
                home=tmp_path,
            )
            assert token not in repr(snap)
        assert token not in su.cache_path(tmp_path).read_text()
