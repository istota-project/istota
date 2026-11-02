"""The `egress_refused` notification: one fixed-text row per subject.

A refused hostname can carry data and a job name is model-writable, while an alert
push can land in a shared room. So neither may reach a push or a stored title, and
the bell shows the subject's name to the signed-in user alone and no host at all.
The real paths are driven: `sync_cron_jobs_to_db` and `record_egress_refusals` as
the two producers, `list_open` as the reader. `send_notification` is the one thing
replaced.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from istota import db, scheduler
from istota.config import Config, UserConfig
from istota.cron_loader import CronJob, sync_cron_jobs_to_db
from istota.executor import file_egress_refusals
from istota.notifications import sources as notification_sources
from istota.notifications import store
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress

USER = "alice"
HOSTILE_NAME = "canary-job-name"
HOSTILE_HOST = "canary-secret.evil.example"
SUBJECT = f"cron:{HOSTILE_NAME}"


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(db_path=db_path, temp_dir=tmp_path / "temp")


@pytest.fixture
def sends(monkeypatch):
    calls: list[dict] = []

    def fake(_config, user_id, message, **kwargs):
        calls.append({"user_id": user_id, "message": message, **kwargs})
        return True

    monkeypatch.setattr("istota.notifications.delivery.send_notification", fake)
    return calls


def _job_task(config, name=HOSTILE_NAME, user=USER):
    with db.get_db(config.db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
            "VALUES (?, ?, '0 9 * * *', 'read the news')",
            (user, name),
        )
        job_id = cursor.lastrowid
        task_id = db.create_task(
            conn, prompt="read the news", user_id=user, source_type="scheduled",
            scheduled_job_id=job_id,
        )
        conn.commit()
        return db.get_task(conn, task_id)


def _rows(config, user=USER):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT user_id, dedup_key, title, body, occurrences, state, object_id, link "
            "FROM notifications WHERE source = 'egress_refused' AND user_id = ?",
            (user,),
        ).fetchall()


def _job_id(config, name=HOSTILE_NAME, user=USER):
    with db.get_db(config.db_path) as conn:
        return db.get_scheduled_job_by_name(conn, user, name).id


def _open(config, user=USER):
    with db.get_db(config.db_path) as conn:
        rows, _total = store.list_open(config, conn, user)
        conn.commit()
    return [resolved for resolved in rows if resolved.source == egress_refused.SOURCE]


def _refuse(config, task, *hosts):
    file_egress_refusals(config, task, [("web_fetch", host) for host in hosts])


class TestTheRow:
    def test_a_refused_host_raises_one_fixed_row_and_one_push(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        [row] = _rows(config)
        assert (row["title"], row["body"]) == (egress_refused.TITLE, egress_refused.BODY)
        assert row["title"] == "A scheduled job or assignment needs network access"
        assert row["body"] == "Review it in Istota"
        assert len(sends) == 1
        assert sends[0]["user_id"] == USER
        assert sends[0]["message"] == f"{egress_refused.TITLE}\n\n{egress_refused.BODY}"

    def test_many_hosts_are_one_row_per_subject(self, config, sends):
        task = _job_task(config)
        _refuse(config, task, "a.evil.example", "b.evil.example")
        _refuse(config, task, "c.evil.example")
        [row] = _rows(config)
        assert row["occurrences"] == 2
        assert len(sends) == 1

    def test_a_second_subject_is_a_second_row(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        _refuse(config, _job_task(config, name="other"), HOSTILE_HOST)
        assert len(_rows(config)) == 2

    def test_no_canary_reaches_the_key_the_title_or_the_push(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        [row] = _rows(config)
        stored = f"{row['title']} {row['body']} {row['dedup_key']}"
        assert "canary" not in stored
        assert all("canary" not in str(call) for call in sends)

    def test_a_long_hostile_name_makes_a_bounded_key(self):
        assert len(egress_refused.dedup_key("cron:" + "x" * 5000)) < 100

    def test_the_sync_raises_it_too(self, config, sends):
        job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="p", reach="public")
        with db.get_db(config.db_path) as conn:
            subjects = sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
            assert subjects == [SUBJECT]
            egress.raise_refused_bell(config, conn, USER, subjects)
        assert len(_rows(config)) == 1
        assert len(sends) == 1
        assert "canary" not in str(sends)


class TestTheBell:
    def test_it_names_the_subject_flat_and_no_host(self, config, sends):
        _refuse(config, _job_task(config, name="evil\n\n**job**"), HOSTILE_HOST)
        [row] = _open(config)
        assert HOSTILE_HOST not in row.body
        assert "canary" not in row.body
        assert "\n" not in row.body
        assert "evil" in row.body and "job" in row.body

    def test_its_one_action_opens_the_page(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        [row] = _open(config)
        [action] = row.actions
        assert action.method == "LINK"
        assert action.href == f"/settings/jobs/open/{_job_id(config)}"
        assert row.link == action.href
        assert notification_sources.SAFE_PATH_RE.match(action.href)

    def test_a_stored_row_with_the_old_link_opens_the_job(self, config, sends):
        """Rows written before the pages have `/settings/network`, and the view is live."""
        _refuse(config, _job_task(config), HOSTILE_HOST)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE notifications SET link = '/settings/network'")
            conn.commit()
        [row] = _open(config)
        assert row.link == f"/settings/jobs/open/{_job_id(config)}"

    def test_the_stored_link_holds_only_the_rows_integer(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        [row] = _rows(config)
        assert row["link"] == f"/settings/jobs/open/{_job_id(config)}"
        assert HOSTILE_NAME not in row["link"]

    def test_an_assignment_links_to_its_own_page(self, config, sends):
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO assignments (id, user_id, slug, title, goal, budget, dir_relpath, state) "
                "VALUES (7, ?, 's', 't', 'g', '{}', 'assignments/s', 'active')",
                (USER,),
            )
            egress.note_refused(conn, USER, "assignment:7", HOSTILE_HOST)
            conn.commit()
            egress.raise_refused_bell(config, conn, USER, ["assignment:7"])
        [row] = _open(config)
        assert row.link == "/settings/assignments/open/7"
        assert notification_sources.SAFE_PATH_RE.match(row.link)

    def test_a_subject_with_no_row_links_to_the_page(self, config):
        with db.get_db(config.db_path) as conn:
            assert egress_refused.link_for(conn, USER, "cron:nothing") == "/settings/jobs"

    def test_another_users_job_of_that_name_is_not_linked(self, config, sends):
        _job_task(config, name="same", user="bob")
        with db.get_db(config.db_path) as conn:
            assert egress_refused.link_for(conn, USER, "cron:same") == "/settings/jobs"

    def test_another_users_row_is_not_seen(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        assert _open(config, user="bob") == []

    def test_a_subject_with_a_row_of_someone_elses_name_closes(self, config, sends):
        """The id is a value on the row, so the read is scoped by the row's user."""
        _refuse(config, _job_task(config), HOSTILE_HOST)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE scheduled_jobs SET user_id = 'bob'")
            conn.commit()
        assert _open(config) == []


