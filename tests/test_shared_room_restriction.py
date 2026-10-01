"""A turn runs with its sender's reach, at every reach seam (ISSUE-576).

A member's turn in a shared room reaches everything their private room does:
asking in a room they know others read is the decision that the answer may be
read there. A guest's turn runs as the host and reaches nothing of the host's.
This drives one real task through ``execute_task`` and asserts at four seams,
each of which passes in a state the others refuse:

1. ``effective_disabled_skills`` withholds ``calendar`` (selection, the menu).
2. The ``allowed_skills`` handed to ``SkillProxy`` omits ``calendar``: the real
   allowlist, and the one the model actually hits.
3. ``HEALTH_DB_PATH`` reaches neither the proxy's base env nor the model's. It
   comes from the health skill's ``setup_env`` hook, which is dispatched over the
   whole index, so authorizing fewer skills does not remove it.
4. The sandbox argv binds no ``{mount}/Users/{user_id}``.

What a member's turn in a shared room does lose is ambient memory: ``USER.md``
is not put into its prompt.

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


def _room(config, *, shared: bool) -> str:
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Family")
        if shared:
            db.add_room_member(conn, room.token, "bob")
    return room.token


def _run(
    config, room_token: str, *, guest: bool = False, attachments: list[str] | None = None,
) -> dict:
    """Run one web task in the room; return what each seam saw.

    ``guest`` runs it as a guest's turn (multiplayer Stage 11, emissary mode).
    """
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
            patch("istota.skills.health.setup_env", _health_hook), \
            patch("istota.task_env._vault_credentials", lambda *a: {"bank": "s3cret"}):
        mock_proxy.return_value.__enter__ = lambda s: s
        mock_proxy.return_value.__exit__ = lambda s, *a: False
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="what's on my calendar tomorrow?", user_id="alice",
                source_type="web", conversation_token=room_token,
                attachments=attachments,
            )
            if guest:
                conn.execute("UPDATE tasks SET guest_participant_id = 1 WHERE id = ?",
                             (task_id,))
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
        "vault": kwargs["vault_credentials"],
        "vault_writes": kwargs["vault_write_limit"],
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


class TestAMembersTurnInASharedRoom:
    """The regression for ISSUE-576: nothing is withheld from a member."""

    def test_calendar_is_not_disabled(self, config):
        seen = _run(config, _room(config, shared=True))
        assert "calendar" not in seen["disabled"]

    def test_the_proxy_runs_calendar(self, config):
        seen = _run(config, _room(config, shared=True))
        assert "calendar" in seen["allowed_skills"]

    def test_the_health_db_path_reaches_the_proxy(self, config):
        seen = _run(config, _room(config, shared=True))
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert "HEALTH_DB_PATH" not in seen["model_env"]

    def test_the_user_workspace_is_bound(self, config):
        seen = _run(config, _room(config, shared=True))
        assert seen["argv"][0] == "bwrap"
        assert _user_dir(config) in _binds(seen["argv"])

    def test_the_vault_is_served(self, config):
        seen = _run(config, _room(config, shared=True))
        assert seen["vault"] == {"bank": "s3cret"}
        assert seen["vault_writes"] == config.security.vault_writes_per_task


class TestAGuestsTurnReachesNothing:
    """Control: the same room, the same seams, a guest's turn."""

    def test_every_seam_withholds(self, config):
        seen = _run(config, _room(config, shared=True), guest=True)
        assert "calendar" in seen["disabled"]
        assert "calendar" not in seen["allowed_skills"]
        assert "HEALTH_DB_PATH" not in seen["proxy_base_env"]
        assert "HEALTH_DB_PATH" not in seen["model_env"]
        assert _user_dir(config) not in _binds(seen["argv"])
        assert seen["vault"] == {}
        assert seen["vault_writes"] == 0


class TestAmbientMemoryStaysOut:
    """``USER.md`` reaches the prompt unasked, so a shared room leaves it out."""

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

    def test_control_user_md_reaches_a_private_room(self, config):
        self._write_user_md(config)
        seen = _run(config, _room(config, shared=False))
        assert self.SENTINEL in seen["prompt"]

    def test_the_memory_files_stay_reachable_on_request(self, config):
        # Left out of the prompt, not out of reach: the workspace that holds
        # USER.md is bound, so "what did I note about X" still works.
        seen = _run(config, _room(config, shared=True))
        assert _user_dir(config) in _binds(seen["argv"])


