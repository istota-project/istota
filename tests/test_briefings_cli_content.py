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


# -- source edits and checks ---------------------------------------------------


@pytest.fixture
def app_ctx(ctx, tmp_path):
    from istota import db
    from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
    from istota.config import Config, UserConfig

    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC"), "bob": UserConfig(timezone="UTC")},
    )
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.upsert_shared_block_config(conn, name="world-headlines", cron="0 6 * * *")
        db.shared_kv_set(conn, SHARED_BLOCK_NAMESPACE, "custom-digest", '{"text": "x"}', "root")
        conn.commit()
    ctx.app_config = config
    return ctx


def _add_sources(ctx, block_id, specs):
    with bdb.connect(ctx.db_path) as conn:
        ids = [bdb.add_source(conn, block_id=block_id, kind=k, config=c) for k, c in specs]
        conn.commit()
    return ids


def _source(ctx, source_id):
    with bdb.connect(ctx.db_path) as conn:
        return bdb.get_source(conn, source_id)


def test_sources_set_merge_config_keeps_id_position_and_other_keys(app_ctx):
    bid = _first_block_id(app_ctx)
    _, sid, _ = _add_sources(app_ctx, bid, [
        ("markets", {}),
        ("shared_block", {"name": "world-headlines", "max_age_hours": 6}),
        ("calendar", {}),
    ])
    before = _source(app_ctx, sid)

    result, output = _invoke(app_ctx, [
        "sources", "set", "--id", str(sid), "--merge-config", '{"max_age_hours": 14}',
    ])

    assert result.exit_code == 0, output
    assert output["status"] == "ok"
    assert output["source"]["id"] == sid
    assert output["source"]["position"] == before.position
    assert output["source"]["config"] == {"name": "world-headlines", "max_age_hours": 14}
    assert _source(app_ctx, sid).config == {"name": "world-headlines", "max_age_hours": 14}


def test_sources_set_merge_config_null_deletes_a_key(app_ctx):
    bid = _first_block_id(app_ctx)
    (sid,) = _add_sources(app_ctx, bid, [
        ("shared_block", {"name": "world-headlines", "max_age_hours": 6}),
    ])

    result, _ = _invoke(app_ctx, [
        "sources", "set", "--id", str(sid), "--merge-config", '{"max_age_hours": null}',
    ])

    assert result.exit_code == 0
    assert _source(app_ctx, sid).config == {"name": "world-headlines"}


