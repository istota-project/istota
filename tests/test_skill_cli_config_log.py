"""A skill CLI's stderr carries no config-load warnings (ISSUE-839).

A skill subprocess configures no logging, so Python's last-resort handler used
to write every WARNING `load_config` reported to stderr, under every result the
model read. The operator still sees them in the daemon log and the admin view.
"""

import logging
import os
import subprocess
import sys

import pytest

from istota.config import load_config

RETIRED = "default_briefings is migration input only"


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'db_path = "{tmp_path / "istota.db"}"\n'
        f'workspace_path = "{tmp_path / "workspace"}"\n'
        "\n"
        "[[default_briefings]]\n"
        'name = "morning"\n'
        'cron = "0 7 * * *"\n'
        'conversation_token = "room1"\n'
        "\n"
        "[users.alice]\n"
        'display_name = "Alice"\n'
    )
    return path


def _run_skill(config_path, *argv):
    env = dict(
        os.environ,
        ISTOTA_CONFIG_PATH=str(config_path),
        ISTOTA_USER_ID="alice",
        BRIEFINGS_USER="alice",
    )
    return subprocess.run(
        [sys.executable, "-m", *argv],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_the_config_does_report_the_retired_key(config_path, caplog):
    with caplog.at_level(logging.WARNING, logger="istota.config"):
        load_config(config_path)
    assert RETIRED in caplog.text


@pytest.mark.parametrize("argv", [
    ("istota.skills.briefings", "list"),
    ("istota.skills.skills", "list"),
])
def test_skill_cli_stderr_has_no_config_warning(config_path, argv):
    result = _run_skill(config_path, *argv)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert RETIRED not in result.stderr


@pytest.fixture
def dropped_problem_warnings():
    from istota.config_mapper import (
        _below_error_document_problem,
        drop_logged_problem_warnings,
    )

    drop_logged_problem_warnings()
    yield
    logging.getLogger("istota.config").removeFilter(_below_error_document_problem)


def test_only_document_problem_warnings_are_dropped(dropped_problem_warnings, caplog):
    from istota.config_mapper import report_problem

    logger = logging.getLogger("istota.config")
    with caplog.at_level(logging.WARNING, logger="istota.config"):
        report_problem("retired", ("x",), "retired key x")
        report_problem("discarded", ("y",), "bad value y", level=logging.ERROR)
        logger.warning("user_profiles overlay skipped")
    assert "retired key x" not in caplog.text
    assert "bad value y" in caplog.text
    assert "user_profiles overlay skipped" in caplog.text
