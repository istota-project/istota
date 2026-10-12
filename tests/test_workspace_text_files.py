"""`workspace.text_files`: one lock and one revision for the user's text files.

The lost-update classes race a web-style save against the daemon's own
read-modify-write of the same file on two threads. The daemon side is paused
between its read and its write; without a shared lock the web save lands in
that window and the daemon's write then drops it.
"""

import threading

import pytest

from istota import cron_loader, tasks_file_poller
from istota.config import Config, EmailConfig, UserConfig
from istota.db import init_db
from istota.memory.curation.file_lock import MemoryMdLocked, deferred_lock_dir
from istota.storage import (
    USER_CONFIG_READ_CAP_BYTES,
    read_user_config_file,
    resolve_user_config_dir,
    write_regular_file,
)
from istota.workspace import text_files

TASKS_PATH = "/Users/alice/istota/config/TASKS.md"


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    init_db(db_path)
    (tmp_path / "mount/Users/alice/istota/config").mkdir(parents=True)
    return Config(
        db_path=db_path,
        workspace_path=tmp_path / "mount",
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(display_name="Alice")},
        email=EmailConfig(enabled=False),
    )


def _config_file(config, name):
    return resolve_user_config_dir(config, "alice") / name


def _web_save(config, name, content, expected):
    """What the web editor does for a bot config file."""
    path = _config_file(config, name)
    return text_files.save(
        config, "alice", path, content, expected,
        read=lambda: read_user_config_file(config, "alice", name),
        write=lambda text: write_regular_file(path, text),
        max_bytes=USER_CONFIG_READ_CAP_BYTES,
    )


# ---------------------------------------------------------------------------
# save: every outcome
# ---------------------------------------------------------------------------


class TestSaveOutcomes:
    def _save(self, config, content, expected, **kw):
        path = _config_file(config, "USER.md")
        kw.setdefault("read", lambda: read_user_config_file(config, "alice", "USER.md"))
        kw.setdefault("write", lambda text: write_regular_file(path, text))
        kw.setdefault("max_bytes", 1024)
        return text_files.save(config, "alice", path, content, expected, **kw)

    def test_ok_writes_and_returns_the_revision_of_what_was_written(self, config):
        out = self._save(config, "# Notes\n", text_files.revision(""))
        assert out.status == "ok"
        assert out.revision == text_files.revision("# Notes\n")
        assert _config_file(config, "USER.md").read_text() == "# Notes\n"

    def test_a_stale_revision_is_a_conflict_and_writes_nothing(self, config):
        _config_file(config, "USER.md").write_text("theirs\n")
        out = self._save(config, "mine\n", text_files.revision(""))
        assert out.status == "conflict"
        assert out.revision is None
        assert _config_file(config, "USER.md").read_text() == "theirs\n"

    def test_a_held_lock_is_locked(self, config, monkeypatch):
        path = _config_file(config, "USER.md")
        held = threading.Event()
        release = threading.Event()

        def _hold():
            with text_files.file_lock(config, "alice", path):
                held.set()
                release.wait(5)

        t = threading.Thread(target=_hold)
        t.start()
        try:
            assert held.wait(5)
            real = text_files.file_lock

            def _short(cfg, uid, p, *, timeout_seconds=5.0):
                return real(cfg, uid, p, timeout_seconds=0.2)

            monkeypatch.setattr(text_files, "file_lock", _short)
            out = self._save(config, "mine\n", text_files.revision(""))
        finally:
            release.set()
            t.join(5)
        assert out.status == "locked"
        assert not path.exists()

    def test_validator_errors_are_invalid_and_nothing_is_written(self, config):
        out = self._save(
            config, "bad\n", text_files.revision(""),
            validate=lambda text: ["line 1: bad"],
        )
        assert out.status == "invalid"
        assert out.errors == ("line 1: bad",)
        assert not _config_file(config, "USER.md").exists()

    def test_a_clean_validator_lets_the_save_through(self, config):
        out = self._save(
            config, "good\n", text_files.revision(""), validate=lambda text: [],
        )
        assert out.status == "ok"

    def test_over_the_cap_is_too_large(self, config):
        out = self._save(config, "x" * 1025, text_files.revision(""))
        assert out.status == "too_large"
        assert not _config_file(config, "USER.md").exists()

    def test_a_refused_write_is_failed(self, config, tmp_path):
        elsewhere = tmp_path / "elsewhere.md"
        elsewhere.write_text("")
        _config_file(config, "USER.md").symlink_to(elsewhere)
        out = self._save(
            config, "mine\n", text_files.revision(""), read=lambda: "",
        )
        assert out.status == "failed"
        assert elsewhere.read_text() == ""

    def test_an_oserror_from_write_is_failed_not_raised(self, config):
        def _boom(text):
            raise OSError("no space left on device")

        out = self._save(config, "mine\n", text_files.revision(""), write=_boom)
        assert out.status == "failed"

    def test_whitespace_only_reads_back_as_empty(self, config):
        assert text_files.revision("  \n") == text_files.revision("")


