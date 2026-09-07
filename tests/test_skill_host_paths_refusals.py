"""Every stamped host path, driven end to end through its own skill CLI.

`tests/test_skill_hostpath_declaration.py` proves the machinery: `host_path`
stamps, `resolve_parsed` resolves, `parse_and_resolve` refuses. It proves it
against parsers the test builds itself. This file is the other half — the real
`build_parser`, the real `main`, the real argv a model would send — so that a
stamp is checked where it actually has to hold.

**Both halves are mandatory and neither is sufficient.** A refusal test passes
just as well against a verb whose argument was deleted, or renamed, or never
stamped in the first place: nothing happens, nothing is written, and "refused"
is indistinguishable from "not there". So every case also asserts that the
in-root call is **accepted** and that the handler received the **resolved**
path, which is what makes the write-back contract observable rather than
assumed. That is why `mount` puts the whole tree behind a symlinked ancestor:
on a plain path the resolved string and the argument are equal, and an
assertion comparing them cannot tell a resolving implementation from a
passthrough. `test_the_case_paths_are_not_already_resolved` is what holds that
property, since losing it would quietly turn every acceptance assertion
vacuous.

**And the parametrization itself is asserted.** A stamp deleted from a
declaration takes its cases out of the collection, and a shrinking
parametrization is a green run — the failure this whole file exists under. So
the collection is compared key for key against `CASES` and carries a floor,
the way `tests/test_skill_host_paths_coverage.py` asserts `len(found) > 40`.

The per-verb control the spec asks for is run against this file: remove one
argument's stamp, run this file, and require exactly that key's acceptance and
refusal node ids to change. Recorded in the stage's result file, verb by verb.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from istota.skills._hostpath import EGRESS, READ, RESOLVING, WRITE, stamped
from tests.support.skill_cli import run_skill_main

SKILLS_DIR = Path(__file__).resolve().parent.parent / "src" / "istota" / "skills"


# --------------------------------------------------------------------------- #
# The environment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Mount:
    """A mount laid out like the real one, reached through a symlink.

    `link` is what `NEXTCLOUD_MOUNT_PATH` names and what every path in a case
    is built from; `real` is where the roots resolve to. Every acceptance
    assertion compares the two, so the indirection is what gives the word
    "resolved" something to mean.
    """

    link: Path
    real: Path
    deferred: Path

    def own(self, *parts: str) -> Path:
        return self.link.joinpath("Users", "alice", *parts)

    def channel(self, *parts: str) -> Path:
        return self.link.joinpath("Channels", "tok1", *parts)

    def talk(self, *parts: str) -> Path:
        return self.link.joinpath("Talk", *parts)


@pytest.fixture
def mount(tmp_path, monkeypatch) -> Mount:
    real = tmp_path / "srv" / "shared"
    for sub in ("Users/alice", "Users/bob", "Channels/tok1", "Talk"):
        (real / sub).mkdir(parents=True)
    link = tmp_path / "mount"
    link.symlink_to(real, target_is_directory=True)
    deferred = tmp_path / "deferred"
    deferred.mkdir()

    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(link))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
    monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
    return Mount(link=link, real=real, deferred=deferred)


# --------------------------------------------------------------------------- #
# The cases
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """One stamped argument, and the argv that reaches it.

    `argv` takes the value for the stamped argument and returns the whole
    command line, so a verb's other required arguments live beside it rather
    than in the test body. `patch` names what to replace with the recorder:
    a module attribute for a skill whose `main` builds its command table from
    module globals, and a `(dict, key)` pair for one whose table is built at
    import — `devbox._DISPATCH` is the second kind, and patching the function
    it already captured would record nothing. The key of such a dict need not
    be a string: `nextcloud._COMMANDS` is keyed on `(group, command)`.
    """

    argv: Callable[[str], list[str]]
    main: str
    patch: tuple[str, object]


CASES: dict[tuple[str, str, str], Case] = {
    ("browse", "screenshot", "output"): Case(
        argv=lambda p: ["screenshot", "https://example.com", "-o", p],
        main="istota.skills.browse",
        patch=("istota.skills.browse", "cmd_screenshot"),
    ),
    ("devbox", "exec-file", "path"): Case(
        argv=lambda p: ["exec-file", p],
        main="istota.skills.devbox",
        patch=("istota.skills.devbox._DISPATCH", "exec-file"),
    ),
    ("devbox", "cp-in", "src"): Case(
        argv=lambda p: ["cp-in", p, "/home/dev/probe.txt"],
        main="istota.skills.devbox",
        patch=("istota.skills.devbox._DISPATCH", "cp-in"),
    ),
    ("devbox", "cp-out", "dest"): Case(
        argv=lambda p: ["cp-out", "/home/dev/probe.txt", p],
        main="istota.skills.devbox",
        patch=("istota.skills.devbox._DISPATCH", "cp-out"),
    ),
    ("feeds", "import-opml", "path"): Case(
        argv=lambda p: ["import-opml", p],
        main="istota.skills.feeds",
        patch=("istota.skills.feeds", "cmd_import_opml"),
    ),
    ("feeds", "export-opml", "output"): Case(
        argv=lambda p: ["export-opml", "--output", p],
        main="istota.skills.feeds",
        patch=("istota.skills.feeds", "cmd_export_opml"),
    ),
    ("kv", "set", "value_file"): Case(
        argv=lambda p: ["set", "notes", "draft", "--value-file", p],
        main="istota.skills.kv",
        patch=("istota.skills.kv", "cmd_set"),
    ),
    ("health", "export-csv", "output"): Case(
        argv=lambda p: ["export-csv", "--output", p],
        main="istota.skills.health",
        patch=("istota.skills.health", "cmd_export_csv"),
    ),
    ("email", "send", "attach"): Case(
        argv=lambda p: [
            "send", "--to", "someone@example.com", "--subject", "hi",
            "--body", "there", "--attach", p,
        ],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_send"),
    ),
    ("email", "reply", "attach"): Case(
        argv=lambda p: ["reply", "17", "--body", "there", "--attach", p],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_reply"),
    ),
    ("email", "reply-all", "attach"): Case(
        argv=lambda p: ["reply-all", "17", "--body", "there", "--attach", p],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_reply"),
    ),

    # -- Stage 5: the reads ------------------------------------------------- #
    ("email", "send", "body_file"): Case(
        argv=lambda p: [
            "send", "--to", "someone@example.com", "--subject", "hi",
            "--body-file", p,
        ],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_send"),
    ),
    ("email", "reply", "body_file"): Case(
        argv=lambda p: ["reply", "17", "--body-file", p],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_reply"),
    ),
    ("email", "reply-all", "body_file"): Case(
        argv=lambda p: ["reply-all", "17", "--body-file", p],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_reply"),
    ),
    ("email", "output", "body_file"): Case(
        argv=lambda p: ["output", "--subject", "S", "--body-file", p],
        main="istota.skills.email",
        patch=("istota.skills.email", "cmd_output"),
    ),
    ("health", "upload", "file_path"): Case(
        argv=lambda p: ["upload", p, "--drawn-at", "2026-01-02"],
        main="istota.skills.health",
        patch=("istota.skills.health", "cmd_upload"),
    ),
    ("health", "import-csv", "file_path"): Case(
        argv=lambda p: ["import-csv", p],
        main="istota.skills.health",
        patch=("istota.skills.health", "cmd_import_csv"),
    ),
    ("health", "attach-document", "path"): Case(
        argv=lambda p: ["attach-document", "--path", p, "--to", "encounter:42"],
        main="istota.skills.health",
        patch=("istota.skills.health", "cmd_attach_document"),
    ),
    ("health", "import-immunizations", "paste_file"): Case(
        argv=lambda p: ["import-immunizations", "--paste-file", p, "--dry-run"],
        main="istota.skills.health",
        patch=("istota.skills.health", "cmd_import_immunizations"),
    ),
    ("memory_search", "index.file", "path"): Case(
        argv=lambda p: ["index", "file", p],
        main="istota.skills.memory_search",
        patch=("istota.skills.memory_search", "cmd_index_file"),
    ),
    ("money", "import-csv", "file"): Case(
        argv=lambda p: ["import-csv", p, "--account", "checking"],
        main="istota.skills.money",
        patch=("istota.skills.money", "cmd_import_csv"),
    ),
    ("money", "portfolio.import", "file"): Case(
        argv=lambda p: ["portfolio", "import", p],
        main="istota.skills.money",
        patch=("istota.skills.money", "cmd_portfolio_import"),
    ),
    ("nextcloud", "files.upload", "local"): Case(
        argv=lambda p: ["files", "upload", p, "/Users/alice/uploaded.bin"],
        main="istota.skills.nextcloud",
        patch=("istota.skills.nextcloud._COMMANDS", ("files", "upload")),
    ),
    ("transcribe", "ocr", "image_path"): Case(
        argv=lambda p: ["ocr", p],
        main="istota.skills.transcribe",
        patch=("istota.skills.transcribe", "cmd_ocr"),
    ),
    ("whisper", "transcribe", "audio_path"): Case(
        argv=lambda p: ["transcribe", p],
        main="istota.skills.whisper.cli",
        patch=("istota.skills.whisper.cli", "cmd_transcribe"),
    ),
}

#: How many resolving stamps this file expects to find at the very least.
#: A parametrization that shrinks is a green run, so the count is asserted
#: rather than trusted — the same reason the coverage walk asserts a floor.
STAMP_FLOOR = 25


def _skill_parser_modules() -> dict[str, str]:
    """Every skill exposing an argparse `build_parser`, by skill name.

    Re-derived rather than imported from either of the two other files that
    walk the same tree: importing a test module for a helper couples three
    collections together, and the thing all three claim is only that they
    agree about the tree.
    """
    out: dict[str, str] = {}
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        if not (skill_dir / "skill.md").exists():
            continue
        for candidate in ("__init__", "cli"):
            source = skill_dir / f"{candidate}.py"
            if source.exists() and "def build_parser" in source.read_text():
                suffix = "" if candidate == "__init__" else ".cli"
                out[skill_dir.name] = f"istota.skills.{skill_dir.name}{suffix}"
                break
    return out


def resolving_stamps() -> dict[tuple[str, str, str], str]:
    """Every `READ` / `EGRESS` / `WRITE` stamp in the tree, by key."""
    out: dict[tuple[str, str, str], str] = {}
    for skill, module_name in _skill_parser_modules().items():
        module = importlib.import_module(module_name)
        for command, dest, mode in stamped(module.build_parser()):
            if mode in RESOLVING:
                out[(skill, command, dest)] = mode
    return out


def _resolve_dotted(dotted: str):
    """A module, or an attribute of one, from a dotted name."""
    try:
        return importlib.import_module(dotted)
    except ImportError:
        module_name, _, attribute = dotted.rpartition(".")
        return getattr(importlib.import_module(module_name), attribute)


class _Recorder:
    """Stands in for the handler and remembers the namespace it was given."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, args):
        self.calls.append(args)
        return {"status": "ok"}

    @property
    def called(self) -> bool:
        return bool(self.calls)

    def received(self, dest: str):
        """The stamped value the handler was handed, unwrapped if a list.

        `email --attach` is `action="append"`, so its value is a list that
        `resolve_parsed` rewrites element by element.
        """
        value = getattr(self.calls[0], dest)
        return value[0] if isinstance(value, list) else value


