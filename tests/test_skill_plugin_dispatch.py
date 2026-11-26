"""Skill plugins at the four dispatch sites, and the boot purge.

Spec: external-skill-packages, Stage 2. Every place that runs a skill asks one
resolver which package holds it: the skill proxy (through the `skill_modules`
dict `build_task_runtime` hands it), `skill_client._run_direct` (no index, so
`lib/skill_module.resolve_unindexed_skill_module`), the scheduler's skill-task
path and `dispatch_setup_env_hooks`. The credential cases (no private fd for a
plugin, no lookup of its variables) are Stage 3's.
"""

import base64
import hashlib
import inspect
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, SchedulerConfig, UserConfig
from istota.lib import skill_module
from istota.lib.skill_module import resolve_unindexed_skill_module
from istota.rooms import egress
from istota.sandbox import skill_proxy
from istota.sandbox.skill_proxy import SkillProxy
from istota.skills._env import EnvContext, dispatch_setup_env_hooks
from istota.skills._loader import load_skill_index
from istota.skills._types import SkillMeta
from tests.support.plugin_fixture import SENTINEL_ENV, build_plugins

REPO = Path(__file__).resolve().parent.parent
BUNDLED = REPO / "src" / "istota" / "skills"


@pytest.fixture
def plugin_builder(tmp_path):
    yield from build_plugins(tmp_path)


@pytest.fixture
def sock_path():
    """A socket path short enough for AF_UNIX on macOS."""
    d = tempfile.mkdtemp(prefix="pd_", dir="/tmp")
    p = Path(d) / "s.sock"
    yield p
    p.unlink(missing_ok=True)
    Path(d).rmdir()


def _request(sock_path, payload):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(30)
    sock.connect(str(sock_path))
    sock.sendall((json.dumps(payload) + "\n").encode())
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    sock.close()
    return json.loads(b"".join(chunks).decode().strip())


def _pythonpath(*sites):
    return os.pathsep.join([str(REPO / "src"), *(str(s) for s in sites)])


class TestTheUnindexedResolver:
    """`_run_direct` loads no config, so it cannot ask the index."""

    def test_a_bundled_name_is_the_bundled_package(self):
        assert resolve_unindexed_skill_module("kv", BUNDLED) == "istota.skills.kv"

    def test_an_installed_plugin_is_its_package(self, plugin_builder):
        plugin = plugin_builder.installed()
        assert resolve_unindexed_skill_module(plugin.skill, BUNDLED) == plugin.package

    def test_an_unknown_name_falls_back_to_the_bundled_spelling(self):
        assert resolve_unindexed_skill_module("no_such_skill", BUNDLED) == "istota.skills.no_such_skill"

    def test_a_bundled_name_wins_over_a_plugin_of_the_same_name(self, plugin_builder):
        plugin_builder.installed(skill="kv")
        assert resolve_unindexed_skill_module("kv", BUNDLED) == "istota.skills.kv"

    def test_two_plugins_of_one_name_resolve_to_neither(self, plugin_builder):
        first = plugin_builder.installed()
        plugin_builder.installed(skill=first.skill)
        assert resolve_unindexed_skill_module(first.skill, BUNDLED) == f"istota.skills.{first.skill}"

    def test_a_reserved_name_is_never_a_plugin(self, plugin_builder):
        plugin_builder.installed(skill="room_notes")
        assert resolve_unindexed_skill_module("room_notes", BUNDLED) == "istota.skills.room_notes"

    @pytest.mark.parametrize("name", ["../kv", "Kv", "a/b", ""])
    def test_a_name_that_is_no_skill_name_touches_nothing(self, name, tmp_path):
        (tmp_path / "kv").mkdir()
        (tmp_path / "kv" / "skill.md").write_text("---\n---\n")
        assert resolve_unindexed_skill_module(name, tmp_path / "x") == f"istota.skills.{name}"

    def test_the_leaf_imports_nothing_from_istota(self):
        probe = (
            "import json, sys\n"
            "import istota.lib.skill_module\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('istota'))))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")}, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["istota", "istota.lib", "istota.lib.skill_module"]


