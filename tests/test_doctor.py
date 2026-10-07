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
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from istota import doctor
from istota.usage import subscription as subscription_usage
from istota.credentials import store as secrets_store
from istota import executor as doctor_executor
from istota.doctor import (
    CHECKS,
    DEEP_CHECKS,
    DEPLOYMENT,
    FAIL,
    IMAGE,
    LIVE_CHECKS,
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


def _which_only(monkeypatch, name, path):
    """`shutil.which` as doctor sees it: `path` for `name`, nothing for anything else."""
    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: str(path) if n == name else None
    )


def _spawn_spy(monkeypatch, *names):
    """Record, then refuse, every call to the named `subprocess` functions."""
    spawns = []

    def _spy(*args, **kwargs):
        spawns.append(args[0] if args else kwargs.get("args"))
        raise OSError("no subprocesses in this test")

    for name in names or ("run",):
        monkeypatch.setattr(subprocess, name, _spy)
    return spawns


#: What `runtime.model_execution` asks the model to echo. Restated rather than
#: imported, so a rename of the private constant cannot silently disarm the
#: sentinel below by making it match nothing.
_MODEL_MARKER = "healthcheck-ok"


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

    def test_names_are_unique_dotted_and_stable(self):
        names = [name for name, _ in CHECKS]
        assert len(names) == len(set(names))
        for name in names:
            assert "." in name, f"{name} is not a dotted id"
            assert name == name.strip()
            assert name.islower()

    def test_deep_and_live_checks_are_registered(self):
        names = {name for name, _ in CHECKS}
        assert DEEP_CHECKS <= names
        assert LIVE_CHECKS <= names

    def test_every_result_name_matches_its_registry_entry(self, make_config, tmp_path):
        """`only=` filters on the registry name, so a result named something
        else is invisible to the caller that asked for it."""
        config = _dev_config(make_config, tmp_path)
        for name, _ in CHECKS:
            # `live=` because a registry entry filtered out of the sweep is a
            # registry entry this test does not check. It is the one place in
            # the default suite that selects the model probe at all, so the
            # sentinel goes on the iteration that takes the risk rather than in
            # a neighbouring test: `_dev_config` configures no users and the
            # check therefore SKIPs, but nothing here enforces that, and a
            # guard that only reports afterwards reports after the spend.
            live = name in LIVE_CHECKS
            with pytest.MonkeyPatch.context() as mp:
                if live:
                    mp.setattr(subprocess, "run", _NoModel())
                results = run_checks(config, only=(name,), deep=True, live=live)
            assert results, f"{name} produced no result"
            for r in results:
                assert r.name == name or r.name.startswith(name + "."), (
                    f"{name} returned a result named {r.name!r}"
                )

    def test_the_registry_sweep_cannot_reach_a_model(self, make_config, tmp_path, monkeypatch):
        """The sweep above is the only `live=True` run in the default suite.

        It does not bill the account because `_dev_config` configures no users
        and `runtime.model_execution` therefore SKIPs — which is luck until
        something asserts it. This is that assertion, with `subprocess.run`
        replaced by a sentinel so a future change that makes the sweep reach a
        model fails here rather than on an invoice.
        """
        sentinel = _NoModel()
        monkeypatch.setattr(subprocess, "run", sentinel)
        config = _dev_config(make_config, tmp_path)
        results = run_checks(config, only=("runtime.model_execution",), deep=True, live=True)
        assert [r.status for r in results] == [SKIP]
        assert sentinel.calls == []

    def test_every_result_is_well_formed(self, make_config, tmp_path):
        """A detail on every result, a known status and scope, and a remedy on
        every WARN and FAIL."""
        config = _dev_config(make_config, tmp_path)
        for r in run_checks(config, deep=True):
            assert r.detail.strip(), f"{r.name} returned an empty detail"
            assert r.status in (OK, WARN, FAIL, SKIP)
            assert r.scope in (IMAGE, DEPLOYMENT)
            if r.status in (WARN, FAIL):
                assert r.remedy.strip(), f"{r.name} is {r.status} with no remedy"

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
        spawns = _spawn_spy(monkeypatch, "run", "Popen", "check_output")
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
        """Positive control for the test above: a check that spawns is seen.

        A guard that cannot fail is not a guard, and the previous version of
        this pair could not: it asserted by raising into a `try/except` that
        exists precisely to swallow.
        """
        spawns = _spawn_spy(monkeypatch)

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

    @staticmethod
    def _import_graph(module: str) -> set[str]:
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

    @pytest.mark.parametrize(
        "module,absent",
        [
            ("istota.sandbox.forge_bin", ("istota.skills", "istota.config")),
            ("istota.webui.static_dir", ("fastapi", "istota.webui.app", "istota.config")),
            # `doctor` imports `subscription_usage` lazily, so it stays cheap
            # for the config-load path that imports `doctor` itself.
            ("istota.doctor", ("istota.usage.subscription",)),
        ],
        ids=["forge_bin", "static_dir", "doctor"],
    )
    def test_a_leaf_stays_off_the_heavy_imports(self, module, absent):
        loaded = self._import_graph(module)
        for name in absent:
            assert name not in loaded

    def _run_in_fresh_interpreter(
        self, tmp_path, body: str
    ) -> tuple[set[str], list[tuple[str, str]]]:
        """Run `body` against a wired-up dev Config in a fresh interpreter.

        Returns the modules loaded afterwards and the `(name, status)` pairs of
        whatever `run_checks` the body called put in `results`. Statuses and not
        just names, because a check that returned early is still a result under
        the same name — see the caller.

        `tmp_path` is handed to the subprocess rather than letting it call
        `mkdtemp`, so pytest owns the cleanup like it does for every other test
        in this file.

        A subprocess rather than `monkeypatch.delitem` on `sys.modules`, for the
        reason the two sibling tests above already spawn one: deleting
        `istota.skills` while its importer stays cached makes the deletion
        inert, so the import chain resolves from cache, nothing re-adds the
        module, and the assertion passes while the property is untested. That is
        ordering-dependent, so under `-n auto` it is a flake rather than only a
        run-it-alone curiosity (ISSUE-335). No arrangement of in-process cache
        surgery fixes this; a clean interpreter is the only honest substrate.
        """
        code = (
            "import json, pathlib, sys\n"
            "from istota.config import CONFIG_LOAD_CHECKS, Config, DeveloperConfig\n"
            "from istota.doctor import run_checks\n"
            "tmp = pathlib.Path(sys.argv[1])\n"
            "skills = tmp / 'skills'; skills.mkdir(exist_ok=True)\n"
            "(skills / '_index.toml').write_text('')\n"
            "mount = tmp / 'mount'; mount.mkdir(exist_ok=True)\n"
            "repos = tmp / 'repos'; repos.mkdir(exist_ok=True)\n"
            "config = Config(\n"
            "    db_path=tmp / 'test.db', temp_dir=tmp / 'temp',\n"
            "    skills_dir=skills, workspace_path=mount,\n"
            "    developer=DeveloperConfig(\n"
            "        enabled=True, repos_dir=str(repos), gitlab_token='t' * 20,\n"
            "        gh_bin_path=str(tmp / 'bin' / 'gh'),\n"
            "        glab_bin_path=str(tmp / 'bin' / 'glab'),\n"
            "    ),\n"
            ")\n"
            "results = []\n"
            f"{body}\n"
            "print(json.dumps({\n"
            "    'modules': sorted(sys.modules),\n"
            "    'results': [[r.name, r.status] for r in results],\n"
            "}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True
        )
        # Not `check=True`: `CalledProcessError` does not render `stderr`, so a
        # traceback from the constructed `Config` — a renamed field, say —
        # would surface only as a non-zero exit status with the cause discarded.
        assert out.returncode == 0, out.stderr
        # The last line, not the whole of stdout: anything the body prints ahead
        # of the payload should fail an assertion rather than a JSON parse.
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        return set(payload["modules"]), [tuple(r) for r in payload["results"]]

    def test_the_config_load_checks_do_not_import_the_skill_package(self, tmp_path):
        """The checks `load_config` runs stay off the skill package's import graph.

        Scoped to `config.CONFIG_LOAD_CHECKS` — the exact tuple
        `_validate_forge_clis` passes — rather than to a `developer.` prefix.
        The prefix was wrong at both ends: it overshot onto `repos_layout` and
        `container`, which reach `executor` deliberately and are on no hot path,
        and it undershot by omitting `security.skill_proxy`, which really does
        run inside every `load_config`.

        `istota.executor` is asserted alongside `istota.skills` because it is
        the importer that pulls the package in (`executor` imports
        `.skills.calendar` at module scope, and `istota.skills.__init__`
        star-imports every skill). Naming both means the guard still bites if
        the star-import is ever removed but the chain onto `executor` is not.
        """
        loaded, ran = self._run_in_fresh_interpreter(
            tmp_path,
            "results = run_checks(config, only=CONFIG_LOAD_CHECKS, probe=False)",
        )
        from istota.config import CONFIG_LOAD_CHECKS

        # Every requested check produced a result. `only=` filters on registry
        # names, so a rename would otherwise leave the module assertions below
        # passing over an empty run.
        for name in CONFIG_LOAD_CHECKS:
            assert any(
                n == name or n.startswith(f"{name}.") for n, _ in ran
            ), f"{name} produced no result; the guard would be vacuous"

        # And the two that reach for something did the reaching. Returning a
        # result is not evidence of work: both of these emit one under their own
        # name from an early `SKIP` — `check_forge_binaries` before it calls
        # `_resolved_forge_bin`, `check_forge_policy` before it imports
        # `forge_cli` — so a tightened gate would leave nothing heavy running and
        # every assertion below satisfied. `security.skill_proxy` is deliberately
        # not held to this: it SKIPs legitimately when `istota-skill` is off the
        # PATH, which is a property of the machine, not of the checks.
        did_work = {"developer.forge_binaries", "developer.forge_policy"}
        for name, status in ran:
            if name in did_work or name.rsplit(".", 1)[0] in did_work:
                assert status != SKIP, f"{name} skipped; nothing heavy was reached"

        assert "istota.skills" not in loaded
        assert "istota.executor" not in loaded

    def test_the_import_probe_can_see_an_import(self, tmp_path):
        """The control for the test above, which otherwise cannot be seen to fail.

        A fresh-interpreter probe that reported an empty or truncated module set
        would satisfy every `not in` assertion above while testing nothing. This
        asserts the other direction on the same helper, against a synthetic body
        rather than against a real check, so no product change can make it churn.

        Only the module named by the body is asserted, and deliberately not
        `istota.skills` alongside it: that one is present because
        `executor` imports `.skills.calendar`, which is a product fact the test
        above says may legitimately change. Asserting it here would make the
        control red for a reason having nothing to do with whether the probe can
        see an import.
        """
        loaded, _ = self._run_in_fresh_interpreter(tmp_path, "import istota.executor")
        assert "istota.executor" in loaded

    def test_web_static_does_not_import_web_app(self, make_config, tmp_path, monkeypatch):
        from istota.config import WebConfig

        build = tmp_path / "build"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html>")
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        monkeypatch.delitem(sys.modules, "istota.webui.app", raising=False)
        config = make_config(web=WebConfig(enabled=True))
        assert run_checks(config, only=("web.static",))[0].status == OK
        assert "istota.webui.app" not in sys.modules

    def test_web_app_and_doctor_resolve_the_same_static_dir(self):
        """One implementation, two callers — the point of the leaf."""
        from istota.webui import static_dir
        from istota.webui import app as web_app

        assert web_app._resolve_static_dir() == static_dir.resolve_static_dir()

    def test_developer_skill_and_doctor_resolve_the_same_binary(self):
        from istota.sandbox import forge_bin
        from istota.skills import developer

        assert developer._resolve_real_bin is forge_bin.resolve_real_bin


class TestPlatform:
    @pytest.mark.parametrize(
        "system,machine,sandbox,status,named",
        [
            ("Linux", "x86_64", True, OK, "x86_64"),
            ("Darwin", "arm64", True, FAIL, "Darwin"),
            ("Darwin", "arm64", False, WARN, "Darwin"),
        ],
        ids=["linux", "non-linux-sandboxed", "non-linux-unsandboxed"],
    )
    def test_status_follows_platform_and_sandbox(
        self, make_config, monkeypatch, system, machine, sandbox, status, named
    ):
        from istota.config import SecurityConfig

        monkeypatch.setattr(doctor.platform, "system", lambda: system)
        monkeypatch.setattr(doctor.platform, "machine", lambda: machine)
        config = make_config(security=SecurityConfig(sandbox_enabled=sandbox))
        r = run_checks(config, only=("runtime.platform",))[0]
        assert r.status == status
        assert named in r.detail
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

    @pytest.mark.parametrize(
        "output,exit_status,probe,status",
        [
            ("bubblewrap 0.8.0", 0, True, OK),
            ("nope", 1, True, FAIL),
            # A binary that exists and is executable is OK without running it,
            # even one that would exit non-zero.
            ("nope", 1, False, OK),
        ],
        ids=["runnable", "unrunnable", "probe-false-answers-from-the-filesystem"],
    )
    def test_a_present_binary(
        self, make_config, tmp_path, monkeypatch, output, exit_status, probe, status
    ):
        fake = _fake_bin(tmp_path / "bin" / "bwrap", output, exit_code=exit_status)
        _which_only(monkeypatch, "bwrap", fake)
        r = run_checks(make_config(), only=("runtime.bwrap",), probe=probe)[0]
        assert r.status == status


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
        _which_only(monkeypatch, "claude", _fake_bin(tmp_path / "bin" / "claude", "2.1.168 (Claude Code)"))
        r = run_checks(make_config(), only=("runtime.model_cli",))[0]
        assert r.status == OK
        assert "2.1.168" in r.detail


