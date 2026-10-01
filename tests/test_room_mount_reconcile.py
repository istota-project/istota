"""The filesystem half runs from a committed identity mapping."""
from unittest.mock import patch

import pytest

from istota import db, room_relocate, storage
from istota.config import Config, UserConfig


@pytest.fixture
def migrated(tmp_path):
    config = Config(db_path=tmp_path / "state.db", workspace_path=tmp_path / "workspace",
                    users={"alice": UserConfig(), "bob": UserConfig()})
    config.workspace_path.mkdir()
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, "old-talk", "alice", origin="talk")
        db.add_room_member(conn, "old-talk", "alice")
    assert room_relocate.migrate_database(config.db_path) == 0
    with db.get_db(config.db_path) as conn:
        new = conn.execute("SELECT new_token FROM room_token_migration").fetchone()[0]
    for user in config.users:
        (config.workspace_path / "Users" / user / config.bot_dir_name / "config").mkdir(parents=True)
    return config, "old-talk", new


def put(config, path, text):
    target = config.workspace_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def userfile(config, user, name, text):
    return put(config, f"Users/{user}/{config.bot_dir_name}/config/{name}", text)


def test_local_sweep_is_resumable_and_reads_legacy_memory(migrated):
    config, old, new = migrated
    put(config, f"Channels/{old}/CHANNEL.md", "remember this")
    put(config, f"Channels/{old}/memories/2026-01-01.md", "older memory")
    assert storage.read_channel_memory(config, new) == "remember this"
    assert room_relocate.reconcile_mount(config) == 0
    assert not (config.workspace_path / "Channels" / old).exists()
    assert storage.read_channel_memory(config, new) == "remember this"
    assert storage.read_channel_memory(config, old) == "remember this"
    assert (config.workspace_path / "Channels" / new / "memories/2026-01-01.md").read_text() == "older memory"
    assert room_relocate.reconcile_mount(config) == 0


def test_reappeared_directory_merges_unique_entries_but_never_conflicts(migrated):
    config, old, new = migrated
    current = put(config, f"Channels/{new}/CHANNEL.md", "current")
    conflict = put(config, f"Channels/{old}/CHANNEL.md", "late write")
    put(config, f"Channels/{old}/memories/2026-01-01.md", "late dated memory")
    assert room_relocate.reconcile_mount(config) == 2
    assert current.read_text() == "current"
    assert conflict.read_text() == "late write"
    conflict.unlink()
    assert room_relocate.reconcile_mount(config) == 0
    assert (config.workspace_path / "Channels" / new / "memories/2026-01-01.md").read_text() == "late dated memory"
    assert not conflict.parent.exists()


@pytest.mark.parametrize("unsafe", ["room_link", "file_link", "fifo"])
def test_compatibility_does_not_bypass_channel_read_guards(migrated, unsafe):
    import os
    config, old, new = migrated
    secret = put(config, "Channels/other/CHANNEL.md", "other room")
    base = config.workspace_path / "Channels" / old
    if unsafe == "room_link":
        base.symlink_to(secret.parent, target_is_directory=True)
    else:
        base.mkdir()
        if unsafe == "file_link":
            (base / "CHANNEL.md").symlink_to(secret)
        else:
            os.mkfifo(base / "CHANNEL.md")
    assert storage.read_channel_memory(config, new) is None
    assert room_relocate.reconcile_mount(config) == 2
    assert secret.read_text() == "other room"


def test_missing_mount_fails_partial(migrated):
    config, old, new = migrated
    config.workspace_path = config.workspace_path / "absent"
    assert room_relocate.reconcile_mount(config) == 2
    assert not config.workspace_path.exists()


def test_task_guard_refuses_before_mount_changes(migrated):
    config, old, new = migrated
    original = put(config, f"Channels/{old}/CHANNEL.md", "notes")
    with db.get_db(config.db_path) as conn:
        ident = db.create_task(conn, user_id="alice", source_type="talk", prompt="hello")
        conn.execute("UPDATE tasks SET status='pending_confirmation' WHERE id=?", (ident,))
    assert room_relocate.reconcile_mount(config) == 1
    assert original.read_text() == "notes"


