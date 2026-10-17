"""Health's other readers and deleters stay inside the user's own tree.

The uploads tree is in the user's workspace, which their tasks can write
(ISSUE-781). Each case links a directory of alice's tree to the same place in
bob's and requires that nothing of bob's is read or deleted.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import UserConfig
from istota.health import db as health_db
from istota.health import documents as health_documents
from istota.health import ocr as health_ocr
from istota.health._loader import resolve_for_user
from istota.health._migrate import ensure_initialised
from tests.support.health_extract import PREFIX, handoff_app

ALICE = b"%PDF-1.4 alice's own report"
BOB = b"%PDF-1.4 BOB-SECRET"


@pytest.fixture
def two_users(make_config):
    config = make_config(users={"alice": UserConfig(), "bob": UserConfig()})
    db.init_db(config.db_path)
    alice = resolve_for_user("alice", config)
    bob = resolve_for_user("bob", config)
    ensure_initialised(alice)
    ensure_initialised(bob)
    return config, alice, bob


def _panel(ctx, source_file: str, raw: bytes, mime: str = "application/pdf") -> int:
    with health_db.connect(ctx.db_path) as conn:
        pid = health_db.insert_panel(
            conn, drawn_at="2026-05-08", source_file=source_file, source_mime=mime,
        )
        conn.commit()
    leaf = ctx.uploads_dir / source_file
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(raw)
    return pid


def _link_to(path: Path, target: Path) -> None:
    """Replace ``path`` with a link to ``target``, as a task in alice's tree could."""
    path.rename(path.with_name(path.name + ".real"))
    path.symlink_to(target, target_is_directory=True)


class TestPanelExtraction:
    def _get(self, ctx, pid):
        with health_db.connect(ctx.db_path) as conn:
            return health_db.get_panel(conn, pid)

    def test_text_mode_does_not_read_through_a_linked_health_dir(self, two_users):
        _config, alice, bob = two_users
        pid = _panel(alice, "1/original.pdf", ALICE)
        _panel(bob, "1/original.pdf", BOB)
        panel = self._get(alice, pid)
        _link_to(alice.data_dir, bob.data_dir)

        def read(path):
            return Path(path).read_bytes().decode() * 20

        with patch.object(health_ocr, "_pdftotext", side_effect=read), \
                patch.object(health_ocr, "_call_brain", return_value=None):
            result = health_ocr.extract_from_panel(alice, panel)
        assert "BOB-SECRET" not in result["raw_text"]
        assert "BOB-SECRET" not in (self._get(alice, pid).ocr_text or "")

    def test_vision_mode_does_not_bind_a_file_through_a_linked_health_dir(
        self, two_users,
    ):
        _config, alice, bob = two_users
        pid = _panel(alice, "1/original.png", ALICE, "image/png")
        _panel(bob, "1/original.png", BOB, "image/png")
        panel = self._get(alice, pid)
        _link_to(alice.data_dir, bob.data_dir)
        seen = []

        def brain(prompt, config, *, read_path=None, user_id=""):
            if read_path is not None:
                seen.append(Path(read_path).read_bytes())
            return None

        with patch.object(health_ocr, "_call_brain", side_effect=brain):
            health_ocr.extract_from_panel(alice, panel)
        assert BOB not in seen

    def test_the_extractor_reads_a_daemon_side_copy_and_it_is_removed(
        self, two_users,
    ):
        config, alice, _bob = two_users
        pid = _panel(alice, "1/original.png", ALICE, "image/png")
        panel = self._get(alice, pid)
        seen = {}

        def brain(prompt, cfg, *, read_path=None, user_id=""):
            seen["path"] = Path(read_path)
            seen["bytes"] = Path(read_path).read_bytes()
            return None

        with patch.object(health_ocr, "_call_brain", side_effect=brain):
            health_ocr.extract_from_panel(alice, panel, config=config)
        assert seen["bytes"] == ALICE
        assert not seen["path"].is_relative_to(alice.workspace_root)
        assert not seen["path"].exists()


class TestStrandedDirSweep:
    def _stranded(self, ctx, doc_id: int) -> Path:
        stranded = ctx.uploads_dir / "documents" / str(doc_id)
        stranded.mkdir(parents=True, exist_ok=True)
        (stranded / "scan.pdf").write_bytes(BOB)
        old = time.time() - 3 * 86400
        os.utime(stranded, (old, old))
        return stranded

    def test_does_not_delete_through_a_linked_documents_dir(self, two_users):
        _config, alice, bob = two_users
        bobs = self._stranded(bob, 7)
        (alice.uploads_dir / "documents").mkdir(parents=True, exist_ok=True)
        _link_to(alice.uploads_dir / "documents", bob.uploads_dir / "documents")
        with health_db.connect(alice.db_path) as conn:
            health_documents.sweep_orphan_documents(conn, alice)
        assert (bobs / "scan.pdf").read_bytes() == BOB

    def test_still_removes_a_stranded_dir_of_ones_own(self, two_users):
        _config, alice, _bob = two_users
        own = self._stranded(alice, 9)
        with health_db.connect(alice.db_path) as conn:
            health_documents.sweep_orphan_documents(conn, alice)
        assert not own.exists()


