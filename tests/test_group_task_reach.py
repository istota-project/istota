"""One resolved group set per task, at each seam it reaches.

`room_scopes.task_group_ids` is computed once in `execute_task` and handed to
the prompt (`## Group memory`), the sandbox (`{mount}/Groups/<id>` bound
read-write) and the skill proxy (`ISTOTA_TASK_GROUPS`, which `kv --group`
gates on, D21). Driven through a real `execute_task`, the harness
`tests/test_shared_room_restriction.py` built for the disclosure gate.
"""

from __future__ import annotations

import pytest

from istota import db, storage
from tests import test_shared_room_restriction as harness
from tests.test_shared_room_restriction import _binds, _room, _run

# The harness's fixtures, by assignment so pytest collects them here.
config = harness.config
_bwrap_flag_cache = harness._bwrap_flag_cache

TASK_GROUPS_VAR = "ISTOTA_TASK_GROUPS"


@pytest.fixture
def family(config):
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "fam", kind="family", display_name="The Fam",
                        created_by="operator")
        db.add_group_member(conn, "fam", "alice", added_by="operator")
    storage.ensure_group_directories(config, "fam", display_name="The Fam")
    path = config.workspace_path / "Groups" / "fam" / "GROUP.md"
    path.write_text(path.read_text() + "\n- FAMILY_SENTINEL\n")
    return config.workspace_path / "Groups" / "fam"


def _group_bound(seen, group_dir) -> bool:
    return str(group_dir.resolve()) in _binds(seen["argv"])


class TestAPrivateRoom:
    def test_every_seam_gets_the_group(self, config, family):
        seen = _run(config, _room(config, shared=False))
        assert "## Group memory" in seen["prompt"]
        assert "FAMILY_SENTINEL" in seen["prompt"]
        assert _group_bound(seen, family)
        assert seen["argv"][seen["argv"].index(str(family.resolve())) - 1] == "--bind"
        assert seen["proxy_base_env"][TASK_GROUPS_VAR] == "fam"
        assert TASK_GROUPS_VAR not in seen["model_env"]

    def test_a_group_created_before_its_directory_is_seeded_on_first_use(
        self, config,
    ):
        with db.get_db(config.db_path) as conn:
            db.create_group(conn, "late", kind="team", display_name="Late",
                            created_by="operator")
            db.add_group_member(conn, "late", "alice", added_by="operator")
        seen = _run(config, _room(config, shared=False))
        late = config.workspace_path / "Groups" / "late"
        assert (late / "GROUP.md").is_file()
        assert _group_bound(seen, late)


class TestARoomWithANonMember:
    def test_no_seam_gets_the_group(self, config, family):
        # bob reads the room and is not in `fam`; granting `memory` and
        # `files` does not change that, since a grant is about the sender's
        # own data and group material follows the audience rule alone.
        seen = _run(config, _room(config, shared=True, grants=("memory", "files")))
        assert "FAMILY_SENTINEL" not in seen["prompt"]
        assert not _group_bound(seen, family)
        assert TASK_GROUPS_VAR not in seen["proxy_base_env"]


class TestAGuestTurn:
    def test_no_seam_gets_the_group(self, config, family):
        seen = _run(config, _room(config, shared=False), guest=True)
        assert "FAMILY_SENTINEL" not in seen["prompt"]
        assert not _group_bound(seen, family)
        assert TASK_GROUPS_VAR not in seen["proxy_base_env"]
