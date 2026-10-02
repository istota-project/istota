"""The root entry-point stub at the devbox proxy's pre-move module path.

Units rendered before the `devbox/` move run `python -m` on the old path, and
the auto-update cron restarts them without re-rendering, so that path has to
keep starting the proxy until the play has run everywhere.
"""

from __future__ import annotations

import subprocess
import sys


def test_the_stub_main_is_the_proxy_main():
    import istota.devbox.proxy as proxy
    import istota.devbox_proxy as stub  # move-modules: keep

    assert stub.main is proxy.main


def test_python_dash_m_on_the_old_path_reaches_the_proxy_parser():
    result = subprocess.run(
        [sys.executable, "-m", "istota.devbox_proxy", "--help"],  # move-modules: keep
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "--user" in result.stdout
