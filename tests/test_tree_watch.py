"""The tree watcher (#939) against an in-memory Nextcloud tree.

The tree is `tests/support/tree_watch_fake.FakeTree`; the rc is replaced at
`rclone_rc.forget_dirs` / `refresh_dirs`, its two entry points.
"""

from __future__ import annotations

import pytest

from istota.config import Config, NextcloudConfig, UserConfig
from istota.nextcloud import tree_watch

from .support import tree_watch_fake


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="istota",
                                  app_password="x"),
        users={"alice": UserConfig(), "bob": UserConfig()},
        workspace_path=tmp_path / "mount",
        workspace_mount_path=tmp_path / "mount",
        bot_name="istota",
    )


@pytest.fixture
def tree(config, monkeypatch):
    fake = tree_watch_fake.install(monkeypatch)
    yield fake
    tree_watch.reset()


ALICE_CONFIG = "/Users/alice/istota/config"
ALICE_SCRIPTS = "/Users/alice/istota/scripts"
BOB_CONFIG = "/Users/bob/istota/config"


def _settle(config, tree):
    tree_watch.run_pass(config, now=0)
    tree.calls.clear()
    tree.forgotten.clear()
    tree.refreshed.clear()


class TestAPass:
    def test_with_nothing_changed_a_pass_is_one_request(self, config, tree):
        _settle(config, tree)
        result = tree_watch.run_pass(config, now=1)
        assert tree.calls == [("/", 0)]
        assert result.changed == [] and result.requests == 1

    def test_the_first_pass_observes_every_watched_path_and_freshens_the_mount(self, config, tree):
        result = tree_watch.run_pass(config, now=0)
        assert result.error == ""
        snap = tree_watch.snapshot([ALICE_CONFIG, ALICE_SCRIPTS, BOB_CONFIG, "/"])
        assert None not in snap
        # The mount outlived the daemon; what it cached before is unknown.
        assert tree.forgotten == [["*"]]

    def test_a_changed_directory_off_the_watched_paths_is_dropped_from_the_mount(
        self, config, tree,
    ):
        """Review finding: with a ten-minute directory cache, a user's upload
        into their inbox must not wait out the cache."""
        tree.mkdir("/Users/alice/inbox")
        _settle(config, tree)
        tree.write("/Users/alice/inbox/report.pdf")
        result = tree_watch.run_pass(config, now=1)
        assert "/Users/alice/inbox" in result.forgotten
        assert ["Users/alice/inbox"] in tree.forgotten

        tree.mkdir("/Channels/room")
        _settle(config, tree)
        tree.write("/Channels/room/notes.md")
        result = tree_watch.run_pass(config, now=2)
        assert result.forgotten == ["/Channels"]

    def test_an_edited_file_refreshes_its_listed_directory(self, config, tree):
        tree.write("/Users/alice/notes.md")
        _settle(config, tree)
        tree.write("/Users/alice/notes.md")
        result = tree_watch.run_pass(config, now=1)
        assert "/Users/alice" in result.refreshed

    def test_a_mark_during_a_pass_survives_it(self, config, tree):
        _settle(config, tree)
        tree.write("/Channels/x.md")
        before = tree_watch.snapshot([ALICE_CONFIG])
        real = tree.propfind

        def marking(config, path, depth):
            tree_watch.mark_changed(ALICE_CONFIG)
            return real(config, path, depth)

        tree_watch._propfind = marking
        try:
            tree_watch.run_pass(config, now=1)
        finally:
            tree_watch._propfind = real
        assert tree_watch.snapshot([ALICE_CONFIG]) != before

    def test_an_edit_goes_down_only_the_changed_branch(self, config, tree):
        _settle(config, tree)
        before = tree_watch.snapshot([ALICE_CONFIG, BOB_CONFIG, ALICE_SCRIPTS])
        tree.write(f"{ALICE_CONFIG}/TASKS.md")

        result = tree_watch.run_pass(config, now=1)

        after = tree_watch.snapshot([ALICE_CONFIG, BOB_CONFIG, ALICE_SCRIPTS])
        assert after[0] != before[0]
        assert after[1:] == before[1:]
        listed = [p for p, depth in tree.calls if depth == 1]
        assert listed == ["/", "/Users", "/Users/alice", "/Users/alice/istota"]
        assert result.forgotten == [ALICE_CONFIG]
        assert tree.forgotten == [["Users/alice/istota/config"]]

    def test_a_write_off_the_watched_paths_lists_nothing_below_the_bot_dir(self, config, tree):
        _settle(config, tree)
        tree.write("/Users/alice/istota/memories/2026-11-07.md")
        result = tree_watch.run_pass(config, now=1)
        assert result.changed == []
        assert [p for p, depth in tree.calls if depth == 1] == [
            "/", "/Users", "/Users/alice", "/Users/alice/istota",
        ]
        assert result.forgotten == ["/Users/alice/istota/memories"]

    def test_the_roots_generation_moves_on_a_new_entry_only(self, config, tree):
        _settle(config, tree)
        root = tree_watch.snapshot(["/"])
        tree.mkdir("/Channels/room")
        tree_watch.run_pass(config, now=1)
        tree.write("/Channels/room/notes.md")
        tree_watch.run_pass(config, now=1)
        assert tree_watch.snapshot(["/"]) == root

        tree.write("/report.pdf")
        result = tree_watch.run_pass(config, now=2)
        assert tree_watch.snapshot(["/"]) != root
        assert "/" in result.refreshed
        names = {e["name"] for e in tree_watch.root_listing()}
        assert "report.pdf" in names

    def test_a_removed_watched_directory_moves_its_generation(self, config, tree):
        _settle(config, tree)
        before = tree_watch.snapshot([ALICE_SCRIPTS])
        tree.remove(ALICE_SCRIPTS)
        tree_watch.run_pass(config, now=1)
        assert tree_watch.snapshot([ALICE_SCRIPTS]) != before

    def test_a_watched_directory_created_later_is_seen(self, config, tree):
        tree.remove("/Users/bob/istota")
        _settle(config, tree)
        before = tree_watch.snapshot([BOB_CONFIG])
        tree.mkdir("/Users/bob/istota")
        tree.mkdir(BOB_CONFIG)
        tree_watch.run_pass(config, now=1)
        assert tree_watch.snapshot([BOB_CONFIG]) != before


