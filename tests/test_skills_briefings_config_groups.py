"""`istota-skill briefings_config group …`: group briefings from the caller's
private chat, under the authority the web group routes apply."""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from istota import db
from istota.briefings import group_store as store
from istota.config import Config, UserConfig
from istota.skills import briefings_config as skill
from istota.skills.briefings_config import main

NOW = datetime(2026, 10, 15, 7, tzinfo=timezone.utc)
PROMPT = "Write a short edition for the team."


def payload(**changes):
    value = {
        "name": "weekly", "title": "Weekly", "cron": "0 9 * * 1", "timezone": "UTC",
        "channel": "in_app", "enabled": False, "generation_mode": "prompt",
        "generation": {"version": 1, "prompt": PROMPT, "research": {"enabled": False}, "inputs": []},
    }
    value.update(changes)
    return value


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "app.db", workspace_path=tmp_path / "workspace",
                    users={name: UserConfig() for name in ("alice", "bob", "carol", "admin")},
                    admin_users={"admin"})
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
        db.add_group_member(conn, "team", "alice", role="owner", added_by="alice")
        db.add_group_member(conn, "team", "bob", added_by="alice")
    for name in config.users:
        root = config.workspace_root(name)
        root.mkdir(parents=True)
        (root / "notes.md").write_text("SELECTED FILE\n")
    return config


@pytest.fixture
def deferred(tmp_path):
    path = tmp_path / "deferred"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch, config, deferred):
    monkeypatch.setattr(skill, "_load_config", lambda: config)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.delenv("ISTOTA_WORKSPACE_PATH", raising=False)
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    as_user(monkeypatch, config, "alice")


def as_user(monkeypatch, config, user_id, **task):
    """Run as `user_id` from a fresh task: room-less unless a token is given."""
    monkeypatch.setenv("ISTOTA_USER_ID", user_id)
    columns = {key: task.pop(key) for key in ("guest_participant_id",) if key in task}
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, prompt="hi", user_id=user_id, **task)
        for name, value in columns.items():
            conn.execute(f"UPDATE tasks SET {name} = ? WHERE id = ?", (value, task_id))
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
    return task_id


def run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as exc:
        code = exc.code
    return json.loads(capsys.readouterr().out), code


def define(config, **changes):
    with db.get_db(config.db_path) as conn:
        return store.save_definition(conn, config, "team", payload(**changes),
                                     actor_id="alice", expected_revision=0)


def stored(config, name="weekly", group_id="team"):
    with db.get_db(config.db_path) as conn:
        row = conn.execute("SELECT id FROM group_briefing_configs WHERE group_id=? AND name=?",
                           (group_id, name)).fetchone()
        return store.get_definition(conn, row["id"]) if row else None


def published(config):
    """An enabled definition with one published edition."""
    definition = define(config)
    with db.get_db(config.db_path) as conn:
        definition = store.save_definition(
            conn, config, "team", payload(enabled=True), actor_id="alice",
            definition_id=definition["id"], expected_revision=definition["revision"])
        made = store.enqueue_run(conn, config, definition["id"], actor_id="alice",
                                 request_id=str(uuid4()), now=NOW)
        claim = store.claim_run(conn, run_id=made["id"], now=NOW)
        made = store.publish_run(conn, config, made["id"], claim["attempt_token"],
                                 subject="Edition", body_md="EDITION BODY", now=NOW)
    return definition, made


# -- the gate ----------------------------------------------------------------


def _shared_web_room(conn):
    room = db.create_web_chat_room(conn, "alice", "Family")
    db.add_web_room_member(conn, room.token, "bob")
    return room.token


SHARED_TURNS = ["shared_web_room", "talk_group", "guest_turn", "guest_subtask", "no_task"]


