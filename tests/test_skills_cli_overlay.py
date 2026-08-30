"""The overlay read verbs on the `skills` CLI (ISSUE-343).

An overlay is skill configuration the *user* authors, not memory the model
accumulates, so the write verbs went and the two read verbs moved here from
`istota-skill memory`. What has to survive the move is the containment: this
CLI runs **host-side** under the skill proxy with the daemon's filesystem
view, while `{mount}/Users/{user_id}` is bound read-write into that user's own
sandbox — so every component of the overlay path is model-plantable and the
bytes a read verb prints go straight back to the model.

The rule is now `storage.resolve_user_skill_overlays_dir`, one call, shared
with the loader and the search reindex, rather than this CLI's own copy of it.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from istota.config import Config, UserConfig


OVERLAY_SKILLS = ("developer", "notes", "sensitive_actions")


@pytest.fixture
def overlay_env(tmp_path, monkeypatch):
    """A pinned skill index plus a user tree with an overlay directory."""
    mount = tmp_path / "mount"
    bundled = tmp_path / "bundled"
    for name in OVERLAY_SKILLS:
        d = bundled / name
        d.mkdir(parents=True)
        (d / "skill.md").write_text(
            f"---\nname: {name}\ndescription: the {name} skill\n---\n\n# {name}\n"
        )

    config = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        nextcloud_mount_path=mount,
        bundled_skills_dir=bundled,
        skills_dir=tmp_path / "ops_skills",
        users={"alice": UserConfig()},
    )
    monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)

    overlays = mount / "Users" / "alice" / config.bot_dir_name / "config" / "skills"
    overlays.parent.mkdir(parents=True)

    from types import SimpleNamespace

    return SimpleNamespace(config=config, overlays=overlays, mount=mount)


def _overlay(args=None):
    from istota.skills.skills import cmd_overlay

    return cmd_overlay(argparse.Namespace(**(args or {"name": "developer"})))


def _overlays():
    from istota.skills.skills import cmd_overlays

    return cmd_overlays(argparse.Namespace())


def _rows(capsys):
    return json.loads(capsys.readouterr().out)


class TestOverlayShow:
    def test_prints_the_overlay_whole(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text(
            "### Testing\n\n- The full suite takes an hour here.\n\n```bash\nqt\n```\n"
        )
        _overlay()
        out = capsys.readouterr().out
        # Prose, a `### ` heading and a fenced block all come back — the point
        # of the cut is that an overlay is a document, not a bullet list.
        assert "### Testing" in out
        assert "The full suite takes an hour here." in out
        assert "```bash" in out

    def test_a_missing_overlay_is_empty_not_an_error(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        _overlay()
        assert capsys.readouterr().out.strip() == ""

    def test_an_unknown_skill_name_is_refused_with_the_known_ones(
        self, overlay_env, capsys
    ):
        # The name is a caller-supplied path component and the skill index is
        # the whole of what bounds it, so it is checked even on a read.
        overlay_env.overlays.mkdir(parents=True)
        with pytest.raises(SystemExit) as e:
            _overlay({"name": "develper"})
        assert e.value.code == 1
        payload = _rows(capsys)
        assert payload["status"] == "error"
        assert "developer" in payload["available_skills"]

    def test_a_traversal_name_is_refused_before_any_path_is_built(
        self, overlay_env, capsys
    ):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays.parent / "USER.md").write_text("- TOP SECRET TOKEN\n")
        with pytest.raises(SystemExit):
            _overlay({"name": "../USER"})
        assert "TOP SECRET" not in capsys.readouterr().out

    def test_a_denylisted_skill_is_still_readable(self, overlay_env, capsys):
        # The denylist stopped text going *in*. A read adds nothing, and a file
        # hand-planted in that slot has to stay visible or nothing reports it.
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "sensitive_actions.md").write_text("- planted\n")
        _overlay({"name": "sensitive_actions"})
        assert "planted" in capsys.readouterr().out


class TestOverlayInventory:
    def test_lists_each_file_with_whether_it_binds(self, overlay_env, capsys):
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text("- a rule\n")
        (overlay_env.overlays / "develper.md").write_text("- a typo'd file\n")
        _overlays()
        payload = _rows(capsys)
        rows = {r["skill"]: r for r in payload["skills"]}
        assert rows["developer"]["binds"] is True
        # The whole reason the inventory exists: a file that looks configured
        # and loads into nothing.
        assert rows["develper"]["binds"] is False
        assert rows["develper"]["reason"]

    def test_a_denylisted_file_still_reports_its_size(self, overlay_env, capsys):
        """Ported with the verb from `memory skills` (ISSUE-341 item 2).

        A denylisted name is a real skill, so `inspect_overlay` still reads the
        file and the inventory can tell someone what is in the thing it says
        does not bind. The contrast is an *unknown* name, which is not read at
        all — that gate is pinned on the loader in `test_skills_loader.py`.
        """
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "sensitive_actions.md").write_text("- planted\n")
        _overlays()
        row = _rows(capsys)["skills"][0]
        assert row["reason"] == "denylisted"
        assert row["bytes"] == len("- planted\n")
        assert row["first_line"] == "- planted"

    def test_a_misfiled_name_reports_no_size(self, overlay_env, capsys):
        """The other half of the same gate: an unknown name is refused before
        the read, so there is nothing to report a size from."""
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "develper.md").write_text("- a typo'd file\n")
        _overlays()
        row = _rows(capsys)["skills"][0]
        assert row["reason"] == "unknown_skill"
        assert "lines" not in row
        assert "first_line" not in row

    def test_an_absent_directory_is_an_empty_inventory(self, overlay_env, capsys):
        assert not overlay_env.overlays.exists()
        _overlays()
        assert _rows(capsys)["skills"] == []


class TestPlantedPaths:
    """Every component of the overlay path is model-plantable, and this CLI
    reads it host-side with the daemon's view."""

    def test_a_symlinked_overlay_file_is_refused_not_followed(
        self, tmp_path, overlay_env, capsys
    ):
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN\n")
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").symlink_to(secret)

        with pytest.raises(SystemExit):
            _overlay()
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_is_a_symlink"

    def test_a_fifo_is_refused_without_blocking(self, overlay_env, capsys):
        # A blocking `open(2)` hangs host-side work until the proxy timeout, so
        # the refusal has to come from O_NONBLOCK + S_ISREG.
        overlay_env.overlays.mkdir(parents=True)
        os.mkfifo(overlay_env.overlays / "developer.md")
        with pytest.raises(SystemExit):
            _overlay()
        assert json.loads(capsys.readouterr().out)["error"] == (
            "overlay_not_a_regular_file"
        )

    @pytest.mark.parametrize("verb", ["overlay", "overlays"])
    def test_a_redirected_directory_is_refused_on_both_read_verbs(
        self, tmp_path, overlay_env, capsys, verb
    ):
        # O_NOFOLLOW covers the last path component only, so a redirected
        # directory would otherwise be read straight through.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "developer.md").write_text("- TOP SECRET TOKEN\n")
        overlay_env.overlays.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(SystemExit):
            _overlay() if verb == "overlay" else _overlays()
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_dir_outside_user_tree"

    @pytest.mark.parametrize("verb", ["overlay", "overlays"])
    def test_a_redirected_ancestor_is_refused_on_both_read_verbs(
        self, tmp_path, overlay_env, capsys, verb
    ):
        # `config/` is an ordinary entry in the read-write-bound user tree, so
        # `mv config config.real && ln -s /anywhere config` is two commands
        # from inside the sandbox — and a leaf `lstat` never sees it.
        victim = tmp_path / "victim"
        (victim / "skills").mkdir(parents=True)
        (victim / "skills" / "developer.md").write_text("- TOP SECRET TOKEN\n")
        config_dir = overlay_env.overlays.parent
        config_dir.rename(tmp_path / "config.real")
        config_dir.symlink_to(victim, target_is_directory=True)

        with pytest.raises(SystemExit):
            _overlay() if verb == "overlay" else _overlays()
        captured = capsys.readouterr().out
        assert "TOP SECRET" not in captured
        assert json.loads(captured)["error"] == "overlay_dir_outside_user_tree"

    def test_a_link_that_stays_inside_the_users_own_tree_is_now_refused(
        self, overlay_env, capsys
    ):
        """ISSUE-344 reverses this deliberately, and the reversal has a cost.

        This used to be read. Containment was the user's own root rather than
        the literal path — the same rule the loader applied, so the read verb
        and the prompt agreed — and it was a conscious relaxation of the write
        path's stricter leaf rule, which existed because that path created and
        owned the directory. ISSUE-343 kept the relaxation when it moved these
        verbs.

        What changed is not the containment question but how it is answered. A
        comparison of resolved paths is only true until something moves, and
        everything under `{mount}/Users/{user_id}` is model-writable, so the
        check and the read that follows it are separated by a window a task can
        land a rename in. `open_overlay_dir` refuses a symlink at any component
        — including one landing back inside the tree — because refusing is the
        only answer that survives the path being rewritten underneath it, and
        every reader now takes that answer.

        The price is paid by a user who deliberately linked `config/skills`
        somewhere else in their own workspace: their overlays stop loading.
        That is why `doctor`'s `config.skill_overlays` reports the directory
        with a remedy rather than leaving it to be discovered as silence —
        `tests/test_overlay_dir_containment.py::TestTheDoctorSweep`.
        """
        config_dir = overlay_env.overlays.parent
        real = config_dir.parent / "config.real"
        config_dir.rename(real)
        config_dir.symlink_to(real, target_is_directory=True)
        (real / "skills").mkdir()
        (real / "skills" / "developer.md").write_text("- a rule\n")

        with pytest.raises(SystemExit):
            _overlay()
        captured = capsys.readouterr().out
        assert "a rule" not in captured
        # Not `overlay_dir_outside_user_tree`: this link resolves *inside* the
        # tree, so that name would claim something the check did not establish.
        # The error says what was determined — the directory could not be
        # opened — which also covers an unreadable `config` or a regular file
        # left at `skills`, since `open_overlay_dir` collapses every `OSError`.
        assert json.loads(captured)["error"] == "overlay_dir_unopenable"


