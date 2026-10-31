"""The isolated browser profile: who gets it, what it may do, how tasks share it.

One profile, `__isolated__`, for every browse use that should carry no user
data. The choice is the daemon's (`Reach.isolated_browser`), reaches a skill CLI
as a proxy-only variable, and the skill and the container each refuse what a
shared cookie jar must not do. `#899`: a stranger's mail drove the host's
signed-in browser, because the header followed `ISTOTA_USER_ID`.
"""

import ast
import json
from pathlib import Path
import shutil
import socket
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from istota.browser import owner
from istota.browser.owner import (
    BROWSER_PROFILE_VAR,
    ISOLATED_PROFILE,
    browser_headers,
    browser_profile,
    require_isolated_profile,
    with_browser_owner,
)
from istota.config import Config, SecurityConfig, UserConfig
from istota.rooms import scopes as room_scopes
from istota.sandbox import task_env
from istota.sandbox.skill_proxy import SkillProxy
from istota.skills import browse as browse_skill
from istota.skills._types import SkillMeta
from tests.test_browser_user_scope import api, post  # noqa: F401 - `api` is a fixture

BROWSE_API = Path(__file__).resolve().parents[1] / "docker/browser/browse_api.py"

INDEX = {name: SkillMeta(name=name, description="x", cli=True)
         for name in ("browse", "calendar", "email", "skills")}


def isolated(owner_id="task-1"):
    return {"X-Istota-User": ISOLATED_PROFILE, "X-Istota-Owner": owner_id}


def isolated_post(api, path, owner_id="task-1", **body):  # noqa: F811
    return api.app.test_client().post(path, headers=isolated(owner_id), json=body)


def open_tab(api, owner_id="task-1"):  # noqa: F811
    response = isolated_post(api, "/browse", owner_id, url="https://example.com/", keep_session=True)
    assert response.status_code == 200, response.text
    return response.json["session_id"]


class TestTheProfileName:
    def test_the_browser_hardcodes_the_same_name(self):
        """The image imports nothing from `istota`, so the name is written twice."""
        tree = ast.parse(BROWSE_API.read_text())
        names = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and isinstance(node.targets[0], ast.Name)
        }
        assert names["ISOLATED_PROFILE"] == ISOLATED_PROFILE == "__isolated__"

    def test_no_configured_user_can_take_the_name(self):
        config = Config(users={ISOLATED_PROFILE: UserConfig(), "alice": UserConfig()})
        assert config.get_user(ISOLATED_PROFILE) is None
        assert config.get_user("alice") is not None


