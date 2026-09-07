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
mirroring what the sandbox binds. `WRITE` gets the same set with the read-only
roots dropped, because a destination's content does not leave that context: it
lands in storage the task reads back and `/chat/files` serves. `EGRESS` gets
the same set with both *shared* roots dropped — the user's own workspace and
the task's own deferred dir — because its content *does* leave: mailed to an
address the model chose, indexed into something later searchable, uploaded, or
persisted for the daemon to read after the task is over. Talk is in the first
set and not the last for the same reason read the other way — a task may
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

**Click gets the same stamp through a different door.** `briefings` is a Click
group rather than an argparse tree, which is why the coverage walk could not
reach it. `click_host_path` stamps a Click parameter and wraps its callback,
so resolution happens during the parse and before the command body — where
argparse gets a post-parse pass because it has no per-argument hook that can
report through the facade. That asymmetry is not a second design: the argparse
`type=` callable was rejected because `parser.error()` prints usage on stderr
and exits 2, and Click under `standalone_mode=False` hands a callback's
exception straight back to the caller, which is `HostPathRefused` and one
envelope. `click_stamped` and `click_commands` are the walk over the other
tree, keyed the way the argparse walk keys, so one coverage check reads both.

Nothing from the package beyond `istota.skill_host_paths`, itself a leaf over
`istota.user_scope` — so a skill subprocess pays nothing beyond what
`istota.skills.__init__` already costs. Click is never imported here either:
the two walks and the decorator go through `__click_params__`, `.params` and
`.commands` by duck typing, so a skill CLI that has nothing to do with Click
does not pay for it.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from istota.skill_host_paths import env_host_roots, resolve_in_roots

log = logging.getLogger(__name__)

READ = "read"              # resolved against the task's whole working context
EGRESS = "egress"          # a read whose bytes leave the task: own workspace only
WRITE = "write"            # a destination: the task's roots, less the read-only ones
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


class HostPathRefused(Exception):
    """A stamped Click parameter naming a path outside the allowlist.

    The argparse side returns the message — `resolve_parsed`'s convention, and
    `resolve_host_path`'s before it — because it runs as a pass after the
    parse, where there is a caller to hand it to. A Click parameter callback
    has no such caller: its return value is the parameter's value, so a
    refusal has to raise. Under `standalone_mode=False` Click hands the
    exception back untouched, which is what lets the facade render the same
    envelope with the same `reason` as an argparse refusal — an exception type
    of our own rather than `click.UsageError`, whose message Click formats
    with usage text the model would then read as a malformed call.
    """


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
    _check_declaration(mode, note)
    action = parser.add_argument(*names, **kwargs)
    setattr(action, STAMP, mode)
    setattr(action, NOTE, note)
    return action


def _check_declaration(mode: str, note: str) -> None:
    """The two rules every declaration is held to, argparse or Click."""
    if mode not in MODES:
        raise ValueError(f"unknown host-path mode {mode!r}; one of {MODES}")
    if mode not in RESOLVING and not note:
        raise ValueError(
            f"mode {mode!r} passes the value through unscoped and needs a note "
            f"saying why: it is a record, not a guarantee"
        )


def click_host_path(mode: str, *, note: str = ""):
    """Stamp the Click parameter declared directly below this decorator.

    Used as::

        @cli.command("add")
        @click_host_path(READ)
        @click.option("--file")
        def add(file): ...

    Decorators run bottom-up, so by the time this one sees the function the
    `click.option` below it has already appended its `Parameter` to
    `__click_params__` and the last entry is that parameter. Placed *above*
    `@cli.command()` there is no such answer — Click reverses the list when it
    builds the command, so the last entry is then the bottom-most decorator
    rather than the one directly below — and a stamp on the wrong argument is
    worse than no stamp, since the coverage walk would call it accounted for.
    So that case raises.

    A resolving mode also wraps the parameter's callback, which is where the
    value is resolved and written back. Wrapped rather than replaced: a
    parameter may already have a callback of its own, and losing it silently
    is the kind of thing a stamp is not allowed to cost.
    """
    _check_declaration(mode, note)

    def decorator(f):
        params = getattr(f, "__click_params__", None)
        if not params:
            raise ValueError(
                "click_host_path found no parameter to stamp: put it directly "
                "above the click.option/click.argument it applies to, below "
                "the command decorator"
            )
        param = params[-1]
        setattr(param, STAMP, mode)
        setattr(param, NOTE, note)
        if mode in RESOLVING:
            param.callback = _click_resolver(mode, param.callback)
        return f

    return decorator