def _shared_turn(monkeypatch, config, shape):
    if shape == "shared_web_room":
        with db.get_db(config.db_path) as conn:
            token = _shared_web_room(conn)
        as_user(monkeypatch, config, "alice", source_type="web", conversation_token=token)
    elif shape == "talk_group":
        as_user(monkeypatch, config, "alice", source_type="talk",
                conversation_token="talk-group-room", is_group_chat=True)
    elif shape == "guest_turn":
        as_user(monkeypatch, config, "alice", source_type="web", guest_participant_id=7)
    elif shape == "guest_subtask":
        parent = as_user(monkeypatch, config, "alice", source_type="web", guest_participant_id=7)
        as_user(monkeypatch, config, "alice", source_type="subtask", parent_task_id=parent)
    else:
        monkeypatch.delenv("ISTOTA_TASK_ID")


class TestThePrivateChatGate:
    @pytest.mark.parametrize("shape", SHARED_TURNS)
    def test_a_shared_turn_is_refused_before_any_group_lookup(self, shape, monkeypatch, config, capsys):
        _shared_turn(monkeypatch, config, shape)

        out, code = run(capsys, ["group", "list", "--group-id", "no-such-group"])

        assert code == 1
        assert out["reason"] == "private_chat_only"

    @pytest.mark.parametrize("shape", SHARED_TURNS)
    def test_a_shared_turn_reads_no_definition(self, shape, monkeypatch, config, capsys):
        define(config)
        _shared_turn(monkeypatch, config, shape)

        out, code = run(capsys, ["group", "show", "--group-id", "team", "--name", "weekly"])

        assert code == 1
        assert out["reason"] == "private_chat_only"
        assert PROMPT not in json.dumps(out)

    def test_a_correspondents_turn_is_refused(self, monkeypatch, config, capsys):
        task_id = as_user(monkeypatch, config, "alice", source_type="web")
        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "general").token
            db.add_message(conn, token, role="user", body="hi", origin_surface="web",
                           task_id=task_id, author_label="someone@elsewhere.example")

        out, code = run(capsys, ["group", "list", "--group-id", "team"])

        assert code == 1
        assert out["reason"] == "private_chat_only"

    def test_another_users_task_is_refused(self, monkeypatch, config, capsys):
        as_user(monkeypatch, config, "bob")
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        out, code = run(capsys, ["group", "list", "--group-id", "team"])

        assert out["reason"] == "private_chat_only"

    def test_a_private_room_passes(self, monkeypatch, config, capsys):
        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "Mine").token
        as_user(monkeypatch, config, "alice", source_type="web", conversation_token=token)

        missing, code = run(capsys, ["group", "list", "--group-id", "no-such-group"])
        assert code == 1 and missing["reason"] == "group_not_found"

        out, code = run(capsys, ["group", "list", "--group-id", "team"])
        assert code == 0 and out["status"] == "ok"

    def test_a_room_less_task_passes(self, config, capsys):
        define(config)

        out, code = run(capsys, ["group", "list"])

        assert code == 0
        groups = {g["group_id"]: g for g in out["groups"]}
        assert [item["name"] for item in groups["team"]["items"]] == ["weekly"]

    def test_the_owners_subtask_passes(self, monkeypatch, config, capsys):
        parent = as_user(monkeypatch, config, "alice")
        as_user(monkeypatch, config, "alice", source_type="subtask", parent_task_id=parent)

        out, code = run(capsys, ["group", "list", "--group-id", "team"])

        assert code == 0


# -- reading -----------------------------------------------------------------


class TestShow:
    def test_the_owner_reads_the_generation(self, config, capsys):
        define(config)

        out, code = run(capsys, ["group", "show", "--group-id", "team", "--name", "weekly"])

        assert code == 0
        assert out["definition"]["generation"]["prompt"] == PROMPT

    def test_a_member_reads_no_generation(self, monkeypatch, config, capsys):
        define(config)
        as_user(monkeypatch, config, "bob")

        out, code = run(capsys, ["group", "show", "--group-id", "team", "--name", "weekly"])

        assert code == 0
        assert "generation" not in out["definition"]
        assert PROMPT not in json.dumps(out)

    def test_a_non_member_gets_group_not_found(self, monkeypatch, config, capsys):
        define(config)
        as_user(monkeypatch, config, "carol")

        out, code = run(capsys, ["group", "show", "--group-id", "team", "--name", "weekly"])

        assert code == 1
        assert out["reason"] == "group_not_found"


