"""Declaring a skill CLI argument as a host path, and enforcing it at parse.

``src/istota/skills/_hostpath.py`` is the declaration side of ISSUE-447: a
path-shaped argument carries its disposition on the argparse action that
declares it, and ``resolve_parsed`` reads that stamp back off the parsed
namespace and resolves the value through the shared allowlist before any
handler runs. This file is the machinery's own test — the stamp, the walk, the
write-back, and the conversion of every skill ``main`` onto
``_cli.parse_and_resolve``.

Three properties here are the ones a plausible wrong implementation gets wrong,
and each has a test written to kill it rather than to describe the right
answer:

**The write-back is a ``str``.** Every handler in the tree receives argparse
values as ``str`` today, and ``money`` splices two of them straight into a
``CliRunner`` argv list, which takes a sequence of strings. Writing back a
``Path`` would fail inside a ``CliRunner.invoke`` whose error the facade
renders as a generic envelope. ``str`` is type-preserving by construction,
which is what makes the conversion behaviour-preserving.

**A dest is not unique across a parser.** ``--path``, ``--file`` and
``--output`` recur across verbs, so the same dest can carry ``READ`` under one
command and ``WRITE`` under another. Resolving a ``READ`` as a ``WRITE`` skips
the existence and symlink checks; the other direction refuses a destination
that does not exist yet. So the resolution walks the parsers actually taken
rather than keying on the dest, which requires every ``add_subparsers`` in the
tree to carry a ``dest=``. Both are asserted below.

**``READ`` and ``EGRESS`` differ in their roots, not in their direction.** Both
read. ``READ`` resolves against the task's whole working context; ``EGRESS`` is
for a read whose bytes leave it, and gets the user's own workspace alone. A
test that only ever exercised an in-workspace path would pass against an
implementation that collapsed the two.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

import pytest

from istota.skills._cli import parse_and_resolve
from istota.skills._hostpath import (
    EGRESS,
    MODES,
    NOT_A_PATH,
    READ,
    REMOTE,
    REPO,
    WRITE,
    host_path,
    resolve_parsed,
    stamp_conflicts,
    stamped,
    subparsers_without_dest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "src" / "istota" / "skills"


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """A mount laid out like the real one, with alice as the caller.

    Deliberately the same shape as `tests/test_skill_host_paths.py`'s fixture:
    the roots are that module's and this one only asks which set a mode picks.
    """
    root = tmp_path / "mount"
    (root / "Users" / "alice").mkdir(parents=True)
    (root / "Users" / "bob").mkdir(parents=True)
    (root / "Channels" / "tok1").mkdir(parents=True)
    (root / "Talk").mkdir(parents=True)
    deferred = tmp_path / "deferred"
    deferred.mkdir()
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
    return root


def _one_verb_parser(mode=READ, **kwargs):
    parser = argparse.ArgumentParser(prog="demo")
    sub = parser.add_subparsers(dest="command", required=True)
    verb = sub.add_parser("go")
    host_path(verb, "--file", mode=mode, **kwargs)
    return parser


class TestTheStamp:
    def test_it_stamps_the_action_argparse_registered(self):
        parser = argparse.ArgumentParser()
        action = host_path(parser, "--file", mode=READ)
        assert action.istota_host_path == READ
        assert action in parser._actions

    def test_it_passes_every_other_keyword_through(self):
        parser = argparse.ArgumentParser()
        action = host_path(
            parser, "-f", "--file", mode=READ, nargs="+", required=True,
            help="a path to read", default=None,
        )
        assert action.nargs == "+"
        assert action.required is True
        assert action.help == "a path to read"
        assert action.option_strings == ["-f", "--file"]

    def test_it_stamps_a_positional(self):
        parser = argparse.ArgumentParser()
        action = host_path(parser, "path", mode=READ)
        assert action.istota_host_path == READ
        assert action.dest == "path"

    def test_an_unknown_mode_is_refused_at_declaration(self):
        parser = argparse.ArgumentParser()
        with pytest.raises(ValueError, match="mode"):
            host_path(parser, "--file", mode="scoped")

    @pytest.mark.parametrize("mode", [REPO, REMOTE, NOT_A_PATH])
    def test_a_non_resolving_mode_has_to_say_why(self, mode):
        """The three pass-through modes are records, not guarantees.

        Same discipline as the coverage registry's non-`SCOPED` entries: a
        disposition no test can settle has to carry the sentence the next
        reader needs, at the declaration.
        """
        parser = argparse.ArgumentParser()
        with pytest.raises(ValueError, match="note"):
            host_path(parser, "--file", mode=mode)
        assert host_path(parser, "--other", mode=mode, note="why").istota_host_path == mode

    def test_the_note_is_readable_off_the_action(self):
        parser = argparse.ArgumentParser()
        action = host_path(parser, "--remote", mode=REMOTE, note="a Nextcloud path")
        assert action.istota_host_path_note == "a Nextcloud path"


class TestStamped:
    def test_it_finds_a_stamp_on_a_nested_subparser(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="group")
        files = sub.add_parser("files")
        files_sub = files.add_subparsers(dest="command")
        upload = files_sub.add_parser("upload")
        host_path(upload, "--local", mode=READ)
        assert stamped(parser) == [("files.upload", "local", READ)]

    def test_it_reports_the_top_level_command_as_the_empty_string(self):
        parser = argparse.ArgumentParser()
        host_path(parser, "--config", mode=READ)
        assert stamped(parser) == [("", "config", READ)]

    def test_an_unstamped_parser_yields_nothing(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--file")
        sub = parser.add_subparsers(dest="command")
        sub.add_parser("go").add_argument("--path")
        assert stamped(parser) == []

    def test_it_walks_past_a_subparser_that_declares_no_dest(self):
        """`stamped` describes the tree; the missing dest is its own failure.

        Reported by `subparsers_without_dest` rather than by this walk going
        quiet, because a walk that stopped there would under-report the tree
        and the coverage check downstream would read as green.
        """
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        host_path(sub.add_parser("go"), "--file", mode=READ)
        assert stamped(parser) == [("go", "file", READ)]


class TestSubparsersWithoutDest:
    def test_it_names_the_command_whose_subparsers_have_no_dest(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        group = sub.add_parser("group")
        group.add_subparsers()
        assert subparsers_without_dest(parser) == ["group"]

    def test_a_fully_declared_tree_is_empty(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        sub.add_parser("group").add_subparsers(dest="action")
        assert subparsers_without_dest(parser) == []


class TestStampConflicts:
    def test_one_dest_with_two_modes_under_one_command_is_an_error(self):
        """Picking one would silently resolve a read as a write, or worse."""
        parser = argparse.ArgumentParser()
        host_path(parser, "--file", mode=READ)
        sub = parser.add_subparsers(dest="command")
        host_path(sub.add_parser("go"), "--file", mode=WRITE)
        assert stamp_conflicts(parser) == [("go", "file", (READ, WRITE))]

    def test_the_same_dest_in_two_sibling_commands_is_not_a_conflict(self):
        """This is the case the whole per-command resolution exists for."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        host_path(sub.add_parser("get"), "--file", mode=READ)
        host_path(sub.add_parser("put"), "--file", mode=WRITE)
        assert stamp_conflicts(parser) == []


