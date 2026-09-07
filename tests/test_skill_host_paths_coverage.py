"""Every skill CLI argument that names a path is accounted for, by name.

A skill CLI runs host-side: the proxy spawns it outside the sandbox with the
daemon's whole filesystem view, precisely so it can reach the databases the
model cannot. So any verb taking a *host* path is an arbitrary-file read or
write unless it is scoped, and the model chooses the path.
``src/istota/skill_host_paths.py`` holds the rule. What nothing held was the
*list of places the rule has to be applied*: it lived in that module's
docstring, was written by hand, and had gone stale — three write verbs and one
read were outside it when this file was written, and the module still described
its consumers as three.

The shape is `tests/test_lint_scope.py`'s and is here for the same reason: a
hand-maintained list that nothing walks goes stale in silence. So the tree is
walked instead. Every skill's parser is built — argparse through
`build_parser`, Click through `build_cli` — every subcommand is descended, and
every argument that could name a path has to carry an explicit disposition. A
new argument fails this test rather than shipping unguarded.

**There are two places a disposition can live, and the declaration is the
one to reach for.** Since ISSUE-447 a stamp goes on the argument
(``_hostpath.host_path`` or ``_hostpath.click_host_path``, read back by
``stamps()`` below), where the value the enforcement reads is the one this
walk sees — so an argument that is declared is enforced, and there is no
second list to keep in step. ``REGISTRY`` is what is left for the arguments a
stamp cannot express: a path this allowlist does not govern, and a value the
heuristic matched that is not a path at all. An argument may be in one or the
other and never both.

**What the registry claims, and what it does not.** ``SCOPED`` is a claim about
the code and is checked: the named guard function's source must call the named
helper, so removing the guard turns this red rather than leaving a registry
entry asserting a boundary that is gone. The other two dispositions are
*records*, not guarantees:

- ``REMOTE`` — the path names a place on another machine (a Nextcloud path, a
  path inside the devbox container), so the host allowlist is the wrong tool
  and the far side does its own scoping.
- ``NOT_A_PATH`` — the walk's help-text heuristic matched a value that is not a
  path at all. Registered rather than filtered, because narrowing the heuristic
  to exclude it is how the next real one gets missed.

**There is no way to record a gap, and that is the point.** ``UNSCOPED`` was
the third record and the deliverable this file existed to produce: sixteen host
paths that went through no allowlist, six of them found by the first run of
this walk and in no issue. It is gone with its last entry (ISSUE-447 stage 7),
because a disposition whose only behaviour is to fail is a second way to be red
for a state the walk already reports — an unstamped argument fails by name,
with its verb and dest in the message — and an available way to record a gap is
an invitation to record one. So a green run of this file now says every
path-shaped argument in a walkable skill CLI is stamped, scoped by a checked
guard, or recorded as naming somewhere this allowlist does not govern.

Two things it still does not say. Being *stamped* is a disposition rather than
a driven refusal: ``tests/test_skill_host_paths_refusals.py`` is what runs each
stamped argument through its own CLI and requires both the refusal and the
acceptance. And one skill is outside the walk altogether —
``google_workspace`` execs a program that is not in this tree, so its arguments
belong to somebody else's parser and are bounded by an argv scan
(``tests/test_skill_host_paths_gws.py``) rather than enumerated. That is a
weaker claim than the rest of this file makes and ``UNWALKABLE`` says so.
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from istota.skills._hostpath import (
    REPO as HOSTPATH_REPO,
    click_commands,
    click_stamped,
    stamped,
)
from tests.support.drift import source_of

REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO / "src" / "istota" / "skills"

SCOPED = "scoped"
REMOTE = "remote"
NOT_A_PATH = "not_a_path"


@dataclass(frozen=True)
class Entry:
    """What is known about one path-shaped CLI argument.

    ``guard`` and ``helper`` are required for ``SCOPED`` and are what makes the
    claim checkable: ``helper`` must appear in ``guard``'s source. ``note`` is
    required for everything else, because the other two dispositions are
    assertions about the world that no test can settle.
    """

    disposition: str
    guard: str = ""
    helper: str = ""
    note: str = ""


#: Keyed on (skill, dotted subcommand path, argparse dest).
REGISTRY: dict[tuple[str, str, str], Entry] = {
    # -- Scoped: the path goes through the shared allowlist ------------------
    #
    # Empty. Every argument that was here is *stamped* now — the disposition
    # is on the argument, in the parser, and `stamps()` below reads it back.
    # `SCOPED` survives as a disposition because an argument a stamp cannot
    # reach may still be scoped by a guard. `UNSCOPED` did not survive its
    # last entry, which is the difference between a record and an invitation.

    # -- Remote: the path names somewhere else -------------------------------
    ("nextcloud", "share.list", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "share.create", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "share.link", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "share.link", "file"): Entry(
        REMOTE, note="a file name inside the shared Nextcloud folder",
    ),
    ("nextcloud", "share.revoke", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "files.stat", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "files.list", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "files.versions", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "files.restore-version", "path"): Entry(
        REMOTE, note="Nextcloud path",
    ),
    ("nextcloud", "files.favorite", "path"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "files.upload", "remote"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "files.download", "remote"): Entry(REMOTE, note="Nextcloud path"),
    ("nextcloud", "talk.share-file", "path"): Entry(REMOTE, note="Nextcloud path"),

    # -- Not a path at all: the help-text heuristic matched something else ----
    ("memory_search", "index.file", "source_type"): Entry(
        NOT_A_PATH, note="a source-type label whose default is 'memory_file'",
    ),
    ("money", "monarch-category-map.list", "profile"): Entry(
        NOT_A_PATH, note="a Monarch profile name",
    ),
    ("money", "monarch-category-map.set", "profile"): Entry(
        NOT_A_PATH, note="a Monarch profile name",
    ),
    ("nextcloud", "user.search", "item_type"): Entry(
        NOT_A_PATH, note="a sharee item type; the help names 'file' as its default",
    ),
    ("nextcloud", "share.search", "item_type"): Entry(
        NOT_A_PATH, note="a sharee item type; the help names 'file' as its default",
    ),
    ("nextcloud", "files.restore-version", "version"): Entry(
        NOT_A_PATH, note="a version id from `files versions`",
    ),
    ("nextcloud", "activity.list", "type"): Entry(
        NOT_A_PATH, note="an activity filter name; the help names 'files'",
    ),

    ("health", "import-immunizations", "paste"): Entry(
        NOT_A_PATH,
        note="literal text. `@PATH` used to make it a path and no longer "
             "does: a leading @ is refused with a message naming "
             "--paste-file, which is the stamped read.",
    ),

    # There is no fourth section. `UNSCOPED` held sixteen entries and is
    # deleted with its last one: an unstamped argument already fails this walk
    # by name, and a second way to be red is a way to stay green.
}

#: A `cli: true` skill whose CLI this walk cannot reach, and why. Held to the
#: same discipline as `test_lint_scope`'s extend-include list: an exemption is
#: named one at a time, never inferred, so a skill that stops exposing a parser
#: fails rather than silently leaving the walk.
UNWALKABLE: dict[str, str] = {
    "google_workspace": (
        "os.execvp('gws'): the arguments belong to a program that is not in "
        "this tree and cannot be enumerated. What stands in for enumeration "
        "is the argv scan in google_workspace.main — a path-shaped token is "
        "resolved through the shared allowlist before the exec, and the "
        "passthrough's cwd is the user's own workspace so a relative one "
        "lands in-roots by construction. Coarser than a declaration and "
        "self-maintaining, which a per-verb policy table for a program we do "
        "not ship would not be. Driven by tests/test_skill_host_paths_gws.py."
    ),
}

#: Argument dests that name a path whatever the help text says.
_PATH_DESTS = frozenset({
    "output", "path", "src", "dest", "value_file", "attach", "file", "worktree",
})


def _module_declaring(skill_dir: Path, function: str) -> str | None:
    """The dotted skill module defining `function`, or None.

    Source text rather than an import, so a skill whose module raises on
    import is a failure at the walk rather than a skill that quietly leaves
    the enumeration.
    """
    for candidate in ("__init__", "cli"):
        source = skill_dir / f"{candidate}.py"
        if source.exists() and f"def {function}" in source.read_text():
            suffix = "" if candidate == "__init__" else ".cli"
            return f"istota.skills.{skill_dir.name}{suffix}"
    return None


def _module_for(skill_dir: Path) -> str | None:
    """The dotted module exposing this skill's argparse parser, or None."""
    return _module_declaring(skill_dir, "build_parser")


