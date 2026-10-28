"""The stack's web process logs `istota.*` INFO lines to its console (ISSUE-872).

uvicorn's own log config covers only the `uvicorn` loggers, so a web process
that never calls `setup_logging` drops every `istota.webui.*` record below
WARNING. The import runs in a child process: `stack_app` configures logging as
an import side effect, and the shipped entrypoint is uvicorn importing it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from istota import logging_setup
from istota.config import Config, LoggingConfig

CHILD = """
import logging
import istota.webui.stack_app  # noqa: F401
logging.getLogger("istota.webui.app").info("web-logging-probe")
"""


def _run_stack_app_import(tmp_path: Path, log_file: Path) -> subprocess.CompletedProcess:
    config = tmp_path / "config.toml"
    config.write_text(f'db_path = "{tmp_path / "istota.db"}"\n')
    env = dict(os.environ)
    env["ISTOTA_CONFIG_PATH"] = str(config)
    # What the image sets: without a `[logging] file`, the config becomes
    # output = "both" with this file, which is the daemon's.
    env["ISTOTA_DEFAULT_LOG_FILE"] = str(log_file)
    return subprocess.run(
        [sys.executable, "-c", CHILD],
        env=env, capture_output=True, text=True, timeout=120,
    )


class TestStackAppLogging:
    def test_an_info_line_reaches_the_console_and_not_the_daemons_file(self, tmp_path):
        log_file = tmp_path / "logs" / "istota.log"

        result = _run_stack_app_import(tmp_path, log_file)

        assert result.returncode == 0, result.stderr
        lines = [ln for ln in result.stderr.splitlines() if "web-logging-probe" in ln]
        assert lines, result.stderr
        # The daemon's format, timestamps included.
        assert "INFO  [istota.webui.app" in lines[0]
        assert lines[0][:4].isdigit(), lines[0]
        assert not log_file.exists(), log_file.read_text()


class TestConsoleOnly:
    def test_console_only_drops_the_file_handler(self, tmp_path):
        log_file = tmp_path / "istota.log"
        config = Config(logging=LoggingConfig(output="both", file=str(log_file)))

        logging_setup.setup_logging(config, daemon_mode=True, console_only=True)

        handlers = logging.getLogger("istota").handlers
        assert [type(h) for h in handlers] == [logging.StreamHandler]
        assert not log_file.exists()

    def test_a_file_only_config_still_gets_a_console(self, tmp_path):
        config = Config(logging=LoggingConfig(output="file", file=str(tmp_path / "x.log")))

        logging_setup.setup_logging(config, console_only=True)

        handlers = logging.getLogger("istota").handlers
        assert [type(h) for h in handlers] == [logging.StreamHandler]