class TestItCloses:
    def _task(self, config):
        task = _job_task(config)
        _refuse(config, task, HOSTILE_HOST)
        assert len(_open(config)) == 1
        return task

    def test_when_the_refusal_is_added(self, config, sends):
        self._task(config)
        with db.get_db(config.db_path) as conn:
            egress.clear_refused(conn, USER, SUBJECT, HOSTILE_HOST)
            conn.commit()
        assert _open(config) == []

    def test_when_every_refusal_is_dismissed(self, config, sends):
        self._task(config)
        with db.get_db(config.db_path) as conn:
            egress.dismiss_refused(conn, USER, SUBJECT, HOSTILE_HOST)
            conn.commit()
        assert _open(config) == []

    def test_a_dismissed_entry_does_not_reopen_it(self, config, sends):
        task = self._task(config)
        with db.get_db(config.db_path) as conn:
            egress.dismiss_refused(conn, USER, SUBJECT, HOSTILE_HOST)
            conn.commit()
        assert _open(config) == []
        _refuse(config, task, HOSTILE_HOST)
        assert _open(config) == []
        assert len(sends) == 1

    def test_a_new_host_reopens_it_and_pushes_again(self, config, sends):
        task = self._task(config)
        with db.get_db(config.db_path) as conn:
            egress.dismiss_refused(conn, USER, SUBJECT, HOSTILE_HOST)
            conn.commit()
        assert _open(config) == []
        _refuse(config, task, "other.evil.example")
        assert len(_open(config)) == 1
        assert len(sends) == 2

    def test_when_the_job_is_removed(self, config, sends):
        self._task(config)
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, USER, [], is_admin=False)
            conn.commit()
        assert _open(config) == []

    def test_when_the_assignment_is_closed(self, config, sends):
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO assignments (id, user_id, slug, title, goal, budget, dir_relpath, state) "
                "VALUES (7, ?, 's', 't', 'g', '{}', 'assignments/s', 'active')",
                (USER,),
            )
            egress.note_refused(conn, USER, "assignment:7", HOSTILE_HOST)
            conn.commit()
            egress.raise_refused_bell(config, conn, USER, ["assignment:7"])
        assert len(_open(config)) == 1
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE assignments SET state = 'completed' WHERE id = 7")
            conn.commit()
        assert _open(config) == []

    @pytest.mark.parametrize("subject", ["tasks-file", "briefing:morning"])
    def test_a_stored_row_on_a_retired_subject_closes(self, config, sends, subject):
        """The briefing itself is still there: its subject is what went."""
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_configs "
                "(user_id, name, cron_expression, conversation_token, output, components) "
                "VALUES (?, 'morning', '0 7 * * *', '', 'email', '{}')",
                (USER,),
            )
            egress.note_refused(conn, USER, subject, HOSTILE_HOST)
            egress_refused.write(conn, USER, subject)
            conn.commit()
        assert _open(config) == []


