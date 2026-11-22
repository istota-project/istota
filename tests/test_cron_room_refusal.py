"""A CRON.md job may name only rooms its owner is a current member of (#979).

`room` becomes the task's `conversation_token`, and that token is joined under
`{mount}/Channels` and bound read-write into the sandbox. Before this, the file
path stored `room` and every `room:` / `web:` / `talk:` target as written: a
`..` component bound another user's home or the whole workspace read-write, and
another user's room token read that room's notes and posted into it.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from istota import db
from istota.config import Config, SchedulerConfig, UserConfig
from istota.cron_loader import load_cron_document, sync_cron_jobs_to_db
from istota.storage import get_user_cron_path
from istota.transport.route_checks import RouteRefused


@pytest.fixture
def setup(tmp_path):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Users" / "bob").mkdir(parents=True)
    (mount / "Users" / "bob" / "secret.txt").write_text("bob-private-canary")
    (mount / "Channels").mkdir()
    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    cfg = Config(
        db_path=db_path, workspace_path=mount, temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(timezone="UTC"), "bob": UserConfig(timezone="UTC")},
        scheduler=SchedulerConfig(cron_max_staleness_minutes=0),
    )
    return cfg, mount


def _write_cron(mount, user, body):
    p = mount / get_user_cron_path(user, "istota").lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("```toml\n" + body + "\n```\n")


def _job(room="", target=""):
    lines = ['[[jobs]]', 'name = "x"', 'cron = "* * * * *"', 'prompt = "summarize"']
    if room:
        lines.append(f'room = "{room}"')
    if target:
        lines.append(f'target = "{target}"')
    return "\n".join(lines)


def _sync(cfg, user):
    doc = load_cron_document(cfg, user)
    with db.get_db(cfg.db_path) as conn:
        sync_cron_jobs_to_db(conn, user, doc.jobs, is_admin=False, config=cfg)
        conn.commit()
        return doc, db.get_user_scheduled_jobs(conn, user)


def _web_room(cfg, owner, *members):
    with db.get_db(cfg.db_path) as conn:
        token = db.create_web_chat_room(conn, owner, "room").token
        for m in members:
            db.add_web_room_member(conn, token, m)
        conn.commit()
    return token


class TestTheRoomNameAtParse:
    @pytest.mark.parametrize("room", ["..", "../Users", "../Users/bob", "a/b", "."])
    def test_a_room_that_is_not_a_token_refuses_the_job(self, setup, room):
        cfg, mount = setup
        _write_cron(mount, "alice", _job(room=room))
        doc = load_cron_document(cfg, "alice")
        assert doc.jobs == []
        assert "x" in doc.refused_names
        assert any("room" in e for e in doc.errors)

    def test_a_row_stored_before_the_refusal_goes(self, setup):
        """The file's only job is refused, so the sync is held, and a held sync
        still removes a refused job's row: it named `..` before this landed."""
        from istota.scheduler import _sync_cron_files

        cfg, mount = setup
        with db.get_db(cfg.db_path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, "
                "conversation_token, enabled) VALUES ('alice', 'x', '* * * * *', 'p', '..', 1)"
            )
            conn.commit()
        _write_cron(mount, "alice", _job(room=".."))
        with db.get_db(cfg.db_path) as conn:
            _sync_cron_files(conn, cfg)
            conn.commit()
            assert db.get_user_scheduled_jobs(conn, "alice") == []


