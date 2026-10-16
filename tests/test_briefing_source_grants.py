"""Recurring disclosure is approved by the resource owner, for one audience."""

import copy
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree as ET

import pytest

from istota import db
from istota.briefings import group_store as store
from istota.config import Config, UserConfig
from istota.groups.policy import GroupError

NOW = datetime(2026, 10, 15, 7, tzinfo=timezone.utc)
CALENDAR = "https://calendar.example.com/calendars/bob/team/"


@pytest.fixture
def env(tmp_path):
    config = Config(db_path=tmp_path / "app.db", workspace_path=tmp_path / "workspace",
                    users={name: UserConfig() for name in ("alice", "bob", "carol", "admin")})
    config.email.enabled = True
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
        for name in ("alice", "bob", "carol"):
            db.add_group_member(conn, "team", name, role="owner" if name == "alice" else "member", added_by="alice")
    for name in config.users:
        root = config.workspace_root(name)
        root.mkdir(parents=True)
        (root / "notes.md").write_text("SELECTED FILE\n")
        (root / "other.md").write_text("UNSELECTED FILE CANARY\n")
    return config


def payload(kind="personal_file", source_config=None, **changes):
    value = {"name": "daily", "title": "Team update", "cron": "0 7 * * *", "timezone": "UTC",
             "channel": "email", "enabled": False, "generation_mode": "prompt",
             "generation": {"version": 1, "prompt": "Summarize the selected input.", "inputs": [
                 {"id": "selected", "kind": kind, "config": source_config or {"owner_id": "bob", "path": "notes.md"}}
             ]}}
    value.update(changes)
    return value


def define(conn, env, **kwargs):
    return store.save_definition(conn, env, "team", payload(**kwargs), actor_id="alice", expected_revision=0)


def approve(conn, env, definition, **changes):
    from istota.briefings.source_grants import approve_source, source_approval_state
    state = source_approval_state(conn, env, definition["id"], "selected", actor_id=changes.get("actor_id", "bob"))
    args = {"actor_id": "bob", "expected_definition_revision": definition["revision"],
            "expected_grant_revision": state["grant_revision"], "acknowledged": True,
            "audience_contract": state["audience_contract"], "now": NOW}
    args.update(changes)
    return approve_source(conn, env, definition["id"], "selected", **args)


def enable(conn, env, definition, **changes):
    raw = payload(**changes)
    raw["enabled"] = True
    return store.save_definition(conn, env, "team", raw, actor_id="alice", definition_id=definition["id"],
                                 expected_revision=definition["revision"])


def test_owner_approval_is_distinct_and_snapshotted_at_admission(env):
    from istota.briefings.source_grants import resolve_source_grant, source_approval_state
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        with pytest.raises(GroupError, match="approval"):
            enable(conn, env, definition)
        with pytest.raises(GroupError):
            approve(conn, env, definition, actor_id="alice")
        with pytest.raises(GroupError):
            approve(conn, env, definition, actor_id="admin")
        with pytest.raises(GroupError):
            approve(conn, env, definition, acknowledged=False)
        state = source_approval_state(conn, env, definition["id"], "selected", actor_id="alice")
        assert not state["approved"] and state["member_count"] == 3
        assert "future members" in state["warning"] and "cannot be recalled" in state["warning"]
        assert "full selected file" in state["warning"] and "each run" in state["warning"]
        with pytest.raises(GroupError) as exc:
            source_approval_state(conn, env, definition["id"], "selected", actor_id="carol")
        assert exc.value.status_code == 404
        grant = approve(conn, env, definition)
        assert grant["owner_id"] == "bob" and grant["revision"] == 1
        assert resolve_source_grant(conn, env, definition, "selected")["id"] == grant["id"]
        definition = enable(conn, env, definition)
        run = store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)
        assert run["snapshot"]["grants"] == [{"source_id": "selected", "id": grant["id"], "revision": 1}]


