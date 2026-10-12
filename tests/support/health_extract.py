"""Drive a health extract route through the daemon hand-off (ISSUE-697).

The routes leave a request and return 202; the daemon answers it. A test that
wants the extractor's result posts, runs the daemon side in-process with
``extract_requests.answer_pending`` and reads the answer back through the poll
route, so the whole seam runs rather than a shortcut around it.

The daemon derives the user's health context from the config, so the route's
``ctx`` has to be that same context: ``handoff_app`` builds both from one
config rather than synthesizing a context the daemon would never find.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota import db
from istota.config import UserConfig
from istota.health import extract_requests
from istota.health._loader import resolve_for_user
from istota.health._migrate import ensure_initialised
from istota.health.routes import get_user_context, require_auth, router

PREFIX = "/istota/api/health"


def handoff_config(make_config, user_id: str = "alice"):
    config = make_config(users={user_id: UserConfig()})
    db.init_db(config.db_path)
    return config


def handoff_app(config, user_id: str = "alice"):
    """A client whose routes and daemon side see one user, and that context."""
    ctx = resolve_for_user(user_id, config)
    ensure_initialised(ctx)
    app = FastAPI()
    app.include_router(router, prefix=PREFIX)
    app.dependency_overrides[require_auth] = lambda: {"username": user_id}
    app.dependency_overrides[get_user_context] = lambda: ctx
    app.state.istota_config = config
    return TestClient(app), ctx


def extract_via_daemon(client: TestClient, config, path: str, **post) -> dict:
    """POST ``path``, answer it daemon-side, and return the answered result."""
    resp = client.post(PREFIX + path, **post)
    assert resp.status_code == 202, resp.text
    request_id = resp.json()["id"]
    extract_requests.answer_pending(config)
    state = client.get(f"{PREFIX}/extract-requests/{request_id}").json()
    assert state["state"] == extract_requests.ANSWERED, state
    return state["result"]
