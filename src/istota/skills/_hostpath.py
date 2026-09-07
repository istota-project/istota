"""Declaring a skill CLI argument as a host path, and enforcing it at parse.

A skill CLI runs host-side. `skill_proxy` spawns it outside the sandbox as the
daemon user, precisely so it can reach the databases `build_bwrap_cmd` masks,
so any verb taking a *host* path is an arbitrary read or write unless it is
scoped — and the model chooses the path. `istota.skill_host_paths` holds the
containment rule. What this module holds is where the rule gets applied.

**The disposition lives on the argument, and the enforcement reads it back.**
Before ISSUE-447 it lived in `tests/test_skill_host_paths_coverage.py`'s
registry — two directories from the parser, hand-maintained, and a new argument
could be added without anybody seeing it. `host_path` is `add_argument` with a
stamp, so an argument that is declared is enforced and an argument that is not
declared fails the coverage walk. There is no third place to keep in step.

**It is not in the skill's frontmatter, and that is a decision.** Frontmatter
is the skill manifest and carries what a *skill* needs — `env`, `dependencies`,
`companion_skills` — which is constant across its verbs. A host-path
disposition is not: `nextcloud files upload` carries `--local` (a host read
that must be scoped) and `--remote` (a Nextcloud path that must not be touched)
side by side. Frontmatter could express that only by naming argparse dests in
YAML, which is the registry relocated rather than removed.

**Six names, four behaviours.** `READ`, `EGRESS` and `WRITE` resolve; `REPO`,
`REMOTE` and `NOT_A_PATH` pass the value through untouched and differ only in
what they tell the next reader — the other allowlist (`resolve_under_repos`,
applied by the handler), a path on another machine that the far side scopes,
and a help-text false positive. Collapsing the three into one `PASS` would save
a constant and cost the reader the sentence they need at the declaration, which
is why each of the three has to carry a `note`. A verb needing something none
of the six expresses is a missing mode, not a special case.

**Two root sets, and the mode picks which.** What varies is not who is asking
but where the bytes end up. `READ` gets the task's whole working context — its
own workspace, its deferred dir, its channel directory and `{mount}/Talk`,
mirroring what the sandbox binds. `EGRESS` and `WRITE` get the user's own
workspace alone, because their content *leaves* that context: mailed to an
address the model chose, indexed into something later searchable, uploaded, or
persisted for the daemon to read after the task is over. Talk is in the first
set and not the second for the same reason read the other way — a task may
legitimately read a Talk attachment into its own reasoning, which is a
different question from whether those bytes may be mailed out.

**A dest is not unique across a parser, so nothing here keys on one.**
`--path`, `--file` and `--output` recur across verbs; `health` has 44 path
arguments and `money` nests two levels. Resolving a `READ` as a `WRITE` skips
the existence and symlink checks *and* names a destination that need not exist;
the other direction refuses a file that is there. So `resolve_parsed` descends
the parsers actually taken, reading each `add_subparsers` dest off the parsed
namespace — which is why every `add_subparsers` in a skill tree has to carry
one, asserted by `tests/test_skill_hostpath_declaration.py` rather than assumed.

**Resolution writes `str` back onto the namespace, not `Path`.** Argparse hands
every value to a handler as `str` today and `money`'s two conversions splice
one straight into a `CliRunner` argv list, which takes a sequence of strings.
`str` is type-preserving by construction, which is what lets every skill `main`
be converted in one behaviour-preserving step. It also retires "callers must
use the returned resolved path" as a rule each handler has to remember:
`args.file_path` already *is* the resolved path.

Imports `argparse`, `logging`, `pathlib` and `istota.skill_host_paths`, which
is itself a stdlib-only leaf — so a skill subprocess pays nothing beyond what
`istota.skills.__init__` already costs.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from istota.skill_host_paths import (
    env_host_roots,
    resolve_in_roots,
    user_workspace_root,
)

log = logging.getLogger(__name__)

READ = "read"              # resolved against the task's whole working context
EGRESS = "egress"          # a read whose bytes leave the task: own workspace only
WRITE = "write"            # a destination: own workspace only
REPO = "repo"              # the other allowlist: resolve_under_repos
REMOTE = "remote"          # a path on another machine; the far side scopes it
NOT_A_PATH = "not_a_path"  # the coverage walk's help-text heuristic matched

MODES = (READ, EGRESS, WRITE, REPO, REMOTE, NOT_A_PATH)

#: The three that touch a path. The other three pass the value through.
RESOLVING = (READ, EGRESS, WRITE)

#: Attributes set on the argparse action. Named rather than inlined so the
#: coverage walk and this module cannot disagree about the spelling.
STAMP = "istota_host_path"
NOTE = "istota_host_path_note"


def host_path(
    parser: argparse.ArgumentParser, *names: str, mode: str, note: str = "", **kwargs,
) -> argparse.Action:
    """`parser.add_argument(*names, **kwargs)`, with the disposition on it.

    Returns the action argparse registered, so a caller that wants to keep
    fiddling with it can. `note` is required for the three pass-through modes
    and optional for the three that resolve: a mode that resolves is checkable
    by running it, and a mode that does not is an assertion about the world
    that only a sentence can carry.
    """
    if mode not in MODES:
        raise ValueError(f"unknown host-path mode {mode!r}; one of {MODES}")
    if mode not in RESOLVING and not note:
        raise ValueError(
            f"mode {mode!r} passes the value through unscoped and needs a note "
            f"saying why: it is a record, not a guarantee"
        )
    action = parser.add_argument(*names, **kwargs)
    setattr(action, STAMP, mode)
    setattr(action, NOTE, note)
    return action


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _children(
    parser: argparse.ArgumentParser,
) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    action = _subparsers(parser)
    if action is None:
        return
    yield from action.choices.items()


def stamped(parser: argparse.ArgumentParser) -> list[tuple[str, str, str]]:
    """Every stamped argument in the tree, as `(dotted command, dest, mode)`.

    The dotted command is the coverage walk's spelling — `""` for an argument
    on the top-level parser, `"files.upload"` for one two levels down — so the
    two enumerations of the same tree can be compared key for key.

    A subparser action with no dest is walked past rather than stopping the
    walk: under-reporting the tree is how the coverage check downstream reads
    as green with arguments it never saw. `subparsers_without_dest` is what
    names that fault.
    """
    out: list[tuple[str, str, str]] = []

    def walk(current: argparse.ArgumentParser, trail: list[str]) -> None:
        dotted = ".".join(trail)
        for action in current._actions:
            mode = getattr(action, STAMP, None)
            if mode is not None:
                out.append((dotted, action.dest, mode))
        for name, child in _children(current):
            walk(child, [*trail, name])

    walk(parser, [])
    return out


def subparsers_without_dest(parser: argparse.ArgumentParser) -> list[str]:
    """Dotted commands whose `add_subparsers` declares no `dest`.

    The precondition for resolving per command rather than per dest: without a
    dest there is nothing on the namespace saying which verb was taken, so a
    stamp below that point could only be applied by name — which is the
    ambiguity the whole design avoids. `add_subparsers()` with no `dest`
    leaves it `argparse.SUPPRESS`.
    """
    out: list[str] = []

    def walk(current: argparse.ArgumentParser, trail: list[str]) -> None:
        action = _subparsers(current)
        if action is not None and action.dest == argparse.SUPPRESS:
            out.append(".".join(trail))
        for name, child in _children(current):
            walk(child, [*trail, name])

    walk(parser, [])
    return out


def stamp_conflicts(
    parser: argparse.ArgumentParser,
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Dests carrying two different modes on one root-to-verb path.

    Two sibling verbs stamping `--file` differently is the case the per-command
    resolution exists for and is not a conflict. The same dest stamped twice on
    one path — on the top-level parser and again on the verb, say — is: the
    resolution would apply both, in declaration order, and picking one silently
    would resolve a read as a write or the other way round.
    """
    out: list[tuple[str, str, tuple[str, ...]]] = []

    def walk(
        current: argparse.ArgumentParser,
        trail: list[str],
        inherited: dict[str, str],
    ) -> None:
        seen = dict(inherited)
        for action in current._actions:
            mode = getattr(action, STAMP, None)
            if mode is None:
                continue
            previous = seen.get(action.dest)
            if previous is not None and previous != mode:
                out.append((".".join(trail), action.dest, (previous, mode)))
            seen[action.dest] = mode
        for name, child in _children(current):
            walk(child, [*trail, name], seen)

    walk(parser, [], {})
    return out