def test_sources_set_config_replaces_and_is_checked(app_ctx):
    bid = _first_block_id(app_ctx)
    (sid,) = _add_sources(app_ctx, bid, [("shared_block", {"name": "world-headlines"})])

    result, output = _invoke(app_ctx, [
        "sources", "set", "--id", str(sid), "--config", '{"name": "no-such-block"}',
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_source"
    assert output["field"] == "name"
    assert _source(app_ctx, sid).config == {"name": "world-headlines"}


def test_sources_set_toggles_enabled_only(ctx):
    bid = _first_block_id(ctx)
    (sid,) = _add_sources(ctx, bid, [("markets", {"indices": ["DAX"]})])

    result, output = _invoke(ctx, ["sources", "set", "--id", str(sid), "--disabled"])
    assert result.exit_code == 0
    assert output["source"]["enabled"] is False
    assert _source(ctx, sid).config == {"indices": ["DAX"]}

    result, output = _invoke(ctx, ["sources", "set", "--id", str(sid), "--enabled"])
    assert result.exit_code == 0
    stored = _source(ctx, sid)
    assert (stored.enabled, stored.config) == (True, {"indices": ["DAX"]})


@pytest.mark.parametrize("extra", [
    [],
    ["--config", "{}", "--merge-config", "{}"],
    ["--config", "[1]"],
])
def test_sources_set_refuses_bad_flag_sets(ctx, extra):
    bid = _first_block_id(ctx)
    (sid,) = _add_sources(ctx, bid, [("markets", {"indices": ["DAX"]})])

    result, output = _invoke(ctx, ["sources", "set", "--id", str(sid), *extra])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"
    assert _source(ctx, sid).config == {"indices": ["DAX"]}


def test_sources_set_on_a_missing_source_is_not_found(ctx):
    result, output = _invoke(ctx, ["sources", "set", "--id", "9999", "--disabled"])

    assert result.exit_code == 1
    assert output["reason"] == "not_found"


def test_sources_add_disabled_stores_enabled_false(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(ctx, [
        "sources", "add", "--block", str(bid), "--kind", "markets", "--disabled",
    ])

    assert result.exit_code == 0
    assert _source(ctx, output["id"]).enabled is False


def test_sources_add_refuses_a_source_that_reads_nothing(ctx):
    bid = _first_block_id(ctx)
    result, output = _invoke(ctx, [
        "sources", "add", "--block", str(bid), "--kind", "browse", "--config", "{}",
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_source"
    with bdb.connect(ctx.db_path) as conn:
        assert bdb.list_sources(conn, bid) == []


def test_sources_reorder_rewrites_positions(ctx):
    bid = _first_block_id(ctx)
    a, b, c = _add_sources(ctx, bid, [("markets", {}), ("calendar", {}), ("todos", {"path": "x.md"})])

    result, output = _invoke(ctx, [
        "sources", "reorder", "--block", str(bid), "--ids", f"{c},{a},{b}",
    ])

    assert result.exit_code == 0
    assert output == {"status": "ok", "reordered": 3}
    with bdb.connect(ctx.db_path) as conn:
        assert [(s.id, s.position) for s in bdb.list_sources(conn, bid)] == [(c, 0), (a, 1), (b, 2)]


@pytest.mark.parametrize("ids", ["{a},{b}", "{a},{b},{c},9999", "{a},{a},{b}", "x,y"])
def test_sources_reorder_refuses_a_non_permutation(ctx, ids):
    bid = _first_block_id(ctx)
    a, b, c = _add_sources(ctx, bid, [("markets", {}), ("calendar", {}), ("todos", {"path": "x.md"})])

    result, output = _invoke(ctx, [
        "sources", "reorder", "--block", str(bid), "--ids", ids.format(a=a, b=b, c=c),
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"
    with bdb.connect(ctx.db_path) as conn:
        assert [(s.id, s.position) for s in bdb.list_sources(conn, bid)] == [(a, 0), (b, 1), (c, 2)]


def test_sources_reorder_on_a_missing_block_is_not_found(ctx):
    result, output = _invoke(ctx, ["sources", "reorder", "--block", "9999", "--ids", "1"])

    assert result.exit_code == 1
    assert output["reason"] == "not_found"


@pytest.fixture
def bob_account(app_ctx, monkeypatch):
    from istota import db
    from istota.mail import accounts

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    with db.get_db(app_ctx.app_config.db_path) as conn:
        home, _ = accounts.ensure_account(
            conn, "bob", slug="home", label="Home", imap_host="imap.bob.example",
            imap_username="bob@bob.example",
        )
        conn.commit()
    return home.id


@pytest.mark.parametrize("which", ["other_user", "unknown"])
def test_sources_add_my_email_refuses_accounts_the_user_cannot_pick(app_ctx, bob_account, which):
    from istota.briefings.sources.my_email import account_refusal

    account = f"imap:{bob_account}" if which == "other_user" else "imap:999999"
    bid = _first_block_id(app_ctx)
    result, output = _invoke(app_ctx, [
        "sources", "add", "--block", str(bid), "--kind", "my_email",
        "--config", json.dumps({"account": account}),
    ])

    assert result.exit_code == 1
    assert output["reason"] == "mail_account_refused"
    assert output["error"] == account_refusal(app_ctx.app_config, "alice", account)
    with bdb.connect(app_ctx.db_path) as conn:
        assert bdb.list_sources(conn, bid) == []


def test_sources_set_my_email_keeps_a_stale_stored_account(app_ctx, bob_account):
    account = f"imap:{bob_account}"
    bid = _first_block_id(app_ctx)
    (sid,) = _add_sources(app_ctx, bid, [("my_email", {"account": account, "lookback_hours": 24})])

    result, output = _invoke(app_ctx, [
        "sources", "set", "--id", str(sid), "--merge-config", '{"lookback_hours": 48}',
    ])
    assert result.exit_code == 0, output

    result, output = _invoke(app_ctx, [
        "sources", "set", "--id", str(sid), "--merge-config", '{"account": null}',
    ])
    assert result.exit_code == 0, output
    assert _source(app_ctx, sid).config == {"lookback_hours": 48}
