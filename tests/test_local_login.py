"""Local login codes: the Mac app signs its own user in without a password.

A code is a one-use `web_auth_tokens` row with purpose `local_login`, minted by
`istota web login-code` inside the VM, so only someone who can run a command
there (the macOS user who owns the app's Lima home) can mint one. It lives 60
seconds, is consumed in the transaction that checks it, and works only on an
install whose `[web] auth` names `local_code`, which the stack allows only
under `local` ingress. The web app then answers only at the loopback names its
`[site] hostname` gives, against DNS rebinding.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from istota import cli, db, setup_wizard, user_profiles
from istota.config import Config, load_config
from istota.webui import auth as web_auth
from tests.support import parity

pytest.importorskip("fastapi")
from httpx import ASGITransport, AsyncClient  # noqa: E402

from tests import test_setup_container as setup_container  # noqa: E402
from tests import test_setup_web as setup_web  # noqa: E402
from tests import test_web_auth_routes as routes  # noqa: E402
from tests.test_setup_container import _args, _forwarding_vm  # noqa: E402
from tests.test_setup_web import READY, _draft, _finish, _make, _unlock  # noqa: E402

# The fixtures, shared rather than copied.
_cheap_password_hash = routes._cheap_password_hash
configured = routes.configured
_no_credentials_in_env = setup_container._no_credentials_in_env
dirs = setup_web.dirs
clock = setup_web.clock

PORT = 8123
HOST = f"localhost:{PORT}"


def mint(db_path, user_id="alice", ttl=60):
    return web_auth.issue_token(db_path, user_id, "local_login", ttl)


@pytest.fixture
def identity(db_path):
    user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
    return web_auth.upsert_identity(db_path, "alice", "alice@example.com")


def expire_tokens(db_path):
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_tokens SET expires_at = ?", (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
        ))


# ---------------------------------------------------------------------------
# The token: one purpose, one use, sixty seconds
# ---------------------------------------------------------------------------


class TestTheToken:
    def test_a_code_signs_in_once(self, db_path, identity):
        code = mint(db_path)
        assert web_auth.consume_local_login(db_path, code) == (
            "alice", "alice@example.com", identity.credential_epoch,
        )
        assert web_auth.get_identity(db_path, "alice").last_login_at
        assert web_auth.consume_local_login(db_path, code) is None

    def test_it_lives_sixty_seconds(self, db_path, identity):
        assert web_auth.LOCAL_LOGIN_TTL_SECONDS == 60
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        mint(db_path)
        with db.get_db(db_path) as conn:
            row = conn.execute("SELECT purpose, expires_at FROM web_auth_tokens").fetchone()
        assert row["purpose"] == "local_login"
        expires = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S.%f")
        assert timedelta(seconds=59) <= expires - before <= timedelta(seconds=61)

    def test_an_expired_code_fails(self, db_path, identity):
        code = mint(db_path)
        expire_tokens(db_path)
        assert web_auth.consume_local_login(db_path, code) is None

    def test_a_newer_code_retires_the_older_one(self, db_path, identity):
        first, second = mint(db_path), mint(db_path)
        assert web_auth.consume_local_login(db_path, first) is None
        assert web_auth.consume_local_login(db_path, second) is not None

    def test_a_reset_link_is_not_a_login_code(self, db_path, identity):
        link = web_auth.issue_token(db_path, "alice", "reset", 3600)
        assert web_auth.consume_local_login(db_path, link) is None
        assert web_auth.peek_token(db_path, link, "reset") is not None

    def test_a_login_code_cannot_set_a_password(self, db_path, identity):
        code = mint(db_path)
        policy = web_auth.Policy(12, 900, 10, 30, 604800, 3600, 600, 3)
        for purpose in ("enrol", "reset", "local_login"):
            assert web_auth.consume_and_set_password(
                db_path, code, purpose, routes.PASSWORD, policy,
            ) is None
        assert web_auth.consume_local_login(db_path, code) is not None

    @pytest.mark.parametrize("mutation", ["disabled", "no_profile", "password"])
    def test_a_credential_change_kills_an_unused_code(self, db_path, identity, mutation):
        code = mint(db_path)
        if mutation == "disabled":
            web_auth.set_disabled(db_path, "alice", True)
        elif mutation == "no_profile":
            user_profiles.delete_profile(db_path, "alice")
        else:
            web_auth.set_password(db_path, "alice", routes.PASSWORD)
        assert web_auth.consume_local_login(db_path, code) is None

    @pytest.mark.parametrize("bad", ["", "x" * 300, "café", None, 7])
    def test_malformed_codes_are_refused(self, db_path, identity, bad):
        assert web_auth.consume_local_login(db_path, bad) is None


# ---------------------------------------------------------------------------
# The web routes (row M1)
# ---------------------------------------------------------------------------


@pytest.fixture
def local(configured):
    configured._config.site.hostname = HOST
    configured._config.web.auth = ["local_code", "email"]
    return configured


def _client(mod, host=HOST):
    return AsyncClient(transport=ASGITransport(app=mod.app), base_url=f"https://{host}")


def _local_form(page):
    return routes.csrf(page, "/istota/auth/local")


async def _sign_in_with(client, code):
    page = await client.get("/istota/auth/local")
    assert page.status_code == 200, page.text
    return await client.post("/istota/auth/local", data={"code": code, "csrf_token": _local_form(page)})


@parity.witness("M1")
class TestOnlyTheAppsUserSignsInWithoutAPassword:
    """M1: a code minted in the VM signs in once; nothing else gets past the
    login page, and the app answers only at its own loopback names."""

    async def test_no_session_gets_the_login_page(self, local):
        async with _client(local) as client:
            assert (await client.get("/istota/api/me")).status_code == 401
            page = await client.get("/istota/login")
        assert page.status_code == 200
        assert 'action="/istota/auth/local"' not in page.text

    async def test_a_code_works_once_and_fails_the_second_time(self, local):
        code = mint(local._config.db_path)
        async with _client(local) as client:
            resp = await _sign_in_with(client, code)
            assert resp.status_code == 302, resp.text
            assert resp.headers["location"] == "/istota/"
            assert routes.session_value(client)["auth"]["method"] == "local_code"
            assert (await client.get("/istota/api/me")).status_code == 200
        async with _client(local) as other:
            again = await _sign_in_with(other, code)
            assert again.status_code == 400
            assert (await other.get("/istota/api/me")).status_code == 401

    async def test_an_expired_code_fails(self, local):
        code = mint(local._config.db_path)
        expire_tokens(local._config.db_path)
        async with _client(local) as client:
            assert (await _sign_in_with(client, code)).status_code == 400
            assert (await client.get("/istota/api/me")).status_code == 401

    async def test_a_foreign_host_is_refused(self, local):
        code = mint(local._config.db_path)
        async with _client(local, "evil.example") as client:
            assert (await client.get("/istota/login")).status_code == 403
            assert (await client.get("/istota/auth/local")).status_code == 403
            resp = await client.post("/istota/auth/local", data={"code": code, "csrf_token": "x"})
            assert resp.status_code == 403
        # The refused request spent nothing.
        assert web_auth.consume_local_login(local._config.db_path, code) is not None

    @pytest.mark.parametrize("host", ["localhost:9999", "127.0.0.2:8123", "localhost.evil.example:8123"])
    async def test_the_right_name_on_the_wrong_port_or_a_lookalike_is_refused(self, local, host):
        async with _client(local, host) as client:
            assert (await client.get("/istota/login")).status_code == 403


class TestTheRoutes:
    async def test_127_0_0_1_on_the_port_is_this_install(self, local):
        code = mint(local._config.db_path)
        async with _client(local, f"127.0.0.1:{PORT}") as client:
            assert (await _sign_in_with(client, code)).status_code == 302

    async def test_the_page_keeps_the_code_out_of_history_and_the_log(self, local):
        async with _client(local) as client:
            page = await client.get("/istota/auth/local")
        assert "location.hash" in page.text
        assert "history.replaceState" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"

    async def test_a_wrong_form_token_is_refused_and_spends_nothing(self, local):
        code = mint(local._config.db_path)
        async with _client(local) as client:
            await client.get("/istota/auth/local")
            resp = await client.post("/istota/auth/local", data={"code": code, "csrf_token": "forged"})
        assert resp.status_code == 403
        assert web_auth.consume_local_login(local._config.db_path, code) is not None

    async def test_without_the_method_there_is_no_route_and_no_host_rule(self, configured):
        code = mint(configured._config.db_path)
        async with _client(configured, "example.com") as client:
            assert (await client.get("/istota/auth/local")).status_code == 404
            resp = await client.post("/istota/auth/local", data={"code": code, "csrf_token": "x"})
            assert resp.status_code == 404
            assert (await client.get("/istota/login")).status_code == 200

    async def test_a_local_code_session_ends_when_the_method_is_turned_off(self, local):
        async with _client(local) as client:
            assert (await _sign_in_with(client, mint(local._config.db_path))).status_code == 302
            local._config.web.auth = ["email"]
            assert (await client.get("/istota/api/me")).status_code == 401

    async def test_signing_out_everywhere_ends_it(self, local):
        async with _client(local) as client:
            assert (await _sign_in_with(client, mint(local._config.db_path))).status_code == 302
            web_auth.bump_epoch(local._config.db_path, "alice")
            assert (await client.get("/istota/api/me")).status_code == 401

    async def test_a_login_page_with_only_local_codes_says_where_to_sign_in(self, local):
        local._config.web.auth = ["local_code"]
        async with _client(local) as client:
            page = await client.get("/istota/login")
        assert page.status_code == 200
        assert 'name="password"' not in page.text
        assert "app on this Mac" in page.text


# ---------------------------------------------------------------------------
# Config: a method for a loopback hostname only
# ---------------------------------------------------------------------------


class TestTheConfig:
    def _load(self, tmp_path, hostname, auth='["local_code", "email"]'):
        path = tmp_path / "config.toml"
        path.write_text(f'[web]\nauth = {auth}\n\n[site]\nhostname = "{hostname}"\n')
        return load_config(path)

    @pytest.mark.parametrize("hostname", [HOST, f"127.0.0.1:{PORT}"])
    def test_a_loopback_hostname_with_a_port_takes_it(self, tmp_path, hostname):
        assert self._load(tmp_path, hostname).web.auth == ["local_code", "email"]

    @pytest.mark.parametrize("hostname", ["istota.example.com", "localhost", "istota.example.com:8123"])
    def test_any_other_hostname_is_refused(self, tmp_path, hostname):
        with pytest.raises(ValueError, match="local_code"):
            self._load(tmp_path, hostname)

    def test_none_stays_exclusive(self, tmp_path):
        assert self._load(tmp_path, HOST, '["none", "local_code"]').web.auth == ["none"]

    def test_the_hosts_it_answers_at(self):
        from istota.config import local_login_hosts
        assert local_login_hosts(HOST) == frozenset({HOST, f"127.0.0.1:{PORT}"})
        assert local_login_hosts("127.0.0.1:80") == frozenset(
            {"localhost:80", "127.0.0.1:80", "localhost", "127.0.0.1"},
        )
        assert local_login_hosts("istota.example.com") == frozenset()
        assert local_login_hosts("localhost") == frozenset()


# ---------------------------------------------------------------------------
# The CLI: `istota web login-code`
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_config(db_path, monkeypatch):
    config = Config(db_path=db_path)
    config.site.hostname = HOST
    config.web.auth = ["local_code", "email"]
    monkeypatch.setattr(cli, "load_config", lambda *a: config)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
    return config


@pytest.fixture
def web_cli(cli_config, monkeypatch, capsys):
    def run(*args):
        monkeypatch.setattr("sys.argv", ["istota", "web", *args])
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


class TestTheLoginCodeVerb:
    def test_it_prints_a_code_for_the_only_user(self, cli_config, web_cli, identity):
        code, out, err = web_cli("login-code")
        assert (code, err) == (0, "")
        printed = out.strip()
        assert re.fullmatch(r"[A-Za-z0-9_-]{40,}", printed), out
        assert web_auth.consume_local_login(cli_config.db_path, printed)[0] == "alice"

    def test_several_users_need_one_named(self, cli_config, web_cli, identity):
        user_profiles.ensure_profile(cli_config.db_path, "bob")
        web_auth.upsert_identity(cli_config.db_path, "bob", "bob@example.com")
        code, out, err = web_cli("login-code")
        assert code == 1 and out == ""
        assert "--user" in err
        code, out, err = web_cli("login-code", "--user", "bob")
        assert code == 0
        assert web_auth.consume_local_login(cli_config.db_path, out.strip())[0] == "bob"

    def test_no_user_is_a_refusal(self, web_cli):
        code, out, err = web_cli("login-code")
        assert code == 1 and out == "" and err

    def test_a_user_without_an_email_identity_is_refused(self, cli_config, web_cli):
        user_profiles.ensure_profile(cli_config.db_path, "alice")
        code, out, err = web_cli("login-code")
        assert code == 1 and out == "" and err

    def test_an_install_without_the_method_mints_nothing(self, cli_config, web_cli, identity):
        cli_config.web.auth = ["email"]
        code, out, err = web_cli("login-code")
        assert code == 1 and out == ""
        assert "local_code" in err
        with db.get_db(cli_config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM web_auth_tokens").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Setup writes the method on the desktop app's VM
# ---------------------------------------------------------------------------


def _desktop_vm(tmp_path, port="8181"):
    vm = _forwarding_vm(tmp_path, port)
    with (vm / "host.env").open("a") as fh:
        fh.write("ISTOTA_DESKTOP_APP=1\n")
    return vm


def _written_auth(tmp_path):
    import tomllib
    doc = tomllib.loads((tmp_path / "data" / "config" / "config.toml").read_text())
    return doc["web"]["auth"]


class TestSetupOnTheDesktopApp:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, _no_credentials_in_env):
        monkeypatch.delenv("ISTOTA_SETUP_SHAPE", raising=False)
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "native-key")

    def test_the_marker_is_read_from_host_env(self, tmp_path):
        assert setup_wizard.desktop_app(_desktop_vm(tmp_path)) is True
        (tmp_path / "plain").mkdir()
        assert setup_wizard.desktop_app(_forwarding_vm(tmp_path / "plain")) is False
        assert setup_wizard.desktop_app(None) is False

    def test_the_terminal_writes_local_code_and_email(self, tmp_path):
        _desktop_vm(tmp_path)
        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0
        assert _written_auth(tmp_path) == ["local_code", "email"]

    def test_without_the_marker_it_is_email(self, tmp_path):
        _forwarding_vm(tmp_path)
        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0
        assert _written_auth(tmp_path) == ["email"]

    def test_a_rerun_keeps_it(self, tmp_path, monkeypatch):
        _desktop_vm(tmp_path)
        assert setup_wizard.run_setup(_args(tmp_path), out=lambda *_: None) == 0
        first = (tmp_path / "data" / "config" / "config.toml").read_bytes()
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY")
        assert setup_wizard.run_setup(
            _args(tmp_path, force=True, user=None, brain=None, native_model=None), out=lambda *_: None,
        ) == 0
        assert (tmp_path / "data" / "config" / "config.toml").read_bytes() == first

    def test_only_under_local_ingress(self):
        a = setup_wizard.ContainerAnswers(user_id="alice", ingress="direct", desktop_app=True,
                                          hostname="istota.example.com")
        assert "local_code" not in a.web_auth

    def test_the_hostname_must_be_a_loopback_name(self):
        a = setup_wizard.ContainerAnswers(user_id="alice", desktop_app=True, hostname="istota.lan:8181")
        assert [e.field for e in setup_wizard.validate_container_answers(a)] == ["hostname"]

    def test_the_marker_is_written_nowhere_by_setup(self):
        a = setup_wizard.ContainerAnswers(user_id="alice", hostname=HOST, desktop_app=True)
        assert "ISTOTA_DESKTOP_APP" not in setup_wizard.host_env_values(a)

    def test_the_web_setup_writes_it_too(self, dirs, clock):
        _desktop_vm(dirs.vm_dir.parent)
        _app, client = _make(dirs, clock)
        assert _unlock(client).status_code == 200
        _draft(client, **{**READY, "hostname": "localhost:8181"})
        resp = _finish(client)
        assert resp.status_code == 200, resp.text
        import tomllib
        doc = tomllib.loads((dirs.data_dir / "config" / "config.toml").read_text())
        assert doc["web"]["auth"] == ["local_code", "email"]
