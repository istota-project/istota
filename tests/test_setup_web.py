"""The first-run setup server (`istota setup --web`), driven over HTTP.

Each test builds the app with `create_setup_app` against temp directories and
a code file, as `istota-stack` would hand them over, and a clock the test
moves, so the throttle and the idle expiry are driven without waiting.
"""

from __future__ import annotations

import json
import logging
import re
import types
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from istota import setup_wizard  # noqa: E402
from istota.webui import setup_app  # noqa: E402

CODE = "7KQ2M-X9D4F-H3T8W-QP6ZA"
SENTINEL = "sentinel-credential-9f3a"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def dirs(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    code_path = vm_dir / "setup-code"
    code_path.write_text(CODE + "\n")
    return types.SimpleNamespace(data_dir=data_dir, vm_dir=vm_dir, code_path=code_path)


@pytest.fixture
def clock():
    return _Clock()


def _make(dirs, clock, *, local=False, **kwargs):
    app = setup_app.create_setup_app(
        data_dir=dirs.data_dir, vm_dir=dirs.vm_dir, code_path=dirs.code_path,
        local=local, clock=clock, **kwargs,
    )
    # A local run answers only at a loopback name, as its publish is reached.
    base = "http://localhost:8080" if local else "https://testserver"
    return app, TestClient(app, base_url=base)


@pytest.fixture
def server(dirs, clock):
    return _make(dirs, clock)


ORIGIN = {"origin": "https://testserver"}


def _unlock(client, code=CODE, headers=ORIGIN):
    return client.post("/istota/api/setup/unlock", json={"code": code}, headers=headers)


@pytest.fixture
def unlocked(server):
    app, client = server
    assert _unlock(client).status_code == 200
    return app, client


class TestUnlock:
    def test_the_right_code_sets_a_strict_session_cookie(self, server):
        _app, client = server
        resp = _unlock(client)
        assert resp.status_code == 200
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f"{setup_app.SESSION_COOKIE}=")
        lowered = cookie.lower()
        assert "httponly" in lowered
        assert "samesite=strict" in lowered
        assert "secure" in lowered
        assert "path=/istota" in lowered

    def test_a_local_run_drops_secure_only(self, dirs, clock):
        _app, client = _make(dirs, clock, local=True)
        resp = _unlock(client, headers={"origin": "http://localhost:8080"})
        assert resp.status_code == 200
        lowered = resp.headers["set-cookie"].lower()
        assert "secure" not in lowered
        assert "httponly" in lowered and "samesite=strict" in lowered

    def test_the_code_is_read_the_way_people_type_it(self, server):
        _app, client = server
        assert _unlock(client, code=" 7kq2m x9d4f-h3t8w qp6za ").status_code == 200

    def test_crockford_lookalikes_are_the_same_code(self, dirs, clock):
        dirs.code_path.write_text("0110A-BBBBB-CCCCC-DDDDD")
        _app, client = _make(dirs, clock)
        assert _unlock(client, code="oIlOa-bbbbb-ccccc-ddddd").status_code == 200

    def test_a_second_unlock_with_the_right_code_is_refused(self, unlocked):
        _app, client = unlocked
        client.cookies.clear()
        resp = _unlock(client)
        assert resp.status_code == 403
        assert "set-cookie" not in resp.headers

    def test_a_wrong_code_and_a_spent_code_get_the_same_answer(self, dirs, clock):
        _app, client = _make(dirs, clock)
        wrong = _unlock(client, code="AAAAA-AAAAA-AAAAA-AAAAA")
        assert _unlock(client).status_code == 200
        client.cookies.clear()
        spent = _unlock(client)
        assert wrong.status_code == spent.status_code == 403
        assert wrong.content == spent.content

    def test_the_sixth_failure_in_a_minute_is_throttled(self, server, clock):
        _app, client = server
        for _ in range(5):
            assert _unlock(client, code="nope").status_code == 403
        assert _unlock(client, code="nope").status_code == 429
        # Even the right code waits out the window rather than being compared.
        assert _unlock(client).status_code == 429
        clock.now += 61
        assert _unlock(client).status_code == 200

    def test_after_fifty_failures_even_the_right_code_is_refused(self, server, clock):
        _app, client = server
        for i in range(setup_app.UNLOCK_FAILURES_TOTAL):
            if i and i % 5 == 0:
                clock.now += 61
            assert _unlock(client, code="nope").status_code == 403
        clock.now += 61
        assert _unlock(client).status_code == 403

    def test_the_fiftieth_failure_stops_the_server(self, dirs, clock):
        # A server nobody can unlock would hold the setup lock and the port
        # for good, so no new run could start; it stops instead.
        app, client = _make(dirs, clock)
        exits = []
        app.state.setup.on_exit = exits.append
        for i in range(setup_app.UNLOCK_FAILURES_TOTAL):
            if i and i % 5 == 0:
                clock.now += 61
            resp = _unlock(client, code="nope")
            assert resp.status_code == 403
        assert resp.json()["error"] == "locked_out"
        assert "setup --web" in resp.json()["message"]
        assert exits == [setup_app.EXIT_LOCKED_OUT]

    @pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080"])
    def test_a_local_run_answers_a_loopback_host(self, dirs, clock, host):
        _app, client = _make(dirs, clock, local=True)
        resp = _unlock(client, headers={"host": host, "origin": f"http://{host}"})
        assert resp.status_code == 200

    def test_a_local_run_refuses_a_rebound_name(self, dirs, clock):
        # A page that rebinds its own name to 127.0.0.1 sends a matching Origin.
        app, client = _make(dirs, clock, local=True)
        resp = _unlock(client, headers={"host": "evil.example:8080", "origin": "http://evil.example:8080"})
        assert resp.status_code == 403
        assert app.state.setup.failures_total == 0

    def test_unlock_refuses_a_cross_site_origin(self, server):
        _app, client = server
        resp = _unlock(client, headers={"origin": "https://evil.example"})
        assert resp.status_code == 403
        # Nothing was compared, so nothing was spent.
        assert _unlock(client).status_code == 200

    def test_the_code_never_reaches_the_log(self, server, caplog):
        _app, client = server
        with caplog.at_level(logging.DEBUG):
            _unlock(client, code="WRONG-CODE-12345")
            _unlock(client)
        assert "WRONG-CODE-12345" not in caplog.text
        assert "7KQ2M" not in caplog.text
        assert "/istota/api/setup/unlock" in caplog.text


SESSION_ROUTES = [
    ("GET", "/istota/api/setup/draft", None),
    ("PUT", "/istota/api/setup/draft", {"bot_name": "Ada"}),
    ("POST", "/istota/api/setup/validate", {"step": "welcome"}),
    ("POST", "/istota/api/setup/check/model", None),
    ("POST", "/istota/api/setup/finish", None),
    ("GET", "/istota/api/setup/models", None),
]
STATE_CHANGING = [route for route in SESSION_ROUTES if route[0] != "GET"]


class TestTheSession:
    @pytest.mark.parametrize("method,path,body", SESSION_ROUTES)
    def test_every_session_route_needs_the_cookie(self, server, method, path, body):
        _app, client = server
        resp = client.request(method, path, json=body, headers=ORIGIN)
        assert resp.status_code == 401

    @pytest.mark.parametrize("method,path,body", SESSION_ROUTES)
    def test_a_forged_cookie_is_not_a_session(self, unlocked, method, path, body):
        _app, client = unlocked
        client.cookies.clear()
        client.cookies.set(setup_app.SESSION_COOKIE, "forged")
        resp = client.request(method, path, json=body, headers=ORIGIN)
        assert resp.status_code == 401

    @pytest.mark.parametrize("method,path,body", STATE_CHANGING)
    @pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {}])
    def test_a_state_changing_route_needs_a_matching_origin(self, unlocked, method, path, body, headers):
        _app, client = unlocked
        resp = client.request(method, path, json=body, headers=headers)
        assert resp.status_code == 403

    def test_a_referer_on_the_same_host_is_an_origin(self, unlocked):
        _app, client = unlocked
        resp = client.put(
            "/istota/api/setup/draft", json={"bot_name": "Ada"},
            headers={"referer": "https://testserver/istota/setup"},
        )
        assert resp.status_code == 200

    def test_an_idle_hour_ends_the_session_and_the_server(self, dirs, clock):
        app, client = _make(dirs, clock)
        exits = []
        app.state.setup.on_exit = exits.append
        assert _unlock(client).status_code == 200
        clock.now += setup_app.IDLE_SECONDS - 1
        assert client.get("/istota/api/setup/draft").status_code == 200
        clock.now += setup_app.IDLE_SECONDS + 1
        assert client.get("/istota/api/setup/draft").status_code == 401
        assert exits == [setup_app.EXIT_TIMED_OUT]
        assert app.state.setup.exit_code == setup_app.EXIT_TIMED_OUT

    def test_the_idle_watch_expires_a_session_nobody_touches(self, dirs, clock):
        app, client = _make(dirs, clock)
        exits = []
        app.state.setup.on_exit = exits.append
        assert _unlock(client).status_code == 200
        setup_app.expire_if_idle(app.state.setup)
        assert exits == []
        clock.now += setup_app.IDLE_SECONDS + 1
        setup_app.expire_if_idle(app.state.setup)
        assert exits == [setup_app.EXIT_TIMED_OUT]

    def test_no_session_means_no_idle_expiry(self, dirs, clock):
        app, _client = _make(dirs, clock)
        clock.now += 10 * setup_app.IDLE_SECONDS
        setup_app.expire_if_idle(app.state.setup)
        assert app.state.setup.exit_code is None

    def test_state_reports_the_phase_and_the_host(self, server):
        _app, client = server
        before = client.get("/istota/api/setup/state", headers={"host": "203.0.113.5"})
        assert before.status_code == 200
        assert before.json()["phase"] == "locked"
        assert before.json()["host"] == "203.0.113.5"
        assert before.json()["local"] is False
        _unlock(client)
        after = client.get("/istota/api/setup/state").json()
        assert after["phase"] == "open"
        fields = [m["field"] for m in after["modules_available"]]
        assert set(fields) <= {f for f, _m, _q in setup_wizard._OPT_OUT_MODULES}