# -- writing -----------------------------------------------------------------


def _set(capsys, revision, *flags):
    return run(capsys, ["group", "set", "--group-id", "team", "--name", "weekly",
                        "--revision", str(revision), *flags])


class TestSet:
    def test_the_owner_changes_the_cron_and_the_revision_moves(self, config, capsys, caplog):
        definition = define(config)

        with caplog.at_level(logging.INFO, logger="istota.skills.briefings_config"):
            out, code = _set(capsys, definition["revision"], "--cron", "0 8 * * 2")

        assert code == 0, out
        after = stored(config)
        assert after["cron"] == "0 8 * * 2"
        assert after["revision"] == definition["revision"] + 1
        assert after["generation"]["prompt"] == PROMPT
        assert out["definition"]["revision"] == after["revision"]
        line = next(r.getMessage() for r in caplog.records if "operation=set" in r.getMessage())
        assert "actor=alice" in line and "outcome=ok" in line
        assert PROMPT not in caplog.text

    def test_a_stale_revision_is_a_conflict(self, config, capsys):
        definition = define(config)
        assert _set(capsys, definition["revision"], "--title", "One")[1] == 0

        out, code = _set(capsys, definition["revision"], "--title", "Two")

        assert code == 1
        assert out["reason"] == "revision_conflict"
        assert stored(config)["title"] == "One"

    def test_a_member_is_forbidden(self, monkeypatch, config, capsys):
        definition = define(config)
        as_user(monkeypatch, config, "bob")

        out, code = _set(capsys, definition["revision"], "--cron", "0 8 * * 2")

        assert out["reason"] == "briefing_forbidden"
        assert stored(config)["cron"] == "0 9 * * 1"

    def test_a_non_member_gets_group_not_found(self, monkeypatch, config, capsys):
        definition = define(config)
        as_user(monkeypatch, config, "carol")

        out, code = _set(capsys, definition["revision"], "--cron", "0 8 * * 2")

        assert out["reason"] == "group_not_found"

    def test_the_prompt_comes_from_a_file(self, config, deferred, capsys):
        definition = define(config)
        path = deferred / "prompt.md"
        path.write_text("A new prompt.\n")

        out, code = _set(capsys, definition["revision"], "--prompt-file", str(path))

        assert code == 0, out
        assert stored(config)["generation"]["prompt"] == "A new prompt."

    def test_a_prompt_file_outside_the_roots_is_refused(self, config, tmp_path, capsys):
        definition = define(config)
        outside = tmp_path / "outside.md"
        outside.write_text("nope")

        out, code = _set(capsys, definition["revision"], "--prompt-file", str(outside))

        assert code == 1
        assert out["reason"] == "host_path_refused"
        assert stored(config)["revision"] == definition["revision"]

    def test_exclude_and_include_change_the_exclusions(self, config, capsys):
        definition = define(config)
        out, code = _set(capsys, definition["revision"], "--exclude", "bob")
        assert code == 0, out
        assert stored(config)["excluded_user_ids"] == ["bob"]

        out, code = _set(capsys, definition["revision"] + 1, "--include", "bob")
        assert code == 0, out
        assert stored(config)["excluded_user_ids"] == []

    def test_an_unknown_name_creates_nothing(self, config, capsys):
        # A definition needs a generation, which only `group save` supplies.
        out, code = _set(capsys, 0, "--cron", "0 8 * * 2", "--enabled")

        assert code == 1
        assert out["reason"] == "invalid_definition"
        assert stored(config) is None

    def test_a_prompt_file_on_a_blocks_definition_is_refused(self, config, deferred, capsys):
        definition = define(config, generation_mode="blocks", generation={"version": 1, "blocks": [
            {"title": "News", "directive": "", "render_mode": "synthesis", "options": {},
             "sources": [{"id": "news", "kind": "browse", "config": {"preset": "bbc"}}]}]})
        path = deferred / "prompt.md"
        path.write_text("A new prompt.\n")

        out, code = _set(capsys, definition["revision"], "--prompt-file", str(path))

        assert out["reason"] == "invalid_argument"
        assert stored(config)["revision"] == definition["revision"]

    def test_nothing_to_change_is_refused(self, config, capsys):
        definition = define(config)

        out, code = _set(capsys, definition["revision"])

        assert out["reason"] == "invalid_argument"


