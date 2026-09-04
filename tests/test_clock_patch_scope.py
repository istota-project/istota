"""No test patches `sleep` or `monotonic` on the stdlib module by hand.

ISSUE-411 converted fifty-four such patches. Every one of them read as though it
patched the module under test and in fact patched the interpreter, because every
module under `src/` does `import time` and `mod.time` **is** the stdlib module —
so the replacement lands on every thread in the xdist worker. The repo had one
false red and one false green from exactly that, each invisible in a single-file
run and each found by chasing an intermittent failure rather than by reading.

The conversion is worth nothing without this. The wrong form is shorter than the
right one, it is what a reader copies from a neighbouring test, and it passes —
so nothing but a scan stops a fifty-fifth site arriving next month.

**Two spellings, not one.** `monkeypatch.setattr(mod.time, "sleep", …)` is the
one the issue was filed about; `mock.patch("mod.time.sleep")` resolves the same
attribute path at run time and is the form this repo used more of. A scan that
saw only the first would have reported a clean tree with twenty-three live sites
in it, which is how this guard was nearly shipped.

The scan is textual on purpose. The property is about the shape a test author
writes, not about anything reachable at run time: by the time the patch is
installed there is no way left to tell it from a deliberate one.

**What it cannot see, stated rather than implied.** A patch reached through a
local alias — `clk = mod.time` then `setattr(clk, "sleep", …)`, or `import time
as clk` — is out of reach of any textual scan, because the name carries none of
the evidence. So this is a guard against the idiom, not a proof about the tree.
It is matched over the whole file rather than line by line, since the repo has no
line-length rule and a wrapped call is ordinary here.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# Every match in these four is documentation prose: the helper docstrings quote
# the wrong form in order to explain it, and the two control files name it in
# theirs. The helpers themselves patch `time_holder(module)`, which this scan
# does not match and is not meant to.
ALLOWED = {
    "support/sleep_spy.py",
    "support/monotonic_spy.py",
    "test_sleep_spy.py",
    "test_monotonic_spy.py",
    "test_clock_patch_scope.py",
}

# `\s*` throughout, because a wrapped call is one the scan still has to see.
# Both quote styles, because nothing in the repo enforces one.
PATCH_RE = re.compile(
    r"""setattr\(\s*[\w.]*\btime\s*,\s*["'](?:sleep|monotonic)["']"""
    r"""|patch\(\s*["'][\w.]*\btime\.(?:sleep|monotonic)["']""",
    re.DOTALL,
)


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in PATCH_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{rel}:{line}: {match.group(0)}")
    return found


def test_clock_patches_go_through_the_thread_bounded_helpers():
    offenders = _offenders()
    assert not offenders, (
        "patch the module's own interval constant, or use "
        "tests.support.sleep_spy.sleep_spy / "
        "tests.support.monotonic_spy.monotonic_spy — a bare patch of the "
        "stdlib module reaches every thread in the worker:\n"
        + "\n".join(offenders)
    )


def test_the_scan_matches_every_form_it_is_meant_to_catch():
    """The guard's own control: a scanner whose regex rotted reports a clean tree.

    The wrapped and single-quoted rows are not hypothetical. `monotonic_spy`'s
    own docstring wraps the call across a line break, so a line-oriented scan
    found nothing in a file the allowlist was exempting — an allowlist entry
    that silenced nothing, over a scan that could not see the form it named.
    """
    must_match = (
        'monkeypatch.setattr(mod.time, "sleep", lambda *_: None)',
        "monkeypatch.setattr(mod.time, 'sleep', lambda *_: None)",
        'monkeypatch.setattr(time, "monotonic", lambda: 0.0)',
        'monkeypatch.setattr(nc_avatars.time, "monotonic", fake)',
        'monkeypatch.setattr(mod.time,\n    "monotonic", fake)',
        '@patch("istota.brain.claude_code.time.sleep")',
        'patch("time.sleep")',
        'with patch("istota.context.time.monotonic", side_effect=[0.0]):',
        "patch(\n    'istota.executor.time.sleep',\n)",
    )
    for sample in must_match:
        assert PATCH_RE.search(sample), sample

    must_not_match = (
        'monkeypatch.setattr(mod, "_SENTINEL_POLL_S", 0.0)',
        "sleep_spy(monkeypatch, mod, record=False)",
        "monotonic_spy(monkeypatch, mod, lambda: 0.0)",
        'monkeypatch.setattr(mod.time, "time", lambda: 0.0)',
        'patch("istota.executor.subprocess.run")',
        'monkeypatch.setattr(time_holder(module), "sleep", spy)',
    )
    for sample in must_not_match:
        assert not PATCH_RE.search(sample), sample


def test_every_allowlist_entry_still_earns_its_place():
    """A dead entry is a place a real offender can later hide unflagged.

    `support/monotonic_spy.py` was exactly that for one revision: it was
    allowlisted for a docstring occurrence the line-oriented scan could not
    match, so it silenced nothing and would have silenced the next genuine site
    in that file.
    """
    for rel in sorted(ALLOWED):
        text = (TESTS / rel).read_text(encoding="utf-8", errors="replace")
        assert PATCH_RE.search(text), (
            f"{rel} is on the allowlist but matches nothing; remove it"
        )
