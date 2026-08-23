"""``istota task --dry-run`` must not queue real work.

The flag promises to show the prompt without executing. It used to create the
task row first and only consult ``--dry-run`` inside the ``--execute`` branch,
so a run without ``-x`` left a ``pending`` row behind — and any running
scheduler picks one of those up within a tick. On a live deployment the flag
whose whole promise is that nothing runs was the flag that queued a real model
call, billed it, and delivered its answer.

So the assertions here are about the task table as much as about stdout.
"""

from __future__ import annotations

import pytest

from istota import cli, db


class _Args:
    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "prompt": "say something",
            "user": "alice",
            "execute": False,
            "dry_run": False,
            "conversation_token": None,
            "source_type": None,
            "no_context": True,
        }
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


@pytest.fixture
def cli_env(monkeypatch, make_config, db_path):
    """Point the CLI at an initialized tmp database and stub the executor.

    ``make_config`` and the ``db_path`` fixture both use ``tmp_path/test.db``,
    so requesting both gives a config whose ``db_path`` has a real schema.
    """
    config = make_config()
    assert config.db_path == db_path
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)

    calls = []

    def _execute_task(task, cfg, resources, **kwargs):
        calls.append({"task": task, "kwargs": kwargs})
        return True, "[DRY RUN] Would execute with prompt:\n\nassembled prompt", None, None

    monkeypatch.setattr(cli, "execute_task", _execute_task)
    return config, calls


def _task_rows(db_path):
    with db.get_db(db_path) as conn:
        return conn.execute("SELECT id, status, prompt FROM tasks").fetchall()


class TestDryRunPersistsNothing:
    def test_dry_run_writes_no_task_row(self, cli_env, db_path, capsys):
        cli.cmd_task(_Args(dry_run=True))
        assert _task_rows(db_path) == []

    def test_dry_run_writes_no_row_even_with_execute(self, cli_env, db_path, capsys):
        """``--dry-run`` wins over ``-x``; neither persists nor runs for real."""
        cli.cmd_task(_Args(dry_run=True, execute=True))
        assert _task_rows(db_path) == []

    def test_dry_run_prints_the_assembled_prompt(self, cli_env, capsys):
        cli.cmd_task(_Args(dry_run=True))
        out = capsys.readouterr().out
        assert "[DRY RUN] Would execute with prompt:" in out
        assert "assembled prompt" in out
        assert "Task created:" not in out

    def test_dry_run_asks_the_executor_for_a_dry_run(self, cli_env):
        _config, calls = cli_env
        cli.cmd_task(_Args(dry_run=True))
        assert len(calls) == 1
        assert calls[0]["kwargs"]["dry_run"] is True

    def test_the_task_handed_to_the_executor_is_unsaved(self, cli_env):
        """id 0 is this codebase's marker for a task with no row behind it."""
        _config, calls = cli_env
        cli.cmd_task(_Args(dry_run=True, conversation_token="tok123"))
        task = calls[0]["task"]
        assert task.id == 0
        assert task.prompt == "say something"
        assert task.user_id == "alice"
        assert task.conversation_token == "tok123"
        assert task.source_type == "talk"  # inferred from the token

    def test_no_context_reaches_the_executor(self, cli_env):
        _config, calls = cli_env
        cli.cmd_task(_Args(dry_run=True, no_context=False))
        assert calls[0]["kwargs"]["use_context"] is True


class TestTheOrdinaryPathStillQueues:
    """Guard against over-fixing: without the flag, a row is still the point."""

    def test_a_plain_submit_creates_a_pending_row(self, cli_env, db_path, capsys):
        cli.cmd_task(_Args())
        rows = _task_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["prompt"] == "say something"

    def test_a_plain_submit_does_not_execute(self, cli_env, capsys):
        _config, calls = cli_env
        cli.cmd_task(_Args())
        assert calls == []