class TestTmux:
    @staticmethod
    def _tmux_brain(make_config):
        from istota.config import BrainConfig

        return make_config(brain=BrainConfig(kind="tmux_claude"))

    def test_skips_unless_tmux_brain(self, make_config):
        r = run_checks(make_config(), only=("runtime.tmux",))[0]
        assert r.status == SKIP

    def test_missing_tmux_fails_under_the_tmux_brain(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        r = run_checks(self._tmux_brain(make_config), only=("runtime.tmux",))[0]
        assert r.status == FAIL

    @pytest.mark.parametrize(
        "probe,wrong_reason",
        [(False, "exists and is executable"), (True, "could not be executed")],
        ids=["probe-false", "probe-true"],
    )
    def test_an_unrunnable_tmux_names_the_missing_bit(
        self, make_config, tmp_path, monkeypatch, probe, wrong_reason
    ):
        """A tmux on disk with no execute bit for anyone.

        The inlined copy of `_binary_status` this replaced answered `OK if
        _executable(path) else FAIL` under one fixed detail, so under
        `probe=False` it reported FAIL beneath "exists and is executable" — the
        opposite of the fault. That is a shipped caller's mode: `setup_wizard`
        runs the registry with `probe=False`. With probing on it tried the spawn
        and reported what the spawn said, so an operator was sent to reinstall
        a tmux that is installed.

        `shutil.which` filters on the execute bit, so the arm is reached when it
        goes away between the lookup and the check, which is why `which` is
        patched rather than a file put on PATH. `os.access(X_OK)` answers False
        for a mode with no execute bit even as root, so no `requires_dac`.
        """
        tmux = tmp_path / "bin" / "tmux"
        tmux.parent.mkdir(parents=True, exist_ok=True)
        tmux.write_text("#!/bin/sh\necho 'tmux 3.4'\n")
        tmux.chmod(0o644)
        _which_only(monkeypatch, "tmux", tmux)

        r = run_checks(self._tmux_brain(make_config), only=("runtime.tmux",), probe=probe)[0]

        assert r.status == FAIL
        assert "present but not executable" in r.detail
        assert wrong_reason not in r.detail
        assert r.remedy == "Install a working tmux."

    def test_the_version_probe_asks_tmux_for_dash_v(
        self, make_config, tmp_path, monkeypatch
    ):
        """`tmux --version` is an error, which is the whole reason a fork of
        `_binary_status` existed to inline.

        Asserted from the binary's own side rather than by recording an argv, so
        a `_run` that stopped forwarding the flag fails this too.
        """
        tmux = tmp_path / "bin" / "tmux"
        tmux.parent.mkdir(parents=True, exist_ok=True)
        tmux.write_text('#!/bin/sh\ntest "$1" = "-V" || exit 64\necho "tmux 3.4"\n')
        tmux.chmod(0o755)
        _which_only(monkeypatch, "tmux", tmux)

        r = run_checks(self._tmux_brain(make_config), only=("runtime.tmux",))[0]

        assert r.status == OK, r.detail
        assert "tmux 3.4" in r.detail

    def test_a_tmux_that_ran_and_exited_non_zero_names_the_status(
        self, make_config, tmp_path, monkeypatch
    ):
        """Second thing the inlined copy flattened: it reported "could not be
        executed" for a binary that executed perfectly well and answered
        non-zero, which sends an operator to reinstall rather than to read the
        status."""
        _which_only(monkeypatch, "tmux", _fake_bin(tmp_path / "bin" / "tmux", "nope", exit_code=3))

        r = run_checks(self._tmux_brain(make_config), only=("runtime.tmux",))[0]

        assert r.status == FAIL
        assert "exited 3 on -V" in r.detail


class TestTheBinaryChecksFollowTheReachableSet:
    """`runtime.model_cli` and `runtime.tmux` ask which kinds a task could run
    under, not which one is the base kind.

    Both halves are here on purpose. A test that only asserts the widening
    passes just as happily against a check that widened unconditionally — which
    would report a missing `claude` on every native-only deployment in the
    estate — so the negative is what says the answer tracks the allowlist. The
    allowlist is not a free-text widener either: a name no brain answers to
    must not turn a check on, and allowlisting `claude_code` widens
    `runtime.model_cli` and nothing else. Routing a lane or a fallback to a CLI
    brain has always put tasks on the binary too.
    """

    @pytest.mark.parametrize(
        "check,brain_fields,status,named",
        [
            ("runtime.model_cli", {"room_selectable": ["claude_code"]}, FAIL, "claude_code"),
            ("runtime.model_cli", {"room_selectable": []}, SKIP, None),
            ("runtime.model_cli", {"room_selectable": ["claude_kode"]}, SKIP, None),
            ("runtime.tmux", {"room_selectable": ["tmux_claude"]}, FAIL, "tmux_claude"),
            ("runtime.tmux", {"room_selectable": []}, SKIP, None),
            ("runtime.tmux", {"room_selectable": ["claude_code"]}, SKIP, None),
            ("runtime.model_cli", {"source_type_overrides": {"scheduled": "claude_code"}}, FAIL, None),
            ("runtime.model_cli", {"fallback": "claude_code"}, FAIL, None),
        ],
        ids=[
            "model_cli-room-may-pin-claude_code",
            "model_cli-empty-allowlist",
            "model_cli-unbuildable-allowlist-entry",
            "tmux-room-may-pin-tmux_claude",
            "tmux-empty-allowlist",
            "tmux-unmoved-by-another-kind",
            "model_cli-source-type-route",
            "model_cli-configured-fallback",
        ],
    )
    def test_a_native_base_kind(
        self, make_config, monkeypatch, check, brain_fields, status, named
    ):
        from istota.config import BrainConfig

        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        config = make_config(brain=BrainConfig(kind="native", **brain_fields))
        r = run_checks(config, only=(check,))[0]
        assert r.status == status
        if named:
            assert named in r.detail


class TestNativeBrainCredential:
    """`runtime.native_brain` — buildable is not runnable.

    `make_brain("native")` constructs a defaulted dataclass and asserts nothing
    about a key, so before this check an operator who allowlisted `native` got
    a room where every turn failed at the provider with nothing in the registry
    naming it.
    """

    @pytest.fixture(autouse=True)
    def _no_env_key(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_BRAIN_NATIVE_API_KEY", raising=False)

    def _native(self, make_config, **brain_fields):
        from istota.config import BrainConfig, NativeBrainConfig

        native = NativeBrainConfig(
            api_key=brain_fields.pop("api_key", ""),
            base_url=brain_fields.pop("base_url", "https://api.anthropic.com/v1"),
            extra_headers=brain_fields.pop("extra_headers", {}),
        )
        return make_config(
            brain=BrainConfig(kind="native", native=native, **brain_fields)
        )

    def _run(self, config):
        return run_checks(config, only=("runtime.native_brain",))[0]

    @pytest.mark.parametrize(
        "brain_fields,status,named",
        [
            ({}, FAIL, None),
            ({"api_key": "sk-test"}, OK, None),
            ({"api_key": "   "}, FAIL, None),
            # The provider merges `extra_headers` over the Authorization header
            # it builds, so an operator can authenticate entirely through them.
            # The check cannot confirm one is a credential, so it says so.
            ({"extra_headers": {"x-api-key": "v"}}, WARN, "extra_headers"),
            ({"extra_headers": {}}, FAIL, None),
            # An Ollama / vLLM / llama.cpp endpoint takes no key, and a FAIL is
            # not inert: the start-up report, the scheduler sweep and the
            # self-check heartbeat each alert on FAIL and none on WARN.
            ({"base_url": "http://localhost:11434/v1"}, WARN, None),
            ({"base_url": "http://127.0.0.1:11434/v1"}, WARN, None),
            ({"base_url": "http://192.168.1.5:11434/v1"}, WARN, None),
            ({"base_url": "http://ollama:11434/v1"}, WARN, None),
            ({"base_url": "http://box.lan:11434/v1"}, WARN, None),
            # The converse: without it the WARN above would be every deployment.
            ({"base_url": "https://openrouter.ai/api/v1"}, FAIL, None),
        ],
        ids=[
            "no-key-anywhere", "instance-key", "whitespace-key",
            "extra-headers", "empty-extra-headers",
            "localhost", "loopback-ip", "private-ip", "bare-hostname", "lan-name",
            "public-endpoint",
        ],
    )
    def test_the_instance_configuration(self, make_config, brain_fields, status, named):
        r = self._run(self._native(make_config, **brain_fields))
        assert r.status == status
        if status != OK:
            assert r.remedy
        if named:
            assert named in r.detail

    def test_the_env_var_satisfies_it(self, make_config, monkeypatch):
        """Asked separately from the field: `load_config` folds the variable
        into `api_key`, but a Config assembled any other way holds one and not
        the other."""
        monkeypatch.setenv("ISTOTA_BRAIN_NATIVE_API_KEY", "sk-env")
        assert self._run(self._native(make_config)).status == OK

    def _stored(self, make_config, monkeypatch, tmp_path, owner="alice", service="native_brain"):
        """A native-brain config whose only key may be a stored secret row."""
        from istota import db
        from istota.config import UserConfig

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, owner, service, "api_key", "sk-u")
        config = self._native(make_config)
        config.db_path = db_path
        config.users = {"alice": UserConfig()}
        return config

    @pytest.mark.parametrize(
        "owner,service,status",
        [
            # The executor overlays this per task, so a deployment where every
            # user brings their own key needs no instance-wide one.
            ("alice", "native_brain", OK),
            # A row for a service that is not the native brain must not count.
            ("alice", "ntfy", FAIL),
            # A row left behind by a removed user is not a credential any
            # current user has.
            ("gone", "native_brain", FAIL),
        ],
        ids=["own-native-key", "another-service", "unlisted-user"],
    )
    def test_a_per_user_secret(self, make_config, monkeypatch, tmp_path, owner, service, status):
        r = self._run(self._stored(make_config, monkeypatch, tmp_path, owner, service))
        assert r.status == status
        if status == OK:
            assert "1 user" in r.detail

    def test_a_stored_key_needs_the_store_key_to_be_readable(
        self, make_config, monkeypatch, tmp_path,
    ):
        """Without `ISTOTA_SECRET_KEY` no stored row can be decrypted, so
        counting one would report a credential the daemon cannot use."""
        config = self._stored(make_config, monkeypatch, tmp_path)
        monkeypatch.delenv("ISTOTA_SECRET_KEY", raising=False)
        assert self._run(config).status == FAIL

    def test_a_missing_database_is_no_key_rather_than_a_traceback(
        self, make_config, monkeypatch, tmp_path,
    ):
        """And it must not *create* one on the way to finding out.

        `secrets_store.secret_exists` opens read-write and commits, so against
        an absent path it leaves a zero-byte database that a later read reports
        as `no such table` — which reads as corruption rather than absence.
        `check_framework_db` states the rule; this check now follows it.

        The sidecars are no longer part of that reason (ISSUE-458): a
        read-write open is the one that *removes* them, which is why
        `connect_read_only` uses one wherever there is nothing to recover.
        """
        from istota.config import UserConfig

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
        root = tmp_path / "empty"
        root.mkdir()
        config = self._native(make_config)
        config.db_path = root / "absent.db"
        config.users = {"alice": UserConfig()}
        assert self._run(config).status == FAIL
        assert list(root.iterdir()) == []

    def test_it_opens_the_database_read_only(
        self, make_config, monkeypatch, tmp_path,
    ):
        """The mechanism, not a side effect of it.

        Asserted on the `sqlite3.connect` arguments because the observable
        outcomes are both weak: a WAL database already has its sidecars from
        `init_db`, so a before/after listing cannot see a read-write open, and
        the missing-file case is covered by an `exists()` guard that a partial
        revert would leave in place. Reverting to `secrets_store.secret_exists`
        — which connects read-write and commits — turns this red, because that
        helper's own connect carries no mode at all.

        **Which mode is not the assertion, since ISSUE-458.** The URI is
        `mode=ro` or `mode=rw` depending on whether the database has a hot
        journal, and this fixture's `db.init_db` leaves a connection open, so
        the read-only branch is the one taken here. What is pinned is that a
        mode is named at all — never the default `rwc`, which would create a
        missing database — and, on the read-write branch, that the write is
        withheld, since a revert dropping `PRAGMA query_only` would hand doctor
        a writable connection and satisfy a mode-only check.
        """
        import sqlite3

        config = self._stored(make_config, monkeypatch, tmp_path)

        opens: list[tuple[tuple, dict]] = []
        statements: list[str] = []
        real_connect = sqlite3.connect

        class _Recording(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                statements.append(str(sql))
                return super().execute(sql, *a, **kw)

        def _recording(*args, **kwargs):
            opens.append((args, kwargs))
            return real_connect(*args, factory=_Recording, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _recording)
        r = self._run(config)

        assert r.status == OK
        assert opens, "the check reached no database at all"
        modes = set()
        for args, kwargs in opens:
            assert kwargs.get("uri") is True, (args, kwargs)
            target = str(args[0])
            assert "?mode=ro" in target or "?mode=rw" in target, (args, kwargs)
            modes.add("ro" if "?mode=ro" in target else "rw")
        if "rw" in modes:
            assert any(
                "query_only" in s.lower() for s in statements
            ), f"the write was never withheld: {statements}"

    @pytest.mark.parametrize(
        "brain_fields,status",
        [
            # The D11 case: the base kind is a CLI brain and native is reachable
            # only because a room may pin it.
            ({"room_selectable": ["native"]}, FAIL),
            ({}, SKIP),
            (None, SKIP),
        ],
        ids=["room-allowlists-native", "claude_code-without-allowlist", "default-config"],
    )
    def test_a_cli_base_kind(self, make_config, brain_fields, status):
        from istota.config import BrainConfig

        if brain_fields is None:
            config = make_config()
        else:
            config = make_config(brain=BrainConfig(kind="claude_code", **brain_fields))
        assert self._run(config).status == status


def _sqlite_with_table(db_path, ddl="CREATE TABLE t (a INTEGER)"):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.commit()
    conn.close()
    return db_path


class TestFrameworkDb:
    def _run(self, make_config, db_path):
        return run_checks(make_config(db_path=db_path), only=("runtime.framework_db",))[0]

    def test_missing_db_warns(self, make_config, tmp_path):
        r = self._run(make_config, tmp_path / "absent.db")
        assert r.status == WARN
        assert r.remedy
        assert r.scope == DEPLOYMENT

    def test_clean_db_is_ok(self, make_config, tmp_path):
        r = self._run(make_config, _sqlite_with_table(tmp_path / "istota.db"))
        assert r.status == OK

    def test_a_zero_length_db_warns_rather_than_reading_clean(self, make_config, tmp_path):
        """SQLite treats a zero-length file as a valid empty database, so
        `quick_check` used to report `quick_check clean` about it (ISSUE-412)."""
        db_path = tmp_path / "istota.db"
        db_path.touch()
        r = self._run(make_config, db_path)
        assert r.status == WARN
        assert "0 bytes" in r.detail
        assert "no schema" in r.detail
        assert "quick_check clean" not in r.detail
        assert "istota init" in r.remedy

    def test_a_header_only_db_warns_too(self, make_config, tmp_path):
        """Size is only a proxy for "has a schema": an interrupted `istota init`
        or a bare `PRAGMA journal_mode=WAL` leaves a non-zero file with none."""
        import sqlite3

        db_path = tmp_path / "istota.db"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        assert db_path.stat().st_size > 0, "this test needs a non-empty file"
        r = self._run(make_config, db_path)
        assert r.status == WARN
        assert "no schema" in r.detail
        assert "istota init" in r.remedy

    def test_unopenable_db_fails(self, make_config, tmp_path):
        db_path = tmp_path / "istota.db"
        db_path.write_bytes(b"this is definitely not a sqlite database")
        assert self._run(make_config, db_path).status == FAIL

    def test_does_not_repair(self, make_config, tmp_path, monkeypatch):
        """Doctor is a diagnostic. `check_db_health` owns the REINDEX."""
        from istota.maintenance import db_health

        db_path = _sqlite_with_table(tmp_path / "istota.db")

        def _fail(*args, **kwargs):
            raise AssertionError("doctor must not repair the database")

        monkeypatch.setattr(db_health, "reindex", _fail)
        monkeypatch.setattr(db_health, "check_and_repair", _fail)
        self._run(make_config, db_path)


class TestTaskFailureRate:
    """The recent-failure-rate query, lifted verbatim off the two hand-rolled
    probes in `heartbeat.py` and `commands.py`.

    Driven through the real `db` helpers against a `tmp_path` database rather
    than by patching the query: the predicate and the NULL coalescing are the
    whole subject, and a patched query would be asserting about the patch.
    """

    def _db(self, tmp_path, statuses):
        from istota import db

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for status in statuses:
                task_id = db.create_task(conn, prompt="p", user_id="alice")
                db.update_task_status(conn, task_id, status)
        return db_path

    def _run(self, make_config, db_path):
        return run_checks(make_config(db_path=db_path), only=("runtime.task_failure_rate",))[0]

    @pytest.mark.parametrize(
        "statuses,status",
        [
            (["failed", "failed", "completed"], WARN),
            # `failed >= completed`, not `>`: one and one is the predicate.
            (["failed", "completed"], WARN),
            (["failed", "failed"], WARN),
            (["failed", "completed", "completed"], OK),
            (["completed", "completed"], OK),
            # `SUM` over no rows is NULL, not 0; a naive port raises here, on
            # the commonest deployment state.
            ([], OK),
        ],
        ids=["majority-failed", "boundary", "all-failed", "minority-failed", "none-failed", "empty-window"],
    )
    def test_the_rate(self, make_config, tmp_path, statuses, status):
        r = self._run(make_config, self._db(tmp_path, statuses))
        assert r.status == status
        # A symptom, not a broken deployment: it never fails the start-up
        # report or `istota doctor`'s exit code.
        assert exit_code([r]) == 0
        if status == WARN:
            assert r.remedy.strip()

    def test_the_warning_names_both_counts(self, make_config, tmp_path):
        r = self._run(make_config, self._db(tmp_path, ["failed", "failed", "completed"]))
        assert "2" in r.detail and "1" in r.detail

    def test_old_rows_are_outside_the_window(self, make_config, tmp_path):
        from istota import db

        db_path = self._db(tmp_path, ["failed", "failed"])
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET created_at = datetime('now', '-2 hours')")
        assert self._run(make_config, db_path).status == OK

    def test_skips_with_no_database(self, make_config, tmp_path):
        r = self._run(make_config, tmp_path / "absent.db")
        assert r.status == SKIP
        assert r.scope == DEPLOYMENT

    def test_fails_with_no_tasks_table(self, make_config, tmp_path):
        """`check_framework_db` never touches `tasks`, so a schema-less database
        passes it. This is where that surfaces."""
        db_path = _sqlite_with_table(tmp_path / "istota.db", "CREATE TABLE unrelated (a INTEGER)")
        r = self._run(make_config, db_path)
        assert r.status == FAIL
        assert "init" in r.remedy

    def test_a_corrupt_file_gets_the_restore_remedy_not_init(self, make_config, tmp_path):
        """The URI form opens lazily, so a non-database connects and raises on
        the first execute, in the branch a missing table lands in."""
        db_path = tmp_path / "istota.db"
        db_path.write_bytes(b"this is definitely not a sqlite database")
        r = self._run(make_config, db_path)
        assert r.status == FAIL
        assert "db_restore" in r.remedy
        assert "init" not in r.remedy

    def test_opens_the_database_without_writing_to_it(
        self, make_config, tmp_path, monkeypatch,
    ):
        """`sudo istota doctor` must not write to the database it inspects.

        Asserted on the URI, not a directory listing: `db.init_db` leaves a hot
        WAL here, so a listing is equal under every implementation. The
        no-strays and no-checkpoint guarantees belong to
        `tests/test_sqlite_util.py::TestConnectReadOnly`; this pins that the
        check goes through that helper. Either mode is accepted (ISSUE-458);
        never the default `rwc`, which would create a missing database.
        """
        import sqlite3

        seen = []
        real_connect = sqlite3.connect

        def _spy(target, *args, **kwargs):
            seen.append(target)
            return real_connect(target, *args, **kwargs)

        db_path = self._db(tmp_path, ["completed"])

        monkeypatch.setattr(sqlite3, "connect", _spy)
        assert self._run(make_config, db_path).status == OK
        assert seen, "the check opened no connection"
        assert all(
            str(t).startswith("file:")
            and ("?mode=ro" in str(t) or "?mode=rw" in str(t))
            for t in seen
        ), seen

    def test_is_not_live_or_deep(self):
        """It opens a file and spawns nothing, so it runs for every caller."""
        assert "runtime.task_failure_rate" not in doctor.LIVE_CHECKS
        assert "runtime.task_failure_rate" not in DEEP_CHECKS


class _ModelReached(BaseException):
    """Deliberately not an ``Exception``: both `check_model_execution` and
    `run_checks` catch `Exception` and turn it into a FAIL result, so a sentinel
    raising `AssertionError` would be swallowed twice over."""


#: Captured before any test can patch it, so the stand-in below can delegate.
_REAL_SUBPROCESS_RUN = subprocess.run


class _NoModel:
    """A `subprocess.run` stand-in that fails the test if the *model probe* runs.

    It discriminates on the probe's own marker rather than refusing everything,
    because `runtime.model_cli`, `runtime.tmux` and the forge checks all spawn a
    real `--version` in a whole-registry run. Everything else is delegated.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        if any(_MODEL_MARKER in str(part) for part in (cmd or ())):
            self.calls.append(cmd)
            raise _ModelReached("a test reached a real model invocation")
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)


class TestModelExecution:
    """The live `claude -p` probe.

    Every case patches `subprocess.run` and asserts the patched callable was
    the one invoked, so no case can bill the account by accident.
    """

    def _config(self, make_config, make_user_config, **overrides):
        fields = {"users": {"alice": make_user_config()}, "admin_users": {"alice"}}
        fields.update(overrides)
        return make_config(**fields)

    def _stub(self, monkeypatch, *, stdout="healthcheck-ok\n", stderr="", raises=None, returncode=0):
        calls = []

        def _run_stub(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(subprocess, "run", _run_stub)
        return calls

    def _unsandboxed(self, monkeypatch):
        """Pin the wrap off, so a Linux host and a macOS host answer alike."""
        from istota import executor

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: False)

    def _sandboxed(self, monkeypatch, wrap=lambda cmd, *a, **kw: list(cmd)):
        from istota import executor

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: True)
        monkeypatch.setattr(executor, "build_bwrap_cmd", wrap)

    def _run(self, config, **kwargs):
        kwargs.setdefault("live", True)
        return run_checks(config, only=("runtime.model_execution",), **kwargs)[0]

    def test_skips_under_probe_false_even_when_live(
        self, make_config, make_user_config, monkeypatch
    ):
        """The no-spawn constraint at the top of doctor.py is unconditional,
        and this is the most expensive possible way to violate it."""
        monkeypatch.setattr(subprocess, "run", _NoModel())
        r = self._run(self._config(make_config, make_user_config), probe=False)
        assert r.status == SKIP

    def test_skips_on_a_native_brain(self, make_config, make_user_config, monkeypatch):
        from istota.config import BrainConfig

        monkeypatch.setattr(subprocess, "run", _NoModel())
        r = self._run(self._config(make_config, make_user_config, brain=BrainConfig(kind="native")))
        assert r.status == SKIP
        assert "native" in r.detail

    def test_skips_with_no_user_to_run_as(self, make_config, monkeypatch):
        sentinel = _NoModel()
        monkeypatch.setattr(subprocess, "run", sentinel)
        r = self._run(make_config())
        assert r.status == SKIP
        assert "user" in r.detail
        assert r.scope == DEPLOYMENT
        # A scope assertion is true of the FAIL a swallowed sentinel would
        # produce as well, so it cannot stand alone on this check.
        assert sentinel.calls == []

    def test_ok_when_the_echo_comes_back(self, make_config, make_user_config, monkeypatch):
        self._unsandboxed(monkeypatch)
        calls = self._stub(monkeypatch)
        r = self._run(self._config(make_config, make_user_config))
        assert r.status == OK
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd[0] == "claude" and "healthcheck-ok" in " ".join(cmd)
        assert kwargs["timeout"] == doctor.MODEL_PROBE_TIMEOUT

    @pytest.mark.parametrize(
        "stub,named",
        [
            # The excerpt is what makes the finding actionable; without
            # asserting it the `observed` chain could return "" and pass.
            ({"stdout": "something else", "stderr": "boom"}, ("boom",)),
            ({"stdout": "only on stdout", "returncode": 7}, ("only on stdout", "7")),
            ({"stdout": "", "stderr": ""}, ("no output",)),
            ({"raises": subprocess.TimeoutExpired(cmd=["claude"], timeout=30)}, ("timed out",)),
            ({"raises": FileNotFoundError("no claude")}, ()),
        ],
        ids=["wrong-output", "stdout-fallback-and-exit-status", "no-output", "timeout", "missing-binary"],
    )
    def test_fails_and_says_why(self, make_config, make_user_config, monkeypatch, stub, named):
        self._unsandboxed(monkeypatch)
        calls = self._stub(monkeypatch, **stub)
        r = self._run(self._config(make_config, make_user_config))
        assert r.status == FAIL
        assert r.remedy.strip()
        assert calls
        for text in named:
            assert text in r.detail

    def test_the_detail_bounds_a_long_stream(self, make_config, make_user_config, monkeypatch):
        """A subprocess stream is unbounded; a rendered check line is not."""
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch, stdout="nope", stderr="x " * 5000)
        r = self._run(self._config(make_config, make_user_config))
        assert len(r.detail) < 400, len(r.detail)

    @pytest.mark.parametrize(
        "users,admins,expected",
        [
            # The probe answers about the deployment, so which user it ran as
            # belongs in the detail.
            (("alice", "bob"), {"bob"}, "bob"),
            # An empty `admin_users` means everyone is admin, so it names
            # nobody and the user list decides.
            (("alice",), set(), "alice"),
            # With several admins it must still run in an admin's sandbox
            # shape; the first configured user here is a non-admin.
            (("alice", "bob", "carol"), {"bob", "carol"}, "bob"),
            # `admin_users` comes from /etc/istota/admins and has no relation
            # to `config.users`; an admin with nothing behind it would get a
            # namespace around a workspace that does not exist.
            (("alice",), {"ghost"}, "alice"),
        ],
        ids=["single-admin", "everyone-admin", "admin-who-is-a-user", "admin-with-no-user-config"],
    )
    def test_which_user_it_runs_as(
        self, make_config, make_user_config, monkeypatch, users, admins, expected
    ):
        self._unsandboxed(monkeypatch)
        self._stub(monkeypatch)
        config = self._config(
            make_config, make_user_config,
            users={u: make_user_config() for u in users}, admin_users=admins,
        )
        assert expected in self._run(config).detail

    def test_refuses_rather_than_creating_the_per_user_temp_dir(
        self, make_config, make_user_config, monkeypatch
    ):
        """Created by `sudo istota doctor`, this directory would be root-owned,
        and every later task binds it read-write and cannot write to it."""
        from istota import executor

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: True)
        monkeypatch.setattr(subprocess, "run", _NoModel())
        config = self._config(make_config, make_user_config)
        user_temp = Path(config.temp_dir) / "alice"
        assert not user_temp.exists()

        r = self._run(config)

        assert r.status == SKIP
        assert not user_temp.exists(), "the check created the directory it was asked about"

    def test_reads_user_resources_without_writing(
        self, make_config, make_user_config, monkeypatch
    ):
        """`db.get_db` connects read-write and *commits*; this must not, nor
        create a missing database (ISSUE-458 dropped the sidecar reason)."""
        import sqlite3

        from istota import db

        seen = []
        real_connect = sqlite3.connect

        def _spy(target, *args, **kwargs):
            seen.append(str(target))
            return real_connect(target, *args, **kwargs)

        config = self._config(make_config, make_user_config)
        db.init_db(Path(config.db_path))
        (Path(config.temp_dir) / "alice").mkdir(parents=True)

        self._sandboxed(monkeypatch)
        self._stub(monkeypatch)
        monkeypatch.setattr(sqlite3, "connect", _spy)

        assert self._run(config).status == OK
        assert seen, "the resource lookup opened no connection"
        assert all(("?mode=ro" in t or "?mode=rw" in t) for t in seen), seen

    def test_an_absent_database_yields_no_resources_and_creates_nothing(
        self, make_config, make_user_config, monkeypatch
    ):
        """The one read-only call site with no `exists()` guard: it relies on
        `connect_read_only` naming a mode rather than defaulting to `rwc`, so a
        missing database raises and its bare `except` returns `[]`."""
        config = self._config(make_config, make_user_config)
        db_path = Path(config.db_path)
        assert not db_path.exists()
        (Path(config.temp_dir) / "alice").mkdir(parents=True)
        self._sandboxed(monkeypatch)
        self._stub(monkeypatch)

        assert doctor._read_user_resources(config, "alice") == []
        assert not db_path.exists(), "the check created the database it was reading"

    def test_wraps_under_effective_sandboxing_not_the_flag(
        self, make_config, make_user_config, monkeypatch
    ):
        """On the shipped Docker stack `sandbox_enabled` is true and the
        namespace cannot be created, so a wrap there dies for the wrong reason."""
        from istota import executor

        def _must_not_wrap(*args, **kwargs):
            raise AssertionError("wrapped a probe on a deployment with no sandbox")

        monkeypatch.setattr(executor, "effective_sandboxing", lambda config: False)
        monkeypatch.setattr(executor, "build_bwrap_cmd", _must_not_wrap)
        calls = self._stub(monkeypatch)
        assert self._run(self._config(make_config, make_user_config)).status == OK
        assert calls[0][0][0] == "claude"

    def test_wraps_when_the_sandbox_is_effective(
        self, make_config, make_user_config, monkeypatch
    ):
        from istota.executor import SandboxProfile

        seen = {}

        def _wrap(cmd, config, task, is_admin, user_resources, user_temp_dir, *a, **kw):
            seen["profile"] = kw.get("profile")
            seen["task"] = task
            return ["bwrap", "--", *cmd]

        self._sandboxed(monkeypatch, _wrap)
        calls = self._stub(monkeypatch)
        config = self._config(make_config, make_user_config)
        # The check refuses to create this itself; the daemon owns that.
        (Path(config.temp_dir) / "alice").mkdir(parents=True)
        assert self._run(config).status == OK
        assert calls[0][0][0] == "bwrap"
        assert seen["profile"] == SandboxProfile.CLAUDE
        assert seen["task"].user_id == "alice"

    def test_env_comes_from_build_model_cli_env(
        self, make_config, make_user_config, monkeypatch
    ):
        """A daemon-side model call with no task behind it: its env is that
        function's business, not `os.environ`'s."""
        from istota import executor

        self._unsandboxed(monkeypatch)
        monkeypatch.setattr(executor, "build_model_cli_env", lambda config: {"MARKER": "1"})
        calls = self._stub(monkeypatch)
        self._run(self._config(make_config, make_user_config))
        assert calls[0][1]["env"] == {"MARKER": "1"}


class TestLiveChecks:
    """The second opt-in axis. `DEEP_CHECKS` means "spawns a namespace";
    `LIVE_CHECKS` means "costs money", and no caller wants one flag for both."""

    def test_the_two_axes_are_disjoint(self):
        assert not (doctor.LIVE_CHECKS & DEEP_CHECKS)

    def test_the_sentinel_matches_the_probes_own_marker(self):
        """`_NoModel` discriminates on this string. A rename in doctor.py that
        did not reach here would disarm every guard in this file at once."""
        assert doctor._MODEL_PROBE_MARKER == _MODEL_MARKER

    def test_deep_checks_is_exactly_the_mask_probe(self):
        """A budget guard for `web_app._doctor_deep_timeout`, which is
        `DEEP_TIMEOUT` plus headroom for exactly one deep check."""
        assert DEEP_CHECKS == frozenset({"sandbox.masks"})

    @pytest.mark.parametrize(
        "flags,present,absent",
        [
            ({"deep": True}, None, "runtime.model_execution"),
            ({"live": True}, "runtime.model_execution", "sandbox.masks"),
        ],
        ids=["deep-selects-no-live-check", "live-selects-it-and-not-deep"],
    )
    def test_each_flag_selects_only_its_own_axis(self, make_config, monkeypatch, flags, present, absent):
        monkeypatch.setattr(subprocess, "run", _NoModel())
        names = {r.name for r in run_checks(make_config(), **flags)}
        assert absent not in names
        if present:
            assert present in names

    def test_live_filters_before_invoking(self, make_config, tmp_path, monkeypatch):
        """A live check discarded after the fact is a live check that already
        billed. Same shape as `test_only_selects_before_invoking`."""
        called = []

        def _explodes(config, probe):
            called.append(1)
            raise AssertionError("a live check must not run without live=True")

        monkeypatch.setattr(
            doctor,
            "CHECKS",
            (("runtime.platform", doctor.check_platform), ("live.check", _explodes)),
        )
        monkeypatch.setattr(doctor, "LIVE_CHECKS", frozenset({"live.check"}))
        monkeypatch.setitem(doctor.CHECK_SCOPES, "live.check", DEPLOYMENT)
        results = run_checks(make_config(), deep=True)
        assert called == []
        assert [r.name for r in results] == ["runtime.platform"]


class TestWritableDirs:
    def test_writable_dirs_are_ok_one_result_per_directory(self, make_config, tmp_path):
        results = run_checks(make_config(), only=("runtime.writable_dirs",))
        assert all(r.status == OK for r in results), [
            (r.name, r.status, r.detail) for r in results if r.status != OK
        ]
        names = {r.name for r in results}
        assert "runtime.writable_dirs.temp_dir" in names
        assert "runtime.writable_dirs.module_db_root" in names

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


class TestMountLiveness:
    @staticmethod
    def _nextcloud_backed(make_config, **overrides):
        from istota.config import NextcloudConfig

        return make_config(
            nextcloud=NextcloudConfig(url="https://cloud.example"), **overrides
        )

    @pytest.mark.parametrize("url", ["", "https://cloud.example"], ids=["local", "nextcloud-url"])
    def test_skips_when_no_mount_configured(self, make_config, url):
        from istota.config import NextcloudConfig

        config = make_config(nextcloud=NextcloudConfig(url=url), nextcloud_mount_path=None)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == SKIP

    def test_configured_but_not_mounted_fails_without_nextcloud_url(
        self, make_config, tmp_path,
    ):
        # `make_config` points nextcloud_mount_path at a plain tmp_path dir,
        # which is on the same filesystem as its parent and so is not a mount.
        config = make_config()
        assert config.storage_is_nextcloud is False
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == FAIL
        assert r.remedy

    def test_a_real_mount_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.os.path, "ismount", lambda p: True)
        config = self._nextcloud_backed(make_config)
        r = run_checks(config, only=("runtime.mount_liveness",))[0]
        assert r.status == OK


# The root conftest neutralizes `subscription_usage.get_snapshot` for the whole
# suite, so the doctor sweep on a developer's macOS laptop cannot read the real
# keychain or issue a live request. This file drives the real function on
# purpose, so it captures it at import time — the same technique the money
# fixture's docstring names.
_REAL_GET_SNAPSHOT = subscription_usage.get_snapshot

# The credential a leak test looks for. Long enough that doctor's own `redact()`
# would catch it if it were a configured secret — it is not one, which is the
# point: the check must keep it out of the report on its own.
_TOKEN_SENTINEL = "sk-ant-oat01-" + "z" * 40
_TOKEN_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN_SENTINEL}


class _UsageTransport:
    """A stub `subscription_usage` transport that records every call.

    Recording rather than raising: `run_checks` turns an exception from a check
    into a FAIL result, so a transport that asserted by raising would be
    swallowed and the test would pass whatever the check did.
    """

    def __init__(self, status=200, body=b"{}", response_headers=None):
        self.status = status
        self.body = body
        self.response_headers = dict(response_headers or {})
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return self.status, self.body, dict(self.response_headers)


# A reset far enough from a minute boundary that the rendered countdown cannot
# tick over between building the payload and reading the result: 1h 04m 30s, so
# the assertion holds for any delay under 30 seconds.
_RESETS_IN = 3870


def _usage_body(*percents, resets_in=_RESETS_IN):
    """A `limits[]` payload with one window per percentage, resetting soon.

    `resets_at` comes from the wall clock because the check passes its own
    `time.time()` through to the fetch, the countdown and the staleness age.
    """
    kinds = ["session", "weekly_all"]
    resets_at = datetime.fromtimestamp(time.time() + resets_in, tz=timezone.utc).isoformat()
    return json.dumps(
        {
            "limits": [
                {
                    "kind": kinds[i] if i < len(kinds) else f"other_{i}",
                    "group": "weekly",
                    "percent": percent,
                    "severity": "normal",
                    "resets_at": resets_at,
                    "scope": None,
                    "is_active": True,
                }
                for i, percent in enumerate(percents)
            ]
        }
    ).encode()


def _usage_config(make_config, **fields):
    from istota.config import BrainConfig, ClaudeCodeBrainConfig

    return make_config(brain=BrainConfig(claude_code=ClaudeCodeBrainConfig(**fields)))


# Never the running developer's real home. `get_snapshot(home=None)` means "use
# `Path.home()`", not "there is no home", so a helper defaulting to None would
# read `~/.claude/.credentials.json` on the machine running the suite.
_NO_HOME = Path("/nonexistent/istota-test-home")


def _drive_usage(
    monkeypatch, *, transport=None, env=None, home=_NO_HOME, darwin_blob=None
):
    """Reinstate the real `get_snapshot` with only the host substituted.

    The check has nowhere to pass a transport, an environment or a home, so the
    resolver would read the developer's own keychain and fetch live. Supplying
    those three behind a wrapper runs the whole real policy (resolution, TTL,
    cache, fetch, stale fallback) against a stub host. `now_ts` is passed
    through rather than frozen, since the check computes the staleness age
    against the same clock it hands the module.
    """
    from istota.usage import subscription as su

    if darwin_blob is not None:
        monkeypatch.setattr(su.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            su.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, darwin_blob, ""),
        )
    else:
        monkeypatch.setattr(su.platform, "system", lambda: "Linux")

    calls = []

    def _wrapper(config, *, now_ts, **kwargs):
        calls.append(now_ts)
        return _REAL_GET_SNAPSHOT(
            config,
            now_ts=now_ts,
            transport=transport,
            env={} if env is None else env,
            home=home,
        )

    monkeypatch.setattr(su, "get_snapshot", _wrapper)
    return calls


def _credential_file(tmp_path, token):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token}})
    )
    return home


class TestSubscriptionUsage:
    """`runtime.subscription_usage` — the plan's own budget.

    On a subscription deployment the dashboard's cost column is deliberately
    blank, so the rate-limit windows are the only budget there is. Every test
    here asserts the check did not SKIP before asserting anything else, since a
    SKIP is this check's natural resting state on an unconfigured host.
    """

    def _result(self, config, **kwargs):
        results = run_checks(config, only=("runtime.subscription_usage",), **kwargs)
        assert len(results) == 1
        return results[0]

    def _drive(self, monkeypatch, *percents, status=200, body=None):
        """Drive the check against a stub endpoint, authenticated from the env."""
        transport = _UsageTransport(
            status=status, body=_usage_body(*percents) if body is None else body
        )
        _drive_usage(monkeypatch, transport=transport, env=_TOKEN_ENV)
        return transport

    def test_it_is_registered_as_a_deployment_check_and_is_not_deep(self):
        """It reads a network endpoint, not the image, and spawns no namespace."""
        assert doctor.CHECK_SCOPES["runtime.subscription_usage"] == DEPLOYMENT
        assert "runtime.subscription_usage" not in DEEP_CHECKS

    def test_disabled_by_config_skips(self, make_config, monkeypatch):
        transport = _UsageTransport(body=_usage_body(40))
        _drive_usage(monkeypatch, transport=transport)
        r = self._result(_usage_config(make_config, subscription_usage=False))
        assert r.status == SKIP
        assert "disabled" in r.detail
        assert transport.calls == []

    def test_probe_false_skips_without_a_network_call(self, make_config, monkeypatch):
        """`TestRegistry` proves no check spawns under probe=False; this one
        must clear the network bar too, without even asking for a snapshot."""
        transport = _UsageTransport(body=_usage_body(40))
        snapshots = _drive_usage(monkeypatch, transport=transport, env=_TOKEN_ENV)
        r = self._result(_usage_config(make_config), probe=False)
        assert r.status == SKIP
        assert "probe disabled" in r.detail
        assert transport.calls == []
        assert snapshots == [], "probe=False must not even ask for a snapshot"

    def test_no_credential_skips(self, make_config, tmp_path, monkeypatch):
        transport = _UsageTransport(body=_usage_body(40))
        _drive_usage(monkeypatch, transport=transport, env={}, home=tmp_path / "nowhere")
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert "no Claude Code OAuth credential found" in r.detail
        assert transport.calls == [], "nothing to authenticate with, so nothing to send"

    def test_a_healthy_plan_is_ok_and_names_every_window(self, make_config, monkeypatch):
        """Worst first, and *all* of them: "5-hour at 12%, weekly at 94%" and
        the reverse call for different operator responses."""
        self._drive(monkeypatch, 12, 40)
        r = self._result(_usage_config(make_config))
        assert r.status == OK
        assert "5-hour at 12%" in r.detail
        assert "Weekly (all models) at 40%" in r.detail
        assert r.detail.index("Weekly (all models)") < r.detail.index("5-hour")
        assert "resets in 1h 04m" in r.detail

    @pytest.mark.parametrize(
        "percent,expect_warn",
        [(0, False), (79.9, False), (80, True), (94.9, True), (95, True), (100, True)],
    )
    def test_the_thresholds(self, make_config, monkeypatch, percent, expect_warn):
        self._drive(monkeypatch, percent)
        r = self._result(_usage_config(make_config))
        assert r.status == (WARN if expect_warn else OK)
        if expect_warn:
            assert r.remedy, "a WARN an operator cannot act on is a log line"

    @pytest.mark.parametrize(
        "warn,high,percent,expect_warn",
        [
            (50.0, 60.0, 45, False),
            (50.0, 60.0, 55, True),
            # `warn` above `high` would otherwise make the band unreachable.
            # The loader corrects the pair; this is the second line. 75% sits
            # below the default warn of 80 too.
            (90.0, 70.0, 75, True),
        ],
        ids=["configured-below", "configured-above", "inverted-pair-still-warns"],
    )
    def test_the_configured_thresholds_are_the_ones_that_are_read(
        self, make_config, monkeypatch, warn, high, percent, expect_warn
    ):
        """A check that ignored `[brain.claude_code]` and used a hardcoded 80/95
        would pass every case above; it fails these."""
        config = _usage_config(
            make_config,
            subscription_usage_warn_percent=warn,
            subscription_usage_high_percent=high,
        )
        self._drive(monkeypatch, percent)
        assert self._result(config).status == (WARN if expect_warn else OK)

    @pytest.mark.parametrize(
        "fallback,present,absent",
        [
            ("native", "native", "No [brain] fallback"),
            ("", "No [brain] fallback is configured", "fail over"),
        ],
        ids=["configured-fallback", "no-fallback"],
    )
    def test_the_busy_remedy_names_the_fallback_or_its_absence(
        self, make_config, monkeypatch, fallback, present, absent
    ):
        """ISSUE-362: the remedy used to promise a failover unconditionally,
        and no kind has an implicit fallback."""
        from istota.config import BrainConfig, ClaudeCodeBrainConfig

        self._drive(monkeypatch, 97)
        brain = {"fallback": fallback} if fallback else {}
        config = make_config(
            brain=BrainConfig(kind="claude_code", claude_code=ClaudeCodeBrainConfig(), **brain)
        )
        r = self._result(config)
        assert r.status == WARN
        assert present in r.remedy
        assert absent not in r.remedy

    @pytest.mark.parametrize("percent", [0, 79.9, 80, 94.9, 95, 100, 150])
    def test_no_utilization_ever_fails(self, make_config, monkeypatch, percent):
        """A plan at 97% is a fact about the plan, not a defect in the host;
        a FAIL would set the exit code and message every admin."""
        self._drive(monkeypatch, percent)
        r = self._result(_usage_config(make_config))
        assert r.status != FAIL
        assert r.status != SKIP, "a SKIP here would pass this test on a broken check"

    @pytest.mark.parametrize("source", ["env", "file", "keychain"])
    def test_a_rejected_credential_skips_naming_which_one(
        self, make_config, tmp_path, monkeypatch, source
    ):
        """Which source the endpoint refused is the whole diagnostic, and only
        its name is fit to print. SKIP with no remedy: the endpoint does not
        serve the setup-token credential both server shapes deploy, so a WARN
        was permanent there and named no action anyone could take."""
        transport = _UsageTransport(status=403, body=b'{"error":"forbidden"}')
        _drive_usage(monkeypatch, transport=transport, **self._sources(tmp_path, source))
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert "403" in r.detail
        assert source in r.detail
        assert not r.remedy, "a SKIP names no repair"
        assert transport.calls, "a resolvable credential should have been tried"

    @pytest.mark.parametrize("source", ["env", "file", "keychain"])
    @pytest.mark.parametrize("status", [200, 403])
    def test_the_token_value_is_never_in_the_report(
        self, make_config, tmp_path, monkeypatch, source, status
    ):
        """Doctor's `redact()` scans `config_secrets`, and this credential is not
        in the config at all, so nothing downstream would catch a leak."""
        transport = _UsageTransport(status=status, body=_usage_body(40))
        _drive_usage(monkeypatch, transport=transport, **self._sources(tmp_path, source))
        r = self._result(_usage_config(make_config))
        # A refused credential is a legitimate SKIP, so "the check ran" is
        # spelled against evidence it reached the endpoint and reported back.
        assert transport.calls, "the check never issued a request"
        assert ("403" in r.detail) if status == 403 else ("40" in r.detail)
        assert _TOKEN_SENTINEL not in r.detail + r.remedy
        assert "sk-ant" not in r.detail + r.remedy
        sent = transport.calls[0][1]["Authorization"]
        assert _TOKEN_SENTINEL in sent, "the token belongs in the header and nowhere else"

    @staticmethod
    def _sources(tmp_path, source):
        """Resolver inputs that make exactly `source` the winning branch."""
        blob = json.dumps({"claudeAiOauth": {"accessToken": _TOKEN_SENTINEL}})
        if source == "env":
            return {"env": _TOKEN_ENV, "home": tmp_path / "no"}
        if source == "file":
            return {"env": {}, "home": _credential_file(tmp_path, _TOKEN_SENTINEL)}
        return {"env": {"USER": "someone"}, "home": tmp_path / "no", "darwin_blob": blob}

    @pytest.mark.parametrize(
        "status,body,named",
        [
            (500, b"", "500"),
            # A shipped shape change reads as "nothing to check", not as 0%,
            # and the detail keeps that the request itself succeeded.
            (200, b'{"limits": [], "quince": null}', "no recognizable rate-limit windows"),
        ],
        ids=["unreachable-no-cache", "no-recognizable-windows"],
    )
    def test_no_reading_skips(self, make_config, monkeypatch, status, body, named):
        self._drive(monkeypatch, status=status, body=body)
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert named in r.detail
        assert not r.remedy

    def test_a_windowless_success_skips_rather_than_raising(
        self, make_config, monkeypatch
    ):
        """`get_snapshot` cannot return this today, so the never-FAIL guard is
        driven directly: unguarded it is an IndexError, which `run_checks`
        turns into the one status this check must never produce."""
        from istota.usage import subscription as su

        monkeypatch.setattr(
            su,
            "get_snapshot",
            lambda config, **kwargs: su.UsageSnapshot(fetched_at=time.time()),
        )
        r = self._result(_usage_config(make_config))
        assert r.status == SKIP
        assert r.detail, "a SKIP still has to say why"

    def _seed_cache(self, config, age_seconds, percent=40):
        """Write a good cache entry `age_seconds` old, as a fetch would have."""
        from istota.usage import subscription as su

        now = time.time()
        windows, spend = su.parse_usage(json.loads(_usage_body(percent)), now_ts=now)
        assert windows, "the fixture must really parse, or the test proves nothing"
        su.write_cache(
            su.cache_path(config.db_path.parent),
            su.UsageSnapshot(fetched_at=now - age_seconds, windows=windows, spend=spend),
        )

    @pytest.mark.parametrize(
        "stale_after,age,status,named,absent",
        [
            # An old-but-real reading is worth more than nothing, but says it
            # is old: the countdown is recomputed against now and the
            # percentage is not.
            (3600, 900, OK, ("5-hour at 40%", "last successful reading is 15m old", "500"), ()),
            # The same reading against a 60s window instead of the default.
            (60, 900, SKIP, (), ("5-hour at 40%",)),
            (3600, 7300, SKIP, ("last successful reading is 2h 01m old", "500"), ()),
        ],
        ids=["within-the-window", "configured-window-is-read", "past-the-window"],
    )
    def test_a_stale_reading(
        self, make_config, monkeypatch, stale_after, age, status, named, absent
    ):
        # The TTL is pinned below the seeded age: at the shipping 1800s default
        # a 900s reading is fresh, nothing fetches and no stale branch runs.
        config = _usage_config(
            make_config,
            subscription_usage_stale_after_seconds=stale_after,
            subscription_usage_cache_ttl_seconds=300,
        )
        self._seed_cache(config, age_seconds=age)
        self._drive(monkeypatch, status=500, body=b"")
        r = self._result(config)
        assert r.status == status
        for text in named:
            assert text in r.detail
        for text in absent:
            assert text not in r.detail, "past the window it is not a reading"
        if status == SKIP:
            assert not r.remedy

    def test_a_fresh_cache_is_served_without_a_request(self, make_config, monkeypatch):
        """The TTL is deployment-wide: doctor, the dashboard and `!usage` share it."""
        config = _usage_config(make_config, subscription_usage_cache_ttl_seconds=300)
        self._seed_cache(config, age_seconds=40, percent=90)
        transport = self._drive(monkeypatch, 1)
        r = self._result(config)
        assert r.status == WARN
        assert "5-hour at 90%" in r.detail
        assert transport.calls == []


class TestSkillProxy:
    def _run(self, config):
        return _by_name(run_checks(config, only=("security.skill_proxy",)))

    def test_resolvable_is_ok(self, make_config, tmp_path, monkeypatch):
        _which_only(monkeypatch, "istota-skill", _fake_bin(tmp_path / "bin" / "istota-skill"))
        assert self._run(make_config())["security.skill_proxy"].status == OK

    def test_unresolvable_fails(self, make_config, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        assert self._run(make_config())["security.skill_proxy"].status == FAIL

    def test_skips_when_the_proxy_is_disabled(self, make_config):
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(skill_proxy_enabled=False))
        assert self._run(config)["security.skill_proxy"].status == SKIP

    @pytest.mark.parametrize(
        "tokens,proxy,status",
        [
            # Wording preserved from `_validate_forge_clis`: the tokens still
            # work, but they sit in the environment the model's shell inherits.
            ({}, False, WARN),
            ({"gitlab_token": "", "github_token": ""}, False, SKIP),
            ({}, True, SKIP),
        ],
        ids=["tokens-proxy-off", "no-tokens", "proxy-on"],
    )
    def test_forge_posture(self, make_config, tmp_path, tokens, proxy, status):
        from istota.config import SecurityConfig

        config = _dev_config(make_config, tmp_path, **tokens)
        config.security = SecurityConfig(skill_proxy_enabled=proxy)
        posture = self._run(config)["security.skill_proxy.forge_posture"]
        assert posture.status == status
        if status == WARN:
            assert "readable by anything else the task runs" in posture.detail


class TestProxyPeerCheck:
    """`security.proxy_peer_check` — ISSUE-550."""

    NAME = "security.proxy_peer_check"

    def _run(self, config):
        return _by_name(run_checks(config, only=(self.NAME,)))[self.NAME]

    def _users(self, make_config, n):
        from istota.config import UserConfig

        return make_config(users={f"u{i}": UserConfig() for i in range(n)})

    def test_fails_where_no_peer_can_be_read(self, make_config, monkeypatch):
        from istota.sandbox import peer_process

        monkeypatch.setattr(peer_process, "supported", lambda: False)
        result = self._run(make_config())
        assert result.status == FAIL
        assert "refuses every connection" in result.detail

    @pytest.mark.parametrize(
        "sandboxed,users", [(True, 3), (False, 1)], ids=["sandboxed", "unsandboxed-one-user"]
    )
    def test_ok(self, make_config, monkeypatch, sandboxed, users):
        monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda c, p: (sandboxed, ""))
        monkeypatch.setattr(doctor, "_ptrace_scope", lambda: 0)
        assert self._run(self._users(make_config, users)).status == OK

    @pytest.mark.parametrize("scope", [0, 1, None])
    def test_warns_unsandboxed_multi_user_whatever_ptrace_says(
        self, make_config, monkeypatch, scope,
    ):
        # A closed ptrace does not stop one task planting code in a file
        # another executes, so it must not turn the finding into an OK.
        monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda c, p: (False, ""))
        monkeypatch.setattr(doctor, "_ptrace_scope", lambda: scope)
        result = self._run(self._users(make_config, 2))
        assert result.status == WARN
        assert "best-effort" in result.detail
        assert ("read its memory" in result.detail) == (scope == 0)

    def test_skips_with_the_proxy_off(self, make_config):
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(skill_proxy_enabled=False))
        assert self._run(config).status == SKIP

    def test_stays_out_of_the_config_load_prefix(self):
        # `security.skill_proxy` runs inside every `load_config`; this check
        # imports the executor through the sandbox lookup and must not.
        from istota.config import CONFIG_LOAD_CHECKS

        assert not any(self.NAME.startswith(p) for p in CONFIG_LOAD_CHECKS)

    def test_ptrace_scope_reads_absent_yama_as_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
        assert doctor._ptrace_scope(tmp_path / "missing") == 0
        (tmp_path / "scope").write_text("2\n")
        assert doctor._ptrace_scope(tmp_path / "scope") == 2


#: The three variables istota's own env builders set in a non-daemon process.
_NON_DAEMON_MARKERS = ("ISTOTA_TASK_ID", "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED")
_MODEL_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _clear_env(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


class TestSkillModelCredential:
    """`security.skill_model_credential` — ISSUE-409.

    `code_review`'s reviewers had no credential while the daemon's own brain
    worked, so the only way to find out was to run a review. Both halves are
    driven with the daemon environment controlled.
    """

    WIRING = "security.skill_model_credential.wiring"
    VALUE = "security.skill_model_credential.value"

    def _run(self, config):
        return _by_name(
            run_checks(config, only=("security.skill_model_credential",))
        )

    def test_wiring_ok_and_value_ok(self, make_config, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        results = self._run(make_config())
        wiring = results[self.WIRING]
        assert wiring.status == OK
        assert "code_review" in wiring.detail
        value = results[self.VALUE]
        assert value.status == OK
        # The name, never the value: a CheckResult is rendered into the boot
        # log and the admin dashboard.
        assert "CLAUDE_CODE_OAUTH_TOKEN" in value.detail
        assert "sk-ant-oat-test" not in value.detail

    def test_a_renamed_skill_fails_the_wiring(self, make_config, monkeypatch):
        """The drift guard, and the only one that catches a *re*-regression:
        `SKILL_MODEL_CALLERS` matches skill names at task-build time, so a
        rename silently stops the injection with no import to break."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        monkeypatch.setattr(
            doctor_executor, "SKILL_MODEL_CALLERS", frozenset({"code_reviewe"}),
        )
        results = self._run(make_config())
        wiring = results[self.WIRING]
        assert wiring.status == FAIL
        assert "code_reviewe" in wiring.detail
        # Nothing to say about a credential for a skill that does not resolve.
        assert self.VALUE not in results

    @pytest.mark.parametrize(
        "extra_env",
        [
            {},
            # The negative control for the marker set: the daemon's own
            # environment carries `ISTOTA_*` variables too, so a namespace test
            # rather than named markers would swallow the FAIL everywhere.
            {"ISTOTA_CONFIG_PATH": "/etc/istota/config.toml", "ISTOTA_ADMINS_FILE": "/etc/istota/admins"},
        ],
        ids=["clean-env", "unrelated-istota-variables"],
    )
    def test_no_credential_fails(self, make_config, monkeypatch, extra_env):
        """The positive control for the whole `.value` half. The markers are
        deleted explicitly: every skip below is a way for this to stop
        reaching FAIL quietly."""
        _clear_env(monkeypatch, *_MODEL_CREDENTIALS, *_NON_DAEMON_MARKERS)
        for name, value in extra_env.items():
            monkeypatch.setenv(name, value)
        results = self._run(make_config())
        assert results[self.WIRING].status == OK
        assert results[self.VALUE].status == FAIL

    def test_an_api_key_counts(self, make_config, monkeypatch):
        _clear_env(monkeypatch, "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-test")
        value = self._run(make_config())[self.VALUE]
        assert value.status == OK
        assert "ANTHROPIC_API_KEY" in value.detail

    def test_the_value_half_skips_with_the_proxy_off(self, make_config, monkeypatch):
        """No injection and no strip: the CLI is re-exec'd with the daemon's
        own environment, so whatever authenticates the daemon authenticates it."""
        from istota.config import SecurityConfig

        _clear_env(monkeypatch, *_MODEL_CREDENTIALS)
        config = make_config(security=SecurityConfig(skill_proxy_enabled=False))
        assert self._run(config)[self.VALUE].status == SKIP

    @pytest.mark.parametrize("marker", _NON_DAEMON_MARKERS)
    def test_the_value_half_cannot_answer_from_a_task_env(
        self, make_config, monkeypatch, marker
    ):
        """`istota doctor` is also a command a task can run, and a task's env is
        the one ISSUE-390 strips the Claude credential out of, so absence there
        says nothing about the daemon. Reading it as the daemon's answer
        reported FAIL, and exited 1, about a deployment whose reviews worked."""
        _clear_env(monkeypatch, *_MODEL_CREDENTIALS, *_NON_DAEMON_MARKERS)
        monkeypatch.setenv(marker, "1")
        value = self._run(make_config())[self.VALUE]
        assert value.status == SKIP
        # What was observed, which is the marker rather than a verdict.
        assert marker in value.detail

    def test_the_wiring_half_still_answers_from_a_task_env(
        self, make_config, monkeypatch
    ):
        """The drift guard reads the skill index and the config, never the
        environment, so it is sound wherever it runs."""
        _clear_env(monkeypatch, *_MODEL_CREDENTIALS)
        monkeypatch.setenv("ISTOTA_TASK_ID", "4213")
        assert self._run(make_config())[self.WIRING].status == OK

        monkeypatch.setattr(
            doctor_executor, "SKILL_MODEL_CALLERS", frozenset({"code_reviewe"}),
        )
        assert self._run(make_config())[self.WIRING].status == FAIL

    def test_a_credential_present_in_a_task_env_still_counts(
        self, make_config, monkeypatch
    ):
        """`build_clean_env` copies the token into every task's env, so presence
        is positive evidence wherever it is read; only absence is unanswerable."""
        monkeypatch.setenv("ISTOTA_TASK_ID", "4213")
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        value = self._run(make_config())[self.VALUE]
        assert value.status == OK
        assert "sk-ant-oat-test" not in value.detail

    def test_a_disabled_skill_skips(self, make_config, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        config = make_config()
        config.disabled_skills = ["code_review"]
        results = self._run(config)
        assert results[self.WIRING].status == SKIP
        assert self.VALUE not in results


class TestSecretKey:
    """`security.secret_key` — the master Fernet key for the secrets store.

    With no ``ISTOTA_SECRET_KEY`` every stored credential is unreachable, and
    `_native_key_holders` reports *0 holders* rather than an error, so the
    absence read as "nobody has configured a credential". The standalone wizard
    generated none for seven weeks and nothing said so.
    """

    NAME = "security.secret_key"

    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        _clear_env(monkeypatch, "ISTOTA_SECRET_KEY", *_NON_DAEMON_MARKERS)

    def _run(self, config):
        return _by_name(run_checks(config, only=(self.NAME,)))[self.NAME]

    @pytest.mark.parametrize(
        "extra_env",
        [
            {},
            # The daemon's own environment carries `ISTOTA_*` variables, so a
            # namespace test would skip everywhere and never fail.
            {"ISTOTA_CONFIG_PATH": "/etc/istota/config.toml"},
        ],
        ids=["clean-env", "unrelated-istota-variable"],
    )
    def test_absent_fails(self, make_config, monkeypatch, extra_env):
        """The positive control for the whole check: every skip below is a way
        for it to stop reaching FAIL quietly."""
        for name, value in extra_env.items():
            monkeypatch.setenv(name, value)
        result = self._run(make_config())
        assert result.status == FAIL
        assert result.remedy
        # The consequence, not just the absence: it is what made the gap invisible.
        assert "credential" in result.detail.lower()

    def test_too_short_fails_naming_the_floor_and_the_length(
        self, make_config, monkeypatch
    ):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "changeme")
        result = self._run(make_config())
        assert result.status == FAIL
        assert str(secrets_store._MIN_KEY_LEN) in result.detail
        # The length is not the value.
        assert "8" in result.detail
        assert "changeme" not in result.detail
        assert "changeme" not in result.remedy

    def test_present_is_ok_and_never_reports_the_value(
        self, make_config, monkeypatch
    ):
        key = "0" * 31 + "sentinelkeymaterial" + "1" * 20
        monkeypatch.setenv("ISTOTA_SECRET_KEY", key)
        result = self._run(make_config())
        assert result.status == OK
        # Never the value, and never a prefix of it.
        assert "sentinelkeymaterial" not in result.detail
        assert "sentinelkeymaterial" not in result.remedy
        assert key[:8] not in result.detail

    @pytest.mark.parametrize("marker", _NON_DAEMON_MARKERS)
    def test_absence_in_a_non_daemon_env_skips(
        self, make_config, monkeypatch, marker
    ):
        """`build_clean_env` strips this name from every task env and
        `_PROXY_LOOKUP_BLOCKED` blocks it from the proxy, so absence inside a
        task is guaranteed and says nothing about the daemon."""
        monkeypatch.setenv(marker, "1")
        result = self._run(make_config())
        assert result.status == SKIP
        assert marker in result.detail

    def test_presence_still_answers_from_a_non_daemon_env(
        self, make_config, monkeypatch
    ):
        """Ordering control: a key that is right there must not be swallowed by
        the skip."""
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
        assert self._run(make_config()).status == OK

    def test_the_standalone_remedy_names_the_env_file(self, make_config):
        from istota.config import WebConfig

        config = make_config(web=WebConfig(auth="none"))
        assert config.is_standalone is True
        result = self._run(config)
        assert result.status == FAIL
        assert "istota.env" in result.remedy

    def _standalone(self, make_config, tmp_path, env_file=None):
        from istota.config import WebConfig

        config_path = tmp_path / "cfg" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("")
        if env_file is not None:
            (config_path.parent / "istota.env").write_text(env_file)
        return make_config(web=WebConfig(auth="none"), config_path=config_path)

    def test_a_key_only_in_the_env_file_is_ok(self, make_config, tmp_path):
        """`cmd_serve` is the only thing that sources istota.env, so `istota
        doctor` in an operator's shell carries no marker and never read the
        file; taking its own env as the answer failed a correct install."""
        config = self._standalone(make_config, tmp_path, "ISTOTA_SECRET_KEY=" + "h" * 64 + "\n")
        result = self._run(config)
        assert result.status == OK
        assert "istota.env" in result.detail
        assert "h" * 8 not in result.detail

    def test_a_short_key_in_the_env_file_fails(self, make_config, tmp_path):
        result = self._run(self._standalone(make_config, tmp_path, "ISTOTA_SECRET_KEY=tooshort\n"))
        assert result.status == FAIL
        assert str(secrets_store._MIN_KEY_LEN) in result.detail
        assert "tooshort" not in result.detail

    def test_an_env_file_without_the_key_still_fails(self, make_config, tmp_path):
        """The fallback must not turn a missing key into a pass just because a
        file is there."""
        config = self._standalone(make_config, tmp_path, "ISTOTA_WEB_INSECURE_COOKIES=1\n")
        assert self._run(config).status == FAIL

    def test_the_remedy_names_the_configs_own_env_file(self, make_config, tmp_path):
        """`istota setup -c` puts the env file beside whatever config it was
        given, so a hardcoded default names a path that may not exist."""
        config = self._standalone(make_config, tmp_path)
        result = self._run(config)
        assert result.status == FAIL
        assert str(Path(config.config_path).parent / "istota.env") in result.remedy

    def test_the_floor_is_read_from_the_secrets_store(
        self, make_config, monkeypatch
    ):
        """The drift guard: a raised floor must move the verdict here without
        any edit to doctor."""
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 40)
        assert self._run(make_config()).status == OK
        monkeypatch.setattr(secrets_store, "_MIN_KEY_LEN", 64)
        assert self._run(make_config()).status == FAIL

    def test_scope_is_deployment(self):
        """A key is a property of an install; a bare `docker run` has none."""
        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT

    # An empty `istota_secret_key` documents the secrets store as disabled, so
    # a deployment can legitimately run without one; the same absence on a
    # deployment that has stored something is the original defect. Only an
    # *observed* empty `secrets` table softens the verdict: a table that could
    # not be read has not established that nothing is stored.

    def _db_with_secrets(self, config, rows: int):
        """Create the framework DB and put `rows` rows in `secrets`."""
        import sqlite3

        db_path = Path(config.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS secrets ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id TEXT NOT NULL, service TEXT NOT NULL,"
                " key TEXT NOT NULL, encrypted_value BLOB NOT NULL,"
                " UNIQUE(user_id, service, key))"
            )
            for n in range(rows):
                conn.execute(
                    "INSERT INTO secrets (user_id, service, key, encrypted_value)"
                    " VALUES (?, ?, ?, ?)",
                    ("someone", "garmin", f"key{n}", b"ciphertext"),
                )
            conn.commit()
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "rows,status,healthy",
        [
            # Two rows exist and neither can be decrypted.
            (2, FAIL, False),
            (1, FAIL, False),
            # The documented opt-out: nothing is unreachable now, but every
            # future `istota secret ensure` will raise. `verdict` is unmoved by
            # a WARN, which keeps it out of the boot alert and every sweep.
            (0, WARN, True),
        ],
        ids=["two-stored", "one-stored", "observed-empty"],
    )
    def test_absence_against_the_store(self, make_config, rows, status, healthy):
        config = make_config()
        self._db_with_secrets(config, rows)
        result = self._run(config)
        assert result.status == status
        assert result.remedy
        if rows:
            assert str(rows) in result.detail
        assert doctor.verdict([result])[0] is healthy

    def test_an_unreadable_database_keeps_the_failure_and_creates_nothing(self, make_config):
        """Absence of the table is not evidence of an empty store. The count goes
        through `connect_read_only`, so a missing file is not created as a
        zero-byte database that later reads as corruption (ISSUE-458 dropped
        the backwards `-wal`/`-shm` half of that reason)."""
        config = make_config()
        db_path = Path(config.db_path)
        assert not db_path.exists()
        assert self._run(config).status == FAIL
        assert not db_path.exists()
        assert not db_path.with_suffix(db_path.suffix + "-wal").exists()
        assert not db_path.with_suffix(db_path.suffix + "-shm").exists()

    def test_a_present_key_never_reads_the_database(
        self, make_config, monkeypatch
    ):
        """The store's rows matter only once the key is absent."""
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
        config = make_config()
        self._db_with_secrets(config, 3)
        assert self._run(config).status == OK


