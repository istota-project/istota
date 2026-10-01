"""No automatic writer reaches a group store (groups spec D4).

v1 ships no group extraction. The personal and the channel sleep cycles run
over a user who is in a group, with a brain that answers with something that
*names* a group fact, and neither may touch the group's `GROUP.md` or its
`group_kv` rows. This is the test that must not be deleted when v2 adds a
writer: it is rewritten then, not removed.
"""

from unittest.mock import patch

import pytest

from istota import db, storage
from istota.config import ChannelSleepCycleConfig, Config, SleepCycleConfig
from istota.memory import sleep_cycle
from istota.memory.sleep_cycle import (
    process_channel_sleep_cycle,
    process_user_sleep_cycle,
)
from tests.support.drift import source_of

GROUP_FACT = (
    "- Group fact for the fam group: the plumber is Ana (2026-09-29)\n"
    "- Remember this for the group: bins go out Tuesday (2026-09-29)\n"
)


@pytest.fixture
def family(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "temp",
        workspace_path=mount,
        sleep_cycle=SleepCycleConfig(
            enabled=True, cron="0 2 * * *", memory_retention_days=90,
            lookback_hours=24,
        ),
        channel_sleep_cycle=ChannelSleepCycleConfig(
            enabled=True, cron="0 3 * * *", lookback_hours=24,
            memory_retention_days=90,
        ),
    )
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="Fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", added_by="operator")
        db.add_group_member(conn, "fam", "bob", added_by="operator")
        db.group_kv_set(conn, "fam", "house", "bins", '"Monday"', "bob")
    assert storage.ensure_group_directories(config, "fam", display_name="Fam")
    return config


def _group_state(config):
    group_md = config.workspace_path / "Groups" / "fam" / "GROUP.md"
    memories = config.workspace_path / "Groups" / "fam" / "memories"
    with db.get_db(config.db_path) as conn:
        rows = [
            (ns, r["key"], r["value"], r["written_by"])
            for ns in db.group_kv_namespaces(conn, "fam")
            for r in db.group_kv_list(conn, "fam", ns)
        ]
    files = sorted(p.name for p in memories.iterdir()) if memories.exists() else []
    return group_md.read_bytes(), rows, files


def _completed(conn, prompt, *, user_id="alice", token=None):
    t = db.create_task(conn, prompt=prompt, user_id=user_id,
                       conversation_token=token)
    db.update_task_status(conn, t, "running")
    db.update_task_status(conn, t, "completed", result="Noted for the group.")
    return t


@patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
def test_the_personal_cycle_writes_no_group_material(mock_run, family):
    mock_run.return_value = (True, GROUP_FACT)
    before = _group_state(family)
    with db.get_db(family.db_path) as conn:
        _completed(conn, "Remember for the fam group: the plumber is Ana")
        assert process_user_sleep_cycle(family, conn, "alice") is True
    # It did run and write the user's own dated memory, so the test is not
    # vacuous: the extraction went somewhere, and not to the group.
    user_memories = family.workspace_path / "Users" / "alice" / "memories"
    assert any("plumber" in p.read_text() for p in user_memories.glob("*.md"))
    assert _group_state(family) == before


@patch("istota.memory.sleep_cycle._run_sleep_cycle_brain")
def test_the_channel_cycle_writes_no_group_material(mock_run, family):
    mock_run.return_value = (True, GROUP_FACT)
    before = _group_state(family)
    with db.get_db(family.db_path) as conn:
        _completed(conn, "For the group: bins go out Tuesday", token="room1")
        _completed(conn, "Yes, Tuesday", user_id="bob", token="room1")
        assert process_channel_sleep_cycle(family, conn, "room1") is True
    channel_memories = family.workspace_path / "Channels" / "room1" / "memories"
    assert any("bins" in p.read_text() for p in channel_memories.glob("*.md"))
    assert _group_state(family) == before


def test_the_sleep_cycle_module_names_no_group_writer():
    # The structural half: no code path from the sleep cycle to a group store
    # exists to be reached. A v2 writer changes this test on purpose.
    source = source_of(sleep_cycle)
    for name in (
        "write_group_memory", "ensure_group_directories", "get_group_memory_path",
        "group_kv_set", "group_kv_delete", "read_group_memory", "Groups/",
        "GROUP.md",
    ):
        assert name not in source, name