@pytest.mark.parametrize("change", ["path", "owner", "channel", "fields", "window"])
def test_source_changes_invalidate_grants_but_prompt_edits_preserve_them(env, change, monkeypatch):
    from istota.briefings.source_grants import resolve_source_grant
    calendar_fixture(env, monkeypatch)
    is_calendar = change in ("fields", "window")
    kwargs = {"kind": "personal_calendar", "source_config": {"owner_id": "bob", "calendar_url": CALENDAR}} if is_calendar else {}
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, **kwargs)
        grant = approve(conn, env, definition)
        raw = payload(**kwargs)
        raw["generation"]["prompt"] = "Use a warmer tone."
        updated = store.save_definition(conn, env, "team", raw, actor_id="alice", definition_id=definition["id"], expected_revision=1)
        assert resolve_source_grant(conn, env, updated, "selected")["id"] == grant["id"]
        raw = copy.deepcopy(raw)
        source = raw["generation"]["inputs"][0]["config"]
        if change == "path":
            source["path"] = "other.md"
        elif change == "owner":
            source["owner_id"] = "alice"
        elif change == "channel":
            raw["channel"] = "in_app"
        elif change == "fields":
            source["fields"] = ["time", "title", "description"]
        else:
            source["lookahead_days"] = 7
        updated = store.save_definition(conn, env, "team", raw, actor_id="alice", definition_id=definition["id"], expected_revision=2)
        with pytest.raises(GroupError):
            resolve_source_grant(conn, env, updated, "selected")
        row = conn.execute("SELECT revoked_at FROM group_briefing_source_grants WHERE id = ?", (grant["id"],)).fetchone()
        assert row["revoked_at"]


def test_revoke_blocks_publication_cancels_pending_sends_and_preserves_history(env):
    from istota.briefings.source_grants import revoke_source
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        grant = approve(conn, env, definition)
        definition = enable(conn, env, definition)
        published = store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)
        claim = store.claim_run(conn, run_id=published["id"], now=NOW)
        store.publish_run(conn, env, claim["id"], claim["attempt_token"], subject="Team", body_md="Retained edition", now=NOW)
        pending = store.enqueue_run(conn, env, definition["id"], actor_id="alice", request_id=str(uuid4()), now=NOW)
        claim = store.claim_run(conn, run_id=pending["id"], now=NOW)
        with pytest.raises(GroupError):
            revoke_source(conn, env, definition["id"], "selected", actor_id="alice", expected_grant_revision=1)
        revoke_source(conn, env, definition["id"], "selected", actor_id="bob", expected_grant_revision=grant["revision"], now=NOW)
        with pytest.raises(GroupError):
            store.publish_run(conn, env, claim["id"], claim["attempt_token"], subject="Leak", body_md="Discard", now=NOW)
        assert store.get_run(conn, pending["id"])["state"] == "cancelled"
        assert store.get_run(conn, published["id"])["body_md"] == "Retained edition"
        assert {r["state"] for r in store.list_recipients(conn, published["id"])} == {"skipped"}
        renewed = approve(conn, env, definition)
        assert renewed["id"] != grant["id"] and renewed["revision"] > grant["revision"]


@pytest.mark.parametrize("loss", ["remove", "disable", "rejoin"])
def test_owner_membership_interval_and_account_eligibility_are_live(env, loss):
    from istota.briefings.source_grants import resolve_source_grant
    from istota.briefings.sources.contributions import read_contribution
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        approve(conn, env, definition)
        if loss == "disable":
            del env.users["bob"]
        else:
            db.end_group_membership(conn, "team", "bob", ended_by="alice")
            if loss == "rejoin":
                db.add_group_member(conn, "team", "bob", added_by="alice")
            conn.commit()
        with pytest.raises(GroupError):
            resolve_source_grant(conn, env, definition, "selected")
        with pytest.raises(GroupError):
            read_contribution(conn, env, definition["id"], "selected", now=NOW)


