"""Every overlay reader holds the directory open instead of naming it twice.

ISSUE-344, the second half of ISSUE-341 item 3. `contained_overlay_dir` answers
containment by comparing resolved paths, and that answer stops being true the
moment anything moves: every component under `{mount}/Users/{user_id}` is
model-writable, because `build_bwrap_cmd` binds that tree read-write into the
user's own sandbox, so `mv config config.real && ln -s /anywhere config` is two
commands from inside it. `open_overlay_dir` walks each component with
`O_NOFOLLOW | O_DIRECTORY` and hands back an fd, so containment holds by
construction. `search.reindex_skill_overlays` was wired that way in `7c83c2f3`;
the five readers here were not.

**The discriminating layout is a symlink that lands back inside the user's own
tree**, and it is what every test below plants. An out-of-tree link is refused
by `contained_overlay_dir` already, so a test using one is green against the
unfixed code and proves nothing. An in-tree link is precisely where the two
gates disagree — the resolved-path comparison accepts it and the fd walk
refuses it — so a reader that still resolves by name reads the planted file and
one that holds an fd does not.

The surfaces are the ones in the issue's own table, plus `skills overlay`,
which the table missed and which is the widest of them: it prints up to
`OVERLAY_READ_CAP_BYTES` (1 MiB, not the loader's 32 KB) of whatever it opens
straight back to the model.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import pytest

from istota.config import Config, UserConfig


SKILLS = ("developer", "notes", "sensitive_actions")

#: What a planted directory holds. Named for a real skill so that every gate
#: except containment passes: the file binds, and a reader that reaches it
#: reports it as a live customization rather than refusing it for some other
#: reason and passing the test by accident.
PLANTED = "- PLANTED BY THE SANDBOX.\n"


def _config(tmp_path, **overrides) -> Config:
    bundled = tmp_path / "bundled"
    for name in SKILLS:
        d = bundled / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "skill.md").write_text(
            f"---\nname: {name}\ndescription: the {name} skill\n---\n\n# {name} body\n"
        )
    ops = tmp_path / "ops_skills"
    ops.mkdir(exist_ok=True)
    overrides.setdefault("nextcloud_mount_path", tmp_path / "mount")
    overrides.setdefault("users", {"alice": UserConfig()})
    return Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "tmp",
        bundled_skills_dir=bundled,
        skills_dir=ops,
        **overrides,
    )


def _user_root(config: Config, user_id: str = "alice") -> Path:
    return Path(config.nextcloud_mount_path) / "Users" / user_id


def _overlays(config: Config, user_id: str = "alice") -> Path:
    d = _user_root(config, user_id) / config.bot_dir_name / "config" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def plant_inside_tree(config: Config, component: str, user_id: str = "alice") -> Path:
    """Replace one component of the overlay path with an in-tree symlink.

    Returns the directory the link lands in, already holding `developer.md`.
    `component` is the bot directory, `config` or `skills` — a task can rewrite
    any of the three, and the guard has to cover each rather than only the leaf.
    """
    root = _user_root(config, user_id)
    bot = config.bot_dir_name
    chain = {bot: [], "config": [bot], "skills": [bot, "config"]}[component]
    parent = root
    for part in chain:
        parent = parent / part
        parent.mkdir(parents=True, exist_ok=True)

    # The link target is a sibling *inside* the user's own tree, so the
    # resolved-path comparison accepts it. Its own subdirectories mirror
    # whatever the link replaced, so the walk below it still finds `skills/`.
    below = {bot: ["config", "skills"], "config": ["skills"], "skills": []}[component]
    target = root / "planted"
    leaf = target.joinpath(*below) if below else target
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / "developer.md").write_text(PLANTED)

    victim = parent / component
    if victim.exists():
        shutil.rmtree(victim)
    victim.symlink_to(target, target_is_directory=True)
    return leaf


COMPONENTS = ("bot_dir", "config", "skills")


def _component(config: Config, which: str) -> str:
    return config.bot_dir_name if which == "bot_dir" else which


def _open_fd_count() -> int:
    """How many descriptors this process holds.

    **Not** "the lowest free descriptor", which is the obvious probe and does
    not work here. That one only sees a leak whose descriptors start at the
    lowest free slot and stay contiguous; anything else opening and closing
    files in between keeps a low slot free, so the number is unchanged while
    descriptors pile up above it. Measured: with `cmd_show`'s `os.close`
    deleted, the lowest-free probe read 3 before and 3 after 25 leaking calls,
    while the count below read 5 and then 30. The first version of this file
    shipped the broken probe.

    `/dev/fd` is the process's own descriptor table on both macOS and Linux
    (a symlink to `/proc/self/fd` on the latter). `listdir` opens one itself
    and closes it before returning, so it biases both readings equally and the
    delta is what the assertions use.
    """
    return len(os.listdir("/dev/fd"))


def _cli_env(config: Config, monkeypatch) -> None:
    monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)


# --------------------------------------------------------------------- the seam


class TestTheLayoutItself:
    """The premise every test below rests on, asserted rather than assumed.

    If `contained_overlay_dir` ever starts refusing an in-tree link, these
    tests stop discriminating and go green against unfixed code — which is the
    failure mode this repo keeps finding on assertions about a mechanism. So
    the disagreement between the two gates is pinned here, once.
    """

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_the_two_gates_disagree_on_an_in_tree_symlink(self, tmp_path, which):
        from istota.skills._loader import contained_overlay_dir, open_overlay_dir

        config = _config(tmp_path)
        _overlays(config)
        plant_inside_tree(config, _component(config, which))
        root = _user_root(config)

        resolved = contained_overlay_dir(
            root / config.bot_dir_name / "config" / "skills", root
        )
        assert resolved is not None, "the permissive gate must accept this layout"
        assert (resolved / "developer.md").read_text() == PLANTED

        assert open_overlay_dir(root, config.bot_dir_name, "config", "skills") is None


class TestTheSharedHelper:
    """`storage.open_user_skill_overlays` — the path and an fd on it, or neither.

    Atomic on purpose. Three of the five readers derive the directory through
    `resolve_user_skill_overlays_dir`, and handing one of them a usable path
    beside a None fd is the shape that made this issue: the fd would then be the
    caller's to remember, and four callers forgot.
    """

    def test_it_returns_both_on_a_plain_tree(self, tmp_path):
        from istota.storage import open_user_skill_overlays

        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text("- a real rule\n")
        d, fd = open_user_skill_overlays(config, "alice")
        assert d is not None and fd is not None
        try:
            assert sorted(e.name for e in os.scandir(fd)) == ["developer.md"]
        finally:
            os.close(fd)

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_an_in_tree_symlink_gets_neither(self, tmp_path, which):
        from istota.storage import open_user_skill_overlays

        config = _config(tmp_path)
        _overlays(config)
        plant_inside_tree(config, _component(config, which))
        assert open_user_skill_overlays(config, "alice") == (None, None)

    def test_no_mount_gets_neither(self, tmp_path):
        from istota.storage import open_user_skill_overlays

        config = _config(tmp_path, nextcloud_mount_path=None)
        assert open_user_skill_overlays(config, "alice") == (None, None)

    def test_a_missing_directory_gets_neither(self, tmp_path):
        """The path never comes back without a descriptor — not even here.

        This is the regression test for the defect both reviews of ISSUE-344
        found. An earlier draft returned `(path, None)` for an absent directory
        on the grounds that absence is benign and the CLI needs to tell it from
        a refusal. Both are true and neither justifies the return shape: the
        prompt loader forwarded that path with no descriptor to read through,
        and `test_the_absent_directory_race` below is what that cost. A caller
        needing the distinction asks for it separately, where the answer picks
        a message rather than a file.
        """
        from istota.storage import open_user_skill_overlays

        config = _config(tmp_path)
        _user_root(config).mkdir(parents=True)
        assert open_user_skill_overlays(config, "alice") == (None, None)

    def test_it_leaks_no_descriptor_across_repeated_refusals(self, tmp_path):
        """It runs once per task on the prompt path of a long-lived daemon."""
        from istota.storage import open_user_skill_overlays

        config = _config(tmp_path)
        _overlays(config)
        plant_inside_tree(config, "config")

        before = _open_fd_count()
        for _ in range(50):
            assert open_user_skill_overlays(config, "alice") == (None, None)
        assert _open_fd_count() <= before


# ---------------------------------------------------------------- the surfaces


class TestThePromptLoader:
    """`_load_user_overlay`, the one the issue calls the worst of the four.

    A win here appends up to `OVERLAY_MAX_BYTES` of any daemon-readable file to
    that skill's prompt section, unsandboxed, on every task that selects the
    skill — where the search path that was fixed first only reached `!search`.
    """

    def test_an_fd_bounds_what_the_loader_can_read(self, tmp_path):
        """The unit property: with an fd, only that directory is reachable.

        `read_overlay_bytes` takes `path.name` when it has a `dir_fd`, so a
        loader holding one cannot be steered by the components above the leaf.
        """
        from istota.skills._loader import load_skills, open_overlay_dir

        config = _config(tmp_path)
        real = _overlays(config)
        (real / "developer.md").write_text("- the real rule\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "developer.md").write_text(PLANTED)

        fd = open_overlay_dir(
            _user_root(config), config.bot_dir_name, "config", "skills"
        )
        assert fd is not None
        try:
            out = load_skills(
                config.skills_dir, ["developer"],
                bundled_dir=config.bundled_skills_dir,
                # A path from somewhere else entirely: the fd is what decides.
                user_overlay_dir=elsewhere,
                user_overlay_dir_fd=fd,
            )
        finally:
            os.close(fd)

        assert "the real rule" in out
        assert "PLANTED" not in out

    def test_the_absent_directory_race(self, tmp_path):
        """The hole both reviews of ISSUE-344 found, reproduced end to end.

        Nothing creates `config/skills`, so it is usually absent — and an
        earlier draft answered that with `(path, None)`, a usable path and
        nothing to read through. `load_skills` then re-stats the name, which is
        a second, name-based resolution of a model-writable path. A task that
        creates `config/skills` as a symlink between the two stats wins: the
        second stat follows the link, the read has no descriptor, and
        `read_overlay_bytes` opens the absolute path with `O_NOFOLLOW` covering
        only the leaf.

        Measured before the fix: the planted body reached the assembled prompt
        under the label that tells the model it takes precedence over the
        skill's own instructions. The fix is that the helper now returns the
        path only together with a descriptor, so the interleaving below cannot
        be constructed — hence the assertion is on the pair, then on the prompt.
        """
        from istota.skills._loader import load_skills
        from istota.storage import open_user_skill_overlays

        config = _config(tmp_path)
        config_dir = _user_root(config) / config.bot_dir_name / "config"
        config_dir.mkdir(parents=True)
        # `config/skills` deliberately does not exist yet.

        overlay_dir, overlay_fd = open_user_skill_overlays(config, "alice")
        assert (overlay_dir, overlay_fd) == (None, None), (
            "a path handed back without a descriptor is the defect itself"
        )

        # The task, inside what used to be the window.
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "developer.md").write_text(PLANTED)
        (config_dir / "skills").symlink_to(victim, target_is_directory=True)

        out = load_skills(
            config.skills_dir, ["developer"], config.bot_name, config.bot_dir_name,
            bundled_dir=config.bundled_skills_dir,
            user_overlay_dir=overlay_dir, user_overlay_dir_fd=overlay_fd,
        )
        assert "developer body" in out
        assert "PLANTED" not in out

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_skills_show_loads_no_overlay_through_an_in_tree_symlink(
        self, tmp_path, monkeypatch, capsys, which
    ):
        """End to end through the second of the two prompt-load paths.

        `skills show` and `executor` both call `load_skills` with the directory
        the shared helper resolves, and the two agreeing is why that derivation
        is shared. This is the one of the two reachable without a task.
        """
        config = _config(tmp_path)
        _overlays(config)
        plant_inside_tree(config, _component(config, which))
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)

        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="developer"))
        out = capsys.readouterr().out
        assert "developer body" in out, "the bundled body must still load"
        assert "PLANTED" not in out

    def test_skills_show_still_loads_a_real_overlay(
        self, tmp_path, monkeypatch, capsys
    ):
        """The positive control. Without it the assertion above passes on a
        loader that stopped reading overlays at all."""
        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text("- the real rule\n")
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)

        from istota.skills.skills import cmd_show

        cmd_show(argparse.Namespace(name="developer"))
        assert "the real rule" in capsys.readouterr().out


class TestTheSkillsCli:
    """`skills overlay` and `skills overlays`, both host-side under the proxy.

    `skills overlay` is the one the issue's table missed and the widest of the
    five: it prints the bytes it read straight back to the model, at the 1 MiB
    read cap rather than the loader's 32 KB.
    """

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _overlays(config)
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        return config

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_overlay_show_refuses_through_an_in_tree_symlink(
        self, env, capsys, which
    ):
        from istota.skills.skills import cmd_overlay

        plant_inside_tree(env, _component(env, which))
        with pytest.raises(SystemExit) as e:
            cmd_overlay(argparse.Namespace(name="developer"))
        assert e.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        # Names what was established. `open_overlay_dir` collapses every
        # `OSError` to a refusal, so this code covers an unreadable `config` or
        # a regular file at `skills` as well as a planted link, and must not
        # claim the directory resolved outside the tree — it did not.
        assert payload["error"] == "overlay_dir_unopenable"
        assert "PLANTED" not in json.dumps(payload)

    def test_an_absent_directory_is_empty_rather_than_an_error(self, env, capsys):
        """Absence stays distinguishable from refusal, which is the whole
        reason the CLI asks that question for itself now."""
        from istota.skills.skills import cmd_overlay, cmd_overlays

        shutil.rmtree(_overlays(env))
        cmd_overlay(argparse.Namespace(name="developer"))
        assert capsys.readouterr().out.strip() == ""
        cmd_overlays(argparse.Namespace())
        assert json.loads(capsys.readouterr().out)["skills"] == []

    def test_overlay_show_still_prints_a_real_overlay(self, env, capsys):
        from istota.skills.skills import cmd_overlay

        (_overlays(env) / "developer.md").write_text("- the real rule\n")
        cmd_overlay(argparse.Namespace(name="developer"))
        assert "the real rule" in capsys.readouterr().out

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_the_inventory_refuses_through_an_in_tree_symlink(
        self, env, capsys, which
    ):
        from istota.skills.skills import cmd_overlays

        plant_inside_tree(env, _component(env, which))
        with pytest.raises(SystemExit) as e:
            cmd_overlays(argparse.Namespace())
        assert e.value.code == 1
        assert "PLANTED" not in capsys.readouterr().out

    def test_the_inventory_still_reports_a_real_overlay(self, env, capsys):
        """The positive control for both refusals above."""
        from istota.skills.skills import cmd_overlays

        (_overlays(env) / "developer.md").write_text("- the real rule\n")
        cmd_overlays(argparse.Namespace())
        rows = json.loads(capsys.readouterr().out)["skills"]
        assert [r["skill"] for r in rows] == ["developer"]
        assert rows[0]["binds"] is True
        assert rows[0]["first_line"] == "- the real rule"


class TestTheNightlyInventory:
    """`sleep_cycle._load_skill_overlay_inventory`, which feeds the curator."""

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_it_reports_nothing_through_an_in_tree_symlink(self, tmp_path, which):
        from istota.memory.sleep_cycle import _load_skill_overlay_inventory

        config = _config(tmp_path)
        _overlays(config)
        plant_inside_tree(config, _component(config, which))
        assert _load_skill_overlay_inventory(config, "alice") == []

    def test_it_still_reports_a_real_overlay(self, tmp_path):
        from istota.memory.sleep_cycle import _load_skill_overlay_inventory

        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text("- one\n- two\n")
        assert _load_skill_overlay_inventory(config, "alice") == [("developer", 2)]

    def test_a_refusal_is_logged_and_an_absence_is_not(self, tmp_path, caplog):
        """This function's own contract is that silence would make a broken
        inventory look like a user with no overlays, so the two cases must not
        both exit quietly — the same split `reindex_skill_overlays` makes."""
        import logging

        from istota.memory.sleep_cycle import _load_skill_overlay_inventory

        config = _config(tmp_path)
        _user_root(config).mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            assert _load_skill_overlay_inventory(config, "alice") == []
        assert not caplog.records, "an absent directory is the ordinary state"

        caplog.clear()
        _overlays(config)
        plant_inside_tree(config, "config")
        with caplog.at_level(logging.WARNING):
            assert _load_skill_overlay_inventory(config, "alice") == []
        assert any("alice" in r.getMessage() for r in caplog.records)

    def test_an_out_of_tree_link_is_logged_as_such(self, tmp_path, caplog):
        """The two refusals are kept apart, and this is the one a single
        boolean got wrong.

        `resolve_user_skill_overlays_dir` returns None both for "no mount" and
        for "resolves outside the user's tree", so a helper keyed on
        `path.exists()` reported the clearest plant of the set as an ordinary
        absence — silent, on the worse of the two failures.
        """
        import logging

        from istota.memory.sleep_cycle import _load_skill_overlay_inventory

        config = _config(tmp_path)
        _overlays(config)
        outside = tmp_path / "outside"
        (outside / "skills").mkdir(parents=True)
        (outside / "skills" / "developer.md").write_text(PLANTED)
        config_dir = _user_root(config) / config.bot_dir_name / "config"
        shutil.rmtree(config_dir)
        config_dir.symlink_to(outside, target_is_directory=True)

        with caplog.at_level(logging.WARNING):
            assert _load_skill_overlay_inventory(config, "alice") == []
        msgs = [r.getMessage() for r in caplog.records]
        assert any("outside" in m for m in msgs), msgs


class TestTheReturnedPathIsDisplayOnly:
    """`open_user_skill_overlays` realpaths the directory *before* it walks it.

    So under a concurrent swap the returned path and the descriptor can name
    different inodes. That is harmless only because no consumer ever opens
    anything through the path — they reach files through the descriptor and use
    the path for `path.name` and for messages. Until this class that was a
    convention stated in a docstring, which is the kind of thing that drifts
    the first time somebody adds a sixth reader.
    """

    def test_a_reader_holding_a_descriptor_ignores_the_path_it_was_given(
        self, tmp_path
    ):
        """The property in one assertion: hand the readers a path pointing
        somewhere else entirely and the descriptor still decides."""
        from istota.skills._loader import (
            OVERLAY_MAX_BYTES,
            inspect_overlay,
            load_skill_index,
            open_overlay_dir,
            read_overlay_bytes,
        )

        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text("- the real rule\n")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "developer.md").write_text(PLANTED)

        fd = open_overlay_dir(
            _user_root(config), config.bot_dir_name, "config", "skills"
        )
        assert fd is not None
        try:
            raw, refusal, _size = read_overlay_bytes(
                decoy / "developer.md", max_bytes=OVERLAY_MAX_BYTES, dir_fd=fd
            )
            assert refusal is None
            assert raw == b"- the real rule\n"

            known = load_skill_index(
                config.skills_dir, bundled_dir=config.bundled_skills_dir
            )
            found = inspect_overlay(
                decoy / "developer.md", known_skills=known,
                max_read_bytes=OVERLAY_MAX_BYTES, dir_fd=fd,
            )
            assert found.binds
            assert found.first_line == "- the real rule"
        finally:
            os.close(fd)

    #: Modules that call `read_overlay_bytes` on something that is **not** an
    #: overlay, and so have no overlay directory descriptor to pass.
    #: ISSUE-339 adopted it as the shared hardened leaf reader for `USER.md`
    #: and `CHANNEL.md` — `O_NOFOLLOW`, `S_ISREG`, an `fstat` size check — and
    #: those files get their containment from `storage.resolve_user_config_dir`
    #: instead. Reusing the reader is the point; the exemption is from *this*
    #: rule, not from containment.
    _NOT_OVERLAY_READERS = frozenset({
        "storage.py",                 # read_regular_file / read_user_config_file
        "skills/memory/__init__.py",  # the memory CLI's _read_text
    })

    def test_no_overlay_reader_opens_a_path_without_a_descriptor(self):
        """A grep-shaped guard, because the risk is a *new* caller.

        `inspect_overlay` is overlay-specific, so every caller must pass a
        descriptor, no exceptions. `read_overlay_bytes` is shared with the
        `config/` file readers (see above), so it is required only of the
        modules doing overlay work.

        The one deliberate exception inside `_loader.py` is
        `_load_user_overlay`'s fallback, which exists for tests asserting on
        rendering and is unreachable from either production caller —
        `open_user_skill_overlays` never returns a path without a descriptor.
        """
        import re
        from pathlib import Path as P

        root = P(__file__).resolve().parent.parent / "src" / "istota"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(root))
            src = path.read_text()
            for m in re.finditer(r"\b(read_overlay_bytes|inspect_overlay)\s*\(", src):
                callee = m.group(1)
                if callee == "read_overlay_bytes" and rel in self._NOT_OVERLAY_READERS:
                    continue
                if rel == "skills/_loader.py":
                    continue  # definition site + the documented fallback
                # Take the call's argument list by matching parentheses.
                i = m.end() - 1
                depth, j = 0, i
                while j < len(src):
                    if src[j] == "(":
                        depth += 1
                    elif src[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if "dir_fd" in src[i : j + 1]:
                    continue
                offenders.append(f"{rel}:{src[:m.start()].count(chr(10)) + 1} {callee}")

        assert offenders == [], (
            "these call an overlay reader without a descriptor, which walks "
            f"every component by name: {offenders}"
        )

    def test_the_guard_above_can_actually_fail(self, tmp_path):
        """The guard is a regex over source, so it is exactly the kind that
        silently stops matching. Feed it a known-bad module and require a hit."""
        import re

        src = "found = inspect_overlay(path, known_skills=known)\n"
        hits = [m.group(1) for m in
                re.finditer(r"\b(read_overlay_bytes|inspect_overlay)\s*\(", src)]
        assert hits == ["inspect_overlay"]
        assert "dir_fd" not in src


class TestTheExecutorPromptPath:
    """The surface the issue calls the worst of the five, end to end.

    `execute_task(..., dry_run=True)` returns the fully assembled prompt after
    running everything assembly calls, so this is the real eager path rather
    than a stand-in for it — `skills show` exercises the same `load_skills`
    call but is the *other* of the two prompt-load paths.

    It reaches no network socket, and that is a property of the fixture rather
    than luck: the case that made `tests/test_prompt_golden.py` need a socket
    guard is `read_user_memory_v2` falling through to `ensure_user_directories_v2`
    and an OCS share POST, which is the Nextcloud backend's branch. `nextcloud.url`
    is empty here, so `storage_backend` is `local` and that branch is never
    taken, while `use_mount` — and so the overlay directory — stays. The socket
    guard below asserts it rather than assuming it, because the whole point of
    that golden-file lesson is that a path reaching the network silently is
    exactly what a green test looks like.
    """

    ROUNDS = 15

    @staticmethod
    def _task():
        from istota import db

        return db.Task(
            id=1, status="running", source_type="talk", user_id="alice",
            prompt="hello", conversation_token="tok",
        )

    @staticmethod
    def _config_with_db(tmp_path):
        """`developer` made eager, because the prompt is what is asserted.

        The shared fixture's skills carry no selector, so `select_skills`
        returns nothing and `load_skills` renders no section at all — a prompt
        with no overlay in it for the wrong reason, which would pass every
        negative assertion below while proving nothing.
        """
        from istota import db

        config = _config(tmp_path)
        (config.bundled_skills_dir / "developer" / "skill.md").write_text(
            "---\nname: developer\ndescription: the developer skill\n"
            "always_include: true\n---\n\n# developer body\n"
        )
        db.init_db(config.db_path)
        return config

    @pytest.fixture(autouse=True)
    def _no_sockets(self, monkeypatch):
        import socket

        def _refuse(self, *a, **kw):
            raise AssertionError(f"prompt assembly opened a socket: {a}")

        monkeypatch.setattr(socket.socket, "connect", _refuse)

    def _assemble(self, config) -> str:
        from istota.executor import execute_task

        ok, out, _actions, _trace = execute_task(
            self._task(), config, [], dry_run=True
        )
        assert ok
        return out

    def test_a_real_overlay_reaches_the_assembled_prompt(self, tmp_path):
        """The positive control. Without it every assertion below passes on an
        executor that stopped applying overlays altogether."""
        config = self._config_with_db(tmp_path)
        (_overlays(config) / "developer.md").write_text("- THE OVERLAY RULE.\n")
        assert "THE OVERLAY RULE" in self._assemble(config)

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_an_in_tree_symlink_puts_nothing_in_the_prompt(self, tmp_path, which):
        config = self._config_with_db(tmp_path)
        _overlays(config)
        plant_inside_tree(config, _component(config, which))
        out = self._assemble(config)
        assert "developer body" in out, "the bundled body must still load"
        assert "PLANTED" not in out

    def test_it_leaks_no_descriptor_per_task(self, tmp_path):
        """This runs once per task in a daemon that stays up for weeks, so a
        leaked descriptor here is the one that actually exhausts the table."""
        config = self._config_with_db(tmp_path)
        (_overlays(config) / "developer.md").write_text("- a real rule\n")

        for _ in range(2):
            self._assemble(config)  # warm imports, caches and connections
        before = _open_fd_count()
        for _ in range(self.ROUNDS):
            self._assemble(config)
        # `<=`, not `==`. A leak can only push the count *up*; a drop is
        # unrelated cleanup — a connection or handler opened during warm-up
        # being collected mid-loop — and was measured happening (24 -> 12).
        # Asserting equality made that noise a failure while catching nothing
        # extra: the negative controls still go red under `<=`.
        assert _open_fd_count() <= before


class TestDescriptorLifetime:
    """Every `finally: os.close(...)` this change added, on the path that has
    a descriptor to close.

    The refusal test up in `TestTheSharedHelper` proves only `open_overlay_dir`'s
    own internal balance — on that path the caller is handed nothing. Delete any
    of the five production `os.close` calls and it stays green, while the
    executor leaks one descriptor per task in a daemon that runs for weeks. So
    this drives the *successful* path, where a descriptor really is handed out
    and the caller really is the one who has to close it.

    Verified to fail as intended: removing the `finally` in `cmd_show`, in
    `cmd_overlays` or in `_load_skill_overlay_inventory` each turns this red.
    """

    ROUNDS = 25

    def _drive(self, config, monkeypatch, capsys):
        import argparse

        from istota.doctor import run_checks
        from istota.memory.sleep_cycle import _load_skill_overlay_inventory
        from istota.skills.skills import cmd_overlay, cmd_overlays, cmd_show
        from istota.storage import open_user_skill_overlays

        d, fd = open_user_skill_overlays(config, "alice")
        assert fd is not None, "this probe is about the path that yields an fd"
        os.close(fd)

        assert _load_skill_overlay_inventory(config, "alice")
        run_checks(config, only=("config.skill_overlays",))
        _cli_env(config, monkeypatch)
        cmd_overlay(argparse.Namespace(name="developer"))
        cmd_overlays(argparse.Namespace())
        cmd_show(argparse.Namespace(name="developer"))
        capsys.readouterr()

    def test_no_surface_leaks_a_descriptor_on_the_happy_path(
        self, tmp_path, monkeypatch, capsys
    ):
        config = _config(tmp_path)
        (_overlays(config) / "developer.md").write_text("- the real rule\n")

        for _ in range(2):
            self._drive(config, monkeypatch, capsys)  # warm caches, connections
        before = _open_fd_count()
        for _ in range(self.ROUNDS):
            self._drive(config, monkeypatch, capsys)
        assert _open_fd_count() <= before  # see the note in the executor test


class TestTheDoctorSweep:
    """`doctor config.skill_overlays`, which walks every user's tree.

    It is also the surface that has to *say* the directory was refused. The
    prompt loader degrades silently and logs at `debug` — every refusal there
    does, deliberately, because it runs once per eager skill per task and a
    `warning` reaches the model's own stderr from a `skills show` subprocess.
    `doctor` is the once-on-a-cadence report that posture depends on, so a
    directory nothing can open must not read here as a user with no overlays.
    """

    NAME = "config.skill_overlays"

    def _run(self, config):
        from istota.doctor import run_checks

        results = run_checks(config, only=(self.NAME,))
        assert len(results) == 1
        return results[0]

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_a_refused_directory_warns_and_is_named(self, tmp_path, which):
        """WARN rather than FAIL, deliberately.

        A sandboxed task can produce this at will — `ln -s /tmp config` inside
        its own workspace — so a FAIL here would be a deployment-scope red an
        attacker can raise on demand, which is the aimable alert ISSUE-340
        split this check to avoid. It is also the severity this module already
        gives a symlinked overlay *file*. Nothing about the refusal itself
        turns on the severity: the link is not followed either way.
        """
        from istota.doctor import WARN

        config = _config(tmp_path)
        _overlays(config)
        plant_inside_tree(config, _component(config, which))
        r = self._run(config)
        assert r.status == WARN
        assert "alice" in r.detail
        assert "dir_not_openable" in r.detail
        assert r.remedy
        # The wording is this check's entire product, so it is asserted rather
        # than left to `status == FAIL`. A directory is not one of `total`
        # overlay files, and folding it into that fraction read "1 of 0".
        assert "1 of 0" not in r.detail
        assert "could not be read" in r.detail

    def test_a_refused_directory_does_not_skew_another_users_file_count(
        self, tmp_path
    ):
        """The mixed tree, where the bad ratio understated rather than absurd."""
        from istota.doctor import FAIL

        config = _config(tmp_path)
        plant_inside_tree(config, "config")
        bob = _overlays(config, "bob")
        (bob / "developer.md").write_text("- fine\n")
        (bob / "develper.md").write_text("- a typo\n")

        r = self._run(config)
        # bob's typo is a real misfiling and still FAILs; alice's directory
        # rides along in the same detail, counted apart from bob's two files
        # rather than inside their denominator.
        assert r.status == FAIL
        assert "1 of 2 overlay file(s)" in r.detail
        assert "1 overlay director" in r.detail

    def test_an_out_of_tree_directory_is_reported_rather_than_skipped(
        self, tmp_path
    ):
        """Nothing else looks at this directory, so skipping it left the most
        clear-cut plant of the set as the one case nothing anywhere named."""
        from istota.doctor import WARN

        config = _config(tmp_path)
        _overlays(config)
        outside = tmp_path / "outside"
        (outside / "skills").mkdir(parents=True)
        (outside / "skills" / "developer.md").write_text(PLANTED)
        config_dir = _user_root(config) / config.bot_dir_name / "config"
        shutil.rmtree(config_dir)
        config_dir.symlink_to(outside, target_is_directory=True)

        r = self._run(config)
        assert r.status == WARN
        assert "alice" in r.detail
        assert "dir_outside_user_tree" in r.detail
        # The safety property is unchanged and is the one that matters: the
        # link is not followed and nothing behind it is named.
        assert "PLANTED" not in r.detail

    def test_a_dotfile_cannot_turn_a_deployment_check_red(self, tmp_path):
        """`touch .developer.md` is one command from any sandboxed task.

        The listing moved from `Path.glob` (which never matched a leading dot)
        to `scandir`, and `_classify_unknown_overlay` reads `.developer` as a
        near-miss of `developer` and buckets it fatal — so without the filter,
        any task could turn a deployment-scope check red at will. That is the
        aimable alert ISSUE-340 split this check to avoid. The asymmetry with
        `skills overlays` and the search reindex, which do list dotfiles, is
        deliberate: neither attaches a status to what it lists.
        """
        from istota.doctor import OK

        config = _config(tmp_path)
        d = _overlays(config)
        (d / "developer.md").write_text("- a real rule\n")
        (d / ".developer.md").write_text("- planted by a task\n")
        (d / ".notes.md").write_text("- and another\n")

        r = self._run(config)
        assert r.status == OK
        assert ".developer" not in r.detail

    @pytest.mark.parametrize("which", COMPONENTS)
    def test_it_reports_no_file_from_behind_the_symlink(self, tmp_path, which):
        """The point of the fix rather than of the report: a filename from
        another tree must not reach a deployment-scope check detail."""
        config = _config(tmp_path)
        _overlays(config)
        leaf = plant_inside_tree(config, _component(config, which))
        (leaf / "carried-across.md").write_text("- planted\n")
        r = self._run(config)
        assert "carried-across" not in r.detail

    def test_a_plain_tree_is_unaffected(self, tmp_path):
        """The positive control: the sweep still reads the real directory."""
        from istota.doctor import FAIL

        config = _config(tmp_path)
        (_overlays(config) / "develper.md").write_text("- a rule\n")
        r = self._run(config)
        assert r.status == FAIL
        assert "develper.md" in r.detail
        assert "unknown_skill" in r.detail
