"""ISSUE-632: re-running a failed scheduled job or briefing by hand.

`!retry #N` on a `scheduled` or `briefing` task queues a fresh occurrence of the
job's *current* definition, linked to the failed one, through the same task
builder the cron tick uses.
"""

import pytest

from istota import db
from istota.commands import CommandContext, cmd_resume, cmd_retry, retry_task
from istota.config import (
    BrainConfig,
    BriefingConfig,
    Config,
    NativeBrainConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.credentials.broker import grants
from istota.scheduler import run_briefing_now, run_scheduled_job_now


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    cfg = Config()
    cfg.db_path = path
    cfg.talk = TalkConfig(enabled=True, bot_username="istota")
    cfg.users = {
        "alice": UserConfig(briefings=[
            BriefingConfig(name="morning", cron="0 8 * * *",
                           conversation_token="room1", output="talk"),
        ]),
        "bob": UserConfig(),
    }
    cfg.scheduler = SchedulerConfig()
    cfg.brain = BrainConfig(kind="native", native=NativeBrainConfig())
    return cfg


def _ctx(config, conn, *, args, user_id="alice", token="room1"):
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface="talk",
    )


def _job(conn, *, name="digest", prompt="Write the digest", user_id="alice"):
    cur = conn.execute(
        "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, "
        "conversation_token, output_target, enabled) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (user_id, name, "0 7 * * *", prompt, "room1", "talk"),
    )
    return cur.lastrowid


def _failed_job_task(conn, job_id, *, prompt="Write the digest", status="failed"):
    tid = db.create_task(
        conn, prompt=prompt, user_id="alice", source_type="scheduled",
        conversation_token="room1", scheduled_job_id=job_id, queue="background",
    )
    db.update_task_status(conn, tid, status, error="attempts exhausted")
    # Old enough that the cooldown is not what the test is about.
    conn.execute(
        "UPDATE tasks SET created_at = datetime('now', '-1 hour'), "
        "attempt_count = 2 WHERE id = ?", (tid,),
    )
    return tid


def _failed_briefing_task(conn, *, name="morning"):
    tid = db.create_task(
        conn, prompt=f"Generate the '{name}' briefing.", user_id="alice",
        source_type="briefing", conversation_token="room1", queue="background",
        briefing_name=name, output_target="talk",
    )
    db.update_task_status(conn, tid, "failed", error="attempts exhausted")
    conn.execute(
        "UPDATE tasks SET created_at = datetime('now', '-1 hour') WHERE id = ?", (tid,),
    )
    return tid


