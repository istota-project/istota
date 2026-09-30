"""A shared room reads only what its members granted, at every reach seam.

The disclosure gate is a boundary only if every route to a member's private
data is cut, so this drives one real task through ``execute_task`` and asserts
at four seams, each of which passes in a state the others refuse:

1. ``effective_disabled_skills`` withholds ``calendar`` (selection, the menu).
2. The ``allowed_skills`` handed to ``SkillProxy`` omits ``calendar``: the real
   allowlist, and the one the model actually hits.
3. ``HEALTH_DB_PATH`` reaches neither the proxy's base env nor the model's. It
   comes from the health skill's ``setup_env`` hook, which is dispatched over the
   whole index, so authorizing fewer skills does not remove it.
4. The sandbox argv binds no ``{mount}/Users/{user_id}``.

A test asserting only the first would pass against an advisory implementation.
Then two controls: the same task in a private room, and in the shared room after
the sender granted ``files``, ``calendar`` and ``health``, both asserting the
opposite on all four.

The default suite patches ``_bwrap_available`` and reads argv; it has never run
a namespace. The smoke witness is ``tests/smoke/test_sandbox_shared_room.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db
from istota.brain import BrainResult
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    SecurityConfig,
)

HEALTH_DB = "/nonexistent/alice/health.db"


def _capturing_brain(captured: list):
    """The real brain for the config, with ``execute`` recording the request."""
    from istota.brain import make_brain

    def _make(brain_config):
        brain = make_brain(brain_config)

        def _execute(req):
            captured.append(req)
            return BrainResult(success=True, result_text="ok")

        brain.execute = _execute
        return brain

    return _make


@pytest.fixture
def _bwrap_flag_cache():
    from istota import executor

    saved = dict(executor._bwrap_flag_support)
    executor._bwrap_flag_support.clear()
    yield
    executor._bwrap_flag_support.clear()
    executor._bwrap_flag_support.update(saved)


@pytest.fixture
def config(tmp_path, _bwrap_flag_cache):
    overrides = tmp_path / "skills"
    overrides.mkdir()
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Users" / "bob").mkdir(parents=True)
    cfg = Config(
        db_path=tmp_path / "data" / "istota.db",
        module_data_dir=tmp_path / "data" / "modules",
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="bot", app_password="nc_secret",
        ),
        workspace_path=mount,
        skills_dir=overrides,
        temp_dir=tmp_path / "temp",
        scheduler=SchedulerConfig(task_timeout_minutes=5),
        security=SecurityConfig(
            sandbox_enabled=True, skill_proxy_enabled=True, skill_proxy_timeout=30,
        ),
    )
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    (cfg.temp_dir / "alice").mkdir(parents=True, exist_ok=True)
    db.init_db(cfg.db_path)
    return cfg


def _room(config, *, shared: bool, grants: tuple[str, ...] = ()) -> str:
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Family")
        if shared:
            db.add_room_member(conn, room.token, "bob")
        for scope in grants:
            conn.execute(
                "INSERT INTO room_data_grants (room_token, user_id, scope) "
                "VALUES (?, ?, ?)",
                (room.token, "alice", scope),
            )
    return room.token


def _run(config, room_token: str) -> dict:
    """Run one web task in the room; return what each seam saw."""
    captured: list = []
    disabled_seen: list[set[str]] = []

    from istota.skills import _loader

    real_disabled = _loader.effective_disabled_skills

    def _spy_disabled(*args, **kwargs):
        result = real_disabled(*args, **kwargs)
        disabled_seen.append(set(result))
        return result

    def _health_hook(ctx):
        return {"HEALTH_DB_PATH": HEALTH_DB}

    with patch("istota.executor.make_brain", _capturing_brain(captured)), \
            patch("istota.executor._bwrap_available", return_value=True), \
            patch("istota.skill_proxy.SkillProxy") as mock_proxy, \
            patch.object(_loader, "effective_disabled_skills", _spy_disabled), \
            patch("istota.skills.health.setup_env", _health_hook):
        mock_proxy.return_value.__enter__ = lambda s: s
        mock_proxy.return_value.__exit__ = lambda s, *a: False
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="what's on my calendar tomorrow?", user_id="alice",
                source_type="web", conversation_token=room_token,
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            outcome = execute_task(task, config, [], conn=conn)
        assert captured, f"the brain was never called: {outcome!r}"
        req = captured[0]
        argv = req.sandbox_wrap(["claude", "-p"])

    assert mock_proxy.call_args is not None, "no skill proxy was built"
    args, kwargs = mock_proxy.call_args
    return {
        "disabled": disabled_seen[0],
        "allowed_skills": set(kwargs["allowed_skills"]),
        "proxy_base_env": args[2],
        "model_env": req.env,
        "argv": argv,
        "prompt": req.prompt,
    }


def _binds(argv: list[str]) -> list[str]:
    """Every source path bound read-write or read-only."""
    out = []
    for i, tok in enumerate(argv):
        if tok in ("--bind", "--ro-bind") and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def _user_dir(config) -> str:
    return str((config.workspace_path / "Users" / "alice").resolve())


class TestASharedRoomWithNoGrants:
    """The sender granted nothing, so nothing private reaches the task."""

    def test_calendar_is_disabled(self, config):
        seen = _run(config, _room(config, shared=True))
        assert "calendar" in seen["disabled"]

    def test_the_proxy_will_not_run_calendar(self, config):
        seen = _run(config, _room(config, shared=True))
        assert "calendar" not in seen["allowed_skills"]

    def test_the_health_db_path_reaches_no_one(self, config):
        seen = _run(config, _room(config, shared=True))
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert "HEALTH_DB_PATH" not in seen["model_env"]

    def test_the_user_workspace_is_not_bound(self, config):
        seen = _run(config, _room(config, shared=True))
        assert seen["argv"][0] == "bwrap"
        assert _user_dir(config) not in _binds(seen["argv"])


class TestMemoryIsAScope:
    """``memory`` is not a skill, so no skill gate can withhold it."""

    SENTINEL = "alice-private-memory-sentinel"

    def _write_user_md(self, config):
        path = (
            config.workspace_path / "Users" / "alice" / config.bot_dir_name
            / "config" / "USER.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Memory\n\n{self.SENTINEL}\n", encoding="utf-8")

    def test_user_md_does_not_reach_a_shared_room(self, config):
        self._write_user_md(config)
        seen = _run(config, _room(config, shared=True))
        assert self.SENTINEL not in seen["prompt"]

    def test_user_md_reaches_a_private_room(self, config):
        self._write_user_md(config)
        seen = _run(config, _room(config, shared=False))
        assert self.SENTINEL in seen["prompt"]

    def test_user_md_reaches_a_shared_room_once_granted(self, config):
        self._write_user_md(config)
        seen = _run(config, _room(config, shared=True, grants=("memory",)))
        assert self.SENTINEL in seen["prompt"]


class TestAPrivateRoomIsUnchanged:
    """Control: one member, so the gate is never consulted."""

    def test_every_seam_reaches_the_private_data(self, config):
        seen = _run(config, _room(config, shared=False))
        assert "calendar" not in seen["disabled"]
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert seen["argv"][0] == "bwrap"
        assert _user_dir(config) in _binds(seen["argv"])


class TestAfterTheSenderGrants:
    """Control: the same shared room once the sender has shared the scopes."""

    def test_every_seam_reaches_the_granted_data(self, config):
        token = _room(config, shared=True, grants=("files", "calendar", "health"))
        seen = _run(config, token)
        assert "calendar" not in seen["disabled"]
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert _user_dir(config) in _binds(seen["argv"])

    def test_a_grant_by_another_member_is_not_the_senders(self, config):
        token = _room(config, shared=True)
        with db.get_db(config.db_path) as conn:
            for scope in ("files", "calendar", "health"):
                conn.execute(
                    "INSERT INTO room_data_grants (room_token, user_id, scope) "
                    "VALUES (?, 'bob', ?)",
                    (token, scope),
                )
        seen = _run(config, token)
        assert "calendar" in seen["disabled"]
        assert "calendar" not in seen["allowed_skills"]
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert _user_dir(config) not in _binds(seen["argv"])


class TestThePolicySwitch:
    def test_policy_off_restores_every_seam(self, config):
        from istota.config import RoomsConfig

        config.rooms = RoomsConfig(shared_room_data_policy="off")
        seen = _run(config, _room(config, shared=True))
        assert "calendar" not in seen["disabled"]
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert _user_dir(config) in _binds(seen["argv"])

    def test_an_unknown_policy_restricts(self, config):
        from istota.config import RoomsConfig

        config.rooms = RoomsConfig(shared_room_data_policy="restirct")
        seen = _run(config, _room(config, shared=True))
        assert "calendar" in seen["disabled"]
        assert _user_dir(config) not in _binds(seen["argv"])


class TestTheMountPlanPerScope:
    """Seam 5 in the plan both the argv and the native roots project.

    ``files`` covers the per-resource mounts as well as the workspace, and
    ``developer`` the repos subtree with the package cache derived inside it:
    a derived cache bound without the repos bind over it is ISSUE-320's
    uncovered bind, so the two go together.
    """

    CASE_KW = dict(
        developer_enabled=True,
        repos_dir=True,
        resources=(("Docs", "readwrite"), ("Notes", "read")),
    )

    def _plan(self, tmp_path, monkeypatch, withheld):
        from istota.sandbox_plan import SandboxProfile, build_mount_plan
        from tests.test_sandbox_argv_golden import Case, _make_config, _make_world

        case = Case("shared_room", **self.CASE_KW)
        root = (tmp_path / "world").resolve()
        root.mkdir()
        world = _make_world(root, case)
        config = _make_config(case, world)
        monkeypatch.setenv("HOME", str(world["home"]))
        task = db.Task(
            id=1, prompt="t", user_id="alice", source_type="web",
            status="running", conversation_token="room123",
        )
        resources = [
            db.UserResource(
                id=i + 1, user_id="alice", resource_type="folder",
                resource_path=path, display_name=None, permissions=perm,
            )
            for i, (path, perm) in enumerate(case.resources)
        ]
        with patch(
            "istota.executor._source_and_venv_paths",
            return_value=(world["src"], world["venv"]),
        ), patch("istota.executor.python_base_prefix_binds", return_value=[]):
            plan = build_mount_plan(
                config, task, True, resources, world["user_temp"],
                profile=SandboxProfile.CLAUDE, withheld_scopes=withheld,
            )
        return {m.reason for m in plan.mounts}

    def test_nothing_withheld_binds_everything(self, tmp_path, monkeypatch):
        reasons = self._plan(tmp_path, monkeypatch, frozenset())
        assert {
            "nextcloud_user_dir", "user_resource", "developer_repos", "package_cache",
        } <= reasons

    def test_files_withheld_drops_the_workspace_and_resources(
        self, tmp_path, monkeypatch,
    ):
        reasons = self._plan(tmp_path, monkeypatch, frozenset({"files"}))
        assert "nextcloud_user_dir" not in reasons
        assert "user_resource" not in reasons
        assert {"developer_repos", "package_cache"} <= reasons

    def test_developer_withheld_drops_the_repos_and_their_cache(
        self, tmp_path, monkeypatch,
    ):
        reasons = self._plan(tmp_path, monkeypatch, frozenset({"developer"}))
        assert "developer_repos" not in reasons
        assert "package_cache" not in reasons
        assert {"nextcloud_user_dir", "user_resource"} <= reasons