def _click_module_for(skill_dir: Path) -> str | None:
    """The dotted module exposing this skill's Click group, or None.

    `build_cli` is to Click what `build_parser` is to argparse, and the
    symmetry is the whole mechanism: a Click skill is walkable by exposing
    the function, not by being named in a list here. `briefings` is the one
    today, which is what took it out of `UNWALKABLE`.
    """
    return _module_declaring(skill_dir, "build_cli")


def _skill_dirs() -> list[Path]:
    return [
        d for d in sorted(SKILLS_DIR.iterdir())
        if d.is_dir() and not d.name.startswith("_") and (d / "skill.md").exists()
    ]


def _declares_cli(skill_dir: Path) -> bool:
    for line in (skill_dir / "skill.md").read_text().splitlines():
        if line.strip().startswith("cli:"):
            return line.split(":", 1)[1].strip().lower() == "true"
    return False


def _is_path_shaped(action: argparse.Action) -> bool:
    """Whether this argument could be naming a path.

    Deliberately wide: a dest that names one outright, a dest suffixed like
    one, or help text that mentions a path, a file or a directory. A flag
    (`nargs == 0`) and an argument with `choices` are excluded, since neither
    can carry a path. Over-matching costs a registry line; under-matching is
    the failure this file exists to prevent.
    """
    if action.nargs == 0 or action.choices:
        return False
    if action.dest in _PATH_DESTS:
        return True
    if action.dest.endswith(("_path", "_file", "_dir")):
        return True
    help_text = (action.help or "").lower()
    return any(word in help_text for word in ("path", "file", "directory"))