class TestWhatAHeaderCarries:
    def test_the_variable_decides_not_the_user_id(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_TASK_ID", "5")
        monkeypatch.setenv(BROWSER_PROFILE_VAR, ISOLATED_PROFILE)
        assert browser_headers() == {"X-Istota-User": ISOLATED_PROFILE, "X-Istota-Owner": "5"}
        assert "alice" not in with_browser_owner({"url": "x"})["owner"]

    def test_a_task_on_its_users_profile_names_no_owner(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_TASK_ID", "5")
        monkeypatch.setenv(BROWSER_PROFILE_VAR, "alice")
        assert browser_headers() == {"X-Istota-User": "alice"}

    def test_a_cli_with_no_proxy_keeps_its_users_profile(self, monkeypatch):
        """A CRON `skill:` job spawns the CLI directly, with no variable set."""
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv(BROWSER_PROFILE_VAR, raising=False)
        assert browser_profile() == "alice"

    def test_the_research_identity_never_names_a_profile(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", owner.BRIEFINGS_IDENTITY)
        monkeypatch.setenv("ISTOTA_TASK_ID", "5")
        monkeypatch.delenv(BROWSER_PROFILE_VAR, raising=False)
        assert browser_headers()["X-Istota-User"] == ISOLATED_PROFILE

    def test_an_isolated_task_without_an_id_is_refused(self, monkeypatch):
        monkeypatch.setenv(BROWSER_PROFILE_VAR, ISOLATED_PROFILE)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        with pytest.raises(ValueError):
            browser_headers()

    def test_a_daemon_caller_gets_a_fresh_owner_per_call(self):
        first, second = browser_headers(ISOLATED_PROFILE), browser_headers(ISOLATED_PROFILE)
        assert first["X-Istota-Owner"] != second["X-Istota-Owner"]

    def test_an_older_browser_is_refused_before_any_page(self):
        with patch("istota.browser.admission.browser_call") as call:
            call.return_value.status_code = 200
            call.return_value.json.return_value = {"briefing_task_isolation": True}
            with pytest.raises(ValueError, match="isolated_browser_unavailable"):
                require_isolated_profile("http://browser:9223")


class TestWhoGetsIt:
    def test_a_guests_turn(self, make_task):
        task = make_task(user_id="alice", guest_participant_id=7)
        assert room_scopes.reach_for_task(None, task, skill_index=INDEX).isolated_browser

    def test_a_correspondents_turn_with_browse_in_its_allowance(self, make_task, monkeypatch):
        monkeypatch.setattr(room_scopes, "correspondent_scopes", lambda *a, **k: (
            room_scopes.CorrespondentAllowance(scopes=frozenset({"browse", "email"}))))
        task = make_task(user_id="alice", source_type="email")
        reach = room_scopes.reach_for_task(None, task, skill_index=INDEX)
        assert "browse" not in reach.withheld
        assert reach.isolated_browser

    def test_research(self, make_task):
        task = make_task(user_id=owner.BRIEFINGS_IDENTITY, source_type="group_briefing_research")
        assert room_scopes.reach_for_task(None, task, skill_index=INDEX).isolated_browser

    def test_an_own_turn_is_the_only_one_on_the_users_profile(self, make_task):
        task = make_task(user_id="alice", source_type="talk")
        assert room_scopes.reach_for_task(None, task, skill_index=INDEX).isolated_browser is False

    def test_an_own_task_holding_nothing_private(self, make_task):
        """A subtask in a group chat that no member asked: every scope withheld."""
        task = make_task(user_id="alice", source_type="subtask", conversation_token="room",
                         is_group_chat=True)
        reach = room_scopes.reach_for_task(None, task, skill_index=INDEX)
        assert reach.author == "own" and reach.withheld
        assert reach.isolated_browser

    def test_an_unreadable_author(self, make_task, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("no such table")
        monkeypatch.setattr(room_scopes, "turn_author", broken)
        task = make_task(user_id="alice", source_type="talk")
        assert room_scopes.reach_for_task(None, task, skill_index=INDEX).isolated_browser

    def test_a_reach_built_elsewhere_defaults_to_isolated(self):
        assert room_scopes.Reach(frozenset(), "own").isolated_browser


class TestTheProxySetsIt:
    """From the reach, through `build_task_runtime`, to the header the CLI sends."""

    def _runtime(self, tmp_path, task, **kwargs):
        temp = tmp_path / "temp" / task.user_id
        control = tmp_path / "temp" / ".control" / task.user_id / f"task_{task.id}"
        temp.mkdir(parents=True, exist_ok=True)
        control.mkdir(parents=True, exist_ok=True)
        (tmp_path / "db").mkdir(exist_ok=True)
        config = Config(
            db_path=tmp_path / "db" / "test.db", temp_dir=tmp_path / "temp",
            workspace_path=tmp_path / "mount",
            security=SecurityConfig(sandbox_enabled=True, skill_proxy_enabled=True),
        )
        return task_env.build_task_runtime(
            config, task, user_temp_dir=temp, control_dir=control, task_attempt=1,
            selected_skills=[], skill_index={}, is_admin=False, user_resources=[],
            user_config=None, discovered_calendars=[], **kwargs,
        )

    def _header_from(self, runtime, monkeypatch):
        base = runtime.proxy_ctx.base_env
        for name in ("ISTOTA_USER_ID", "ISTOTA_TASK_ID", BROWSER_PROFILE_VAR):
            monkeypatch.setenv(name, base[name])
        return browser_headers()["X-Istota-User"]

    def test_a_correspondents_turn_sends_the_isolated_identity(self, tmp_path, make_task, monkeypatch):
        task = make_task(id=8, user_id="alice", source_type="email")
        reach = room_scopes.reach_for_task(None, task, skill_index=INDEX)
        runtime = self._runtime(tmp_path, task, isolated_browser=reach.isolated_browser)
        assert runtime.proxy_ctx.base_env["ISTOTA_USER_ID"] == "alice"
        assert self._header_from(runtime, monkeypatch) == ISOLATED_PROFILE

    def test_the_hosts_own_turn_sends_the_host(self, tmp_path, make_task, monkeypatch):
        task = make_task(id=9, user_id="alice", source_type="talk")
        reach = room_scopes.reach_for_task(None, task, skill_index=INDEX)
        runtime = self._runtime(tmp_path, task, isolated_browser=reach.isolated_browser)
        assert self._header_from(runtime, monkeypatch) == "alice"

    def test_the_model_never_holds_it(self, tmp_path, make_task):
        task = make_task(id=10, user_id="alice", source_type="talk")
        runtime = self._runtime(tmp_path, task, isolated_browser=True)
        assert BROWSER_PROFILE_VAR not in runtime.env

    def test_a_missing_answer_narrows(self, tmp_path, make_task):
        task = make_task(id=11, user_id="alice", source_type="talk")
        runtime = self._runtime(tmp_path, task)
        assert runtime.proxy_ctx.base_env[BROWSER_PROFILE_VAR] == ISOLATED_PROFILE

    def test_a_daemon_environment_value_does_not_survive(self, tmp_path, make_task, monkeypatch):
        monkeypatch.setenv(BROWSER_PROFILE_VAR, "bob")
        task = make_task(id=12, user_id="alice", source_type="talk")
        runtime = self._runtime(tmp_path, task, isolated_browser=False)
        assert runtime.proxy_ctx.base_env[BROWSER_PROFILE_VAR] == "alice"

    @patch("istota.sandbox.skill_proxy.run_capture")
    def test_a_client_cannot_name_a_profile(self, run):
        """A request carrying its own env changes nothing: the child's is the snapshot."""
        run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        # A short path, for AF_UNIX's limit.
        directory = Path(tempfile.mkdtemp(prefix="bp_", dir="/tmp"))
        sock_path = directory / "s.sock"
        request = {"skill": "browse", "args": ["state"], "env": {BROWSER_PROFILE_VAR: "alice"}}
        try:
            with SkillProxy(sock_path, {}, {"PATH": "/usr/bin", BROWSER_PROFILE_VAR: ISOLATED_PROFILE}):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(10)
                    client.connect(str(sock_path))
                    client.sendall((json.dumps(request) + "\n").encode())
                    while client.recv(4096):
                        pass
        finally:
            shutil.rmtree(directory)
        assert run.call_args.kwargs["env"][BROWSER_PROFILE_VAR] == ISOLATED_PROFILE


class TestTheSkillRefuses:
    @pytest.fixture(autouse=True)
    def on_the_isolated_profile(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_TASK_ID", "21")
        monkeypatch.setenv(BROWSER_PROFILE_VAR, ISOLATED_PROFILE)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(tmp_path / "db" / "istota.db"))

    @pytest.mark.parametrize("flag", [
        "--fill-credential", "--fill-otp", "--fill-recovery", "--fill-card", "--purchase",
        "--capture-otp", "--capture-codes", "--capture-phrase", "--capture-codes-download",
        "--save-recovery", "--find-secrets",
        # argparse takes an unambiguous abbreviation, so an exact match is not enough.
        "--fill-cred", "--fill-o", "--purch", "--save-rec", "--find-s",
    ])
    def test_each_credential_flag_before_any_lookup(self, flag, capsys):
        with patch("istota.skills.browse.parse_and_resolve") as parse:
            assert browse_skill.main(["interact", "s1", f"{flag}=#password=acme"]) == 1
            assert browse_skill.main(["interact", "s1", flag, "#password=acme"]) == 1
            parse.assert_not_called()
        assert "isolated_browser_credentials_refused" in capsys.readouterr().out

    @pytest.mark.parametrize("verb", ["state", "forget"])
    def test_state_and_forget(self, verb, capsys):
        with patch("istota.skills.browse.parse_and_resolve") as parse:
            assert browse_skill.main([verb]) == 1
            parse.assert_not_called()
        assert "isolated_browser_operation_refused" in capsys.readouterr().out

    def test_an_older_browser(self, capsys):
        with patch("istota.browser.admission.browser_call") as call:
            call.return_value.status_code = 200
            call.return_value.json.return_value = {"status": "ok"}
            assert browse_skill.main(["render", "https://example.com"]) == 1
        assert "isolated_browser_unavailable" in capsys.readouterr().out

    def test_the_users_own_profile_keeps_every_flag(self, monkeypatch, capsys):
        monkeypatch.setenv(BROWSER_PROFILE_VAR, "alice")
        with patch("istota.skills.browse.parse_and_resolve", side_effect=SystemExit(9)) as parse:
            with pytest.raises(SystemExit):
                browse_skill.main(["interact", "s1", "--fill-credential", "#p=acme"])
            parse.assert_called_once()

    @pytest.mark.parametrize("argv", [
        ["interact", "s1", "--fill", "#q=hello", "--click", "#go"],
        ["screenshot", "--session", "s1"],
        ["challenge", "s1"],
        ["links", "https://example.com"],
    ])
    def test_a_plain_verb_passes_the_gate(self, argv):
        with patch("istota.skills.browse.require_isolated_profile"), \
                patch("istota.skills.browse.parse_and_resolve", side_effect=SystemExit(9)) as parse:
            with pytest.raises(SystemExit):
                browse_skill.main(argv)
            parse.assert_called_once()

    def test_the_results_scope_is_the_profile(self):
        result = browse_skill._profile_result({"status": "ok"}, ISOLATED_PROFILE)
        assert result["user_scope"] == ISOLATED_PROFILE and "shared_profile" not in result


class TestTheContainerRefuses:
    def test_the_capability_flag(self, api):  # noqa: F811
        flags = api.app.test_client().get("/health").json
        assert flags["isolated_browser_profile"] is True
        assert "briefing_task_isolation" not in flags

    def test_an_owner_is_required(self, api):  # noqa: F811
        response = api.app.test_client().post(
            "/render", headers={"X-Istota-User": ISOLATED_PROFILE},
            json={"url": "https://example.com"})
        assert response.status_code == 403
        assert api.pool.live() == []

    def test_the_retired_name_opens_no_profile(self, api):  # noqa: F811
        response = api.app.test_client().post(
            "/render", headers={"X-Istota-User": "__briefings__", "X-Istota-Owner": "x"},
            json={"url": "https://example.com"})
        assert response.status_code == 403
        assert api.pool.live() == []

    @pytest.mark.parametrize("path", ["/evaluate", "/state", "/render-document"])
    def test_what_stays_refused(self, api, path):  # noqa: F811
        assert isolated_post(api, path).status_code == 403
        assert api.pool.live() == []

    def test_state_and_forget(self, api):  # noqa: F811
        client = api.app.test_client()
        assert client.get("/state", headers=isolated()).status_code == 403
        assert client.delete("/state", headers=isolated(), json={"all": True}).status_code == 403
        assert api.pool.live() == []

    @pytest.mark.parametrize("action", [
        {"type": "fill", "selector": "#p", "value": "x", "credential": True, "bound_hosts": []},
        {"type": "fill", "selector": "#c", "value": "x", "card_field": "number"},
        {"type": "fill", "selector": "#o", "value": "1", "expires_at": 1},
        {"type": "recovery_fill", "selector": "#r", "credential": True},
        {"type": "read_recovery", "selector": "#r"},
        {"type": "read_secret", "selector": "#r"},
        {"type": "read_secret_download", "selector": "#r"},
        {"type": "find_secrets"},
        {"type": "read_secret", "selector": "auto"},
    ])
    def test_a_credential_action_is_refused_whole(self, api, action):  # noqa: F811
        sid = open_tab(api)
        page = api._sessions[sid]["page"]
        page.reset_mock()
        response = isolated_post(api, "/interact", session_id=sid,
                                 actions=[{"type": "wait", "timeout": 1}, action])
        assert response.status_code == 403
        assert response.json["error"] == "isolated_browser_credentials_refused"
        assert page.mock_calls == []

    def test_a_named_users_credential_action_still_runs(self, api):  # noqa: F811
        sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
        response = post(api, "/interact", session_id=sid, actions=[{"type": "find_secrets"}])
        assert response.status_code == 200


class TestFullAbilities:
    def test_a_plain_interact(self, api):  # noqa: F811
        sid = open_tab(api)
        response = isolated_post(api, "/interact", session_id=sid,
                                 actions=[{"type": "wait", "timeout": 1}])
        assert response.status_code == 200, response.text
        assert response.json["user_scope"] == ISOLATED_PROFILE

    def test_a_screenshot(self, api):  # noqa: F811
        response = isolated_post(api, "/screenshot", session_id=open_tab(api))
        assert response.status_code == 200
        assert response.data == b"image-bytes"

    def test_a_challenge(self, api):  # noqa: F811
        response = isolated_post(api, "/challenge", session_id=open_tab(api))
        assert response.status_code == 200, response.text

    def test_a_forged_body_owner_is_replaced(self, api):  # noqa: F811
        sid = isolated_post(api, "/browse", "task-1", url="https://example.com/",
                            keep_session=True, owner="task-2").json["session_id"]
        assert api._sessions[sid]["owner"] == "task-1"

    @pytest.mark.parametrize("path", ["/browse", "/render", "/extract", "/interact", "/screenshot", "/challenge"])
    def test_another_tasks_tab_is_not_found(self, api, path):  # noqa: F811
        sid = open_tab(api, "task-1")
        page = api._sessions[sid]["page"]
        page.reset_mock()
        assert isolated_post(api, path, "task-2", session_id=sid).status_code == 404
        assert page.mock_calls == []


class TestConcurrency:
    def test_three_tasks_do_not_evict_each_other(self, api):  # noqa: F811
        tabs = [open_tab(api, f"task-{n}") for n in range(3)]
        assert set(tabs) == set(api._sessions)
        assert {api._sessions[sid]["owner"] for sid in tabs} == {"task-0", "task-1", "task-2"}

    def test_a_task_over_its_budget_replaces_only_its_own_tab(self, api):  # noqa: F811
        other = open_tab(api, "task-b")
        api._sessions[other]["last_used_at"] -= 100
        first = open_tab(api, "task-a")
        second = open_tab(api, "task-a")
        third = open_tab(api, "task-a")
        assert first not in api._sessions
        assert {other, second, third} == set(api._sessions)

    def test_a_users_budget_stays_per_user(self, api):  # noqa: F811
        first = post(api, "/browse", url="https://example.com/", keep_session=True, owner="t1").json["session_id"]
        post(api, "/browse", url="https://example.com/", keep_session=True, owner="t2")
        post(api, "/browse", url="https://example.com/", keep_session=True, owner="t3")
        assert first not in api._sessions

    def test_the_tab_ceiling_is_the_global_one(self, api):  # noqa: F811
        """Measured: at `MAX_TOTAL_SESSIONS` a new owner's tab replaces the oldest anywhere."""
        tabs = [open_tab(api, f"task-{n}") for n in range(api.MAX_TOTAL_SESSIONS)]
        api._sessions[tabs[0]]["last_used_at"] -= 100
        extra = open_tab(api, "task-extra")
        assert tabs[0] not in api._sessions
        assert extra in api._sessions and len(api._sessions) == api.MAX_TOTAL_SESSIONS

    def test_all_isolated_tasks_hold_one_chrome_slot(self, api):  # noqa: F811
        for n in range(3):
            open_tab(api, f"task-{n}")
        assert [inst.user_id for inst in api.pool.live()] == [ISOLATED_PROFILE]

    def test_two_busy_users_keep_the_isolated_profile_waiting_until_a_tab_expires(self, api):  # noqa: F811
        """Measured: the isolated profile is a third identity in `MAX_INSTANCES` slots."""
        tabs = [post(api, "/browse", user, url="https://example.com/", keep_session=True).json["session_id"]
                for user in ("alice", "bob")]
        refused = isolated_post(api, "/browse", url="https://example.com/")
        assert refused.status_code == 503
        assert refused.json["retry_after_seconds"] > 0
        for sid in tabs:
            api._sessions[sid]["last_used_at"] -= api.SESSION_TTL + 1
        assert isolated_post(api, "/browse", url="https://example.com/").status_code == 200

    def test_a_kept_isolated_tab_holds_a_slot_against_a_user(self, api):  # noqa: F811
        open_tab(api)
        post(api, "/browse", "alice", url="https://example.com/", keep_session=True)
        assert post(api, "/browse", "bob", url="https://example.com/").status_code == 503


class TestAForegroundDetail:
    TITLE = "Another task's page"

    def _stub(self, api, monkeypatch):  # noqa: F811
        verdict = api.visual.Foreground(
            False, False, "tab_not_foreground",
            f"asked for the tab titled 'mine' and the window is showing {self.TITLE!r}")
        monkeypatch.setattr(api.visual, "bring_to_front", lambda *a, **k: verdict)

    def _click(self, respond, sid):
        return respond("/interact", session_id=sid, actions=[{"type": "click", "selector": "#go"}])

    def test_another_tabs_title_is_left_out(self, api, monkeypatch):  # noqa: F811
        sid = open_tab(api)
        self._stub(api, monkeypatch)
        response = self._click(lambda path, **body: isolated_post(api, path, **body), sid)
        assert response.status_code == 200, response.text
        assert self.TITLE not in response.text
        assert response.json["actions"][0]["ok"] is not None

    def test_a_named_users_detail_keeps_it(self, api, monkeypatch):  # noqa: F811
        sid = post(api, "/browse", url="https://example.com/", keep_session=True).json["session_id"]
        self._stub(api, monkeypatch)
        response = self._click(lambda path, **body: post(api, path, **body), sid)
        assert self.TITLE in response.text


class TestTheOldProfileGoes:
    def _users(self, api):  # noqa: F811
        users = Path(api.pool.PROFILE_ROOT) / "users"
        users.mkdir(parents=True, exist_ok=True)
        return users

    def test_the_old_directory_is_deleted_and_others_stay(self, api):  # noqa: F811
        users = self._users(api)
        (users / "__briefings__" / "Default").mkdir(parents=True)
        (users / "__briefings__" / "Default" / "Cookies").write_text("jar")
        (users / "alice").mkdir()
        (users / "alice" / "Cookies").write_text("kept")
        api.pool.remove_retired_profiles()
        assert not (users / "__briefings__").exists()
        assert (users / "alice" / "Cookies").read_text() == "kept"

    def test_a_second_boot_finds_nothing_to_do(self, api):  # noqa: F811
        self._users(api)
        api.pool.remove_retired_profiles()
        api.pool.remove_retired_profiles()

    def test_a_link_is_removed_not_followed(self, api, tmp_path):  # noqa: F811
        users = self._users(api)
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "data").write_text("kept")
        (users / "__briefings__").symlink_to(victim, target_is_directory=True)
        api.pool.remove_retired_profiles()
        assert not (users / "__briefings__").is_symlink()
        assert (victim / "data").read_text() == "kept"

    def test_a_failure_is_a_warning_and_the_browser_still_boots(self, api, monkeypatch, caplog):  # noqa: F811
        users = self._users(api)
        (users / "__briefings__").mkdir()
        monkeypatch.setattr(api.pool.shutil, "rmtree", MagicMock(side_effect=OSError("busy")))
        with caplog.at_level("WARNING"):
            api.pool.remove_retired_profiles()
        assert "__briefings__" in caplog.text

    def test_boot_calls_it(self):
        source = BROWSE_API.read_text()
        boot = source[source.index('if __name__ == "__main__":'):]
        assert boot.index("pool.remove_retired_profiles()") < boot.index("_start_liveness_server()")
