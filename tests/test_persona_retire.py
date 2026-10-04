"""Retiring the per-user PERSONA.md copies once the operator persona is in force."""

import json
import os
from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.maintenance import persona_retire
from istota.maintenance.persona_retire import retire_user_personas
from istota.notifications.resolvers import task_alert
from istota.prompts import persona
from istota.prompts.persona import persona_digest

SHIPPED = "You are {BOT_NAME}.\n\nShipped character, current version.\n"
OLD_SHIPPED = "You are {BOT_NAME}.\n\nShipped character, an older version.\n"
EDITED = "You are {BOT_NAME}.\n\nYou are the front desk for this office.\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True)
    (config_dir / "persona.md").write_text(SHIPPED)
    root = tmp_path / "mount"
    root.mkdir()
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty_bundled",
        workspace_path=root,
        db_path=db_path,
        users={uid: UserConfig() for uid in ("alice", "bob", "carol")},
    )
    monkeypatch.setattr(
        persona,
        "SHIPPED_PERSONA_DIGESTS",
        frozenset({persona_digest(SHIPPED), persona_digest(OLD_SHIPPED)}),
    )
    return config, root


def _config_dir(config, root, user_id):
    path = root / "Users" / user_id / config.bot_dir_name / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plant(config, root, user_id, text):
    path = _config_dir(config, root, user_id) / "PERSONA.md"
    path.write_text(text)
    return path


def _by_user(outcomes):
    return {o.user_id: o for o in outcomes}


def _alerts(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT user_id, dedup_key, title, body, severity, params, last_delivered_at "
            "FROM notifications WHERE source = ?",
            (task_alert.SOURCE,),
        ).fetchall()


