"""Workspace reach follows the real planner and live membership.

Mount extraction belongs to security parity row 1; its artifact witness stays
in tests/smoke/test_sandbox_in_stack.py. These tests inspect the plan only.
"""
import hashlib
import logging

import pytest

from istota import db, doctor, executor
from istota.config import Config, ResourceConfig, UserConfig
from istota.sandbox import plan


@pytest.fixture
def config(tmp_path):
    c = Config(db_path=tmp_path / "state" / "istota.db", workspace_path=tmp_path / "workspace",
               temp_dir=tmp_path / "temp")
    c.db_path.parent.mkdir()
    db.init_db(c.db_path)
    for path in ("Users/alice", "Talk", "Channels/room-a", "Groups/team", "linked/rw", "linked/ro"):
        (c.workspace_path / path).mkdir(parents=True)
    return c


@pytest.fixture
def conn(config):
    with db.get_db(config.db_path) as conn:
        yield conn


def resources():
    return [db.UserResource(id=n, user_id="alice", resource_type="folder",
                            resource_path=f"linked/{mode}", display_name=None, permissions=permission)
            for n, mode, permission in ((1, "rw", "readwrite"), (2, "ro", "read"))]


@pytest.mark.parametrize("withheld, reasons", [
    (frozenset(), ["nextcloud_user_dir", "nextcloud_channel_dir",
                   "nextcloud_group_dir", "user_resource", "user_resource"]),
    ({"memory"}, ["nextcloud_user_dir", "nextcloud_channel_dir", "nextcloud_group_dir",
                  "user_resource", "user_resource"]),
    ({"files"}, ["nextcloud_channel_dir", "nextcloud_group_dir"]),
])
def test_mount_gates_and_order(config, withheld, reasons):
    config.nextcloud.url = "https://cloud.example.com"
    config.workspace_mount_path = config.workspace_path
    mounts = plan.user_data_mounts(config, user_id="alice", conversation_token="room-a",
                                  user_resources=resources(), withheld_scopes=withheld,
                                  group_ids=["team", "team"])
    assert [m.reason for m in mounts] == reasons
    assert all(m.user_data for m in mounts)
    if not withheld:
        assert [m.mode for m in mounts] == ["rw", "rw", "rw", "rw", "ro"]


def test_local_workspace_binds_no_talk_dir(config):
    config.nextcloud.url = "https://cloud.example.com"
    mounts = plan.user_data_mounts(config, user_id="alice", conversation_token="room-a",
                                  user_resources=[], withheld_scopes=frozenset(), group_ids=[])
    assert "nextcloud_talk_dir" not in [m.reason for m in mounts]
    assert "nextcloud_channel_dir" in [m.reason for m in mounts]


def test_mounts_are_read_only_and_quiet(config, tmp_path, caplog):
    before = sorted(tmp_path.rglob("*"))
    with caplog.at_level(logging.WARNING):
        assert plan.user_data_mounts(config, user_id="..", conversation_token=None,
                                     user_resources=[], withheld_scopes={"files"},
                                     group_ids=["..", "missing"]) == []
    assert sorted(tmp_path.rglob("*")) == before
    assert caplog.records == []


def test_resource_merge(config, conn):
    from istota.workspace.places import user_resources_for
    config.users["alice"] = UserConfig(resources=[ResourceConfig(type="folder", path="linked/ro")])
    db.add_user_resource(conn, "alice", "folder", "linked/rw")
    merged = user_resources_for(config, conn, "alice")
    assert [(r.resource_path, r.id == 0) for r in merged] == [("linked/rw", False), ("linked/ro", True)]
    supplied = resources()
    assert user_resources_for(config, None, "alice", resources=supplied)[:2] == supplied
    assert len(supplied) == 2


def test_places_and_reach_through_live_membership(config, conn):
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach
    room = db.create_web_chat_room(conn, "bob", "<b>Shared</b>" + "x" * 150)
    db.add_room_member(conn, room.token, "alice")
    (config.workspace_path / "Channels" / room.token).mkdir()
    db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
    db.add_group_member(conn, "team", "alice", added_by="alice")
    rw_id = db.add_user_resource(conn, "alice", "folder", "linked/rw", permissions="readwrite")
    config.users["alice"] = UserConfig(resources=[ResourceConfig(type="folder", path="linked/ro")])
    cfg_id = "linked:cfg:" + hashlib.sha256(b"linked/ro").hexdigest()[:12]
    # No Talk attachments place even on the Nextcloud mount: no task reaches
    # the bot's flat `Talk/` folder (ISSUE-880).
    config.nextcloud.url = "https://cloud.example.com"
    config.workspace_mount_path = config.workspace_path
    places = list_places(config, conn, "alice")
    by_id = {p.id: p for p in places}
    assert set(by_id) == {"home", f"linked:{rw_id}", cfg_id, f"room:{room.token}", "group:team", "nextcloud"}
    assert by_id["home"].view_root == "/Users/alice"
    assert len(by_id[f"room:{room.token}"].label) == 120
    reach = place_reach(config, conn, "alice", places)
    for place_id, mode in (("home", "rw"), (f"linked:{rw_id}", "rw"), (cfg_id, "ro")):
        assert (reach[place_id].private, reach[place_id].shared_member, reach[place_id].restricted) == (mode, mode, "none")
    r = reach[f"room:{room.token}"]
    assert (r.private, r.shared_member, r.restricted) == ("rw", "rw", "rw")
    assert "own tasks" in r.note
    assert "memory" in reach["home"].note
    conn.execute("DELETE FROM room_members WHERE room_token = ? AND user_id = ?", (room.token, "alice"))
    assert f"room:{room.token}" not in {p.id for p in list_places(config, conn, "alice")}


