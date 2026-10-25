"""The read capabilities through the real router: `kv:read`, `files:read`, `calendar:read`, and Activity.

Same harness as `test_apps_routes.py`: a bare FastAPI app with the real
apps router, the auth stub overridden, a real framework database for KV and a
real `tmp_path` workspace with real symlinks. The calendar is doubled the way
the heartbeat tests double it, by patching `istota.skills.calendar`'s client
and event reader, so the owner filter in `owned_calendars` runs for real.

Each scope rule is checked twice: through a grant the install wrote, and
through a grant row tampered after the fact, since the bridge re-checks the
stored scope rather than trusting that only the install path wrote it.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.apps import db as apps_db
from istota.apps import install
from istota.config import NextcloudConfig
from istota.skills.calendar import CalendarEvent
from tests import test_apps_routes as harness
from tests.test_apps_routes import PREFIX, USER, _call, _calls, _conn, _install

# The routes suite's fixtures, bound here by name so pytest finds them.
apps_dir = harness.apps_dir
config = harness.config
apps_root = harness.apps_root
client = harness.client


def _user_root(config):
    return config.workspace_root(USER)


def _set_scope(config, capability, scope, *, app_id="board", version=1):
    with _conn(config) as conn:
        apps_db.put_grant(conn, app_id, version, capability, scope)
        conn.commit()


def _code(resp):
    return resp.json()["error"]["code"]


# -- kv:read ------------------------------------------------------------------


class TestKvRead:
    def _seed(self, config, namespace="notes", user=USER):
        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, user, namespace, "today", '{"text": "rain later"}')
            db.kv_set(conn, user, namespace, "plain", "not json")
            db.kv_set(conn, user, namespace, "other:1", "[1, 2]")

    def test_get_and_list_read_the_granted_namespace(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        self._seed(config)
        assert _call(client, "kv:read:notes", "get", {"key": "today"}).json() == {"result": {"text": "rain later"}}
        assert _call(client, "kv:read:notes", "get", {"key": "plain"}).json() == {"result": "not json"}
        assert _call(client, "kv:read:notes", "get", {"key": "missing"}).json() == {"result": None}
        listed = _call(client, "kv:read:notes", "list", {"prefix": "other:"}).json()["result"]
        assert listed == {"items": [{"key": "other:1", "value": [1, 2]}], "total": 1}
        assert _call(client, "kv:read:notes", "list", {}).json()["result"]["total"] == 3

    def test_only_the_users_own_rows_are_read(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        self._seed(config, user="bob")
        assert _call(client, "kv:read:notes", "list", {}).json() == {"result": {"items": [], "total": 0}}

    def test_another_namespace_is_not_granted(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        self._seed(config, namespace="journal")
        resp = _call(client, "kv:read:journal", "get", {"key": "today"})
        assert resp.status_code == 403
        assert _code(resp) == "not_granted"

    def test_without_the_grant_it_is_not_granted(self, config, apps_root, client):
        _install(config, apps_root)
        assert _code(_call(client, "kv:read:notes", "get", {"key": "today"})) == "not_granted"

    def test_writes_are_not_an_operation(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        resp = _call(client, "kv:read:notes", "set", {"key": "today", "value": 1})
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"
        with db.get_db(config.db_path) as conn:
            assert db.kv_get(conn, USER, "notes", "today") is None

    @pytest.mark.parametrize("stored", ["_secrets", "app.other", "app.board", "", None, 7])
    def test_a_stored_scope_the_rule_refuses_is_not_granted(self, config, apps_root, client, stored):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        target = stored if isinstance(stored, str) and stored else "notes"
        with db.get_db(config.db_path) as conn:
            db.kv_set(conn, USER, target, "k", '"leaked"')
        _set_scope(config, "kv:read:notes", stored)
        resp = _call(client, "kv:read:notes", "get", {"key": "k"})
        assert resp.status_code == 403
        assert _code(resp) == "not_granted"
        assert "leaked" not in resp.text

    def test_the_call_is_recorded(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        _call(client, "kv:read:notes", "get", {"key": "today"})
        [row] = _calls(config)
        assert (row["capability"], row["op"], row["status"]) == ("kv:read:notes", "get", "ok")


# -- files:read ---------------------------------------------------------------


class TestFilesRead:
    def _notes(self, config):
        folder = _user_root(config) / "notes"
        (folder / "aviation").mkdir(parents=True)
        (folder / "aviation" / "weather.md").write_text("# METAR\n")
        (folder / "blob.bin").write_bytes(bytes(range(256)))
        (folder / ".hidden").write_text("no")
        return folder

    def test_list_and_read_under_the_granted_folder(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["files:read:notes"])
        self._notes(config)
        listed = _call(client, "files:read:notes", "list", {"path": ""}).json()["result"]
        assert sorted((e["name"], e["kind"]) for e in listed["entries"]) == [("aviation", "dir"), ("blob.bin", "file")]
        inner = _call(client, "files:read:notes", "list", {"path": "aviation"}).json()["result"]
        assert [e["name"] for e in inner["entries"]] == ["weather.md"]
        assert _call(client, "files:read:notes", "read", {"path": "aviation/weather.md"}).json() == {"result": "# METAR\n"}
        encoded = base64.b64encode(bytes(range(256))).decode()
        assert _call(
            client, "files:read:notes", "read", {"path": "blob.bin", "encoding": "base64"},
        ).json() == {"result": encoded}

    def test_a_missing_file_is_not_found(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["files:read:notes"])
        self._notes(config)
        assert _code(_call(client, "files:read:notes", "read", {"path": "nope.md"})) == "not_found"
        assert _code(_call(client, "files:read:notes", "list", {"path": "nope"})) == "not_found"

    @pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", ".hidden", "a/../b", "aviation//weather.md"])
    def test_a_path_leaving_the_folder_is_bad_args(self, config, apps_root, client, path):
        _install(config, apps_root, capabilities=["files:read:notes"])
        self._notes(config)
        (_user_root(config) / "secret.txt").write_text("private")
        resp = _call(client, "files:read:notes", "read", {"path": path})
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"
        assert "private" not in resp.text

    def test_a_symlink_inside_the_folder_is_not_followed(self, config, apps_root, client, tmp_path):
        _install(config, apps_root, capabilities=["files:read:notes"])
        folder = self._notes(config)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("private")
        (folder / "link.txt").symlink_to(outside / "secret.txt")
        (folder / "linkdir").symlink_to(outside)
        assert _code(_call(client, "files:read:notes", "read", {"path": "link.txt"})) == "bad_args"
        assert _code(_call(client, "files:read:notes", "read", {"path": "linkdir/secret.txt"})) == "bad_args"
        assert _code(_call(client, "files:read:notes", "list", {"path": "linkdir"})) == "bad_args"
        listed = _call(client, "files:read:notes", "list", {"path": ""}).json()["result"]
        assert ("link.txt", "link") in [(e["name"], e["kind"]) for e in listed["entries"]]

    def test_a_granted_folder_that_is_a_symlink_is_refused(self, config, apps_root, client, tmp_path):
        _install(config, apps_root, capabilities=["files:read:notes"])
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("private")
        _user_root(config).mkdir(parents=True, exist_ok=True)
        (_user_root(config) / "notes").symlink_to(outside)
        resp = _call(client, "files:read:notes", "read", {"path": "secret.txt"})
        assert _code(resp) == "bad_args"
        assert "private" not in resp.text
        assert _code(_call(client, "files:read:notes", "list", {"path": ""})) == "bad_args"

    def test_the_stored_folder_is_read_not_the_capabilitys_own(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["files:read:notes"])
        self._notes(config)
        journal = _user_root(config) / "journal"
        journal.mkdir()
        (journal / "day.md").write_text("journal entry")
        with _conn(config) as conn:
            install.grant(conn, "board", 1, "files:read:notes", "journal", bot_dir=config.bot_dir_name)
            conn.commit()
        assert _call(client, "files:read:notes", "read", {"path": "day.md"}).json() == {"result": "journal entry"}
        assert _code(_call(client, "files:read:notes", "read", {"path": "blob.bin"})) == "not_found"

    @pytest.mark.parametrize("stored", ["Istota", "istota/config", "memories", "Memories/x", ".git", "a/../b", "", None])
    def test_a_stored_folder_the_deny_list_refuses_is_not_granted(self, config, apps_root, client, stored):
        _install(config, apps_root, capabilities=["files:read:notes"])
        root = _user_root(config)
        for name in ("Istota", "memories"):
            (root / name).mkdir(parents=True, exist_ok=True)
            (root / name / "x").write_text("private")
        _set_scope(config, "files:read:notes", stored)
        resp = _call(client, "files:read:notes", "read", {"path": "x"})
        assert resp.status_code == 403
        assert _code(resp) == "not_granted"
        assert "private" not in resp.text

    def test_writes_are_not_an_operation(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["files:read:notes"])
        folder = self._notes(config)
        resp = _call(client, "files:read:notes", "write", {"path": "new.md", "content": "x"})
        assert _code(resp) == "bad_args"
        assert not (folder / "new.md").exists()


# -- calendar:read ------------------------------------------------------------


ALICE_WORK = "https://cloud.example.com/remote.php/dav/calendars/istota/work/"
ALICE_HOME = "https://cloud.example.com/remote.php/dav/calendars/istota/home/"
BOB = "https://cloud.example.com/remote.php/dav/calendars/istota/bob/"


def _cal(name, url, owner):
    cal = MagicMock()
    cal.name = name
    cal.url = url
    cal.get_properties.return_value = {"{DAV:}owner": f"/remote.php/dav/principals/users/{owner}/"}
    return cal


def _events_by_url():
    return {
        ALICE_WORK: [CalendarEvent(
            uid="w1", summary="Standup", start=datetime(2026, 11, 2, 9, 0), end=datetime(2026, 11, 2, 9, 15),
            location="Room 4", timezone="Europe/Berlin",
        )],
        ALICE_HOME: [CalendarEvent(
            uid="h1", summary="Holiday", start=datetime(2026, 11, 3), end=datetime(2026, 11, 4), all_day=True,
        )],
        BOB: [CalendarEvent(
            uid="b1", summary="Bob's dentist", start=datetime(2026, 11, 2, 10, 0), end=datetime(2026, 11, 2, 11, 0),
        )],
    }


@pytest.fixture
def caldav(config):
    config.nextcloud = NextcloudConfig(url="https://cloud.example.com", username="istota", app_password="pw")
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.principal.return_value.calendars.return_value = [
        _cal("Work", ALICE_WORK, USER),
        _cal("Home", ALICE_HOME, USER),
        _cal("Bob", BOB, "bob"),
    ]
    events = _events_by_url()
    windows = []

    def get_events(_client, url, start, end):
        windows.append((url, start, end))
        return list(events.get(url, []))

    with patch("istota.skills.calendar.get_caldav_client", return_value=client), \
            patch("istota.skills.calendar.get_events", side_effect=get_events):
        yield windows


WINDOW = {"from": "2026-11-01", "to": "2026-11-08"}


class TestCalendarRead:
    def test_reads_every_owned_calendar_and_no_one_elses(self, config, apps_root, client, caldav):
        _install(config, apps_root, capabilities=["calendar:read"])
        result = _call(client, "calendar:read", "list", WINDOW).json()["result"]
        assert sorted(url for url, _, _ in caldav) == sorted([ALICE_WORK, ALICE_HOME])
        assert result["calendars"] == ["Work", "Home"]
        assert result["truncated"] is False
        assert [(e["calendar"], e["summary"]) for e in result["events"]] == [("Work", "Standup"), ("Home", "Holiday")]
        standup, holiday = result["events"]
        assert standup == {
            "uid": "w1", "calendar": "Work", "summary": "Standup",
            "start": "2026-11-02T09:00:00", "end": "2026-11-02T09:15:00", "all_day": False,
            "timezone": "Europe/Berlin", "location": "Room 4", "description": None,
        }
        assert (holiday["start"], holiday["end"], holiday["all_day"]) == ("2026-11-03", "2026-11-04", True)
        assert "dentist" not in str(result)

    def test_the_window_is_in_the_users_zone(self, config, apps_root, client, caldav):
        config.users[USER].timezone = "America/New_York"
        _install(config, apps_root, capabilities=["calendar:read"])
        _call(client, "calendar:read", "list", WINDOW)
        _, start, end = caldav[0]
        assert start.tzinfo is not None and str(start.tzinfo) == "America/New_York"
        assert (start.date(), end.date()) == (date(2026, 11, 1), date(2026, 11, 8))

    def test_a_named_calendar_reads_only_that_one(self, config, apps_root, client, caldav):
        _install(config, apps_root, capabilities=["calendar:read:Home"])
        result = _call(client, "calendar:read:Home", "list", WINDOW).json()["result"]
        assert [url for url, _, _ in caldav] == [ALICE_HOME]
        assert [e["summary"] for e in result["events"]] == ["Holiday"]

    def test_a_named_calendar_the_user_does_not_own_is_not_found(self, config, apps_root, client, caldav):
        _install(config, apps_root, capabilities=["calendar:read:Bob"])
        resp = _call(client, "calendar:read:Bob", "list", WINDOW)
        assert resp.status_code == 404
        assert _code(resp) == "not_found"
        assert caldav == []
        assert "dentist" not in resp.text

    def test_the_named_grant_does_not_reach_the_others(self, config, apps_root, client, caldav):
        _install(config, apps_root, capabilities=["calendar:read:Home"])
        assert _code(_call(client, "calendar:read", "list", WINDOW)) == "not_granted"
        assert _code(_call(client, "calendar:read:Work", "list", WINDOW)) == "not_granted"
        assert caldav == []

    def test_with_no_calendar_connected_it_is_not_found(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["calendar:read"])
        resp = _call(client, "calendar:read", "list", WINDOW)
        assert resp.status_code == 404
        assert resp.json()["error"] == {"code": "not_found", "message": "No calendar is connected."}

    @pytest.mark.parametrize("args", [
        {},
        {"from": "2026-11-01"},
        {"from": "2026-11-08", "to": "2026-11-01"},
        {"from": "2026-11-01", "to": "2026-11-01"},
        {"from": "2026-01-01", "to": "2027-06-01"},
        {"from": "next week", "to": "2026-11-08"},
        {"from": 20261101, "to": "2026-11-08"},
    ])
    def test_a_bad_window_is_bad_args(self, config, apps_root, client, caldav, args):
        _install(config, apps_root, capabilities=["calendar:read"])
        resp = _call(client, "calendar:read", "list", args)
        assert resp.status_code == 400
        assert _code(resp) == "bad_args"
        assert caldav == []

    def test_a_server_failure_is_internal_and_says_nothing_of_it(self, config, apps_root, client, caldav):
        _install(config, apps_root, capabilities=["calendar:read"])
        with patch("istota.skills.calendar.get_events", side_effect=RuntimeError("https://user:pw@host")):
            resp = _call(client, "calendar:read", "list", WINDOW)
        assert resp.status_code == 500
        assert _code(resp) == "internal"
        assert "pw@host" not in resp.text


# -- Activity -----------------------------------------------------------------


class TestActivity:
    def test_lists_the_calls_newest_first_and_the_current_versions_errors(self, config, apps_root, client):
        _install(config, apps_root, capabilities=["kv:read:notes"])
        _call(client, "kv:own", "set", {"key": "a", "value": 1})
        _call(client, "kv:read:journal", "get", {"key": "x"})
        client.post(f"{PREFIX}/board/v/1/errors", json={"kind": "error", "message": "v1 broke", "stack": "at x"})
        _install(config, apps_root, capabilities=["kv:read:notes"], index="<p>v2</p>")
        client.post(f"{PREFIX}/board/v/2/errors", json={"kind": "navigated", "message": "left", "stack": ""})
        _call(client, "kv:own", "get", {"key": "a"}, version=2)

        resp = client.get(f"{PREFIX}/board/activity")
        assert resp.status_code == 200
        body = resp.json()
        assert body["app_id"] == "board"
        assert body["current_version"] == 2
        calls = [(c["version"], c["capability"], c["op"], c["status"]) for c in body["calls"]]
        assert calls == [
            (2, "kv:own", "get", "ok"),
            (1, "kv:read:journal", "get", "not_granted"),
            (1, "kv:own", "set", "ok"),
        ]
        assert all(c["at"].endswith("Z") and "T" in c["at"] for c in body["calls"])
        assert "args_digest" not in body["calls"][0]
        assert [(e["version"], e["kind"], e["message"]) for e in body["errors"]] == [(2, "navigated", "left")]
        assert body["errors"][0]["at"].endswith("Z")

    def test_calls_are_capped_at_200(self, config, apps_root, client):
        _install(config, apps_root)
        with _conn(config) as conn:
            for i in range(205):
                apps_db.insert_call(
                    conn, app_id="board", version=1, capability="kv:own", op="get",
                    args={"i": i}, status="ok", retention_days=30,
                )
            conn.commit()
        assert len(client.get(f"{PREFIX}/board/activity").json()["calls"]) == 200

    def test_an_unknown_app_is_404(self, config, apps_root, client):
        assert client.get(f"{PREFIX}/nope/activity").status_code == 404
        assert client.get(f"{PREFIX}/NOT_AN_ID/activity").status_code == 404

    def test_with_the_module_off_it_is_404(self, config, apps_root, client):
        _install(config, apps_root)
        config.users[USER].disabled_modules = ["apps"]
        assert client.get(f"{PREFIX}/board/activity").status_code == 404