class TestConsumers:
    def test_two_consumers_of_one_directory_each_see_the_change(self, config, tree):
        _settle(config, tree)
        for key in ("tasks:alice", "cron:alice"):
            run, snap = tree_watch.should_run(key, [ALICE_CONFIG], now=1)
            assert run
            tree_watch.record_run(key, snap, now=1)
            assert tree_watch.should_run(key, [ALICE_CONFIG], now=2)[0] is False

        tree.write(f"{ALICE_CONFIG}/CRON.md")
        tree_watch.run_pass(config, now=3)

        run, snap = tree_watch.should_run("tasks:alice", [ALICE_CONFIG], now=4)
        assert run
        tree_watch.record_run("tasks:alice", snap, now=4)
        assert tree_watch.should_run("cron:alice", [ALICE_CONFIG], now=4)[0] is True

    def test_a_consumer_runs_unconditionally_every_hour(self, config, tree):
        _settle(config, tree)
        run, snap = tree_watch.should_run("k", [ALICE_CONFIG], now=10)
        tree_watch.record_run("k", snap, now=10)
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=10 + tree_watch.HEAL_SECONDS - 1)[0] is False
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=10 + tree_watch.HEAL_SECONDS)[0] is True

    def test_mark_changed_runs_the_consumer_again(self, config, tree):
        _settle(config, tree)
        run, snap = tree_watch.should_run("k", [ALICE_CONFIG], now=1)
        tree_watch.record_run("k", snap, now=1)
        tree_watch.mark_changed(ALICE_CONFIG)
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=2)[0] is True

    def test_before_any_pass_every_consumer_runs(self, config, tree):
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=1) == (True, None)
        tree_watch.record_run("k", None, now=1)
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=2)[0] is True


class TestOffMeansAlwaysChanged:
    def test_a_failed_pass_turns_the_answers_off_and_commits_nothing(self, config, tree):
        _settle(config, tree)
        run, snap = tree_watch.should_run("k", [ALICE_CONFIG], now=1)
        tree_watch.record_run("k", snap, now=1)
        tree.fail = True
        result = tree_watch.run_pass(config, now=2)
        assert result.error == "OSError"
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=3)[0] is True

        tree.fail = False
        tree.write(f"{ALICE_CONFIG}/TASKS.md")
        tree_watch.run_pass(config, now=4)
        assert tree_watch.snapshot([ALICE_CONFIG]) != snap

    def test_a_root_on_external_storage_is_not_trusted(self, config, tree):
        tree.root_mount_type = "external"
        result = tree_watch.run_pass(config, now=0)
        assert result.error == "untrusted_storage"
        assert tree_watch.snapshot([ALICE_CONFIG]) is None
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=1)[0] is True
        # With no watcher the mount's cache is dropped every pass, which is
        # what its old 30 s expiry did.
        tree_watch.run_pass(config, now=2)
        assert tree.forgotten == [["*"], ["*"]]

    def test_without_the_rc_a_change_moves_again_once_the_cache_expired(self, config, tree):
        _settle(config, tree)
        tree.rc_ok = False
        tree.write(f"{ALICE_CONFIG}/TASKS.md")
        tree_watch.run_pass(config, now=100)
        run, snap = tree_watch.should_run("k", [ALICE_CONFIG], now=101)
        tree_watch.record_run("k", snap, now=101)

        tree_watch.run_pass(config, now=100 + tree_watch.DIR_CACHE_SECONDS - 1)
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=102)[0] is False
        tree_watch.run_pass(config, now=100 + tree_watch.DIR_CACHE_SECONDS)
        assert tree_watch.should_run("k", [ALICE_CONFIG], now=103)[0] is True


class TestRegisteredDirectories:
    def test_a_registered_directory_is_followed(self, config, tree):
        tree.mkdir("/Users/alice/notes")
        tree.write("/Users/alice/notes/prompt.md")
        _settle(config, tree)
        tree_watch.watch("cron:alice", ["/Users/alice/notes"])
        tree_watch.run_pass(config, now=1)
        before = tree_watch.snapshot(["/Users/alice/notes"])
        assert before == (1,)

        tree.write("/Users/alice/notes/prompt.md")
        result = tree_watch.run_pass(config, now=2)
        assert tree_watch.snapshot(["/Users/alice/notes"]) != before
        assert "/Users/alice/notes" in result.forgotten