class TestPanelDelete:
    def test_does_not_delete_through_a_linked_uploads_dir(self, two_users):
        config, alice, bob = two_users
        client, alice = handoff_app(config, "alice")
        pid = _panel(alice, "1/original.pdf", ALICE)
        _panel(bob, "1/original.pdf", BOB)
        _link_to(alice.uploads_dir, bob.uploads_dir)
        resp = client.delete(f"{PREFIX}/panels/{pid}")
        assert resp.status_code == 200
        assert (bob.uploads_dir / "1" / "original.pdf").read_bytes() == BOB

    def test_removes_ones_own_panel_dir(self, two_users):
        config, _alice, _bob = two_users
        client, alice = handoff_app(config, "alice")
        pid = _panel(alice, "1/original.pdf", ALICE)
        assert client.delete(f"{PREFIX}/panels/{pid}").status_code == 200
        assert not (alice.uploads_dir / "1").exists()


class TestTheAnchorWithAUserRoot:
    def test_uploads_outside_the_user_root_are_refused(self, two_users):
        """A context whose uploads resolved outside its user root (a bot
        directory flipped between the loader's two resolves) must not fall
        back to anchoring at the parent of that workspace."""
        _config, alice, bob = two_users
        _panel(bob, "1/original.pdf", BOB)
        odd = replace(
            alice,
            workspace_root=bob.workspace_root,
            data_dir=bob.data_dir,
            uploads_dir=bob.uploads_dir,
        )
        assert odd.user_root == alice.user_root
        try:
            fd = health_documents.open_upload(odd, "1/original.pdf")
        except ValueError:
            fd = None
        if fd is not None:
            try:
                served = os.read(fd, 100)
            finally:
                os.close(fd)
            assert served != BOB
        with pytest.raises(ValueError):
            health_documents.uploads_walk(odd, "1")


class TestTheExtractionCopyIsBounded:
    """A task can make a panel source a sparse file of any size; reading it
    whole put that much into the daemon's memory, and a request is kept until
    answered, so the crash repeated on every tick."""

    def test_an_oversized_source_is_refused_and_the_request_answered(
        self, two_users,
    ):
        from istota.health import extract_requests

        config, alice, _bob = two_users
        pid = _panel(alice, "1/original.pdf", ALICE)
        leaf = alice.uploads_dir / "1" / "original.pdf"
        with open(leaf, "r+b") as fh:
            fh.truncate(1 << 30)
        request_id = extract_requests.request_extract(
            config, "alice", extract_requests.PANEL, panel_id=pid,
        )
        staged = []
        real_stage = extract_requests.stage_upload

        def watching(*args, **kwargs):
            staged.append(args)
            return real_stage(*args, **kwargs)

        with patch.object(extract_requests, "stage_upload", side_effect=watching), \
                patch.object(health_ocr, "_call_brain", return_value=None):
            extract_requests.answer_pending(config)
        state = extract_requests.extract_state(config, "alice", request_id)
        assert state["state"] == extract_requests.ANSWERED
        assert "error" in state["result"]
        assert staged == []

    def test_the_copy_is_streamed_not_read_whole(self, two_users):
        config, alice, _bob = two_users
        pid = _panel(alice, "1/original.png", ALICE, "image/png")
        with health_db.connect(alice.db_path) as conn:
            panel = health_db.get_panel(conn, pid)
        seen = {}

        def brain(prompt, cfg, *, read_path=None, user_id=""):
            seen["bytes"] = Path(read_path).read_bytes()
            return None

        real_fdopen = os.fdopen

        def no_whole_read(fd, *args, **kwargs):
            fh = real_fdopen(fd, *args, **kwargs)
            if "r" in (args[0] if args else kwargs.get("mode", "r")):
                def refuse(*a, **k):
                    raise AssertionError("the source was read whole")
                fh.read = refuse
            return fh

        with patch.object(health_ocr.os, "fdopen", side_effect=no_whole_read), \
                patch.object(health_ocr, "_call_brain", side_effect=brain):
            health_ocr.extract_from_panel(alice, panel, config=config)
        assert seen["bytes"] == ALICE