def test_files_rewrite_canonical_fields_preserve_prose_and_backup(migrated):
    config, old, new = migrated
    cron_text = ('notes above\n```toml\n[[jobs]]\nname = "job"\ncron = "0 8 * * *"\n'
                 'prompt = "Mention old-talk literally"\nroom = "old-talk"\n'
                 'target = "room:old-talk, talk:old-talk"\nbrain = "native"\n'
                 'once = true\n```\nnotes below\n')
    brief_text = ('intro\n```toml\n[[briefings]]\nname = "morning"\ncron = "0 8 * * *"\n'
                  'conversation_token = "old-talk"\noutput = "room:old-talk"\n'
                  '[briefings.components]\nweather = true\n```\nend\n')
    cron = userfile(config, "alice", "CRON.md", cron_text)
    brief = userfile(config, "alice", "BRIEFINGS.md", brief_text)
    assert room_relocate.reconcile_mount(config) == 0
    assert cron.read_text().startswith('notes above\n```toml\n')
    assert cron.read_text().endswith('```\nnotes below\n')
    from istota.cron_loader import load_cron_jobs
    job = load_cron_jobs(config, "alice")[0]
    assert (job.room, job.target, job.brain, job.once) == (new, f"room:{new}, talk:{old}", "native", True)
    assert job.prompt == "Mention old-talk literally"
    from istota.user_briefings import parse_briefings_md
    entry = parse_briefings_md(brief.read_text())[0]
    assert entry["conversation_token"] == new
    assert entry["output"] == f"room:{new}"
    assert entry["components"] == {"weather": True}
    backups = list((config.workspace_path / "Backups").rglob("*.md"))
    assert sorted(p.read_text() for p in backups) == sorted([cron_text, brief_text])
    assert room_relocate.reconcile_mount(config) == 0
    assert list((config.workspace_path / "Backups").rglob("*.md")) == backups


@pytest.mark.parametrize("bad", [
    '```toml\n[[jobs]]\nroom = "old-talk"\n',
    '```toml\n[[jobs]]\nname="lost"\nroom="old-talk"\n```\n',
    '```toml\n[[jobs]]\nname="job"\ncron="* * * * *"\nprompt="hi"\nroom="old-talk"\nfuture=true\n```\n',
    '```toml\n[[jobs]]\nname="job"\ncron="* * * * *"\nprompt="hi"\nroom="old-talk"\nenabled="false"\n```\n',
])
def test_malformed_file_refuses_user_before_either_file_is_written(migrated, bad):
    config, old, new = migrated
    cron = userfile(config, "alice", "CRON.md", bad)
    briefing_text = '```toml\n[[briefings]]\nname="morning"\ncron="0 8 * * *"\nconversation_token="old-talk"\n```\n'
    brief = userfile(config, "alice", "BRIEFINGS.md", briefing_text)
    bob = userfile(config, "bob", "BRIEFINGS.md", briefing_text)
    assert room_relocate.reconcile_mount(config) == 2
    assert cron.read_text() == bad
    assert brief.read_text() == briefing_text
    assert new in bob.read_text()


def test_dav_move_ambiguity_verifies_both_paths(migrated):
    from istota.nextcloud._http import OcsError
    config, old, new = migrated
    config.nextcloud.url = "https://cloud.example.com"
    config.nextcloud.username = "bot"
    put(config, f"Channels/{old}/CHANNEL.md", "stale mount")
    from istota.room_mount_reconcile import _move_dav
    err = OcsError("timeout", None, None, "MOVE")
    absent = OcsError("missing", 404, None, "PROPFIND")
    with patch("istota.room_mount_reconcile.dav_request", side_effect=err) as request, patch(
        "istota.room_mount_reconcile.dav.stat", side_effect=[absent, {"is_dir": True}]
    ) as check:
        _move_dav(config, f"/Channels/{old}", f"/Channels/{new}")
    assert check.call_count == 2
    assert request.call_args.kwargs["headers"]["Overwrite"] == "F"
    with patch("istota.room_mount_reconcile.dav_request", side_effect=err), patch(
        "istota.room_mount_reconcile.dav.stat", return_value={"is_dir": True}
    ):
        with pytest.raises(OcsError):
            _move_dav(config, f"/Channels/{old}", f"/Channels/{new}")


def test_cli_reconcile_uses_config_and_db_override(migrated):
    config, old, new = migrated
    put(config, f"Channels/{old}/CHANNEL.md", "notes")
    with patch("istota.config.load_config", return_value=config):
        assert room_relocate.main(["--reconcile-mount", "--db-path", str(config.db_path)]) == 0
    assert (config.workspace_path / "Channels" / new / "CHANNEL.md").read_text() == "notes"



def test_read_only_rename_is_partial_and_resumes(migrated):
    config, old, new = migrated
    original = put(config, f"Channels/{old}/CHANNEL.md", "notes")
    with patch("istota.room_mount_reconcile.os.rename", side_effect=OSError("read only")):
        assert room_relocate.reconcile_mount(config) == 2
    assert original.read_text() == "notes"
    assert room_relocate.reconcile_mount(config) == 0


def test_dry_run_leaves_directories_files_and_backups_untouched(migrated):
    config, old, new = migrated
    original = put(config, f"Channels/{old}/CHANNEL.md", "notes")
    before = sorted(str(p) for p in config.workspace_path.rglob("*"))
    assert room_relocate.reconcile_mount(config, dry_run=True) == 0
    assert original.read_text() == "notes"
    assert sorted(str(p) for p in config.workspace_path.rglob("*")) == before


