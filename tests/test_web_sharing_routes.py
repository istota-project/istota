"""Settings > Connections "Shared with Istota": the ICS feed routes, over the real store.

A feed address is a bearer secret, so the properties that matter are that no
response carries it, a refusal echoes nothing it was sent, another user's id
reaches nothing, and a failed add writes nothing. The feed is a stdlib server
on loopback, named in `[sharing] private_hosts`.
"""

import pytest

from istota import db
from istota.sharing import sources
from tests.test_sharing_ics import GOOGLE, HTML, _Server
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures
from tests.test_web_mail_accounts import login

BASE = "/istota/api/settings/sharing"
ORIGIN = {"Origin": "https://example.com"}
MARKER = "synthetic-feed-path-41c2"


@pytest.fixture
def feed_server():
    server = _Server()
    server.routes[f"/cal/{MARKER}.ics"] = (200, {}, GOOGLE)
    server.routes["/login.ics"] = (200, {}, HTML)
    yield server
    server.close()


@pytest.fixture
async def signed_client(client, config, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config.sharing.private_hosts = ["127.0.0.1"]
    await login(client, "alice")
    return client


def _counts(config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("shared_sources", "secrets", "shared_feed_cache")
        )


async def test_adding_lists_and_removing_a_feed(signed_client, feed_server, config):  # noqa: F811
    url = feed_server.url(f"/cal/{MARKER}.ics")
    response = await signed_client.post(f"{BASE}/feeds", json={"url": url}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    assert MARKER not in response.text
    feed = response.json()["feed"]
    assert (feed["name"], feed["state"], feed["host"]) == ("Work", "active", f"127.0.0.1:{feed_server.port}")
    assert response.headers["cache-control"] == "no-store"

    listing = await signed_client.get(BASE)
    assert listing.status_code == 200 and MARKER not in listing.text
    assert [f["id"] for f in listing.json()["feeds"]] == [feed["id"]]
    assert listing.json()["can_add"] is True

    again = await signed_client.post(f"{BASE}/feeds", json={"url": url}, headers=ORIGIN)
    assert again.status_code == 409
    assert (again.json()["reason"], again.json()["id"]) == ("already_subscribed", feed["id"])

    removed = await signed_client.delete(f"{BASE}/feeds/{feed['id']}", headers=ORIGIN)
    assert removed.status_code == 200
    assert (await signed_client.get(BASE)).json()["feeds"] == []
    with db.get_db(config.db_path) as conn:
        assert sources.get_source(conn, feed["id"], "alice").state == "revoked"


@pytest.mark.parametrize("body, reason", [
    ({"url": "ftp://x.example/a.ics"}, "invalid_url"),
    ({"url": "https://x.example/a.ics", "extra": 1}, "invalid_body"),
    ({"address": "https://x.example/a.ics"}, "invalid_body"),
    ({"url": 7}, "invalid_body"),
], ids=["scheme", "extra-field", "wrong-field", "not-a-string"])
async def test_a_refusal_echoes_nothing_and_writes_nothing(signed_client, config, body, reason):  # noqa: F811
    before = _counts(config)
    response = await signed_client.post(f"{BASE}/feeds", json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["reason"] == reason
    assert "x.example" not in response.text
    assert _counts(config) == before


async def test_a_page_that_is_not_a_calendar_is_refused(signed_client, feed_server, config):  # noqa: F811
    before = _counts(config)
    response = await signed_client.post(
        f"{BASE}/feeds", json={"url": feed_server.url("/login.ics")}, headers=ORIGIN)
    assert (response.status_code, response.json()["reason"]) == (400, "not_a_calendar")
    assert "login" not in response.text
    assert _counts(config) == before


async def test_another_users_feed_cannot_be_removed(signed_client, feed_server, config):  # noqa: F811
    url = feed_server.url(f"/cal/{MARKER}.ics")
    alices = (await signed_client.post(f"{BASE}/feeds", json={"url": url}, headers=ORIGIN)).json()["feed"]
    await login(signed_client, "bob")
    assert (await signed_client.get(BASE)).json()["feeds"] == []
    for ident in (str(alices["id"]), "99999", "1e3", "../1"):
        response = await signed_client.delete(f"{BASE}/feeds/{ident}", headers=ORIGIN)
        assert response.status_code == 404, ident
    with db.get_db(config.db_path) as conn:
        assert sources.get_source(conn, alices["id"], "alice").state == "active"


async def test_a_write_needs_the_origin_check(signed_client, feed_server):
    response = await signed_client.post(
        f"{BASE}/feeds", json={"url": feed_server.url(f"/cal/{MARKER}.ics")},
        headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert feed_server.received == []


async def test_without_the_master_key_nothing_is_fetched(signed_client, feed_server, monkeypatch):
    monkeypatch.delenv("ISTOTA_SECRET_KEY")
    response = await signed_client.post(
        f"{BASE}/feeds", json={"url": feed_server.url(f"/cal/{MARKER}.ics")}, headers=ORIGIN)
    assert response.status_code == 503
    assert feed_server.received == []


async def test_me_advertises_the_card(signed_client):
    me = await signed_client.get("/istota/api/me")
    assert me.json()["features"]["sharing"] is True


# --- Istota's calendar: the feed link and the delivery choice ----------------

FEED_LINK = f"{BASE}/calendar-feed"


@pytest.fixture
def fresh_feed_limits():
    from istota.sharing import feed_routes

    feed_routes.reset_limits()
    yield
    feed_routes.reset_limits()


async def test_the_listing_describes_istotas_calendar(signed_client):
    calendar = (await signed_client.get(BASE)).json()["calendar"]
    assert calendar["delivery"] == "invite"
    # The test config has no [email], so nothing can be sent and the feed is the route.
    assert (calendar["effective_delivery"], calendar["email_enabled"]) == ("feed", False)
    assert calendar["feed"] is None and calendar["reachable_elsewhere"] is True


async def test_a_feed_link_is_shown_once_and_serves_the_feed(signed_client, fresh_feed_limits):
    made = await signed_client.post(FEED_LINK, headers=ORIGIN)
    assert made.status_code == 201, made.text
    assert made.headers["cache-control"] == "no-store"
    url = made.json()["url"]
    assert url.startswith("https://example.com/istota/calendar/") and url.endswith(".ics")
    token = url.rsplit("/", 1)[1].removesuffix(".ics")

    listing = await signed_client.get(BASE)
    assert token not in listing.text
    assert listing.json()["calendar"]["feed"]["created_at"]

    path = url.removeprefix("https://example.com")
    served = await signed_client.get(path)
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/calendar")

    reset = await signed_client.post(FEED_LINK, headers=ORIGIN)
    assert reset.json()["url"] != url
    assert (await signed_client.get(path)).status_code == 404


async def test_removing_the_link_ends_the_feed(signed_client, fresh_feed_limits):
    url = (await signed_client.post(FEED_LINK, headers=ORIGIN)).json()["url"]
    removed = await signed_client.delete(FEED_LINK, headers=ORIGIN)
    assert removed.status_code == 200
    assert (await signed_client.get(url.removeprefix("https://example.com"))).status_code == 404
    assert (await signed_client.delete(FEED_LINK, headers=ORIGIN)).status_code == 404


async def test_the_feed_link_needs_the_origin_check(signed_client, config):  # noqa: F811
    response = await signed_client.post(FEED_LINK, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM calendar_feed_tokens").fetchone()[0] == 0


async def test_delivery_is_stored(signed_client, config):  # noqa: F811
    from istota import user_profiles

    response = await signed_client.put(f"{BASE}/delivery", json={"delivery": "feed"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert user_profiles.get_profile(config.db_path, "alice").calendar_delivery == "feed"
    assert (await signed_client.get(BASE)).json()["calendar"]["delivery"] == "feed"


@pytest.mark.parametrize("body", [
    {"delivery": "both"}, {"delivery": "feed", "extra": 1}, {"mode": "feed"}, {"delivery": 1},
], ids=["unknown", "extra-field", "wrong-field", "not-a-string"])
async def test_a_bad_delivery_is_refused_and_echoes_nothing(signed_client, config, body):  # noqa: F811
    from istota import user_profiles

    response = await signed_client.put(f"{BASE}/delivery", json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert "both" not in response.text
    profile = user_profiles.get_profile(config.db_path, "alice")
    assert profile is None or profile.calendar_delivery == "invite"


async def test_a_feed_link_is_one_users(signed_client, fresh_feed_limits):
    alices = (await signed_client.post(FEED_LINK, headers=ORIGIN)).json()["url"]
    await login(signed_client, "bob")
    assert (await signed_client.get(BASE)).json()["calendar"]["feed"] is None
    assert (await signed_client.delete(FEED_LINK, headers=ORIGIN)).status_code == 404
    assert (await signed_client.get(alices.removeprefix("https://example.com"))).status_code == 200