@pytest.mark.parametrize("config", [
    {"owner_id": "bob", "path": "../alice/notes.md"}, {"owner_id": "bob", "path": "/etc/file.md"},
    {"owner_id": "bob", "path": "notes.md", "trusted": True}, {"owner_id": "bob", "path": "folder"},
    {"owner_id": "bob", "calendar_url": CALENDAR, "fields": ["credentials"]},
    {"owner_id": "bob", "calendar_url": CALENDAR, "lookahead_days": 100000},
    {"owner_id": "bob", "calendar_url": CALENDAR, "include_private": "false"},
])
def test_invalid_resource_configs_are_rejected_at_draft_save(env, config):
    kind = "personal_calendar" if "calendar_url" in config else "personal_file"
    with db.get_db(env.db_path) as conn, pytest.raises(GroupError) as exc:
        define(conn, env, kind=kind, source_config=config)
    assert exc.value.status_code == 422


def test_all_users_approval_binds_future_installation_members_and_retained_history(env):
    from istota.briefings.source_grants import source_approval_state
    from istota.groups.system import ensure_all_users_group
    with db.get_db(env.db_path) as conn:
        group = ensure_all_users_group(conn, env)
        definition = store.save_definition(conn, env, group["group_id"], payload(), actor_id="admin", is_admin=True, expected_revision=0)
        state = source_approval_state(conn, env, definition["id"], "selected", actor_id="bob")
        assert "everyone currently using this installation and anyone who joins later" in state["warning"]
        contract = state["audience_contract"]
        assert contract["version"] == 1 and contract["future_members"] and contract["retained_history"]
        with pytest.raises(GroupError):
            approve(conn, env, definition, audience_contract={**contract, "future_members": False})
        approve(conn, env, definition)


@pytest.mark.parametrize("hazard", ["symlink", "parent_symlink", "fifo", "oversize", "encoding", "missing", "root_missing"])
def test_file_adapter_uses_only_the_exact_pinned_regular_utf8_file(env, hazard, tmp_path, caplog):
    from istota.briefings.sources.contributions import read_contribution
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        grant = approve(conn, env, definition)
        assert read_contribution(conn, env, definition["id"], "selected", now=NOW)["content"] == "SELECTED FILE\n"
        root = env.workspace_root("bob")
        selected = root / "notes.md"
        selected.unlink()
        if hazard == "symlink":
            selected.symlink_to(env.workspace_root("alice") / "other.md")
        elif hazard == "parent_symlink":
            nested = root / "folder"
            nested.mkdir()
            (nested / "notes.md").write_text("Selected")
            raw = payload(source_config={"owner_id": "bob", "path": "folder/notes.md"})
            definition = store.save_definition(conn, env, "team", raw, actor_id="alice", definition_id=definition["id"], expected_revision=1)
            grant = approve(conn, env, definition)
            nested.rename(root / "original")
            nested.symlink_to(env.workspace_root("alice"), target_is_directory=True)
        elif hazard == "fifo":
            os.mkfifo(selected)
        elif hazard == "oversize":
            selected.write_bytes(b"x" * (65536 + 1))
        elif hazard == "encoding":
            selected.write_bytes(b"\xff")
        elif hazard == "root_missing":
            root.rename(root.with_name("unavailable"))
        result = read_contribution(conn, env, definition["id"], "selected", expected_grant={"source_id": "selected", "id": grant["id"], "revision": grant["revision"]}, now=NOW)
        assert result["status"] == ("missing" if hazard == "missing" else "refused")
        assert "CANARY" not in json.dumps(result) + caplog.text


