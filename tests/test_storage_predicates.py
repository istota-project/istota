"""The two storage predicates, and which callers read which.

`Config.nextcloud_enabled` is "a Nextcloud server is configured" (the URL):
Talk, the `nextcloud` skill, OCS, avatars. `Config.storage_is_nextcloud` is
"the workspace is the bot's Nextcloud files" (the URL and
`nextcloud_mount_path`). The local-workspace spec's stage 1 assigned every
reader to one of them; a source grep would hold that table invisibly to
testmon, so each reassigned caller is driven here under the three configs that
matter and asserted on the branch it takes. A new caller is not caught by this.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import scheduler, storage
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    WebConfig,
    _validate_nextcloud_workspace,
)
from istota.doctor import SKIP, run_checks
from istota.sandbox import attachment_source

URL = "https://cloud.example.test"

# The three shapes: no Nextcloud, full integration through the mount, and a
# Nextcloud install whose workspace is local disk.
LOCAL, MOUNT, BESIDE = "local", "mount", "beside"


def _config(tmp_path: Path, shape: str, **overrides) -> Config:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    settings = dict(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "temp",
        workspace_path=workspace,
        nextcloud_mount_path=workspace if shape == MOUNT else None,
        nextcloud=NextcloudConfig(
            url="" if shape == LOCAL else URL,
            username="istota",
            app_password="pw",
        ),
    )
    settings.update(overrides)
    return Config(**settings)


class TestThePredicateMatrix:
    @pytest.mark.parametrize(
        "url, mount, enabled, is_nextcloud",
        [
            ("", None, False, False),
            ("", Path("/mnt/shared"), False, False),
            (URL, Path("/mnt/shared"), True, True),
            (URL, None, True, False),
        ],
        ids=["neither", "mount-only", "url-and-mount", "url-only"],
    )
    def test_url_by_mount_path(self, url, mount, enabled, is_nextcloud):
        config = Config(
            nextcloud=NextcloudConfig(url=url),
            workspace_path=Path("/data/workspace"),
            nextcloud_mount_path=mount,
        )
        assert config.nextcloud_enabled is enabled
        assert config.storage_is_nextcloud is is_nextcloud
        assert config.storage_backend == ("nextcloud" if is_nextcloud else "local")
        assert config.storage_label == ("Nextcloud" if is_nextcloud else "your workspace")


class TestTheLoadTimeRefusal:
    """Keyed on the URL: the mount path fills `workspace_path` when it is
    unset, so a refusal keyed on `storage_is_nextcloud` could never fire."""

    def test_a_url_with_no_workspace_is_refused(self):
        with pytest.raises(ValueError, match="workspace_path"):
            _validate_nextcloud_workspace(Config(nextcloud=NextcloudConfig(url=URL)))

    @pytest.mark.parametrize("shape", [LOCAL, MOUNT, BESIDE])
    def test_every_shape_with_a_workspace_loads(self, tmp_path, shape):
        _validate_nextcloud_workspace(_config(tmp_path, shape))


class TestTheNextcloudCapability:
    @pytest.mark.parametrize("shape, offered", [(LOCAL, False), (MOUNT, True), (BESIDE, True)])
    def test_the_skill_follows_the_url(self, tmp_path, shape, offered):
        assert ("nextcloud" in _config(tmp_path, shape).available_capabilities()) is offered


class TestAvatarImport:
    def _importing(self, tmp_path, shape) -> Config:
        return _config(
            tmp_path, shape,
            web=WebConfig(enabled=True, avatar_import_from_nextcloud=True),
            scheduler=SchedulerConfig(avatar_import_interval=60),
        )

    @pytest.mark.parametrize("shape, runs", [(LOCAL, False), (MOUNT, True), (BESIDE, True)])
    def test_the_job_passes_its_gate_wherever_nextcloud_is(self, tmp_path, shape, runs):
        asked = []

        def _nc_configured(config):
            asked.append(config)
            return False

        with patch.object(scheduler, "nc_configured", _nc_configured):
            assert scheduler.check_avatar_import(self._importing(tmp_path, shape)) == []
        assert bool(asked) is runs

    @pytest.mark.parametrize("shape, enabled", [(LOCAL, False), (MOUNT, True), (BESIDE, True)])
    def test_the_loop_gate_agrees(self, tmp_path, shape, enabled):
        gates = {g.name: g for g in scheduler.build_interval_gates(self._importing(tmp_path, shape))}
        assert gates["avatar-import"].enabled(self._importing(tmp_path, shape)) is enabled

    @pytest.mark.parametrize("shape, skipped", [(LOCAL, True), (MOUNT, False), (BESIDE, False)])
    def test_doctor_skips_only_without_nextcloud(self, tmp_path, shape, skipped):
        from istota import db

        config = self._importing(tmp_path, shape)
        db.init_db(config.db_path)
        result = run_checks(config, only=("web.avatar_import",))[0]
        assert (result.status == SKIP and "no nextcloud" in result.detail.lower()) is skipped


class TestTheAdminStoragePane:
    @pytest.mark.parametrize("shape, configured", [(LOCAL, False), (MOUNT, True), (BESIDE, True)])
    def test_the_nextcloud_rows_follow_the_url(self, tmp_path, shape, configured):
        import istota.webui.app as app

        config = _config(tmp_path, shape)
        with patch.object(app, "_config", config):
            out = app._admin_storage_section(config.db_path)
        assert out["nextcloud_configured"] is configured
        assert (out["nextcloud_username"] == "istota") is configured


class TestTheBootTimeShare:
    """The OCS share names a path that exists in Nextcloud only under the mount."""

    @pytest.mark.parametrize("shape, shared", [(LOCAL, False), (MOUNT, True), (BESIDE, False)])
    def test_the_bot_dir_is_shared_only_from_the_mount(self, tmp_path, shape, shared):
        config = _config(tmp_path, shape)
        config.nextcloud.auto_share_bot_dir = True
        with patch.object(storage, "share_folder_with_user") as share:
            assert storage.ensure_user_directories_v2(config, "alice")
        assert share.called is shared


class TestTheNextcloudDataFallback:
    """`NC_DATA_ROOT` is Nextcloud's own data directory; it is searched for a
    Talk attachment only when the workspace is the mount."""

    @pytest.mark.parametrize("shape, found", [(LOCAL, False), (MOUNT, True), (BESIDE, False)])
    def test_the_senders_tree_is_searched_only_beside_the_mount(
        self, tmp_path, monkeypatch, shape, found,
    ):
        nc_data = tmp_path / "nc-data"
        sent = nc_data / "bob" / "files" / "Talk" / "photo.jpg"
        sent.parent.mkdir(parents=True)
        sent.write_bytes(b"jpeg")
        monkeypatch.setattr(attachment_source, "NC_DATA_ROOT", nc_data)

        # No token: beside the mount the WebDAV fetch has nowhere to write, so
        # the only way to find the file would be the sender's tree.
        task = SimpleNamespace(id=1, conversation_token=None, talk_message_id=None)
        result = scheduler.download_talk_attachments(
            _config(tmp_path, shape), task, ["Talk/photo.jpg"],
        )

        assert result == ([str(sent)] if found else ["Talk/photo.jpg"])
