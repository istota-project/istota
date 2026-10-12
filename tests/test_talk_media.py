"""The retention sweep for Talk attachments fetched into room folders."""

import os
import time

from istota import db
from istota.config import Config, NextcloudConfig, TalkConfig
from istota.maintenance.talk_media import sweep_talk_media
from istota.scheduler import run_cleanup_checks


def _message_dir(workspace, token, message_id, files, *, age_days):
    directory = workspace / "Channels" / token / "media" / str(message_id)
    directory.mkdir(parents=True)
    stamp = time.time() - age_days * 86400
    for name in files:
        (directory / name).write_bytes(b"x")
        os.utime(directory / name, (stamp, stamp))
    return directory


def test_an_old_message_directory_goes_and_a_new_one_stays(tmp_path):
    old = _message_dir(tmp_path, "room1", 1, ["a.jpg", "b.jpg"], age_days=40)
    new = _message_dir(tmp_path, "room1", 2, ["c.jpg"], age_days=1)

    assert sweep_talk_media(tmp_path, 30) == 1

    assert not old.exists()
    assert (new / "c.jpg").exists()


def test_the_newest_file_decides(tmp_path):
    mixed = _message_dir(tmp_path, "room1", 1, ["old.jpg"], age_days=40)
    (mixed / "fresh.jpg").write_bytes(b"x")

    assert sweep_talk_media(tmp_path, 30) == 0
    assert (mixed / "old.jpg").exists()


def test_a_symlinked_message_directory_is_skipped(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"x")
    stamp = time.time() - 40 * 86400
    os.utime(target / "keep.txt", (stamp, stamp))
    media = tmp_path / "Channels" / "room1" / "media"
    media.mkdir(parents=True)
    (media / "5").symlink_to(target)

    assert sweep_talk_media(tmp_path, 30) == 0
    assert (target / "keep.txt").exists()
    assert (media / "5").is_symlink()


def test_a_symlinked_media_folder_is_skipped(tmp_path):
    target = tmp_path / "elsewhere"
    old = _message_dir(target, "x", 1, ["a.jpg"], age_days=40)
    room = tmp_path / "Channels" / "room1"
    room.mkdir(parents=True)
    (room / "media").symlink_to(old.parent)

    assert sweep_talk_media(tmp_path, 30) == 0
    assert (old / "a.jpg").exists()


def test_a_link_inside_an_old_directory_keeps_the_directory(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    old = _message_dir(tmp_path, "room1", 1, ["a.jpg"], age_days=40)
    (old / "link").symlink_to(outside)

    assert sweep_talk_media(tmp_path, 30) == 0
    assert outside.read_bytes() == b"keep"
    assert (old / "link").is_symlink()
    assert not (old / "a.jpg").exists()


def test_zero_keeps_everything(tmp_path):
    old = _message_dir(tmp_path, "room1", 1, ["a.jpg"], age_days=400)
    assert sweep_talk_media(tmp_path, 0) == 0
    assert (old / "a.jpg").exists()


def test_no_channels_folder_is_nothing_to_do(tmp_path):
    assert sweep_talk_media(tmp_path, 30) == 0


def test_the_cleanup_pass_sweeps_a_local_workspace_and_not_the_mount(tmp_path):
    for mounted in (False, True):
        workspace = tmp_path / f"ws-{mounted}"
        workspace.mkdir()
        db_path = tmp_path / f"db-{mounted}" / "istota.db"
        db_path.parent.mkdir()
        db.init_db(db_path)
        config = Config(
            db_path=db_path, temp_dir=tmp_path / "temp", workspace_path=workspace,
            nextcloud_mount_path=workspace if mounted else None,
            nextcloud=NextcloudConfig(url="https://cloud.test", username="istota"),
            talk=TalkConfig(attachment_retention_days=30),
        )
        old = _message_dir(workspace, "room1", 1, ["a.jpg"], age_days=40)

        run_cleanup_checks(config)

        assert old.exists() is mounted, mounted
