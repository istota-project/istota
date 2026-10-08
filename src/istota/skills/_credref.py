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
and which this process reaches through its private inherited fd. Callers
without that fd use the public proxy socket and are subject to the reveal
policy (`CredentialBrokerConfig.reveal_enforced`). They share a shape — a stamp on the argument, read back at the parse — and nothing
else, so a credential is not a seventh `_hostpath` mode. What they do share is
the parser walk: `actions_on_path` descends the verbs an invocation actually
took, which matters here for the same reason it matters there (a dest recurs
across sibling verbs), and a second copy of that walk is the duplication the
walk itself was written to avoid.

**Forms describe what the argument resolves.**
`NAME` is the whole value; `PAIR` is `LABEL=NAME`, where the label is the
caller's own (a CSS selector, a header name) and only the right-hand side is
resolved; `ENTRY` names a whole vault entry and resolves to a `SecretEntry`
holding every field of it (password, username, URL, custom fields) for one
fetch. `OTP_PAIR` resolves a selector and entry name to a boxed current code
and its expiry. `RECOVERY_PAIR` resolves a selector and a generated
credential to the hosts its recovery codes may be read on, and no value. A form that none of them expresses is a missing form rather than a
special case in a handler.

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

**Vault requests spend from the task attempt's fetch budget**, which
`SkillProxy` owns (`[security] vault_fetch_limit_per_task`). An `ENTRY` is one
request however many fields the entry has (ISSUE-583). Nothing here
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

Nothing from the package beyond `istota.sandbox.credential_shim`, which is a stdlib-only
leaf (it is copied verbatim into the task's own shim directory and runs with no
istota package on its path), and `._hostpath`, itself a leaf over
`istota.sandbox.host_paths` — so a skill subprocess pays nothing for this beyond
what `istota.skills.__init__` already costs.

`CARD` resolves a purchase id to boxed card fields. It uses only the private
channel and spends the purchase fill limit, never the vault fetch budget.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence

from istota.sandbox.credential_shim import (
    ProxyError, fetch_card, fetch_credential, fetch_entry, fetch_otp, fetch_recovery_target,
)

from ._hostpath import actions_on_path, stamped as _stamped_by

log = logging.getLogger(__name__)

#: The whole value is a credential name.
NAME = "name"
#: `LABEL=NAME`: the label is the caller's, the name is resolved.
PAIR = "pair"
#: `SELECTOR=NAME`: resolves a current OTP code and its expiry.
OTP_PAIR = "otp_pair"
#: The whole value is a vault entry name, resolved to every field of it.
ENTRY = "entry"

CARD = "card"
#: `SELECTOR=NAME`: where a generated credential's recovery codes may be read
#: from, for saving. Resolves hosts, never a value (ISSUE-688).
RECOVERY_PAIR = "recovery_pair"
RECOVERY_FILL = "recovery_fill"

FORMS = (NAME, PAIR, OTP_PAIR, ENTRY, CARD, RECOVERY_PAIR, RECOVERY_FILL)

#: Attribute set on the argparse action, holding the form. Named rather than
#: inlined so the coverage walk and this module cannot disagree on the spelling.
STAMP = "istota_credential_ref"

#: The compatibility socket's audit label. Private-fd requests have their
#: skill provenance supplied by the server, independent of this claim.
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

    __slots__ = ("name", "_value", "bound_hosts")

    def __init__(self, name: str, value: str, bound_hosts=()) -> None:
        self.name = name
        self._value = value
        self.bound_hosts = tuple(bound_hosts)

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


class OtpPair:
    """A selector and boxed OTP code, with the time the code expires."""

    __slots__ = ("label", "value", "expires_at")

    def __init__(self, label: str, value: SecretValue, expires_at: int) -> None:
        self.label = label
        self.value = value
        self.expires_at = expires_at

    def __repr__(self) -> str:
        return f"OtpPair({self.label}, {self.value!r}, expires_at={self.expires_at})"


class SecretEntry:
    """A resolved vault entry: each field boxed, the whole rendering as its name.

    ``fields`` maps ``password``, ``username``, ``url`` and each custom field
    to a `SecretValue`, holding only the fields the entry has; the three
    standard ones are also attributes, ``None`` where the entry has none.
    ``bound_hosts`` belongs to the entry, since every field shares its binding.
    """

    __slots__ = ("name", "fields", "bound_hosts")

    def __init__(self, name: str, fields: dict[str, SecretValue], bound_hosts=()) -> None:
        self.name = name
        self.fields = dict(fields)
        self.bound_hosts = tuple(bound_hosts)

    @property
    def password(self) -> SecretValue | None:
        return self.fields.get("password")

    @property
    def username(self) -> SecretValue | None:
        return self.fields.get("username")

    @property
    def url(self) -> SecretValue | None:
        return self.fields.get("url")

    def __repr__(self) -> str:
        return f"SecretEntry({self.name})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return format(repr(self), spec)

    def __reduce__(self):
        raise TypeError("a SecretEntry may not be serialized")