def calendar_fixture(env, monkeypatch):
    from istota.skills import calendar
    env.caldav.url = "https://calendar.example.com/"
    env.caldav.username = "service"
    env.caldav.password = "fixture-value"
    owner = ET.fromstring('<d:owner xmlns:d="DAV:"><d:href>/principals/users/bob/</d:href></d:owner>')
    selected = SimpleNamespace(name="Team", url=CALENDAR, get_properties=lambda *a, **kw: {"{DAV:}owner": owner})
    class Client:
        searches = []
        on_search = None
        outside_window = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def principal(self):
            return SimpleNamespace(calendars=lambda: [selected])
        def calendar(self, url):
            assert url == CALENDAR
            def search(**kwargs):
                self.searches.append(kwargs)
                if self.on_search:
                    self.on_search()
                start = "20261001T090000Z" if self.outside_window else "20261015T090000Z"
                end = "20261001T100000Z" if self.outside_window else "20261015T100000Z"
                return [SimpleNamespace(data=(
                    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:test\r\n"
                    f"DTSTART:{start}\r\nDTEND:{end}\r\n"
                    f"SUMMARY:{title}\r\nCLASS:{privacy}\r\nDESCRIPTION:DESCRIPTION CANARY\r\n"
                    "LOCATION:LOCATION CANARY\r\nATTENDEE:mailto:alice@example.com\r\n"
                    "URL:https://example.com/event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
                )) for title, privacy in (("Planning", "PUBLIC"), ("PRIVATE CANARY", "PRIVATE"), ("CONFIDENTIAL CANARY", "CONFIDENTIAL"))]
            return SimpleNamespace(search=search)
    client = Client()
    monkeypatch.setattr(calendar, "get_caldav_client", lambda *a, **kw: client)
    return client


def test_calendar_adapter_filters_private_events_and_projects_before_return(env, monkeypatch, caplog):
    from istota.briefings.sources.contributions import read_contribution
    client = calendar_fixture(env, monkeypatch)
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config={"owner_id": "bob", "calendar_url": CALENDAR})
        approve(conn, env, definition)
        result = read_contribution(conn, env, definition["id"], "selected", now=NOW)
        assert result["status"] == "readable" and len(result["items"]) == 1
        assert set(result["items"][0]) == {"time", "title"}
        assert result["items"][0]["title"] == "Planning"
        assert "CANARY" not in json.dumps(result) + caplog.text
        assert len(client.searches) == 1
        assert client.searches[0]["start"] == NOW.replace(hour=0)
        assert client.searches[0]["end"] == NOW.replace(day=16, hour=0)


def test_calendar_adapter_enforces_window_on_provider_results(env, monkeypatch):
    from istota.briefings.sources.contributions import read_contribution
    client = calendar_fixture(env, monkeypatch)
    client.outside_window = True
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config={"owner_id": "bob", "calendar_url": CALENDAR})
        approve(conn, env, definition)
        assert read_contribution(conn, env, definition["id"], "selected", now=NOW)["items"] == []


def test_wider_calendar_projection_and_private_events_require_separate_approval(env, monkeypatch):
    from istota.briefings.source_grants import source_approval_state
    from istota.briefings.sources.contributions import read_contribution
    calendar_fixture(env, monkeypatch)
    selected = {"owner_id": "bob", "calendar_url": CALENDAR, "fields": ["title", "description", "attendees", "links", "location"],
                "include_private": True, "lookback_days": 7, "lookahead_days": 7}
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config=selected)
        state = source_approval_state(conn, env, definition["id"], "selected", actor_id="bob")
        assert "attendees" in state["warning"] and "Private-marked events are included" in state["warning"]
        assert "7 days back, 7 days ahead" in state["warning"]
        with pytest.raises(GroupError):
            read_contribution(conn, env, definition["id"], "selected", now=NOW)
        approve(conn, env, definition)
        result = read_contribution(conn, env, definition["id"], "selected", now=NOW)
        assert len(result["items"]) == 3
        assert set(result["items"][0]) == set(selected["fields"])
        assert result["items"][0]["attendees"] == ["mailto:alice@example.com"]
        assert result["items"][0]["links"] == ["https://example.com/event"]


