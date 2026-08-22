"""Tests for the runtime self-check registry (`istota doctor`).

Two things are under test here and they pull in opposite directions.

The *checks* are assertions about a host, so each one is driven against
fabricated binaries in ``tmp_path`` rather than against whatever the machine
running the suite happens to have installed. A check that passed because the
developer had ``gh`` on their PATH would be asserting nothing.

The *registry* is the part every layer above doctor consumes as an oracle, so
its invariants get their own class: names unique, a result's name predictable
from its registry entry, ``only=`` selecting before invoking, ``probe=False``
spawning nothing, and a raising check reported rather than propagated.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from istota import doctor
from istota.doctor import (
    CHECKS,
    DEEP_CHECKS,
    DEPLOYMENT,
    FAIL,
    IMAGE,
    OK,
    SKIP,
    WARN,
    CheckResult,
    exit_code,
    render_json,
    render_text,
    run_checks,
)


def _fake_bin(path, output="", exit_code=0):
    """Write an executable shell script printing `output` and exiting `exit_code`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


def _by_name(results):
    return {r.name: r for r in results}


def _dev_config(make_config, tmp_path, **developer_overrides):
    """A Config with the developer skill fully wired — the shape that makes the
    `developer.*` checks actually run rather than SKIP."""
    from istota.config import DeveloperConfig

    repos = tmp_path / "repos"
    repos.mkdir(exist_ok=True)
    fields = {
        "enabled": True,
        "repos_dir": str(repos),
        "gitlab_token": "t" * 20,
        "gh_bin_path": str(tmp_path / "bin" / "gh"),
        "glab_bin_path": str(tmp_path / "bin" / "glab"),
    }
    fields.update(developer_overrides)
    return make_config(developer=DeveloperConfig(**fields))