def _bwrap(monkeypatch, available, checked=None):
    """Answer the sandbox-availability axis: the probe and the memo
    `effective_sandboxing_if_known` reads, which is a process global."""
    from istota import executor

    if available is not None:
        monkeypatch.setattr(executor, "_bwrap_available", lambda: available)
    monkeypatch.setattr(executor, "_bwrap_checked", available if checked is None else checked)


def _broken_availability(monkeypatch):
    from istota import executor

    def _boom(config):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(executor, "effective_sandboxing", _boom)


class TestSandboxCredentials:
    """`security.sandbox_credentials` — ISSUE-396.

    `load_config` warns on the sandbox-on/proxy-off pairing (ISSUE-393), but the
    boot log is read once at install; the Health pane and `istota doctor` are
    what an operator returns to. The finding must keep firing where the sandbox
    turns out not to be in force, the shape a check written around
    `effective_sandboxing` would most naturally go quiet on.
    """

    NAME = "security.sandbox_credentials"

    def _run(self, make_config, *, sandbox=True, proxy=False, **kwargs):
        from istota.config import SecurityConfig

        config = make_config(
            security=SecurityConfig(sandbox_enabled=sandbox, skill_proxy_enabled=proxy)
        )
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    def test_the_pairing_warns_and_names_the_credentials(self, make_config, monkeypatch):
        """The finding ISSUE-396 was filed for. Before the check existed the
        `only=` run came back empty and the indexing raised — the negative
        control for the whole class.

        The remedy is asserted here because `TestRegistry`'s sweep runs over
        `_dev_config`, which leaves the proxy on, so it never sees this WARN.
        """
        _bwrap(monkeypatch, True)
        r = self._run(make_config)

        assert r.status == WARN
        assert "every configured service credential" in r.detail
        assert "readable by the model" in r.detail
        assert "the sandbox is in force" in r.detail
        assert r.remedy.strip()
        assert "skill_proxy_enabled" in r.remedy
        # `tests/test_config.py` filters `caplog` on the load_config warning's
        # opening phrase, so this detail must not contain it.
        assert "sandbox_enabled with skill_proxy_enabled = false" not in r.detail
        assert "skill_proxy_enabled" in r.detail
        assert "sandbox_enabled" in r.detail

    @pytest.mark.parametrize(
        "sandbox,proxy,status,named",
        [
            # The single-user install's deliberate trust decision: the task runs
            # unconfined as the daemon user, so removing a variable from its env
            # is decorative rather than a boundary (ISSUE-393).
            (False, False, SKIP, "unconfined by design"),
            (True, True, OK, "injected per call"),
            (False, True, SKIP, None),
        ],
        ids=["both-off", "proxy-on", "sandbox-off-proxy-on"],
    )
    def test_the_shapes_it_stays_quiet_on(self, make_config, sandbox, proxy, status, named):
        # No availability patch: these return before anything reaches `executor`.
        r = self._run(make_config, sandbox=sandbox, proxy=proxy)
        assert r.status == status
        if named:
            assert named in r.detail

    def test_it_still_warns_where_bubblewrap_does_not_work(
        self, make_config, monkeypatch
    ):
        """The shipped Docker stack: the probe fails and every task runs
        unconfined while `sandbox_enabled` reads true. The credentials are in
        the task env on the strength of `skill_proxy_enabled` alone, so the
        operator has neither half of what they configured."""
        _bwrap(monkeypatch, False)
        r = self._run(make_config)

        assert r.status == WARN
        assert "every configured service credential" in r.detail
        assert "runtime.bwrap" in r.detail

    @pytest.mark.parametrize("cause", ["unprobed-cold-memo", "broken-lookup"])
    def test_an_unanswered_sandbox_state_keeps_the_warning(self, make_config, monkeypatch, cause):
        """A check whose subject is a boundary may not report a protection it
        did not look for (`runtime.session_log_dir` set the precedent), must not
        raise, and must not lose its finding: only the clause is in doubt."""
        if cause == "broken-lookup":
            _broken_availability(monkeypatch)
            r = self._run(make_config)
        else:
            _bwrap(monkeypatch, None, checked=None)
            r = self._run(make_config, probe=False)

        assert r.status == WARN
        assert "every configured service credential" in r.detail
        assert "unestablished" in r.detail

    def test_it_is_deployment_scoped_and_off_the_config_load_path(self):
        """`--scope image` must not select a posture an operator chose in a
        rendered config. Nor may `load_config` run it: it reaches
        `istota.executor`, which `TestConfigLoadPathStaysCheap` forbids there,
        and its WARN would be logged beside ISSUE-393's on every load."""
        from istota.config import CONFIG_LOAD_CHECKS

        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT
        assert self.NAME not in CONFIG_LOAD_CHECKS

    def test_the_skill_proxy_check_is_unchanged(self, make_config):
        """ISSUE-396 rejected widening the existing SKIP, which reads as "not
        applicable" for a live exposure; the old result keeps its status."""
        from istota.config import SecurityConfig

        config = make_config(security=SecurityConfig(sandbox_enabled=True, skill_proxy_enabled=False))
        results = _by_name(run_checks(config, only=("security.skill_proxy",)))

        assert results["security.skill_proxy"].status == SKIP
        assert self.NAME not in results


class TestSandboxEffective:
    """`security.sandbox_effective` — the flag versus what the deployment got.

    `runtime.bwrap` asks whether bubblewrap is installed and runnable, a
    property of the *image*. Whether a namespace can be created is a property of
    the *deployment*: the shipped compose file grants neither
    `seccomp:unconfined` nor `systempaths=unconfined`, so every task runs
    unsandboxed while `sandbox_enabled` reads true (ISSUE-381). The scope
    exclusion is asserted directly, since folding this onto `runtime.bwrap`
    would turn `tests/image/test_istota_image.py` red. The `probe=False` case is
    neither a pass nor a FAIL: a boundary check must not report a protection it
    did not look for, nor an exposure it did not observe.
    """

    NAME = "security.sandbox_effective"

    def _config(self, make_config, *, sandbox=True):
        from istota.config import SecurityConfig

        return make_config(security=SecurityConfig(sandbox_enabled=sandbox))

    def _run(self, config, **kwargs):
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    @pytest.mark.parametrize(
        "env,probe",
        [
            ({}, True),
            # The daemon probes at start-up, so `probe=False` with a warm memo
            # is not blind.
            ({}, False),
            # `ISTOTA_SANDBOXED` is set only where the sandbox really was in
            # force, so a task on the ISSUE-381 shape carries `ISTOTA_TASK_ID`
            # without it and its probe is valid; skipping on the whole marker
            # set would hide the deployment this check exists to report.
            ({"ISTOTA_TASK_ID": "1234"}, True),
        ],
        ids=["probed", "unprobed-warm-memo", "task-on-unconfined-deployment"],
    )
    def test_a_namespace_that_cannot_be_created_fails(self, make_config, monkeypatch, env, probe):
        """The ISSUE-381 shape. Before the check existed the `only=` run came
        back empty and `_run` raised — the negative control for the class.

        The remedy names both halves, since a container wants the two
        `security_opt` settings and a bare-metal host wants user namespaces.
        """
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        _bwrap(monkeypatch, False)
        r = self._run(self._config(make_config), probe=probe)

        assert r.status == FAIL
        assert "unsandboxed" in r.detail
        assert "seccomp:unconfined" in r.remedy
        assert "systempaths=unconfined" in r.remedy
        assert "unprivileged_userns_clone" in r.remedy

    def test_a_working_namespace_is_ok(self, make_config, monkeypatch):
        _bwrap(monkeypatch, True)
        r = self._run(self._config(make_config))

        assert r.status == OK
        assert not r.remedy

    def test_the_sandbox_being_off_skips(self, make_config):
        """The operator turned it off and knows. No availability patch: this
        branch returns before anything reaches `executor`."""
        r = self._run(self._config(make_config, sandbox=False))

        assert r.status == SKIP
        assert "sandbox_enabled" in r.detail

    def test_an_unprobed_run_with_a_cold_memo_settles_nothing_and_spawns_nothing(
        self, make_config, monkeypatch
    ):
        """Not a FAIL, since nothing observed an exposure, and not a silent OK,
        since nothing observed the boundary. The memo is read directly: the
        registry-wide no-spawn sweep runs with whatever memo state the worker
        carries, and a cold memo is the state that would spawn."""
        _bwrap(monkeypatch, None, checked=None)
        spawns = _spawn_spy(monkeypatch)
        r = self._run(self._config(make_config), probe=False)

        assert spawns == []
        assert r.status != FAIL
        assert r.status != OK
        assert "not probed on this run" in r.detail
        assert "unknown" in r.detail
        assert r.remedy.strip()

    def test_a_broken_availability_lookup_does_not_raise(self, make_config, monkeypatch):
        """A diagnostic must not raise, nor pass on a question it could not ask."""
        _broken_availability(monkeypatch)
        r = self._run(self._config(make_config))

        assert r.status != OK
        assert "the check itself raised" not in r.detail
        assert "could not be determined" in r.detail
        assert "unknown" in r.detail

    def test_a_probe_from_inside_a_sandbox_settles_nothing_and_asks_nothing(
        self, make_config, monkeypatch
    ):
        """The reported defect. A task's namespace is built with
        `--disable-userns`, so bwrap in there fails whatever the deployment can
        do; `istota doctor` run by a task reported every task unsandboxed, and
        exited 1, from inside a task whose masks were in place.

        The failing availability answer is what a real nested probe gives,
        which makes the first half a reproduction. The marker is read before
        either route to that answer, since a probe run and discarded would
        keep the failure one refactor from being read again.
        """
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        _bwrap(monkeypatch, False)
        r = self._run(self._config(make_config))

        assert r.status == SKIP
        assert "ISTOTA_SANDBOXED" in r.detail
        assert "unsandboxed" not in r.detail

        asked = self._record_availability(monkeypatch)
        assert self._run(self._config(make_config)).status == SKIP
        assert asked == []

    @staticmethod
    def _record_availability(monkeypatch):
        """Record both routes to the availability answer: `effective_sandboxing`
        under `probe=True` and `effective_sandboxing_if_known` under
        `probe=False`. A recorder on one would leave the other blind."""
        from istota import executor

        asked = []

        def _probed(config):
            asked.append("probed")
            return False

        def _memo(config):
            asked.append("memo")
            return False

        monkeypatch.setattr(executor, "effective_sandboxing", _probed)
        monkeypatch.setattr(executor, "effective_sandboxing_if_known", _memo)
        return asked

    def test_scope_image_never_selects_it(self, make_config, monkeypatch):
        """`tests/image/test_istota_image.py::test_no_check_fails` runs
        `doctor --scope image` with `probe=True` in a bare `docker run`. The
        recorder catches a run that happened and was filtered out afterwards."""
        asked = self._record_availability(monkeypatch)
        results = run_checks(self._config(make_config), scope=IMAGE)

        assert self.NAME not in {r.name for r in results}
        assert asked == [], "the check ran and was discarded rather than filtered"

    @pytest.mark.parametrize("probe,route", [(True, "probed"), (False, "memo")])
    def test_the_recorder_sees_each_route(self, make_config, monkeypatch, probe, route):
        """Positive controls for the test above, whose assertions also pass
        against a deleted check."""
        asked = self._record_availability(monkeypatch)
        results = run_checks(self._config(make_config), only=(self.NAME,), probe=probe)

        assert self.NAME in {r.name for r in results}
        assert asked == [route]

    def test_it_is_registered_deployment_scoped_and_neither_deep_nor_live(self):
        """`effective_sandboxing` memoizes its probe and the daemon has paid for
        it at start-up, so this needs no opt-in axis."""
        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT
        assert self.NAME in {name for name, _ in CHECKS}
        assert self.NAME not in DEEP_CHECKS
        assert self.NAME not in LIVE_CHECKS

    def test_the_bwrap_check_is_unchanged(self, make_config, tmp_path, monkeypatch):
        """An installed, runnable bwrap on a host where the namespace is refused
        must still leave `runtime.bwrap` OK and IMAGE-scoped, which the image
        tier depends on; the finding lands on the deployment-scoped check."""
        _which_only(monkeypatch, "bwrap", _fake_bin(tmp_path / "bin" / "bwrap", "bubblewrap 0.11.0"))
        _bwrap(monkeypatch, False)
        results = _by_name(
            run_checks(self._config(make_config), only=("runtime.bwrap", self.NAME))
        )

        assert results["runtime.bwrap"].status == OK
        assert results["runtime.bwrap"].scope == IMAGE
        assert doctor.CHECK_SCOPES["runtime.bwrap"] == IMAGE
        assert results[self.NAME].status == FAIL


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

    def test_without_a_token_the_binary_checks_skip_and_the_policy_check_runs(
        self, make_config, tmp_path
    ):
        """`forge_cli_permit` validation is about the config file, not about
        whether a credential happens to be wired yet."""
        config = _dev_config(make_config, tmp_path, gitlab_token="", github_token="")
        results = _by_name(run_checks(config, only=("developer.",)))
        assert results["developer.forge_binaries.gh"].status == SKIP
        assert results["developer.forge_config_drift.gh"].status == SKIP
        assert results["developer.forge_policy"].status != SKIP


