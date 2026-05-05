"""Repo-wide guard: no `echo >>` against USER.md or CHANNEL.md.

The runtime CLI (`istota-skill memory`) is the single write path for
durable memory files. Append-to-EOF via shell redirection bypasses
section routing, dedup, the flock, and the audit log; if it ever gets
back into a doc or a skill body, lint pass + bypass detection will
notice but not prevent it. This test fails fast at PR review time.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = [
    REPO_ROOT / "src",
    REPO_ROOT / "config",
    REPO_ROOT / "docs",
    REPO_ROOT / ".claude" / "rules",
]

# Skip the test files themselves — they document the forbidden pattern
# in regex form and would otherwise trip the guard.
SKIP_FILES = {
    REPO_ROOT / "tests" / "test_no_echo_user_md.py",
    REPO_ROOT / "tests" / "test_skill_memory_classification.py",
}

_BAD_PATTERNS = [
    re.compile(r"echo\s+[^\n]*>>\s*\S*USER\.md"),
    re.compile(r"echo\s+[^\n]*>>\s*\S*CHANNEL\.md"),
    re.compile(r"tee\s+-a\s*\S*USER\.md"),
    re.compile(r"tee\s+-a\s*\S*CHANNEL\.md"),
]


def test_no_echo_redirect_into_memory_files():
    offenders: list[tuple[Path, int, str]] = []
    for root in SCAN_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path in SKIP_FILES:
                continue
            if path.suffix not in (".md", ".py", ".sh", ".j2", ".yml", ".yaml", ".toml"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                for pat in _BAD_PATTERNS:
                    if pat.search(line):
                        offenders.append((path, i, line.strip()))

    assert not offenders, "Forbidden echo>>/tee -a into USER.md or CHANNEL.md:\n" + "\n".join(
        f"  {p.relative_to(REPO_ROOT)}:{lineno}: {snippet}"
        for p, lineno, snippet in offenders
    )