def test_other_calendar_cannot_be_approved_or_fetched(env, monkeypatch):
    calendar_fixture(env, monkeypatch)
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config={
            "owner_id": "bob", "calendar_url": "https://calendar.example.com/calendars/alice/private/"})
        with pytest.raises(GroupError) as exc:
            approve(conn, env, definition)
        assert exc.value.code == "calendar_not_found"
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_source_grants").fetchone()[0] == 0


def test_revocation_during_provider_read_discards_source_bytes(env, monkeypatch):
    from istota.briefings.source_grants import revoke_source
    from istota.briefings.sources.contributions import read_contribution
    client = calendar_fixture(env, monkeypatch)
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config={"owner_id": "bob", "calendar_url": CALENDAR})
        grant = approve(conn, env, definition)
        def revoke():
            with db.get_db(env.db_path) as writer:
                revoke_source(writer, env, definition["id"], "selected", actor_id="bob", expected_grant_revision=grant["revision"])
        client.on_search = revoke
        with pytest.raises(GroupError) as exc:
            read_contribution(conn, env, definition["id"], "selected", now=NOW)
        assert "CANARY" not in str(exc.value)


def test_calendar_outage_is_bounded_and_contains_no_provider_error(env, monkeypatch, caplog):
    from istota.briefings.sources.contributions import read_contribution
    client = calendar_fixture(env, monkeypatch)
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config={"owner_id": "bob", "calendar_url": CALENDAR})
        approve(conn, env, definition)
        def unavailable():
            raise OSError("PRIVATE PROVIDER ERROR CANARY")
        client.on_search = unavailable
        result = read_contribution(conn, env, definition["id"], "selected", now=NOW)
        assert result == {"status": "unavailable", "error_code": "calendar_unavailable"}
        assert "CANARY" not in json.dumps(result) + caplog.text


def test_grant_cannot_be_replayed_into_duplicate_definition_or_stale_read(env):
    from istota.briefings.source_grants import grant_snapshot
    from istota.briefings.sources.contributions import read_contribution
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        grant = approve(conn, env, definition)
        duplicate = define(conn, env, name="duplicate")
        with pytest.raises(GroupError):
            read_contribution(conn, env, duplicate["id"], "selected", expected_grant=grant_snapshot(grant), now=NOW)
        renewed = approve(conn, env, definition)
        assert renewed["id"] != grant["id"]
        with pytest.raises(GroupError):
            read_contribution(conn, env, definition["id"], "selected", expected_grant=grant_snapshot(grant), now=NOW)


def test_stale_approval_and_self_authored_contribution_still_require_confirmation(env):
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, source_config={"owner_id": "alice", "path": "notes.md"})
        with pytest.raises(GroupError):
            approve(conn, env, definition, actor_id="alice", acknowledged=False)
        approve(conn, env, definition, actor_id="alice")
        with pytest.raises(GroupError):
            approve(conn, env, definition, actor_id="alice", expected_grant_revision=0)
        with pytest.raises(GroupError):
            approve(conn, env, definition, actor_id="alice", expected_definition_revision=0)