class TestMembershipAtSync:
    def test_another_users_private_room_is_refused(self, setup):
        cfg, mount = setup
        bob_room = _web_room(cfg, "bob")
        _write_cron(mount, "alice", _job(room=bob_room, target=f"web:{bob_room}"))
        _, rows = _sync(cfg, "alice")
        assert rows == []

    def test_a_target_alone_naming_another_users_room_is_refused(self, setup):
        cfg, mount = setup
        bob_room = _web_room(cfg, "bob")
        _write_cron(mount, "alice", _job(target=f"room:{bob_room}"))
        _, rows = _sync(cfg, "alice")
        assert rows == []

    def test_a_shared_room_the_owner_left_is_refused(self, setup):
        cfg, mount = setup
        room = _web_room(cfg, "bob", "alice", "carol")
        with db.get_db(cfg.db_path) as conn:
            db.remove_room_member(conn, room, "alice")
            conn.commit()
        _write_cron(mount, "alice", _job(room=room, target=f"web:{room}"))
        _, rows = _sync(cfg, "alice")
        assert rows == []

    def test_a_talk_room_the_owner_left_is_refused(self, setup):
        """Talk records a departure as `left_at` and keeps the member row, so
        the member row alone is not membership (ISSUE-594's rule)."""
        cfg, mount = setup
        with db.get_db(cfg.db_path) as conn:
            room = db.register_room(conn, "talkref1", "bob", origin="talk").token
            db.add_room_member(conn, room, "alice")
            db.add_room_binding(conn, room, "talk", "talkref1")
            for uid in ("alice", "bob"):
                db.upsert_room_participant(
                    conn, room_token=room, surface="talk", surface_ref=uid,
                    kind="principal", user_id=uid,
                )
            db.sync_room_roster(conn, room_token=room, surface="talk", present=["bob"])
            conn.commit()
        _write_cron(mount, "alice", _job(room="talkref1", target="talk:talkref1"))
        _, rows = _sync(cfg, "alice")
        assert rows == []

    def test_an_unregistered_talk_target_is_refused(self, setup):
        cfg, mount = setup
        _write_cron(mount, "alice", _job(target="talk:somebodyelses"))
        _, rows = _sync(cfg, "alice")
        assert rows == []

    def test_an_existing_row_is_removed_when_its_room_is_refused(self, setup):
        cfg, mount = setup
        own = _web_room(cfg, "alice")
        _write_cron(mount, "alice", _job(room=own, target=f"web:{own}"))
        _, rows = _sync(cfg, "alice")
        assert [r.conversation_token for r in rows] == [own]
        bob_room = _web_room(cfg, "bob")
        _write_cron(mount, "alice", _job(room=bob_room, target=f"web:{bob_room}"))
        _, rows = _sync(cfg, "alice")
        assert rows == []

    def test_the_owners_own_room_syncs(self, setup):
        cfg, mount = setup
        own = _web_room(cfg, "alice")
        _write_cron(mount, "alice", _job(room=own, target=f"web:{own}"))
        _, rows = _sync(cfg, "alice")
        assert [(r.conversation_token, r.output_target) for r in rows] == [(own, f"web:{own}")]

    def test_a_shared_room_the_owner_is_in_syncs(self, setup):
        """ISSUE-594: a member's own job aimed at a shared room they are in is
        their turn asked in advance, so it is not refused here."""
        cfg, mount = setup
        room = _web_room(cfg, "bob", "alice")
        _write_cron(mount, "alice", _job(room=room, target=f"web:{room}"))
        _, rows = _sync(cfg, "alice")
        assert [r.conversation_token for r in rows] == [room]

    def test_the_owners_provisioned_talk_channel_syncs(self, setup):
        """A provisioned alerts channel need not be in the room registry."""
        cfg, mount = setup
        cfg.users["alice"] = UserConfig(timezone="UTC", alerts_channel="alertsref")
        _write_cron(mount, "alice", _job(room="alertsref", target="talk:alertsref"))
        _, rows = _sync(cfg, "alice")
        assert [r.conversation_token for r in rows] == ["alertsref"]

    def test_a_target_naming_no_room_syncs(self, setup):
        cfg, mount = setup
        _write_cron(mount, "alice", _job(target="email"))
        _, rows = _sync(cfg, "alice")
        assert len(rows) == 1


def _insert_row(cfg, *, room, target=None):
    yesterday = (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).isoformat()
    with db.get_db(cfg.db_path) as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, "
            "conversation_token, output_target, enabled, last_run_at, created_at) "
            "VALUES ('alice', 'x', '* * * * *', 'p', ?, ?, 1, ?, ?)",
            (room, target, yesterday, yesterday),
        )
        conn.commit()


