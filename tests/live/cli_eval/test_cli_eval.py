"""The CLI-brain tool eval: the CLI's own tools against tools served over MCP.

Live, costs money (or subscription quota), and never a merge gate. Run with
``-n0``:

    ISTOTA_EVAL_ENV_FILE=~/.config/istota/eval.env \\
    ISTOTA_CLI_EVAL_CLAUDE=/path/to/claude \\
    ISTOTA_CLI_EVAL_SPIKE_SERVER=/path/to/spike_mcp_server.py \\
    uv run pytest tests/live/cli_eval -m live -n0

Settings, from the environment or the env file:

- ``CLAUDE_CODE_OAUTH_TOKEN`` (subscription) or ``ANTHROPIC_API_KEY``. First
  party only: an ``ANTHROPIC_BASE_URL`` naming another host fails the session,
  and ``ANTHROPIC_AUTH_TOKEN`` is never forwarded.
- ``ISTOTA_CLI_EVAL_CLAUDE``: the ``claude`` binary, default the one on PATH.
  Its version must equal the image's ``CLAUDE_CODE_VERSION`` pin.
- ``ISTOTA_CLI_EVAL_SPIKE_SERVER``: the Stage 2 spike server; the spike arm
  skips without it.
- ``ISTOTA_CLI_EVAL_ARMS`` (default ``builtin,spike``; ``spike_cwd`` is the
  diagnostic arm the runner describes), ``ISTOTA_CLI_EVAL_CASES``
  (default all), ``ISTOTA_EVAL_RUNS`` (default 1), ``ISTOTA_EVAL_OUT`` (the JSON
  report; stream files go in ``<stem>-streams/`` beside it),
  ``ISTOTA_EVAL_BUDGET_USD`` (default 15, against the CLI's own
  ``total_cost_usd``, which on a subscription is notional).

The model is the claude_code brain's ``general`` tier, resolved by the brain's
own resolver. ``test_init_frame`` costs nothing: it runs each arm with a bogus
key and reads ``system/init``, which the CLI emits before its first request.
"""

from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path

import pytest

from ..native_eval import runner as native_runner
from . import runner
from .cases import CASES, by_name

pytestmark = pytest.mark.live

DEFAULT_BUDGET_USD = 15.0


def _read_env() -> tuple[dict[str, str], str]:
    # Module scope: the suite's autouse scrub removes credential names before
    # a test body runs.
    env = dict(os.environ)
    env_file = env.get("ISTOTA_EVAL_ENV_FILE")
    if not env_file:
        return env, ""
    try:
        env.update(native_runner.load_env_file(Path(env_file).expanduser()))
    except OSError as exc:
        return env, f"ISTOTA_EVAL_ENV_FILE could not be read: {type(exc).__name__}"
    return env, ""


_ENV, _ENV_ERROR = _read_env()


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(_ENV.get(name, "") or default))
    except ValueError:
        return default


RUNS = _int("ISTOTA_EVAL_RUNS", 1)
ARMS = [a for a in (_ENV.get("ISTOTA_CLI_EVAL_ARMS") or ",".join(runner.ARMS)).split(",") if a]
CASE_NAMES = [
    c for c in (_ENV.get("ISTOTA_CLI_EVAL_CASES") or ",".join(c.name for c in CASES)).split(",") if c
]


def _unavailable(reason: str) -> None:
    if _ENV.get("ISTOTA_LIVE_TIER") == "1":
        pytest.fail(f"ISTOTA_LIVE_TIER=1 says this host can run the live tier: {reason}")
    pytest.skip(reason)


def _claude() -> str:
    claude = _ENV.get("ISTOTA_CLI_EVAL_CLAUDE") or shutil.which("claude") or ""
    if not claude or not Path(claude).exists():
        _unavailable("no `claude` binary (set ISTOTA_CLI_EVAL_CLAUDE)")
    return claude