def test_resource_owner_can_select_only_their_requested_source_in_a_disabled_draft(env):
    from istota.briefings.source_grants import resolve_source_grant
    with db.get_db(env.db_path) as conn:
        raw = payload(source_config={"owner_id": "bob"})
        raw["generation"]["inputs"].append({"id": "public", "kind": "markets", "config": {"symbol": "ABC"}})
        definition = store.save_definition(conn, env, "team", raw, actor_id="alice", expected_revision=0)
        selected = {"owner_id": "bob", "path": "notes.md"}
        grant = approve(conn, env, definition, resource_config=selected)
        updated = store.get_definition(conn, definition["id"])
        assert updated["revision"] == 2 and updated["updated_by"] == "bob" and not updated["enabled"]
        assert updated["generation"]["inputs"][0]["config"] == selected
        assert updated["generation"]["inputs"][1] == definition["generation"]["inputs"][1]
        for field in ("title", "name", "group_id", "channel", "cron", "timezone", "generation_mode"):
            assert updated[field] == definition[field]
        assert updated["generation"]["prompt"] == definition["generation"]["prompt"]
        assert resolve_source_grant(conn, env, updated, "selected")["id"] == grant["id"]
        with pytest.raises(GroupError):
            approve(conn, env, updated, resource_config={"owner_id": "alice", "path": "notes.md"})
        with pytest.raises(GroupError):
            approve(conn, env, updated, resource_config={**selected, "channel": "in_app"})
        raw = payload()
        raw["enabled"] = True
        definition = store.save_definition(conn, env, "team", raw, actor_id="alice", definition_id=updated["id"], expected_revision=2)
        with pytest.raises(GroupError) as exc:
            approve(conn, env, definition, resource_config={"owner_id": "bob", "path": "other.md"})
        assert exc.value.code == "definition_disabled_required"


def test_requested_calendar_preview_discloses_owners_exact_selection(env, monkeypatch):
    from istota.briefings.source_grants import source_approval_state
    calendar_fixture(env, monkeypatch)
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, kind="personal_calendar", source_config={"owner_id": "bob"})
        selected = {"owner_id": "bob", "calendar_url": CALENDAR, "fields": ["time", "title", "description"], "lookahead_days": 7}
        state = source_approval_state(conn, env, definition["id"], "selected", actor_id="bob", resource_config=selected)
        assert "description" in state["warning"] and "7 days ahead" in state["warning"]
        assert CALENDAR in state["warning"] and not state["approved"]
        assert store.get_definition(conn, definition["id"])["generation"]["inputs"][0]["config"] == {"owner_id": "bob"}
        with pytest.raises(GroupError):
            source_approval_state(conn, env, definition["id"], "selected", actor_id="alice", resource_config=selected)
        approve(conn, env, definition, resource_config=selected)


def test_run_checks_live_owner_removal_even_before_delivery_worker_exists(env):
    from istota.briefings.source_grants import validate_run_grants
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        approve(conn, env, definition)
        definition = enable(conn, env, definition)
        run = store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        db.end_group_membership(conn, "team", "bob", ended_by="alice")
        conn.commit()
        with pytest.raises(GroupError):
            validate_run_grants(conn, env, run)
        with pytest.raises(GroupError):
            store.publish_run(conn, env, claim["id"], claim["attempt_token"], subject="Leak", body_md="Discard", now=NOW)
        assert not store.list_recipients(conn, run["id"])


@pytest.mark.parametrize("lifecycle", ["leave", "disable", "delete_profile", "config_refresh"])
def test_lifecycle_revokes_consent_and_cancels_queued_sends_without_revival(env, lifecycle):
    from istota import user_profiles
    from istota.briefings.source_grants import resolve_source_grant
    from istota.groups.system import ensure_all_users_group, reconcile_all_users
    from istota.webui import auth
    user_profiles.ensure_profile(env.db_path, "bob")
    auth.upsert_identity(env.db_path, "bob", "bob@example.com")
    with db.get_db(env.db_path) as conn:
        ensure_all_users_group(conn, env)
        definition = define(conn, env)
        grant = approve(conn, env, definition)
        definition = enable(conn, env, definition)
        run = store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="Team", body_md="Published", now=NOW)
        if lifecycle == "leave":
            db.end_group_membership(conn, "team", "bob", ended_by="alice")
            db.add_group_member(conn, "team", "bob", added_by="alice")
            conn.commit()
        elif lifecycle == "disable":
            auth.set_disabled(env.db_path, "bob", True)
            auth.set_disabled(env.db_path, "bob", False)
        elif lifecycle == "delete_profile":
            user_profiles.delete_profile(env.db_path, "bob")
            user_profiles.ensure_profile(env.db_path, "bob")
        else:
            user_profiles.delete_profile(env.db_path, "bob")
            # A config-only account has no profile-delete callback on removal.
            renewed = approve(conn, env, definition)
            grant = renewed
            del env.users["bob"]
            reconcile_all_users(conn, env, registry_complete=False)
            assert conn.execute("SELECT revoked_at FROM group_briefing_source_grants WHERE id = ?", (grant["id"],)).fetchone()[0] is None
            reconcile_all_users(conn, env, registry_complete=True)
            env.users["bob"] = UserConfig()
            reconcile_all_users(conn, env)
        with pytest.raises(GroupError):
            resolve_source_grant(conn, env, definition, "selected")
        assert conn.execute("SELECT revoked_at FROM group_briefing_source_grants WHERE id = ?", (grant["id"],)).fetchone()[0]
        assert {r["state"] for r in store.list_recipients(conn, run["id"])} == {"skipped"}


