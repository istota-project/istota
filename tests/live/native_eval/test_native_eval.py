"""The native brain against a real endpoint, one test per case and run (`live`).

Costs money. Run by hand, serially:

    uv run pytest -m live tests/live/native_eval -n0 -q

A case passing or failing is a metric in the report, not a test result: a
model's bad day must not read as a regression in istota. The test asserts only
that the run produced a report row.

Environment, read when this module is imported (before the suite's autouse
scrub takes every `ISTOTA_*` name out of a test body):

- `ISTOTA_EVAL_ENV_FILE`: a file of `KEY=VALUE` lines, read first; its values
  win over the shell's. The way to supply the key without exporting it.
- `ISTOTA_BRAIN_NATIVE_API_KEY`: the endpoint key. Absent, every case skips
  (`ISTOTA_LIVE_TIER=1` turns that into a failure). Never printed or written.
- `ISTOTA_EVAL_BASE_URL`: default `https://openrouter.ai/api/v1`.
- `ISTOTA_EVAL_MODEL`: default `deepseek/deepseek-v4.1-flash`.
- `ISTOTA_EVAL_RUNS`: runs per case, default 1.
- `ISTOTA_EVAL_OUT`: the JSON report path; default a file in the session's
  temporary directory. Rewritten after every case, started fresh per session.
  Each run's session log is copied to `<report stem>-logs/<case>-<run>.jsonl`
  beside it, and the report names that copy.
- `ISTOTA_EVAL_CONFIG`: a TOML file of `[brain.native]` overrides (a
  `[brain.native]` table or bare top-level keys), so one case can run with a
  feature on and off.
- `ISTOTA_EVAL_BUDGET_USD`: the spend cap, default 5.0. A case is skipped once
  the report's summed `cost_usd` plus the dearest case so far would cross it.
- `ISTOTA_EVAL_RECORD_SSE`: a path; every raw SSE line of every provider call
  in the session is appended to it in the `llm/replay.py` fixture format.
"""

from __future__ import annotations

import datetime
import json
import os
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import pytest

from istota.config import NativeBrainConfig
from istota.config_mapper import apply_section

from . import runner
from .cases import CASES, by_name

pytestmark = pytest.mark.live

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BUDGET_USD = 5.0
KEY_NAME = "ISTOTA_BRAIN_NATIVE_API_KEY"


def _read_env() -> tuple[dict[str, str], str]:
    env = dict(os.environ)
    env_file = env.get("ISTOTA_EVAL_ENV_FILE")
    if not env_file:
        return env, ""
    try:
        env.update(runner.load_env_file(Path(env_file).expanduser()))
    except OSError as exc:
        # The path only; the file holds a key.
        return env, f"ISTOTA_EVAL_ENV_FILE could not be read: {type(exc).__name__}"
    return env, ""


_ENV, _ENV_ERROR = _read_env()


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(_ENV.get(name, "") or default))
    except ValueError:
        return default


RUNS = _int("ISTOTA_EVAL_RUNS", 1)


def _unavailable(reason: str) -> None:
    if _ENV.get("ISTOTA_LIVE_TIER") == "1":
        pytest.fail(f"ISTOTA_LIVE_TIER=1 says this host can run the live tier: {reason}")
    pytest.skip(reason)


def _overrides() -> dict:
    path = _ENV.get("ISTOTA_EVAL_CONFIG")
    if not path:
        return {}
    doc = tomllib.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    native = doc.get("brain", {}).get("native") if isinstance(doc.get("brain"), dict) else None
    data = native if isinstance(native, dict) else doc
    if "api_key" in data:
        pytest.fail("ISTOTA_EVAL_CONFIG must not carry api_key; use the env file")
    return data


@pytest.fixture(scope="session")
def eval_settings():
    if _ENV_ERROR:
        pytest.fail(_ENV_ERROR)
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("the eval suite writes one report; run it with -n0")
    api_key = (_ENV.get(KEY_NAME) or "").strip()
    if not api_key:
        _unavailable(f"{KEY_NAME} is not set")
    overrides = _overrides()
    config = NativeBrainConfig(
        model=_ENV.get("ISTOTA_EVAL_MODEL") or DEFAULT_MODEL,
        base_url=_ENV.get("ISTOTA_EVAL_BASE_URL") or DEFAULT_BASE_URL,
        api_key=api_key,
    )
    unknown: list[str] = []
    apply_section(config, overrides, prefix="brain.native", unknown=unknown)
    if unknown:
        pytest.fail(f"ISTOTA_EVAL_CONFIG names unknown keys: {sorted(unknown)}")
    try:
        budget = float(_ENV.get("ISTOTA_EVAL_BUDGET_USD") or DEFAULT_BUDGET_USD)
    except ValueError:
        budget = DEFAULT_BUDGET_USD
    record = _ENV.get("ISTOTA_EVAL_RECORD_SSE")
    return {
        "config": config,
        "overrides": overrides,
        "budget": budget,
        "record_sse": Path(record).expanduser() if record else None,
        "out": _ENV.get("ISTOTA_EVAL_OUT"),
    }


@pytest.fixture(scope="session")
def eval_report(eval_settings, tmp_path_factory):
    out = eval_settings["out"]
    path = Path(out).expanduser() if out else tmp_path_factory.mktemp("eval") / "report.json"
    config = eval_settings["config"]
    report = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": config.model,
        "base_url_host": urlparse(config.base_url).hostname or "",
        "overrides": eval_settings["overrides"],
        "runs": RUNS,
        "budget_usd": eval_settings["budget"],
        "total_cost_usd": 0.0,
        "results": [],
    }
    runner.write_report(path, report)
    return report, path


@pytest.mark.parametrize(
    "case_name,run",
    [(case.name, run) for case in CASES for run in range(1, RUNS + 1)],
    ids=[f"{case.name}-{run}" for case in CASES for run in range(1, RUNS + 1)],
)
def test_case(case_name, run, eval_settings, eval_report, tmp_path):
    report, path = eval_report
    allowed, reason = runner.budget_allows(report, eval_settings["budget"])
    if not allowed:
        pytest.skip(reason)

    result = runner.run_case(
        by_name(case_name),
        eval_settings["config"],
        tmp_path / "repo",
        log_root=tmp_path / "logs",
        record_sse=eval_settings["record_sse"],
        keep_log=runner.kept_log_path(path, case_name, run),
    )
    row = result.to_dict()
    row["run"] = run
    report["results"].append(row)
    report["total_cost_usd"] = runner.report_cost(report)
    runner.write_report(path, report)

    written = json.loads(path.read_text())
    assert written["results"][-1]["case"] == case_name
    print(
        f"\n{case_name} run {run}: passed={result.passed} turns={result.turns} "
        f"tool_errors={result.tool_errors} compactions={result.compactions} "
        f"cost={result.cost_usd:.5f} report={path}"
    )