class TestTheProxy:
    def test_a_plugin_runs_from_its_own_package(self, plugin_builder, sock_path):
        plugin = plugin_builder.installed(activate=False)
        base_env = {"PATH": os.environ["PATH"], "PYTHONPATH": _pythonpath(plugin.site)}
        with SkillProxy(
            sock_path, {}, base_env,
            allowed_skills=frozenset({plugin.skill}),
            skill_modules={plugin.skill: plugin.package},
        ):
            response = _request(sock_path, {"skill": plugin.skill, "args": ["ping"]})
        assert response["returncode"] == 0, response["stderr"]
        assert json.loads(response["stdout"])["status"] == "ok"

    def test_an_allowed_skill_missing_from_the_dict_is_refused_unspawned(
        self, sock_path, monkeypatch,
    ):
        spawned = []
        monkeypatch.setattr(skill_proxy, "run_capture", lambda *a, **k: spawned.append(a))
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin"},
            allowed_skills=frozenset({"kv", "example"}),
            skill_modules={"kv": "istota.skills.kv"},
        ):
            response = _request(sock_path, {"skill": "example", "args": ["ping"]})
        assert response["reason"] == "unknown_skill"
        assert response["returncode"] == 1
        assert spawned == []

    def test_no_dict_means_the_bundled_package(self, sock_path, monkeypatch):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr(skill_proxy, "run_capture", fake_run)
        with SkillProxy(sock_path, {}, {"PATH": "/usr/bin"}, allowed_skills=frozenset({"kv"})):
            _request(sock_path, {"skill": "kv", "args": ["namespaces"]})
        assert seen and seen[0][1:4] == ["-m", "istota.skills.kv", "namespaces"]


class TestNoCredentialChannelForAPlugin:
    """The private fd serves vault reads past the reveal policy, wallet cards,
    OTP codes and mail account passwords. A plugin gets none of it (Stage 3)."""

    def test_a_plugin_sees_no_fd_and_the_same_code_outside_plugin_skills_does(
        self, plugin_builder, sock_path,
    ):
        # Two copies of one package through one proxy: the only difference is
        # membership of `plugin_skills`, so the second half is the control
        # that shows the first half can fail.
        plugin = plugin_builder.installed(activate=False)
        bundled_style = plugin_builder.installed(activate=False)
        base_env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": _pythonpath(plugin.site, bundled_style.site),
        }
        with SkillProxy(
            sock_path, {}, base_env,
            allowed_skills=frozenset({plugin.skill, bundled_style.skill}),
            skill_modules={
                plugin.skill: plugin.package, bundled_style.skill: bundled_style.package,
            },
            plugin_skills=frozenset({plugin.skill}),
        ):
            as_plugin = _request(sock_path, {"skill": plugin.skill, "args": ["ping"]})
            as_bundled = _request(sock_path, {"skill": bundled_style.skill, "args": ["ping"]})
        assert as_plugin["returncode"] == 0, as_plugin["stderr"]
        assert as_bundled["returncode"] == 0, as_bundled["stderr"]
        assert json.loads(as_plugin["stdout"]) == {"status": "ok", "cred_fd": False}
        assert json.loads(as_bundled["stdout"]) == {"status": "ok", "cred_fd": True}

    def test_no_fd_is_passed_and_the_plugins_own_credential_still_is(
        self, sock_path, monkeypatch,
    ):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr(skill_proxy, "run_capture", fake_run)
        with SkillProxy(
            sock_path,
            {"PLUGIN_EXAMPLE_TOKEN": "plugin-placeholder", "KV_TOKEN": "kv-placeholder"},
            # A stray value in the base env must not reach a plugin either.
            {"PATH": "/usr/bin", "ISTOTA_CRED_FD": "7"},
            skill_credential_map={"example": {"PLUGIN_EXAMPLE_TOKEN"}, "kv": {"KV_TOKEN"}},
            allowed_skills=frozenset({"example", "kv"}),
            skill_modules={"example": "example_plugin", "kv": "istota.skills.kv"},
            plugin_skills=frozenset({"example"}),
        ):
            _request(sock_path, {"skill": "example", "args": ["ping"]})
            _request(sock_path, {"skill": "kv", "args": ["namespaces"]})
        (plugin_cmd, plugin_call), (_, bundled_call) = calls
        assert plugin_cmd[1:3] == ["-m", "example_plugin"]
        assert plugin_call["pass_fds"] == ()
        assert "ISTOTA_CRED_FD" not in plugin_call["env"]
        assert plugin_call["env"]["PLUGIN_EXAMPLE_TOKEN"] == "plugin-placeholder"
        assert "KV_TOKEN" not in plugin_call["env"]
        assert len(bundled_call["pass_fds"]) == 1
        assert bundled_call["env"]["ISTOTA_CRED_FD"] == str(bundled_call["pass_fds"][0])


