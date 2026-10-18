"""ISSUE-782: a linked resource root is never reached through a link.

A `folder` or `shared_file` resource under the reader's own home is a path the
reader's tasks control component by component. Resolving it let a task swap the
shared item for a link to another user's directory, which then became a
servable root, a Files place and a sandbox bind for the reader's next task.
"""

import os

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.sandbox import plan
from istota.webui.chat_files import ChatFileError, open_chat_file, servable_roots
from istota.workspace.places import list_places


@pytest.fixture
def config(tmp_path):
    c = Config(
        db_path=tmp_path / "state" / "istota.db",
        workspace_path=tmp_path / "workspace",
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(display_name="Bob")},
    )
    c.db_path.parent.mkdir()
    db.init_db(c.db_path)
    (c.workspace_path / "Users" / "alice" / "shared").mkdir(parents=True)
    bob = c.workspace_path / "Users" / "bob"
    bob.mkdir(parents=True)
    (bob / "secret.txt").write_text("bob's secret")
    return c


def _add(config, resource_type, path):
    with db.get_db(config.db_path) as conn:
        db.add_user_resource(conn, "alice", resource_type, path)


def _roots(config):
    with db.get_db(config.db_path) as conn:
        return servable_roots(config, conn, "alice")


def _places(config):
    with db.get_db(config.db_path) as conn:
        return list_places(config, conn, "alice")


def _read(config, path, roots=None):
    fd, _ = open_chat_file(config, "alice", path, roots=roots)
    try:
        return os.read(fd, 1024)
    finally:
        os.close(fd)


def _resource_mounts(config):
    with db.get_db(config.db_path) as conn:
        resources = db.get_user_resources(conn, "alice")
    mounts = plan.user_data_mounts(
        config, user_id="alice", conversation_token=None, user_resources=resources,
        withheld_scopes=frozenset(), group_ids=[],
    )
    return [m for m in mounts if m.reason == "user_resource"]


def test_a_folder_swapped_for_a_link_to_another_home_is_no_root(config):
    """The reported reproduction: the organizer's row, the item replaced by a link."""
    _add(config, "folder", "/Users/alice/shared/Doc")
    (config.workspace_path / "Users/alice/shared/Doc").symlink_to(config.workspace_path / "Users/bob")

    assert [p.kind for p in _places(config)] == ["home"]
    assert [view for view, *_ in _roots(config)] == ["/Users/alice"]
    with pytest.raises(ChatFileError) as caught:
        _read(config, "/Users/bob/secret.txt")
    assert caught.value.status == 403


def test_a_shared_file_swapped_for_a_link_is_no_root(config):
    _add(config, "shared_file", "/Users/alice/shared/Doc")
    (config.workspace_path / "Users/alice/shared/Doc").symlink_to(
        config.workspace_path / "Users/bob/secret.txt"
    )

    assert [p.kind for p in _places(config)] == ["home"]
    with pytest.raises(ChatFileError):
        _read(config, "/Users/bob/secret.txt")


def test_a_link_above_the_leaf_is_refused_too(config):
    (config.workspace_path / "elsewhere").symlink_to(config.workspace_path / "Users/bob")
    _add(config, "shared_file", "elsewhere/secret.txt")

    assert [p.kind for p in _places(config)] == ["home"]
    assert _resource_mounts(config) == []


def test_the_sandbox_plan_binds_no_resource_through_a_link(config):
    _add(config, "folder", "/Users/alice/shared/Doc")
    (config.workspace_path / "Users/alice/shared/Doc").symlink_to(config.workspace_path / "Users/bob")

    assert _resource_mounts(config) == []


def test_a_root_swapped_after_listing_is_not_followed_at_open(config):
    """The open walks from the workspace root, not from the root `servable_roots` named."""
    linked = config.workspace_path / "projects" / "Doc"
    linked.mkdir(parents=True)
    (linked / "notes.txt").write_text("project notes")
    _add(config, "folder", "projects/Doc")
    roots = _roots(config)
    assert _read(config, "/projects/Doc/notes.txt", roots) == b"project notes"

    linked.rename(config.workspace_path / "projects" / "Doc.real")
    linked.symlink_to(config.workspace_path / "Users/bob")
    with pytest.raises(ChatFileError) as caught:
        _read(config, "/projects/Doc/secret.txt", roots)
    assert caught.value.status == 403
    assert caught.value.kind == "link"


def test_a_real_folder_outside_home_still_lists_serves_and_binds(config):
    linked = config.workspace_path / "projects" / "Doc"
    linked.mkdir(parents=True)
    (linked / "notes.txt").write_text("project notes")
    (config.workspace_path / "projects" / "brief.txt").write_text("the brief")
    _add(config, "folder", "projects/Doc")
    _add(config, "shared_file", "/projects/brief.txt")

    assert sorted(p.view_root for p in _places(config) if p.kind == "linked") == [
        "/projects/Doc", "/projects/brief.txt",
    ]
    assert _read(config, "/projects/Doc/notes.txt") == b"project notes"
    assert _read(config, "/projects/brief.txt") == b"the brief"
    sources = sorted(m.source for m in _resource_mounts(config))
    workspace = config.workspace_path.resolve()
    assert sources == [workspace / "projects/Doc", workspace / "projects/brief.txt"]


def test_a_resource_inside_home_is_skipped_by_its_spelling(config):
    """The home skip reads the lexical path: a real folder in home is covered by home."""
    (config.workspace_path / "Users/alice/shared/Doc").mkdir()
    _add(config, "folder", "/Users/alice/shared/Doc")

    assert [p.kind for p in _places(config)] == ["home"]
    assert _resource_mounts(config) == []
