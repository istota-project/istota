"""Push preferences round-trip through the shared self-profile API."""

import pytest

from istota import user_profiles
from tests.test_web_notifications import (  # noqa: F401
    ORIGIN, _db_path, _login, client,
)

URL = "/istota/api/settings/profile"


@pytest.mark.parametrize("setting", ["alerts", "badge", "off"])
async def test_push_preferences_round_trip(client, setting):  # noqa: F811
    cookies = await _login(client, "alice")
    response = await client.put(URL, json={"push_chat_alerts": setting, "push_preview": False}, cookies=cookies, headers=ORIGIN)
    assert response.status_code == 200
    profile = (await client.get(URL, cookies=cookies)).json()["profile"]
    assert profile["push_chat_alerts"] == setting
    assert profile["push_preview"] is False
    stored = user_profiles.get_profile(_db_path(), "alice")
    assert stored.push_chat_alerts == setting and stored.push_preview is False


@pytest.mark.parametrize("body", [{"push_chat_alerts": "no"}, {"push_preview": "false"}, {"unknown": True}])
async def test_invalid_preference_is_rejected(client, body):  # noqa: F811
    cookies = await _login(client, "alice")
    assert (await client.put(URL, json=body, cookies=cookies, headers=ORIGIN)).status_code == 400



def test_preferences_seed_and_merge_through_user_config(tmp_path):
    from istota import db
    from istota.config import UserConfig

    path = tmp_path / "test.db"
    db.init_db(path)
    configured = UserConfig(push_chat_alerts="badge", push_preview=False)
    profile = user_profiles.ensure_profile(path, "alice", seed_from=configured)
    assert profile.push_chat_alerts == "badge" and profile.push_preview is False
    configured.push_chat_alerts = "alerts"
    configured.push_preview = True
    user_profiles.merge_into_user_config(profile, configured)
    assert configured.push_chat_alerts == "badge" and configured.push_preview is False