class TestSave:
    def test_a_file_creates_a_disabled_draft(self, config, deferred, capsys):
        path = deferred / "weekly.json"
        path.write_text(json.dumps(payload()))

        out, code = run(capsys, ["group", "save", "--group-id", "team", "--file", str(path),
                                 "--revision", "0"])

        assert code == 0, out
        assert stored(config)["generation"]["prompt"] == PROMPT

    def test_an_enabled_create_is_refused(self, config, deferred, capsys):
        path = deferred / "weekly.json"
        path.write_text(json.dumps(payload(enabled=True)))

        out, code = run(capsys, ["group", "save", "--group-id", "team", "--file", str(path),
                                 "--revision", "0"])

        assert out["reason"] == "draft_required"

    def test_an_unknown_field_is_refused(self, config, deferred, capsys):
        path = deferred / "weekly.json"
        path.write_text(json.dumps({**payload(), "created_by": "mallory"}))

        out, code = run(capsys, ["group", "save", "--group-id", "team", "--file", str(path),
                                 "--revision", "0"])

        assert out["reason"] == "invalid_argument"
        assert stored(config) is None

    def test_a_file_updates_an_existing_definition(self, config, deferred, capsys):
        definition = define(config)
        path = deferred / "weekly.json"
        path.write_text(json.dumps(payload(title="Renamed")))

        out, code = run(capsys, ["group", "save", "--group-id", "team", "--file", str(path),
                                 "--revision", str(definition["revision"])])

        assert code == 0, out
        assert stored(config)["title"] == "Renamed"


class TestRun:
    def test_a_preview_gets_a_fresh_request_id(self, config, capsys):
        define(config)

        first, code = run(capsys, ["group", "run", "--group-id", "team", "--name", "weekly",
                                   "--mode", "preview"])
        second, _ = run(capsys, ["group", "run", "--group-id", "team", "--name", "weekly",
                                 "--mode", "preview"])

        assert code == 0, first
        assert first["edition"]["mode"] == "preview"
        assert first["request_id"] != second["request_id"]
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM group_briefing_runs").fetchone()[0] == 2

    def test_publishing_a_disabled_definition_is_refused(self, config, capsys):
        define(config)

        out, code = run(capsys, ["group", "run", "--group-id", "team", "--name", "weekly",
                                 "--mode", "publish"])

        assert code == 1
        assert out["reason"] == "definition_disabled"

    def test_a_member_cannot_run(self, monkeypatch, config, capsys):
        define(config)
        as_user(monkeypatch, config, "bob")

        out, code = run(capsys, ["group", "run", "--group-id", "team", "--name", "weekly",
                                 "--mode", "preview"])

        assert out["reason"] == "briefing_forbidden"


