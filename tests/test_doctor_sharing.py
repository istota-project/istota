"""`sharing.ics_feeds`: the calendar feeds users shared, as counts by state.

A real database: the check reads it read-only and names no address, host or
calendar name.
"""

from __future__ import annotations

import pytest

from istota import db, doctor
from istota.config import Config
from istota.sharing import sources


def _feed(conn, user, key, *, failing=False):
    source_id = sources.create_source(
        conn, user_id=user, kind="ics_feed", state="active",
        remote_host="secret-host.example", remote_key=key, extras={"name": "Private name"},
    )
    if failing:
        sources.set_state(conn, source_id, user, "error", reason="http_500")
    return source_id


def _check(db_path):
    return doctor.check_sharing_ics_feeds(Config(db_path=db_path), False)


def test_registered_with_a_deployment_scope():
    assert "sharing.ics_feeds" in dict(doctor.CHECKS)
    assert doctor.CHECK_SCOPES["sharing.ics_feeds"] == doctor.DEPLOYMENT


def test_no_database_skips(tmp_path):
    assert _check(tmp_path / "absent.db").status == doctor.SKIP


def test_no_feeds_is_ok(db_path):
    result = _check(db_path)
    assert (result.status, result.detail) == (doctor.OK, "no calendar feeds are subscribed")


def test_counts_by_state_and_names_nothing(db_path):
    with db.get_db(db_path) as conn:
        _feed(conn, "alice", "a")
        _feed(conn, "bob", "b", failing=True)
        revoked = _feed(conn, "bob", "c")
        sources.revoke(conn, revoked, "bob", reason="user_removed")
        conn.commit()
    result = _check(db_path)
    assert result.status == doctor.OK
    assert result.detail == "1 calendar feed(s) answering, 1 failing"
    assert "secret-host" not in result.detail and "Private" not in result.detail


def test_every_feed_failing_warns_with_a_remedy(db_path):
    with db.get_db(db_path) as conn:
        _feed(conn, "alice", "a", failing=True)
        conn.commit()
    result = _check(db_path)
    assert result.status == doctor.WARN
    assert '[private_hosts] "<host>" = ["sharing"]' in result.remedy


def test_an_unmigrated_database_warns_rather_than_raising(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    assert _check(path).status == doctor.WARN


def _event(db_path, user="alice"):
    from istota.sharing import events

    config = Config(db_path=db_path)
    with db.get_db(db_path) as conn:
        task = db.get_task(conn, db.create_task(conn, "x", user, source_type="cli"))
        events.apply_ops(conn, config, task, [events.upsert_op(
            f"{user}-1@istota.example.org", create=True, fields={
                "summary": "Private summary", "dtstart": "2026-11-02", "dtend": "2026-11-03",
                "all_day": True})])
        conn.commit()


def _mail_config(db_path, *, authserv_id=""):
    from istota.config import EmailConfig

    return Config(db_path=db_path, email=EmailConfig(
        enabled=True, bot_email="istota@example.org", authserv_id=authserv_id))


def test_invitations_without_an_authserv_id_warn(db_path):
    _event(db_path)
    result = doctor.check_sharing_ics_feeds(_mail_config(db_path), False)
    assert result.status == doctor.WARN
    assert "authserv_id" in result.detail + (result.remedy or "")
    assert "alice" not in result.detail and "Private" not in result.detail


def test_invitations_with_an_authserv_id_are_ok(db_path):
    _event(db_path)
    result = doctor.check_sharing_ics_feeds(_mail_config(db_path, authserv_id="mx.example.org"), False)
    assert result.status == doctor.OK


def test_feed_delivery_needs_no_authserv_id(db_path):
    from istota import user_profiles

    _event(db_path)
    user_profiles.ensure_profile(db_path, "alice")
    user_profiles.update_profile(db_path, "alice", calendar_delivery="feed")
    assert doctor.check_sharing_ics_feeds(_mail_config(db_path), False).status == doctor.OK


def test_no_istota_events_needs_no_authserv_id(db_path):
    assert doctor.check_sharing_ics_feeds(_mail_config(db_path), False).status == doctor.OK


# --- sharing.ocm ---------------------------------------------------------------


def _ocm_config(db_path, *, hostname="bot.example.com", enabled=True, nextcloud_url=""):
    from istota.config import NextcloudConfig, SharingConfig, SharingOcmConfig, SiteConfig

    return Config(
        db_path=db_path, site=SiteConfig(hostname=hostname),
        nextcloud=NextcloudConfig(url=nextcloud_url),
        sharing=SharingConfig(ocm=SharingOcmConfig(enabled=enabled)),
    )


def test_ocm_registered_with_a_deployment_scope():
    assert "sharing.ocm" in dict(doctor.CHECKS)
    assert doctor.CHECK_SCOPES["sharing.ocm"] == doctor.DEPLOYMENT


def test_ocm_off_skips(db_path):
    assert doctor.check_sharing_ocm(_ocm_config(db_path, enabled=False), False).status == doctor.SKIP


def test_ocm_on_a_public_name_is_ok_without_a_probe(db_path, monkeypatch):
    def refuse(url):
        raise AssertionError("probe=False opened a socket")

    monkeypatch.setattr(doctor, "_fetch_ocm_discovery", refuse)
    assert doctor.check_sharing_ocm(_ocm_config(db_path), False).status == doctor.OK


@pytest.mark.parametrize("hostname", ["", "localhost:8181", "127.0.0.1", "[::1]:8080"])
def test_ocm_needs_a_name_another_server_can_reach(db_path, hostname):
    result = doctor.check_sharing_ocm(_ocm_config(db_path, hostname=hostname), False)
    assert result.status == doctor.FAIL and result.remedy


def test_ocm_on_the_nextcloud_host_fails(db_path):
    config = _ocm_config(db_path, nextcloud_url="https://bot.example.com/nextcloud")
    assert doctor.check_sharing_ocm(config, False).status == doctor.FAIL


def test_ocm_probe_wants_istotas_own_document(db_path, monkeypatch):
    from istota.sharing import ocm

    config = _ocm_config(db_path)
    asked = []

    def ours(url):
        asked.append(url)
        return 200, ocm.discovery_document(config)

    monkeypatch.setattr(doctor, "_fetch_ocm_discovery", ours)
    assert doctor.check_sharing_ocm(config, True).status == doctor.OK
    assert asked == ["https://bot.example.com/ocm-provider/"]


@pytest.mark.parametrize("answer", [
    (200, {"apiVersion": "1.0-proposal1", "endPoint": "https://bot.example.com/ocm"}),
    (200, {"apiVersion": "1.1.0", "endPoint": "", "provider": "Istota"}),
    (404, None),
    (200, None),
], ids=["a-nextcloud-answers", "empty-endpoint", "404", "not-json"])
def test_ocm_probe_fails_on_any_other_answer(db_path, monkeypatch, answer):
    monkeypatch.setattr(doctor, "_fetch_ocm_discovery", lambda url: answer)
    assert doctor.check_sharing_ocm(_ocm_config(db_path), True).status == doctor.FAIL


def test_ocm_probe_that_cannot_connect_fails(db_path, monkeypatch):
    def unreachable(url):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor, "_fetch_ocm_discovery", unreachable)
    result = doctor.check_sharing_ocm(_ocm_config(db_path), True)
    assert result.status == doctor.FAIL and result.remedy
