"""The four notification-inbox routes.

    GET  /istota/api/notifications/count
    GET  /istota/api/notifications?filter=all|action&limit=50
    POST /istota/api/notifications/{id}/dismiss
    POST /istota/api/notifications/seen

Three properties carry the weight here, and each is a defect this suite exists
to catch rather than a restatement of the handler:

**Auth.** All four are `Depends(_require_api_auth)`. A notification body carries
a stranger's subject line and a bot-composed question; an unauthenticated read
of the count alone would still leak how much is waiting on a named user.

**Scoping.** Every query is bound to the *session's* `user_id` and never to a
value from the request. An id belonging to another user gets a 404, not a 403 —
the row's existence is not the other user's business to confirm.

**Derived tab counts.** The panel's two tab labels come from the list response,
not from `/count`: `list_open` returns the post-sweep total, so a label reading
"Needs action (3)" can never sit above a visibly shorter list.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota import notification_sources as sources
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.notification_resolvers import confirmation as confirmation_source

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

pytestmark = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

ORIGIN = {"origin": "https://example.com"}


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


def _db_path():
    import istota.web_app as mod
    return mod._config.db_path


def _held_task(user_id="alice", title="Question", prompt="Shall I proceed?") -> int:
    """A task parked in `pending_confirmation`, with its notification row."""
    with db.get_db(_db_path()) as conn:
        task_id = db.create_task(
            conn, prompt="do the thing", user_id=user_id, source_type="web",
            conversation_token=f"room-{user_id}",
        )
        db.set_task_confirmation(conn, task_id, prompt)
        confirmation_source.write(conn, user_id, task_id=task_id, title=title)
    return task_id


def _bare_row(user_id="alice", *, source="test_source", key="k1",
              title="A thing happened", actionable=False) -> int:
    """A row whose source has no registered resolver.

    Renders from stored text with a `status_note` and a working Dismiss — a row
    nobody can explain is still one the user should be able to clear.
    """
    from istota.notification_store import write_notification
    with db.get_db(_db_path()) as conn:
        result = write_notification(
            conn, user_id, source=source, dedup_key=key,
            title=title, actionable=actionable,
        )
    return result.notification_id


def _row(notification_id: int):
    with db.get_db(_db_path()) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# auth — all four routes
# ---------------------------------------------------------------------------


class TestAuth:
    @pytest.mark.parametrize(("method", "path"), [
        ("GET", "/istota/api/notifications/count"),
        ("GET", "/istota/api/notifications"),
        ("POST", "/istota/api/notifications/1/dismiss"),
        ("POST", "/istota/api/notifications/seen"),
    ])
    async def test_requires_a_session(self, client, method, path):
        resp = await client.request(method, path, headers=ORIGIN, json={"seen": []})
        assert resp.status_code == 401

    async def test_mutations_check_the_origin(self, client):
        """CSRF: the two POSTs carry `_verify_origin` like every other mutation.

        A cross-site form POST to `/seen` would silently resolve a user's
        fire-and-forget rows; one to `/dismiss` would clear a held question.
        """
        cookies = await _login(client, "alice")
        nid = _bare_row()
        for path, body in (
            (f"/istota/api/notifications/{nid}/dismiss", None),
            ("/istota/api/notifications/seen", {"seen": []}),
        ):
            resp = await client.post(
                path, cookies=cookies, json=body,
                headers={"origin": "https://evil.example"},
            )
            assert resp.status_code == 403, path


# ---------------------------------------------------------------------------
# GET /notifications/count
# ---------------------------------------------------------------------------


class TestCount:
    async def test_empty(self, client):
        cookies = await _login(client, "alice")
        resp = await client.get("/istota/api/notifications/count", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == {"open": 0, "actionable": 0}

    async def test_counts_open_and_actionable(self, client):
        cookies = await _login(client, "alice")
        _held_task()                       # actionable
        _bare_row(key="a", actionable=False)
        resp = await client.get("/istota/api/notifications/count", cookies=cookies)
        assert resp.json() == {"open": 2, "actionable": 1}

    async def test_scoped_to_the_session_user(self, client):
        cookies = await _login(client, "bob")
        _held_task(user_id="alice")
        _bare_row(user_id="alice", key="a")
        resp = await client.get("/istota/api/notifications/count", cookies=cookies)
        assert resp.json() == {"open": 0, "actionable": 0}

    async def test_a_dismissed_row_leaves_the_count(self, client):
        cookies = await _login(client, "alice")
        nid = _bare_row()
        await client.post(
            f"/istota/api/notifications/{nid}/dismiss",
            cookies=cookies, headers=ORIGIN,
        )
        resp = await client.get("/istota/api/notifications/count", cookies=cookies)
        assert resp.json()["open"] == 0


# ---------------------------------------------------------------------------
# GET /notifications
# ---------------------------------------------------------------------------


class TestList:
    async def test_empty_payload_shape(self, client):
        cookies = await _login(client, "alice")
        resp = await client.get("/istota/api/notifications", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == {"notifications": [], "total_open": 0}

    async def test_renders_a_held_task_with_its_actions(self, client):
        cookies = await _login(client, "alice")
        task_id = _held_task()
        resp = await client.get("/istota/api/notifications", cookies=cookies)
        body = resp.json()
        assert body["total_open"] == 1
        (item,) = body["notifications"]
        assert item["source"] == "confirmation"
        assert item["actionable"] is True
        assert item["object_type"] == "task"
        assert item["object_id"] == str(task_id)
        # The endpoints are the *existing* producer routes, apiFetch-relative.
        # There is deliberately no generic dispatcher: those handlers already own
        # their authorization, and a second gate would be the weaker one.
        assert [(a["id"], a["endpoint"]) for a in item["actions"]] == [
            ("confirm", f"/chat/tasks/{task_id}/confirm"),
            ("discard", f"/chat/tasks/{task_id}/cancel"),
        ]

    async def test_scoped_to_the_session_user(self, client):
        cookies = await _login(client, "bob")
        _held_task(user_id="alice")
        resp = await client.get("/istota/api/notifications", cookies=cookies)
        assert resp.json() == {"notifications": [], "total_open": 0}

    async def test_defaults_to_all_and_filters_to_action(self, client):
        cookies = await _login(client, "alice")
        _held_task()
        _bare_row(key="a")

        default = await client.get("/istota/api/notifications", cookies=cookies)
        assert len(default.json()["notifications"]) == 2

        action = await client.get(
            "/istota/api/notifications?filter=action", cookies=cookies,
        )
        items = action.json()["notifications"]
        assert [i["source"] for i in items] == ["confirmation"]

    async def test_total_open_is_the_unfiltered_post_sweep_count(self, client):
        """`total_open` counts the open set, not the filtered page.

        The client derives both tab labels from this and the returned rows, which
        is what keeps "Needs action (3)" from sitting above a shorter list.
        """
        cookies = await _login(client, "alice")
        _held_task()
        _bare_row(key="a")
        _bare_row(key="b")
        resp = await client.get(
            "/istota/api/notifications?filter=action", cookies=cookies,
        )
        body = resp.json()
        assert len(body["notifications"]) == 1
        assert body["total_open"] == 3

    async def test_limit_bounds_the_page_but_not_the_total(self, client):
        cookies = await _login(client, "alice")
        for n in range(5):
            _bare_row(key=f"k{n}")
        resp = await client.get(
            "/istota/api/notifications?limit=2", cookies=cookies,
        )
        body = resp.json()
        assert len(body["notifications"]) == 2
        assert body["total_open"] == 5

    async def test_a_junk_limit_does_not_blank_the_panel(self, client):
        """A bad `?limit=` must not read to the user as "nothing is waiting".

        The store coerces before it clamps for exactly this reason; the route
        must not undo that by 422-ing on a typed parameter and returning nothing.
        """
        cookies = await _login(client, "alice")
        _bare_row()
        resp = await client.get(
            "/istota/api/notifications?limit=banana", cookies=cookies,
        )
        assert resp.status_code == 200
        assert len(resp.json()["notifications"]) == 1

    async def test_an_unknown_filter_falls_back_to_all(self, client):
        cookies = await _login(client, "alice")
        _bare_row()
        resp = await client.get(
            "/istota/api/notifications?filter=nonsense", cookies=cookies,
        )
        assert resp.status_code == 200
        assert len(resp.json()["notifications"]) == 1

    async def test_a_row_with_no_resolver_still_renders_with_a_note(self, client):
        cookies = await _login(client, "alice")
        _bare_row(title="Something from an older version")
        (item,) = (await client.get(
            "/istota/api/notifications", cookies=cookies,
        )).json()["notifications"]
        assert item["title"] == "Something from an older version"
        assert item["actions"] == []
        assert item["status_note"]

    async def test_the_payload_carries_no_markup_of_its_own(self, client):
        """Attacker-supplied text ships as data and is rendered as a text node.

        A gated email's subject reaches this payload. The panel never uses
        `{@html}`, and the server never pre-renders markup — so what arrives is
        the string, escaped by nothing and injected as nothing.
        """
        cookies = await _login(client, "alice")
        _bare_row(title="<img src=x onerror=alert(1)> [click](http://evil)")
        (item,) = (await client.get(
            "/istota/api/notifications", cookies=cookies,
        )).json()["notifications"]
        assert item["title"] == "<img src=x onerror=alert(1)> [click](http://evil)"
        assert item["link"] is None


# ---------------------------------------------------------------------------
# POST /notifications/{id}/dismiss
# ---------------------------------------------------------------------------


class TestDismiss:
    async def test_dismisses_the_row(self, client):
        cookies = await _login(client, "alice")
        nid = _bare_row()
        resp = await client.post(
            f"/istota/api/notifications/{nid}/dismiss",
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        row = _row(nid)
        assert row["state"] == "dismissed"
        assert row["resolved_by"] == "web"

    async def test_a_second_dismiss_is_a_200(self, client):
        cookies = await _login(client, "alice")
        nid = _bare_row()
        for _ in range(2):
            resp = await client.post(
                f"/istota/api/notifications/{nid}/dismiss",
                cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 200

    async def test_another_users_row_is_a_404_not_a_403(self, client):
        """404, deliberately. A 403 confirms the row exists.

        The whole cross-user story is that the session's `user_id` scopes every
        query — never a value off the request — and the reply must not leak what
        the scoping refused.
        """
        cookies = await _login(client, "bob")
        nid = _bare_row(user_id="alice")
        resp = await client.post(
            f"/istota/api/notifications/{nid}/dismiss",
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 404
        assert _row(nid)["state"] == "open"

    async def test_an_unknown_id_is_a_404(self, client):
        cookies = await _login(client, "alice")
        resp = await client.post(
            "/istota/api/notifications/999999/dismiss",
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /notifications/seen
# ---------------------------------------------------------------------------


class TestSeen:
    async def test_stamps_seen_at(self, client):
        cookies = await _login(client, "alice")
        nid = _bare_row()
        updated_at = _row(nid)["updated_at"]
        resp = await client.post(
            "/istota/api/notifications/seen",
            json={"seen": [{"id": nid, "updated_at": updated_at}]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        row = _row(nid)
        assert row["seen_at"]
        # Object-backed, so seeing it does not close it — an item you have seen
        # and not acted on still needs you.
        assert row["state"] == "open"

    async def test_another_users_id_is_skipped_without_erroring(self, client):
        """A partial batch is not worth failing a panel open over."""
        cookies = await _login(client, "bob")
        mine = _bare_row(user_id="bob", key="mine")
        theirs = _bare_row(user_id="alice", key="theirs")
        resp = await client.post(
            "/istota/api/notifications/seen",
            json={"seen": [
                {"id": mine, "updated_at": _row(mine)["updated_at"]},
                {"id": theirs, "updated_at": _row(theirs)["updated_at"]},
            ]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert _row(mine)["seen_at"]
        assert _row(theirs)["seen_at"] is None

    async def test_an_empty_batch_is_accepted(self, client):
        cookies = await _login(client, "alice")
        resp = await client.post(
            "/istota/api/notifications/seen", json={"seen": []},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200

    async def test_a_malformed_body_is_a_422(self, client):
        cookies = await _login(client, "alice")
        for body in ({"seen": "everything"}, {"seen": [1, 2, 3]}, ["nope"]):
            resp = await client.post(
                "/istota/api/notifications/seen", json=body,
                cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 422, body

    async def test_a_non_json_body_is_a_400(self, client):
        cookies = await _login(client, "alice")
        resp = await client.post(
            "/istota/api/notifications/seen", content=b"not json",
            cookies=cookies, headers={**ORIGIN, "content-type": "application/json"},
        )
        assert resp.status_code == 400

    def test_the_batch_cap_covers_everything_the_panel_can_render(self):
        """The route's cap is restated, not imported — so pin the tie.

        `list_open` clamps its render limit to `LIVENESS_SCAN_MAX`, so a cap
        below that number would refuse a batch an honest client did render.
        """
        from istota.notification_store import LIVENESS_SCAN_MAX
        from istota.web_app import _SEEN_BATCH_MAX
        assert _SEEN_BATCH_MAX >= LIVENESS_SCAN_MAX

    async def test_the_batch_is_bounded(self, client):
        """A client cannot post an unbounded id list at the write path."""
        cookies = await _login(client, "alice")
        resp = await client.post(
            "/istota/api/notifications/seen",
            json={"seen": [{"id": n, "updated_at": "x"} for n in range(5000)]},
            cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 422