def _tree(root):
    return sorted(
        (str(p.relative_to(root)), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in root.rglob("*")
    )


def test_the_key_is_fixed():
    assert task_alert.persona_retired_key() == "persona-retired"


class TestRetire:
    def test_a_copy_of_the_current_shipped_version_is_deleted(self, setup):
        config, root = setup
        path = _plant(config, root, "alice", SHIPPED)
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "deleted"
        assert not path.exists()
        assert _alerts(config) == []

    def test_a_copy_of_an_older_shipped_version_is_deleted(self, setup):
        # The prod case: two untouched copies seeded from an earlier release.
        config, root = setup
        path = _plant(config, root, "alice", OLD_SHIPPED.replace("\n", "\r\n") + "\n")
        assert _by_user(retire_user_personas(config))["alice"].action == "deleted"
        assert not path.exists()

    def test_a_copy_equal_to_the_operator_file_is_deleted(self, setup):
        config, root = setup
        operator_text = "You are {BOT_NAME}, as the operator wrote it.\n"
        (root / "PERSONA.md").write_text(operator_text)
        path = _plant(config, root, "alice", operator_text)
        assert _by_user(retire_user_personas(config))["alice"].action == "deleted"
        assert not path.exists()
        assert _alerts(config) == []

    def test_an_empty_copy_is_deleted_without_a_notice(self, setup):
        # The old loader ignored an empty per-user file, so there is nothing to keep.
        config, root = setup
        path = _plant(config, root, "alice", "  \n")
        assert _by_user(retire_user_personas(config))["alice"].action == "deleted"
        assert not path.exists()
        assert _alerts(config) == []

    def test_an_edited_copy_is_renamed_and_its_owner_told_once(self, setup):
        config, root = setup
        path = _plant(config, root, "alice", EDITED)
        outcomes = _by_user(retire_user_personas(config))
        assert outcomes["alice"].action == "retired"
        assert not path.exists()
        retired = path.with_name("PERSONA.md.retired")
        assert retired.read_text() == EDITED

        rows = _alerts(config)
        assert len(rows) == 1
        user_id, key, title, body, severity, params, delivered = rows[0]
        assert (user_id, key, severity) == ("alice", "persona-retired", "info")
        assert title == "Your persona file was retired"
        assert "PERSONA.md.retired" in body
        assert "USER.md" in body
        assert "role or standing instructions" in body
        assert json.loads(params)["alert_type"] == task_alert.ALERT_TYPE_NOTE
        # Written, never delivered: init runs with every service stopped.
        assert delivered is None

    def test_a_notice_the_store_refuses_is_reported_as_failed(self, setup):
        config, root = setup
        path = _plant(config, root, "alice", EDITED)
        with db.get_db(config.db_path) as conn:
            conn.execute("DROP TABLE notifications")
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "retired"
        assert outcome.notice_failed is True
        assert path.with_name("PERSONA.md.retired").read_text() == EDITED

    def test_users_without_a_copy_are_absent(self, setup):
        config, root = setup
        _config_dir(config, root, "bob")
        outcomes = _by_user(retire_user_personas(config))
        assert outcomes["bob"].action == "absent"
        # No config directory at all is absent too, and nothing is created.
        assert outcomes["carol"].action == "absent"
        assert not (root / "Users" / "carol").exists()

    def test_only_configured_users_are_considered(self, setup):
        config, root = setup
        stranger = _plant(config, root, "mallory", EDITED)
        outcomes = retire_user_personas(config)
        assert {o.user_id for o in outcomes} == {"alice", "bob", "carol"}
        assert stranger.read_text() == EDITED

    def test_a_second_run_is_a_no_op(self, setup):
        config, root = setup
        _plant(config, root, "alice", EDITED)
        _plant(config, root, "bob", SHIPPED)
        retire_user_personas(config)
        before = _tree(root)
        outcomes = retire_user_personas(config)
        assert {o.action for o in outcomes} == {"absent"}
        assert _tree(root) == before
        assert len(_alerts(config)) == 1

    def test_an_existing_retired_file_gets_a_timestamped_name(self, setup):
        config, root = setup
        path = _plant(config, root, "alice", EDITED)
        earlier = path.with_name("PERSONA.md.retired")
        earlier.write_text("an earlier retirement")
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "retired"
        assert earlier.read_text() == "an earlier retirement"
        stamped = [p for p in path.parent.iterdir() if p.name.startswith("PERSONA.md.retired-")]
        assert len(stamped) == 1
        assert stamped[0].read_text() == EDITED
        assert len(stamped[0].name) == len("PERSONA.md.retired-") + len("20261004T120000")
        assert stamped[0].name in _alerts(config)[0][3]

    def test_both_names_taken_is_refused_and_nothing_overwritten(self, setup, monkeypatch):
        config, root = setup
        path = _plant(config, root, "alice", EDITED)
        monkeypatch.setattr(persona_retire, "_utc_stamp", lambda: "20261004T120000")
        (path.parent / "PERSONA.md.retired").write_text("one")
        (path.parent / "PERSONA.md.retired-20261004T120000").write_text("two")
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "refused"
        assert path.read_text() == EDITED
        assert (path.parent / "PERSONA.md.retired").read_text() == "one"
        assert (path.parent / "PERSONA.md.retired-20261004T120000").read_text() == "two"
        assert _alerts(config) == []

    def test_a_symlinked_copy_is_refused_and_left(self, setup, tmp_path):
        config, root = setup
        target = tmp_path / "elsewhere.md"
        target.write_text(SHIPPED)
        link = _config_dir(config, root, "alice") / "PERSONA.md"
        link.symlink_to(target)
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "refused"
        assert link.is_symlink()
        assert target.read_text() == SHIPPED

    def test_a_fifo_is_refused_without_blocking(self, setup):
        config, root = setup
        fifo = _config_dir(config, root, "alice") / "PERSONA.md"
        os.mkfifo(fifo)
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "refused"
        assert fifo.exists()

    def test_a_config_dir_symlinked_out_of_the_tree_is_refused(self, setup, tmp_path):
        config, root = setup
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "PERSONA.md").write_text(SHIPPED)
        bot_dir = root / "Users" / "alice" / config.bot_dir_name
        bot_dir.mkdir(parents=True)
        (bot_dir / "config").symlink_to(outside, target_is_directory=True)
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "refused"
        assert (outside / "PERSONA.md").read_text() == SHIPPED

    def test_a_symlinked_bot_dir_inside_the_tree_is_refused(self, setup):
        # Every component is opened O_NOFOLLOW, whatever the link points at.
        config, root = setup
        real = root / "Users" / "alice" / "real"
        (real / "config").mkdir(parents=True)
        (real / "config" / "PERSONA.md").write_text(EDITED)
        (root / "Users" / "alice" / config.bot_dir_name).symlink_to(real, target_is_directory=True)
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "refused"
        assert (real / "config" / "PERSONA.md").read_text() == EDITED

    @pytest.mark.parametrize("user_id", ["..", ".", "a/b"])
    def test_an_unscopable_user_id_is_refused(self, setup, user_id):
        config, root = setup
        config.users = {user_id: UserConfig()}
        # Where `{mount}/Users/..` would lead.
        target = root / config.bot_dir_name / "config" / "PERSONA.md"
        target.parent.mkdir(parents=True)
        target.write_text(EDITED)
        outcome = _by_user(retire_user_personas(config))[user_id]
        assert outcome.action == "refused"
        assert target.read_text() == EDITED

    def test_a_retired_name_created_after_the_check_is_not_overwritten(self, setup, monkeypatch):
        # Every existence check loses the race: the name looks free, and only
        # a move that refuses an existing target keeps the racer's file.
        config, root = setup
        path = _plant(config, root, "alice", EDITED)
        racer = path.with_name("PERSONA.md.retired")
        racer.write_text("written in between")
        monkeypatch.setattr(persona_retire, "_exists_at", lambda name, dir_fd: False)
        outcome = _by_user(retire_user_personas(config))["alice"]
        assert outcome.action == "refused"
        assert racer.read_text() == "written in between"
        assert path.read_text() == EDITED
        assert _alerts(config) == []

    def test_a_filesystem_without_hard_links_still_retires(self, setup, monkeypatch):
        config, root = setup
        path = _plant(config, root, "alice", EDITED)

        def _no_links(*a, **k):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(persona_retire.os, "link", _no_links)
        assert _by_user(retire_user_personas(config))["alice"].action == "retired"
        assert path.with_name("PERSONA.md.retired").read_text() == EDITED
        assert not path.exists()

    def test_dry_run_changes_nothing(self, setup):
        config, root = setup
        _plant(config, root, "alice", EDITED)
        _plant(config, root, "bob", SHIPPED)
        before = _tree(root)
        outcomes = _by_user(retire_user_personas(config, dry_run=True))
        assert outcomes["alice"].action == "retired"
        assert outcomes["bob"].action == "deleted"
        assert _tree(root) == before
        assert _alerts(config) == []

    def test_no_workspace(self, setup):
        config, _root = setup
        config.workspace_path = None
        outcomes = retire_user_personas(config)
        assert {o.action for o in outcomes} == {"no_workspace"}

    def test_a_missing_root_touches_nothing(self, setup, tmp_path):
        config, _root = setup
        config.workspace_path = tmp_path / "offline-mount"
        outcomes = retire_user_personas(config)
        assert {o.action for o in outcomes} == {"root_unavailable"}
        assert not (tmp_path / "offline-mount").exists()


