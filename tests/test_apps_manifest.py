"""`istota.apps.capabilities` and `istota.apps.manifest`: what an app may ask for, and its manifest."""

from __future__ import annotations

import pytest

from istota.apps.capabilities import (
    ALWAYS_GRANTED,
    SKILL_OPERATIONS,
    Capability,
    CapabilityError,
    describe,
    is_sensitive,
    parse_capability,
)
from istota.apps.manifest import Manifest, ManifestError, parse_manifest

BOT_DIR = "istota"

VALID = """\
id = "board"
name = "Board"
icon = "layout"
capabilities = ["kv:own", "calendar:read"]
description = "Cards and columns next to a conversation."

[display]
narrowest = "phone"
reason = ""
"""


def _parse(text, folder="board"):
    return parse_manifest(text, folder_name=folder, bot_dir=BOT_DIR)


def _errors(text, folder="board"):
    with pytest.raises(ManifestError) as caught:
        _parse(text, folder)
    return caught.value.errors


class TestParseCapability:
    @pytest.mark.parametrize("text, name, scope", [
        ("kv:own", "kv:own", None),
        ("files:own", "files:own", None),
        ("kv:read:notes", "kv:read", "notes"),
        ("kv:read:app", "kv:read", "app"),
        ("files:read:notes", "files:read", "notes"),
        ("files:read:notes/aviation weather", "files:read", "notes/aviation weather"),
        ("calendar:read", "calendar:read", None),
        ("calendar:read:Personal", "calendar:read", "Personal"),
        ("skill:markets.quote", "skill", "markets.quote"),
        ("skill:memory_search.timeline", "skill", "memory_search.timeline"),
    ])
    def test_accepted(self, text, name, scope):
        cap = parse_capability(text, bot_dir=BOT_DIR)
        assert cap == Capability(name, scope)
        assert cap.text == text

    @pytest.mark.parametrize("text", [
        "",
        "kv",
        "kv:own:x",
        "kv:write:notes",
        "run:ls",
        "files:read:",
        "files:read:/etc",
        "files:read:notes/../memories",
        "files:read:notes//x",
        "files:read:./notes",
        "files:read:notes/.hidden",
        "files:read:.config",
        f"files:read:{BOT_DIR}",
        f"files:read:{BOT_DIR}/x",
        "files:read:memories",
        "files:read:Memories/2026",
        "kv:read:",
        "kv:read:_x",
        "kv:read:app.other",
        "kv:read:has space",
        "calendar:read:",
        "calendar:read:two\nlines",
        "skill:markets.trade",
        "skill:shell.run",
        "skill:",
    ])
    def test_refused(self, text):
        with pytest.raises(CapabilityError):
            parse_capability(text, bot_dir=BOT_DIR)

    def test_a_non_string_is_refused(self):
        with pytest.raises(CapabilityError):
            parse_capability(3, bot_dir=BOT_DIR)  # type: ignore[arg-type]

    def test_the_skill_allowlist_is_the_six_the_spec_names(self):
        assert SKILL_OPERATIONS == frozenset({
            "markets.quote", "health.trend", "location.current", "location.places",
            "memory_search.facts", "memory_search.timeline",
        })

    def test_always_granted(self):
        assert ALWAYS_GRANTED == ("kv:own", "files:own")

    def test_every_capability_has_a_plain_description(self):
        for text in ["kv:own", "files:own", "kv:read:notes", "files:read:notes",
                     "calendar:read", "calendar:read:Personal",
                     *(f"skill:{op}" for op in sorted(SKILL_OPERATIONS))]:
            line = describe(parse_capability(text, bot_dir=BOT_DIR))
            assert line and "\n" not in line

    def test_a_description_names_the_scope(self):
        assert "notes" in describe(parse_capability("files:read:notes", bot_dir=BOT_DIR))
        assert "Personal" in describe(parse_capability("calendar:read:Personal", bot_dir=BOT_DIR))

    def test_only_health_is_sensitive(self):
        assert is_sensitive(parse_capability("skill:health.trend", bot_dir=BOT_DIR))
        assert not is_sensitive(parse_capability("skill:markets.quote", bot_dir=BOT_DIR))
        assert not is_sensitive(parse_capability("kv:own", bot_dir=BOT_DIR))


