"""Declaring a skill CLI argument as a shared-credential *name*, and resolving it.

A skill CLI is spawned by `skill_proxy` **outside** the sandbox, as the daemon
user. So a verb that needs a credential can take the *name* from the model and
resolve the value itself: the model's argv carries a label, the value goes from
the daemon to wherever the CLI sends it, and nothing in the sandbox ever holds
it. That is a real boundary rather than a habit, and it is the reason this
module exists — `browse interact --fill-credential` is the first consumer and
the case the mechanism was designed around.

**A sibling of `_hostpath`, not an extension of it.** The two resolve different
things against different authorities: a host path is checked against a root
list this process derives from its own environment, and a credential name is
looked up in the user's `vault_entries` namespace, which only the daemon holds
and which this process can reach only by asking over the proxy socket. They
share a shape — a stamp on the argument, read back at the parse — and nothing
else, so a credential is not a seventh `_hostpath` mode. What they do share is
the parser walk: `actions_on_path` descends the verbs an invocation actually
took, which matters here for the same reason it matters there (a dest recurs
across sibling verbs), and a second copy of that walk is the duplication the
walk itself was written to avoid.

**Two forms, because the argument's value is not always just a name.**
`NAME` is the whole value; `PAIR` is `LABEL=NAME`, where the label is the
caller's own (a CSS selector, a header name) and only the right-hand side is
resolved. A form that neither expresses is a missing form rather than a special
case in a handler.

**A resolved value is boxed.** `SecretValue` renders `SecretValue(<name>)` from
`__repr__`, `__str__` and `__format__`, and only `reveal()` returns the
plaintext. The namespace it lands on is what handlers log, what exception text
interpolates and what pytest's assertion rewriting prints — the argument
`VaultRead.__repr__` already makes one module over. A handler that needs the
value says so, in one visible call.

**A refusal happens before dispatch.** `_cli.parse_and_resolve` turns it into
the facade's envelope with `reason="vault_credential_refused"` and exits, so no
handler ever runs on a namespace where some names resolved and others did not.
That is the rule that file already states for host paths, and the reason it is
not an argparse `type=` callable is the same one: `parser.error()` is usage text
on stderr and exit 2, which the model reads as a malformed call rather than as
a boundary.

**Every request spends from the task attempt's fetch budget**, which
`SkillProxy` owns (`[security] vault_fetch_limit_per_task`). Nothing here
caches or de-duplicates: three `--fill-credential` flags are three requests, by
§4b's own arithmetic, and a resolver that quietly collapsed two identical names
would make the cap mean something other than what it says. Resolution stops at
the first refusal for the same reason `_hostpath` does — the call is refused
either way, and the names after it would spend budget on a call that is already
over.

**And the spend is at the parse, so a handler that then fails does not get it
back.** A `browse interact` against a dead container or an expired session has
already charged its credentials by the time the request fails, and five such
attempts at two credentials each exhaust the default budget of ten with nothing
typed. That is the cost of the pre-dispatch refusal rather than an oversight:
resolving at dispatch would buy the refund and give up the guarantee that no
handler ever runs on a half-resolved namespace, which is the property §4a asks
for and the one the whole stamp exists to provide.

Nothing from the package beyond `istota.credential_shim`, which is a stdlib-only
leaf (it is copied verbatim into the task's own shim directory and runs with no
istota package on its path), and `._hostpath`, itself a leaf over
`istota.skill_host_paths` — so a skill subprocess pays nothing for this beyond
what `istota.skills.__init__` already costs.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from istota.credential_shim import ProxyError, fetch_credential

from ._hostpath import actions_on_path, stamped as _stamped_by

log = logging.getLogger(__name__)

#: The whole value is a credential name.
NAME = "name"
#: `LABEL=NAME`: the label is the caller's, the name is resolved.
PAIR = "pair"

FORMS = (NAME, PAIR)

#: Attribute set on the argparse action, holding the form. Named rather than
#: inlined so the coverage walk and this module cannot disagree on the spelling.
STAMP = "istota_credential_ref"

#: What the proxy records this request as. A claim rather than a fact — the
#: proxy sees a socket, not a process — so it decides a log level and nothing
#: else, and `skill` is the one that says the value is going somewhere other
#: than the model's own context.
MODE = "skill"


class SecretValue:
    """A resolved credential, which renders as its name and never as itself.

    ``__slots__`` so no `__dict__` carries the plaintext into a `vars()` dump,
    and `__format__` is defined rather than inherited: `object.__format__`
    raises on a non-empty spec, which would turn an f-string somebody wrote as
    ``f"{value:>20}"`` into a traceback whose message names the class rather
    than into a redaction. Formatting the *redacted* string is the answer that
    is right under every spec.

    Deliberately not comparable and not hashable beyond identity. A credential
    that can be tested for equality against a string is a credential an
    assertion can brute-force one character at a time, and nothing in the tree
    needs either.
    """

    __slots__ = ("name", "_value")

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self._value = value

    def reveal(self) -> str:
        """The plaintext. The one call that hands it over, so it is greppable."""
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue({self.name})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)

    def __reduce__(self):
        """Refuse to serialize. `__slots__` closes `vars()` and not this.

        The default reduction carries the slot state, so `pickle` — and with
        it `copy.deepcopy`, `multiprocessing` and anything that ships an object
        between processes — would move the plaintext where no rendering rule
        reaches. Nothing in the tree pickles a namespace today; refusing costs
        nothing and keeps the box's guarantee from depending on that staying
        true.
        """
        raise TypeError("a SecretValue may not be serialized")


class CredentialPair:
    """`LABEL=NAME` resolved: the caller's label, and the value behind the name.

    A small class rather than a tuple, because a tuple's `repr` is its
    elements' and a handler unpacking one positionally would be one line away
    from logging the pair. `SecretValue.__repr__` redacts either way; this makes
    the redaction survive the container too.
    """

    __slots__ = ("label", "value")

    def __init__(self, label: str, value: SecretValue) -> None:
        self.label = label
        self.value = value

    def __repr__(self) -> str:
        return f"CredentialPair({self.label}, {self.value!r})"


def credential_ref(
    parser: argparse.ArgumentParser, *names: str, form: str = NAME, **kwargs,
) -> argparse.Action:
    """`parser.add_argument(*names, **kwargs)`, with the credential stamp on it.

    Returns the action argparse registered, matching `_hostpath.host_path`, so
    a caller that wants to keep fiddling with it can.
    """
    if form not in FORMS:
        raise ValueError(f"unknown credential form {form!r}; one of {FORMS}")
    action = parser.add_argument(*names, **kwargs)
    setattr(action, STAMP, form)
    return action


def stamped(parser: argparse.ArgumentParser) -> list[tuple[str, str, str]]:
    """Every stamped argument in the tree, as `(dotted command, dest, form)`.

    `_hostpath.stamped` reading this module's attribute: one walk, keyed the
    way the host-path coverage check keys, so the two enumerations of the same
    tree can be compared key for key.
    """
    return _stamped_by(parser, attr=STAMP)


def _operation(dotted: str, action: argparse.Action) -> str:
    """How a refusal names what was refused: the verb and the flag.

    This contributes neither the credential name nor any part of a value. The
    assembled message can still carry a name, because the proxy's own refusal
    does (`No shared credential named 'x'`, bounded by `label_for_display`) and
    that text is carried through; what this keeps out is a second, unbounded
    spelling of it. The log line below carries only what this builds, since it
    goes to the daemon's log where a name off a socket would need bounding and
    naming nothing at all is cheaper than bounding it.
    """
    flag = action.option_strings[0] if action.option_strings else action.dest
    return " ".join(part for part in (dotted.replace(".", " "), flag) if part)


def _resolve_name(name: str, operation: str) -> tuple[SecretValue | None, str | None]:
    """One name to a `SecretValue`, or the refusal to report.

    The proxy's own message is carried through: it is already bounded and
    flattened (`label_for_display`), and it is the only place that knows
    whether the refusal was an absent name, the per-attempt cap, or a socket
    that would not answer. One arm of `ProxyError` is the client's own rather
    than the proxy's — `fetch_credential` raises `no value for <name>` when the
    reply carries no string value — and that one is not flattened. It is
    reachable only from a proxy answering something this protocol does not
    define, and the name in it is the caller's own argv string, so it reaches
    the model that chose it and no log line at all.
    """
    if not name:
        return None, f"Empty credential name: {operation} refused."
    try:
        return SecretValue(name, fetch_credential(name, MODE)), None
    except ProxyError as exc:
        return None, f"{operation} refused: {exc}"


def _resolve_one(
    value: object, form: str, operation: str,
) -> tuple[object | None, str | None]:
    """One stamped element under one form."""
    raw = "" if value is None else str(value).strip()
    if form == PAIR:
        # The **last** `=`, not the first. A credential name cannot contain one
        # — `secrets_vault.VAULT_NAME_RE` is `[a-z][a-z0-9_]{0,63}` — while a
        # label routinely does: `input[type=password]=acme_pw` is the ordinary
        # spelling of the field this exists for, and splitting at the first `=`
        # makes the label `input[type` and the name `password]=acme_pw`, which
        # is non-empty, so the malformed-value guard does not fire. The proxy
        # then charges the attempt's fetch budget for it and answers with a
        # refusal naming the vault rather than the selector. `--fill` splits the
        # other way on purpose: there the *right* side is a literal value, which
        # may contain `=`, and the left is the selector either way.
        label, sep, name = raw.rpartition("=")
        if not sep or not label.strip():
            return None, (
                f"Malformed {operation} value: expected SELECTOR=NAME."
            )
        resolved, error = _resolve_name(name.strip(), operation)
        if error is not None:
            return None, error
        return CredentialPair(label.strip(), resolved), None
    return _resolve_name(raw, operation)


def _resolve_value(
    value: object, form: str, operation: str,
) -> tuple[object | None, str | None]:
    """One stamped value, whatever shape it arrived in.

    A list or tuple resolves element by element and comes back in the same
    container type, and the first refusal refuses the whole call — `_hostpath`'s
    rule, for its reason (a partially filled login form is worse than a refused
    one) and for one of this module's own: every element past the refusal would
    spend from a budget the call is not going to use.
    """
    if isinstance(value, (list, tuple)):
        resolved_all: list[object] = []
        for element in value:
            resolved, error = _resolve_one(element, form, operation)
            if error is not None:
                return None, error
            resolved_all.append(resolved)
        return type(value)(resolved_all), None
    return _resolve_one(value, form, operation)


def resolve_parsed(
    parser: argparse.ArgumentParser, args: argparse.Namespace,
) -> str | None:
    """Resolve every stamped credential on `args`, in place. The refusal, or None.

    A `None` value is skipped — the argument was not passed, which is the
    ordinary case for every verb that has one. Returns the message rather than
    raising, matching `_hostpath.resolve_parsed`; `_cli.parse_and_resolve` is
    what turns it into the facade's envelope, and it exits, so no handler sees
    the namespace this leaves *partially* rewritten on a refusal.
    """
    for dotted, action in actions_on_path(parser, args):
        form = getattr(action, STAMP, None)
        if form is None:
            continue
        value = getattr(args, action.dest, None)
        if value is None:
            continue
        operation = _operation(dotted, action)

        resolved, error = _resolve_value(value, form, operation)
        if error is not None:
            # The skill and the flag, and nothing off the socket: the name is
            # the model's own string and the value is the thing this module
            # exists to keep out of a log line.
            log.warning("credential refused: %s %s", parser.prog, operation)
            return error
        setattr(args, action.dest, resolved)
    return None


__all__: Sequence[str] = (
    "FORMS",
    "MODE",
    "NAME",
    "PAIR",
    "STAMP",
    "CredentialPair",
    "SecretValue",
    "credential_ref",
    "resolve_parsed",
    "stamped",
)