class TestMain:
    @pytest.fixture
    def run(self, setup, monkeypatch):
        config, root = setup
        import istota.config

        monkeypatch.setattr(istota.config, "load_config", lambda *a, **k: config)
        return config, root

    def test_complete_is_zero(self, run, capsys):
        config, root = run
        _plant(config, root, "alice", EDITED)
        _plant(config, root, "bob", SHIPPED)
        assert persona_retire.main([]) == 0
        out = capsys.readouterr().out
        assert "alice: retired" in out
        assert "bob: deleted" in out

    def test_nothing_to_do_is_zero(self, run):
        assert persona_retire.main([]) == 0

    def test_no_workspace_is_one(self, run):
        config, _root = run
        config.workspace_path = None
        assert persona_retire.main([]) == 1

    def test_root_unavailable_is_one(self, run, tmp_path):
        config, _root = run
        config.workspace_path = tmp_path / "offline-mount"
        assert persona_retire.main([]) == 1

    def test_a_refused_user_beside_handled_ones_is_two(self, run, tmp_path):
        config, root = run
        _plant(config, root, "alice", EDITED)
        target = tmp_path / "elsewhere.md"
        target.write_text("x")
        (_config_dir(config, root, "bob") / "PERSONA.md").symlink_to(target)
        assert persona_retire.main([]) == 2
        assert (root / "Users" / "alice" / config.bot_dir_name / "config" / "PERSONA.md.retired").exists()

    def _live_task(self, config):
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id="alice")
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))

    def test_a_task_in_flight_refuses_a_real_run(self, run, capsys):
        config, root = run
        path = _plant(config, root, "alice", EDITED)
        self._live_task(config)
        assert persona_retire.main([]) == 1
        err = capsys.readouterr().err
        assert "refusal: live_tasks" in err
        assert "stop all units first" in err
        assert path.read_text() == EDITED

    def test_an_unreadable_task_table_refuses_a_real_run(self, run, capsys):
        config, root = run
        path = _plant(config, root, "alice", EDITED)
        with db.get_db(config.db_path) as conn:
            conn.execute("DROP TABLE tasks")
        assert persona_retire.main([]) == 1
        assert "refusal: live_tasks" in capsys.readouterr().err
        assert path.read_text() == EDITED

    def test_list_runs_beside_a_task_in_flight(self, run, capsys):
        config, root = run
        _plant(config, root, "alice", EDITED)
        self._live_task(config)
        assert persona_retire.main(["--list"]) == 0
        assert "alice: would retire" in capsys.readouterr().out

    def test_list_writes_nothing(self, run, capsys):
        config, root = run
        _plant(config, root, "alice", EDITED)
        _plant(config, root, "bob", OLD_SHIPPED)
        before = _tree(root)
        assert persona_retire.main(["--list"]) == 0
        out = capsys.readouterr().out
        assert "alice: would retire" in out
        assert "bob: would delete" in out
        assert "carol: absent" in out
        assert _tree(root) == before
        assert _alerts(config) == []

    def test_dry_run_writes_nothing_and_reports_the_sync(self, run, capsys):
        config, root = run
        _plant(config, root, "alice", EDITED)
        before = _tree(root)
        with db.get_db(config.db_path) as conn:
            kv_before = conn.execute("SELECT * FROM shared_kv").fetchall()
        assert persona_retire.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "operator persona: wrote" in out
        assert "alice: would retire" in out
        assert _tree(root) == before
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT * FROM shared_kv").fetchall() == kv_before
        assert _alerts(config) == []


