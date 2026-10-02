"""The istota-connector plugin's pure PHP core, run under the host's `php`.

`integrations/wordpress/istota-connector/includes/fields.php` holds the value
model behind the field editing abilities: normalize and denormalize, paths,
operations, shape and row-count checks, the token. It calls no WordPress, so
`tests/php/fields_test.php` exercises it with a bare `php` binary and plain
asserts, exiting 1 on any failure. Skipped where `php` is not installed, like
the plugin's syntax check in `test_skills_wordpress_connector.py`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIELDS = REPO / "integrations" / "wordpress" / "istota-connector" / "includes" / "fields.php"
SCRIPT = REPO / "tests" / "php" / "fields_test.php"

pytestmark = pytest.mark.skipif(shutil.which("php") is None, reason="php is not installed")


@pytest.mark.parametrize("path", [FIELDS, SCRIPT], ids=["fields.php", "fields_test.php"])
def test_it_is_valid_php(path):
    result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_php_core_passes_its_own_tests():
    result = subprocess.run(["php", str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checks passed" in result.stdout
