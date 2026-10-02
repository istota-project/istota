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


def test_missing_mount_refuses_so_the_deploy_retries(migrated):
    # A partial counts as deployed (ISSUE-588); an outage must not.
    config, old, new = migrated
    config.workspace_path = config.workspace_path / "absent"
    assert room_relocate.reconcile_mount(config) == 1
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
    # ISSUE-596: a refused prompt_file is not a skipped entry, and still must
    # not be rendered out of the file by a rewrite.
    '```toml\n[[jobs]]\nname="job"\ncron="* * * * *"\nprompt="hi"\nroom="old-talk"\n'
    '[[jobs]]\nname="theirs"\ncron="* * * * *"\nprompt_file="/Users/bob/x.txt"\n```\n',
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
    with patch("istota.lib.rclone_client.subprocess.run", side_effect=[
        subprocess.CompletedProcess([], returncode, stdout="", stderr="read failed"),
        subprocess.CompletedProcess([], 0, stdout="old notes", stderr=""),
    ]) as command:
        assert storage.read_channel_memory(config, new) == expected
    assert command.call_count == (1 if returncode == 1 else 2)


def test_existing_vector_index_does_not_refuse_the_sweep(migrated, monkeypatch):
    # The inventory reads table_info on every table, a vec0 one included, so
    # the sweep's own connection needs the extension the database half loads.
    sqlite_vec = pytest.importorskip("sqlite_vec")
    monkeypatch.setattr("istota.memory.search._vec_available", None)
    config, old, new = migrated
    with db.get_db(config.db_path) as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("CREATE VIRTUAL TABLE memory_chunks_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[384])")
    put(config, f"Channels/{old}/CHANNEL.md", "remember this")
    assert room_relocate.reconcile_mount(config) == 0
    assert (config.workspace_path / "Channels" / new / "CHANNEL.md").read_text() == "remember this"


DAY = "memories/2026-10-01.md"


def _backups(config):
    root = config.workspace_path / "Backups" / "room-token-migration"
    # Drop the date directory: the sweep stamps today's date, the test does not.
    return {p.relative_to(root).parts[1:]: p.read_text() for p in root.rglob("*.md")}


def test_mint_day_dated_memory_merges_older_content_first(migrated):
    """ISSUE-588: a phone room's alias and its minted room both wrote today."""
    config, old, new = migrated
    alias = put(config, f"Channels/{old}/{DAY}", "before the mint\n")
    target = put(config, f"Channels/{new}/{DAY}", "after the mint\n")
    assert room_relocate.reconcile_mount(config) == 0
    assert target.read_text() == "before the mint\n\nafter the mint\n"
    assert not alias.exists()
    assert _backups(config) == {
        ("Channels", old, "memories", "2026-10-01.md"): "before the mint\n",
        ("Channels", new, "before", old, "memories", "2026-10-01.md"): "after the mint\n",
    }
    assert room_relocate.reconcile_mount(config) == 0
    assert target.read_text() == "before the mint\n\nafter the mint\n"


def test_dated_memory_merge_resumes_after_the_write(migrated):
    config, old, new = migrated
    alias = put(config, f"Channels/{old}/{DAY}", "before\n")
    target = put(config, f"Channels/{new}/{DAY}", "before\n\nafter\n")
    assert room_relocate.reconcile_mount(config) == 0
    assert target.read_text() == "before\n\nafter\n"
    assert not alias.exists()


def test_alias_text_that_only_prefixes_the_target_is_still_merged(migrated):
    config, old, new = migrated
    alias = put(config, f"Channels/{old}/{DAY}", "- Discussed the launch\n")
    target = put(config, f"Channels/{new}/{DAY}", "- Discussed the launch date\n")
    assert room_relocate.reconcile_mount(config) == 0
    assert target.read_text() == "- Discussed the launch\n\n- Discussed the launch date\n"
    assert not alias.exists()
    assert len(_backups(config)) == 2


def test_dated_memory_merge_that_cannot_write_keeps_both(migrated):
    config, old, new = migrated
    alias = put(config, f"Channels/{old}/{DAY}", "before\n")
    target = put(config, f"Channels/{new}/{DAY}", "after\n")
    with patch("istota.room_mount_reconcile.storage.write_regular_file", return_value=False):
        assert room_relocate.reconcile_mount(config) == 2
    assert alias.read_text() == "before\n"
    assert target.read_text() == "after\n"


def test_dated_memory_dry_run_writes_nothing(migrated):
    config, old, new = migrated
    alias = put(config, f"Channels/{old}/{DAY}", "before\n")
    target = put(config, f"Channels/{new}/{DAY}", "after\n")
    assert room_relocate.reconcile_mount(config, dry_run=True) == 0
    assert alias.read_text() == "before\n"
    assert target.read_text() == "after\n"
    assert not (config.workspace_path / "Backups").exists()


@pytest.mark.parametrize("name", [
    "memories/notes.md", "memories/2026-02-30.md", "memories/2026-10-01.txt",
    "2026-10-01.md", "memories/x/2026-10-01.md",
])
def test_other_same_name_collisions_still_refuse(migrated, name):
    config, old, new = migrated
    alias = put(config, f"Channels/{old}/{name}", "before\n")
    target = put(config, f"Channels/{new}/{name}", "after\n")
    assert room_relocate.reconcile_mount(config) == 2
    assert alias.read_text() == "before\n"
    assert target.read_text() == "after\n"
    assert not (config.workspace_path / "Backups").exists()


