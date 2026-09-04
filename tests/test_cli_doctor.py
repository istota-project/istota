"""Tests for the ``istota doctor`` CLI subcommand.

The handler is thin by design — parse, run the registry, render, exit — so
these drive it with ``run_checks`` patched. What matters here is that the flags
reach ``run_checks`` unchanged (a ``--only`` that silently did nothing would
make every layer above doctor assert over the wrong set) and that the exit code
is the one a script can branch on.
"""

from __future__ import annotations

import json

import pytest

from istota import cli
from istota.doctor import DEPLOYMENT, FAIL, IMAGE, OK, SKIP, WARN, CheckResult


class _Args:
    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "verbose": False,
            "json": False,
            "deep": False,
            "only": None,
            "scope": None,
        }
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)



def _loaded(make_config, tmp_path):
    """A config that came from a file.

    Every test below drives `cmd_doctor` with `load_config` patched, and the
    gate added for ISSUE-412 reads `config.config_path is None` as "this run
    cannot see the deployment's config" and renders one line instead of the
    registry. A bare `make_config()` is that state, so these would all assert
    against the gate rather than against the plumbing they are about.
    """
    path = tmp_path / "config.toml"
    path.write_text("")
    return make_config(config_path=path)

@pytest.fixture
def patched(monkeypatch, make_config, tmp_path):
    """Capture the kwargs `cmd_doctor` passes to `run_checks`."""
    calls = {}
    results = [CheckResult("runtime.platform", OK, "Linux x86_64", scope=IMAGE)]

    def _run_checks(config, **kwargs):
        calls.update(kwargs)
        calls["config"] = config
        return results

    monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
    monkeypatch.setattr("istota.doctor.run_checks", _run_checks)
    return calls, results


class TestArgumentPlumbing:
    def test_only_is_repeatable_and_arrives_as_a_tuple(self, patched, capsys):
        calls, _ = patched
        cli.cmd_doctor(_Args(only=["developer.", "runtime.bwrap"]))
        assert calls["only"] == ("developer.", "runtime.bwrap")

    def test_no_only_means_everything(self, patched, capsys):
        calls, _ = patched
        cli.cmd_doctor(_Args())
        assert calls["only"] == ()

    def test_scope_is_forwarded(self, patched, capsys):
        calls, _ = patched
        cli.cmd_doctor(_Args(scope="image"))
        assert calls["scope"] == "image"

    def test_no_scope_means_unfiltered(self, patched, capsys):
        calls, _ = patched
        cli.cmd_doctor(_Args())
        assert calls["scope"] == ""

    def test_deep_is_forwarded(self, patched, capsys):
        calls, _ = patched
        cli.cmd_doctor(_Args(deep=True))
        assert calls["deep"] is True

    def test_the_cli_always_probes(self, patched, capsys):
        """`probe=False` exists for the config-load path. An operator running
        `istota doctor` by hand wants the binaries actually executed."""
        calls, _ = patched
        cli.cmd_doctor(_Args())
        assert calls["probe"] is True


class TestExitCode:
    def test_clean_run_exits_zero(self, monkeypatch, make_config, tmp_path, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", OK, "fine")],
        )
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_a_warning_is_not_a_failure(self, monkeypatch, make_config, tmp_path, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", WARN, "iffy", remedy="look")],
        )
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_a_skip_is_not_a_failure(self, monkeypatch, make_config, tmp_path, capsys):
        """A skill that is not wired is not a broken deployment."""
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", SKIP, "not applicable")],
        )
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_a_failure_exits_one(self, monkeypatch, make_config, tmp_path, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", FAIL, "broken", remedy="fix")],
        )
        assert cli.cmd_doctor(_Args()) == 1


