"""The inbound frame spool (ISSUE-669): written, read back in order, forgotten."""

from __future__ import annotations

import os
import stat

from istota.transport.whatsapp import inbound_spool


def _dir(tmp_path):
    path = tmp_path / "spool"
    path.mkdir(mode=0o700)
    return path


def test_frames_come_back_in_arrival_order(tmp_path):
    spool = _dir(tmp_path)
    for index in range(5):
        inbound_spool.write(spool, "inbound", {"message_id": f"M{index}"})

    frames = inbound_spool.pending(spool)

    assert [frame.payload["message_id"] for frame in frames] == [
        "M0", "M1", "M2", "M3", "M4",
    ]
    assert {frame.message_type for frame in frames} == {"inbound"}


def test_a_kept_frame_is_private(tmp_path):
    spool = _dir(tmp_path)
    path = inbound_spool.write(spool, "inbound", {"text": "private words"})

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_removed_frame_is_not_replayed(tmp_path):
    spool = _dir(tmp_path)
    path = inbound_spool.write(spool, "inbound", {"message_id": "M1"})
    inbound_spool.remove(path)
    inbound_spool.remove(path)
    inbound_spool.remove(None)

    assert inbound_spool.pending(spool) == []


def test_a_damaged_entry_is_removed_and_the_rest_are_read(tmp_path):
    spool = _dir(tmp_path)
    inbound_spool.write(spool, "inbound", {"message_id": "M1"})
    (spool / "00000000000000000000-00000000.json").write_text("{not json")
    (spool / "00000000000000000001-00000000.json").write_text('{"type": 3}')
    os.symlink(tmp_path / "elsewhere", spool / "00000000000000000002-00000000.json")

    frames = inbound_spool.pending(spool)

    assert [frame.payload["message_id"] for frame in frames] == ["M1"]
    assert sorted(os.listdir(spool)) == [frames[0].path.name]


def test_a_staged_record_replaces_the_frame_whole(tmp_path):
    spool = _dir(tmp_path)
    path = inbound_spool.write(spool, "inbound", {"message_id": "M1"})

    assert inbound_spool.record_staged(
        path, "inbound", {"message_id": "M1"},
        {"staged_path": "/Users/alice/inbox/x.jpg"}, None,
    )

    [frame] = inbound_spool.pending(spool)
    assert frame.path == path
    assert frame.payload == {"message_id": "M1"}
    assert frame.staged == {
        "media": {"staged_path": "/Users/alice/inbox/x.jpg"}, "claimed_from": None,
    }


def test_an_interrupted_rewrite_leaves_the_original(tmp_path):
    spool = _dir(tmp_path)
    path = inbound_spool.write(spool, "inbound", {"message_id": "M1"})
    leftover = path.with_name(path.stem + ".tmp")
    leftover.write_text("{half")

    [frame] = inbound_spool.pending(spool)

    assert frame.staged is None
    assert not leftover.exists()


def test_an_old_frame_is_dropped_at_load(tmp_path):
    spool = _dir(tmp_path)
    old = inbound_spool.write(spool, "inbound", {"message_id": "OLD"})
    inbound_spool.write(spool, "inbound", {"message_id": "NEW"})
    stamp = old.stat().st_mtime - inbound_spool.SPOOL_MAX_AGE_SECONDS - 1
    os.utime(old, (stamp, stamp))

    frames = inbound_spool.pending(spool)

    assert [frame.payload["message_id"] for frame in frames] == ["NEW"]
    assert not old.exists()


def test_a_missing_directory_is_a_refusal_not_a_raise(tmp_path):
    missing = tmp_path / "absent"

    assert inbound_spool.write(missing, "inbound", {"n": 1}) is None
    assert inbound_spool.pending(missing) == []