class TestTheBadgeIsRightWithNoPanelOpen:
    """`counts()` runs no resolvers, so a removed subject has to close its own row."""

    def _badge(self, config, user=USER):
        with db.get_db(config.db_path) as conn:
            return store.counts(conn, user, strict=True)["open"]

    def _closed_by(self, config):
        [row] = _rows(config)
        with db.get_db(config.db_path) as conn:
            by = conn.execute(
                "SELECT resolved_by FROM notifications WHERE source = 'egress_refused'",
            ).fetchone()[0]
        return row["state"], by

    def test_a_deleted_job_closes_its_row(self, config, sends):
        task = _job_task(config)
        _refuse(config, task, HOSTILE_HOST)
        assert self._badge(config) == 1
        with db.get_db(config.db_path) as conn:
            db.delete_scheduled_job(conn, task.scheduled_job_id)
            conn.commit()
        assert self._badge(config) == 0
        assert self._closed_by(config) == ("resolved", "subject_deleted")

    def test_it_leaves_another_users_row_of_the_same_name(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        bob_task = _job_task(config, user="bob")
        _refuse(config, bob_task, HOSTILE_HOST)
        with db.get_db(config.db_path) as conn:
            db.delete_scheduled_job(conn, bob_task.scheduled_job_id)
            conn.commit()
        assert self._badge(config, "bob") == 0
        assert self._badge(config, USER) == 1

    def test_a_new_job_under_an_old_name_starts_with_no_row(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM scheduled_jobs")
            job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="another")
            sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
            conn.commit()
        assert self._badge(config) == 0

    def test_that_job_asking_for_something_pushes_again(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        assert len(sends) == 1
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM scheduled_jobs")
            job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="another", reach="public")
            subjects = sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
            conn.commit()
            egress.raise_refused_bell(config, conn, USER, subjects)
        assert self._badge(config) == 1
        assert len(sends) == 2

    def test_a_refusal_filed_after_the_subject_went_raises_nothing(self, config, sends):
        with db.get_db(config.db_path) as conn:
            egress.note_refused(conn, USER, "cron:gone", HOSTILE_HOST)
            conn.commit()
            egress.raise_refused_bell(config, conn, USER, ["cron:gone"])
        assert _rows(config) == []
        assert sends == []

    def _job_asking(self, config, **fields):
        job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="p", **fields)
        with db.get_db(config.db_path) as conn:
            subjects = sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
            egress.raise_refused_bell(config, conn, USER, subjects)
        assert self._badge(config) == 1

    def test_an_edit_that_withdraws_the_request_closes_its_row(self, config, sends):
        self._job_asking(config, reach="public")
        job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="p")
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
        assert self._badge(config) == 0

    def test_a_declared_entry_taken_out_closes_its_row(self, config, sends):
        self._job_asking(config, egress=("example.com",))
        job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="p")
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
        assert self._badge(config) == 0

    def test_a_ceiling_that_now_covers_the_request_closes_its_row(self, config, sends):
        self._job_asking(config, reach="public")
        job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="p", reach="public")
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, USER, [job], is_admin=False, ceiling="public")
        assert self._badge(config) == 0

    def test_a_host_still_refused_keeps_its_row_through_an_edit(self, config, sends):
        _refuse(config, _job_task(config), HOSTILE_HOST)
        job = CronJob(name=HOSTILE_NAME, cron="0 9 * * *", prompt="read the news")
        with db.get_db(config.db_path) as conn:
            sync_cron_jobs_to_db(conn, USER, [job], is_admin=False)
        assert self._badge(config) == 1

    def test_the_cli_answering_the_last_refusal_closes_its_row(self, config, sends, tmp_path):
        from istota.cli import cmd_egress

        _refuse(config, _job_task(config), "news.example.com")
        config_path = tmp_path / "config.toml"
        config_path.write_text(f'db_path = "{config.db_path}"\n')
        cmd_egress(argparse.Namespace(
            config=str(config_path), egress_action="grant", user=USER, subject=SUBJECT,
            entry="news.example.com", json=False, digest=None,
        ))
        assert self._badge(config) == 0

    def test_removing_a_user_takes_the_row(self, config, sends):
        from istota import user_profiles

        _refuse(config, _job_task(config), HOSTILE_HOST)
        user_profiles.ensure_profile(config.db_path, USER)
        assert user_profiles.delete_profile(config.db_path, USER) is True
        assert _rows(config) == []

    def _module_job_with_a_row(self, config):
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, command) "
                "VALUES (?, '_module.feeds.prune', '0 3 * * *', 'true')",
                (USER,),
            )
            egress.note_refused(conn, USER, "cron:_module.feeds.prune", HOSTILE_HOST)
            conn.commit()
            egress.raise_refused_bell(config, conn, USER, ["cron:_module.feeds.prune"])
        assert self._badge(config) == 1

    def _sync_modules(self, config, *, jobs):
        with db.get_db(config.db_path) as conn:
            scheduler._sync_module_jobs(
                conn, config, module_name="feeds", module_prefix="_module.feeds.",
                resolve_for_user=lambda user_id, cfg, conn=None: object(),
                jobs_for_user=lambda ctx, user_id: jobs,
                user_not_found_exc=LookupError,
            )
            conn.commit()

    def _module_user(self, config):
        return Config(
            db_path=config.db_path, temp_dir=config.temp_dir, users={USER: UserConfig()},
        )

    def test_a_module_job_no_longer_wanted_closes_its_row(self, config, sends):
        self._module_job_with_a_row(config)
        module_config = self._module_user(config)
        with patch.object(Config, "is_module_enabled", return_value=True):
            self._sync_modules(module_config, jobs=[])
        assert self._badge(config) == 0
        with db.get_db(config.db_path) as conn:
            assert egress.list_refused(conn, USER, "cron:_module.feeds.prune") == []

    def test_a_disabled_modules_jobs_close_their_rows(self, config, sends):
        self._module_job_with_a_row(config)
        module_config = self._module_user(config)
        with patch.object(Config, "is_module_enabled", return_value=False):
            self._sync_modules(module_config, jobs=[])
        assert self._badge(config) == 0
        with db.get_db(config.db_path) as conn:
            assert egress.list_refused(conn, USER, "cron:_module.feeds.prune") == []
            assert conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0] == 0


