"""``[private_hosts]``: one table of grants, and each consumer's reach as written.

The same host name means something different to each consumer, so what this
holds is that a grant reaches only the consumers it names, and that a config
written with the old per-section lists keeps every grant.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import textwrap
from pathlib import Path

import pytest

from istota import apply, config as config_module
from istota.apply import CheckResult
from istota.config import (
    PRIVATE_HOST_CONSUMERS,
    Config,
    config_from_document,
    fold_section_private_hosts,
    load_config,
)
from istota.config_editor import edits, handoff, schema
from istota.config_editor.secrets import SENTINEL, redact_document
from istota.config_mapper import collect_problems
from istota.lib import toml_write
from istota.webui import admin_config_view as view
from istota.webui.admin_config_view import is_secret_path

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "wiki.example.internal"


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    with collect_problems() as problems:
        config = load_config(path, apply_db_overlays=False)
    return config, [problem for problem in problems if "private_hosts" in problem["path"]]


def _grants(config) -> dict[str, list[str]]:
    return {name: getattr(config, name).private_hosts for name in PRIVATE_HOST_CONSUMERS}


def _kinds(problems) -> list[tuple[str, tuple[str, ...]]]:
    return [(problem["kind"], tuple(problem["path"])) for problem in problems]


class TestTheConsumers:
    def test_the_six_are_the_ones_with_the_field(self):
        assert PRIVATE_HOST_CONSUMERS == (
            "browser", "wordpress", "mail_accounts", "sharing", "servarr", "feeds",
        )
        for name in PRIVATE_HOST_CONSUMERS:
            assert getattr(Config(), name).private_hosts == []

    @pytest.mark.parametrize("consumer", PRIVATE_HOST_CONSUMERS)
    def test_a_grant_reaches_the_consumer_it_names_and_no_other(self, tmp_path, consumer):
        config, problems = _load(tmp_path, f"""
            [private_hosts]
            "{HOST}" = ["{consumer}"]
        """)
        assert problems == []
        assert _grants(config) == {
            name: [HOST] if name == consumer else [] for name in PRIVATE_HOST_CONSUMERS
        }
        assert config.private_hosts == {HOST: [consumer]}

    def test_a_grant_naming_two_reaches_those_two(self, tmp_path):
        config, _ = _load(tmp_path, f"""
            [private_hosts]
            "{HOST}" = ["browser", "feeds"]
        """)
        assert _grants(config) == {
            name: [HOST] if name in ("browser", "feeds") else [] for name in PRIVATE_HOST_CONSUMERS
        }

    @pytest.mark.parametrize("consumer", PRIVATE_HOST_CONSUMERS)
    def test_an_old_key_loads_as_the_table_would(self, tmp_path, consumer):
        config, problems = _load(tmp_path, f"""
            [{consumer}]
            private_hosts = ["{HOST}"]
        """)
        assert _kinds(problems) == [("warning", (consumer, "private_hosts"))]
        assert HOST not in problems[0]["message"]
        assert f'[{consumer}] private_hosts moved to [private_hosts]' in problems[0]["message"]
        assert f'"<host>" = ["{consumer}"]' in problems[0]["message"]
        assert _grants(config) == {
            name: [HOST] if name == consumer else [] for name in PRIVATE_HOST_CONSUMERS
        }
        assert config.private_hosts == {HOST: [consumer]}


class TestTheTwoSpellings:
    def test_six_old_keys_and_their_table_load_the_same(self, tmp_path):
        old = "\n".join(
            f'[{name}]\nprivate_hosts = ["{name}.example.internal", "10.0.0.5"]\n'
            for name in PRIVATE_HOST_CONSUMERS
        )
        table = "[private_hosts]\n" + "".join(
            f'"{name}.example.internal" = ["{name}"]\n' for name in PRIVATE_HOST_CONSUMERS
        ) + '"10.0.0.5" = [' + ", ".join(f'"{name}"' for name in PRIVATE_HOST_CONSUMERS) + "]\n"
        from_old, old_problems = _load(tmp_path, old)
        from_table, table_problems = _load(tmp_path, table)
        assert table_problems == []
        assert len(old_problems) == len(PRIVATE_HOST_CONSUMERS)
        # A field's order follows the table's keys, and the two wrote those differently.
        for name in PRIVATE_HOST_CONSUMERS:
            assert sorted(getattr(from_old, name).private_hosts) == sorted(getattr(from_table, name).private_hosts)
        assert from_old.private_hosts == from_table.private_hosts

    def test_the_same_host_in_both_is_the_union(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            [private_hosts]
            "{HOST}" = ["browser"]
            "other.example.internal" = ["sharing"]

            [feeds]
            private_hosts = ["{HOST}"]
        """)
        assert _kinds(problems) == [("warning", ("feeds", "private_hosts"))]
        assert config.private_hosts == {HOST: ["browser", "feeds"], "other.example.internal": ["sharing"]}
        assert config.browser.private_hosts == [HOST]
        assert config.feeds.private_hosts == [HOST]
        assert config.servarr.private_hosts == []

    def test_an_empty_old_list_is_popped_with_no_problem(self, tmp_path):
        config, problems = _load(tmp_path, "[feeds]\nprivate_hosts = []\n")
        assert problems == []
        assert config.private_hosts == {}
        assert config.feeds.private_hosts == []

    def test_the_fold_logs_nothing_and_returns_what_it_found(self, caplog):
        caplog.set_level(logging.DEBUG, logger="istota.config")
        document = {"feeds": {"private_hosts": [HOST]}, "sharing": {"private_hosts": []}}
        found = fold_section_private_hosts(document)
        assert [(problem["kind"], tuple(problem["path"])) for problem in found] == [
            ("warning", ("feeds", "private_hosts")),
        ]
        assert document == {"feeds": {}, "sharing": {}, "private_hosts": {HOST: ["feeds"]}}
        assert caplog.records == []

    def test_nothing_to_fold_adds_no_table(self):
        document = {"feeds": {}}
        assert fold_section_private_hosts(document) == []
        assert document == {"feeds": {}}

    def test_folding_twice_finds_nothing_new(self):
        document = {"feeds": {"private_hosts": [HOST]}}
        fold_section_private_hosts(document)
        assert fold_section_private_hosts(document) == []
        assert document["private_hosts"] == {HOST: ["feeds"]}

    def test_the_loader_reports_what_the_fold_returned(self, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        document = {"feeds": {"private_hosts": [HOST]}}
        config_module.normalize_legacy_document(document)
        assert [r for r in caplog.records if "[feeds] private_hosts moved" in r.getMessage()]
        assert document["private_hosts"] == {HOST: ["feeds"]}

    def test_a_section_that_is_not_a_table_loses_nothing_from_the_table(self, tmp_path):
        config, _ = _load(tmp_path, f"""
            browser = 3

            [private_hosts]
            "{HOST}" = ["feeds"]
        """)
        assert config.feeds.private_hosts == [HOST]


class TestShapes:
    def test_a_host_with_no_consumers_grants_nothing(self, tmp_path):
        config, problems = _load(tmp_path, f'[private_hosts]\n"{HOST}" = []\n')
        assert problems == []
        assert all(hosts == [] for hosts in _grants(config).values())

    def test_an_unknown_consumer_is_discarded_and_the_others_apply(self, tmp_path):
        config, problems = _load(tmp_path, f'[private_hosts]\n"{HOST}" = ["browsr", "feeds"]\n')
        assert _kinds(problems) == [("discarded", ("private_hosts", HOST))]
        assert "browsr" in problems[0]["message"]
        assert config.feeds.private_hosts == [HOST]
        assert config.browser.private_hosts == []
        assert config.private_hosts == {HOST: ["feeds"]}

    def test_a_consumer_that_is_not_a_string_is_discarded(self, tmp_path):
        config, problems = _load(tmp_path, f'[private_hosts]\n"{HOST}" = [1, "feeds"]\n')
        assert _kinds(problems) == [("discarded", ("private_hosts", HOST))]
        assert config.feeds.private_hosts == [HOST]

    def test_an_unquoted_dotted_host_says_to_quote_it_and_grants_nothing(self, tmp_path):
        config, problems = _load(tmp_path, '[private_hosts]\nwiki.internal = ["browser"]\n')
        assert _kinds(problems) == [("discarded", ("private_hosts", "wiki"))]
        assert "quoted" in problems[0]["message"]
        assert all(hosts == [] for hosts in _grants(config).values())
        assert config.private_hosts == {}

    def test_a_host_whose_value_is_a_string_is_discarded(self, tmp_path):
        config, problems = _load(tmp_path, f'[private_hosts]\n"{HOST}" = "browser"\n')
        assert _kinds(problems) == [("discarded", ("private_hosts", HOST))]
        assert config.browser.private_hosts == []

    def test_a_top_level_list_is_discarded_and_old_keys_beside_it_still_load(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            private_hosts = ["shared.example.internal"]

            [feeds]
            private_hosts = ["{HOST}"]
        """)
        assert sorted(_kinds(problems)) == [
            ("discarded", ("private_hosts",)),
            ("warning", ("feeds", "private_hosts")),
        ]
        assert config.private_hosts == {HOST: ["feeds"]}
        assert "shared.example.internal" not in config.browser.private_hosts

    def test_a_non_list_at_a_host_gives_way_to_the_old_grant(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            [private_hosts]
            "{HOST}" = "browser"

            [feeds]
            private_hosts = ["{HOST}"]
        """)
        assert sorted(_kinds(problems)) == [
            ("discarded", ("private_hosts", HOST)),
            ("warning", ("feeds", "private_hosts")),
        ]
        assert config.private_hosts == {HOST: ["feeds"]}

    def test_an_old_key_that_is_not_a_list_is_discarded(self, tmp_path):
        config, problems = _load(tmp_path, f'[feeds]\nprivate_hosts = "{HOST}"\n')
        assert _kinds(problems) == [("discarded", ("feeds", "private_hosts"))]
        assert HOST not in problems[0]["message"]
        assert config.feeds.private_hosts == []

    def test_an_old_entry_that_is_not_a_string_is_discarded_and_the_rest_kept(self, tmp_path):
        config, problems = _load(tmp_path, f"""
            [feeds]
            private_hosts = [1, {{a = 1}}, "{HOST}", ["x"], true]
        """)
        assert sorted(_kinds(problems)) == [
            ("discarded", ("feeds", "private_hosts", "0")),
            ("discarded", ("feeds", "private_hosts", "1")),
            ("discarded", ("feeds", "private_hosts", "3")),
            ("discarded", ("feeds", "private_hosts", "4")),
            ("warning", ("feeds", "private_hosts")),
        ]
        assert config.feeds.private_hosts == [HOST]

    @pytest.mark.parametrize("entry", ["https://wiki", "wiki:8443", "*.lan"])
    def test_a_malformed_host_warns_and_stays_visible(self, tmp_path, entry):
        config, problems = _load(tmp_path, f'[private_hosts]\n"{entry}" = ["browser"]\n"ok.lan" = ["browser"]\n')
        assert _kinds(problems) == [("warning", ("private_hosts",))]
        assert repr(entry) in problems[0]["message"]
        assert "[private_hosts]" in problems[0]["message"]
        assert "ok.lan" not in problems[0]["message"]
        assert entry in config.private_hosts
        assert entry in config.browser.private_hosts

    def test_two_spellings_of_one_host_both_land(self, tmp_path):
        config, _ = _load(tmp_path, '[private_hosts]\n"wiki.lan" = ["feeds"]\n"WIKI.LAN." = ["feeds"]\n')
        assert config.feeds.private_hosts == ["wiki.lan", "WIKI.LAN."]

    def test_a_duplicate_consumer_is_read_once(self, tmp_path):
        config, problems = _load(tmp_path, f'[private_hosts]\n"{HOST}" = ["feeds", "feeds"]\n')
        assert problems == []
        assert config.private_hosts == {HOST: ["feeds"]}
        assert config.feeds.private_hosts == [HOST]

    def test_whitespace_around_a_host_is_stripped_on_the_field(self, tmp_path):
        config, problems = _load(tmp_path, '[private_hosts]\n" wiki.lan " = ["feeds"]\n"wiki.lan" = ["feeds"]\n')
        assert problems == []
        assert config.feeds.private_hosts == ["wiki.lan"]


class TestThroughTheDocumentPaths:
    DOCUMENT = {
        "private_hosts": {HOST: ["browser", "feeds"], "other.example.internal": ["sharing"]},
        "servarr": {"private_hosts": ["10.0.0.5"]},
    }

    def test_config_from_document_agrees_with_load_config(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(toml_write.dumps(self.DOCUMENT))
        loaded = load_config(path, apply_db_overlays=False)
        from_document = config_from_document(self.DOCUMENT)
        assert _grants(from_document) == _grants(loaded)
        assert from_document.private_hosts == loaded.private_hosts
        assert loaded.servarr.private_hosts == ["10.0.0.5"]

    def test_config_from_document_leaves_the_callers_document(self):
        document = {"feeds": {"private_hosts": [HOST]}}
        config_from_document(document)
        assert document == {"feeds": {"private_hosts": [HOST]}}

    def test_a_plan_with_an_old_key_is_applied_with_a_warning(self):
        result = apply.collect({"feeds": {"private_hosts": [HOST]}})
        assert not result.refused
        assert [problem["kind"] for problem in result.problems if "private_hosts" in problem["path"]] == ["warning"]

    def test_a_plan_naming_an_unknown_consumer_is_refused(self):
        result = apply.collect({"private_hosts": {HOST: ["browsr"]}})
        assert result.refused
        assert [problem["path"] for problem in result.discarded] == [["private_hosts", HOST]]

    def test_a_rendered_config_carrying_empty_lists_reports_nothing(self, tmp_path):
        rendered = tmp_path / "config.toml"
        shutil.copy(FIXTURES / "bare_metal" / "role-config.toml", rendered)
        with collect_problems() as problems:
            load_config(rendered, apply_db_overlays=False)
        assert [problem for problem in problems if "private_hosts" in problem["path"]] == []


@pytest.mark.parametrize("host", ["compass.lan", "authentik.home.lan", "tokens.lan", "secret.lan"])
class TestAHostNamedLikeACredential:
    def test_the_predicate_does_not_take_it_for_one(self, host):
        assert not is_secret_path(("private_hosts", host))
        assert not is_secret_path(("private_hosts", host, "0"))

    def test_the_view_shows_it_with_its_consumers(self, host):
        payload = view.build_config_view(Config(private_hosts={host: ["browser", "feeds"]}))
        general = next(section for section in payload["sections"] if section["key"] == "general")
        field = next(entry for entry in general["fields"] if entry["name"] == "private_hosts")
        assert field["value"] == {host: ["browser", "feeds"]}
        assert field["secret"] is False
        assert field["type"] == "dict"
        assert field["set"] is True

    def test_a_loaded_problem_shows_the_value(self, tmp_path, host):
        _, problems = _load(tmp_path, f'[private_hosts]\n"{host}" = ["browsr"]\n')
        assert "browsr" in problems[0]["message"]

    def test_redaction_leaves_it(self, host):
        document = {"private_hosts": {host: ["browser"]}}
        assert redact_document(document) == document

    def test_a_table_edit_previews_over_an_old_key(self, host):
        async def run_check(document: dict, **kwargs) -> CheckResult:
            return CheckResult()

        found = asyncio.run(edits.preview(
            f'[feeds]\nprivate_hosts = ["{host}"]\n',
            {"ops": [{
                "op": "set_toml", "path": ["private_hosts"],
                "toml": f'[private_hosts]\n"{host}" = ["browser"]\n',
            }]},
            config=Config(admin_users={"alice"}), acting_admin="alice",
            run_check=run_check, environ={},
        ))
        assert found.errors == []
        assert found.inline_secrets == {}
        assert found.document["private_hosts"] == {host: ["browser"]}
        assert SENTINEL not in str(found.document)

    def test_the_handoff_holds_no_credential_for_it(self, tmp_path, host):
        handoff.write(tmp_path, {"config": {"private_hosts": {host: ["browser"]}}}, {}, False)
        assert handoff._credential_values(tmp_path) == set()


def test_the_table_uses_the_editors_table_control():
    assert ("private_hosts",) in schema.TABLE_PATHS
    assert schema.editor_kind(("private_hosts",), dict[str, list[str]], {}) == "table"