class TestForgeBinaries:
    def _run(self, config, **kwargs):
        return _by_name(run_checks(config, only=("developer.forge_binaries",), **kwargs))

    def test_present_and_executable_is_ok_one_image_scoped_result_per_binary(
        self, make_config, tmp_path
    ):
        _fake_bin(tmp_path / "bin" / "gh", "gh version 2.98.0 (2026-01-01)")
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        results = self._run(_dev_config(make_config, tmp_path))
        assert set(results) == {"developer.forge_binaries.gh", "developer.forge_binaries.glab"}
        for r in results.values():
            assert r.status == OK
            assert r.scope == IMAGE

    def test_missing_binary_fails(self, make_config, tmp_path):
        """The ISSUE-263 shape: `os.execve` onto a path that does not exist."""
        gh = self._run(_dev_config(make_config, tmp_path))["developer.forge_binaries.gh"]
        assert gh.status == FAIL
        assert str(tmp_path / "bin" / "gh") in gh.detail
        assert gh.remedy

    @pytest.mark.parametrize(
        "probe,status,named",
        [
            # `check_forge_versions` was deleted as redundant, which is only
            # true while `_binary_status` executes `--version` under probe.
            (True, FAIL, "exited 1"),
            # Nothing may shell out on the probe-disabled path, and the result
            # has to say that nothing ran.
            (False, OK, "not executed"),
        ],
        ids=["probe", "no-probe"],
    )
    def test_a_binary_that_exits_nonzero(self, make_config, tmp_path, probe, status, named):
        _fake_bin(tmp_path / "bin" / "gh", "boom", exit_code=1)
        _fake_bin(tmp_path / "bin" / "glab", "glab 1.114.0")
        results = self._run(_dev_config(make_config, tmp_path), probe=probe)
        assert results["developer.forge_binaries.gh"].status == status
        assert named in results["developer.forge_binaries.gh"].detail
        assert results["developer.forge_binaries.glab"].status == OK

    def test_present_but_not_executable_fails(self, make_config, tmp_path):
        path = tmp_path / "bin" / "gh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o644)
        gh = self._run(_dev_config(make_config, tmp_path))["developer.forge_binaries.gh"]
        assert gh.status == FAIL
        assert "not executable" in gh.detail


class TestForgeConfigDrift:
    """`_resolve_real_bin`'s fallback is correct and load-bearing, and it hides
    the stale-config condition. This check restores that signal. It never fails."""

    def _drift(self, config):
        results = run_checks(config, only=("developer.forge_config_drift",))
        assert all(r.status != FAIL for r in results)
        return _by_name(results)["developer.forge_config_drift.gh"]

    def test_configured_path_that_exists_and_resolves_to_itself_is_ok(
        self, make_config, tmp_path
    ):
        _fake_bin(tmp_path / "bin" / "gh")
        _fake_bin(tmp_path / "bin" / "glab")
        assert self._drift(_dev_config(make_config, tmp_path)).status == OK

    def test_stale_configured_path_warns_naming_both(self, make_config, tmp_path, monkeypatch):
        """The retained-volume upgrade: `config.toml` predates the binaries, so
        resolution falls through to the image location."""
        from istota.skills import developer as developer_skill

        shipped = _fake_bin(tmp_path / "image" / "gh")
        stale = "/usr/local/bin/gh"
        original_exists = doctor.Path.exists
        monkeypatch.setattr(
            doctor.Path,
            "exists",
            lambda path: False if str(path) == stale else original_exists(path),
        )
        monkeypatch.setattr(
            developer_skill.os.path,
            "exists",
            lambda path: False if str(path) == stale else original_exists(doctor.Path(path)),
        )
        monkeypatch.setitem(developer_skill._IMAGE_BIN, "gh", str(shipped))
        drift = self._drift(_dev_config(make_config, tmp_path, gh_bin_path=str(stale)))
        assert drift.status == WARN
        assert stale in drift.detail
        assert str(shipped) in drift.detail
        assert drift.remedy

    def test_an_explicit_missing_path_does_not_contradict_itself(self, make_config, tmp_path):
        """`_resolve_real_bin` returns an explicitly chosen path as given, so one
        combined message would read "x but the wrapper will exec x"."""
        drift = self._drift(
            _dev_config(make_config, tmp_path, gh_bin_path=str(tmp_path / "nowhere" / "gh"))
        )
        assert drift.status == WARN
        assert "nothing exists there" in drift.detail
        assert "but the wrapper will exec" not in drift.detail


class TestWrapperShadowing:
    """The question is "is something *unexpected* reachable by name", not "is a
    real forge binary on PATH" — the latter is true by design on the Ansible
    shape, which is what production runs."""

    def _gh(self, config):
        results = run_checks(config, only=("developer.forge_wrapper_shadowing",))
        return _by_name(results)["developer.forge_wrapper_shadowing.gh"]

    def test_nothing_on_path_is_ok(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        assert self._gh(_dev_config(make_config, tmp_path)).status == OK

    def test_an_unexpected_real_binary_on_path_fails(self, make_config, tmp_path, monkeypatch):
        """Someone apt-installed gh onto the image shape: the model's shell finds
        it before the per-task wrapper and skips the policy and the injection."""
        real = tmp_path / "path" / "gh"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)
        real.chmod(0o755)
        _which_only(monkeypatch, "gh", real)
        # The deployment resolved something else entirely.
        _fake_bin(tmp_path / "bin" / "gh")
        gh = self._gh(_dev_config(make_config, tmp_path))
        assert gh.status == FAIL
        assert str(real) in gh.detail
        assert gh.remedy

    def test_the_ansible_shape_is_ok(self, make_config, tmp_path, monkeypatch):
        """The role installs the real binaries into /usr/bin and renders those
        paths into config.toml, so `which` finding them is correct."""
        installed = _fake_bin(tmp_path / "usr-bin" / "gh")
        _which_only(monkeypatch, "gh", installed)
        gh = self._gh(_dev_config(make_config, tmp_path, gh_bin_path=str(installed)))
        assert gh.status == OK
        assert "Ansible shape" in gh.detail

    def test_the_real_wrapper_on_path_is_ok(self, make_config, tmp_path, monkeypatch):
        """Copied from `forge_cli.py` itself, not hand-written to match: the
        per-task wrapper *is* a verbatim copy of that file."""
        import shutil as _shutil

        from istota.sandbox import forge_cli

        wrapper = tmp_path / "path" / "gh"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy(forge_cli.__file__, wrapper)
        wrapper.chmod(0o755)
        _which_only(monkeypatch, "gh", wrapper)
        assert self._gh(_dev_config(make_config, tmp_path)).status == OK

    def test_the_sentinel_is_near_the_top_of_both_copies_of_the_wrapper(self):
        """`_looks_like_the_wrapper` reads only the file's head, and the devbox
        image ships a byte-identical copy under another name."""
        from istota.sandbox import forge_cli

        assert doctor._WRAPPER_SENTINEL in Path(forge_cli.__file__).read_bytes()[:8192]
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
        _which_only(monkeypatch, "gh", opaque)
        try:
            gh = self._gh(_dev_config(make_config, tmp_path))
        finally:
            opaque.chmod(0o644)
        assert gh.status == WARN
        assert gh.remedy


class TestForgePolicy:
    @pytest.mark.parametrize(
        "permits,status",
        [([], OK), (["gh not-a-real-verb-at-all"], WARN), (["gh nonsense"], WARN)],
    )
    def test_permits(self, make_config, tmp_path, permits, status):
        config = _dev_config(make_config, tmp_path, forge_cli_permit=permits)
        r = run_checks(config, only=("developer.forge_policy",))[0]
        assert r.status == status
        for permit in permits:
            assert permit.split(" ", 1)[1] in r.detail


class TestGitlabReviewer:
    """ISSUE-289. The setting was silent in both directions: a numeric value
    produced `failed to find user by name` inside the task, and an unset one
    produced nothing at all, so every MR for weeks opened with no reviewer. A
    boot-time line is the only thing that closes that loop. It never fails."""

    def _run(self, config):
        r = run_checks(config, only=("developer.gitlab_reviewer",))[0]
        assert r.status != FAIL
        return r

    @pytest.mark.parametrize(
        "fields,status",
        [
            ({"gitlab_reviewer": "reviewer-user"}, OK),
            # Not configuring a reviewer is a choice, not a misconfiguration.
            ({"gitlab_reviewer": ""}, OK),
            ({"gitlab_reviewer": "reviewer-user", "gitlab_reviewer_id": "1234567"}, OK),
            # TOML types its scalars, so an unquoted value arrives as an int; a
            # crash would page the operator, since a raising check is a FAIL.
            ({"gitlab_reviewer": "", "gitlab_reviewer_id": 1234567}, WARN),
        ],
        ids=["username", "unset", "id-beside-username", "int-in-old-key"],
    )
    def test_status(self, make_config, tmp_path, fields, status):
        assert self._run(_dev_config(make_config, tmp_path, **fields)).status == status

    @pytest.mark.parametrize(
        "fields,in_detail,in_remedy",
        [
            # `glab mr create --reviewer` resolves by username, and a GitLab
            # username cannot be all digits, so this can only be the user id.
            ({"gitlab_reviewer": "1234567"}, ("1234567",), ()),
            ({"gitlab_reviewer": 1234567}, ("user id",), ()),
            # The upgrade shape: only `gitlab_reviewer_id` set used to build a
            # reviewer flag and now builds none.
            ({"gitlab_reviewer": "", "gitlab_reviewer_id": "1234567"}, ("gitlab_reviewer",), ("username",)),
            # `gitlab_reviewer_id` was documented as a username for one day, so
            # a host may have a working username in the retired key; calling it
            # "the id" sends that operator looking for something they have.
            ({"gitlab_reviewer": "", "gitlab_reviewer_id": "reviewer-user"}, (), ("reviewer-user", "copy it verbatim")),
            # The recipe expands `--reviewer $GITLAB_REVIEWER` unquoted.
            ({"gitlab_reviewer": "First Last"}, ("whitespace",), ()),
        ],
        ids=["all-digits", "int", "old-id-key-alone", "username-in-old-key", "whitespace"],
    )
    def test_warns(self, make_config, tmp_path, fields, in_detail, in_remedy):
        r = self._run(_dev_config(make_config, tmp_path, **fields))
        assert r.status == WARN
        assert r.remedy
        for text in in_detail:
            assert text in r.detail
        for text in in_remedy:
            assert text in r.remedy

    def test_non_ascii_digits_are_not_called_a_user_id(self, make_config, tmp_path):
        """`str.isdigit` is Unicode-wide; Arabic-Indic digits are no user id."""
        r = self._run(_dev_config(make_config, tmp_path, gitlab_reviewer="\u0661\u0662\u0663"))
        assert "user id" not in r.detail

    def test_skips_when_the_developer_skill_is_off(self, make_config):
        from istota.config import DeveloperConfig

        config = make_config(developer=DeveloperConfig(enabled=False))
        assert self._run(config).status == SKIP


class TestForgeTransport:
    """A forge token sent over plain HTTP.

    Reachable since the developer skill seeds glab's `api_protocol` for an
    `http://` forge URL; before that a plain-HTTP forge failed at the TLS
    handshake. A working plaintext credential transport is worth one line in
    the report. It never fails.
    """

    def _run(self, make_config, tmp_path, **fields):
        r = run_checks(_dev_config(make_config, tmp_path, **fields), only=("developer.forge_transport",))[0]
        assert r.status != FAIL, r.detail
        return r

    def test_https_is_ok(self, make_config, tmp_path):
        assert self._run(make_config, tmp_path, gitlab_url="https://gitlab.com").status == OK

    def test_plain_http_with_a_token_warns_naming_the_url_and_not_the_token(
        self, make_config, tmp_path
    ):
        r = self._run(
            make_config, tmp_path,
            gitlab_url="http://gitlab.internal:8080", gitlab_token="glpat-" + "s" * 20,
        )
        assert r.status == WARN
        assert "http://gitlab.internal:8080" in r.detail
        assert r.remedy
        # The detail names the URL, and a URL can carry userinfo.
        assert "glpat-" not in (r.detail + (r.remedy or ""))

    @pytest.mark.parametrize(
        "fields",
        [
            # No carve-out for localhost: a loopback forge URL in a deployment
            # is a proxy or a tunnel, and its far side is not knowable here.
            {"gitlab_url": "http://127.0.0.1:18080"},
            # gh refuses a scheme inside `GH_HOST`, so the token never leaves,
            # but the operator still wrote `http://`.
            {"gitlab_url": "https://gitlab.com", "github_url": "http://ghe.internal", "github_token": "g" * 20},
        ],
        ids=["loopback", "github"],
    )
    def test_still_warns(self, make_config, tmp_path, fields):
        assert self._run(make_config, tmp_path, **fields).status == WARN

    def test_skips_without_a_token(self, make_config, tmp_path):
        r = self._run(
            make_config, tmp_path,
            gitlab_url="http://gitlab.internal:8080", gitlab_token="", github_token="",
        )
        assert r.status == SKIP

    def test_a_url_carrying_a_credential_is_warned_about_and_redacted_visibly(
        self, make_config, tmp_path
    ):
        """The token belongs in `gitlab_token`; `_plain_http_host_entry` refuses
        to write an entry for such a URL, so without this nothing says why the
        call fails. Removing the userinfo silently would hide that the
        configured value carried a credential at all."""
        r = self._run(make_config, tmp_path, gitlab_url="https://bot:sekritvalue@gitlab.internal")

        assert r.status == WARN
        assert "gitlab.internal" in r.detail
        assert "sekritvalue" not in (r.detail + (r.remedy or "")), r.detail
        assert "@gitlab.internal" in r.detail, r.detail

    def test_a_malformed_url_does_not_turn_a_warning_into_a_failure(
        self, make_config, tmp_path
    ):
        """`urlsplit` raises on `http://[::1`; unguarded, `run_checks` reports a
        FAIL blaming the check for the operator's typo."""
        self._run(make_config, tmp_path, gitlab_url="http://[::1")


class TestWebStatic:
    def test_skips_when_no_web_surface(self, make_config):
        r = run_checks(make_config(), only=("web.static",))[0]
        assert r.status == SKIP

    @pytest.mark.parametrize(
        "index,status",
        [(None, FAIL), ("", FAIL), ("<!doctype html><html></html>", OK)],
        ids=["missing-build", "empty-index", "present-build"],
    )
    def test_the_build(self, make_config, tmp_path, monkeypatch, index, status):
        from istota.config import WebConfig

        build = tmp_path / "build"
        if index is not None:
            build.mkdir()
            (build / "index.html").write_text(index)
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        r = run_checks(make_config(web=WebConfig(enabled=True)), only=("web.static",))[0]
        assert r.status == status
        if status == FAIL:
            assert r.remedy


class TestWebBuildCurrent:
    """ISSUE-428: whether the served bundle is current for this checkout's `web/`.

    `web.static` stays true across a stale bundle, which is what the issue
    reported: a frontend-only commit landed, every unit restarted, and the
    browser kept running old code. The predicate is "has `web/` changed since
    the build", not "is the stamp HEAD": the cron rebuilds only on a `web/`
    change, so the stamp trails HEAD nearly always and equality would warn on
    every ordinary deploy. These drive a real git repository, since the check
    shells out to `git diff`.
    """

    @staticmethod
    def _repo(root: Path):
        """A checkout with one `web/` file and one Python file."""

        def git(*args: str) -> str:
            env = dict(os.environ)
            env.update(
                {
                    "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@example.com",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_SYSTEM": "/dev/null",
                }
            )
            out = subprocess.run(
                ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
            )
            return out.stdout.strip()

        root.mkdir(parents=True, exist_ok=True)
        git("init", "--initial-branch=main", ".")
        (root / "web").mkdir()
        (root / "web" / "app.svelte").write_text("<p>a</p>\n")
        (root / "app.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-m", "a")
        return git

    @staticmethod
    def _bundle(tmp_path, monkeypatch, version: str | None):
        build = tmp_path / "build"
        (build / "_app").mkdir(parents=True)
        (build / "index.html").write_text("<!doctype html>")
        if version is not None:
            (build / "_app" / "version.json").write_text('{"version":"%s"}' % version)
        monkeypatch.setenv("ISTOTA_WEB_STATIC_DIR", str(build))
        return build

    def _checkout(self, tmp_path, monkeypatch, then=None):
        """A real repository wired in as the checkout, with a bundle stamped at
        its first commit; `then` names a file to change in a second commit."""
        repo = tmp_path / "repo"
        git = self._repo(repo)
        self._bundle(tmp_path, monkeypatch, git("rev-parse", "HEAD"))
        if then is not None:
            (repo / then).write_text("changed\n")
            git("add", "-A")
            git("commit", "-m", "later")
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
        return git

    def _run(self, make_config, **kwargs):
        from istota.config import WebConfig

        config = make_config(web=WebConfig(enabled=True))
        return run_checks(config, only=("web.build_current",), **kwargs)[0]

    def test_skips_when_no_web_surface(self, make_config):
        r = run_checks(make_config(), only=("web.build_current",))[0]
        assert r.status == SKIP

    @pytest.mark.parametrize(
        "version,named",
        [
            (None, None),
            # SvelteKit's default version is a build timestamp, which a
            # container image and a developer's own build both produce.
            ("1788110322364", "not stamped"),
        ],
        ids=["no-version", "not-stamped-with-a-commit"],
    )
    def test_skips_on_an_unusable_version(self, make_config, tmp_path, monkeypatch, version, named):
        self._bundle(tmp_path, monkeypatch, version)
        r = self._run(make_config)
        assert r.status == SKIP
        if named:
            assert named in r.detail

    def test_a_malformed_version_file_skips(self, make_config, tmp_path, monkeypatch):
        """Never raises: one caller is the daemon's boot sequence."""
        build = self._bundle(tmp_path, monkeypatch, "a" * 40)
        (build / "_app" / "version.json").write_text("{not json")
        assert self._run(make_config).status == SKIP

    def test_skips_when_there_is_no_checkout(self, make_config, tmp_path, monkeypatch):
        """A wheel install has the bundle and no repository to compare it to."""
        self._bundle(tmp_path, monkeypatch, "a" * 40)
        monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_path / "nowhere")
        assert self._run(make_config).status == SKIP

    def test_skips_under_probe_false_rather_than_guessing(
        self, make_config, tmp_path, monkeypatch
    ):
        """The comparison needs git, and `probe=False` forbids spawning; it must
        not fall back to comparing shas for equality. The repository is real on
        purpose: against a missing path the no-checkout arm SKIPs before the
        spawn, so removing the probe gate left this passing."""
        self._checkout(tmp_path, monkeypatch)

        def _fail(*args, **kwargs):
            raise AssertionError("spawned under probe=False")

        monkeypatch.setattr(subprocess, "run", _fail)
        r = self._run(make_config, probe=False)
        assert r.status == SKIP, r.detail
        assert "not executed" in r.detail

    def test_skips_when_the_stamped_commit_is_unknown(self, make_config, tmp_path, monkeypatch):
        """A bundle can outlive a re-clone. Unanswerable, not stale."""
        self._bundle(tmp_path, monkeypatch, "a" * 40)
        repo = tmp_path / "repo"
        self._repo(repo)
        monkeypatch.setattr(doctor, "_repo_root", lambda: repo)
        r = self._run(make_config)
        assert r.status == SKIP
        assert "could not compare" in r.detail

    @pytest.mark.parametrize(
        "then", [None, "app.py"], ids=["built-from-head", "head-moved-without-web"]
    )
    def test_a_current_bundle_is_ok(self, make_config, tmp_path, monkeypatch, then):
        """The false positive this check must not have: after a Python-only
        commit the stamp trails HEAD while the bundle is byte-correct."""
        self._checkout(tmp_path, monkeypatch, then)
        r = self._run(make_config)
        assert r.status == OK, r.detail

    def test_a_web_change_since_the_build_warns(self, make_config, tmp_path, monkeypatch):
        """The reported condition, and the whole reason this check exists."""
        self._checkout(tmp_path, monkeypatch, "web/app.svelte")
        r = self._run(make_config)
        assert r.status == WARN, r.detail
        assert r.remedy
        # A WARN must not page anyone: the next auto-update tick clears it.
        assert doctor.verdict([r])[0] is True

    def test_the_git_call_carries_the_hardening_overrides(
        self, make_config, tmp_path, monkeypatch
    ):
        """Asserted on the argv, because behaviourally it cannot be observed:
        `git diff --quiet` runs no `diff.external` (measured), so this guards
        against a future change such as a dropped `--quiet`."""
        from istota.sandbox.git_hardening import GIT_HARDENING

        self._checkout(tmp_path, monkeypatch)
        seen = []
        real = doctor._run
        monkeypatch.setattr(doctor, "_run", lambda argv, **kw: seen.append(argv) or real(argv, **kw))
        self._run(make_config)

        assert seen, "the check never ran git"
        argv = seen[0]
        assert argv[0] == "git"
        for flag in GIT_HARDENING:
            assert flag in argv, f"{flag} missing from {argv}"


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
    @pytest.mark.parametrize(
        "statuses,code",
        [((OK, FAIL), 1), ((OK, WARN, SKIP), 0), ((), 0)],
        ids=["any-fail", "no-fail", "nothing"],
    )
    def test_exit_code(self, statuses, code):
        results = [CheckResult(f"c.{i}", s, "x", remedy="fix it") for i, s in enumerate(statuses)]
        assert exit_code(results) == code

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
        """`render_json` crosses an HTTP boundary to the admin dashboard, so
        omitting `secrets` has to be a decision, spelled `secrets=()`."""
        results = [CheckResult("a.b", OK, "fine")]
        with pytest.raises(TypeError):
            render_json(results)
        with pytest.raises(TypeError):
            render_text(results)

    @pytest.mark.parametrize("render", [render_json, render_text], ids=["json", "text"])
    def test_a_credential_in_a_detail_or_remedy_is_redacted(self, render):
        """Check authors are forbidden from putting credentials in `detail`;
        the renderer does not take their word for it."""
        secret = "NOT-A-REAL-TOKEN-aaaaaaaa"
        results = [CheckResult("x.y", FAIL, f"token {secret} rejected", remedy=f"rotate {secret}")]
        rendered = render(results, secrets=[secret])
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


class TestConfigSecrets:
    def test_collects_configured_credentials_but_no_empty_value(self, make_config, tmp_path):
        config = _dev_config(make_config, tmp_path, gitlab_token="NOT-A-REAL-TOKEN-zzzzzzzz")
        assert "NOT-A-REAL-TOKEN-zzzzzzzz" in doctor.config_secrets(config)
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

    @pytest.mark.parametrize(
        "header,value",
        [
            ("Authorization", "NOT-A-REAL-HEADER-VALUE-1"),
            # The whole field is an auth channel by construction, so a header
            # spelling nobody anticipated must not escape redaction.
            ("x-goog-api-client", "zzzzzzzzzzzzzzzzzz"),
        ],
    )
    def test_collects_the_always_secret_header_dict(self, make_config, header, value):
        """`brain.native.extra_headers` is where a non-Anthropic deployment puts
        its provider key, and `admin_config_view` marks it always-secret."""
        config = make_config()
        config.brain.native.extra_headers = {header: value}
        assert value in doctor.config_secrets(config)

    def test_terminates_on_a_self_referential_config(self, make_config):
        """A cycle must not hang the boot path."""
        config = make_config()
        config.users = {"loop": config}
        doctor.config_secrets(config)


# ---------------------------------------------------------------------------
# The development container


def _container_config(make_config, tmp_path, *, devbox=True, users=("alice",), **overrides):
    """A Config with `[developer.container]` wired and one devbox user.

    `devbox` is the switch the backend is derived from; there is no `backend`
    key to set any more.
    """
    from istota.config import (
        ContainerConfig,
        DeveloperConfig,
        DevboxConfig,
        SecurityConfig,
        UserConfig,
    )

    repos = tmp_path / "repos"
    repos.mkdir(exist_ok=True)
    exec_root = tmp_path / "run" / "exec"
    exec_root.mkdir(parents=True, exist_ok=True)
    # The per-user socket directory is what says "this user has a devbox": the
    # role creates it only for `istota_devbox_users`, and the check treats its
    # absence as "not configured for one" rather than as a broken container.
    if not overrides.pop("no_socket_dirs", False):
        for u in users:
            (exec_root / u).mkdir(exist_ok=True)
    container_fields = {
        "exec_socket_dir": str(exec_root),
        "connect_timeout_seconds": 0.5,
    }
    container_fields.update(overrides.pop("container", {}))
    # Overridable so a caller can express "no repos_dir at all", which is the
    # shape the derived package cache does not exist in.
    repos_dir = overrides.pop("repos_dir", str(repos))
    return make_config(
        developer=DeveloperConfig(
            enabled=True, repos_dir=repos_dir, container=ContainerConfig(**container_fields)
        ),
        devbox=DevboxConfig(enabled=devbox),
        security=SecurityConfig(**overrides.pop("security", {})),
        users={u: UserConfig(display_name=u) for u in users},
        **overrides,
    )


def _ping_reply(socket_path, payload, timeout):
    return [{"pong": True, "protocol": 1}, {"exit_code": 0}], ""


def _agreeing_container(repos_root, *, uid_offset=0, cache_exit=0, reaper=True):
    """A fake server that answers ping, stat and `test -d` the way a healthy one does.

    `reaper=None` is a server too old to know the field, which is a third answer
    and not a quieter spelling of `False`.
    """
    import os as _os

    def _reply(socket_path, payload, timeout):
        body = payload.decode()
        if '"ping"' in body:
            return [{"pong": True, "protocol": 1}, {"exit_code": 0}], ""
        if '"stat"' in body:
            stat = {"uid": _os.getuid() + uid_offset, "repos_root": repos_root}
            if reaper is not None:
                stat["reaper"] = reaper
            return [stat, {"exit_code": 0}], ""
        return [{"exit_code": cache_exit}], ""

    return _reply