def _is_click_path_shaped(param) -> bool:
    """The same heuristic over a Click parameter, plus the type Click has.

    `click.Path` and `click.File` are the signals argparse has no equivalent
    of and they are the strongest of the five: a parameter declaring either
    *is* a path however its help text reads, and `click.File` opens one by
    definition. The rest mirrors `_is_path_shaped` — a flag carries no path
    and `click.Choice` bounds the value to a list that is not one.

    A `click.Argument` carries no `help` at all, so for a positional the type
    is often the only signal there is; missing one of the two types would
    leave the Click walk weaker than the argparse one, which does see a
    positional's help text.
    """
    import click

    if getattr(param, "is_flag", False):
        return False
    if isinstance(param.type, (click.Path, click.File)):
        return True
    if isinstance(param.type, click.Choice):
        return False
    if param.name in _PATH_DESTS or param.name.endswith(("_path", "_file", "_dir")):
        return True
    help_text = (getattr(param, "help", "") or "").lower()
    return any(word in help_text for word in ("path", "file", "directory"))


def _walk(parser: argparse.ArgumentParser, trail: list[str], found: list):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                _walk(sub, [*trail, name], found)
        elif _is_path_shaped(action):
            found.append((".".join(trail), action.dest))


def _walk_click(group, found: list):
    for dotted, command in click_commands(group):
        for param in command.params:
            if _is_click_path_shaped(param):
                found.append((dotted, param.name))


