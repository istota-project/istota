import argparse
import json
import logging
import pickle
import socket
import threading

import pytest

from istota import db
from istota.sandbox import credential_shim
from istota.sandbox.skill_proxy import SkillProxy
from istota.skills import _credref
from istota.skills._cli import parse_and_resolve
from tests.support.wallet import NUMBER, request, wallet_fixture  # noqa: F401


def proxy_for(env, tmp_path):
    path, config, _, task_id = env
    config.db_path = path
    return SkillProxy(tmp_path / "proxy.sock", {}, {}, config=config, user_id="alice", task_id=task_id)


def exchange(proxy, payload, *, public=False):
    client, server = socket.socketpair()
    target = proxy._handle_connection if public else proxy._serve_credential_channel
    worker = threading.Thread(target=target, args=(server,))
    worker.start()
    try:
        client.settimeout(3)
        client.sendall(json.dumps(payload).encode() + b"\n")
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()
        worker.join(3)
        server.close()
        assert not worker.is_alive()


def test_private_claim_commits_and_never_logs_values(wallet_env, tmp_path, caplog):
    path, _, _, _ = wallet_env
    with db.get_db(path) as conn:
        purchase = request(conn, wallet_env, extra_hosts=["js.stripe.com"])
    proxy = proxy_for(wallet_env, tmp_path)
    with caplog.at_level(logging.DEBUG):
        reply = exchange(proxy, {"type": "wallet_card", "purchase_id": purchase.purchase_id})
    assert reply["fields"]["number"] == NUMBER
    assert reply["fields"]["exp_month"] == "12"
    assert reply["bound_hosts"] == ["shop.example", "js.stripe.com"]
    with db.get_db(path) as conn:
        row = conn.execute("SELECT state, fill_count FROM wallet_purchases").fetchone()
        assert tuple(row) == ("filled", 1)
    assert proxy._vault_fetches == 0
    assert NUMBER not in caplog.text


def test_public_refuses_before_database_open(wallet_env, tmp_path, monkeypatch):
    proxy = proxy_for(wallet_env, tmp_path)
    monkeypatch.setattr(proxy, "_peer_in_task", lambda peer: True)
    def no_db(*args, **kwargs):
        raise AssertionError("public card request opened DB")
    monkeypatch.setattr(db, "get_db", no_db)
    reply = exchange(proxy, {"type": "wallet_card", "purchase_id": 1, "trusted_skill": True}, public=True)
    assert reply == {"ok": False, "reason": "wallet_channel_unavailable"}


@pytest.mark.parametrize("purchase_id", [None, True, [], "secret input", -1])
def test_invalid_id_is_safe(wallet_env, tmp_path, purchase_id):
    reply = exchange(proxy_for(wallet_env, tmp_path), {"type": "wallet_card", "purchase_id": purchase_id})
    assert reply == {"ok": False, "reason": "purchase_not_found"}


def test_fill_limit_and_other_task_refuse(wallet_env, tmp_path):
    path, _, _, _ = wallet_env
    with db.get_db(path) as conn:
        purchase = request(conn, wallet_env)
    proxy = proxy_for(wallet_env, tmp_path)
    payload = {"type": "wallet_card", "purchase_id": purchase.purchase_id}
    proxy.task_id += 1
    assert exchange(proxy, payload)["reason"] == "purchase_not_found"
    proxy.task_id -= 1
    for _ in range(3):
        assert "fields" in exchange(proxy, payload)
    assert exchange(proxy, payload)["reason"] == "purchase_fill_limit"


def test_fetch_and_parse_box_card_over_real_channel(wallet_env, tmp_path, monkeypatch):
    path, _, _, _ = wallet_env
    with db.get_db(path) as conn:
        purchase = request(conn, wallet_env)
    proxy = proxy_for(wallet_env, tmp_path)
    parser = argparse.ArgumentParser()
    _credref.credential_ref(parser, "--purchase", form=_credref.CARD)
    with proxy._credential_channel(3) as fd:
        monkeypatch.setenv("ISTOTA_CRED_FD", str(fd))
        args = parse_and_resolve(parser, ["--purchase", str(purchase.purchase_id)])
    assert isinstance(args.purchase, _credref.CardSecret)
    assert args.purchase.purchase_id == purchase.purchase_id
    assert args.purchase.fields["number"].reveal() == NUMBER
    assert args.purchase.bound_hosts == ("shop.example",)
    assert NUMBER not in repr(args)
    assert NUMBER not in str(args.purchase.fields)
    with pytest.raises(TypeError):
        pickle.dumps(args.purchase)


def test_card_has_no_public_fallback(monkeypatch, capsys):
    monkeypatch.delenv("ISTOTA_CRED_FD", raising=False)
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", "/never-use-public.sock")
    with pytest.raises(credential_shim.ProxyError, match="wallet_channel_unavailable"):
        credential_shim.fetch_card(1, credential_fd=None)
    parser = argparse.ArgumentParser()
    _credref.credential_ref(parser, "--purchase", form=_credref.CARD)
    with pytest.raises(SystemExit) as exc:
        parse_and_resolve(parser, ["--purchase", "1"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "wallet_channel_unavailable"


def test_unexpected_secret_error_rolls_back_and_redacts(wallet_env, tmp_path, monkeypatch, caplog):
    path, _, _, _ = wallet_env
    with db.get_db(path) as conn:
        purchase = request(conn, wallet_env)
    def broken(*args, **kwargs):
        raise RuntimeError(NUMBER)
    monkeypatch.setattr("istota.wallet.cards.read_card_secrets", broken)
    reply = exchange(proxy_for(wallet_env, tmp_path), {"type": "wallet_card", "purchase_id": purchase.purchase_id})
    assert reply == {"ok": False, "reason": "wallet_error"}
    assert NUMBER not in caplog.text
    assert "RuntimeError" in caplog.text
    with db.get_db(path) as conn:
        assert conn.execute("SELECT fill_count FROM wallet_purchases").fetchone()[0] == 0


def test_live_feature_refusal_reaches_parse_envelope(wallet_env, tmp_path, monkeypatch, capsys):
    path, config, _, _ = wallet_env
    with db.get_db(path) as conn:
        purchase = request(conn, wallet_env)
    proxy = proxy_for(wallet_env, tmp_path)
    config.experimental.features = []
    parser = argparse.ArgumentParser()
    _credref.credential_ref(parser, "--purchase", form=_credref.CARD)
    with proxy._credential_channel(3) as fd:
        monkeypatch.setenv("ISTOTA_CRED_FD", str(fd))
        with pytest.raises(SystemExit) as exc:
            parse_and_resolve(parser, ["--purchase", str(purchase.purchase_id)])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "wallet_unavailable"


@pytest.mark.parametrize("fd", ["not a descriptor", "-1"])
def test_invalid_private_descriptor_never_falls_back(fd, monkeypatch):
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", "/never-use-public.sock")
    with pytest.raises(credential_shim.ProxyError, match="wallet_channel_unavailable"):
        credential_shim.fetch_card(1, credential_fd=fd)


@pytest.mark.parametrize("reply", [{"ok": False, "reason": NUMBER}, {"fields": {"number": NUMBER}}])
def test_malformed_card_reply_never_echoes_data(reply, monkeypatch):
    monkeypatch.setattr(credential_shim, "_request", lambda *a, **kw: reply)
    with pytest.raises(credential_shim.ProxyError, match="^wallet_error$"):
        credential_shim.fetch_card(1, credential_fd="3")