@pytest.fixture
def drive(monkeypatch):
    """`(key, path) -> (run, recorder)`, with the handler replaced."""

    def _drive(key, path):
        case = CASES[key]
        recorder = _Recorder()
        target, name = case.patch
        holder = _resolve_dotted(target)
        if isinstance(holder, dict):
            monkeypatch.setitem(holder, name, recorder)
        else:
            monkeypatch.setattr(holder, name, recorder)
        main = importlib.import_module(case.main).main
        return run_skill_main(main, case.argv(str(path))), recorder

    return _drive


KEYS = sorted(CASES)


def _params(keys):
    """Parametrize with the key spelled out in the node id.

    The per-verb control is "remove one stamp and require exactly that key's
    node ids to change", which needs the key readable in the id — `key0` says
    nothing and moves when a neighbour is added.
    """
    return [
        pytest.param(key, id=".".join(part or "top" for part in key))
        for key in keys
    ]


def _expected_resolution(mode: str, given: Path) -> str:
    """What the handler must have been handed for `given`.

    A read resolves the path itself; a write anchors on the parent, which is
    what lets it name something that does not exist yet.
    """
    if mode == WRITE:
        return str(given.parent.resolve() / given.name)
    return str(given.resolve())


# --------------------------------------------------------------------------- #
# The parametrization itself
# --------------------------------------------------------------------------- #


