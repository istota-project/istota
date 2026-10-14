"""Group notebook exclusion across the shipped daemon and web services."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.support import group_memory_processes as witness

pytestmark = pytest.mark.smoke


def test_group_memory_anchor_is_shared_by_daemon_and_web(stack):
    # Stack.exec drops the daemon; web exec needs the same explicit drop.
    def execute(script, args=(), service="daemon"):
        argv = ["/app/.venv/bin/python", "-c", script, *args]
        if service == "web":
            argv.insert(0, "istota-drop")
        result = stack.exec(argv, service=service, timeout=40)
        assert result.returncode == 0, result.stderr
        return result.stdout
    paths = execute("""
import json
from pathlib import Path
from istota.config import load_config
config = load_config(Path('/data/config/config.toml'))
print(json.dumps([str(config.db_path), str(config.workspace_path),
                  str(config.db_path.parent / 'locks/group-memory/test-witness')]))
""")
    args = json.loads(paths)
    execute(witness.SEED, args)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cli = pool.submit(execute, witness.CLI, args)
        try:
            execute(witness.PRELUDE + '\nwait_for("held")', args, service="web")
            web = pool.submit(execute, witness.WEB, args, "web")
            execute(witness.RELEASE, args)
            assert json.loads(cli.result(timeout=30))["status"] == "ok"
            assert json.loads(web.result(timeout=30))["status"] == 409
            checked = json.loads(execute(witness.VERIFY, args, service="web"))
            assert checked["uid"] == 10001
            assert "/locks/group-memory/GROUP.md." in checked["anchor"]
        finally:
            execute(witness.PRELUDE + '\n(sync / "release").touch()', args)