class TestUnreadableFiles:
    """Refusals `read_overlay_bytes` can return that no planted inode
    produces. Each must reach the caller: reporting one as an empty file would
    hide the only thing worth saying about that path."""

    def test_a_non_utf8_overlay_is_refused_not_printed(self, overlay_env, capsys):
        # The one place `cmd_overlay` decodes model-plantable bytes.
        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_bytes(b"- caf\xe9 rule\n")
        with pytest.raises(SystemExit):
            _overlay()
        assert json.loads(capsys.readouterr().out)["error"] == "overlay_not_utf8"

    def test_a_file_over_the_read_cap_is_refused(self, overlay_env, capsys):
        from istota.skills._loader import OVERLAY_READ_CAP_BYTES

        overlay_env.overlays.mkdir(parents=True)
        (overlay_env.overlays / "developer.md").write_text(
            "- x\n" * ((OVERLAY_READ_CAP_BYTES // 4) + 16)
        )
        with pytest.raises(SystemExit):
            _overlay()
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "overlay_unreadably_large"


class TestNoMountDeployment:
    """`nextcloud_mount_path = None` is the rclone-remote shape. There are no
    overlays for anyone, which is a different answer from "this skill has none"
    and must not be rendered as one."""

    def test_the_inventory_reports_no_directory(self, overlay_env, capsys):
        overlay_env.config.nextcloud_mount_path = None
        _overlays()
        payload = _rows(capsys)
        assert payload["dir"] is None
        assert payload["skills"] == []

    def test_reading_one_is_an_error_not_an_empty_body(self, overlay_env, capsys):
        overlay_env.config.nextcloud_mount_path = None
        with pytest.raises(SystemExit):
            _overlay()
        assert json.loads(capsys.readouterr().out)["error"] == "no_overlay_storage"


class TestInventoryPathRendering:
    def test_the_directory_is_reported_relative_to_the_mount(
        self, overlay_env, capsys
    ):
        """An absolute host path would tell the model where a symlinked
        `config/` actually resolves on the daemon's filesystem."""
        overlay_env.overlays.mkdir(parents=True)
        _overlays()
        reported = _rows(capsys)["dir"]
        assert reported == "Users/alice/istota/config/skills"
        assert not reported.startswith("/")


class TestReadVerbsWriteNothing:
    def test_neither_verb_creates_the_overlay_directory(self, overlay_env, capsys):
        # The write path used to `mkdir(parents=True)`. Nothing here may.
        assert not overlay_env.overlays.exists()
        _overlays()
        capsys.readouterr()
        _overlay()
        capsys.readouterr()
        assert not overlay_env.overlays.exists()