class TestParseManifest:
    def test_a_valid_manifest(self):
        manifest = _parse(VALID)
        assert isinstance(manifest, Manifest)
        assert manifest.id == "board"
        assert manifest.name == "Board"
        assert manifest.icon == "layout"
        assert manifest.entry == "index.html"
        assert manifest.description == "Cards and columns next to a conversation."
        assert manifest.narrowest == "phone"
        assert manifest.reason == ""
        assert manifest.warnings == ()

    def test_kv_own_and_files_own_are_always_added_first(self):
        manifest = _parse(VALID)
        assert [cap.text for cap in manifest.capabilities] == [
            "kv:own", "files:own", "calendar:read",
        ]

    def test_added_when_the_manifest_names_none(self):
        manifest = _parse('id = "board"\nname = "Board"\nicon = "layout"\n')
        assert [cap.text for cap in manifest.capabilities] == ["kv:own", "files:own"]

    def test_a_duplicate_capability_is_listed_once(self):
        text = VALID.replace('["kv:own", "calendar:read"]', '["calendar:read", "calendar:read"]')
        assert [cap.text for cap in _parse(text).capabilities] == [
            "kv:own", "files:own", "calendar:read",
        ]

    def test_name_and_description_are_flattened_to_one_line(self):
        text = VALID.replace('name = "Board"', 'name = "My\\n  Board"').replace(
            'description = "Cards and columns next to a conversation."',
            'description = """Two\nlines"""',
        )
        manifest = _parse(text)
        assert manifest.name == "My Board"
        assert manifest.description == "Two lines"

    def test_the_id_must_equal_the_folder_name(self):
        errors = _errors(VALID, folder="tides")
        assert any("folder" in error for error in errors)

    @pytest.mark.parametrize("app_id", ["Board", "-board", "bo_ard", "", "a" * 41, "board/x"])
    def test_a_bad_id(self, app_id):
        assert _errors(VALID.replace('id = "board"', f'id = "{app_id}"'), folder=app_id or "x")

    def test_an_unknown_capability(self):
        errors = _errors(VALID.replace('"calendar:read"', '"shell:run"'))
        assert any("shell:run" in error for error in errors)

    @pytest.mark.parametrize("cap", [
        f"files:read:{BOT_DIR}/x", "files:read:memories", "kv:read:_x", "kv:read:app.other",
    ])
    def test_a_capability_out_of_scope(self, cap):
        errors = _errors(VALID.replace('"calendar:read"', f'"{cap}"'))
        assert any(cap in error for error in errors)

    def test_a_wider_display_needs_a_reason(self):
        errors = _errors(VALID.replace('narrowest = "phone"', 'narrowest = "desktop"'))
        assert any("reason" in error for error in errors)

    def test_a_wider_display_with_a_reason(self):
        text = VALID.replace('narrowest = "phone"', 'narrowest = "desktop"').replace(
            'reason = ""', 'reason = "A graph worth reading needs the width."',
        )
        manifest = _parse(text)
        assert manifest.narrowest == "desktop"
        assert manifest.reason == "A graph worth reading needs the width."

    def test_an_unknown_display_range(self):
        assert _errors(VALID.replace('narrowest = "phone"', 'narrowest = "watch"'))

    def test_the_display_table_is_optional(self):
        manifest = _parse('id = "board"\nname = "Board"\nicon = "layout"\n')
        assert manifest.narrowest == "phone"

    def test_the_entry_must_be_index_html(self):
        assert _parse('entry = "index.html"\n' + VALID).entry == "index.html"
        text = 'entry = "main.html"\n' + VALID
        assert any("entry" in error for error in _errors(text))

    def test_an_unknown_top_level_key_is_a_warning(self):
        manifest = _parse('version = "2"\n' + VALID)
        assert len(manifest.warnings) == 1
        assert "version" in manifest.warnings[0]

    def test_missing_required_keys(self):
        errors = _errors('icon = "layout"\n')
        assert any("id" in error for error in errors)
        assert any("name" in error for error in errors)

    def test_a_long_name_and_description(self):
        text = VALID.replace('name = "Board"', f'name = "{"n" * 41}"')
        assert any("name" in error for error in _errors(text))
        text = VALID.replace(
            'description = "Cards and columns next to a conversation."', f'description = "{"d" * 201}"',
        )
        assert any("description" in error for error in _errors(text))

    @pytest.mark.parametrize("icon", ["Layout", "lay out", "", "x" * 41])
    def test_a_bad_icon(self, icon):
        assert _errors(VALID.replace('icon = "layout"', f'icon = "{icon}"'))

    def test_wrong_types(self):
        errors = _errors(VALID.replace('name = "Board"', "name = 3").replace(
            '["kv:own", "calendar:read"]', '"kv:own"',
        ))
        assert any("name" in error for error in errors)
        assert any("capabilities" in error for error in errors)

    def test_invalid_toml(self):
        errors = _errors("id = \n")
        assert len(errors) == 1
        assert "manifest.toml" in errors[0]

    def test_every_error_is_collected(self):
        errors = _errors('id = "Board"\nicon = "Bad Icon"\ncapabilities = ["nope"]\n', folder="board")
        assert len(errors) >= 4
