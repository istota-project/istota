"""The self profile PUT may not claim another user's address or raise its caps.

`find_user_by_email` returns the first user holding an address, so a user who
added another's address could have that person's mail routed to their own
tasks. The refusal must not say who holds the address: a user must not learn
who else is on the deployment from a settings form.
"""

from unittest.mock import AsyncMock

import pytest

from istota import user_profiles
from tests.test_web_app import (  # noqa: F401 -- shared web fixtures
    _needs_web_deps,
    _patch_app,
    app,
    client,
    config,
)

ORIGIN = {"origin": "https://example.com"}
URL = "/istota/api/settings/profile"

pytestmark = _needs_web_deps


async def _login(client, username):  # noqa: F811
    import istota.webui.app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"access_token": "stub"},
    )
    mod._nc_oauth2_userinfo = AsyncMock(
        return_value={"id": username, "displayname": username.title()},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
def seeded(config):  # noqa: F811
    _patch_app(config)
    for user_id in ("alice", "bob"):
        user_profiles.ensure_profile(config.db_path, user_id)
    user_profiles.update_profile(
        config.db_path, "bob", email_addresses=["bob@example.com"],
    )
    return config


async def test_another_users_address_is_refused_without_naming_them(
    client, seeded,  # noqa: F811
):
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL, json={"email_addresses": ["alice@example.com", "BOB@example.com"]},
        cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "another user" in detail
    assert "bob" not in detail.lower()
    assert user_profiles.get_profile(seeded.db_path, "alice").email_addresses == []


async def test_another_users_login_email_is_refused(client, seeded):  # noqa: F811
    from istota.webui import auth as web_auth

    web_auth.upsert_identity(seeded.db_path, "bob", "bob-login@example.com")
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL, json={"email_addresses": ["bob-login@example.com"]},
        cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 409


async def test_a_free_address_is_saved(client, seeded):  # noqa: F811
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL, json={"email_addresses": ["alice@example.com"]},
        cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 200
    assert user_profiles.get_profile(seeded.db_path, "alice").email_addresses == [
        "alice@example.com",
    ]


async def test_an_already_stored_duplicate_resubmitted_passes(
    client, seeded,  # noqa: F811
):
    user_profiles.update_profile(
        seeded.db_path, "alice", email_addresses=["bob@example.com"],
    )
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL,
        json={"email_addresses": ["bob@example.com", "alice@example.com"]},
        cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "field", ["max_foreground_workers", "max_background_workers"],
)
async def test_worker_caps_are_not_self_editable(client, seeded, field):  # noqa: F811
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL, json={field: 99}, cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == f"unknown field: {field}"
    profile = user_profiles.get_profile(seeded.db_path, "alice")
    assert getattr(profile, field) == 0


async def test_the_profile_get_still_reports_worker_caps(client, seeded):  # noqa: F811
    # Read-only on the user's page: the value is the admin's to set.
    user_profiles.update_profile(seeded.db_path, "alice", max_foreground_workers=3)
    cookies = await _login(client, "alice")
    resp = await client.get(URL, cookies=cookies)
    assert resp.json()["profile"]["max_foreground_workers"] == 3


def _mark_managed(db_path, user_id, *fields):
    from istota import db

    with db.get_db(db_path) as conn:
        user_profiles.set_managed_fields(conn, user_id, fields)


async def test_a_managed_field_changed_is_refused_and_named(
    client, seeded,  # noqa: F811
):
    """A field the deploy re-writes on every converge would be reverted
    silently, so the edit is refused instead, and nothing in the body lands."""
    user_profiles.update_profile(
        seeded.db_path, "alice", email_addresses=["alice@example.com"],
    )
    _mark_managed(seeded.db_path, "alice", "email_addresses")
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL,
        json={"email_addresses": ["other@example.com"], "display_name": "New"},
        cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "managed_by_provisioning"
    assert body["fields"] == ["email_addresses"]
    assert "next deploy" in body["detail"]
    profile = user_profiles.get_profile(seeded.db_path, "alice")
    assert profile.email_addresses == ["alice@example.com"]
    assert profile.display_name != "New"


async def test_a_managed_field_resubmitted_unchanged_passes(
    client, seeded,  # noqa: F811
):
    user_profiles.update_profile(
        seeded.db_path, "alice", email_addresses=["alice@example.com"],
    )
    _mark_managed(seeded.db_path, "alice", "email_addresses")
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL,
        json={"email_addresses": ["alice@example.com"], "display_name": "New"},
        cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 200
    assert user_profiles.get_profile(seeded.db_path, "alice").display_name == "New"


async def test_another_users_managed_field_does_not_lock_mine(
    client, seeded,  # noqa: F811
):
    _mark_managed(seeded.db_path, "bob", "display_name")
    cookies = await _login(client, "alice")
    resp = await client.put(
        URL, json={"display_name": "New"}, cookies=cookies, headers=ORIGIN,
    )
    assert resp.status_code == 200


async def test_the_profile_get_reports_the_managed_set(client, seeded):  # noqa: F811
    _mark_managed(seeded.db_path, "alice", "timezone", "email_addresses")
    cookies = await _login(client, "alice")
    resp = await client.get(URL, cookies=cookies)
    assert resp.json()["profile"]["managed"] == ["email_addresses", "timezone"]