def test_skip_missing_covered_and_nonfile_resources(config, conn):
    from istota.workspace.places import list_places
    for path in ("Users/alice", "missing"):
        db.add_user_resource(conn, "alice", "folder", path)
    db.add_user_resource(conn, "alice", "calendar", "linked/rw")
    assert {p.id for p in list_places(config, conn, "alice")} == {"home"}
    assert not any(p.kind == "home" for p in list_places(config, conn, ".."))


def test_drive_grant_and_advertisement(config, conn):
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach
    config.google_workspace.enabled = True
    conn.execute("INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry, scopes) VALUES (?, '', '', '', ?)",
                 ("alice", '["https://www.googleapis.com/auth/drive.readonly"]'))
    places = list_places(config, conn, "alice")
    assert "drive" in {p.id for p in places}
    r = place_reach(config, conn, "alice", places)["drive"]
    assert (r.private, r.shared_member, r.restricted) == ("skill", "skill", "none")
    config.disabled_skills = ["google_workspace"]
    assert place_reach(config, conn, "alice", places)["drive"].private == "none"
    conn.execute("UPDATE google_oauth_tokens SET scopes = '[]'")
    assert "drive" not in {p.id for p in list_places(config, conn, "alice")}


@pytest.mark.parametrize("effective", [True, False, None])
def test_notes_without_probing(config, monkeypatch, effective):
    from istota.workspace.reach import deployment_notes
    monkeypatch.setattr(executor, "_bwrap_checked", effective)
    monkeypatch.setattr(executor, "_bwrap_available", lambda: pytest.fail("web must not probe"))
    notes = deployment_notes(config)
    assert notes[-1] == "Files are stored in a local folder."
    if effective is False:
        assert "development checkout" in notes[0]
    elif effective is None:
        assert notes[0] == "Sandboxing has not been checked yet in this process."
    else:
        assert len(notes) == 1


def test_notes_use_daemon_verdict_and_only_nextcloud_host(config, monkeypatch):
    from istota.workspace.reach import deployment_notes
    doctor.record_sandbox_verdict(config, True)
    monkeypatch.setattr(executor, "_bwrap_checked", False)
    config.nextcloud.url = "https://alice:secret@cloud.example.com/private"
    assert deployment_notes(config)[-1] == "Files are stored in a local folder."
    config.workspace_mount_path = config.workspace_path
    assert deployment_notes(config) == ["Files are stored in your Nextcloud at cloud.example.com."]


def test_room_mount_is_only_in_its_own_context(config):
    for token, expected in ((None, []), ("other-room", []), ("room-a", ["room-a"])):
        mounts = plan.user_data_mounts(config, user_id="alice", conversation_token=token,
                                      user_resources=[], withheld_scopes={"files"}, group_ids=[])
        assert [m.source.name for m in mounts] == expected


def test_reach_preserves_last_covering_mount(config, conn):
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach
    first = db.add_user_resource(conn, "alice", "folder", "linked/ro", permissions="read")
    db.add_user_resource(conn, "alice", "folder", "linked", permissions="readwrite")
    reach = place_reach(config, conn, "alice", list_places(config, conn, "alice"))
    assert reach[f"linked:{first}"].private == "rw"


