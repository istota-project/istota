"""The network access routes behind the job and assignment pages, and `istota egress`,
over the real store.

What matters: a route reaches only the signed-in user's subjects, every write needs
an origin, an approval names the text the page showed and is refused once that text
moved, and a refused body echoes nothing it was sent. An admin gets nothing extra.
"""

import argparse
import json
from unittest.mock import AsyncMock

import pytest

from istota import cron_loader, db, storage
from istota.cli import cmd_egress
from istota.notifications import store
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

BASE = "/istota/api/settings/network"
JOBS = "/istota/api/settings/jobs"
ORIGIN = {"Origin": "https://example.com"}
SUBJECT = "cron:digest"
HOST = "news.example.com"
CANARY = "canary-body-value"


async def login(http, user):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user})
    response = await http.get("/istota/callback", follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text


@pytest.fixture
async def signed_client(client, config):  # noqa: F811
    await login(client, "alice")
    return client


def _job(config, user="alice", name="digest", prompt="read the news", reach="private"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, reach) "
            "VALUES (?, ?, '0 9 * * *', ?, ?)",
            (user, name, prompt, reach),
        )
        conn.commit()
        return egress.job_digest({
            "prompt": prompt, "command": None, "skill": None, "skill_args": None,
            "publish_shared_kv": None, "publish_shared_kv_trusted": 0,
        })


def _grants(config, user="alice", subject=SUBJECT):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return egress.grants_for(conn, user, subject)


def _refused(config, entry=HOST, user="alice", subject=SUBJECT):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        egress.note_refused(conn, user, subject, entry)
        result = egress_refused.write(conn, user, subject)
        conn.commit()
    return result


def _open_rows(config, user="alice"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        rows, _ = store.list_open(config, conn, user)
        conn.commit()
    return [row for row in rows if row.source == egress_refused.SOURCE]


async def _listing(http):
    """Each subject as the Scheduled jobs page reads it."""
    response = await http.get(JOBS)
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "subjects": [job["network"] for job in data["jobs"] if job["network"]],
        "external_skills": data["external_skills"],
    }


def _subject(data, subject=SUBJECT):
    return next(item for item in data["subjects"] if item["subject"] == subject)