def _secret_meta(name, var, service, *, module="", origin="bundled"):
    from istota.skills._types import EnvSpec

    return SkillMeta(
        name=name, description="x", cli=True, module=module, origin=origin,
        env_specs=[EnvSpec(var=var, source="secret", service=service, key="token", sensitive=True)],
    )


class TestTheLookupAllowlist:
    """`istota-credential env NAME` never serves a plugin's variable, broker on
    or off; the plugin's own CLI still gets it injected (Stage 3)."""

    def _index(self, origin="plugin"):
        return {
            "bookmarks": _secret_meta("bookmarks", "KARAKEEP_API_KEY", "karakeep"),
            "example": _secret_meta(
                "example", "PLUGIN_EXAMPLE_TOKEN", "plugin_example",
                module="example_plugin", origin=origin,
            ),
        }

    def test_a_plugins_variable_is_not_fetchable_by_name(self):
        from istota.executor import derive_lookup_allowlist

        assert derive_lookup_allowlist(["bookmarks", "example"], self._index()) == {"KARAKEEP_API_KEY"}

    def test_nor_under_an_operator_entry_over_the_plugin(self):
        from istota.executor import derive_lookup_allowlist

        index = self._index(origin="operator")
        assert derive_lookup_allowlist(["bookmarks", "example"], index) == {"KARAKEEP_API_KEY"}

    def test_the_plugins_cli_still_gets_its_variable(self):
        from istota.executor import derive_skill_credential_map

        assert derive_skill_credential_map(["example"], self._index()) == {
            "example": {"PLUGIN_EXAMPLE_TOKEN"},
        }


class TestTheDirectRun:
    def test_a_plugin_runs_with_no_proxy(self, plugin_builder):
        plugin = plugin_builder.installed(activate=False)
        env = {
            k: v for k, v in os.environ.items()
            if k not in {"ISTOTA_SKILL_PROXY_SOCK", "ISTOTA_SANDBOXED"}
        }
        env["PYTHONPATH"] = _pythonpath(plugin.site)
        result = subprocess.run(
            [sys.executable, "-m", "istota.skill_client", plugin.skill, "ping"],
            capture_output=True, text=True, env=env, cwd=REPO, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "ok"


class TestTheSkillTask:
    def _config(self, db_path, tmp_path):
        for name in ("mount", "temp"):
            (tmp_path / name).mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            workspace_path=tmp_path / "mount",
            temp_dir=tmp_path / "temp",
            skills_dir=tmp_path / "operator-skills",
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            users={"alice": UserConfig()},
        )

    def test_a_plugin_skill_task_runs_its_package(self, plugin_builder, db_path, tmp_path):
        from istota.scheduler import _execute_skill_task

        plugin = plugin_builder.installed()
        config = self._config(db_path, tmp_path)
        task = db.Task(
            id=1, status="running", source_type="talk", user_id="alice", prompt="",
            skill=plugin.skill, skill_args='["ping"]',
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout='{"status": "ok"}', stderr="")

        with patch("istota.scheduler._run_capture", side_effect=fake_run), \
                patch("istota.scheduler.discover_calendars_for_task", return_value=[]):
            success, result = _execute_skill_task(task, config)
        assert success is True, result
        assert captured["cmd"] == [sys.executable, "-m", plugin.package, "ping"]


def _ctx(tmp_path):
    return EnvContext(
        config=Config(db_path=tmp_path / "db.sqlite", temp_dir=tmp_path / "temp"),
        task=MagicMock(user_id="alice"),
        user_resources=[],
        user_config=None,
        user_temp_dir=tmp_path,
        is_admin=True,
    )


