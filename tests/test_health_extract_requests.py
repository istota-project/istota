"""Health document extraction runs in the daemon, never the web process (ISSUE-697)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from istota import db
from istota.health import db as health_db
from istota.health import extract_requests
from tests.support.health_extract import (
    PREFIX,
    extract_via_daemon,
    handoff_app,
    handoff_config,
)

PDF = ("visit.pdf", b"%PDF-1.4 visit", "application/pdf")
PNG = ("card.png", b"\x89PNG\r\n\x1a\ncard", "image/png")


@pytest.fixture
def config(make_config):
    return handoff_config(make_config)


@pytest.fixture
def app(config):
    return handoff_app(config)


@pytest.fixture
def client(app):
    return app[0]


@pytest.fixture
def ctx(app):
    return app[1]


def _forbid_in_web(monkeypatch):
    """Make every extractor raise, so a route that extracts inline fails."""
    from istota.health import encounter_ocr, immunization_ocr, ocr

    def _boom(*a, **kw):
        raise AssertionError("extraction ran in the web process")

    monkeypatch.setattr(encounter_ocr, "extract_from_file", _boom)
    monkeypatch.setattr(immunization_ocr, "extract_from_file", _boom)
    monkeypatch.setattr(ocr, "extract_from_panel", _boom)


def _staging(config) -> Path:
    return (config.temp_dir / ".health-extract").resolve()


def _pending_requests(config) -> list[dict]:
    with db.get_db(config.db_path) as conn:
        rows = db.shared_kv_list(conn, extract_requests.EXTRACT_NAMESPACE)
    return [json.loads(r["value"]) for r in rows if r["key"].startswith("request:")]


def _make_panel(ctx, *, with_source: bool = True) -> int:
    with health_db.connect(ctx.db_path) as conn:
        pid = health_db.insert_panel(
            conn, drawn_at="2026-01-02", lab_name="Lab", source_mime="image/png",
            draft=True,
        )
        if with_source:
            target = ctx.uploads_dir / str(pid) / "original.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\npanel")
            conn.execute(
                "UPDATE panels SET source_file = ? WHERE id = ?",
                (f"{pid}/original.png", pid),
            )
        conn.commit()
    return pid


class TestTheRoutesOnlyHandOff:
    """The reported defect: the model call ran wherever the upload landed."""

    @pytest.mark.parametrize("path,kind,upload", [
        ("/encounters/extract", "encounter", PDF),
        ("/immunizations/extract", "immunization", PNG),
    ])
    def test_an_import_upload_is_staged_and_not_extracted(
        self, client, config, ctx, monkeypatch, path, kind, upload,
    ):
        _forbid_in_web(monkeypatch)
        resp = client.post(PREFIX + path, files={"file": upload})

        assert resp.status_code == 202, resp.text
        [request] = _pending_requests(config)
        assert request["id"] == resp.json()["id"]
        assert request["kind"] == kind
        assert request["user_id"] == ctx.user_id
        # A bare name, staged where no task of the user's can write.
        assert "/" not in request["staged"]
        staged = _staging(config) / request["staged"]
        assert staged.read_bytes() == upload[1]

    def test_a_panel_extract_is_handed_off(self, client, config, ctx, monkeypatch):
        _forbid_in_web(monkeypatch)
        pid = _make_panel(ctx)

        resp = client.post(f"{PREFIX}/panels/{pid}/extract")

        assert resp.status_code == 202, resp.text
        [request] = _pending_requests(config)
        assert request["kind"] == "panel"
        assert request["panel_id"] == pid

    def test_an_unknown_panel_is_404_before_any_request(self, client, config):
        resp = client.post(f"{PREFIX}/panels/999/extract")
        assert resp.status_code == 404
        assert _pending_requests(config) == []

    def test_without_a_config_the_route_refuses(self, ctx):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota.health.routes import get_user_context, require_auth, router

        app = FastAPI()
        app.include_router(router, prefix=PREFIX)
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[get_user_context] = lambda: ctx
        resp = TestClient(app).post(PREFIX + "/encounters/extract", files={"file": PDF})
        assert resp.status_code == 503


class TestTheDaemonAnswers:
    def test_an_encounter_import_is_extracted_kept_and_cleaned_up(
        self, client, config, ctx, monkeypatch,
    ):
        captured: dict = {}

        def _fake(path: Path, mime: str, *, config=None, user_id="") -> dict:
            captured.update(path=path, mime=mime, user_id=user_id,
                            body=path.read_bytes())
            return {"rows": [{"encounter_date": "2026-01-02"}],
                    "mode": "text", "warnings": []}

        from istota.health import encounter_ocr
        monkeypatch.setattr(encounter_ocr, "extract_from_file", _fake)

        result = extract_via_daemon(client, config, "/encounters/extract",
                                    files={"file": PDF})

        assert result["rows"] == [{"encounter_date": "2026-01-02"}]
        assert captured["mime"] == "application/pdf"
        assert captured["user_id"] == ctx.user_id
        assert captured["body"] == PDF[1]
        assert captured["path"].parent == _staging(config)
        assert not captured["path"].exists()
        with health_db.connect(ctx.db_path) as conn:
            doc = health_db.get_document(conn, result["document_id"])
        assert doc.source == "import"
        assert doc.filename == "visit.pdf"
        assert (ctx.uploads_dir / doc.stored_path).read_bytes() == PDF[1]
        assert _pending_requests(config) == []

    def test_an_immunization_import_gets_the_refs(
        self, client, config, ctx, monkeypatch,
    ):
        seen: dict = {}

        def _fake(path, mime, refs, *, config=None, user_id=""):
            seen["refs"] = refs
            return {"rows": [], "mode": "vision", "warnings": []}

        from istota.health import immunization_ocr
        monkeypatch.setattr(immunization_ocr, "extract_from_file", _fake)

        result = extract_via_daemon(client, config, "/immunizations/extract",
                                    files={"file": PNG})

        assert seen["refs"], "the bundled immunization refs were not passed"
        assert isinstance(result["document_id"], int)

    def test_a_panel_is_extracted_from_the_users_own_uploads(
        self, client, config, ctx, monkeypatch,
    ):
        seen: dict = {}

        def _fake(c, panel, *, config=None):
            seen.update(uploads=c.uploads_dir, panel=panel.id)
            return {"biomarkers": [], "warnings": [], "raw_text": ""}

        from istota.health import ocr
        monkeypatch.setattr(ocr, "extract_from_panel", _fake)
        pid = _make_panel(ctx)

        result = extract_via_daemon(client, config, f"/panels/{pid}/extract")

        assert result == {"biomarkers": [], "warnings": [], "raw_text": ""}
        assert seen == {"uploads": ctx.uploads_dir, "panel": pid}

    def test_a_panel_deleted_meanwhile_is_answered_not_found(
        self, client, config, ctx,
    ):
        pid = _make_panel(ctx)
        resp = client.post(f"{PREFIX}/panels/{pid}/extract")
        with health_db.connect(ctx.db_path) as conn:
            conn.execute("DELETE FROM panels WHERE id = ?", (pid,))
            conn.commit()
        extract_requests.answer_pending(config)
        state = client.get(f"{PREFIX}/extract-requests/{resp.json()['id']}").json()
        assert state == {"state": "answered", "result": {"error": "panel not found"}}

    def test_a_storage_failure_still_returns_the_rows(
        self, client, config, monkeypatch,
    ):
        from istota.health import documents as health_documents
        from istota.health import encounter_ocr

        def _boom(*a, **kw):
            raise health_documents.DocumentError("disk on fire")

        monkeypatch.setattr(health_documents, "store_document", _boom)
        monkeypatch.setattr(
            encounter_ocr, "extract_from_file",
            lambda *a, **kw: {"rows": [{"encounter_date": "2026-06-29"}],
                              "mode": "vision", "warnings": []},
        )
        result = extract_via_daemon(client, config, "/encounters/extract",
                                    files={"file": PDF})
        assert result["document_id"] is None
        assert len(result["rows"]) == 1
        assert any("could not be kept" in w for w in result["warnings"])

    def test_an_extractor_that_raises_is_answered_and_the_queue_moves(
        self, client, config, ctx, monkeypatch,
    ):
        from istota.health import encounter_ocr

        def _raise(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(encounter_ocr, "extract_from_file", _raise)
        first = client.post(PREFIX + "/encounters/extract", files={"file": PDF})
        second = client.post(PREFIX + "/encounters/extract", files={"file": PDF})
        extract_requests.answer_pending(config)
        for resp in (first, second):
            state = client.get(f"{PREFIX}/extract-requests/{resp.json()['id']}").json()
            assert state == {"state": "answered",
                             "result": {"error": "extraction failed"}}
        assert list(_staging(config).iterdir()) == []

    def test_a_staged_name_outside_the_work_dir_is_refused(
        self, config, ctx, monkeypatch, tmp_path,
    ):
        from istota.health import encounter_ocr

        outside = tmp_path / "elsewhere.pdf"
        outside.write_bytes(b"secret")
        monkeypatch.setattr(
            encounter_ocr, "extract_from_file",
            lambda *a, **kw: pytest.fail("extracted a file outside the work dir"),
        )
        for name in ("../elsewhere.pdf", str(outside), ".."):
            request_id = extract_requests.request_extract(
                config, ctx.user_id, extract_requests.ENCOUNTER,
                staged=name, mime="application/pdf",
            )
            extract_requests.answer_pending(config)
            state = extract_requests.extract_state(config, ctx.user_id, request_id)
            assert state["result"] == {
                "error": "the uploaded file is no longer available"}
        assert outside.read_bytes() == b"secret"

    def test_a_request_stays_pending_until_answered(self, client, config):
        resp = client.post(PREFIX + "/encounters/extract", files={"file": PDF})
        request_id = resp.json()["id"]
        # Reading the queue does not take the request: a daemon restarted
        # mid-run must still find it.
        assert extract_requests.next_request(config)["id"] == request_id
        assert extract_requests.next_request(config)["id"] == request_id
        state = client.get(f"{PREFIX}/extract-requests/{request_id}").json()
        assert state == {"state": "pending"}

    def test_a_malformed_request_is_dropped_not_blocking(self, config, ctx):
        with db.get_db(config.db_path) as conn:
            db.shared_kv_set(conn, extract_requests.EXTRACT_NAMESPACE,
                             "request:bad", "not json", "web")
        request_id = extract_requests.request_extract(
            config, ctx.user_id, extract_requests.PANEL, panel_id=1,
        )
        assert extract_requests.next_request(config)["id"] == request_id


class TestTheStagedFileIsOutOfTaskReach:
    """Review of ISSUE-697: the user's work dir is bound read-write into their
    tasks, and the daemon reading from it is not sandboxed."""

    def test_the_staging_dir_is_not_the_users_work_dir(self, client, config, ctx):
        client.post(PREFIX + "/encounters/extract", files={"file": PDF})
        [request] = _pending_requests(config)
        user_dir = (config.temp_dir / ctx.user_id).resolve()
        assert not (user_dir / request["staged"]).exists()
        assert user_dir not in (_staging(config) / request["staged"]).parents

    def test_a_link_swapped_in_during_extraction_is_not_kept(
        self, client, config, ctx, monkeypatch, tmp_path,
    ):
        from istota.health import encounter_ocr

        secret = tmp_path / "secret"
        secret.write_bytes(b"not the user's document")

        def _swap(path: Path, mime: str, *, config=None, user_id="") -> dict:
            path.unlink()
            path.symlink_to(secret)
            return {"rows": [], "mode": "text", "warnings": []}

        monkeypatch.setattr(encounter_ocr, "extract_from_file", _swap)
        result = extract_via_daemon(client, config, "/encounters/extract",
                                    files={"file": PDF})

        with health_db.connect(ctx.db_path) as conn:
            doc = health_db.get_document(conn, result["document_id"])
        assert (ctx.uploads_dir / doc.stored_path).read_bytes() == PDF[1]

    def test_a_staged_link_is_refused(self, config, ctx, monkeypatch, tmp_path):
        from istota.health import encounter_ocr

        secret = tmp_path / "secret"
        secret.write_bytes(b"secret")
        (extract_requests._staging_dir(config) / "upload-link.pdf").symlink_to(secret)
        monkeypatch.setattr(
            encounter_ocr, "extract_from_file",
            lambda *a, **kw: pytest.fail("extracted through a link"),
        )
        request_id = extract_requests.request_extract(
            config, ctx.user_id, extract_requests.ENCOUNTER,
            staged="upload-link.pdf", mime="application/pdf",
        )
        extract_requests.answer_pending(config)
        state = extract_requests.extract_state(config, ctx.user_id, request_id)
        assert state["result"] == {"error": "the uploaded file is no longer available"}


class TestThePerUserCap:
    def test_a_user_past_the_cap_gets_429_and_nothing_is_staged(
        self, client, config,
    ):
        for _ in range(extract_requests.MAX_PENDING_PER_USER):
            assert client.post(PREFIX + "/encounters/extract",
                               files={"file": PDF}).status_code == 202
        resp = client.post(PREFIX + "/encounters/extract", files={"file": PDF})

        assert resp.status_code == 429
        assert len(_pending_requests(config)) == extract_requests.MAX_PENDING_PER_USER
        assert len(list(_staging(config).iterdir())) == extract_requests.MAX_PENDING_PER_USER

    def test_another_users_queue_does_not_count(self, client, config):
        for _ in range(extract_requests.MAX_PENDING_PER_USER):
            extract_requests.request_extract(
                config, "bob", extract_requests.PANEL, panel_id=1,
            )
        resp = client.post(PREFIX + "/encounters/extract", files={"file": PDF})
        assert resp.status_code == 202


class TestThePollRoute:
    def test_another_users_request_is_unknown(self, client, config):
        request_id = extract_requests.request_extract(
            config, "bob", extract_requests.PANEL, panel_id=1,
        )
        state = client.get(f"{PREFIX}/extract-requests/{request_id}").json()
        assert state == {"state": "unknown"}

    def test_an_answer_is_removed_once_its_owner_reads_it(
        self, client, config, ctx,
    ):
        pid = _make_panel(ctx, with_source=False)
        request_id = client.post(f"{PREFIX}/panels/{pid}/extract").json()["id"]
        with health_db.connect(ctx.db_path) as conn:
            conn.execute("DELETE FROM panels WHERE id = ?", (pid,))
            conn.commit()
        extract_requests.answer_pending(config)

        first = client.get(f"{PREFIX}/extract-requests/{request_id}").json()
        assert first["state"] == "answered"
        assert client.get(f"{PREFIX}/extract-requests/{request_id}").json() == {
            "state": "unknown"}
        with db.get_db(config.db_path) as conn:
            assert db.shared_kv_list(conn, extract_requests.EXTRACT_NAMESPACE) == []

    def test_an_unknown_id_is_unknown(self, client):
        assert client.get(f"{PREFIX}/extract-requests/nope").json() == {
            "state": "unknown"}


class TestTheGate:
    def test_the_gate_answers_on_a_thread(self, client, config, monkeypatch):
        from istota import scheduler
        from istota.health import encounter_ocr

        monkeypatch.setattr(
            encounter_ocr, "extract_from_file",
            lambda *a, **kw: {"rows": [], "mode": "text", "warnings": []},
        )
        resp = client.post(PREFIX + "/encounters/extract", files={"file": PDF})

        inflight: dict = {}
        scheduler.poll_health_extract_requests(config, inflight)
        thread = inflight["health-extract-request"]
        thread.join(timeout=10)

        state = client.get(f"{PREFIX}/extract-requests/{resp.json()['id']}").json()
        assert state["state"] == "answered"

    def test_the_gate_spawns_nothing_when_idle(self, config):
        from istota import scheduler

        inflight: dict = {}
        scheduler.poll_health_extract_requests(config, inflight)
        assert inflight == {}

    def test_the_gate_is_in_the_daemon_table(self, config):
        from istota import scheduler

        gates = scheduler.build_interval_gates(
            config, pool=None, background_checks={}, doctor_state={},
            pressure_state={}, backup_state={},
        )
        assert "health-extract-request" in {g.name for g in gates}
