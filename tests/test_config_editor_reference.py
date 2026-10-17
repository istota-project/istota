"""`config_editor.reference`: the example reader, the credential map and the
container layout."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from istota.config import Config
from istota.config_editor import reference

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"


def test_the_example_prose_lands_on_its_key():
    docs = reference.read_example((CONFIG_DIR / "config.example.toml").read_text())
    assert any("name your bot uses" in line for line in docs.keys[("", "bot_name")])
    assert any("Availability failover" in line for line in docs.keys[("brain", "fallback")])
    # A trailing comment and its continuation lines.
    transient = " ".join(docs.keys[("brain", "fallback_on_transient")])
    assert "also reroute" in transient and "(429 / 5xx / 529" in transient


class TestTheExampleReader:
    def test_a_key_takes_the_comment_block_above_it_and_its_trailing_note(self):
        docs = reference.read_example(
            "# ==========\n"
            "# Section intro\n"
            "# ==========\n"
            "[a]\n"
            "# First paragraph.\n"
            "#\n"
            "# Second paragraph.\n"
            "# x = 1   # the note\n"
            "#         # more note\n"
            "# y = \"s\"\n"
        )
        assert docs.sections["a"] == ["Section intro"]
        assert docs.keys[("a", "x")] == ["First paragraph.", "", "Second paragraph.", "the note", "more note"]
        assert docs.keys[("a", "y")] == []
        assert docs.examples[("a", "x")] == "1"

    def test_a_blank_line_ends_a_comment_block(self):
        docs = reference.read_example("[a]\n# About something else.\n\nx = 1\n")
        assert docs.keys[("a", "x")] == []

    def test_prose_that_looks_like_an_assignment_is_not_a_key(self):
        docs = reference.read_example("[a]\n# lower = more intermediate text is shown live.\nx = 1\n")
        assert ("a", "lower") not in docs.keys


def test_credential_files_is_every_credential_and_its_secrets_file():
    assert reference.credential_files() == {
        "nextcloud.app_password": "istota_nextcloud_app_password",
        "caldav.password": "istota_caldav_password",
        "email.imap_password": "istota_email_imap_password",
        "email.smtp_password": "istota_email_smtp_password",
        "developer.gitlab_token": "istota_developer_gitlab_token",
        "developer.github_token": "istota_developer_github_token",
        "google_workspace.client_secret": "istota_google_workspace_client_secret",
        "web.oauth2_client_secret": "istota_web_oauth2_client_secret",
        "web.session_secret_key": "istota_web_session_secret_key",
        "sms.twilio.account_sid": "",
        "sms.twilio.auth_token": "",
        "sms.twilio.api_key_sid": "",
        "sms.twilio.api_key_secret": "",
        "sms.twilio.messaging_service_sid": "",
        "sms.telnyx.api_key": "istota_sms_telnyx_api_key",
        "sms.telnyx.public_key": "istota_sms_telnyx_public_key",
        "sms.telnyx.messaging_profile_id": "istota_sms_telnyx_messaging_profile_id",
        "whatsapp.cloud.access_token": "",
        "whatsapp.cloud.app_secret": "",
        "whatsapp.cloud.verify_token": "",
        "brain.native.api_key": "istota_brain_native_api_key",
        "brain.native.extra_headers": "",
        "servarr.apps.sonarr.api_key": "istota_servarr_sonarr_api_key",
        "servarr.apps.radarr.api_key": "istota_servarr_radarr_api_key",
        "servarr.apps.lidarr.api_key": "istota_servarr_lidarr_api_key",
        "servarr.apps.prowlarr.api_key": "istota_servarr_prowlarr_api_key",
    }


def test_setup_paths_are_the_container_layout():
    assert reference.setup_paths() == {
        "db_path": "/data/db/istota.db",
        "workspace_path": "/data/workspace",
        "temp_dir": "/data/tmp",
    }


def _resolves(path: tuple[str, ...]) -> bool:
    """Whether `path` names a field of the `Config` tree, or a key a dict or
    list-of-dataclasses field holds (`[models.aliases]`, `[[default_briefings]]`)."""
    node: object = Config()
    for index, part in enumerate(path):
        if isinstance(node, dict):
            return True
        if not dataclasses.is_dataclass(node):
            return False
        names = {f.name for f in dataclasses.fields(node)}
        if part not in names:
            return False
        value = getattr(node, part)
        if isinstance(value, list) and index + 1 < len(path):
            return True
        node = value
    return True


class TestFieldDocs:
    def test_a_key_carries_its_prose(self):
        docs = reference.field_docs(CONFIG_DIR)
        assert "name your bot uses" in docs[("bot_name",)]
        assert "Availability failover" in docs[("brain", "fallback")]

    def test_paragraphs_are_joined_and_separated(self):
        docs = reference.docs_from_text(
            "[a]\n# First line\n# continues here.\n#\n# Second paragraph.\n# x = 1\n"
        )
        assert docs[("a", "x")] == "First line continues here.\n\nSecond paragraph."

    def test_every_documented_path_resolves_on_the_config_tree(self):
        unresolved = sorted(
            ".".join(path) for path in reference.field_docs(CONFIG_DIR) if not _resolves(path)
        )
        assert unresolved == []

    def test_an_absent_example_is_no_docs(self, tmp_path):
        assert reference.field_docs(tmp_path) == {}

    def test_it_rereads_a_changed_file(self, tmp_path):
        example = tmp_path / "config.example.toml"
        example.write_text("# The bot's name.\n# bot_name = \"x\"\n")
        assert reference.field_docs(tmp_path) == {("bot_name",): "The bot's name."}
        example.write_text("# Its name.\n# bot_name = \"x\"\n")
        stat = example.stat()
        os.utime(example, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert reference.field_docs(tmp_path) == {("bot_name",): "Its name."}