def test_grants_schema_migration_is_repeatable_with_schema_parity(env):
    with db.get_db(env.db_path) as conn:
        conn.execute("DROP TABLE group_briefing_source_grants")
        conn.commit()
        db._run_migrations(conn)
        db._run_migrations(conn)
        declared = sqlite3.connect(":memory:")
        declared.executescript((Path(__file__).resolve().parents[1] / "schema.sql").read_text())
        assert [tuple(row) for row in conn.execute("PRAGMA table_info(group_briefing_source_grants)")] == declared.execute("PRAGMA table_info(group_briefing_source_grants)").fetchall()
        assert sorted(tuple(row)[1:4] for row in conn.execute("PRAGMA index_list(group_briefing_source_grants)")) == sorted(tuple(row)[1:4] for row in declared.execute("PRAGMA index_list(group_briefing_source_grants)"))
        declared.close()


def test_deleting_a_definition_revokes_its_grants_and_closes_its_requests(env):
    from istota.notifications.resolvers.group_briefing import RESOLVER
    from istota.notifications.sources import NotificationRow
    with db.get_db(env.db_path) as conn:
        approved = define(conn, env)
        approve(conn, env, approved)
        requested = define(conn, env, name="weekly")
        requests = "SELECT * FROM notifications WHERE source='group_briefing' AND object_type='group_source' " \
                   "AND object_id=? AND state='open'"
        open_row = conn.execute(requests, (str(requested["id"]),)).fetchone()
        assert open_row is not None
        for definition in (approved, requested):
            store.delete_definition(conn, definition["id"], admin_id="admin")
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_source_grants WHERE definition_id=? "
                            "AND revoked_at IS NULL", (approved["id"],)).fetchone()[0] == 0
        assert conn.execute(requests, (str(requested["id"]),)).fetchone() is None
        # The resolver is the backstop: a request for a deleted definition is gone.
        row = NotificationRow(id=open_row["id"], user_id=open_row["user_id"], source=open_row["source"],
                              dedup_key=open_row["dedup_key"], object_type="group_source",
                              object_id=open_row["object_id"], severity="info", actionable=True, title="", body="",
                              params=json.loads(open_row["params"]))
        assert RESOLVER.resolve(env, conn, row) is None


def test_hold_writes_no_approval_request_and_resume_writes_one(env):
    with db.get_db(env.db_path) as conn:
        requested = define(conn, env, name="weekly")
        rows = "SELECT state FROM notifications WHERE source='group_briefing' AND object_type='group_source' " \
               "AND object_id=?"

        def states():
            return sorted(r["state"] for r in conn.execute(rows, (str(requested["id"]),)))

        assert states() == ["open"]
        store.set_hold(conn, env, requested["id"], admin_id="admin")
        assert "open" not in states()
        assert len(states()) == 1
        store.clear_hold(conn, env, requested["id"])
        assert states().count("open") == 1
        assert len(states()) == 2