class TestRegistry:
    """Invariants the layers above doctor depend on."""

    def test_names_are_unique(self):
        names = [name for name, _ in CHECKS]
        assert len(names) == len(set(names))

    def test_names_are_dotted_and_stable(self):
        for name, _ in CHECKS:
            assert "." in name, f"{name} is not a dotted id"
            assert name == name.strip()
            assert name.islower()

    def test_deep_checks_are_registered(self):
        names = {name for name, _ in CHECKS}
        assert DEEP_CHECKS <= names

    def test_every_result_name_matches_its_registry_entry(self, make_config, tmp_path):
        """`only=` filters on the registry name, so a result named something
        else is invisible to the caller that asked for it."""
        config = _dev_config(make_config, tmp_path)
        for name, _ in CHECKS:
            results = run_checks(config, only=(name,), deep=True)
            assert results, f"{name} produced no result"
            for r in results:
                assert r.name == name or r.name.startswith(name + "."), (
                    f"{name} returned a result named {r.name!r}"
                )

    def test_every_result_has_a_detail(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            assert r.detail.strip(), f"{r.name} returned an empty detail"

    def test_warn_and_fail_carry_a_remedy(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            if r.status in (WARN, FAIL):
                assert r.remedy.strip(), f"{r.name} is {r.status} with no remedy"

    def test_every_result_has_a_known_status_and_scope(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            assert r.status in (OK, WARN, FAIL, SKIP)
            assert r.scope in (IMAGE, DEPLOYMENT)

    def test_only_selects_before_invoking(self, make_config, tmp_path, monkeypatch):
        """Filtering after the fact would run every check to discard most."""
        called = []

        def _explodes(config, probe):
            called.append(1)
            raise AssertionError("this check must never be invoked")

        monkeypatch.setattr(
            doctor, "CHECKS", (("runtime.platform", doctor.check_platform), ("boom.check", _explodes))
        )
        results = run_checks(make_config(), only=("runtime.",))
        assert called == []
        assert [r.name for r in results] == ["runtime.platform"]

    def test_only_accepts_several_prefixes(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        results = run_checks(config, only=("developer.", "security.skill_proxy"))
        assert results
        for r in results:
            assert r.name.startswith("developer.") or r.name.startswith("security.skill_proxy")

    def test_empty_only_runs_everything_except_deep(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        names = {r.name.split(".")[0] + "." + r.name.split(".")[1] for r in run_checks(config)}
        assert not (names & DEEP_CHECKS)
        assert "runtime.platform" in names

    def test_deep_checks_run_only_when_asked(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        shallow = {r.name for r in run_checks(config, only=("sandbox.masks",), deep=False)}
        deep = {r.name for r in run_checks(config, only=("sandbox.masks",), deep=True)}
        assert shallow == set()
        assert deep == {"sandbox.masks"}

    def test_scope_filters(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, scope=IMAGE):
            assert r.scope == IMAGE
        for r in run_checks(config, scope=DEPLOYMENT):
            assert r.scope == DEPLOYMENT

    def test_every_registry_entry_declares_a_scope(self):
        assert {name for name, _ in CHECKS} == set(doctor.CHECK_SCOPES)
        assert set(doctor.CHECK_SCOPES.values()) <= {IMAGE, DEPLOYMENT}

    def test_a_checks_results_carry_its_registry_scope(self, make_config, tmp_path):
        """`scope=` selects on the registry entry, so a result whose own scope
        disagreed would be selected by one value and reported with another."""
        config = _dev_config(make_config, tmp_path)
        for name, _ in CHECKS:
            for r in run_checks(config, only=(name,), deep=True):
                assert r.scope == doctor.CHECK_SCOPES[name], f"{r.name} disagrees with {name}"

    def test_scope_selects_before_invoking(self, make_config, tmp_path, monkeypatch):
        """`--scope image` runs in a volume-less `docker run`, where the
        deployment-scoped checks would fail on a perfectly good image. Filtering
        afterwards would pay for them in order to discard them."""
        called = []

        def _deployment_check(config, probe):
            called.append(1)
            return CheckResult("dep.check", OK, "ran", scope=DEPLOYMENT)

        def _image_check(config, probe):
            return CheckResult("img.check", OK, "ran", scope=IMAGE)

        monkeypatch.setattr(
            doctor, "CHECKS", (("dep.check", _deployment_check), ("img.check", _image_check))
        )
        monkeypatch.setattr(
            doctor, "CHECK_SCOPES", {"dep.check": DEPLOYMENT, "img.check": IMAGE}
        )
        results = run_checks(make_config(), scope=IMAGE)
        assert called == []
        assert [r.name for r in results] == ["img.check"]

    def test_skip_excludes_before_invoking(self, make_config, monkeypatch):
        called = []

        def _expensive(config, probe):
            called.append(1)
            return CheckResult("runtime.framework_db", OK, "ran")

        monkeypatch.setattr(
            doctor,
            "CHECKS",
            (("runtime.framework_db", _expensive), ("runtime.platform", doctor.check_platform)),
        )
        results = run_checks(make_config(), skip=("runtime.framework_db",))
        assert called == []
        assert [r.name for r in results] == ["runtime.platform"]

    def test_skip_wins_over_only(self, make_config, monkeypatch):
        results = run_checks(
            make_config(), only=("runtime.",), skip=("runtime.framework_db",)
        )
        assert "runtime.framework_db" not in {r.name for r in results}
        assert "runtime.platform" in {r.name for r in results}

    def test_a_raising_check_becomes_fail(self, make_config, monkeypatch):
        """Doctor runs on the daemon start-up path; an exception there would
        turn a diagnostic into an outage."""

        def _raises(config, probe):
            raise RuntimeError("kaboom in the check")

        monkeypatch.setattr(doctor, "CHECKS", (("boom.check", _raises),))
        results = run_checks(make_config())
        assert len(results) == 1
        assert results[0].name == "boom.check"
        assert results[0].status == FAIL
        assert "kaboom in the check" in results[0].detail
        assert results[0].remedy

    def test_probe_false_spawns_no_subprocess(self, make_config, tmp_path, monkeypatch):
        """`_validate_forge_clis` calls this from `load_config`, which runs in
        every host-side skill-CLI subprocess. Five `--version` spawns per call
        is not a refactor, it is a regression.

        Counted with a spy rather than asserted by raising: `run_checks` catches
        every exception per check and turns it into a FAIL result, so a raising
        stub is swallowed and the test passes no matter what the checks do.
        """
        spawns = []

        def _spy(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            raise OSError("no subprocesses in this test")

        monkeypatch.setattr(subprocess, "run", _spy)
        monkeypatch.setattr(subprocess, "Popen", _spy)
        monkeypatch.setattr(subprocess, "check_output", _spy)
        config = _dev_config(make_config, tmp_path)
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        results = run_checks(config, probe=False, deep=True)
        assert spawns == [], f"probe=False spawned: {spawns}"
        # And nothing was quietly converted into a FAIL along the way, which is
        # how the raising version of this test hid its own failure.
        raised = [r for r in results if "the check itself raised" in r.detail]
        assert raised == [], [(r.name, r.detail) for r in raised]

    def test_the_spy_would_catch_a_spawn(self, make_config, tmp_path, monkeypatch):
        """Positive control for the test above.

        A guard that cannot fail is not a guard, and the previous version of
        this pair could not: it asserted by raising into a `try/except` that
        exists precisely to swallow. Register a check that spawns, and confirm
        the technique sees it.
        """
        spawns = []

        def _spy(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            raise OSError("no subprocesses in this test")

        monkeypatch.setattr(subprocess, "run", _spy)

        def _spawning_check(config, probe):
            subprocess.run(["/bin/true"], capture_output=True)
            return CheckResult("boom.spawns", OK, "should not get here")

        monkeypatch.setattr(doctor, "CHECKS", (("boom.spawns", _spawning_check),))
        run_checks(make_config(), probe=False)
        assert spawns != []

    def test_probe_false_is_named_in_the_detail(self, make_config, tmp_path):
        """An operator reading a probe=False result must be able to tell it was
        answered from the filesystem rather than by running anything."""
        config = _dev_config(make_config, tmp_path)
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        results = _by_name(run_checks(config, only=("developer.forge_binaries",), probe=False))
        assert "not executed" in results["developer.forge_binaries.gh"].detail


class TestConfigLoadPathStaysCheap:
    """The config-load path is the hot one, and two heavy imports crept onto it.

    `_validate_forge_clis` runs inside every `load_config`: the daemon, the web
    app, the webhook receiver, every CLI invocation, and every host-side skill
    CLI subprocess the skill proxy spawns *per call*. Reaching the forge
    resolution rule through `istota.skills.developer` pulled in the whole skill
    package (~190ms, because `istota.skills.__init__` star-imports every skill),
    and `web.static` reaching its path through `web_app` pulled in FastAPI,
    authlib and a second full `load_config()` (+56MB RSS).

    Asserted as import graphs rather than as timings: a wall-clock threshold on
    a shared laptop is a flaky test, and the thing that actually regressed is
    which module gets imported.
    """

    def _import_graph(self, module: str) -> set[str]:
        """Modules pulled in by importing `module` in a fresh interpreter."""
        code = (
            "import json, sys\n"
            f"import {module}\n"
            "print(json.dumps(sorted(sys.modules)))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        return set(json.loads(out.stdout))

    def test_forge_bin_is_a_stdlib_only_leaf(self):
        loaded = self._import_graph("istota.forge_bin")
        assert "istota.skills" not in loaded
        assert "istota.config" not in loaded

    def test_static_dir_is_a_stdlib_only_leaf(self):
        loaded = self._import_graph("istota.static_dir")
        assert "fastapi" not in loaded
        assert "istota.web_app" not in loaded
        assert "istota.config" not in loaded

    def test_the_forge_checks_do_not_import_the_skill_package(
        self, make_config, tmp_path, monkeypatch
    ):
        config = _dev_config(make_config, tmp_path)
        monkeypatch.delitem(sys.modules, "istota.skills", raising=False)
        monkeypatch.delitem(sys.modules, "istota.skills.developer", raising=False)
        run_checks(config, only=("developer.",), probe=False)
        assert "istota.skills" not in sys.modules

    def test_web_static_does_not_import_web_app(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html>")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        monkeypatch.delitem(sys.modules, "istota.web_app", raising=False)
        config = make_config(web=WebConfig(enabled=True))
        assert run_checks(config, only=("web.static",))[0].status == OK
        assert "istota.web_app" not in sys.modules

    def test_web_app_and_doctor_resolve_the_same_static_dir(self):
        """One implementation, two callers — the point of the leaf."""
        from istota import static_dir, web_app

        assert web_app._resolve_static_dir() == static_dir.resolve_static_dir()

    def test_developer_skill_and_doctor_resolve_the_same_binary(self):
        from istota import forge_bin
        from istota.skills import developer

        assert developer._resolve_real_bin is forge_bin.resolve_real_bin


class TestPlatform:
    def test_linux_is_ok(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
        r = run_checks(make_config(), only=("runtime.platform",))[0]
        assert r.status == OK
        assert "x86_64" in r.detail

    def test_non_linux_with_sandbox_enabled_fails(self, make_config, monkeypatch):
        from istota.config import SecurityConfig

        monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
        config = make_config(security=SecurityConfig(sandbox_enabled=True))
        r = run_checks(config, only=("runtime.platform",))[0]
        assert r.status == FAIL
        assert "Darwin" in r.detail

    def test_non_linux_without_sandbox_warns(self, make_config, monkeypatch):
        from istota.config import SecurityConfig

        monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
        config = make_config(security=SecurityConfig(sandbox_enabled=False))
        r = run_checks(config, only=("runtime.platform",))[0]
        assert r.status == WARN

    def test_scope_is_image(self, make_config):
        r = run_checks(make_config(), only=("runtime.platform",))[0]
        assert r.scope == IMAGE


class TestBwrap:
    def test_skips_when_sandbox_disabled(self, make_config):
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(sandbox_enabled=False))
        r = run_checks(config, only=("runtime.bwrap",))[0]
        assert r.status == SKIP

    def test_missing_bwrap_fails(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        r = run_checks(make_config(), only=("runtime.bwrap",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_present_and_runnable_is_ok(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "bwrap", "bubblewrap 0.8.0")
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None)
        r = run_checks(make_config(), only=("runtime.bwrap",))[0]
        assert r.status == OK

    def test_unrunnable_bwrap_fails(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "bwrap", "nope", exit_code=1)
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None)
        r = run_checks(make_config(), only=("runtime.bwrap",))[0]
        assert r.status == FAIL

    def test_probe_false_answers_from_the_filesystem(self, make_config, tmp_path, monkeypatch):
        """A binary that exists and is executable is OK without running it —
        even one that would exit non-zero."""
        fake = _fake_bin(tmp_path / "bin" / "bwrap", "nope", exit_code=1)
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "bwrap" else None)
        r = run_checks(make_config(), only=("runtime.bwrap",), probe=False)[0]
        assert r.status == OK


class TestModelCli:
    def test_skips_under_the_native_brain(self, make_config):
        from istota.config import BrainConfig

        config = make_config(brain=BrainConfig(kind="native"))
        r = run_checks(config, only=("runtime.model_cli",))[0]
        assert r.status == SKIP
        assert "native" in r.detail

    def test_missing_claude_fails_under_claude_code(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        r = run_checks(make_config(), only=("runtime.model_cli",))[0]
        assert r.status == FAIL

    def test_present_claude_is_ok(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "claude", "2.1.168 (Claude Code)")
        monkeypatch.setattr(doctor.shutil, "which", lambda name: str(fake) if name == "claude" else None)
        r = run_checks(make_config(), only=("runtime.model_cli",))[0]
        assert r.status == OK
        assert "2.1.168" in r.detail


class TestTmux:
    def test_skips_unless_tmux_brain(self, make_config):
        r = run_checks(make_config(), only=("runtime.tmux",))[0]
        assert r.status == SKIP

    def test_missing_tmux_fails_under_the_tmux_brain(self, make_config, monkeypatch):
        from istota.config import BrainConfig

        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = make_config(brain=BrainConfig(kind="tmux_claude"))
        r = run_checks(config, only=("runtime.tmux",))[0]
        assert r.status == FAIL


class TestFrameworkDb:
    def test_missing_db_warns(self, make_config, tmp_path):
        config = make_config(db_path=tmp_path / "absent.db")
        r = run_checks(config, only=("runtime.framework_db",))[0]
        assert r.status == WARN
        assert r.remedy

    def test_clean_db_is_ok(self, make_config, tmp_path):
        import sqlite3

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
        conn.close()
        config = make_config(db_path=db_path)
        r = run_checks(config, only=("runtime.framework_db",))[0]
        assert r.status == OK

    def test_unopenable_db_fails(self, make_config, tmp_path):
        db_path = tmp_path / "istota.db"
        db_path.write_bytes(b"this is definitely not a sqlite database")
        config = make_config(db_path=db_path)
        r = run_checks(config, only=("runtime.framework_db",))[0]
        assert r.status == FAIL

    def test_is_deployment_scoped(self, make_config, tmp_path):
        config = make_config(db_path=tmp_path / "absent.db")
        assert run_checks(config, only=("runtime.framework_db",))[0].scope == DEPLOYMENT

    def test_does_not_repair(self, make_config, tmp_path, monkeypatch):
        """Doctor is a diagnostic. `check_db_health` owns the REINDEX."""
        import sqlite3

        from istota import db_health

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
        conn.close()

        def _fail(*args, **kwargs):
            raise AssertionError("doctor must not repair the database")

        monkeypatch.setattr(db_health, "reindex", _fail)
        monkeypatch.setattr(db_health, "check_and_repair", _fail)
        run_checks(make_config(db_path=db_path), only=("runtime.framework_db",))


class TestWritableDirs:
    def test_writable_dirs_are_ok(self, make_config, tmp_path):
        config = make_config()
        results = run_checks(config, only=("runtime.writable_dirs",))
        assert results
        assert all(r.status == OK for r in results), [
            (r.name, r.status, r.detail) for r in results if r.status != OK
        ]

    @pytest.mark.requires_dac
    def test_unwritable_dir_fails(self, make_config, tmp_path):
        if sys.platform == "win32":  # pragma: no cover
            pytest.skip("posix permissions")
        temp = tmp_path / "locked"
        temp.mkdir()
        temp.chmod(0o500)
        try:
            config = make_config(temp_dir=temp)
            results = _by_name(run_checks(config, only=("runtime.writable_dirs",)))
            assert results["runtime.writable_dirs.temp_dir"].status == FAIL
        finally:
            temp.chmod(0o700)

    def test_one_result_per_directory(self, make_config):
        names = {r.name for r in run_checks(make_config(), only=("runtime.writable_dirs",))}
        assert "runtime.writable_dirs.temp_dir" in names
        assert "runtime.writable_dirs.module_db_root" in names


class TestMountLiveness:
    @staticmethod
    def _nextcloud_backed(make_config, **overrides):
        from istota.config import NextcloudConfig

        return make_config(
            nextcloud=NextcloudConfig(url="https://cloud.example"), **overrides
        )

    def test_skips_when_no_mount_configured(self, make_config):
        config = make_config(nextcloud_mount_path=None)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == SKIP

    def test_skips_for_a_local_workspace_folder(self, make_config):
        """The local single-user install points this at a plain directory under
        `~` that nothing ever mounts. Asserting ismount there reports a healthy
        install as broken."""
        r = run_checks(make_config(), only=("runtime.mount_liveness",))[0]
        assert r.status == SKIP
        assert "local workspace folder" in r.detail

    def test_configured_but_not_mounted_fails(self, make_config, tmp_path):
        # `make_config` points nextcloud_mount_path at a plain tmp_path dir,
        # which is on the same filesystem as its parent and so is not a mount.
        config = self._nextcloud_backed(make_config)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_a_real_mount_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.os.path, "ismount", lambda p: True)
        config = self._nextcloud_backed(make_config)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == OK


class TestSkillProxy:
    def test_resolvable_is_ok(self, make_config, tmp_path, monkeypatch):
        fake = _fake_bin(tmp_path / "bin" / "istota-skill")
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(fake) if name == "istota-skill" else None
        )
        results = _by_name(run_checks(make_config(), only=("security.skill_proxy",)))
        assert results["security.skill_proxy"].status == OK

    def test_unresolvable_fails(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        results = _by_name(run_checks(make_config(), only=("security.skill_proxy",)))
        assert results["security.skill_proxy"].status == FAIL

    def test_skips_when_the_proxy_is_disabled(self, make_config):
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(skill_proxy_enabled=False))
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        assert results["security.skill_proxy"].status == SKIP

    def test_forge_posture_warns_when_tokens_configured_and_proxy_off(
        self, make_config, tmp_path
    ):
        """Wording preserved from `_validate_forge_clis`: the tokens still work,
        but they sit in the environment the model's own shell inherits."""
        from istota.config import SecurityConfig

        config = _dev_config(make_config, tmp_path)
        config.security = SecurityConfig(skill_proxy_enabled=False)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        posture = results["security.skill_proxy.forge_posture"]
        assert posture.status == WARN
        assert "readable by anything else the task runs" in posture.detail

    def test_forge_posture_skips_without_tokens(self, make_config, tmp_path):
        from istota.config import SecurityConfig

        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        config.security = SecurityConfig(skill_proxy_enabled=False)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        assert results["security.skill_proxy.forge_posture"].status == SKIP

    def test_forge_posture_skips_when_the_proxy_is_on(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))
        assert results["security.skill_proxy.forge_posture"].status == SKIP


class TestForgeGating:
    """Every `developer.*` check inherits today's gating. Without this, a
    tokenless developer-skill deployment goes from silent to alerting."""

    def test_skips_when_the_skill_is_off(self, make_config):
        results = run_checks(make_config(), only=("developer.",))
        assert results
        assert all(r.status == SKIP for r in results)

    def test_skips_without_repos_dir(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, repos_dir="")
        results = run_checks(config, only=("developer.",))
        assert all(r.status == SKIP for r in results)

    def test_binary_checks_skip_without_a_token(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        results = _by_name(run_checks(config, only=("developer.",)))
        assert results["developer.forge_binaries.gh"].status == SKIP
        assert results["developer.forge_config_drift.gh"].status == SKIP
        assert results["developer.forge_versions.gh"].status == SKIP

    def test_policy_check_runs_without_a_token(self, make_config, tmp_path):
        """`forge_cli_permit` validation is about the config file, not about
        whether a credential happens to be wired yet."""
        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        results = _by_name(run_checks(config, only=("developer.forge_policy",)))
        assert results["developer.forge_policy"].status != SKIP


class TestForgeBinaries:
    def test_present_and_executable_is_ok(self, make_config, tmp_path):
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == OK
        assert results["developer.forge_binaries.glab"].status == OK

    def test_missing_binary_fails(self, make_config, tmp_path):
        """The ISSUE-263 shape: `os.execve` onto a path that does not exist."""
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == FAIL
        assert str(tmp_path / "bin" / "gh") in results["developer.forge_binaries.gh"].detail
        assert results["developer.forge_binaries.gh"].remedy

    def test_present_but_not_executable_fails(self, make_config, tmp_path):
        path = tmp_path / "bin" / "gh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o644)
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_binaries",)))
        assert results["developer.forge_binaries.gh"].status == FAIL
        assert "not executable" in results["developer.forge_binaries.gh"].detail

    def test_one_result_per_binary(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        names = {r.name for r in run_checks(config, only=("developer.forge_binaries",))}
        assert names == {"developer.forge_binaries.gh", "developer.forge_binaries.glab"}

    def test_is_image_scoped(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, only=("developer.forge_binaries",)):
            assert r.scope == IMAGE


class TestForgeConfigDrift:
    """`_resolve_real_bin`'s fallback is correct and load-bearing, and it hides
    the stale-config condition. This check restores that signal."""

    def test_configured_path_that_exists_and_resolves_to_itself_is_ok(
        self, make_config, tmp_path
    ):
        _fake_bin(tmp_path / "bin" / "gh")
        _fake_bin(tmp_path / "bin" / "glab")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_config_drift",)))
        assert results["developer.forge_config_drift.gh"].status == OK

    def test_stale_configured_path_warns_naming_both(self, make_config, tmp_path, monkeypatch):
        """The retained-volume upgrade: `config.toml` predates the binaries, so
        resolution falls through to the image location."""
        from istota.skills import developer as developer_skill

        shipped = _fake_bin(tmp_path / "image" / "gh")
        monkeypatch.setitem(developer_skill._IMAGE_BIN, "gh", str(shipped))
        config = _dev_config(
            make_config, tmp_path, gh_bin_path="/usr/local/bin/gh"
        )
        results = _by_name(run_checks(config, only=("developer.forge_config_drift",)))
        drift = results["developer.forge_config_drift.gh"]
        assert drift.status == WARN
        assert "/usr/local/bin/gh" in drift.detail
        assert str(shipped) in drift.detail
        assert drift.remedy

    def test_an_explicit_missing_path_does_not_contradict_itself(self, make_config, tmp_path):
        """`_resolve_real_bin` returns an explicitly chosen path as given, so
        configured == resolved while nothing exists there. One combined message
        would read "x but the wrapper will exec x"."""
        config = _dev_config(make_config, tmp_path, gh_bin_path=str(tmp_path / "nowhere" / "gh"))
        results = _by_name(run_checks(config, only=("developer.forge_config_drift",)))
        drift = results["developer.forge_config_drift.gh"]
        assert drift.status == WARN
        assert "nothing exists there" in drift.detail
        assert "but the wrapper will exec" not in drift.detail

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, only=("developer.forge_config_drift",)):
            assert r.status != FAIL


class TestForgeVersions:
    def test_at_known_good_is_ok(self, make_config, tmp_path):
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_versions",)))
        assert results["developer.forge_versions.gh"].status == OK
        assert results["developer.forge_versions.glab"].status == OK

    def test_above_known_good_is_ok(self, make_config, tmp_path):
        _fake_bin(tmp_path / "bin" / "gh", "gh version 3.4.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_versions",)))
        assert results["developer.forge_versions.gh"].status == OK

    def test_below_known_good_warns_naming_both_numbers(self, make_config, tmp_path):
        """bookworm ships gh 2.23; the exercised version is 2.98."""
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.23.0 (2023-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_versions",)))
        gh = results["developer.forge_versions.gh"]
        assert gh.status == WARN
        assert "2.23" in gh.detail
        assert "2.98" in gh.detail

    def test_unparseable_version_warns_rather_than_crashing(self, make_config, tmp_path):
        _fake_bin(tmp_path / "bin" / "gh", "some entirely unexpected banner")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_versions",)))
        assert results["developer.forge_versions.gh"].status == WARN

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, only=("developer.forge_versions",)):
            assert r.status != FAIL

    def test_skips_without_probe(self, make_config, tmp_path):
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_versions",), probe=False))
        assert results["developer.forge_versions.gh"].status == SKIP


class TestVersionParsing:
    """The real output shapes, pinned."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("gh version 2.98.0 (2026-01-01)", (2, 98, 0)),
            ("gh version 2.23.0 (2023-05-05)\nhttps://github.com/cli/cli", (2, 23, 0)),
            ("glab 1.114.0", (1, 114, 0)),
            ("glab version 1.114.0 (2026-01-01)", (1, 114, 0)),
            ("v2.98.0", (2, 98, 0)),
        ],
    )
    def test_parses_the_real_shapes(self, text, expected):
        assert doctor.parse_version(text) == expected

    @pytest.mark.parametrize("text", ["", "unexpected banner", "gh version x.y.z", None])
    def test_unparseable_returns_none(self, text):
        assert doctor.parse_version(text or "") is None

    def test_known_good_constants_live_in_forge_cli(self):
        from istota.forge_cli import GH_KNOWN_GOOD, GLAB_KNOWN_GOOD

        assert GH_KNOWN_GOOD == (2, 98)
        assert GLAB_KNOWN_GOOD == (1, 114)


class TestWrapperShadowing:
    """The question is "is something *unexpected* reachable by name", not "is a
    real forge binary on PATH" — the latter is true by design on the Ansible
    shape, which is what production runs."""

    def test_nothing_on_path_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        assert results["developer.forge_wrapper_shadowing.gh"].status == OK

    def test_an_unexpected_real_binary_on_path_fails(self, make_config, tmp_path, monkeypatch):
        """Someone apt-installed gh onto the image shape: the model's shell finds
        it before the per-task wrapper and skips the policy and the injection."""
        real = tmp_path / "path" / "gh"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)
        real.chmod(0o755)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(real) if name == "gh" else None
        )
        # The deployment resolved something else entirely.
        _fake_bin(tmp_path / "bin" / "gh")
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        gh = results["developer.forge_wrapper_shadowing.gh"]
        assert gh.status == FAIL
        assert str(real) in gh.detail
        assert gh.remedy

    def test_the_ansible_shape_is_ok(self, make_config, tmp_path, monkeypatch):
        """The role installs the real binaries into /usr/bin and renders those
        paths into config.toml, so `which` finding them is correct. A FAIL here
        would alert the admin allowlist on every boot of a healthy host."""
        installed = _fake_bin(tmp_path / "usr-bin" / "gh")
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(installed) if name == "gh" else None
        )
        config = _dev_config(make_config, tmp_path, gh_bin_path=str(installed))
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        gh = results["developer.forge_wrapper_shadowing.gh"]
        assert gh.status == OK
        assert "Ansible shape" in gh.detail

    def test_the_real_wrapper_on_path_is_ok(self, make_config, tmp_path, monkeypatch):
        """Copied from `forge_cli.py` itself, not hand-written to match.

        The wrapper the daemon writes per task *is* a verbatim copy of that
        file, so a hand-written stand-in could satisfy the identity test while
        the real article failed it — which is how the previous docstring-prose
        matching would have broken on a reworded comment.
        """
        import shutil as _shutil

        from istota import forge_cli

        wrapper = tmp_path / "path" / "gh"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy(forge_cli.__file__, wrapper)
        wrapper.chmod(0o755)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(wrapper) if name == "gh" else None
        )
        config = _dev_config(make_config, tmp_path)
        results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        assert results["developer.forge_wrapper_shadowing.gh"].status == OK

    def test_the_sentinel_is_near_the_top_of_the_wrapper(self):
        """`_looks_like_the_wrapper` reads only the file's head."""
        from istota import forge_cli

        head = Path(forge_cli.__file__).read_bytes()[:8192]
        assert doctor._WRAPPER_SENTINEL in head

    def test_the_devbox_copy_carries_the_sentinel_too(self):
        """The devbox image ships a byte-identical copy under another name; it
        is the one shape where the wrapper really is on PATH."""
        copy = Path(__file__).resolve().parents[1] / "docker/devbox/lib/istota_forge_cli.py"
        assert doctor._WRAPPER_SENTINEL in copy.read_bytes()[:8192]

    @pytest.mark.requires_dac
    def test_an_unreadable_binary_is_unknown_not_a_failure(
        self, make_config, tmp_path, monkeypatch
    ):
        """A permission bit is not evidence of a shadowing real binary."""
        opaque = tmp_path / "path" / "gh"
        opaque.parent.mkdir(parents=True, exist_ok=True)
        opaque.write_text("whatever")
        opaque.chmod(0o311)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda name: str(opaque) if name == "gh" else None
        )
        config = _dev_config(make_config, tmp_path)
        try:
            results = _by_name(run_checks(config, only=("developer.forge_wrapper_shadowing",)))
        finally:
            opaque.chmod(0o644)
        gh = results["developer.forge_wrapper_shadowing.gh"]
        assert gh.status == WARN
        assert gh.remedy


class TestForgePolicy:
    def test_clean_permits_are_ok(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, forge_cli_permit=[])
        r = run_checks(config, only=("developer.forge_policy",))[0]
        assert r.status == OK

    def test_unmatched_permit_warns_naming_the_entry(self, make_config, tmp_path):
        config = _dev_config(
            make_config, tmp_path, forge_cli_permit=["gh not-a-real-verb-at-all"]
        )
        r = run_checks(config, only=("developer.forge_policy",))[0]
        assert r.status == WARN
        assert "not-a-real-verb-at-all" in r.detail

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, forge_cli_permit=["gh nonsense"])
        assert run_checks(config, only=("developer.forge_policy",))[0].status != FAIL


class TestForgeTransport:
    """A forge token sent over plain HTTP.

    This became reachable when the developer skill started seeding glab's
    `api_protocol` for an `http://` forge URL — before that a plain-HTTP forge
    simply failed at the TLS handshake, so no token ever left. It works now,
    and a working plaintext credential transport is worth one line in the
    report rather than silence.
    """

    def test_https_is_ok(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_url="https://gitlab.com")
        r = run_checks(config, only=("developer.forge_transport",))[0]
        assert r.status == OK

    def test_plain_http_with_a_token_warns_naming_the_url(self, make_config, tmp_path):
        config = _dev_config(
            make_config, tmp_path, gitlab_url="http://gitlab.internal:8080"
        )
        r = run_checks(config, only=("developer.forge_transport",))[0]
        assert r.status == WARN
        assert "http://gitlab.internal:8080" in r.detail
        assert r.remedy

    def test_the_token_value_is_never_in_the_report(self, make_config, tmp_path):
        """The detail names the URL, and a URL can carry userinfo."""
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_url="http://gitlab.internal:8080",
            gitlab_token="glpat-" + "s" * 20,
        )
        r = run_checks(config, only=("developer.forge_transport",))[0]
        assert "glpat-" not in (r.detail + (r.remedy or ""))

    def test_loopback_still_warns(self, make_config, tmp_path):
        """No carve-out for localhost.

        A loopback forge URL in a real deployment is a proxy or a tunnel, and
        what is on the far side of it is not knowable from here. The check is
        cheap and a WARN costs nothing; guessing wrong is a silently plaintext
        credential.
        """
        config = _dev_config(make_config, tmp_path, gitlab_url="http://127.0.0.1:18080")
        assert run_checks(config, only=("developer.forge_transport",))[0].status == WARN

    def test_skips_without_a_token(self, make_config, tmp_path):
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_url="http://gitlab.internal:8080",
            gitlab_token="",
            github_token="",
        )
        assert run_checks(config, only=("developer.forge_transport",))[0].status == SKIP

    def test_a_plain_http_github_url_warns_too(self, make_config, tmp_path):
        """Both forges, even though gh cannot reach a non-443 host.

        gh drops the port, so it would reach `https://the-host:443` rather than
        the plaintext one — but the scheme is still what the operator wrote,
        and a check that stayed quiet about it would be reporting on the
        deployment it wished it had.
        """
        config = _dev_config(
            make_config,
            tmp_path,
            gitlab_url="https://gitlab.com",
            github_url="http://ghe.internal",
            github_token="g" * 20,
        )
        assert run_checks(config, only=("developer.forge_transport",))[0].status == WARN

    def test_never_fails(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_url="http://gitlab.internal")
        assert run_checks(config, only=("developer.forge_transport",))[0].status != FAIL


class TestWebStatic:
    def test_skips_when_no_web_surface(self, make_config):
        r = run_checks(make_config(), only=("web.static",))[0]
        assert r.status == SKIP

    def test_missing_build_fails(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(tmp_path / "nope"))
        config = make_config(web=WebConfig(enabled=True))
        r = run_checks(config, only=("web.static",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_empty_index_fails(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        config = make_config(web=WebConfig(enabled=True))
        r = run_checks(config, only=("web.static",))[0]
        assert r.status == FAIL

    def test_present_build_is_ok(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html><html></html>")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        config = make_config(web=WebConfig(enabled=True))
        r = run_checks(config, only=("web.static",))[0]
        assert r.status == OK


class TestSandboxMasks:
    def test_skips_when_bwrap_is_unavailable(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor, "_bwrap_usable", lambda: False)
        r = run_checks(make_config(), only=("sandbox.masks",), deep=True)[0]
        assert r.status == SKIP

    def test_skips_under_probe_false(self, make_config, monkeypatch):
        """The probe contract is unconditional. Checked before `_bwrap_usable`,
        which spawns a probe of its own."""

        def _fail(*args, **kwargs):
            raise AssertionError("spawned under probe=False")

        monkeypatch.setattr(doctor.subprocess, "run", _fail)
        r = run_checks(make_config(), only=("sandbox.masks",), deep=True, probe=False)[0]
        assert r.status == SKIP
        assert "probe disabled" in r.detail

    def test_timeout_is_reported_as_fail_not_a_hang(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor, "_bwrap_usable", lambda: True)

        def _timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="bwrap", timeout=30)

        monkeypatch.setattr(doctor.subprocess, "run", _timeout)
        r = run_checks(make_config(), only=("sandbox.masks",), deep=True)[0]
        assert r.status == FAIL
        assert "timed out" in r.detail.lower()


class TestRendering:
    def test_exit_code_is_one_on_any_fail(self):
        results = [
            CheckResult("a.b", OK, "fine"),
            CheckResult("c.d", FAIL, "broken", remedy="fix it"),
        ]
        assert exit_code(results) == 1

    def test_exit_code_is_zero_without_a_fail(self):
        results = [
            CheckResult("a.b", OK, "fine"),
            CheckResult("c.d", WARN, "iffy", remedy="look at it"),
            CheckResult("e.f", SKIP, "n/a"),
        ]
        assert exit_code(results) == 0

    def test_exit_code_of_nothing_is_zero(self):
        assert exit_code([]) == 0

    def test_render_json_round_trips(self):
        results = [
            CheckResult("a.b", OK, "fine", scope=IMAGE),
            CheckResult("c.d", FAIL, "broken", remedy="fix it"),
        ]
        parsed = json.loads(render_json(results, secrets=()))
        assert isinstance(parsed, list)
        assert parsed[0] == {
            "name": "a.b",
            "status": OK,
            "detail": "fine",
            "remedy": "",
            "scope": IMAGE,
        }
        assert parsed[1]["remedy"] == "fix it"

    def test_render_json_is_valid_even_when_checks_failed(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path)
        json.loads(render_json(run_checks(config), secrets=doctor.config_secrets(config)))

    def test_secrets_is_required_so_the_boundary_cannot_be_fail_open(self):
        """`render_json` crosses an HTTP boundary to the admin dashboard. A
        caller that simply forgot the argument must not get unredacted output —
        omitting it has to be a decision, spelled `secrets=()`."""
        results = [CheckResult("a.b", OK, "fine")]
        with pytest.raises(TypeError):
            render_json(results)
        with pytest.raises(TypeError):
            render_text(results)

    def test_render_json_redacts_a_credential_in_a_detail(self):
        """Check authors are forbidden from putting credentials in `detail`;
        the renderer does not take their word for it."""
        secret = "NOT-A-REAL-TOKEN-aaaaaaaa"
        results = [CheckResult("x.y", FAIL, f"token {secret} rejected", remedy=f"rotate {secret}")]
        rendered = render_json(results, secrets=[secret])
        assert secret not in rendered
        assert "[redacted]" in rendered

    def test_render_json_redaction_ignores_empty_secrets(self):
        results = [CheckResult("x.y", OK, "all good")]
        rendered = render_json(results, secrets=["", None])
        assert "all good" in rendered

    def test_render_text_groups_by_prefix_and_indents_remedies(self):
        results = [
            CheckResult("runtime.platform", OK, "Linux x86_64"),
            CheckResult("developer.forge_binaries.gh", FAIL, "missing", remedy="install gh"),
        ]
        text = render_text(results, secrets=())
        assert "runtime" in text
        assert "developer" in text
        assert "install gh" in text
        remedy_line = [ln for ln in text.splitlines() if "install gh" in ln][0]
        assert remedy_line.startswith(" ")

    def test_render_text_redacts_too(self):
        secret = "NOT-A-REAL-TOKEN-bbbbbbbb"
        results = [CheckResult("x.y", FAIL, f"saw {secret}", remedy="rotate")]
        assert secret not in render_text(results, secrets=[secret])


class TestConfigSecrets:
    def test_collects_configured_credentials(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="NOT-A-REAL-TOKEN-zzzzzzzz")
        secrets = doctor.config_secrets(config)
        assert "NOT-A-REAL-TOKEN-zzzzzzzz" in secrets

    def test_ignores_short_and_empty_values(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="")
        assert "" not in doctor.config_secrets(config)

    def test_descends_into_dicts(self, make_config):
        """`config.users` is a dict of dataclasses and holds every per-user
        credential; a walk that only followed attributes missed all of them."""
        from istota.config import UserConfig

        config = make_config()
        user = UserConfig(display_name="Alice")
        if hasattr(user, "email_password"):
            user.email_password = "hunter2-hunter2-hunter2"
            config.users = {"alice": user}
            assert "hunter2-hunter2-hunter2" in doctor.config_secrets(config)

    def test_collects_the_always_secret_header_dict(self, make_config):
        """`brain.native.extra_headers` is a dict `admin_config_view` marks
        always-secret, because it is where a non-Anthropic deployment puts its
        provider key."""
        config = make_config()
        config.brain.native.extra_headers = {"Authorization": "NOT-A-REAL-HEADER-VALUE-1"}
        assert "NOT-A-REAL-HEADER-VALUE-1" in doctor.config_secrets(config)

    def test_collects_a_header_whose_name_is_not_obviously_a_credential(self, make_config):
        """The whole field is an auth channel by construction, so a header spelling
        nobody anticipated must not be the thing that escapes redaction."""
        config = make_config()
        config.brain.native.extra_headers = {"x-goog-api-client": "zzzzzzzzzzzzzzzzzz"}
        assert "zzzzzzzzzzzzzzzzzz" in doctor.config_secrets(config)

    def test_terminates_on_a_self_referential_config(self, make_config):
        """A cycle must not hang the boot path."""
        config = make_config()
        config.users = {"loop": config}
        doctor.config_secrets(config)