class TestTheBody:
    def test_a_body_over_64_kib_is_413(self, unlocked):
        _app, client = unlocked
        big = json.dumps({"bot_name": "x" * (setup_app.BODY_LIMIT + 1)})
        resp = client.put(
            "/istota/api/setup/draft", content=big,
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert resp.status_code == 413

    def test_an_unlock_body_over_64_kib_is_413(self, server):
        _app, client = server
        resp = client.post(
            "/istota/api/setup/unlock", content=b"{" + b" " * (setup_app.BODY_LIMIT + 1),
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert resp.status_code == 413

    @pytest.mark.parametrize(
        "raw",
        [
            '{"native_api_key": "%s"' % SENTINEL,
            '["%s"]' % SENTINEL,
            '{"%s": 1}' % SENTINEL,
            '{"password": ["%s"]}' % SENTINEL,
        ],
    )
    def test_a_refused_body_does_not_echo_what_it_carried(self, unlocked, raw):
        _app, client = unlocked
        resp = client.put(
            "/istota/api/setup/draft", content=raw,
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert SENTINEL not in resp.text
        assert "error" in resp.json() and "message" in resp.json()


CREDENTIALS = {
    "native_api_key": SENTINEL + "-a",
    "nextcloud_app_password": SENTINEL + "-b",
    "claude_code_oauth_token": SENTINEL + "-c",
    "imap_password": SENTINEL + "-d",
    "caldav_password": SENTINEL + "-e",
    "password": SENTINEL + "-f",
}


class TestTheDraft:
    def test_a_draft_round_trips_without_its_credentials(self, unlocked):
        _app, client = unlocked
        body = {"bot_name": "Ada", "timezone": "Europe/Berlin", **CREDENTIALS}
        assert client.put("/istota/api/setup/draft", json=body, headers=ORIGIN).status_code == 200
        resp = client.get("/istota/api/setup/draft")
        assert resp.status_code == 200
        assert SENTINEL not in resp.text
        draft = resp.json()["draft"]
        assert draft["bot_name"] == "Ada"
        for name in CREDENTIALS:
            assert draft[name] == {"set": True}

    def test_the_put_answer_carries_no_credential_either(self, unlocked):
        _app, client = unlocked
        resp = client.put("/istota/api/setup/draft", json=CREDENTIALS, headers=ORIGIN)
        assert resp.status_code == 200
        assert SENTINEL not in resp.text

    def test_puts_merge_and_a_null_credential_keeps_its_value(self, unlocked):
        app, client = unlocked
        client.put("/istota/api/setup/draft", json={"bot_name": "Ada", **CREDENTIALS}, headers=ORIGIN)
        client.put(
            "/istota/api/setup/draft",
            json={"display_name": "Ada L", "native_api_key": None, "bot_name": None},
            headers=ORIGIN,
        )
        draft = client.get("/istota/api/setup/draft").json()["draft"]
        assert draft["display_name"] == "Ada L"
        assert "bot_name" not in draft
        assert draft["native_api_key"] == {"set": True}
        assert app.state.setup.draft["native_api_key"] == CREDENTIALS["native_api_key"]

    def test_an_empty_credential_reads_as_unset(self, unlocked):
        _app, client = unlocked
        client.put("/istota/api/setup/draft", json={"native_api_key": ""}, headers=ORIGIN)
        assert client.get("/istota/api/setup/draft").json()["draft"]["native_api_key"] == {"set": False}

    def test_an_unknown_key_is_refused_and_nothing_is_merged(self, unlocked):
        app, client = unlocked
        resp = client.put(
            "/istota/api/setup/draft", json={"bot_name": "Ada", "ssh_key": SENTINEL},
            headers=ORIGIN,
        )
        assert resp.status_code == 400
        assert SENTINEL not in resp.text
        assert app.state.setup.draft == {}


def _validate(client, step):
    return client.post("/istota/api/setup/validate", json={"step": step}, headers=ORIGIN)


class TestValidate:
    def test_only_the_named_steps_errors_come_back(self, unlocked):
        _app, client = unlocked
        client.put(
            "/istota/api/setup/draft",
            json={
                "timezone": "Mars/Olympus", "brain_kind": "native", "user_id": "ada",
                "user_email": "ada@example.com",
            },
            headers=ORIGIN,
        )
        welcome = _validate(client, "welcome")
        assert welcome.status_code == 200
        assert welcome.json()["ok"] is False
        assert {e["field"] for e in welcome.json()["fields"]} == {"timezone"}

        model = _validate(client, "model").json()
        assert {e["field"] for e in model["fields"]} == {"native_model", "native_api_key"}

    def test_a_clean_step_is_ok(self, unlocked):
        _app, client = unlocked
        client.put(
            "/istota/api/setup/draft",
            json={"user_id": "ada", "timezone": "Europe/Berlin", "user_email": "ada@example.com"},
            headers=ORIGIN,
        )
        assert _validate(client, "welcome").json() == {"ok": True, "fields": []}

    def test_a_payload_refusal_is_merged_into_its_step(self, unlocked):
        _app, client = unlocked
        client.put("/istota/api/setup/draft", json={"storage": "nextcloud", "user_id": "ada"}, headers=ORIGIN)
        fields = {e["field"] for e in _validate(client, "nextcloud").json()["fields"]}
        assert fields == {"nextcloud_url"}

    def test_a_wrong_type_is_a_field_error_not_a_500(self, unlocked):
        _app, client = unlocked
        client.put("/istota/api/setup/draft", json={"listen_port": "eighty"}, headers=ORIGIN)
        fields = {e["field"] for e in _validate(client, "address").json()["fields"]}
        assert fields == {"listen_port"}

    def test_the_account_step_applies_the_password_policy(self, unlocked):
        _app, client = unlocked
        client.put(
            "/istota/api/setup/draft",
            json={"user_id": "ada", "user_email": "ada@example.com", "password": "short"},
            headers=ORIGIN,
        )
        errors = _validate(client, "account").json()["fields"]
        assert [e["field"] for e in errors] == ["password"]
        assert "short" not in json.dumps(errors)

        client.put("/istota/api/setup/draft", json={"password": "ada@example.com"}, headers=ORIGIN)
        assert _validate(client, "account").json()["ok"] is False

        client.put("/istota/api/setup/draft", json={"password": "a long enough passphrase"}, headers=ORIGIN)
        assert _validate(client, "account").json() == {"ok": True, "fields": []}

    def test_no_password_is_an_account_error(self, unlocked):
        _app, client = unlocked
        assert [e["field"] for e in _validate(client, "account").json()["fields"]] == ["password"]

    def test_review_reports_every_step(self, unlocked):
        _app, client = unlocked
        client.put(
            "/istota/api/setup/draft",
            json={"timezone": "Mars/Olympus", "brain_kind": "native"},
            headers=ORIGIN,
        )
        fields = {e["field"] for e in _validate(client, "review").json()["fields"]}
        assert {"user_id", "timezone", "native_model", "native_api_key", "password"} <= fields

    def test_every_answer_field_belongs_to_a_step(self):
        stepped = {f for fields in setup_app.SETUP_STEPS.values() for f in fields}
        assert set(setup_wizard.WEB_SETUP_FIELDS) | {"password"} <= stepped

    def test_an_unknown_step_is_400(self, unlocked):
        _app, client = unlocked
        assert _validate(client, "nope").status_code == 400


class TestOnlyTheSetupSurface:
    @pytest.mark.parametrize(
        "path",
        [
            "/istota/api/me",
            "/istota/api/auth/login",
            "/istota/api/setup/nope",
            "/istota/login",
            "/webhooks/location/overland",
            "/api/me",
        ],
    )
    def test_anything_else_is_404(self, unlocked, path):
        _app, client = unlocked
        assert client.get(path).status_code == 404
        assert client.post(path, headers=ORIGIN).status_code in (404, 405)

    @pytest.mark.parametrize("path", ["/", "/istota", "/istota/"])
    def test_the_roots_go_to_the_setup_page(self, server, path):
        _app, client = server
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/istota/setup"

    def test_the_bundle_is_served_with_the_web_apps_cache_rule(self, dirs, clock, tmp_path, monkeypatch):
        build = tmp_path / "build"
        (build / "_app" / "immutable").mkdir(parents=True)
        (build / "_app" / "immutable" / "x.js").write_text("x")
        (build / "setup").mkdir()
        (build / "setup" / "index.html").write_text("<html>setup</html>")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        _app, client = _make(dirs, clock)

        page = client.get("/istota/setup/")
        assert page.status_code == 200 and "setup" in page.text
        assert page.headers["cache-control"] == "no-cache"
        asset = client.get("/istota/_app/immutable/x.js")
        assert "immutable" in asset.headers["cache-control"]


class TestRefusals:
    def test_an_existing_config_refuses_the_app(self, dirs):
        (dirs.data_dir / "config" / "config.toml").write_text("")
        with pytest.raises(setup_wizard.SetupError, match="already set up"):
            setup_app.create_setup_app(
                data_dir=dirs.data_dir, vm_dir=dirs.vm_dir, code_path=dirs.code_path, local=False,
            )

    def test_a_missing_code_file_refuses_the_app(self, dirs):
        dirs.code_path.unlink()
        with pytest.raises(setup_wizard.SetupError, match="setup code"):
            setup_app.create_setup_app(
                data_dir=dirs.data_dir, vm_dir=dirs.vm_dir, code_path=dirs.code_path, local=False,
            )

    def _cli(self, monkeypatch, capsys, argv):
        from istota import cli

        served = []
        monkeypatch.setattr(setup_app, "_run_server", lambda *a, **k: served.append(a) or 0)
        monkeypatch.setattr("sys.argv", ["istota", *argv])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        return excinfo.value.code, capsys.readouterr().err, served

    def test_istota_setup_web_refuses_with_a_config_and_binds_nothing(self, dirs, monkeypatch, capsys):
        (dirs.data_dir / "config" / "config.toml").write_text("")
        code, err, served = self._cli(
            monkeypatch, capsys,
            ["setup", "--web", "--vm-dir", str(dirs.vm_dir), "--data-dir", str(dirs.data_dir)],
        )
        assert code == 1
        assert "already set up" in err
        assert served == []

    @pytest.mark.parametrize(
        "extra,needle",
        [
            (["--force"], "--force"),
            (["--shape", "standalone"], "container"),
        ],
    )
    def test_web_refuses_the_flags_it_cannot_mean(self, dirs, monkeypatch, capsys, extra, needle):
        code, err, served = self._cli(
            monkeypatch, capsys,
            ["setup", "--web", "--vm-dir", str(dirs.vm_dir), "--data-dir", str(dirs.data_dir), *extra],
        )
        assert code == 1
        assert needle in err
        assert served == []

    def test_web_needs_a_vm_dir(self, dirs, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_SETUP_SHAPE", "container")
        code, err, served = self._cli(
            monkeypatch, capsys, ["setup", "--web", "--data-dir", str(dirs.data_dir)],
        )
        assert code == 1 and "--vm-dir" in err and served == []

    def test_local_without_web_is_refused(self, dirs, monkeypatch, capsys):
        code, err, served = self._cli(
            monkeypatch, capsys,
            ["setup", "--local", "--vm-dir", str(dirs.vm_dir), "--data-dir", str(dirs.data_dir)],
        )
        assert code == 1 and "--web" in err and served == []

    def test_a_tls_run_needs_the_certificate(self, dirs, monkeypatch, capsys):
        code, err, served = self._cli(
            monkeypatch, capsys,
            ["setup", "--web", "--vm-dir", str(dirs.vm_dir), "--data-dir", str(dirs.data_dir)],
        )
        assert code == 1 and "cert.pem" in err and served == []

    @pytest.mark.parametrize(
        "local,port,tls",
        [(True, 8080, False), (False, 8443, True)],
    )
    def test_serve_binds_the_port_its_mode_names(self, dirs, monkeypatch, capsys, local, port, tls):
        if tls:
            (dirs.vm_dir / "cert.pem").write_text("cert")
            (dirs.vm_dir / "key.pem").write_text("key")
        argv = ["setup", "--web", "--vm-dir", str(dirs.vm_dir), "--data-dir", str(dirs.data_dir)]
        code, _err, served = self._cli(monkeypatch, capsys, argv + (["--local"] if local else []))
        assert code == 0
        (_app, settings), = served
        assert settings["host"] == "0.0.0.0"
        assert settings["port"] == port
        assert settings["access_log"] is False
        assert bool(settings.get("ssl_certfile")) is tls


# --- connection checks --------------------------------------------------------

from istota.config import config_from_document  # noqa: E402
from istota.webui import setup_checks  # noqa: E402

from testbed.services.model_endpoint import serve_script  # noqa: E402
from tests.support.setup_stubs import (  # noqa: E402
    CalendarServer, FakeNextcloud, RefusingModel, basic,
)

UPSTREAM_TEXT = "upstream-said-something-7c1e"
#: What raw exception text looks like; none of it may reach the browser.
EXCEPTION_MARKERS = ("Errno", "Traceback", "Error(", "errno", "HTTPConnection", "httpx", "HTTP 4", "HTTP 5")


def _check(client, kind, *, headers=ORIGIN):
    return client.post(f"/istota/api/setup/check/{kind}", headers=headers)


def _draft(client, **fields):
    resp = client.put("/istota/api/setup/draft", json=fields, headers=ORIGIN)
    assert resp.status_code == 200, resp.text
    return resp


def _plain(resp, *secrets):
    """The response carries no exception text and none of ``secrets``."""
    for marker in (*EXCEPTION_MARKERS, *secrets):
        assert marker not in resp.text, f"{marker!r} reached the browser: {resp.text}"


def _closed_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


NATIVE = {"brain_kind": "native", "native_model": "model-from-the-draft", "native_api_key": SENTINEL}


class TestTheCheckRoute:
    def test_an_unknown_kind_is_refused(self, unlocked):
        _app, client = unlocked
        resp = _check(client, "ftp")
        assert resp.status_code == 400
        assert resp.json()["error"] == "unknown_check"

    def test_a_subscription_has_no_live_check(self, unlocked):
        _app, client = unlocked
        _draft(client, brain_kind="claude_code", claude_code_oauth_token=SENTINEL)
        resp = _check(client, "model")
        assert resp.status_code == 400
        assert resp.json()["error"] == "no_live_check"
        assert SENTINEL not in resp.text

    def test_a_check_with_nothing_to_test_names_the_fields(self, unlocked):
        _app, client = unlocked
        _draft(client, brain_kind="native", native_model="m")
        resp = _check(client, "model")
        assert resp.status_code == 400
        assert [f["field"] for f in resp.json()["fields"]] == ["native_api_key"]

    def test_checks_are_limited_to_twenty_a_minute(self, unlocked, clock):
        _app, client = unlocked
        _draft(client, ingress="direct", hostname="localhost")
        for _ in range(setup_app.CHECKS_PER_MINUTE):
            assert _check(client, "address").status_code == 200
        assert _check(client, "address").status_code == 429
        clock.now += 61
        assert _check(client, "address").status_code == 200

    def test_a_check_is_logged_without_its_credential(self, unlocked, caplog):
        _app, client = unlocked
        with RefusingModel(status=401, body=UPSTREAM_TEXT).serve() as model:
            _draft(client, **NATIVE, native_base_url=model.base_url)
            with caplog.at_level(logging.INFO, logger="istota.setup_web"):
                _check(client, "model")
        assert "check model ok=False" in caplog.text
        assert SENTINEL not in caplog.text


class TestTheModelCheck:
    def test_it_asks_the_endpoint_the_draft_names(self, unlocked):
        _app, client = unlocked
        with serve_script([{"text": "OK"}]) as endpoint:
            _draft(client, **NATIVE, native_base_url=endpoint.url)
            resp = _check(client, "model")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert [r["model"] for r in endpoint.requests] == ["model-from-the-draft"]

    def test_the_key_is_the_drafts_not_the_environments(self, unlocked, monkeypatch):
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "from-the-environment")
        _app, client = unlocked
        with RefusingModel(status=401).serve() as model:
            _draft(client, **NATIVE, native_base_url=model.base_url)
            _check(client, "model")
        assert model.auth_headers == [f"Bearer {SENTINEL}"]

    @pytest.mark.parametrize(
        "status,needle",
        [(401, "API key was not accepted"), (403, "API key was not accepted"),
         (404, "model-from-the-draft"), (429, "Try again in a minute"), (500, "Try again in a minute")],
    )
    def test_a_refusal_is_a_plain_message(self, unlocked, status, needle):
        _app, client = unlocked
        with RefusingModel(status=status, body=UPSTREAM_TEXT).serve() as model:
            _draft(client, **NATIVE, native_base_url=model.base_url)
            resp = _check(client, "model")
        body = resp.json()
        assert resp.status_code == 200 and body["ok"] is False
        assert needle in body["message"]
        _plain(resp, UPSTREAM_TEXT, SENTINEL)

    def test_a_closed_port_cannot_be_reached(self, unlocked):
        # The provider reports a refused connection as "All connection attempts
        # failed" with nothing to tell it from any other, so the message is the
        # general one.
        _app, client = unlocked
        _draft(client, **NATIVE, native_base_url=f"http://127.0.0.1:{_closed_port()}/v1")
        resp = _check(client, "model")
        assert resp.json() == {"ok": False, "warning": False, "message": "Couldn't reach 127.0.0.1."}

    def test_a_name_that_does_not_resolve_says_so(self, unlocked):
        _app, client = unlocked
        _draft(client, **NATIVE, native_base_url="http://models.invalid/v1")
        resp = _check(client, "model")
        assert resp.json()["message"] == "Couldn't reach models.invalid: the name doesn't resolve."
        _plain(resp, SENTINEL)

    def test_a_slow_endpoint_meets_the_deadline(self, dirs, clock):
        _app, client = _make(dirs, clock, check_seconds=0.3)
        assert _unlock(client).status_code == 200
        with RefusingModel(status=200, delay=1.0).serve() as model:
            _draft(client, **NATIVE, native_base_url=model.base_url)
            resp = _check(client, "model")
        assert resp.json()["ok"] is False
        assert "didn't answer within" in resp.json()["message"]


NC_USER, NC_PASSWORD = "istota", "nextcloud-app-password-5d2b"


def _nextcloud_draft(client, url, **overrides):
    fields = {
        "storage": "nextcloud", "nextcloud_url": url, "nextcloud_username": NC_USER,
        "nextcloud_app_password": NC_PASSWORD, "user_id": "alice",
    }
    fields.update(overrides)
    _draft(client, **fields)


def _lines(resp):
    return {line["key"]: line for line in resp.json()["lines"]}


class TestTheNextcloudCheck:
    def test_a_good_setup_passes_every_line(self, unlocked):
        _app, client = unlocked
        with FakeNextcloud(username=NC_USER, password=NC_PASSWORD, sharees=("alice",)).serve() as nc:
            _nextcloud_draft(client, nc.url)
            resp = _check(client, "nextcloud")
        body = resp.json()
        assert resp.status_code == 200 and body["ok"] is True
        assert [line["key"] for line in body["lines"]] == list(setup_checks.NEXTCLOUD_LINES)
        assert all(line["ok"] and not line["warning"] for line in body["lines"])
        assert body["talk_available"] is True
        assert set(nc.auth_headers) == {basic(NC_USER, NC_PASSWORD)}
        assert nc.calls_matching("PROPFIND", f"/remote.php/dav/files/{NC_USER}/")
        assert nc.calls_matching("GET", "/sharees")[0].query["search"] == ["alice"]
        _plain(resp, NC_PASSWORD)

    def test_a_wrong_app_password_fails_the_sign_in_line(self, unlocked):
        _app, client = unlocked
        with FakeNextcloud(username=NC_USER, password="the-real-one", error_body=UPSTREAM_TEXT).serve() as nc:
            _nextcloud_draft(client, nc.url)
            resp = _check(client, "nextcloud")
        lines = _lines(resp)
        assert resp.json()["ok"] is False
        assert lines["server"]["ok"] is True
        assert lines["signin"]["ok"] is False
        assert "weren't accepted" in lines["signin"]["message"]
        assert all(not lines[key]["ok"] for key in ("admin", "files", "talk", "user"))
        _plain(resp, UPSTREAM_TEXT, NC_PASSWORD)

    def test_warnings_do_not_fail_the_check(self, unlocked):
        _app, client = unlocked
        with FakeNextcloud(username=NC_USER, password=NC_PASSWORD, groups=("admin",), talk=False).serve() as nc:
            _nextcloud_draft(client, nc.url)
            resp = _check(client, "nextcloud")
        body = resp.json()
        lines = _lines(resp)
        assert body["ok"] is True
        assert body["talk_available"] is False
        for key in ("admin", "talk", "user"):
            assert lines[key]["ok"] is True and lines[key]["warning"] is True, key
        assert "administrator" in lines["admin"]["message"]
        assert "web app only" in lines["talk"]["message"]
        assert "alice" in lines["user"]["message"]

    def test_unreachable_files_fail_their_line(self, unlocked):
        _app, client = unlocked
        with FakeNextcloud(
            username=NC_USER, password=NC_PASSWORD, files_status=403, error_body=UPSTREAM_TEXT,
        ).serve() as nc:
            _nextcloud_draft(client, nc.url)
            resp = _check(client, "nextcloud")
        assert resp.json()["ok"] is False
        assert _lines(resp)["files"]["ok"] is False
        assert "couldn't open the account's files" in _lines(resp)["files"]["message"]
        _plain(resp, UPSTREAM_TEXT)

    def test_a_server_it_cannot_reach_points_at_the_internal_address(self, unlocked):
        _app, client = unlocked
        _nextcloud_draft(client, f"http://127.0.0.1:{_closed_port()}")
        resp = _check(client, "nextcloud")
        server = _lines(resp)["server"]
        assert server["ok"] is False
        assert "the connection was refused" in server["message"]
        assert "Address this server uses to reach Nextcloud" in server["message"]
        _plain(resp, NC_PASSWORD)

    def test_local_storage_has_no_nextcloud_to_check(self, unlocked):
        _app, client = unlocked
        _draft(client, storage="local")
        assert _check(client, "nextcloud").status_code == 400


class TestTheCalendarCheck:
    def test_the_right_password_signs_in(self, unlocked):
        _app, client = unlocked
        with CalendarServer(username="ada", password=SENTINEL).serve() as cal:
            _draft(client, caldav_url=f"{cal.url}/dav/", caldav_username="ada", caldav_password=SENTINEL)
            resp = _check(client, "caldav")
        assert resp.json()["ok"] is True
        assert cal.calls_matching("PROPFIND", "/dav")

    def test_a_wrong_password_is_named(self, unlocked):
        _app, client = unlocked
        with CalendarServer(username="ada", password="other", error_body=UPSTREAM_TEXT).serve() as cal:
            _draft(client, caldav_url=f"{cal.url}/dav/", caldav_username="ada", caldav_password=SENTINEL)
            resp = _check(client, "caldav")
        assert resp.json()["ok"] is False
        assert "weren't accepted" in resp.json()["message"]
        _plain(resp, UPSTREAM_TEXT, SENTINEL)


class TestTheEmailCheckRoute:
    def test_a_mail_server_that_does_not_resolve(self, unlocked):
        _app, client = unlocked
        _draft(client, email_enabled=True, imap_host="mail.invalid", imap_user="ada", imap_password=SENTINEL)
        resp = _check(client, "email")
        assert resp.json()["ok"] is False
        assert "mail.invalid: the name doesn't resolve" in resp.json()["message"]
        _plain(resp, SENTINEL)

    def test_it_needs_a_password_to_test(self, unlocked):
        _app, client = unlocked
        _draft(client, email_enabled=True, imap_host="mail.example.com", imap_user="ada")
        resp = _check(client, "email")
        assert resp.status_code == 400
        assert [f["field"] for f in resp.json()["fields"]] == ["imap_password"]


class TestTheAddressCheck:
    def _at(self, client, browser_host):
        return client.post(
            "/istota/api/setup/check/address",
            headers={"host": browser_host, "origin": f"https://{browser_host}"},
        )

    def test_a_name_pointing_here_passes(self, unlocked):
        _app, client = unlocked
        _draft(client, ingress="direct", hostname="localhost")
        body = self._at(client, "127.0.0.1").json()
        assert body["ok"] is True and body["warning"] is False

    def test_a_name_pointing_elsewhere_is_a_warning(self, unlocked):
        _app, client = unlocked
        _draft(client, ingress="direct", hostname="localhost")
        body = self._at(client, "203.0.113.5").json()
        assert body["ok"] is False and body["warning"] is True
        assert "203.0.113.5" in body["message"]

    def test_a_name_that_does_not_resolve_yet_is_a_warning(self, unlocked):
        _app, client = unlocked
        _draft(client, ingress="direct", hostname="istota.invalid")
        body = self._at(client, "203.0.113.5").json()
        assert body["ok"] is False and body["warning"] is True
        assert "doesn't resolve" in body["message"]

    def test_only_on_this_computer_has_nothing_to_check(self, unlocked):
        _app, client = unlocked
        _draft(client, ingress="local")
        assert _check(client, "address").status_code == 400


def _email_config(imap_port, smtp_port, *, password=SENTINEL):
    return config_from_document({"email": {
        "enabled": True, "imap_host": "127.0.0.1", "imap_port": imap_port, "imap_user": "ada",
        "imap_password": password, "smtp_host": "127.0.0.1", "smtp_port": smtp_port,
    }})


@pytest.fixture
def tls_pair(tmp_path, monkeypatch):
    from testbed.certs import generate_self_signed

    cert, key = generate_self_signed(tmp_path / "tls", sans=("localhost", "127.0.0.1"))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    return cert, key


class TestTheEmailCheck:
    def test_it_signs_in_to_both_and_sends_nothing(self, tls_pair):
        from tests.support.mail_stub import ImapStub, SmtpStub

        cert, key = tls_pair
        with ImapStub(cert=cert, key=key, username="ada", password=SENTINEL) as imap, \
                SmtpStub(cert=cert, key=key, username="ada", password=SENTINEL) as smtp:
            result = setup_checks.check_email(_email_config(imap.port, smtp.port), seconds=5)
        assert result.ok is True, result.to_json()
        assert imap.logins == [("ada", True)] and smtp.logins == [("ada", True)]
        assert "STARTTLS" in imap.commands and "STARTTLS" in smtp.commands
        assert "MAIL" not in smtp.commands and "DATA" not in smtp.commands

    def test_a_wrong_password_is_named_for_incoming_mail(self, tls_pair):
        from tests.support.mail_stub import ImapStub, SmtpStub

        cert, key = tls_pair
        with ImapStub(cert=cert, key=key, username="ada", password="other") as imap, \
                SmtpStub(cert=cert, key=key, username="ada", password=SENTINEL) as smtp:
            result = setup_checks.check_email(_email_config(imap.port, smtp.port), seconds=5)
        body = json.dumps(result.to_json())
        assert result.ok is False
        assert "mail server didn't accept that username and password" in result.message
        assert SENTINEL not in body and "AUTHENTICATIONFAILED" not in body

    def test_a_wrong_password_is_named_for_outgoing_mail(self, tls_pair):
        from tests.support.mail_stub import ImapStub, SmtpStub

        cert, key = tls_pair
        with ImapStub(cert=cert, key=key, username="ada", password=SENTINEL) as imap, \
                SmtpStub(cert=cert, key=key, username="ada", password="other") as smtp:
            result = setup_checks.check_email(_email_config(imap.port, smtp.port), seconds=5)
        assert result.ok is False
        assert "outgoing mail server didn't accept" in result.message

    def test_an_outgoing_server_without_tls_is_refused(self, tls_pair):
        from tests.support.mail_stub import ImapStub, SmtpStub

        cert, key = tls_pair
        with ImapStub(cert=cert, key=key, username="ada", password=SENTINEL) as imap, \
                SmtpStub(cert=cert, key=key, username="ada", password=SENTINEL, starttls=False) as smtp:
            result = setup_checks.check_email(_email_config(imap.port, smtp.port), seconds=5)
        assert result.ok is False
        assert "secure connection" in result.message
        assert smtp.logins == []


class TestConfigFromDocument:
    def test_it_maps_what_the_setup_would_write(self):
        a = setup_wizard.ContainerAnswers(
            user_id="ada", brain_kind="native", native_model="m", native_api_key=SENTINEL,
            nextcloud_url="https://cloud.example.com", nextcloud_app_password="pw",
        )
        config = config_from_document(setup_wizard.container_config_document(a, inline_credentials=True))
        assert config.brain.native.api_key == SENTINEL
        assert config.brain.native.model == "m"
        assert config.nextcloud.url == "https://cloud.example.com"
        assert config.nextcloud.app_password == "pw"

    def test_it_leaves_the_callers_document_alone(self):
        doc = {"whatsapp": {"enabled": True, "phone_number_id": "123"}}
        before = json.dumps(doc, sort_keys=True)
        config_from_document(doc)
        assert json.dumps(doc, sort_keys=True) == before


# --- finish -------------------------------------------------------------------

from istota import user_profiles  # noqa: E402
from istota.config import Config  # noqa: E402
from istota import apply  # noqa: E402
from istota.lib import rclone_conf  # noqa: E402
from istota.webui import auth  # noqa: E402

PASSWORD = "a long enough passphrase"
#: A draft that finishes: local storage, the native brain, no connections.
READY = {
    "bot_name": "Ada's assistant", "display_name": "Ada Lovelace", "user_id": "ada",
    "user_email": "Ada@Example.com", "timezone": "Europe/Berlin",
    "brain_kind": "native", "native_model": "model-from-the-draft", "native_api_key": SENTINEL,
    "ingress": "local", "hostname": "localhost:8080", "profiles": ["browser"],
    "password": PASSWORD,
}


def _finish(client, headers=ORIGIN):
    return client.post("/istota/api/setup/finish", headers=headers)


def _db_path(dirs):
    return dirs.data_dir / "db" / "istota.db"


def _written(root):
    """Every file under ``root`` with its bytes, the setup's own state left out."""
    skip = {"setup-code", "setup-done"}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in skip and "db" not in path.relative_to(root).parts
    }


@pytest.fixture
def pinned(monkeypatch):
    """The two random choices the writer makes, and managed.json's clock,
    fixed so two runs compare."""
    monkeypatch.setattr(setup_wizard.secrets, "token_hex", lambda n: "k" * (2 * n))
    monkeypatch.setattr(rclone_conf, "_new_iv", lambda: b"i" * 16)
    monkeypatch.setattr(apply, "_utc_now", lambda: datetime(2026, 10, 12, tzinfo=timezone.utc))


class TestFinish:
    def test_it_writes_what_the_terminal_writer_writes(self, unlocked, dirs, tmp_path, pinned):
        _app, client = unlocked
        draft = {
            **READY, "storage": "nextcloud", "nextcloud_url": "https://cloud.example.com",
            "nextcloud_username": "istota", "nextcloud_app_password": "nc-app-password",
            "user_id": "alice", "talk_enabled": True, "ingress": "direct",
            "hostname": "istota.example.com",
        }
        _draft(client, **draft)
        resp = _finish(client)
        assert resp.status_code == 200, resp.text

        terminal = tmp_path / "terminal"
        (terminal / "data" / "config").mkdir(parents=True)
        (terminal / "vm").mkdir()
        answers = setup_wizard.answers_from_payload({k: v for k, v in draft.items() if k != "password"})
        setup_wizard.write_container_setup(
            answers, data_dir=terminal / "data", vm_dir=terminal / "vm", out=lambda _line: None,
        )
        assert _written(dirs.data_dir) == _written(terminal / "data")
        assert _written(dirs.vm_dir) == _written(terminal / "vm")
        assert (dirs.vm_dir / "rclone.conf").exists()
        managed = json.loads((dirs.data_dir / "config" / "managed.json").read_text())
        assert {name: (e["managed"], e["source"]) for name, e in managed.items()} == {
            "config": (False, "setup"), "secrets": (False, "setup"), "admins": (False, "setup"),
        }

    def test_nextcloud_with_a_local_workspace_writes_no_mount(self, unlocked, dirs):
        _app, client = unlocked
        _draft(client, **{
            **READY, "storage": "nextcloud", "workspace": "local",
            "nextcloud_url": "https://cloud.example.com", "nextcloud_username": "istota",
            "nextcloud_app_password": "nc-app-password", "user_id": "alice", "talk_enabled": True,
        })
        resp = _finish(client)
        assert resp.status_code == 200, resp.text

        host_env = (dirs.vm_dir / "host.env").read_text()
        assert "STORAGE=local" in host_env.splitlines()
        assert not (dirs.vm_dir / "rclone.conf").exists()
        config = (dirs.data_dir / "config" / "config.toml").read_text()
        assert "https://cloud.example.com" in config
        assert "nextcloud_mount_path" not in config

    def test_the_first_admin_can_sign_in(self, unlocked, dirs):
        _app, client = unlocked
        _draft(client, **READY)
        resp = _finish(client)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"login_url": "http://localhost:8080/istota/login", "email": "ada@example.com"}

        db_path = _db_path(dirs)
        profile = user_profiles.get_profile(db_path, "ada")
        assert profile.display_name == "Ada Lovelace"
        assert profile.timezone == "Europe/Berlin"
        assert profile.email_addresses == ["Ada@Example.com"]
        policy = auth.policy_from_config(Config())
        status, identity = auth.authenticate(db_path, policy, "ada@example.com", PASSWORD, ip=None)
        assert status == "ok" and identity.user_id == "ada"
        assert auth.authenticate(db_path, policy, "ada@example.com", "not the password", ip=None)[0] == "bad"
        # The password is a hash in the identity row and nowhere else.
        for path in [*dirs.data_dir.rglob("*"), *dirs.vm_dir.rglob("*")]:
            if path.is_file() and "db" not in path.relative_to(dirs.data_dir.parent).parts:
                assert PASSWORD.encode() not in path.read_bytes(), path

    def test_an_internet_address_is_signed_in_over_https(self, unlocked):
        _app, client = unlocked
        _draft(client, **{**READY, "ingress": "direct", "hostname": "istota.example.com"})
        assert _finish(client).json()["login_url"] == "https://istota.example.com/istota/login"

    def test_the_marker_is_written_and_the_server_stops(self, unlocked, dirs):
        app, client = unlocked
        stopped = []
        app.state.setup.on_exit = stopped.append
        _draft(client, **READY)
        assert _finish(client).status_code == 200
        marker = dirs.vm_dir / "setup-done"
        assert marker.read_bytes() == b""
        assert marker.stat().st_mode & 0o777 == 0o400
        assert stopped == [0] and app.state.setup.exit_code == 0

    def test_after_finishing_the_draft_is_gone_and_finish_is_refused(self, unlocked):
        _app, client = unlocked
        _draft(client, **READY)
        assert _finish(client).status_code == 200
        assert client.get("/istota/api/setup/state").json()["phase"] == "finishing"
        assert client.get("/istota/api/setup/draft").json() == {"draft": {}}
        assert _finish(client).status_code == 409

    def test_every_error_comes_back_with_its_step_and_nothing_is_written(self, unlocked, dirs):
        app, client = unlocked
        _draft(client, **{**READY, "timezone": "Mars/Olympus", "password": "short"}, storage="nextcloud")
        resp = _finish(client)
        assert resp.status_code == 400
        body = resp.json()
        steps = {(e["field"], e["step"]) for e in body["fields"]}
        assert steps == {("timezone", "welcome"), ("password", "account"), ("nextcloud_url", "nextcloud")}
        assert body["step"] == "welcome"
        assert "short" not in resp.text and SENTINEL not in resp.text
        assert not (dirs.data_dir / "config" / "config.toml").exists()
        assert not _db_path(dirs).exists()
        assert not (dirs.vm_dir / "setup-done").exists()
        assert app.state.setup.exit_code is None

    def test_with_nextcloud_the_username_is_the_nextcloud_screens(self, unlocked):
        _app, client = unlocked
        _draft(client, **{**READY, "user_id": ""}, storage="nextcloud", nextcloud_url="https://cloud.example.com")
        fields = _finish(client).json()["fields"]
        assert [(e["field"], e["step"]) for e in fields] == [("user_id", "nextcloud")]

    @pytest.mark.parametrize("field,step", [("user_email", "welcome"), ("password", "account")])
    def test_the_login_needs_an_email_and_a_password(self, unlocked, field, step):
        _app, client = unlocked
        _draft(client, **{k: v for k, v in READY.items() if k != field})
        resp = _finish(client)
        assert resp.status_code == 400
        assert [(e["field"], e["step"]) for e in resp.json()["fields"]] == [(field, step)]

    def test_a_failed_write_is_reported_and_a_retry_finishes(self, unlocked, dirs):
        app, client = unlocked
        _draft(client, **READY)
        # A file where the database directory goes: fails for root too.
        (dirs.data_dir / "db").write_text("in the way")
        resp = _finish(client)
        assert resp.status_code == 500
        assert resp.json()["error"] == "write_failed"
        assert resp.json()["step"] == "account"
        assert resp.json()["message"].startswith("Couldn't create your account:")
        assert "Errno" not in resp.text
        assert app.state.setup.exit_code is None
        assert not (dirs.vm_dir / "setup-done").exists()
        key = (dirs.data_dir / ".secret_key").read_bytes()
        admins = (dirs.data_dir / "config" / "admins").read_bytes()

        (dirs.data_dir / "db").unlink()
        assert _finish(client).status_code == 200
        assert (dirs.data_dir / ".secret_key").read_bytes() == key
        assert (dirs.data_dir / "config" / "admins").read_bytes() == admins
        status, _identity = auth.authenticate(
            _db_path(dirs), auth.policy_from_config(Config()), "ada@example.com", PASSWORD, ip=None,
        )
        assert status == "ok"

    def test_a_retry_over_a_finished_account_is_harmless(self, unlocked, dirs):
        _app, client = unlocked
        _draft(client, **READY)
        # The account step runs and the marker does not: the retry runs both again.
        (dirs.vm_dir / "setup-done").mkdir()
        resp = _finish(client)
        assert resp.status_code == 500 and resp.json()["step"] == "marker"
        (dirs.vm_dir / "setup-done").rmdir()
        assert _finish(client).status_code == 200
        assert list(user_profiles.list_profiles(_db_path(dirs))) == ["ada"]
        status, _identity = auth.authenticate(
            _db_path(dirs), auth.policy_from_config(Config()), "ada@example.com", PASSWORD, ip=None,
        )
        assert status == "ok"

    def test_a_settings_write_failure_names_the_step(self, unlocked, dirs):
        _app, client = unlocked
        _draft(client, **READY)
        (dirs.data_dir / "config" / "config.toml").mkdir()
        resp = _finish(client)
        assert resp.status_code == 500
        assert resp.json()["step"] == "settings"
        assert resp.json()["message"].startswith("Couldn't save the settings:")
        assert "Errno" not in resp.text


class TestValidateWithAPayloadErrorElsewhere:
    def test_an_earlier_step_still_reports_its_own_errors(self, unlocked):
        # storage=nextcloud with no address cannot become answers; welcome's
        # own errors are still found.
        _app, client = unlocked
        _draft(client, timezone="Mars/Olympus", user_id="ada", user_email="ada@example.com", storage="nextcloud")
        assert {e["field"] for e in _validate(client, "welcome").json()["fields"]} == {"timezone"}
        assert {e["field"] for e in _validate(client, "nextcloud").json()["fields"]} == {"nextcloud_url"}

    def test_a_wrong_type_does_not_hide_the_rest(self, unlocked):
        _app, client = unlocked
        _draft(
            client, listen_port="eighty", ingress="proxied", hostname="istota.example.com",
            user_id="ada", user_email="ada@example.com",
        )
        fields = {e["field"] for e in _validate(client, "address").json()["fields"]}
        assert fields == {"listen_port", "upstream_proxy", "listen_addr"}

    def test_welcome_needs_an_email(self, unlocked):
        _app, client = unlocked
        _draft(client, user_id="ada")
        assert [e["field"] for e in _validate(client, "welcome").json()["fields"]] == ["user_email"]


#: One distinct value per credential field, so a leak names its field.
CREDENTIAL_SENTINELS = {name: f"sentinel-{name}-0b7e" for name in sorted(setup_app.CREDENTIAL_FIELDS)}


class TestNothingSecretIsLogged:
    def test_no_credential_reaches_a_log_line_over_the_whole_flow(self, server, dirs, caplog):
        _app, client = server
        s = CREDENTIAL_SENTINELS
        with caplog.at_level(logging.DEBUG):
            assert _unlock(client).status_code == 200
            # A provider that quotes the key back is the worst case.
            with RefusingModel(status=401, body=f"Incorrect API key: {s['native_api_key']}").serve() as model, \
                    FakeNextcloud(username=NC_USER, password=s["nextcloud_app_password"]).serve() as nc, \
                    CalendarServer(username="ada", password="other", error_body=s["caldav_password"]).serve() as cal:
                _draft(
                    client, **{**READY, "password": s["password"], "native_api_key": s["native_api_key"]},
                    native_base_url=model.base_url,
                    storage="nextcloud", nextcloud_url=nc.url, nextcloud_username=NC_USER,
                    nextcloud_app_password=s["nextcloud_app_password"],
                    email_enabled=True, imap_host="mail.invalid", imap_user="ada",
                    imap_password=s["imap_password"],
                    caldav_url=f"{cal.url}/dav/", caldav_username="ada", caldav_password=s["caldav_password"],
                    claude_code_oauth_token=s["claude_code_oauth_token"],
                )
                for step in setup_app.SETUP_STEPS:
                    _validate(client, step)
                assert _check(client, "model").json()["ok"] is False
                assert _check(client, "nextcloud").status_code == 200
                assert _check(client, "email").json()["ok"] is False
                _draft(client, storage="local")
                assert _check(client, "caldav").json()["ok"] is False
                # Finish with the subscription and Nextcloud, so every
                # credential the draft holds goes through a writer.
                _draft(client, storage="nextcloud", brain_kind="claude_code")
                shown = client.get("/istota/api/setup/draft").text
                resp = _finish(client)
                assert resp.status_code == 200, resp.text

        written = b"".join(p.read_bytes() for p in (dirs.vm_dir / "secrets").iterdir())
        assert s["claude_code_oauth_token"].encode() in written
        assert s["nextcloud_app_password"].encode() in written
        for name, value in s.items():
            assert value not in caplog.text, f"{name} reached the log"
            assert value not in shown and value not in resp.text
        assert "7KQ2M" not in caplog.text
        assert "setup: finish" in caplog.text


class TestTheModelDefaults:
    def test_state_names_each_api_key_services_address_and_model(self, server):
        _app, client = server
        models = client.get("/istota/api/setup/state").json()["defaults"]["models"]
        assert models == {
            "anthropic": {
                "base_url": setup_wizard.DEFAULT_ANTHROPIC_BASE_URL,
                "model": setup_wizard.DEFAULT_ANTHROPIC_MODEL,
            },
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "model": setup_wizard.DEFAULT_OPENROUTER_MODEL,
            },
        }

    def test_the_openrouter_default_is_the_same_model_by_its_openrouter_name(self):
        # OpenRouter writes the version with a dot: claude-sonnet-4-6 is
        # anthropic/claude-sonnet-4.6. Bumping one default without the other
        # turns this red.
        expected = "anthropic/" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", setup_wizard.DEFAULT_ANTHROPIC_MODEL)
        assert setup_wizard.DEFAULT_OPENROUTER_MODEL == expected

    def test_the_terminal_wizard_offers_the_same_anthropic_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)
        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return "carol" if "user id" in prompt else ""

        args = types.SimpleNamespace(
            shape="container", vm_dir=str(tmp_path / "vm"), data_dir=str(tmp_path / "data"),
            yes=False, force=False, user=None, display_name=None, timezone=None,
            user_email=None, bot_name=None, hostname=None, ingress=None,
            upstream_proxy=None, listen_addr=None, listen_port=None, tls_cert_source=None,
            nextcloud_url=None, nextcloud_public_url=None, nextcloud_user=None,
            nextcloud_dav_prefix=None, no_nextcloud_auto_share=False, no_talk=False,
            oauth_client_id=None, no_email_login=False, brain="native",
            native_base_url=None, native_model=None, email=False,
            imap_host=None, imap_user=None, smtp_host=None, bot_email=None,
            caldav_url=None, caldav_username=None, location=False, developer=False,
            profile=None, no_money=False, no_health=False, no_feeds=False,
            no_briefings=False,
        )
        a = setup_wizard.collect_container_answers(
            args, input_fn=answer, out=lambda *_: None, getpass_fn=lambda _prompt: "a-native-key",
        )
        assert a.native_model == setup_wizard.DEFAULT_ANTHROPIC_MODEL
        assert any(setup_wizard.DEFAULT_ANTHROPIC_MODEL in p for p in prompts)


