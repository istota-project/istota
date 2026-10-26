"""Run one eval case through `NativeBrain.execute` and measure it.

No executor and no scheduler: a `BrainRequest` with the six core tools, the
case's fixture copied into a fresh working directory, sandboxing off (the
unconfined native shape) and a session log enabled, so the metrics come from
the transcript as well as from `BrainResult`. The transcript is read through
`session_log_read`, the one parser for it.

A case passing or failing is a metric. Nothing here raises on a model's bad
answer; the driver asserts only that a report was produced.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from istota.brain import BrainRequest
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig, SessionLogConfig
from istota.session.session_log_read import read_records, record_text
from istota.session.tools import ToolEnv, build_default_tools
from istota.session.tools.bash import bash_result_failed
from istota.usage.telemetry import cache_hit_rate

FIXTURES = Path(__file__).parent / "fixtures"
SYSTEM_PROMPT_FILE = FIXTURES / "system.md"

EVAL_USER = "eval"

# What a check's argv and a case's prompt may name instead of a literal path.
PYTHON_PLACEHOLDER = "{python}"

METRIC_FIELDS = (
    "passed",
    "stop_reason",
    "turns",
    "tool_calls",
    "tool_errors",
    "edit_failures",
    "tool_errors_before_success",
    "compactions",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_hit_rate",
    "cost_usd",
    "wall_seconds",
)


@dataclass
class EvalCase:
    """One task, its fixture repository and how to tell it worked.

    ``check_argv`` runs in the working directory after the run (exit 0 is a
    pass); ``check_fn(workdir, result_text) -> (passed, detail)`` is the other
    form. ``success_marker`` is text whose first appearance in a tool result
    counts as the first success, for ``tool_errors_before_success``.
    """

    name: str
    fixture: str | Path
    prompt: str
    timeout: int = 300
    check_argv: list[str] | None = None
    check_fn: Callable[[Path, str], tuple[bool, str]] | None = None
    config_overrides: dict = field(default_factory=dict)
    crlf_files: tuple[str, ...] = ()
    generate: Callable[[Path], None] | None = None
    min_compactions: int = 0
    success_marker: str | None = None


@dataclass
class EvalResult:
    case: str
    passed: bool = False
    check_detail: str = ""
    success: bool = False
    stop_reason: str = ""
    result_text: str = ""
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    edit_failures: int = 0
    tool_errors_before_success: int | None = None
    compactions: int = 0
    # Prompt tokens billed at the input rate: cache reads are not in it.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_hit_rate: float = 0.0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    session_log: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def core_tool_names() -> list[str]:
    """The six tools the native brain registers, in registration order."""
    return [tool.schema.name for tool in build_default_tools(ToolEnv(cwd=Path.cwd()))]


def _fixture_dir(case: EvalCase) -> Path:
    path = Path(case.fixture)
    return path if path.is_absolute() else FIXTURES / path


def _with_python(text: str) -> str:
    return text.replace(PYTHON_PLACEHOLDER, sys.executable)


def _prompt_text(text: str) -> str:
    # The model sees a bare `python`, never sys.executable: that path names
    # this checkout, and a model handed it took the checkout for the project
    # and ran istota's own suite into the case timeout.
    return text.replace(PYTHON_PLACEHOLDER, "python")


def _shim_dir(workdir: Path) -> Path:
    return workdir.parent / "bin"


def _write_python_shim(workdir: Path) -> None:
    # A wrapper, not a symlink: a symlinked venv interpreter looks for
    # pyvenv.cfg beside the link and loses the venv's packages.
    bin_dir = _shim_dir(workdir)
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)


def _path_outside_checkout() -> str:
    root = Path(__file__).resolve().parents[3]
    kept = [
        entry
        for entry in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
        if entry and not Path(entry).resolve().is_relative_to(root)
    ]
    return os.pathsep.join(kept)


def prepare_workdir(case: EvalCase, workdir: Path) -> None:
    """Copy the fixture into ``workdir``, then apply the case's own setup.

    CRLF files are stored LF in the repository and converted here, so git's
    end-of-line handling can never quietly change what the case tests.
    """
    shutil.copytree(_fixture_dir(case), workdir)
    for name in case.crlf_files:
        path = workdir / name
        data = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(data.replace(b"\n", b"\r\n"))
    if case.generate is not None:
        case.generate(workdir)
    _write_python_shim(workdir)


def task_env(workdir: Path) -> dict[str, str]:
    """The environment the model's Bash gets: a PATH and nothing secret.

    Explicit, because an empty ``BrainRequest.env`` means "inherit the
    daemon's", and here that is the test process holding the API key.
    """
    return {
        "PATH": os.pathsep.join([str(_shim_dir(workdir)), _path_outside_checkout()]),
        "HOME": str(workdir),
        "LANG": "C.UTF-8",
        "TMPDIR": str(workdir.parent / "tmp"),
    }


def run_check(case: EvalCase, workdir: Path, result_text: str, *, env: dict) -> tuple[bool, str]:
    if case.check_fn is not None:
        try:
            return case.check_fn(workdir, result_text)
        except Exception as exc:  # noqa: BLE001 — a broken check is a failed case
            return False, f"check raised {type(exc).__name__}: {exc}"
    if case.check_argv:
        argv = [_with_python(arg) for arg in case.check_argv]
        try:
            proc = subprocess.run(
                argv, cwd=workdir, env=env, capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"check did not run: {type(exc).__name__}: {exc}"
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, f"exit {proc.returncode}: {output[-500:]}"
    return False, "case has no check"


def kept_log_path(report_path: Path, case_name: str, run: int) -> Path:
    """Where a run's transcript is kept: a directory beside the report.

    pytest's basetemp keeps three sessions, so a log left there is gone by the
    end of a multi-arm sweep while the report still names it.
    """
    report_path = Path(report_path)
    return report_path.with_name(f"{report_path.stem}-logs") / f"{case_name}-{run}.jsonl"


def _single_log(log_root: Path) -> Path | None:
    files = sorted((log_root / EVAL_USER).glob("*.jsonl"))
    return files[-1] if files else None


def metrics_from_log(path: Path, success_marker: str | None) -> dict:
    """Tool and compaction counts from one transcript."""
    out = {
        "tool_calls": 0,
        "tool_errors": 0,
        "edit_failures": 0,
        "tool_errors_before_success": None,
        "compactions": 0,
        "turns": None,
    }
    errors_so_far = 0
    for record in read_records(path):
        kind = record.get("type")
        if kind == "compaction":
            # A failed summary writes a record too, and cut nothing; so does a
            # background summary discarded as stale.
            if record.get("ok") is not False and record.get("placed") is not False:
                out["compactions"] += 1
        elif kind == "result":
            turns = record.get("turns")
            if isinstance(turns, int):
                out["turns"] = turns
        elif kind == "message":
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "tool_result":
                continue
            out["tool_calls"] += 1
            failed = bool(message.get("is_error")) or (
                message.get("tool_name") == "Bash" and bash_result_failed(record_text(record))
            )
            if failed:
                out["tool_errors"] += 1
                errors_so_far += 1
                if message.get("tool_name") == "Edit":
                    out["edit_failures"] += 1
                continue
            if (
                success_marker
                and out["tool_errors_before_success"] is None
                and success_marker in record_text(record)
            ):
                out["tool_errors_before_success"] = errors_so_far
    return out


def tee_sse(parser_owner, path: Path) -> None:
    """Append every raw SSE line ``parser_owner`` parses to ``path``.

    Wraps the instance's ``_parse_sse_lines``, which every provider call goes
    through, so all calls of a run land in one file in the ``write_fixture``
    format and replay with ``ReplayProvider``. Request headers never pass
    through here, so the API key cannot reach the file.
    """
    inner = parser_owner._parse_sse_lines
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    async def _parse(lines, model):
        async def _tee():
            async for line in lines:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line) + "\n")
                yield line

        async for event in inner(_tee(), model):
            yield event

    parser_owner._parse_sse_lines = _parse


def run_case(
    case: EvalCase,
    brain_config: NativeBrainConfig,
    workdir: Path,
    *,
    provider=None,
    log_root: Path | None = None,
    record_sse: Path | None = None,
    keep_log: Path | None = None,
) -> EvalResult:
    """Run ``case`` once in ``workdir`` (created here) and measure it.

    ``provider`` is for the offline test; left ``None`` the brain builds its
    own from ``brain_config``. ``record_sse`` tees the run's raw SSE lines to a
    file (a live provider only; tee an injected one yourself). ``keep_log`` is
    a path the session log is copied to, and the one the result names.
    """
    workdir = Path(workdir)
    log_root = Path(log_root) if log_root is not None else workdir.parent / "logs"
    control = workdir.parent / "control"
    control.mkdir(parents=True, exist_ok=True)
    (workdir.parent / "tmp").mkdir(parents=True, exist_ok=True)
    system_file = control / "system.md"
    shutil.copyfile(SYSTEM_PROMPT_FILE, system_file)
    prepare_workdir(case, workdir)

    config = dataclasses.replace(
        brain_config,
        session_log=SessionLogConfig(enabled=True, dir=str(log_root)),
        **case.config_overrides,
    )
    brain = NativeBrain(config, provider=provider)
    if record_sse is not None and provider is None:
        tee_sse(brain._provider, record_sse)

    env = task_env(workdir)
    req = BrainRequest(
        prompt=_prompt_text(case.prompt),
        allowed_tools=core_tool_names(),
        cwd=workdir.resolve(),
        env=env,
        timeout_seconds=case.timeout,
        composed_system_prompt_path=system_file.resolve(),
        task_id=1,
        attempt=1,
        user_id=EVAL_USER,
        source_type="eval",
        streaming=False,
    )

    result = EvalResult(case=case.name)
    started = time.monotonic()
    try:
        brain_result = brain.execute(req)
    except Exception as exc:  # noqa: BLE001 — the brain is not meant to raise
        result.wall_seconds = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.wall_seconds = time.monotonic() - started

    result.success = bool(brain_result.success)
    result.stop_reason = brain_result.stop_reason
    result.result_text = brain_result.result_text or ""
    usage = brain_result.usage
    if usage is not None:
        result.turns = usage.turns
        result.input_tokens = usage.billed_input_tokens
        result.output_tokens = usage.output_tokens
        result.cache_read_tokens = usage.cache_read_tokens
        result.cache_write_tokens = usage.cache_write_tokens
        result.cache_hit_rate = cache_hit_rate(usage)
        result.cost_usd = float(usage.cost_usd or 0.0)
    if not result.success:
        # The brain puts a failure's reason in result_text (an output-limit
        # stop with no answer is the marker alone); the row reads `error`.
        result.error = result.result_text.strip() or f"brain stopped with {result.stop_reason}"

    log_path = _single_log(log_root)
    if log_path is not None:
        if keep_log is not None:
            keep_log = Path(keep_log)
            keep_log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(log_path, keep_log)  # keeps the transcript's 0600
            result.session_log = str(keep_log)
        else:
            result.session_log = str(log_path)
        counts = metrics_from_log(log_path, case.success_marker)
        logged_turns = counts.pop("turns")
        if not result.turns and logged_turns:
            result.turns = logged_turns
        for key, value in counts.items():
            setattr(result, key, value)
    else:
        result.error = "; ".join(
            part for part in (result.error, "no session log was written") if part
        )

    passed, detail = run_check(case, workdir, result.result_text, env=env)
    if passed and result.compactions < case.min_compactions:
        passed = False
        detail = f"{detail}; compactions {result.compactions} < {case.min_compactions}"
    result.passed = passed
    result.check_detail = detail
    return result


# --------------------------------------------------------------------------
# The report and the spend cap
# --------------------------------------------------------------------------


def report_cost(report: dict) -> float:
    total = 0.0
    for row in report.get("results", []):
        cost = row.get("cost_usd") if isinstance(row, dict) else None
        if isinstance(cost, (int, float)):
            total += float(cost)
    return total


def budget_allows(report: dict, budget_usd: float) -> tuple[bool, str]:
    """Whether another case may start without crossing ``budget_usd``.

    A case's cost is not known before it runs, so the projection is the spend
    so far plus the dearest case so far. The first case always runs.
    """
    spent = report_cost(report)
    dearest = 0.0
    for row in report.get("results", []):
        cost = row.get("cost_usd") if isinstance(row, dict) else None
        if isinstance(cost, (int, float)):
            dearest = max(dearest, float(cost))
    projected = spent + dearest
    if projected > budget_usd:
        return False, (
            f"spend cap: {spent:.4f} USD spent, next case projected to "
            f"{projected:.4f} USD against a cap of {budget_usd:.2f}"
        )
    return True, ""


def write_report(path: Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines; blank lines, comments and other lines are skipped.

    An ``export`` prefix and one layer of matching quotes are tolerated, so a
    file written for a shell works too.
    """
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            out[key] = value
    return out