def discovered() -> dict[tuple[str, str, str], str]:
    """Every path-shaped argument in every walkable skill CLI.

    Both kinds of parser, keyed identically: the dotted command as the walk
    spells it and the dest — `param.name` on the Click side, which is what a
    Click command's own signature receives.
    """
    out: dict[tuple[str, str, str], str] = {}
    for skill_dir in _skill_dirs():
        found: list = []
        module_name = _module_for(skill_dir)
        if module_name is not None:
            _walk(importlib.import_module(module_name).build_parser(), [], found)
        click_module_name = _click_module_for(skill_dir)
        if click_module_name is not None:
            _walk_click(importlib.import_module(click_module_name).build_cli(), found)
        for command, dest in found:
            out[(skill_dir.name, command, dest)] = module_name or click_module_name
    return out


def stamps() -> dict[tuple[str, str, str], str]:
    """Every argument carrying a disposition on its own declaration.

    The same walk, reading `istota.skills._hostpath.stamped` instead of the
    help-text heuristic. Keyed identically, so the two enumerations of one
    tree compare key for key.
    """
    out: dict[tuple[str, str, str], str] = {}
    for skill_dir in _skill_dirs():
        module_name = _module_for(skill_dir)
        if module_name is not None:
            module = importlib.import_module(module_name)
            for command, dest, mode in stamped(module.build_parser()):
                out[(skill_dir.name, command, dest)] = mode
        click_module_name = _click_module_for(skill_dir)
        if click_module_name is not None:
            module = importlib.import_module(click_module_name)
            for command, dest, mode in click_stamped(module.build_cli()):
                out[(skill_dir.name, command, dest)] = mode
    return out


def _resolve(dotted: str):
    module_name, _, attribute = dotted.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def test_every_path_shaped_argument_is_accounted_for():
    """Stamped, or registered. An argument that is neither is an open hole.

    The stamp is the disposition that lives *on the declaration* and is read
    back by the enforcement (`resolve_parsed`), so an argument that carries one
    is scoped by the fact of being parsed. `REGISTRY` is what remains for the
    arguments a stamp cannot yet express, and it shrinks to nothing as the
    ISSUE-447 stages land.
    """
    accounted = set(REGISTRY) | set(stamps())
    unregistered = sorted(k for k in discovered() if k not in accounted)
    assert not unregistered, (
        f"{unregistered} name a path and carry no disposition. A skill CLI "
        f"runs host-side with the daemon's filesystem view, so a host path the "
        f"model chooses is an arbitrary read or write unless it goes through "
        f"istota.skill_host_paths. Declare it with `_hostpath.host_path`, or "
        f"register why it needs no scoping."
    )


def test_the_registry_holds_nothing_that_no_longer_exists():
    """A stale entry is how the list stops describing the tree."""
    live = discovered()
    stale = sorted(k for k in REGISTRY if k not in live)
    assert not stale, (
        f"{stale} are in REGISTRY and in no skill parser. Remove them; a "
        f"registry carrying arguments that do not exist cannot be read as a "
        f"description of the tree."
    )


def test_no_argument_is_both_stamped_and_registered():
    """Two dispositions for one argument is the drift this file exists under.

    The registry entry would go on asserting a guard that the conversion to a
    stamp deleted — which is exactly the stale-claim failure `SCOPED`'s helper
    check was added to catch, one level up.
    """
    both = sorted(set(REGISTRY) & set(stamps()))
    assert not both, (
        f"{both} carry a stamp on the declaration and a REGISTRY entry. Delete "
        f"the entry: the stamp is the disposition, and the enforcement reads it."
    )


