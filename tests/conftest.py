"""Shared test fixtures for istota tests."""

import os
import sqlite3
from pathlib import Path

import pytest


def _load_dotenv():
    """Load .env file from project root into os.environ (simple key=value parser)."""
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            os.environ.setdefault(key, value)


_load_dotenv()

from istota import db
from istota.config import Config, UserConfig


@pytest.fixture
def db_path(tmp_path):
    """Initialize a real SQLite database using schema.sql and return its path."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def db_conn(db_path):
    """Yield a database connection with row factory set."""
    with db.get_db(db_path) as conn:
        yield conn


@pytest.fixture
def make_task():
    """Factory fixture that creates Task dataclass instances with defaults."""
    def _make_task(**overrides):
        defaults = {
            "id": 1,
            "prompt": "test prompt",
            "user_id": "testuser",
            "source_type": "cli",
            "status": "pending",
        }
        defaults.update(overrides)
        return db.Task(**defaults)
    return _make_task


@pytest.fixture
def make_config(tmp_path):
    """Factory fixture that creates Config instances with tmp paths."""
    def _make_config(**overrides):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        index_file = skills_dir / "_index.toml"
        if not index_file.exists():
            index_file.write_text("")

        mount_path = tmp_path / "mount"
        mount_path.mkdir(exist_ok=True)

        defaults = {
            "db_path": tmp_path / "test.db",
            "temp_dir": tmp_path / "temp",
            "skills_dir": skills_dir,
            "nextcloud_mount_path": mount_path,
        }
        defaults.update(overrides)
        return Config(**defaults)
    return _make_config


@pytest.fixture
def make_user_config():
    """Factory fixture that creates UserConfig instances with defaults."""
    def _make_user_config(**overrides):
        defaults = {
            "display_name": "Test User",
            "email_addresses": [],
            "timezone": "UTC",
            "briefings": [],
        }
        defaults.update(overrides)
        return UserConfig(**defaults)
    return _make_user_config
