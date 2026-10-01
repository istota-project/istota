from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from istota import cli, db
from istota.config import Config
from tests.test_ansible_web_build import Rig

REPO = Path(__file__).resolve().parents[1]


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


def test_role_serializes_the_whole_offline_window():
    tasks = yaml.safe_load((REPO / 'deploy/ansible/tasks/main.yml').read_text())
    task = next(t for t in tasks if t.get('name') == 'Relocate room identities offline')
    assert 'not istota_web_only' in task['when']
    assert not any('istota_update_only' in value for value in task['when'])
    assert 'scripts/relocate-rooms.sh' in task['command']
    assert '--lock-held' not in task['command']
    assert 'istota_update_lock_wait' in task['command']
    assert 'failed_when' not in task


@pytest.mark.parametrize('rc,reason', [(0, ''), (1, 'live_tasks'), (1, 'pending_confirmation'), (2, 'partial')])
def test_update_script_offline_window_and_refusals(tmp_path, rc, reason):
    rig = Rig(tmp_path)
    target = rig.commit({'src/app.py': 'x = 2\n'})
    # The command boundary is coarse: the shipped CLI's real DB+mount work is
    # exercised above and in the image tier. Here the shell owns service order.
    (rig.home / '.venv/bin/istota').write_text(
        '#!/bin/sh\n'
        f'echo "istota $*" >> "{rig.stub_log}"\n'
        f'echo "refusal: {reason}" >&2\nexit {rc}\n'
    )
    (rig.home / '.venv/bin/istota').chmod(0o755)
    result = rig.run()
    calls = rig.calls()
    migrations = [i for i, c in enumerate(calls) if 'init --relocate-rooms' in c]
    assert migrations, calls
    for unit in ('scheduler', 'web', 'webhooks'):
        assert calls.index(f'systemctl stop istota-{unit}') < migrations[0]
        verb = 'restart' if rc == 0 or reason == 'live_tasks' else 'start'
        assert calls.index(f'systemctl {verb} istota-{unit}') > migrations[0]
    assert (result.returncode == 0) == (rc == 0 or reason == 'live_tasks'), result.stderr
    if rc and reason != 'live_tasks':
        assert (rig.state / 'last-deployed-sha').read_text().strip() != target


def test_update_script_stop_failure_never_migrates(tmp_path):
    rig = Rig(tmp_path)
    rig.commit({'src/app.py': 'x = 2\n'})
    (rig.bin / 'systemctl').write_text(
        '#!/bin/sh\n'
        f'echo "systemctl $*" >> "{rig.stub_log}"\n'
        '[ "$1" != "show" ] || echo loaded\n'
        '[ "$1 $2" != "stop istota-web" ]\n'
    )
    result = rig.run()
    assert result.returncode != 0
    assert not any('init --relocate-rooms' in c for c in rig.calls())
    assert 'systemctl start istota-scheduler' in rig.calls()


def test_update_keeps_absent_and_stopped_units_stopped(tmp_path):
    rig = Rig(tmp_path)
    rig.commit({'src/app.py': 'x = 2\n'})
    (rig.bin / 'systemctl').write_text(
        '#!/bin/sh\n'
        f'echo "systemctl $*" >> "{rig.stub_log}"\n'
        'case "$1 $2 $3" in\n'
        '  "is-active --quiet istota-web"|"is-active --quiet istota-webhooks") exit 3 ;;\n'
        '  "show istota-webhooks --property=LoadState") echo not-found ;;\n'
        '  show*) echo loaded ;;\n'
        'esac\nexit 0\n'
    )
    result = rig.run()
    assert result.returncode == 0, result.stderr
    calls = rig.calls()
    assert 'systemctl stop istota-web' in calls
    assert 'systemctl stop istota-webhooks' not in calls
    assert 'systemctl restart istota-scheduler' in calls
    for unit in ('web', 'webhooks'):
        assert f'systemctl start istota-{unit}' not in calls
        assert f'systemctl restart istota-{unit}' not in calls


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


def test_fresh_offline_init_needs_no_workspace(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / 'state.db')
    monkeypatch.setattr(cli, 'load_config', lambda path: config)
    assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=True)) == 0


