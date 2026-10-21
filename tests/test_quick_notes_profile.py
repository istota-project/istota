"""The quick notes folder preference, as the self profile PUT validates it."""

import os

import pytest

from istota import user_profiles
from tests.test_settings_profile_uniqueness import ORIGIN, URL, _login
from tests.test_web_app import (  # noqa: F401 -- shared web fixtures
    _needs_web_deps,
    _patch_app,
    app,
    client,
    config,
)

pytestmark = _needs_web_deps


@pytest.fixture
def home(config):  # noqa: F811
    _patch_app(config)
    user_profiles.ensure_profile(config.db_path, "alice")
    home = config.workspace_path / "Users/alice"
    home.mkdir(parents=True)
    return home


def _stored(config):  # noqa: F811
    return user_profiles.get_profile(config.db_path, "alice").quick_notes_folder


@pytest.mark.parametrize("value,stored", [
    ("Notes/Inbox", "Notes/Inbox"),
    ("/Notes//Inbox/", "Notes/Inbox"),
    ("Notes/Inbox/Quick ideas", "Notes/Inbox/Quick ideas"),
])
async def test_a_nested_path_is_stored_without_slashes(client, config, home, value, stored):  # noqa: F811
    (home / "Notes").mkdir()
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"quick_notes_folder": value}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 200
    assert _stored(config) == stored
    response = await client.get("/istota/api/settings/profile", cookies=cookies)
    assert response.json()["profile"]["quick_notes_folder"] == stored
    # Accepting a folder that does not exist yet creates nothing.
    assert not (home / "Notes/Inbox").exists()


async def test_an_existing_folder_is_accepted(client, config, home):  # noqa: F811
    (home / "Notes/Inbox").mkdir(parents=True)
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"quick_notes_folder": "Notes/Inbox"}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 200
    assert _stored(config) == "Notes/Inbox"


async def test_an_empty_string_clears_the_folder(client, config, home):  # noqa: F811
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="Notes/Inbox")
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"quick_notes_folder": ""}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 200
    assert _stored(config) is None
    response = await client.get("/istota/api/settings/profile", cookies=cookies)
    assert response.json()["profile"]["quick_notes_folder"] is None


@pytest.mark.parametrize("value", [
    "../bob",
    "Notes/../../bob",
    "Notes/./Inbox",
    "Notes/bad\0name",
    "/".join(["a"] * 9),
    "x" * 513,
    5,
])
async def test_an_invalid_path_is_a_422(client, config, home, value):  # noqa: F811
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="Notes/Inbox")
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"quick_notes_folder": value}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_folder"
    assert response.json()["error"]
    assert _stored(config) == "Notes/Inbox"


async def test_a_path_through_a_symlink_is_a_422(client, config, home, tmp_path):  # noqa: F811
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Inbox").mkdir(parents=True)
    os.symlink(elsewhere, home / "Notes")
    cookies = await _login(client, "alice")
    for value in ("Notes", "Notes/Inbox", "Notes/Missing"):
        response = await client.put(URL, json={"quick_notes_folder": value}, cookies=cookies, headers=ORIGIN)
        assert response.status_code == 422, value
        assert response.json()["code"] == "invalid_folder"
    assert _stored(config) is None


async def test_a_path_through_a_file_is_a_422(client, config, home):  # noqa: F811
    (home / "Notes").write_text("not a folder")
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"quick_notes_folder": "Notes/Inbox"}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_folder"


def test_the_settings_plan_snapshot_writes_unset_as_empty():
    from istota.settings_plan.profiles import snapshot_profile

    unset = snapshot_profile(user_profiles.UserProfile("alice"))
    assert unset["profile"]["quick_notes_folder"] == ""
    chosen = snapshot_profile(user_profiles.UserProfile("alice", quick_notes_folder="Notes/Inbox"))
    assert chosen["profile"]["quick_notes_folder"] == "Notes/Inbox"


def test_the_store_normalises_and_clears(config):  # noqa: F811
    user_profiles.ensure_profile(config.db_path, "alice")
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="/Notes//Inbox/")
    assert _stored(config) == "Notes/Inbox"
    user_profiles.update_profile(config.db_path, "alice", quick_notes_folder="")
    assert _stored(config) is None


async def test_the_folder_check_runs_off_the_event_loop(client, config, home, monkeypatch):  # noqa: F811
    import threading

    from istota.workspace import quick_notes

    loop_thread = threading.get_ident()
    seen = []
    real_open_dir = quick_notes.open_dir

    def recording_open_dir(*args, **kwargs):
        seen.append(threading.get_ident())
        return real_open_dir(*args, **kwargs)

    monkeypatch.setattr(quick_notes, "open_dir", recording_open_dir)
    (home / "Notes").mkdir()
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"quick_notes_folder": "Notes"}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 200
    assert seen and loop_thread not in seen