class TestTheReposLayoutCheck:
    """The loud path for an upgrade that has not moved its clones.

    `repos_dir` became a per-user root on *every* backend, and the bind is
    skipped when its source does not exist — so a host whose clones still sit
    flat has an unusable developer skill and no error anywhere naming a path.
    """

    def _check(self, make_config, tmp_path, bare=(), users=("alice",)):
        from istota.config import DeveloperConfig, UserConfig

        repos = tmp_path / "repos"
        repos.mkdir(exist_ok=True)
        for rel in bare:
            path = repos / rel
            path.mkdir(parents=True, exist_ok=True)
            for marker in ("HEAD", "config"):
                (path / marker).write_text("")
            (path / "objects").mkdir(exist_ok=True)
        config = make_config(
            developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
            users={u: UserConfig(display_name=u) for u in users},
        )
        return doctor.check_repos_layout(config, probe=False)

    def test_the_flat_layout_fails_and_names_what_it_found(self, make_config, tmp_path):
        result = self._check(make_config, tmp_path, ["namespace/project.git"])
        assert result.status == FAIL
        assert "namespace" in result.detail
        assert result.remedy

    def test_a_half_migrated_host_still_fails(self, make_config, tmp_path):
        """One user moved and another not is the shape a partial play leaves."""
        result = self._check(
            make_config, tmp_path,
            ["alice/ns/project.git", "leftover/project.git"], users=("alice", "bob"),
        )
        assert result.status == FAIL
        assert "leftover" in result.detail

    @pytest.mark.parametrize(
        "bare", [["alice/namespace/project.git"], []], ids=["per-user-layout", "empty-root"]
    )
    def test_ok(self, make_config, tmp_path, bare):
        assert self._check(make_config, tmp_path, bare).status == OK

    def test_a_directory_holding_no_repository_is_not_a_finding(
        self, make_config, tmp_path
    ):
        """`repos_dir` is a directory an operator may put other things in."""
        (tmp_path / "repos" / "notes").mkdir(parents=True)
        (tmp_path / "repos" / "notes" / "README").write_text("hi")
        assert self._check(make_config, tmp_path).status == OK

    def test_it_skips_when_the_skill_is_off(self, make_config, tmp_path):
        from istota.config import DeveloperConfig

        config = make_config(developer=DeveloperConfig(enabled=False))
        assert doctor.check_repos_layout(config, probe=False).status == SKIP

    def test_it_spawns_nothing_under_probe_false(self, make_config, tmp_path, monkeypatch):
        """It is on the config-load path, where `probe=False` forbids spawning."""
        monkeypatch.setattr(
            doctor, "_run",
            lambda *a, **k: pytest.fail("the repos layout check spawned a process"),
        )
        self._check(make_config, tmp_path, ["namespace/project.git"])


def _container_results(monkeypatch, config, reply=None, probe=True):
    """Run `check_developer_container`, answering every socket with `reply`."""
    if reply is not None:
        monkeypatch.setattr(doctor, "_exec_transport_request", reply)
    return _by_name(doctor.check_developer_container(config, probe=probe))


class TestTheDeveloperContainerChecks:
    """Five properties, each of which fails silently on its own.

    Registered as one entry so a single connection per user answers four of
    them; the fifth reads the rendered config file and opens nothing.
    """

    GROUP = "developer.container"
    NAMES = {
        "developer.container.backend",
        "developer.container.transport",
        "developer.container.identity",
        "developer.container.uv_cache",
        "developer.container.command_reaper",
    }

    def test_all_five_are_produced_whatever_happens(self, make_config, tmp_path):
        """A caller asserts on a name, never on a count — a check that vanishes
        under some configuration is a check nothing can require."""
        assert self.GROUP in {name for name, _ in CHECKS}
        for devbox in (False, True):
            config = _container_config(make_config, tmp_path, devbox=devbox)
            results = doctor.check_developer_container(config, probe=False)
            assert {r.name for r in results} == self.NAMES

    def test_the_backend_being_off_skips_the_ones_that_need_a_container(
        self, make_config, tmp_path, monkeypatch
    ):
        """The skip's detail says which derivation input holds the transport off
        (this used to warn about a pair that is no longer configurable)."""
        config = _container_config(make_config, tmp_path, devbox=False)
        by_name = _container_results(monkeypatch, config)

        for name in self.NAMES - {"developer.container.backend"}:
            assert by_name[name].status == SKIP
        assert "[devbox] enabled is false" in by_name["developer.container.transport"].detail

    def test_the_skip_names_the_developer_skill_when_that_is_what_is_off(
        self, make_config, tmp_path, monkeypatch
    ):
        """Control for the test above: a different input off has to produce a
        different sentence, or the detail is decoration rather than a diagnosis."""
        config = _container_config(make_config, tmp_path, devbox=True)
        config.developer.enabled = False

        transport = _container_results(monkeypatch, config)["developer.container.transport"]

        assert transport.status == SKIP
        assert "the developer skill is off" in transport.detail
        assert "[devbox] enabled is false" not in transport.detail
        assert "devbox skill is offered" not in transport.detail

    def test_probe_false_opens_no_socket(self, make_config, tmp_path, monkeypatch):
        """Doctor runs on the daemon's start-up path; `probe=False` must connect
        to nothing."""
        called = []
        reply = lambda *a, **k: (called.append(a), ([], "unreachable"))[1]  # noqa: E731
        config = _container_config(make_config, tmp_path)

        by_name = _container_results(monkeypatch, config, reply, probe=False)

        assert not called
        assert by_name["developer.container.transport"].status == SKIP

    def test_a_user_with_no_devbox_is_not_a_failure(self, make_config, tmp_path, monkeypatch):
        """Which users have a devbox lives in Ansible, not the daemon's config.
        Counting every user as unreachable would FAIL permanently on the
        reference shape and alert every admin hourly."""
        config = _container_config(
            make_config, tmp_path, users=("alice", "bob"), no_socket_dirs=True
        )

        transport = _container_results(monkeypatch, config)["developer.container.transport"]

        assert transport.status == SKIP
        assert "no devbox socket directory" in transport.detail

    def test_a_dead_container_is_a_fail_naming_the_socket(
        self, make_config, tmp_path, monkeypatch
    ):
        config = _container_config(make_config, tmp_path)
        transport = _container_results(
            monkeypatch, config,
            lambda socket_path, payload, timeout: ([], f"could not connect to {socket_path}"),
        )["developer.container.transport"]

        assert transport.status == FAIL
        assert "alice" in transport.detail
        assert transport.remedy

    def test_a_live_container_that_agrees_is_ok(self, make_config, tmp_path, monkeypatch):
        config = _container_config(
            make_config, tmp_path,
            security={"sandbox_cache_dir": str(tmp_path / "cache")},
        )
        by_name = _container_results(
            monkeypatch, config, _agreeing_container(str(tmp_path / "repos" / "alice"))
        )

        for name in self.NAMES - {"developer.container.backend"}:
            assert by_name[name].status == OK

    @pytest.mark.parametrize(
        "agreement,result,status,in_detail,in_remedy",
        [
            # Untreated, uid drift ends in worktrees that can never be reaped,
            # and no error message anywhere says so.
            ({"uid_offset": 1}, "identity", FAIL, ("uid",), ("reap",)),
            ({"cache_exit": 1}, "uv_cache", WARN, ("alice",), ()),
            # The transport works and every command is still killed on its own
            # exit path; what is gone is the backstop, so the cost is a leak
            # rather than an outage.
            ({"reaper": False}, "command_reaper", WARN, ("alice",), ("docker logs",)),
            # A missing field and a `false` are different facts; reading the
            # first as the second warns on every container not yet rebuilt.
            ({"reaper": None}, "command_reaper", SKIP, (), ()),
        ],
        ids=["uid-mismatch", "missing-cache-mount", "no-reaper", "too-old-to-answer"],
    )
    def test_a_container_that_disagrees(
        self, make_config, tmp_path, monkeypatch, agreement, result, status, in_detail, in_remedy
    ):
        config = _container_config(
            make_config, tmp_path,
            security={"sandbox_cache_dir": str(tmp_path / "cache")},
        )
        r = _container_results(
            monkeypatch, config,
            _agreeing_container(str(tmp_path / "repos" / "alice"), **agreement),
        )[f"developer.container.{result}"]

        assert r.status == status
        for text in in_detail:
            assert text in r.detail
        for text in in_remedy:
            assert text in r.remedy

    def test_a_repos_root_mismatch_fails(self, make_config, tmp_path, monkeypatch):
        """The shim sends `os.getcwd()` and the server checks it with `realpath`
        against its own root, so a disagreement refuses every working directory."""
        config = _container_config(make_config, tmp_path)
        identity = _container_results(
            monkeypatch, config, _agreeing_container("/somewhere/else")
        )["developer.container.identity"]

        assert identity.status == FAIL
        assert "/somewhere/else" in identity.detail

    def test_an_unset_repos_dir_skips_rather_than_warning(
        self, make_config, tmp_path, monkeypatch
    ):
        """This used to WARN when `security.sandbox_cache_dir` was unset, which
        stopped being the cache root: the cache is derived under `repos_dir`, so
        the old assertion fired on every correct deployment. With no `repos_dir`
        there is nothing to look for."""
        config = _container_config(make_config, tmp_path, repos_dir="")
        cache = _container_results(monkeypatch, config, _ping_reply)["developer.container.uv_cache"]

        assert cache.status == SKIP


class TestTheDevboxResultsKeepTheirPerCallerDifferences:
    """The four devbox results reduce through one three-arm helper, and three of
    them differ in a way that fold could flatten silently. Nothing above pins
    the wording, the skip predicate's subject or the separator.
    """

    def test_the_reaper_skip_tells_unanswered_from_unreachable(
        self, make_config, tmp_path, monkeypatch
    ):
        """`command_reaper` skips on `not ok`, its siblings on `not reachable`,
        and the reasons send an operator to different places."""
        config = _container_config(make_config, tmp_path)
        answered = _container_results(
            monkeypatch, config,
            _agreeing_container(str(tmp_path / "repos" / "alice"), reaper=None),
        )["developer.container.command_reaper"]
        assert answered.status == SKIP
        assert "no container reported whether" in answered.detail

        silent = _container_results(
            monkeypatch, config, lambda *a, **k: ([], "unreachable")
        )["developer.container.command_reaper"]
        assert silent.status == SKIP
        assert "nothing was asked" in silent.detail

    def test_the_reaper_separates_users_with_a_comma(
        self, make_config, tmp_path, monkeypatch
    ):
        """It lists bare user ids; its siblings list `user: sentence` pairs
        separated by semicolons."""
        config = _container_config(make_config, tmp_path, users=("alice", "bob"))
        result = _container_results(
            monkeypatch, config,
            _agreeing_container(str(tmp_path / "repos" / "alice"), reaper=False),
        )["developer.container.command_reaper"]

        assert result.status == WARN
        assert "alice, bob" in result.detail

    def test_the_uv_cache_skip_names_the_configuration_or_the_container(self):
        """Driven against the reducer, because the unset-`repos_dir` reason is
        unreachable through `check_developer_container`: an empty `repos_dir`
        derives `backend = none` and the outer gate SKIPs first."""
        unconfigured = doctor._uv_cache_result("", [], [], [])
        assert unconfigured.status == SKIP
        assert "developer.repos_dir is unset" in unconfigured.detail

        unreached = doctor._uv_cache_result("/srv/repos", [], [], [])
        assert unreached.status == SKIP
        assert "no container answered" in unreached.detail

        # The precondition outranks a finding, so an unset repos_dir is never
        # answered with a remedy naming a path derived from it.
        contradicted = doctor._uv_cache_result("", [], ["alice: no such dir"], [])
        assert contradicted.status == SKIP
        assert "developer.repos_dir is unset" in contradicted.detail


class TestTheBackendAgreementCheck:
    """An operator needs to see on the affected host that the file and the
    running daemon disagree. The check re-derives from the file for the same
    reason the daemon does; reading `[developer.container] backend` would report
    OK forever, since nothing writes that key any more."""

    def _result(self, make_config, tmp_path, *, running, file_devbox=True, repos_dir=None, retired=None):
        config = _container_config(make_config, tmp_path, devbox=running)
        path = tmp_path / "config.toml"
        repos = str(tmp_path / "repos") if repos_dir is None else repos_dir
        body = (
            f'[developer]\nenabled = true\nrepos_dir = "{repos}"\n\n'
            f"[devbox]\nenabled = {str(bool(file_devbox)).lower()}\n"
        )
        if retired is not None:
            body += f'\n[developer.container]\nbackend = "{retired}"\n'
        path.write_text(body)
        config.config_path = path
        if repos_dir is not None:
            config.developer.repos_dir = repos_dir
        return _by_name(doctor.check_developer_container(config, probe=False))[
            "developer.container.backend"
        ]

    def test_agreement_is_ok(self, make_config, tmp_path):
        """Also the control for the retired-key WARN below: the ordinary
        rendering must not trip it."""
        assert self._result(make_config, tmp_path, running=True).status == OK

    def test_a_daemon_running_the_old_value_fails(self, make_config, tmp_path):
        """What an operator sees after editing config.toml and not restarting: a
        feature switched on that did not switch on."""
        result = self._result(make_config, tmp_path, running=False)

        assert result.status == FAIL
        assert "devbox" in result.detail and "none" in result.detail
        assert result.remedy

    @pytest.mark.parametrize(
        "running,repos_dir",
        [
            # The derivation is a conjunction, so the re-derivation has to be
            # one too: the devbox on with no `repos_dir` is not a devbox host.
            (False, ""),
            # Both derivations strip, so neither calls a blank path a root; a
            # mismatch here would be a permanent FAIL telling the operator to
            # restart a daemon already running the right answer.
            (True, "   "),
        ],
        ids=["empty-repos-dir", "whitespace-repos-dir"],
    )
    def test_every_input_is_read(self, make_config, tmp_path, running, repos_dir):
        assert self._result(make_config, tmp_path, running=running, repos_dir=repos_dir).status == OK

    def test_a_file_still_carrying_the_retired_key_is_reported(
        self, make_config, tmp_path
    ):
        """An operator who wrote `backend = "none"` had builds on the host until
        this release; the derivation now ignores the key, so the deployment can
        do the opposite of what the file appears to say."""
        result = self._result(make_config, tmp_path, running=True, retired="none")

        assert result.status == WARN
        assert "retired" in result.detail
        assert result.remedy

    def test_the_retired_key_does_not_suppress_a_real_drift(
        self, make_config, tmp_path
    ):
        """A hand-maintained config keeps the stale key for ever, so a WARN
        about the key must never stand in for a FAIL about the daemon."""
        result = self._result(make_config, tmp_path, running=False, retired="devbox")

        assert result.status == FAIL
        assert "restart" in result.remedy.lower()
        # Named, but explicitly not blamed — deleting it would not clear this.
        assert "retired" in result.detail
        assert "not the cause" in result.detail

    @pytest.mark.parametrize("path,status", [(None, SKIP), ("gone.toml", WARN)], ids=["in-memory", "unreadable"])
    def test_no_file_to_compare(self, make_config, tmp_path, path, status):
        config = _container_config(make_config, tmp_path)
        config.config_path = None if path is None else tmp_path / path
        result = _by_name(doctor.check_developer_container(config, probe=False))[
            "developer.container.backend"
        ]
        assert result.status == status
        if status == WARN:
            assert result.remedy