class TestTheListing:
    async def test_the_old_listing_is_gone(self, signed_client):
        """Both pages read a subject from their own listing, and nothing reads this."""
        assert (await signed_client.get(BASE)).status_code in (404, 405)

    async def test_it_needs_a_session(self, client):  # noqa: F811
        assert (await client.get(JOBS)).status_code == 401

    async def test_a_new_job_is_limited_with_nothing_approved(self, signed_client, config):  # noqa: F811
        digest = _job(config)
        item = _subject(await _listing(signed_client))
        assert item["state"] == "limited"
        assert item["kind"] == "cron"
        assert (item["hosts"], item["skills"], item["approvals"]) == ([], [], [])
        assert item["job"]["digest"] == digest
        assert item["job"]["prompt"] == "read the news"

    async def test_it_shows_each_state(self, signed_client, config):  # noqa: F811
        open_digest = _job(config, name="full")
        public_digest = _job(config, name="pub", prompt="public text", reach="public")
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", "cron:full", f"open:{open_digest}", decided_via="cli")
            egress.add_grant(conn, "alice", "cron:pub", f"public:{public_digest}", decided_via="cli")
            conn.commit()
        data = await _listing(signed_client)
        assert _subject(data, "cron:full")["state"] == "open"
        assert _subject(data, "cron:pub")["state"] == "public"

    async def test_a_stale_approval_reads_as_limited(self, signed_client, config):  # noqa: F811
        _job(config)
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", SUBJECT, "open:" + "0" * 64, decided_via="cli")
            conn.commit()
        assert _subject(await _listing(signed_client))["state"] == "limited"

    async def test_it_lists_refusals_but_no_dismissed_one(self, signed_client, config):  # noqa: F811
        _job(config)
        _refused(config)
        _refused(config, entry="gone.example.com")
        with db.get_db(config.db_path) as conn:
            egress.dismiss_refused(conn, "alice", SUBJECT, "gone.example.com")
            conn.commit()
        item = _subject(await _listing(signed_client))
        assert [row["entry"] for row in item["refused"]] == [HOST]

    async def test_it_holds_only_the_callers_subjects(self, signed_client, config):  # noqa: F811
        _job(config, user="bob", name="bobs-secret-job")
        _refused(config, user="bob", subject="cron:bobs-secret-job", entry="bob.example.com")
        response = await signed_client.get(JOBS)
        assert "bob" not in response.text

    async def test_a_module_job_shows_only_with_a_refusal(self, signed_client, config):  # noqa: F811
        _job(config, name="_module.feeds.run_scheduled")
        data = await _listing(signed_client)
        assert all("_module" not in item["subject"] for item in data["subjects"])
        _refused(config, subject="cron:_module.feeds.run_scheduled")
        data = await _listing(signed_client)
        assert any("_module" in item["subject"] for item in data["subjects"])

    async def test_a_module_job_is_hidden_with_only_a_seeded_grant(self, signed_client, config):  # noqa: F811
        digest = _job(config, name="_module.feeds.prune")
        subject = "cron:_module.feeds.prune"
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", subject, f"open:{digest}", decided_via="migration")
            egress.add_grant(conn, "alice", subject, HOST, decided_via="web")
            conn.commit()
        data = await _listing(signed_client)
        assert all(item["subject"] != subject for item in data["subjects"])

    async def test_a_module_job_goes_again_once_its_refusal_is_dismissed(self, signed_client, config):  # noqa: F811
        _job(config, name="_module.feeds.prune")
        subject = "cron:_module.feeds.prune"
        _refused(config, subject=subject)
        with db.get_db(config.db_path) as conn:
            egress.dismiss_refused(conn, "alice", subject, HOST)
            conn.commit()
        data = await _listing(signed_client)
        assert all(item["subject"] != subject for item in data["subjects"])

    async def test_the_skills_on_offer_are_external_ones(self, signed_client, config):  # noqa: F811
        data = await _listing(signed_client)
        assert "feeds" not in data["external_skills"]

    async def test_it_is_not_cached(self, signed_client):
        response = await signed_client.get(JOBS)
        assert "no-store" in response.headers["cache-control"]