def test_shared_file_and_outside_resource(config, conn, tmp_path):
    from istota.workspace.places import list_places
    file = config.workspace_path / "linked" / "report.txt"
    file.write_text("report")
    resource_id = db.add_user_resource(conn, "alice", "shared_file", "linked/report.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    (config.workspace_path / "escape").symlink_to(outside, target_is_directory=True)
    db.add_user_resource(conn, "alice", "folder", "escape")
    places = {p.id: p for p in list_places(config, conn, "alice")}
    assert set(places) == {"home", f"linked:{resource_id}"}
    assert places[f"linked:{resource_id}"].view_root == "/linked/report.txt"


def test_no_workspace_and_nextcloud_connection(config, conn):
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach
    config.workspace_path = None
    assert list_places(config, conn, "alice") == []
    config.nextcloud.url = "https://cloud.example.com"
    places = list_places(config, conn, "alice")
    assert [p.id for p in places] == ["nextcloud"]
    assert places[0].browsable
    assert place_reach(config, conn, "alice", places)["nextcloud"].private == "none"
    config.disabled_skills = ["nextcloud"]
    assert place_reach(config, conn, "alice", places)["nextcloud"].private == "none"


def test_shared_group_reach_depends_on_every_room_reader(config, conn):
    from istota.rooms.scopes import task_group_ids
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach

    db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
    db.add_group_member(conn, "team", "alice", added_by="alice")
    room = db.create_web_chat_room(conn, "alice", "Project")
    db.add_room_member(conn, room.token, "bob")
    task_id = db.create_task(conn, user_id="alice", prompt="Read the group files", source_type="web", conversation_token=room.token)
    task = db.get_task(conn, task_id)
    assert task_group_ids(conn, task) == []
    reach = place_reach(config, conn, "alice", list_places(config, conn, "alice"))["group:team"]
    assert reach.shared_member == "conditional"
    assert "every reader" in reach.note
    db.add_group_member(conn, "team", "bob", added_by="alice")
    assert task_group_ids(conn, task) == ["team"]
    assert place_reach(config, conn, "alice", list_places(config, conn, "alice"))["group:team"].shared_member == "conditional"


def test_personal_nextcloud_connection_is_not_task_reach(config, conn):
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach
    config.nextcloud.url = "https://cloud.example.com"
    places = list_places(config, conn, "alice")
    reach = place_reach(config, conn, "alice", places)["nextcloud"]
    assert (reach.private, reach.shared_member, reach.restricted) == ("none", "none", "none")
    assert reach.note == (
        "Only you can see this view. It uses your personal Nextcloud connection, "
        "which tasks never use. Istota’s Nextcloud skill uses the bot’s own account "
        "instead. To work on a file with Istota, put a copy in My files."
    )
    config.bot_name = "Helper"
    note = place_reach(config, conn, "alice", places)["nextcloud"].note
    assert "Helper’s Nextcloud skill" in note and "with Helper," in note
    assert "Istota" not in note


def test_unsandboxed_note_names_the_bot(config, monkeypatch):
    from istota.workspace.reach import deployment_notes
    monkeypatch.setattr(executor, "_bwrap_checked", False)
    config.bot_name = "Helper"
    note = deployment_notes(config)[0]
    # The process is the software, which keeps its own name; the assistant is the deployment's.
    assert note.endswith(
        "the Istota process can, so the reach shown below is what Helper is told, not a limit."
    )


def _share(conn, user="alice", key="cloud.example.org|41", name="Taxes 2026", state="active",
           access="read"):
    from istota.sharing import sources
    source_id = sources.create_source(
        conn, user_id=user, kind="ocm_share", state="pending", remote_host="cloud.example.org",
        remote_key=key, access=access, extras={"name": name} if name is not None else {},
    )
    if state != "pending":
        sources.set_state(conn, source_id, user, state)
    return source_id


def test_accepted_shares_are_places_reached_through_the_skill(config, conn):
    from istota.workspace.places import list_places
    from istota.workspace.reach import place_reach
    active = _share(conn, key="cloud.example.org|1", name="Taxes\n 2026", access="write")
    unnamed = _share(conn, key="cloud.example.org|2", name=None)
    _share(conn, key="cloud.example.org|3", state="pending")
    _share(conn, key="cloud.example.org|4", state="revoked")
    _share(conn, key="cloud.example.org|5", state="declined")
    _share(conn, user="bob", key="cloud.example.org|6")
    conn.commit()

    places = {p.id: p for p in list_places(config, conn, "alice") if p.kind == "share"}
    assert set(places) == {f"share:{active}", f"share:{unnamed}"}
    place = places[f"share:{active}"]
    assert (place.label, place.browsable, place.root, place.view_root) == (
        "Taxes 2026", True, None, None)
    assert (place.remote_host, place.remote_access) == ("cloud.example.org", "write")
    assert places[f"share:{unnamed}"].label == "cloud.example.org"

    reach = place_reach(config, conn, "alice", list(places.values()))
    mine = reach[f"share:{active}"]
    assert (mine.private, mine.shared_member, mine.restricted) == ("skill", "skill", "none")
    assert "cloud.example.org" in mine.note and "read and change" in mine.note
    assert "read and change" not in reach[f"share:{unnamed}"].note
    config.disabled_skills = ["shares"]
    mine = place_reach(config, conn, "alice", list(places.values()))[f"share:{active}"]
    assert (mine.private, mine.shared_member, mine.restricted) == ("none", "none", "none")


def test_a_share_in_error_stays_a_place_and_needs_no_workspace(tmp_path):
    from istota.sharing import sources
    from istota.workspace.places import list_places
    c = Config(db_path=tmp_path / "istota.db", temp_dir=tmp_path / "temp")
    db.init_db(c.db_path)
    with db.get_db(c.db_path) as conn:
        source_id = _share(conn)
        sources.set_state(conn, source_id, "alice", "error", reason="timeout")
        conn.commit()
        assert [p.id for p in list_places(c, conn, "alice")] == [f"share:{source_id}"]