class TestOutput:
    def test_json_output_is_parseable(self, monkeypatch, make_config, tmp_path, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", OK, "fine", scope=IMAGE)],
        )
        cli.cmd_doctor(_Args(json=True))
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == [
            {"name": "a.b", "status": OK, "detail": "fine", "remedy": "", "scope": IMAGE}
        ]

    def test_json_is_valid_even_when_checks_failed(self, monkeypatch, make_config, tmp_path, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", FAIL, "broken", remedy="fix")],
        )
        cli.cmd_doctor(_Args(json=True))
        assert json.loads(capsys.readouterr().out)[0]["status"] == FAIL

    def test_text_output_carries_the_remedy(self, monkeypatch, make_config, tmp_path, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: _loaded(make_config, tmp_path))
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [
                CheckResult("developer.forge_binaries.gh", FAIL, "missing", remedy="install gh")
            ],
        )
        cli.cmd_doctor(_Args())
        out = capsys.readouterr().out
        assert "developer.forge_binaries.gh" in out
        assert "install gh" in out

    def test_output_redacts_a_configured_credential(self, monkeypatch, make_config, tmp_path, capsys):
        """`detail` carries observed paths and raw exception text. Check authors
        are told not to put a credential there; the renderer does not rely on it."""
        from istota.config import DeveloperConfig

        secret = "NOT-A-REAL-TOKEN-" + "s" * 12
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        config = make_config(
            config_path=config_file,
            developer=DeveloperConfig(enabled=True, repos_dir="/tmp", gitlab_token=secret),
        )
        monkeypatch.setattr(cli, "load_config", lambda path=None: config)
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda cfg, **kw: [CheckResult("a.b", FAIL, f"rejected {secret}", remedy="rotate")],
        )
        cli.cmd_doctor(_Args())
        assert secret not in capsys.readouterr().out