class TestEditions:
    def test_the_edition_body_is_fenced(self, monkeypatch, config, capsys):
        _, made = published(config)
        as_user(monkeypatch, config, "bob")

        out, code = run(capsys, ["group", "edition", "--group-id", "team", "--name", "weekly",
                                 "--run-id", str(made["id"])])

        assert code == 0, out
        body = out["edition"]["body_md"]
        assert "EDITION BODY" in body
        assert body.startswith("[UNTRUSTED GROUP BRIEFING EDITION")
        assert "document" not in out["edition"]

    def test_runs_lists_the_published_edition(self, monkeypatch, config, capsys):
        _, made = published(config)
        as_user(monkeypatch, config, "bob")

        out, code = run(capsys, ["group", "runs", "--group-id", "team", "--name", "weekly"])

        assert code == 0
        assert [item["id"] for item in out["items"]] == [made["id"]]

    def test_audience_counts_for_the_owner(self, config, capsys):
        published(config)

        out, code = run(capsys, ["group", "audience", "--group-id", "team", "--name", "weekly"])

        assert code == 0
        assert out["member_count"] == 2


class TestDelete:
    def _delete(self, capsys, revision, *flags):
        return run(capsys, ["group", "delete", "--group-id", "team", "--name", "weekly",
                            "--revision", str(revision), *flags])

    def test_without_confirmed_nothing_changes(self, config, capsys):
        definition = define(config)

        out, code = self._delete(capsys, definition["revision"])

        assert code == 1
        assert out["reason"] == "confirmation_required"
        assert out["would"] == {"group_id": "team", "name": "weekly", "enabled": False,
                                "revision": definition["revision"]}
        assert stored(config)["deleted_at"] is None

    def test_an_enabled_definition_must_be_disabled_first(self, config, capsys):
        definition, _ = published(config)

        out, code = self._delete(capsys, definition["revision"], "--confirmed")

        assert out["reason"] == "disable_first"

    def test_a_draft_is_soft_deleted(self, config, capsys):
        definition = define(config)

        out, code = self._delete(capsys, definition["revision"], "--confirmed")

        assert code == 0, out
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT deleted_at FROM group_briefing_configs WHERE id=?",
                               (definition["id"],)).fetchone()
        assert row["deleted_at"] is not None

    def test_a_member_sees_no_would(self, monkeypatch, config, capsys):
        definition = define(config)
        as_user(monkeypatch, config, "bob")

        out, code = self._delete(capsys, definition["revision"])

        assert out["reason"] == "briefing_forbidden"
        assert "would" not in out


class TestDelivery:
    def test_a_member_mutes_their_own_delivery(self, monkeypatch, config, capsys):
        definition = define(config)
        as_user(monkeypatch, config, "bob")

        out, code = run(capsys, ["group", "delivery", "--group-id", "team", "--name", "weekly",
                                 "--outbound-muted"])

        assert code == 0, out
        assert out["outbound_muted"] is True and out["in_app_muted"] is False
        with db.get_db(config.db_path) as conn:
            assert store.delivery_preferences(conn, "team", "bob", definition_id=definition["id"],
                                              include_group=False)["outbound_muted"] is True
            assert store.delivery_preferences(conn, "team", "alice", definition_id=definition["id"],
                                              include_group=False)["outbound_muted"] is False

    def test_without_flags_it_reads(self, monkeypatch, config, capsys):
        define(config)
        as_user(monkeypatch, config, "bob")

        out, code = run(capsys, ["group", "delivery", "--group-id", "team"])

        assert code == 0
        assert out["outbound_muted"] is False


# -- source approvals --------------------------------------------------------


def _with_personal_source(config):
    with db.get_db(config.db_path) as conn:
        definition = store.save_definition(conn, config, "team", payload(generation={
            "version": 1, "prompt": PROMPT, "research": {"enabled": False}, "inputs": [
                {"id": "selected", "kind": "personal_file",
                 "config": {"owner_id": "bob", "path": "notes.md"}}]}),
            actor_id="alice", expected_revision=0)
        from istota.briefings.source_grants import approve_source, source_approval_state
        state = source_approval_state(conn, config, definition["id"], "selected", actor_id="bob")
        approve_source(conn, config, definition["id"], "selected", actor_id="bob",
                       expected_definition_revision=definition["revision"],
                       expected_grant_revision=state["grant_revision"], acknowledged=True,
                       audience_contract=state["audience_contract"], now=NOW)
        state = source_approval_state(conn, config, definition["id"], "selected", actor_id="bob")
    return definition, state