def test_the_stamps_reach_the_arguments_they_are_meant_to_cover():
    """A stamp walk over an empty set accounts for everything, vacuously."""
    found = stamps()
    assert len(found) >= 28, sorted(found)
    for key in [
        ("browse", "screenshot", "output"),
        ("kv", "set", "value_file"),
        ("devbox", "cp-out", "dest"),
        ("feeds", "import-opml", "path"),
        ("health", "export-csv", "output"),
        ("code_review", "run", "worktree"),
        ("email", "send", "attach"),
        ("email", "send", "body_file"),
        ("health", "import-csv", "file_path"),
        ("memory_search", "index.file", "path"),
        ("money", "portfolio.import", "file"),
        ("nextcloud", "files.upload", "local"),
        ("whisper", "transcribe", "audio_path"),
    ]:
        assert key in found, f"{key} carries no stamp"


@pytest.mark.parametrize(
    "key", sorted(k for k, mode in stamps().items() if mode == HOSTPATH_REPO),
)
def test_a_repo_stamp_names_a_handler_that_still_scopes(key):
    """`REPO` is the one resolving-shaped disposition nothing else can check.

    `resolve_parsed` skips it and the refusals file never parametrizes over
    it, so the stamp alone is a `note` — and a note survives the deletion of
    the call it describes. That is exactly the stale claim `SCOPED`'s helper
    assertion exists to catch, and converting `code_review --worktree` from a
    registry entry to a stamp would otherwise have traded a checked claim for
    an unchecked one. So the source assertion moves onto the stamp: a skill
    declaring a `REPO` argument has to call `resolve_under_repos` somewhere.

    Module-wide rather than function-wide, since the stamp names the argument
    and not the handler; `tests/test_code_review_cli.py` is what drives the
    refusal itself.
    """
    skill = key[0]
    module = importlib.import_module(_module_for(SKILLS_DIR / skill))
    source = Path(module.__file__).read_text()
    assert "resolve_under_repos(" in source, (
        f"{key} is stamped REPO, which records that the handler scopes it "
        f"against DEVELOPER_REPOS_DIR, and {skill} calls resolve_under_repos "
        f"nowhere."
    )


@pytest.mark.parametrize("declare", ["path", "file"])
def test_the_click_heuristic_sees_both_of_clicks_own_path_types(declare):
    """A positional carries no help text, so the type is the only signal.

    `click.Argument` has no `help` attribute at all, which makes the Click
    walk weaker than the argparse one unless both of Click's path-opening
    types are recognised. Under-matching is the failure this file exists to
    prevent.
    """
    import click

    @click.group()
    def cli():
        pass

    kind = click.Path() if declare == "path" else click.File("rb")

    @cli.command("go")
    @click.argument("target", type=kind)
    def go(target):
        pass

    found = []
    _walk_click(cli, found)
    assert found == [("go", "target")]


def test_no_skill_exposes_both_kinds_of_parser():
    """One key space, two walks, and nothing reconciles a collision.

    `discovered()` and `stamps()` write `(skill, dotted command, dest)` from
    both walks into one dict, and both spell the root command `""`. A skill
    exposing `build_parser` *and* `build_cli` could therefore have one entry
    overwrite the other and hide an unstamped argument. No skill does today;
    this is what keeps that true rather than assumed.
    """
    both = sorted(
        d.name for d in _skill_dirs()
        if _module_for(d) is not None and _click_module_for(d) is not None
    )
    assert both == [], (
        f"{both} expose both an argparse parser and a Click group, and the two "
        f"walks share one key space. Key on the parser kind before adding one."
    )


def test_the_click_walk_reaches_the_briefings_group():
    """The other half of the walk, which no argparse assertion can establish.

    `briefings` holds no path-shaped parameter today, so every count-based
    assertion in this file is satisfied whether the Click walk works or not —
    and an import that starts failing, a renamed `build_cli` or a group whose
    subcommands stop being descended would all leave it that way. What is
    asserted is therefore that the walk *arrives*: the group is reached and
    its nested commands are enumerated, so a `click.Path` parameter added to
    one of them lands in `discovered()` rather than in nobody's list. That is
    the deliverable — the walk being able to reach it, not the count.
    """
    module_name = _click_module_for(SKILLS_DIR / "briefings")
    assert module_name is not None, "briefings exposes no build_cli"
    group = importlib.import_module(module_name).build_cli()
    walked = {dotted for dotted, _ in click_commands(group)}
    for command in ("list", "blocks", "blocks.add", "sources.add", "archive.show"):
        assert command in walked, f"{command} not walked; walked {sorted(walked)}"


