"""``[brain.native] extra_headers`` from its stack secret file.

`istota-secrets` exports `istota_brain_native_extra_headers` as
``ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS``: a JSON object of header name to string
value. A valid one replaces the inline table whole; anything else is a
`discarded` problem that names the variable and never the value, and the
inline table stays.
"""

from __future__ import annotations

import json
import logging
import textwrap

import pytest

from istota.config import load_config
from istota.config_mapper import collect_problems

VAR = "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS"
SENTINEL = "never-shown-header-sentinel"

INLINE = """
    [brain.native]
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


def _about_headers(problems):
    return [p for p in problems if p["path"][:3] == ("brain", "native", "extra_headers")]


class TestFromTheVariable:
    def test_a_valid_object_replaces_the_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, json.dumps({"Authorization": f"Bearer {SENTINEL}", "X-Tenant": "t"}))
        config, problems = _load(tmp_path)
        assert config.brain.native.extra_headers == {
            "Authorization": f"Bearer {SENTINEL}", "X-Tenant": "t",
        }
        assert _about_headers(problems) == []

    def test_it_applies_with_no_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, json.dumps({"X-Tenant": "t"}))
        config, _ = _load(tmp_path, '[brain.native]\nmodel = "model-x"\n')
        assert config.brain.native.extra_headers == {"X-Tenant": "t"}

    def test_an_empty_object_clears_the_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VAR, "{}")
        config, _ = _load(tmp_path)
        assert config.brain.native.extra_headers == {}

    def test_unset_or_empty_keeps_the_inline_table(self, tmp_path, monkeypatch):
        monkeypatch.delenv(VAR, raising=False)
        config, _ = _load(tmp_path)
        assert config.brain.native.extra_headers == {"X-Inline": "inline-value"}
        monkeypatch.setenv(VAR, "")
        config, problems = _load(tmp_path)
        assert config.brain.native.extra_headers == {"X-Inline": "inline-value"}
        assert _about_headers(problems) == []


@pytest.mark.parametrize("raw", [
    f"not json {SENTINEL}",
    json.dumps([SENTINEL]),
    json.dumps(SENTINEL),
    json.dumps({"X-Number": 7, "X-Secret": SENTINEL}),
    json.dumps({"X-Nested": {"inner": SENTINEL}}),
])
class TestARefusedValue:
    def test_it_is_discarded_and_the_inline_table_kept(self, tmp_path, monkeypatch, raw):
        monkeypatch.setenv(VAR, raw)
        config, problems = _load(tmp_path)
        assert config.brain.native.extra_headers == {"X-Inline": "inline-value"}
        found = _about_headers(problems)
        assert [p["kind"] for p in found] == ["discarded"]
        assert VAR in found[0]["message"]

    def test_the_value_is_in_no_problem_and_no_log_line(self, tmp_path, monkeypatch, caplog, raw):
        monkeypatch.setenv(VAR, raw)
        with caplog.at_level(logging.DEBUG):
            _, problems = _load(tmp_path)
        assert SENTINEL not in json.dumps(problems)
        assert SENTINEL not in caplog.text
