"""The CLI eval's cases: the native eval's five, plus one image case.

`long_context` keeps its prompt and check but drops what only the native brain
can honour: its `context_window` override and its compaction floor. The CLI
has no context-window knob, so on its own window it is a plain twelve-file read.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from ..native_eval.cases import CASES as NATIVE_CASES
from ..native_eval.runner import EvalCase

FIXTURES = Path(__file__).parent / "fixtures"

_SIGN_TEXT = "OSPREY 318"


def _check_read_image(workdir: Path, result_text: str) -> tuple[bool, str]:
    answer = re.sub(r"\s+", " ", result_text).upper()
    if _SIGN_TEXT in answer:
        return True, "the sign's text is in the answer"
    return False, f"answer was {result_text.strip()[:80]!r}"


READ_IMAGE = EvalCase(
    name="read_image",
    fixture=FIXTURES / "read_image",
    prompt=(
        "The file sign.png in this repository is a picture of a short line of "
        "text. Open the image with the Read tool and answer with that text alone, "
        "exactly as written."
    ),
    timeout=180,
    check_fn=_check_read_image,
)


def _cli_case(case: EvalCase) -> EvalCase:
    if case.name == "long_context":
        return dataclasses.replace(case, config_overrides={}, min_compactions=0)
    return case


CASES = [*(_cli_case(case) for case in NATIVE_CASES), READ_IMAGE]


def by_name(name: str) -> EvalCase:
    for case in CASES:
        if case.name == name:
            return case
    raise KeyError(name)