def test_deleted_room_mapping_is_a_tombstone(migrated):
    config, old, new = migrated
    original = put(config, f"Channels/{old}/CHANNEL.md", "deleted notes")
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM room_members WHERE room_token=?", (new,))
        conn.execute("DELETE FROM rooms WHERE token=?", (new,))
    assert room_relocate.reconcile_mount(config) == 0
    assert original.exists()
    assert not (original.parent.parent / new).exists()
    assert storage.read_channel_memory(config, old) is None


def test_empty_canonical_notes_do_not_revive_legacy_notes(migrated):
    config, old, new = migrated
    put(config, f"Channels/{old}/CHANNEL.md", "obsolete")
    put(config, f"Channels/{new}/CHANNEL.md", "")
    assert storage.read_channel_memory(config, new) is None


def test_all_accepted_job_fields_and_multiline_prompt_survive(migrated):
    from istota.cron_loader import CronJob, generate_cron_md, load_cron_jobs
    config, old, new = migrated
    job = CronJob(name="job", cron="0 8 * * *", prompt="first\nsecond", room=old,
                  enabled=False, silent_unless_action=True, skip_log_channel=True,
                  once=True, model="example-model", effort="high", brain="native",
                  publish_shared_kv="public", publish_shared_kv_trusted=True)
    path = userfile(config, "alice", "CRON.md", generate_cron_md([job]))
    assert room_relocate.reconcile_mount(config) == 0
    job.room = new
    assert load_cron_jobs(config, "alice") == [job]
    assert "prompt_file" not in path.read_text()


@pytest.mark.parametrize("filename", ["CRON.md", "BRIEFINGS.md"])
def test_ambiguous_fences_are_refused(migrated, filename):
    config, old, new = migrated
    text = '```toml\n```\n\n```toml\nunknown="old-talk"\n```\n'
    path = userfile(config, "alice", filename, text)
    assert room_relocate.reconcile_mount(config) == 2
    assert path.read_text() == text


def test_backup_failure_prevents_rewrite(migrated):
    config, old, new = migrated
    from istota.cron_loader import CronJob, generate_cron_md
    text = generate_cron_md([CronJob(name="job", cron="0 8 * * *", prompt="hi", room=old)])
    path = userfile(config, "alice", "CRON.md", text)
    with patch("istota.room_mount_reconcile.storage.create_file_if_absent", return_value=False):
        assert room_relocate.reconcile_mount(config) == 2
    assert path.read_text() == text



@pytest.mark.parametrize("suffix", [
    '[[briefings]]\nname=" morning "\ncron="0 9 * * *"\n',
    'output="none"\n',
])
def test_briefing_import_ambiguity_refuses_rewrite(migrated, suffix):
    config, old, new = migrated
    text = ('```toml\n[[briefings]]\nname="morning"\ncron="0 8 * * *"\n'
            'conversation_token="old-talk"\n' + suffix + '```\n')
    path = userfile(config, "alice", "BRIEFINGS.md", text)
    assert room_relocate.reconcile_mount(config) == 2
    assert path.read_text() == text



def test_live_source_identity_never_becomes_another_rooms_alias(migrated):
    config, old, new = migrated
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, old, "bob", origin="web")
        assert db._canonical_room_token(conn, old, cross_surface=False) == old
    original = put(config, f"Channels/{old}/CHANNEL.md", "Bob private notes")
    from istota.cron_loader import CronJob, generate_cron_md
    text = generate_cron_md([CronJob(name="job", cron="0 8 * * *", prompt="hi", room=old)])
    cron = userfile(config, "bob", "CRON.md", text)
    assert storage.read_channel_memory(config, new) is None
    assert storage.read_channel_memory(config, old) == "Bob private notes"
    assert room_relocate.reconcile_mount(config) == 2
    assert original.read_text() == "Bob private notes"
    assert not (original.parent.parent / new).exists()
    assert cron.read_text() == text



@pytest.mark.parametrize("returncode,expected", [(1, None), (3, "old notes"), (4, "old notes")])
def test_rclone_fallback_requires_a_missing_file(migrated, returncode, expected):
    import subprocess
    config, old, new = migrated
    config.workspace_path = None
    with patch("istota.rclone_client.subprocess.run", side_effect=[
        subprocess.CompletedProcess([], returncode, stdout="", stderr="read failed"),
        subprocess.CompletedProcess([], 0, stdout="old notes", stderr=""),
    ]) as command:
        assert storage.read_channel_memory(config, new) == expected
    assert command.call_count == (1 if returncode == 1 else 2)
