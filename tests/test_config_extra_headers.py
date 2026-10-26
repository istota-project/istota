"""A provider's ``extra_headers`` from its stack secret file.

`istota-secrets` exports `istota_provider_<id>_extra_headers` as
``ISTOTA_PROVIDER_<ID>_EXTRA_HEADERS``: a JSON object of header name to string
value. A valid one replaces the provider's inline table whole; anything else is
a `discarded` problem that names the variable and never the value, and the
inline table stays.

For one release the legacy ``ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS`` is read the
same way, as native's provider's headers (``config.LEGACY_PROVIDER_SOURCES``),
and a current variable wins over it.
"""

from __future__ import annotations

import json
import logging
import textwrap

import pytest

from istota.config import load_config
from istota.config_mapper import collect_problems

VAR = "ISTOTA_PROVIDER_OPENROUTER_EXTRA_HEADERS"
LEGACY_VAR = "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS"
SENTINEL = "never-shown-header-sentinel"

INLINE = """
    [brain.native]
    provider = "openrouter"
    model = "model-x"

    [providers.openrouter.extra_headers]
    X-Inline = "inline-value"
"""

LEGACY = """
    [brain.native]
    base_url = "https://openrouter.ai/api/v1"
    model = "model-x"

    [brain.native.extra_headers]
    X-Inline = "inline-value"
"""


def _load(tmp_path, body: str = INLINE):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    with collect_problems() as problems:
        config = load_config(path)
    return config, problems


def _headers(config):
    return config.providers["openrouter"].extra_headers


def _about_headers(problems):
    return [
        p for p in problems
        if p["kind"] == "discarded" and p["path"][-1] == "extra_headers"
    ]


@pytest.fixture(autouse=True)
def _no_ambient_headers(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv(LEGACY_VAR, raising=False)


class TestFromTheVariable:
    def test_a_valid_object_replaces_the_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, json.dumps({"Authorization": f"Bearer {SENTINEL}", "X-Tenant": "t"}))
        config, problems = _load(tmp_path)
        assert _headers(config) == {"Authorization": f"Bearer {SENTINEL}", "X-Tenant": "t"}
        assert _about_headers(problems) == []

    def test_it_applies_with_no_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, json.dumps({"X-Tenant": "t"}))
        config, _ = _load(tmp_path, '[brain.native]\nprovider = "openrouter"\nmodel = "model-x"\n')
        assert _headers(config) == {"X-Tenant": "t"}

    def test_an_empty_object_clears_the_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, "{}")
        config, _ = _load(tmp_path)
        assert _headers(config) == {}

    def test_unset_or_empty_keeps_the_inline_table(self, tmp_path, monkeypatch):
        config, _ = _load(tmp_path)
        assert _headers(config) == {"X-Inline": "inline-value"}
        monkeypatch.setenv(VAR, "")
        config, problems = _load(tmp_path)
        assert _headers(config) == {"X-Inline": "inline-value"}
        assert _about_headers(problems) == []

    def test_it_reaches_the_providers_spec(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, json.dumps({"X-Tenant": "t"}))
        config, _ = _load(tmp_path)
        assert dict(config.brain.provider_specs["openrouter"].extra_headers) == {"X-Tenant": "t"}


class TestTheLegacyVariable:
    def test_it_is_read_as_the_implied_providers_headers(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LEGACY_VAR, json.dumps({"X-Tenant": "legacy"}))
        config, _ = _load(tmp_path, LEGACY)
        assert config.brain.native.provider == "openrouter"
        assert _headers(config) == {"X-Tenant": "legacy"}

    def test_the_legacy_inline_table_is_read_without_it(self, tmp_path):
        config, _ = _load(tmp_path, LEGACY)
        assert _headers(config) == {"X-Inline": "inline-value"}

    def test_the_current_variable_wins_over_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LEGACY_VAR, json.dumps({"X-Tenant": "legacy"}))
        monkeypatch.setenv(VAR, json.dumps({"X-Tenant": "current"}))
        config, _ = _load(tmp_path, LEGACY)
        assert _headers(config) == {"X-Tenant": "current"}


@pytest.mark.parametrize("variable, body", [(VAR, INLINE), (LEGACY_VAR, LEGACY)])
@pytest.mark.parametrize("raw", [
    f"not json {SENTINEL}",
    json.dumps([SENTINEL]),
    json.dumps(SENTINEL),
    json.dumps({"X-Number": 7, "X-Secret": SENTINEL}),
    json.dumps({"X-Nested": {"inner": SENTINEL}}),
])
class TestARefusedValue:
    def test_it_is_discarded_and_the_inline_table_kept(self, tmp_path, monkeypatch, raw, variable, body):
        monkeypatch.setenv(variable, raw)
        config, problems = _load(tmp_path, body)
        assert _headers(config) == {"X-Inline": "inline-value"}
        found = _about_headers(problems)
        assert [p["kind"] for p in found] == ["discarded"]
        assert variable in found[0]["message"]

    def test_the_value_is_in_no_problem_and_no_log_line(
        self, tmp_path, monkeypatch, caplog, raw, variable, body,
    ):
        monkeypatch.setenv(variable, raw)
        with caplog.at_level(logging.DEBUG):
            _, problems = _load(tmp_path, body)
        assert SENTINEL not in json.dumps(problems)
        assert SENTINEL not in caplog.text
