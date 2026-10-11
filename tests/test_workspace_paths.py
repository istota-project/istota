"""`maintenance.workspace_paths`: the old host's paths in users' workspace files."""

from __future__ import annotations

import os

import pytest

from istota.maintenance import workspace_paths as wp

PREFIXES = [("/srv/mount/nextcloud/content", "/mnt/shared"), ("/srv/app/zorg/tmp", "/data/tmp")]
OLD_HOME = "/srv/app/zorg"


def _rules():
    return wp.compile_rules(PREFIXES, OLD_HOME)


class TestRewritingText:
    def test_the_old_venvs_binaries_become_bare_commands(self):
        text = (
            'command = "/srv/app/zorg/istota/.venv/bin/python /srv/mount/nextcloud/content/Users/a/x.py"\n'
            'command = "/srv/app/zorg/.venv/bin/istota-skill location import"\n'
        )
        new, count = wp.rewrite_text(text, _rules())
        assert new == (
            'command = "python /mnt/shared/Users/a/x.py"\n'
            'command = "istota-skill location import"\n'
        )
        assert count == 3

    def test_a_prefix_matches_only_at_a_path_boundary(self):
        new, count = wp.rewrite_text("/srv/app/zorg/tmpfiles and /srv/app/zorg/tmp/a", _rules())
        assert new == "/srv/app/zorg/tmpfiles and /data/tmp/a"
        assert count == 1

    def test_a_path_in_backticks_and_at_line_end(self):
        new, _ = wp.rewrite_text("ls `/srv/app/zorg/tmp`\nRUNDIR=/srv/app/zorg/tmp\n", _rules())
        assert new == "ls `/data/tmp`\nRUNDIR=/data/tmp\n"


@pytest.fixture
def workspace(tmp_path):
    base = tmp_path / "Users" / "alice" / "zorg"
    (base / "config").mkdir(parents=True)
    (base / "scripts" / "prompts").mkdir(parents=True)
    (base / "config" / "CRON.md").write_text(
        'command = "/srv/app/zorg/istota/.venv/bin/python /srv/mount/nextcloud/content/Users/alice/zorg/scripts/a.py"\n'
    )
    (base / "scripts" / "prompts" / "digest.txt").write_text("RUNDIR=/srv/app/zorg/tmp/alice/news\n")
    (base / "scripts" / "notes.txt").write_text("nothing here\n")
    (base / "scripts" / "image.png").write_bytes(b"/srv/app/zorg/tmp")
    return tmp_path


class TestRunningOverAWorkspace:
    def test_it_rewrites_and_keeps_a_copy_of_each_changed_file(self, workspace):
        base = workspace / "Users" / "alice" / "zorg"
        result = wp.run(workspace, "zorg", PREFIXES, OLD_HOME)
        assert sorted(p.name for p, _ in result.changed) == ["CRON.md", "digest.txt"]
        assert (base / "config" / "CRON.md").read_text() == (
            'command = "python /mnt/shared/Users/alice/zorg/scripts/a.py"\n'
        )
        assert "/srv/app/zorg" in (base / "config" / "CRON.md.pre-migration").read_text()
        assert not (base / "scripts" / "notes.txt.pre-migration").exists()
        assert (base / "scripts" / "image.png").read_bytes() == b"/srv/app/zorg/tmp"

    def test_a_dry_run_writes_nothing(self, workspace):
        cron = workspace / "Users" / "alice" / "zorg" / "config" / "CRON.md"
        before = cron.read_text()
        result = wp.run(workspace, "zorg", PREFIXES, OLD_HOME, dry_run=True)
        assert len(result.changed) == 2
        assert cron.read_text() == before
        assert not cron.with_name("CRON.md.pre-migration").exists()

    def test_a_second_run_keeps_the_first_copy(self, workspace):
        cron = workspace / "Users" / "alice" / "zorg" / "config" / "CRON.md"
        original = cron.read_text()
        wp.run(workspace, "zorg", PREFIXES, OLD_HOME)
        assert wp.run(workspace, "zorg", PREFIXES, OLD_HOME).changed == []
        assert cron.with_name("CRON.md.pre-migration").read_text() == original

    def test_an_unmapped_old_home_path_is_reported_not_guessed(self, workspace):
        prompt = workspace / "Users" / "alice" / "zorg" / "scripts" / "prompts" / "check.txt"
        prompt.write_text("Never modify a file under /srv/app/zorg/istota.\n")
        result = wp.run(workspace, "zorg", PREFIXES, OLD_HOME)
        assert prompt.read_text() == "Never modify a file under /srv/app/zorg/istota.\n"
        assert [(p.name, n) for p, n, _ in result.leftovers] == [("check.txt", 1)]

    def test_a_planted_link_is_not_followed(self, workspace, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("/srv/app/zorg/tmp/x\n")
        os.symlink(outside, workspace / "Users" / "alice" / "zorg" / "config" / "LINK.md")
        os.symlink(tmp_path, workspace / "Users" / "alice" / "zorg" / "loop")
        wp.run(workspace, "zorg", PREFIXES, OLD_HOME)
        assert outside.read_text() == "/srv/app/zorg/tmp/x\n"


class TestTheCommand:
    def test_the_map_comes_from_the_command_line_without_a_record(self, workspace, tmp_path, monkeypatch, capsys):
        config = tmp_path / "config.toml"
        config.write_text(
            f'bot_name = "Zorg"\nworkspace_path = "{workspace}"\nnextcloud_mount_path = "{workspace}"\n'
            f'db_path = "{tmp_path / "istota.db"}"\n'
        )
        rc = wp.main([
            "-c", str(config), "--old-home", OLD_HOME,
            "--map", "/srv/mount/nextcloud/content=/mnt/shared", "--map", "/srv/app/zorg/tmp=/data/tmp",
        ])
        assert rc == 0
        assert "workspace paths: rewrote 2 file(s)" in capsys.readouterr().out
        cron = workspace / "Users" / "alice" / "zorg" / "config" / "CRON.md"
        assert "/mnt/shared" in cron.read_text()

    def test_nothing_to_map_is_a_refusal(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(f'workspace_path = "{tmp_path}"\ndb_path = "{tmp_path / "istota.db"}"\n')
        assert wp.main(["-c", str(config)]) == 1
