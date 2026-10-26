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


# --- Folder shares (OCM): the federated id and pending offers, read-only ------


def _pending_offer(config, user="alice", **extras):  # noqa: F811
    from istota.credentials import store as secrets_store

    with db.get_db(config.db_path) as conn:
        source_id = sources.create_source(
            conn, user_id=user, kind="ocm_share", state="pending", remote_host="cloud.example.org",
            remote_key="cloud.example.org|5",
            extras={"name": "Taxes 2026", "owner": "carol@https://cloud.example.org",
                    "webdav": "https://cloud.example.org/public.php/webdav/", **extras},
        )
        secrets_store.set_secret(None, user, sources.SECRET_SERVICE, str(source_id),
                                 "synthetic-ocm-secret-99", connection=conn)
        conn.commit()
    return source_id


async def test_no_ocm_section_while_ocm_is_off(signed_client, config):  # noqa: F811
    _pending_offer(config)
    listing = (await signed_client.get(BASE)).json()
    assert listing.get("ocm") is None
    me = await signed_client.get("/istota/api/me")
    assert me.json()["features"]["sharing_ocm"] is False


async def test_the_federated_id_and_pending_offers(signed_client, config):  # noqa: F811
    config.sharing.ocm.enabled = True
    source_id = _pending_offer(config)
    response = await signed_client.get(BASE)
    assert "synthetic-ocm-secret-99" not in response.text
    assert "public.php" not in response.text
    ocm = response.json()["ocm"]
    assert ocm["federated_id"] == "alice@example.com"
    [offer] = ocm["pending"]
    assert offer == {
        "id": source_id, "name": "Taxes 2026", "owner": "carol@https://cloud.example.org",
        "owner_display_name": None, "shared_by": None, "shared_by_display_name": None,
        "host": "cloud.example.org", "reoffer": False, "created_at": offer["created_at"],
    }
    me = await signed_client.get("/istota/api/me")
    assert me.json()["features"]["sharing_ocm"] is True


async def test_an_offer_names_its_sharer_when_it_is_not_the_owner(signed_client, config):  # noqa: F811
    config.sharing.ocm.enabled = True
    _pending_offer(config, owner_display_name="Carol", shared_by="dave@https://cloud.example.org",
                   shared_by_display_name="Dave")
    [offer] = (await signed_client.get(BASE)).json()["ocm"]["pending"]
    assert (offer["owner_display_name"], offer["shared_by"], offer["shared_by_display_name"]) == (
        "Carol", "dave@https://cloud.example.org", "Dave")