# ---------------------------------------------------------------------------
# The anchor every writer of a user's file takes
# ---------------------------------------------------------------------------


class TestLockAnchor:
    def test_is_the_curators_expression(self, config):
        # `sleep_cycle` spells `deferred_lock_dir(config.temp_dir / user_id)`.
        assert text_files.lock_dir(config, "alice") == deferred_lock_dir(
            config.temp_dir / "alice")

    def test_is_the_memory_skills_anchor_for_a_task_of_the_same_user(
        self, config, monkeypatch,
    ):
        from istota.skills import memory

        # The executor exports the per-user temp dir as `ISTOTA_DEFERRED_DIR`
        # for every task nothing is withheld from.
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(config.temp_dir / "alice"))
        target = memory.Target(_config_file(config, "USER.md"), memory._USER)
        assert memory._lock_dir(target) == text_files.lock_dir(config, "alice")

    def test_the_lock_excludes_a_second_holder(self, config):
        path = _config_file(config, "CRON.md")
        with text_files.file_lock(config, "alice", path):
            with pytest.raises(MemoryMdLocked):
                with text_files.file_lock(
                    config, "alice", path, timeout_seconds=0.1,
                ):
                    pass


# ---------------------------------------------------------------------------
# Lost updates between the web and the daemon
# ---------------------------------------------------------------------------


def _pause_daemon_after(monkeypatch, module, name, read_done, go):
    """Make the thread named "daemon" stop right after `module.name` returns."""
    real = getattr(module, name)

    def _paused(*args, **kwargs):
        result = real(*args, **kwargs)
        if threading.current_thread().name == "daemon":
            read_done.set()
            go.wait(2)
        return result

    monkeypatch.setattr(module, name, _paused)


def _race(daemon_fn, web_fn, read_done, go):
    """Run the daemon to its pause, then the web save, then let the daemon on."""
    results = {}
    daemon = threading.Thread(
        target=lambda: results.__setitem__("daemon", daemon_fn()), name="daemon",
    )
    daemon.start()
    assert read_done.wait(5), "the daemon side never reached its read"
    web = threading.Thread(
        target=lambda: results.__setitem__("web", web_fn()), name="web",
    )
    web.start()
    # Long enough for an unlocked save to finish; a locked one is still waiting.
    web.join(0.5)
    go.set()
    daemon.join(10)
    web.join(10)
    assert not daemon.is_alive() and not web.is_alive()
    return results


CRON = """# Scheduled jobs

```toml
[[jobs]]
name = "a"
cron = "0 9 * * *"
prompt = "morning"
```
"""

CRON_WITH_B = CRON.replace(
    "```\n", '\n[[jobs]]\nname = "b"\ncron = "0 18 * * *"\nprompt = "evening"\n```\n',
)


