"""Settings > Assignments' backend, over the real store and the real lifecycle.

What matters: only someone the skill is open to reaches a route, only their own
assignments answer, every write needs an origin, each action lands the transition the
CLI's verb lands, and nothing here can write a charter.
"""

from unittest.mock import AsyncMock

import pytest

from istota import db
from istota.assignments import store
from istota.rooms import egress
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

BASE = "/istota/api/assignments"
ORIGIN = {"Origin": "https://example.com"}
BUDGET = {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 20, "per_wake_turns": 60}
CANARY = "canary-body-value"


async def login(http, user):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user})
    response = await http.get("/istota/callback", follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text


@pytest.fixture
async def admin(client, config):  # noqa: F811
    config.admin_users = {"alice"}
    await login(client, "alice")
    return client


@pytest.fixture
async def member(client, config):  # noqa: F811
    config.admin_users = {"bob"}
    await login(client, "alice")
    return client


def _seed(config, state="sleeping", *, user="alice", slug="heat-pump", **fields):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        assignment_id = store.insert_assignment(
            conn, user_id=user, slug=slug, title="Heat pump quotes", goal="Three quotes.",
            autonomy="May email installers.", budget=BUDGET, dir_relpath=f"assignments/{slug}",
            **fields,
        )
        if state != "draft":
            store.set_state(
                conn, assignment_id, state,
                reason="user" if state == "paused" else None,
                waiting_on=["completion"] if state == "waiting" else None,
            )
    return assignment_id


def _get(config, assignment_id):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return store.get_assignment(conn, assignment_id)


def _inbox_kinds(config, assignment_id):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return [event.kind for event in store.list_inbox(conn, assignment_id)]


def _every_write(assignment_id):
    base = f"{BASE}/{assignment_id}"
    return [
        (f"{base}/pause", None),
        (f"{base}/resume", None),
        (f"{base}/wake", None),
        (f"{base}/abandon", {"reason": "no"}),
        (f"{base}/topup", {"usd": 1}),
        (f"{base}/note", {"text": "hi"}),
        (f"{base}/close", {"completed": True}),
        (f"{base}/items/1/confirm", None),
        (f"{base}/handoffs/1/done", {"report": "done"}),
    ]


class TestTheGate:
    async def test_a_non_admin_gets_a_404_on_every_route(self, member, config):  # noqa: F811
        assignment_id = _seed(config)
        assert (await member.get(BASE)).status_code == 404
        assert (await member.get(f"{BASE}/{assignment_id}")).status_code == 404
        for path, body in _every_write(assignment_id):
            response = await member.post(path, json=body, headers=ORIGIN)
            assert response.status_code == 404, path
        assert _get(config, assignment_id).state == "sleeping"

    async def test_a_skill_the_user_disabled_closes_the_routes(self, admin, config):  # noqa: F811
        from istota.config import UserConfig

        config.users["alice"] = UserConfig(disabled_skills=["assignment"])
        assert (await admin.get(BASE)).status_code == 404
        me = await admin.get("/istota/api/me")
        assert me.json()["features"]["assignments"] is False

    async def test_me_says_whether_the_section_shows(self, admin, config):  # noqa: F811
        assert (await admin.get("/istota/api/me")).json()["features"]["assignments"] is True
        config.admin_users = {"bob"}
        assert (await admin.get("/istota/api/me")).json()["features"]["assignments"] is False

    async def test_no_session_is_refused(self, client, config):  # noqa: F811
        assignment_id = _seed(config)
        assert (await client.get(BASE)).status_code == 401
        response = await client.post(f"{BASE}/{assignment_id}/pause", headers=ORIGIN)
        assert response.status_code == 401

    async def test_every_write_needs_an_origin(self, admin, config):  # noqa: F811
        assignment_id = _seed(config)
        for path, body in _every_write(assignment_id):
            response = await admin.post(
                path, json=body, headers={"Origin": "https://elsewhere.example.org"},
            )
            assert response.status_code == 403, path
        assert _get(config, assignment_id).state == "sleeping"

    async def test_another_users_id_reads_as_a_missing_one(self, admin, config):  # noqa: F811
        bobs = _seed(config, user="bob", slug="bobs")
        theirs = await admin.get(f"{BASE}/{bobs}")
        missing = await admin.get(f"{BASE}/999999")
        assert theirs.status_code == missing.status_code == 404
        assert theirs.json() == missing.json()
        for path in (f"{BASE}/x1", f"{BASE}/{'9' * 40}"):
            assert (await admin.get(path)).json() == missing.json()
        for path, body in _every_write(bobs):
            response = await admin.post(path, json=body, headers=ORIGIN)
            assert response.status_code == 404, path
            assert response.json() == missing.json()
        assert _get(config, bobs).state == "sleeping"

    async def test_no_route_can_write_a_charter(self, app, admin, config):  # noqa: F811
        assignment_id = _seed(config)
        writes = {
            route.path.rsplit("/", 1)[-1] for route in app.routes
            if getattr(route, "path", "").startswith(BASE) and "GET" not in route.methods
        }
        assert writes == {
            "pause", "resume", "wake", "abandon", "topup", "note", "close", "confirm", "done",
        }
        for path, body in (
            (BASE, {"title": "New"}),
            (f"{BASE}/{assignment_id}/activate", {}),
            (f"{BASE}/{assignment_id}/set", {"goal": "Other"}),
        ):
            response = await admin.post(path, json=body, headers=ORIGIN)
            assert response.status_code in (404, 405), path
        response = await admin.put(f"{BASE}/{assignment_id}", json={"goal": "Other"}, headers=ORIGIN)
        assert response.status_code == 405
        assert _get(config, assignment_id).goal == "Three quotes."


class TestReading:
    async def test_the_list_has_terminal_states_last(self, admin, config):  # noqa: F811
        done = _seed(config, "completed", slug="done")
        running = _seed(config, "sleeping", slug="running")
        draft = _seed(config, "draft", slug="draft")
        _seed(config, user="bob", slug="bobs")
        with db.get_db(config.db_path) as conn:
            store.insert_action(conn, running, text="Call Acme", report_spec="the date")

        response = await admin.get(BASE)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        rows = response.json()["assignments"]
        assert [row["id"] for row in rows] == [running, draft, done]
        assert rows[0]["open_handoffs"] == 1
        assert rows[1]["open_handoffs"] == 0
        assert rows[0]["ledger"]["remaining"] == {"cost_usd": 20.0, "wakes": 40}
        assert rows[0]["budget"] == BUDGET

    async def test_the_detail_is_the_board_and_what_the_page_needs(self, admin, config):  # noqa: F811
        assignment_id = _seed(config)
        with db.get_db(config.db_path) as conn:
            store.insert_item(conn, assignment_id, text="Signed", kind="confirmed", validator=None)
            store.insert_frozen(conn, assignment_id, name="area", value="the house")
            store.insert_action(conn, assignment_id, text="Call Acme", report_spec="the date")
            for n in range(5):
                wake = store.insert_wake(conn, assignment_id, fired_by="time")
                store.update_wake(conn, wake, ended_at=db.iso_utc_now(), outcome="next", summary=f"w{n}")

        response = await admin.get(f"{BASE}/{assignment_id}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["assignment"]["id"] == assignment_id
        assert data["goal"] == "Three quotes."
        assert data["autonomy"] == "May email installers."
        assert data["items"][0]["text"] == "Signed"
        assert data["frozen"][0]["name"] == "area"
        assert data["open_handoffs"][0]["text"] == "Call Acme"
        assert [wake["summary"] for wake in data["wakes"]] == ["w4", "w3", "w2", "w1", "w0"]
        assert "data" in data["notice"]
        assert data["completion_evidence"] is None
        assert data["network"]["subject"] == f"assignment:{assignment_id}"
        assert data["network"]["job"] is None
        assert isinstance(data["external_skills"], list)
        assert data["chat_href"].startswith("/chat")
        assert data["folder_href"] == f"/files/assignments/{assignment_id}"

    async def test_a_terminal_assignment_has_no_network_card(self, admin, config):  # noqa: F811
        assignment_id = _seed(config, "abandoned")
        assert (await admin.get(f"{BASE}/{assignment_id}")).json()["network"] is None

    async def test_a_proposed_completion_carries_its_evidence(self, admin, config):  # noqa: F811
        assignment_id = _seed(config, "waiting")
        with db.get_db(config.db_path) as conn:
            wake = store.insert_wake(conn, assignment_id, fired_by="time")
            store.update_wake(conn, wake, ended_at=db.iso_utc_now(), complete_request="Three PDFs.")
        data = (await admin.get(f"{BASE}/{assignment_id}")).json()
        assert data["completion_evidence"] == "Three PDFs."

    async def test_evidence_is_gone_once_it_was_answered(self, admin, config):  # noqa: F811
        assignment_id = _seed(config, "sleeping")
        with db.get_db(config.db_path) as conn:
            wake = store.insert_wake(conn, assignment_id, fired_by="time")
            store.update_wake(conn, wake, ended_at=db.iso_utc_now(), complete_request="Three PDFs.")
        data = (await admin.get(f"{BASE}/{assignment_id}")).json()
        assert data["completion_evidence"] is None

    async def test_the_network_view_shows_a_grant(self, admin, config):  # noqa: F811
        a = _seed(config)
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", f"assignment:{a}", "example.com", decided_via="web")
        data = (await admin.get(f"{BASE}/{a}")).json()
        assert "example.com" in str(data["network"])


class TestActions:
    async def _post(self, http, path, body=None):
        return await http.post(f"{BASE}/{path}", json=body, headers=ORIGIN)

    async def test_pause_then_resume_then_wake(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/pause")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["assignment"]["state"] == "paused"
        assert _get(config, a).state_reason == "user"

        response = await self._post(admin, f"{a}/resume")
        assert response.json()["assignment"]["state"] == "sleeping"
        assert response.json()["wake_due"] is not None

        with db.get_db(config.db_path) as conn:
            store.update_assignment(conn, a, next_wake_at=None)
        response = await self._post(admin, f"{a}/wake")
        assert response.status_code == 200, response.text
        assert _get(config, a).next_wake_at is not None

    async def test_abandon_records_its_reason(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/abandon", {"reason": "Bought one."})
        assert response.status_code == 200, response.text
        found = _get(config, a)
        assert (found.state, found.state_reason) == ("abandoned", "Bought one.")

    async def test_abandon_needs_no_reason(self, admin, config):  # noqa: F811
        a = _seed(config)
        assert (await self._post(admin, f"{a}/abandon", {})).status_code == 200
        assert _get(config, a).state == "abandoned"

    async def test_a_top_up_raises_the_cap(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/topup", {"usd": 5})
        assert response.status_code == 200, response.text
        assert _get(config, a).budget["max_cost_usd"] == 25.0
        assert "still_paused" not in response.json()
        await self._post(admin, f"{a}/topup", {"wakes": 2})
        assert _get(config, a).budget["max_wakes"] == 42

    async def test_a_top_up_under_the_floor_says_it_is_still_paused(self, admin, config):  # noqa: F811
        from istota.usage.telemetry import BrainUsage

        a = _seed(config)
        with db.get_db(config.db_path) as conn:
            store.update_assignment(conn, a, budget={**BUDGET, "max_cost_usd": 1.0})
            store.set_state(conn, a, "paused", reason="budget")
            task_id = db.create_task(conn, prompt="[assignment wake]", user_id="alice", assignment_id=a)
            wake = store.insert_wake(conn, a, fired_by="time", task_id=task_id)
            store.update_wake(conn, wake, ended_at=db.iso_utc_now(), outcome="no_next_step")
            db.insert_task_usage(
                conn, usage=BrainUsage(has_totals=True, cost_usd=0.94), task_id=task_id,
                user_id="alice", source_type="assignment", assignment_id=a,
            )
        response = await self._post(admin, f"{a}/topup", {"usd": 0.5})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["assignment"]["state"] == "paused"
        assert data["still_paused"]["cheapest_wake_usd"] == pytest.approx(0.94)
        assert data["still_paused"]["remaining"]["cost_usd"] == pytest.approx(0.56)

    @pytest.mark.parametrize("body", [
        {}, {"usd": 1, "wakes": 1}, {"usd": "1"}, {"usd": True}, {"wakes": 1.5}, {"wakes": True},
        {"usd": 10 ** 400},
    ])
    async def test_a_top_up_takes_exactly_one_number(self, admin, config, body):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/topup", body)
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_input"
        assert _get(config, a).budget == BUDGET

    async def test_a_ceiling_is_a_refusal(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/topup", {"usd": 10 ** 9})
        assert response.status_code == 400
        assert response.json()["error"] == "over_ceiling"

    async def test_a_note_is_a_trusted_inbox_event(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/note", {"text": "Try Acme first."})
        assert response.status_code == 200, response.text
        assert isinstance(response.json()["inbox_id"], int)
        with db.get_db(config.db_path) as conn:
            [event] = store.list_inbox(conn, a)
        assert event.kind == "user_note" and event.trusted

    @pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": 7}, {"text": ["x"]}])
    async def test_a_note_needs_text(self, admin, config, body):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/note", body)
        assert response.status_code == 400, response.text
        assert _inbox_kinds(config, a) == []

    async def test_close_answers_a_proposal(self, admin, config):  # noqa: F811
        a = _seed(config, "waiting")
        response = await self._post(admin, f"{a}/close", {"completed": True})
        assert response.status_code == 200, response.text
        assert _get(config, a).state == "completed"

    async def test_close_sends_a_proposal_back(self, admin, config):  # noqa: F811
        a = _seed(config, "waiting")
        response = await self._post(admin, f"{a}/close", {"reject": "Only two quotes."})
        assert response.status_code == 200, response.text
        assert _get(config, a).state == "sleeping"
        assert _inbox_kinds(config, a) == ["completion_rejected"]

    @pytest.mark.parametrize("body", [
        {}, {"completed": False}, {"completed": "yes"}, {"reject": 7},
        {"completed": True, "reject": "both"},
    ])
    async def test_close_takes_one_answer(self, admin, config, body):  # noqa: F811
        a = _seed(config, "waiting")
        response = await self._post(admin, f"{a}/close", body)
        assert response.status_code == 400, response.text
        assert _get(config, a).state == "waiting"

    async def test_confirming_the_last_item_completes(self, admin, config):  # noqa: F811
        a = _seed(config)
        with db.get_db(config.db_path) as conn:
            item = store.insert_item(conn, a, text="Signed", kind="confirmed", validator=None)
        response = await self._post(admin, f"{a}/items/{item}/confirm")
        assert response.status_code == 200, response.text
        assert response.json()["item"] == {"id": item, "status": "confirmed"}
        assert _get(config, a).state == "completed"

    async def test_a_handoff_done_reaches_the_next_wake(self, admin, config):  # noqa: F811
        a = _seed(config)
        with db.get_db(config.db_path) as conn:
            action = store.insert_action(conn, a, text="Call Acme", report_spec="the date")
        response = await self._post(admin, f"{a}/handoffs/{action}/done", {"report": "Tuesday."})
        assert response.status_code == 200, response.text
        assert response.json()["handoff"] == {"id": action, "status": "done"}
        assert _inbox_kinds(config, a) == ["handoff_done"]

    async def test_an_item_or_handoff_elsewhere_is_not_found(self, admin, config):  # noqa: F811
        a = _seed(config)
        other = _seed(config, slug="other")
        with db.get_db(config.db_path) as conn:
            action = store.insert_action(conn, other, text="Call Acme", report_spec="the date")
        response = await self._post(admin, f"{a}/handoffs/{action}/done", {"report": "x"})
        assert response.status_code == 404
        assert (await self._post(admin, f"{a}/handoffs/x1/done", {"report": "x"})).status_code == 404
        assert (await self._post(admin, f"{a}/items/x1/confirm")).status_code == 404


class TestRefusals:
    async def _post(self, http, path, body=None):
        return await http.post(f"{BASE}/{path}", json=body, headers=ORIGIN)

    async def test_a_bad_state_is_a_409_with_the_state(self, admin, config):  # noqa: F811
        a = _seed(config, "completed")
        response = await self._post(admin, f"{a}/pause")
        assert response.status_code == 409
        data = response.json()
        assert data["error"] == "bad_state"
        assert data["state"] == "completed"
        assert data["detail"]

    async def test_any_other_transition_error_is_a_400(self, admin, config):  # noqa: F811
        a = _seed(config)
        with db.get_db(config.db_path) as conn:
            store.insert_item(conn, a, text="Mech", kind="mechanical", validator={
                "check": "contains", "file": "a.md", "text": "done",
            })
        response = await self._post(admin, f"{a}/close", {"completed": True})
        assert response.status_code == 400
        assert response.json()["error"] == "not_met"
        assert _get(config, a).state == "sleeping"

    async def test_a_body_is_never_echoed(self, admin, config):  # noqa: F811
        a = _seed(config)
        for path, body in (
            (f"{a}/topup", {"usd": CANARY}),
            (f"{a}/note", {"text": {CANARY: CANARY}}),
            (f"{a}/close", {"reject": [CANARY]}),
            (f"{a}/abandon", {"reason": {CANARY: 1}}),
            (f"{a}/handoffs/{CANARY}/done", {"report": CANARY}),
        ):
            response = await self._post(admin, path, body)
            assert response.status_code in (400, 404), path
            assert CANARY not in response.text, path

    async def test_a_body_that_is_not_json_or_an_object(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await admin.post(f"{BASE}/{a}/note", content=f"{{{CANARY}", headers=ORIGIN)
        assert response.status_code == 400
        assert CANARY not in response.text
        assert (await self._post(admin, f"{a}/note", ["x"])).status_code == 400

    async def test_a_body_over_the_cap(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await self._post(admin, f"{a}/note", {"text": "x" * (17 * 1024)})
        assert response.status_code == 413
        assert _inbox_kinds(config, a) == []

    async def test_a_log_line_carries_no_words(self, admin, config, caplog):  # noqa: F811
        a = _seed(config)
        with db.get_db(config.db_path) as conn:
            action = store.insert_action(conn, a, text="Call Acme", report_spec="the date")
        with caplog.at_level("DEBUG", logger="istota.webui.assignment_routes"):
            await self._post(admin, f"{a}/note", {"text": CANARY})
            await self._post(admin, f"{a}/handoffs/{action}/done", {"report": CANARY})
        assert CANARY not in caplog.text


class TestTheRedirect:
    async def test_the_owner_lands_on_the_assignment(self, admin, config):  # noqa: F811
        a = _seed(config)
        response = await admin.get(f"/istota/settings/assignments/open/{a}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == f"/istota/settings/assignments?assignment={a}"

    async def test_anything_else_lands_on_the_bare_page(self, admin, config):  # noqa: F811
        bobs = _seed(config, user="bob", slug="bobs")
        for ident in (bobs, 999999, "x1", "9" * 40):
            response = await admin.get(
                f"/istota/settings/assignments/open/{ident}", follow_redirects=False,
            )
            assert response.status_code == 302, ident
            assert response.headers["location"] == "/istota/settings/assignments", ident

    async def test_no_session_lands_on_the_bare_page(self, client, config):  # noqa: F811
        a = _seed(config)
        response = await client.get(f"/istota/settings/assignments/open/{a}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/istota/settings/assignments"
