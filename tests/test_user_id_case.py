from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from istota import cli, db, user_profiles
from istota.config import UserConfig, _merge_profile_rows, load_config
from istota.mail.ownership import exact_recipient_users
from istota.transport.talk.inbound import _istota_members_for_conversation, _talk_author
from istota.webui import auth
from tests.test_cli_user_ensure import _FakeArgs
from tests.test_web_app import _make_config


@pytest.fixture
def config(tmp_path):
    return _make_config(tmp_path, users={"Alice": UserConfig(display_name="Alice")})


def test_lookup_preserves_spelling_and_tracks_user_changes(config):
    assert config.resolve_user_id("ALICE") == "Alice"
    assert config.get_user("alice") is config.users["Alice"]
    config.users["Bob"] = UserConfig()
    assert config.resolve_user_id("bob") == "Bob"
    config.users = {"Carol": UserConfig()}
    assert config.resolve_user_id("alice") is None
    assert config.resolve_user_id("carol") == "Carol"


@pytest.mark.parametrize("from_db", [False, True])
def test_config_refuses_case_collision(tmp_path, from_db):
    path = tmp_path / "config.toml"
    database = tmp_path / "test.db"
    db.init_db(database)
    text = f'db_path = "{database}"\n[users.Alice]\n'
    if from_db:
        user_profiles.ensure_profile(database, "alice")
    else:
        text += "[users.alice]\n"
    path.write_text(text)
    with pytest.raises(ValueError, match="differs only in case"):
        load_config(path)


def test_profile_overlay_refuses_before_changing_snapshot(config):
    rows = {"Alice": user_profiles.UserProfile(user_id="Alice", display_name="Changed"),
            "alice": user_profiles.UserProfile(user_id="alice")}
    with pytest.raises(ValueError, match="differs only in case"):
        _merge_profile_rows(config, rows)
    assert config.users["Alice"].display_name == "Alice"


@pytest.mark.parametrize("writer", ["ensure", "status", "upsert", "identity"])
def test_profile_writers_refuse_case_collision(config, writer):
    user_profiles.ensure_profile(config.db_path, "Alice")
    with pytest.raises(ValueError, match="differs only in case"):
        if writer == "ensure":
            user_profiles.ensure_profile(config.db_path, "alice")
        elif writer == "status":
            user_profiles.ensure_profile_with_status(config.db_path, "alice")
        elif writer == "upsert":
            user_profiles.upsert_profile(config.db_path, user_profiles.UserProfile(user_id="alice"))
        else:
            auth.upsert_identity(config.db_path, "alice", "alice@example.com", create_profile=True,
                                 reject_case_collision=True)
    assert list(user_profiles.list_profiles(config.db_path)) == ["Alice"]


@pytest.mark.parametrize("from_db", [False, True])
def test_cli_ensure_refuses_collision(config, monkeypatch, from_db):
    if from_db:
        user_profiles.ensure_profile(config.db_path, "Alice")
        config.users = {}
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_user_ensure(_FakeArgs(name="alice"))
    assert exc.value.code == 1
    assert user_profiles.get_profile(config.db_path, "alice") is None


def test_talk_resolves_author_and_members_to_stored_id(config):
    author = _talk_author("users", "alice", "Alice", config)
    assert author.user_id == "Alice"
    assert author.surface_ref == "alice"
    assert _talk_author("guests", "alice", "Alice", config).user_id is None
    assert _istota_members_for_conversation({"type": 1, "name": "alice"}, [], config) == ["Alice"]
    assert _istota_members_for_conversation({"type": 2}, [
        {"actorType": "users", "actorId": "alice"},
        {"actorType": "users", "actorId": "ALICE"},
    ], config) == ["Alice"]


def test_plus_address_uses_stored_id(config):
    config.email.bot_email = "bot@example.com"
    mail = SimpleNamespace(to=["bot+ALICE@example.com"], cc=["bot+alice@example.com"])
    assert exact_recipient_users(config, mail) == ["Alice"]


def test_admins_are_case_insensitive(config, monkeypatch):
    import istota.webui.app as app
    config.admin_users = {"aLiCe"}
    monkeypatch.setattr(app, "_config", config)
    assert config.is_admin("Alice")
    assert config.is_shared_kv_writer("Alice")
    assert app._user_is_web_admin("Alice")
    assert not config.is_admin("Bob")


@pytest.mark.parametrize("seeded", [False, True])
async def test_oauth_resolves_session_and_seed_to_stored_id(config, monkeypatch, seeded):
    import istota.webui.app as app
    if seeded:
        user_profiles.ensure_profile(config.db_path, "Alice")
    monkeypatch.setattr(app, "_config", config)
    monkeypatch.setattr(app.app.state, "istota_config", config, raising=False)
    oauth = MagicMock()
    oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    monkeypatch.setattr(app, "_oauth", oauth)
    async with AsyncClient(transport=ASGITransport(app=app.app), base_url="https://example.com") as client:
        response = await client.get("/istota/callback")
        assert response.status_code == 302
        me = await client.get("/istota/api/me")
        assert me.json()["username"] == "Alice"
    assert list(user_profiles.list_profiles(config.db_path)) == ["Alice"]


def test_repl_resolves_user(config, monkeypatch):
    from istota import repl
    run = MagicMock()
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(repl, "run_session", run)
    cli.cmd_repl(SimpleNamespace(config=None, user="alice", token=None, workspace=None,
                                 model=None, effort=None))
    assert run.call_args.kwargs["user_id"] == "Alice"


def test_task_persists_stored_user_id(config, monkeypatch):
    from tests.test_cli_task_dry_run import _Args
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    cli.cmd_task(_Args(user="alice"))
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT user_id FROM tasks").fetchone()[0] == "Alice"


def test_concurrent_profile_creation_refuses_one_spelling(config):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def create(user_id):
        barrier.wait(timeout=5)
        try:
            user_profiles.ensure_profile(config.db_path, user_id)
            return True
        except ValueError as exc:
            assert "differs only in case" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, ["Alice", "alice"])) == [False, True]
    assert len(user_profiles.list_profiles(config.db_path)) == 1


def test_last_admin_protection_uses_case_insensitive_membership(config):
    user_profiles.ensure_profile(config.db_path, "Alice")
    auth.upsert_identity(config.db_path, "Alice", "alice@example.com")
    with pytest.raises(ValueError, match="last enabled admin"):
        auth.set_disabled(config.db_path, "Alice", True, protected_admins={"alice"})


async def test_oauth_collision_during_seed_refuses_session(config, monkeypatch):
    import istota.webui.app as app

    monkeypatch.setattr(app, "_config", config)
    monkeypatch.setattr(app.app.state, "istota_config", config, raising=False)
    oauth = MagicMock()
    oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    monkeypatch.setattr(app, "_oauth", oauth)

    def refuse(*args, **kwargs):
        raise ValueError("User ID differs only in case")

    monkeypatch.setattr(user_profiles, "ensure_profile_with_status", refuse)
    async with AsyncClient(transport=ASGITransport(app=app.app), base_url="https://example.com") as client:
        response = await client.get("/istota/callback")
        assert response.status_code == 403
        assert (await client.get("/istota/api/me")).status_code == 401
