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
walked instead. Every skill's argparse parser is built, every subparser is
descended, and every argument that could name a path has to carry an explicit
disposition. A new argument fails this test rather than shipping unguarded.

**There are two places a disposition can live, and one of them is going away.**
Since ISSUE-447 the disposition belongs on the *declaration*
(``_hostpath.host_path``, read back by ``stamps()`` below), where the same
value the enforcement reads is the one this walk sees — so an argument that is
declared is enforced, and there is no second list to keep in step. ``REGISTRY``
is what is left for the arguments that have not been converted yet, and it
shrinks to nothing as the remaining stages land. An argument may be in one or
the other and never both.

**What the registry claims, and what it does not.** ``SCOPED`` is a claim about
the code and is checked: the named guard function's source must call the named
helper, so removing the guard turns this red rather than leaving a registry
entry asserting a boundary that is gone. The other three dispositions are
*records*, not guarantees:

- ``REMOTE`` — the path names a place on another machine (a Nextcloud path, a
  path inside the devbox container), so the host allowlist is the wrong tool
  and the far side does its own scoping.
- ``NOT_A_PATH`` — the walk's help-text heuristic matched a value that is not a
  path at all. Registered rather than filtered, because narrowing the heuristic
  to exclude it is how the next real one gets missed.
- ``UNSCOPED`` — a host path that goes through no allowlist. **These are open
  gaps**, listed so they are countable and so the next one added to the tree has
  to be added here too. Reading a green run of this file as "every host path is
  scoped" is a misreading; read it as "every host path is enumerated".

The unscoped list is the deliverable this file exists to produce. Six of the
entries below came out of the first run and were not in the issue that
motivated the work: ``nextcloud files upload --local`` reads any host file and
puts it in Nextcloud, ``nextcloud files download --local`` writes anywhere on
the host, ``email --body-file`` reads any host file into an outgoing message,
``email attachments --dest`` writes into any directory (and its docstring says
"scoped", which is where a stale claim ends up), and the ``health`` file verbs
read any host file on a deployment where the deferred path does not apply.

Being *stamped* is not enough on its own either, and the difference matters
when reading a green run here: this file says an argument has a disposition,
and ``tests/test_skill_host_paths_refusals.py`` is what drives each stamped
argument through its own CLI and requires the refusal and the acceptance.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

from istota.skills._hostpath import REPO as HOSTPATH_REPO, stamped

REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO / "src" / "istota" / "skills"

SCOPED = "scoped"
REMOTE = "remote"
NOT_A_PATH = "not_a_path"
UNSCOPED = "unscoped"


