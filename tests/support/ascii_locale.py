"""Run a snippet in a child interpreter whose default encoding is ASCII.

A file written without ``encoding=`` takes ``locale.getencoding()``, so a test
running under the developer's UTF-8 locale cannot tell a pinned call site from
an unpinned one — both round-trip fine. Forcing the child's locale to C is what
makes the difference observable, and a locale nothing sets is the shape the
mismatch takes on a real deploy. Each caller's own module docstring says which
deploy that is.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_ascii_locale(script: str) -> subprocess.CompletedProcess:
    """Run ``script`` in a child interpreter whose default encoding is ASCII.

    The script goes to a file rather than to ``-c``. Under ``LC_ALL=C`` with
    PEP 538 coercion off, Linux decodes argv as ASCII and refuses a ``-c``
    body containing a non-ASCII character — "Unable to decode the command from
    the command line" — before the interpreter starts, which is a fact about
    argv rather than about the encoding these tests are about. (macOS forces
    UTF-8 for argv, so such a test passes there and only fails in the Linux
    runner.) Source files are UTF-8 by default whatever the locale, so writing
    the script out gets it in intact while leaving the child's *runtime*
    locale ASCII, which is the condition under test.
    """
    env = dict(os.environ)
    env.update({
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
        "PYTHONPATH": str(REPO_ROOT / "src"),
    })
    with tempfile.TemporaryDirectory() as workdir:
        script_path = Path(workdir) / "snippet.py"
        script_path.write_text(textwrap.dedent(script), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True, text=True, env=env, timeout=60,
            # The *parent's* decoding. A failing snippet's traceback quotes the
            # non-ASCII source line that provoked it, so a pytest process under
            # LC_ALL=C would raise here instead of reporting the test failure.
            encoding="utf-8", errors="replace",
        )
