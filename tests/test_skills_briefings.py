"""The `briefings` skill facade: what the model receives from the Click tree."""

import pytest

from istota import db
from istota.briefings import db as bdb
from istota.briefings import resolve_for_user
from istota.config import Config, UserConfig


@pytest.fixture()
def config(tmp_path, monkeypatch):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )
    db.init_db(cfg.db_path)
    monkeypatch.setenv("BRIEFINGS_USER", "alice")
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    return cfg


def _run(args):
    import istota.skills.briefings as sb

    return sb._run(args)


@pytest.mark.parametrize("args", [
    ["blocks", "remove", "--id", "9999"],
    ["sources", "remove", "--id", "9999"],
    ["sources", "list", "--block", "9999"],
])
def test_a_cli_refusal_arrives_as_the_envelope_dict(config, args):
    result = _run(args)

    assert result["status"] == "error"
    assert result["reason"] == "not_found"
    assert "{" not in result["error"]


def test_the_facade_exits_one_on_the_envelope(config, capsys):
    import istota.skills.briefings as sb

    with pytest.raises(SystemExit) as exc:
        sb.main(["blocks", "remove", "--id", "9999"])
    assert exc.value.code == 1


def test_blocks_set_agent_is_refused_through_the_facade(config):
    added = _run(["blocks", "add", "--briefing", "M", "--title", "News"])
    bid = added["block"]["id"]

    result = _run(["blocks", "set", "--id", str(bid), "--render-mode", "agent"])

    assert result["reason"] == "invalid_definition"
    ctx = resolve_for_user("alice", config)
    with bdb.connect(ctx.db_path) as conn:
        assert bdb.get_block(conn, bid).render_mode == "synthesis"


def test_the_context_carries_the_app_config(config, monkeypatch):
    from istota.briefings import cli as bcli

    seen = {}
    real_ctx = bcli._ctx

    def capture(click_ctx):
        bctx = real_ctx(click_ctx)
        seen["app_config"] = bctx.app_config
        return bctx

    monkeypatch.setattr(bcli, "_ctx", capture)
    _run(["list"])

    assert seen["app_config"] is config
