"""No `| grep -q` in a shell script that runs under `pipefail` (ISSUE-736).

`grep -q` exits at its first match. A writer that has not finished takes
SIGPIPE and exits 141, and under `pipefail` that is the pipeline's status, so
the test reads "no match" on a match, depending on timing. Capture the output
and test it with a here-string, or drop `-q` and send grep's output to
`/dev/null` so it reads to the end.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("host", "scripts", "docker", ".githooks", "ansible")
ROOT_FILES = ("install.sh",)
# Not shell: a Python docstring may describe the pattern.
SKIPPED_SUFFIXES = {".py", ".md", ".json", ".toml"}
PIPED_QUIET_GREP = re.compile(
    r"(?<!\|)\|&?\s*"                     # a pipe, not an `||`
    r"(?:command\s+|\w+=\S*\s+)*"          # `command grep`, `LC_ALL=C grep`
    r"[ef]?grep\b[^|;&#]*?\s"
    r"(?:-[A-Za-z]*q[A-Za-z]*|--quiet)\b"  # -q in any flag group
)
# A pipe ending one line and the grep starting the next.
CONTINUED_PIPE = re.compile(r"\|\s*\\?\n\s*")


def _candidates() -> list[Path]:
    paths = [REPO / name for name in ROOT_FILES]
    for root in ROOTS:
        paths.extend(sorted((REPO / root).rglob("*")))
    return paths


def _shell_sources() -> list[Path]:
    found = []
    for path in _candidates():
        if not path.is_file() or path.suffix in SKIPPED_SUFFIXES:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "pipefail" in text:
            found.append(path)
    return found


def test_the_walk_finds_the_scripts_that_were_affected():
    names = {str(p.relative_to(REPO)) for p in _shell_sources()}
    assert {"host/istota-stack", "host/mac/istota-stack", "host/cloud-init.yaml", "install.sh"} <= names


def test_no_pipefail_script_pipes_into_a_quiet_grep():
    hits = []
    for path in _shell_sources():
        text = CONTINUED_PIPE.sub("| ", path.read_text())
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split(" #", 1)[0]
            if not code.lstrip().startswith("#") and PIPED_QUIET_GREP.search(code):
                hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_the_pattern_matches_the_spellings_and_not_an_or():
    assert PIPED_QUIET_GREP.search("cmd | grep -qxF daemon")
    assert PIPED_QUIET_GREP.search("            | grep -q 'X'; then")
    assert PIPED_QUIET_GREP.search("cmd |grep -Fq x")
    assert PIPED_QUIET_GREP.search("cmd | grep --quiet x")
    assert PIPED_QUIET_GREP.search("cmd | grep -E -q x")
    assert PIPED_QUIET_GREP.search("cmd |& grep -q x")
    assert PIPED_QUIET_GREP.search("cmd | egrep -q x")
    assert PIPED_QUIET_GREP.search("cmd | LC_ALL=C grep -q x")
    assert PIPED_QUIET_GREP.search("cmd | command grep -q x")
    assert PIPED_QUIET_GREP.search(CONTINUED_PIPE.sub("| ", "cmd |\n    grep -q x"))
    assert not PIPED_QUIET_GREP.search("grep -Fq a f || grep -Fq b f")
    assert not PIPED_QUIET_GREP.search("cmd | grep -x x >/dev/null")
