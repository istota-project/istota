"""Who may use the servarr skill, and with which verbs and flags.

Every case drives the CLI's own `main`, so the tier is resolved the way a task
resolves it: from `skills._load_context`, `_guard_skill`, the room's withheld
scopes and whether the task's room is an email thread. A refusal must send
nothing, so each one asserts the fake transport recorded zero requests.

Verbs whose handlers later stages write answer ``not_implemented`` once the
table lets them through; that answer is how these tests see "allowed".
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, ServarrApp, UserConfig
from istota.skills._loader import load_skill_index
from istota.skills.servarr import access
from tests.support.fake_arr import KEY, FakeArr, fixture, url
from tests.support.skill_cli import run_skill_main

UUID = "1d2b2b3a-4c5d-6e7f-8091-a2b3c4d5e6f7"


def _config(tmp_path, *, admins=("alice",), requesters=("Bob",), users=("alice", "bob", "carol"),
            apps=("sonarr", "radarr", "lidarr", "prowlarr")) -> Config:
    (tmp_path / "skills").mkdir(exist_ok=True)
    config = Config(
        db_path=tmp_path / "istota.db",
        skills_dir=tmp_path / "skills",
        admin_users=set(admins),
        users={name: UserConfig(display_name=name.title()) for name in users},
    )
    config.servarr.apps = {name: ServarrApp(url=url(name), api_key=KEY) for name in apps}
    config.servarr.requesters = list(requesters)
    config.servarr.private_hosts = ["10.0.0.5"]
    db.init_db(config.db_path)
    return config


@pytest.fixture
def arr(tmp_path, monkeypatch):
    """A fake stack, the CLI's hooks pointed at it, and the loaded config."""
    import istota.skills.servarr as servarr

    fake = FakeArr()
    for app in ("sonarr", "radarr", "lidarr", "prowlarr"):
        fake.json(app, "health", [])
    fake.json("radarr", "qualityprofile", fixture("qualityprofile.json"))
    fake.json("radarr", "rootfolder", fixture("rootfolder.json"))
    fake.json("radarr", "tag", fixture("tag.json"))
    state = {"config": _config(tmp_path)}
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: state["config"])
    monkeypatch.setattr(servarr, "_transport", fake.transport)
    monkeypatch.setenv("ISTOTA_USER_ID", "bob")
    for name in ("ISTOTA_TASK_ID", "ISTOTA_WITHHELD_SCOPES", "ISTOTA_CONVERSATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    fake.state = state
    return fake


def run(*argv):
    from istota.skills.servarr import main

    result = run_skill_main(main, list(argv))
    assert KEY not in result.stdout
    return result.exit_code, result.envelope


def as_user(monkeypatch, user):
    monkeypatch.setenv("ISTOTA_USER_ID", user)


# One argv per verb, each with only the arguments that verb requires.
VERBS = {
    "apps": ["apps"],
    "status": ["status", "--app", "radarr"],
    "describe": ["describe", "--app", "radarr"],
    "search": ["search", "Paris, Texas", "--kind", "film"],
    "add": ["add", "--kind", "film", "--tmdb", "655"],
    "queue": ["queue"],
    "why": ["why", "--kind", "film", "--tmdb", "655"],
    "wanted": ["wanted"],
    "search-now": ["search-now", "--kind", "film", "--library-id", "3"],
    "requests": ["requests"],
    "indexers": ["indexers"],
}
REQUESTER_VERBS = {"apps", "status", "search", "add", "queue", "why", "search-now", "requests"}
ADMIN_ONLY_VERBS = set(VERBS) - REQUESTER_VERBS


def allowed(code, envelope) -> bool:
    if envelope.get("status") == "ok":
        return True
    return envelope.get("reason") == "not_implemented"


class TestTheTable:
    def test_the_table_names_every_verb_the_parser_has(self):
        from istota.skills.servarr import build_parser

        parser = build_parser()
        subparsers = next(a for a in parser._actions if a.dest == "command")
        assert set(subparsers.choices) == set(access.VERB_TIERS) == set(VERBS)

    def test_the_admin_only_verbs_are_the_spec_s(self):
        assert {v for v, t in access.VERB_TIERS.items() if t == "admin"} == {
            "describe", "wanted", "indexers",
        }

    def test_every_flag_in_the_table_exists_on_its_verb(self):
        from istota.skills.servarr import build_parser

        parser = build_parser()
        subparsers = next(a for a in parser._actions if a.dest == "command")
        for (verb, flag) in access.FLAG_TIERS:
            options = {o for a in subparsers.choices[verb]._actions for o in a.option_strings}
            assert flag in options, (verb, flag)


class TestTiers:
    @pytest.mark.parametrize("verb", sorted(VERBS))
    def test_an_admin_may_call_every_verb(self, arr, monkeypatch, verb):
        as_user(monkeypatch, "alice")
        code, envelope = run(*VERBS[verb])
        assert allowed(code, envelope), envelope

    @pytest.mark.parametrize("verb", sorted(REQUESTER_VERBS))
    def test_a_requester_may_call_the_request_verbs(self, arr, verb):
        code, envelope = run(*VERBS[verb])
        assert allowed(code, envelope), envelope

    @pytest.mark.parametrize("verb", sorted(ADMIN_ONLY_VERBS))
    def test_a_requester_is_refused_the_admin_verbs_with_nothing_sent(self, arr, verb):
        code, envelope = run(*VERBS[verb])
        assert code == 1
        assert envelope["reason"] == "verb_requires_admin"
        assert arr.requests == []

    @pytest.mark.parametrize("verb", sorted(VERBS))
    def test_an_outsider_gets_nothing(self, arr, monkeypatch, verb):
        as_user(monkeypatch, "carol")
        code, envelope = run(*VERBS[verb])
        assert code == 1
        assert envelope["reason"] == "not_authorized"
        assert arr.requests == []

    def test_requesters_match_case_insensitively(self, arr, monkeypatch):
        as_user(monkeypatch, "BOB")
        code, envelope = run("apps")
        assert envelope["tier"] == "requester"

    def test_an_admin_not_listed_as_a_requester_is_still_admin(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        assert run("apps")[1]["tier"] == "admin"

    def test_empty_requesters_leaves_only_admins(self, arr, monkeypatch, tmp_path):
        arr.state["config"].servarr.requesters = []
        assert run("apps")[1]["reason"] == "not_authorized"
        as_user(monkeypatch, "alice")
        assert run("apps")[1]["tier"] == "admin"

    def test_an_empty_admins_file_with_several_users_makes_nobody_admin(self, arr, monkeypatch):
        arr.state["config"].admin_users = set()
        as_user(monkeypatch, "alice")
        assert run("apps")[1]["reason"] == "not_authorized"
        as_user(monkeypatch, "bob")
        assert run("apps")[1]["tier"] == "requester"

    def test_no_user_id_is_not_authorized_never_a_default_user(self, arr, monkeypatch):
        monkeypatch.delenv("ISTOTA_USER_ID")
        code, envelope = run("status")
        assert code == 1
        assert envelope["reason"] == "not_authorized"
        assert arr.requests == []

    def test_a_blank_user_id_is_not_authorized(self, arr, monkeypatch):
        as_user(monkeypatch, "  ")
        assert run("apps")[1]["reason"] == "not_authorized"


class TestFlags:
    @pytest.mark.parametrize("flag, value", [
        ("--profile", "Ultra-HD"), ("--root", "/data/elsewhere"), ("--metadata-profile", "None"),
    ])
    def test_an_admin_only_flag_is_refused_for_a_requester_by_name(self, arr, flag, value):
        code, envelope = run("add", "--kind", "film", "--tmdb", "655", flag, value)
        assert code == 1
        assert envelope["reason"] == "flag_requires_admin"
        assert envelope["flag"] == flag
        assert flag in envelope["error"]
        assert arr.requests == []

    @pytest.mark.parametrize("extra", [["--monitor", "lastSeason"], ["--no-search"]])
    def test_monitor_and_no_search_are_allowed(self, arr, extra):
        code, envelope = run("add", "--kind", "show", "--tvdb", "1", *extra)
        assert allowed(code, envelope), envelope

    def test_the_same_flags_pass_for_an_admin(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        code, envelope = run("add", "--kind", "film", "--tmdb", "655", "--profile", "Ultra-HD",
                             "--root", "/data/x")
        assert allowed(code, envelope), envelope

    @pytest.mark.parametrize("confirmed", [[], ["--confirmed"]])
    def test_an_artist_add_is_refused_for_a_requester_even_confirmed(self, arr, confirmed):
        code, envelope = run("add", "--kind", "artist", "--mbid", UUID, *confirmed)
        assert code == 1
        assert envelope["reason"] == "flag_requires_admin"
        assert envelope["flag"] == "--kind artist"
        assert arr.requests == []

    def test_an_album_add_is_allowed_for_a_requester(self, arr):
        code, envelope = run("add", "--kind", "album", "--mbid", UUID)
        assert allowed(code, envelope), envelope

    def test_indexers_test_is_admin_only(self, arr):
        assert run("indexers", "--test")[1]["reason"] == "verb_requires_admin"

    def test_why_releases_is_open_to_a_requester(self, arr):
        code, envelope = run("why", "--kind", "film", "--tmdb", "655", "--releases")
        assert allowed(code, envelope), envelope

    def test_an_invalid_monitor_mode_is_an_argparse_error(self, arr):
        code, _ = run("add", "--kind", "show", "--tvdb", "1", "--monitor", "everything")
        assert code == 2
        assert arr.requests == []


class TestTheRoom:
    def test_withheld_scopes_refuse_even_an_admin(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "files")
        code, envelope = run("status")
        assert code == 1
        assert envelope["reason"] == "not_authorized"
        assert arr.requests == []

    def _thread_room(self, config) -> str:
        with db.get_db(config.db_path) as conn:
            token = db.register_room(conn, None, "alice", origin="email", name="Thread").token
            db.add_room_binding(conn, token, "email", "<root@example.com>")
        return token

    def test_an_email_thread_caps_an_admin_at_requester(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", self._thread_room(arr.state["config"]))
        assert run("apps")[1]["tier"] == "requester"
        code, envelope = run("describe")
        assert envelope["reason"] == "verb_requires_admin"
        assert arr.requests == []

    def test_a_web_room_does_not_cap(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        with db.get_db(arr.state["config"].db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "general").token
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", token)
        assert run("apps")[1]["tier"] == "admin"

    @pytest.mark.parametrize("user", ["alice", "bob"])
    def test_confirmed_is_refused_in_an_email_thread_for_anyone(self, arr, monkeypatch, user):
        as_user(monkeypatch, user)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", self._thread_room(arr.state["config"]))
        code, envelope = run("add", "--kind", "film", "--tmdb", "655", "--confirmed")
        assert code == 1
        assert envelope["reason"] == "confirmation_not_allowed_here"
        assert arr.requests == []

    def test_a_room_check_that_raises_is_treated_as_an_email_thread(self, arr, monkeypatch):
        def boom(conn, token):
            raise RuntimeError("database is locked")

        as_user(monkeypatch, "alice")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "rm_whatever")
        monkeypatch.setattr("istota.rooms.scopes.is_email_thread_room", boom)
        assert run("apps")[1]["tier"] == "requester"

    def test_the_apps_verb_says_whether_confirmed_is_allowed(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        assert run("apps")[1]["confirmed_allowed"] is True
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", self._thread_room(arr.state["config"]))
        assert run("apps")[1]["confirmed_allowed"] is False


class TestTheSkillGate:
    def test_operator_disabled_skill_is_refused(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        arr.state["config"].disabled_skills = ["servarr"]
        code, envelope = run("status")
        assert envelope["reason"] == "not_authorized"
        assert "disabled" in envelope["error"]
        assert arr.requests == []

    def test_a_per_user_disable_is_refused(self, arr):
        arr.state["config"].users["bob"].disabled_skills = ["servarr"]
        assert run("apps")[1]["reason"] == "not_authorized"
        assert arr.requests == []

    def test_no_apps_configured_turns_the_skill_off(self, arr, monkeypatch):
        as_user(monkeypatch, "alice")
        arr.state["config"].servarr.apps = {}
        code, envelope = run("apps")
        assert envelope["reason"] == "not_authorized"


class TestTheKeyNeverLeaks:
    @pytest.mark.parametrize("argv", [
        ["status"], ["status", "--app", "radarr"], ["describe"], ["describe", "--app", "lidarr"],
    ])
    def test_failure_envelopes_carry_no_key(self, arr, monkeypatch, argv):
        import httpx

        as_user(monkeypatch, "alice")
        for app in ("sonarr", "radarr", "lidarr", "prowlarr"):
            arr.on(app, "GET", "system/status", httpx.Response(401, text=f"bad {KEY}"))
        code, envelope = run(*argv)
        # `run` asserts the key is absent from stdout; the shape is checked too.
        assert envelope


class TestTheFrontmatter:
    def _meta(self):
        bundled = Path(__file__).resolve().parents[1] / "src" / "istota" / "skills"
        return load_skill_index(bundled / "_none_", bundled_dir=bundled)["servarr"]

    def test_the_skill_is_private_in_a_shared_room(self):
        assert self._meta().shared_room == "private"

    def test_the_skill_needs_the_servarr_capability(self):
        assert self._meta().requires_capability == ["servarr"]

    def test_no_env_entry_hands_a_key_to_the_sandbox(self):
        meta = self._meta()
        assert meta.env_specs == []
        text = (Path(meta.skill_dir) / "skill.md").read_text()
        frontmatter = text.split("---", 2)[1]
        assert "env" not in {line.split(":", 1)[0].strip() for line in frontmatter.splitlines()}


# ---------------------------------------------------------------------------
# The real seam: a SkillProxy spawning the CLI, against a recording server
# ---------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def do_GET(self):  # noqa: N802 — http.server's spelling
        type(self).seen.append((self.path, self.headers.get("X-Api-Key", "")))
        if self.path.endswith("/system/status"):
            body = json.dumps(fixture("system_status_radarr.json")).encode()
        else:
            body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def recording_arr():
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _proxy_call(sock_path, args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(60)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps({"skill": "servarr", "args": args}) + "\n").encode())
        with sock.makefile("rb") as stream:
            return json.loads(stream.readline())


class TestThroughTheProxy:
    def _env(self, tmp_path, port, **extra):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        admins = tmp_path / "admins"
        admins.write_text("alice\n")
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'db_path = "{db_path}"\n'
            f'skills_dir = "{tmp_path / "skills"}"\n'
            '[users.alice]\n[users.bob]\n'
            '[servarr]\nrequesters = ["bob"]\nprivate_hosts = ["127.0.0.1"]\n'
            f'[servarr.apps.radarr]\nurl = "http://127.0.0.1:{port}"\napi_key = "{KEY}"\n'
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("ISTOTA_TASK_ID", "ISTOTA_WITHHELD_SCOPES", "ISTOTA_CONVERSATION_TOKEN")}
        env.update(ISTOTA_USER_ID="bob", ISTOTA_CONFIG_PATH=str(config_path),
                   ISTOTA_ADMINS_FILE=str(admins), ISTOTA_DB_PATH=str(db_path), **extra)
        return env

    def _call(self, env, args):
        from istota.sandbox.skill_proxy import SkillProxy

        with tempfile.TemporaryDirectory(prefix="servarr_proxy_", dir="/tmp") as directory:
            sock = Path(directory) / "s"
            with SkillProxy(sock, {}, env, allowed_skills=frozenset({"servarr"})):
                response = _proxy_call(sock, args)
        assert KEY not in response["stdout"] and KEY not in response["stderr"]
        return response

    def test_a_requester_whose_turn_withholds_scopes_is_refused_and_nothing_is_sent(
        self, tmp_path, recording_arr,
    ):
        env = self._env(tmp_path, recording_arr.server_port,
                        ISTOTA_WITHHELD_SCOPES="files,servarr")
        response = self._call(env, ["status", "--app", "radarr"])
        assert response["returncode"] == 1, response
        assert json.loads(response["stdout"])["reason"] == "not_authorized"
        assert _Recorder.seen == []

    def test_a_requester_in_their_private_room_gets_the_requester_tier(
        self, tmp_path, recording_arr,
    ):
        env = self._env(tmp_path, recording_arr.server_port)
        response = self._call(env, ["apps"])
        assert response["returncode"] == 0, response
        envelope = json.loads(response["stdout"])
        assert envelope["tier"] == "requester"
        assert _Recorder.seen == []

        response = self._call(env, ["status", "--app", "radarr"])
        assert response["returncode"] == 0, response
        assert json.loads(response["stdout"])["apps"][0]["version"] == "5.9.1.9070"
        assert [path for path, _ in _Recorder.seen] == [
            "/api/v3/system/status", "/api/v3/health",
        ]
        assert {key for _, key in _Recorder.seen} == {KEY}