async def test_the_files_page_is_offered_wherever_offers_can_arrive(signed_client, config):  # noqa: F811
    """#849: the bell sends a waiting offer to Files, so Files exists with no workspace."""
    config.workspace_path = None
    config.sharing.ocm.enabled = True
    me = (await signed_client.get("/istota/api/me")).json()
    # Files is offered; `workspace` still means a workspace, which other pages read.
    assert (me["features"]["files"], me["features"]["workspace"]) == (True, False)
    response = await signed_client.get("/istota/files/shares", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/istota/files?view=shares"


async def test_another_users_offers_are_not_listed(signed_client, config):  # noqa: F811
    config.sharing.ocm.enabled = True
    _pending_offer(config, user="bob")
    assert (await signed_client.get(BASE)).json()["ocm"]["pending"] == []


# --- Accepting, declining and removing a folder share --------------------------

FOLDERS = f"{BASE}/folders"
OFFERED_VALUE = "synthetic-ocm-secret-99"


@pytest.fixture
def ocm_sender():
    from tests.test_sharing_ocm import Sender

    sender = Sender()
    sender.server.dav_secrets = {OFFERED_VALUE}
    yield sender
    sender.close()


@pytest.fixture
def sandboxed(monkeypatch):
    """The daemon's published verdict, as a stack whose tasks are confined reports it."""
    monkeypatch.setattr("istota.credentials.names.tasks_sandboxed", lambda _config: True)


def _offer_from(config, sender, user="alice"):  # noqa: F811
    from istota.credentials import store as secrets_store

    with db.get_db(config.db_path) as conn:
        source_id = sources.create_source(
            conn, user_id=user, kind="ocm_share", state="pending", remote_host=sender.host,
            remote_key=f"{sender.host}|5",
            extras={"name": "Taxes 2026", "owner": f"carol@http://{sender.host}",
                    "shared_by": f"carol@http://{sender.host}", "provider_id": "5",
                    "webdav": f"http://{sender.host}/public.php/webdav/", "share_path": ""},
        )
        secrets_store.set_secret(None, user, sources.SECRET_SERVICE, str(source_id), OFFERED_VALUE,
                                 connection=conn)
        conn.commit()
    return source_id


def _state(config, source_id, user="alice"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        source = sources.get_source(conn, source_id, user)
    return (source.state, source.access)


async def test_accept_lists_the_share_and_never_returns_the_secret(  # noqa: F811
        signed_client, config, ocm_sender, sandboxed):  # noqa: F811
    config.sharing.ocm.enabled = True
    source_id = _offer_from(config, ocm_sender)
    # The bell's Accept posts to this same path with this same body.
    response = await signed_client.post(f"{FOLDERS}/{source_id}/accept",
                                        json={"allow_scheduled": False}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert OFFERED_VALUE not in response.text
    assert response.json()["share"]["state"] == "active"
    assert response.json()["notified"] is True
    assert _state(config, source_id) == ("active", "write")

    listing = await signed_client.get(BASE)
    assert OFFERED_VALUE not in listing.text and "public.php" not in listing.text
    ocm = listing.json()["ocm"]
    assert ocm["pending"] == []
    [share] = ocm["shares"]
    assert (share["id"], share["access"], share["host"]) == (source_id, "write", ocm_sender.host)
    assert ocm["accept_refusal"] is None


@pytest.mark.parametrize("body", [{}, {"allow_scheduled": "yes"}, {"allow_scheduled": True, "x": 1}],
                         ids=["empty", "not-bool", "extra"])
async def test_accept_needs_exactly_the_checkbox(signed_client, config, ocm_sender, sandboxed,  # noqa: F811
                                                 body):
    config.sharing.ocm.enabled = True
    source_id = _offer_from(config, ocm_sender)
    response = await signed_client.post(f"{FOLDERS}/{source_id}/accept", json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert _state(config, source_id) == ("pending", "read")
    assert ocm_sender.propfinds == []


async def test_a_secret_the_host_refuses_is_a_409(signed_client, config, ocm_sender, sandboxed):  # noqa: F811
    config.sharing.ocm.enabled = True
    ocm_sender.server.dav_secrets = set()
    source_id = _offer_from(config, ocm_sender)
    response = await signed_client.post(f"{FOLDERS}/{source_id}/accept",
                                        json={"allow_scheduled": False}, headers=ORIGIN)
    assert response.status_code == 409
    assert response.json() == {"detail": "This share could not be opened.",
                               "reason": "share_unopened"}
    assert _state(config, source_id) == ("pending", "read")


async def test_an_unsandboxed_multi_user_stack_refuses_and_says_why(  # noqa: F811
        signed_client, config, ocm_sender, monkeypatch):  # noqa: F811
    monkeypatch.setattr("istota.credentials.names.tasks_sandboxed", lambda _config: False)
    config.sharing.ocm.enabled = True
    source_id = _offer_from(config, ocm_sender)
    response = await signed_client.post(f"{FOLDERS}/{source_id}/accept",
                                        json={"allow_scheduled": False}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "isolation"
    assert ocm_sender.propfinds == []
    assert (await signed_client.get(BASE)).json()["ocm"]["accept_refusal"]


async def test_decline_and_remove(signed_client, config, ocm_sender, sandboxed):  # noqa: F811
    config.sharing.ocm.enabled = True
    declined = _offer_from(config, ocm_sender)
    response = await signed_client.post(f"{FOLDERS}/{declined}/decline", headers=ORIGIN)
    assert response.status_code == 200
    assert _state(config, declined)[0] == "declined"

    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM shared_sources")
        conn.commit()
    removed = _offer_from(config, ocm_sender)
    await signed_client.post(f"{FOLDERS}/{removed}/accept", json={"allow_scheduled": False},
                             headers=ORIGIN)
    response = await signed_client.delete(f"{FOLDERS}/{removed}", headers=ORIGIN)
    assert response.status_code == 200
    assert _state(config, removed)[0] == "revoked"
    assert [n["notificationType"] for n in ocm_sender.notifications] == [
        "SHARE_DECLINED", "SHARE_ACCEPTED", "SHARE_DECLINED"]


async def test_another_users_share_is_404(signed_client, config, ocm_sender, sandboxed):  # noqa: F811
    config.sharing.ocm.enabled = True
    source_id = _offer_from(config, ocm_sender, user="bob")
    for method, path, body in (("POST", f"{FOLDERS}/{source_id}/accept", {"allow_scheduled": False}),
                               ("POST", f"{FOLDERS}/{source_id}/decline", None),
                               ("DELETE", f"{FOLDERS}/{source_id}", None),
                               ("DELETE", f"{FOLDERS}/x7", None)):
        response = await signed_client.request(method, path, json=body, headers=ORIGIN)
        assert response.status_code == 404, path
    assert _state(config, source_id, user="bob") == ("pending", "read")
    assert ocm_sender.propfinds == []


async def test_shares_stay_removable_with_ocm_off(signed_client, config, ocm_sender, sandboxed):  # noqa: F811
    config.sharing.ocm.enabled = True
    source_id = _offer_from(config, ocm_sender)
    await signed_client.post(f"{FOLDERS}/{source_id}/accept", json={"allow_scheduled": False},
                             headers=ORIGIN)
    config.sharing.ocm.enabled = False
    ocm = (await signed_client.get(BASE)).json()["ocm"]
    assert (ocm["enabled"], ocm["federated_id"], ocm["pending"]) == (False, None, [])
    assert [s["id"] for s in ocm["shares"]] == [source_id]
    assert (await signed_client.delete(f"{FOLDERS}/{source_id}", headers=ORIGIN)).status_code == 200