class TestParser:
    def _parse(self, argv, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "argv", ["istota", *argv])
        captured = {}

        def _cmd_doctor(args):
            captured["args"] = args
            return 0

        monkeypatch.setattr(cli, "cmd_doctor", _cmd_doctor)
        monkeypatch.setattr(cli, "load_config", lambda path=None: None)
        monkeypatch.setattr(cli, "setup_logging", lambda *a, **kw: None)
        cli.main()
        return captured["args"]

    def test_bare_doctor_parses(self, monkeypatch):
        args = self._parse(["doctor"], monkeypatch)
        assert args.command == "doctor"
        assert args.json is False
        assert args.deep is False

    def test_only_accumulates(self, monkeypatch):
        args = self._parse(
            ["doctor", "--only", "developer.", "--only", "runtime.bwrap"], monkeypatch
        )
        assert args.only == ["developer.", "runtime.bwrap"]

    def test_scope_is_constrained(self, monkeypatch):
        args = self._parse(["doctor", "--scope", "image"], monkeypatch)
        assert args.scope == "image"

    def test_an_unknown_scope_is_rejected(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._parse(["doctor", "--scope", "nonsense"], monkeypatch)

    def test_json_and_deep_flags(self, monkeypatch):
        args = self._parse(["doctor", "--json", "--deep"], monkeypatch)
        assert args.json is True
        assert args.deep is True

    def test_the_parser_builds_without_installed_package_metadata(self, monkeypatch):
        """`--version` is built while the parser is, so its failure took everything.

        `importlib.metadata.version` raises when istota is importable but not
        installed — the Linux tier's image, which reaches the source through a
        path entry in its venv and so carries no distribution metadata for the
        package. That raise happened during `add_argument`, so no subcommand
        could parse at all, not even `--help`.
        """
        import importlib.metadata

        def _absent(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _absent)

        assert cli._installed_version() == "unknown (not installed)"
        assert self._parse(["doctor"], monkeypatch).command == "doctor"


class TestTheConfigGate:
    """`istota doctor` renders one line, and nothing else, when this run cannot
    see the deployment's config (ISSUE-412).

    Short-circuiting rather than pushing three-state discipline into all 31
    checks: a run with no config is about a default `Config`, and there is no
    subset of it worth reading. A single honest line among 31 fictional ones
    does not stop a reader taking `OK runtime.framework_db` as a statement about
    the deployment.
    """

    def _no_config(self, monkeypatch, make_config):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())

    def test_a_task_run_renders_one_line_and_runs_no_checks(
        self, monkeypatch, make_config, capsys
    ):
        ran = []

        def _explodes(config, **kwargs):
            ran.append(1)
            raise AssertionError("the registry must not run behind the gate")

        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr("istota.doctor.run_checks", _explodes)
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/nonexistent/config.toml")

        assert cli.cmd_doctor(_Args()) in (0, None)
        assert ran == []
        out = capsys.readouterr().out
        assert "config.loaded" in out
        assert out.strip().count("\n") == 1  # the group header and the one line

    def test_the_task_arm_does_not_exit_one(self, monkeypatch, make_config, capsys):
        """`config/` is bound into no sandbox by design, so a task's own doctor
        run must not report the boundary as a broken deployment.

        `run_checks` is patched to the pre-change answer — a FAIL about a
        default `Config`, which is what all 31 checks produced against
        `data/istota.db` — so removing the gate turns this red. Patching it to
        `[]` would not: `exit_code([])` is 0 and the assertion would hold with
        the gate gone, which is the no-op probe `.claude/rules/testbed.md`
        catalogues.
        """
        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda c, **kw: [
                CheckResult("runtime.framework_db", FAIL, "no such table", remedy="init")
            ],
        )
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_an_unresolvable_exported_path_exits_one(
        self, monkeypatch, make_config, capsys
    ):
        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [])
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/nonexistent/config.toml")
        assert cli.cmd_doctor(_Args()) == 1

    def test_a_run_from_the_wrong_directory_exits_one(
        self, monkeypatch, make_config, capsys
    ):
        """That invocation used to run 31 checks against defaults and exit 1 on
        the several that fail. A run that answered nothing must not read as a
        run that passed everything."""
        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [])
        for name in ("ISTOTA_SANDBOXED", "ISTOTA_TASK_ID", "ISTOTA_CONFIG_PATH"):
            monkeypatch.delenv(name, raising=False)
        assert cli.cmd_doctor(_Args()) == 1

    def test_only_does_not_open_the_gate(self, monkeypatch, make_config, capsys):
        """`--only` selects by name across both scopes and carries no
        declaration that the run is config-independent."""

        def _explodes(config, **kwargs):
            raise AssertionError("the registry must not run behind the gate")

        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr("istota.doctor.run_checks", _explodes)
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        cli.cmd_doctor(_Args(only=["runtime."]))
        assert "config.loaded" in capsys.readouterr().out

    def test_image_scope_opens_the_gate_and_runs_the_registry(
        self, monkeypatch, make_config, capsys
    ):
        """`IMAGE` is defined as what a bare `docker run` with no volumes can
        answer, which is exactly a host with no config on any search path. The
        image tier's whole oracle is `doctor --json --scope image`."""
        seen = {}

        def _run_checks(config, **kwargs):
            seen.update(kwargs)
            return [CheckResult("runtime.bwrap", OK, "0.11.0", scope=IMAGE)]

        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr("istota.doctor.run_checks", _run_checks)
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.setenv("ISTOTA_CONFIG_PATH", "/nonexistent/config.toml")

        assert cli.cmd_doctor(_Args(scope="image")) in (0, None)
        assert seen["scope"] == "image"
        out = capsys.readouterr().out
        assert "runtime.bwrap" in out
        assert "config.loaded" not in out

    def test_the_json_shape_is_unchanged(self, monkeypatch, make_config, capsys):
        """A machine consumer gets the same list of result objects it always
        did, with one element in it."""
        self._no_config(monkeypatch, make_config)
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [])
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        cli.cmd_doctor(_Args(json=True))
        parsed = json.loads(capsys.readouterr().out)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "config.loaded"
        assert parsed[0]["status"] == SKIP
        assert parsed[0]["scope"] == DEPLOYMENT

    def test_a_loaded_config_runs_the_registry(
        self, monkeypatch, make_config, tmp_path, capsys
    ):
        """The negative control: the gate is closed by the config, not by the
        environment, so a real run is untouched inside a sandbox."""
        monkeypatch.setattr(
            cli, "load_config", lambda path=None: _loaded(make_config, tmp_path)
        )
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda c, **kw: [CheckResult("a.b", OK, "fine")],
        )
        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        cli.cmd_doctor(_Args())
        assert "a.b" in capsys.readouterr().out