def _index(tmp_path, *entry_points, operator_skills=None):
    """The index over an empty bundled layer, so only the plugins dispatch."""
    bundled = tmp_path / "bundled"
    bundled.mkdir(exist_ok=True)
    operator = tmp_path / "operator"
    operator.mkdir(exist_ok=True)
    for name, text in (operator_skills or {}).items():
        (operator / name).mkdir()
        (operator / name / "skill.md").write_text(text)
    return load_skill_index(operator, bundled, plugin_entry_points=list(entry_points))


class TestTheSetupEnvHooks:
    def test_a_plugin_hook_contributes_only_what_its_manifest_declares(
        self, plugin_builder, tmp_path, caplog,
    ):
        skill = "example_hooked"
        var = "PLUGIN_EXAMPLE_HOOKED_DIR"
        plugin = plugin_builder.injected(
            skill=skill,
            env=[{"var": var, "from": "setup_env"}],
            setup_env={var: "/tmp/example", "NC_URL": "https://example.invalid", "KV_DB": "x"},
        )
        index = _index(tmp_path, plugin.entry_point)
        with caplog.at_level(logging.WARNING, logger="istota.skills_env"):
            env = dispatch_setup_env_hooks([], index, _ctx(tmp_path))
            again = dispatch_setup_env_hooks([], index, _ctx(tmp_path))
        assert env == {var: "/tmp/example"}
        assert again == env
        dropped = [r for r in caplog.records if skill in r.getMessage() and "NC_URL" in r.getMessage()]
        assert len(dropped) == 1

    def test_an_operator_entry_over_a_plugin_still_reaches_the_plugin_hook(
        self, plugin_builder, tmp_path,
    ):
        skill = "example_overlaid"
        var = "PLUGIN_EXAMPLE_OVERLAID_DIR"
        plugin = plugin_builder.injected(
            skill=skill, env=[{"var": var, "from": "setup_env"}], setup_env={var: "v"},
        )
        operator_md = (
            "---\ndescription: Operator doc\ncli: true\n"
            f'env: [{{"var":"{var}","from":"setup_env"}}]\n---\nOperator body\n'
        )
        index = _index(tmp_path, plugin.entry_point, operator_skills={skill: operator_md})
        assert index[skill].origin == "operator"
        assert dispatch_setup_env_hooks([], index, _ctx(tmp_path)) == {var: "v"}

    def test_a_plugin_that_raises_at_import_is_skipped_once(
        self, plugin_builder, tmp_path, caplog,
    ):
        broken = plugin_builder.injected(init_raises=True)
        var = "PLUGIN_EXAMPLE_GOOD_DIR"
        good = plugin_builder.injected(
            skill="example_good", env=[{"var": var, "from": "setup_env"}], setup_env={var: "ok"},
        )
        index = _index(tmp_path, broken.entry_point, good.entry_point)
        assert broken.skill in index
        with caplog.at_level(logging.ERROR, logger="istota.skills_env"):
            first = dispatch_setup_env_hooks([], index, _ctx(tmp_path))
            second = dispatch_setup_env_hooks([], index, _ctx(tmp_path))
        assert first == second == {var: "ok"}
        errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and broken.skill in r.getMessage()
        ]
        assert len(errors) == 1
        assert broken.package not in sys.modules

    def test_the_broken_plugin_s_code_ran_once(self, plugin_builder, tmp_path, monkeypatch):
        sentinel = tmp_path / "sentinel"
        monkeypatch.setenv(SENTINEL_ENV, str(sentinel))
        broken = plugin_builder.injected(init_raises=True)
        index = _index(tmp_path, broken.entry_point)
        dispatch_setup_env_hooks([], index, _ctx(tmp_path))
        dispatch_setup_env_hooks([], index, _ctx(tmp_path))
        assert sentinel.read_text() == "imported\n"

    def test_a_bundled_module_raising_at_import_still_propagates(self, tmp_path, monkeypatch):
        import importlib

        real = importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name == "istota.skills.zz_broken":
                raise RuntimeError("bundled import failed")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(importlib, "import_module", fake_import)
        index = {"zz_broken": SkillMeta(name="zz_broken", description="x", skill_dir=str(tmp_path))}
        with pytest.raises(RuntimeError, match="bundled import failed"):
            dispatch_setup_env_hooks([], index, _ctx(tmp_path))


def _job(conn, name, skill):
    conn.execute(
        "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, skill, "
        "skill_args, enabled) VALUES ('alice', ?, '*/5 * * * *', '', ?, '[]', 1)",
        (name, skill),
    )