def _own_roots(*, writable: bool) -> list[Path]:
    """`{mount}/Users/{ISTOTA_USER_ID}` alone, or nothing.

    `user_workspace_root` is the existing derivation of that one root and is
    asked for it rather than a fourth root list being built here. `writable` is
    accepted and unused: the workspace is the same directory either way, and
    taking it keeps the two branches of `_resolve_one` reading alike.
    """
    own = user_workspace_root()
    return [own] if own is not None else []


def _roots_for(mode: str, *, writable: bool) -> list[Path]:
    if mode == READ:
        return env_host_roots(writable=writable)
    return _own_roots(writable=writable)


def _operation(dotted: str, action: argparse.Action) -> str:
    """How the refusal names what was refused: the verb and the flag.

    Never the value of anything else on the namespace, and never the roots —
    the message goes back to the model, and naming the roots hands it another
    user's directory names.
    """
    flag = action.option_strings[0] if action.option_strings else action.dest
    return " ".join(part for part in (dotted.replace(".", " "), flag) if part)


def _resolve_one(
    value: object, mode: str, operation: str,
) -> tuple[str | None, str | None]:
    writable = mode == WRITE
    resolved, error = resolve_in_roots(
        Path(str(value)),
        _roots_for(mode, writable=writable),
        writable=writable,
        operation=operation,
    )
    if error is not None:
        return None, error
    return str(resolved), None


