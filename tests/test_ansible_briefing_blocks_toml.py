"""The Ansible ``istota_briefing_blocks_toml`` filter renders valid, seedable TOML.

Config-authored briefing blocks are in-memory-only Config; the Ansible path
provisions schedule/delivery via the CLI/DB but must render a content-only
``[[users.X.briefings]]`` stub so the blocks reach the module-DB seeder. This
verifies the filter's output parses as TOML and normalises to the expected
seeder specs.
"""

import importlib.util
import tomllib
from pathlib import Path

import pytest

from istota.briefings import normalize_block_specs
from istota.config import _parse_user_data


_FILTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "ansible" / "filter_plugins" / "istota_toml.py"
)


def _load_filter():
    spec = importlib.util.spec_from_file_location("istota_toml", _FILTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.istota_briefing_blocks_toml


render = _load_filter()


_USERS = {
    "dana": {
        "briefings": [
            {  # blocks-bearing → rendered
                "name": "world",
                "cron": "0 7 * * *",
                "output": "email",
                "blocks": [
                    {
                        "title": "World News",
                        "directive": "3-5 stories, neutral.",
                        "render_mode": "synthesis",
                        "options": {"story_count": 5},
                        "sources": [
                            {"kind": "rss", "config": {
                                "feed_ref": {"kind": "category", "value": 4},
                                "lookback_hours": 24,
                            }},
                            {"kind": "browse", "config": {"preset": "ap"}},
                        ],
                    },
                    {
                        "title": "Markets",
                        "render_mode": "structured",
                        "sources": [{"kind": "markets", "config": {}}],
                    },
                ],
            },
            {  # components-only → NOT rendered by this filter
                "name": "plain",
                "cron": "0 8 * * *",
                "components": {"news": True},
            },
        ]
    }
}


class TestBriefingBlocksTomlFilter:
    def test_empty_when_no_blocks(self):
        assert render({}) == ""
        assert render({"u": {"briefings": [{"name": "x", "cron": "* * * * *"}]}}) == ""
        assert render("not a dict") == ""

    def test_renders_parseable_toml(self):
        out = render(_USERS)
        parsed = tomllib.loads(out)
        briefings = parsed["users"]["dana"]["briefings"]
        # Only the blocks-bearing briefing is rendered.
        assert [b["name"] for b in briefings] == ["world"]
        blocks = briefings[0]["blocks"]
        assert [b["title"] for b in blocks] == ["World News", "Markets"]
        assert blocks[0]["options"] == {"story_count": 5}
        assert blocks[0]["sources"][0]["config"]["feed_ref"] == {
            "kind": "category", "value": 4,
        }

    def test_round_trips_through_parse_and_normalize(self):
        out = render(_USERS)
        parsed = tomllib.loads(out)
        uc = _parse_user_data(parsed["users"]["dana"], "dana")
        # The single rendered briefing carries the raw blocks passthrough.
        assert len(uc.briefings) == 1
        specs = normalize_block_specs(uc.briefings[0].blocks, briefing_name="world")
        assert [s["title"] for s in specs] == ["World News", "Markets"]
        assert [s["kind"] for s in specs[0]["sources"]] == ["rss", "browse"]
        assert specs[1]["render_mode"] == "structured"

    def test_string_escaping(self):
        users = {"u": {"briefings": [{
            "name": "q", "cron": "* * * * *",
            "blocks": [{
                "title": 'Say "hi"\\bye',
                "sources": [{"kind": "notes", "config": {}}],
            }],
        }]}}
        parsed = tomllib.loads(render(users))
        title = parsed["users"]["u"]["briefings"][0]["blocks"][0]["title"]
        assert title == 'Say "hi"\\bye'


def test_config_example_blocks_round_trip():
    """The worked example in config.example.toml parses and seeds cleanly."""
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.toml"
    parsed = tomllib.loads(example.read_text())
    alice = parsed["users"]["alice"]
    world = [b for b in alice["briefings"] if b["name"] == "world"]
    assert world, "expected a config-authored 'world' briefing with blocks"
    uc = _parse_user_data(alice, "alice")
    world_bc = [b for b in uc.briefings if b.name == "world"][0]
    specs = normalize_block_specs(world_bc.blocks, briefing_name="world")
    assert [s["title"] for s in specs] == ["World News", "Markets"]
