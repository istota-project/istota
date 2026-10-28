"""`briefings.group_service` is the group briefing operations without the web layer.

A skill CLI imports it, so it must not load FastAPI or a route module; the
route tests in `test_group_briefing_routes.py` are what prove the move kept
the behaviour.
"""

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# `istota.config` itself loads the stdlib-only `webui.map_basemap`, and every
# skill CLI loads config, so the web modules config brings are the baseline.
PROBE = (
    "import sys\n"
    "import istota.config\n"
    "baseline = set(sys.modules)\n"
    "import istota.briefings.group_service\n"
    "def web(m):\n"
    "    return (m.split('.')[0] in ('fastapi', 'starlette') or m.startswith('istota.webui')\n"
    "            or (m.startswith('istota.') and m.endswith('routes')))\n"
    "loaded = sorted(m for m in set(sys.modules) - baseline if web(m))\n"
    "assert not any(m.split('.')[0] in ('fastapi', 'starlette') for m in baseline), 'config loads fastapi'\n"
    "print(','.join(loaded))\n"
)


def test_group_service_loads_no_web_layer(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))
    result = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                            cwd=tmp_path, env=env, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-4000:]
    # The last line only, so an import-time print cannot read as a loaded module.
    loaded = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert loaded == "", f"group_service pulled in the web layer: {loaded}"
