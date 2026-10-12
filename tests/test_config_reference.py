"""The `istota_config` reference in ansible/defaults/main.yml is the generated one.

Regenerate after a config field or the example's prose changes:

    uv run env ISTOTA_UPDATE_CONFIG_REFERENCE=1 pytest tests/test_config_reference.py -n0
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.support import config_reference as ref
from tests.test_config_field_coverage import walk_fields
from tests.test_prompt_golden import updating

from istota import config as config_module
from istota.config import Config

REPO = Path(__file__).resolve().parent.parent
UPDATE_ENV = "ISTOTA_UPDATE_CONFIG_REFERENCE"
UPDATE_CMD = f"uv run env {UPDATE_ENV}=1 pytest tests/test_config_reference.py -n0"
DEFAULTS = REPO / "ansible" / "defaults" / "main.yml"
EXAMPLE = REPO / "config" / "config.example.toml"


def _generated() -> str:
    return ref.render(EXAMPLE.read_text())


def _uncommented(block: str) -> str:
    body = block.split("# istota_config:\n", 1)[1].rsplit(ref.END, 1)[0]
    return "istota_config:\n" + "\n".join(re.sub(r"^# ?", "", line) for line in body.splitlines())


def test_the_defaults_carry_the_generated_reference():
    current = DEFAULTS.read_text()
    generated = _generated()
    if updating(UPDATE_ENV, UPDATE_CMD):
        DEFAULTS.write_text(ref.splice(current, generated))
        current = DEFAULTS.read_text()
    assert ref.BEGIN in current and ref.END in current
    start = current.index(ref.BEGIN)
    end = current.index(ref.END) + len(ref.END) + 1
    assert current[start:end] == generated, (
        f"the istota_config reference is stale; regenerate with `{UPDATE_CMD}`"
    )


def test_the_reference_is_comments_only_and_istota_config_stays_empty():
    assert yaml.safe_load(DEFAULTS.read_text())["istota_config"] == {}


def test_uncommented_it_is_yaml_naming_every_setting():
    """Each leaf field the coverage guard counts is a key at its own path."""
    tree = yaml.safe_load(_uncommented(_generated()))["istota_config"]
    missing = []
    for path in walk_fields(Config()):
        if path.split(".", 1)[0] in config_module._NOT_CONFIGURATION or "._" in f".{path}":
            continue
        node = tree
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(path)
                break
            node = node[part]
    assert missing == []


def test_uncommented_defaults_equal_the_dataclass_defaults():
    tree = yaml.safe_load(_uncommented(_generated()))["istota_config"]
    config = Config()
    assert tree["bot_name"] == config.bot_name
    assert tree["nextcloud"]["share_default_expire_days"] == config.nextcloud.share_default_expire_days
    assert tree["scheduler"]["poll_interval"] == config.scheduler.poll_interval
    assert tree["brain"]["kind"] == config.brain.kind


def test_the_example_prose_lands_on_its_key():
    docs = ref.read_example(EXAMPLE.read_text())
    assert any("name your bot uses" in line for line in docs.keys[("", "bot_name")])
    assert any("Availability failover" in line for line in docs.keys[("brain", "fallback")])
    # A trailing comment and its continuation lines.
    transient = " ".join(docs.keys[("brain", "fallback_on_transient")])
    assert "also reroute" in transient and "(429 / 5xx / 529" in transient


class TestTheExampleReader:
    def test_a_key_takes_the_comment_block_above_it_and_its_trailing_note(self):
        docs = ref.read_example(
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
        docs = ref.read_example("[a]\n# About something else.\n\nx = 1\n")
        assert docs.keys[("a", "x")] == []

    def test_prose_that_looks_like_an_assignment_is_not_a_key(self):
        docs = ref.read_example("[a]\n# lower = more intermediate text is shown live.\nx = 1\n")
        assert ("a", "lower") not in docs.keys


def test_credentials_point_at_their_secrets_file():
    secrets = ref.credential_files()
    assert secrets["nextcloud.app_password"] == "istota_nextcloud_app_password"
    assert secrets["web.session_secret_key"] == "istota_web_session_secret_key"
    assert secrets["brain.native.api_key"] == "istota_brain_native_api_key"
    assert secrets["sms.twilio.auth_token"] == ""
    assert secrets["whatsapp.cloud.app_secret"] == ""
    block = _generated()
    assert "set it as istota_secrets.istota_nextcloud_app_password, not here" in block


def test_the_container_paths_are_the_ones_setup_writes():
    tree = yaml.safe_load(_uncommented(_generated()))["istota_config"]
    assert tree["db_path"] == "/data/db/istota.db"
    assert tree["temp_dir"] == "/data/tmp"
