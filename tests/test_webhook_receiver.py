"""Token-map construction inside ``webhook_receiver.reload_config``.

The map is keyed by the SHA-256 of a token and holds ``(user_id, row_id)``:
per-device rows from ``location_ingest_tokens`` beside the legacy shared
``overland`` / ``ingest_token`` secret, whose row id is ``None``. The
location-module gate and the disabled-account check decide which users are
scanned at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.credentials import store as secrets_store
from istota.config import (
    Config,
    LocationReceiverConfig,
    UserConfig,
)
from istota.location import ingest_tokens
from istota.location.ingest_tokens import hash_token
from istota.webui import auth as web_auth

DEVICE = "6F1C2A3B-4D5E-4F60-8A7B-9C0D1E2F3A4B"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


@pytest.fixture(autouse=True)
def _fresh_receiver(monkeypatch):
    from istota.webui import webhook_receiver as wr

    for name, value in (
        ("_config", None), ("_sms_providers", None), ("_token_map", {}),
        ("_user_contexts", {}), ("_places_cache", {}), ("_sentinel_stamp", 0.0),
    ):
        monkeypatch.setattr(wr, name, value, raising=False)


def _make_config(db_path: Path, users: dict[str, UserConfig]) -> Config:
    # Per-user split: receiver resolves a LocationContext for each
    # ingest user, which requires workspace_path.
    return Config(
        db_path=db_path,
        users=users,
        location=LocationReceiverConfig(enabled=True),
        workspace_path=db_path.parent,
    )


def _mint(db_path, user_id, kind="code", device_id=None):
    with db.get_db(db_path) as conn:
        return ingest_tokens.mint(conn, user_id, kind, device_id, "label")


def _reload(cfg):
    from istota.webui import webhook_receiver as wr

    with patch.object(wr, "load_config", return_value=cfg):
        wr.reload_config()
    return wr


def test_legacy_secret_is_mapped_by_its_hash(db_path):
    secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
    secrets_store.set_secret(db_path, "bob",   "overland", "ingest_token", "bob-tok")

    wr = _reload(_make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob"),
    }))

    assert wr._token_map[hash_token("alice-tok")] == ("alice", None)
    assert wr._token_map[hash_token("bob-tok")] == ("bob", None)
    assert "alice-tok" not in wr._token_map


def test_row_tokens_are_mapped_beside_the_legacy_secret(db_path):
    secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
    app_id, app_tok = _mint(db_path, "alice", "app", DEVICE)
    code_id, code_tok = _mint(db_path, "alice")

    wr = _reload(_make_config(db_path, {"alice": UserConfig(display_name="Alice")}))

    assert wr._token_map == {
        hash_token("alice-tok"): ("alice", None),
        hash_token(app_tok): ("alice", app_id),
        hash_token(code_tok): ("alice", code_id),
    }
    assert "alice" in wr._user_contexts


def test_a_user_with_only_row_tokens_is_admitted(db_path):
    row_id, tok = _mint(db_path, "alice")

    wr = _reload(_make_config(db_path, {"alice": UserConfig(display_name="Alice")}))

    assert wr._token_map == {hash_token(tok): ("alice", row_id)}
    assert "alice" in wr._user_contexts


def test_disabled_module_excluded_from_token_map(db_path):
    secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
    secrets_store.set_secret(db_path, "bob",   "overland", "ingest_token", "bob-tok")
    _, bob_row_tok = _mint(db_path, "bob")

    wr = _reload(_make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob", disabled_modules=["location"]),
    }))

    assert hash_token("alice-tok") in wr._token_map
    assert hash_token("bob-tok") not in wr._token_map
    assert hash_token(bob_row_tok) not in wr._token_map
    assert "bob" not in wr._user_contexts


def test_disabled_account_excluded_from_token_map(db_path):
    secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
    secrets_store.set_secret(db_path, "bob",   "overland", "ingest_token", "bob-tok")
    _, bob_row_tok = _mint(db_path, "bob")
    web_auth.upsert_identity(db_path, "bob", "bob@example.com", create_profile=True)
    web_auth.set_disabled(db_path, "bob", True)

    wr = _reload(_make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob"),
    }))

    assert hash_token("alice-tok") in wr._token_map
    assert hash_token("bob-tok") not in wr._token_map
    assert hash_token(bob_row_tok) not in wr._token_map
    assert "bob" not in wr._user_contexts


def test_user_without_any_token_skipped(db_path):
    secrets_store.set_secret(db_path, "bob", "overland", "ingest_token", "bob-tok")

    wr = _reload(_make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob"),
    }))

    assert wr._token_map == {hash_token("bob-tok"): ("bob", None)}
    assert "alice" not in wr._user_contexts


def test_a_failed_row_read_keeps_the_previous_map(db_path):
    row_id, tok = _mint(db_path, "alice")
    cfg = _make_config(db_path, {"alice": UserConfig(display_name="Alice")})
    wr = _reload(cfg)
    before = dict(wr._token_map)
    assert before == {hash_token(tok): ("alice", row_id)}

    def locked(conn, user_ids):
        raise sqlite3.OperationalError("database is locked")

    with patch.object(ingest_tokens, "all_token_hashes", locked):
        _reload(cfg)

    assert wr._token_map == before
    assert "alice" in wr._user_contexts


def test_a_kept_map_hands_the_signal_back_for_a_retry(db_path):
    """A removal whose reload hit a locked database applies on the next request."""
    from istota.location import ingest_signal

    row_id, tok = _mint(db_path, "alice")
    cfg = _make_config(db_path, {"alice": UserConfig(display_name="Alice")})
    wr = _reload(cfg)
    with db.get_db(db_path) as conn:
        assert ingest_tokens.delete_token(conn, "alice", row_id)
    ingest_signal.signal_reload(db_path)

    def locked(conn, user_ids):
        raise sqlite3.OperationalError("database is locked")

    with patch.object(wr, "load_config", return_value=cfg):
        with patch.object(ingest_tokens, "all_token_hashes", locked):
            wr._maybe_reload_for_signal()
        assert hash_token(tok) in wr._token_map
        wr._maybe_reload_for_signal()

    assert hash_token(tok) not in wr._token_map
