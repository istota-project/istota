import pytest

from istota import db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        db.create_group(conn, "family", kind="family", display_name="F", created_by="carol")
        db.add_group_member(conn, "family", "alice", role="owner", added_by="admin")
        db.add_group_member(conn, "family", "bob", added_by="admin")
        yield conn


def test_owner_manages_with_same_content_access(conn):
    from istota.groups.policy import group_capabilities
    owner = group_capabilities(conn, "family", "alice", is_admin=False)
    member = group_capabilities(conn, "family", "bob", is_admin=False)
    assert owner["manage"] and not member["manage"]
    assert owner["read_memory"] and member["read_memory"]
    assert owner["edit_memory"] and member["edit_memory"]
    assert not group_capabilities(conn, "family", "carol", is_admin=False)["manage"]


@pytest.mark.parametrize("group_id", ["family", "missing"])
def test_admin_nonmember_cannot_read_content(conn, group_id):
    from istota.groups.policy import GroupError, group_capabilities, require_group_member
    assert not group_capabilities(conn, group_id, "admin", is_admin=True)["read_memory"]
    with pytest.raises(GroupError) as exc:
        require_group_member(conn, group_id, "admin")
    assert (exc.value.status_code, exc.value.code) == (404, "group_not_found")


def test_archived_and_removed_members_lose_content(conn):
    from istota.groups.policy import GroupError, require_group_member
    require_group_member(conn, "family", "bob")
    db.end_group_membership(conn, "family", "bob", ended_by="admin")
    with pytest.raises(GroupError):
        require_group_member(conn, "family", "bob")
    db.archive_group(conn, "family")
    with pytest.raises(GroupError):
        require_group_member(conn, "family", "alice")
