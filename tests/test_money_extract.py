"""Tests for the standalone-moneyman extraction tooling."""

import importlib.util
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_extract_module():
    spec = importlib.util.spec_from_file_location(
        "_extract", REPO_ROOT / "scripts" / "extract_money_to_standalone.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Isolation guard
# ---------------------------------------------------------------------------


class TestIsolationGuard:
    def test_src_money_has_no_istota_imports(self):
        """Extract requires this; if it fails, the public extract is broken."""
        result = subprocess.run(
            [str(REPO_ROOT / "scripts" / "check_money_isolation.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"Isolation check failed: {result.stdout}{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Extract script
# ---------------------------------------------------------------------------


class TestExtract:
    def test_extract_produces_layout(self, tmp_path):
        mod = _load_extract_module()
        mod.extract(tmp_path, version="0.9.9")
        assert (tmp_path / "src" / "moneyman" / "cli.py").exists()
        assert (tmp_path / "src" / "moneyman" / "core" / "models.py").exists()
        assert (tmp_path / "tests").is_dir()
        assert (tmp_path / "pyproject.toml").exists()
        assert (tmp_path / "README.md").exists()

    def test_extract_excludes_istota_only_files(self, tmp_path):
        mod = _load_extract_module()
        mod.extract(tmp_path, version="0.9.9")
        excluded = ["routes.py", "jobs.py", "workspace.py"]
        for fn in excluded:
            assert not (tmp_path / "src" / "moneyman" / fn).exists(), (
                f"{fn} should be excluded from the standalone extract"
            )

    def test_extract_rewrites_money_imports(self, tmp_path):
        mod = _load_extract_module()
        mod.extract(tmp_path, version="0.9.9")
        import re
        bad_pattern = re.compile(r"^\s*(from|import)\s+money(\.|\s|$)", re.MULTILINE)
        bad = []
        for py in (tmp_path / "src" / "moneyman").rglob("*.py"):
            if bad_pattern.search(py.read_text()):
                bad.append(py)
        assert bad == [], f"residual `money` imports: {bad}"

    def test_extract_rewrites_test_imports(self, tmp_path):
        mod = _load_extract_module()
        mod.extract(tmp_path, version="0.9.9")
        import re
        bad_pattern = re.compile(r"^\s*(from|import)\s+money(\.|\s|$)", re.MULTILINE)
        bad = []
        for py in (tmp_path / "tests").rglob("*.py"):
            if bad_pattern.search(py.read_text()):
                bad.append(py)
        assert bad == [], f"residual `money` imports in tests: {bad}"

    def test_extract_preserves_dot_git(self, tmp_path):
        # Pre-create a .git/HEAD to simulate a pre-cloned destination
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        # Also drop a stale file that should be wiped
        (tmp_path / "stale.txt").write_text("old")

        mod = _load_extract_module()
        mod.extract(tmp_path, version="0.9.9")
        assert (git_dir / "HEAD").read_text() == "ref: refs/heads/main\n"
        assert not (tmp_path / "stale.txt").exists()

    def test_extract_pyproject_has_version(self, tmp_path):
        mod = _load_extract_module()
        mod.extract(tmp_path, version="1.2.3")
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'version = "1.2.3"' in text
        assert 'name = "moneyman"' in text

    def test_extract_frontend_flattens_money_subpath(self, tmp_path):
        mod = _load_extract_module()
        mod.extract(tmp_path, version="0.9.9")
        # web/src/lib/money/api.ts should land at web/src/lib/api.ts
        api_ts = tmp_path / "web" / "src" / "lib" / "api.ts"
        if api_ts.exists():
            text = api_ts.read_text()
            # No leftover /money/ paths in imports
            assert "$lib/money/" not in text
            # base URL rewrite for standalone
            assert "${base}/money/api" not in text
