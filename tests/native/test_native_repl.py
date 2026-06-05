"""Smoke test for the Tier-2 standalone loop runner (scripts/native_repl.py).

Loads the script module by path (it lives in scripts/, not a package) and runs
main() end-to-end against the mock provider — exercising arg parsing, config
build, provider selection, the NativeBrain adapter, and real file tools in a
throwaway temp dir. No network, no credits.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "native_repl.py"


def _load_repl():
    spec = importlib.util.spec_from_file_location("native_repl", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mock_provider_runs_tools_and_completes(monkeypatch, capsys):
    repl = _load_repl()
    script = _SCRIPT.parent.parent / "tests" / "native" / "fixtures" / "two_tool_turn.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "native_repl.py",
            "--provider", "mock",
            "--script", str(script),
            "--show-events",
            "write and read a file",
        ],
    )
    rc = repl.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "hi from native repl" in out
    assert "stop_reason" in out and "completed" in out
    # Both tool turns surfaced in the trace.
    assert "Writing hello.txt" in out
    assert "Reading hello.txt" in out


def test_mock_requires_script(monkeypatch):
    repl = _load_repl()
    monkeypatch.setattr("sys.argv", ["native_repl.py", "--provider", "mock", "hi"])
    with pytest.raises(SystemExit):
        repl.main()