class TestHostPathsFollowTheFilesScope:
    """A skill CLI's host-path roots drop the workspace with ``files``."""

    def _roots(self, tmp_path, monkeypatch, withheld: str | None):
        from istota.skill_host_paths import (
            WITHHELD_SCOPES_VAR,
            env_host_roots,
            user_workspace_root,
        )

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path / "deferred"))
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room123")
        if withheld is None:
            monkeypatch.delenv(WITHHELD_SCOPES_VAR, raising=False)
        else:
            monkeypatch.setenv(WITHHELD_SCOPES_VAR, withheld)
        user_dir = (mount / "Users" / "alice").resolve()
        return user_dir, env_host_roots(), user_workspace_root()

    def test_files_withheld_drops_the_workspace(self, tmp_path, monkeypatch):
        user_dir, roots, own = self._roots(tmp_path, monkeypatch, "calendar,files,memory")
        assert user_dir not in roots
        assert own is None
        # The channel directory stays: CHANNEL.md is the room's own.
        assert any(r.name == "room123" for r in roots)

    def test_without_the_marker_the_workspace_is_a_root(self, tmp_path, monkeypatch):
        user_dir, roots, own = self._roots(tmp_path, monkeypatch, None)
        assert user_dir in roots
        assert own == user_dir

    def test_the_marker_reaches_the_proxy_and_not_the_model(self, config):
        from istota.skill_host_paths import WITHHELD_SCOPES_VAR

        seen = _run(config, _room(config, shared=True), guest=True)
        assert "files" in seen["proxy_base_env"][WITHHELD_SCOPES_VAR].split(",")
        assert WITHHELD_SCOPES_VAR not in seen["model_env"]
        member = _run(config, _room(config, shared=True))
        assert WITHHELD_SCOPES_VAR not in member["proxy_base_env"]


class TestAPrivateRoomIsUnchanged:
    """Control: one member."""

    def test_every_seam_reaches_the_private_data(self, config):
        seen = _run(config, _room(config, shared=False))
        assert "calendar" not in seen["disabled"]
        assert "calendar" in seen["allowed_skills"]
        assert seen["proxy_base_env"].get("HEALTH_DB_PATH") == HEALTH_DB
        assert seen["argv"][0] == "bwrap"
        assert _user_dir(config) in _binds(seen["argv"])


class TestTheMountPlanPerScope:
    """Seam 5 in the plan both the argv and the native roots project.

    ``files`` covers the per-resource mounts as well as the workspace, and
    ``developer`` the repos subtree with the package cache derived inside it:
    a derived cache bound without the repos bind over it is ISSUE-320's
    uncovered bind, so the two go together.
    """

    CASE_KW = dict(
        claude_home=True,
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
            "claude_projects",
        } <= reasons

    def test_files_withheld_drops_the_workspace_and_resources(
        self, tmp_path, monkeypatch,
    ):
        reasons = self._plan(tmp_path, monkeypatch, frozenset({"files"}))
        assert "nextcloud_user_dir" not in reasons
        # Every earlier task's CLI session JSONL is under here.
        assert "claude_projects" not in reasons
        assert "user_resource" not in reasons
        assert {"developer_repos", "package_cache"} <= reasons

    def test_developer_withheld_drops_the_repos_and_their_cache(
        self, tmp_path, monkeypatch,
    ):
        reasons = self._plan(tmp_path, monkeypatch, frozenset({"developer"}))
        assert "developer_repos" not in reasons
        assert "package_cache" not in reasons
        assert {"nextcloud_user_dir", "user_resource"} <= reasons


class TestAGuestsTurn:
    """Emissary mode (multiplayer Stage 11): a guest's turn runs as the host,
    and the host's own temp directory, where every other task of theirs leaves
    deferred ops the scheduler replays with their authority, is not in its
    namespace. Only a directory of its own inside it is."""

    def test_the_hosts_temp_dir_is_not_bound_only_its_own(self, config):
        seen = _run(config, _room(config, shared=True), guest=True)
        argv = seen["argv"]
        host_dir = str((config.temp_dir / "alice").resolve())
        rw = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
        assert host_dir not in rw
        own = [path for path in rw if path.startswith(host_dir + "/emissary-task-")]
        assert len(own) == 1
        assert seen["model_env"]["ISTOTA_DEFERRED_DIR"] == own[0]
        assert "calendar" in seen["disabled"]

    def test_control_an_unrestricted_turn_binds_the_temp_dir(self, config):
        # A member's turn, in a private or a shared room, is unrestricted and
        # binds the per-user directory.
        seen = _run(config, _room(config, shared=True))
        host_dir = str((config.temp_dir / "alice").resolve())
        argv = seen["argv"]
        assert host_dir in [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