class TestRetryRoutesToRunNow:
    async def test_exhausted_scheduled_task_queues_the_current_definition(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn, prompt="old prompt")
            old = _failed_job_task(conn, job_id, prompt="old prompt")
            # The job is fixed after the failure; the re-run runs the fix.
            conn.execute(
                "UPDATE scheduled_jobs SET prompt = 'fixed prompt' WHERE id = ?",
                (job_id,),
            )
            out = await cmd_retry(_ctx(config, conn, args=f"#{old}"))
            new = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ?", (old,),
            ).fetchone()
        assert new is not None, out
        assert f"#{new['id']}" in out
        assert new["prompt"] == "fixed prompt"
        assert new["source_type"] == "scheduled"
        assert new["scheduled_job_id"] == job_id
        assert new["queue"] == "background"
        assert new["status"] == "pending"
        assert new["attempt_count"] == 0

    async def test_briefing_task_queues_a_fresh_briefing(self, config):
        with db.get_db(config.db_path) as conn:
            old = _failed_briefing_task(conn)
            out = await cmd_retry(_ctx(config, conn, args=f"#{old}"))
            new = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ?", (old,),
            ).fetchone()
        assert new is not None, out
        assert new["source_type"] == "briefing"
        assert new["briefing_name"] == "morning"
        assert new["output_target"] == "talk"
        assert "briefing 'morning'" in out

    async def test_resume_on_a_job_runs_it_from_the_start(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            out = await cmd_resume(_ctx(config, conn, args=f"#{old}"))
            new = conn.execute(
                "SELECT prompt FROM tasks WHERE parent_task_id = ?", (old,),
            ).fetchone()
        assert "from the start" in out
        assert new["prompt"] == "Write the digest"

    async def test_bare_retry_still_targets_interactive_tasks_only(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            _failed_job_task(conn, job_id)
            out = await cmd_retry(_ctx(config, conn, args=""))
        assert "No failed or cancelled task" in out

    async def test_a_heartbeat_task_is_still_refused(self, config):
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="x", user_id="alice", source_type="heartbeat",
                conversation_token="room1",
            )
            db.update_task_status(conn, tid, "failed")
            out = await cmd_retry(_ctx(config, conn, args=f"#{tid}"))
        assert "can't be re-run" in out

    async def test_a_deleted_job_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            db.delete_scheduled_job(conn, job_id)
            out = await cmd_retry(_ctx(config, conn, args=f"#{old}"))
        assert "still exists" in out

    async def test_a_removed_briefing_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            old = _failed_briefing_task(conn, name="gone")
            out = await cmd_retry(_ctx(config, conn, args=f"#{old}"))
        assert "no longer exists" in out


class TestGuards:
    def test_refused_while_an_occurrence_is_in_flight(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            db.create_task(
                conn, prompt="Write the digest", user_id="alice",
                source_type="scheduled", scheduled_job_id=job_id,
            )
            task = db.get_task(conn, old)
            out = retry_task(conn, config, "alice", task)
        assert isinstance(out, str)
        assert "already queued or running" in out

    def test_briefing_refused_while_one_is_in_flight(self, config):
        with db.get_db(config.db_path) as conn:
            old = _failed_briefing_task(conn)
            db.create_task(
                conn, prompt="x", user_id="alice", source_type="briefing",
                briefing_name="morning",
            )
            out = retry_task(conn, config, "alice", db.get_task(conn, old))
        assert isinstance(out, str)
        assert "already queued or running" in out

    def test_a_second_manual_run_inside_the_cooldown_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            first = retry_task(conn, config, "alice", db.get_task(conn, old))
            assert not isinstance(first, str)
            # The first manual run finished; a second click is still refused.
            db.update_task_status(conn, first.new_task_id, "completed")
            again = retry_task(conn, config, "alice", db.get_task(conn, old))
        assert isinstance(again, str)
        assert "in the last" in again

    def test_a_task_that_failed_seconds_ago_can_be_rerun(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            # A permanent failure moments after the fire: the failed occurrence
            # itself must not trip the cooldown.
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now') WHERE id = ?", (old,),
            )
            out = retry_task(conn, config, "alice", db.get_task(conn, old))
        assert not isinstance(out, str), out

    def test_a_module_job_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn, name="_module.feeds.run_scheduled")
            job = db.get_scheduled_job(conn, job_id)
            new_id, refusal = run_scheduled_job_now(conn, config, job)
        assert new_id is None
        assert "module" in refusal

    def test_another_users_task_is_refused(self, config):
        config.admin_users = {"alice"}  # non-empty, so bob is not an admin
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            out = retry_task(conn, config, "bob", db.get_task(conn, old))
        assert out == f"Task #{old} belongs to another user."

    def test_run_now_leaves_the_schedule_alone(self, config):
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            before = db.get_scheduled_job(conn, job_id).last_run_at
            run_scheduled_job_now(conn, config, db.get_scheduled_job(conn, job_id))
            assert db.get_scheduled_job(conn, job_id).last_run_at == before
            run_briefing_now(conn, config, "alice", "morning")
            assert db.get_briefing_last_run(conn, "alice", "morning") is None


class TestCredentialScope:
    def test_a_manual_run_is_a_scheduled_task_to_the_grant_check(self, config):
        """A manual run must not get a broader grant than a scheduled one: it
        keeps the automated source type the grant code keys its scheduled
        restriction on."""
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            old = _failed_job_task(conn, job_id)
            outcome = retry_task(conn, config, "alice", db.get_task(conn, old))
            _, job_scheduled = grants._task_context(conn, outcome.new_task_id, "alice")
            briefing = retry_task(
                conn, config, "alice", db.get_task(conn, _failed_briefing_task(conn)),
            )
            _, briefing_scheduled = grants._task_context(
                conn, briefing.new_task_id, "alice",
            )
        assert job_scheduled is True
        assert briefing_scheduled is True


class TestTheCli:
    """`istota job run <user> <name>`, through `main` so the parser and the
    dispatch table agree."""

    @pytest.fixture
    def cfg(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        path = tmp_path / "config.toml"
        path.write_text(
            f'db_path = "{db_path}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            '[users.alice]\ndisplay_name = "Alice"\n'
            '[[users.alice.briefings]]\nname = "morning"\ncron = "0 8 * * *"\n'
            'conversation_token = "room1"\noutput = "talk"\n'
        )
        return str(path), db_path

    def _main(self, monkeypatch, cfg_path, *argv):
        from istota import cli

        monkeypatch.setattr("sys.argv", ["istota", "-c", cfg_path, *argv])
        cli.main()

    def test_runs_a_job(self, cfg, monkeypatch, capsys):
        cfg_path, db_path = cfg
        with db.get_db(db_path) as conn:
            job_id = _job(conn)
        self._main(monkeypatch, cfg_path, "job", "run", "alice", "digest")
        assert "Queued as task #" in capsys.readouterr().out
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT source_type, parent_task_id FROM tasks WHERE scheduled_job_id = ?",
                (job_id,),
            ).fetchone()
        assert row["source_type"] == "scheduled"
        assert row["parent_task_id"] is None

    def test_runs_a_briefing(self, cfg, monkeypatch, capsys):
        cfg_path, db_path = cfg
        self._main(monkeypatch, cfg_path, "job", "run", "alice", "morning", "--briefing")
        assert "Queued as task #" in capsys.readouterr().out
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE briefing_name = 'morning'",
            ).fetchone()[0] == 1

    def test_refusal_exits_nonzero(self, cfg, monkeypatch, capsys):
        cfg_path, db_path = cfg
        with db.get_db(db_path) as conn:
            _job(conn)
        self._main(monkeypatch, cfg_path, "job", "run", "alice", "digest")
        with pytest.raises(SystemExit) as exc:
            self._main(monkeypatch, cfg_path, "job", "run", "alice", "digest")
        assert exc.value.code == 1
        assert "already queued" in capsys.readouterr().err

    def test_unknown_job_exits_nonzero(self, cfg, monkeypatch, capsys):
        cfg_path, _ = cfg
        with pytest.raises(SystemExit):
            self._main(monkeypatch, cfg_path, "job", "run", "alice", "nope")
        assert "No scheduled job" in capsys.readouterr().err