class TestApprovals:
    def test_the_contributor_reads_and_revokes_their_approval(self, monkeypatch, config, capsys):
        _, state = _with_personal_source(config)
        as_user(monkeypatch, config, "bob")

        listed, code = run(capsys, ["group", "approvals", "--group-id", "team", "--name", "weekly"])
        assert code == 0, listed
        assert [a["source_id"] for a in listed["approvals"]] == ["selected"]

        argv = ["group", "revoke-approval", "--group-id", "team", "--name", "weekly",
                "--source-id", "selected", "--grant-revision", str(state["grant_revision"])]
        asked, code = run(capsys, argv)
        assert asked["reason"] == "confirmation_required"
        assert asked["would"] == {"group_id": "team", "name": "weekly", "source_id": "selected"}

        out, code = run(capsys, [*argv, "--confirmed"])
        assert code == 0, out
        with db.get_db(config.db_path) as conn:
            live = conn.execute("SELECT COUNT(*) FROM group_briefing_source_grants "
                                "WHERE revoked_at IS NULL").fetchone()[0]
        assert live == 0

    def test_someone_else_cannot_revoke_it(self, config, capsys):
        _, state = _with_personal_source(config)

        out, code = run(capsys, ["group", "revoke-approval", "--group-id", "team", "--name", "weekly",
                                 "--source-id", "selected", "--grant-revision",
                                 str(state["grant_revision"]), "--confirmed"])

        assert code == 1
        assert out["reason"] == "source_not_found"
        with db.get_db(config.db_path) as conn:
            live = conn.execute("SELECT COUNT(*) FROM group_briefing_source_grants "
                                "WHERE revoked_at IS NULL").fetchone()[0]
        assert live == 1


# -- All users ---------------------------------------------------------------


class TestAllUsers:
    def _all_users(self, config):
        from istota.groups.system import reconcile_all_users
        with db.get_db(config.db_path) as conn:
            group = reconcile_all_users(conn, config)
            definition = store.save_definition(conn, config, group["group_id"], payload(),
                                               actor_id="admin", expected_revision=0, is_admin=True)
        return group["group_id"], definition

    def test_an_admin_edits_an_all_users_definition(self, monkeypatch, config, capsys):
        group_id, definition = self._all_users(config)
        as_user(monkeypatch, config, "admin")

        out, code = run(capsys, ["group", "set", "--group-id", group_id, "--name", "weekly",
                                 "--revision", str(definition["revision"]), "--title", "Everyone"])

        assert code == 0, out
        assert stored(config, group_id=group_id)["title"] == "Everyone"

    def test_a_non_admin_is_forbidden(self, monkeypatch, config, capsys):
        group_id, definition = self._all_users(config)
        as_user(monkeypatch, config, "carol")

        out, code = run(capsys, ["group", "set", "--group-id", group_id, "--name", "weekly",
                                 "--revision", str(definition["revision"]), "--title", "Everyone"])

        assert out["reason"] == "briefing_forbidden"


class TestUnavailable:
    def test_a_database_error_is_groups_unavailable(self, monkeypatch, config, capsys, caplog):
        import sqlite3

        from istota.briefings import group_service

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(group_service, "definitions", broken)
        with caplog.at_level(logging.WARNING, logger="istota.skills.briefings_config"):
            out, code = run(capsys, ["group", "list", "--group-id", "team"])

        assert code == 1
        assert out["reason"] == "groups_unavailable"
        assert "outcome=unavailable" in caplog.text