class TestAddingAndRemoving:
    async def test_add_a_refused_host_clears_it_and_closes_the_bell(self, signed_client, config):  # noqa: F811
        _job(config)
        _refused(config)
        assert len(_open_rows(config)) == 1
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": HOST}, headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _grants(config) == {HOST}
        item = _subject(await _listing(signed_client))
        assert item["hosts"] == [HOST]
        assert item["refused"] == []
        assert _open_rows(config) == []
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT decided_via FROM egress_grants WHERE entry = ?", (HOST,),
            ).fetchone()
        assert row[0] == "web"

    @pytest.mark.parametrize("entry", [
        "https://news.example.com", "news", "*.example.com", "192.0.2.1", "a.example:0",
        "news.example.com/path", "",
    ])
    async def test_a_malformed_host_is_refused_plainly(self, signed_client, config, entry):  # noqa: F811
        _job(config)
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": entry}, headers=ORIGIN,
        )
        assert response.status_code == 400
        assert response.json()["field"] == "entry"
        assert _grants(config) == frozenset()

    @pytest.mark.parametrize("entry", ["skill:feeds", "skill:no-such-skill"])
    async def test_a_local_or_unknown_skill_is_refused(self, signed_client, config, entry):  # noqa: F811
        _job(config)
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": entry}, headers=ORIGIN,
        )
        assert response.status_code == 400
        assert _grants(config) == frozenset()

    async def test_an_external_skill_can_be_granted(self, signed_client, config):  # noqa: F811
        _job(config)
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": "skill:shares"}, headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _grants(config) == {"skill:shares"}

    async def test_a_digest_cannot_come_in_as_an_entry(self, signed_client, config):  # noqa: F811
        """The approval route compares it with the row. Here it would not."""
        digest = _job(config)
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": f"open:{digest}"}, headers=ORIGIN,
        )
        assert response.status_code == 400
        assert _grants(config) == frozenset()

    async def test_an_endpoint_the_operator_allows_can_be_granted(self, signed_client, config):  # noqa: F811
        _job(config)
        config.egress.private_hosts = ["192.168.1.10"]
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": "192.168.1.10:8080"}, headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _grants(config) == {"192.168.1.10:8080"}

    async def test_one_the_operator_does_not_says_so_beside_the_field(self, signed_client, config):  # noqa: F811
        _job(config)
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": "192.168.1.10:8080"}, headers=ORIGIN,
        )
        assert response.status_code == 400
        assert "not one this deployment allows" in response.text
        assert "192.168.1.10" not in response.text
        assert _grants(config) == frozenset()

    async def test_remove_takes_the_entry_off(self, signed_client, config):  # noqa: F811
        _job(config)
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", SUBJECT, HOST, decided_via="cli")
            conn.commit()
        response = await signed_client.post(
            BASE + "/grants/remove", json={"subject": SUBJECT, "entry": HOST}, headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _grants(config) == frozenset()

    async def test_removing_open_narrows_the_job(self, signed_client, config):  # noqa: F811
        digest = _job(config)
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", SUBJECT, f"open:{digest}", decided_via="migration")
            conn.commit()
        assert _subject(await _listing(signed_client))["state"] == "open"
        await signed_client.post(
            BASE + "/grants/remove", json={"subject": SUBJECT, "entry": f"open:{digest}"},
            headers=ORIGIN,
        )
        assert _subject(await _listing(signed_client))["state"] == "limited"

    async def test_dismiss_marks_it_and_closes_the_bell(self, signed_client, config):  # noqa: F811
        _job(config)
        _refused(config)
        response = await signed_client.post(
            BASE + "/refused/dismiss", json={"subject": SUBJECT, "entry": HOST}, headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _grants(config) == frozenset()
        with db.get_db(config.db_path) as conn:
            assert egress.list_refused(conn, "alice", SUBJECT)[0]["dismissed"] == 1
        assert _open_rows(config) == []

    async def test_one_of_two_refusals_leaves_the_bell_open(self, signed_client, config):  # noqa: F811
        _job(config)
        _refused(config)
        _refused(config, entry="other.example.com")
        await signed_client.post(
            BASE + "/refused/dismiss", json={"subject": SUBJECT, "entry": HOST}, headers=ORIGIN,
        )
        assert len(_open_rows(config)) == 1


def _sync(config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(
            conn, "alice", cron_loader.load_cron_jobs(config, "alice"),
            ceiling=config.security.scheduled_job_max_unapproved,
        )
        conn.commit()


def _cron_file(config, body, *, around=("", "")):  # noqa: F811
    """A real CRON.md and the row its sync writes, so the file is what is tested."""
    directory = storage.resolve_user_config_dir(config, "alice")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "CRON.md"
    path.write_text(f"{around[0]}```toml\n{body}```\n{around[1]}")
    _sync(config)
    return path


def _row(config, name="digest"):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return egress.job_row_by_subject(conn, "alice", egress.cron_subject(name))


def _digest(config):  # noqa: F811
    return egress.job_digest(_row(config))


JOB = 'name = "digest"\ncron = "0 9 * * *"\nprompt = "read the news"\n'


async def _choose(http, access, digest, subject=SUBJECT):
    return await http.post(
        BASE + "/access",
        json={"subject": subject, "access": access, "digest": digest},
        headers=ORIGIN,
    )


class TestRetiredSubjects:
    """A personal briefing and TASKS.md hold nothing a person can grant, so the page
    treats them as any other subject that does not exist."""

    @pytest.fixture(autouse=True)
    def _briefing(self, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO briefing_configs "
                "(user_id, name, cron_expression, conversation_token, output, components) "
                "VALUES ('alice', 'morning', '0 7 * * *', '', 'email', '{}')",
            )
            conn.commit()

    async def test_the_page_does_not_list_a_briefing(self, signed_client, config):  # noqa: F811
        _job(config)
        data = await _listing(signed_client)
        assert [item["subject"] for item in data["subjects"]] == [SUBJECT]

    @pytest.mark.parametrize("subject", ["briefing:morning", "tasks-file"])
    async def test_a_grant_is_the_same_404(self, signed_client, config, subject):  # noqa: F811
        response = await signed_client.post(
            BASE + "/grants", json={"subject": subject, "entry": HOST}, headers=ORIGIN,
        )
        assert response.status_code == 404
        assert _grants(config, subject=subject) == frozenset()


class TestChoosingAccess:
    """The file authors `reach`, so a choice that did not reach CRON.md would be undone
    by the next sync. Each case therefore syncs before it reads the state back."""

    @pytest.fixture(autouse=True)
    def _ceiling(self, config):  # noqa: F811
        config.security.scheduled_job_max_unapproved = "limited"

    async def test_full_writes_the_file_and_approves_the_text(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n{JOB}")
        digest = _digest(config)
        response = await _choose(signed_client, "open", digest)
        assert response.status_code == 200, response.text
        assert 'reach = "full"' in path.read_text()
        _sync(config)
        assert _grants(config) == {f"open:{digest}"}
        assert _subject(await _listing(signed_client))["state"] == "open"

    async def test_public_needs_nothing_stated_in_the_file_first(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n{JOB}")
        digest = _digest(config)
        response = await _choose(signed_client, "public", digest)
        assert response.status_code == 200, response.text
        assert 'reach = "public"' in path.read_text()
        _sync(config)
        assert _grants(config) == {f"public:{digest}"}
        assert _subject(await _listing(signed_client))["state"] == "public"

    async def test_a_setting_that_covers_it_writes_no_approval(self, signed_client, config):  # noqa: F811
        config.security.scheduled_job_max_unapproved = "public"
        _cron_file(config, f"[[jobs]]\n{JOB}")
        response = await _choose(signed_client, "public", _digest(config))
        assert response.status_code == 200, response.text
        _sync(config)
        assert _grants(config) == frozenset()
        item = _subject(await _listing(signed_client))
        assert (item["state"], item["by_default"]) == ("limited", "public")
        assert item["refused"] == []

    async def test_public_replaces_full(self, signed_client, config):  # noqa: F811
        _cron_file(config, f'[[jobs]]\n{JOB}reach = "full"\n')
        digest = _digest(config)
        await _choose(signed_client, "open", digest)
        await _choose(signed_client, "public", digest)
        _sync(config)
        assert _grants(config) == {f"public:{digest}"}
        assert _subject(await _listing(signed_client))["state"] == "public"

    async def test_full_replaces_public(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n{JOB}")
        digest = _digest(config)
        await _choose(signed_client, "public", digest)
        await _choose(signed_client, "open", digest)
        assert 'reach = "full"' in path.read_text()
        _sync(config)
        assert _grants(config) == {f"open:{digest}"}
        assert _subject(await _listing(signed_client))["state"] == "open"

    async def test_limited_takes_the_approval_and_the_ask_off(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f'[[jobs]]\n{JOB}reach = "full"\n')
        digest = _digest(config)
        await _choose(signed_client, "open", digest)
        response = await _choose(signed_client, "limited", digest)
        assert response.status_code == 200, response.text
        assert 'reach = "limited"' in path.read_text()
        _sync(config)
        assert _grants(config) == frozenset()
        item = _subject(await _listing(signed_client))
        assert (item["state"], item["by_default"], item["refused"]) == ("limited", None, [])
        assert _open_rows(config) == []

    async def test_limited_holds_under_a_setting_that_needs_no_approval(self, signed_client, config):  # noqa: F811
        config.security.scheduled_job_max_unapproved = "full"
        path = _cron_file(config, f"[[jobs]]\n{JOB}")
        assert _subject(await _listing(signed_client))["by_default"] == "open"
        await _choose(signed_client, "limited", _digest(config))
        assert 'reach = "limited"' in path.read_text()
        _sync(config)
        assert _subject(await _listing(signed_client))["by_default"] is None

    async def test_limited_leaves_a_file_that_already_says_so(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n# keep this\n{JOB}")
        digest = _digest(config)
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "alice", SUBJECT, f"open:{digest}", decided_via="web")
            conn.commit()
        before = path.read_text()
        response = await _choose(signed_client, "limited", digest)
        assert response.status_code == 200, response.text
        assert path.read_text() == before
        assert _grants(config) == frozenset()

    async def test_a_multiline_prompt_keeps_its_approval_through_the_rewrite(self, signed_client, config):  # noqa: F811
        body = (
            '[[jobs]]\nname = "digest"\ncron = "0 9 * * *"\n'
            'prompt = """\nfirst line\n\nsecond line\n"""\n'
        )
        _cron_file(config, body)
        digest = _digest(config)
        response = await _choose(signed_client, "open", digest)
        assert response.status_code == 200, response.text
        _sync(config)
        _sync(config)
        assert _digest(config) == digest
        assert _subject(await _listing(signed_client))["state"] == "open"

    async def test_notes_around_the_block_survive(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n{JOB}", around=("# Mine\n\n", "\nAfter.\n"))
        await _choose(signed_client, "open", _digest(config))
        text = path.read_text()
        assert text.startswith("# Mine\n\n```toml")
        assert text.endswith("\nAfter.\n")

    async def test_a_stale_digest_changes_nothing(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n{JOB}")
        digest = _digest(config)
        before = path.read_text()
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE scheduled_jobs SET prompt = 'fetch something else'")
            conn.commit()
        response = await _choose(signed_client, "open", digest)
        assert response.status_code == 409
        assert response.json()["error"] == "stale_job"
        assert "Reload" in response.json()["detail"]
        assert path.read_text() == before
        assert _grants(config) == frozenset()

    async def test_widening_is_refused_outright_when_the_file_cannot_change(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f"[[jobs]]\n{JOB}")
        digest = _digest(config)
        path.unlink()
        response = await _choose(signed_client, "open", digest)
        assert response.status_code == 409
        assert response.json()["error"] == "file_unwritten"
        assert _grants(config) == frozenset()
        assert _row(config)["reach"] == egress.REACH_UNSTATED

    async def test_narrowing_still_removes_the_approval_when_the_file_cannot_change(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f'[[jobs]]\n{JOB}reach = "full"\n')
        digest = _digest(config)
        await _choose(signed_client, "open", digest)
        path.unlink()
        response = await _choose(signed_client, "limited", digest)
        assert response.status_code == 409
        assert response.json()["error"] == "file_unwritten"
        assert _grants(config) == frozenset()
        assert _row(config)["reach"] == "full"
        assert _subject(await _listing(signed_client))["state"] == "limited"

    async def test_a_file_with_an_entry_it_could_not_read_is_not_rewritten(self, signed_client, config):  # noqa: F811
        path = _cron_file(config, f'[[jobs]]\n{JOB}\n[[jobs]]\nname = "typo"\n')
        digest = _digest(config)
        before = path.read_text()
        response = await _choose(signed_client, "open", digest)
        assert response.status_code == 409
        assert path.read_text() == before
        assert _grants(config) == frozenset()

    async def test_a_module_job_is_in_nobodys_file(self, signed_client, config):  # noqa: F811
        digest = _job(config, name="_module.money")
        response = await _choose(signed_client, "open", digest, subject="cron:_module.money")
        assert response.status_code == 400
        assert _grants(config, subject="cron:_module.money") == frozenset()

    async def test_only_a_job_has_access_to_choose(self, signed_client, config):  # noqa: F811
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO assignments (id, user_id, slug, title, goal, budget, dir_relpath, state) "
                "VALUES (7, 'alice', 's', 't', 'g', '{}', 'assignments/s', 'active')",
            )
            conn.commit()
        response = await _choose(signed_client, "open", "0" * 64, subject="assignment:7")
        assert response.status_code == 404

    async def test_someone_elses_job_reads_as_absent(self, signed_client, config):  # noqa: F811
        digest = _job(config, user="bob")
        response = await _choose(signed_client, "open", digest)
        assert response.status_code == 404
        assert _grants(config, user="bob") == frozenset()

    async def test_an_unknown_choice_echoes_nothing(self, signed_client, config):  # noqa: F811
        _cron_file(config, f"[[jobs]]\n{JOB}")
        response = await _choose(signed_client, CANARY, _digest(config))
        assert response.status_code == 400
        assert CANARY not in response.text

    async def test_the_listing_says_what_a_choice_needs(self, signed_client, config):  # noqa: F811
        _cron_file(config, f"[[jobs]]\n{JOB}")
        job = _subject(await _listing(signed_client))["job"]
        assert job["settable"] is True
        assert job["needs_approval"] == {"public": True, "open": True}
        config.security.scheduled_job_max_unapproved = "public"
        job = _subject(await _listing(signed_client))["job"]
        assert job["needs_approval"] == {"public": False, "open": True}


class TestTheBoundaries:
    @pytest.mark.parametrize("path,body", [
        ("/grants", {"subject": SUBJECT, "entry": HOST}),
        ("/grants/remove", {"subject": SUBJECT, "entry": HOST}),
        ("/refused/dismiss", {"subject": SUBJECT, "entry": HOST}),
        ("/access", {"subject": SUBJECT, "access": "open", "digest": "0" * 64}),
    ])
    async def test_every_write_needs_an_origin(self, signed_client, config, path, body):  # noqa: F811
        _job(config)
        assert (await signed_client.post(BASE + path, json=body)).status_code == 403
        hostile = {"Origin": "https://evil.example"}
        assert (await signed_client.post(BASE + path, json=body, headers=hostile)).status_code == 403
        assert _grants(config) == frozenset()

    @pytest.mark.parametrize("path,body", [
        ("/grants", {"subject": SUBJECT, "entry": HOST}),
        ("/grants/remove", {"subject": SUBJECT, "entry": "bob.example.com"}),
        ("/refused/dismiss", {"subject": SUBJECT, "entry": "bob.example.com"}),
    ])
    async def test_another_users_subject_is_a_404(self, signed_client, config, path, body):  # noqa: F811
        _job(config, user="bob")
        with db.get_db(config.db_path) as conn:
            egress.add_grant(conn, "bob", SUBJECT, "bob.example.com", decided_via="cli")
            egress.note_refused(conn, "bob", SUBJECT, "bob.example.com")
            conn.commit()
        assert (await signed_client.post(BASE + path, json=body, headers=ORIGIN)).status_code == 404
        assert _grants(config, user="bob") == {"bob.example.com"}
        assert _grants(config, user="alice") == frozenset()
        with db.get_db(config.db_path) as conn:
            assert egress.list_refused(conn, "bob", SUBJECT)[0]["dismissed"] == 0

    async def test_an_admin_gets_nothing_extra(self, client, config):  # noqa: F811
        """Sessions decide the user; no parameter names another one."""
        await login(client, "alice")
        digest = _job(config, user="bob")
        response = await client.post(
            BASE + "/access",
            json={"subject": SUBJECT, "access": "open", "digest": digest, "user": "bob"},
            headers=ORIGIN,
        )
        assert response.status_code == 400
        assert _grants(config, user="bob") == frozenset()

    @pytest.mark.parametrize("raw", [
        b"not json " + CANARY.encode(),
        json.dumps([CANARY]).encode(),
        json.dumps({"subject": SUBJECT, "entry": CANARY, "extra": CANARY}).encode(),
        json.dumps({"subject": CANARY, "entry": 5}).encode(),
    ])
    async def test_a_refused_body_echoes_nothing(self, signed_client, config, raw):  # noqa: F811
        _job(config)
        response = await signed_client.post(
            BASE + "/grants", content=raw, headers={**ORIGIN, "Content-Type": "application/json"},
        )
        assert response.status_code in (400, 404)
        assert CANARY not in response.text

    async def test_a_big_body_is_refused_before_it_is_parsed(self, signed_client, config):  # noqa: F811
        _job(config)
        response = await signed_client.post(
            BASE + "/grants", json={"subject": SUBJECT, "entry": "x" * 20_000}, headers=ORIGIN,
        )
        assert response.status_code == 413


class TestTheOperatorCli:
    def _args(self, config, action, **fields):  # noqa: F811
        path = config.db_path.parent / "cfg.toml"
        path.write_text(f'db_path = "{config.db_path}"\n')
        defaults = {"config": str(path), "egress_action": action, "user": "alice",
                    "subject": SUBJECT, "entry": HOST, "json": False, "digest": None}
        return argparse.Namespace(**{**defaults, **fields})

    def test_grant_list_and_revoke(self, config, capsys):  # noqa: F811
        _job(config)
        cmd_egress(self._args(config, "grant"))
        assert _grants(config) == {HOST}
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT decided_via FROM egress_grants").fetchone()
        assert row[0] == "cli"
        capsys.readouterr()
        cmd_egress(self._args(config, "list", json=True))
        listed = json.loads(capsys.readouterr().out)
        assert listed["subjects"][0]["hosts"] == [HOST]
        cmd_egress(self._args(config, "revoke"))
        assert _grants(config) == frozenset()

    def test_a_malformed_entry_is_a_plain_refusal(self, config, capsys):  # noqa: F811
        _job(config)
        with pytest.raises(SystemExit) as stop:
            cmd_egress(self._args(config, "grant", entry="https://news.example.com"))
        assert stop.value.code == 1
        assert "Traceback" not in capsys.readouterr().err
        assert _grants(config) == frozenset()

    def test_an_endpoint_follows_the_config_it_loads(self, config, capsys):  # noqa: F811
        _job(config)
        args = self._args(config, "grant", entry="192.168.1.10:8080")
        with pytest.raises(SystemExit):
            cmd_egress(args)
        assert "not one this deployment allows" in capsys.readouterr().err
        config.db_path.parent.joinpath("cfg.toml").write_text(
            f'db_path = "{config.db_path}"\n[private_hosts]\n"192.168.1.10" = ["egress"]\n'
        )
        cmd_egress(args)
        assert _grants(config) == {"192.168.1.10:8080"}

    def test_open_uses_the_rows_text_and_prints_it(self, config, capsys):  # noqa: F811
        digest = _job(config)
        cmd_egress(self._args(config, "grant", entry="open"))
        assert _grants(config) == {f"open:{digest}"}
        assert "read the news" in capsys.readouterr().out

    def test_a_stated_digest_must_match(self, config):  # noqa: F811
        _job(config)
        with pytest.raises(SystemExit):
            cmd_egress(self._args(config, "grant", entry="open", digest="0" * 64))
        assert _grants(config) == frozenset()

    def test_an_unknown_subject_is_refused(self, config):  # noqa: F811
        with pytest.raises(SystemExit):
            cmd_egress(self._args(config, "grant", subject="cron:nothing"))

    @pytest.mark.parametrize("subject", ["briefing:morning", "tasks-file"])
    def test_a_retired_subject_is_refused(self, config, subject):  # noqa: F811
        with pytest.raises(SystemExit):
            cmd_egress(self._args(config, "grant", subject=subject))
        assert _grants(config, subject=subject) == frozenset()


class TestRunState:
    @pytest.mark.parametrize("enabled,suspended,expected", [
        (1, None, "running"),
        (0, None, "disabled"),
        (1, "2026-10-30 12:00:00", "suspended"),
        (0, "2026-10-30 12:00:00", "disabled"),
    ], ids=["running", "disabled", "suspended", "both-reads-disabled"])
    def test_describe_subject_says_whether_the_job_fires(self, tmp_path, enabled, suspended, expected):
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, enabled, "
                "auto_disabled_at) VALUES ('alice', 'digest', '0 9 * * *', 'p', ?, ?)",
                (enabled, suspended),
            )
            job = egress.describe_subject(conn, "alice", "cron:digest")["job"]
        assert job["run_state"] == expected

    def test_a_disabled_one_time_job_waits_on_nothing(self, tmp_path):
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, once, "
                "reach, enabled) VALUES ('alice', 'once', '0 9 * * *', 'p', 1, 'full', 1)",
            )
            assert egress.describe_subject(conn, "alice", "cron:once")["job"]["waiting"] is True
            conn.execute("UPDATE scheduled_jobs SET enabled = 0")
            assert egress.describe_subject(conn, "alice", "cron:once")["job"]["waiting"] is False


class TestThePublishTargetIsShown:
    def test_describe_subject_names_it(self, tmp_path):
        path = tmp_path / "t.db"
        db.init_db(path)
        with db.get_db(path) as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt, "
                "publish_shared_kv, publish_shared_kv_trusted) VALUES "
                "('alice', 'digest', '0 9 * * *', 'p', 'ns/key', 1)",
            )
            job = egress.describe_subject(conn, "alice", "cron:digest")["job"]
        assert job["publish_shared_kv"] == "ns/key"
        assert job["publish_shared_kv_trusted"] is True