class TestTheCollection:
    def test_every_resolving_stamp_has_a_case(self):
        """A stamp with no case here is an argument nothing drives.

        The coverage walk would still call it accounted for — it is stamped —
        so this is the only thing standing between "declared" and "checked".
        """
        assert sorted(resolving_stamps()) == KEYS

    def test_it_found_at_least_the_stamps_it_expects(self):
        found = resolving_stamps()
        assert len(found) >= STAMP_FLOOR, sorted(found)

    def test_the_case_paths_are_not_already_resolved(self, mount):
        """The premise every acceptance assertion below rests on.

        `mount.link` is a symlink, so an argument built through it is a
        different string from its resolution. On a plain path the two are
        equal and `received == expected` passes against a `main` that resolves
        nothing at all.
        """
        given = mount.own("probe.txt")
        assert str(given) != str(given.resolve())


# --------------------------------------------------------------------------- #
# Accepted, refused, and the filesystem afterwards
# --------------------------------------------------------------------------- #


class TestEveryStampedArgument:
    @pytest.mark.parametrize("key", _params(KEYS))
    def test_an_in_root_path_reaches_the_handler_resolved(self, key, mount, drive):
        mode = resolving_stamps()[key]
        if mode == WRITE:
            given = mount.own("out", "landed.bin")
        else:
            given = mount.own("source.bin")
            given.write_bytes(b"payload")

        run, recorder = drive(key, given)

        assert recorder.called, (
            f"{key} refused an in-root path: {run.envelope or run.stdout!r}"
        )
        received = recorder.received(key[2])
        assert received == _expected_resolution(mode, given), received
        assert received != str(given), (
            "the handler was handed the argument rather than its resolution"
        )

    @pytest.mark.parametrize("key", _params(KEYS))
    def test_a_path_outside_the_roots_is_refused_before_the_handler(
        self, key, mount, tmp_path, drive,
    ):
        mode = resolving_stamps()[key]
        outside = tmp_path / "outside"
        if mode == WRITE:
            given = outside / "landed.bin"
        else:
            outside.mkdir()
            given = outside / "source.bin"
            given.write_bytes(b"payload")

        run, recorder = drive(key, given)

        assert not recorder.called, f"{key} dispatched an out-of-root path"
        assert run.exit_code == 1, run.stdout
        assert run.envelope.get("status") == "error", run.stdout
        assert run.envelope.get("reason") == "host_path_refused", run.envelope
        if mode == WRITE:
            # Asserted on the filesystem, not on the envelope: the ordering
            # defect this convention exists for created the tree and *then*
            # refused, which a test reading the message alone cannot see.
            assert not outside.exists(), outside

    @pytest.mark.parametrize("key", _params(KEYS))
    def test_another_users_workspace_is_refused(self, key, mount, drive):
        given = mount.link / "Users" / "bob" / "theirs.bin"
        if resolving_stamps()[key] != WRITE:
            given.write_bytes(b"payload")

        run, recorder = drive(key, given)

        assert not recorder.called, f"{key} reached another user's workspace"
        assert run.exit_code == 1, run.stdout

    @pytest.mark.parametrize("key", _params(KEYS))
    def test_a_symlink_out_of_the_roots_is_refused(
        self, key, mount, tmp_path, drive,
    ):
        """In-roots as a name, somewhere else on disk.

        A read refuses the link before resolving it; a write refuses one
        standing at the destination's own leaf, which is the check
        `write_resolved`'s `O_NOFOLLOW` then backs up.
        """
        target = tmp_path / "elsewhere.bin"
        target.write_bytes(b"not yours")
        given = mount.own("innocent.bin")
        given.symlink_to(target)

        run, recorder = drive(key, given)

        assert not recorder.called, f"{key} followed a link out of the roots"
        assert run.exit_code == 1, run.stdout
        assert target.read_bytes() == b"not yours"

    @pytest.mark.parametrize("key", _params(KEYS))
    def test_an_explicitly_empty_value_is_refused(self, key, mount, drive):
        """The second, smaller narrowing this work carries, named here.

        `resolve_parsed` skips a value of `None` — the argument was not passed
        — and refuses one that is present and empty, because `Path("")` is
        `Path(".")`, the process cwd, which for a proxied skill is the
        daemon's and for a task's own shell may well be inside the workspace.

        Three of these verbs used to branch on falsiness themselves and reach
        their own "not given" path: `browse screenshot --output ""` derived a
        name under the bot dir, `health export-csv --output ""` printed the CSV
        to stdout, `feeds export-opml --output ""` did the same. Those branches
        are unreachable through an explicit empty string now. It is a refusal
        rather than a widening, and it is the one the spec's Edge cases list
        does not name, so it is asserted here rather than left to be
        rediscovered from a bug report.
        """
        run, recorder = drive(key, "")

        assert not recorder.called, f"{key} dispatched an empty path"
        assert run.exit_code == 1, run.stdout
        assert "empty" in run.envelope.get("error", "").lower(), run.envelope

    @pytest.mark.parametrize("key", _params(KEYS))
    def test_a_refusal_does_not_name_the_other_roots(self, key, mount, drive):
        """The message goes back to the model; the roots are other people's
        directory names."""
        run, _ = drive(key, Path("/etc/passwd"))
        message = run.envelope.get("error", "")
        # The refused path, not the allowlist. A write anchors on the parent,
        # so what it names is `/etc` rather than the leaf.
        assert "/etc" in message, message
        assert str(mount.real / "Channels") not in message
        assert str(mount.deferred) not in message


