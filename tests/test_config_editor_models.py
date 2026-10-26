"""`config_editor.models.model_problems`: the model check the `apply --check`
child runs over a proposed document."""

from __future__ import annotations

from istota.brain import _roles
from istota.config import Config
from istota.config_editor.models import model_problems


def _config(aliases: dict | None = None) -> Config:
    config = Config()
    config.models.aliases = aliases or {}
    return config


def _kinds(problems: list[dict]) -> set[tuple[str, tuple]]:
    return {(p["kind"], tuple(p["path"])) for p in problems}


class TestProposedAliases:
    def test_a_proposed_alias_resolves_a_field_and_leaves_the_process_table_alone(self):
        before = _roles.current_alias_table()
        aliases = {"house": "sonnet:high"}
        document = {"models": {"aliases": aliases}, "brain": {"claude_code": {"model": "house"}}}
        assert model_problems(document, _config(aliases)) == []
        assert _roles.current_alias_table() is before
        assert before.target("house", "anthropic") is None

    def test_an_alias_whose_target_does_not_resolve_is_a_warning(self):
        aliases = {"house": {"anthropic": "anthropic/claude-nope-9"}}
        problems = model_problems({"models": {"aliases": aliases}}, _config(aliases))
        assert ("model_unknown", ("models", "aliases", "house")) in _kinds(problems)


class TestFields:
    def test_only_fields_the_document_sets_are_checked(self):
        """A default (`fast`, `smart:high`) is never reported."""
        assert model_problems({}, _config()) == []

    def test_a_native_model_is_not_judged_while_native_has_no_model_list(self):
        config = _config()
        config.brain.native.provider = "openrouter"
        document = {"brain": {"native": {"provider": "openrouter", "model": "some-vendor/new-model"}}}
        assert model_problems(document, config) == []

    def test_a_check_that_raises_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "istota.llm.discovery.catalogue_from_caches",
            lambda config: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert model_problems({"advisor_model": "nosuch/x"}, _config()) == []