def _actions_on_path(
    parser: argparse.ArgumentParser, args: argparse.Namespace,
) -> Iterator[tuple[str, argparse.Action]]:
    """Every argument on the parsers this invocation actually descended.

    Reads each subparser action's dest off the namespace to pick the child, so
    a stamp under a verb that was not taken is never applied — which matters
    because a sibling verb may hold the same dest with a different mode, and
    because a `set_defaults` can leave that dest on the namespace regardless.
    """
    trail: list[str] = []
    current = parser
    while True:
        dotted = ".".join(trail)
        subparsers = _subparsers(current)
        for action in current._actions:
            if action is not subparsers:
                yield dotted, action
        if subparsers is None or subparsers.dest == argparse.SUPPRESS:
            return
        chosen = getattr(args, subparsers.dest, None)
        if not isinstance(chosen, str) or chosen not in subparsers.choices:
            return
        trail.append(chosen)
        current = subparsers.choices[chosen]


def resolve_parsed(
    parser: argparse.ArgumentParser, args: argparse.Namespace,
) -> str | None:
    """Resolve every stamped value on `args`, in place. The refusal, or None.

    A `None` value is skipped — the argument was not passed. A list or tuple
    resolves element by element and is written back in the same container type,
    and the first refusal refuses the whole call: `email --attach` naming three
    files, one of them outside the workspace, is one refused send rather than a
    send with two attachments.

    Returns the message rather than raising, matching `resolve_host_path`'s own
    convention. `_cli.parse_and_resolve` is what turns it into the facade's
    envelope; a caller that wants to do something else with it can.
    """
    for dotted, action in _actions_on_path(parser, args):
        mode = getattr(action, STAMP, None)
        if mode not in RESOLVING:
            continue
        value = getattr(args, action.dest, None)
        if value is None:
            continue
        operation = _operation(dotted, action)

        if isinstance(value, (list, tuple)):
            resolved_all: list[str] = []
            for element in value:
                if element is None:
                    continue
                resolved, error = _resolve_one(element, mode, operation)
                if error is not None:
                    log.warning("host path refused: %s", operation)
                    return error
                resolved_all.append(resolved)
            setattr(args, action.dest, type(value)(resolved_all))
            continue

        resolved, error = _resolve_one(value, mode, operation)
        if error is not None:
            log.warning("host path refused: %s", operation)
            return error
        setattr(args, action.dest, resolved)
    return None


__all__: Sequence[str] = (
    "EGRESS",
    "MODES",
    "NOTE",
    "NOT_A_PATH",
    "READ",
    "REMOTE",
    "REPO",
    "RESOLVING",
    "STAMP",
    "WRITE",
    "host_path",
    "resolve_parsed",
    "stamp_conflicts",
    "stamped",
    "subparsers_without_dest",
)
