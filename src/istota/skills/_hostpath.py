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

Nothing from the package beyond `istota.skill_host_paths`, itself a leaf over
`istota.user_scope` — so a skill subprocess pays nothing beyond what
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
    # The first is the only one: argparse raises on a second `add_subparsers`
    # against one parser, so there is no second to descend.
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

    **An `add_parser` alias yields one key per name, deliberately.**
    `calendar list|agenda` and `location current|last` each come back twice,
    because `action.choices` maps both names to the same parser and
    `test_skill_host_paths_coverage.py`'s walk iterates it the same way. That
    is what "compared key for key" above means; de-duplicating by parser
    identity here would make the two enumerations disagree.
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

    A dest that is stamped on the parent and left *unstamped* on the verb is
    reported with `""` as the second mode. That case is quieter and no less
    wrong: the verb's value is what the namespace carries, so it would be
    resolved under a mode declared for a different argument.
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
            previous = seen.get(action.dest)
            if mode is None:
                # An *unstamped* dest shadowing a stamped one on the same path
                # is the quieter half of the same fault and is reported too.
                # `resolve_parsed` reads `getattr(args, dest)`, which after the
                # child parse holds the child's value — so a stamped
                # `--file` on the parent plus a plain `--file` on the verb
                # resolves the verb's value under the parent's mode, with a
                # conflict walk that only looked at stamped pairs staying green.
                if previous is not None:
                    out.append((".".join(trail), action.dest, (previous, "")))
                    seen.pop(action.dest, None)
                continue
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
    """`TASK` for a `READ`, `OWN` for an `EGRESS` or a `WRITE`.

    **`OWN` is narrower than what every shipped host-path consumer uses
    today, and stamping an existing argument is therefore a behaviour
    change.** The six scoped call sites all go through
    `resolve_host_path(writable=True)`, whose roots are
    `env_host_roots(writable=True)` — the deferred dir and
    `{mount}/Channels/{token}` as well as the workspace. A `WRITE` stamp
    drops both. `browse screenshot --output`, `devbox cp-out --dest`,
    `feeds export-opml --output` and `health export-csv --output` are the
    four that will feel it, and each is declared in the stage that also
    carries its own control over the behaviour that is already known.

    `EGRESS` has the same shape against a different incumbent:
    `memory_search index file` states this idea today as
    `env_host_roots(talk=False)`, which drops `{mount}/Talk` and keeps the
    deferred and channel roots. That is a second spelling of egress and it
    is deliberately left standing until the stage that restamps that verb,
    since narrowing it is a boundary change that wants its own control
    rather than arriving as a side effect of the machinery landing.

    Nothing is stamped yet, so neither narrowing is live in this commit.
    """
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
    """Resolve one value under one mode. `(resolved str, None)` or `(None, error)`.

    **An empty or whitespace-only value is refused before a `Path` is built**,
    because `Path("")` is `Path(".")` — the process cwd, which for a proxied
    skill is the daemon's and for a task's own shell may well be inside the
    workspace. Measured: `--file ""` under a cwd in the workspace resolved
    clean and wrote the workspace *directory* back onto the namespace, turning
    an argument a handler would have failed to open into a valid path to
    somewhere it never named.

    A relative value is resolved against that same cwd and so is almost always
    refused, which is the documented behaviour rather than an oversight; the
    refusal names the resolved absolute path, so the cause is visible.
    """
    if value is None or not str(value).strip():
        return None, f"Empty path argument: {operation} refused."
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
    envelope; a caller that wants to do something else with it can — and such a
    caller should know that **`args` is left partially rewritten on a
    refusal**: the dests resolved before the refusing one are already absolute
    strings and the rest are as parsed, with nothing on the namespace saying
    which is which. `parse_and_resolve` exits, so no handler ever sees that.
    """
    for dotted, action in _actions_on_path(parser, args):
        mode = getattr(action, STAMP, None)
        if mode not in RESOLVING:
            continue
        value = getattr(args, action.dest, None)
        if value is None:
            continue
        operation = _operation(dotted, action)
        skill = parser.prog

        if isinstance(value, (list, tuple)):
            resolved_all: list[str] = []
            for element in value:
                # Refused rather than skipped. Dropping it shortens the list
                # silently, and a handler pairing a stamped list positionally
                # against another argument then reads the wrong element with
                # nothing having failed. Argparse's own `nargs` never produces
                # one, so reaching here means a `type=` callable or a
                # `set_defaults` put it there and the declaration is the bug.
                resolved, error = _resolve_one(element, mode, operation)
                if error is not None:
                    log.warning("host path refused: %s %s", skill, operation)
                    return error
                resolved_all.append(resolved)
            setattr(args, action.dest, type(value)(resolved_all))
            continue

        resolved, error = _resolve_one(value, mode, operation)
        if error is not None:
            log.warning("host path refused: %s %s", skill, operation)
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