def test_maintenance_window_shares_the_cron_lock(tmp_path):
    import fcntl
    import os
    import subprocess

    rig = Rig(tmp_path)
    rig.script()  # render the same wrapper the updater invokes
    command = ['bash', str(tmp_path / 'relocate-rooms.sh'), 'istota', 'istota',
               str(rig.home / '.venv/bin/istota'), str(tmp_path / 'config.toml'), '0',
               str(rig.home / 'data/istota.db')]
    env = dict(os.environ, PATH=f'{rig.bin}:{rig.tools}')
    with (tmp_path / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        assert blocked.returncode != 0
        assert not rig.calls(), 'a blocked window must not stop a service or migrate'
    allowed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert allowed.returncode == 0, allowed.stderr
    assert 'systemctl stop istota-scheduler' in rig.calls()
    assert 'systemctl restart istota-scheduler' in rig.calls()


def test_later_role_restart_cannot_reopen_a_writer_during_cron_migration(tmp_path):
    import os
    import shlex
    import subprocess
    import time
    from jinja2 import Environment

    rig = Rig(tmp_path)
    rig.commit({'src/app.py': 'x = 2\n'})
    entered, release = tmp_path / 'entered', tmp_path / 'release'
    (rig.home / '.venv/bin/istota').write_text(
        '#!/usr/bin/env python3\n'
        'from pathlib import Path\nimport time\n'
        f'Path({str(entered)!r}).touch()\n'
        f'while not Path({str(release)!r}).exists(): time.sleep(0.02)\n'
    )
    script = tmp_path / 'update.sh'
    script.write_text(rig.script())
    env = dict(os.environ, PATH=f'{rig.bin}:{rig.tools}')
    process = subprocess.Popen(['bash', str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), rig.log.read_text()
        tasks = yaml.safe_load((REPO / 'deploy/ansible/tasks/main.yml').read_text())
        task = next(t for t in tasks if t.get('name') == 'Restart web interface (web-only mode)')
        command = Environment().from_string(task['command']).render(
            istota_namespace='istota', istota_update_lock_wait=0,
        ).replace('/tmp/istota-update.lock', str(tmp_path / 'lock'))
        blocked = subprocess.run(shlex.split(command), env=env, capture_output=True, timeout=10)
        assert blocked.returncode != 0
        assert not any('systemctl restart' in call for call in rig.calls())
    finally:
        release.touch()
        process.communicate(timeout=10)
    assert process.returncode == 0, rig.log.read_text()
    allowed = subprocess.run(shlex.split(command), env=env, capture_output=True, timeout=10)
    assert allowed.returncode == 0


def test_migration_uses_the_services_environment_file(tmp_path):
    rig = Rig(tmp_path)
    rig.commit({'src/app.py': 'x = 2\n'})
    runner = rig.bin / 'systemd-run'
    runner.write_text(
        '#!/bin/sh\n'
        f'echo "systemd-run $*" >> "{rig.stub_log}"\n'
        'while [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n'
    )
    assert rig.run().returncode == 0
    call = next(call for call in rig.calls() if call.startswith('systemd-run '))
    assert '--uid=istota' in call
    assert '--property=EnvironmentFile=-/etc/istota/secrets.env' in call
    assert '--wait --pipe --collect' in call



def test_ownership_repair_runs_inside_stopped_window(tmp_path):
    rig = Rig(tmp_path)
    rig.commit({'src/app.py': 'x = 2\n'})
    data = rig.home / 'data'
    data.mkdir(exist_ok=True)
    for name in ('istota.db', 'istota.db-wal', 'istota.db-shm'):
        (data / name).touch()
    result = rig.run()
    assert result.returncode == 0, result.stderr
    calls = rig.calls()
    owned = [calls.index(f'chown istota: {data / name}') for name in ('istota.db', 'istota.db-wal', 'istota.db-shm')]
    stopped = [calls.index(f'systemctl stop istota-{unit}') for unit in ('scheduler', 'web', 'webhooks')]
    migration = next(i for i, call in enumerate(calls) if 'init --relocate-rooms' in call)
    assert max(stopped) < min(owned) <= max(owned) < migration


def test_ownership_failure_restores_writers_without_migrating(tmp_path):
    rig = Rig(tmp_path)
    rig.commit({'src/app.py': 'x = 2\n'})
    (rig.bin / 'chown').write_text('#!/bin/sh\ncase "$*" in *data/istota.db*) exit 1 ;; esac\n')
    result = rig.run()
    assert result.returncode != 0
    assert not any('init --relocate-rooms' in call for call in rig.calls())
    assert 'systemctl start istota-scheduler' in rig.calls()


def test_room_maintenance_follows_legacy_database_work():
    tasks = yaml.safe_load((REPO / 'deploy/ansible/tasks/main.yml').read_text())
    names = [task.get('name') for task in tasks]
    room = names.index('Relocate room identities offline')
    assert names.index('Migrate framework location data into per-user location.db files') < room
    assert names.index('Relocate module DBs from mount to local disk') < room
    assert room < names.index('Deploy istota-scheduler systemd service')