@pytest.fixture(scope="session")
def cli_settings(tmp_path_factory) -> runner.Settings:
    if _ENV_ERROR:
        pytest.fail(_ENV_ERROR)
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("the eval writes one report; run it with -n0")
    claude = _claude()
    pinned = runner.pinned_cli_version()
    probe_dirs = runner.scratch_dirs(tmp_path_factory.mktemp("version"))
    found = runner.cli_version(claude, runner.cli_env(os.environ.get("PATH", ""), probe_dirs, {}, 60))
    if found != pinned:
        pytest.fail(f"{claude} is {found or 'unknown'}, the image pins {pinned}")
    model, effort = runner.general_model()
    server = _ENV.get("ISTOTA_CLI_EVAL_SPIKE_SERVER")
    return runner.Settings(
        claude=claude, model=model, effort=effort, auth={},
        spike_server=Path(server).expanduser() if server else None,
    )


@pytest.fixture(scope="session")
def auth(cli_settings) -> tuple[str, dict[str, str]]:
    try:
        return runner.anthropic_auth(_ENV)
    except runner.AuthError as exc:
        if "BASE_URL" in str(exc):
            pytest.fail(str(exc))
        _unavailable(str(exc))


@pytest.fixture(scope="session")
def eval_report(cli_settings, auth, tmp_path_factory):
    out = _ENV.get("ISTOTA_EVAL_OUT")
    path = Path(out).expanduser() if out else tmp_path_factory.mktemp("cli-eval") / "report.json"
    try:
        budget = float(_ENV.get("ISTOTA_EVAL_BUDGET_USD") or DEFAULT_BUDGET_USD)
    except ValueError:
        budget = DEFAULT_BUDGET_USD
    report = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cli_version": runner.pinned_cli_version(),
        "model": cli_settings.model,
        "effort": cli_settings.effort,
        "auth_mode": auth[0],
        "arms": ARMS,
        "runs": RUNS,
        "budget_usd": budget,
        "total_cost_usd": 0.0,
        "results": [],
    }
    native_runner.write_report(path, report)
    return report, path, budget


def _skip_unless_spike_ready(arm: str, settings: runner.Settings) -> None:
    if arm in runner.SPIKE_ARMS and (settings.spike_server is None or not settings.spike_server.is_file()):
        _unavailable("ISTOTA_CLI_EVAL_SPIKE_SERVER does not name the spike server")


@pytest.mark.parametrize("arm", ARMS)
def test_init_frame(arm, cli_settings, tmp_path):
    """No model call: the arm's tool set and MCP server, as `system/init` lists them."""
    _skip_unless_spike_ready(arm, cli_settings)
    smoke = runner.init_smoke(arm, tmp_path, cli_settings)
    assert smoke["init_tools"] is not None, f"no system/init frame: {smoke}"
    assert smoke["tool_calls"] == 0
    if arm in runner.SPIKE_ARMS:
        assert set(smoke["init_tools"]) == runner.expected_spike_tools()
        assert smoke["init_mcp_servers"] == [
            {"name": runner.SPIKE_SERVER_KEY, "status": "connected"}
        ]
    else:
        assert "Bash" in smoke["init_tools"]
        assert not any(t.startswith("mcp__") for t in smoke["init_tools"])


@pytest.mark.parametrize(
    "case_name,run,arm",
    [(c, r, a) for c in CASE_NAMES for r in range(1, RUNS + 1) for a in ARMS],
    ids=[f"{a}-{c}-{r}" for c in CASE_NAMES for r in range(1, RUNS + 1) for a in ARMS],
)
def test_case(case_name, run, arm, cli_settings, auth, eval_report, tmp_path):
    _skip_unless_spike_ready(arm, cli_settings)
    report, path, budget = eval_report
    allowed, reason = native_runner.budget_allows(report, budget)
    if not allowed:
        pytest.skip(reason)
    settings = runner.Settings(
        claude=cli_settings.claude, model=cli_settings.model, effort=cli_settings.effort,
        auth=auth[1], spike_server=cli_settings.spike_server,
    )
    keep = Path(path).with_name(f"{Path(path).stem}-streams")
    result = runner.run_case(by_name(case_name), arm, run, tmp_path, settings, keep_dir=keep)
    report["results"].append(result.to_dict())
    report["total_cost_usd"] = round(native_runner.report_cost(report), 6)
    native_runner.write_report(path, report)
    # A failed case is a metric, not a test failure. A run that never reached
    # the model is a broken arm, and says so.
    assert result.metrics.get("stop_reason"), (
        f"no result frame (rc={result.returncode}, timed_out={result.timed_out}): {result.stream}"
    )
