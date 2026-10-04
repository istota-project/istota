"""The operator persona: digests, the conffile sync, and the last good copy."""

import json
import os
from pathlib import Path

import pytest

from istota import db
from istota.config import Config
from istota.prompts import persona
from istota.prompts.persona import (
    KV_NAMESPACE,
    SHIPPED_PERSONA_DIGESTS,
    persona_digest,
    read_last_good,
    sync_operator_persona,
)

REPO = Path(__file__).resolve().parent.parent
SHIPPED = "You are {BOT_NAME}.\n\nShipped character, current version.\n"
OLD_SHIPPED = "You are {BOT_NAME}.\n\nShipped character, an older version.\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    skills_dir = config_dir / "skills"
    skills_dir.mkdir(parents=True)
    (config_dir / "persona.md").write_text(SHIPPED)
    root = tmp_path / "mount"
    root.mkdir()
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(
        skills_dir=skills_dir,
        bundled_skills_dir=tmp_path / "_empty_bundled",
        workspace_path=root,
        db_path=db_path,
    )
    # The test's shipped texts stand in for git history.
    monkeypatch.setattr(
        persona,
        "SHIPPED_PERSONA_DIGESTS",
        frozenset({persona_digest(SHIPPED), persona_digest(OLD_SHIPPED)}),
    )
    return config, root


def _state(config):
    with db.get_db(config.db_path) as conn:
        row = db.shared_kv_get(conn, KV_NAMESPACE, "state")
    return None if row is None else json.loads(row["value"])


def _record_shipped_digest(config, digest):
    with db.get_db(config.db_path) as conn:
        db.shared_kv_set(
            conn, KV_NAMESPACE, "state", json.dumps({"shipped_digest": digest}), "test",
        )


def _tree(root):
    return sorted(
        (str(p.relative_to(root)), p.read_bytes() if p.is_file() else None)
        for p in root.rglob("*")
    )


class TestDigest:
    def test_line_endings_and_surrounding_whitespace_do_not_count(self):
        base = persona_digest("a\nb")
        assert persona_digest("a\r\nb") == base
        assert persona_digest("a\nb\n") == base
        assert persona_digest("  \n a\nb \n\n") == base

    def test_a_changed_word_does(self):
        assert persona_digest("a\nb") != persona_digest("a\nc")

    def test_the_current_shipped_persona_is_in_the_set(self):
        text = (REPO / "config" / "persona.md").read_text(encoding="utf-8")
        digest = persona_digest(text)
        assert digest in SHIPPED_PERSONA_DIGESTS, (
            "config/persona.md changed without its digest being added to "
            f"SHIPPED_PERSONA_DIGESTS in src/istota/prompts/persona.py: add {digest!r}"
        )

    def test_every_shipped_version_is_kept(self):
        # Twelve versions as of 2026-10-04; the set only ever grows, because
        # an older one is what an unedited per-user copy can still hold.
        assert len(SHIPPED_PERSONA_DIGESTS) >= 12