class TestMembershipAtDispatch:
    """The sync runs only when the file changes, so a member who leaves keeps
    a row naming the room; dispatch asks again."""

    @pytest.mark.parametrize("room", ["..", "../Users", "../Users/bob"])
    def test_a_stored_traversal_makes_no_task(self, setup, room):
        from istota.scheduler import _create_scheduled_job_task

        cfg, _ = setup
        _insert_row(cfg, room=room)
        with db.get_db(cfg.db_path) as conn:
            (job,) = db.get_user_scheduled_jobs(conn, "alice")
            with pytest.raises(RouteRefused):
                _create_scheduled_job_task(conn, cfg, job)
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_a_room_the_owner_left_makes_no_task_and_spends_the_slot(self, setup, monkeypatch):
        from istota import scheduler

        cfg, _ = setup
        room = _web_room(cfg, "bob", "alice", "carol")
        _insert_row(cfg, room=room, target=f"web:{room}")
        with db.get_db(cfg.db_path) as conn:
            db.remove_room_member(conn, room, "alice")
            conn.commit()
        monkeypatch.setattr(scheduler, "_sync_cron_files", lambda conn, config: None)
        with db.get_db(cfg.db_path) as conn:
            assert scheduler.check_scheduled_jobs(conn, cfg) == []
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            (job,) = db.get_user_scheduled_jobs(conn, "alice")
            assert job.last_run_at and not job.last_run_at.startswith(
                (datetime.now(ZoneInfo("UTC")) - timedelta(days=1)).date().isoformat()
            )

    def test_run_now_refuses_too(self, setup):
        from istota.scheduler import run_scheduled_job_now

        cfg, _ = setup
        bob_room = _web_room(cfg, "bob")
        _insert_row(cfg, room=bob_room, target=f"web:{bob_room}")
        with db.get_db(cfg.db_path) as conn:
            (job,) = db.get_user_scheduled_jobs(conn, "alice")
            task_id, refusal = run_scheduled_job_now(conn, cfg, job)
        assert task_id is None and refusal

    def test_the_owners_own_room_makes_a_task(self, setup):
        from istota.scheduler import _create_scheduled_job_task

        cfg, _ = setup
        own = _web_room(cfg, "alice")
        _insert_row(cfg, room=own, target=f"web:{own}")
        with db.get_db(cfg.db_path) as conn:
            (job,) = db.get_user_scheduled_jobs(conn, "alice")
            task = db.get_task(conn, _create_scheduled_job_task(conn, cfg, job))
        assert task.conversation_token == own


class TestTheChannelBind:
    @pytest.mark.parametrize("token", ["..", "../Users", "../Users/bob"])
    def test_a_token_that_leaves_channels_is_not_bound(self, setup, token):
        from istota.sandbox.plan import user_data_mounts

        cfg, mount = setup
        mounts = user_data_mounts(
            cfg, user_id="alice", conversation_token=token,
            user_resources=[], withheld_scopes=frozenset(), group_ids=[],
        )
        assert [m for m in mounts if m.reason == "nextcloud_channel_dir"] == []

    def test_a_linked_channel_dir_is_not_bound(self, setup):
        from istota.sandbox.plan import user_data_mounts

        cfg, mount = setup
        (mount / "Channels" / "planted").symlink_to(mount / "Users" / "bob")
        mounts = user_data_mounts(
            cfg, user_id="alice", conversation_token="planted",
            user_resources=[], withheld_scopes=frozenset(), group_ids=[],
        )
        assert [m for m in mounts if m.reason == "nextcloud_channel_dir"] == []

    def test_a_real_channel_dir_is_bound(self, setup):
        from istota.sandbox.plan import user_data_mounts

        cfg, mount = setup
        (mount / "Channels" / "room1").mkdir()
        mounts = user_data_mounts(
            cfg, user_id="alice", conversation_token="room1",
            user_resources=[], withheld_scopes=frozenset(), group_ids=[],
        )
        (bind,) = [m for m in mounts if m.reason == "nextcloud_channel_dir"]
        assert bind.mode == "rw"
        assert bind.source == mount.resolve() / "Channels" / "room1"

    @pytest.mark.parametrize("token", ["..", "../Users"])
    def test_the_image_roots_take_the_same_rule(self, setup, token):
        from istota.executor import image_bind_roots

        cfg, mount = setup
        task = db.Task(id=1, status="pending", source_type="scheduled",
                       user_id="alice", prompt="p", conversation_token=token)
        roots = image_bind_roots(cfg, task, cfg.temp_dir / "alice")
        assert mount.resolve() not in roots
        assert (mount / "Users").resolve() not in roots