class FakeDav:
    """Just enough of a WebDAV server for the merge: files and collections."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/", "/Channels"}

    def put_file(self, path, text):
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            self.dirs.add("/" + "/".join(parts[:i]))
        self.files[path] = text.encode()

    def etag(self, path):
        return str(len(self.files[path])) + "-" + str(sum(self.files[path]))

    def stat(self, config, path):
        from istota.nextcloud._http import OcsError
        if path in self.dirs:
            return {"path": path, "is_dir": True, "size": 0, "etag": "d"}
        if path in self.files:
            return {"path": path, "is_dir": False, "size": len(self.files[path]), "etag": self.etag(path)}
        raise OcsError("missing", 404, None, path)

    def list_dir(self, config, path):
        names = {p[len(path) + 1:].split("/")[0] for p in [*self.files, *self.dirs]
                 if p.startswith(path + "/")}
        return [{"name": n, "path": f"{path}/{n}"} for n in sorted(names)]

    def request(self, config, method, url, *, content=None, headers=None, **_):
        from types import SimpleNamespace
        from istota.nextcloud._http import OcsError
        headers = headers or {}
        if method == "GET":
            if url not in self.files:
                raise OcsError("missing", 404, None, url)
            return SimpleNamespace(content=self.files[url], status_code=200)
        if method == "PUT":
            match = headers.get("If-Match")
            if match is not None and (url not in self.files or match.strip('"') != self.etag(url)):
                raise OcsError("precondition", 412, None, url)
            self.files[url] = content
            return SimpleNamespace(content=b"", status_code=204)
        if method == "DELETE":
            if self.files.pop(url, None) is None:
                raise OcsError("missing", 404, None, url)
            return SimpleNamespace(content=b"", status_code=204)
        if method == "MOVE":
            target = headers["Destination"]
            moved = {p: v for p, v in self.files.items() if p == url or p.startswith(url + "/")}
            for p, v in moved.items():
                del self.files[p]
                self.files[target + p[len(url):]] = v
            for d in [d for d in self.dirs if d == url or d.startswith(url + "/")]:
                self.dirs.discard(d)
                self.dirs.add(target + d[len(url):])
            return SimpleNamespace(content=b"", status_code=201)
        raise AssertionError(method)


@pytest.fixture
def dav_server(migrated):
    config, old, new = migrated
    config.nextcloud.url = "https://cloud.example.com"
    config.nextcloud.username = "bot"
    server = FakeDav()
    with patch("istota.room_mount_reconcile.dav.stat", side_effect=server.stat), patch(
        "istota.room_mount_reconcile.dav.list_dir", side_effect=server.list_dir
    ), patch("istota.room_mount_reconcile.dav_request", side_effect=server.request), patch(
        "istota.room_mount_reconcile.dav_files_url", side_effect=lambda config, path: path
    ):
        yield config, old, new, server


def test_dav_mint_day_dated_memory_merges_older_content_first(dav_server):
    config, old, new, server = dav_server
    server.put_file(f"/Channels/{old}/{DAY}", "before the mint\n")
    server.put_file(f"/Channels/{new}/{DAY}", "after the mint\n")
    server.put_file(f"/Channels/{old}/CHANNEL.md", "notes")
    assert room_relocate.reconcile_mount(config) == 0
    assert server.files[f"/Channels/{new}/{DAY}"] == b"before the mint\n\nafter the mint\n"
    assert f"/Channels/{old}/{DAY}" not in server.files
    assert server.files[f"/Channels/{new}/CHANNEL.md"] == b"notes"
    assert _backups(config) == {
        ("Channels", old, "memories", "2026-10-01.md"): "before the mint\n",
        ("Channels", new, "before", old, "memories", "2026-10-01.md"): "after the mint\n",
    }
    assert room_relocate.reconcile_mount(config) == 0


def test_dav_other_collision_still_refuses(dav_server):
    config, old, new, server = dav_server
    server.put_file(f"/Channels/{old}/memories/notes.md", "before\n")
    server.put_file(f"/Channels/{new}/memories/notes.md", "after\n")
    assert room_relocate.reconcile_mount(config) == 2
    assert server.files[f"/Channels/{old}/memories/notes.md"] == b"before\n"
    assert server.files[f"/Channels/{new}/memories/notes.md"] == b"after\n"


def test_dav_merge_refuses_a_target_that_changed_under_it(dav_server):
    config, old, new, server = dav_server
    server.put_file(f"/Channels/{old}/{DAY}", "before\n")
    server.put_file(f"/Channels/{new}/{DAY}", "after\n")
    real = server.request

    def late_write(config, method, url, **kw):
        if method == "PUT":
            server.files[url] = b"a late write\n"
        return real(config, method, url, **kw)

    with patch("istota.room_mount_reconcile.dav_request", side_effect=late_write):
        assert room_relocate.reconcile_mount(config) == 2
    assert server.files[f"/Channels/{new}/{DAY}"] == b"a late write\n"
    assert server.files[f"/Channels/{old}/{DAY}"] == b"before\n"
