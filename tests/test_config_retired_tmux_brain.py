"""A config written for the retired tmux brain still loads, as `claude_code`.

`tmux_claude` left in `[brain] kind` would stop the daemon at `make_brain`, so
`config._retire_tmux_brain` moves every place a kind is named onto
`claude_code` with a `retired` problem, and ignores `[brain.tmux]` apart from
the model a tmux deployment was running.
"""

import textwrap

from istota.brain import make_brain, resolve_brain_kind
from istota.brain.claude_code import ClaudeCodeBrain
from istota.config import load_config
from istota.config_mapper import collect_problems


def _load(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(text))
    with collect_problems() as problems:
        config = load_config(path)
    return config, problems


def _kinds_at(problems, path):
    return [p["kind"] for p in problems if p["path"] == path]


class TestTheKind:
    def test_a_tmux_deployment_loads_and_builds_the_cli_brain(self, tmp_path):
        config, problems = _load(tmp_path, """
            [brain]
            kind = "tmux_claude"
        """)
        assert config.brain.kind == "claude_code"
        assert isinstance(make_brain(config.brain), ClaudeCodeBrain)
        assert _kinds_at(problems, ("brain", "kind")) == ["retired"]

    def test_the_tmux_model_is_the_one_it_keeps_running(self, tmp_path):
        config, problems = _load(tmp_path, """
            model = "haiku"
            [brain]
            kind = "tmux_claude"
            [brain.tmux]
            model = "opus"
            effort = "high"
            cli_version_pin = "2.1.168"
        """)
        assert config.brain.claude_code.model == "opus"
        assert config.brain.claude_code.effort == "high"
        assert not hasattr(config.brain, "tmux")
        assert _kinds_at(problems, ("brain", "tmux")) == ["retired"]

    def test_a_claude_code_model_already_set_wins(self, tmp_path):
        config, _ = _load(tmp_path, """
            [brain]
            kind = "tmux_claude"
            [brain.claude_code]
            model = "sonnet"
            [brain.tmux]
            model = "opus"
        """)
        assert config.brain.claude_code.model == "sonnet"

    def test_a_stray_tmux_block_is_ignored_on_another_kind(self, tmp_path):
        config, problems = _load(tmp_path, """
            [brain.tmux]
            model = "opus"
        """)
        assert config.brain.kind == "claude_code"
        assert config.brain.claude_code.model == ""
        assert _kinds_at(problems, ("brain", "tmux")) == ["retired"]

    def test_a_current_config_reports_nothing(self, tmp_path):
        _, problems = _load(tmp_path, """
            [brain]
            kind = "claude_code"
        """)
        assert [p for p in problems if p["path"][:1] == ("brain",)] == []


class TestTheOtherPlacesAKindIsNamed:
    def test_fallback_overrides_and_room_selectable(self, tmp_path):
        config, problems = _load(tmp_path, """
            [brain]
            kind = "native"
            fallback = "tmux_claude"
            room_selectable = ["tmux_claude", "claude_code", "native"]
            [brain.source_type_overrides]
            scheduled = "tmux_claude"
        """)
        assert config.brain.fallback == "claude_code"
        assert config.brain.source_type_overrides == {"scheduled": "claude_code"}
        assert config.brain.room_selectable == ["claude_code", "native"]
        assert _kinds_at(problems, ("brain", "fallback")) == ["retired"]
        assert _kinds_at(problems, ("brain", "source_type_overrides", "scheduled")) == ["retired"]
        assert _kinds_at(problems, ("brain", "room_selectable")) == ["retired"]

    def test_a_row_still_pinned_to_tmux_runs_the_deployment_kind(self, tmp_path):
        config, _ = _load(tmp_path, """
            [brain]
            kind = "claude_code"
            room_selectable = ["claude_code", "native"]
        """)
        resolved = resolve_brain_kind("talk", config.brain, override="tmux_claude")
        assert resolved.kind == "claude_code"