class TestSkillOverlays:
    """`config.skill_overlays` is the only thing that ever says a per-skill
    overlay will not be read. Every case here asserts the check did **not**
    SKIP — a suite asserting "no FAIL" is green on exactly the broken tree.
    """

    NAME = "config.skill_overlays"

    @staticmethod
    def _config(make_config, tmp_path, **overrides):
        bundled = tmp_path / "bundled"
        for skill in ("developer", "notes", "browse", "sensitive_actions"):
            d = bundled / skill
            d.mkdir(parents=True, exist_ok=True)
            (d / "skill.md").write_text(
                f"---\nname: {skill}\ndescription: the {skill} skill\n---\n\n# {skill}\n"
            )
        return make_config(bundled_skills_dir=bundled, **overrides)

    @staticmethod
    def _overlays(config, user_id="alice"):
        d = (
            Path(config.workspace_path)
            / "Users" / user_id / config.bot_dir_name / "config" / "skills"
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run(self, config):
        results = run_checks(config, only=(self.NAME,))
        assert len(results) == 1
        return results[0]

    def _with(self, make_config, tmp_path, files, **overrides):
        """Run the check over alice's overlay directory holding `files`."""
        config = self._config(make_config, tmp_path, **overrides)
        d = self._overlays(config)
        for name, text in files.items():
            (d / name).write_text(text)
        return self._run(config)

    def test_it_skips_without_a_mount(self, make_config, tmp_path):
        r = self._run(self._config(make_config, tmp_path, workspace_path=None))
        assert r.status == SKIP
        assert "mount" in r.detail

    def test_it_skips_when_there_are_no_user_trees_yet(self, make_config, tmp_path):
        assert self._run(self._config(make_config, tmp_path)).status == SKIP

    def test_no_overlays_anywhere_is_ok(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path)
        (Path(config.workspace_path) / "Users" / "alice").mkdir(parents=True)
        assert self._run(config).status == OK

    @pytest.mark.parametrize(
        "files,overrides",
        [
            ({"developer.md": "- one rule\n"}, {}),
            # A disabled skill's overlay binds again the moment it is switched
            # back on, so it is configuration, not a defect in the file.
            ({"browse.md": "- a rule\n"}, {"disabled_skills": ["browse"]}),
        ],
        ids=["good-overlay", "disabled-skill"],
    )
    def test_ok(self, make_config, tmp_path, files, overrides):
        assert self._with(make_config, tmp_path, files, **overrides).status == OK

    @pytest.mark.parametrize(
        "files,named",
        [
            ({"develper.md": "- a rule\n"}, ("develper.md", "unknown_skill", "did you mean developer")),
            ({"Developer.md": "- a rule\n"}, ("did you mean developer",)),
            ({"sensitive_actions.md": "- planted\n"}, ("denylisted",)),
            # A misspelling of a denylisted name is still a file its author
            # believed was live; the note must not suggest renaming to it.
            ({"sensitive_action.md": "- a rule\n"}, ("sensitive_actions", "takes no overlay")),
            ({"develper.md": "- a rule\n", "notes.md": "## heading\n- a rule\n"}, ()),
            ({"zzz.md": "- scratch\n", "develper.md": "- a rule\n"}, ("develper.md",)),
            ({"zzz.md": "- scratch\n", "sensitive_actions.md": "- planted\n"}, ("denylisted",)),
        ],
        ids=[
            "misspelled", "case-difference", "denylisted", "typo-of-denylisted",
            "fail-outranks-warning", "stray-does-not-hide-typo", "stray-does-not-mask-denylisted",
        ],
    )
    def test_fails(self, make_config, tmp_path, files, named):
        """A typo keeps FAIL, and the suggestion makes the report actionable
        without opening a shell on the deployment."""
        r = self._with(make_config, tmp_path, files)
        assert r.status == FAIL
        assert r.remedy
        for text in named:
            assert text in r.detail

    @pytest.mark.parametrize(
        "files,named",
        [
            ({"notes.md": "## My rules\n\n- a rule\n"}, ("shallow_heading",)),
            # It loads as nothing, but FAIL is reserved for the misfiling a
            # person fixes by renaming or shrinking.
            ({"developer.md": ""}, ("empty",)),
            # Any task can create a file here with one `touch`, and a FAIL an
            # ordinary task can pin red is an alert an operator learns to skip.
            ({"zzz.md": "- scratch\n"}, ("zzz.md", "unknown_skill")),
            # `<skill>2`, `<skill>~` and `<skill>.bak` are what an editor and a
            # task leave behind, each one edit from the name it copies.
            (
                {"developer2.md": "- a copy\n", "developer~.md": "- a copy\n", "notes.bak.md": "- a copy\n"},
                ("developer2.md",),
            ),
            # `nte` is two edits from `notes`, which the short-name budget
            # does not accept; see `TestOverlayNearMiss`.
            ({"nte.md": "- scratch\n"}, ()),
        ],
        ids=["shallow-heading", "empty-file", "stray-file", "backup-copies", "short-stray-name"],
    )
    def test_warns(self, make_config, tmp_path, files, named):
        r = self._with(make_config, tmp_path, files)
        assert r.status == WARN
        assert r.remedy
        for text in named:
            assert text in r.detail
        if "zzz.md" in files:
            assert "unknown_skill" in r.remedy

    @pytest.mark.parametrize(
        "size_attr,status,label",
        [("OVERLAY_MAX_BYTES", FAIL, "over_cap"), ("OVERLAY_WARN_BYTES", WARN, "over_warn_bytes")],
    )
    def test_an_oversized_overlay(self, make_config, tmp_path, size_attr, status, label):
        from istota.skills import _loader

        size = getattr(_loader, size_attr)
        r = self._with(make_config, tmp_path, {"developer.md": "- x\n" * (size // 4 + 4)})
        assert r.status == status
        assert label in r.detail
        assert r.remedy

    def test_it_walks_every_user_tree_not_just_the_configured_ones(
        self, make_config, tmp_path
    ):
        """A user whose config block was removed still has files on disk."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config, "alice") / "developer.md").write_text("- ok\n")
        (self._overlays(config, "bob") / "develper.md").write_text("- broken\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "bob/develper.md" in r.detail
        assert "alice" not in r.detail

    def test_the_detail_names_at_most_a_handful(self, make_config, tmp_path):
        # `developer` with one character dropped, nine ways, so the truncated
        # list is the FAIL list. Not `developer{i}`: a trailing digit WARNs.
        name = "developer"
        typos = [name[:i] + name[i + 1:] for i in range(len(name))]
        assert len(set(typos)) == 9
        r = self._with(make_config, tmp_path, {f"{t}.md": "- a rule\n" for t in typos})
        assert r.status == FAIL
        assert "9 of 9" in r.detail
        assert "and 4 more" in r.detail

    def test_a_control_character_in_a_filename_cannot_forge_a_second_line(
        self, make_config, tmp_path
    ):
        """A filename here is text the model wrote, and the detail is one line
        printed to a terminal and rendered into the admin dashboard. WARN, since
        nothing this shape is a typo's distance from a skill name."""
        r = self._with(make_config, tmp_path, {"bad\nname\x1b[31m.md": "- a rule\n"})
        assert r.status == WARN
        assert "\n" not in r.detail
        assert "\x1b" not in r.detail

    def test_a_very_long_filename_is_truncated(self, make_config, tmp_path):
        r = self._with(make_config, tmp_path, {"z" * 200 + ".md": "- a rule\n"})
        assert r.status == WARN
        assert len(r.detail) < 200
        assert "..." in r.detail

    def test_a_symlinked_user_entry_is_not_descended_into(
        self, make_config, tmp_path
    ):
        """Every component under `{mount}/Users/{user_id}` is model-writable, so
        a planted link must not report a file against the wrong user."""
        config = self._config(make_config, tmp_path)
        (self._overlays(config, "alice") / "develper.md").write_text("- broken\n")
        users = Path(config.workspace_path) / "Users"
        (users / "mallory").symlink_to(users / "alice", target_is_directory=True)

        r = self._run(config)
        assert r.status == FAIL
        assert "alice/develper.md" in r.detail
        assert "mallory" not in r.detail

    def test_an_overlay_dir_redirected_out_of_the_user_tree_is_named_not_followed(
        self, make_config, tmp_path
    ):
        """Following a replaced `config/` or `skills/` would open files anywhere
        the daemon can read. It used to be skipped and so reported by nothing
        (ISSUE-344); WARN rather than FAIL, because a sandboxed task can create
        the link at will and an aimable red is what ISSUE-340 split this to avoid.
        """
        config = self._config(make_config, tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "develper.md").write_text("- planted\n")
        user_config = (
            Path(config.workspace_path)
            / "Users" / "alice" / config.bot_dir_name / "config"
        )
        user_config.mkdir(parents=True)
        (user_config / "skills").symlink_to(elsewhere, target_is_directory=True)

        r = self._run(config)
        assert r.status == WARN
        assert "dir_outside_user_tree" in r.detail
        # Nothing behind the link was opened, so the planted name is absent.
        assert "develper" not in r.detail

    def test_a_symlinked_overlay_file_is_reported_and_never_read(
        self, make_config, tmp_path
    ):
        config = self._config(make_config, tmp_path)
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN\n")
        (self._overlays(config) / "developer.md").symlink_to(secret)

        r = self._run(config)
        assert r.status == WARN
        assert "overlay_is_a_symlink" in r.detail
        assert "TOP SECRET" not in r.detail + r.remedy

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
    def test_a_fifo_does_not_hang_the_check(self, make_config, tmp_path):
        """`doctor` runs on the daemon's start-up path, where a blocking
        `open(2)` has no timeout over it at all."""
        config = self._config(make_config, tmp_path)
        os.mkfifo(self._overlays(config) / "developer.md")

        r = self._run(config)
        assert r.status == WARN
        assert "overlay_not_a_regular_file" in r.detail

    def test_no_overlay_content_ever_leaves_the_users_directory(
        self, make_config, tmp_path
    ):
        """The result is rendered into the admin dashboard every admin reads; a
        filename is the most that may cross out of one user's tree."""
        r = self._with(make_config, tmp_path, {"develper.md": "- alice's private rule about her doctor\n"})
        assert "private rule" not in r.detail + r.remedy

    def test_a_fail_never_hides_the_files_that_only_warn(
        self, make_config, tmp_path
    ):
        """One planted typo must not suppress the rest of the report: a FAIL
        branch that skipped `warned` reported "1 of 21" and hid twenty files
        that reach no prompt, a suppression aimable with a single `touch`."""
        files = {"develper.md": "- a rule\n"}
        files.update({f"projectnotes{i}.md": "- scratch\n" for i in range(20)})
        r = self._with(make_config, tmp_path, files)
        assert r.status == FAIL
        assert "1 of 21" in r.detail
        assert "20 more" in r.detail
        assert "projectnotes0.md" in r.detail
        # The glossary for the warned half travels with the FAIL remedy.
        assert "unknown_skill on its own" in r.remedy

    def test_a_fail_does_not_hide_an_overlay_near_the_loading_cap(
        self, make_config, tmp_path
    ):
        """The file being hidden is a real overlay a few KB from the cliff past
        which it silently stops reaching any prompt."""
        from istota.skills._loader import OVERLAY_WARN_BYTES

        r = self._with(make_config, tmp_path, {
            "developer.md": "- x\n" * (OVERLAY_WARN_BYTES // 4 + 4),
            "notse.md": "- planted\n",
        })
        assert r.status == FAIL
        assert "over_warn_bytes" in r.detail
        assert "developer.md" in r.detail


class TestOverlayNearMiss:
    """The predicate separating a misspelled overlay from a scratch file."""

    KNOWN = ("developer", "notes", "browse", "sensitive_actions")

    @pytest.mark.parametrize(
        "stem,expected",
        [
            # The caller only asks about rejected names, but the predicate must
            # not claim a name is a typo of itself.
            ("developer", None),
            ("develper", "developer"),
            ("developerr", "developer"),
            ("dveloper", "developer"),
            ("sensitiveactons", "sensitive_actions"),
            ("develo", None),
            # A short name gets a tighter budget. The singular is the commonest
            # misspelling there is, and `note.md` reaches no prompt at all.
            ("note", "notes"),
            ("nots", "notes"),
            ("nte", None),
            ("NOTES", "notes"),
            ("zzz", None),
            ("scratch", None),
            ("", None),
        ],
    )
    def test_against_the_known_names(self, stem, expected):
        from istota.doctor import _overlay_near_miss

        assert _overlay_near_miss(stem, self.KNOWN) == expected

    def test_the_budget_switches_at_the_stated_length(self):
        from istota.doctor import _OVERLAY_TYPO_SHORT_NAME_CHARS, _overlay_near_miss

        assert _OVERLAY_TYPO_SHORT_NAME_CHARS == 5
        # Four characters, two edits from `browse`: short budget, so no.
        assert _overlay_near_miss("brse", ("browse",)) is None
        # Five characters, two edits from `browser`: long budget, so yes.
        assert _overlay_near_miss("brwse", ("browse",)) == "browse"

    @pytest.mark.parametrize(
        "stem,expected,known",
        [
            # One edit from `notes`, two from `notest`: the closest wins, not
            # the first sorted match.
            ("notez", "notes", ("notest", "notes")),
            ("notez", "notes", ("notes", "notest")),
            # Both one edit away: only the sort makes the answer independent
            # of the caller's order.
            ("noteX", "notea", ("noteb", "notea")),
            ("noteX", "notea", ("notea", "noteb")),
        ],
    )
    def test_the_choice_between_candidates(self, stem, expected, known):
        from istota.doctor import _overlay_near_miss

        assert _overlay_near_miss(stem, known) == expected


class TestClassifyUnknownOverlay:
    """Severity and wording for a filename the skill index rejected.

    One helper decides both, because every earlier version of this had a label
    stating a reason the branch above it had not used.
    """

    KNOWN = ("developer", "notes", "browse", "sensitive_actions")

    def _classify(self, stem):
        from istota.doctor import _classify_unknown_overlay

        return _classify_unknown_overlay(stem, self.KNOWN)

    @pytest.mark.parametrize(
        "stem",
        [
            "notes2", "notes-1", "notes~", "notes.bak", "notes.tmp",
            "notes-old", "notes_new", "notes copy", "notes.orig",
            "notes backup", "notes.save", "notes v2", "notes.bak2",
        ],
    )
    def test_a_copy_of_a_real_overlay_warns_and_says_what_it_copies(self, stem):
        """Each is one or two edits from the name it was made from, so distance
        alone reads the class as misspellings; the label has to say it is a
        copy, or the remedy calls `notes2` "not close enough to be a typo"."""
        assert self._classify(stem) == (False, "unknown_skill, a copy of notes.md")

    def test_a_copy_marker_on_a_name_that_is_not_a_skill_is_still_a_typo(self):
        # Strips to `develper`, which is not a skill, so it falls through.
        fails, note = self._classify("develper2")
        assert fails is True
        assert "did you mean developer" in note

    @pytest.mark.parametrize(
        "stem,expected",
        [
            ("2", (False, "unknown_skill")),
            ("~", (False, "unknown_skill")),
            ("bak", (False, "unknown_skill")),
            ("zzz", (False, "unknown_skill")),
            ("scratch", (False, "unknown_skill")),
            ("develper", (True, "unknown_skill, did you mean developer?")),
            # `developer2` names `developer` and is also a copy of it; the copy
            # reading is the quieter and the correct one.
            ("developer2", (False, "unknown_skill, a copy of developer.md")),
        ],
    )
    def test_exact_classification(self, stem, expected):
        assert self._classify(stem) == expected

    def test_a_typo_of_a_denylisted_name_does_not_suggest_a_rename(self):
        """The write path refuses `sensitive_actions`, so suggesting it would
        walk the operator from this FAIL straight into the next one."""
        fails, note = self._classify("sensitive_action")
        assert fails is True
        assert "takes no overlay" in note
        assert "did you mean" not in note

    @pytest.mark.parametrize(
        "stem",
        ["developer.local", "01-developer", "developer-overlay", "developer_overlay",
         "my-developer-rules"],
    )
    def test_a_name_built_around_a_real_skill_is_fatal(self, stem):
        """The more deliberately a name is decorated the further it gets from the
        skill, while its author's belief that it was live gets more obvious."""
        fails, note = self._classify(stem)
        assert fails is True
        assert "names the developer skill but is not developer.md" in note

    def test_a_two_word_skill_needs_both_words(self):
        assert self._classify("sensitive-actions-old")[0] is True
        assert self._classify("actions-only")[0] is False


def _now_iso() -> str:
    """A timestamp the staleness bound reads as fresh."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TestAvatarImport:
    """`web.avatar_import` — configuration and recorded state, and no socket.

    The check runs on the daemon's start-up path, on a scheduler interval, from
    `istota doctor` and from the admin Health pane, so a live Nextcloud call
    would hang the admin page behind a remote timeout (as for `web.basemap`).
    """

    @staticmethod
    def _config(make_config, db_path, **overrides):
        from istota.config import NextcloudConfig, SchedulerConfig, WebConfig

        settings = {
            "db_path": db_path,
            "nextcloud": NextcloudConfig(url="https://cloud.example"),
            "web": WebConfig(enabled=True, avatar_import_from_nextcloud=True),
            "scheduler": SchedulerConfig(avatar_import_interval=21600),
        }
        settings.update(overrides)
        return make_config(**settings)

    @staticmethod
    def _run(config):
        return run_checks(config, only=("web.avatar_import",))[0]

    @staticmethod
    def _record(db_path, header, **counts):
        from istota.webui import avatars
        from istota import db as db_module

        state = {"at": _now_iso(), "users": 1, "imported": 0, "no_custom": 0,
                 "unchanged": 0, "failed": 0, "header": getattr(avatars, header)}
        state.update(counts)
        with db_module.get_db(db_path) as conn:
            avatars.write_import_state(conn, state)

    def test_skips_on_a_local_storage_backend(self, make_config, db_path):
        from istota.config import NextcloudConfig

        r = self._run(self._config(make_config, db_path, nextcloud=NextcloudConfig(url="")))
        assert r.status == SKIP
        assert "nextcloud" in r.detail.lower()

    def test_skips_when_switched_off(self, make_config, db_path):
        from istota.config import SchedulerConfig, WebConfig

        off = WebConfig(enabled=True, avatar_import_from_nextcloud=False)
        assert self._run(self._config(make_config, db_path, web=off)).status == SKIP
        no_interval = SchedulerConfig(avatar_import_interval=0)
        assert self._run(self._config(make_config, db_path, scheduler=no_interval)).status == SKIP

    def test_reports_that_no_tick_has_run_yet_without_paging_anyone(
        self, make_config, db_path
    ):
        """The daemon runs the first tick seconds after boot, after doctor's own
        boot run. A WARN here would fire on every restart."""
        r = self._run(self._config(make_config, db_path))
        assert r.status == OK
        assert "no import tick" in r.detail.lower()

    @pytest.mark.parametrize(
        "header,counts,status,named",
        [
            # `failed` used to be rendered and gate nothing, so a deployment
            # whose every fetch raised printed its failure count in a green line.
            ("HEADER_UNOBSERVED", {"users": 5, "failed": 5}, WARN, "every user"),
            # The control: one unreachable account among many must not warn.
            ("HEADER_SEEN", {"users": 5, "imported": 2, "no_custom": 2, "failed": 1}, OK, None),
            # A wedged fetch or an unreadable probe state stops the job silently
            # and leaves the last good row standing.
            ("HEADER_SEEN", {"at": "2019-01-01T00:00:00Z", "users": 2, "imported": 1, "no_custom": 1}, WARN, "may have stopped"),
            # `at` is a JSON value out of a KV table; a shape change must not
            # turn a healthy import into a warning.
            ("HEADER_SEEN", {"at": "not-a-timestamp", "imported": 1}, OK, None),
            ("HEADER_UNOBSERVED", {}, OK, None),
        ],
        ids=["every-user-failed", "failures-with-progress", "stale-tick", "unreadable-timestamp", "observed-nothing"],
    )
    def test_the_recorded_tick(self, make_config, db_path, header, counts, status, named):
        self._record(db_path, header, **counts)
        r = self._run(self._config(make_config, db_path))
        assert r.status == status
        if status == WARN:
            assert r.remedy
        if named:
            assert named in r.detail

    def test_reports_the_recorded_state(self, make_config, db_path):
        from istota.webui import avatars
        from istota import db as db_module

        recorded_at = _now_iso()
        with db_module.get_db(db_path) as conn:
            avatars.put_user_avatar(
                conn, "alice", source=avatars.SOURCE_NEXTCLOUD,
                image=b"not-really-an-image", content_hash="deadbeef",
                remote_etag='"e1"',
            )
            avatars.touch_import_probe(conn, "bob", remote_etag='"g"')
        self._record(db_path, "HEADER_SEEN", at=recorded_at, users=5, imported=1,
                     no_custom=1, unchanged=3)

        r = self._run(self._config(make_config, db_path))

        assert r.status == OK
        assert recorded_at in r.detail
        # Every counter is rendered: without `unchanged`, the steady state, a
        # healthy deployment's numbers did not add up to its user count.
        for text in ("5 users", "1 imported", "1 with no custom avatar", "3 unchanged", "0 failed"):
            assert text in r.detail

    def test_a_missing_custom_avatar_header_warns_with_a_remedy(
        self, make_config, db_path
    ):
        """The header is how a user-set picture is told from the coloured letter
        Nextcloud generates, so without it nothing will ever be imported."""
        self._record(db_path, "HEADER_ABSENT", at="2026-08-30T09:00:00Z", users=2, no_custom=2)
        r = self._run(self._config(make_config, db_path))

        assert r.status == WARN
        assert "avatar_import_from_nextcloud" in r.remedy

    def test_an_unreadable_database_is_reported_rather_than_raised(
        self, make_config, tmp_path
    ):
        # WARN specifically: with this config every SKIP branch is unreachable,
        # so accepting SKIP would pass a future regression in the gates.
        r = self._run(self._config(make_config, tmp_path / "nothing" / "istota.db"))
        assert r.status == WARN

    def test_it_opens_no_socket(self, make_config, db_path, monkeypatch):
        """A remote call here hangs the daemon's boot and the admin Health pane."""
        import socket

        attempts: list[str] = []

        def _refuse(target):
            def _fn(*args, **kwargs):
                attempts.append(target)
                raise OSError(f"network blocked: {target}")

            return _fn

        monkeypatch.setattr(socket.socket, "connect", _refuse("connect"))
        monkeypatch.setattr(socket.socket, "connect_ex", _refuse("connect_ex"))
        monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
        monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))

        r = self._run(self._config(make_config, db_path))

        assert not attempts, f"web.avatar_import reached the network: {attempts}"
        assert r.status in (OK, SKIP, WARN)


class TestSessionLogDir:
    """`runtime.session_log_dir` — where the native brain's transcripts land, and
    whether the sandbox's database mask actually covers them.

    The check must ask `_mask_dir`'s own question rather than a copy of it. "Is
    the resolved directory under `db_path.parent`" answers True on the
    standalone install, where the mask is refused — the `map_basemap`
    two-consumers failure — and the WARN/OK pair below proves the predicate is
    the real one.
    """

    NAME = "runtime.session_log_dir"

    def _config(self, make_config, tmp_path, *, logs=False, **session_log_kwargs):
        from istota.config import BrainConfig, NativeBrainConfig, SessionLogConfig

        home = tmp_path / "srv"
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "data" / "istota.db").touch()
        if logs:
            (home / "data" / "logs").mkdir()
        temp = tmp_path / "tmp" / "istota"
        temp.mkdir(parents=True, exist_ok=True)
        return make_config(
            db_path=home / "data" / "istota.db",
            temp_dir=temp,
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(
                    session_log=SessionLogConfig(**session_log_kwargs),
                ),
            ),
        )

    def _run(self, config, **kwargs):
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    @pytest.fixture(autouse=True)
    def _bwrap_works_here(self, monkeypatch):
        """Every test runs as if bubblewrap works, or the class would answer on
        the availability axis on a developer machine and never reach the mask
        reasoning. Both the function and the memo, because the check reads each
        by a different route, and the memo is a process global set by whatever
        ran earlier in the same xdist worker. Tests about that axis patch it back.
        """
        _bwrap(monkeypatch, True)

    # -- when it does not apply -------------------------------------------

    @pytest.mark.parametrize(
        "brain,session_log",
        [({"kind": "claude_code"}, {}), ({}, {"enabled": False})],
        ids=["no-routing-reaches-native", "feature-off"],
    )
    def test_skips_unless_a_sweep_rule_is_on(self, make_config, tmp_path, brain, session_log):
        config = self._config(make_config, tmp_path, retention_days=0, max_total_gb=0, **session_log)
        for name, value in brain.items():
            setattr(config.brain, name, value)
        r = self._run(config)
        assert r.status == SKIP
        if brain:
            assert "native" in r.detail

        # With the retention rules on, the sweep's own finding still reports.
        config = self._config(make_config, tmp_path, logs=True, **session_log)
        for name, value in brain.items():
            setattr(config.brain, name, value)
        self._record_sweep(config, still_over=True)
        assert self._run(config).status == WARN

    @pytest.mark.parametrize(
        "routing",
        [{"fallback": "native"}, {"source_type_overrides": {"scheduled": "native"}}],
        ids=["native-fallback", "source-type-override"],
    )
    def test_a_route_onto_native_is_not_a_skip(self, make_config, tmp_path, routing):
        # `brain/native.py` builds the writer from `session_log` alone, so a
        # `claude_code` primary writes a transcript whenever a task reaches
        # native. Gating on `kind` SKIPs on the mixed-brain deployment.
        config = self._config(make_config, tmp_path)
        config.brain.kind = "claude_code"
        for name, value in routing.items():
            setattr(config.brain, name, value)
        assert self._run(config).status != SKIP

    # -- the healthy shape -------------------------------------------------

    def test_ok_on_the_default_directory(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path, logs=True)
        r = self._run(config)
        assert r.status == OK
        assert str(config.db_path.parent / "logs") in r.detail

    def test_ok_before_the_directory_exists(self, make_config, tmp_path):
        # Nothing creates it until the first native task.
        assert self._run(self._config(make_config, tmp_path)).status == OK

    def test_the_ok_line_reports_the_size_against_the_ceiling(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path, max_total_gb=5.0)
        log_dir = config.db_path.parent / "logs" / "alice"
        log_dir.mkdir(parents=True)
        (log_dir / "a.jsonl").write_bytes(b"x" * 4096)
        r = self._run(config)
        assert r.status == OK
        assert "5.0" in r.detail
        assert "1 file" in r.detail

    # -- the exposures -----------------------------------------------------

    def test_warns_when_the_directory_is_outside_the_masked_one(self, make_config, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        r = self._run(self._config(make_config, tmp_path, dir=str(elsewhere)))
        assert r.status == WARN
        assert str(elsewhere) in r.detail
        assert r.remedy

    def _workspace_shape(self, make_config, tmp_path, *, sandbox_enabled):
        """`setup_wizard`'s layout: db_path.parent *is* the workspace and the
        temp dir is inside it, so `_mask_dir` refuses. `sandbox_enabled` is
        explicit both ways because `setup_wizard` ships it false, which never
        reaches the mask reasoning at all."""
        from istota.config import (
            BrainConfig,
            NativeBrainConfig,
            SecurityConfig,
            SessionLogConfig,
        )

        workspace = tmp_path / "istota-home"
        (workspace / "tmp").mkdir(parents=True)
        (workspace / "istota.db").touch()
        return make_config(
            db_path=workspace / "istota.db",
            temp_dir=workspace / "tmp",
            workspace_path=workspace,
            security=SecurityConfig(sandbox_enabled=sandbox_enabled),
            brain=BrainConfig(
                kind="native",
                native=NativeBrainConfig(session_log=SessionLogConfig()),
            ),
        )

    def test_the_two_shapes_disagree_which_is_the_point_of_the_check(
        self, make_config, tmp_path,
    ):
        # Both have the sandbox on, so only the mask refusal separates them;
        # an "under db_path.parent" copy would answer the same for both.
        ansible = self._config(make_config, tmp_path / "a")
        assert self._run(ansible).status == OK

        r = self._run(self._workspace_shape(make_config, tmp_path / "b", sandbox_enabled=True))
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert "workspace" in r.detail.lower()
        assert r.remedy

    def test_warns_when_the_sandbox_is_switched_off_entirely(
        self, make_config, tmp_path,
    ):
        # On a layout whose mask would otherwise be emitted. Written against the
        # workspace shape it passes on the mask-refusal reason instead and stays
        # green with this arm deleted. Measured.
        from istota.config import SecurityConfig

        config = self._config(make_config, tmp_path)
        config.security = SecurityConfig(sandbox_enabled=False)
        r = self._run(config)
        assert r.status == WARN
        assert "switched off on this deployment" in r.detail
        assert r.remedy

    def test_the_standalone_install_as_shipped_warns_on_both_counts(
        self, make_config, tmp_path,
    ):
        # `setup_wizard` writes the workspace layout *and* `sandbox_enabled =
        # false`; gating the mask arm on `sandbox_enabled` made this a plain OK.
        # Both reasons are named, availability first: whether a mask exists at
        # all outranks where it would land.
        r = self._run(self._workspace_shape(make_config, tmp_path, sandbox_enabled=False))
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert r.remedy
        assert "switched off on this deployment" in r.detail
        assert "workspace" in r.detail.lower()
        assert r.detail.index("switched off") < r.detail.index("workspace")

    # -- the availability axis ---------------------------------------------

    def test_warns_when_bubblewrap_does_not_work_on_this_deployment(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The shipped Docker stack: no mask is emitted while `sandbox_enabled`
        # reads true, on the layout whose mask *would* cover the directory.
        _bwrap(monkeypatch, False, checked=True)
        r = self._run(self._config(make_config, tmp_path, logs=True))
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert "bubblewrap" in r.detail.lower()
        assert r.remedy

    def test_a_nested_probe_is_unknown_rather_than_an_exposure(
        self, make_config, tmp_path, monkeypatch,
    ):
        """Inside a task's own sandbox the probe observed only its own
        namespace, so the logs must not be reported unbound — the
        `security.sandbox_effective` defect, one prefix along."""
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        _bwrap(monkeypatch, False, checked=True)
        r = self._run(self._config(make_config, tmp_path, logs=True))

        assert "could not be established" in r.detail
        assert "unbound rather than masked" not in r.detail
        assert "ISTOTA_SANDBOXED" in r.detail

    def test_an_unavailable_sandbox_and_a_refused_mask_are_both_reported(
        self, make_config, tmp_path, monkeypatch,
    ):
        # Neither reason pre-empts the other, or an operator who fixes one is
        # told nothing about the second. Availability leads on this arm too.
        _bwrap(monkeypatch, False, checked=True)
        r = self._run(self._workspace_shape(make_config, tmp_path, sandbox_enabled=True))
        assert r.status == WARN
        assert "bubblewrap" in r.detail.lower()
        assert "workspace" in r.detail.lower()
        assert r.detail.index("bubblewrap") < r.detail.index("workspace")

    def _recording_probe(self, monkeypatch, *, cached, answer=True):
        """Stand in for the bwrap probe, recording whether it was invoked.

        A `subprocess` spy cannot answer the spawn question here:
        `_bwrap_available` returns at its `sys.platform` check on a developer
        machine and memoizes afterwards, so the spy stays empty with the
        `probe` gate deleted while this recorder fires. Measured.
        """
        from istota import executor

        calls: list[str] = []

        def _probe():
            calls.append("bwrap")
            return answer

        monkeypatch.setattr(executor, "_bwrap_available", _probe)
        monkeypatch.setattr(executor, "_bwrap_checked", cached)
        return calls

    def test_probe_false_does_not_claim_a_mask_it_could_not_verify(
        self, make_config, tmp_path, monkeypatch,
    ):
        # With a cold memo the availability axis cannot be answered, and the
        # cheap half cannot tell the Ansible shape from the Docker one. The
        # finding must not assert the exposure in one clause and disclaim it in
        # the next on a deployment whose mask is fine.
        calls = self._recording_probe(monkeypatch, cached=None)
        r = self._run(self._config(make_config, tmp_path, logs=True), probe=False)
        assert calls == [], "probe=False invoked the bwrap probe"
        assert r.status == WARN
        assert "not probed" in r.detail
        assert "could not be established" in r.detail
        assert "unbound" not in r.detail.lower()

    def test_probe_false_answers_from_a_warm_memo_rather_than_declining_to_look(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The daemon probes at start-up, and a warm memo of False is the Docker
        # shape this is about, so "not probed" there would be wrong.
        calls = self._recording_probe(monkeypatch, cached=False)
        r = self._run(self._config(make_config, tmp_path, logs=True), probe=False)
        assert calls == []
        assert r.status == WARN
        assert "bubblewrap does not work" in r.detail
        assert "not probed" not in r.detail

    def test_an_availability_question_that_raises_is_a_finding_not_a_pass(
        self, make_config, tmp_path, monkeypatch,
    ):
        # Swallowing to `True` reinstated ISSUE-381 in miniature: an answer
        # nobody could get, reported as a protection in place.
        _broken_availability(monkeypatch)
        r = self._run(self._config(make_config, tmp_path, logs=True))
        assert r.status == WARN
        assert "could not be determined" in r.detail

    @pytest.mark.requires_dac
    def test_fails_on_an_unwritable_directory(self, make_config, tmp_path):
        config = self._config(make_config, tmp_path, logs=True)
        log_dir = config.db_path.parent / "logs"
        os.chmod(log_dir, 0o500)
        try:
            r = self._run(config)
        finally:
            os.chmod(log_dir, 0o700)
        assert r.status == FAIL
        assert r.remedy

    # -- the ceiling is what actually binds --------------------------------

    def _record_sweep(self, config, **fields):
        from istota import db as _db
        from istota.session.session_log import (
            SWEEP_STATE_KEY,
            SWEEP_STATE_NAMESPACE,
            SweepResult,
            encode_sweep_state,
        )

        _db.init_db(config.db_path)
        with _db.get_db(config.db_path) as conn:
            _db.shared_kv_set(
                conn,
                SWEEP_STATE_NAMESPACE,
                SWEEP_STATE_KEY,
                encode_sweep_state(SweepResult(**fields), now=time.time()),
                "test",
            )

    @pytest.mark.parametrize(
        "sweep,session_log,status,named",
        [
            # `deleted_size > 0` means retention is a function of load rather
            # than `retention_days`; an operator wanting 14 days and getting 3
            # should be told.
            ({"deleted_size": 7}, {}, WARN, "retention"),
            ({"deleted_age": 12}, {}, OK, None),
            # The worse condition: over the ceiling with everything inside the
            # live window, so nothing is reclaiming it at all.
            ({"deleted_size": 3, "still_over": True}, {}, WARN, "nothing it could evict"),
            # With both rules off nothing rewrites the row, so a stale
            # `deleted_size` would warn for ever about a rule that never runs.
            ({"deleted_size": 9}, {"retention_days": 0, "max_total_gb": 0}, OK, None),
        ],
        ids=["evicted-by-size", "evicted-by-age", "still-over", "stale-row-both-rules-off"],
    )
    def test_the_last_sweep(self, make_config, tmp_path, sweep, session_log, status, named):
        config = self._config(make_config, tmp_path, logs=True, **session_log)
        self._record_sweep(config, **sweep)
        r = self._run(config)
        assert r.status == status
        if status == WARN:
            assert r.remedy
        if named:
            assert named in r.detail.lower() or named in r.detail

    def test_the_exposure_and_the_retention_findings_are_composed_not_raced(
        self, make_config, tmp_path,
    ):
        # An operator-set `dir` and the standalone shape are permanent exposure
        # conditions, so returning at the first finding made the retention arm
        # unreachable on exactly the deployments that need it.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        config = self._config(make_config, tmp_path, dir=str(elsewhere))
        self._record_sweep(config, deleted_size=4)
        r = self._run(config)
        assert r.status == WARN
        assert "unbound" in r.detail.lower()
        assert "retention" in r.detail.lower()

    def test_an_infinite_ceiling_reads_as_no_ceiling(self, make_config, tmp_path):
        # TOML spells `inf` and the sweep reads it as no ceiling; the two
        # consumers of the setting must agree.
        r = self._run(self._config(make_config, tmp_path, logs=True, max_total_gb=float("inf")))
        assert "of inf GB" not in r.detail
        assert "no ceiling configured" in r.detail

    @pytest.mark.parametrize(
        "files",
        [("alice/a.jsonl", "stray.jsonl"), ("alice/deep/a.jsonl",)],
        ids=["stray-file-at-the-root", "nested-file-in-a-user-tree"],
    )
    def test_the_file_count_matches_what_the_sweep_measures(self, make_config, tmp_path, files):
        # The sweep measures per-user directories recursively, so a stray file
        # at the root is in no user's tree and a nested one is counted.
        config = self._config(make_config, tmp_path)
        for rel in files:
            path = config.db_path.parent / "logs" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 4096)
        assert "1 file" in self._run(config).detail

    def test_an_unreadable_state_row_is_not_a_finding(self, make_config, tmp_path):
        from istota import db as _db
        from istota.session.session_log import SWEEP_STATE_KEY, SWEEP_STATE_NAMESPACE

        config = self._config(make_config, tmp_path, logs=True)
        _db.init_db(config.db_path)
        with _db.get_db(config.db_path) as conn:
            _db.shared_kv_set(
                conn, SWEEP_STATE_NAMESPACE, SWEEP_STATE_KEY, "not json", "test",
            )
        assert self._run(config).status == OK

    def test_the_check_never_raises_on_a_broken_config(self, make_config, tmp_path):
        # A `dir` that names a file must come back as a finding, not an exception.
        config = self._config(make_config, tmp_path)
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        config.brain.native.session_log.dir = str(blocker)
        assert self._run(config).status in (WARN, FAIL)


class TestTaskControlDir:
    """`runtime.task_control_dir` — the daemon-owned tree the framework writes
    each task's prompt halves, briefing metadata and prepared image attachments
    into.

    Two independent questions, both answered on every run: is the tree itself a
    directory the daemon owns at 0700, and is anything model-writable at or
    above it. The second is what the layout rests on — the tree is a *sibling*
    of the per-user workspaces — and it is a property of the rendered config,
    the class of fact the suite cannot see. The mask axis runs the other way
    from `runtime.session_log_dir`'s: a mask *over* the control tree takes away
    a file the task must open, so the finding is the mask existing.
    """

    NAME = "runtime.task_control_dir"

    def _config(self, make_config, tmp_path, *, users=("alice",), **overrides):
        from istota.config import UserConfig

        temp = tmp_path / "srv" / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        data = tmp_path / "srv" / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "istota.db").touch()
        # A mapping is taken as written, so a test that needs resources on a
        # user can pass one; rebuilding it from the keys once ran the resource
        # tests against users with no rows, passing on an OK that meant nothing.
        if not isinstance(users, dict):
            users = {u: UserConfig() for u in users}
        kwargs = {
            "db_path": data / "istota.db",
            "temp_dir": temp,
            "users": users,
        }
        kwargs.update(overrides)
        return make_config(**kwargs)

    def _run(self, config, **kwargs):
        return run_checks(config, only=(self.NAME,), **kwargs)[0]

    def _root(self, config, mode=None):
        """The control root; with `mode`, created and chmod'd to it (after the
        `mkdir`, so the umask cannot decide the mode under test)."""
        from istota.executor import CONTROL_DIR_NAME

        root = Path(config.temp_dir).resolve() / CONTROL_DIR_NAME
        if mode is not None:
            root.mkdir(parents=True)
            os.chmod(root, mode)
        return root

    def test_skips_when_no_users_are_configured(self, make_config, tmp_path):
        # Nothing names a control directory until a task runs for somebody.
        r = self._run(self._config(make_config, tmp_path, users=()))
        assert r.status == SKIP
        assert "user" in r.detail

    # -- the healthy shape -------------------------------------------------

    def test_ok_before_the_tree_exists(self, make_config, tmp_path):
        # `ensure_task_control_dir` creates it on the first task. The OK line
        # names the root and how many users it resolved for.
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        r = self._run(config)
        assert r.status == OK
        assert str(self._root(config)) in r.detail
        assert "2" in r.detail

    def test_ok_on_a_well_formed_tree(self, make_config, tmp_path):
        """Also the control for the uid finding below, which would otherwise pass
        on a check that reported a uid line unconditionally."""
        config = self._config(make_config, tmp_path)
        root = self._root(config, 0o700)
        (root / "alice").mkdir()
        os.chmod(root / "alice", 0o700)
        r = self._run(config)
        assert r.status == OK
        assert not r.remedy
        assert "uid" not in r.detail

    # -- question one: is the tree itself ours ------------------------------

    def test_reports_a_widened_root(self, make_config, tmp_path):
        # Self-healing on the next task, but only while the daemon still owns
        # the directory; until then every local account can walk into it.
        config = self._config(make_config, tmp_path)
        self._root(config, 0o755)
        r = self._run(config)
        assert r.status == WARN
        assert "0755" in r.detail
        assert r.remedy

    @pytest.mark.parametrize("shape", ["file", "symlink"])
    def test_fails_when_the_root_is_not_a_directory(self, make_config, tmp_path, shape):
        # `_ensure_control_level` refuses either with ENOTDIR or on its
        # containment equality, so every task of every user fails at start-up.
        config = self._config(make_config, tmp_path)
        if shape == "file":
            self._root(config).write_text("not a directory")
        else:
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            self._root(config).symlink_to(elsewhere)
        r = self._run(config)
        assert r.status == FAIL
        assert ("not a directory" if shape == "file" else "symlink") in r.detail
        assert r.remedy

    def test_reports_a_root_owned_by_another_account(
        self, make_config, tmp_path, monkeypatch,
    ):
        # Every task under the level would fail, but WARN: the only uid
        # available is this process's, `istota doctor` is often run from an
        # operator's own account, and a FAIL mails every admin from the sweep.
        # The real uid is read *before* the patch, since `doctor.os` is `os`.
        config = self._config(make_config, tmp_path)
        self._root(config, 0o700)
        mine = os.geteuid()
        monkeypatch.setattr(doctor.os, "geteuid", lambda: mine + 1)
        r = self._run(config)
        assert r.status == WARN
        # Both uids, so a reader can tell which of the two cases they are in.
        assert f"owned by uid {mine}" in r.detail
        assert f"runs as uid {mine + 1}" in r.detail
        assert r.remedy

    @pytest.mark.parametrize("shape", ["widened", "file"])
    def test_reports_a_per_user_level(self, make_config, tmp_path, shape):
        # `_ensure_control_level` checks all three levels and fails the task
        # from any of them, so inspecting only the root reports a healthy
        # deployment while every task of one user fails.
        config = self._config(make_config, tmp_path)
        root = self._root(config, 0o700)
        if shape == "widened":
            (root / "alice").mkdir()
            os.chmod(root / "alice", 0o755)
        else:
            (root / "alice").write_text("not a directory")
        r = self._run(config)
        assert r.status == (WARN if shape == "widened" else FAIL)
        assert "control directory of user 'alice'" in r.detail
        if shape == "widened":
            assert "0755" in r.detail
            assert r.remedy

    # -- question two: is anything model-writable above it ------------------

    def test_reports_a_user_workspace_at_or_above_the_control_tree(
        self, make_config, tmp_path,
    ):
        # `{temp_dir}/{user}` is bound read-write into that user's sandbox; a
        # link resolving it to the shared temp root puts every user's control
        # tree inside that bind.
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        temp = Path(config.temp_dir)
        (temp / "bob").symlink_to(temp)
        r = self._run(config)
        assert r.status == WARN
        assert "overlaps the control tree" in r.detail
        assert "bob" in r.detail
        assert r.remedy

    def test_reports_the_user_id_that_collides_with_the_control_directory(
        self, make_config, tmp_path,
    ):
        # `get_user_temp_dir` is a plain join, so this user's scratch directory
        # is exactly where the control root goes. `get_task_control_dir`
        # refuses the name; this says so out loud.
        r = self._run(self._config(make_config, tmp_path, users=("alice", ".control")))
        assert r.status == FAIL
        # Both arms by name: the refusal alone satisfies a loose `".control"`
        # check and would stay green with the overlap comparison deleted.
        assert "no control directory can be named for '.control'" in r.detail
        assert "the workspace of user '.control'" in r.detail
        assert "overlaps the control tree" in r.detail
        assert r.remedy

    @pytest.mark.parametrize(
        "path,permissions,sandbox,status",
        [
            # A `user_resources` row resolves to `mount / resource_path`,
            # bounded by the workspace root alone, so where `temp_dir` sits
            # under it a row is a second route into the tree. No shipped shape
            # produces the layout; this would say so if one did.
            ("tmp/.control", "readwrite", True, WARN),
            # Read-only is the read exposure: one task reading every other task
            # of that user's assembled prompt.
            ("tmp", "read", True, WARN),
            # Nothing is bound with the sandbox off, so there is no bind to
            # widen. The *requested* flag, because the effective one spawns.
            ("tmp/.control", "readwrite", False, OK),
            ("Docs", "readwrite", True, OK),
        ],
        ids=["readwrite-row", "read-only-row", "sandbox-off", "row-elsewhere"],
    )
    def test_a_resource_row(self, make_config, tmp_path, path, permissions, sandbox, status):
        from istota.config import ResourceConfig, SecurityConfig, UserConfig

        mount = tmp_path / "mount"
        temp = mount / "tmp"
        temp.mkdir(parents=True)
        (mount / "Docs").mkdir(parents=True, exist_ok=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            workspace_path=mount,
            security=SecurityConfig(sandbox_enabled=sandbox),
            users={
                "alice": UserConfig(
                    resources=[ResourceConfig(type="folder", path=path, permissions=permissions)],
                ),
            },
        )
        r = self._run(config)
        assert r.status == status
        if status == WARN:
            assert "overlaps the control tree" in r.detail
            assert permissions in r.detail
            assert r.remedy

    def test_reports_a_repos_subtree_that_holds_the_control_tree(
        self, make_config, tmp_path,
    ):
        # Measured in review: with `temp_dir` under `{repos_dir}/{user}`, a
        # read-write bind for an admin developer task, the check said `ok`.
        from istota.config import DeveloperConfig, SecurityConfig

        repos = tmp_path / "repos"
        temp = repos / "alice" / "tmp"
        temp.mkdir(parents=True)
        config = self._config(
            make_config, tmp_path,
            temp_dir=temp,
            security=SecurityConfig(sandbox_enabled=True),
            developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
        )
        r = self._run(config)
        assert r.status == WARN
        assert "overlaps the control tree" in r.detail
        assert "repos subtree" in r.detail

    def test_reports_a_sandbox_ro_paths_entry_over_the_control_tree(
        self, make_config, tmp_path,
    ):
        # `load_config` already logs this once per process; doctor is the
        # surface an operator reads, and this entry is bound verbatim.
        from istota.config import SecurityConfig

        config = self._config(make_config, tmp_path)
        config.security = SecurityConfig(
            sandbox_enabled=True, sandbox_ro_paths=[str(config.temp_dir)],
        )
        r = self._run(config)
        assert r.status == WARN
        assert "sandbox_ro_paths entry" in r.detail
        assert "overlaps the control tree" in r.detail

    # -- question two, second half: the database mask -----------------------

    def _bwrap(self, monkeypatch, *, available=True, cached=True):
        """Stand in for the bwrap capability probe, recording each invocation.
        A `subprocess` spy cannot see the spawn: `_bwrap_available` returns at
        its platform check on macOS and memoizes after its first call."""
        from istota import executor

        calls: list[str] = []

        def _probe():
            calls.append("bwrap")
            return available

        monkeypatch.setattr(executor, "_bwrap_available", _probe)
        monkeypatch.setattr(executor, "_bwrap_checked", cached)
        return calls

    def _masked(self, make_config, tmp_path, user=None):
        """Put `db_path` at the control root, or inside `user`'s level, so the
        database mask lands on the tree. A mask anywhere above `temp_dir`
        shadows the workspace and is refused, so only these reach it."""
        config = self._config(make_config, tmp_path)
        target = self._root(config, 0o700)
        if user is not None:
            target = target / user
            target.mkdir()
        config.db_path = target / "istota.db"
        config.db_path.touch()
        return config, target

    @pytest.mark.parametrize("user", [None, "alice"], ids=["over-the-tree", "inside-one-level"])
    def test_reports_a_mask_that_reaches_the_control_tree(
        self, make_config, tmp_path, monkeypatch, user,
    ):
        # The mask is the last mount operation, so a control directory under it
        # could never be opened and every Claude Code task would fail at
        # start-up. A mask inside the tree takes one user's and is emitted
        # rather than refused; a one-directional test reported nothing there.
        self._bwrap(monkeypatch, available=True)
        config, target = self._masked(make_config, tmp_path, user)
        r = self._run(config)
        assert r.status == WARN
        # The established prefix by name: "masked out of every sandbox" alone
        # is a substring of the "would be" prefix too.
        assert doctor._CONTROL_MASKED in r.detail
        assert str(target) in r.detail
        assert r.remedy

    def test_a_mask_that_is_refused_does_not_produce_a_finding(
        self, make_config, tmp_path, monkeypatch,
    ):
        # `mask_shadowed_by` is the sandbox builder's own predicate: "is the
        # tree under db_path.parent" answers True on the standalone install,
        # where the mask is refused and never emitted.
        from istota.config import UserConfig

        self._bwrap(monkeypatch, available=True)
        workspace = tmp_path / "istota-home"
        (workspace / "tmp").mkdir(parents=True)
        (workspace / "istota.db").touch()
        config = make_config(
            db_path=workspace / "istota.db",
            temp_dir=workspace / "tmp",
            workspace_path=workspace,
            users={"alice": UserConfig()},
        )
        assert "masked" not in self._run(config).detail

    def test_the_healthy_layout_asks_no_availability_question_at_all(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The answer cannot change the verdict where no mask would reach the
        # tree, and asking it spawns, so the shape question is asked first.
        calls = self._bwrap(monkeypatch, available=True, cached=None)
        assert self._run(self._config(make_config, tmp_path)).status == OK
        assert calls == []

    def test_probe_false_does_not_spawn_for_the_mask_question(
        self, make_config, tmp_path, monkeypatch,
    ):
        # And it must not assert, in an unestablished answer, that the tree is
        # masked: neither pass on an unsettled question nor assert an
        # unobserved condition.
        calls = self._bwrap(monkeypatch, cached=None)
        r = self._run(self._masked(make_config, tmp_path)[0], probe=False)
        assert calls == [], "probe=False invoked the bwrap probe"
        assert r.status == WARN
        assert "could not be established" in r.detail
        assert "is masked out of every sandbox" not in r.detail

    def test_a_warm_memo_answers_without_probing(
        self, make_config, tmp_path, monkeypatch,
    ):
        # The daemon probes at start-up, so "could not be established" while
        # `_bwrap_checked` holds the answer would be wrong.
        calls = self._bwrap(monkeypatch, available=False, cached=False)
        r = self._run(self._masked(make_config, tmp_path)[0], probe=False)
        assert calls == []
        assert "would be masked" in r.detail
        assert "could not be established" not in r.detail

    def test_both_questions_are_reported_rather_than_the_first(
        self, make_config, tmp_path,
    ):
        # An operator who fixes one reason would otherwise hear nothing about
        # the second and still have the tree reachable.
        config = self._config(make_config, tmp_path, users=("alice", "bob"))
        self._root(config, 0o755)
        temp = Path(config.temp_dir)
        (temp / "bob").symlink_to(temp)
        r = self._run(config)
        assert r.status == WARN
        # `"0755"` alone appears in the unconditional `observed` prefix, so
        # assert on wording only the mode finding produces.
        assert "rather than 0700" in r.detail
        assert "overlaps the control tree" in r.detail

    @pytest.mark.parametrize("broken", ["relative-temp-dir", "empty-db-path"])
    def test_never_raises(self, make_config, tmp_path, broken):
        # Called directly rather than through `run_checks`, which catches every
        # exception and would report a raise as a plain FAIL.
        config = self._config(make_config, tmp_path)
        if broken == "relative-temp-dir":
            config.temp_dir = Path("relative/tmp")
        else:
            config.db_path = Path("")
            config.workspace_path = None
        r = doctor.check_task_control_dir(config, True)
        assert r.status in (OK, WARN, FAIL, SKIP)


class TestSandboxMasksUsesTheNativeProfile:
    """The `sandbox.masks` probe execs `/bin/sh`, not the `claude` CLI.

    It moved to `SandboxProfile.NATIVE` with the profile split, so a diagnostic
    no longer builds a namespace holding the subscription credential. The claim
    that came with that move is that its *verdict* is unchanged: everything the
    probe reads — the two database masks, and the system binds that make
    `/bin/sh` resolve — is part of the generic plan and identical under both
    profiles. The verdict is asserted end to end only on the Linux tier
    (`tests/linux/test_sandbox_profiles_real.py`).
    """

    @pytest.fixture
    def claude_home(self, tmp_path, monkeypatch):
        """A HOME with a sentinel at every Claude runtime path, so "the probe
        carries none of them" is an assertion about the gate rather than about
        an empty home directory."""
        home = tmp_path / "home"
        for d in (
            ".local/bin", ".local/share/claude", ".local/state/claude",
            ".claude/projects", ".claude/debug", ".claude/todos",
        ):
            (home / d).mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text('{"token": "sentinel"}')
        monkeypatch.setenv("HOME", str(home))
        return home

    def _run_and_capture(self, make_config, monkeypatch, tmp_path):
        monkeypatch.setattr(doctor, "_bwrap_usable", lambda: True)
        monkeypatch.setattr("istota.executor._bwrap_available", lambda: True)
        seen = {}

        class _Result:
            stdout = ""
            stderr = ""
            returncode = 0

        def _capture(cmd, **kwargs):
            seen["cmd"] = cmd
            return _Result()

        monkeypatch.setattr(doctor.subprocess, "run", _capture)
        # The database in a directory of its own: beside the mount a mask is
        # refused, which would make every assertion about the refusal.
        config = make_config(db_path=tmp_path / "data" / "istota.db")
        Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(config.db_path).touch()
        verdict = run_checks(config, only=("sandbox.masks",), deep=True)[0]
        return verdict, seen["cmd"], config

    def test_the_probe_is_built_under_the_native_profile_and_still_masks(
        self, make_config, monkeypatch, claude_home, tmp_path,
    ):
        """The mask half is the control: a NATIVE probe that had lost the masks
        would satisfy "no Claude paths" perfectly."""
        verdict, cmd, config = self._run_and_capture(make_config, monkeypatch, tmp_path)

        assert verdict.status == OK
        assert cmd[0] == "bwrap"
        for token in cmd:
            assert str(claude_home / ".claude") not in token
            assert str(claude_home / ".local") not in token

        masked = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--tmpfs"]
        db_dir = Path(config.db_path).parent.resolve()
        assert str(db_dir) in masked
        # `_mask_dir` skips a candidate an earlier mask already covers, so the
        # module root is asserted covered rather than masked on its own.
        assert config.module_db_root().resolve().is_relative_to(db_dir)

    def test_everything_the_probe_reads_is_identical_under_both_profiles(
        self, make_config, monkeypatch, claude_home, tmp_path,
    ):
        """Rebuilds the same argv under CLAUDE and compares the masks and the
        system binds, so a profile-dependent change shows here before the
        Linux tier."""
        import tempfile

        from istota import db
        from istota.executor import SandboxProfile, build_bwrap_cmd

        _, native_cmd, config = self._run_and_capture(
            make_config, monkeypatch, tmp_path,
        )
        task = db.Task(
            id=0, status="running", source_type="doctor", user_id="doctor", prompt="",
        )
        script = native_cmd[native_cmd.index("--") + 3]
        with tempfile.TemporaryDirectory(prefix="istota-doctor-probe-") as user_temp:
            claude_cmd = build_bwrap_cmd(
                ["/bin/sh", "-c", script], config, task, is_admin=False,
                user_resources=[], user_temp_dir=Path(user_temp),
                profile=SandboxProfile.CLAUDE,
            )

        def _masks(argv):
            """Every tmpfs/remount-ro after the last bind. Not every `--tmpfs`:
            `/tmp` is mounted early, and under CLAUDE so is the `~/.claude`
            base this profile deliberately drops."""
            last_bind = max(
                (i for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")),
                default=-1,
            )
            return [
                (argv[i], argv[i + 1]) for i in range(last_bind + 1, len(argv) - 1)
                if argv[i] in ("--tmpfs", "--remount-ro")
            ]

        def _system_binds(argv):
            return [
                (argv[i], argv[i + 1], argv[i + 2])
                for i, a in enumerate(argv)
                if a in ("--ro-bind", "--symlink")
                and argv[i + 1].startswith(("/usr", "/bin", "/lib", "/sbin", "/etc"))
            ]

        # The user temp dir differs between the two calls, so compare the two
        # families the verdict actually reads.
        assert _masks(native_cmd) == _masks(claude_cmd)
        assert _system_binds(native_cmd) == _system_binds(claude_cmd)
        assert native_cmd[native_cmd.index("--"):][:3] == ["--", "/bin/sh", "-c"]


class TestVerdict:
    """`verdict` — the adapter for a caller that needs a boolean and a sentence.

    `!check`'s non-admin arm and `heartbeat._check_self` are the two consumers;
    both used to compute pass/fail from their own hand-rolled probe.
    """

    @staticmethod
    def _results(*statuses):
        return [CheckResult(f"a.{i}", s, "detail") for i, s in enumerate(statuses)]

    @pytest.mark.parametrize(
        "statuses,healthy,summary",
        [
            ((OK, FAIL), False, "1 fail"),
            # A warning that pages someone is a failure wearing the wrong label;
            # `heartbeat._check_self` used to return unhealthy for one.
            ((OK, WARN), True, "1 warn"),
            # A run that checked nothing must not read as one that passed
            # everything, so the count carries the caveat.
            ((SKIP, SKIP), True, "2 skip"),
            ((SKIP, SKIP), True, "0 ok"),
        ],
        ids=["fail", "warn", "all-skip", "all-skip-no-ok"],
    )
    def test_the_verdict(self, statuses, healthy, summary):
        got_healthy, got_summary = doctor.verdict(self._results(*statuses))
        assert got_healthy is healthy
        assert summary in got_summary

    def test_every_status_appears_in_the_summary_in_order(self):
        assert doctor.verdict(self._results(OK, WARN, FAIL, SKIP))[1] == "1 ok, 1 warn, 1 fail, 1 skip"

    def test_an_empty_list_says_so_in_words(self):
        assert doctor.verdict([]) == (True, "no checks ran")

    @pytest.mark.parametrize("statuses", [(OK,), (WARN,), (FAIL,), (SKIP,), ()])
    def test_it_agrees_with_exit_code(self, statuses):
        """A caller reading the bool and a caller reading `exit_code` must not
        disagree about whether the deployment is healthy."""
        results = self._results(*statuses)
        healthy, _ = doctor.verdict(results)
        assert healthy is (exit_code(results) == 0)

    def test_verdict_and_summarize_are_different_functions(self):
        """`summarize` returns counts by status and `verdict` a bool and a
        sentence; a tidy-up merging them would hand the two callers a dict."""
        assert doctor.verdict is not doctor.summarize
        results = self._results(OK)
        assert isinstance(doctor.summarize(results), dict)
        assert isinstance(doctor.verdict(results), tuple)


#: The two files that used to carry a hand-rolled copy of the health probe.
_ONCE_HAND_ROLLED = ("heartbeat.py", "commands.py")

#: Strings that only a hand-rolled copy of the probe can contain. The marker is
#: what the copies asked the model to echo; `build_bwrap_cmd` is how they
#: wrapped it. Both now live in `doctor.check_model_execution` and nowhere else.
_PROBE_FINGERPRINTS = (_MODEL_MARKER, "build_bwrap_cmd")


@pytest.mark.parametrize("filename", _ONCE_HAND_ROLLED)
def test_no_hand_rolled_health_probe(filename):
    """Neither caller carries its own health probe any more.

    Cheap, and it is what stops the copies growing back — the same shape as
    `tests/test_lint_scope.py`. Both files ran the same five checks as doctor's
    registry, in the same order, drifting from it and from each other; the whole
    point of pointing them at `run_checks` is that there is one copy.
    """
    source = Path(doctor.__file__).parent / filename
    text = source.read_text()
    for fingerprint in _PROBE_FINGERPRINTS:
        assert fingerprint not in text, (
            f"{filename} contains {fingerprint!r} — a hand-rolled health probe "
            "has grown back. The one copy lives in "
            "`doctor.check_model_execution`; call `doctor.run_checks` instead."
        )


# ---------------------------------------------------------------------------
# talk.signaling_*
# ---------------------------------------------------------------------------


def _signaling_config(make_config, *, talk=True, enabled=True, url="", **fields):
    from istota.config import NextcloudConfig, TalkConfig, TalkSignalingConfig

    config = make_config(nextcloud=NextcloudConfig(url="https://nc.example.com"))
    config.talk = TalkConfig(
        enabled=talk,
        signaling=TalkSignalingConfig(enabled=enabled, url=url, **fields),
    )
    return config


def _settings_payload(mode="external", server="https://hpb.example.com/signaling"):
    from istota.transport.talk import signaling as sig

    return sig.parse_settings(
        {"signalingMode": mode, "server": server, "helloAuthParams": {}},
        nextcloud_url="https://nc.example.com",
    )


def _caps(mode="external", key="LS0tLS1CRUdJTiBQVUJMSUMgS0VZ"):
    config = {"signaling": {"mode": mode}}
    if key is not None:
        config["signaling"]["hello-v2-token-key"] = key
    return {"capabilities": {"spreed": {"config": config}}}


@pytest.fixture
def signaling_seams(monkeypatch):
    """Stand in for the three network calls the signaling checks make, which
    against the real thing would assert about the developer's own host."""
    doctor.reset_signaling_probe_memo()

    state = {
        "settings": _settings_payload(),
        "settings_error": "",
        "welcome": {"type": "welcome", "welcome": {
            "version": "2.1.1",
            "features": ["hello-v2", "chat-relay", "welcome"],
        }},
        "welcome_error": "",
        "capabilities": _caps(),
        "capabilities_error": "",
        "urls": [],
    }

    def fake_settings(config, timeout):
        if state["settings_error"]:
            return None, state["settings_error"]
        return state["settings"], ""

    def fake_welcome(ws_url, timeout):
        state["urls"].append(ws_url)
        if state["welcome_error"]:
            return {}, state["welcome_error"]
        return state["welcome"], ""

    def fake_caps(config, timeout):
        if state["capabilities_error"]:
            return None, state["capabilities_error"]
        return state["capabilities"], ""

    monkeypatch.setattr(doctor, "_signaling_settings", fake_settings)
    monkeypatch.setattr(doctor, "_signaling_welcome_frame", fake_welcome)
    monkeypatch.setattr(doctor, "_signaling_capabilities", fake_caps)
    yield state
    doctor.reset_signaling_probe_memo()


_SIGNALING_CHECKS = (
    "talk.signaling_reachable",
    "talk.signaling_chat_relay",
    "talk.signaling_auth",
    "talk.signaling_watchers",
)


def test_the_signaling_checks_are_registered_deployment_scoped_and_cheap():
    # Every one asks about a rendered config, a running signaling server or a
    # live supervisor, none of which a bare `docker run` has.
    names = {name for name, _ in CHECKS}
    for name in _SIGNALING_CHECKS:
        assert name in names
        assert doctor.CHECK_SCOPES[name] == DEPLOYMENT
        assert name not in DEEP_CHECKS
        assert name not in LIVE_CHECKS


class TestSignalingReachable:
    """The HPB answers a ``welcome``, read before any hello.

    Unauthenticated by construction: no hello, so no signaling session and no
    ``participants/active`` POST. ``doctor`` runs on a scheduler interval and
    from the admin Health pane, so a check that joined a room would put a
    phantom participant in it every time somebody opened a dashboard.
    """

    @pytest.mark.parametrize("fields", [{"enabled": False}, {"talk": False}], ids=["signaling-off", "talk-off"])
    def test_disabled_skips(self, make_config, signaling_seams, fields):
        result = doctor.check_signaling_reachable(_signaling_config(make_config, **fields), True)
        assert result.status == SKIP

    def test_a_deployment_with_nothing_configured_skips(self, make_config):
        """The spec's acceptance line: answers `skip`, not `fail`."""
        results = run_checks(make_config(), only=("talk.signaling_reachable",))
        assert [r.status for r in results] == [SKIP]

    def test_probe_disabled_opens_no_socket(self, make_config, signaling_seams):
        result = doctor.check_signaling_reachable(_signaling_config(make_config), False)
        assert result.status == SKIP
        assert signaling_seams["urls"] == [], "a socket was opened under probe=False"

    def test_a_welcome_frame_from_the_discovered_server_is_ok(self, make_config, signaling_seams):
        result = doctor.check_signaling_reachable(_signaling_config(make_config), True)
        assert result.status == OK, result.detail
        assert "2.1.1" in result.detail
        assert signaling_seams["urls"] == ["wss://hpb.example.com/signaling/spreed"]

    def test_a_configured_url_wins_and_costs_nextcloud_nothing(
        self, make_config, signaling_seams, monkeypatch
    ):
        def refuse(config, timeout):
            raise AssertionError(
                "the settings endpoint was called for a configured URL"
            )

        monkeypatch.setattr(doctor, "_signaling_settings", refuse)
        config = _signaling_config(make_config, url="https://other.example.com/sig/")

        result = doctor.check_signaling_reachable(config, True)

        assert result.status == OK, result.detail
        assert signaling_seams["urls"] == ["wss://other.example.com/sig/spreed"]

    def test_internal_mode_skips_and_only_the_auth_check_fails(
        self, make_config, signaling_seams
    ):
        """One cause, one FAIL. With no backend registered there is nothing to
        reach, and `talk.signaling_auth` already FAILs it with the remedy that
        fixes it; a second FAIL would page an operator twice."""
        signaling_seams["settings"] = _settings_payload(mode="internal")
        signaling_seams["capabilities"] = _caps(mode="internal")
        config = _signaling_config(make_config)

        result = doctor.check_signaling_reachable(config, True)
        assert result.status == SKIP
        assert "internal" in result.detail
        assert "talk.signaling_auth" in result.detail

        results = run_checks(config, only=("talk.signaling_",))
        failed = [r.name for r in results if r.status == FAIL]
        assert failed == ["talk.signaling_auth"], [
            (r.name, r.status) for r in results
        ]

    def test_an_unreachable_server_fails_without_promising_a_boot_refusal(
        self, make_config, signaling_seams
    ):
        """An unreachable-but-registered server is not one of the two startup
        refusals: watchers retry on a backoff and reconciliation carries
        inbound meanwhile, so the remedy must not send the operator looking for
        a boot failure that will not happen."""
        signaling_seams["welcome_error"] = "connection refused"
        result = doctor.check_signaling_reachable(_signaling_config(make_config), True)

        assert result.status == FAIL
        assert "connection refused" in result.detail
        assert "refuses to start" not in result.remedy
        assert "room_sync_interval" in result.remedy

    def test_a_settings_call_that_failed_is_reported_with_its_cause(
        self, make_config, signaling_seams
    ):
        signaling_seams["settings_error"] = "401 Unauthorized"
        result = doctor.check_signaling_reachable(_signaling_config(make_config), True)

        assert result.status == SKIP
        assert "401" in result.detail

    def test_a_missing_library_fails_naming_the_extra(
        self, make_config, signaling_seams, monkeypatch
    ):
        from istota.transport.talk import signaling as sig

        def refuse():
            raise sig.SignalingUnavailable(
                "the websockets library is not installed; install istota[signaling]"
            )

        monkeypatch.setattr(sig, "require_websockets", refuse)
        result = doctor.check_signaling_reachable(_signaling_config(make_config), True)

        # A fault, not an unanswerable question: `enabled = true` with no
        # library is one of the two startup refusals.
        assert result.status == FAIL
        assert "signaling" in result.detail
        assert "refuses to start" in result.remedy
        assert signaling_seams["urls"] == []

    def test_the_probe_is_shared_with_the_chat_relay_check(
        self, make_config, signaling_seams
    ):
        """One socket per doctor run, not one per check: probing twice would
        double the settings call and the connect on every sweep and page load."""
        results = run_checks(_signaling_config(make_config), only=("talk.signaling_",))
        assert len(signaling_seams["urls"]) == 1, signaling_seams["urls"]
        assert {r.status for r in results} <= {OK, SKIP}


class TestSignalingChatRelay:
    def test_present_is_ok(self, make_config, signaling_seams):
        result = doctor.check_signaling_chat_relay(_signaling_config(make_config), True)
        assert result.status == OK

    def test_absent_warns_naming_the_consequence(self, make_config, signaling_seams):
        signaling_seams["welcome"] = {
            "type": "welcome",
            "welcome": {"version": "2.0.1", "features": ["hello-v2", "welcome"]},
        }
        result = doctor.check_signaling_chat_relay(_signaling_config(make_config), True)

        assert result.status == WARN
        assert "chat-relay" in result.detail
        assert result.remedy

    def test_an_unreachable_server_skips_rather_than_claiming_absence(
        self, make_config, signaling_seams
    ):
        """Reporting "no chat-relay" for a server we could not reach would send
        an operator to upgrade a server that is simply down."""
        signaling_seams["welcome_error"] = "connection refused"
        result = doctor.check_signaling_chat_relay(_signaling_config(make_config), True)

        assert result.status == SKIP
        assert "talk.signaling_reachable" in result.detail

    def test_probe_disabled_skips(self, make_config, signaling_seams):
        result = doctor.check_signaling_chat_relay(_signaling_config(make_config), False)
        assert result.status == SKIP
        assert signaling_seams["urls"] == []


class TestSignalingAuth:
    """Talk's own half: external mode, and a hello-v2 token key.

    Reads ``/cloud/capabilities`` and mints nothing. The mode question is
    answered by ``signaling.signaling_mode_reason``, the same predicate the
    startup refusal uses, so the two cannot disagree.
    """

    def _check(self, make_config, signaling_seams, capabilities=None, probe=True, **fields):
        if capabilities is not None:
            signaling_seams["capabilities"] = capabilities
        return doctor.check_signaling_auth(_signaling_config(make_config, **fields), probe)

    def test_external_with_a_key_is_ok(self, make_config, signaling_seams):
        assert self._check(make_config, signaling_seams).status == OK

    def test_internal_mode_fails(self, make_config, signaling_seams):
        result = self._check(make_config, signaling_seams, _caps(mode="internal"))

        assert result.status == FAIL
        assert "internal" in result.detail
        assert "talk:signaling" in result.remedy

    def test_no_hello_v2_key_warns_naming_the_non_expiring_ticket(
        self, make_config, signaling_seams
    ):
        result = self._check(make_config, signaling_seams, _caps(key=None))

        assert result.status == WARN
        assert "v1" in result.detail
        assert "expire" in result.detail or "rotate" in result.detail

    def test_the_key_itself_is_never_echoed(self, make_config, signaling_seams):
        """It is a *public* key, so this is hygiene rather than a boundary: a
        detail is one line of what was observed, not a base64 blob."""
        marker = "PUBLIC-KEY-MATERIAL-abcdef"
        result = self._check(make_config, signaling_seams, _caps(key=marker))

        assert marker not in result.detail
        assert marker not in result.remedy

    def test_unreadable_capabilities_warn_rather_than_pass(
        self, make_config, signaling_seams
    ):
        signaling_seams["capabilities_error"] = "connection timed out"
        result = self._check(make_config, signaling_seams)

        assert result.status == WARN
        assert "timed out" in result.detail

    @pytest.mark.parametrize(
        "probe,fields", [(False, {}), (True, {"enabled": False})], ids=["probe-disabled", "signaling-off"]
    )
    def test_skips(self, make_config, signaling_seams, probe, fields):
        assert self._check(make_config, signaling_seams, probe=probe, **fields).status == SKIP

    @pytest.mark.parametrize(
        "capabilities",
        [
            {"capabilities": {"spreed": {"config": {"signaling": {
                "hello-v2-token-key": "LS0tLS1CRUdJTg==",
            }}}}},
            {"capabilities": {}},
        ],
        ids=["no-mode-key", "no-spreed-block"],
    )
    def test_a_talk_that_publishes_no_mode_warns_rather_than_failing(
        self, make_config, signaling_seams, capabilities
    ):
        """`capabilities.spreed.config.signaling.mode` was read off the
        deployment this design was verified against; another Talk version may
        not publish it, and falling through `signaling_mode_reason`'s
        unreadable-mode arm would FAIL a deployment whose backend is fine."""
        result = self._check(make_config, signaling_seams, capabilities)

        assert result.status == WARN, result.detail
        if "spreed" in capabilities["capabilities"]:
            assert "mode" in result.detail
            assert "talk.signaling_reachable" in result.remedy


class TestSignalingWatchers:
    """The number that separates "the stream is working" from "the safety net
    is carrying it": a sweep-style net backfills silently, so every room stays
    current while the event path delivers nothing. ``rooms_behind`` is what
    makes that visible, and the census must not contradict itself.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        from istota.transport.talk import signaling as sig

        sig.clear_stats_source()
        yield
        sig.clear_stats_source()

    def _check(self, make_config, probe=True, enabled=True, **fields):
        from istota.transport.talk import signaling as sig

        base = {"watchers": 3, "connected": 3, "disconnected": [], "rooms_behind": 0}
        base.update(fields)
        sig.set_stats_source(lambda: dict(base))
        return doctor.check_signaling_watchers(_signaling_config(make_config, enabled=enabled), probe)

    def test_no_supervisor_in_this_process_skips(self, make_config):
        """doctor also runs in the web process, the CLI and `!check`, which
        were never supposed to have watchers."""
        result = doctor.check_signaling_watchers(_signaling_config(make_config), True)
        assert result.status == SKIP

    @pytest.mark.parametrize(
        "probe", [True, False], ids=["probed", "no-probe-needed-for-in-process-counters"]
    )
    def test_all_connected_and_nothing_behind_is_ok(self, make_config, probe):
        result = self._check(make_config, probe=probe)
        assert result.status == OK, result.detail
        assert "3" in result.detail

    @pytest.mark.parametrize(
        "fields,named",
        [
            ({"connected": 2, "disconnected": ["abc123"]}, ("abc123",)),
            # The case the check exists for: a socket that is up and delivering
            # nothing looks healthy from every other angle.
            ({"rooms_behind": 4}, ("4",)),
            # A supervisor may report counters and no token list; reading only
            # the list said OK beside "1 of 5 watchers connected".
            ({"watchers": 5, "connected": 1}, ("1 of 5",)),
        ],
        ids=["disconnected-watcher", "rooms-behind", "counts-only-shortfall"],
    )
    def test_warns(self, make_config, fields, named):
        result = self._check(make_config, **fields)
        assert result.status == WARN, result.detail
        assert result.remedy
        for text in named:
            assert text in result.detail

    def test_a_string_disconnected_field_does_not_become_six_rooms(self, make_config):
        """`or []` on a bare string iterates it character by character."""
        result = self._check(make_config, watchers=5, connected=4, disconnected="abc123")

        assert "a, b, c" not in result.detail
        assert "4 of 5" in result.detail

    def test_a_missing_key_reads_as_zero_rather_than_raising(self, make_config):
        from istota.transport.talk import signaling as sig

        sig.set_stats_source(lambda: {})
        result = doctor.check_signaling_watchers(_signaling_config(make_config), True)

        assert result.status == OK
        assert "0 of 0" in result.detail

    def test_a_supervisor_that_raises_skips_rather_than_failing(self, make_config):
        from istota.transport.talk import signaling as sig

        def boom():
            raise RuntimeError("mid-restart")

        sig.set_stats_source(boom)
        assert doctor.check_signaling_watchers(_signaling_config(make_config), True).status == SKIP

    def test_disabled_signaling_skips(self, make_config):
        assert self._check(make_config, enabled=False).status == SKIP

    def test_the_reader_is_handed_a_copy_not_the_supervisors_own_mapping(self):
        """The supervisor is on the loop thread and the reader is not."""
        from istota.transport.talk import signaling as sig

        live = {"watchers": 1, "connected": 1}
        sig.set_stats_source(lambda: live)

        read = sig.read_stats()
        read["watchers"] = 99

        assert live["watchers"] == 1

    def test_registering_a_mapping_instead_of_a_callable_is_reported(self, caplog):
        """A non-callable used to clear the source silently, after which the
        check reported "no supervisor in this process" for the daemon's life."""
        import logging

        from istota.transport.talk import signaling as sig

        with caplog.at_level(logging.WARNING, logger=sig.logger.name):
            sig.set_stats_source({"watchers": 1})

        assert sig.read_stats() is None
        assert any("callable" in r.getMessage() for r in caplog.records)


class TestTheProbeNeverAuthenticates:
    """The spec's load-bearing constraint, asserted against the one function
    that could break it.

    Every other signaling test monkeypatches `_signaling_welcome_frame`
    wholesale, so all of them assert about the fixture. Opening a signaling
    session means POSTing `participants/active`, so a check that sent a hello
    would put a phantom participant into a live room every time an admin opened
    the Health pane. Adding a `send` to that function must turn something red.
    """

    class _FakeSocket:
        def __init__(self, frame, log):
            self._frame = frame
            self.log = log

        async def recv(self):
            self.log.append("recv")
            return self._frame

        async def send(self, _payload):
            self.log.append("send")

        async def close(self):
            self.log.append("close")

    class _FakeConnect:
        def __init__(self, socket, log):
            self._socket = socket
            self._log = log

        def __call__(self, url, **kwargs):
            self._log.append(("connect", url, sorted(kwargs)))
            return self

        async def __aenter__(self):
            return self._socket

        async def __aexit__(self, *exc):
            self._log.append("exit")
            return False

    @pytest.fixture
    def fake_websockets(self, monkeypatch):
        import types

        from istota.transport.talk import signaling as sig

        log = []
        socket = self._FakeSocket(
            json.dumps({
                "type": "welcome",
                "welcome": {"version": "2.1.1", "features": ["chat-relay"]},
            }),
            log,
        )
        module = types.SimpleNamespace(
            connect=self._FakeConnect(socket, log)
        )
        monkeypatch.setattr(sig, "require_websockets", lambda: module)
        return log

    def test_it_reads_one_frame_and_sends_none(self, fake_websockets):
        frame, error = doctor._signaling_welcome_frame(
            "wss://hpb.example.com/spreed", 5.0,
        )

        assert error == ""
        assert frame["type"] == "welcome"
        assert "send" not in fake_websockets, (
            "the reachability probe sent a frame; a hello here creates a "
            "signaling session and, with it, a Talk participant row"
        )
        assert fake_websockets.count("recv") == 1

    def test_the_control_can_fail(self, fake_websockets):
        """Drive the same fake socket through a variant that does send a hello;
        if the log stays empty the instrument cannot see sends and the test
        above proves nothing."""
        import asyncio

        from istota.transport.talk import signaling as sig

        async def _sending_read():
            websockets = sig.require_websockets()
            async with websockets.connect("wss://h/spreed") as socket:
                await socket.send(json.dumps({"type": "hello"}))
                return json.loads(await asyncio.wait_for(socket.recv(), 1))

        doctor._run_off_loop(_sending_read, 5.0)

        assert "send" in fake_websockets, (
            "the instrument does not observe sends, so the assertion above "
            "proves nothing"
        )

    def test_it_bounds_itself_well_inside_the_callers_budget(self, monkeypatch):
        """The handshake and the first frame share the budget: giving each leg
        the whole number let the pair run to twice it before the outer bound
        noticed, naming the wrong number and leaving the socket live."""
        import types

        from istota.transport.talk import signaling as sig

        seen = {}

        class _Connect:
            def __call__(self, url, **kwargs):
                seen.update(kwargs)
                raise OSError("refused")

        monkeypatch.setattr(
            sig, "require_websockets",
            lambda: types.SimpleNamespace(connect=_Connect()),
        )

        doctor._signaling_welcome_frame("wss://h/spreed", 10.0)

        assert seen["open_timeout"] <= 10.0 / 2.0


class TestConfigVisibility:
    """The gate in front of the whole registry (ISSUE-412).

    `load_config` returns a bare `Config()` when no candidate resolves, so
    every path-and-policy check then answers about defaults while reading like
    a run about the real deployment. Inside a task that is *unconditional*:
    `build_clean_env` exports `ISTOTA_CONFIG_PATH` naming a file under
    `config/`, which is bound into no sandbox by design. A boundary doing its
    job is not a fault, so a *task* gets a `SKIP`, and "is this the daemon" is
    answered by `ISTOTA_TASK_ID` rather than by `ISTOTA_SANDBOXED`, which
    `task_env` sets only under `skill_proxy_enabled and effective_sandboxing`.
    """

    def _gate(self, make_config, monkeypatch, env, **kwargs):
        """The gate's answer for a config that loaded nothing, under `env`."""
        _clear_env(monkeypatch, "ISTOTA_SANDBOXED", "ISTOTA_TASK_ID", "ISTOTA_CONFIG_PATH")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return doctor.config_visibility(make_config(config_path=None), **kwargs)

    def test_a_loaded_config_opens_the_gate(self, make_config, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(tmp_path / "absent.toml"))
        path = tmp_path / "config.toml"
        path.write_text("")
        assert doctor.config_visibility(make_config(config_path=path)) is None

    @pytest.mark.parametrize(
        "env",
        [
            {"ISTOTA_SANDBOXED": "1"},
            # The defect this predicate exists for: a `sandbox_enabled`
            # deployment with the proxy off — warned about since ISSUE-393, and
            # shipped — puts a task in a namespace with no marker, and keying
            # on the marker would FAIL it with a remedy it cannot follow.
            {"ISTOTA_TASK_ID": "41"},
        ],
        ids=["sandbox-marker", "task-id-without-the-sandbox-marker"],
    )
    def test_inside_a_task_it_skips_and_exits_zero(self, make_config, tmp_path, monkeypatch, env):
        """Reporting the boundary working as a fault is the ISSUE-381 shape, and
        a non-zero code every time a task runs doctor is noise."""
        r = self._gate(make_config, monkeypatch, {**env, "ISTOTA_CONFIG_PATH": str(tmp_path / "absent.toml")})
        assert r.status == SKIP
        assert exit_code([r]) == 0

    @pytest.mark.parametrize("marker", ["ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"])
    def test_the_task_detail_says_the_checks_would_be_about_defaults(
        self, make_config, monkeypatch, marker
    ):
        """A SKIP's remedy is not rendered by `render_text`, so the one line an
        operator or a model sees has to carry the whole point."""
        r = self._gate(make_config, monkeypatch, {marker: "7"})
        assert "default" in r.detail.lower()
        assert exit_code([r]) == 0

    def test_a_cron_command_job_is_not_a_task_arm(
        self, make_config, tmp_path, monkeypatch
    ):
        """`PRECOMMIT_SCANS_REQUIRED` is deliberately not in this predicate: a
        cron `command` job runs unsandboxed as the daemon user with the config
        directory in front of it."""
        r = self._gate(make_config, monkeypatch, {
            "PRECOMMIT_SCANS_REQUIRED": "1", "ISTOTA_CONFIG_PATH": str(tmp_path / "absent.toml"),
        })
        assert r.status == FAIL

    def test_an_exported_path_that_does_not_resolve_fails_outside_a_task(
        self, make_config, tmp_path, monkeypatch
    ):
        """The daemon named the file and the subprocess could not read it: a
        broken install, wrong permissions, or a stale exported value."""
        r = self._gate(make_config, monkeypatch, {"ISTOTA_CONFIG_PATH": str(tmp_path / "absent.toml")})
        assert r.status == FAIL
        assert "ISTOTA_CONFIG_PATH" in r.detail
        assert str(tmp_path / "absent.toml") in r.detail
        assert r.remedy

    def test_an_explicit_c_that_does_not_resolve_fails_and_names_itself(
        self, make_config, tmp_path, monkeypatch
    ):
        """`-c` is consulted instead of the environment, so the finding names
        the argument rather than a variable the operator never set."""
        asked = tmp_path / "typo.toml"
        r = self._gate(make_config, monkeypatch, {}, requested=asked)
        assert r.status == FAIL
        assert str(asked) in r.detail
        assert "-c" in r.detail

    def test_a_whitespace_path_is_not_reported_as_nothing_named(
        self, make_config, monkeypatch
    ):
        """The arm is chosen from `source`, never from the rendered `named`: a
        whitespace path collapses to empty and would give the wrong cause."""
        r = self._gate(make_config, monkeypatch, {"ISTOTA_CONFIG_PATH": "   "})
        assert r.status == FAIL
        assert "ISTOTA_CONFIG_PATH" in r.detail
        assert "standard locations" not in r.detail

    def test_nothing_named_and_nothing_found_still_fails(
        self, make_config, monkeypatch
    ):
        """That invocation used to run 31 checks against defaults and exit 1 on
        several; a run that checked nothing must not read as one that passed
        everything, the line `verdict` draws for an empty list."""
        r = self._gate(make_config, monkeypatch, {})
        assert r.status == FAIL
        assert exit_code([r]) == 1
        assert r.remedy

    @pytest.mark.parametrize("scope,exempt", [(IMAGE, True), (DEPLOYMENT, False)])
    def test_only_image_scope_is_exempt(self, make_config, monkeypatch, scope, exempt):
        """`IMAGE` is what a bare `docker run` with no volumes can answer, which
        is exactly a host with no config on any search path."""
        env = {"ISTOTA_CONFIG_PATH": "/nonexistent/config.toml"}
        if exempt:
            env["ISTOTA_SANDBOXED"] = "1"
        r = self._gate(make_config, monkeypatch, env, scope=scope)
        if exempt:
            assert r is None
        else:
            assert r is not None and r.status == FAIL

    def test_the_named_path_cannot_forge_a_line_of_output(
        self, make_config, monkeypatch
    ):
        """The value lands on a terminal line, and both sources are writable by
        anyone who can set an environment on the machine."""
        r = self._gate(make_config, monkeypatch, {
            "ISTOTA_CONFIG_PATH": "/a.toml\n  OK   security.sandbox_effective  fine",
        })
        assert r.status == FAIL
        assert "\n" not in r.detail
        assert len(r.detail.splitlines()) == 1

    def test_a_very_long_named_path_is_capped_and_says_so(
        self, make_config, monkeypatch
    ):
        """An operator told a path did not resolve must not be shown a different
        path from the one that was tried."""
        r = self._gate(make_config, monkeypatch, {"ISTOTA_CONFIG_PATH": "/" + "x" * 5000 + ".toml"})
        assert len(r.detail) < 500
        assert "…" in r.detail

    def test_the_gate_is_deliberately_outside_the_registry(self):
        """It answers whether the run is about this host at all, so it has no
        `only=` prefix to be selected by; stated so the registry invariants'
        silence about it is not mistaken for coverage."""
        assert doctor.CONFIG_GATE not in {name for name, _ in CHECKS}
        assert doctor.CONFIG_GATE not in doctor.CHECK_SCOPES

    def test_every_arm_satisfies_the_registry_invariants(
        self, make_config, monkeypatch
    ):
        """It renders through the same renderers and is read by the same
        consumers, so `scope` is set explicitly. Also the negative control: the
        task arm and the daemon arm must be reported differently."""
        arms = (
            {"ISTOTA_TASK_ID": "1", "ISTOTA_CONFIG_PATH": "/nonexistent/c.toml"},
            {"ISTOTA_CONFIG_PATH": "/nonexistent/c.toml"},
            {},
        )
        seen = []
        for env in arms:
            with pytest.MonkeyPatch.context() as mp:
                r = self._gate(make_config, mp, env)
            seen.append(r.status)
            assert r.detail.strip()
            assert r.status in (OK, WARN, FAIL, SKIP)
            assert r.scope == DEPLOYMENT
            if r.status in (WARN, FAIL):
                assert r.remedy.strip()
        assert seen == [SKIP, FAIL, FAIL]


class TestEmailAddressUniqueness:
    """`users.email_address_uniqueness` — duplicates stored before the rule.

    The web and CLI writers refuse a *new* duplicate but let a stored one be
    resubmitted, so the only place an existing one is surfaced is here.
    """

    @staticmethod
    def _run(config):
        return run_checks(config, only=("users.email_address_uniqueness",))

    def test_ok_on_a_clean_database(self, make_config, tmp_path):
        from istota import db as db_module, user_profiles

        db_path = tmp_path / "clean.db"
        db_module.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["alice@example.com"],
        )
        [result] = self._run(make_config(db_path=db_path))
        assert result.status == OK

    def test_warns_listing_the_address_and_its_holders(self, make_config, tmp_path):
        from istota import db as db_module, user_profiles
        from istota.webui import auth as web_auth

        db_path = tmp_path / "dup.db"
        db_module.init_db(db_path)
        for user_id in ("alice", "bob", "carol"):
            user_profiles.ensure_profile(db_path, user_id)
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["shared@example.com"],
        )
        user_profiles.update_profile(
            db_path, "bob", email_addresses=["Shared@Example.com"],
        )
        web_auth.upsert_identity(db_path, "carol", "carol@example.com")
        [result] = self._run(make_config(db_path=db_path))
        assert result.status == WARN
        assert "shared@example.com" in result.detail
        assert "alice" in result.detail and "bob" in result.detail
        assert "carol" not in result.detail
        assert result.remedy

    def test_a_login_holder_is_labelled(self, make_config, tmp_path):
        from istota import db as db_module, user_profiles
        from istota.webui import auth as web_auth

        db_path = tmp_path / "login.db"
        db_module.init_db(db_path)
        for user_id in ("alice", "carol"):
            user_profiles.ensure_profile(db_path, user_id)
        user_profiles.update_profile(
            db_path, "alice", email_addresses=["x@example.com"],
        )
        web_auth.upsert_identity(db_path, "carol", "x@example.com")
        [result] = self._run(make_config(db_path=db_path))
        assert result.status == WARN
        assert "x@example.com (alice, carol (login))" in result.detail

    def test_skips_without_a_database(self, make_config, tmp_path):
        [result] = self._run(make_config(db_path=tmp_path / "absent.db"))
        assert result.status == SKIP
        assert not (tmp_path / "absent.db").exists()


class TestOperatorPersona:
    """`config.operator_persona`: the one persona, and what the sync left over.

    Every result is counts only: the detail reaches every admin, so no user id
    and no persona text may appear in it.
    """

    NAME = "config.operator_persona"
    SHIPPED = "You are {BOT_NAME}.\n\nShipped character, current version.\n"
    OLD_SHIPPED = "You are {BOT_NAME}.\n\nShipped character, an older version.\n"
    EDITED = "You are {BOT_NAME}.\n\nSECRET-EDIT-MARKER character.\n"

    @pytest.fixture(autouse=True)
    def _digests(self, monkeypatch):
        from istota.prompts import persona

        monkeypatch.setattr(
            persona,
            "SHIPPED_PERSONA_DIGESTS",
            frozenset({
                persona.persona_digest(self.SHIPPED),
                persona.persona_digest(self.OLD_SHIPPED),
            }),
        )

    def _config(self, make_config, **overrides):
        from istota.config import UserConfig

        config = make_config(
            users={uid: UserConfig() for uid in ("alice", "bob", "carol")}, **overrides
        )
        (config.skills_dir.parent / "persona.md").write_text(self.SHIPPED)
        return config

    @staticmethod
    def _user_dir(config, user_id):
        d = Path(config.workspace_path) / "Users" / user_id / config.bot_dir_name / "config"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run(self, config):
        [result] = run_checks(config, only=(self.NAME,), probe=False)
        assert result.scope == DEPLOYMENT
        return result

    def test_registered_as_deployment(self):
        assert self.NAME in {name for name, _ in CHECKS}
        assert doctor.CHECK_SCOPES[self.NAME] == DEPLOYMENT

    def test_skips_without_a_file_root(self, make_config):
        config = self._config(make_config, workspace_path=None, nextcloud_mount_path=None)
        assert not config.has_workspace
        r = self._run(config)
        assert r.status == SKIP
        assert "shipped persona is in force" in r.detail

    def test_warns_when_the_root_is_not_a_directory(self, make_config, tmp_path):
        r = self._run(self._config(make_config, workspace_path=tmp_path / "offline"))
        assert r.status == WARN
        assert r.remedy
        assert not (tmp_path / "offline").exists()

    def test_ok_follows_the_shipped_version_with_a_retired_count(self, make_config):
        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(self.SHIPPED)
        (self._user_dir(config, "alice") / "PERSONA.md.retired").write_text(self.EDITED)
        (self._user_dir(config, "bob") / "PERSONA.md.retired-20261004T000000").write_text("x")
        r = self._run(config)
        assert r.status == OK
        assert "follows the shipped persona" in r.detail
        assert "2 retired" in r.detail
        for uid in ("alice", "bob", "carol"):
            assert uid not in r.detail

    def test_edited_and_older_shipped_are_told_apart(self, make_config):
        config = self._config(make_config)
        operator = Path(config.workspace_path) / "PERSONA.md"
        operator.write_text(self.EDITED)
        r = self._run(config)
        assert r.status == OK
        assert "edited" in r.detail
        assert "SECRET-EDIT-MARKER" not in r.detail
        operator.write_text(self.OLD_SHIPPED)
        assert "older shipped version" in self._run(config).detail

    def test_an_empty_operator_file_is_the_shipped_persona(self, make_config):
        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text("  \n")
        r = self._run(config)
        assert r.status == OK
        assert "operator file empty; shipped persona in force" in r.detail

    def test_warns_on_a_shipped_file_beside_an_edit(self, make_config):
        config = self._config(make_config)
        root = Path(config.workspace_path)
        (root / "PERSONA.md").write_text(self.EDITED)
        (root / "PERSONA.md.shipped").write_text(self.SHIPPED)
        r = self._run(config)
        assert r.status == WARN
        assert "shipped persona changed since your edit" in r.detail
        assert "PERSONA.md.shipped" in r.remedy

    def test_warns_on_remaining_per_user_copies_by_count(self, make_config):
        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(self.SHIPPED)
        (self._user_dir(config, "alice") / "PERSONA.md").write_text(self.EDITED)
        (self._user_dir(config, "carol") / "PERSONA.md").write_text(self.SHIPPED)
        r = self._run(config)
        assert r.status == WARN
        assert "2 user(s) still have" in r.detail
        assert "istota init" in r.remedy
        for uid in ("alice", "bob", "carol"):
            assert uid not in r.detail
            assert uid not in r.remedy
        assert "SECRET-EDIT-MARKER" not in r.detail

    def test_an_unconfigured_users_copy_is_not_counted(self, make_config):
        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(self.SHIPPED)
        (self._user_dir(config, "mallory") / "PERSONA.md").write_text(self.EDITED)
        assert self._run(config).status == OK

    def test_warns_on_a_symlinked_operator_file(self, make_config, tmp_path):
        config = self._config(make_config)
        target = tmp_path / "elsewhere.md"
        target.write_text(self.EDITED)
        (Path(config.workspace_path) / "PERSONA.md").symlink_to(target)
        r = self._run(config)
        assert r.status == WARN
        assert "refused" in r.detail
        assert "last good copy" in r.detail

    def test_warns_on_an_over_cap_operator_file(self, make_config):
        from istota.prompts import persona

        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(
            "x" * (persona.PERSONA_MAX_BYTES + 1)
        )
        r = self._run(config)
        assert r.status == WARN
        assert "refused" in r.detail
        assert f"{persona.PERSONA_MAX_BYTES + 1} bytes" in r.detail

    def test_a_fifo_does_not_block_the_check(self, make_config):
        """And a per-user FIFO is not a copy `init` can clear: it refuses one,
        so telling the operator to run it would never go quiet."""
        config = self._config(make_config)
        os.mkfifo(Path(config.workspace_path) / "PERSONA.md")
        os.mkfifo(self._user_dir(config, "alice") / "PERSONA.md")
        r = self._run(config)
        assert r.status == WARN
        assert "refused" in r.detail
        assert "still have" not in r.detail
        assert "1 user(s) have a" in r.detail
        assert "not a regular file" in r.detail
        assert "by hand" in r.remedy

    def test_non_regular_user_copies_agree_with_what_init_refuses(self, make_config, tmp_path):
        from istota.maintenance import persona_retire

        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(self.SHIPPED)
        target = tmp_path / "planted.md"
        target.write_text(self.EDITED)
        (self._user_dir(config, "alice") / "PERSONA.md").symlink_to(target)
        (self._user_dir(config, "bob") / "PERSONA.md").mkdir()
        (self._user_dir(config, "carol") / "PERSONA.md").write_text(self.EDITED)
        census = persona_retire.census_user_personas(config)
        assert (census.remaining, census.irregular) == (1, 2)
        outcomes = {o.user_id: o.action for o in persona_retire.retire_user_personas(config, dry_run=True)}
        assert outcomes == {"alice": "refused", "bob": "refused", "carol": "retired"}
        r = self._run(config)
        assert "1 user(s) still have" in r.detail
        assert "2 user(s) have a" in r.detail

    def test_an_unreadable_shipped_file_with_nothing_else_leaves_no_persona(self, make_config):
        config = self._config(make_config)
        (config.skills_dir.parent / "persona.md").unlink()
        r = self._run(config)
        assert r.status == WARN
        assert "sync refuses" in r.detail
        assert "no persona is in force" in r.detail
        assert "config/persona.md" in r.remedy

    def test_an_unreadable_shipped_file_with_a_last_good_copy(self, make_config):
        import json

        from istota import db
        from istota.prompts import persona

        config = self._config(make_config)
        (config.skills_dir.parent / "persona.md").unlink()
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.shared_kv_set(
                conn, persona.KV_NAMESPACE, persona.KV_KEY,
                json.dumps({"last_good_text": self.EDITED}), "test",
            )
        r = self._run(config)
        assert r.status == WARN
        assert "the last good copy is in force" in r.detail
        assert "no persona" not in r.detail

    def test_an_unreadable_shipped_file_beside_an_edit(self, make_config):
        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(self.EDITED)
        (config.skills_dir.parent / "persona.md").unlink()
        r = self._run(config)
        assert r.status == WARN
        assert "sync refuses" in r.detail
        assert "edited and kept" in r.detail

    def test_no_operator_file_names_the_fallback(self, make_config):
        r = self._run(self._config(make_config))
        assert r.status == OK
        assert "the last good copy, or else the shipped persona, is in force" in r.detail
        assert "until the next sync" in r.detail

    def test_a_stale_shipped_file_beside_a_shipped_copy_is_not_an_edit(self, make_config):
        config = self._config(make_config)
        root = Path(config.workspace_path)
        (root / "PERSONA.md").write_text(self.SHIPPED)
        (root / "PERSONA.md.shipped").write_text(self.OLD_SHIPPED)
        r = self._run(config)
        assert r.status == OK
        assert "since your edit" not in r.detail
        assert "stale and the next sync removes it" in r.detail

    def test_a_shipped_file_beside_an_empty_operator_file_is_left_over(self, make_config):
        config = self._config(make_config)
        root = Path(config.workspace_path)
        (root / "PERSONA.md").write_text("")
        (root / "PERSONA.md.shipped").write_text(self.SHIPPED)
        r = self._run(config)
        assert r.status == WARN
        assert "since your edit" not in r.detail
        assert "Delete PERSONA.md.shipped" in r.remedy

    @pytest.mark.parametrize("target", ["operator_persona_path", "census"])
    def test_a_raise_inside_is_a_warning_not_a_failure(self, make_config, monkeypatch, target):
        from istota.maintenance import persona_retire
        from istota.prompts import persona

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        if target == "census":
            monkeypatch.setattr(persona_retire, "census_user_personas", _boom)
        else:
            monkeypatch.setattr(persona, "operator_persona_path", _boom)
        r = self._run(self._config(make_config))
        assert r.status == WARN
        assert r.remedy

    def test_the_census_does_not_raise_on_a_broken_loader_import(self, make_config, monkeypatch):
        import builtins

        from istota.maintenance import persona_retire

        real_import = builtins.__import__

        def _import(name, *args, **kwargs):
            if name == "istota.skills._loader":
                raise ImportError("no loader")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _import)
        census = persona_retire.census_user_personas(self._config(make_config))
        assert census == persona_retire.PersonaCensus()

    def test_a_symlinked_user_config_folder_is_counted_not_followed(self, make_config, tmp_path):
        config = self._config(make_config)
        (Path(config.workspace_path) / "PERSONA.md").write_text(self.SHIPPED)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "PERSONA.md").write_text(self.EDITED)
        bot_dir = Path(config.workspace_path) / "Users" / "bob" / config.bot_dir_name
        bot_dir.mkdir(parents=True)
        (bot_dir / "config").symlink_to(outside)
        r = self._run(config)
        assert r.status == OK
        assert "1 user config folder(s) could not be opened" in r.detail
        assert "`istota init` refuses those users" in r.detail

    def test_nothing_is_written(self, make_config):
        config = self._config(make_config)
        root = Path(config.workspace_path)
        (self._user_dir(config, "alice") / "PERSONA.md").write_text(self.EDITED)
        before = sorted(str(p) for p in root.rglob("*"))
        self._run(config)
        assert sorted(str(p) for p in root.rglob("*")) == before
        assert not (root / "PERSONA.md").exists()