class TestTheFailureNotice:
    """A job that fails for good raises one bell row with a Run-now action, and
    still nothing in its delivery room."""

    def _config(self, tmp_path, max_failures=5):
        from istota.config import EmailConfig, NextcloudConfig

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com", username="istota", app_password="x",
            ),
            talk=TalkConfig(enabled=True, bot_username="istota"),
            email=EmailConfig(enabled=False),
            scheduler=SchedulerConfig(
                scheduled_job_max_consecutive_failures=max_failures,
            ),
            workspace_path=mount,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(briefings=[
                BriefingConfig(name="morning", cron="0 8 * * *",
                               conversation_token="room1", output="talk"),
            ])},
        )

    def _run(self, config):
        from unittest.mock import patch

        from istota.scheduler import process_one_task

        delivered = []

        def _capture(cfg, results):
            delivered.extend(r for r in results if r is not None)

        with patch("istota.scheduler.execute_task",
                   return_value=(False, "boom", None, None)), \
             patch("istota.scheduler.asyncio.run", return_value=42) as arun, \
             patch("istota.scheduler.deliver_pending", side_effect=_capture):
            process_one_task(config)
        return delivered, arun

    def _rows(self, config):
        with db.get_db(config.db_path) as conn:
            return conn.execute(
                "SELECT * FROM notifications WHERE source = 'job_failure'",
            ).fetchall()

    def _open_job_failures(self, config, conn):
        from istota.notifications import store

        rendered, _ = store.list_open(config, conn, "alice")
        return [r.to_dict() for r in rendered if r.to_dict()["source"] == "job_failure"]

    def test_an_exhausted_job_raises_one_row_with_run_now(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            tid = db.create_task(
                conn, prompt="Write the digest", user_id="alice",
                source_type="scheduled", conversation_token="room1",
                scheduled_job_id=job_id, output_target="talk",
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (tid,))
        delivered, arun = self._run(config)

        (row,) = self._rows(config)
        assert row["object_id"] == str(tid)
        assert "digest" in row["title"]
        assert "boom" not in row["body"]
        assert len(delivered) == 1
        # Nothing posted to the delivery room: the error stays out of it.
        arun.assert_not_called()

        with db.get_db(config.db_path) as conn:
            (item,) = self._open_job_failures(config, conn)
        (action,) = item["actions"]
        assert action["endpoint"] == f"/chat/tasks/{tid}/retry"
        assert action["method"] == "POST"

        # Running it now supersedes the row.
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-1 hour') WHERE id = ?",
                (tid,),
            )
            outcome = retry_task(conn, config, "alice", db.get_task(conn, tid))
            assert not isinstance(outcome, str)
            assert self._open_job_failures(config, conn) == []

    def test_a_failed_briefing_raises_a_row(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(
                conn, prompt="Generate the 'morning' briefing.", user_id="alice",
                source_type="briefing", conversation_token="room1",
                briefing_name="morning", output_target="talk",
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (tid,))
        self._run(config)
        (row,) = self._rows(config)
        assert row["title"] == "Briefing 'morning' failed"

    def test_a_manual_run_that_fails_says_so(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            parent = _failed_job_task(conn, job_id)
            tid = db.create_task(
                conn, prompt="Write the digest", user_id="alice",
                source_type="scheduled", scheduled_job_id=job_id,
                parent_task_id=parent,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (tid,))
        self._run(config)
        (row,) = self._rows(config)
        assert row["body"].startswith("This manual run failed.")

    def test_a_retry_that_will_happen_raises_nothing(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            db.create_task(
                conn, prompt="Write the digest", user_id="alice",
                source_type="scheduled", scheduled_job_id=job_id,
            )
        self._run(config)
        assert self._rows(config) == []

    def test_a_job_switched_off_by_this_failure_raises_only_cron_job(self, tmp_path):
        config = self._config(tmp_path, max_failures=1)
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn)
            tid = db.create_task(
                conn, prompt="Write the digest", user_id="alice",
                source_type="scheduled", scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (tid,))
        self._run(config)
        assert self._rows(config) == []
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE source = 'cron_job'",
            ).fetchone()[0] == 1

    def test_a_module_job_raises_nothing(self, tmp_path):
        config = self._config(tmp_path)
        with db.get_db(config.db_path) as conn:
            job_id = _job(conn, name="_module.feeds.run_scheduled")
            tid = db.create_task(
                conn, prompt="poll", user_id="alice", source_type="scheduled",
                scheduled_job_id=job_id,
            )
            conn.execute("UPDATE tasks SET attempt_count = 2 WHERE id = ?", (tid,))
        self._run(config)
        assert self._rows(config) == []
        with db.get_db(config.db_path) as conn:
            # It did fail for good; only the notice is withheld.
            assert db.get_task(conn, tid).status == "failed"
