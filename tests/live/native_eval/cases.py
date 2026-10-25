"""The five eval cases. Each fixture is under `fixtures/<name>/`.

A check reads the working directory after the run and never trusts what the
model says it did, except where the answer itself is the deliverable.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from .runner import FIXTURES, EvalCase


def _unchanged(workdir: Path, case_name: str, relative: str) -> bool:
    original = (FIXTURES / case_name / relative).read_bytes()
    return (workdir / relative).read_bytes() == original


def _python(workdir: Path, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- 1. fix_failing_test ---------------------------------------------------


def _check_fix_failing_test(workdir: Path, result_text: str) -> tuple[bool, str]:
    if not _unchanged(workdir, "fix_failing_test", "tests/test_pricing.py"):
        return False, "the test file was changed"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    tail = (proc.stdout + proc.stderr).strip()[-300:]
    return proc.returncode == 0, f"pytest exit {proc.returncode}: {tail}"


FIX_FAILING_TEST = EvalCase(
    name="fix_failing_test",
    fixture="fix_failing_test",
    prompt=(
        "One test in this repository fails. Find the bug in the code and fix it "
        "so the whole test suite passes. Do not change the tests. Run the tests "
        "with `{python} -m pytest -q -p no:cacheprovider`."
    ),
    timeout=300,
    check_fn=_check_fix_failing_test,
)


# --- 2. multi_site_edit ----------------------------------------------------

_MULTI_SITE_PROBE = """
from billing import sum_line_items, invoice_total
from report import summary, grand_total
items = [(250, 2), (100, 3)]
assert sum_line_items(items) == 800
assert invoice_total(items, 50) == 850
assert summary(items) == "2 items, 800 total"
assert grand_total([items, [(10, 1)]]) == 810
"""


def _check_multi_site_edit(workdir: Path, result_text: str) -> tuple[bool, str]:
    billing = (workdir / "billing.py").read_bytes()
    report = (workdir / "report.py").read_bytes()
    if b"compute_total" in billing or b"compute_total" in report:
        return False, "the old name is still present"
    if billing.count(b"\n") != billing.count(b"\r\n"):
        return False, "billing.py lost its CRLF line endings"
    if b"\r\n" in report:
        return False, "report.py gained CRLF line endings"
    proc = _python(workdir, _MULTI_SITE_PROBE)
    tail = (proc.stdout + proc.stderr).strip()[-300:]
    return proc.returncode == 0, f"probe exit {proc.returncode}: {tail}"


MULTI_SITE_EDIT = EvalCase(
    name="multi_site_edit",
    fixture="multi_site_edit",
    prompt=(
        "Rename the function `compute_total` to `sum_line_items` everywhere in "
        "this repository: its definition, every call and every import. Do not "
        "change what any function does, and keep each file's line endings."
    ),
    timeout=300,
    check_fn=_check_multi_site_edit,
    crlf_files=("billing.py",),
)


# --- 3. answer_from_code ---------------------------------------------------

_ANSWER_CODE = "KV-7724"
_OTHER_CODES = ("KV-1180", "KV-3391", "KV-5062")


def _check_answer_from_code(workdir: Path, result_text: str) -> tuple[bool, str]:
    if _ANSWER_CODE not in result_text:
        return False, "the code is not in the answer"
    if any(code in result_text for code in _OTHER_CODES):
        return False, "the answer names another region's code as well"
    return True, "exact code in the answer"


ANSWER_FROM_CODE = EvalCase(
    name="answer_from_code",
    fixture="answer_from_code",
    prompt=(
        "Which routing code does this service use for its default region? "
        "Answer with the code alone."
    ),
    timeout=180,
    check_fn=_check_answer_from_code,
)


# --- 4. long_context -------------------------------------------------------

_LONG_CONTEXT_WINDOW = 32768
_LONG_CONTEXT_PARTS = 12
_LONG_CONTEXT_LINES = 125
_FRAGMENTS = (
    "amber", "basalt", "cobalt", "delta", "ember", "fjord",
    "garnet", "harbor", "indigo", "juniper", "kestrel", "lumen",
)
_LONG_CONTEXT_ANSWER = "-".join(_FRAGMENTS)


def _generate_long_context(workdir: Path) -> None:
    notes = workdir / "notes"
    notes.mkdir()
    for part in range(1, _LONG_CONTEXT_PARTS + 1):
        lines = []
        for n in range(_LONG_CONTEXT_LINES):
            digest = hashlib.sha256(f"{part}:{n}".encode()).hexdigest()[:32]
            lines.append(f"entry {part:02d}.{n:03d}: reading nominal, checksum {digest}")
        # Mid-file, so the fragment is only found by reading the file.
        lines.insert(_LONG_CONTEXT_LINES // 2, f"fragment {part:02d}: {_FRAGMENTS[part - 1]}")
        (notes / f"part_{part:02d}.txt").write_text("\n".join(lines) + "\n")


def _check_long_context(workdir: Path, result_text: str) -> tuple[bool, str]:
    if _LONG_CONTEXT_ANSWER not in result_text:
        return False, "the joined fragments are not in the answer"
    return True, "joined fragments in the answer"


LONG_CONTEXT = EvalCase(
    name="long_context",
    fixture="long_context",
    prompt=(
        f"The directory notes/ holds {_LONG_CONTEXT_PARTS} files, part_01.txt to "
        f"part_{_LONG_CONTEXT_PARTS:02d}.txt. Each contains exactly one line of the "
        "form `fragment NN: word`. Read every file in full with the Read tool, one "
        "file at a time and in order; do not use Grep, Glob or Bash on them. Then "
        "answer with the twelve words joined by hyphens in part order, for "
        "example `one-two-three`."
    ),
    timeout=600,
    check_fn=_check_long_context,
    config_overrides={"context_window": _LONG_CONTEXT_WINDOW},
    generate=_generate_long_context,
    min_compactions=1,
)


# --- 5. recover_from_failure -----------------------------------------------

_RATE = "0.73519"


def _check_recover_from_failure(workdir: Path, result_text: str) -> tuple[bool, str]:
    out = workdir / "out" / "rate.txt"
    if not out.is_file():
        return False, "out/rate.txt was not written"
    written = out.read_text(encoding="utf-8", errors="replace").strip()
    if written != _RATE:
        return False, f"out/rate.txt holds {written[:40]!r}"
    if not _unchanged(workdir, "recover_from_failure", "sources/fetch_rate.sh"):
        return False, "the feed client was changed"
    return True, "rate written"


RECOVER_FROM_FAILURE = EvalCase(
    name="recover_from_failure",
    fixture="recover_from_failure",
    prompt=(
        "Write the current exchange rate for the symbol XYZ to out/rate.txt, the "
        "number alone. Three sources should hold it; use the first one that "
        "works:\n"
        "1. the live feed: `bash sources/fetch_rate.sh XYZ`\n"
        "2. the cache file data/rates_cache.json\n"
        "3. the archive: `{python} sources/archive.py --symbol XYZ`\n"
        "Do not modify the sources."
    ),
    timeout=300,
    check_fn=_check_recover_from_failure,
    success_marker=_RATE,
)


CASES = [
    FIX_FAILING_TEST,
    MULTI_SITE_EDIT,
    ANSWER_FROM_CODE,
    LONG_CONTEXT,
    RECOVER_FROM_FAILURE,
]


def by_name(name: str) -> EvalCase:
    for case in CASES:
        if case.name == name:
            return case
    raise KeyError(name)
