"""Three top-level keys moved into the section that reads them.

`advisor_model` is the claude CLI's (`[brain.claude_code]`, read by that
brain), `custom_system_prompt` is every brain's (`[brain]`), and
`[[briefing_shared_blocks]]` is the briefings module's
(`[[briefings.shared_blocks]]`). The old spellings still load, rewritten before
the walk by `normalize_legacy_document`.
"""

import textwrap

from istota import config as config_module
from istota.config import Config, legacy_spellings, load_config
from istota.config_mapper import collect_problems
from istota.webui import admin_config_view as view


def _load(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(text))
    with collect_problems() as problems:
        config = load_config(path)
    return config, problems


def _kinds_at(problems, path):
    return [p["kind"] for p in problems if p["path"] == path]


class TestAdvisorModel:
    def test_new_spelling_loads(self, tmp_path):
        config, problems = _load(tmp_path, """
            [brain.claude_code]
            advisor_model = "opus"
        """)
        assert config.brain.claude_code.advisor_model == "opus"
        assert _kinds_at(problems, ("advisor_model",)) == []

    def test_old_spelling_moves_with_a_warning(self, tmp_path):
        config, problems = _load(tmp_path, 'advisor_model = "opus"\n')
        assert config.brain.claude_code.advisor_model == "opus"
        assert not hasattr(config, "advisor_model")
        assert _kinds_at(problems, ("advisor_model",)) == ["warning"]

    def test_new_spelling_wins_over_old(self, tmp_path):
        config, problems = _load(tmp_path, """
            advisor_model = "haiku"
            [brain.claude_code]
            advisor_model = "opus"
        """)
        assert config.brain.claude_code.advisor_model == "opus"
        assert _kinds_at(problems, ("advisor_model",)) == ["retired"]

    def test_validation_names_the_new_path(self, tmp_path):
        _, problems = _load(tmp_path, """
            [brain.claude_code]
            advisor_model = "opus:high"
        """)
        assert "warning" in _kinds_at(problems, ("brain", "claude_code", "advisor_model"))


class TestCustomSystemPrompt:
    def test_new_spelling_loads(self, tmp_path):
        config, _ = _load(tmp_path, "[brain]\ncustom_system_prompt = true\n")
        assert config.brain.custom_system_prompt is True

    def test_old_spelling_moves_with_a_warning(self, tmp_path):
        config, problems = _load(tmp_path, "custom_system_prompt = true\n")
        assert config.brain.custom_system_prompt is True
        assert not hasattr(config, "custom_system_prompt")
        assert _kinds_at(problems, ("custom_system_prompt",)) == ["warning"]

    def test_old_spelling_beside_an_existing_brain_table(self, tmp_path):
        config, _ = _load(tmp_path, """
            custom_system_prompt = true
            [brain]
            kind = "claude_code"
        """)
        assert config.brain.custom_system_prompt is True
        assert config.brain.kind == "claude_code"


class TestSharedBlocks:
    BLOCK = """
        name = "headlines"
        cron = "0 6 * * *"
        [[{table}.sources]]
        kind = "browse"
        config = {{ preset = "ap" }}
    """

    def test_new_spelling_loads(self, tmp_path):
        config, _ = _load(tmp_path, "[[briefings.shared_blocks]]" + self.BLOCK.format(table="briefings.shared_blocks"))
        assert [b.name for b in config.briefings.shared_blocks] == ["headlines"]
        assert config.briefings.shared_blocks[0].sources[0]["kind"] == "browse"

    def test_old_spelling_moves_with_a_warning(self, tmp_path):
        config, problems = _load(tmp_path, "[[briefing_shared_blocks]]" + self.BLOCK.format(table="briefing_shared_blocks"))
        assert [b.name for b in config.briefings.shared_blocks] == ["headlines"]
        assert not hasattr(config, "briefing_shared_blocks")
        assert _kinds_at(problems, ("briefing_shared_blocks",)) == ["warning"]

    def test_old_spelling_keeps_the_rest_of_the_briefings_table(self, tmp_path):
        config, _ = _load(tmp_path, """
            briefing_shared_blocks = []
            [briefings]
            shared_block_timezone = "Europe/Berlin"
        """)
        assert config.briefings.shared_blocks == []
        assert config.briefings.shared_block_timezone == "Europe/Berlin"

    def test_absent_seeds_the_defaults(self, tmp_path):
        config, _ = _load(tmp_path, 'bot_name = "Istota"\n')
        assert "world-headlines" in {b.name for b in config.briefings.shared_blocks}

    def test_empty_list_opts_out(self, tmp_path):
        config, _ = _load(tmp_path, "[briefings]\nshared_blocks = []\n")
        assert config.briefings.shared_blocks == []

    def test_not_a_list_is_discarded(self, tmp_path):
        config, problems = _load(tmp_path, '[briefings]\nshared_blocks = "x"\n')
        assert _kinds_at(problems, ("briefings", "shared_blocks")) == ["discarded"]
        assert config.briefings.shared_blocks == []


class TestLegacySpellings:
    def test_each_new_path_names_its_old_one(self):
        assert legacy_spellings(("brain", "claude_code", "advisor_model")) == [("advisor_model",)]
        assert legacy_spellings(("brain", "custom_system_prompt")) == [("custom_system_prompt",)]
        assert legacy_spellings(("briefings", "shared_blocks")) == [("briefing_shared_blocks",)]

    def test_normalize_leaves_a_non_table_target_alone(self):
        document = {"brain": "nonsense", "advisor_model": "opus"}
        config_module.normalize_legacy_document(document)
        assert document == {"brain": "nonsense", "advisor_model": "opus"}


class TestAdminView:
    def _fields(self, payload):
        return {f["key"]: (s["key"], f) for s in payload["sections"] for f in s["fields"]}

    def test_each_key_is_in_its_section(self):
        fields = self._fields(view.build_config_view(Config()))
        assert fields["brain.claude_code.advisor_model"][0] == "brain.claude_code"
        assert fields["brain.custom_system_prompt"][0] == "brain"
        assert fields["briefings.shared_blocks"][0] == "briefings"
        for gone in ("advisor_model", "custom_system_prompt", "briefing_shared_blocks"):
            assert gone not in fields

    def test_shared_blocks_render_as_a_count(self):
        fields = self._fields(view.build_config_view(Config()))
        assert fields["briefings.shared_blocks"][1]["type"] == "count"

    def test_an_old_spelling_shows_in_its_new_place(self):
        document = "[[briefing_shared_blocks]]\nname = \"x\"\ncron = \"* * * * *\"\n"
        fields = self._fields(view.build_config_view(Config(), document=document))
        entry = fields["briefings.shared_blocks"][1]
        assert entry["in_file"] is True
        assert entry["toml"].startswith("[[shared_blocks]]\n")
