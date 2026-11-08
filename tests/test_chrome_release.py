"""The browser's Chrome against Google's stable releases (#930).

The release history parser and the `browser.chrome_version` doctor check. The
build half, which keys the image's Chrome layers on the stable version, is in
tests/test_istota_stack.py (`TestTheChromeVersion`) and the image tier.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from istota.browser import chrome_release

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _release(version: str, start: str, end: str | None = None) -> dict:
    serving = {"startTime": start}
    if end:
        serving["endTime"] = end
    return {
        "name": f"chrome/platforms/linux/channels/stable/versions/{version}/releases/1",
        "serving": serving, "fraction": 1, "version": version,
    }


# The API's own shape, newest first, as it answered on 2026-11-07.
HISTORY = {"releases": [
    _release("157.0.8103.38", "2026-11-03T21:11:56.227067Z"),
    _release("156.0.8078.79", "2026-10-29T21:26:00.079161Z", "2026-11-03T21:11:56.227067Z"),
    _release("156.0.8078.73", "2026-10-27T20:23:56.401913Z", "2026-10-29T21:26:00.079161Z"),
    _release("156.0.8078.47", "2026-10-20T19:57:10.889791Z", "2026-10-27T20:23:56.401913Z"),
]}


class TestParsing:
    def test_a_version_is_four_numbers(self):
        assert chrome_release.parse_version("157.0.8103.38") == (157, 0, 8103, 38)
        assert chrome_release.parse_version("Google Chrome 157.0.8103.38 ") == (157, 0, 8103, 38)

    @pytest.mark.parametrize("text", ["", "157.0.8103", "Chromium", None, "157.0.x.38"])
    def test_anything_else_is_none(self, text):
        assert chrome_release.parse_version(text) is None

    def test_versions_compare_as_numbers(self):
        """156.0.8078.9 is older than 156.0.8078.79, which a string sort gets wrong."""
        assert chrome_release.parse_version("156.0.8078.9") < chrome_release.parse_version("156.0.8078.79")

    def test_the_history_is_read(self):
        releases = chrome_release.parse_releases(json.dumps(HISTORY).encode())
        assert [r.text for r in releases] == [
            "157.0.8103.38", "156.0.8078.79", "156.0.8078.73", "156.0.8078.47"]
        assert releases[0].started == datetime(2026, 11, 3, 21, 11, 56, 227067, tzinfo=timezone.utc)

    def test_a_malformed_entry_is_dropped_not_fatal(self):
        body = {"releases": [
            {"version": "nonsense", "serving": {"startTime": "2026-11-03T21:11:56Z"}},
            {"version": "157.0.8103.38"},
            _release("156.0.8078.79", "2026-10-29T21:26:00Z"),
        ]}
        assert [r.text for r in chrome_release.parse_releases(json.dumps(body).encode())] == ["156.0.8078.79"]

    def test_a_staged_rollout_is_not_stable_yet(self):
        """Google lists a staged rollout's arms beside the full releases."""
        body = {"releases": [
            {**_release("158.0.8127.18", "2026-11-06T18:00:00Z"), "fraction": 0.005},
            _release("157.0.8103.38", "2026-11-03T21:11:56Z"),
        ]}
        assert [r.text for r in chrome_release.parse_releases(json.dumps(body).encode())] == ["157.0.8103.38"]

    @pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"releases": "x"}',
                                      b'{"releases": []}', b'{"releases": [{"version": "x"}]}'])
    def test_an_unreadable_history_raises(self, body):
        with pytest.raises(chrome_release.HistoryUnavailable):
            chrome_release.parse_releases(body)


class TestTrailing:
    def _releases(self):
        return chrome_release.parse_releases(json.dumps(HISTORY).encode())

    def test_the_current_stable_trails_nothing(self):
        newest, since = chrome_release.trailing((157, 0, 8103, 38), self._releases())
        assert newest.text == "157.0.8103.38"
        assert since is None

    def test_one_release_behind_trails_since_that_release(self):
        newest, since = chrome_release.trailing((156, 0, 8078, 79), self._releases())
        assert newest.text == "157.0.8103.38"
        assert since == datetime(2026, 11, 3, 21, 11, 56, 227067, tzinfo=timezone.utc)

    def test_several_behind_trails_since_the_first_newer_release(self):
        """How long it has been behind is measured from the oldest release newer
        than it, not from the newest."""
        _, since = chrome_release.trailing((156, 0, 8078, 47), self._releases())
        assert since == datetime(2026, 10, 27, 20, 23, 56, 401913, tzinfo=timezone.utc)

    def test_a_chrome_newer_than_the_history_trails_nothing(self):
        assert chrome_release.trailing((158, 0, 0, 1), self._releases())[1] is None


