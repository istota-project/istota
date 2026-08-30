"""Tests for istota.skills.memory — runtime memory CLI."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from istota import db
from istota.memory.curation.audit import (
    AUDIT_NAMESPACE,
    CURATION_NAMESPACE,
    LAST_SEEN_KEY,
)
from istota.skills.memory import main as memory_main


SEED_USER_MD = (
    "# User Memory\n"
    "\n"
    "## Notes\n"
    "\n"
    "- Existing note 1\n"
    "- Existing note 2\n"
    "\n"
    "## Communication style\n"
    "\n"
    "- Prefers short replies\n"
    "\n"
    "### Email\n"
    "\n"
    "- Always sign off with name\n"
    "- Use plain text\n"
    "\n"
)


def _setup_user(tmp_path, monkeypatch, user_id="alice", bot_dir="istota"):
    mount = tmp_path / "mount"
    user_md_dir = mount / "Users" / user_id / bot_dir / "config"
    user_md_dir.mkdir(parents=True)
    user_md = user_md_dir / "USER.md"
    user_md.write_text(SEED_USER_MD)
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
    monkeypatch.setenv("ISTOTA_USER_ID", user_id)
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", bot_dir)
    # The audit trail and the USER.md fingerprint are rows in the framework
    # KV store, so the CLI needs the DB the skill proxy hands every skill CLI.
    db_path = tmp_path / "istota.db"
    if not db_path.exists():
        db.init_db(db_path)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    return user_md


def _audit_entries(tmp_path, user_id="alice"):
    """The audit rows the CLI wrote, oldest first."""
    with db.get_db(tmp_path / "istota.db") as conn:
        rows = db.kv_list(conn, user_id, AUDIT_NAMESPACE)
    return [json.loads(r["value"]) for r in rows]


def _last_seen(tmp_path, user_id="alice"):
    with db.get_db(tmp_path / "istota.db") as conn:
        return db.kv_get(conn, user_id, CURATION_NAMESPACE, LAST_SEEN_KEY)


class TestAppend:
    def test_append_to_existing_heading(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Brand new bullet"])
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "status": "ok",
            "outcome": "applied",
            "heading": "Notes",
            "line": "Brand new bullet",
        }
        body = user_md.read_text()
        assert "- Brand new bullet" in body
        assert "## Notes" in body

    def test_append_to_missing_heading_returns_error(
        self, tmp_path, monkeypatch, capsys
    ):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "DoesNotExist", "--line", "Hi"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert out["error"] == "heading_missing"
        assert "available_headings" in out
        assert "Notes" in out["available_headings"]
        # File untouched.
        assert user_md.read_text() == before

    def test_append_duplicate_is_noop(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        memory_main(["append", "--heading", "Notes", "--line", "Existing note 1"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["outcome"] == "noop_dup"
        assert user_md.read_text() == before

    def test_append_writes_audit_log(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Audit me"])
        capsys.readouterr()
        entries = _audit_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["source"] == "runtime"
        assert entries[0]["entry_kind"] == "batch"
        assert entries[0]["applied"][0]["outcome"] == "applied"

    def test_append_writes_no_sidecar_files(self, tmp_path, monkeypatch, capsys):
        """The whole point of the move: the user's config folder holds the
        documents they wrote and nothing the machine keeps for itself."""
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "Audit me"])
        capsys.readouterr()
        assert sorted(p.name for p in user_md.parent.iterdir()) == ["USER.md"]


class TestAddHeading:
    def test_creates_new_heading(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "add-heading", "--heading", "Travel",
            "--line", "Default vehicle is motorcycle",
            "--line", "Prefer overnight trains",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "## Travel" in body
        assert "Default vehicle is motorcycle" in body
        assert "Prefer overnight trains" in body

    def test_duplicate_heading_rejected(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["add-heading", "--heading", "Notes", "--line", "Already"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_exists"


class TestRemove:
    def test_removes_unique_match(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["remove", "--heading", "Notes", "--match", "note 1"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "Existing note 1" not in user_md.read_text()
        assert "Existing note 2" in user_md.read_text()

    def test_removes_bullet_under_subsection(self, tmp_path, monkeypatch, capsys):
        # Removal now reaches into `### subsections` so stale bullets there
        # can be pruned (previously rejected as match_in_subsection).
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "remove", "--heading", "Communication style",
            "--match", "sign off",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "Always sign off with name" not in body
        # The subheading and the other section content survive. `### Email`
        # keeps a second bullet, so this stays a test about scoping rather than
        # about the delete-on-empty rule (tests/test_curation_subheading_removal.py).
        assert "### Email" in body
        assert "Use plain text" in body
        assert "Prefers short replies" in body


class TestReplaceCli:
    def test_replace_unique_bullet(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "replace", "--heading", "Notes",
            "--match", "note 1", "--line", "Reworded note one",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "- Reworded note one" in body
        assert "Existing note 1" not in body

    def test_replace_in_subsection(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "replace", "--heading", "Communication style",
            "--match", "sign off", "--line", "Sign off with first name only",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "Sign off with first name only" in body
        assert "Always sign off with name" not in body


class TestRemoveHeadingCli:
    def test_remove_heading_drops_section(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["remove-heading", "--heading", "Notes"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "## Notes" not in body
        assert "## Communication style" in body

    def test_remove_heading_missing(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["remove-heading", "--heading", "Nope"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_missing"


class TestAppendSubheadingCli:
    def test_append_under_subheading(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main([
            "append", "--heading", "Communication style",
            "--subheading", "Email", "--line", "Use plain text, no HTML",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        body = user_md.read_text()
        assert "Use plain text, no HTML" in body
        # Lands under the Email subsection, after its existing bullet.
        assert body.index("Always sign off") < body.index("Use plain text")


class TestShowHeadings:
    def test_headings_list(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        memory_main(["headings"])
        out = json.loads(capsys.readouterr().out)
        assert out["headings"] == ["Notes", "Communication style"]

    def test_show_one_heading(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        memory_main(["show", "--heading", "Notes"])
        out = capsys.readouterr().out
        assert "## Notes" in out
        assert "Existing note 1" in out

    def test_show_missing_heading(self, tmp_path, monkeypatch, capsys):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["show", "--heading", "Nope"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "heading_missing"
        assert "Notes" in out["available_headings"]


class TestLockAnchorPlacement:
    def test_anchor_goes_under_deferred_dir_not_tmp(
        self, tmp_path, monkeypatch, capsys
    ):
        # With ISTOTA_DEFERRED_DIR set (the daemon path), the flock anchor must
        # live under that per-user dir — it's bind-mounted into the sandbox, so
        # a sandboxed CLI and the host curator share the same inode.
        user_md = _setup_user(tmp_path, monkeypatch)
        deferred = tmp_path / "deferred" / "alice"
        deferred.mkdir(parents=True)
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
        memory_main(["append", "--heading", "Notes", "--line", "Anchored"])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "- Anchored" in user_md.read_text()
        # Anchor created under the deferred dir, not as a mount sibling.
        anchors = list((deferred / ".md-locks").glob("USER.md.*.lock"))
        assert anchors, "expected a flock anchor under ISTOTA_DEFERRED_DIR/.md-locks"
        assert not (user_md.parent / "USER.md.lock").exists()


class TestChannel:
    def _setup(self, tmp_path, monkeypatch, token="room-1"):
        mount = tmp_path / "mount"
        ch_dir = mount / "Channels" / token
        ch_dir.mkdir(parents=True)
        ch_md = ch_dir / "CHANNEL.md"
        ch_md.write_text("# Channel Memory\n\n## Decisions\n\n- Use Postgres\n\n")
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        # A reachable store that stays empty is the assertion; an absent one
        # would make "channel writes are not audited" true for the wrong
        # reason.
        db_path = tmp_path / "istota.db"
        if not db_path.exists():
            db.init_db(db_path)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        return ch_md

    def test_channel_append(self, tmp_path, monkeypatch, capsys):
        ch_md = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room-1")
        memory_main([
            "append", "--heading", "Decisions",
            "--line", "Use Redis for queues", "--channel", "room-1",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "Use Redis for queues" in ch_md.read_text()

    def test_channel_token_mismatch_refused(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch, token="room-1")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room-1")
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--heading", "Decisions", "--line", "X",
                "--channel", "room-OTHER",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel token mismatch — refusing cross-channel write"

    def test_channel_refused_when_env_token_unset(self, tmp_path, monkeypatch, capsys):
        # ISSUE-075: empty/unset ISTOTA_CONVERSATION_TOKEN must refuse --channel,
        # not pass through. Otherwise prompt-injected non-Talk tasks (email,
        # briefing, scheduled, cron, subtask) can write into any channel's
        # CHANNEL.md by passing --channel <victim_token>.
        self._setup(tmp_path, monkeypatch, token="room-1")
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--heading", "Decisions", "--line", "X",
                "--channel", "room-1",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel write requires ISTOTA_CONVERSATION_TOKEN"

    def test_channel_refused_when_env_token_empty(self, tmp_path, monkeypatch, capsys):
        # Same gap: env var present but empty string also short-circuited the
        # original guard. Treat empty as unset.
        self._setup(tmp_path, monkeypatch, token="room-1")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "")
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--heading", "Decisions", "--line", "X",
                "--channel", "room-1",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel write requires ISTOTA_CONVERSATION_TOKEN"

    def test_channel_write_skips_audit_log(self, tmp_path, monkeypatch, capsys):
        # ISSUE-076: channel writes intentionally skip the audit log + the
        # USER.md last_seen sidecar (no per-channel audit infrastructure).
        # Lock in the asymmetry so a future change can't quietly start
        # writing channel-write entries to the user-scoped audit file.
        ch_md = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room-1")
        # Materialize a USER.md so the audit path is resolvable and the
        # absence of a write is meaningful (not just a missing parent dir).
        user_md_dir = tmp_path / "mount" / "Users" / "alice" / "istota" / "config"
        user_md_dir.mkdir(parents=True, exist_ok=True)
        (user_md_dir / "USER.md").write_text("# User Memory\n\n")

        memory_main([
            "append", "--heading", "Decisions",
            "--line", "Use Redis for queues", "--channel", "room-1",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"
        assert "Use Redis for queues" in ch_md.read_text()

        # Channel writes are not audited and do not move the USER.md
        # fingerprint. Asserted against the store rather than against absent
        # sidecar files, which nothing writes any more and which would
        # therefore be a check that cannot fail.
        assert _audit_entries(tmp_path) == []
        assert _last_seen(tmp_path) is None


class TestAtomicWrite:
    def test_no_partial_write_on_reject(self, tmp_path, monkeypatch, capsys):
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        # multiple_matches: "note" appears in both bullets
        with pytest.raises(SystemExit):
            memory_main(["remove", "--heading", "Notes", "--match", "note"])
        capsys.readouterr()
        assert user_md.read_text() == before


class TestBotDirFallback:
    """ISSUE-077: when ISTOTA_BOT_DIR_NAME is unset, refuse to guess between
    multiple candidate bot dirs. Single-candidate fallback still works."""

    def _setup_two_bots(self, tmp_path, monkeypatch, user_id="alice"):
        mount = tmp_path / "mount"
        for bot_dir in ("istota", "helper"):
            d = mount / "Users" / user_id / bot_dir / "config"
            d.mkdir(parents=True)
            (d / "USER.md").write_text(SEED_USER_MD)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        return mount

    def test_multiple_candidates_refused(self, tmp_path, monkeypatch, capsys):
        self._setup_two_bots(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "X"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "multiple bot dirs" in out["error"]
        assert sorted(out["candidates"]) == ["helper", "istota"]

    def test_single_candidate_used(self, tmp_path, monkeypatch, capsys):
        mount = tmp_path / "mount"
        d = mount / "Users" / "alice" / "istota" / "config"
        d.mkdir(parents=True)
        user_md = d / "USER.md"
        user_md.write_text(SEED_USER_MD)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        memory_main(["append", "--heading", "Notes", "--line", "Inferred"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert "- Inferred" in user_md.read_text()

    def test_zero_candidates_refused(self, tmp_path, monkeypatch, capsys):
        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_BOT_DIR_NAME", raising=False)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "X"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "could not infer" in out["error"]


# --------------------------------------------------------- per-skill overlays

class TestOverlayVerbsAreGone:
    """An overlay is skill configuration the user authors, not memory the
    model accumulates (ISSUE-343).

    The bullet-op vocabulary reached about 20% of a real overlay: `remove`
    re-parented the fenced block that belonged to the bullet it popped, a bare
    `append` landed above the file's own first heading, and prose and fences
    could not be written at all — `validate_appendable_line` refuses a newline,
    correctly, so a code block was unwritable by construction. The verbs went
    rather than growing a whole-file verb beside them.

    Nothing was lost in the going. The audit trail they wrote had no reader
    anywhere in the repo, and `_update_last_seen` deliberately declined to
    stamp after an overlay write, so the sanctioned path bought neither a
    readable history nor a signal when it was skipped. The loader is the
    enforcement point and it is total: a hand edit cannot corrupt a prompt, it
    can only produce a file that does not bind, and `istota-skill skills
    overlays` says so by name and with a reason.
    """

    WRITE_VERBS = ["append", "remove", "replace", "remove-subheading"]

    @pytest.mark.parametrize(
        "verb",
        WRITE_VERBS + ["add-heading", "remove-heading", "show", "headings"],
    )
    def test_every_verb_redirects_a_skill_flag(
        self, tmp_path, monkeypatch, capsys, verb
    ):
        """Refused in this CLI's envelope, on stdout — not by argparse.

        The command that wrote overlays is durable in a way deleting the code
        does not reach: it is in live `config/skills/*.md` files, in USER.md,
        in conversation history, and it was in the development-rules doc until
        this change. Taking `--skill` off the parser makes every one of those
        an argparse exit 2 with a usage dump on stderr and nothing on stdout,
        which the model reads as an empty answer — the exact failure
        `_require_heading` already exists to avoid.
        """
        _setup_user(tmp_path, monkeypatch)
        argv = [verb, "--skill", "developer"]
        if verb in ("append", "replace", "add-heading"):
            argv += ["--line", "x"]
        if verb in ("remove", "replace"):
            argv += ["--match", "x"]
        if verb in ("add-heading", "remove-heading"):
            argv += ["--heading", "Notes"]
        with pytest.raises(SystemExit) as e:
            memory_main(argv)
        assert e.value.code == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["error"] == "overlay_writes_removed"
        assert payload["skill"] == "developer"
        # The redirect is the point — it must name where the work moved.
        assert "skills overlay" in payload["hint"]
        assert captured.err == ""

    def test_the_redirect_fires_before_the_write(
        self, tmp_path, monkeypatch, capsys
    ):
        """A refusal that still wrote would be worse than the old behaviour."""
        user_md = _setup_user(tmp_path, monkeypatch)
        before = user_md.read_text()
        with pytest.raises(SystemExit):
            memory_main([
                "append", "--skill", "developer",
                "--heading", "Notes", "--line", "must not land",
            ])
        capsys.readouterr()
        assert user_md.read_text() == before
        assert _audit_entries(tmp_path) == []

    def test_a_verb_without_the_flag_is_untouched(
        self, tmp_path, monkeypatch, capsys
    ):
        """Control: the redirect must not fire on an ordinary USER.md write."""
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "ordinary"])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert "ordinary" in user_md.read_text()

    def test_the_skills_inventory_subcommand_is_gone(
        self, tmp_path, monkeypatch, capsys
    ):
        _setup_user(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as e:
            memory_main(["skills"])
        assert e.value.code == 2
        capsys.readouterr()

    def test_the_module_no_longer_imports_the_overlay_document_model(self):
        # `curation/overlay.py` existed to re-express the ops over a flat
        # document. With no ops there is nothing to re-express.
        import istota.skills.memory as mem

        assert not hasattr(mem, "apply_overlay_op")
        assert not hasattr(mem, "_do_overlay_op")
        assert not hasattr(mem, "_check_overlay_dir")

    def test_usermd_writes_are_untouched(self, tmp_path, monkeypatch, capsys):
        # The cut is to the overlay target only. USER.md keeps every verb, the
        # lock, the audit entry and the fingerprint.
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "still works"])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
        assert "still works" in user_md.read_text()
        assert _audit_entries(tmp_path)
        assert _last_seen(tmp_path) is not None


class TestUserMdPlantedPaths:
    """USER.md sits under `{mount}/Users/{uid}`, which `build_bwrap_cmd` binds
    read-write into that user's own sandbox, while this CLI runs host-side with
    the daemon's filesystem view (ISSUE-339).

    The write half is the serious one: `config/` is an ancestor, so a link
    there redirected an `append` into a directory of the model's choosing with
    model-chosen content, as the daemon user.
    """

    def test_a_symlink_at_user_md_is_refused_on_read(
        self, tmp_path, monkeypatch, capsys
    ):
        user_md = _setup_user(tmp_path, monkeypatch)
        secret = tmp_path / "credentials.json"
        secret.write_text("TOP SECRET TOKEN\n")
        user_md.unlink()
        user_md.symlink_to(secret)

        with pytest.raises(SystemExit):
            memory_main(["show"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_is_a_symlink"

    def test_a_symlink_at_user_md_is_refused_on_the_write_path_too(
        self, tmp_path, monkeypatch, capsys
    ):
        user_md = _setup_user(tmp_path, monkeypatch)
        victim = tmp_path / "victim.md"
        victim.write_text("- untouched\n")
        user_md.unlink()
        user_md.symlink_to(victim)

        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "planted"])
        assert json.loads(capsys.readouterr().out)["error"] == "overlay_is_a_symlink"
        assert victim.read_text() == "- untouched\n"

    def test_a_fifo_at_user_md_is_refused_without_blocking(
        self, tmp_path, monkeypatch, capsys
    ):
        # A blocking `open(2)` here holds the skill proxy for its whole
        # timeout, so the refusal has to come from O_NONBLOCK + S_ISREG rather
        # than from a read that never returns.
        from .support.blocking import fails_if_it_blocks

        user_md = _setup_user(tmp_path, monkeypatch)
        user_md.unlink()
        os.mkfifo(user_md)
        with fails_if_it_blocks(what="memory show"), pytest.raises(SystemExit):
            memory_main(["show"])
        assert (
            json.loads(capsys.readouterr().out)["error"]
            == "overlay_not_a_regular_file"
        )

    def test_a_symlinked_config_dir_cannot_redirect_the_write(
        self, tmp_path, monkeypatch, capsys
    ):
        # O_NOFOLLOW covers the last path component only, and every component
        # above it is model-writable.
        user_md = _setup_user(tmp_path, monkeypatch)
        victim = tmp_path / "victim"
        victim.mkdir()
        config_dir = user_md.parent
        shutil.rmtree(config_dir)
        config_dir.symlink_to(victim, target_is_directory=True)

        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "planted"])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "user_md_outside_user_tree"
        assert list(victim.iterdir()) == []

    def test_a_symlinked_config_dir_cannot_redirect_the_read(
        self, tmp_path, monkeypatch, capsys
    ):
        user_md = _setup_user(tmp_path, monkeypatch)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "USER.md").write_text("## Notes\n- TOP SECRET TOKEN\n")
        config_dir = user_md.parent
        shutil.rmtree(config_dir)
        config_dir.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(SystemExit):
            memory_main(["show"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "user_md_outside_user_tree"

    def test_an_ancestor_symlink_inside_the_users_own_tree_is_allowed(
        self, tmp_path, monkeypatch, capsys
    ):
        # Containment is the user's own root, not the literal path. A link that
        # stays inside the tree the sandbox binds read-write leads nowhere the
        # user could not already reach, so refusing it would be stricter than
        # the threat calls for — and would break a user who had reorganised
        # their own config directory.
        user_md = _setup_user(tmp_path, monkeypatch)
        config_dir = user_md.parent
        real = config_dir.parent / "config.real"
        config_dir.rename(real)
        config_dir.symlink_to(real, target_is_directory=True)

        memory_main(["append", "--heading", "Notes", "--line", "a fact"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert "- a fact" in (real / "USER.md").read_text()

    def test_the_user_md_cap_matches_the_daemons(self):
        """`_MAX_USER_MD_READ_BYTES` is restated here rather than imported from
        `istota.storage`, which this per-write CLI deliberately does not pull
        in. If the two drift, the daemon reads a USER.md this CLI cannot edit,
        or this CLI writes one the daemon will not load."""
        from istota.skills.memory import _MAX_USER_MD_READ_BYTES
        from istota.storage import USER_CONFIG_READ_CAP_BYTES

        assert _MAX_USER_MD_READ_BYTES == USER_CONFIG_READ_CAP_BYTES

    def test_a_write_that_would_pass_the_read_cap_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        """Past the cap the CLI can no longer read the file back, so the write
        would leave a USER.md that `show`, `append` and `remove` all refuse."""
        user_md = _setup_user(tmp_path, monkeypatch)
        # Derived from the seed rather than a literal: `_read_text` reads this
        # global at call time, so a cap below the seed's own size would refuse
        # at the *read* with `overlay_unreadably_large` and this test would
        # report a cap-check regression that had not happened.
        cap = len(SEED_USER_MD.encode("utf-8")) + 50
        monkeypatch.setattr("istota.skills.memory._MAX_USER_MD_READ_BYTES", cap)
        with pytest.raises(SystemExit):
            memory_main(["append", "--heading", "Notes", "--line", "x" * (cap * 2)])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "would_exceed_read_cap"
        assert "xxxx" not in user_md.read_text()

    def test_a_document_exactly_at_the_cap_is_still_readable(
        self, tmp_path, monkeypatch, capsys
    ):
        """The bound is `>` on both sides, so a file at exactly the cap can
        still be read and therefore still be shrunk. An off-by-one the other way
        wedges a document into being neither usable nor editable."""
        user_md = _setup_user(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "istota.skills.memory._MAX_USER_MD_READ_BYTES",
            len(user_md.read_text().encode("utf-8")),
        )
        memory_main(["headings"])
        assert "Notes" in json.loads(capsys.readouterr().out)["headings"]

    def test_a_refused_read_is_recorded_in_the_audit_trail(
        self, tmp_path, monkeypatch, capsys
    ):
        """Guard 4. `_audit_for` runs *after* the write, and it used to
        re-resolve the USER.md path — which exits on refusal, so a link swapped
        into that window made a successful, recorded append report an error and
        exit 1, which the model reads as failure and retries.

        Both halves are asserted here: the write still reports success, and the
        unreadable state is recorded *as* a refusal rather than collapsing into
        the `user_md_size_bytes: None` that a missing file produces —
        `detect_bypass_write` compares stored sizes, so the two must not read
        alike on exactly the files that were tampered with.
        """
        import istota.skills.memory as mem

        from .support.blocking import fails_if_it_blocks

        user_md = _setup_user(tmp_path, monkeypatch)
        real_write = mem._atomic_write

        def write_then_plant(path, text):
            real_write(path, text)
            # Whatever the op did has landed. Now make the file unreadable
            # before the audit entry is composed.
            path.unlink()
            os.mkfifo(path)

        monkeypatch.setattr(mem, "_atomic_write", write_then_plant)
        # The alarm is not decoration. Measured against the pre-fix `_audit_for`
        # — which re-resolved the path and read it with a plain `read_text()` —
        # this call blocks on the FIFO forever rather than failing, so without
        # the guard a regression wedges an xdist worker instead of going red.
        with fails_if_it_blocks(what="memory append (audit path)"):
            memory_main(["append", "--heading", "Notes", "--line", "recorded"])
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["outcome"] == "applied"

        entry = _audit_entries(tmp_path)[-1]
        assert entry.get("user_md_read_refused") == "overlay_not_a_regular_file"
        # `write_audit_log` omits the key entirely for a None size, so the
        # refusal reason is the only thing distinguishing this from a missing
        # file — which is exactly why it has to be recorded.
        assert "user_md_size_bytes" not in entry
        assert user_md.is_fifo()

    def test_an_ordinary_write_records_a_size_and_no_refusal(
        self, tmp_path, monkeypatch, capsys
    ):
        """The control for the above: without it, an audit entry that recorded a
        refusal on every write would pass that test."""
        _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "recorded"])
        capsys.readouterr()
        entry = _audit_entries(tmp_path)[-1]
        assert "user_md_read_refused" not in entry
        assert entry["user_md_size_bytes"] > 0

    def test_an_ordinary_write_still_works(self, tmp_path, monkeypatch, capsys):
        # The control: every refusal above is worth nothing if the happy path
        # is also refused.
        user_md = _setup_user(tmp_path, monkeypatch)
        memory_main(["append", "--heading", "Notes", "--line", "a fact"])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert "- a fact" in user_md.read_text()
        assert _audit_entries(tmp_path)
        assert _last_seen(tmp_path) is not None


class TestChannelMemoryPlantedPaths:
    """`{mount}/Channels/{token}` is bound read-write into the sandbox of every
    task in that room, so it is the same vector as the user's own tree. The
    token checks bound the *name*; they say nothing about where the directory
    resolves to."""

    def _channel_env(self, tmp_path, monkeypatch):
        _setup_user(tmp_path, monkeypatch)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room1")
        d = tmp_path / "mount" / "Channels" / "room1"
        d.mkdir(parents=True)
        (d / "CHANNEL.md").write_text("## Notes\n- existing\n")
        return d

    def test_a_symlinked_channel_dir_cannot_redirect_the_write(
        self, tmp_path, monkeypatch, capsys
    ):
        d = self._channel_env(tmp_path, monkeypatch)
        victim = tmp_path / "victim"
        victim.mkdir()
        shutil.rmtree(d)
        d.symlink_to(victim, target_is_directory=True)

        with pytest.raises(SystemExit):
            memory_main([
                "append", "--channel", "room1", "--heading", "Notes",
                "--line", "planted",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel_dir_outside_channel_root"
        assert list(victim.iterdir()) == []

    def test_a_symlink_at_channel_md_is_refused_on_read(
        self, tmp_path, monkeypatch, capsys
    ):
        d = self._channel_env(tmp_path, monkeypatch)
        secret = tmp_path / "credentials.json"
        secret.write_text("TOP SECRET TOKEN\n")
        (d / "CHANNEL.md").unlink()
        (d / "CHANNEL.md").symlink_to(secret)

        with pytest.raises(SystemExit):
            memory_main(["show", "--channel", "room1"])
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_is_a_symlink"

    def test_a_fifo_at_channel_md_is_refused_without_blocking(
        self, tmp_path, monkeypatch, capsys
    ):
        from .support.blocking import fails_if_it_blocks

        d = self._channel_env(tmp_path, monkeypatch)
        (d / "CHANNEL.md").unlink()
        os.mkfifo(d / "CHANNEL.md")
        with fails_if_it_blocks(what="memory show --channel"), pytest.raises(
            SystemExit
        ):
            memory_main(["show", "--channel", "room1"])
        assert (
            json.loads(capsys.readouterr().out)["error"]
            == "overlay_not_a_regular_file"
        )

    def test_a_link_to_another_room_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        """The ceiling here is an **equality**, not "under `Channels/`", and
        this is the assertion that documents why.

        `Channels/` is bot-managed and holds every room, so the looser rule
        used for a user's own tree would accept a link at `Channels/{token}`
        pointing at another room's directory — landing the write on that room's
        CHANNEL.md and defeating the token equality check that exists to refuse
        exactly that.
        """
        d = self._channel_env(tmp_path, monkeypatch)
        other = d.parent / "room2"
        other.mkdir()
        (other / "CHANNEL.md").write_text("## Notes\n- other room's business\n")
        shutil.rmtree(d)
        d.symlink_to(other, target_is_directory=True)

        with pytest.raises(SystemExit):
            memory_main([
                "append", "--channel", "room1", "--heading", "Notes",
                "--line", "planted",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "channel_dir_outside_channel_root"
        assert "planted" not in (other / "CHANNEL.md").read_text()

    def test_an_ordinary_channel_write_still_works(
        self, tmp_path, monkeypatch, capsys
    ):
        d = self._channel_env(tmp_path, monkeypatch)
        memory_main([
            "append", "--channel", "room1", "--heading", "Notes",
            "--line", "a fact",
        ])
        assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
        assert "- a fact" in (d / "CHANNEL.md").read_text()
