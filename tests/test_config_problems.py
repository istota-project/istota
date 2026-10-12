"""Typed config problems: what the loader discarded, did not recognise, or retired.

A value the loader throws away used to be only a log line, and `istota apply`
found the throwaways by matching the wording, which missed every validator that
phrased it differently. `config_mapper.report_problem` logs the same line and,
under `collect_problems()`, records `{kind, path, message}` as well, so a caller
decides on the kind rather than on the words.

The hook table below is the guard that matters: every `_CONFIG_HOOKS` entry has
a bad value here, and the coverage test fails when a hook is added without one.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from istota import config as config_module
from istota.config import (
    ENV_OVERRIDES,
    FIELD_CHOICES,
    MULTI_CHOICES,
    SECRET_ENV_OVERRIDES,
    Config,
    load_config,
)
from istota.config_mapper import (
    PROBLEM_KINDS,
    coerce_int,
    collect_problems,
    report_problem,
)
from istota.lib.toml_write import dumps


def _nest(path: tuple[str, ...], value: object) -> dict:
    document: dict = {}
    node = document
    for name in path[:-1]:
        node = node.setdefault(name, {})
    node[path[-1]] = value
    return document


def _load(tmp_path, document: dict) -> list[dict]:
    p = tmp_path / "config.toml"
    p.write_text(dumps(document))
    with collect_problems() as problems:
        load_config(p)
    return problems


def _resolve(path: tuple[str, ...]) -> object:
    node: object = Config()
    for name in path:
        assert dataclasses.is_dataclass(node), f"{'.'.join(path)}: {name} is under a non-section"
        assert name in {f.name for f in dataclasses.fields(node)}, f"{'.'.join(path)} is not a field"
        node = getattr(node, name)
    return node


#: One value per `_CONFIG_HOOKS` key that the hook must refuse.
BAD_HOOK_VALUES: dict[str, object] = {
    "web.auth": ["bogus"],
    "web.token_storage": "plain",
    "developer.container": {"exec_socket_dir": "relative/dir"},
    "developer.forge_cli_extra_denied": 5,
    "developer.forge_cli_permit": 5,
    "developer.review": "not a table",
    "developer.review.file_budget": 0,
    "developer.review.snapshot_max_bytes": 0,
    "developer.review.snapshot_max_file_bytes": 0,
    "security.sandbox_cache_dir": [1],
    "security.sandbox_cache_max_gb": -1.0,
    "security.credential_broker.scan_max_bytes": 0,
    "security.credential_broker.leaf_validity_hours": 0,
    "security.sandbox_ro_paths": "/srv/app",
    "scheduler.email_task_queue": "backgroud",
    "scheduler.talk_poll_timeout": 0,
    "scheduler.talk_poll_wait": -1.0,
    "scheduler.talk_poll_full_sweep_interval": -1,
    "talk.signaling.room_sync_interval": 0,
    "talk.signaling.reconnect_backoff_max": 0,
    "advisor_model": 5,
    "health.max_document_bytes": -1,
    "security.vault_fetch_limit_per_task": -1,
    "security.vault_writes_per_task": -1,
    "security.wallet_requests_per_task": -1,
    "security.wallet_fills_per_purchase": -1,
    "security.wallet_authorization_minutes": "soon",
    "brain.fallback": 5,
    "brain.source_type_overrides": "scheduled",
    "brain.room_selectable": "native",
}


class TestEveryHookReportsADiscard:
    def test_every_hook_has_a_bad_value_here(self):
        assert set(BAD_HOOK_VALUES) == set(config_module._CONFIG_HOOKS)

    @pytest.mark.parametrize("key", sorted(BAD_HOOK_VALUES))
    def test_a_bad_value_is_recorded_as_discarded_at_its_path(self, tmp_path, key):
        path = tuple(key.split("."))
        problems = _load(tmp_path, _nest(path, BAD_HOOK_VALUES[key]))
        discarded = [p for p in problems if p["kind"] == "discarded"]
        assert any(p["path"][: len(path)] == path for p in discarded), problems

    def test_a_good_value_records_nothing(self, tmp_path):
        problems = _load(tmp_path, {"scheduler": {"talk_poll_timeout": 30}, "web": {"auth": ["email"]}})
        assert problems == []


class TestTheWalk:
    def test_a_wrong_type_is_discarded(self, tmp_path):
        problems = _load(tmp_path, {"scheduler": {"max_subtasks_per_task": "many"}})
        assert problems == [{
            "kind": "discarded",
            "path": ("scheduler", "max_subtasks_per_task"),
            "message": "[config] scheduler.max_subtasks_per_task='many' is not an integer; "
                       "ignoring the value and keeping the default",
        }]

    def test_a_section_that_is_not_a_table_is_discarded(self, tmp_path):
        problems = _load(tmp_path, {"scheduler": 5})
        assert [(p["kind"], p["path"]) for p in problems] == [("discarded", ("scheduler",))]

    def test_a_list_element_is_addressed_by_index(self, tmp_path):
        problems = _load(tmp_path, {"security": {"network": {"extra_hosts": ["a.example:443", [7]]}}})
        assert [(p["kind"], p["path"]) for p in problems] == [
            ("discarded", ("security", "network", "extra_hosts", "1")),
        ]

    def test_unknown_keys_are_recorded_one_each(self, tmp_path):
        problems = _load(tmp_path, {"breifings": {"x": 1}, "scheduler": {"max_subtask_dept": 2}})
        unknown = sorted(p["path"] for p in problems if p["kind"] == "unknown")
        assert unknown == [("breifings",), ("scheduler", "max_subtask_dept")]

    def test_a_not_configuration_field_is_unknown(self, tmp_path):
        problems = _load(tmp_path, {"config_path": "/elsewhere.toml"})
        assert [(p["kind"], p["path"]) for p in problems] == [("unknown", ("config_path",))]


class TestRetiredKeys:
    @pytest.mark.parametrize("document, path", [
        ({"ntfy": {"topic": "x"}}, ("ntfy",)),
        ({"skills": {"always_eager": []}}, ("skills",)),
        ({"devbox": {"docker_cli": "docker"}}, ("devbox", "docker_cli")),
        ({"rooms": {"shared_room_data_policy": "x"}}, ("rooms", "shared_room_data_policy")),
        ({"security": {"sandbox_admin_db_write": True}}, ("security", "sandbox_admin_db_write")),
        ({"scheduler": {"istota_file_poll_interval": 9}}, ("scheduler", "istota_file_poll_interval")),
        ({"models": {"roles": {"fast": "haiku"}}}, ("models", "roles")),
        ({"developer": {"container": {"backend": "none"}}}, ("developer", "container", "backend")),
        ({"model": "sonnet"}, ("model",)),
    ])
    def test_a_retired_key_is_recorded_as_retired(self, tmp_path, document, path):
        problems = _load(tmp_path, document)
        assert ("retired", path) in [(p["kind"], p["path"]) for p in problems], problems


class TestHandParsedSections:
    def test_an_unknown_experimental_feature_is_a_warning(self, tmp_path):
        problems = _load(tmp_path, {"experimental": {"features": ["no_such_flag"]}})
        assert [(p["kind"], p["path"]) for p in problems] == [("warning", ("experimental", "features"))]

    def test_models_aliases_that_is_not_a_table_is_discarded(self, tmp_path):
        problems = _load(tmp_path, {"models": {"aliases": 5}})
        assert ("discarded", ("models", "aliases")) in [(p["kind"], p["path"]) for p in problems]

    def test_a_shared_block_with_no_cron_is_discarded(self, tmp_path):
        problems = _load(tmp_path, {"briefing_shared_blocks": [{"name": "news"}]})
        assert [(p["kind"], p["path"]) for p in problems] == [("discarded", ("briefing_shared_blocks", "0"))]

    def test_a_clamped_percentage_is_discarded(self, tmp_path):
        problems = _load(tmp_path, {"brain": {"claude_code": {"subscription_usage_high_percent": 150.0}}})
        assert ("discarded", ("brain", "claude_code", "subscription_usage_high_percent")) in [
            (p["kind"], p["path"]) for p in problems
        ]

    def test_an_unknown_room_selectable_kind_is_a_warning(self, tmp_path):
        problems = _load(tmp_path, {"brain": {"room_selectable": ["nativ"]}})
        assert [(p["kind"], p["path"]) for p in problems] == [("warning", ("brain", "room_selectable"))]


class TestRedaction:
    def test_a_credential_value_never_reaches_the_message_or_the_log(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        problems = _load(tmp_path, {"email": {"imap_password": ["hunter2-value"]}})
        assert problems[0]["path"] == ("email", "imap_password")
        assert "<redacted>" in problems[0]["message"]
        assert "hunter2-value" not in problems[0]["message"]
        assert "hunter2-value" not in caplog.text

    def test_anything_under_extra_headers_is_redacted_whatever_its_name(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        problems = _load(tmp_path, {"brain": {"native": {"extra_headers": "x-harmless sk-live-value"}}})
        assert problems[0]["path"] == ("brain", "native", "extra_headers")
        assert "sk-live-value" not in problems[0]["message"]
        assert "sk-live-value" not in caplog.text

    def test_an_ordinary_value_is_shown(self, tmp_path):
        problems = _load(tmp_path, {"web": {"token_storage": "plain"}})
        assert "'plain'" in problems[0]["message"]

    def test_a_non_secret_key_is_shown_though_its_name_matches(self, tmp_path):
        # `web.token_storage` matches TOKEN by name and is allowlisted by path.
        problems = _load(tmp_path, {"web": {"token_storage": 5}})
        assert "5" in problems[0]["message"] and "<redacted>" not in problems[0]["message"]


class TestTheReporter:
    def test_it_logs_the_same_line_with_or_without_a_collector(self, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        coerce_int("x", "scheduler.poll_interval")
        with collect_problems() as problems:
            coerce_int("x", "scheduler.poll_interval")
        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 2 and lines[0] == lines[1] == problems[0]["message"]

    def test_nothing_is_recorded_outside_a_collector(self):
        with collect_problems() as outer:
            pass
        report_problem("warning", ("scheduler",), "outside")
        assert outer == []

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            report_problem("odd", ("scheduler",), "x")

    def test_the_kinds(self):
        assert PROBLEM_KINDS == ("discarded", "unknown", "retired", "warning")


class TestVocabularies:
    def test_every_field_choice_resolves_and_admits_its_default(self):
        for path, choices in FIELD_CHOICES.items():
            assert isinstance(choices, tuple) and choices
            assert _resolve(path) in choices, path

    def test_every_multi_choice_resolves_and_admits_its_default(self):
        for path, choices in MULTI_CHOICES.items():
            values = choices()
            assert isinstance(values, tuple) and values
            assert set(_resolve(path)) <= set(values), path

    def test_the_vocabularies_are_the_validators_own(self):
        from istota.brain import KNOWN_BRAIN_KINDS
        from istota.experimental import KNOWN_FEATURES
        from istota.mail.outbound_policy import VALID_POLICIES
        from istota.rooms.speech_gate import MODES
        from istota.webui.map_basemap import provider_names

        assert config_module._CONFIG_HOOKS["web.token_storage"].choices == FIELD_CHOICES[("web", "token_storage")]
        assert FIELD_CHOICES[("email", "outbound_approval_floor")] == VALID_POLICIES
        assert FIELD_CHOICES[("email", "confirm_sender_match")] == config_module.CONFIRM_SENDER_MATCH_POLICIES
        assert set(FIELD_CHOICES[("brain", "kind")]) == KNOWN_BRAIN_KINDS
        assert set(FIELD_CHOICES[("brain", "fallback")]) == KNOWN_BRAIN_KINDS | {""}
        assert FIELD_CHOICES[("speech_gate", "mode")] == MODES
        assert FIELD_CHOICES[("web", "map", "provider")] == provider_names()
        assert set(MULTI_CHOICES[("brain", "room_selectable")]()) == KNOWN_BRAIN_KINDS
        assert MULTI_CHOICES[("experimental", "features")]() == tuple(KNOWN_FEATURES)

    def test_one_of_carries_its_choices(self):
        hook = config_module._one_of("a", "b")
        assert hook.choices == ("a", "b")


class TestEnvOverrides:
    def test_every_secret_override_names_a_string_field(self):
        for env_var, section, field in SECRET_ENV_OVERRIDES:
            assert env_var.startswith("ISTOTA_")
            assert isinstance(_resolve((*section.split("."), field)), str), env_var

    def test_every_plain_override_names_a_field(self):
        for env_var, path in ENV_OVERRIDES:
            assert env_var.startswith("ISTOTA_")
            _resolve(path)

    def test_the_lifted_tables_still_apply(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_EMAIL_IMAP_PASSWORD", "from-env")
        monkeypatch.setenv("ISTOTA_WEB_AUTH", "email")
        monkeypatch.setenv("ISTOTA_WEB_TOKEN_STORAGE", "encrypted")
        p = tmp_path / "config.toml"
        p.write_text("")
        cfg = load_config(p)
        assert cfg.email.imap_password == "from-env"
        assert cfg.web.auth == ["email"]
        assert cfg.web.token_storage == "encrypted"
