"""Exercise the real Flask request boundary with browser processes stubbed."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def api(monkeypatch, tmp_path):
    # Older browser tests install a module-global Flask double. Load the real
    # package only inside this fixture and restore their modules afterwards.
    for name in list(sys.modules):
        if name == "flask" or name.startswith("flask."):
            monkeypatch.delitem(sys.modules, name)
    browser = Path(__file__).resolve().parents[1] / "docker/browser"
    monkeypatch.syspath_prepend(str(browser))
    if "patchright.sync_api" not in sys.modules:
        sync = types.ModuleType("patchright.sync_api")
        sync.sync_playwright = MagicMock()
        monkeypatch.setitem(sys.modules, "patchright", types.ModuleType("patchright"))
        monkeypatch.setitem(sys.modules, "patchright.sync_api", sync)
    if "markdownify" not in sys.modules:
        converter = types.ModuleType("markdownify")
        converter.MarkdownConverter = type("MarkdownConverter", (), {})
        monkeypatch.setitem(sys.modules, "markdownify", converter)
    spec = importlib.util.spec_from_file_location("_scope_api", browser / "browse_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    monkeypatch.setattr(module.pool, "PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setattr(module.pool, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module.pool, "_instances", {})
    monkeypatch.setattr(module, "_get_memory_pct", lambda: 0)

    def launch(inst):
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com/"
        page.title.return_value = "Example"
        page.content.return_value = "<p>Example</p>"
        page.evaluate.return_value = []
        page.query_selector_all.return_value = []
        page.screenshot.return_value = b"image-bytes"
        page.opener.return_value = None
        page.frames = []
        ctx = MagicMock()
        ctx.pages = [page]
        ctx.new_page.return_value = page
        inst.pw_context = ctx
        inst.proc = MagicMock()
        inst.proc.poll.return_value = None

    monkeypatch.setattr(module.pool, "_start_display", lambda inst: None)
    monkeypatch.setattr(module.chrome, "launch_chrome", launch)
    module.connected_instances = []
    monkeypatch.setattr(module.chrome, "connect_cdp", module.connected_instances.append)
    monkeypatch.setattr(module.chrome, "ensure_chrome", lambda inst: launch(inst) if inst.pw_context is None else None)
    monkeypatch.setattr(module.chrome, "get_context", lambda inst, **kw: inst.pw_context)
    monkeypatch.setattr(module.chrome, "is_cdp_connected", lambda inst: inst is not None and inst.pw_context is not None)
    monkeypatch.setattr(module, "_navigate_and_wait", lambda *a, **kw: None)
    monkeypatch.setattr(module.browsing, "wait_for_datadome", lambda page: None)
    monkeypatch.setattr(module.browsing, "simulate_human_behavior", lambda *a, **kw: None)
    monkeypatch.setattr(module.browsing, "detect_captcha", lambda page: False)
    monkeypatch.setattr(module.browsing, "extract_page_content", lambda *a, **kw: {"text": "Example"})
    monkeypatch.setattr(module.browsing, "challenge_boxes", lambda page: [])
    monkeypatch.setattr(module.browsing, "cloudflare_checkbox_target", lambda page: (None, "no_challenge"))
    monkeypatch.setattr(module.render, "to_markdown", lambda *a, **kw: {"markdown": "Example"})
    monkeypatch.setattr(module, "_capture_foreground", lambda page: {})
    monkeypatch.setattr(module, "_foreground_headers", lambda verdict: {})
    monkeypatch.setattr(module.visual, "build_capture", lambda *a, **kw: (None, "unmeasured"))
    yield module
    module._sessions.clear()
    module.pool._instances.clear()


def post(api, path, user="alice", **body):
    headers = {} if user is None else {"X-Istota-User": user}
    return api.app.test_client().post(path, headers=headers, json=body)


@pytest.mark.parametrize("user", [None, "", ".", "..", "a/b", "/etc", " alice", "alice ", "al\tice", "é"])
def test_header_required_without_shared_fallback(api, monkeypatch, user):
    acquire = MagicMock(wraps=api.pool.acquire)
    monkeypatch.setattr(api.pool, "acquire", acquire)
    response = post(api, "/browse", user, url="https://example.com/")
    assert response.status_code == 400
    assert response.json == {"status": "error", "error": "user_scope_required"}
    acquire.assert_not_called()
    assert api.pool.live() == []


def test_health_and_empty_liveness_need_no_user_or_chrome(api):
    response = api.app.test_client().get("/health")
    assert response.status_code == 200
    assert response.json["per_user_profiles"] is True
    assert response.json["status"] == "ok"
    assert api._probe(False) == (200, b"ok\n")
    assert api._probe(True) == (200, b"ok\n")
    assert api.pool.live() == []


@pytest.mark.parametrize("user", ["alice", "a%2Fb", "alice: team", "#alice"])
def test_exact_identity_selects_profile_and_is_echoed(api, user):
    response = post(api, "/browse", user, url="https://example.com/", keep_session=True)
    assert response.status_code == 200
    assert response.json["user_scope"] == user
    inst = api.pool.instance_for(user)
    assert Path(inst.profile_dir) == Path(api.pool.PROFILE_ROOT) / "users" / user
    assert api._sessions[response.json["session_id"]]["user_id"] == user


@pytest.mark.parametrize("endpoint", ["browse", "render", "extract", "interact", "evaluate", "challenge", "screenshot", "get", "delete"])
def test_foreign_session_is_refused_before_any_touch(api, endpoint):
    created = post(api, "/browse", url="https://example.com/", keep_session=True)
    sid = created.json["session_id"]
    session = api._sessions[sid]
    session["last_used_at"] = 0  # expiry must not become a cross-user delete
    page = session["page"]
    page.reset_mock()
    before = dict(session)
    if endpoint in ("get", "delete"):
        response = api.app.test_client().open(f"/sessions/{sid}", method=endpoint.upper(), headers={"X-Istota-User": "bob"})
    else:
        response = post(api, "/" + endpoint, "bob", session_id=sid, expression="1")
    assert response.status_code == 404
    assert response.json["user_scope"] == "bob"
    assert api._sessions[sid] == before
    assert page.mock_calls == []
    assert api.pool.instance_for("bob") is None


@pytest.mark.parametrize("endpoint", ["browse", "render", "extract", "interact", "evaluate", "challenge", "screenshot", "get", "delete"])
def test_session_endpoints_echo_applied_scope(api, endpoint):
    sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    if endpoint in ("get", "delete"):
        response = api.app.test_client().open(f"/sessions/{sid}", method=endpoint.upper(), headers={"X-Istota-User": "alice"})
    else:
        response = post(api, "/" + endpoint, session_id=sid, expression="1")
    assert response.status_code == 200
    if endpoint == "screenshot":
        assert response.data == b"image-bytes"
        assert response.headers["X-Istota-User-Scope"] == "alice"
    else:
        assert response.json["user_scope"] == "alice"


def test_close_missing_session_does_not_launch(api):
    response = api.app.test_client().delete("/sessions/missing", headers={"X-Istota-User": "alice"})
    assert response.status_code == 200
    assert response.json["user_scope"] == "alice"
    assert api.pool.live() == []


def test_pool_errors_keep_scope_and_retry_metadata(api, monkeypatch):
    monkeypatch.setattr(api.pool, "acquire", MagicMock(side_effect=api.pool.PoolFull("busy")))
    response = post(api, "/browse", url="https://example.com/")
    assert response.status_code == 503
    assert response.json["user_scope"] == "alice"
    assert response.json["retry_after_seconds"] > 0
    monkeypatch.setattr(api.pool, "acquire", MagicMock(side_effect=api.pool.LaunchFailed("launch failed")))
    response = post(api, "/browse", url="https://example.com/")
    assert response.status_code == 502
    assert response.json["user_scope"] == "alice"


def test_non_string_wire_identity_is_refused(api):
    response = api.app.test_client().post(
        "/browse", json={"url": "https://example.com/"},
        environ_overrides={"HTTP_X_ISTOTA_USER": 123},
    )
    assert response.status_code == 400
    assert api.pool.live() == []


def test_credential_readback_redaction_is_per_user(api):
    alice = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    # The existing action records before validation, including partially failed
    # fills. No real input is necessary to exercise that retained secret set.
    post(api, "/interact", session_id=alice,
         actions=[{"type": "fill", "credential": True, "value": "sample-value"}])
    bob = post(api, "/browse", "bob", url="https://example.com/", keep_session=True).json["session_id"]
    page = api.pool.instance_for("bob").pw_context.pages[0]
    element = MagicMock()
    element.evaluate.return_value = {"text": "sample-value", "tag": "div"}
    page.query_selector_all.return_value = [element]
    response = post(api, "/extract", "bob", session_id=bob)
    assert response.status_code == 200
    assert response.json["elements"][0]["text"] == "sample-value"


def test_errors_and_captcha_echo_scope(api, monkeypatch):
    response = post(api, "/render", url="javascript:void(0)")
    assert response.status_code == 400
    assert response.json["user_scope"] == "alice"
    monkeypatch.setattr(api.browsing, "detect_captcha", lambda page: True)
    response = post(api, "/browse", url="https://example.com/", keep_session=True)
    assert response.json["status"] == "captcha"
    assert response.json["user_scope"] == "alice"


def test_two_users_own_separate_pages_and_closure(api):
    alice = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    bob = post(api, "/browse", "bob", url="https://example.com/", keep_session=True).json["session_id"]
    assert api._sessions[alice]["page"] is not api._sessions[bob]["page"]
    api._sessions[bob]["page"].reset_mock()
    response = api.app.test_client().delete(f"/sessions/{alice}", headers={"X-Istota-User": "alice"})
    assert response.status_code == 200
    assert alice not in api._sessions
    assert bob in api._sessions
    assert api._sessions[bob]["page"].mock_calls == []


def test_close_reaped_session_does_not_launch_or_touch_stale_page(api):
    sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    page = api._sessions[sid]["page"]
    api.pool._instances.clear()
    page.reset_mock()
    response = api.app.test_client().delete(f"/sessions/{sid}", headers={"X-Istota-User": "alice"})
    assert response.status_code == 200
    assert sid not in api._sessions
    assert page.mock_calls == []
    assert api.pool.live() == []


def test_per_user_budget_does_not_consume_another_users_allowance(api, monkeypatch):
    monkeypatch.setattr(api, "MAX_SESSIONS", 1)
    sid = post(api, "/browse", url="https://example.com/", keep_session=True, owner="task").json["session_id"]
    page = api._sessions[sid]["page"]
    page.reset_mock()
    response = post(api, "/browse", "bob", url="https://example.com/", keep_session=True, owner="task")
    assert response.status_code == 200
    assert sid in api._sessions
    assert page.mock_calls == []


def test_chrome_receives_concrete_instances_not_request_proxies(api):
    for user in ("alice", "bob"):
        response = post(api, "/render", user, url="https://example.com/", keep_session=True)
        assert response.status_code == 200
    # chrome retains these objects in its shared-driver recovery registry.
    # They must remain usable after Flask has torn the request context down.
    assert {inst.user_id for inst in api.connected_instances} == {"alice", "bob"}
    assert all(inst is api.pool.instance_for(inst.user_id) for inst in api.connected_instances)


def test_watchdog_is_armed_before_initial_cdp_connection(api, monkeypatch):
    observed = []
    monkeypatch.setattr(api.chrome, "connect_cdp", lambda inst: observed.append((inst, api._inflight)))
    response = post(api, "/browse", url="https://example.com/", keep_session=True)
    assert response.status_code == 200
    assert observed
    for inst, inflight in observed:
        assert inflight is not None
        assert inflight["instance"] is inst
        assert inst is api.pool.instance_for("alice")
    assert api._inflight is None


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_watchdog_covers_existing_session_teardown_without_acquisition(api, monkeypatch, method):
    sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    if method == "GET":
        api._sessions[sid]["last_used_at"] = 0
    observed = []

    def context(inst, **kwargs):
        observed.append((inst, api._inflight))
        return inst.pw_context

    monkeypatch.setattr(api.chrome, "get_context", context)
    acquire = MagicMock(side_effect=AssertionError("session teardown must not acquire"))
    monkeypatch.setattr(api.pool, "acquire", acquire)
    response = api.app.test_client().open(f"/sessions/{sid}", method=method, headers={"X-Istota-User": "alice"})
    assert response.status_code == (404 if method == "GET" else 200)
    assert observed
    for inst, inflight in observed:
        assert inflight is not None
        assert inflight["instance"] is inst
    acquire.assert_not_called()
    assert api._inflight is None


def test_global_budget_evicts_other_user_only_when_binding(api, monkeypatch):
    monkeypatch.setattr(api, "MAX_TOTAL_SESSIONS", 1)
    sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    response = post(api, "/browse", "bob", url="https://example.com/", keep_session=True)
    assert response.status_code == 200
    assert sid not in api._sessions
    assert list(api._sessions) == [response.json["session_id"]]


def test_busy_pool_refuses_without_stopping_live_session(api, monkeypatch):
    monkeypatch.setattr(api.pool, "MAX_INSTANCES", 1)
    sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    inst = api.pool.instance_for("alice")
    response = post(api, "/browse", "bob", url="https://example.com/")
    assert response.status_code == 503
    assert response.json["retry_after_seconds"] > 0
    assert api.pool.live() == [inst]
    assert sid in api._sessions


def test_health_reports_recovery_loop_of_one_instance(api):
    import time
    for user in ("alice", "bob"):
        post(api, "/browse", user, url="https://example.com/", keep_session=True)
    api.pool.instance_for("bob").wedge_recoveries = [time.monotonic()] * api.WEDGE_RECOVERY_THRESHOLD
    response = api.app.test_client().get("/health")
    assert response.json["status"] == "degraded"
    assert len(response.json["instances"]) == 2


def test_global_eviction_arms_watchdog_for_victim_and_restores_request(api, monkeypatch):
    monkeypatch.setattr(api, "MAX_TOTAL_SESSIONS", 1)
    post(api, "/browse", url="https://example.com/", keep_session=True)
    observed = []

    def context(inst, **kwargs):
        observed.append((inst.user_id, api._inflight["instance"].user_id))
        return inst.pw_context

    monkeypatch.setattr(api.chrome, "get_context", context)
    response = post(api, "/browse", "bob", url="https://example.com/", keep_session=True)
    assert response.status_code == 200
    assert ("alice", "alice") in observed
    assert all(actual == armed for actual, armed in observed)
    assert api._sessions[response.json["session_id"]]["user_id"] == "bob"


def test_cleanup_reaps_idle_foreign_instance_before_pressure_session(api, monkeypatch):
    import time
    sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    post(api, "/browse", "bob", url="https://example.com/")
    bob = api.pool.instance_for("bob")
    bob.last_used = time.monotonic()  # pressure, not the idle timer
    monkeypatch.setattr(api, "_get_memory_pct", lambda: 90)
    api._evict_request.set()
    response = post(api, "/evaluate", session_id=sid, expression="1")
    assert response.status_code == 200
    assert api.pool.instance_for("bob") is None
    assert sid in api._sessions
    assert not api._evict_request.is_set()


def test_cleanup_reaps_expired_foreign_sessions_and_idle_instance(api):
    sid = post(api, "/browse", "bob", url="https://example.com/", keep_session=True).json["session_id"]
    api._sessions[sid]["last_used_at"] = 0
    api.pool.instance_for("bob").last_used = 0
    response = post(api, "/browse", url="https://example.com/")
    assert response.status_code == 200
    assert sid not in api._sessions
    assert api.pool.instance_for("bob") is None


def test_monitor_attributes_descendant_rss_without_browser_calls(api, monkeypatch, caplog):
    import logging
    import threading
    for user, pid in (("alice", 100), ("bob", 200)):
        post(api, "/browse", user, url="https://example.com/", keep_session=True)
        api.pool.instance_for(user).proc.pid = pid
    # Grandchild first, plus a separate chrome tree that belongs to nobody.
    rows = "103 102 1024 chrome --type=renderer\n102 100 2048 chrome --type=zygote\n100 1 4096 chrome\n201 200 8192 chrome --type=renderer\n200 1 1024 chrome\n999 1 65536 chrome\n"
    monkeypatch.setattr(api.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(stdout=rows))
    monkeypatch.setattr(api, "_get_memory_pct", lambda: 90)
    monkeypatch.setattr(api.chrome, "get_context", MagicMock(side_effect=AssertionError("monitor touched browser")))
    with caplog.at_level(logging.WARNING):
        thread = threading.Thread(target=api._monitor_tick)
        thread.start()
        thread.join(5)
    assert "slot=0:rss=7MB" in caplog.text
    assert "slot=1:rss=9MB" in caplog.text
    assert "chrome_rss=16MB" in caplog.text
    assert len(api._sessions) == 2
    assert api._evict_request.is_set()


def test_health_logs_a_second_instances_wedge_once(api, monkeypatch, caplog):
    import logging
    import time
    for user in ("alice", "bob"):
        post(api, "/browse", user, url="https://example.com/", keep_session=True)
    monkeypatch.setattr(api.chrome, "devtools_responding", lambda *a, **kw: True)
    api.pool.instance_for("bob").cdp_health.update(
        consecutive_failures=api.CDP_FAILURE_THRESHOLD, last_failure=time.monotonic(), last_error="example failure",
    )
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            assert api._probe(True) == (503, b"cdp-wedged slot=1\n")
    assert len([r for r in caplog.records if "consecutive CDP failures" in r.message]) == 1


def test_global_budget_prefers_own_session_even_if_other_user_is_older(api, monkeypatch):
    monkeypatch.setattr(api, "MAX_TOTAL_SESSIONS", 2)
    alice = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
    bob = post(api, "/browse", "bob", url="https://example.com/", keep_session=True).json["session_id"]
    api._sessions[alice]["last_used_at"] -= 10
    response = post(api, "/browse", "bob", url="https://example.com/", keep_session=True)
    assert response.status_code == 200
    assert set(api._sessions) == {alice, response.json["session_id"]}
    assert bob not in api._sessions
