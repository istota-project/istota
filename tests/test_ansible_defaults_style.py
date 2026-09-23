"""Keep the role defaults easy to scan beside the values they explain."""

from pathlib import Path
import re


DEFAULTS = Path(__file__).resolve().parent.parent / "deploy/ansible/defaults/main.yml"


def test_defaults_comments_are_compact_and_consistent():
    content = DEFAULTS.read_text()
    lines = content.splitlines()
    comment_run = 0

    for line_number, line in enumerate(lines, 1):
        if "#" in line:
            assert len(line) <= 120, line_number
        if line.startswith("#"):
            assert not re.fullmatch(r"#\s*[-=_*]{3,}\s*", line), line_number
            assert not line.startswith("# ---"), line_number
            comment_run += 1
            assert comment_run <= 6, f"comment block exceeds six lines at {line_number}"
        else:
            comment_run = 0
            if re.match(r"istota_[a-z0-9_]+:", line):
                assert not re.search(r" {3,}#", line), line_number
                assert not re.search(r"(?<! ) #", line), line_number

    assert "\n\n\n" not in content
