"""A dry run with the default config must not create the framework database (#571).

`Config.db_path` defaults to the relative `data/istota.db` and `sqlite3.connect`
creates a missing file, so every optional read and write on the path to the
dry-run return left one in the working directory. Web and Talk tasks then
raised on the empty file; CLI and scheduled tasks completed and left a 40 KB
database behind that later tests in the same directory read.
"""

import pytest

from istota import cli, db
from istota.config import Config
from istota.executor import execute_task


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A cwd holding an empty `data/`, and a bare Config pointing into it."""
    monkeypatch.chdir(tmp_path)
    # With a master key the shared-credential gate reaches the database too.
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "k" * 40)
    (tmp_path / "data").mkdir()
    config = Config()
    config.temp_dir = tmp_path / "tmp"
    # Switched on so the two recall passes reach their database reads.
    config.memory_search.enabled = True
    config.memory_search.auto_recall = True
    config.playbooks.enabled = True
    return tmp_path, config


def _task(source_type, **kw):
    fields = dict(
        id=11, status="running", source_type=source_type, user_id="alice",
        prompt="hello", conversation_token="tok1",
    )
    fields.update(kw)
    return db.Task(**fields)


def _nothing_created(tmp_path):
    assert [p.name for p in (tmp_path / "data").rglob("*")] == []


@pytest.mark.parametrize("source_type", ["cli", "scheduled", "web", "talk"])
def test_a_dry_run_with_a_bare_config_creates_no_database(bare, source_type):
    tmp_path, config = bare
    success, output, _, _ = execute_task(_task(source_type), config, [], dry_run=True)
    assert success, output
    assert "hello" in output
    _nothing_created(tmp_path)


def test_a_reply_parent_lookup_creates_no_database(bare):
    tmp_path, config = bare
    task = _task("web", reply_to_message_id=5, reply_to_content="the parent")
    success, output, _, _ = execute_task(task, config, [], dry_run=True)
    assert success, output
    _nothing_created(tmp_path)


def test_a_briefing_with_no_database_fails_and_creates_nothing(bare, monkeypatch):
    """A briefing is refused rather than skipped, before its module is
    resolved, since resolving it would create the module database."""
    from istota.config import UserConfig

    tmp_path, config = bare
    config.users["alice"] = UserConfig()
    config.workspace_path = tmp_path / "workspace"
    config.workspace_path.mkdir()
    monkeypatch.setattr(Config, "is_module_enabled", lambda self, *a, **kw: True)
    task = _task("briefing", briefing_name="morning")
    success, _output, _, _ = execute_task(task, config, [], dry_run=True)
    assert not success
    _nothing_created(tmp_path)


def test_the_cli_dry_run_creates_no_database(bare, monkeypatch, capsys):
    """`istota task --dry-run` opened the database itself before the executor."""
    tmp_path, config = bare
    monkeypatch.setattr(cli, "load_config", lambda path=None: config)

    class Args:
        config = None
        prompt = "hello"
        user = "alice"
        execute = False
        dry_run = True
        conversation_token = None
        source_type = None
        no_context = False

    cli.cmd_task(Args())
    assert "hello" in capsys.readouterr().out
    _nothing_created(tmp_path)