def _pending_task(conn, skill):
    return db.create_task(
        conn, prompt="", user_id="alice", source_type="scheduled",
        skill=skill, skill_args="[]", queue="background",
    )


class TestTheBootPurge:
    def _conn(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_a_broken_plugin_keeps_its_jobs_and_an_absent_one_loses_them(self, tmp_path):
        from istota.scheduler import _purge_obsolete_skill_jobs

        conn = self._conn(tmp_path)
        _job(conn, "kept job", "example_broken")
        _job(conn, "gone job", "example_gone")
        kept_task = _pending_task(conn, "example_broken")
        gone_task = _pending_task(conn, "example_gone")
        for name in ("kept job", "gone job"):
            egress.add_grant(conn, "alice", egress.cron_subject(name), "example.invalid", decided_via="web")
        conn.commit()

        _purge_obsolete_skill_jobs(conn, {}, keep=frozenset({"example_broken"}))

        names = {r["name"] for r in conn.execute("SELECT name FROM scheduled_jobs")}
        assert names == {"kept job"}
        status = dict(conn.execute("SELECT id, status FROM tasks").fetchall())
        assert status[kept_task] == "pending"
        assert status[gone_task] == "failed"
        assert egress.grants_for(conn, "alice", egress.cron_subject("kept job")) == {"example.invalid"}
        # The documented cost: an absent plugin's job takes its approvals with it.
        assert egress.grants_for(conn, "alice", egress.cron_subject("gone job")) == frozenset()

    def test_the_startup_purge_keeps_a_problem_plugin_s_jobs(
        self, plugin_builder, tmp_path, db_path, caplog,
    ):
        from istota.scheduler import _purge_skill_jobs_at_startup

        broken = plugin_builder.installed(extra={"always_include": True})
        with db.get_db(db_path) as conn:
            _job(conn, "plugin job", broken.skill)
            _job(conn, "orphan job", "example_never_installed")
        config = Config(db_path=db_path, skills_dir=tmp_path / "operator-skills")
        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            _purge_skill_jobs_at_startup(config)
        with db.get_db(db_path) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM scheduled_jobs")}
        assert names == {"plugin job"}
        problems = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and broken.skill in r.getMessage()
            and "forbidden_manifest_field" in r.getMessage()
        ]
        assert len(problems) == 1

    def test_a_discovery_failure_does_not_stop_the_purge(
        self, tmp_path, db_path, monkeypatch, caplog,
    ):
        from istota.scheduler import _purge_skill_jobs_at_startup
        from istota.skills import _loader

        def broken(*args, **kwargs):
            raise RuntimeError("entry points unreadable")

        monkeypatch.setattr(_loader, "plugin_problems", broken)
        with db.get_db(db_path) as conn:
            _job(conn, "orphan job", "example_never_installed")
        config = Config(db_path=db_path, skills_dir=tmp_path / "operator-skills")
        with caplog.at_level(logging.ERROR, logger="istota.scheduler"):
            _purge_skill_jobs_at_startup(config)
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0] == 0
        assert any("entry points unreadable" in r.getMessage() for r in caplog.records)


def _dist_info(tree: Path, name: str, *, entry_points: str | None) -> Path:
    """A `.dist-info` as `uv pip install --target` leaves one, with no package."""
    tree.mkdir(parents=True, exist_ok=True)
    dist_info = tree / f"{name.replace('-', '_')}-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n")
    if entry_points is not None:
        (dist_info / "entry_points.txt").write_text(entry_points)
    (dist_info / "RECORD").write_text(f"{dist_info.name}/RECORD,,\n")
    return tree