def _click_resolver(mode: str, previous):
    """A parameter callback that resolves the value before anything sees it."""

    def callback(ctx, param, value):
        if value is not None:
            operation = _click_operation(ctx, param)
            resolved, error = _resolve_value(value, mode, operation)
            if error is not None:
                log.warning("host path refused: %s", operation)
                raise HostPathRefused(error)
            value = resolved
        return previous(ctx, param, value) if previous is not None else value

    return callback


def _click_operation(ctx, param) -> str:
    """How the refusal names what was refused: the verb and the flag.

    `ctx.command_path` leads with the program name, which for a skill CLI is
    whatever Click was invoked as and says nothing; dropping it leaves the
    same "verb flag" spelling `_operation` produces on the argparse side.
    Never the roots, and never another argument's value.
    """
    path = str(getattr(ctx, "command_path", "") or "").split()
    flag = param.opts[0] if getattr(param, "opts", None) else param.name
    return " ".join([*path[1:], flag])


def click_commands(group) -> list[tuple[str, object]]:
    """`(dotted command, command)` for a Click group and everything under it.

    The root is `""`, matching the argparse walk's spelling for an argument on
    the top-level parser, so the two enumerations of one tree compare key for
    key. Duck-typed on `.commands` rather than `isinstance(click.Group)` so
    this module still imports nothing but the standard library and the
    allowlist.
    """
    out: list[tuple[str, object]] = []

    def walk(command, trail: list[str]) -> None:
        out.append((".".join(trail), command))
        children = getattr(command, "commands", None)
        if not children:
            return
        for name in sorted(children):
            walk(children[name], [*trail, name])

    walk(group, [])
    return out


def click_stamped(group) -> list[tuple[str, str, str]]:
    """Every stamped Click parameter, as `(dotted command, name, mode)`.

    `stamped`'s counterpart, keyed the same way — `param.name` is the Click
    side's dest, being the name the command's own signature receives.
    """
    return [
        (dotted, param.name, getattr(param, STAMP))
        for dotted, command in click_commands(group)
        for param in getattr(command, "params", [])
        if getattr(param, STAMP, None) is not None
    ]


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
    """`{mount}/Users/{ISTOTA_USER_ID}` and the caller's own deferred dir.

    The `TASK` set with both *shared* roots dropped, which is what `OWN`
    means: `{mount}/Channels/{token}` and `{mount}/Talk` hold material other
    people put there, and a verb sending bytes out of the task must not be
    able to name it directly. The deferred directory is not that. It is per
    *user* rather than per task — `task_env` sets `ISTOTA_DEFERRED_DIR` to
    `user_temp_dir`, so this user's concurrent tasks share one — but each of
    them is the same person's model acting on their behalf, which is the
    question `EGRESS` asks, and nobody else can write there at all. So
    excluding it bought no boundary and cost two things.

    **An earlier reading of `OWN` as the workspace alone was over-narrow, and
    both costs are the kind a stamp hides.** `NEXTCLOUD_MOUNT_PATH` is `""` on
    a deployment with no mount configured, so that reading made the root list
    *empty* there and every `EGRESS` argument refused unconditionally —
    correctly, since an empty allowlist means refuse everything, but for a
    reason nothing in the refusal named. `memory_search index file` worked on
    that shape before it was stamped. And it put this CLI's accepted set at
    odds with the deferred replay's: `scheduler_deferred._source_path_allowed`
    derives exactly this pair, so a source the CLI accepted could be deferred
    and then dropped at replay with nothing but a daemon warning.

    It gives up nothing. `EGRESS` is a guard against naming shared material
    directly — a speed bump on accident rather than a barrier against intent,
    since a task may already copy a Talk attachment into its workspace and
    mail that — and it is not weakened by admitting the most private root
    there is. `outbound_drafts._confined_attachment` is the one caller that
    genuinely needs the workspace alone, and it keeps its own check for a
    reason about durability rather than sharing: a held draft is released
    hours later, by which time the temp dir has been swept.

    `writable` is accepted and unused: `env_host_roots` drops the read-only
    roots on a writable call and neither of these two is one, so the answer is
    the same either way, and taking it keeps the two branches of `_roots_for`
    reading alike.
    """
    return env_host_roots(writable=writable, talk=False, channel=False)