class RecoveryTarget:
    """A selector and the generated credential whose codes it shows. No value."""

    __slots__ = ("label", "name", "bound_hosts")

    def __init__(self, label: str, name: str, bound_hosts=()) -> None:
        self.label = label
        self.name = name
        self.bound_hosts = tuple(bound_hosts)

    def __repr__(self) -> str:
        return f"RecoveryTarget({self.label}, {self.name})"


class CardRefusal(ProxyError):
    """A fixed wallet refusal code for the CLI envelope."""


class CardSecret:
    __slots__ = ("purchase_id", "fields", "bound_hosts")

    def __init__(self, purchase_id: int, fields: dict[str, SecretValue], bound_hosts=()):
        self.purchase_id = purchase_id
        self.fields = dict(fields)
        self.bound_hosts = tuple(bound_hosts)

    def __repr__(self):
        return f"CardSecret(purchase_id={self.purchase_id})"

    __str__ = __repr__

    def __format__(self, spec):
        return format(repr(self), spec)

    def __reduce__(self):
        raise TypeError("a CardSecret may not be serialized")


def resolve_card(raw: str) -> CardSecret:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19 or not 0 < int(raw) <= 2**63 - 1:
        raise CardRefusal("purchase_not_found")
    purchase_id = int(raw)
    try:
        fields, hosts = fetch_card(purchase_id, credential_fd=os.environ.get("ISTOTA_CRED_FD"))
    except ProxyError as exc:
        raise CardRefusal(str(exc)) from None
    boxed = {key: SecretValue(f"purchase:{purchase_id}.{key}", value) for key, value in fields.items()}
    return CardSecret(purchase_id, boxed, hosts)


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
        value, hosts = fetch_credential(
            name, MODE, binding=True, credential_fd=os.environ.get("ISTOTA_CRED_FD"),
        )
        return SecretValue(name, value, hosts), None
    except ProxyError as exc:
        return None, f"{operation} refused: {exc}"


def resolve_entry(name: str, operation: str) -> tuple[SecretEntry | None, str | None]:
    """One entry name to a `SecretEntry`, or the refusal to report. One fetch.

    Public for the one caller whose entry name is not an argv value: the
    `wordpress` CLI maps `--site NAME` to the entry ``wordpress_NAME`` and
    resolves it before the verb's handler runs, so the stamp's guarantee (no
    handler works on an unresolved credential) holds there too. The name is
    model-chosen, so this is not a narrower reach than a stamp; what bounds it
    is the entry's own host binding.
    """
    if not name:
        return None, f"Empty credential name: {operation} refused."
    try:
        fields, hosts = fetch_entry(
            name, MODE, credential_fd=os.environ.get("ISTOTA_CRED_FD"),
        )
    except ProxyError as exc:
        return None, f"{operation} refused: {exc}"
    boxed = {key: SecretValue(f"{name}.{key}", value) for key, value in fields.items()}
    return SecretEntry(name, boxed, hosts), None


def _resolve_one(
    value: object, form: str, operation: str,
) -> tuple[object | None, str | None]:
    """One stamped element under one form."""
    raw = "" if value is None else str(value).strip()
    if form == CARD:
        return resolve_card(raw), None
    if form == ENTRY:
        return resolve_entry(raw, operation)
    if form in (PAIR, OTP_PAIR, RECOVERY_PAIR, RECOVERY_FILL):
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
        name = name.strip()
        if form in (RECOVERY_PAIR, RECOVERY_FILL):
            if not name:
                return None, f"Empty credential name: {operation} refused."
            try:
                hosts = fetch_recovery_target(name, credential_fd=os.environ.get("ISTOTA_CRED_FD"))
            except ProxyError as exc:
                return None, f"{operation} refused: {exc}"
            return RecoveryTarget(label.strip(), name, hosts), None
        if form == OTP_PAIR:
            if not name:
                return None, f"Empty credential name: {operation} refused."
            try:
                code, expires_at, hosts = fetch_otp(
                    name, MODE, credential_fd=os.environ.get("ISTOTA_CRED_FD"),
                )
            except ProxyError as exc:
                return None, f"{operation} refused: {exc}"
            return OtpPair(label.strip(), SecretValue(name, code, hosts), expires_at), None
        resolved, error = _resolve_name(name, operation)
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
    "OTP_PAIR",
    "OtpPair",
    "RECOVERY_PAIR",
    "RecoveryTarget",
    "STAMP",
    "ENTRY",
    "CARD",
    "CardSecret",
    "CardRefusal",
    "resolve_card",
    "CredentialPair",
    "SecretEntry",
    "SecretValue",
    "credential_ref",
    "resolve_entry",
    "resolve_parsed",
    "stamped",
)