@dataclass(frozen=True)
class Entry:
    """What is known about one path-shaped CLI argument.

    ``guard`` and ``helper`` are required for ``SCOPED`` and are what makes the
    claim checkable: ``helper`` must appear in ``guard``'s source. ``note`` is
    required for everything else, because the other three dispositions are
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
    # One entry left. Everything that used to be here is *stamped* now — the
    # disposition is on the argument, in the parser, and `stamps()` below reads
    # it back. `memory_search index file` is the last argument scoped by a
    # guard rather than by a declaration, and it is stage 5 of the spec.
    ("memory_search", "index.file", "path"): Entry(
        SCOPED, guard="istota.skills.memory_search.cmd_index_file",
        helper="resolve_in_roots",
        note="the shared rule over `env_host_roots(talk=False)`: the indexed "
             "content comes back through `search`, so Talk is not a root",
    ),

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

    # -- Unscoped: open gaps, listed so they are countable -------------------
    ("email", "attachments", "dest"): Entry(
        UNSCOPED,
        note="host write: attachments are saved into any directory named. The "
             "docstring on cmd_attachments says 'scoped' and nothing scopes it.",
    ),
    ("email", "send", "body_file"): Entry(
        UNSCOPED, note="host read: any file becomes the body of an outgoing message",
    ),
    ("email", "reply", "body_file"): Entry(
        UNSCOPED, note="host read: any file becomes the body of an outgoing message",
    ),
    ("email", "reply-all", "body_file"): Entry(
        UNSCOPED, note="host read: any file becomes the body of an outgoing message",
    ),
    ("email", "output", "body_file"): Entry(
        UNSCOPED, note="host read: any file becomes the body of a deferred reply",
    ),
    ("health", "upload", "file_path"): Entry(
        UNSCOPED,
        note="host read. Sandboxed it defers, and scheduler_deferred."
             "_source_path_allowed scopes the replay; unsandboxed it reads the "
             "path here with nothing in front of it.",
    ),
    ("health", "import-csv", "file_path"): Entry(
        UNSCOPED, note="host read; same deferred-only scoping as `upload`",
    ),
    ("health", "attach-document", "path"): Entry(
        UNSCOPED, note="host read; same deferred-only scoping as `upload`",
    ),
    ("health", "import-immunizations", "paste"): Entry(
        UNSCOPED, note="host read: a leading @ makes the value a path to read",
    ),
    ("money", "import-csv", "file"): Entry(UNSCOPED, note="host read"),
    ("money", "portfolio.import", "file"): Entry(UNSCOPED, note="host read"),
    ("nextcloud", "files.upload", "local"): Entry(
        UNSCOPED, note="host read: any host file is uploaded to Nextcloud",
    ),
    ("nextcloud", "files.download", "local"): Entry(
        UNSCOPED, note="host write: the download lands wherever this names",
    ),
    ("transcribe", "ocr", "image_path"): Entry(
        UNSCOPED, note="host read; the OCR text comes back to the caller",
    ),
    ("whisper", "transcribe", "audio_path"): Entry(
        UNSCOPED, note="host read; the transcript comes back to the caller",
    ),
}

#: A `cli: true` skill whose CLI this walk cannot reach, and why. Held to the
#: same discipline as `test_lint_scope`'s extend-include list: an exemption is
#: named one at a time, never inferred, so a skill that stops exposing a parser
#: fails rather than silently leaving the walk.
UNWALKABLE: dict[str, str] = {
    "briefings": "a bare Click passthrough: argv goes straight to briefings.cli",
    "google_workspace": "os.execvp('gws'): the arguments are another program's",
}

#: Argument dests that name a path whatever the help text says.
_PATH_DESTS = frozenset({
    "output", "path", "src", "dest", "value_file", "attach", "file", "worktree",
})


def _module_for(skill_dir: Path) -> str | None:
    """The dotted module exposing this skill's parser, or None."""
    for candidate in ("__init__", "cli"):
        source = skill_dir / f"{candidate}.py"
        if source.exists() and "def build_parser" in source.read_text():
            suffix = "" if candidate == "__init__" else ".cli"
            return f"istota.skills.{skill_dir.name}{suffix}"
    return None


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


def _walk(parser: argparse.ArgumentParser, trail: list[str], found: list):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                _walk(sub, [*trail, name], found)
        elif _is_path_shaped(action):
            found.append((".".join(trail), action.dest))


def discovered() -> dict[tuple[str, str, str], str]:
    """Every path-shaped argument in every walkable skill parser."""
    out: dict[tuple[str, str, str], str] = {}
    for skill_dir in _skill_dirs():
        module_name = _module_for(skill_dir)
        if module_name is None:
            continue
        module = importlib.import_module(module_name)
        found: list = []
        _walk(module.build_parser(), [], found)
        for command, dest in found:
            out[(skill_dir.name, command, dest)] = module_name
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
        if module_name is None:
            continue
        module = importlib.import_module(module_name)
        for command, dest, mode in stamped(module.build_parser()):
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
    assert len(found) >= 14, sorted(found)
    for key in [
        ("browse", "screenshot", "output"),
        ("kv", "set", "value_file"),
        ("devbox", "cp-out", "dest"),
        ("feeds", "import-opml", "path"),
        ("health", "export-csv", "output"),
        ("code_review", "run", "worktree"),
        ("email", "send", "attach"),
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
    source = inspect.getsource(_resolve(entry.guard))
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
    assert entry.disposition in (REMOTE, NOT_A_PATH, UNSCOPED), entry.disposition
    assert entry.note, f"{key} is {entry.disposition} and says nothing about why"


def test_every_cli_skill_is_walkable_or_exempted():
    """The walk only sees argparse, and not every skill CLI is argparse.

    A skill whose CLI is a Click passthrough or an `execvp` has arguments this
    file cannot enumerate. Naming each one keeps that a decision rather than a
    blind spot — and makes a skill that *loses* its parser fail here instead of
    quietly dropping out of the enumeration.
    """
    unreachable = sorted(
        d.name for d in _skill_dirs()
        if _declares_cli(d) and _module_for(d) is None
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
