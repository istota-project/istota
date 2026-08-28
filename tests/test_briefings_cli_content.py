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
