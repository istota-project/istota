"""Talk files on a local workspace, against the tier's real Nextcloud.

A local workspace has no `Talk/` folder on disk, so a file posted in Talk is
fetched over WebDAV from the bot's own `Talk/` folder, and a workspace file
posted into Talk is uploaded there first. Both halves are driven from the test
process with a local-workspace `Config` aimed at the stack's Nextcloud: the
stack itself runs the mount, and the testbed allows one full profile, so the
daemon is not booted a second time in the other mode.

What only a real server can say: that the bot's copy of a shared file is at
`Talk/<name>` under the bot account's root, outside `dav_prefix`, under the
name the message's `file` parameter carries; that an `If-None-Match: *` PUT
onto an existing name answers 412; and that a share of the uploaded copy posts
a file message into the room.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.config import Config, NextcloudConfig
from istota.scheduler import download_talk_attachments
from istota.skills.nextcloud import main as nextcloud_cli

pytestmark = pytest.mark.full

FULL = pytest.mark.profile("full")


def _file_messages(nextcloud, token: str) -> list[dict]:
    """The room's file messages as the bot sees them, newest first."""
    return [
        row for row in nextcloud.messages(token, user=nextcloud.bot_user)
        if (row.get("messageParameters") or {}).get("file")
    ]


def _share_as_test_user(nextcloud, token: str, name: str, body: bytes) -> None:
    folder = f"lw-{uuid.uuid4().hex[:8]}"
    _, status = nextcloud._dav(folder, user=nextcloud.test_user, method="MKCOL")
    assert status == 201, status
    _, status = nextcloud._dav(
        f"{folder}/{name}", user=nextcloud.test_user, method="PUT", body=body,
    )
    assert status == 201, status
    answer = nextcloud._ocs(
        "/ocs/v2.php/apps/files_sharing/api/v1/shares",
        user=nextcloud.test_user, method="POST",
        body={"path": f"/{folder}/{name}", "shareType": 10, "shareWith": token},
    )
    assert answer.status_code in (100, 200), answer


def _local_config(nextcloud, workspace: Path) -> Config:
    return Config(
        workspace_path=workspace,
        temp_dir=workspace.parent / "temp",
        nextcloud=NextcloudConfig(
            url=nextcloud.base_url,
            username=nextcloud.bot_user,
            app_password=nextcloud._passwords[nextcloud.bot_user],
            dav_prefix="Shared Files",
        ),
    )


@FULL
class TestTalkFilesOnALocalWorkspace:
    def test_a_posted_file_is_fetched_into_the_rooms_media_folder(self, stack, tmp_path):
        nextcloud = stack.service("nextcloud")
        token = nextcloud.create_room(
            name=f"testbed-{uuid.uuid4().hex[:8]}", participants=[nextcloud.bot_user],
        )
        name = f"lw-{uuid.uuid4().hex[:8]}.txt"
        _share_as_test_user(nextcloud, token, name, b"posted in talk\n")

        [message] = _file_messages(nextcloud, token)
        posted = message["messageParameters"]["file"]
        assert posted["path"] == f"Talk/{posted['name']}", posted

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = _local_config(nextcloud, workspace)
        task = SimpleNamespace(
            id=0, user_id=nextcloud.test_user, conversation_token="lwroom",
            talk_message_id=int(message["id"]),
        )

        [path] = download_talk_attachments(config, task, [f"Talk/{posted['name']}"])

        expected = workspace.resolve() / "Channels" / "lwroom" / "media" / str(message["id"])
        assert Path(path) == expected / posted["name"], path
        assert Path(path).read_bytes() == b"posted in talk\n"
        # A retry finds the first copy rather than writing a second.
        assert download_talk_attachments(config, task, [f"Talk/{posted['name']}"]) == [path]
        assert [p.name for p in expected.iterdir()] == [posted["name"]]

    def test_share_file_uploads_a_numbered_copy_and_posts_it(
        self, stack, tmp_path, monkeypatch, capsys,
    ):
        nextcloud = stack.service("nextcloud")
        token = nextcloud.create_room(
            name=f"testbed-{uuid.uuid4().hex[:8]}", participants=[nextcloud.bot_user],
        )
        workspace = tmp_path / "workspace"
        home = workspace / "Users" / nextcloud.test_user
        home.mkdir(parents=True)
        (home / "chart.txt").write_bytes(b"a chart\n")
        admins = tmp_path / "admins"
        admins.write_text("nobody-else\n")

        monkeypatch.setenv("NC_URL", nextcloud.base_url)
        monkeypatch.setenv("NC_USER", nextcloud.bot_user)
        monkeypatch.setenv("NC_PASS", nextcloud._passwords[nextcloud.bot_user])
        monkeypatch.setenv("NC_DAV_PREFIX", "Shared Files")
        monkeypatch.setenv("NC_STORAGE_IS_NEXTCLOUD", "False")
        monkeypatch.setenv("ISTOTA_USER_ID", nextcloud.test_user)
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(workspace))
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)

        def post() -> dict:
            try:
                nextcloud_cli(
                    ["talk", "share-file", token, "--path",
                     f"/Users/{nextcloud.test_user}/chart.txt"],
                )
            except SystemExit as e:
                assert e.code in (0, None), capsys.readouterr()
            return json.loads(capsys.readouterr().out)

        first = post()
        second = post()

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert first["uploaded_to"] == f"/Talk/{token}/{day}/chart.txt", first
        assert second["uploaded_to"] == f"/Talk/{token}/{day}/chart (2).txt", second
        listed = nextcloud.files(f"Talk/{token}/{day}", user=nextcloud.bot_user)
        assert listed == [f"Talk/{token}/{day}/chart (2).txt", f"Talk/{token}/{day}/chart.txt"]
        assert nextcloud.read_file(
            f"Talk/{token}/{day}/chart.txt", user=nextcloud.bot_user,
        ) == b"a chart\n"

        deadline = time.monotonic() + 30
        names: list[str] = []
        while time.monotonic() < deadline:
            names = [
                row["messageParameters"]["file"]["name"]
                for row in _file_messages(nextcloud, token)
            ]
            if len(names) == 2:
                break
            time.sleep(1)
        assert sorted(names) == ["chart (2).txt", "chart.txt"], names