class TestCronMdLostUpdate:
    def test_a_web_save_inside_a_cron_disable_is_not_lost(self, config, monkeypatch):
        path = _config_file(config, "CRON.md")
        path.write_text(CRON)
        loaded = text_files.revision(CRON)
        read_done, go = threading.Event(), threading.Event()
        _pause_daemon_after(
            monkeypatch, cron_loader, "load_cron_document", read_done, go)

        results = _race(
            lambda: cron_loader.update_job_enabled_in_cron_md(
                config, "alice", "a", False),
            lambda: _web_save(config, "CRON.md", CRON_WITH_B, loaded),
            read_done, go,
        )

        assert results["daemon"] is True
        final = path.read_text()
        assert "enabled = false" in final
        web = results["web"]
        # Either the web edit is in the file, or the web was told it was not
        # written. Reporting `ok` for an edit that is gone is the lost update.
        if web.status == "ok":
            assert 'name = "b"' in final, "the web save was reported ok and lost"
        else:
            assert web.status == "conflict"

    def test_a_cron_removal_inside_a_web_save_window_is_not_lost(
        self, config, monkeypatch,
    ):
        path = _config_file(config, "CRON.md")
        path.write_text(CRON_WITH_B)
        loaded = text_files.revision(CRON_WITH_B)
        read_done, go = threading.Event(), threading.Event()
        _pause_daemon_after(
            monkeypatch, cron_loader, "load_cron_document", read_done, go)
        edited = CRON_WITH_B.replace('prompt = "morning"', 'prompt = "dawn"')

        results = _race(
            lambda: cron_loader.remove_job_from_cron_md(config, "alice", "b"),
            lambda: _web_save(config, "CRON.md", edited, loaded),
            read_done, go,
        )

        assert results["daemon"] is True
        final = path.read_text()
        assert 'name = "b"' not in final
        if results["web"].status == "ok":
            assert 'prompt = "dawn"' in final, "the web save was reported ok and lost"
        else:
            assert results["web"].status == "conflict"

    def test_a_restore_does_not_overwrite_jobs_saved_since_the_template_was_read(
        self, config,
    ):
        """`migrate_db_jobs_to_file(overwrite=True)` is the sync's restore.

        The sync decided from a read outside the lock that the file is the
        seeded template. If the user has saved jobs since, the restore must
        not write the table over them.
        """
        from istota import db

        path = _config_file(config, "CRON.md")
        path.write_text(CRON)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt)"
                " VALUES ('alice', 'old', '0 1 * * *', 'from the table')",
            )
            conn.commit()
            assert cron_loader.migrate_db_jobs_to_file(
                conn, config, "alice", overwrite=True) is False
        assert path.read_text() == CRON


class TestTasksMdLostUpdate:
    def test_a_web_save_inside_a_poll_is_not_lost(self, config, monkeypatch):
        path = _config_file(config, "TASKS.md")
        original = "- [ ] first\n"
        path.write_text(original)
        read_done, go = threading.Event(), threading.Event()
        _pause_daemon_after(
            monkeypatch, tasks_file_poller, "_read_tasks_file", read_done, go)

        results = _race(
            lambda: tasks_file_poller.poll_user_tasks_file(config, "alice", TASKS_PATH),
            lambda: _web_save(
                config, "TASKS.md", original + "- [ ] second\n",
                text_files.revision(original)),
            read_done, go,
        )

        assert len(results["daemon"]) == 1
        final = path.read_text()
        assert "- [~] first..." in final
        if results["web"].status == "ok":
            assert "second" in final, "the web save was reported ok and lost"
        else:
            assert results["web"].status == "conflict"

    def test_a_web_save_inside_a_completion_is_not_lost(self, config, monkeypatch):
        from istota import db

        path = _config_file(config, "TASKS.md")
        path.write_text("- [ ] first\n")
        (task_id,) = tasks_file_poller.poll_user_tasks_file(config, "alice", TASKS_PATH)
        polled = path.read_text()
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)

        read_done, go = threading.Event(), threading.Event()
        _pause_daemon_after(
            monkeypatch, tasks_file_poller, "_read_tasks_file", read_done, go)

        results = _race(
            lambda: tasks_file_poller.handle_tasks_file_completion(
                config, task, True, "done"),
            lambda: _web_save(
                config, "TASKS.md", polled + "- [ ] second\n",
                text_files.revision(polled)),
            read_done, go,
        )

        final = path.read_text()
        assert "- [x]" in final and "Result: done" in final
        if results["web"].status == "ok":
            assert "second" in final, "the web save was reported ok and lost"
        else:
            assert results["web"].status == "conflict"

    def test_a_held_lock_skips_the_file_for_this_pass(self, config, monkeypatch):
        path = _config_file(config, "TASKS.md")
        path.write_text("- [ ] first\n")
        real = text_files.file_lock

        def _short(cfg, uid, p, *, timeout_seconds=5.0):
            return real(cfg, uid, p, timeout_seconds=0.1)

        monkeypatch.setattr(text_files, "file_lock", _short)
        with real(config, "alice", path):
            assert tasks_file_poller.poll_user_tasks_file(
                config, "alice", TASKS_PATH) == []
        assert path.read_text() == "- [ ] first\n"
        # The next pass picks it up.
        assert len(tasks_file_poller.poll_user_tasks_file(
            config, "alice", TASKS_PATH)) == 1
