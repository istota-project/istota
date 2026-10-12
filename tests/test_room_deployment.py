from types import SimpleNamespace

import pytest

from istota import cli, db
from istota.config import Config


def test_offline_init_migrates_rooms_and_sweeps_mount(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / 'state.db')
    config.workspace_path = tmp_path / 'mount'
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, 'old-talk', 'alice', origin='talk')
        db.add_room_binding(conn, 'old-talk', 'talk', 'old-talk')
        db.add_message(conn, 'old-talk', role='user', body='Keep this history', origin_surface='talk')
    old = config.workspace_path / 'Channels' / 'old-talk'
    old.mkdir(parents=True)
    (old / 'CHANNEL.md').write_text('Keep this memory')
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    args = SimpleNamespace(config=None, relocate_rooms=True)
    assert cli.cmd_init(args) in (None, 0)
    with db.get_db(config.db_path) as conn:
        token = conn.execute('SELECT token FROM rooms').fetchone()[0]
        assert db.is_canonical_room_token(token)
        assert conn.execute('SELECT room_token, surface_ref FROM room_bindings').fetchone()[:] == (token, 'old-talk')
        assert conn.execute('SELECT room_token,body FROM messages').fetchone()[:] == (token, 'Keep this history')
    assert (old.parent / token / 'CHANNEL.md').read_text() == 'Keep this memory'
    assert not old.exists()
    assert cli.cmd_init(args) in (None, 0)
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT token FROM rooms').fetchone()[0] == token


@pytest.mark.parametrize('status', ['running', 'pending_confirmation'])
def test_offline_init_refusal_does_not_move_workspace(tmp_path, monkeypatch, status):
    config = Config(db_path=tmp_path / 'state.db', workspace_path=tmp_path / 'workspace')
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, 'old-talk', 'alice', origin='talk')
        task = db.create_task(conn, user_id='alice', source_type='talk', prompt='Wait', conversation_token='old-talk')
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, task))
    old = config.workspace_path / 'Channels' / 'old-talk'
    old.mkdir(parents=True)
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=True)) == 1
    assert old.is_dir()
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT token FROM rooms').fetchone()[0] == 'old-talk'


def _migrated_with_orphan(tmp_path, status):
    config = Config(db_path=tmp_path / 'state.db', workspace_path=tmp_path / 'workspace')
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, 'old-talk', 'alice', origin='talk')
    (config.workspace_path / 'Channels').mkdir(parents=True)
    from istota.maintenance import room_relocate
    assert room_relocate.migrate_database(config.db_path) == 0
    with db.get_db(config.db_path) as conn:
        token = conn.execute('SELECT token FROM rooms').fetchone()[0]
        task = db.create_task(conn, user_id='alice', source_type='talk', prompt='Wait', conversation_token=token)
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, task))
    late = config.workspace_path / 'Channels' / 'old-talk'
    late.mkdir()
    (late / 'CHANNEL.md').write_text('a late write')
    return config, task, token


@pytest.mark.parametrize('status', ['running', 'locked'])
def test_a_stopped_scheduler_s_orphan_does_not_defer_the_sweep(tmp_path, monkeypatch, capsys, status):
    """What the pool's shutdown left running is released, as the next start would (ISSUE-690)."""
    config, task, token = _migrated_with_orphan(tmp_path, status)
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    args = SimpleNamespace(config=None, relocate_rooms=True, scheduler_stopped=True)
    assert cli.cmd_init(args) == 0
    assert 'refusal:' not in capsys.readouterr().err
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'pending'
    channels = config.workspace_path / 'Channels'
    assert (channels / token / 'CHANNEL.md').read_text() == 'a late write'
    assert not (channels / 'old-talk').exists()


def test_without_the_flag_a_running_row_still_defers_the_sweep(tmp_path, monkeypatch, capsys):
    config, task, _ = _migrated_with_orphan(tmp_path, 'running')
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=True)) == 1
    assert 'refusal: live_tasks' in capsys.readouterr().err
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'running'
    assert (config.workspace_path / 'Channels' / 'old-talk').is_dir()


def test_a_parked_question_is_not_an_orphan(tmp_path, monkeypatch, capsys):
    config, task, _ = _migrated_with_orphan(tmp_path, 'pending_confirmation')
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    args = SimpleNamespace(config=None, relocate_rooms=True, scheduler_stopped=True)
    assert cli.cmd_init(args) == 1
    assert 'refusal: pending_confirmation' in capsys.readouterr().err
    with db.get_db(config.db_path) as conn:
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'pending_confirmation'


def test_partial_sweep_raises_an_admin_alert_and_a_clean_one_closes_it(tmp_path, monkeypatch):
    from istota.config import UserConfig
    config = Config(db_path=tmp_path / 'state.db', workspace_path=tmp_path / 'mount',
                    users={'alice': UserConfig(), 'bob': UserConfig()})
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, 'old-talk', 'alice', origin='talk')
    for user in config.users:
        (config.workspace_path / 'Users' / user / config.bot_dir_name / 'config').mkdir(parents=True)
    channels = config.workspace_path / 'Channels'
    (channels / 'old-talk').mkdir(parents=True)
    conflict = channels / 'old-talk' / 'CHANNEL.md'
    conflict.write_text('alias notes')
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    monkeypatch.setattr('istota.config.load_admin_users', lambda *a, **k: {'bob'})
    args = SimpleNamespace(config=None, relocate_rooms=True)
    assert cli.cmd_init(args) == 0
    with db.get_db(config.db_path) as conn:
        token = conn.execute('SELECT token FROM rooms').fetchone()[0]
    (channels / 'old-talk').mkdir()
    conflict.write_text('a late write')
    (channels / token / 'CHANNEL.md').write_text('room notes')
    assert cli.cmd_init(args) == 2
    with db.get_db(config.db_path) as conn:
        rows = conn.execute(
            "SELECT user_id, state, title, params FROM notifications WHERE source='task_alert'"
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [('bob', 'open')]
    assert 'old-talk' in rows[0][3] and 'CHANNEL.md' in rows[0][3]
    conflict.unlink()
    assert cli.cmd_init(args) == 0
    with db.get_db(config.db_path) as conn:
        states = conn.execute("SELECT state FROM notifications WHERE source='task_alert'").fetchall()
    assert [s[0] for s in states] == ['resolved']


def test_fresh_offline_init_needs_no_workspace(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / 'state.db')
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=True)) == 0