class TestResolveParsed:
    def test_it_writes_back_the_resolved_path_as_a_string(self, mount):
        target = mount / "Users" / "alice" / "notes.txt"
        target.write_text("x")
        parser = _one_verb_parser(READ)
        args = parser.parse_args(["go", "--file", str(target)])
        assert resolve_parsed(parser, args) is None
        assert args.file == str(target)
        assert type(args.file) is str

    def test_a_leaf_symlink_is_refused(self, mount):
        """Even one pointing inside the roots: the rule is `resolve_in_roots`'s.

        Here to pin that `resolve_parsed` delegates rather than re-deciding —
        an implementation that resolved the value itself and then asked
        `path_under_roots` would admit this and be green everywhere else.
        """
        real = mount / "Users" / "alice" / "real.txt"
        real.write_text("x")
        (mount / "Users" / "alice" / "link.txt").symlink_to(real)
        parser = _one_verb_parser(READ)
        args = parser.parse_args(
            ["go", "--file", str(mount / "Users" / "alice" / "link.txt")]
        )
        assert resolve_parsed(parser, args) is not None  # a leaf symlink is refused

    def test_an_intermediate_symlink_resolves_into_the_written_back_value(self, mount):
        real = mount / "Users" / "alice" / "deep"
        real.mkdir()
        (real / "f.txt").write_text("x")
        (mount / "Users" / "alice" / "via").symlink_to(real)
        parser = _one_verb_parser(READ)
        args = parser.parse_args(
            ["go", "--file", str(mount / "Users" / "alice" / "via" / "f.txt")]
        )
        assert resolve_parsed(parser, args) is None
        assert args.file == str(real / "f.txt")

    def test_a_path_outside_the_roots_is_refused_and_named(self, mount):
        parser = _one_verb_parser(READ)
        args = parser.parse_args(["go", "--file", "/etc/hosts"])
        error = resolve_parsed(parser, args)
        assert error and "/etc/hosts" in error

    def test_a_refusal_does_not_name_the_roots(self, mount):
        """Naming them leaks another user's directory names to the model."""
        parser = _one_verb_parser(READ)
        args = parser.parse_args(["go", "--file", "/etc/hosts"])
        error = resolve_parsed(parser, args)
        assert "alice" not in error

    def test_an_argument_that_was_not_passed_is_skipped(self, mount):
        parser = _one_verb_parser(READ)
        args = parser.parse_args(["go"])
        assert resolve_parsed(parser, args) is None
        assert args.file is None

    def test_every_element_of_a_list_is_resolved(self, mount):
        one = mount / "Users" / "alice" / "one.txt"
        two = mount / "Users" / "alice" / "two.txt"
        one.write_text("1")
        two.write_text("2")
        parser = _one_verb_parser(READ, nargs="+")
        args = parser.parse_args(["go", "--file", str(one), str(two)])
        assert resolve_parsed(parser, args) is None
        assert args.file == [str(one), str(two)]
        assert all(type(v) is str for v in args.file)

    def test_a_list_is_refused_on_its_first_bad_element(self, mount):
        good = mount / "Users" / "alice" / "one.txt"
        good.write_text("1")
        parser = _one_verb_parser(READ, nargs="+")
        args = parser.parse_args(["go", "--file", str(good), "/etc/hosts"])
        error = resolve_parsed(parser, args)
        assert error and "/etc/hosts" in error

    def test_a_write_destination_need_not_exist(self, mount):
        dest = mount / "Users" / "alice" / "out" / "report.csv"
        parser = _one_verb_parser(WRITE)
        args = parser.parse_args(["go", "--file", str(dest)])
        assert resolve_parsed(parser, args) is None
        assert args.file == str(dest)

    def test_resolution_creates_nothing_on_the_filesystem(self, mount, tmp_path):
        """Resolution answers a question; it does not have a side effect.

        Asserted on the filesystem rather than on the return value, because the
        ordering defect this guards against created the tree and *then*
        refused — a test reading only the message would pass against it.
        """
        outside = tmp_path / "outside" / "x"
        parser = _one_verb_parser(WRITE)
        args = parser.parse_args(["go", "--file", str(outside)])
        assert resolve_parsed(parser, args) is not None
        assert not outside.exists()
        assert not outside.parent.exists()

        inside = mount / "Users" / "alice" / "fresh" / "x"
        args = parser.parse_args(["go", "--file", str(inside)])
        assert resolve_parsed(parser, args) is None
        assert not inside.parent.exists()

    def test_egress_gets_the_users_own_workspace_alone(self, mount):
        """The one place the two root sets have to be observably different.

        A path in the task's own channel directory is fine for a `READ` and is
        refused for an `EGRESS`, because an `EGRESS` value's bytes leave the
        task. A test using an in-workspace path for both would pass against an
        implementation that never distinguished them.
        """
        channel_file = mount / "Channels" / "tok1" / "shared.txt"
        channel_file.write_text("x")

        read_parser = _one_verb_parser(READ)
        args = read_parser.parse_args(["go", "--file", str(channel_file)])
        assert resolve_parsed(read_parser, args) is None

        egress_parser = _one_verb_parser(EGRESS)
        args = egress_parser.parse_args(["go", "--file", str(channel_file)])
        assert resolve_parsed(egress_parser, args) is not None

    def test_egress_still_admits_the_users_own_workspace(self, mount):
        own = mount / "Users" / "alice" / "letter.txt"
        own.write_text("x")
        parser = _one_verb_parser(EGRESS)
        args = parser.parse_args(["go", "--file", str(own)])
        assert resolve_parsed(parser, args) is None
        assert args.file == str(own)

    def test_read_admits_talk_and_egress_does_not(self, mount):
        shared = mount / "Talk" / "attachment.pdf"
        shared.write_text("x")
        read_parser = _one_verb_parser(READ)
        args = read_parser.parse_args(["go", "--file", str(shared)])
        assert resolve_parsed(read_parser, args) is None

        egress_parser = _one_verb_parser(EGRESS)
        args = egress_parser.parse_args(["go", "--file", str(shared)])
        assert resolve_parsed(egress_parser, args) is not None

    @pytest.mark.parametrize("mode", [REPO, REMOTE, NOT_A_PATH])
    def test_a_pass_through_mode_leaves_the_value_untouched(self, mount, mode):
        parser = _one_verb_parser(mode, note="somewhere else")
        args = parser.parse_args(["go", "--file", "/etc/hosts"])
        assert resolve_parsed(parser, args) is None
        assert args.file == "/etc/hosts"

    def test_the_same_dest_resolves_by_command_not_by_name(self, mount):
        """The load-bearing one, and the reason the walk descends the namespace.

        `get --file` is a read and `put --file` is a write. Keyed on the dest
        alone, one of the two gets the other's rule: the read would skip its
        existence and symlink checks, and the write would be refused for naming
        something that does not exist yet.
        """
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        host_path(sub.add_parser("get"), "--file", mode=READ)
        host_path(sub.add_parser("put"), "--file", mode=WRITE)

        missing = mount / "Users" / "alice" / "not-there.txt"
        args = parser.parse_args(["put", "--file", str(missing)])
        assert resolve_parsed(parser, args) is None

        args = parser.parse_args(["get", "--file", str(missing)])
        error = resolve_parsed(parser, args)
        assert error and "not found" in error.lower()

    def test_a_stamp_on_a_command_that_was_not_taken_is_not_resolved(self, mount):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        get = sub.add_parser("get")
        host_path(get, "--file", mode=READ)
        get.set_defaults(file=None)
        put = sub.add_parser("put")
        put.add_argument("--plain")
        args = parser.parse_args(["put", "--plain", "/etc/hosts"])
        assert resolve_parsed(parser, args) is None
        assert args.plain == "/etc/hosts"

    def test_no_roots_refuses_rather_than_widening(self, monkeypatch, tmp_path):
        for name in (
            "NEXTCLOUD_MOUNT_PATH", "ISTOTA_USER_ID",
            "ISTOTA_DEFERRED_DIR", "ISTOTA_CONVERSATION_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        source = tmp_path / "f.txt"
        source.write_text("x")
        parser = _one_verb_parser(READ)
        args = parser.parse_args(["go", "--file", str(source)])
        assert resolve_parsed(parser, args) is not None


class TestParseAndResolve:
    def test_it_returns_the_namespace_with_resolved_values(self, mount):
        target = mount / "Users" / "alice" / "notes.txt"
        target.write_text("x")
        parser = _one_verb_parser(READ)
        args = parse_and_resolve(parser, ["go", "--file", str(target)])
        assert args.file == str(target)

    def test_a_refusal_is_one_envelope_on_stdout_and_exit_1(self, mount, capsys):
        parser = _one_verb_parser(READ)
        with pytest.raises(SystemExit) as exc:
            parse_and_resolve(parser, ["go", "--file", "/etc/hosts"])
        assert exc.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["status"] == "error"
        assert payload["reason"] == "host_path_refused"
        assert "/etc/hosts" in payload["error"]

    def test_an_unstamped_parser_behaves_exactly_like_parse_args(self, mount):
        """The claim the conversion of twenty `main` functions rests on."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        go = sub.add_parser("go")
        go.add_argument("--file")
        go.add_argument("rest", nargs="*")
        argv = ["go", "--file", "/etc/hosts", "a", "b"]
        assert vars(parse_and_resolve(parser, argv)) == vars(parser.parse_args(argv))


def _skill_parser_modules() -> dict[str, str]:
    """Every skill exposing an argparse `build_parser`, by skill name.

    Re-derived rather than imported from
    `tests/test_skill_host_paths_coverage.py`: that file's walk is its own
    deliverable and importing a test module for a helper couples two
    collections together. The two agree on the tree, which is the only thing
    either of them claims.
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


class TestEverySkillMainGoesThroughIt:
    """The conversion, asserted on the tree rather than on a list.

    A skill whose `main` still calls `parse_args` is a skill where a later
    stamp is declared and never enforced — the argument is scoped in the
    parser and unscoped in the process. Converting all twenty rather than the
    twelve that hold a path today is what makes a path argument added to a
    currently path-free skill enforced without a second edit.
    """

    def test_the_walk_reaches_the_skills_it_is_meant_to_check(self):
        modules = _skill_parser_modules()
        assert len(modules) >= 20, sorted(modules)
        for name in ("email", "health", "money", "nextcloud", "whisper", "tasks"):
            assert name in modules, name

    @pytest.mark.parametrize("skill", sorted(_skill_parser_modules()))
    def test_main_parses_through_parse_and_resolve(self, skill):
        module = importlib.import_module(_skill_parser_modules()[skill])
        source = Path(module.__file__).read_text()
        main_source = source[source.index("\ndef main("):]
        assert "parse_and_resolve(" in main_source, (
            f"{skill}.main does not parse through parse_and_resolve; a stamped "
            f"argument in this skill would be declared and never enforced."
        )

    @pytest.mark.parametrize("skill", sorted(_skill_parser_modules()))
    def test_every_subparsers_action_declares_a_dest(self, skill):
        module = importlib.import_module(_skill_parser_modules()[skill])
        missing = subparsers_without_dest(module.build_parser())
        assert missing == [], (
            f"{skill}: {missing} declare subparsers with no dest, so "
            f"resolve_parsed cannot tell which verb it is resolving for."
        )

    @pytest.mark.parametrize("skill", sorted(_skill_parser_modules()))
    def test_no_dest_carries_two_modes_under_one_command(self, skill):
        module = importlib.import_module(_skill_parser_modules()[skill])
        assert stamp_conflicts(module.build_parser()) == []


def test_the_modes_are_the_six_the_spec_names():
    assert set(MODES) == {READ, EGRESS, WRITE, REPO, REMOTE, NOT_A_PATH}
