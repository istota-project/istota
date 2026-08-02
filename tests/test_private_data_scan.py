"""Tests for the pre-commit private-data scanner (scripts/check-private-data.sh).

The scanner is a security control whose failure mode is silence: a regex that
stops matching reports a clean tree, exactly as if there were nothing to find.
So every pattern class gets a positive control, and the two ways of *not*
matching (the exemption marker, the placeholder heuristic) get their own.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER = REPO_ROOT / "scripts" / "check-private-data.sh"
PATTERN_FILE = REPO_ROOT / ".private-data-patterns"
HOOK = REPO_ROOT / ".githooks" / "pre-commit"


def run_scan(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCANNER), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def write(tmp_path: Path, content: str, name: str = "sample.txt") -> Path:
    target = tmp_path / name
    target.write_text(content)
    return target


class TestScannerIsWired:
    def test_scanner_is_executable(self):
        assert SCANNER.exists()
        assert os.access(
            SCANNER, os.X_OK
        ), "scanner must be executable to run from the hook"

    def test_hook_is_executable_and_calls_both_scans(self):
        assert HOOK.exists()
        assert os.access(HOOK, os.X_OK)
        body = HOOK.read_text()
        assert "gitleaks" in body
        assert "check-private-data.sh" in body

    def test_hook_and_scanner_are_valid_bash(self):
        for script in (HOOK, SCANNER):
            result = subprocess.run(
                ["bash", "-n", str(script)], capture_output=True, text=True
            )
            assert result.returncode == 0, f"{script.name}: {result.stderr}"

    def test_local_denylist_is_gitignored(self):
        """The denylist names the very things it exists to keep out of a public repo."""
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".private-data-local"],
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, ".private-data-local must be gitignored"

    def test_example_denylist_carries_no_uncommented_terms(self):
        """The committed template must be comments only — a filled-in one is a leak."""
        lines = (REPO_ROOT / ".private-data-local.example").read_text().splitlines()
        live = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
        assert live == [], f"uncommented entries in the example denylist: {live}"


class TestGitleaksConfig:
    """The companion scan. Its config encodes two decisions worth pinning."""

    @staticmethod
    def _load():
        import tomllib

        return tomllib.loads((REPO_ROOT / ".gitleaks.toml").read_text())

    def test_extends_the_default_ruleset(self):
        """Without useDefault the config replaces the rules rather than adding to them."""
        assert self._load()["extend"]["useDefault"] is True

    def test_prose_files_are_not_path_allowlisted(self):
        """CHANGELOG/DEVLOG are where a pasted credential lands; they must stay scanned."""
        allowlisted = [
            path
            for entry in self._load().get("allowlists", [])
            for path in entry.get("paths", [])
        ]
        for prose in ("CHANGELOG", "DEVLOG"):
            assert not any(
                prose in path for path in allowlisted
            ), f"{prose}.md must not be allowlisted"


class TestCleanTree:
    def test_repository_is_clean(self):
        """The tracked tree must stay clean, or the hook blocks every commit."""
        result = subprocess.run(
            ["bash", str(SCANNER), "--all"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout

    def test_ordinary_file_passes(self, tmp_path):
        target = write(tmp_path, "user_id = 'alice'\ntoken = 'abc123'\n")
        assert run_scan(target).returncode == 0


class TestPatternClasses:
    """One positive control per class of private data the patterns claim to catch."""

    # Every value below is synthetic. The trailing markers exempt the *source*
    # line from the scanners this file tests — they are outside the string, so
    # the value written to tmp_path still carries no marker and must be caught.
    @pytest.mark.parametrize(
        "label,line",
        [
            ("visa", "card: 4111 1111 1111 1111"),  # private-data-ok
            ("visa-nospace", "card: 4111111111111111"),  # private-data-ok
            ("mastercard", "card: 5500-0000-0000-0004"),  # private-data-ok
            ("amex", "card: 3782 822463 10005"),  # private-data-ok
            ("iban", "iban: DE89 3704 0044 0532 0130 00"),  # private-data-ok
            ("ssn", "ssn: 123-45-6789"),  # private-data-ok
            (
                "nextcloud_app_password",
                'app_password = "abcde-fghij-klmno-pqrst-uvwyz"',  # private-data-ok
            ),
            (
                "anthropic_key",
                "key = sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",  # private-data-ok
            ),
            ("openrouter_key", "key = sk-or-v1-" + "a1b2c3d4" * 6),  # private-data-ok
            (
                "github_pat",
                "token = ghp_" + "A1b2C3d4E5" * 3 + "F6g7h8",
            ),  # private-data-ok
            (
                "gitlab_pat",
                "token = glpat-AbCdEfGhIjKlMnOpQrSt",  # private-data-ok gitleaks:allow
            ),
        ],
    )
    def test_pattern_matches(self, tmp_path, label, line):
        target = write(tmp_path, f"# {label}\n{line}\n")
        result = run_scan(target)
        assert result.returncode == 1, f"{label} not detected:\n{result.stdout}"

    def test_home_directory_path_is_derived(self, tmp_path):
        """No configuration needed: an absolute path into $HOME is private by construction."""
        target = write(tmp_path, f"log_path = '{Path.home()}/notes/private.md'\n")
        result = run_scan(target)
        assert result.returncode == 1
        assert "home directory" in result.stdout

    def test_git_user_email_is_derived(self, tmp_path):
        email = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        ).stdout.strip()
        if not email:
            pytest.skip("no git user.email configured")
        target = write(tmp_path, f"contact: {email}\n")
        assert run_scan(target).returncode == 1


class TestExemptions:
    def test_marker_exempts_a_line(self, tmp_path):
        target = write(tmp_path, "card 4111111111111111  private-data-ok\n")
        assert run_scan(target).returncode == 0

    def test_marker_does_not_exempt_neighbouring_lines(self, tmp_path):
        target = write(
            tmp_path,
            "card 4111111111111111  private-data-ok\ncard 4111111111111112\n",
        )
        result = run_scan(target)
        assert result.returncode == 1
        assert ":2 " in result.stdout and ":1 " not in result.stdout

    @pytest.mark.parametrize(
        "line",
        [
            'app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"',
            'gitlab_token = "glpat-xxxxxxxxxxxxxxxxxxxx"',
            "token = ghp_<YOUR_TOKEN_HERE>",
            "app_password = CHANGEME-abcde-fghij-klmno-pqrst",
        ],
    )
    def test_documentation_placeholders_pass(self, tmp_path, line):
        assert run_scan(write(tmp_path, line + "\n")).returncode == 0

    def test_pattern_files_are_not_scanned_against_themselves(self):
        result = run_scan(PATTERN_FILE)
        assert result.returncode == 0


class TestStagedMode:
    """Staged mode reads the index, not the worktree — that is what the hook commits."""

    @pytest.fixture
    def repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for name in ("user.email", "user.name"):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", name, "scanner-test"], check=True
            )
        for artifact in (".private-data-patterns", "scripts/check-private-data.sh"):
            dest = tmp_path / artifact
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO_ROOT / artifact, dest)
        return tmp_path

    def run_staged(self, repo: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "scripts/check-private-data.sh", "--staged"],
            capture_output=True,
            text=True,
            cwd=repo,
        )

    def test_staged_violation_is_caught(self, repo):
        (repo / "leak.txt").write_text("ssn 123-45-6789\n")  # private-data-ok
        subprocess.run(["git", "-C", str(repo), "add", "leak.txt"], check=True)
        result = self.run_staged(repo)
        assert result.returncode == 1
        assert "leak.txt:1" in result.stdout

    def test_unstaged_violation_is_ignored(self, repo):
        (repo / "leak.txt").write_text("ssn 123-45-6789\n")  # private-data-ok
        result = self.run_staged(repo)
        assert result.returncode == 0, result.stdout

    def test_scans_the_index_not_the_worktree(self, repo):
        """A leak staged then reverted on disk is still what would be committed."""
        leak = repo / "leak.txt"
        leak.write_text("ssn 123-45-6789\n")  # private-data-ok
        subprocess.run(["git", "-C", str(repo), "add", "leak.txt"], check=True)
        leak.write_text("clean\n")
        assert self.run_staged(repo).returncode == 1

    def test_binary_file_does_not_break_the_scan(self, repo):
        (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
        (repo / "ok.txt").write_text("nothing here\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        result = self.run_staged(repo)
        assert result.returncode == 0, result.stdout
        assert "warning" not in result.stderr.lower()


class TestNeverPrintsTheValue:
    """A scanner that echoes the secret puts it in the scrollback, then in a chat log."""

    def test_matched_value_is_absent_from_output(self, tmp_path):
        secret = "4111111111111111"  # private-data-ok
        target = write(tmp_path, f"card {secret}\n")
        result = run_scan(target)
        assert result.returncode == 1
        assert secret not in result.stdout
        assert secret not in result.stderr
