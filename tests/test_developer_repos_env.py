"""`DEVELOPER_REPOS_DIR` names one user's subtree, and how it gets there.

Stage 1 of the per-user-repos-dir spec narrowed the sandbox bind to
``{repos_dir}/{user_id}`` and left the variable naming the shared root. That
combination is worse than either end of it: the model is told to clone into a
directory the namespace no longer contains, so the clone lands on bwrap's root
tmpfs and is thrown away when the task ends. The variable and the bind have to
move together, which is what this file holds.

The second half is the *mechanism*, and it is here because the spec left it to
a measurement rather than deciding it up front. A skill's ``setup_env`` hook
and its manifest can both name the same variable, and the merge
``execute_task`` performs has an order. ``TestManifestOutranksSetupEnv``
measures that order against a synthetic skill, so the answer is a test rather
than a reading of the source — and so the developer manifest's
``from: setup_env`` entry has a reason attached to it that survives.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import (
    Config,
    DeveloperConfig,
    NextcloudConfig,
    SchedulerConfig,
    SecurityConfig,
)


def _write_skill_md(base_dir: Path, name: str, frontmatter: dict) -> Path:
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{key}: {json.dumps(value, separators=(',', ':'))}")
        elif isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(f"{name} docs")
    md_path = skill_dir / "skill.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def _base_config(tmp_path: Path, *, bundled: Path | None = None) -> Config:
    """A config that reaches the env-building block of ``execute_task``."""
    overrides = tmp_path / "skill-overrides"
    overrides.mkdir(exist_ok=True)
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True, exist_ok=True)
    (mount / "Users" / "bob").mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "data" / "istota.db",
        module_data_dir=tmp_path / "data" / "modules",
        # No Nextcloud URL, and that is load-bearing rather than tidiness.
        # `execute_task`'s memory path reaches `ensure_user_directories_v2`,
        # which POSTs an OCS share — measured at 32 DNS lookups across this
        # file with a URL set. It costs nothing here (nothing asserts on
        # Nextcloud) and a test that opens a socket is a test that lies about
        # what it runs against; `_no_sockets` below keeps it that way.
        nextcloud=NextcloudConfig(),
        nextcloud_mount_path=mount,
        skills_dir=overrides,
        bundled_skills_dir=bundled,
        temp_dir=tmp_path / "temp",
        scheduler=SchedulerConfig(task_timeout_minutes=5),
        security=SecurityConfig(skill_proxy_enabled=True, skill_proxy_timeout=30),
    )


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    """Nothing here may reach the network, and it has to be enforced.

    Every caller on the memory path swallows exceptions for graceful
    degradation, so a guard that only raised would be caught and the property
    would quietly revert to a claim. Record, refuse, and assert at teardown —
    the same shape as `tests/test_prompt_golden.py::_no_sockets`, and for the
    same reason.
    """
    import socket

    attempts: list[str] = []

    def _refuse(*args, **kwargs):
        attempts.append(str(args[:2]))
        raise OSError("network access is not allowed in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    yield
    assert attempts == [], f"this test reached for the network: {attempts}"


def _run_task(config: Config, user_id: str = "alice") -> dict:
    """Execute one task and return the environment handed to the model."""
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    (config.temp_dir / user_id).mkdir(parents=True, exist_ok=True)
    db.init_db(config.db_path)

    with patch("istota.executor.subprocess.run") as mock_run, \
            patch("istota.skill_proxy.SkillProxy") as mock_proxy:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        mock_proxy.return_value.__enter__ = lambda s: s
        mock_proxy.return_value.__exit__ = lambda s, *a: False
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id=user_id, source_type="talk",
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

    return mock_run.call_args[1]["env"]


class TestTheVariableIsTheTasksOwnSubtree:
    """What the model is told, against what the sandbox actually binds.

    ``build_bwrap_cmd`` binds ``{repos_dir}/{user_id}``. A variable still
    naming ``{repos_dir}`` is not merely stale: the documented clone recipe is
    ``$DEVELOPER_REPOS_DIR/<namespace>/<project>.git``, so every clone would be
    written to a directory bubblewrap created on its own root tmpfs and lost at
    task exit, after minutes of work.
    """

    @pytest.fixture
    def config(self, tmp_path):
        cfg = _base_config(tmp_path)
        repos = tmp_path / "repos"
        repos.mkdir()
        # A forge token, because it is what auto-authorizes the developer skill
        # for a task that did not select it — and so what made the manifest's
        # `from: config` entry resolve on the live deployment. Without one the
        # variable is simply absent and every assertion below passes for a
        # reason that has nothing to do with the layout.
        cfg.developer = DeveloperConfig(
            enabled=True, repos_dir=str(repos), gitlab_token="GL-TOKEN-FOR-TESTS",
        )
        cfg.admin_users = set()  # empty file means everyone is an admin
        return cfg

    def test_the_variable_is_the_per_user_subtree(self, config, tmp_path):
        env = _run_task(config)
        assert env["DEVELOPER_REPOS_DIR"] == str(tmp_path / "repos" / "alice")

    def test_the_variable_is_never_the_shared_root(self, config, tmp_path):
        """The exposure stated as a refusal, because the two paths differ by
        one component and a partial fix reads the same at a glance."""
        env = _run_task(config)
        assert env["DEVELOPER_REPOS_DIR"] != str(tmp_path / "repos")

    def test_two_users_are_told_two_different_roots(self, config, tmp_path):
        alice = _run_task(config, user_id="alice")
        bob = _run_task(config, user_id="bob")
        assert alice["DEVELOPER_REPOS_DIR"] == str(tmp_path / "repos" / "alice")
        assert bob["DEVELOPER_REPOS_DIR"] == str(tmp_path / "repos" / "bob")

    def test_the_variable_names_the_directory_the_hook_created(
        self, config, tmp_path,
    ):
        """A path nothing created binds nothing — `_bind` skips a path that is
        not there — so the variable would name a tmpfs directory again."""
        env = _run_task(config)
        named = Path(env["DEVELOPER_REPOS_DIR"])
        assert named.is_dir()

    def test_a_non_admin_is_told_nothing(self, config):
        """The bind is admin-gated and so is the subtree the hook creates. A
        variable with no bind behind it names a directory on the root tmpfs,
        which is the same defect one gate further out."""
        config.admin_users = {"someone-else"}
        env = _run_task(config)
        assert "DEVELOPER_REPOS_DIR" not in env


class TestManifestOutranksSetupEnv:
    """The measurement the spec left open, on a synthetic skill.

    ``execute_task`` merges ``build_skill_env``'s manifest-resolved vars first
    and the ``setup_env`` hooks' vars second, both with ``if k not in env``. So
    where both name the same variable the *manifest* wins and the hook's value
    is dropped silently. That is why the developer manifest declares
    ``DEVELOPER_REPOS_DIR`` as ``from: setup_env`` — a metadata-only source —
    rather than leaving a ``from: config`` entry for the hook to override.

    Both halves are needed. The first alone would pass against a merge that
    dropped hook vars entirely, which would be a different defect with the same
    symptom.
    """

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        empty_bundled = tmp_path / "_empty_bundled"
        empty_bundled.mkdir()
        cfg = _base_config(tmp_path, bundled=empty_bundled)
        cfg.admin_users = set()
        cfg.bot_name = "from-the-manifest"
        _write_skill_md(cfg.skills_dir, "widget", {
            "description": "Widget store",
            "always_include": True,
            "env": [{
                "var": "WIDGET_ROOT", "from": "config", "config_path": "bot_name",
            }],
        })
        # `dispatch_setup_env_hooks` imports `istota.skills.<name>`, so the hook
        # has to be reachable under that name. A module in `sys.modules` is,
        # and it keeps the measurement independent of any shipped skill.
        mod = types.ModuleType("istota.skills.widget")
        mod.setup_env = lambda ctx: {
            "WIDGET_ROOT": "from-the-hook",
            "WIDGET_HOOK_ONLY": "from-the-hook",
        }
        monkeypatch.setitem(sys.modules, "istota.skills.widget", mod)
        return cfg

    def test_the_manifest_value_wins(self, config):
        env = _run_task(config)
        assert env["WIDGET_ROOT"] == "from-the-manifest"

    def test_a_hook_var_the_manifest_does_not_name_still_arrives(self, config):
        """The control. Without it the assertion above passes just as well
        against a merge that discards every hook variable."""
        env = _run_task(config)
        assert env["WIDGET_HOOK_ONLY"] == "from-the-hook"

    @pytest.mark.parametrize("skill", ["developer", "code_review"])
    def test_no_shipped_manifest_resolves_the_variable_itself(self, tmp_path, skill):
        """The consequence, on both manifests that name it.

        Two skills declare `DEVELOPER_REPOS_DIR`, and a `from: config` entry in
        either is enough to shadow the hook for every task — `build_skill_env`
        resolves over all authorized skills into one dict. So the property is
        per manifest, not per skill.
        """
        from istota.skills._loader import load_skill_index

        overrides = tmp_path / "no_overrides"
        overrides.mkdir(exist_ok=True)
        specs = {s.var: s for s in load_skill_index(overrides)[skill].env_specs}
        assert specs["DEVELOPER_REPOS_DIR"].source == "setup_env"
