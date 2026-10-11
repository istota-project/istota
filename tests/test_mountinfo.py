"""`lib/mountinfo.py`: the one mountinfo parse.

The container entrypoint's preflight (parity row 9: is the workspace on the
VM's rclone mount) and `sandbox/cgroup.py` (parity row 4: is the declared root
on this container's own cgroup2 mount) each had a parse of their own. This
holds the shared one to the format's awkward parts and both callers to it.
"""

from __future__ import annotations

from pathlib import Path

from istota.lib import mountinfo

REPO = Path(__file__).resolve().parent.parent

MOUNTINFO = (
    "30 1 0:30 / / rw,relatime - overlay overlay rw\n"
    # Two optional fields before the separator: the fstype is after `-`.
    "41 30 0:41 / /sys/fs/cgroup rw,nosuid shared:5 master:1 - cgroup2 cgroup rw\n"
    "52 30 0:52 / /mnt/shared rw,nosuid - fuse.rclone remote: rw\n"
    "53 30 0:53 / /mnt/with\\040space rw - tmpfs tmpfs rw\n"
)


def test_the_longest_covering_mount_answers():
    mount = mountinfo.covering_mount("/mnt/shared/Users/alice", MOUNTINFO)

    assert mount == mountinfo.Mount(point="/mnt/shared", root="/", fstype="fuse.rclone")


def test_a_path_on_no_mount_of_its_own_is_on_the_root():
    assert mountinfo.covering_mount("/data/workspace", MOUNTINFO).point == "/"


def test_the_fstype_is_read_after_the_separator_not_by_column():
    assert mountinfo.covering_mount("/sys/fs/cgroup", MOUNTINFO).fstype == "cgroup2"


def test_escaped_paths_are_unescaped():
    assert mountinfo.covering_mount("/mnt/with space/x", MOUNTINFO).fstype == "tmpfs"


def test_a_prefix_that_is_not_a_parent_does_not_cover():
    assert mountinfo.covering_mount("/mnt/shared-other", MOUNTINFO).point == "/"


def test_a_later_mount_at_the_same_point_wins():
    text = MOUNTINFO + "60 52 0:60 / /mnt/shared rw - tmpfs tmpfs rw\n"

    assert mountinfo.covering_mount("/mnt/shared", text).fstype == "tmpfs"


def test_both_callers_use_it():
    entrypoint = (REPO / "docker" / "istota" / "entrypoint.sh").read_text()
    cgroup = (REPO / "src" / "istota" / "sandbox" / "cgroup.py").read_text()

    assert "from istota.lib.mountinfo import covering_mount" in entrypoint
    assert "mountinfo.covering_mount(" in cgroup
    for source in (entrypoint, cgroup):
        assert 'index("-")' not in source, "a second mountinfo parse"