def test_the_walk_finds_the_arguments_it_is_meant_to_guard():
    """A guard over an empty set passes on any tree at all.

    Building parsers by import, descending subparsers and matching help text is
    enough machinery to go quietly wrong — a renamed directory, an import that
    starts failing, a heuristic that stops matching — and every one of those
    leaves the two tests above green with nothing discovered.
    """
    found = discovered()
    assert len(found) > 40, len(found)
    for key in [
        ("browse", "screenshot", "output"),
        ("kv", "set", "value_file"),
        ("devbox", "cp-out", "dest"),
        ("feeds", "import-opml", "path"),
        ("health", "export-csv", "output"),
        ("code_review", "run", "worktree"),
    ]:
        assert key in found, f"{key} not discovered; the walk is not reaching it"


@pytest.mark.parametrize(
    "key", sorted(k for k, v in REGISTRY.items() if v.disposition == SCOPED),
)
def test_a_scoped_entry_names_a_guard_that_calls_its_helper(key):
    """The drift guard, and the only claim in the registry a test can settle.

    Without it, an entry saying `scoped` survives the guard being deleted, and
    the registry then asserts a boundary that is not there — which is the
    failure mode of the hand-maintained list this file replaced, one level up.
    """
    entry = REGISTRY[key]
    assert entry.guard and entry.helper, key
    source = source_of(_resolve(entry.guard))
    # The open paren is load-bearing: a bare name match is satisfied by the
    # function-scope `from istota.skill_host_paths import resolve_host_path`
    # several of these guards carry, so deleting the call and leaving the
    # import would keep this green. Measured — it did.
    assert f"{entry.helper}(" in source, (
        f"{key} is registered as scoped by {entry.guard} calling "
        f"{entry.helper}, and that call is not in its source."
    )


@pytest.mark.parametrize(
    "key", sorted(k for k, v in REGISTRY.items() if v.disposition != SCOPED),
)
def test_an_unscoped_or_exempt_entry_says_why(key):
    entry = REGISTRY[key]
    assert entry.disposition in (REMOTE, NOT_A_PATH), entry.disposition
    assert entry.note, f"{key} is {entry.disposition} and says nothing about why"


def test_every_cli_skill_is_walkable_or_exempted():
    """Two kinds of parser are walked, and one kind of CLI cannot be.

    A skill that execs another program has arguments this file cannot
    enumerate at all. `briefings` looked like the same case and was not: a
    Click group is walkable with different machinery, which is what
    `_click_module_for` and `_walk_click` are, so the exemption is gone and
    only `google_workspace` is left. Naming that one keeps it a decision
    rather than a blind spot — and makes a skill that *loses* its parser fail
    here instead of quietly dropping out of the enumeration.
    """
    unreachable = sorted(
        d.name for d in _skill_dirs()
        if _declares_cli(d)
        and _module_for(d) is None
        and _click_module_for(d) is None
    )
    assert unreachable == sorted(UNWALKABLE), (
        f"skills with a CLI and no reachable argparse parser: {unreachable}; "
        f"exempted: {sorted(UNWALKABLE)}"
    )


def test_the_exemptions_are_still_cli_skills():
    """An exemption for a skill that no longer has a CLI is dead weight."""
    by_name = {d.name: d for d in _skill_dirs()}
    for name in UNWALKABLE:
        assert name in by_name, f"{name} is exempted and is not a skill"
        assert _declares_cli(by_name[name]), f"{name} is exempted and has no CLI"
