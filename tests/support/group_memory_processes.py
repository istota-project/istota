"""The same competing CLI and web writes on local files and in the stack."""
PRELUDE = """
import json, os, sys, time
from pathlib import Path
from istota import db, storage
from istota.config import Config
from istota.workspace.text_files import revision
config = Config(db_path=Path(sys.argv[1]), workspace_path=Path(sys.argv[2]))
sync = Path(sys.argv[3])
group_id = "memory-witness"
path = config.workspace_path / "Groups" / group_id / "GROUP.md"
def wait_for(name):
    deadline = time.monotonic() + 20
    while not (sync / name).exists():
        assert time.monotonic() < deadline, name + " timed out"
        time.sleep(0.05)
"""

SEED = PRELUDE + """
sync.mkdir(parents=True, exist_ok=True)
for name in ("held", "web-started", "web-done", "release"):
    (sync / name).unlink(missing_ok=True)
with db.get_db(config.db_path) as conn:
    if db.get_group(conn, group_id) is None:
        db.create_group(conn, group_id, kind="group", display_name="Witness", created_by="operator")
    for member in ("alice", "bob"):
        db.add_group_member(conn, group_id, member, added_by="operator")
assert storage.ensure_group_directories(config, group_id)
assert storage.write_group_memory(config, group_id, chr(10).join(["## Notes", "", "- original", ""]))
"""

CLI = PRELUDE + """
from istota.skills import memory
os.environ.update(ISTOTA_DB_PATH=str(config.db_path), NEXTCLOUD_MOUNT_PATH=str(config.workspace_path),
                  ISTOTA_USER_ID="alice", ISTOTA_TASK_GROUPS=group_id,
                  ISTOTA_DEFERRED_DIR=str(sync / "alice"))
original = memory._read_target
def paused(target):
    result = original(target)
    (sync / "held").touch()
    wait_for("release")
    return result
memory._read_target = paused
memory.main(["append", "--group", group_id, "--heading", "Notes", "--line", "CLI edit"])
"""

WEB = PRELUDE + """
from istota.groups.memory import save_memory
from istota.groups.policy import GroupError
expected = revision(storage.read_group_memory_result(config, group_id)["content"])
(sync / "web-started").touch()
with db.get_db(config.db_path) as conn:
    try:
        result = save_memory(conn, config, "bob", group_id, {"content": "web edit", "revision": expected})
    except GroupError as exc:
        result = {"error": exc.code, "status": exc.status_code}
(sync / "web-done").write_text(json.dumps(result))
print(json.dumps(result))
"""

RELEASE = PRELUDE + """
wait_for("held")
wait_for("web-started")
time.sleep(0.3)
try:
    assert not (sync / "web-done").exists(), "web bypassed the CLI lock"
finally:
    (sync / "release").touch()
"""

VERIFY = PRELUDE + """
from istota.memory.curation.file_lock import group_memory_lock_dir, lock_path_for
result = json.loads((sync / "web-done").read_text())
assert result == {"error": "memory_conflict", "status": 409}, result
assert "- CLI edit" in path.read_text() and "web edit" not in path.read_text()
anchor = lock_path_for(path, lock_dir=group_memory_lock_dir(config.db_path))
assert anchor.is_file() and anchor.stat().st_uid == os.getuid()
print(json.dumps({"uid": os.getuid(), "anchor": str(anchor)}))
"""