def _roots_for(mode: str, *, writable: bool) -> list[Path]:
    """`TASK` for a `READ` or a `WRITE`, `OWN` for an `EGRESS`.

    **A `WRITE` takes the task's roots with the read-only ones dropped —
    exactly today's `env_host_roots(writable=True)` — and reading `OWN` into
    it is the mistake this docstring exists to stop.** The rule above the two
    sets is where the bytes end up, and a destination's content does not
    *leave* the task's context: it lands in storage the same task then reads
    back and `/chat/files` serves. `user_workspace_root` is the case that
    settles it, since it exists precisely because `browse screenshot` needs a
    destination that is both. Narrowing a `WRITE` to `OWN` would silently
    refuse `browse screenshot --output`, `devbox cp-out --dest`, `feeds
    export-opml --output` and `health export-csv --output` into the deferred
    dir or `{mount}/Channels/{token}`, all four of which work today — and it
    would arrive with no test naming it, because every test is written
    against the stamp. A consolidation that moves a boundary as a side effect
    is the failure this work exists to prevent, and it does not become
    acceptable for moving in the safer-looking direction.

    `EGRESS` is the one narrowing the spec does carry, and it is stated as
    such: the workspace and the deferred dir, so a verb whose content leaves
    the task can name neither `{mount}/Channels/{token}` nor `{mount}/Talk`.
    Talk is in the first set and not the second for the same rule read the
    other way — a task may legitimately read a Talk attachment into its own
    reasoning, which is a different question from whether those bytes may be
    mailed out. Why the deferred dir stays: see `_own_roots`.
    """
    if mode == EGRESS:
        return _own_roots(writable=writable)
    return env_host_roots(writable=writable)


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


def _resolve_value(
    value: object, mode: str, operation: str,
) -> tuple[object | None, str | None]:
    """One stamped value, whatever shape it arrived in.

    A list or tuple resolves element by element and comes back in the same
    container type, and the first refusal refuses the whole call: `email
    --attach` naming three files, one of them outside the workspace, is one
    refused send rather than a send with two attachments.

    An element is refused rather than skipped. Dropping it shortens the list
    silently, and a handler pairing a stamped list positionally against
    another argument then reads the wrong element with nothing having failed.
    """
    if isinstance(value, (list, tuple)):
        resolved_all: list[str] = []
        for element in value:
            resolved, error = _resolve_one(element, mode, operation)
            if error is not None:
                return None, error
            resolved_all.append(resolved)
        return type(value)(resolved_all), None
    return _resolve_one(value, mode, operation)


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

        resolved, error = _resolve_value(value, mode, operation)
        if error is not None:
            log.warning("host path refused: %s %s", skill, operation)
            return error
        setattr(args, action.dest, resolved)
    return None


__all__: Sequence[str] = (
    "EGRESS",
    "HostPathRefused",
    "MODES",
    "NOTE",
    "NOT_A_PATH",
    "READ",
    "REMOTE",
    "REPO",
    "RESOLVING",
    "STAMP",
    "WRITE",
    "click_commands",
    "click_host_path",
    "click_stamped",
    "host_path",
    "resolve_parsed",
    "stamp_conflicts",
    "stamped",
    "subparsers_without_dest",
)
