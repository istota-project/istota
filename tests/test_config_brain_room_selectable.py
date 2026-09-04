"""``[brain] room_selectable`` — the round trip and the mapper hook.

The key is the operator's allowlist of brain kinds a room may pin for itself,
and it is a gate: empty means no room may override the brain at all. That makes
the two defect classes ``config_mapper.py`` records worth ruling out explicitly
here, because both of them look from the outside like a feature that simply
does not work.

**A field the loader never read.** A setting declared on the dataclass,
documented in ``config.example.toml`` and missing from the loader reads to an
operator as a value they set and a daemon that ignored it. The walk covers
``list[str]`` on its own, but "covers it" is exactly the standing the eleven
settings had that turned out not to be read, so the round trip is asserted
rather than assumed.

**A typo that did nothing.** A kind name is compared literally against the
buildable set, so ``" native"`` matches nothing and grants nothing — and here
the consequence of a silently inert entry is a room that cannot select the
brain the operator thought they had enabled. The hook strips, so the two
questions "did the value load" and "is the kind allowed" stay separable.

That separation is the other thing under test. The loader is not the gate: it
normalizes text and nothing else, and an unbuildable name survives it and is
dropped by ``brain.room_selectable_kinds`` at the point a room override is
admitted. Stage 2 of the per-room-brain-selection spec.
"""

from __future__ import annotations

import logging
import re
import textwrap
from pathlib import Path

from istota.brain import reachable_brain_kinds, room_selectable_kinds
from istota.config import BrainConfig, Config, load_config

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO / "config" / "config.example.toml"


def _load(tmp_path, body: str) -> Config:
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(body))
    return load_config(cfg)


class TestTheDefault:
    """Empty, so the feature ships inert."""

    def test_an_absent_key_leaves_no_kind_selectable(self, tmp_path):
        config = _load(tmp_path, '[brain]\nkind = "claude_code"\n')
        assert config.brain.room_selectable == []
        assert room_selectable_kinds(config.brain) == frozenset()

    def test_an_absent_brain_section_is_the_same_answer(self, tmp_path):
        config = _load(tmp_path, 'bot_name = "Istota"\n')
        assert config.brain.room_selectable == []

    def test_the_dataclass_default_is_the_only_default(self):
        assert BrainConfig().room_selectable == []


class TestTheRoundTrip:
    def test_a_list_reaches_the_dataclass(self, tmp_path):
        config = _load(
            tmp_path,
            """
            [brain]
            kind = "claude_code"
            room_selectable = ["claude_code", "native"]
            """,
        )
        assert config.brain.room_selectable == ["claude_code", "native"]

    def test_a_loaded_list_is_what_a_room_may_pin(self, tmp_path):
        """The round trip through to the consumer, not only to the field."""
        config = _load(
            tmp_path,
            """
            [brain]
            kind = "claude_code"
            room_selectable = ["native"]
            """,
        )
        assert room_selectable_kinds(config.brain) == frozenset({"native"})

    def test_a_loaded_list_widens_the_reachable_set(self, tmp_path):
        """Which is what the `doctor` checks read, so this is the whole route
        from a TOML line to a check that runs."""
        config = _load(
            tmp_path,
            """
            [brain]
            kind = "native"
            room_selectable = ["tmux_claude"]
            """,
        )
        assert reachable_brain_kinds(config.brain) == frozenset(
            {"native", "tmux_claude"}
        )

    def test_an_empty_list_is_honoured_as_written(self, tmp_path):
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = []
            """,
        )
        assert config.brain.room_selectable == []


class TestTheHook:
    def test_surrounding_whitespace_is_stripped(self, tmp_path):
        """A rendered config is where a stray space comes from, and an entry
        that matches nothing is indistinguishable from one nobody wrote."""
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = ["  native  ", "claude_code\t"]
            """,
        )
        assert config.brain.room_selectable == ["native", "claude_code"]

    def test_blank_entries_are_dropped(self, tmp_path):
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = ["native", "", "   "]
            """,
        )
        assert config.brain.room_selectable == ["native"]

    def test_a_number_is_stringified_rather_than_crashing(self, tmp_path):
        """TOML will hold one, and every consumer indexes this list with
        strings. It matches no kind, which is the honest outcome."""
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = ["native", 3]
            """,
        )
        assert config.brain.room_selectable == ["native", "3"]
        assert room_selectable_kinds(config.brain) == frozenset({"native"})

    def test_a_bare_string_keeps_the_default_and_warns(self, tmp_path, caplog):
        """Never iterated into one entry per character.

        The warning is the point of the branch: without it an operator who
        wrote a string instead of a list gets a feature that is silently off
        and no line anywhere saying why.
        """
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load(
                tmp_path,
                """
                [brain]
                room_selectable = "native"
                """,
            )
        assert config.brain.room_selectable == []
        assert any(
            "brain.room_selectable" in record.getMessage()
            and "a list of brain kinds" in record.getMessage()
            for record in caplog.records
        )

    def test_a_number_in_place_of_the_list_keeps_the_default(self, tmp_path):
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = 7
            """,
        )
        assert config.brain.room_selectable == []


class TestTheLoaderIsNotTheGate:
    """Normalizing text and deciding what a room may run are two jobs."""

    def test_an_unbuildable_kind_survives_the_load(self, tmp_path):
        """Kept on the field so the value an operator wrote is still visible in
        the admin config view, and warned about where it is refused instead."""
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = ["native", "gpt_5_brain"]
            """,
        )
        assert config.brain.room_selectable == ["native", "gpt_5_brain"]

    def test_and_is_dropped_before_any_room_can_pin_it(self, tmp_path):
        config = _load(
            tmp_path,
            """
            [brain]
            room_selectable = ["native", "gpt_5_brain"]
            """,
        )
        assert room_selectable_kinds(config.brain) == frozenset({"native"})


class TestItIsDocumented:
    def test_the_example_config_names_the_key(self):
        text = EXAMPLE_CONFIG.read_text()
        assert re.search(r"^#\s*room_selectable\s*=", text, re.M), (
            "room_selectable is settable and undocumented in "
            "config.example.toml. It works and there is no way to learn it "
            "exists."
        )

    def test_the_documentation_says_the_default_is_inert(self):
        """The one thing an operator has to know before reading anything else:
        naming no kind is not a partial enablement, it is off."""
        text = EXAMPLE_CONFIG.read_text()
        block = text.split("room_selectable", 1)[0].rsplit("[brain]", 1)[-1]
        assert "Empty (the default)" in block
