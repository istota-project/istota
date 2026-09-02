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
from istota.doctor import FAIL, IMAGE, OK, SKIP, WARN, CheckResult


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


@pytest.fixture
def patched(monkeypatch, make_config):
    """Capture the kwargs `cmd_doctor` passes to `run_checks`."""
    calls = {}
    results = [CheckResult("runtime.platform", OK, "Linux x86_64", scope=IMAGE)]

    def _run_checks(config, **kwargs):
        calls.update(kwargs)
        calls["config"] = config
        return results

    monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
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
    def test_clean_run_exits_zero(self, monkeypatch, make_config, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", OK, "fine")],
        )
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_a_warning_is_not_a_failure(self, monkeypatch, make_config, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", WARN, "iffy", remedy="look")],
        )
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_a_skip_is_not_a_failure(self, monkeypatch, make_config, capsys):
        """A skill that is not wired is not a broken deployment."""
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", SKIP, "not applicable")],
        )
        assert cli.cmd_doctor(_Args()) in (0, None)

    def test_a_failure_exits_one(self, monkeypatch, make_config, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", FAIL, "broken", remedy="fix")],
        )
        assert cli.cmd_doctor(_Args()) == 1


class TestOutput:
    def test_json_output_is_parseable(self, monkeypatch, make_config, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", OK, "fine", scope=IMAGE)],
        )
        cli.cmd_doctor(_Args(json=True))
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == [
            {"name": "a.b", "status": OK, "detail": "fine", "remedy": "", "scope": IMAGE}
        ]

    def test_json_is_valid_even_when_checks_failed(self, monkeypatch, make_config, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", FAIL, "broken", remedy="fix")],
        )
        cli.cmd_doctor(_Args(json=True))
        assert json.loads(capsys.readouterr().out)[0]["status"] == FAIL

    def test_text_output_carries_the_remedy(self, monkeypatch, make_config, capsys):
        monkeypatch.setattr(cli, "load_config", lambda path=None: make_config())
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

    def test_output_redacts_a_configured_credential(self, monkeypatch, make_config, capsys):
        """`detail` carries observed paths and raw exception text. Check authors
        are told not to put a credential there; the renderer does not rely on it."""
        from istota.config import DeveloperConfig

        secret = "NOT-A-REAL-TOKEN-" + "s" * 12
        config = make_config(
            developer=DeveloperConfig(enabled=True, repos_dir="/tmp", gitlab_token=secret)
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