class TestSync:
    def test_missing_file_is_written(self, setup):
        config, root = setup
        result = sync_operator_persona(config)
        assert result.action == "wrote"
        assert (root / "PERSONA.md").read_text() == SHIPPED

    def test_empty_file_is_left_alone(self, setup):
        config, root = setup
        (root / "PERSONA.md").write_text("  \n")
        result = sync_operator_persona(config)
        assert result.action == "empty"
        assert (root / "PERSONA.md").read_text() == "  \n"
        assert not (root / "PERSONA.md.shipped").exists()
        assert "last_good_text" not in (_state(config) or {})

    def test_an_unedited_older_version_is_upgraded(self, setup):
        config, root = setup
        (root / "PERSONA.md").write_text(OLD_SHIPPED.replace("\n", "\r\n"))
        (root / "PERSONA.md.shipped").write_text("stale")
        result = sync_operator_persona(config)
        assert result.action == "upgraded"
        assert (root / "PERSONA.md").read_text() == SHIPPED
        assert not (root / "PERSONA.md.shipped").exists()

    def test_the_unedited_current_version_is_unchanged(self, setup):
        config, root = setup
        (root / "PERSONA.md").write_text(SHIPPED + "\n")
        (root / "PERSONA.md.shipped").write_text("stale")
        result = sync_operator_persona(config)
        assert result.action == "unchanged"
        assert (root / "PERSONA.md").read_text() == SHIPPED + "\n"
        assert not (root / "PERSONA.md.shipped").exists()

    def test_an_edit_is_kept_and_the_moved_shipped_text_written_beside(self, setup):
        config, root = setup
        _record_shipped_digest(config, persona_digest(OLD_SHIPPED))
        edited = b"You are {BOT_NAME}.\r\n\r\nThe operator's own words.\r\n"
        (root / "PERSONA.md").write_bytes(edited)
        result = sync_operator_persona(config)
        assert result.action == "wrote_shipped_beside"
        assert (root / "PERSONA.md").read_bytes() == edited
        assert (root / "PERSONA.md.shipped").read_text() == SHIPPED
        assert _state(config)["shipped_digest"] == persona_digest(SHIPPED)

    def test_first_sync_on_an_edit_writes_the_shipped_text_beside(self, setup):
        config, root = setup
        (root / "PERSONA.md").write_text("Edited.")
        result = sync_operator_persona(config)
        assert result.action == "wrote_shipped_beside"
        assert (root / "PERSONA.md.shipped").read_text() == SHIPPED

    def test_an_edit_with_the_shipped_text_unchanged_is_kept(self, setup):
        config, root = setup
        _record_shipped_digest(config, persona_digest(SHIPPED))
        (root / "PERSONA.md").write_text("Edited.")
        result = sync_operator_persona(config)
        assert result.action == "kept_edited"
        assert (root / "PERSONA.md").read_text() == "Edited."
        assert not (root / "PERSONA.md.shipped").exists()

    def test_a_second_sync_after_writing_beside_keeps_the_edit_quietly(self, setup):
        config, root = setup
        (root / "PERSONA.md").write_text("Edited.")
        assert sync_operator_persona(config).action == "wrote_shipped_beside"
        assert sync_operator_persona(config).action == "kept_edited"
        assert (root / "PERSONA.md.shipped").read_text() == SHIPPED

    def test_a_symlink_is_refused_and_its_target_untouched(self, setup, tmp_path):
        config, root = setup
        target = tmp_path / "elsewhere.md"
        target.write_text("not the bot's")
        (root / "PERSONA.md").symlink_to(target)
        result = sync_operator_persona(config)
        assert result.action == "refused"
        assert target.read_text() == "not the bot's"
        assert (root / "PERSONA.md").is_symlink()
        assert not (root / "PERSONA.md.shipped").exists()

    def test_a_fifo_is_refused_without_blocking(self, setup):
        from .support.blocking import fails_if_it_blocks

        config, root = setup
        os.mkfifo(root / "PERSONA.md")
        with fails_if_it_blocks(what="sync_operator_persona"):
            result = sync_operator_persona(config)
        assert result.action == "refused"

    def test_an_over_cap_file_is_refused(self, setup):
        config, root = setup
        big = "x" * (persona.PERSONA_MAX_BYTES + 1)
        (root / "PERSONA.md").write_text(big)
        assert sync_operator_persona(config).action == "refused"
        assert (root / "PERSONA.md").read_text() == big

    def test_a_missing_root_is_unavailable_and_not_created(self, setup):
        config, root = setup
        root.rmdir()
        result = sync_operator_persona(config)
        assert result.action == "root_unavailable"
        assert not root.exists()

    def test_no_workspace(self, setup):
        config, _root = setup
        config.workspace_path = None
        assert sync_operator_persona(config).action == "no_workspace"

    def test_an_unreadable_shipped_file_is_refused(self, setup, tmp_path):
        config, root = setup
        (tmp_path / "config" / "persona.md").unlink()
        assert sync_operator_persona(config).action == "refused"
        assert not (root / "PERSONA.md").exists()

    @pytest.mark.parametrize("planted", [None, "old", "edited"])
    def test_dry_run_writes_nothing(self, setup, planted):
        config, root = setup
        if planted == "old":
            (root / "PERSONA.md").write_text(OLD_SHIPPED)
            (root / "PERSONA.md.shipped").write_text("stale")
        elif planted == "edited":
            (root / "PERSONA.md").write_text("Edited.")
        before = _tree(root)
        result = sync_operator_persona(config, dry_run=True)
        assert result.action in {"wrote", "upgraded", "wrote_shipped_beside"}
        assert _tree(root) == before
        assert _state(config) is None

    def test_a_missing_database_is_not_created(self, setup):
        config, root = setup
        config.db_path.unlink()
        assert sync_operator_persona(config).action == "wrote"
        assert not config.db_path.exists()


class TestLastGood:
    def test_recorded_after_a_sync_and_kept_when_the_file_breaks(self, setup):
        config, root = setup
        (root / "PERSONA.md").write_text("Edited.")
        sync_operator_persona(config)
        state = _state(config)
        assert state["last_good_text"] == "Edited."
        assert state["last_good_digest"] == persona_digest("Edited.")
        assert state["updated_at"]

        (root / "PERSONA.md").unlink()
        (root / "PERSONA.md").symlink_to(root / "nowhere")
        assert sync_operator_persona(config).action == "refused"
        assert read_last_good(config) == "Edited."

    def test_the_written_shipped_text_becomes_last_good(self, setup):
        config, _root = setup
        sync_operator_persona(config)
        assert read_last_good(config) == SHIPPED

    def test_none_when_nothing_was_recorded(self, setup):
        config, _root = setup
        assert read_last_good(config) is None

    def test_none_when_there_is_no_database(self, setup):
        config, _root = setup
        config.db_path.unlink()
        assert read_last_good(config) is None
        assert not config.db_path.exists()

    def test_none_on_a_malformed_row(self, setup):
        config, _root = setup
        with db.get_db(config.db_path) as conn:
            db.shared_kv_set(conn, KV_NAMESPACE, "state", "[not a dict", "test")
        assert read_last_good(config) is None


def test_the_namespace_is_reserved():
    from istota.sandbox.kv_namespaces import is_reserved_namespace

    assert is_reserved_namespace(KV_NAMESPACE)