# --------------------------------------------------------------------------- #
# Which roots each mode gets
# --------------------------------------------------------------------------- #


def _keys_for(mode: str) -> list[tuple[str, str, str]]:
    return sorted(k for k in KEYS if resolving_stamps().get(k) == mode)


class TestTheModeDecidesTheRoots:
    """The two root sets, observed through the verbs rather than the helper.

    `tests/test_skill_hostpath_declaration.py` asserts the same split against
    a parser it builds itself. These are the shipped verbs, which is where a
    stamp chosen wrongly actually costs something — and both directions are
    asserted, because a mode that is too narrow refuses work that has always
    been legitimate and arrives with no test naming it.
    """

    @pytest.mark.parametrize("key", _params(_keys_for(WRITE)))
    def test_a_write_may_still_name_the_tasks_own_channel_directory(
        self, key, mount, drive,
    ):
        """The correction: a `WRITE` keeps the task's roots minus the
        read-only ones, exactly `env_host_roots(writable=True)`.

        A destination's content does not leave the task's context — it lands
        where the task reads it back — so `browse screenshot --output`,
        `devbox cp-out --dest`, `feeds export-opml --output` and `health
        export-csv --output` all write into `{mount}/Channels/{token}` today
        and must keep doing so. Under an `OWN` reading of `WRITE` every one of
        these goes red.
        """
        run, recorder = drive(key, mount.channel("landed.bin"))
        assert recorder.called, run.envelope or run.stdout

    @pytest.mark.parametrize("key", _params(_keys_for(WRITE)))
    def test_a_write_may_still_name_the_deferred_directory(
        self, key, mount, drive,
    ):
        run, recorder = drive(key, mount.deferred / "landed.bin")
        assert recorder.called, run.envelope or run.stdout

    @pytest.mark.parametrize("key", _params(_keys_for(READ)))
    def test_a_read_may_name_a_talk_attachment(self, key, mount, drive):
        source = mount.talk("shared.pdf")
        source.write_bytes(b"payload")
        run, recorder = drive(key, source)
        assert recorder.called, run.envelope or run.stdout

    @pytest.mark.parametrize("key", _params(_keys_for(EGRESS)))
    def test_egress_refuses_a_talk_attachment(self, key, mount, drive):
        """The spec's one user-visible narrowing.

        `email --attach` may no longer mail on a Talk attachment or a file
        from the task's channel directory. It is not a new rule — the held
        draft path already applied it, because `outbound_drafts` re-validates
        against the workspace alone hours later — it is the direct-send path
        getting the same answer as the held one.
        """
        source = mount.talk("shared.pdf")
        source.write_bytes(b"payload")
        run, recorder = drive(key, source)
        assert not recorder.called, f"{key} mailed a Talk attachment"
        assert run.envelope.get("reason") == "host_path_refused", run.envelope

    @pytest.mark.parametrize("key", _params(_keys_for(EGRESS)))
    def test_egress_refuses_the_tasks_own_channel_directory(
        self, key, mount, drive,
    ):
        source = mount.channel("shared.pdf")
        source.write_bytes(b"payload")
        run, recorder = drive(key, source)
        assert not recorder.called, f"{key} mailed a file from a channel dir"

    @pytest.mark.parametrize("key", _params(_keys_for(EGRESS)))
    def test_egress_refuses_the_deferred_directory(self, key, mount, drive):
        """The third root `OWN` drops, and the one a reader forgets.

        `memory_search index file` had it before stage 5 and no longer does.
        The deferred dir is the task's own scratch space — a `READ` and a
        `WRITE` both reach it — but a file in it is as fit to be mailed out
        or indexed as anything else the task wrote, which is the question
        `EGRESS` answers rather than where the bytes currently sit.
        """
        source = mount.deferred / "scratch.bin"
        source.write_bytes(b"payload")
        run, recorder = drive(key, source)
        assert not recorder.called, f"{key} took a file from the deferred dir"
        assert run.envelope.get("reason") == "host_path_refused", run.envelope

    @pytest.mark.parametrize("key", _params(_keys_for(EGRESS)))
    def test_egress_still_admits_the_users_own_workspace(
        self, key, mount, drive,
    ):
        source = mount.own("letter.pdf")
        source.write_bytes(b"payload")
        run, recorder = drive(key, source)
        assert recorder.called, run.envelope or run.stdout