class _Catalog:
    """`fetch_openrouter_catalog` as the setup server sees it, counting calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def __call__(self, base_url, **_kwargs):
        self.calls.append(base_url)
        if self.fail:
            raise OSError(f"[Errno 101] Network is unreachable: {UPSTREAM_TEXT}")
        return {"z-ai/glm-5": object(), "anthropic/claude-sonnet-4.6": object()}


@pytest.fixture
def catalog(monkeypatch):
    from istota.llm import openrouter_catalog

    fake = _Catalog()
    monkeypatch.setattr(openrouter_catalog, "fetch_openrouter_catalog", fake)
    return fake


class TestTheModelList:
    def test_it_lists_openrouters_model_ids_and_asks_once(self, unlocked, catalog):
        _app, client = unlocked
        first = client.get("/istota/api/setup/models")
        assert first.status_code == 200
        assert first.json() == {"models": ["anthropic/claude-sonnet-4.6", "z-ai/glm-5"]}
        assert client.get("/istota/api/setup/models").json() == first.json()
        assert catalog.calls == [setup_wizard.DEFAULT_OPENROUTER_BASE_URL]

    def test_a_failed_fetch_is_an_empty_list_and_is_not_repeated(self, unlocked, catalog):
        _app, client = unlocked
        catalog.fail = True
        resp = client.get("/istota/api/setup/models")
        assert resp.status_code == 200
        assert resp.json() == {"models": []}
        _plain(resp, UPSTREAM_TEXT)
        assert client.get("/istota/api/setup/models").json() == {"models": []}
        assert len(catalog.calls) == 1

    def test_it_needs_the_session(self, server, catalog):
        _app, client = server
        assert client.get("/istota/api/setup/models").status_code == 401
        assert catalog.calls == []


class TestTheSubscriptionToken:
    def test_the_web_setup_asks_for_the_token(self, unlocked):
        _app, client = unlocked
        _draft(client, brain_kind="claude_code")
        fields = _validate(client, "model").json()["fields"]
        assert [e["field"] for e in fields] == ["claude_code_oauth_token"]
        _draft(client, claude_code_oauth_token=SENTINEL)
        assert _validate(client, "model").json() == {"ok": True, "fields": []}


class TestTheUsername:
    @pytest.mark.parametrize("user_id", ["ada", "ada.lovelace", "a-1_b", "0" * 32])
    def test_a_username_the_login_allows_passes(self, unlocked, user_id):
        _app, client = unlocked
        _draft(client, user_id=user_id, user_email="ada@example.com")
        assert _validate(client, "welcome").json() == {"ok": True, "fields": []}

    @pytest.mark.parametrize("user_id", ["Ada", "ada lovelace", "-ada", ".ada", "ada!", "a" * 33])
    def test_a_username_the_login_refuses_is_a_welcome_error(self, unlocked, user_id):
        _app, client = unlocked
        _draft(client, user_id=user_id, user_email="ada@example.com")
        fields = _validate(client, "welcome").json()["fields"]
        assert [e["field"] for e in fields] == ["user_id"]
        assert user_id not in fields[0]["message"]

    def test_finish_refuses_it_too(self, unlocked, dirs):
        _app, client = unlocked
        _draft(client, **{**READY, "user_id": "Ada"})
        resp = _finish(client)
        assert resp.status_code == 400
        assert [(e["field"], e["step"]) for e in resp.json()["fields"]] == [("user_id", "welcome")]
        assert not (dirs.data_dir / "config" / "config.toml").exists()

    def test_the_terminal_rule_is_unchanged(self):
        a = setup_wizard.answers_from_payload({"user_id": "Ada Lovelace"})
        assert [e for e in setup_wizard.validate_container_answers(a) if e.field == "user_id"] == []

    def test_the_local_rule_is_not_the_nextcloud_screens(self, unlocked):
        # With Nextcloud the username is the user's Nextcloud id, which the
        # Nextcloud screen checks by its own rule (stage 8).
        _app, client = unlocked
        _draft(
            client, user_id="Ada", user_email="ada@example.com",
            storage="nextcloud", nextcloud_url="https://cloud.example.com",
        )
        assert _validate(client, "welcome").json() == {"ok": True, "fields": []}


class TestARetryWithAnotherUsername:
    def _fail_once(self, client, dirs):
        _draft(client, **READY)
        (dirs.data_dir / "db").write_text("in the way")
        assert _finish(client).status_code == 500
        (dirs.data_dir / "db").unlink()

    def test_it_is_refused_on_the_username(self, unlocked, dirs):
        _app, client = unlocked
        self._fail_once(client, dirs)
        admins = (dirs.data_dir / "config" / "admins").read_bytes()

        _draft(client, user_id="grace")
        resp = _finish(client)
        assert resp.status_code == 400
        body = resp.json()
        assert body["step"] == "welcome"
        assert [e["field"] for e in body["fields"]] == ["user_id"]
        assert "“ada”" in body["fields"][0]["message"]
        assert (dirs.data_dir / "config" / "admins").read_bytes() == admins
        assert not _db_path(dirs).exists()

        # The welcome screen says so as well, so Next stops there.
        assert [e["field"] for e in _validate(client, "welcome").json()["fields"]] == ["user_id"]

    def test_keeping_the_first_username_finishes(self, unlocked, dirs):
        _app, client = unlocked
        self._fail_once(client, dirs)
        _draft(client, user_id="grace")
        assert _finish(client).status_code == 400
        _draft(client, user_id="ada")
        assert _finish(client).status_code == 200
        assert list(user_profiles.list_profiles(_db_path(dirs))) == ["ada"]


# --- stage 8: the Nextcloud username, the web address, plain wording ----------

NEXTCLOUD = {"storage": "nextcloud", "nextcloud_url": "https://cloud.example.com"}


def _fields(resp):
    return [(e["field"], e["message"]) for e in resp.json()["fields"]]


class TestTheNextcloudUsername:
    # Ids a Nextcloud hands out that the native login's pattern refuses.
    @pytest.mark.parametrize(
        "user_id", ["Ada", "first.last", "ada lovelace", "ada@example.com", "o'brien", "CORP\\ada"],
    )
    def test_an_id_istota_can_hold_passes(self, unlocked, user_id):
        _app, client = unlocked
        _draft(client, user_id=user_id, user_email="ada@example.com", **NEXTCLOUD)
        assert _fields(_validate(client, "nextcloud")) == []

    # `is_scopable_user_id`, the rule every path and task under a user id
    # applies, and the admins file reading a leading `#` as a comment. Space
    # around an id is not among them: the payload mapping strips it.
    @pytest.mark.parametrize("user_id", [".", "..", "ada/files", "#ada"])
    def test_an_id_istota_cannot_hold_is_a_nextcloud_error(self, unlocked, user_id):
        _app, client = unlocked
        _draft(client, user_id=user_id, user_email="ada@example.com", **NEXTCLOUD)
        fields = _fields(_validate(client, "nextcloud"))
        assert [f for f, _m in fields] == ["user_id"]
        assert "Istota can't use" in fields[0][1]

    def test_an_empty_id_asks_for_the_nextcloud_username(self, unlocked):
        _app, client = unlocked
        _draft(client, user_id="", user_email="ada@example.com", **NEXTCLOUD)
        assert _fields(_validate(client, "nextcloud")) == [("user_id", "Enter your own Nextcloud username.")]

    def test_the_welcome_screen_does_not_report_it(self, unlocked):
        _app, client = unlocked
        _draft(client, user_id="ada/files", user_email="ada@example.com", **NEXTCLOUD)
        assert _validate(client, "welcome").json() == {"ok": True, "fields": []}

    def test_finish_refuses_it_on_the_nextcloud_screen(self, unlocked, dirs):
        _app, client = unlocked
        _draft(client, **{**READY, "user_id": "..", **NEXTCLOUD})
        resp = _finish(client)
        assert resp.status_code == 400
        assert [(e["field"], e["step"]) for e in resp.json()["fields"]] == [("user_id", "nextcloud")]
        assert not (dirs.data_dir / "config" / "config.toml").exists()


class TestTheWebAddress:
    @pytest.mark.parametrize("ingress", ["direct", "proxied"])
    @pytest.mark.parametrize("hostname", ["", "localhost", "localhost:8080", "127.0.0.1"])
    def test_a_public_choice_needs_a_name(self, unlocked, ingress, hostname):
        _app, client = unlocked
        proxy = {"upstream_proxy": "10.0.0.2", "listen_addr": "10.0.0.5"} if ingress == "proxied" else {}
        _draft(client, ingress=ingress, hostname=hostname, **proxy)
        assert [f for f, _m in _fields(_validate(client, "address"))] == ["hostname"]

    def test_a_name_passes(self, unlocked):
        _app, client = unlocked
        _draft(client, ingress="direct", hostname="istota.example.com")
        assert _validate(client, "address").json() == {"ok": True, "fields": []}

    def test_only_on_this_computer_needs_none(self, unlocked):
        _app, client = unlocked
        _draft(client, ingress="local", hostname="localhost:8080")
        assert _validate(client, "address").json() == {"ok": True, "fields": []}


#: The words a web user never sees (the spec's first goal), an option the
#: terminal takes, and the shape of an environment variable's name.
_OPERATOR_WORDS = re.compile(r"\b(ingress|brain|tls|oauth|dav|compose)\b", re.IGNORECASE)
_OPERATOR_SHAPES = re.compile(r"--[a-z]|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")

_PROXIED = {"ingress": "proxied", "hostname": "istota.example.com"}

#: One draft per refusal the shared validator and the payload mapping make,
#: with the field it is refused on.
_REFUSALS = [
    ({"user_id": ""}, "user_id"),
    ({"user_id": "", **NEXTCLOUD}, "user_id"),
    ({"timezone": "Mars/Olympus"}, "timezone"),
    ({"ingress": "sideways"}, "ingress"),
    ({**_PROXIED, "listen_addr": "10.0.0.5"}, "upstream_proxy"),
    ({**_PROXIED, "upstream_proxy": "10.0.0.2", "listen_addr": "0.0.0.0"}, "listen_addr"),
    ({**_PROXIED, "upstream_proxy": "10.0.0.2", "listen_addr": "10.0.0.5", "tls_cert_source": "acme"}, "tls_cert_source"),
    ({"ingress": "direct", "hostname": "istota.example.com", "tls_cert_source": "self-signed"}, "tls_cert_source"),
    ({"listen_port": "eighty"}, "listen_port"),
    ({"storage": "attic"}, "storage"),
    ({"storage": "nextcloud"}, "nextcloud_url"),
    ({**NEXTCLOUD, "workspace": "attic"}, "workspace"),
    ({"talk_enabled": "yes"}, "talk_enabled"),
    ({"brain_kind": "oracle"}, "brain_kind"),
    ({"brain_kind": "native"}, "native_model"),
    ({"brain_kind": "native"}, "native_api_key"),
    ({"caldav_url": "https://cal.example.com/cal/"}, "caldav_password"),
    ({"profiles": ["devbox-x"]}, "profiles"),
    ({"profiles": ["signaling"]}, "profiles"),
    ({"profiles": "browser"}, "profiles"),
]


def _assert_plain(fields):
    for name, message in fields:
        assert not _OPERATOR_WORDS.search(message), (name, message)
        assert not _OPERATOR_SHAPES.search(message), (name, message)


class TestPlainWordsOnTheWeb:
    @pytest.mark.parametrize("draft,field", _REFUSALS)
    def test_a_refusal_reads_plainly(self, unlocked, draft, field):
        _app, client = unlocked
        _draft(client, **{"user_id": "ada", "user_email": "ada@example.com", **draft})
        fields = _fields(_validate(client, "review"))
        assert field in [f for f, _m in fields]
        _assert_plain(fields)

    def test_finish_reads_plainly_too(self, unlocked):
        _app, client = unlocked
        _draft(client, **{**READY, "native_api_key": "", "native_model": ""})
        fields = _fields(_finish(client))
        assert {f for f, _m in fields} == {"native_model", "native_api_key"}
        _assert_plain(fields)

    def test_a_check_refused_on_the_answers_reads_plainly(self, unlocked):
        _app, client = unlocked
        _draft(client, brain_kind="native", native_model="m", native_api_key=SENTINEL, listen_port="eighty")
        resp = _check(client, "model")
        assert resp.status_code == 400
        assert [f for f, _m in _fields(resp)] == ["listen_port"]
        _assert_plain(_fields(resp))

    def test_the_terminal_keeps_its_own_wording(self):
        a = setup_wizard.answers_from_payload({"user_id": "ada", "brain_kind": "native"})
        messages = {e.field: e.message for e in setup_wizard.validate_container_answers(a)}
        assert "--native-model" in messages["native_model"]
        assert "ISTOTA_BRAIN_NATIVE_API_KEY" in messages["native_api_key"]
