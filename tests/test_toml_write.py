"""The tree's one TOML writer, and that nobody keeps a second.

`lib/toml_write.py` replaced three: the standalone wizard's string escaper with
hand-built lines, the container wizard's `tomli_w`, and the testbed's own. What
holds is the round trip, `tomllib.loads(dumps(doc)) == doc`, over the shapes a
config actually has, plus a source sweep so a fourth copy fails here.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import tomllib
from pathlib import Path

import pytest

from istota.lib import toml_write

REPO = Path(__file__).resolve().parent.parent

DOCUMENTS = [
    {},
    {"a": 1, "b": "two", "c": True, "d": 1.5, "e": [1, 2], "f": []},
    {"top": "x", "t": {"inner": {"deep": {"k": "v"}}}},
    {"empty": {}, "nested": {"also_empty": {}}},
    {"resources": [{"type": "folder", "path": "/a"}, {"type": "folder", "path": "/b", "x": {"y": 1}}]},
    {"users": {"alice": {"briefings": [{"name": "m", "components": {"todos": True}}]}}},
    {"mixed": [1, "a", {"k": "v"}], "inline_empty": [{}]},
    {"quoted keys": {"a.b": 1, "with space": "v", "ünï": "x"}},
    {"escapes": "tab\there \"quote\" back\\slash bell\x07 del\x7f nl\n"},
    {"when": dt.date(2026, 10, 9), "at": dt.datetime(2026, 10, 9, 12, 30, tzinfo=dt.timezone.utc), "t": dt.time(7, 0)},
    {"big": 2**62, "neg": -3, "inf": math.inf, "ninf": -math.inf},
]


@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_round_trip_is_exact(document):
    assert tomllib.loads(toml_write.dumps(document)) == document


def test_nan_is_written_as_nan():
    assert math.isnan(tomllib.loads(toml_write.dumps({"x": math.nan}))["x"])


@pytest.mark.parametrize("bad", [{"x": None}, {"x": [1, None]}, {"x": object()}, {1: "a"}])
def test_a_value_toml_cannot_hold_is_refused(bad):
    with pytest.raises(toml_write.TomlWriteError):
        toml_write.dumps(bad)


def test_the_container_config_round_trips():
    from istota.setup_wizard import ContainerAnswers, container_config_document

    answers = ContainerAnswers(user_id="alice", hostname="bot.example.com", ingress="direct")
    document = container_config_document(answers, inline_credentials=True)
    assert tomllib.loads(toml_write.dumps(document)) == document


def test_the_testbed_writes_with_it():
    from testbed import stack

    assert stack.toml_dumps is toml_write.dumps


def test_the_wizard_escapes_with_it():
    from istota import setup_wizard

    assert setup_wizard._toml_str is toml_write.toml_string


_SECOND_WRITER = re.compile(r"^\s*(import tomli_w|from tomli_w import)|^def _?toml_(str|string|dumps|value)\(", re.M)
#: Writers of documents that are not istota's config, which predate this module.
_OTHER_DOCUMENTS = {
    "src/istota/money/routes.py": "money's own config tables",
    "src/istota/cli_money.py": "money's own config tables",
    "src/istota/maintenance/room_mount_reconcile.py": "a CRON.md block",
    "src/istota/cron_loader.py": "CRON.md, with its own backtick escape (ISSUE-385)",
}


def test_no_second_writer_for_a_config():
    """Test fixtures may write TOML however they like; product code and the
    testbed, which writes the stacks' config.toml, may not grow a second writer."""
    offenders = []
    for root in ("src", "testbed"):
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel in _OTHER_DOCUMENTS or rel == "src/istota/lib/toml_write.py":
                continue
            if _SECOND_WRITER.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(rel)
    assert offenders == [], f"a second TOML writer: {offenders}"
