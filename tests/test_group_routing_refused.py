"""A group is never a delivery target (groups spec D8).

`parse_output_target` drops a `group` or `group:<id>` leaf with a WARNING, so
`output_target = "group:household"` in a CRON job is refused at the parser
rather than half-working. Every validator that asks "does this descriptor name
anywhere" (cron sync, the settings pages, `istota user`) reads an empty parse
as a refusal, so the drop reaches them too.
"""

import logging

import pytest

from istota.transport.routing import Destination, parse_output_target


@pytest.mark.parametrize("spec", [
    "group", "group:household", "GROUP:fam", " group : fam ",
])
def test_a_group_leaf_is_dropped_and_warned(spec, caplog):
    with caplog.at_level(logging.WARNING, logger="istota.transport.routing"):
        assert parse_output_target(spec) == []
    assert any("group" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


def test_the_other_leaves_survive(caplog):
    with caplog.at_level(logging.WARNING, logger="istota.transport.routing"):
        plan = parse_output_target("talk,group:fam,email")
    assert plan == [Destination("talk"), Destination("email")]
    assert sum("group" in r.getMessage() for r in caplog.records) == 1


def test_the_warning_names_the_task(caplog):
    with caplog.at_level(logging.WARNING, logger="istota.transport.routing"):
        parse_output_target("group:fam", task_id=77)
    assert any("77" in r.getMessage() for r in caplog.records)


def test_a_surface_merely_starting_with_group_is_not_a_group():
    # Only the surface `group` is refused. A surface named, say, `groupware`
    # is somebody else's (unknown) surface and is the registry's to drop.
    assert parse_output_target("groupware") == [Destination("groupware")]


def test_resolve_delivery_plan_never_routes_to_a_group(tmp_path):
    from istota import db
    from istota.config import Config
    from istota.transport.routing import resolve_delivery_plan

    config = Config(db_path=tmp_path / "istota.db")
    db.init_db(config.db_path)
    task = db.Task(
        id=5, status="running", source_type="cron", user_id="alice",
        prompt="x", output_target="group:fam",
    )
    plan = resolve_delivery_plan(config, task, None)
    assert all(d.surface != "group" for d in plan)
