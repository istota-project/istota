"""Settings > Scheduled jobs, over the real store, the real file and the real sync.

What matters: a route reaches only the signed-in user's jobs, every write needs an
origin, a refusal echoes nothing it was sent, the file stays the authority and a save
cannot lose a job, and whoever may not set a restricted field is told so at save time.
"""

import sqlite3
from unittest.mock import AsyncMock

import pytest

from istota import cron_loader, db, storage
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

BASE = "/istota/api/settings/jobs"
ORIGIN = {"Origin": "https://example.com"}
CANARY = "canary-body-value"
DIGEST_JOB = '[[jobs]]\nname = "digest"\ncron = "0 9 * * *"\nprompt = "read the news"\n'


async def login(http, user):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user})
    response = await http.get("/istota/callback", follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text


@pytest.fixture
async def signed_client(client, config):  # noqa: F811
    config.admin_users = {"bob"}
    await login(client, "alice")
    return client


@pytest.fixture
async def admin_client(client, config):  # noqa: F811
    config.admin_users = {"alice"}
    await login(client, "alice")
    return client


def _cron_path(config, user="alice"):  # noqa: F811
    directory = storage.resolve_user_config_dir(config, user)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "CRON.md"


def _sync(config, user="alice"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(
            conn, user, cron_loader.load_cron_jobs(config, user),
            is_admin=config.is_admin(user),
            ceiling=config.security.scheduled_job_max_unapproved,
        )
        conn.commit()


def _cron_file(config, body=DIGEST_JOB, *, sync=True, user="alice"):  # noqa: F811
    path = _cron_path(config, user)
    path.write_text(f"# My jobs\n\n```toml\n{body}```\n\nA note of mine.\n")
    if sync:
        _sync(config, user)
    return path


def _row(config, name="digest", user="alice"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return db.get_scheduled_job_by_name(conn, user, name)


def _insert_row(config, name, user="alice"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) "
            "VALUES (?, ?, '0 9 * * *', 'read the news')",
            (user, name),
        )
        conn.commit()
        return cursor.lastrowid


async def _listing(http):
    response = await http.get(BASE)
    assert response.status_code == 200, response.text
    return response.json()


def _named(data, name="digest"):
    return next(job for job in data["jobs"] if job["name"] == name)


def _new_job(**fields):
    return {"name": "fresh", "cron": "0 7 * * *", "kind": "prompt", "prompt": "say hi", **fields}


async def _edit(http, config, **fields):  # noqa: F811
    listing = await _listing(http)
    job = _named(listing)
    return await http.put(
        f"{BASE}/{job['id']}", json={"revision": job["revision"], **fields}, headers=ORIGIN,
    )


class TestWhoReachesWhat:
    ROUTES = [
        ("post", ""),
        ("put", "/1"),
        ("post", "/1/enable"),
        ("post", "/1/disable"),
        ("post", "/1/run"),
        ("delete", "/1"),
    ]

    async def test_every_route_needs_a_session(self, client):  # noqa: F811
        assert (await client.get(BASE)).status_code == 401
        for method, path in self.ROUTES:
            response = await client.request(method, BASE + path, headers=ORIGIN)
            assert response.status_code == 401, path

    @pytest.mark.parametrize("method,path", ROUTES)
    async def test_every_write_needs_an_origin(self, signed_client, config, method, path):  # noqa: F811
        _cron_file(config)
        job_id = _row(config).id
        target = BASE + path.replace("/1", f"/{job_id}")
        assert (await signed_client.request(method, target)).status_code == 403
        hostile = {"Origin": "https://evil.example"}
        assert (await signed_client.request(method, target, headers=hostile)).status_code == 403
        assert _row(config).enabled

    @pytest.mark.parametrize("method,suffix", [
        ("put", ""), ("post", "/enable"), ("post", "/disable"), ("post", "/run"), ("delete", ""),
    ])
    async def test_somebody_elses_job_reads_as_no_job_at_all(
        self, signed_client, config, method, suffix,  # noqa: F811
    ):
        theirs = _insert_row(config, "bobs", user="bob")
        body = {"json": {"revision": "x"}} if method == "put" else {}
        other = await signed_client.request(method, f"{BASE}/{theirs}{suffix}", headers=ORIGIN, **body)
        missing = await signed_client.request(method, f"{BASE}/99999{suffix}", headers=ORIGIN, **body)
        assert other.status_code == 404
        assert other.content == missing.content
        assert _row(config, "bobs", "bob") is not None

    async def test_an_admin_gets_nothing_extra(self, admin_client, config):  # noqa: F811
        theirs = _insert_row(config, "bobs", user="bob")
        assert (await admin_client.post(f"{BASE}/{theirs}/run", headers=ORIGIN)).status_code == 404
        assert all(job["name"] != "bobs" for job in (await _listing(admin_client))["jobs"])

    @pytest.mark.parametrize("bad", ["abc", "1e3", "-1", "9" * 40])
    async def test_an_id_that_is_no_number_is_the_same_404(self, signed_client, bad):
        response = await signed_client.post(f"{BASE}/{bad}/enable", headers=ORIGIN)
        assert response.status_code == 404
        assert bad not in response.text

    @pytest.mark.parametrize("method,suffix", [
        ("put", ""), ("post", "/enable"), ("post", "/disable"), ("post", "/run"), ("delete", ""),
    ])
    async def test_a_modules_job_is_refused(self, signed_client, config, method, suffix):  # noqa: F811
        module_job = _insert_row(config, "_module.feeds.poll")
        body = {"json": {"revision": "x"}} if method == "put" else {}
        response = await signed_client.request(
            method, f"{BASE}/{module_job}{suffix}", headers=ORIGIN, **body,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "module_job"
        assert _row(config, "_module.feeds.poll") is not None


class TestTheListing:
    async def test_a_synced_job_merges_the_file_with_the_row(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE scheduled_jobs SET consecutive_failures = 2, last_error = 'boom', "
                "last_run_at = '2026-10-01 09:00:00' WHERE name = 'digest'",
            )
            conn.commit()
        data = await _listing(signed_client)
        job = _named(data)
        assert job["id"] == _row(config).id
        assert job["pending"] is False
        assert job["state"] == "running"
        assert (job["consecutive_failures"], job["last_error"]) == (2, "boom")
        assert job["last_run_at"] == "2026-10-01 09:00:00"
        assert job["definition"]["kind"] == "prompt"
        assert job["definition"]["prompt"] == "read the news"
        assert job["editable"] is True
        assert job["revision"]
        assert job["network"]["subject"] == "cron:digest"
        assert data["file"] == {"readable": True, "errors": []}

    async def test_it_is_not_cached(self, signed_client):
        response = await signed_client.get(BASE)
        assert response.headers["cache-control"] == "no-store"

    async def test_a_job_in_the_file_and_not_the_table_is_pending(self, signed_client, config):  # noqa: F811
        _cron_file(config, sync=False)
        job = _named(await _listing(signed_client))
        assert job["id"] is None
        assert job["pending"] is True
        assert job["network"] is None
        assert job["last_run_at"] is None
        assert job["state"] == "running"

    async def test_a_row_the_file_does_not_name_is_read_only(self, signed_client, config):  # noqa: F811
        _cron_file(config, body="")
        _insert_row(config, "gone")
        job = _named(await _listing(signed_client), "gone")
        assert job["id"] is not None
        assert job["pending"] is False
        assert job["editable"] is False
        assert job["revision"] is None
        assert job["definition"]["prompt"] == "read the news"
        assert job["network"] is not None

    async def test_a_typo_elsewhere_makes_every_job_read_only(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        _cron_file(config, body=DIGEST_JOB + '[[jobs]]\nname = "broken"\n', sync=False)
        data = await _listing(signed_client)
        assert data["file"]["readable"] is False
        assert data["file"]["errors"]
        job = _named(data)
        assert job["editable"] is False
        assert job["revision"] is None

    async def test_a_file_that_does_not_parse_still_lists_the_rows(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        _cron_path(config).write_text("```toml\n[[jobs\n```\n")
        data = await _listing(signed_client)
        assert data["file"]["readable"] is False
        job = _named(data)
        assert job["editable"] is False
        assert job["definition"]["prompt"] == "read the news"

    async def test_a_modules_job_shows_only_with_an_open_refusal(self, signed_client, config):  # noqa: F811
        _insert_row(config, "_module.feeds.poll")
        assert (await _listing(signed_client))["jobs"] == []
        with db.get_db(config.db_path) as conn:
            egress.note_refused(conn, "alice", "cron:_module.feeds.poll", "news.example.com")
            conn.commit()
        job = _named(await _listing(signed_client), "_module.feeds.poll")
        assert job["editable"] is False
        assert job["revision"] is None

    async def test_a_command_job_the_sync_drops_is_not_pending_forever(self, signed_client, config):  # noqa: F811
        _cron_file(config, body='[[jobs]]\nname = "shell"\ncron = "0 9 * * *"\ncommand = "ls"\n')
        assert _row(config, "shell") is None
        job = _named(await _listing(signed_client), "shell")
        assert job["id"] is None
        assert job["pending"] is False
        assert job["editable"] is True
        assert "admin" in job["file_error"]

    async def test_no_workspace_means_no_writer(self, signed_client, config):  # noqa: F811
        config.workspace_path = None
        data = await _listing(signed_client)
        assert data["file"]["readable"] is False
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "file_unreadable"

    async def test_options_are_empty_for_a_non_admin(self, signed_client):
        data = await _listing(signed_client)
        assert data["options"]["selectable_brains"] == []
        assert data["options"]["can_publish"] is False
        assert "outputs" in data["options"]
        assert set(data["options"]["rooms"]) == {"talk", "web"}
        assert "external_skills" in data
        assert data["timezone"]

    async def test_a_shared_kv_writer_can_publish(self, admin_client):
        assert (await _listing(admin_client))["options"]["can_publish"] is True


class TestCreate:
    async def test_it_writes_the_file_and_leaves_the_table_to_the_sync(self, signed_client, config):  # noqa: F811
        path = _cron_file(config)
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 201, response.text
        assert response.json()["applies"] == "next_sync"
        text = path.read_text()
        assert 'name = "fresh"' in text
        assert text.startswith("# My jobs\n") and "A note of mine." in text
        assert _row(config, "fresh") is None
        assert _named(await _listing(signed_client), "fresh")["pending"] is True
        _sync(config)
        assert _row(config, "fresh").prompt == "say hi"

    async def test_with_no_file_and_no_rows_it_makes_the_file(self, signed_client, config):  # noqa: F811
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 201, response.text
        assert 'name = "fresh"' in _cron_path(config).read_text()

    async def test_a_missing_file_does_not_lose_the_rows(self, signed_client, config):  # noqa: F811
        """A passing mount fault: a file holding only the new job would orphan the rest."""
        _insert_row(config, "existing")
        assert (await _listing(signed_client))["file"]["readable"] is False
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "file_unreadable"
        assert not _cron_path(config).exists()

    async def test_a_reseeded_template_does_not_lose_the_rows(self, signed_client, config):  # noqa: F811
        """A lost file comes back as the template, and the next tick restores the rows
        into it. A job added first would make it stop being a template, and the sweep
        would take them."""
        _insert_row(config, "existing")
        path = _cron_path(config)
        path.write_text(storage.CRON_TEMPLATE)
        before = path.read_text()
        assert (await _listing(signed_client))["file"]["readable"] is False
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "file_unreadable"
        assert path.read_text() == before

    async def test_only_a_modules_rows_do_not_hold_the_file_back(self, signed_client, config):  # noqa: F811
        _insert_row(config, "_module.feeds.poll")
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 201, response.text

    async def test_a_field_error_is_beside_its_field_and_echoes_nothing(self, signed_client):
        response = await signed_client.post(BASE, json=_new_job(cron=CANARY), headers=ORIGIN)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_field"
        assert response.json()["field"] == "cron"
        assert CANARY not in response.text

    async def test_a_taken_name_points_at_the_name(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        response = await signed_client.post(BASE, json=_new_job(name="digest"), headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "name_taken"
        assert response.json()["field"] == "name"

    async def test_a_typo_elsewhere_refuses_and_leaves_the_file(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, body=DIGEST_JOB + '[[jobs]]\nname = "broken"\n', sync=False)
        before = path.read_text()
        response = await signed_client.post(BASE, json=_new_job(), headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "file_unreadable"
        assert path.read_text() == before

    @pytest.mark.parametrize("content,status,error", [
        (b"not json " + CANARY.encode(), 400, "invalid_body"),
        (b'["' + CANARY.encode() + b'"]', 400, "invalid_field"),
        (b'{"name": "' + b"x" * (70 * 1024) + b'"}', 413, "too_large"),
    ])
    async def test_a_bad_body_echoes_nothing(self, signed_client, content, status, error):
        response = await signed_client.post(BASE, content=content, headers=ORIGIN)
        assert response.status_code == status
        assert response.json()["error"] == error
        assert CANARY not in response.text

    async def test_a_non_admins_command_is_a_field_error(self, signed_client):
        response = await signed_client.post(
            BASE, json=_new_job(kind="command", prompt="", command="ls /"), headers=ORIGIN,
        )
        assert response.status_code == 400
        assert response.json()["field"] == "command"

    async def test_a_shared_room_target_is_refused(self, signed_client, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "ours")
            db.add_room_member(conn, room.token, "bob")
            conn.commit()
        response = await signed_client.post(
            BASE, json=_new_job(target=f"web:{room.token}"), headers=ORIGIN,
        )
        assert response.status_code == 400
        assert response.json()["field"] == "target"
        assert room.token not in response.text


class TestRestrictedFields:
    @pytest.mark.parametrize("field,value", [
        ("brain", "native"),
        ("provider", "someprovider"),
        ("publish_shared_kv", "news/today"),
        ("publish_shared_kv_trusted", True),
    ])
    async def test_a_non_admin_cannot_set_one(self, signed_client, field, value):
        response = await signed_client.post(BASE, json=_new_job(**{field: value}), headers=ORIGIN)
        assert response.status_code == 400
        assert response.json()["field"] == field

    async def test_a_reserved_namespace_is_refused_for_a_writer(self, admin_client):
        response = await admin_client.post(
            BASE, json=_new_job(publish_shared_kv="_doctor/report"), headers=ORIGIN,
        )
        assert response.status_code == 400
        assert response.json()["field"] == "publish_shared_kv"

    async def test_a_writer_can_publish(self, admin_client, config):  # noqa: F811
        response = await admin_client.post(
            BASE, json=_new_job(publish_shared_kv="news/today"), headers=ORIGIN,
        )
        assert response.status_code == 201, response.text
        assert 'publish_shared_kv = "news/today"' in _cron_path(config).read_text()

    @pytest.fixture
    def held(self, config):  # noqa: F811
        """A job whose owner has since lost the right to what it says."""
        return _cron_file(config, body=DIGEST_JOB + 'publish_shared_kv = "news/today"\n')

    async def test_an_unchanged_value_saves(self, signed_client, config, held):  # noqa: F811
        response = await _edit(signed_client, config, prompt="read other news",
                               publish_shared_kv="news/today")
        assert response.status_code == 200, response.text
        assert 'publish_shared_kv = "news/today"' in held.read_text()

    async def test_a_changed_value_is_refused(self, signed_client, config, held):  # noqa: F811
        response = await _edit(signed_client, config, publish_shared_kv="news/tomorrow")
        assert response.status_code == 400
        assert response.json()["field"] == "publish_shared_kv"

    async def test_it_can_be_cleared(self, signed_client, config, held):  # noqa: F811
        response = await _edit(signed_client, config, publish_shared_kv="")
        assert response.status_code == 200, response.text
        assert "publish_shared_kv" not in held.read_text()


class TestEdit:
    async def test_it_writes_the_file_and_not_the_row(self, signed_client, config):  # noqa: F811
        path = _cron_file(config)
        response = await _edit(signed_client, config, prompt="read other news")
        assert response.status_code == 200, response.text
        assert response.json()["applies"] == "next_sync"
        assert response.json()["access_kept"] is None
        assert "read other news" in path.read_text()
        assert _row(config).prompt == "read the news"

    async def test_a_stale_revision_changes_nothing(self, signed_client, config):  # noqa: F811
        path = _cron_file(config)
        job = _named(await _listing(signed_client))
        path.write_text(path.read_text().replace("read the news", "the model's edit"))
        before = path.read_text()
        response = await signed_client.put(
            f"{BASE}/{job['id']}",
            json={"revision": job["revision"], "prompt": "mine"},
            headers=ORIGIN,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "job_changed"
        assert path.read_text() == before

    async def test_a_revision_is_required(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        job_id = _row(config).id
        response = await signed_client.put(f"{BASE}/{job_id}", json={"prompt": "x"}, headers=ORIGIN)
        assert response.status_code == 400
        assert response.json()["field"] == "revision"

    async def test_a_row_the_file_does_not_name_cannot_be_edited(self, signed_client, config):  # noqa: F811
        _cron_file(config, body="")
        job_id = _insert_row(config, "gone")
        response = await signed_client.put(
            f"{BASE}/{job_id}", json={"revision": "x", "prompt": "y"}, headers=ORIGIN,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "file_unreadable"

    async def test_a_missing_file_does_not_lose_the_rows(self, signed_client, config):  # noqa: F811
        job_id = _insert_row(config, "existing")
        response = await signed_client.put(
            f"{BASE}/{job_id}", json={"revision": "x", "prompt": "y"}, headers=ORIGIN,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "file_unreadable"
        assert not _cron_path(config).exists()

    @pytest.mark.parametrize("status,code,error", [
        ("locked", 409, "locked"),
        ("failed", 500, "failed"),
        ("too_large", 413, "too_large"),
        ("unreadable", 409, "file_unreadable"),
        ("not_found", 409, "job_changed"),
    ])
    async def test_each_writer_refusal_has_its_answer(
        self, signed_client, config, monkeypatch, status, code, error,  # noqa: F811
    ):
        _cron_file(config)
        monkeypatch.setattr(
            cron_loader, "save_job_definition",
            lambda *_a, **_k: cron_loader.JobSaveOutcome(status),
        )
        response = await _edit(signed_client, config, prompt="x")
        assert response.status_code == code
        assert response.json()["error"] == error

    async def test_an_invalid_job_says_why_without_its_name(self, signed_client, config, monkeypatch):  # noqa: F811
        _cron_file(config)
        monkeypatch.setattr(
            cron_loader, "save_job_definition",
            lambda *_a, **_k: cron_loader.JobSaveOutcome(
                "invalid", (f"Job '{CANARY}': the schedule is wrong.",),
            ),
        )
        response = await _edit(signed_client, config, name=CANARY)
        assert response.status_code == 422
        assert response.json()["error"] == "invalid"
        assert response.json()["errors"] == ["This job: the schedule is wrong."]
        assert CANARY not in response.text


class TestKeepingAccess:
    @pytest.fixture
    def approved(self, config):  # noqa: F811
        path = _cron_file(config, body=DIGEST_JOB + 'reach = "full"\n')
        with db.get_db(config.db_path) as conn:
            row = egress.job_row_by_subject(conn, "alice", "cron:digest")
            egress.approve_job(conn, "alice", "cron:digest", "open", egress.job_digest(row), decided_via="web")
            conn.commit()
        return path

    def _grants(self, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            return egress.grants_for(conn, "alice", "cron:digest")

    def _state(self, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            grants = egress.grants_for(conn, "alice", "cron:digest")
            return egress.job_state(grants, egress.job_row_by_subject(conn, "alice", "cron:digest"))

    async def test_kept_then_synced_stays_approved_and_asks_nothing(
        self, signed_client, config, approved,  # noqa: F811
    ):
        response = await _edit(signed_client, config, prompt="new text", keep_access="open")
        assert response.status_code == 200, response.text
        assert response.json()["access_kept"] is True
        assert len(self._grants(config)) == 2
        _sync(config)
        assert self._state(config) == "open"
        assert len(self._grants(config)) == 1
        with db.get_db(config.db_path) as conn:
            assert not egress.has_open_refusal(conn, "alice", "cron:digest")

    async def test_the_digest_is_the_servers(self, signed_client, config, approved):  # noqa: F811
        await _edit(signed_client, config, prompt="new text", keep_access="open", digest=CANARY)
        assert all(CANARY not in grant for grant in self._grants(config))

    async def test_without_it_the_job_runs_limited_and_asks(self, signed_client, config, approved):  # noqa: F811
        response = await _edit(signed_client, config, prompt="new text")
        assert response.status_code == 200, response.text
        _sync(config)
        assert self._state(config) == "limited"
        with db.get_db(config.db_path) as conn:
            assert egress.has_open_refusal(conn, "alice", "cron:digest")

    async def test_an_approval_not_held_is_not_granted(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        response = await _edit(signed_client, config, prompt="new text", keep_access="open")
        assert response.status_code == 200, response.text
        assert response.json()["access_kept"] is False
        assert self._grants(config) == frozenset()

    async def test_the_held_check_reads_before_the_write(
        self, signed_client, config, approved, monkeypatch,  # noqa: F811
    ):
        """A sync landing between the file write and the grant has already dropped the
        old approval, so whether the job holds it cannot be asked after the write."""
        real_save = cron_loader.save_job_definition

        def save_then_sync(*args, **kwargs):
            outcome = real_save(*args, **kwargs)
            _sync(config)
            return outcome

        monkeypatch.setattr(cron_loader, "save_job_definition", save_then_sync)
        response = await _edit(signed_client, config, prompt="new text", keep_access="open")
        assert response.json()["access_kept"] is True
        assert self._state(config) == "open"
        with db.get_db(config.db_path) as conn:
            assert not egress.has_open_refusal(conn, "alice", "cron:digest")

    async def test_a_rename_starts_from_nothing(self, signed_client, config, approved):  # noqa: F811
        response = await _edit(signed_client, config, name="renamed", keep_access="open")
        assert response.status_code == 200, response.text
        assert response.json()["access_kept"] is False
        with db.get_db(config.db_path) as conn:
            assert egress.grants_for(conn, "alice", "cron:renamed") == frozenset()

    async def test_text_that_did_not_move_keeps_what_it_has(self, signed_client, config, approved):  # noqa: F811
        before = self._grants(config)
        response = await _edit(signed_client, config, cron="0 10 * * *", keep_access="open")
        assert response.json()["access_kept"] is True
        assert self._grants(config) == before

    async def test_a_failed_grant_leaves_the_save_standing(
        self, signed_client, config, approved, monkeypatch,  # noqa: F811
    ):
        def broken(*_args, **_kwargs):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(egress, "approve_saved_text", broken)
        response = await _edit(signed_client, config, prompt="new text", keep_access="open")
        assert response.status_code == 200
        assert response.json()["access_kept"] is False
        assert "new text" in approved.read_text()

    async def test_it_settles_the_bell(self, signed_client, config, approved):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            egress.note_refused(conn, "alice", "cron:digest", "open")
            egress_refused.write(conn, "alice", "cron:digest")
            conn.commit()
        await _edit(signed_client, config, prompt="new text", keep_access="open")
        with db.get_db(config.db_path) as conn:
            assert not egress.has_open_refusal(conn, "alice", "cron:digest")

    @pytest.mark.parametrize("value", ["full", 1, CANARY])
    async def test_anything_else_is_a_field_error(self, signed_client, config, approved, value):  # noqa: F811
        response = await _edit(signed_client, config, prompt="new text", keep_access=value)
        assert response.status_code == 400
        assert response.json()["field"] == "keep_access"
        assert CANARY not in response.text
        assert "new text" not in approved.read_text()


class TestEnableDisable:
    async def test_disable_writes_the_file_then_the_row(self, signed_client, config):  # noqa: F811
        path = _cron_file(config)
        job_id = _row(config).id
        response = await signed_client.post(f"{BASE}/{job_id}/disable", headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "file"
        assert "enabled = false" in path.read_text()
        assert not _row(config).enabled
        response = await signed_client.post(f"{BASE}/{job_id}/enable", headers=ORIGIN)
        assert response.json()["outcome"] == "file"
        assert _row(config).enabled

    async def test_a_row_the_file_does_not_hold_is_db_only(self, signed_client, config):  # noqa: F811
        job_id = _insert_row(config, "gone")
        response = await signed_client.post(f"{BASE}/{job_id}/disable", headers=ORIGIN)
        assert response.json()["outcome"] == "db_only"


class TestRun:
    async def test_it_queues_a_task(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        job_id = _row(config).id
        response = await signed_client.post(f"{BASE}/{job_id}/run", headers=ORIGIN)
        assert response.status_code == 200, response.text
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, response.json()["task_id"])
        assert task.scheduled_job_id == job_id
        assert task.user_id == "alice"

    async def test_one_already_running_is_refused_with_the_text(self, signed_client, config):  # noqa: F811
        _cron_file(config)
        job_id = _row(config).id
        assert (await signed_client.post(f"{BASE}/{job_id}/run", headers=ORIGIN)).status_code == 200
        response = await signed_client.post(f"{BASE}/{job_id}/run", headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "run_refused"
        assert response.json()["detail"]


class TestDelete:
    async def test_the_file_goes_first_and_then_the_row(self, signed_client, config):  # noqa: F811
        path = _cron_file(config)
        job_id = _row(config).id
        response = await signed_client.delete(f"{BASE}/{job_id}", headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert 'name = "digest"' not in path.read_text()
        assert "A note of mine." in path.read_text()
        assert _row(config) is None

    async def test_a_file_still_naming_it_keeps_the_row(self, signed_client, config, monkeypatch):  # noqa: F811
        _cron_file(config)
        job_id = _row(config).id
        monkeypatch.setattr(cron_loader, "remove_job_definition", lambda *_a: False)
        response = await signed_client.delete(f"{BASE}/{job_id}", headers=ORIGIN)
        assert response.status_code == 409
        assert response.json()["error"] == "cron_md_unwritable"
        assert _row(config) is not None

    async def test_no_transaction_is_open_across_the_file(self, signed_client, config, monkeypatch):  # noqa: F811
        _cron_file(config)
        job_id = _row(config).id
        real = cron_loader.remove_job_definition
        seen = []

        def writes_then_probes(*args):
            result = real(*args)
            # A second writer gets in at once, or something holds the lock.
            with db.get_db(config.db_path, busy_timeout_ms=100) as conn:
                conn.execute("UPDATE scheduled_jobs SET last_error = 'probe' WHERE id = ?", (job_id,))
                conn.commit()
            seen.append(True)
            return result

        monkeypatch.setattr(cron_loader, "remove_job_definition", writes_then_probes)
        assert (await signed_client.delete(f"{BASE}/{job_id}", headers=ORIGIN)).status_code == 200
        assert seen == [True]


class TestLogs:
    async def test_no_log_line_names_a_job(self, signed_client, config, caplog):  # noqa: F811
        import logging

        _cron_file(config, body=DIGEST_JOB.replace("digest", CANARY))
        job_id = _row(config, CANARY).id
        caplog.set_level(logging.INFO, logger="istota.webui.job_routes")
        job = _named(await _listing(signed_client), CANARY)
        await signed_client.put(
            f"{BASE}/{job_id}", json={"revision": job["revision"], "prompt": "x"}, headers=ORIGIN,
        )
        await signed_client.post(f"{BASE}/{job_id}/disable", headers=ORIGIN)
        await signed_client.delete(f"{BASE}/{job_id}", headers=ORIGIN)
        route_lines = [r.getMessage() for r in caplog.records if r.name == "istota.webui.job_routes"]
        assert route_lines
        assert not any(CANARY in line for line in route_lines)


class TestTheRedirect:
    async def test_it_lands_on_the_users_own_job(self, signed_client, config):  # noqa: F811
        job_id = _insert_row(config, "digest")
        response = await signed_client.get(
            f"/istota/settings/jobs/open/{job_id}", follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == f"/istota/settings/jobs?job={job_id}"

    @pytest.mark.parametrize("target", ["theirs", "99999", "abc", "9" * 40])
    async def test_anything_else_is_the_bare_page(self, signed_client, config, target):  # noqa: F811
        theirs = _insert_row(config, "bobs", user="bob")
        path = str(theirs) if target == "theirs" else target
        response = await signed_client.get(f"/istota/settings/jobs/open/{path}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/istota/settings/jobs"

    async def test_with_no_session_it_is_the_bare_page(self, client, config):  # noqa: F811
        job_id = _insert_row(config, "digest")
        response = await client.get(f"/istota/settings/jobs/open/{job_id}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/istota/settings/jobs"
