"""A CLI read-modify-write excludes a web replacement in another process."""
import json
import subprocess
import sys
import time

import pytest

from istota import db
from tests.support import group_memory_processes as witness


@pytest.mark.parametrize("split_anchor", [False, True], ids=["shared", "negative-control"])
def test_cli_and_web_processes_share_the_group_lock(tmp_path, split_anchor):
    database = tmp_path / "istota.db"
    db.init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sync = tmp_path / "sync"
    args = [str(database), str(workspace), str(sync)]
    def run(script):
        result = subprocess.run([sys.executable, "-c", script, *args], capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stderr
        return result.stdout
    run(witness.SEED)
    cli_script = witness.CLI
    if split_anchor:
        cli_script = cli_script.replace("memory._read_target = paused",
                                        "memory._read_target = paused\nmemory._lock_dir = lambda target: sync / 'isolated'")
    cli = subprocess.Popen([sys.executable, "-c", cli_script, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    web = None
    try:
        deadline = time.monotonic() + 10
        while not (sync / "held").exists():
            assert cli.poll() is None, cli.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.05)
        web = subprocess.Popen([sys.executable, "-c", witness.WEB, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if split_anchor:
            with pytest.raises(AssertionError, match="web bypassed the CLI lock"):
                run(witness.RELEASE)
        else:
            run(witness.RELEASE)
        out, err = cli.communicate(timeout=10)
        assert cli.returncode == 0, err
        assert json.loads(out)["status"] == "ok"
        out, err = web.communicate(timeout=10)
        assert web.returncode == 0, err
        if split_anchor:
            assert json.loads(out)["status"] == "ok"
        else:
            assert json.loads(out)["status"] == 409
            run(witness.VERIFY)
    finally:
        (sync / "release").touch()
        for process in (cli, web):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
