"""The subscription section of `GET /istota/api/admin/stats`.

On a subscription deployment the dashboard's cost column is deliberately blank —
a plan-equivalent list price is not spend — so the rate-limit windows are the
only budget there is. This section carries them onto `/admin`.

Three properties are worth a test file of their own:

* **The payload shape is a contract.** The `api.ts` type and the card that
  renders it are a later stage's work, so nothing on the frontend pins this yet
  and these tests assert the whole dict rather than a key at a time.
* **`available: false` still renders.** Disabled, no credential and a failed
  fetch with no stale fallback all produce `available: false` *with* an `error`,
  so the card can say why rather than vanishing. An operator who expects the
  reading and does not get it must learn the reason.
* **The credential never reaches the response.** `token_source` is the
  resolver's branch name (`env` / `file` / `keychain`), never the token. Nothing
  downstream would catch a leak: this credential is not in the config, so the
  web app has no redaction pass that would see it.

The suite-wide `_no_subscription_usage_lookups` fixture neutralizes
`get_snapshot` for every test in the suite, and its default is a *no-credential*
snapshot — so a test here that forgot to patch would quietly exercise the
`available: false` path and pass for the wrong reason. Every test below pins the
path it means to test.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from istota import subscription_usage as su
from tests.test_web_app import _needs_web_deps, _patch_app


# Captured before the autouse fixture replaces it, so the sentinel test can run
# the real resolution/fetch/cache policy with only the host substituted.
_REAL_GET_SNAPSHOT = su.get_snapshot

# Shaped like the real credential and matching nothing in the config, which is
# the point: the section has to keep it out of the payload on its own.
_TOKEN_SENTINEL = "sk-ant-oat01-" + "z" * 40

# Never the running developer's real home. `get_snapshot(home=None)` means "use
# `Path.home()`", not "there is no home".
_NO_HOME = "/nonexistent/istota-test-home"

NOW = datetime(2026, 8, 22, 16, 40, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 22, 16, 35, 12, tzinfo=timezone.utc)


def _window(**kw):
    base = dict(
        key="session",
        label="5-hour",
        percent=40.0,
        resets_at="2026-08-22T18:07:33Z",
        resets_in_seconds=3600,
        severity="normal",
        is_active=True,
    )
    base.update(kw)
    return su.UsageWindow(**base)


def _snapshot(**kw):
    base = dict(
        fetched_at=FETCHED.timestamp(),
        windows=(_window(),),
        spend=su.Spend(
            enabled=False,
            used_minor=0,
            limit_minor=2000,
            currency="USD",
            exponent=2,
            percent=0.0,
        ),
        source="cache",
        token_source="env",
        error="",
    )
    base.update(kw)
    return su.UsageSnapshot(**base)


def _patch_snapshot(monkeypatch, result):
    """Replace `get_snapshot` with one that returns (or raises) `result`.

    Returns the list of `now_ts` values it was called with, so a test can assert
    the section reads the payload's one clock rather than the wall clock.
    """
    calls = []

    def _fake(config, *, now_ts, **kwargs):
        calls.append(now_ts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(su, "get_snapshot", _fake)
    return calls


def _config(tmp_path):
    from istota import db
    from tests.test_web_app import _make_config

    config = _make_config(tmp_path)
    config.db_path = tmp_path / "istota.db"
    config.admin_users = {"alice"}
    db.init_db(config.db_path)
    return config


async def _login(client, username):
    import istota.web_app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username}
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


class _UsageTransport:
    """A stub `subscription_usage` transport that records every call.

    Recording rather than asserting: the section is called inside a best-effort
    try/except, so a transport that asserted by raising would be swallowed into
    an `{"error": ...}` payload and the test would pass whatever happened.

    `raises` is the exception to throw instead of answering, and it is how the
    one realistic leak gets exercised: `http.client` puts a rejected header
    value into its own `ValueError` as a repr, so an exception message is the
    single place the credential can re-enter a string the module builds.
    """

    def __init__(self, status=200, body=b"{}", raises=None, response_headers=None):
        self.status = status
        self.body = body
        self.raises = raises
        self.response_headers = dict(response_headers or {})
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        if self.raises is not None:
            raise self.raises
        return self.status, self.body, dict(self.response_headers)


def _usage_body(percent=40):
    resets_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return json.dumps(
        {
            "limits": [
                {
                    "kind": "session",
                    "group": "session",
                    "percent": percent,
                    "severity": "normal",
                    "resets_at": resets_at,
                    "scope": None,
                    "is_active": True,
                }
            ]
        }
    ).encode()


def _drive_real_snapshot(monkeypatch, transport, env):
    """Reinstate the real `get_snapshot` with only the host substituted.

    The section calls `get_snapshot(config, now_ts=...)` and has nowhere to pass
    a transport, an environment or a home — right for production, useless here,
    because the resolver would then read the developer's own keychain and the
    fetch would be a live request.
    """
    from pathlib import Path

    # The env branch wins before the resolver ever reaches the keychain, so this
    # is belt-and-braces — but a `security find-generic-password` spawned by a
    # test on a developer's laptop can pop an authorization dialog, and the
    # resolver's order is not this test's to depend on.
    monkeypatch.setattr(su.platform, "system", lambda: "Linux")

    def _wrapper(config, *, now_ts, **kwargs):
        return _REAL_GET_SNAPSHOT(
            config,
            now_ts=now_ts,
            transport=transport,
            env=env,
            home=Path(_NO_HOME),
        )

    monkeypatch.setattr(su, "get_snapshot", _wrapper)


@_needs_web_deps
class TestSubscriptionSection:
    """`_admin_subscription_section` — the payload the card is typed against."""

    def test_a_reading_renders_the_documented_shape(self, tmp_path, monkeypatch):
        from istota import web_app

        _patch_snapshot(monkeypatch, _snapshot())

        section = web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert section == {
            "available": True,
            "windows": [
                {
                    "key": "session",
                    "label": "5-hour",
                    "percent": 40.0,
                    "resets_at": "2026-08-22T18:07:33Z",
                    "resets_in_seconds": 3600,
                    "severity": "normal",
                    "is_active": True,
                }
            ],
            "spend": {
                "enabled": False,
                "used_minor": 0,
                "limit_minor": 2000,
                "currency": "USD",
                "exponent": 2,
                "percent": 0.0,
            },
            "fetched_at": "2026-08-22T16:35:12Z",
            "stale": False,
            "token_source": "env",
            "warn_percent": 80.0,
            "high_percent": 95.0,
            "error": "",
        }
        # It rides a JSON endpoint: a tuple or a dataclass here is a 500 there.
        json.dumps(section)

    def test_the_operator_s_own_thresholds_reach_the_card(
        self, tmp_path, monkeypatch
    ):
        """The tint is the operator's rule, not a literal in the TypeScript.

        The card colours each tile by these two numbers, so a threshold set in
        the TOML and not carried here is a threshold the dashboard ignores
        without ever saying it did.
        """
        from istota import web_app

        _patch_snapshot(monkeypatch, _snapshot())
        config = _config(tmp_path)
        config.brain.claude_code.subscription_usage_warn_percent = 55.0
        config.brain.claude_code.subscription_usage_high_percent = 70.0

        section = web_app._admin_subscription_section(config, NOW)

        assert section["warn_percent"] == 55.0
        assert section["high_percent"] == 70.0

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Not a number: the default, so the wire never carries a `null` the
            # frontend would have to invent a literal for.
            ("80", 80.0),
            (None, 80.0),
            (float("nan"), 80.0),
            (float("inf"), 80.0),
            # `True` is an `int`, and 1.0 would tint every tile amber at 1%.
            (True, 80.0),
            # A real number out of range is carried through *unchanged*. See the
            # test below: clamping it here is what would split the two readers.
            (400.0, 400.0),
            (-5.0, -5.0),
            (0, 0.0),
        ],
    )
    def test_an_unusable_threshold_falls_back_to_the_documented_default(
        self, tmp_path, monkeypatch, value, expected
    ):
        """Asserted value by value, not by type and range.

        A range assertion passes against a broken guard: drop the `bool` arm and
        `True` becomes `1.0`, which is a float inside `[0, 100]` and so satisfies
        every weaker test that could be written here — while tinting every tile
        amber at 1%, which is the regression this exists to catch.
        """
        from istota import web_app

        _patch_snapshot(monkeypatch, _snapshot())
        config = _config(tmp_path)
        config.brain.claude_code.subscription_usage_warn_percent = value

        section = web_app._admin_subscription_section(config, NOW)

        assert section["warn_percent"] == expected
        assert isinstance(section["warn_percent"], float)

    def test_a_threshold_is_not_clamped_on_its_way_to_the_card(
        self, tmp_path, monkeypatch
    ):
        """The same rule as doctor's, including the clamp neither applies.

        This looks like the one place a tightening would be free, and it is the
        one place it costs something: the card and `istota doctor` are two
        readers of one number, and doctor compares against `min(warn, high)`
        unclamped. Clamp only here and a `150` becomes 100 on the wire, so the
        card paints a full window red while doctor calls the same reading OK.
        The loader is where a nonsense threshold is corrected.
        """
        from istota import web_app

        _patch_snapshot(monkeypatch, _snapshot())
        config = _config(tmp_path)
        config.brain.claude_code.subscription_usage_warn_percent = 150.0
        config.brain.claude_code.subscription_usage_high_percent = 150.0

        section = web_app._admin_subscription_section(config, NOW)

        assert section["warn_percent"] == 150.0
        assert section["high_percent"] == 150.0

    def test_it_reads_the_payload_s_own_clock(self, tmp_path, monkeypatch):
        """One clock for the whole payload.

        Reading the wall clock here would measure a cached snapshot's age
        against a different moment than the rest of the dashboard's timestamps.
        """
        from istota import web_app

        calls = _patch_snapshot(monkeypatch, _snapshot())

        web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert calls == [NOW.timestamp()]

    def test_a_stale_reading_renders_its_numbers_and_says_it_is_stale(
        self, tmp_path, monkeypatch
    ):
        """The stale-cache branch: real windows *and* the failure that aged them.

        Doctor renders the same pair for the same reason — an old real reading
        beats no reading, as long as the surface admits it is old.
        """
        from istota import web_app

        _patch_snapshot(
            monkeypatch,
            _snapshot(source="stale-cache", error="could not reach api.anthropic.com"),
        )

        section = web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert section["available"] is True
        assert section["stale"] is True
        assert section["error"] == "could not reach api.anthropic.com"
        assert section["windows"][0]["percent"] == 40.0
        assert section["fetched_at"] == "2026-08-22T16:35:12Z"

    @pytest.mark.parametrize(
        "error,token_source",
        [
            # No branch resolved anything, so there is no credential to name.
            (su.NO_CREDENTIAL_ERROR, ""),
            (su.DISABLED_ERROR, ""),
            # A refused credential *is* named — which one was rejected is the
            # whole diagnostic, and the module stamps the branch on its failures
            # for exactly that reason.
            ("the usage endpoint returned HTTP 403", "env"),
        ],
    )
    def test_the_unavailable_cases_still_render_with_a_reason(
        self, tmp_path, monkeypatch, error, token_source
    ):
        """Disabled, no credential and a dead fetch with no cache behind it.

        All three are `available: false` *with* an error, so the card renders a
        one-line note instead of disappearing.
        """
        from istota import web_app

        _patch_snapshot(
            monkeypatch,
            su.UsageSnapshot(
                fetched_at=0.0, source="none", error=error, token_source=token_source
            ),
        )

        section = web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert section["available"] is False
        assert section["error"] == error
        assert section["windows"] == []
        assert section["spend"] is None
        assert section["stale"] is False
        # No data means no fetch time: a `fetched_at` of 0 rendered as 1970 is
        # worse than an absent one.
        assert section["fetched_at"] is None
        assert section["token_source"] == token_source

    def test_a_successful_request_that_named_no_window_is_unavailable(
        self, tmp_path, monkeypatch
    ):
        """A shipped shape change reads as unavailable, not as 0% utilization.

        The fetch time survives — the endpoint did answer — but there is nothing
        to render, so the card must not draw an empty grid.
        """
        from istota import web_app

        _patch_snapshot(
            monkeypatch,
            _snapshot(
                windows=(),
                source="fetch",
                error=su.NO_WINDOWS_ERROR,
            ),
        )

        section = web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert section["available"] is False
        assert section["stale"] is False
        assert section["error"] == su.NO_WINDOWS_ERROR
        assert section["fetched_at"] == "2026-08-22T16:35:12Z"

    def test_unavailable_is_never_rendered_without_a_reason(
        self, tmp_path, monkeypatch
    ):
        """The one payload this section must not emit.

        `get_snapshot` already promises a windowless result carries
        `NO_WINDOWS_ERROR`, so this drives a snapshot it does not produce —
        which is the point: a card reading `available: false` with a blank note
        tells an operator nothing, and there is nowhere downstream to notice.
        Doctor guards the same case for the same reason.
        """
        from istota import web_app

        _patch_snapshot(monkeypatch, _snapshot(windows=(), source="fetch", error=""))

        section = web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert section["available"] is False
        assert section["error"] == su.NO_WINDOWS_ERROR

    def test_a_window_with_no_reset_keeps_its_nulls(self, tmp_path, monkeypatch):
        """`resets_at: null` is a real state — the card says "no reset
        scheduled" rather than inventing a countdown."""
        from istota import web_app

        _patch_snapshot(
            monkeypatch,
            _snapshot(
                windows=(
                    _window(
                        key="weekly_scoped:fable",
                        label="Weekly (Fable)",
                        percent=0.0,
                        resets_at=None,
                        resets_in_seconds=None,
                        is_active=False,
                    ),
                ),
                spend=None,
            ),
        )

        section = web_app._admin_subscription_section(_config(tmp_path), NOW)

        assert section["windows"] == [
            {
                "key": "weekly_scoped:fable",
                "label": "Weekly (Fable)",
                "percent": 0.0,
                "resets_at": None,
                "resets_in_seconds": None,
                "severity": "normal",
                "is_active": False,
            }
        ]
        assert section["spend"] is None


