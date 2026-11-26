"""`skills.plugins` and `skills.plugin_imports`: what doctor says about skill plugins.

Spec: external-skill-packages, Stage 4. Every case uses the installed fixture,
since doctor reads the real entry points and passes no injection. The import
probe's child sees the fixture only through the test's own `PYTHONPATH`, which
is one of the four names its allowlist forwards.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from istota import doctor
from istota.config import Config
from istota.doctor import (
    CHECK_SCOPES,
    CHECKS,
    DEEP_CHECKS,
    DEPLOYMENT,
    FAIL,
    OK,
    PLUGIN_IMPORT_ENV,
    SKIP,
    WARN,
    check_skill_plugin_imports,
    check_skill_plugins,
    run_checks,
)
from istota.lib.skill_module import normalize_dist_name, wheel_dist_name
from tests.support.plugin_fixture import build_plugins


@pytest.fixture
def plugin_builder(tmp_path):
    yield from build_plugins(tmp_path)


def _by_name(results) -> dict:
    return {r.name: r for r in results}


def _wheel_for(plugin) -> str:
    return f"{plugin.dist_name.replace('-', '_')}-{plugin.version}-py3-none-any.whl"


class TestTheLoadedList:
    def test_none_installed_is_ok_and_says_so(self, tmp_path):
        results = check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent")
        assert [(r.name, r.status, r.detail) for r in results] == [
            ("skills.plugins", OK, "none installed"),
        ]

    def test_one_plugin_is_one_line_naming_its_distribution_and_package(self, plugin_builder, tmp_path):
        plugin = plugin_builder.installed()
        results = _by_name(check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent"))
        assert results["skills.plugins"].status == OK
        assert results["skills.plugins"].detail == (
            f"{plugin.skill}: {plugin.dist_name} 0.1.0 ({plugin.package})"
        )
        assert set(results) == {"skills.plugins"}

    def test_a_discovery_that_raises_is_one_fail_naming_it(self, monkeypatch, tmp_path):
        def _broken(entry_points=None):
            raise OSError("metadata unreadable")

        monkeypatch.setattr("istota.skills._loader.discover_plugin_skills", _broken)
        results = check_skill_plugins(Config(), True, wheel_dir=tmp_path)
        assert len(results) == 1
        assert results[0].name == "skills.plugins"
        assert results[0].status == FAIL
        assert "OSError: metadata unreadable" in results[0].detail
        assert results[0].remedy


class TestProblems:
    def test_a_clash_with_a_bundled_skill_fails(self, plugin_builder, tmp_path):
        plugin = plugin_builder.installed(skill="kv")
        results = _by_name(check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent"))
        problems = results["skills.plugins.problems"]
        assert problems.status == FAIL
        assert f"kv ({plugin.dist_name} 0.1.0): clashes_with_bundled" in problems.detail
        assert problems.remedy
        # The bundled skill is what loaded, so no plugin is in the list.
        assert results["skills.plugins"].detail == "none loaded"

    def test_a_duplicate_fails_once_per_distribution(self, plugin_builder, tmp_path):
        first = plugin_builder.installed()
        second = plugin_builder.installed(skill=first.skill)
        problems = _by_name(
            check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent")
        )["skills.plugins.problems"]
        assert problems.status == FAIL
        assert f"{first.skill} ({first.dist_name} 0.1.0): duplicate_plugin" in problems.detail
        assert f"{first.skill} ({second.dist_name} 0.1.0): duplicate_plugin" in problems.detail
        assert problems.detail.startswith("2 installed plugin(s)")


class TestTheWheelComparison:
    def test_a_wheel_matching_no_plugin_fails(self, plugin_builder, tmp_path):
        plugin = plugin_builder.installed()
        wheels = tmp_path / "src"
        wheels.mkdir()
        (wheels / _wheel_for(plugin)).write_bytes(b"")
        (wheels / "plugin_example_other-0.2.0-py3-none-any.whl").write_bytes(b"")
        (wheels / "README.txt").write_text("not a wheel")

        result = _by_name(check_skill_plugins(Config(), True, wheel_dir=wheels))["skills.plugins.wheels"]
        assert result.status == FAIL
        assert "plugin_example_other-0.2.0-py3-none-any.whl" in result.detail
        assert _wheel_for(plugin) not in result.detail
        assert "README.txt" not in result.detail
        assert result.remedy

    def test_every_wheel_loaded_is_ok(self, plugin_builder, tmp_path):
        plugin = plugin_builder.installed()
        wheels = tmp_path / "src"
        wheels.mkdir()
        (wheels / _wheel_for(plugin)).write_bytes(b"")
        result = _by_name(check_skill_plugins(Config(), True, wheel_dir=wheels))["skills.plugins.wheels"]
        assert result.status == OK

    def test_a_wheel_behind_a_problem_is_not_reported_twice(self, plugin_builder, tmp_path):
        plugin = plugin_builder.installed(skill="kv")
        wheels = tmp_path / "src"
        wheels.mkdir()
        (wheels / _wheel_for(plugin)).write_bytes(b"")
        results = _by_name(check_skill_plugins(Config(), True, wheel_dir=wheels))
        assert results["skills.plugins.problems"].status == FAIL
        assert results["skills.plugins.wheels"].status == OK

    def test_no_result_when_the_directory_is_absent(self, plugin_builder, tmp_path):
        plugin_builder.installed()
        results = _by_name(check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent"))
        assert "skills.plugins.wheels" not in results

    def test_the_default_directory_is_the_stack_bind(self):
        assert str(doctor.PLUGIN_WHEEL_DIR) == "/app/plugins/src"

    @pytest.mark.parametrize("raw,expected", [
        ("Example-Plugin", "example-plugin"),
        ("example_plugin", "example-plugin"),
        ("example.__plugin", "example-plugin"),
    ])
    def test_distribution_names_normalize(self, raw, expected):
        assert normalize_dist_name(raw) == expected

    @pytest.mark.parametrize("filename,expected", [
        ("example_plugin-0.1.0-py3-none-any.whl", "example-plugin"),
        ("Example.Plugin-1.0-1-py3-none-any.whl", "example-plugin"),
        ("noversion.whl", None),
        ("example_plugin-0.1.0.tar.gz", None),
    ])
    def test_a_wheel_filename_names_its_distribution(self, filename, expected):
        assert wheel_dist_name(filename) == expected


class TestTheEntryWarning:
    def test_cli_without_main_warns(self, plugin_builder, tmp_path):
        plugin = plugin_builder.installed(with_main=False)
        result = _by_name(check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent"))["skills.plugins.entry"]
        assert result.status == WARN
        assert plugin.skill in result.detail
        assert result.remedy

    def test_a_plugin_with_main_does_not(self, plugin_builder, tmp_path):
        plugin_builder.installed()
        results = _by_name(check_skill_plugins(Config(), True, wheel_dir=tmp_path / "absent"))
        assert "skills.plugins.entry" not in results


class TestTheImportProbe:
    def test_none_installed_is_ok(self):
        result = check_skill_plugin_imports(Config(), True)
        assert (result.name, result.status, result.detail) == (
            "skills.plugin_imports", OK, "none installed",
        )

    def test_a_package_that_raises_on_import_warns_and_one_that_imports_is_ok(
        self, plugin_builder, monkeypatch,
    ):
        good = plugin_builder.installed()
        bad = plugin_builder.installed(init_raises=True)
        monkeypatch.setenv("PYTHONPATH", f"{good.site}:{bad.site}")
        results = _by_name(check_skill_plugin_imports(Config(), True))

        assert results[f"skills.plugin_imports.{good.skill}"].status == OK
        failed = results[f"skills.plugin_imports.{bad.skill}"]
        assert failed.status == WARN
        assert "RuntimeError: example plugin refused to import" in failed.detail
        assert failed.remedy

    def test_the_child_gets_the_allowlist_and_never_the_master_key(self, plugin_builder, monkeypatch):
        plugin = plugin_builder.installed()
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "example-master-key-value")
        monkeypatch.setenv("PYTHONPATH", str(plugin.site))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        seen = []

        def _run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(doctor.subprocess, "run", _run)
        result = check_skill_plugin_imports(Config(), True)[0]

        assert result.status == OK
        argv, kwargs = seen[0]
        assert argv == [sys.executable, "-P", "-c", f"import {plugin.package}"]
        assert kwargs["timeout"] == doctor.PLUGIN_IMPORT_TIMEOUT
        env = kwargs["env"]
        assert set(env) <= set(PLUGIN_IMPORT_ENV)
        assert "ISTOTA_SECRET_KEY" not in env
        assert env["PYTHONPATH"] == str(plugin.site)
        assert env["PATH"] == "/usr/bin:/bin"

    def test_probe_off_spawns_nothing(self, plugin_builder, monkeypatch):
        plugin_builder.installed()

        def _explodes(*args, **kwargs):
            raise AssertionError("probe=False must spawn nothing")

        monkeypatch.setattr(doctor.subprocess, "run", _explodes)
        result = check_skill_plugin_imports(Config(), False)
        assert result.status == SKIP

    def test_a_clashing_plugin_is_not_imported(self, plugin_builder, monkeypatch):
        plugin_builder.installed(skill="kv")
        monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
        result = check_skill_plugin_imports(Config(), True)
        assert result.detail == "none installed"


class TestTheRegistry:
    def test_both_are_registered_with_a_deployment_scope(self):
        names = {name for name, _ in CHECKS}
        assert {"skills.plugins", "skills.plugin_imports"} <= names
        assert CHECK_SCOPES["skills.plugins"] == DEPLOYMENT
        assert CHECK_SCOPES["skills.plugin_imports"] == DEPLOYMENT

    def test_only_the_import_probe_is_deep(self):
        assert "skills.plugin_imports" in DEEP_CHECKS
        assert "skills.plugins" not in DEEP_CHECKS

    def test_a_run_without_deep_imports_nothing(self, plugin_builder, monkeypatch):
        plugin_builder.installed()
        monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
        names = {r.name for r in run_checks(Config(), only=("skills.",))}
        assert "skills.plugins" in names
        assert not any(n.startswith("skills.plugin_imports") for n in names)

    def test_a_deep_run_reaches_the_probe(self, plugin_builder, monkeypatch):
        plugin = plugin_builder.installed()
        monkeypatch.setenv("PYTHONPATH", str(plugin.site))
        results = _by_name(run_checks(Config(), only=("skills.",), deep=True))
        assert results[f"skills.plugin_imports.{plugin.skill}"].status == OK
