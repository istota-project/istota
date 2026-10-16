"""Admin › Scheduled jobs: the list, disable and enable, and delete.

Through the FastAPI app with a real database and a real CRON.md on disk, since
CRON.md is what the sync reads back: a change the file does not carry is
reverted on the next tick, so every assertion about a user job checks the file
as well as the row.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

from istota import cron_loader, db, user_profiles
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.webui import auth as web_auth

pytestmark = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")

ORIGIN = {"Origin": "https://example.com"}
BASE = "/istota/api/admin/scheduled-jobs"
MODULE_JOB = "_module.feeds.run_scheduled"
CRON_MD = (
    "# Scheduled Jobs\n\nNotes the user wrote.\n\n```toml\n"
    '[[jobs]]\nname = "digest"\ncron = "0 9 * * *"\nprompt = "summarise"\n\n'
    '[[jobs]]\nname = "weekly"\ncron = "0 9 * * 1"\nprompt = "review"\n'
    "```\n"
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import istota.webui.app as mod

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    mount = tmp_path / "mount"
    cron_path = mount / "Users" / "bob" / "istota" / "config" / "CRON.md"
    cron_path.parent.mkdir(parents=True)
    cron_path.write_text(CRON_MD, encoding="utf-8")
    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=mount,
        workspace_mount_path=mount,
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766, oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
    )
    config.admin_users = {"alice"}
    config.email.enabled = True
    db.init_db(config.db_path)
    for user_id in ("alice", "bob"):
        user_profiles.ensure_profile(config.db_path, user_id, display_name=user_id.title())
        web_auth.upsert_identity(config.db_path, user_id, f"{user_id}@example.com")
    with db.get_db(config.db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(conn, "bob", cron_loader.load_cron_jobs(config, "bob"))
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, skill, skill_args, enabled) "
            "VALUES ('bob', ?, '*/5 * * * *', '', 'feeds', '[\"run-scheduled\"]', 1)",
            (MODULE_JOB,),
        )
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    sent: list[tuple] = []
    monkeypatch.setattr(mod.web_auth_mail, "send_auth_email", lambda *args: sent.append(args))
    return {"config": config, "cron": cron_path, "mail": sent}


async def _client(user_id):
    import istota.webui.app as mod

    client = AsyncClient(transport=ASGITransport(app=mod.app), base_url="https://example.com")
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user_id})
    await client.get("/istota/callback", follow_redirects=False)
    return client


@pytest.fixture
async def alice(setup):
    client = await _client("alice")
    yield client
    await client.aclose()


@pytest.fixture
async def bob(setup):
    client = await _client("bob")
    yield client
    await client.aclose()


def _job(setup, name):
    with db.get_db(setup["config"].db_path) as conn:
        return db.get_scheduled_job_by_name(conn, "bob", name)


def _file_jobs(setup):
    return {job.name: job for job in cron_loader.load_cron_jobs(setup["config"], "bob")}


async def _step_up(client, setup) -> dict:
    response = await client.post(
        "/istota/api/settings/step-up", json={"action": "admin_cron_delete"}, headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    code = re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", setup["mail"][-1][2])[0]
    return {"request_id": response.json()["request_id"], "code": code}


async def _delete(client, setup, name, **body):
    job = _job(setup, name)
    if "step_up" not in body:
        body["step_up"] = await _step_up(client, setup)
    return await client.request("DELETE", f"{BASE}/{job.id}", json=body, headers=ORIGIN)


class TestList:
    async def test_lists_every_job_with_both_state_columns(self, alice, setup):
        response = await alice.get(BASE)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        jobs = {job["name"]: job for job in response.json()["jobs"]}
        assert set(jobs) == {"digest", "weekly", MODULE_JOB}
        assert jobs["digest"]["user_id"] == "bob"
        assert jobs["digest"]["enabled"] is True
        assert jobs["digest"]["auto_disabled_at"] is None

    async def test_refuses_a_non_admin(self, bob):
        assert (await bob.get(BASE)).status_code == 403


class TestDisableEnable:
    async def test_disable_writes_cron_md_and_keeps_the_users_notes(self, alice, setup):
        job = _job(setup, "digest")
        response = await alice.post(f"{BASE}/{job.id}/disable", headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "file"
        assert response.json()["job"]["enabled"] is False
        assert _job(setup, "digest").enabled is False
        assert _file_jobs(setup)["digest"].enabled is False
        assert _file_jobs(setup)["weekly"].enabled is True
        assert "Notes the user wrote." in setup["cron"].read_text()

    async def test_a_disable_survives_the_next_sync(self, alice, setup):
        job = _job(setup, "digest")
        await alice.post(f"{BASE}/{job.id}/disable", headers=ORIGIN)
        config = setup["config"]
        with db.get_db(config.db_path) as conn:
            cron_loader.sync_cron_jobs_to_db(conn, "bob", cron_loader.load_cron_jobs(config, "bob"))
        assert _job(setup, "digest").enabled is False

    async def test_enable_lifts_a_suspension(self, alice, setup):
        job = _job(setup, "digest")
        with db.get_db(setup["config"].db_path) as conn:
            db.suspend_scheduled_job(conn, job.id)
            conn.execute("UPDATE scheduled_jobs SET consecutive_failures = 5 WHERE id = ?", (job.id,))
        response = await alice.post(f"{BASE}/{job.id}/enable", headers=ORIGIN)
        assert response.status_code == 200, response.text
        after = _job(setup, "digest")
        assert after.auto_disabled_at is None
        assert after.consecutive_failures == 0
        assert after.enabled is True

    async def test_a_module_job_changes_in_the_table_alone(self, alice, setup):
        before = setup["cron"].read_text()
        job = _job(setup, MODULE_JOB)
        response = await alice.post(f"{BASE}/{job.id}/disable", headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "module"
        assert _job(setup, MODULE_JOB).enabled is False
        assert setup["cron"].read_text() == before

    async def test_a_refused_file_write_is_reported(self, alice, setup, monkeypatch):
        monkeypatch.setattr(cron_loader, "_write_cron_md", lambda *args, **kwargs: False)
        job = _job(setup, "digest")
        response = await alice.post(f"{BASE}/{job.id}/disable", headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "db_only"

    async def test_needs_an_origin_and_an_admin(self, alice, bob, setup):
        job = _job(setup, "digest")
        assert (await alice.post(f"{BASE}/{job.id}/disable")).status_code == 403
        assert (await bob.post(f"{BASE}/{job.id}/disable", headers=ORIGIN)).status_code == 403
        assert _job(setup, "digest").enabled is True

    async def test_an_unknown_job_is_404(self, alice):
        response = await alice.post(f"{BASE}/999999/disable", headers=ORIGIN)
        assert response.status_code == 404
        assert response.json()["error"] == "job_not_found"


class TestDelete:
    async def test_removes_the_job_from_cron_md_and_the_table(self, alice, setup):
        response = await _delete(alice, setup, "digest", confirm="digest")
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] is True
        assert _job(setup, "digest") is None
        assert set(_file_jobs(setup)) == {"weekly"}
        assert "Notes the user wrote." in setup["cron"].read_text()
        assert _job(setup, "weekly") is not None

    async def test_the_typed_name_must_match(self, alice, setup):
        response = await _delete(alice, setup, "digest", confirm="weekly", step_up={})
        assert response.status_code == 409
        assert response.json()["error"] == "confirm_required"
        assert _job(setup, "digest") is not None

    async def test_a_step_up_proof_is_required(self, alice, setup):
        response = await _delete(alice, setup, "digest", confirm="digest", step_up={})
        assert response.status_code == 403
        assert response.json()["reason"]
        assert _job(setup, "digest") is not None
        assert "digest" in _file_jobs(setup)

    async def test_a_module_job_cannot_be_deleted(self, alice, setup):
        response = await _delete(alice, setup, MODULE_JOB, confirm=MODULE_JOB, step_up={})
        assert response.status_code == 409
        assert response.json()["error"] == "module_job"
        assert _job(setup, MODULE_JOB) is not None

    async def test_keeps_the_row_when_cron_md_still_names_the_job(self, alice, setup, monkeypatch):
        monkeypatch.setattr(cron_loader, "_write_cron_md", lambda *args, **kwargs: False)
        response = await _delete(alice, setup, "digest", confirm="digest")
        assert response.status_code == 409
        assert response.json()["error"] == "cron_md_unwritable"
        assert _job(setup, "digest") is not None

    async def test_a_row_cron_md_does_not_name_is_deleted(self, alice, setup):
        with db.get_db(setup["config"].db_path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
                "VALUES ('bob', 'stray', '0 1 * * *', 'p')",
            )
        response = await _delete(alice, setup, "stray", confirm="stray")
        assert response.status_code == 200, response.text
        assert _job(setup, "stray") is None
        assert set(_file_jobs(setup)) == {"digest", "weekly"}

    async def test_only_an_admin_may_ask_for_the_code(self, bob):
        response = await bob.post(
            "/istota/api/settings/step-up", json={"action": "admin_cron_delete"}, headers=ORIGIN,
        )
        assert response.status_code == 403