class TestTheExecutorRefusesABadToken:
    def test_an_invalid_conversation_token_fails_the_task(self, setup):
        from istota.executor import execute_task

        cfg, _ = setup
        task = db.Task(id=7, status="running", source_type="scheduled",
                       user_id="alice", prompt="p", conversation_token="../Users")
        success, result, _, _ = execute_task(task, cfg, [])
        assert success is False
        assert "conversation token" in result


def _heartbeat(mount, *, channel="", token=""):
    config_dir = mount / "Users" / "alice" / "istota" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = f'[settings]\nconversation_token = "{token}"\n\n' if token else ""
    channel_line = f'channel = "{channel}"\n' if channel else ""
    (config_dir / "HEARTBEAT.md").write_text(
        "```toml\n" + settings
        + '[[checks]]\nname = "gone"\ntype = "file-watch"\npath = "/nonexistent.txt"\n'
        + channel_line + "```\n"
    )


class TestTheHeartbeatChannel:
    """HEARTBEAT.md is task-writable too, and its `channel` reached any room."""

    def _run(self, cfg, monkeypatch):
        from istota import heartbeat

        sent = []
        monkeypatch.setattr(
            heartbeat, "send_heartbeat_alert",
            lambda config, user_id, check, result, settings: sent.append(check.channel) or True,
        )
        with db.get_db(cfg.db_path) as conn:
            heartbeat.check_heartbeats(conn, cfg)
            state = db.get_heartbeat_state(conn, "alice", "gone")
        return sent, state

    def test_a_channel_naming_another_users_room_sends_nothing(self, setup, monkeypatch):
        cfg, mount = setup
        bob_room = _web_room(cfg, "bob")
        _heartbeat(mount, channel=f"web:{bob_room}")
        sent, state = self._run(cfg, monkeypatch)
        assert sent == []
        assert state.consecutive_errors == 0

    def test_a_token_naming_another_users_talk_room_sends_nothing(self, setup, monkeypatch):
        from istota.config import NextcloudConfig

        cfg, mount = setup
        cfg.nextcloud = NextcloudConfig(url="https://cloud.example.com")
        with db.get_db(cfg.db_path) as conn:
            db.register_room(conn, "bobsref", "bob", origin="talk")
            conn.commit()
        _heartbeat(mount, channel="talk", token="bobsref")
        sent, _ = self._run(cfg, monkeypatch)
        assert sent == []

    def test_the_owners_own_room_is_sent(self, setup, monkeypatch):
        cfg, mount = setup
        own = _web_room(cfg, "alice")
        _heartbeat(mount, channel=f"web:{own}")
        sent, _ = self._run(cfg, monkeypatch)
        assert sent == [f"web:{own}"]

    def test_a_token_no_talk_leg_uses_is_not_judged(self, setup, monkeypatch):
        cfg, mount = setup
        own = _web_room(cfg, "alice")
        _heartbeat(mount, channel=f"web:{own}", token="bobsref")
        sent, _ = self._run(cfg, monkeypatch)
        assert sent == [f"web:{own}"]


class TestTheValidatorSaysSo:
    """The web editor, the jobs page and the schedules skill show the file's
    problems through `cron_document_errors`; a dropped job must be one."""

    def test_another_users_room_is_a_problem(self, setup):
        from istota.cron_loader import cron_document_errors

        cfg, _ = setup
        bob_room = _web_room(cfg, "bob")
        errors = cron_document_errors(
            "```toml\n" + _job(room=bob_room, target=f"web:{bob_room}") + "\n```\n",
            cfg, "alice",
        )
        assert len(errors) == 1
        assert "Job 'x'" in errors[0] and "not one of your rooms" in errors[0]

    def test_the_owners_own_room_is_not(self, setup):
        from istota.cron_loader import cron_document_errors

        cfg, _ = setup
        own = _web_room(cfg, "alice")
        assert cron_document_errors(
            "```toml\n" + _job(room=own, target=f"web:{own}") + "\n```\n", cfg, "alice",
        ) == []
