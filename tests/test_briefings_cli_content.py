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


# -- block authoring in one call -----------------------------------------------


#: The body `tests/test_briefings_routes.py::TestAtomicBlockWrites` sends to
#: `PUT /blocks`, so the route and `blocks save --file` are driven with one payload.
ROUTE_BLOCK_PAYLOAD = {
    "briefing_name": "M", "title": "News", "directive": "Concise",
    "render_mode": "structured", "options": {"custom": 42, "presentation": {"width": "full"}},
    "sources": [{"kind": "notes", "config": {"path": "shared/notes.md"}, "enabled": False},
                {"kind": "calendar", "config": {"days": 3}, "enabled": True}],
}


@pytest.fixture
def deferred(tmp_path, monkeypatch):
    """A task's deferred dir as the only host root, the way a sandboxed task has it."""
    directory = tmp_path / "deferred"
    directory.mkdir()
    for name in ("ISTOTA_WORKSPACE_PATH", "NEXTCLOUD_MOUNT_PATH", "ISTOTA_CONVERSATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(directory))
    return directory


def _route_client(ctx):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from istota.briefings.routes import get_user_context, require_auth, router, verify_origin

    app = FastAPI()
    app.include_router(router, prefix="/istota/api/briefings")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    app.dependency_overrides[verify_origin] = lambda: None
    return TestClient(app)


def _row_without_ids(block) -> dict:
    return {
        "briefing_name": block.briefing_name, "position": block.position,
        "title": block.title, "directive": block.directive,
        "render_mode": block.render_mode, "options": block.options,
        "sources": [
            {"position": s.position, "kind": s.kind, "config": s.config, "enabled": s.enabled}
            for s in block.sources
        ],
    }


def _blocks(ctx, name):
    with bdb.connect(ctx.db_path) as conn:
        return bdb.list_blocks(conn, name)


def test_blocks_add_with_sources_creates_block_and_sources_in_one_call(ctx):
    sources = [{"kind": "calendar", "config": {}}, {"kind": "notes", "config": {"path": "n.md"}, "enabled": False}]

    result, output = _invoke(ctx, [
        "blocks", "add", "--briefing", "noon", "--title", "Day",
        "--sources", json.dumps(sources),
    ])

    assert result.exit_code == 0, output
    (block,) = _blocks(ctx, "noon")
    assert [(s.kind, s.config, s.enabled) for s in block.sources] == [
        ("calendar", {}, True), ("notes", {"path": "n.md"}, False),
    ]
    assert output["block"]["id"] == block.id


@pytest.mark.parametrize("sources, reason", [
    ('[{"kind": "calendar"}, {"kind": "browse", "config": {}}]', "invalid_source"),
    ('[{"kind": "banana"}]', "invalid_definition"),
    ("[]", "invalid_definition"),
    ('{"kind": "calendar"}', "invalid_argument"),
    ("[", "invalid_argument"),
])
def test_blocks_add_with_a_bad_source_list_leaves_no_block(ctx, sources, reason):
    result, output = _invoke(ctx, [
        "blocks", "add", "--briefing", "noon", "--title", "Day", "--sources", sources,
    ])

    assert result.exit_code == 1
    assert output["reason"] == reason, output
    assert _blocks(ctx, "noon") == []


def test_blocks_save_file_matches_the_route_for_the_same_payload(ctx, deferred, tmp_path):
    web_ctx = synthesize_briefings_context("alice", tmp_path / "web")
    web_ctx.ensure_dirs()
    bdb.init_db(web_ctx.db_path)
    response = _route_client(web_ctx).put("/istota/api/briefings/blocks", json=ROUTE_BLOCK_PAYLOAD)
    assert response.status_code == 200, response.text
    body = deferred / "block.json"
    body.write_text(json.dumps(ROUTE_BLOCK_PAYLOAD))

    result, output = _invoke(ctx, ["blocks", "save", "--file", str(body)])

    assert result.exit_code == 0, output
    (from_route,) = _blocks(web_ctx, "M")
    (from_cli,) = _blocks(ctx, "M")
    assert _row_without_ids(from_cli) == _row_without_ids(from_route)


def test_blocks_save_file_updates_in_place_and_keeps_an_unchanged_source(ctx, deferred):
    bid = _first_block_id(ctx)
    # A web template source with an empty config: not re-judged unless it changes.
    (sid,) = _add_sources(ctx, bid, [("browse", {})])
    body = deferred / "block.json"
    body.write_text(json.dumps({
        "id": bid, "title": "Renamed",
        "sources": [{"id": sid, "kind": "browse", "config": {}}, {"kind": "calendar"}],
    }))

    result, output = _invoke(ctx, ["blocks", "save", "--file", str(body)])

    assert result.exit_code == 0, output
    assert output["block"]["id"] == bid
    assert output["block"]["title"] == "Renamed"
    assert [(s["id"], s["kind"]) for s in output["block"]["sources"]][0] == (sid, "browse")
    assert [s["kind"] for s in output["block"]["sources"]] == ["browse", "calendar"]


def test_blocks_save_file_checks_a_changed_source(ctx, deferred):
    bid = _first_block_id(ctx)
    (sid,) = _add_sources(ctx, bid, [("browse", {"preset": "bbc"})])
    body = deferred / "block.json"
    body.write_text(json.dumps({"id": bid, "sources": [{"id": sid, "kind": "browse", "config": {}}]}))

    result, output = _invoke(ctx, ["blocks", "save", "--file", str(body)])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_source"
    assert _source(ctx, sid).config == {"preset": "bbc"}


@pytest.mark.parametrize("payload, reason", [
    ({"reorder": {"briefing_name": "morning", "ordered_ids": [1, 2]}}, "invalid_argument"),
    ([1, 2], "invalid_argument"),
    ({"id": 9999, "title": "x"}, "not_found"),
    ({"briefing_name": "M", "title": "x", "render_mode": "agent"}, "invalid_definition"),
])
def test_blocks_save_file_refusals(ctx, deferred, payload, reason):
    body = deferred / "block.json"
    body.write_text(json.dumps(payload))

    result, output = _invoke(ctx, ["blocks", "save", "--file", str(body)])

    assert result.exit_code == 1
    assert output["reason"] == reason, output
    assert _blocks(ctx, "M") == []


def test_blocks_save_file_with_a_foreign_source_id_is_invalid_definition(ctx, deferred):
    first, second = (b.id for b in _blocks(ctx, "morning"))
    (sid,) = _add_sources(ctx, second, [("calendar", {})])
    body = deferred / "block.json"
    body.write_text(json.dumps({"id": first, "sources": [{"id": sid, "kind": "calendar"}]}))

    result, output = _invoke(ctx, ["blocks", "save", "--file", str(body)])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_definition"


def test_blocks_save_file_that_is_not_json_is_invalid_argument(ctx, deferred):
    body = deferred / "block.json"
    body.write_text("{not json")

    result, output = _invoke(ctx, ["blocks", "save", "--file", str(body)])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_directive_file_sets_the_directive_on_add_and_set(ctx, deferred):
    text = deferred / "directive.md"
    text.write_text("Lead with the outage.\n")

    result, output = _invoke(ctx, [
        "blocks", "add", "--briefing", "noon", "--title", "Day", "--directive-file", str(text),
    ])
    assert result.exit_code == 0, output
    assert output["block"]["directive"] == "Lead with the outage.\n"

    text.write_text("Shorter.")
    result, output = _invoke(ctx, [
        "blocks", "set", "--id", str(output["block"]["id"]), "--directive-file", str(text),
    ])
    assert result.exit_code == 0, output
    assert output["block"]["directive"] == "Shorter."


def test_an_empty_directive_file_is_invalid_argument(ctx, deferred):
    bid = _first_block_id(ctx)
    text = deferred / "directive.md"
    text.write_text("")

    result, output = _invoke(ctx, ["blocks", "set", "--id", str(bid), "--directive-file", str(text)])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_directive_and_directive_file_are_exclusive(ctx, deferred):
    bid = _first_block_id(ctx)
    text = deferred / "directive.md"
    text.write_text("x")

    result, output = _invoke(ctx, [
        "blocks", "set", "--id", str(bid), "--directive", "y", "--directive-file", str(text),
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_a_directive_file_that_is_not_a_file_is_invalid_argument(ctx, deferred):
    bid = _first_block_id(ctx)

    result, output = _invoke(ctx, [
        "blocks", "set", "--id", str(bid), "--directive-file", str(deferred),
    ])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_kinds_equals_the_config_route_lists(ctx):
    from istota.briefings.models import RENDER_MODES

    route = _route_client(ctx).get("/istota/api/briefings/config").json()

    result, output = _invoke(ctx, ["kinds"])

    assert result.exit_code == 0
    assert output == {
        "status": "ok",
        "source_kinds": route["source_kinds"],
        "structured_kinds": route["structured_kinds"],
        "render_modes": list(RENDER_MODES),
        "block_templates": route["block_templates"],
    }


# -- options: the editor's pickers, one answer per kind -----------------------


@pytest.fixture
def picker_ctx(app_ctx, tmp_path, monkeypatch):
    from istota import feeds
    from istota.feeds import db as feeds_db
    from istota.feeds.workspace import synthesize_feeds_context

    user_root = app_ctx.app_config.workspace_path / "Users" / "alice"
    (user_root / "shared").mkdir(parents=True, exist_ok=True)
    (user_root / "shared" / "team-todo.md").write_text("- [ ] x\n")
    (user_root / "notes.bin").write_text("ignore me")

    fctx = synthesize_feeds_context("alice", tmp_path / "feeds-ws", db_path=tmp_path / "feeds.db")
    feeds_db.init_db(fctx.db_path)
    with feeds_db.connect(fctx.db_path) as conn:
        category = feeds_db.upsert_category(conn, "news", "News")
        feeds_db.upsert_feed(
            conn, url="https://example.com/feed.xml", title="Example", site_url=None,
            source_type="rss", category_id=category, poll_interval_minutes=60,
        )
        conn.commit()
    monkeypatch.setattr(feeds, "resolve_for_user", lambda user_id, cfg: fctx)
    return app_ctx


def _picker_route(ctx):
    client = _route_client(ctx)
    client.app.state.istota_config = ctx.app_config
    return client


@pytest.mark.parametrize("kind, route, key", [
    ("rss", "/istota/api/briefings/feed-options", None),
    ("browse", "/istota/api/briefings/browse-presets", "presets"),
    ("my_email", "/istota/api/briefings/email-accounts", "accounts"),
    ("shared_block", "/istota/api/briefings/shared-block-options", "options"),
    ("notes", "/istota/api/briefings/path-suggest", "paths"),
])
def test_options_match_the_picker_route(picker_ctx, kind, route, key):
    body = _picker_route(picker_ctx).get(route).json()
    expected = body if key is None else body[key]

    result, output = _invoke(picker_ctx, ["options", "--kind", kind])

    assert result.exit_code == 0
    assert output == {"status": "ok", "kind": kind, "options": expected}
    assert expected, f"the {kind} picker answered nothing, so parity says nothing"


def test_options_rss_lists_the_users_feeds(picker_ctx):
    _, output = _invoke(picker_ctx, ["options", "--kind", "rss"])

    assert output["options"]["available"] is True
    assert [s["label"] for s in output["options"]["subscriptions"]] == ["Example"]
    assert [c["label"] for c in output["options"]["categories"]] == ["News"]


def test_options_path_query_filters_and_checks_like_the_routes(picker_ctx):
    client = _picker_route(picker_ctx)
    suggested = client.get("/istota/api/briefings/path-suggest", params={"q": "shared/team-todo.md"}).json()
    checked = client.get("/istota/api/briefings/path-check", params={"path": "shared/team-todo.md"}).json()

    result, output = _invoke(picker_ctx, ["options", "--kind", "todos", "--q", "shared/team-todo.md"])

    assert result.exit_code == 0
    assert output["options"] == suggested["paths"] == ["shared/team-todo.md"]
    assert output["check"] == checked == {"ok": True, "resolved": "Users/alice/shared/team-todo.md"}


def test_options_path_check_refuses_a_path_outside_the_users_folder(picker_ctx):
    (picker_ctx.app_config.workspace_path / "secret.md").write_text("nope")

    _, output = _invoke(picker_ctx, ["options", "--kind", "notes", "--q", "../secret.md"])

    assert output["options"] == []
    assert output["check"]["ok"] is False


def test_options_for_a_kind_with_no_picker(picker_ctx):
    result, output = _invoke(picker_ctx, ["options", "--kind", "markets"])

    assert result.exit_code == 0
    assert output == {"status": "ok", "kind": "markets", "options": None, "note": "no options for this kind"}


def test_options_unknown_kind_is_invalid_argument(picker_ctx):
    result, output = _invoke(picker_ctx, ["options", "--kind", "banana"])

    assert result.exit_code == 1
    assert output["reason"] == "invalid_argument"


def test_options_without_the_istota_config_is_unavailable(ctx):
    result, output = _invoke(ctx, ["options", "--kind", "rss"])

    assert result.exit_code == 1
    assert output["reason"] == "unavailable"