class TestAModuleSyncMatchesItsPrefixLiterally:
    """`_` is a LIKE wildcard, and a job name is the user's or a task's own."""

    @pytest.mark.parametrize("enabled", [True, False])
    def test_a_look_alike_job_survives(self, config, enabled):
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
                "VALUES (?, 'xmodule.feeds.mine', '0 9 * * *', 'p')",
                (USER,),
            )
            conn.commit()
            module_config = Config(
                db_path=config.db_path, temp_dir=config.temp_dir, users={USER: UserConfig()},
            )
            with patch.object(Config, "is_module_enabled", return_value=enabled):
                scheduler._sync_module_jobs(
                    conn, module_config, module_name="feeds", module_prefix="_module.feeds.",
                    resolve_for_user=lambda user_id, cfg, conn=None: object(),
                    jobs_for_user=lambda ctx, user_id: [],
                    user_not_found_exc=LookupError,
                )
            conn.commit()
            names = [row[0] for row in conn.execute("SELECT name FROM scheduled_jobs")]
        assert names == ["xmodule.feeds.mine"]


class TestAFailureNeverDecides:
    def test_a_bell_failure_is_a_warning(self, config, monkeypatch, caplog):
        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        _job_task(config)
        monkeypatch.setattr("istota.notifications.store.write_notification", broken)
        with db.get_db(config.db_path) as conn:
            egress.raise_refused_bell(config, conn, USER, [SUBJECT])
        assert "egress_bell_failed" in caplog.text
        assert "canary" not in caplog.text
