"""`istota.apps.routes` and `istota.apps.bridge`: the frame, the bridge and error reports.

Through the real router on a bare FastAPI app, with only the auth stub
overridden (the host app overrides it the same way). The module context is
resolved for real, so the module gate is the one production uses. Apps are
installed through `install.create_pending` and `approve` from a draft folder
in a `tmp_path` workspace; KV lands in a real framework database and files in
a real `data/` folder, with real symlinks for the refusals.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota import db
from istota.apps import bridge, install, kit, routes
from istota.apps import db as apps_db
from istota.config import Config, UserConfig
from istota.webui.router_stubs import require_auth
from tests.support.apps_build import write_build

USER = "alice"
INDEX = "<!doctype html><html><head><title>x</title></head><body><p>v1</p></body></html>\n"
PREFIX = "/istota/api/apps"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    directory = tmp_path / "build-apps"
    directory.mkdir()
    write_build(directory)
    monkeypatch.setenv("ISTOTA_WEB_APPS_DIR", str(directory))
    kit.reset_cache()
    yield directory
    kit.reset_cache()


@pytest.fixture
def config(tmp_path, apps_dir):
    cfg = Config(
        db_path=tmp_path / "state" / "istota.db",
        workspace_path=tmp_path / "ws",
        users={USER: UserConfig()},
        bot_name="Istota",
    )
    cfg.experimental.features = ["module_apps"]
    cfg.db_path.parent.mkdir(parents=True)
    db.init_db(cfg.db_path)
    return cfg


@pytest.fixture
def apps_root(config):
    root = config.workspace_root(USER) / config.bot_dir_name / "apps"
    root.mkdir(parents=True)
    return root


def _apps_db_path(config):
    return config.module_db_path(USER, "apps")


def _conn(config):
    path = _apps_db_path(config)
    apps_db.init_db(path)
    return apps_db.connect(path)


def _install(config, apps_root, *, app_id="board", capabilities=(), index=INDEX):
    folder = apps_root / app_id
    folder.mkdir(exist_ok=True)
    caps = ", ".join(f'"{c}"' for c in capabilities)
    (folder / "manifest.toml").write_text(
        f'id = "{app_id}"\nname = "Board"\nicon = "layout"\ncapabilities = [{caps}]\n'
    )
    (folder / "index.html").write_text(index)
    with _conn(config) as conn:
        pending_id = install.create_pending(config, USER, conn, app_id, origin="draft")
        new = install.new_capabilities(conn, app_id, apps_db.get_pending(conn, pending_id)["manifest"])
        version = install.approve(conn, pending_id, new_capabilities_ack=new)
        conn.commit()
    return version


def _make_app(config, *, signed_in=True):
    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)
    app.state.istota_config = config
    if signed_in:
        app.dependency_overrides[require_auth] = lambda: {"username": USER}
    return app


@pytest.fixture
def client(config):
    return TestClient(_make_app(config))


def _call(client, capability, op, args, *, app_id="board", version=1):
    return client.post(
        f"{PREFIX}/{app_id}/v/{version}/call",
        json={"capability": capability, "op": op, "args": args},
    )


def _calls(config, app_id="board"):
    with _conn(config) as conn:
        return apps_db.list_calls(conn, app_id)


def _data_dir(config, app_id="board"):
    return config.workspace_root(USER) / config.bot_dir_name / "apps" / app_id / "data"


# -- the frame ----------------------------------------------------------------


class TestFrame:
    def test_serves_the_stored_document_with_its_headers(self, config, apps_root, client):
        _install(config, apps_root)
        resp = client.get(f"{PREFIX}/board/v/1/frame")
        assert resp.status_code == 200
        with _conn(config) as conn:
            version = apps_db.get_version(conn, "board", 1)
            document = apps_db.get_document(conn, version["content_hash"])
        assert resp.content == document
        assert resp.headers["content-security-policy"] == version["csp"]
        assert resp.headers["content-security-policy"].endswith("frame-ancestors 'self'; sandbox allow-scripts")
        assert resp.headers["content-type"] == "text/html; charset=utf-8"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["cross-origin-resource-policy"] == "same-origin"
        assert resp.headers["cache-control"] == "private, no-store"

    def test_a_version_that_is_not_current_is_409(self, config, apps_root, client):
        _install(config, apps_root)
        _install(config, apps_root, index=INDEX.replace("v1", "v2"))
        assert client.get(f"{PREFIX}/board/v/1/frame").status_code == 409
        assert client.get(f"{PREFIX}/board/v/2/frame").status_code == 200
        with _conn(config) as conn:
            install.rollback(conn, "board", 1)
            conn.commit()
        assert client.get(f"{PREFIX}/board/v/2/frame").status_code == 409
        assert client.get(f"{PREFIX}/board/v/1/frame").status_code == 200

    def test_an_unknown_app_is_404(self, config, apps_root, client):
        assert client.get(f"{PREFIX}/nope/v/1/frame").status_code == 404
        assert client.get(f"{PREFIX}/NOT_AN_ID/v/1/frame").status_code == 404

    def test_without_a_session_it_is_401(self, config, apps_root):
        _install(config, apps_root)
        resp = TestClient(_make_app(config, signed_in=False)).get(f"{PREFIX}/board/v/1/frame")
        assert resp.status_code == 401
        assert "content-security-policy" not in resp.headers

    def test_with_the_module_off_for_the_user_it_is_404(self, config, apps_root, client):
        _install(config, apps_root)
        config.users[USER].disabled_modules = ["apps"]
        assert client.get(f"{PREFIX}/board/v/1/frame").status_code == 404
        assert _call(client, "kv:own", "get", {"key": "a"}).status_code == 404


# -- the bridge: kv:own -------------------------------------------------------


class TestKvOwn:
    def test_set_get_list_delete_round_trip(self, config, apps_root, client):
        _install(config, apps_root)
        value = {"id": "c1", "title": "Flat on Elm", "tags": ["rent"], "n": 3}
        assert _call(client, "kv:own", "set", {"key": "card:c1", "value": value}).json() == {"result": None}
        assert _call(client, "kv:own", "set", {"key": "card:c2", "value": "plain"}).status_code == 200
        assert _call(client, "kv:own", "set", {"key": "columns", "value": [1, 2]}).status_code == 200

        assert _call(client, "kv:own", "get", {"key": "card:c1"}).json() == {"result": value}
        assert _call(client, "kv:own", "get", {"key": "missing"}).json() == {"result": None}

        listed = _call(client, "kv:own", "list", {"prefix": "card:"}).json()["result"]
        assert listed == {"items": [{"key": "card:c1", "value": value}, {"key": "card:c2", "value": "plain"}], "total": 2}
        page = _call(client, "kv:own", "list", {"offset": 1, "limit": 1}).json()["result"]
        assert page == {"items": [{"key": "card:c2", "value": "plain"}], "total": 3}

        assert _call(client, "kv:own", "delete", {"key": "card:c2"}).json() == {"result": {"deleted": True}}
        assert _call(client, "kv:own", "delete", {"key": "card:c2"}).json() == {"result": {"deleted": False}}

    def test_values_land_in_the_app_namespace_as_the_kv_skill_stores_them(self, config, apps_root, client):
        _install(config, apps_root)
        _call(client, "kv:own", "set", {"key": "card:c1", "value": {"title": "Elm"}})
        with db.get_db(config.db_path) as conn:
            row = db.kv_get(conn, USER, "app.board", "card:c1")
            assert db.kv_list(conn, USER, "board") == []
        # The kv skill decodes with json.loads, so this is what the agent reads back.
        assert json.loads(row["value"]) == {"title": "Elm"}

    def test_a_value_the_agent_wrote_reads_back(self, config, apps_root, client):
        _install(config, apps_root)
        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, USER, "app.board", "columns", json.dumps([{"id": "a", "title": "Todo"}]))
            db.kv_set(conn, USER, "app.board", "note", "not json")
        assert _call(client, "kv:own", "get", {"key": "columns"}).json() == {"result": [{"id": "a", "title": "Todo"}]}
        assert _call(client, "kv:own", "get", {"key": "note"}).json() == {"result": "not json"}

    def test_one_app_never_reaches_another_s_namespace(self, config, apps_root, client):
        _install(config, apps_root)
        _install(config, apps_root, app_id="ledger")
        _call(client, "kv:own", "set", {"key": "k", "value": 1}, app_id="ledger")
        assert _call(client, "kv:own", "get", {"key": "k"}).json() == {"result": None}

    @pytest.mark.parametrize("args", [
        {},
        {"key": ""},
        {"key": 3},
        {"key": "x" * 1025},
    ])
    def test_a_bad_key_is_bad_args(self, config, apps_root, client, args):
        _install(config, apps_root)
        resp = _call(client, "kv:own", "get", args)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_args"

    def test_set_without_a_value_is_bad_args(self, config, apps_root, client):
        _install(config, apps_root)
        assert _call(client, "kv:own", "set", {"key": "a"}).status_code == 400


# -- the bridge: files:own ----------------------------------------------------


class TestFilesOwn:
    def test_write_read_list_delete_round_trip(self, config, apps_root, client):
        _install(config, apps_root)
        resp = _call(client, "files:own", "write", {"path": "notes/today.md", "content": "# hi\n", "encoding": "utf8"})
        assert resp.json() == {"result": {"path": "notes/today.md", "size": 5}}
        assert (_data_dir(config) / "notes" / "today.md").read_text() == "# hi\n"

        raw = bytes(range(256))
        encoded = base64.b64encode(raw).decode()
        _call(client, "files:own", "write", {"path": "blob.bin", "content": encoded, "encoding": "base64"})
        assert (_data_dir(config) / "blob.bin").read_bytes() == raw
        assert _call(client, "files:own", "read", {"path": "blob.bin", "encoding": "base64"}).json() == {"result": encoded}
        assert _call(client, "files:own", "read", {"path": "notes/today.md", "encoding": "utf8"}).json() == {"result": "# hi\n"}

        _call(client, "files:own", "write", {"path": "notes/today.md", "content": "replaced", "encoding": "utf8"})
        assert (_data_dir(config) / "notes" / "today.md").read_text() == "replaced"

        listed = _call(client, "files:own", "list", {"path": ""}).json()["result"]
        assert [(e["name"], e["kind"]) for e in listed["entries"]] == [("notes", "dir"), ("blob.bin", "file")]
        inner = _call(client, "files:own", "list", {"path": "notes"}).json()["result"]
        assert [e["name"] for e in inner["entries"]] == ["today.md"]

        assert _call(client, "files:own", "delete", {"path": "notes/today.md"}).json() == {"result": {"deleted": True}}
        assert not (_data_dir(config) / "notes" / "today.md").exists()
        assert _call(client, "files:own", "read", {"path": "notes/today.md"}).json()["error"]["code"] == "not_found"

    def test_listing_a_data_folder_that_does_not_exist_yet_is_empty(self, config, apps_root, client):
        _install(config, apps_root)
        assert _call(client, "files:own", "list", {"path": ""}).json() == {"result": {"entries": [], "truncated": False}}

    @pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", ".hidden", "a/../b", "a//b", "", "a/.git/x"])
    def test_a_path_leaving_the_folder_is_bad_args(self, config, apps_root, client, path):
        _install(config, apps_root)
        resp = _call(client, "files:own", "write", {"path": path, "content": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_args"
        assert not (apps_root / "board" / "escape.txt").exists()

    def test_a_symlink_in_the_way_is_refused(self, config, apps_root, client, tmp_path):
        _install(config, apps_root)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        data = _data_dir(config)
        data.mkdir()
        (data / "linked").symlink_to(outside)
        (data / "leaf.txt").symlink_to(outside / "secret.txt")

        read_through = _call(client, "files:own", "read", {"path": "linked/secret.txt"})
        assert read_through.status_code == 400
        assert read_through.json()["error"]["code"] == "bad_args"
        leaf = _call(client, "files:own", "read", {"path": "leaf.txt"})
        assert leaf.status_code == 400
        write = _call(client, "files:own", "write", {"path": "linked/new.txt", "content": "x"})
        assert write.status_code == 400
        assert not (outside / "new.txt").exists()
        assert _call(client, "files:own", "list", {"path": "linked"}).status_code in (400, 404)
        assert (outside / "secret.txt").read_text() == "secret"

    def test_a_data_folder_that_is_a_symlink_is_refused(self, config, apps_root, client, tmp_path):
        _install(config, apps_root)
        outside = tmp_path / "outside"
        outside.mkdir()
        _data_dir(config).symlink_to(outside)
        assert _call(client, "files:own", "write", {"path": "x.txt", "content": "x"}).status_code == 400
        assert not (outside / "x.txt").exists()

    def test_bad_content_is_bad_args(self, config, apps_root, client):
        _install(config, apps_root)
        assert _call(client, "files:own", "write", {"path": "a", "content": "%%%", "encoding": "base64"}).status_code == 400
        assert _call(client, "files:own", "write", {"path": "a", "content": 3}).status_code == 400
        assert _call(client, "files:own", "write", {"path": "a", "content": "x", "encoding": "latin1"}).status_code == 400

    def test_reading_binary_as_utf8_is_bad_args(self, config, apps_root, client):
        _install(config, apps_root)
        data = _data_dir(config)
        data.mkdir()
        (data / "b.bin").write_bytes(b"\xff\xfe")
        assert _call(client, "files:own", "read", {"path": "b.bin"}).json()["error"]["code"] == "bad_args"


# -- the caps -----------------------------------------------------------------


class TestQuota:
    def test_a_value_over_the_value_cap_is_quota(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.kv_value_max_bytes = 100
        resp = _call(client, "kv:own", "set", {"key": "big", "value": "x" * 200})
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "quota"
        with db.get_db(config.db_path) as conn:
            assert db.kv_get(conn, USER, "app.board", "big") is None

    def test_a_write_over_the_namespace_cap_is_quota(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.kv_max_bytes = 100
        assert _call(client, "kv:own", "set", {"key": "a", "value": "x" * 40}).status_code == 200
        # Replacing a key counts its new size, not both.
        assert _call(client, "kv:own", "set", {"key": "a", "value": "y" * 60}).status_code == 200
        resp = _call(client, "kv:own", "set", {"key": "b", "value": "z" * 40})
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "quota"
        with db.get_db(config.db_path) as conn:
            assert db.kv_get(conn, USER, "app.board", "b") is None

    def test_a_file_over_the_files_cap_is_quota(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.files_max_bytes = 10_000
        assert _call(client, "files:own", "write", {"path": "a.txt", "content": "x" * 100}).status_code == 200
        resp = _call(client, "files:own", "write", {"path": "b.txt", "content": "x" * 20_000})
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "quota"
        assert not (_data_dir(config) / "b.txt").exists()

    def test_a_body_over_the_bound_is_413(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.kv_value_max_bytes = 10
        resp = _call(client, "kv:own", "set", {"key": "a", "value": "x" * (70 * 1024)})
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "quota"


# -- grants, versions and the other error codes -------------------------------


class TestBridgeErrors:
    def test_a_capability_not_granted_is_not_granted(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["calendar:read"])
        resp = _call(client, "kv:read:notes", "get", {"key": "a"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "not_granted"
        with _conn(config) as conn:
            apps_db.set_grants(conn, "board", 1, {"kv:own": None})
            conn.commit()
        assert _call(client, "files:own", "list", {"path": ""}).json()["error"]["code"] == "not_granted"

    def test_an_unknown_operation_is_bad_args(self, config, apps_root, client):
        _install(config, apps_root)
        assert _call(client, "kv:own", "drop_all", {}).json()["error"]["code"] == "bad_args"

    def test_a_malformed_body_is_bad_args(self, config, apps_root, client):
        _install(config, apps_root)
        url = f"{PREFIX}/board/v/1/call"
        assert client.post(url, content=b"not json").status_code == 400
        assert client.post(url, json=["kv:own"]).status_code == 400
        assert client.post(url, json={"capability": "kv:own", "op": "get", "args": "key"}).status_code == 400

    def test_a_version_that_is_not_current_is_stale_version(self, config, apps_root, client):
        _install(config, apps_root)
        _install(config, apps_root, index=INDEX.replace("v1", "v2"))
        resp = _call(client, "kv:own", "get", {"key": "a"}, version=1)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "stale_version"

    def test_an_app_that_is_not_installed_is_not_found(self, config, apps_root, client):
        _install(config, apps_root)
        resp = _call(client, "kv:own", "get", {"key": "a"}, app_id="ghost")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_a_failing_handler_is_internal_and_logged_without_its_arguments(
        self, config, apps_root, client, monkeypatch, caplog,
    ):
        _install(config, apps_root)

        def explode(ctx, args):
            raise RuntimeError("boom")

        monkeypatch.setitem(bridge.HANDLERS, ("kv:own", "get"), explode)
        resp = _call(client, "kv:own", "get", {"key": "very-private-key"})
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "internal"
        assert "very-private-key" not in caplog.text
        assert "app=board" in caplog.text
        assert _calls(config)[0]["status"] == "internal"


class TestCallRecording:
    def test_every_call_is_recorded_with_its_status_and_no_arguments(self, config, apps_root, client):
        _install(config, apps_root)
        _call(client, "kv:own", "set", {"key": "secret-key", "value": 1})
        _call(client, "kv:own", "get", {})
        _call(client, "kv:read:notes", "get", {"key": "a"})
        rows = _calls(config)
        assert sorted(r["status"] for r in rows) == ["bad_args", "not_granted", "ok"]
        assert {r["version"] for r in rows} == {1}
        ok = next(r for r in rows if r["status"] == "ok")
        assert ok["capability"] == "kv:own" and ok["op"] == "set"
        assert ok["args_digest"] == apps_db.args_digest({"key": "secret-key", "value": 1})
        assert "secret-key" not in json.dumps(rows)

    def test_a_stale_call_is_recorded_too(self, config, apps_root, client):
        _install(config, apps_root)
        _install(config, apps_root, index=INDEX.replace("v1", "v2"))
        _call(client, "kv:own", "get", {"key": "a"}, version=1)
        assert [(r["version"], r["status"]) for r in _calls(config)] == [(1, "stale_version")]

    def test_old_calls_are_pruned_on_insert(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.calls_retention_days = 1
        with _conn(config) as conn:
            apps_db.insert_call(
                conn, app_id="board", version=1, capability="kv:own", op="get", args={},
                status="ok", retention_days=30,
                now=datetime.now(timezone.utc) - timedelta(days=3),
            )
            conn.commit()
        _call(client, "kv:own", "get", {"key": "a"})
        assert len(_calls(config)) == 1


class TestRateLimit:
    NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def _seed(self, conn, seconds_ago, n, status="ok"):
        for _ in range(n):
            apps_db.insert_call(
                conn, app_id="board", version=1, capability="kv:own", op="get", args={},
                status=status, retention_days=30, now=self.NOW - timedelta(seconds=seconds_ago),
            )

    def _dispatch(self, config, conn, now):
        return bridge.dispatch(
            config, USER, conn, app_id="board", version=1,
            capability="kv:own", op="get", args={"key": "a"}, now=now,
        )

    def test_over_the_limit_is_rate_limited_until_the_oldest_call_ages_out(self, config, apps_root):
        _install(config, apps_root)
        config.apps.calls_per_minute = 3
        with _conn(config) as conn:
            self._seed(conn, 50, 1)
            self._seed(conn, 20, 2)
            conn.commit()
            with pytest.raises(bridge.BridgeError) as exc:
                self._dispatch(config, conn, self.NOW)
            assert exc.value.code == "rate_limited"
            assert exc.value.status == 429
            assert exc.value.retry_after == 10
            # Refused calls do not hold the window open.
            with pytest.raises(bridge.BridgeError):
                self._dispatch(config, conn, self.NOW + timedelta(seconds=5))
            assert self._dispatch(config, conn, self.NOW + timedelta(seconds=11)) is None

    def test_calls_older_than_a_minute_do_not_count(self, config, apps_root):
        _install(config, apps_root)
        config.apps.calls_per_minute = 2
        with _conn(config) as conn:
            self._seed(conn, 61, 5)
            self._seed(conn, 10, 1)
            conn.commit()
            assert self._dispatch(config, conn, self.NOW) is None

    def test_the_route_answers_429_with_retry_after(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.calls_per_minute = 2
        now = datetime.now(timezone.utc)
        with _conn(config) as conn:
            for _ in range(2):
                apps_db.insert_call(
                    conn, app_id="board", version=1, capability="kv:own", op="get", args={},
                    status="ok", retention_days=30, now=now - timedelta(seconds=10),
                )
            conn.commit()
        resp = _call(client, "kv:own", "get", {"key": "a"})
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "rate_limited"
        assert 45 <= int(resp.headers["retry-after"]) <= 51


# -- error reports ------------------------------------------------------------


class TestErrorReports:
    def _post(self, client, body, version=1):
        return client.post(f"{PREFIX}/board/v/{version}/errors", json=body)

    def test_a_report_is_stored(self, config, apps_root, client):
        _install(config, apps_root)
        resp = self._post(client, {"kind": "navigated", "message": "the frame navigated", "stack": ""})
        assert resp.status_code == 204
        with _conn(config) as conn:
            rows = apps_db.list_errors(conn, "board", 1)
        assert [(r["kind"], r["message"]) for r in rows] == [("navigated", "the frame navigated")]

    def test_reports_are_trimmed_per_version(self, config, apps_root, client):
        _install(config, apps_root)
        config.apps.errors_per_version = 3
        for i in range(5):
            assert self._post(client, {"kind": "error", "message": f"e{i}", "stack": "at x"}).status_code == 204
        with _conn(config) as conn:
            assert [r["message"] for r in apps_db.list_errors(conn, "board", 1)] == ["e4", "e3", "e2"]

    def test_long_text_is_cut(self, config, apps_root, client):
        _install(config, apps_root)
        self._post(client, {"kind": "error", "message": "m" * 5000, "stack": "s" * 5000})
        with _conn(config) as conn:
            row = apps_db.list_errors(conn, "board", 1)[0]
        assert len(row["message"]) == 4096 and len(row["stack"]) == 4096

    def test_a_bad_report_is_refused(self, config, apps_root, client):
        _install(config, apps_root)
        assert self._post(client, {"kind": "other", "message": "x"}).status_code == 400
        assert self._post(client, {"kind": "error", "message": 3}).status_code == 400
        assert self._post(client, {"kind": "error", "message": "x" * 20_000}).status_code == 413
        assert self._post(client, {"kind": "error", "message": "x"}, version=9).status_code == 404
