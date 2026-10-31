"""#884: the browser API takes `X-Istota-User` only from a holder of its token.

The server half runs a real listener, because the token is checked in the
dispatcher in front of Flask and a test client would walk past it.
"""

import importlib.util
import sys
import threading
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
TOKEN = "fixture-browser-token"


def _browser_server():
    spec = importlib.util.spec_from_file_location(
        "browser_server_under_test", REPO / "docker" / "browser" / "browser_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def serve(monkeypatch):
    """Start the dispatcher over a small app; yields the base URL and who it saw."""
    # Older browser tests install a module-global Flask double, so the real
    # package is loaded here and its modules restored afterwards.
    for name in list(sys.modules):
        if name == "flask" or name.startswith("flask."):
            monkeypatch.delitem(sys.modules, name)
    from flask import Flask, request

    browser_server = _browser_server()
    running = []
    seen = []

    def start(token):
        app = Flask("browser-under-test")

        @app.route("/health")
        def health():
            seen.append("/health")
            return {"status": "ok"}

        @app.route("/instances")
        def instances():
            seen.append("/instances")
            return {"instances": []}

        @app.route("/browse", methods=["POST"])
        def browse():
            seen.append(request.headers.get("X-Istota-User"))
            return {"status": "ok"}

        server, work = browser_server.make_browser_server(
            app, host="127.0.0.1", port=0, token=token,
        )
        stopping = threading.Event()
        worker = threading.Thread(
            target=browser_server.serve_browser_requests, args=(server, work, stopping),
        )
        worker.start()
        running.append((stopping, worker))
        return f"http://127.0.0.1:{server.server_port}", seen

    yield start
    for stopping, worker in running:
        stopping.set()
        worker.join(5)


@pytest.mark.parametrize("headers", [
    {"X-Istota-User": "victim"},
    {"X-Istota-User": "victim", "Authorization": "Bearer wrong"},
    {"X-Istota-User": "victim", "Authorization": TOKEN},
    {"X-Istota-User": "victim", "Authorization": f"Basic {TOKEN}"},
])
def test_a_peer_without_the_token_cannot_select_a_user(serve, headers):
    base, seen = serve(TOKEN)
    response = httpx.post(base + "/browse", headers=headers, timeout=5)
    assert response.status_code == 401
    assert response.json()["error"] == "browser_api_unauthorized"
    assert TOKEN not in response.text
    assert seen == []


def test_the_token_holder_reaches_the_route_as_the_user_it_names(serve):
    base, seen = serve(TOKEN)
    response = httpx.post(
        base + "/browse", timeout=5,
        headers={"X-Istota-User": "alice", "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert seen == ["alice"]


def test_the_instance_list_is_behind_the_token_too(serve):
    """It runs off the owning thread and names every user with a live browser."""
    base, seen = serve(TOKEN)
    assert httpx.get(base + "/instances", timeout=5).status_code == 401
    assert seen == []
    answered = httpx.get(base + "/instances", timeout=5,
                         headers={"Authorization": f"Bearer {TOKEN}"})
    assert answered.status_code == 200


def test_health_stays_open_for_the_doctor_and_the_version_gate(serve):
    base, _ = serve(TOKEN)
    assert httpx.get(base + "/health", timeout=5).status_code == 200


def test_a_service_with_no_token_refuses_rather_than_opens(serve):
    base, seen = serve("")
    for headers in ({"X-Istota-User": "alice"},
                    {"X-Istota-User": "alice", "Authorization": "Bearer anything"}):
        response = httpx.post(base + "/browse", headers=headers, timeout=5)
        assert response.status_code == 503
        assert response.json()["error"] == "browser_api_token_missing"
    assert seen == []
    assert httpx.get(base + "/health", timeout=5).status_code == 200


class TestTheCaller:
    def _sent(self, monkeypatch):
        from istota.browser import admission

        sent = []

        def post(url, **kwargs):
            sent.append(kwargs)
            return "answer"

        monkeypatch.setattr(admission.httpx, "post", post)
        return admission, sent

    def test_browser_call_carries_the_token_the_process_holds(self, monkeypatch):
        admission, sent = self._sent(monkeypatch)
        monkeypatch.setenv("ISTOTA_BROWSER_API_TOKEN", TOKEN)
        headers = {"X-Istota-User": "alice"}
        assert admission.browser_call("post", "http://browser/browse", headers=headers) == "answer"
        assert sent[0]["headers"] == {
            "X-Istota-User": "alice", "Authorization": f"Bearer {TOKEN}",
        }
        # The caller's dict is often reused for the next request or a log line.
        assert headers == {"X-Istota-User": "alice"}

    def test_a_process_with_no_token_sends_no_authorization(self, monkeypatch):
        admission, sent = self._sent(monkeypatch)
        monkeypatch.delenv("ISTOTA_BROWSER_API_TOKEN", raising=False)
        admission.browser_call("post", "http://browser/browse", headers={"X-Istota-User": "alice"})
        assert sent[0]["headers"] == {"X-Istota-User": "alice"}


async def test_the_daemon_s_instance_poll_carries_the_token(monkeypatch):
    """scheduler.py records this list for the admin page, so a refused poll
    would show every browser as unavailable."""
    from istota.config import BrowserConfig
    from istota.webui import admin_browsers

    seen = []

    def answer(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json={"instances": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(admin_browsers.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(answer), **kw,
    ))
    monkeypatch.setenv("ISTOTA_BROWSER_API_TOKEN", TOKEN)
    result = await admin_browsers.snapshot(BrowserConfig(enabled=True))
    assert result["status"] == "ok"
    assert seen == [f"Bearer {TOKEN}"]


class TestTheDoctorCheck:
    def _run(self, monkeypatch, status=None, raises=None, enabled=True):
        from istota import doctor
        from istota.config import BrowserConfig, Config

        def get(url, **kwargs):
            if raises:
                raise raises
            return httpx.Response(status, json={"instances": []})

        monkeypatch.setattr(httpx, "get", get)
        config = Config()
        config.browser = BrowserConfig(enabled=enabled)
        return doctor.check_browser_api_token(config, True)

    @pytest.mark.parametrize("status", [401, 503])
    def test_a_refused_token_fails_and_says_what_to_do(self, monkeypatch, status):
        from istota.doctor import FAIL

        result = self._run(monkeypatch, status)
        assert result.status == FAIL
        assert "istota_browser_api_token" in result.remedy

    def test_an_accepted_token_passes(self, monkeypatch):
        from istota.doctor import OK

        assert self._run(monkeypatch, 200).status == OK

    def test_a_browser_that_is_down_is_some_other_check_s_news(self, monkeypatch):
        from istota.doctor import SKIP

        assert self._run(monkeypatch, raises=httpx.ConnectError("down")).status == SKIP

    def test_it_is_registered(self):
        from istota import doctor

        names = [check[0] for check in doctor.CHECKS]
        assert "browser.api_token" in names


class TestTheAdminEditor:
    def test_a_change_set_cannot_replace_the_generated_token(self):
        from istota.config_editor import edits

        with pytest.raises(edits.ChangeError, match="generated"):
            edits._read_secrets({"istota_browser_api_token": "chosen"})


def test_setup_generates_the_token_and_a_re_run_keeps_it(tmp_path):
    from istota import setup_wizard

    answers = setup_wizard.ContainerAnswers()
    setup_wizard._carry_forward_browser_api_token(answers, tmp_path)
    assert len(answers.browser_api_token) == 64
    (tmp_path / "istota_browser_api_token").write_text("already-here")
    setup_wizard._carry_forward_browser_api_token(answers, tmp_path)
    assert answers.browser_api_token == "already-here"


class TestTheComposeFile:
    def _services(self):
        import yaml

        return yaml.safe_load(
            (REPO / "docker" / "docker-compose.yml").read_text(encoding="utf-8"),
        )["services"]

    def test_the_daemon_and_the_browser_hold_it_and_web_does_not(self):
        services = self._services()
        name = "istota_browser_api_token"
        assert name in services["daemon"]["secrets"]
        assert name in services["browser"]["secrets"]
        assert name not in services["web"]["secrets"]

    def test_it_never_goes_through_the_environment_block(self):
        environment = self._services()["browser"]["environment"]
        assert not [key for key in environment if "TOKEN" in key]


class TestTheEntrypoint:
    def _text(self):
        return (REPO / "docker" / "browser" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_the_token_is_exported_only_after_every_other_child_started(self):
        """The proxy and websockify have no use for it. The value is exported
        rather than passed on the exec line, where tini's argv would show it."""
        text = self._text()
        export = text.index("export ISTOTA_BROWSER_API_TOKEN")
        assert export > text.index("egress_proxy.py")
        assert export > text.index("websockify --web")
        assert export < text.index("exec tini")
        assert "ISTOTA_BROWSER_API_TOKEN=" not in text[text.index("exec tini"):]

    def test_the_service_takes_it_out_of_its_environment_before_chrome_starts(self):
        source = (REPO / "docker" / "browser" / "browse_api.py").read_text(encoding="utf-8")
        main = source[source.index('if __name__ == "__main__":'):]
        assert main.index('os.environ.pop("ISTOTA_BROWSER_API_TOKEN"') < main.index("migrate_legacy_profile")
        assert "token=" in main[main.index("make_browser_server(app"):]