class TestTheTreeCheck:
    """`check_tree`, which `istota-stack plugins sync` runs on the tree it has
    just installed, before that tree becomes the one the venv reads (Stage 5).
    It refuses the shadowing discovery cannot see: a distribution or a package
    the image's venv already has wins over a plugin appended after it."""

    def _bad_trees(self, tmp_path) -> dict[str, Path]:
        tag = secrets.token_hex(4)
        return {
            "no-entry-point": _dist_info(tmp_path / "no-ep", f"example-library-{tag}", entry_points=None),
            "installed-name": _dist_info(
                tmp_path / "installed", "pytest",
                entry_points=f"[istota.skills]\nexample_{tag} = example_{tag}\n"),
            "importable-package": _dist_info(
                tmp_path / "importable", f"example-plugin-{tag}",
                entry_points=f"[istota.skills]\nexample_{tag} = json\n"),
        }

    def test_a_clean_tree_passes(self, plugin_builder):
        plugin = plugin_builder.installed(activate=False)
        assert skill_module.check_tree(plugin.site) == []

    @pytest.mark.parametrize(("case", "names"), [
        ("no-entry-point", ("example-library", "istota.skills")),
        ("installed-name", ("pytest",)),
        ("importable-package", ("json",)),
    ])
    def test_each_shadowing_case_is_one_reason(self, tmp_path, case, names):
        reasons = skill_module.check_tree(self._bad_trees(tmp_path)[case])
        assert len(reasons) == 1, reasons
        for name in names:
            assert name in reasons[0], reasons

    def test_the_artifact_tiers_wheel_unpacks_to_a_clean_tree(self, plugin_builder, tmp_path):
        """The wheel the image and vm tiers install: its filename, a RECORD whose
        hashes match, and a tree `check_tree` passes once unpacked."""
        wheel = plugin_builder.wheel(tmp_path / "wheels")
        assert wheel.name == "example_plugin-0.1.0-py3-none-any.whl"
        unpacked = tmp_path / "unpacked"
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(unpacked)
        record = (unpacked / "example_plugin-0.1.0.dist-info" / "RECORD").read_text().splitlines()
        hashed = [line.rsplit(",", 2) for line in record if not line.endswith(",,")]
        assert {name for name, _, _ in hashed} >= {"example_plugin/__init__.py", "example_plugin/skill.md"}
        for name, digest, size in hashed:
            data = (unpacked / name).read_bytes()
            expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert (digest, int(size)) == (f"sha256={expected}", len(data)), name
        assert skill_module.check_tree(unpacked) == []
        assert "example = example_plugin" in (
            unpacked / "example_plugin-0.1.0.dist-info" / "entry_points.txt").read_text()

    def test_the_synced_tree_the_venv_reads_is_not_the_venv(self, plugin_builder, tmp_path):
        """A re-sync installs plugins the current `site` already holds, and that
        tree is on `sys.path` through the `.pth` while the one-shot checks the
        next one. Counted as the venv, every re-sync would refuse itself."""
        current = plugin_builder.installed()
        upgrade = tmp_path / "next"
        shutil.copytree(current.site, upgrade)
        assert len(skill_module.check_tree(upgrade, ignore=())) == 2
        assert skill_module.check_tree(upgrade, ignore=(str(current.site),)) == []

    def test_the_default_exclusion_is_the_trees_place_in_the_image(self):
        assert skill_module.PLUGIN_SITE_DIR == "/app/plugins/installed/site"
        default = inspect.signature(skill_module.check_tree).parameters["ignore"].default
        assert default == (skill_module.PLUGIN_SITE_DIR,)

    def test_a_missing_tree_is_a_reason(self, tmp_path):
        reasons = skill_module.check_tree(tmp_path / "absent")
        assert len(reasons) == 1 and "not a directory" in reasons[0], reasons

    def _main(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-P", "-m", "istota.lib.skill_module", *args],
            capture_output=True, text=True, cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")}, timeout=60,
        )

    def test_the_command_exits_0_on_a_clean_tree(self, plugin_builder):
        plugin = plugin_builder.installed(activate=False)
        result = self._main("check-tree", str(plugin.site))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "" and result.stderr == ""

    @pytest.mark.parametrize("case", ["no-entry-point", "installed-name", "importable-package"])
    def test_the_command_exits_1_with_one_line_per_reason(self, tmp_path, case):
        result = self._main("check-tree", str(self._bad_trees(tmp_path)[case]))
        assert result.returncode == 1, result.stdout + result.stderr
        assert len(result.stderr.splitlines()) == 1, result.stderr

    @pytest.mark.parametrize("args", [(), ("check-tree",), ("check", "/tmp"), ("check-tree", "a", "b")])
    def test_anything_else_is_a_usage_error(self, args):
        assert self._main(*args).returncode == 64
