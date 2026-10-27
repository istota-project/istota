"""Tests for the briefings content-management Click CLI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from istota.briefings import db as bdb
from istota.briefings.cli import cli
from istota.briefings.workspace import synthesize_briefings_context


@pytest.fixture
def ctx(tmp_path):
    context = synthesize_briefings_context(
        "alice", tmp_path, configured_briefing_names=("weekly",),
    )
    context.ensure_dirs()
    bdb.init_db(context.db_path)
    with bdb.connect(context.db_path) as conn:
        bdb.add_block(conn, briefing_name="morning", title="Headlines")
        bdb.add_block(conn, briefing_name="morning", title="Calendar")
        bdb.add_block(conn, briefing_name="evening", title="Markets")
        conn.commit()
    return context


def _invoke(ctx, args):
    runner = CliRunner()
    result = runner.invoke(
        cli, args, obj=ctx, standalone_mode=False, catch_exceptions=False,
    )
    return result, json.loads(result.output)


def test_blocks_list_reports_unknown_briefing_with_available_names(ctx):
    result, output = _invoke(ctx, ["blocks", "list", "--briefing", "Morning"])

    assert result.exit_code == 0
    assert output == {
        "status": "not_found",
        "reason": "not_found",
        "error": "no briefing named 'Morning'",
        "available": ["evening", "morning", "weekly"],
    }


def test_blocks_list_distinguishes_configured_briefing_with_no_blocks(ctx):
    result, output = _invoke(ctx, ["blocks", "list", "--briefing", "weekly"])

    assert result.exit_code == 0
    assert output == {"status": "ok", "blocks": []}


def test_list_orders_case_sensitive_names_deterministically(ctx):
    with bdb.connect(ctx.db_path) as conn:
        bdb.add_block(conn, briefing_name="Evening", title="Headlines")
        conn.commit()

    _, output = _invoke(ctx, ["list"])

    assert [row["name"] for row in output["briefings"]] == [
        "Evening", "evening", "morning", "weekly",
    ]


def test_list_reports_names_block_counts_and_last_generated_timestamp(ctx):
    with bdb.connect(ctx.db_path) as conn:
        bdb.insert_archive(
            conn,
            briefing_name="morning",
            subject="Morning Briefing",
            body_md="News",
            generated_at="2026-08-24T07:00:00+00:00",
        )
        conn.commit()

    result, output = _invoke(ctx, ["list"])

    assert result.exit_code == 0
    assert output == {
        "status": "ok",
        "briefings": [
            {
                "name": "evening",
                "block_count": 1,
                "last_generated_at": None,
            },
            {
                "name": "morning",
                "block_count": 2,
                "last_generated_at": "2026-08-24T07:00:00+00:00",
            },
            {
                "name": "weekly",
                "block_count": 0,
                "last_generated_at": None,
            },
        ],
    }


@pytest.mark.parametrize("options", [[], {"presentation": {"width": "invalid"}}])
def test_invalid_options_refuse_before_cli_title_change(ctx, options):
    with bdb.connect(ctx.db_path) as conn:
        block = bdb.list_blocks(conn, "morning")[0]
    result = CliRunner().invoke(cli, ["blocks", "set", "--id", str(block.id),
                                    "--title", "Changed", "--options", json.dumps(options)], obj=ctx)
    assert result.exit_code != 0
    assert "options" in result.output or "presentation" in result.output
    with bdb.connect(ctx.db_path) as conn:
        assert bdb.get_block(conn, block.id).title == block.title


def test_cli_presentation_preserves_unrelated_options(ctx):
    with bdb.connect(ctx.db_path) as conn:
        bid = bdb.list_blocks(conn, "morning")[0].id
    options = {"tone": "neutral", "custom": 42, "presentation": {"width": "full"}}
    _, response = _invoke(ctx, ["blocks", "set", "--id", str(bid), "--options", json.dumps(options)])
    assert response["block"]["options"] == options


# -- one error shape, block writes through save_block_definition --------------


def _first_block_id(ctx):
    with bdb.connect(ctx.db_path) as conn:
        return bdb.list_blocks(conn, "morning")[0].id


@pytest.mark.parametrize("args", [
    ["blocks", "remove", "--id", "9999"],
    ["sources", "remove", "--id", "9999"],
    ["sources", "list", "--block", "9999"],
])
def test_missing_ids_answer_not_found(ctx, args):
    result, output = _invoke(ctx, args)

    assert result.exit_code == 1
    assert output["status"] == "error"
    assert output["reason"] == "not_found"


def test_blocks_remove_of_a_real_block_still_removes_it(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(ctx, ["blocks", "remove", "--id", str(bid)])

    assert result.exit_code == 0
    assert output == {"status": "ok"}
    with bdb.connect(ctx.db_path) as conn:
        assert bdb.get_block(conn, bid) is None


def test_blocks_set_refuses_agent_render_mode_and_keeps_the_row(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(
        ctx, ["blocks", "set", "--id", str(bid), "--render-mode", "agent"],
    )

    assert result.exit_code == 1
    assert output["reason"] == "invalid_definition"
    assert "render_mode" in output["error"]
    with bdb.connect(ctx.db_path) as conn:
        assert bdb.get_block(conn, bid).render_mode == "synthesis"


def test_blocks_set_on_a_missing_block_is_not_found(ctx):
    result, output = _invoke(ctx, ["blocks", "set", "--id", "9999", "--title", "X"])

    assert result.exit_code == 1
    assert output["reason"] == "not_found"


def test_blocks_add_stores_render_mode_and_options(ctx):
    options = {"presentation": {"width": "wide"}}
    result, output = _invoke(ctx, [
        "blocks", "add", "--briefing", "morning", "--title", "Quotes",
        "--render-mode", "structured", "--options", json.dumps(options),
    ])

    assert result.exit_code == 0
    with bdb.connect(ctx.db_path) as conn:
        stored = bdb.get_block(conn, output["block"]["id"])
    assert stored.render_mode == "structured"
    assert stored.options == options


def test_blocks_add_refuses_an_unknown_render_mode(ctx):
    with bdb.connect(ctx.db_path) as conn:
        before = len(bdb.list_blocks(conn, "morning"))
    result, output = _invoke(ctx, [
        "blocks", "add", "--briefing", "morning", "--title", "Quotes",
        "--render-mode", "banana",
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_definition"
    with bdb.connect(ctx.db_path) as conn:
        assert len(bdb.list_blocks(conn, "morning")) == before


def test_bad_options_json_is_invalid_argument(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(ctx, ["blocks", "set", "--id", str(bid), "--options", "{nope"])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_merge_options_keeps_stored_keys_and_merges_presentation(ctx):
    bid = _first_block_id(ctx)
    _invoke(ctx, ["blocks", "set", "--id", str(bid), "--options", json.dumps(
        {"tone": "neutral", "presentation": {"width": "full"}},
    )])

    result, output = _invoke(ctx, [
        "blocks", "set", "--id", str(bid), "--merge-options",
        json.dumps({"story_count": 3}),
    ])
    assert result.exit_code == 0
    assert output["block"]["options"] == {
        "tone": "neutral", "story_count": 3, "presentation": {"width": "full"},
    }

    result, output = _invoke(ctx, [
        "blocks", "set", "--id", str(bid), "--merge-options",
        json.dumps({"presentation": {"width": "wide"}}),
    ])
    assert result.exit_code == 0
    assert output["block"]["options"] == {
        "tone": "neutral", "story_count": 3, "presentation": {"width": "wide"},
    }


def test_options_and_merge_options_are_exclusive(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(ctx, [
        "blocks", "set", "--id", str(bid), "--options", "{}", "--merge-options", "{}",
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_reorder_with_a_bad_id_list_is_invalid_argument(ctx):
    result, output = _invoke(ctx, ["blocks", "reorder", "--briefing", "morning", "--ids", "a,b"])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_sources_add_unknown_kind_is_invalid_argument(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(ctx, ["sources", "add", "--block", str(bid), "--kind", "bogus"])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"
