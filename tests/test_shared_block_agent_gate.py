"""An agent shared block's task passes the research gate and reads like a cron job's.

The task is `shared_block_research` under `__briefings__`: the same gate as group
research, the shared-block timezone on its clock, and an empty answer that
stays empty so the prior block is kept.
"""

from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from istota import db, executor
from istota.briefings import group_store
from istota.config import Config, UserConfig
from istota.rooms.scopes import is_briefing_research
from istota.session.result import _compose_full_result, _NO_FINAL_ANSWER_NOTICE
from tests.test_group_briefing_store import NOW, define, enqueue

SHARED_TZ = "America/Los_Angeles"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "effective_sandboxing", lambda cfg: True)
    mount = tmp_path / "mount"
    mount.mkdir()
    cfg = Config(
        db_path=tmp_path / "app.db",
        temp_dir=tmp_path / "temp",
        workspace_path=mount,
        users={name: UserConfig(timezone="Asia/Tokyo") for name in ("alice", "bob", "carol", "admin")},
    )
    cfg.browser.enabled = True
    cfg.security.network.enabled = True
    cfg.security.skill_proxy_enabled = True
    cfg.briefings.shared_block_timezone = SHARED_TZ
    db.init_db(cfg.db_path)
    return cfg


def _agent_task(make_task, **overrides):
    fields = dict(user_id="__briefings__", source_type="shared_block_research",
                  prompt="Read the front pages and list the day's stories.",
                  briefing_name="shared_block:world:20261015T070000Z")
    fields.update(overrides)
    return make_task(**fields)


def test_the_gate_admits_an_agent_block_task_with_browse(config, make_task):
    success, text, *_ = executor.execute_task(_agent_task(make_task), config, [], dry_run=True)
    assert success, text
    assert f"User timezone: {SHARED_TZ}" in text
    assert "istota-skill browse" in text
    assert "published verbatim as a shared briefing block" in text


@pytest.mark.parametrize("overrides, error", [
    ({"user_id": "alice"}, "briefing_research_identity_refused"),
    ({"conversation_token": "room-token"}, "briefing_research_identity_refused"),
    ({"attachments": ["/tmp/a.png"]}, "briefing_research_identity_refused"),
])
def test_the_gate_refuses_an_agent_task_outside_the_research_shape(config, make_task, monkeypatch, overrides, error):
    brain = Mock(side_effect=AssertionError("must refuse before invoking a model"))
    monkeypatch.setattr(executor, "make_brain", brain)
    task = _agent_task(make_task, **overrides)
    assert is_briefing_research(task)
    success, text, *_ = executor.execute_task(task, config, [])
    assert (success, text) == (False, error)
    brain.assert_not_called()


@pytest.mark.parametrize("capability", ["browser", "network", "skill_proxy"])
def test_the_gate_refuses_an_agent_task_without_its_capabilities(config, make_task, monkeypatch, capability):
    monkeypatch.setattr(executor, "make_brain", Mock(side_effect=AssertionError("refused first")))
    if capability == "browser":
        config.browser.enabled = False
    elif capability == "network":
        config.security.network.enabled = False
    else:
        config.security.skill_proxy_enabled = False
    success, text, *_ = executor.execute_task(_agent_task(make_task), config, [])
    assert (success, text) == (False, f"briefing_research_{capability}_unavailable")


def test_an_agent_task_reads_the_shared_block_clock(config, make_task):
    tz, name = executor._resolve_user_tz(config, "__briefings__", task=_agent_task(make_task))
    assert (tz, name) == (ZoneInfo(SHARED_TZ), SHARED_TZ)


def test_group_research_reads_its_definitions_clock(config, make_task):
    config.email.enabled = True
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
        db.add_group_member(conn, "team", "alice", role="owner", added_by="alice")
    with db.get_db(config.db_path) as conn:
        definition = define(conn, config, generation={"version": 1, "prompt": "Write", "research": {
            "enabled": True, "prompt": "Research"}})
        run = enqueue(conn, config, definition)
        claim = group_store.claim_run(conn, run_id=run["id"], now=NOW)
    task = make_task(user_id="__briefings__", source_type="group_briefing_research",
                     briefing_name=f"{run['id']}:{claim['attempt_token']}")
    assert definition["timezone"] == "Europe/London"
    assert executor._resolve_user_tz(config, "__briefings__", task=task)[1] == "Europe/London"


def test_group_research_with_an_unreadable_run_falls_back_to_the_shared_block_clock(config, make_task):
    task = make_task(user_id="__briefings__", source_type="group_briefing_research", briefing_name="999:gone")
    assert executor._resolve_user_tz(config, "__briefings__", task=task)[1] == SHARED_TZ


def test_a_users_own_task_keeps_the_profile_clock(config, make_task):
    task = make_task(user_id="alice", source_type="scheduled")
    assert executor._resolve_user_tz(config, "alice", task=task)[1] == "Asia/Tokyo"
    assert executor._resolve_user_tz(config, "__briefings__")[1] == "UTC"


def test_an_empty_agent_answer_stays_empty(make_task):
    trace = [{"type": "text", "text": "Fetching the first front page."},
             {"type": "tool", "text": "istota-skill browse render https://example.com"}]
    assert _compose_full_result("", trace, _agent_task(make_task)) == ""
    control = make_task(user_id="alice", source_type="talk")
    assert _compose_full_result("", trace, control).startswith(_NO_FINAL_ANSWER_NOTICE)
