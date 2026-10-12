"""The first-run setup server (`istota setup --web`), driven over HTTP.

Each test builds the app with `create_setup_app` against temp directories and
a code file, as `istota-stack` would hand them over, and a clock the test
moves, so the throttle and the idle expiry are driven without waiting.
"""

from __future__ import annotations

import json
import logging
import types

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


def _make(dirs, clock, *, local=False):
    app = setup_app.create_setup_app(
        data_dir=dirs.data_dir, vm_dir=dirs.vm_dir, code_path=dirs.code_path,
        local=local, clock=clock,
    )
    base = "http://testserver" if local else "https://testserver"
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
        resp = _unlock(client, headers={"origin": "http://testserver"})
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
            json={"timezone": "Mars/Olympus", "brain_kind": "native", "user_id": "ada"},
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
        client.put("/istota/api/setup/draft", json={"storage": "nextcloud"}, headers=ORIGIN)
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