@_needs_web_deps
class TestFetchedAtRendering:
    """`_iso_utc_from_epoch` — the one number in this payload that is not
    already a string when it arrives.

    It comes back out of a JSON file on disk that two processes write and an
    operator can edit, so the guard is not theoretical. Driven directly because
    every one of these inputs is unreachable through a `UsageSnapshot` built by
    the module, which is exactly why deleting the guard would leave the rest of
    the file green.
    """

    @pytest.mark.parametrize(
        "value",
        [
            0.0,
            -1.0,
            float("nan"),
            float("inf"),
            float("-inf"),
            1e18,  # past the year 9999
            "not a number",
            None,
        ],
    )
    def test_an_unusable_stamp_renders_as_null(self, value):
        from istota import web_app

        assert web_app._iso_utc_from_epoch(value) is None

    def test_a_real_stamp_renders_as_iso_utc(self):
        from istota import web_app

        assert (
            web_app._iso_utc_from_epoch(FETCHED.timestamp()) == "2026-08-22T16:35:12Z"
        )


@_needs_web_deps
class TestSubscriptionOnTheStatsEndpoint:
    """The section as the dashboard actually receives it."""

    async def test_the_endpoint_carries_the_section(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _patch_snapshot(monkeypatch, _snapshot())
        app = _patch_app(config)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com"
        ) as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)

        assert resp.status_code == 200
        section = resp.json()["subscription"]
        assert section["available"] is True
        assert section["token_source"] == "env"
        assert section["windows"][0]["label"] == "5-hour"
        assert section["stale"] is False

    async def test_a_raising_section_degrades_to_an_error_string(
        self, tmp_path, monkeypatch
    ):
        """Best-effort like every other section — an error string in the
        payload, not a 500 on the whole dashboard."""
        config = _config(tmp_path)
        _patch_snapshot(monkeypatch, RuntimeError("boom"))
        app = _patch_app(config)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com"
        ) as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["subscription"] == {"error": "boom"}
        # And the rest of the payload survived.
        assert "tasks" in payload
        assert "usage" in payload
        assert "error" not in payload

    async def test_the_key_is_always_present(self, tmp_path, monkeypatch):
        """Present rather than absent, on the branch that has nothing to report.

        There are two shapes on the wire, not one: this section's full dict, and
        the `{"error": ...}` the best-effort wrapper substitutes when it raises
        (above). Whatever the card is typed against has to tolerate both, and
        the key itself is the only thing common to them.
        """
        config = _config(tmp_path)
        _patch_snapshot(
            monkeypatch,
            su.UsageSnapshot(
                fetched_at=0.0, source="none", error=su.NO_CREDENTIAL_ERROR
            ),
        )
        app = _patch_app(config)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com"
        ) as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)

        section = resp.json()["subscription"]
        assert section["available"] is False
        assert section["error"] == su.NO_CREDENTIAL_ERROR

    @pytest.mark.parametrize("status", [200, 403])
    async def test_the_token_value_is_never_in_the_response(
        self, tmp_path, monkeypatch, status
    ):
        """Driven through the *real* resolution and fetch, with a stub host.

        A test against a hand-built snapshot could not catch this: the token
        would never have been anywhere near the code under test. Here it is
        resolved from the environment, sent in a header, and must appear in
        neither half of the response.
        """
        config = _config(tmp_path)
        transport = _UsageTransport(status=status, body=_usage_body(40))
        _drive_real_snapshot(
            monkeypatch,
            transport,
            {"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        app = _patch_app(config)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com"
        ) as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)

        assert resp.status_code == 200
        assert _TOKEN_SENTINEL not in resp.text
        assert "sk-ant" not in resp.text
        section = resp.json()["subscription"]
        assert section["token_source"] == "env", "the branch name, not the token"
        assert section["available"] is (status == 200)
        assert transport.calls, "a resolvable credential should have been tried"
        assert (
            _TOKEN_SENTINEL in transport.calls[0][1]["Authorization"]
        ), "the token belongs in the header and nowhere else"

    async def test_a_transport_exception_carrying_the_token_leaks_nothing(
        self, tmp_path, monkeypatch
    ):
        """The one path where the credential can re-enter an error string.

        Both status cases above return normally, so neither reaches the branch
        that builds an error out of an *exception* — and that is where a leak
        would come from: `http.client` embeds a rejected header value in its own
        `ValueError` as a repr. The module answers by reporting the exception's
        class rather than its message, and an implementation that used
        `str(exc)` would pass every other test in this file and fail this one.

        The app log is deliberately not asserted here. The module logs the
        failure at DEBUG with `exc_info=True`, so the traceback carries whatever
        the exception said — but a real transport cannot raise carrying an
        *accepted* credential, because `_clean_token` rejects exactly the tokens
        `http.client` would refuse and quote back. This stub constructs the case
        that resolution already removes, so an assertion on the log here would
        pin a promise the module does not make.
        """
        config = _config(tmp_path)
        transport = _UsageTransport(
            raises=ValueError(f"Invalid header value {_TOKEN_SENTINEL!r}")
        )
        _drive_real_snapshot(
            monkeypatch,
            transport,
            {"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL},
        )
        app = _patch_app(config)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com"
        ) as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/stats", cookies=cookies)

        assert resp.status_code == 200
        section = resp.json()["subscription"]
        assert section["available"] is False
        assert _TOKEN_SENTINEL not in resp.text
        assert "sk-ant" not in resp.text
        # The class name, not the message: that substitution is the mechanism,
        # and an error reading "an unreachable endpoint" with nothing in it
        # would satisfy the two assertions above just as well.
        assert "ValueError" in section["error"]
        assert "Invalid header value" not in section["error"]
