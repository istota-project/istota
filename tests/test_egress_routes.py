"""Settings > Network access, and `istota egress`, over the real store.

What matters: a route reaches only the signed-in user's subjects, every write needs
an origin, an approval names the text the page showed and is refused once that text
moved, and a refused body echoes nothing it was sent. An admin gets nothing extra.
"""

import argparse
import json
from unittest.mock import AsyncMock

import pytest

from istota import db
from istota.cli import cmd_egress
from istota.notifications import store
from istota.notifications.resolvers import egress_refused
from istota.rooms import egress
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

BASE = "/istota/api/settings/network"
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
    response = await http.get(BASE)
    assert response.status_code == 200, response.text
    return response.json()


def _subject(data, subject=SUBJECT):
    return next(item for item in data["subjects"] if item["subject"] == subject)


class TestTheListing:
    async def test_it_needs_a_session(self, client):  # noqa: F811
        assert (await client.get(BASE)).status_code == 401

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
        response = await signed_client.get(BASE)
        assert "bob" not in response.text

    async def test_a_module_job_shows_only_with_a_refusal(self, signed_client, config):  # noqa: F811
        _job(config, name="_module.feeds.run_scheduled")
        data = await _listing(signed_client)
        assert all("_module" not in item["subject"] for item in data["subjects"])
        _refused(config, subject="cron:_module.feeds.run_scheduled")
        data = await _listing(signed_client)
        assert any("_module" in item["subject"] for item in data["subjects"])

    async def test_the_skills_on_offer_are_external_ones(self, signed_client, config):  # noqa: F811
        data = await _listing(signed_client)
        assert "feeds" not in data["external_skills"]

    async def test_it_is_not_cached(self, signed_client):
        response = await signed_client.get(BASE)
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
        "https://news.example.com", "news", "*.example.com", "192.0.2.1", "a.example:443",
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


class TestApprovingAJob:
    async def test_open_needs_the_digest_the_page_showed(self, signed_client, config):  # noqa: F811
        digest = _job(config)
        _refused(config, entry="open")
        response = await signed_client.post(
            BASE + "/approvals", json={"subject": SUBJECT, "kind": "open", "digest": digest},
            headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _grants(config) == {f"open:{digest}"}
        item = _subject(await _listing(signed_client))
        assert item["state"] == "open"
        assert item["refused"] == []
        assert _open_rows(config) == []

    async def test_a_stale_digest_is_refused_with_a_message_to_reload(self, signed_client, config):  # noqa: F811
        digest = _job(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE scheduled_jobs SET prompt = 'fetch something else'")
            conn.commit()
        response = await signed_client.post(
            BASE + "/approvals", json={"subject": SUBJECT, "kind": "open", "digest": digest},
            headers=ORIGIN,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "stale_job"
        assert "Reload" in response.json()["detail"]
        assert _grants(config) == frozenset()

    async def test_public_needs_the_job_to_ask(self, signed_client, config):  # noqa: F811
        digest = _job(config)
        response = await signed_client.post(
            BASE + "/approvals", json={"subject": SUBJECT, "kind": "public", "digest": digest},
            headers=ORIGIN,
        )
        assert response.status_code == 400
        assert _grants(config) == frozenset()

    async def test_public_is_approved_for_a_job_that_asks(self, signed_client, config):  # noqa: F811
        digest = _job(config, reach="public")
        response = await signed_client.post(
            BASE + "/approvals", json={"subject": SUBJECT, "kind": "public", "digest": digest},
            headers=ORIGIN,
        )
        assert response.status_code == 200, response.text
        assert _subject(await _listing(signed_client))["state"] == "public"

    async def test_an_assignment_has_no_text_to_approve(self, signed_client, config):  # noqa: F811
        response = await signed_client.post(
            BASE + "/approvals",
            json={"subject": egress.TASKS_FILE_SUBJECT, "kind": "open", "digest": "0" * 64},
            headers=ORIGIN,
        )
        assert response.status_code == 404


class TestTheBoundaries:
    @pytest.mark.parametrize("path,body", [
        ("/grants", {"subject": SUBJECT, "entry": HOST}),
        ("/grants/remove", {"subject": SUBJECT, "entry": HOST}),
        ("/refused/dismiss", {"subject": SUBJECT, "entry": HOST}),
        ("/approvals", {"subject": SUBJECT, "kind": "open", "digest": "0" * 64}),
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
            BASE + "/approvals",
            json={"subject": SUBJECT, "kind": "open", "digest": digest, "user": "bob"},
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
