"""Token-map construction inside ``webhook_receiver.reload_config``.

Verifies the modules-refactor behaviour: the Overland ingest token comes
from the encrypted ``secrets`` table (not from a ``[[resources]]`` block),
and the location-module gate (``is_module_enabled``) decides which users
get scanned.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db, secrets_store
from istota.config import (
    Config,
    LocationReceiverConfig,
    UserConfig,
)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
    p = tmp_path / "istota.db"
    db.init_db(p)
    return p


def _make_config(db_path: Path, users: dict[str, UserConfig]) -> Config:
    # Per-user split: receiver resolves a LocationContext for each
    # ingest user, which requires nextcloud_mount_path.
    return Config(
        db_path=db_path,
        users=users,
        location=LocationReceiverConfig(enabled=True),
        nextcloud_mount_path=db_path.parent,
    )


def test_token_map_built_from_secrets_table(db_path):
    secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
    secrets_store.set_secret(db_path, "bob",   "overland", "ingest_token", "bob-tok")

    cfg = _make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob"),
    })

    from istota import webhook_receiver as wr

    with patch.object(wr, "load_config", return_value=cfg):
        wr.reload_config()

    assert wr._token_map["alice-tok"] == "alice"
    assert wr._token_map["bob-tok"] == "bob"


def test_disabled_module_excluded_from_token_map(db_path):
    secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
    secrets_store.set_secret(db_path, "bob",   "overland", "ingest_token", "bob-tok")

    cfg = _make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob", disabled_modules=["location"]),
    })

    from istota import webhook_receiver as wr

    with patch.object(wr, "load_config", return_value=cfg):
        wr.reload_config()

    assert "alice-tok" in wr._token_map
    assert "bob-tok" not in wr._token_map


def test_user_without_ingest_token_skipped(db_path):
    # Alice has no overland secret stored — she should simply not appear.
    secrets_store.set_secret(db_path, "bob", "overland", "ingest_token", "bob-tok")

    cfg = _make_config(db_path, {
        "alice": UserConfig(display_name="Alice"),
        "bob":   UserConfig(display_name="Bob"),
    })

    from istota import webhook_receiver as wr

    with patch.object(wr, "load_config", return_value=cfg):
        wr.reload_config()

    assert wr._token_map == {"bob-tok": "bob"}