class TestFetch:
    def test_it_asks_google_with_no_credential(self, monkeypatch):
        seen = []

        def get(url, headers, timeout, **kwargs):
            seen.append((url, dict(headers)))
            return 200, json.dumps(HISTORY).encode(), {}

        monkeypatch.setattr(chrome_release.fixed_get, "get", get)
        releases = chrome_release.fetch_stable_releases()
        assert releases[0].text == "157.0.8103.38"
        assert seen[0][0].startswith(
            "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/")
        assert seen[0][1] == {}

    def test_a_refusal_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(chrome_release.fixed_get, "get", lambda *a, **k: (503, b"", {}))
        with pytest.raises(chrome_release.HistoryUnavailable):
            chrome_release.fetch_stable_releases()

    def test_a_transport_failure_is_unavailable(self, monkeypatch):
        def get(*a, **k):
            raise OSError("no route")

        monkeypatch.setattr(chrome_release.fixed_get, "get", get)
        with pytest.raises(chrome_release.HistoryUnavailable):
            chrome_release.fetch_stable_releases()


class TestTheDoctorCheck:
    def _run(self, monkeypatch, *, running="157.0.8103.38", health_status=200, health_raises=None,
             history=HISTORY, enabled=True, probe=True, token_refused=False):
        from istota import doctor
        from istota.config import BrowserConfig, Config

        seen = []

        def get(url, **kwargs):
            seen.append((url, kwargs.get("headers")))
            if health_raises:
                raise health_raises
            body = {"status": "ok"} if not token_refused else {"status": "ok", "card_fill": True}
            if not token_refused:
                body["cdp_last_error"] = ""
            if running is not None and not token_refused:
                body["chrome_version"] = running
            return httpx.Response(health_status, json=body)

        def fetch(timeout=10.0):
            if history is None:
                raise chrome_release.HistoryUnavailable("ConnectError")
            return chrome_release.parse_releases(json.dumps(history).encode())

        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(chrome_release, "fetch_stable_releases", fetch)
        monkeypatch.setattr(doctor, "_utcnow", lambda: NOW)
        monkeypatch.setenv("ISTOTA_BROWSER_API_TOKEN", "t" * 32)
        config = Config()
        config.browser = BrowserConfig(enabled=enabled)
        result = doctor.check_browser_chrome_version(config, probe)
        return result, seen

    def test_the_current_stable_is_ok_and_named(self, monkeypatch):
        from istota.doctor import OK

        result, seen = self._run(monkeypatch)
        assert result.status == OK
        assert "157.0.8103.38" in result.detail
        assert seen[0][0].endswith("/health")
        assert seen[0][1] == {"Authorization": "Bearer " + "t" * 32}

    def test_a_few_days_behind_is_ok_and_says_how_far(self, monkeypatch):
        """Google's staged rollout and an update cadence of days are normal."""
        from istota.doctor import OK

        result, _ = self._run(monkeypatch, running="156.0.8078.79")
        assert result.status == OK
        assert "156.0.8078.79" in result.detail and "157.0.8103.38" in result.detail
        assert "for 3 days" in result.detail and "behind" in result.detail

    def test_more_than_a_week_behind_warns(self, monkeypatch):
        from istota.doctor import WARN

        result, _ = self._run(monkeypatch, running="156.0.8078.47")
        assert result.status == WARN
        assert "156.0.8078.47" in result.detail and "157.0.8103.38" in result.detail
        assert "for 10 days" in result.detail
        assert "istota-stack update" in result.remedy

    def test_an_unreadable_history_still_names_the_version(self, monkeypatch):
        from istota.doctor import OK

        result, _ = self._run(monkeypatch, history=None)
        assert result.status == OK
        assert "157.0.8103.38" in result.detail
        assert "could not" in result.detail

    def test_an_image_that_reports_no_version_warns(self, monkeypatch):
        from istota.doctor import WARN

        result, _ = self._run(monkeypatch, running=None)
        assert result.status == WARN
        assert "istota-stack update" in result.remedy

    def test_a_browser_that_is_down_is_some_other_check_s_news(self, monkeypatch):
        from istota.doctor import SKIP

        assert self._run(monkeypatch, health_raises=httpx.ConnectError("down"))[0].status == SKIP
        assert self._run(monkeypatch, health_status=503)[0].status == SKIP

    def test_a_token_the_browser_did_not_take_is_the_token_check_s_news(self, monkeypatch):
        """/health answers 200 to a peer without the token, with no version in
        it; that is not an old image, and `update` would not help."""
        from istota.doctor import SKIP

        result, _ = self._run(monkeypatch, token_refused=True)
        assert result.status == SKIP
        assert "browser.api_token" in result.detail

    def test_disabled_and_unprobed_skip_without_a_request(self, monkeypatch):
        from istota.doctor import SKIP

        for kwargs in ({"enabled": False}, {"probe": False}):
            result, seen = self._run(monkeypatch, **kwargs)
            assert result.status == SKIP
            assert seen == []

    def test_it_is_registered_for_deployments(self):
        from istota import doctor

        assert "browser.chrome_version" in [check[0] for check in doctor.CHECKS]
        assert doctor.CHECK_SCOPES["browser.chrome_version"] == doctor.DEPLOYMENT