class TestInit:
    def test_init_retires_a_seeded_copy_after_the_sync(self, setup, monkeypatch):
        from istota import cli

        config, root = setup
        seeded = _plant(config, root, "alice", OLD_SHIPPED)
        edited = _plant(config, root, "bob", EDITED)
        monkeypatch.setattr(cli, "load_config", lambda path: config)
        assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=False)) is None
        assert (root / "PERSONA.md").read_text() == SHIPPED
        assert not seeded.exists()
        assert edited.with_name("PERSONA.md.retired").read_text() == EDITED
        assert [r[0] for r in _alerts(config)] == ["bob"]

    def test_a_refused_user_is_reported_and_does_not_change_the_exit_code(
        self, setup, monkeypatch, capsys, tmp_path,
    ):
        from istota import cli

        config, root = setup
        target = tmp_path / "elsewhere.md"
        target.write_text("x")
        (_config_dir(config, root, "alice") / "PERSONA.md").symlink_to(target)
        monkeypatch.setattr(cli, "load_config", lambda path: config)
        assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=True)) == 0
        assert "user persona alice: refused" in capsys.readouterr().err

    def test_a_notice_that_cannot_be_written_is_reported(self, setup, monkeypatch, capsys):
        # The store never raises: a failed write is a None return, so the
        # failure has to be real rather than a patched raise.
        from istota import cli

        config, root = setup
        edited = _plant(config, root, "alice", EDITED)
        with db.get_db(config.db_path) as conn:
            conn.execute("DROP TABLE notifications")
        monkeypatch.setattr(cli, "load_config", lambda path: config)
        # `init_db` would recreate the table this test removed.
        monkeypatch.setattr(cli.db, "init_db", lambda path: None)
        assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=False)) is None
        assert edited.with_name("PERSONA.md.retired").read_text() == EDITED
        err = capsys.readouterr().err
        assert "user persona alice: retired: PERSONA.md.retired; the notice could not be written" in err

    def test_a_retirement_that_raises_does_not_fail_init(self, setup, monkeypatch, capsys):
        from istota import cli

        config, _root = setup
        monkeypatch.setattr(cli, "load_config", lambda path: config)

        def _boom(_config):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(persona_retire, "retire_user_personas", _boom)
        assert cli.cmd_init(SimpleNamespace(config=None, relocate_rooms=True)) == 0
        assert "user personas: retirement failed (RuntimeError)" in capsys.readouterr().err


def test_the_seed_is_gone():
    from istota import storage

    assert not hasattr(storage, "get_user_persona_path")