class TestWallet:
    def _config(self, make_config, db_path):
        config = make_config(db_path=db_path)
        return config

    def _run(self, config, probe=False):
        return _by_name(run_checks(config, only=("security.wallet",), probe=probe))

    def test_checks_run_by_default(self, make_config, db_path):
        config = self._config(make_config, db_path)
        assert config.experimental.features == []
        results = self._run(config)
        assert set(results) == {"security.wallet.isolation", "security.wallet.cards", "security.wallet.browser"}

    @pytest.mark.parametrize("effective,opt_in,status", [
        (False, False, FAIL), (True, False, OK),
        (False, True, WARN), (None, False, WARN),
    ])
    def test_isolation_without_any_vault(self, make_config, db_path, monkeypatch,
                                         effective, opt_in, status):
        from istota.config import UserConfig

        config = self._config(make_config, db_path)
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        config.security.allow_unsandboxed_multi_user_vaults = opt_in
        monkeypatch.setattr(doctor, "_deployment_sandboxing", lambda c, p: (effective, "not probed"))
        assert self._run(config)["security.wallet.isolation"].status == status

    @pytest.mark.parametrize("damage,count", [("none", 0), ("key", 1), ("missing", 1), ("ciphertext", 1)])
    def test_cards_read_only_counts_without_values(self, make_config, db_path, monkeypatch, caplog,
                                                   damage, count):
        from istota import db
        from istota.wallet import cards
        from .support.wallet import NUMBER, card_input

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        with db.get_db(db_path) as conn:
            cards.add_card(conn, "alice", card_input(label="Private card label"))
            if damage == "missing":
                conn.execute("DELETE FROM secrets WHERE key LIKE '%:cvc'")
            if damage == "ciphertext":
                conn.execute("UPDATE secrets SET encrypted_value=?", (b"broken",))
            before = [tuple(row) for row in conn.execute("SELECT * FROM secrets")]
        if damage == "key":
            monkeypatch.setenv("ISTOTA_SECRET_KEY", "cafebabe" * 8)
        result = self._run(self._config(make_config, db_path))["security.wallet.cards"]
        assert result.status == (WARN if count else OK)
        assert f"{count} of 1 card(s)" in result.detail
        rendered = repr(result) + caplog.text
        assert NUMBER not in rendered and "Private card label" not in rendered and "alice" not in rendered
        with db.get_db(db_path) as conn:
            assert [tuple(row) for row in conn.execute("SELECT * FROM secrets")] == before

    @pytest.mark.parametrize("health,status", [
        ({"card_fill": True}, OK), ({"card_fill": False}, WARN), ({}, WARN),
        ({"card_fill": "true"}, WARN), ([], WARN),
    ])
    def test_browser_health(self, make_config, db_path, monkeypatch, health, status):
        import httpx

        config = self._config(make_config, db_path)
        config.browser.enabled = True
        config.browser.api_url = "http://browser.example:9223/"
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json=health)

        monkeypatch.setattr(httpx, "get", get)
        assert self._run(config, probe=True)["security.wallet.browser"].status == status
        assert calls == [("http://browser.example:9223/health", {"timeout": 5.0})]

    def test_probe_false_makes_no_network_or_process_call(self, make_config, db_path, monkeypatch):
        import httpx

        config = self._config(make_config, db_path)
        config.browser.enabled = True
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: pytest.fail("network call"))
        spawns = _spawn_spy(monkeypatch)
        result = self._run(config)["security.wallet.browser"]
        assert result.status == SKIP and not spawns

    def test_browser_failure_does_not_echo_response_or_exception(self, make_config, db_path, monkeypatch, caplog):
        import httpx

        config = self._config(make_config, db_path)
        config.browser.enabled = True

        def get(*args, **kwargs):
            raise httpx.ConnectError("private response sentinel")

        monkeypatch.setattr(httpx, "get", get)
        result = self._run(config, probe=True)["security.wallet.browser"]
        assert result.status == WARN
        assert "private response sentinel" not in repr(result) + caplog.text

    def test_missing_database_is_not_created(self, make_config):
        config = make_config()
        config.db_path = config.db_path.with_name("missing-wallet.db")
        results = self._run(config)
        assert results["security.wallet.cards"].status == WARN
        assert results["security.wallet.browser"].status == WARN
        assert not config.db_path.exists()


    def test_standalone_key_absent_from_cli_is_unchecked_not_damaged(self, make_config, db_path, monkeypatch):
        from istota import db
        from istota.wallet import cards
        from .support.wallet import card_input

        key = "deadbeef" * 8
        monkeypatch.setenv("ISTOTA_SECRET_KEY", key)
        with db.get_db(db_path) as conn:
            cards.add_card(conn, "alice", card_input())
        config = self._config(make_config, db_path)
        config.config_path = db_path.parent / "config.toml"
        config.config_path.with_name("istota.env").write_text(f"ISTOTA_SECRET_KEY={key}\n")
        monkeypatch.delenv("ISTOTA_SECRET_KEY")
        result = self._run(config)["security.wallet.cards"]
        assert result.status == WARN
        assert "not checked" in result.detail
        assert "undecryptable" not in result.detail and "re-add" not in result.remedy
        assert key not in repr(result)
