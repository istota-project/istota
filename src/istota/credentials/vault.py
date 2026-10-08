"""A user's KeePass credential vault: the bytes, and the names they hold.

A user who keeps their credentials in a password manager otherwise maintains two
copies of every key, and the copy istota reads is the one they cannot see,
search or back up. This module is the answer's provisioning half: a KDBX file in
the user's own workspace that istota reads and may add a credential to, and
the pass that copies what it holds into the encrypted ``secrets`` table.

**The file is the consent boundary; the ``istota`` group is an optional
narrowing inside it.** A top-level group of that name — matched
case-insensitively, whitespace stripped — scopes the read to itself and
everything outside is parsed and discarded. With no such group the **whole
file** is shared, the database root standing in for ``istota/``. So the file a
user puts in their vault folder is read in full unless they narrow it, and
pointing at the everyday KDBX they already keep is the one thing not to do
without creating the group first. An unscoped read is reported and never
silent: on the settings card, in ``vault-status``, and once per user as a
notification. Below the starting point the shape is theirs — entries directly
under it or in subgroups, each contributing one name per field, with the name
derived from the path rather than typed (:func:`slug_name`).

**It is provisioning input, not a storage backend.** ``resolve_secret``'s order
is unchanged, the table stays the live store, and a vault that is missing,
half-synced or locked leaves every credential working. What the pass adds is the
one direction the table cannot express on its own: the file is authoritative for
the whole namespace, deletions included — which is why the applying half is
where the destructive rules live, and why :class:`VaultRead` carries a ``held``
set rather than letting the apply infer a deletion from an absence.

**The one exception is the ``generated/`` group**, whose entries are mirror
copies of credentials Istota generated and owns in the table
(``credentials.generated``, ISSUE-686). They are read into
``VaultRead.generated``, compared, and never applied; :func:`mirror_entry` is
the only writer of the file.

**Two functions rather than one**, and that split is the design rather than
tidiness. The sync cycle hashes the file bytes to decide whether to parse at
all, so a single ``read_vault(path, passphrase)`` would spend an Argon2id unlock
— tuned to about a second — every cycle just to learn that nothing had changed.
``read_vault_bytes`` is the half that runs every time and needs no library at
all; ``parse_vault`` is the half that runs when the digest moved.

**What this module does not do is decide which file to open.** ``path`` arrives
already resolved, and the containment rules — a relative path under
``{mount}/Users/{user_id}``, an absolute one refused if it lands anywhere under
the workspace — belong to ``storage.resolve_user_vault_path``. What is covered
here is the last component alone, by way of ``read_overlay_bytes``: its
``O_NOFOLLOW``, and the ``dir_fd`` that resolver hands over for the relative
form so the components *above* the leaf are not walked by name a second time.

**And it never logs a value, at any level.** Counts and names are loggable and
are what the warnings below carry — bounded and flattened by ``_label``, since
the file is writable by a task in that user's own sandbox and every group and
entry title in it is therefore an attacker-reachable string. The rule is easy to defeat
by accident, which is why ``VaultRead`` carries a ``__repr__`` of its own: that
dataclass holds every plaintext the vault carries between parse and apply, and
pytest's assertion rewriting prints the repr of whatever a failing comparison
touched, so nobody has to write a logging call to leak it.
"""

from __future__ import annotations

import dataclasses
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import stat
import string
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from istota.credentials import store as secrets_store
from istota.lib import file_lock, totp

logger = logging.getLogger(__name__)

VAULT_WRITE_GROUP = "generated"
#: The KeePass tag that keeps an entry out of the sync's first-sight grant.
VAULT_NO_GRANT_TAG = "istota:nogrant"
_VAULT_LOCK_WAIT_SECONDS = 2.0

#: The size above which the file is refused unread. A vault is a few kilobytes
#: of credentials; 8 MiB is slack for attachments the user kept in the same
#: database and a bound on what a planted file can make the daemon read.
VAULT_READ_CAP_BYTES = 8 * 1024 * 1024

#: The one top-level group that *narrows* the read, matched with surrounding
#: whitespace stripped and case folded — so ``Istota``, ``ISTOTA`` and
#: ``"istota "`` all scope.
#:
#: **The file is the consent boundary; this group is an optional narrowing
#: inside it** (§1). A file with no such group is read in full, with the root
#: standing in for ``istota/``. That inverts the earlier rule and dissolves its
#: worst failure: under the old reading a missing or mistyped group was a
#: successful parse of *nothing*, which the namespace sweep turned into the
#: deletion of every stored credential, with the file's contents still sitting
#: there unread. Under this one such a file is read, so the credentials are
#: rewritten rather than destroyed.
#:
#: **What it does not do is make a renamed group harmless, and the honest form
#: of the claim is narrower than "an empty read means an empty file".** Two
#: things still produce an empty read — an empty file, and an ``istota`` group
#: with nothing in it, which is the deliberate revocation §3 names. And a
#: *renamed* group produces a read that is not empty but is **disjoint**: the
#: group now contributes a path segment it did not before, so every derived
#: name changes, the old ones are swept and the new ones are written in the
#: same pass. That is recoverable — rename it back and one cycle restores every
#: name — where the old rule's wipe was not, and it is reported rather than
#: silent, because the read is unscoped and says so on three surfaces. It is
#: still a surprise worth knowing about before editing this rule.
#:
#: **The case-insensitive match is load-bearing rather than a courtesy.**
#: ``Istota`` is what a person types, and under an exact lowercase match it
#: falls through to the unscoped read — so the most likely spelling of the
#: narrowing would silently trigger the widest behaviour. Under the old rule
#: that was a wipe; under this one it is a disclosure.
VAULT_ROOT_GROUP = "istota"

#: How deep below the read's root the walk goes — the ``istota`` group where
#: one narrows the read, and the file root where none does — counted in
#: subgroup levels, so ``8`` admits ``<root>/a/b/c/d/e/f/g/h/<entry>``. A bound rather than a limit
#: anyone should meet: the names below flatten the path, so the depth that
#: produces a usable name is bounded by ``VAULT_NAME_MAX_CHARS`` long before
#: this. What it is really for is a file a task in that user's own sandbox can
#: overwrite — a self-referential group would otherwise be walked forever.
VAULT_MAX_DEPTH = 8

#: How many entries one walk visits, and how many *fields* it considers.
#: Both stop the walk and warn rather than failing the sync: half a namespace
#: applied is a user with some credentials working, and a refusal is a user
#: with none.
#:
#: The second is spelled as a bound on names produced and enforced as a bound
#: on fields examined, which is the same guarantee and a tighter one. A field
#: that produces *no* name still costs a skip record and a daemon WARNING, and
#: ``Entry.custom_properties`` is unbounded in cardinality — so counting only
#: what a field produced left one entry whose custom fields all slug to nothing
#: emitting one record and one log line each, past every cap, from a file a
#: task in that user's own sandbox can write.
VAULT_MAX_ENTRIES = 512
VAULT_MAX_NAMES = 1024

#: The cap on one value, in UTF-8 bytes. 8 KiB is slack over an RSA private
#: key, which is a legitimate thing to keep in a password manager.
VAULT_MAX_VALUE_BYTES = 8192

#: The longest usable name. The bound is in the pattern below too; it is named
#: separately because the warning that reports a refusal says what the limit
#: was, and a second spelling of ``64`` in a log string is the drift that makes
#: the message wrong rather than merely different.
VAULT_NAME_MAX_CHARS = 64

#: What a usable name looks like. The leading-letter and length rules are there
#: because the name is what a person types in a shell and what a script may
#: export as a variable, and because it lands in the ``secrets`` table's ``key``
#: column and in log lines.
VAULT_NAME_RE = re.compile(rf"\A[a-z][a-z0-9_]{{0,{VAULT_NAME_MAX_CHARS - 1}}}\Z")

#: Every run of characters a segment may not keep. Each becomes one ``_``.
_SLUG_DROP_RE = re.compile(r"[^a-z0-9]+")

#: Which of ``_Walk.stopped``'s values mean the read is a prefix of the file.
#: A set rather than ``bool(walk.stopped)``, so a cap added later has to answer
#: this question on purpose.
_TRUNCATING_CAPS = frozenset({"entry", "name"})

#: The suffixes the three standard fields contribute, as a *segment* handed to
#: :func:`slug_name` rather than as a string appended to its answer — so the
#: composed name goes through the same validation as every other, and a custom
#: string field named ``URL`` produces the same name the URL field would (and
#: therefore collides with it, which is the rule rather than an accident).
_USERNAME_SEGMENT = "username"
_URL_SEGMENT = "url"

#: How much of a group or key name a log line may carry (see ``_label``).
_LABEL_MAX_CHARS = 64

#: The same bound for a *path*. Wider because 64 characters truncates an
#: ordinary resolved vault path — and the one WARNING that bound is applied in
#: exists to tell an operator which file failed. Matches
#: ``storage._VAULT_PATH_LOG_MAX_CHARS``, which bounds the same value one module
#: over on its way out of ``config.toml``.
_PATH_LABEL_MAX_CHARS = 200

#: The service the vault's own passphrase is stored under.
VAULT_PASSPHRASE_SERVICE = "vault"

#: The service every shared credential is stored under, one row per derived
#: name (§3).
#:
#: **A separate service from the passphrase's, and the separation is structural
#: rather than a rule somebody has to remember.** The namespace sweep below
#: cannot reach ``vault/passphrase`` because it is not in the namespace, and
#: the task-side read is one ``get_service_secrets(..., "vault_entries")`` that
#: cannot return it. An entry the user titles ``passphrase`` becomes
#: ``vault_entries/passphrase`` and is harmless.
#:
#: It is deliberately **not** added to ``secret_schema``: the vault owns it, so
#: nothing may write it through the settings page or ``istota secret ensure``,
#: and staying out of the registry is what refuses those for free. ``istota
#: secret list`` still shows the rows, because it reads the table.
VAULT_ENTRY_SERVICE = "vault_entries"

#: The fixed vocabulary the read and the apply report skips with. The name
#: rides in its own slot of the pair, so a reason never interpolates one: these
#: reach ``vault-status`` output, where a caller wants to group by reason
#: rather than parse a sentence.
SKIP_UNUSABLE_NAME = "the entry name cannot be used"
SKIP_DUPLICATE_NAME = "two entries produce the same name"
SKIP_EMPTY_VALUE = "the field is empty"
SKIP_UNUSABLE_OTP = "the OTP source cannot be used"
SKIP_OVERSIZE_VALUE = "the value is larger than the limit"
SKIP_UNREADABLE_ROW = "stored value will not decrypt, so it is not deleted"
#: A name the file produced that a credential added in Istota already holds.
#: The local credential wins: the file never overwrites a value the user typed
#: into Istota, nor inherits its grant.
SKIP_NAME_TAKEN = "name is already used by a credential added in Istota"

#: The whole of it. ``SKIP_RESERVED_SERVICE``, ``SKIP_INELIGIBLE_SERVICE``,
#: ``SKIP_UNKNOWN_KEY`` and ``SKIP_DELETE_HELD`` left with the service mapping
#: and the eligibility machinery that produced them, and a drift guard in
#: ``tests/test_secrets_vault.py`` refuses their return — a reason with no
#: producer is worse than absent, because it reads as a condition the apply can
#: still reach.
SKIP_REASONS = frozenset(
    {
        SKIP_UNUSABLE_NAME,
        SKIP_DUPLICATE_NAME,
        SKIP_EMPTY_VALUE,
        SKIP_OVERSIZE_VALUE,
        SKIP_UNREADABLE_ROW,
        SKIP_NAME_TAKEN,
        SKIP_UNUSABLE_OTP,
    }
)


class VaultError(Exception):
    """Base for every way a vault read can fail.

    The classes below are what §7's retry rule and §8's notification are keyed
    on, so they are a closed set with distinct remedies rather than a detail of
    the message: one of them is fixed by editing the file and the rest are not.
    """


class VaultMissing(VaultError):
    """No file at the path. The path is wrong, or nothing has been put there."""


class VaultUnreadable(VaultError):
    """The read was refused: a symlink, a FIFO, an oversize or unreadable file.

    ``str(exc)`` is ``read_overlay_bytes``' own refusal reason verbatim, so a
    reporting surface can name which refusal it was without this module keeping
    a second vocabulary for the same four answers.
    """


class VaultCorrupt(VaultError):
    """Bytes that are not a readable KeePass database.

    Also what a zero-byte file and a write caught mid-sync come back as, which
    is why its retry rule is the one that waits for the bytes to change.
    """


class VaultLocked(VaultError):
    """The stored credentials do not open the file.

    Distinct from ``VaultCorrupt`` because the remedy touches no byte of the
    vault — so a cycle that cached this digest would make that remedy inert.

    **It does not mean the passphrase is wrong, and saying so was a real cost.**
    ``pykeepass`` raises one ``CredentialsError`` for every way a credential set
    can fail to open a database, and measured against pykeepass 4.2 a correct
    password on a database that also requires a **key file** is byte-for-byte
    the same exception as a wrong password on one that does not. A hardware
    challenge-response secret is a third. istota passes ``password`` alone, so
    a key-file-protected database can never open here — and the old message
    ("the stored passphrase does not match the file") asserted the one cause it
    could not distinguish, sending a user to re-provision a password that was
    correct all along. Found in production by somebody doing exactly that.
    """


class VaultLibraryMissing(VaultError):
    """The ``vault`` extra is not installed on this host. An operator remedy."""


class VaultChanged(VaultError):
    """The file changed after the caller made its collision decision."""


class VaultWriteRefused(VaultError):
    """The requested create cannot preserve the vault's existing namespace."""


@dataclass(frozen=True)
class PasswordPolicy:
    length: int = 24
    require_lower: bool = True
    require_upper: bool = True
    require_digits: bool = True
    require_symbols: bool = True
    allow_lower: bool = True
    allow_upper: bool = True
    allow_digits: bool = True
    allow_symbols: bool = True


def generate_password(policy: PasswordPolicy = PasswordPolicy()) -> str:
    """Generate a password with every required class, or refuse the policy."""
    classes = (
        (string.ascii_lowercase, policy.require_lower, policy.allow_lower),
        (string.ascii_uppercase, policy.require_upper, policy.allow_upper),
        (string.digits, policy.require_digits, policy.allow_digits),
        ("!@#$%^&*-_=+?", policy.require_symbols, policy.allow_symbols),
    )
    if policy.length < 1 or policy.length > VAULT_MAX_VALUE_BYTES:
        raise VaultWriteRefused("password length is outside the allowed range")
    required = []
    alphabet = ""
    for characters, need, allowed in classes:
        if need and not allowed:
            raise VaultWriteRefused("password policy requires a forbidden class")
        if allowed:
            alphabet += characters
        if need:
            required.append(secrets.choice(characters))
    if not alphabet or len(required) > policy.length:
        raise VaultWriteRefused("password policy cannot fit the requested length")
    result = required + [secrets.choice(alphabet) for _ in range(policy.length - len(required))]
    secrets.SystemRandom().shuffle(result)
    return "".join(result)


@dataclass(frozen=True)
class VaultRead:
    """One parsed vault: the names it holds, and what it will not say about.

    ``services`` is the namespace itself — a usable name to its value, flat,
    for everything the walk found under ``istota/``. The field keeps its name
    from the shape it replaces, which was service to key to value; it holds no
    services any more and the spec's interface line spells it this way, so it
    is left alone rather than renamed under a change whose subject is the read.

    ``held`` is every name the file *produced* whose value cannot be written:
    empty after stripping, or over ``VAULT_MAX_VALUE_BYTES``. It is the one
    piece of state the applying half cannot derive from ``services``, and it
    exists for §3's rule that an empty value is a skip rather than a deletion.
    The namespace sweep deletes every stored name the read does not hold, so
    without this field a blanked password field — the fat-finger case, and the
    one the old ``entry_titles`` existed for — would delete the credential it
    was meant to change. Oversize takes the same hold for the same reason:
    refusing the new value *and* destroying the old one is a combination no
    rule here intends. A **collided** name is deliberately not held: §2 says a
    name produced twice is absent, and therefore deleted, because istota cannot
    say which of the two values it is.

    Required rather than defaulted, like the field it replaces: a hand-built
    ``VaultRead`` that omitted it would get the destructive reading silently.

    ``truncated`` names the cap the walk stopped at, and is empty when it did
    not: this read is then a **prefix** of the file rather than the whole of
    it. It is the second thing the applying half cannot derive, and the second
    one a default would get wrong in the destructive direction: the namespace sweep deletes every stored name
    the read does not hold, and on a truncated read the names past the cap are
    absent for a reason that has nothing to do with the user removing them. The
    same never-destroy-what-you-cannot-read rule the unreadable-row hold
    follows says an incomplete read may not delete at all.

    Only the entry and name caps set it. The depth cap does not, because a
    group below :data:`VAULT_MAX_DEPTH` is excluded by a rule rather than
    interrupted by a budget — nothing under it has ever been readable, so there
    is no row it could have written and none it can strand.

    **A truncated read also weakens the collision rule, and that is accepted
    rather than unnoticed.** Where a cap falls between two producers of one
    name, only the first reached ``candidates``, so the name resolves to a
    single value and is applied — which is exactly the XML-order dependence the
    rule exists to refuse. Withholding writes as well as deletions would close
    it and is refused: the whole point of a cap that applies what it read is
    that a 513-entry vault gives the user 512 working credentials rather than
    none. Deletions are the half that must be withheld, because they are the
    half that cannot be undone by fixing the cause.

    Required rather than defaulted, like ``held``: a hand-built
    :class:`VaultRead` that omitted it would claim a complete read. It carries
    the cap's name rather than a flag because the applying half reports why it
    withheld a sweep, and "the vault holds more than istota will read" is a
    different sentence from "the vault produces more names than it will read".

    ``scoped`` is whether a top-level :data:`VAULT_ROOT_GROUP` narrowed the
    read. False means the whole file was read with the root standing in for
    ``istota/`` (§1), which is a **wider** read than the user may have meant —
    it is what "I pointed at my real password database" looks like from here.
    Nothing refuses on it: the file is the consent boundary and istota does not
    second-guess a file the user deliberately placed in their own vault folder.
    What it drives is reporting — the settings card, ``vault-status``, and a
    one-shot notification on the first unscoped sync — because the two failure
    modes are not equally reversible. A wipe is undone by fixing the file and
    waiting one sync; a whole everyday database copied into the ``secrets``
    table is fetchable by name by any of that user's tasks and is undone only
    by rotating all of it.

    Required rather than defaulted for the same reason as the two fields above,
    and the default that would be wrong is the *reassuring* one: a hand-built
    :class:`VaultRead` defaulting to ``True`` would claim a narrowing nobody
    performed, and the notice this field exists to raise would never fire.

    ``skipped`` is ``(name, reason)`` for what the read refused, from the fixed
    vocabulary above, carried so the applying half can report it beside its own
    skips. ``name`` is the produced name where there was one and the bounded
    original path where the refusal is that there is not.
    """

    digest: str
    services: dict[str, str]
    held: frozenset[str]
    truncated: str
    scoped: bool
    skipped: tuple[tuple[str, str], ...] = ()
    generated_count: int = 0
    bindings: dict[str, dict] = field(default_factory=dict)
    no_auto_grant: frozenset[str] = frozenset()
    #: Entries directly under ``generated/``: owner name -> ``password``,
    #: ``username``, ``url``, ``otp``. Not names in the namespace: since
    #: ISSUE-686 they are the mirror of credentials the table owns, compared by
    #: ``credentials.generated.reconcile`` and never applied over it.
    generated: dict[str, dict[str, str]] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Everything but the values.

        ``services`` holds every plaintext between parse and apply, and the
        no-value-logging rule above is defeated by one
        ``logger.debug("read=%r", read)``. The precedent is ``testbed``'s
        ``ServiceCall.__repr__``, which redacts for exactly this reason: a
        dataclass's generated repr is what pytest's assertion rewriting prints
        for whatever a failing comparison touched, and a list's repr calls repr
        on each element, so a ``__str__``-only override would be bypassed by
        every real failure.

        Names are kept, because they are the thing a reader needs and are
        loggable by the same rule the warnings below follow.

        **It closes the route through this object and not the field itself.**
        ``services`` is public, so ``assert read.services == {...}`` compares two
        plain dicts and prints both, and a caller is free to log the attribute
        directly. What this covers is the case nobody writes on purpose: the
        object reaching a repr by way of a container, a failing comparison or a
        debug line about the read as a whole.
        """
        return (
            f"VaultRead(digest={self.digest!r}, "
            f"names={sorted(self.services)!r}, "
            f"held={sorted(self.held)!r}, truncated={self.truncated!r}, "
            f"scoped={self.scoped!r}, skipped={self.skipped!r})"
        )


def read_vault_bytes(path: Path, *, dir_fd: int | None = None) -> tuple[bytes, str]:
    """The file's bytes and their SHA-256, or a mapped ``VaultError``.

    Read through ``skills._loader.read_overlay_bytes`` rather than a fresh
    ``open()``, because that primitive already does the three things this read
    needs and for the same reasons it needed them. The relative form of
    ``vault_path`` lives under ``{mount}/Users/{user_id}``, which is bound
    **read-write** into that user's own sandbox, so the filename is
    model-writable:

    - ``O_NOFOLLOW``, because a symlink planted at the name otherwise hands the
      daemon another file — and this one is then decrypted with a key the daemon
      holds and written into the secrets table;
    - ``S_ISREG`` behind ``O_NONBLOCK``, because a FIFO at that name blocks
      ``open(2)`` until somebody writes to it, and the sync runs on a background
      gate with no timeout behind it;
    - the size checked on the fd *before* the read, since reading the file and
      refusing afterwards bounds nothing.

    **``dir_fd`` changes what ``path`` means, and it is what covers the three
    components above the leaf.** ``O_NOFOLLOW`` is the last component only, and
    for the relative form of ``vault_path`` every component above it lives in
    the tree bound read-write into that user's own sandbox — so ``mv config
    config.real && ln -s /anywhere config`` between the containment check and
    the ``open(2)`` hands the daemon another directory and nothing here sees it.
    Given a descriptor only ``path.name`` is opened, resolved by the kernel
    relative to that directory, so no component above the leaf is consulted a
    second time. ``storage.resolve_user_vault_path`` is what produces the pair
    and it produces them together: **pass the descriptor it gave you beside the
    path it gave you**, never one without the other, and never an absolute path
    from somewhere else — the two have to name the same file or the read is
    about a different one. ``None`` is the absolute form, which resolves outside
    the workspace by construction and so has no model-writable ancestor to hold.

    **Absence and emptiness are the same bytes and are told apart by the size
    element.** A missing file is ``(b"", None, None)`` and is ``VaultMissing``; a
    zero-byte regular file is ``(b"", None, 0)`` — what an rclone write caught
    mid-flight looks like — and is ``VaultCorrupt``, a file that exists and is
    not a KDBX. They differ in their retry rule, so reading the bytes alone
    would collapse a file-shaped failure into a path-shaped one.

    The import is function-scoped like ``storage.read_regular_file``'s: reaching
    ``skills._loader`` executes ``istota.skills.__init__``, which star-imports
    every skill.

    **Every failure leaves here as a ``VaultError``, which is a contract rather
    than an observation**: §7 keys its retry rule and §8 its notification on the
    class, so an unmapped exception out of a background gate is a different
    failure class from a mapped one. ``read_overlay_bytes`` catches
    ``FileNotFoundError`` and ``OSError``, which leaves two escapes — a NUL in
    the path makes ``os.open`` raise ``ValueError``, and ``vault_path`` is a TOML
    string, where ``\\u0000`` is expressible (``_loader`` names that exact escape
    in ``open_overlay_dir``, which is a different function and screens a
    different argument); and a platform with no ``dir_fd`` support raises
    ``NotImplementedError``. Both are the caller naming a path this read cannot
    make, so both are a refusal.
    """
    from istota.skills._loader import OVERLAY_UNREADABLE, read_overlay_bytes

    try:
        data, refusal, size = read_overlay_bytes(
            path, max_bytes=VAULT_READ_CAP_BYTES, dir_fd=dir_fd
        )
    except (ValueError, NotImplementedError) as exc:
        raise VaultUnreadable(OVERLAY_UNREADABLE) from exc
    if refusal is not None:
        raise VaultUnreadable(refusal)
    if size is None:
        raise VaultMissing("no file at the configured vault path")
    if not data:
        raise VaultCorrupt("the vault file is empty")
    return data, _digest(data)


def _vault_lock_path(location, lock_root: Path) -> Path:
    """One host-private lock name for all processes resolving this vault."""
    if location.dir_fd is None:
        identity = str(location.path.resolve(strict=False))
    else:
        parent = os.fstat(location.dir_fd)
        identity = f"{parent.st_dev}:{parent.st_ino}:{location.path.name}"
    name = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return lock_root / f".istota-vault-{name}.lock"


@contextlib.contextmanager
def _vault_file_lock(location, *, lock_root: Path, blocking: bool):
    """Serialize writes with syncs through a daemon-owned lock directory.

    A non-blocking caller (a create) is refused at once while another vault
    operation holds the lock; a blocking one (a sync) waits a bounded time.
    """
    name = _vault_lock_path(location, lock_root)

    # A private subclass, so an ETIMEDOUT from the open (itself a TimeoutError)
    # still reads as an unreadable lock rather than a busy one.
    class _Busy(TimeoutError):
        pass

    def refused(_anchor: str) -> BaseException:
        if blocking:
            return _Busy("vault lock busy")
        return VaultWriteRefused("another vault operation holds the lock")

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock.exclusive_lock(
                name,
                timeout_seconds=_VAULT_LOCK_WAIT_SECONDS if blocking else 0.0,
                on_timeout=refused,
                nofollow=True,
            ))
        except (_Busy, VaultWriteRefused):
            raise
        except OSError as exc:
            raise VaultUnreadable("the vault lock cannot be acquired") from exc
        yield


def _vault_leaf(location):
    return location.path.name if location.dir_fd is not None else location.path


def _preserve_vault_metadata(fd: int, original) -> None:
    """Keep the original access and refuse a write that would change its owner."""
    saved = os.fstat(fd)
    if (saved.st_uid, saved.st_gid) != (original.st_uid, original.st_gid):
        try:
            os.fchown(fd, original.st_uid, original.st_gid)
        except OSError:
            raise VaultWriteRefused("the vault file owner cannot be preserved") from None
    os.fchmod(fd, stat.S_IMODE(original.st_mode) or 0o600)


def generated_entry_names(slug: str) -> tuple[str | None, str | None, str | None]:
    """The password, username and URL names a generated credential for ``slug`` has.

    The same names the sync derives for a ``generated/`` entry titled ``slug``,
    so the table's rows and the mirror copy agree. ``None`` in a slot means the
    slug is too long for that name, and ``new`` refuses it.
    """
    return (
        slug_name((VAULT_WRITE_GROUP, slug)),
        slug_name((VAULT_WRITE_GROUP, slug, _USERNAME_SEGMENT)),
        slug_name((VAULT_WRITE_GROUP, slug, _URL_SEGMENT)),
    )


def _generated_group(kp, *, create: bool):
    """The ``generated/`` group under the read's root, or a refusal.

    Under ``istota/`` when the file has that top-level group, under the root
    otherwise. Never creates a top-level ``istota`` group: that would narrow
    the next read and drop the user's other credentials from the namespace.
    """
    found, live = _root_groups(kp, _recyclebin_uuid(kp))
    if len(found) > 1 or (found and not live):
        raise VaultWriteRefused("the vault has an ambiguous top-level istota group")
    root = live[0] if live else kp.root_group
    groups = [
        group for group in root.subgroups
        if str(group.name or "").strip().casefold() == VAULT_WRITE_GROUP
    ]
    if len(groups) > 1 or (groups and groups[0].uuid == _recyclebin_uuid(kp)):
        raise VaultWriteRefused("the vault has ambiguous generated groups")
    if groups:
        return groups[0]
    return kp.add_group(root, VAULT_WRITE_GROUP) if create else None


def _generated_entries(group, name: str) -> list:
    if group is None:
        return []
    return [entry for entry in group.entries
            if slug_name((group.name or "", entry.title or "")) == name]


_LEGACY_OTP_FIELDS = ("totp seed", "totp settings")


def mirror_entry(
    location,
    passphrase: str,
    *,
    name: str,
    values: dict[str, str],
    expected_digest: str,
    lock_root: Path,
    db_path: Path | None = None,
    user_id: str | None = None,
) -> str:
    """Write the table's copy of a generated credential into ``generated/``.

    One way (ISSUE-686): the entry is created, or its password, username, URL
    and OTP are set to ``values``, whatever the file held. Only an entry whose
    derived name is ``name`` is touched. Returns the new digest.
    """
    prefix = VAULT_WRITE_GROUP + "_"
    slug = name[len(prefix):] if name.startswith(prefix) else ""
    if not slug or slug_name((VAULT_WRITE_GROUP, slug)) != name:
        raise VaultWriteRefused("not a generated credential name")
    password = values.get("password") or ""
    if not password:
        raise VaultWriteRefused("the credential has no password to mirror")

    def prepare(kp, read):
        group = _generated_group(kp, create=True)
        entries = _generated_entries(group, name)
        if len(entries) > 1:
            raise VaultWriteRefused("the vault has more than one copy of this credential")
        if entries:
            entry = entries[0]
            # The copy being replaced may hold an edit made in the password
            # manager; history keeps it recoverable there.
            entry.save_history()
            entry.password = password
            entry.username = values.get("username") or ""
            entry.url = values.get("url") or ""
        else:
            if len(kp.entries) >= VAULT_MAX_ENTRIES:
                raise VaultWriteRefused("the vault is at its entry cap")
            entry = kp.add_entry(group, slug, values.get("username") or "", password,
                                 url=values.get("url") or "")
        for field_name in list(entry.custom_properties):
            folded = str(field_name).casefold()
            if folded in _LEGACY_OTP_FIELDS or folded.startswith(("timeotp-", "hmacotp-")):
                entry.delete_custom_property(field_name)
        if values.get("otp"):
            entry.otp = values["otp"]
        elif entry.otp:
            entry.otp = ""
        from istota.credentials import generated
        for field_name in list(entry.custom_properties):
            if str(field_name).casefold() == generated.RECOVERY_FIELD.casefold():
                entry.delete_custom_property(field_name)
        if values.get("recovery"):
            entry.set_custom_property(generated.RECOVERY_FIELD, values["recovery"], protect=True)

        def check(verified):
            return not generated.divergence(values, verified.generated.get(name))

        return check

    return _write_entry(
        location, passphrase, expected_digest=expected_digest, lock_root=lock_root,
        db_path=db_path, user_id=user_id, prepare=prepare,
    )


def remove_mirrored_entry(
    location,
    passphrase: str,
    *,
    name: str,
    expected_digest: str,
    lock_root: Path,
    db_path: Path | None = None,
    user_id: str | None = None,
) -> str:
    """Delete a retired generated credential's copy from ``generated/``. Returns the digest."""

    def prepare(kp, read):
        group = _generated_group(kp, create=False)
        for entry in _generated_entries(group, name):
            kp.delete_entry(entry)

        def check(verified):
            return name not in verified.generated

        return check

    return _write_entry(
        location, passphrase, expected_digest=expected_digest, lock_root=lock_root,
        db_path=db_path, user_id=user_id, prepare=prepare,
    )


def _write_entry(location, passphrase, *, expected_digest, lock_root, db_path, user_id, prepare):
    """Lock, mutate in memory, verify a staged vault, then replace and import.

    ``prepare(kp, read)`` mutates the open database and returns
    ``check(verified) -> bool``, which the staged file must pass before it
    replaces the live one.
    """
    with _vault_file_lock(location, lock_root=lock_root, blocking=False):
        data, digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
        if digest != expected_digest:
            raise VaultChanged("the vault changed since it was read")
        read = parse_vault(data, passphrase)
        if read.truncated:
            raise VaultWriteRefused("the vault read stopped at a cap")
        try:
            from pykeepass import PyKeePass
        except ImportError as exc:
            raise VaultLibraryMissing("the 'vault' extra is not installed on this host") from exc
        kp = PyKeePass(io.BytesIO(data), password=passphrase)
        check = prepare(kp, read)

        leaf = _vault_leaf(location)
        original = os.stat(leaf, dir_fd=location.dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(original.st_mode):
            raise VaultUnreadable("the vault is not a regular file")
        temp_name = f".{location.path.name}.{secrets.token_hex(12)}.tmp"
        temp_leaf = Path(temp_name) if location.dir_fd is not None else location.path.parent / temp_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_leaf, flags, 0o600, dir_fd=location.dir_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                try:
                    kp.save(stream)
                except Exception:  # noqa: BLE001 - pykeepass can reject malformed XML text
                    raise VaultWriteRefused("the vault library could not save this entry") from None
                _preserve_vault_metadata(stream.fileno(), original)
                stream.flush()
                os.fsync(stream.fileno())
            temp_data, temp_digest = read_vault_bytes(temp_leaf, dir_fd=location.dir_fd)
            verified = parse_vault(temp_data, passphrase)
            if verified.truncated or not check(verified):
                raise VaultWriteRefused("the saved vault did not verify")
            latest, latest_digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
            if latest_digest != digest or latest != data:
                raise VaultChanged("the vault changed during the write")
            # Not atomic_write: this rename is dir_fd-relative and only runs
            # after the staged file verified and the live one did not change.
            os.replace(temp_leaf, leaf, src_dir_fd=location.dir_fd, dst_dir_fd=location.dir_fd)
            if db_path is not None and user_id is not None:
                try:
                    applied = apply_vault(db_path, user_id, verified)
                except Exception as exc:  # noqa: BLE001 - the file is already replaced
                    # Never cache the new digest when the apply did not finish;
                    # the next sync retries it.
                    logger.warning(
                        "vault: %s: apply after write failed (%s); sync will retry",
                        _label(user_id), type(exc).__name__,
                    )
                else:
                    recorded = _record_sync_state(
                        db_path, user_id, OUTCOME_OK, "",
                        unscoped=not verified.scoped,
                        generated_count=verified.generated_count,
                        name_conflicts=applied.name_conflicts,
                    )
                    if recorded:
                        _SYNC_STATE[user_id] = (temp_digest, OUTCOME_OK)
                    else:
                        reset_sync_state(user_id)
            return temp_digest
        finally:
            try:
                os.unlink(temp_leaf, dir_fd=location.dir_fd)
            except FileNotFoundError:
                pass


def _open_vault_for_write(config, user_id: str):
    """``(location, passphrase)`` for a mirror write, or ``None`` with no vault.

    The caller closes ``location.dir_fd``.
    """
    from istota import storage

    if not _vault_is_enabled(config, user_id) or vault_isolation_refusal(config, user_id):
        return None
    location = storage.vault_location_for(config, user_id).location
    if location is None:
        return None
    try:
        passphrase = _resolve_passphrase(config.db_path, user_id)
    except VaultError:
        if location.dir_fd is not None:
            os.close(location.dir_fd)
        raise
    return location, passphrase


def _with_vault_write(config, user_id: str, write) -> bool:
    """Run ``write(location, passphrase, digest)`` once, retried once on a change.

    ``False`` when there is no vault or the write could not land; the caller
    records that and the next sync cycle tries again. Never raises: a stored
    credential must not turn into an error because its copy failed.
    """
    try:
        opened = _open_vault_for_write(config, user_id)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("vault: %s: mirror write skipped (%s)", _label(user_id), type(exc).__name__)
        return False
    if opened is None:
        return False
    location, passphrase = opened
    try:
        for attempt in range(2):
            try:
                _, digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
                write(location, passphrase, digest)
                return True
            except VaultChanged:
                if attempt:
                    raise
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("vault: %s: mirror write did not land (%s); will retry",
                       _label(user_id), type(exc).__name__)
        return False
    finally:
        if location.dir_fd is not None:
            os.close(location.dir_fd)
    return False


def mirror_generated(config, user_id: str, name: str) -> str:
    """Write a generated credential's table copy to the KeePass file now.

    Returns the resulting state: ``mirrored``, ``pending`` (the write did not
    land, or the values changed during it; the sync retries) or ``off``
    (mirroring is off, or no vault). Must not be called while the caller holds
    the vault lock. Divergence notices raised by the write's own apply are
    written to the bell but not pushed; the next sync pushes nothing more for
    them, since a notice is raised once per divergence.
    """
    from istota import db
    from istota.credentials import generated

    if not _vault_is_enabled(config, user_id):
        return generated.STATE_OFF
    with db.get_db(config.db_path) as conn:
        if not generated.is_generated(conn, user_id, name):
            return generated.STATE_OFF
        if not generated.mirror_state(conn, user_id, name)["mirror"]:
            return generated.STATE_OFF
        values = generated.stored_values(conn, user_id, name)

    def write(location, passphrase, digest):
        mirror_entry(location, passphrase, name=name, values=values, expected_digest=digest,
                     lock_root=config.db_path.parent, db_path=config.db_path, user_id=user_id)

    landed = _with_vault_write(config, user_id, write)
    with db.get_db(config.db_path) as conn:
        return generated.record_mirror_result(conn, user_id, name, values, landed)


def unmirror_generated(config, user_id: str, name: str) -> bool:
    """Remove a retired credential's KeePass copy. The tombstone stays."""
    from istota import db
    from istota.credentials import generated

    def write(location, passphrase, digest):
        remove_mirrored_entry(location, passphrase, name=name, expected_digest=digest,
                              lock_root=config.db_path.parent, db_path=config.db_path,
                              user_id=user_id)

    if not _with_vault_write(config, user_id, write):
        return False
    with db.get_db(config.db_path) as conn:
        generated.record_copy_removed(conn, user_id, name)
    return True


def mirror_pending(config, user_id: str) -> None:
    """Retry the mirror writes and removals that have not landed. Never raises."""
    from istota import db
    from istota.credentials import generated

    try:
        with db.get_db(config.db_path) as conn:
            to_write, to_remove = generated.pending_names(conn, user_id)
    except Exception:  # noqa: BLE001 - a sync cycle must not fail on its retry pass
        logger.warning("vault: %s: mirror retry list failed", _label(user_id), exc_info=True)
        return
    for name, retry in [(n, mirror_generated) for n in to_write] + [
            (n, unmirror_generated) for n in to_remove]:
        try:
            retry(config, user_id, name)
        except Exception:  # noqa: BLE001 - one credential must not stop the others
            logger.warning("vault: %s: mirror retry for %s failed", _label(user_id),
                           _label(name), exc_info=True)


def parse_vault(data: bytes, passphrase: str) -> VaultRead:
    """``data`` opened with ``passphrase``, mapped, or a mapped ``VaultError``.

    ``PyKeePass`` takes a file-like object — ``PyKeePass.read`` branches on
    ``hasattr(filename, "read")`` — so the ciphertext is opened from memory and
    the plaintext never touches disk.

    The library import lives here, not at module scope: ``pykeepass`` pulls
    ``lxml``, ``argon2-cffi`` and ``pycryptodomex``, two of them with compiled
    extensions, and a deployment without the ``vault`` extra has to reach
    ``VaultLibraryMissing`` with a remedy rather than an ImportError out of a
    background gate at startup.

    **The catch-all names the exception's type and does not log it.** The spec
    asked for ``exc_info``, and the no-value rule outranks it: KDBX3 carries no
    payload HMAC, so a body corrupted after decryption reaches lxml rather than
    failing a checksum, and an ``XMLSyntaxError`` quotes the document it choked
    on — which at that point is decrypted credential text. Measured against
    pykeepass 4.2.0, a header intact over a garbled body also reaches here as a
    bare ``KeyError`` whose argument is an XML key lifted out of the decrypted
    document. The chain is suppressed for the same reason, since any caller's
    ``logger.exception`` would render it. The type and its module are enough to
    tell a ``construct`` stream error from an XML one, and carry no payload.

    **The catch-all is the ordinary path for a half-synced file, not an
    exceptional one**, so the log line does not call it unexpected. Only an HMAC
    mismatch raises ``PayloadChecksumError``; a *truncated* file — §2's own named
    mid-write case — dies inside ``construct`` with a ``StreamError`` that
    pykeepass re-raises unmapped. Adding ``construct.core.StreamError`` to the
    mapped arm was the alternative and is refused: it would name a transitive of
    a transitive in an import this module otherwise keeps to pykeepass's own
    surface, to change a log string. Which arm ran is pinned by that string.

    **The mapping runs inside the guard too.** A file that decrypts and is then
    structurally malformed — `/KeePassFile/Root/Group` absent, which needs the
    passphrase and so cannot be arranged by a task — makes ``kp.root_group``
    None, and an ``AttributeError`` out of the walk is not a class §7 and §8 can
    act on.
    """
    try:
        from pykeepass import PyKeePass
        from pykeepass.exceptions import (
            CredentialsError,
            HeaderChecksumError,
            PayloadChecksumError,
        )
    except ImportError as exc:
        raise VaultLibraryMissing(
            "the 'vault' extra is not installed on this host"
        ) from exc

    try:
        kp = PyKeePass(io.BytesIO(data), password=passphrase)
        return _map_groups(kp, _digest(data))
    except CredentialsError as exc:
        # Deliberately does not name the passphrase as the cause: see
        # VaultLocked. The key file is named because it is the one cause the
        # user can check in a few seconds and the one istota cannot support.
        raise VaultLocked(
            "the stored credentials do not open this vault — the passphrase may "
            "be wrong, or the file may also need a key file or a hardware key, "
            "which istota cannot supply"
        ) from exc
    except (HeaderChecksumError, PayloadChecksumError) as exc:
        raise VaultCorrupt("the file is not a readable KeePass database") from exc
    except Exception as exc:
        logger.warning(
            "vault: parse failed (%s.%s)",
            type(exc).__module__,
            type(exc).__name__,
        )
        raise VaultCorrupt("the file is not a readable KeePass database") from None


@dataclass
class VaultApplyResult:
    """What one apply did, in the vocabulary the CLI and the panel report.

    ``deleted_keys`` names what went rather than counting it, because the
    deletion rule's documented surprise is a sync removing credentials the user
    did not realise the file was authoritative for — a bare count tells them
    four are gone and leaves them to work out which four.

    ``skipped`` is ``(name, reason)``, from the fixed vocabulary above, never a
    sentence built around a name. It carries the read's own refusals as well as
    the apply's, so one list answers "why is this name not here".

    ``unreadable_overwrites`` counts the writes that landed on a row which was
    present and would not decrypt. Those are the reason the ``created`` /
    ``updated`` split cannot be taken from ``upsert_secret`` alone: it derives
    its answer from ``get_secret``, which reports an undecryptable row as absent,
    so every write on a deployment with a stale ``ISTOTA_SECRET_KEY`` would
    otherwise read as ``created``. The pre-check below corrects the split and
    this counts what it corrected, which is a condition an operator wants named
    — it is the same stale key the deletion side already holds rows back for.

    ``swept`` is whether the namespace sweep ran at all. It is False on a
    truncated read, where no deletion is licensed because the names past the
    cap are absent for a reason that has nothing to do with the user removing
    them — and ``deleted == 0`` alone cannot tell that from a file nobody
    edited.

    ``name_conflicts`` counts the file *entries* skipped as
    :data:`SKIP_NAME_TAKEN` (an entry's username and URL fields are skipped
    with it and not counted again), for the sync record the settings card
    reads without opening the file.
    """

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    unreadable_overwrites: int = 0
    swept: bool = True
    deleted_keys: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    name_conflicts: int = 0
    auto_granted: int = 0
    #: Divergence notices written this pass, for the caller to push once the
    #: vault lock and its directory descriptor are released.
    generated_notices: list = field(default_factory=list)


def apply_vault(db_path: Path, user_id: str, read: VaultRead) -> VaultApplyResult:
    """Write ``read``'s namespace into ``vault_entries``, and sweep out of it.

    Two rules, and the second is what makes deleting an entry revoke it:

    1. **Every name in the read is upserted** under
       :data:`VAULT_ENTRY_SERVICE`, counted by the state ``upsert_secret``
       returns.
    2. **Every vault-sourced ``vault_entries`` row for this user whose key is
       not in the read is deleted.** The file is authoritative for the names it
       produced. The namespace has three sources, read off each row's binding
       (:func:`_stored_entry_sources`): ``vault`` (this sync, and any legacy
       row with no binding, since the sync was the only writer that ever
       produced one), ``local`` (a credential added in Istota, written only by
       ``local_credentials``) and ``config`` (deployment forge tokens). Only
       ``vault`` rows are swept or rebound here.

    **A name the file produces that a ``local`` row holds is not written.** It
    is skipped as :data:`SKIP_NAME_TAKEN` and counted in ``name_conflicts``:
    the file never overwrites a value the user typed into Istota, and the local
    row keeps its grant. Every field of that file entry is skipped with it.
    Renaming either one ends the conflict on the next sync that reads the
    file: the digest cache skips an unchanged file, so that is the next edit
    to it, a restart, or a forced sync.

    **Four things hold a deletion back, and every one of them is about a delete
    being the single action here that cannot be undone by fixing the cause.**

    - A name in ``read.held``: the file produced it and could not supply a
      value — an empty field, or one over the byte cap. Blanking a password
      field is the fat-finger case and must not delete the credential it was
      meant to change; refusing an oversize value *and* destroying the old one
      is a combination no rule here intends. Deleting a credential means
      deleting the entry.
    - A **truncated** read (``read.truncated``): this read is a prefix of the
      file, so a stored name's absence from it says nothing about the file. The
      whole sweep is withheld rather than filtered, because there is no way to
      tell a name past the cap from one the user removed. Writes still land —
      the point of a cap that applies what it read is that a 513-entry vault
      gives the user 512 working credentials rather than none — and deletions
      are the half that must wait for a read that got to the end.
    - A row that is present and will not decrypt: that is a stale master key
      rather than a credential the user retired, and the credential comes back
      when the right key does but never from a delete.
    - A name this pass just wrote. Subtracted so the pass can never write a
      name and delete it in the same call; it follows from the stored set being
      read before any write, and keeping it explicit makes it a property of
      this function rather than one inherited from a collaborator.

    **The passphrase is out of reach structurally rather than by exemption.**
    The sweep enumerates ``VAULT_ENTRY_SERVICE`` alone, and ``vault/passphrase``
    is in a different service — so there is no filter here that a later edit
    could get wrong, and an entry the user titles ``passphrase`` lands at
    ``vault_entries/passphrase`` beside it.

    **Every write happens before any delete**, which is the reason the
    deletions are planned into a list and executed at the end rather than
    inline: a failure part-way through leaves credentials present rather than
    absent.

    **A missing master key refuses the whole pass**, in the store's own
    vocabulary. Without it an empty read on a deployment that can neither read
    what it is removing nor write a replacement would sweep the namespace flat.
    That guard tests presence and a length floor, which is all
    ``secret_key_available`` can see, so a key that is *wrong* passes it — the
    per-row readability test above is what covers that, and neither substitutes
    for the other.

    **It bumps ``last_accessed_at`` on every name it reaches**, because
    vault-wins needs the comparison ``upsert_secret`` makes against
    ``get_secret``. What that means is that ``last_accessed_at`` on a
    ``vault_entries`` row is not evidence anything read it.
    """
    try:
        # The store's own validator rather than `secret_key_available`, which
        # collapses "absent" and "below the length floor" into one False — and
        # the two have different remedies, which is the distinction `doctor`'s
        # `security.secret_key` check is built around. Reaching for the private
        # name follows that check's precedent (it reads `_MIN_KEY_LEN` from here
        # for the same reason): a second copy of the rule is the drift the rule
        # exists to catch. The key itself is not bound.
        secrets_store._validated_key()
    except (
        secrets_store.SecretKeyMissingError,
        secrets_store.SecretKeyTooWeakError,
    ) as exc:
        raise type(exc)(
            f"{exc} Refusing to apply a vault: it would delete credentials it "
            f"can neither read nor replace."
        ) from exc

    result = VaultApplyResult(skipped=list(read.skipped))
    # Before the stored set is read: adopting a legacy `generated/` row moves
    # it out of the `vault` subset the sweep below may delete from.
    result.generated_notices = _reconcile_generated(db_path, user_id, read)

    # The stored key set, read **before** any write and including rows that
    # will not decrypt — which is why this is `list_user_services` rather than
    # `get_service_secrets`: that one silently drops an undecryptable row, and
    # a sweep built on it would not know such a row exists to hold back.
    sources = _stored_entry_sources(db_path, user_id)
    stored = {name for name, source in sources.items() if source == "vault"}
    taken = {name for name, source in sources.items() if source in ("local", "generated")}
    _baseline_auto_grants(db_path, user_id, sources)

    written: set[str] = set()
    conflicting_entries: set[str] = set()
    for name in sorted(read.services):
        # The whole entry is skipped, not just the colliding name: a sibling
        # field carries `credential: <owner>` and would otherwise join the
        # local credential's group, and its grant, bound to the file's hosts.
        owner = read.bindings.get(name, {}).get("credential", name)
        if name in taken or owner in taken:
            result.skipped.append((name, SKIP_NAME_TAKEN))
            conflicting_entries.add(owner)
            result.name_conflicts = len(conflicting_entries)
            logger.warning(
                "vault: %s is already used by a credential added in Istota, "
                "so the file's entry is skipped",
                _label(name),
            )
            continue
        # Asked *before* the upsert, because `upsert_secret` derives its own
        # answer from `get_secret` and a row that will not decrypt reads there
        # as absent — so on a deployment with a stale master key every write
        # reports `created` and the counts an operator reads are exactly
        # backwards about what happened to their credentials.
        existed = name in sources
        state = secrets_store.upsert_secret(
            db_path, user_id, VAULT_ENTRY_SERVICE, name, read.services[name],
            binding=read.bindings.get(name, {"hosts": [], "headers": [],
                                             "revealable": False, "source": "vault"}),
        )
        written.add(name)
        if state == "created" and existed:
            # Present, overwritten, and unreadable beforehand: the row was
            # replaced rather than created, and the reason the store could not
            # tell is a condition worth naming on its own.
            result.updated += 1
            result.unreadable_overwrites += 1
            logger.warning(
                "vault: %s was stored but would not decrypt, so it has been "
                "overwritten from the vault; check ISTOTA_SECRET_KEY",
                _label(name),
            )
        elif state == "created":
            result.created += 1
        elif state == "updated":
            result.updated += 1
        elif state == "noop":
            result.unchanged += 1
        else:  # pragma: no cover - the store's contract is three literals
            raise ValueError(f"unexpected upsert state {state!r}")

    # A held value stays stored, but an edited URL or removed reveal tag must
    # still revoke its old policy. Ambiguous names are unbound by the parser.
    from istota import db
    from istota.credentials.broker.bindings import is_otp_seed, put_binding
    from istota.credentials.broker.grants import auto_grant_vault_entries
    owners = {read.bindings.get(name, {}).get("credential", name) for name in written}
    with db.get_db(db_path) as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        for name in sorted(read.held & stored):
            if name in read.bindings:
                binding = read.bindings[name]
                # The held value is unchanged, so its seed classification must survive.
                if is_otp_seed(conn, user_id, name):
                    binding = {**binding, "kind": "totp"}
                put_binding(conn, user_id, name, binding)
        result.auto_granted = auto_grant_vault_entries(
            conn, user_id, owners, declined=read.no_auto_grant, scoped=read.scoped,
        )
    if result.auto_granted:
        logger.info("vault: %s: granted %d new credential(s)", _label(user_id), result.auto_granted)

    if read.truncated:
        result.swept = False
        logger.warning(
            "vault: %s: the read stopped at the %s cap, so nothing is deleted "
            "this cycle; %d stored name(s) are left alone",
            _label(user_id),
            read.truncated,
            len(stored - written),
        )
        return result

    pending_deletes: list[str] = []
    for name in sorted(stored - written - read.held):
        if secrets_store.get_secret(db_path, user_id, VAULT_ENTRY_SERVICE, name) is None:
            # Present and undecryptable: a stale master key, a half-loaded
            # `secrets.env`, a rotation applied to the wrong host. The
            # process-level guard above cannot see this — `_MIN_KEY_LEN` and
            # presence are all it tests — so without this arm a wrong key turns
            # a transient misconfiguration into permanent loss: the credential
            # comes back when the right key does, and does not come back from a
            # delete. Never destroy what cannot be read.
            result.skipped.append((name, SKIP_UNREADABLE_ROW))
            logger.warning(
                "vault: %s is stored but will not decrypt, so it is not "
                "deleted; check ISTOTA_SECRET_KEY",
                _label(name),
            )
            continue
        pending_deletes.append(name)

    if pending_deletes:
        # Named **before** the loop rather than after it. Each `delete_secret`
        # commits its own transaction, so a raise part-way through — a locked
        # database on a background gate contending with the scheduler's own
        # writers — leaves earlier rows gone with nothing on any surface saying
        # which. Logging the plan over-reports in that case, which is the
        # direction to be wrong in: the operator gets a superset of what went.
        logger.warning(
            "vault: %s: deleting %d shared credential(s) absent from the "
            "vault: %s",
            _label(user_id),
            len(pending_deletes),
            ", ".join(_label(name) for name in pending_deletes),
        )
    for name in pending_deletes:
        if secrets_store.delete_secret(db_path, user_id, VAULT_ENTRY_SERVICE, name):
            result.deleted += 1
            result.deleted_keys.append(name)
    # The tag store must only consume a complete read. This repeats the early
    # `read.truncated` return above so a later refactor cannot move the closure
    # into the prefix-read path and permanently shut an address off.
    if not read.truncated:
        from istota import db
        from istota.credentials import generated
        with db.get_db(db_path) as conn:
            # A signup address stays open while its credential is stored; the
            # file's copy is a mirror and may be missing (ISSUE-686).
            present = set(read.services) | read.held | set(generated.generated_names(conn, user_id))
            db.close_missing_signup_tags(conn, user_id, present)
    return result


def _reconcile_generated(db_path: Path, user_id: str, read: VaultRead) -> list:
    """Adopt, import and compare generated credentials; raise a notice per new divergence."""
    from istota import db
    from istota.credentials import generated

    raised = []
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name, found in generated.reconcile(conn, user_id, read):
            logger.warning(
                "vault: %s: the KeePass copy of %s differs from Istota's (%s); not applied",
                _label(user_id), _label(name), ", ".join(found),
            )
            notice = generated.raise_divergence_notice(conn, user_id, name, found)
            if notice is not None:
                raised.append(notice)
    return raised


def _stored_entry_names(db_path: Path, user_id: str) -> set[str]:
    """Every ``vault_entries`` key this user has, decryptable or not.

    ``list_user_services`` rather than ``get_service_secrets``, and the
    difference is the whole of the unreadable-row hold: the latter drops a row
    it cannot decrypt, so a sweep built on it would not know the row is there
    and would not be able to hold it back from deletion. This one returns no
    plaintext at all, which is also what a delete plan wants.
    """
    services = secrets_store.list_user_services(db_path, user_id)
    return {
        str(row.get("key"))
        for row in services.get(VAULT_ENTRY_SERVICE, [])
        if row.get("key")
    }


def _baseline_auto_grants(db_path: Path, user_id: str, names) -> None:
    """Record the entries stored before auto-grant existed, before any write.

    Its own transaction ahead of the upserts, so a pass that fails part-way
    leaves the entries it created unmarked, and the next pass still grants them.
    """
    from istota import db
    from istota.credentials.broker.bindings import credential_name
    from istota.credentials.broker.grants import baseline_auto_grants
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        baseline_auto_grants(conn, user_id, {credential_name(conn, user_id, name) for name in names})


def _stored_entry_sources(db_path: Path, user_id: str) -> dict[str, str]:
    """Every ``vault_entries`` key this user has, with its binding source.

    Decryptable or not, for the reason :func:`_stored_entry_names` gives, and
    without opening a Fernet. A key with no binding row reads as ``vault``: the
    sync is the only writer that ever stored one without a binding, so a
    legacy row stays the sweep's to remove.
    """
    from istota import db  # noqa: PLC0415 - see `sync_user` for the import rule

    with db.get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT s.key, b.source FROM secrets s "
            "LEFT JOIN credential_bindings b ON b.user_id = s.user_id AND b.name = s.key "
            "WHERE s.user_id = ? AND s.service = ?",
            (user_id, VAULT_ENTRY_SERVICE),
        ).fetchall()
    return {str(row[0]): str(row[1] or "vault") for row in rows if row[0]}


def has_shared_credentials(db_path, user_id: str) -> bool:
    """Whether this user has anything in the shared-credential namespace.

    Presence, never a value: ``_stored_entry_names`` goes through
    ``list_user_services``, which returns key names and timestamps, opens no
    Fernet, needs no master key and bumps no ``last_accessed_at``. That matters
    because the one caller is the prompt gate, which runs on every task
    assembly including the dry run the goldens take.

    **Gated on the master key as well as on the rows**, so this answers the
    same question the serving read answers. ``get_service_secrets`` — what
    ``task_env`` hands the proxy — returns ``{}`` outright with no
    ``ISTOTA_SECRET_KEY``, which is a real shipped shape (``doctor``'s
    ``security.secret_key`` exists because the standalone wizard shipped
    without one). Without this arm the system half would tell the model it has
    credentials while ``istota-credential list`` returned nothing, with no line
    anywhere saying why.

    What it still cannot see is a row that will not decrypt under a *wrong*
    key: this counts it and the serving read drops it. That is the same
    asymmetry ``apply_vault`` records one screen up, and it is bounded — the
    prompt over-promises by one name rather than by the namespace.

    Never raises and never reports a vault that is not there: a falsy
    ``db_path`` and an unreadable database are both False, which is the
    direction that withholds a prompt line rather than promising a namespace
    nothing can serve.
    """
    if not db_path or not secrets_store.secret_key_available():
        return False
    # Opening creates a missing file, and a dry run with a bare Config()
    # reaches here with the relative default path (ISSUE-571).
    if not Path(db_path).is_file():
        return False
    try:
        return bool(_stored_entry_names(Path(db_path), user_id))
    except Exception as exc:
        logger.debug("vault: could not count shared credentials: %s", exc)
        return False


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recyclebin_uuid(kp):
    """The recycle bin's UUID, or None where there is not one.

    **The boundary on the unscoped path, defence in depth only on the scoped
    one**, and the distinction is new rather than pedantic. It used to be
    defence in depth outright: the walk started at the *children* of a named
    ``istota`` group, and KeePassXC's bin is a root-level group, so a trashed
    entry and a trashed ``istota`` group were both outside what was read by
    construction. §1's unscoped read starts **at** the file root, where a
    root-level bin is an ordinary subgroup — so ``_visit_group``'s
    ``subgroup.uuid == walk.recyclebin`` test is now the only thing keeping a
    credential the user deleted out of ``vault_entries``, and out of the
    namespace their own tasks can fetch by name. Do not remove it as redundant.

    It still covers the layout neither path excludes by construction: any group
    can be nominated as the bin, including one nested inside the read, at which
    point everything the user deleted sits exactly where the walk looks.

    ``recyclebin_group`` reads the Meta element and decodes it, so a database
    whose Meta is absent or malformed raises rather than answering — and a
    missing recycle bin must not fail the whole read.
    """
    try:
        group = kp.recyclebin_group
    except Exception:  # pragma: no cover - a Meta element no editor writes
        return None
    return getattr(group, "uuid", None) if group is not None else None


def slug_name(segments: Sequence[str]) -> str | None:
    """The one name these path segments produce, or ``None`` for none.

    A name is derived rather than typed, so the user never has to learn
    istota's spelling rules to fill in the file. Each segment — a group name, an
    entry title, or the field's own suffix — is casefolded, every character
    outside ``[a-z0-9]`` becomes ``_``, runs of ``_`` collapse and the ends are
    stripped; the segments are then joined with ``_`` and the whole must match
    :data:`VAULT_NAME_RE`.

    **A segment that slugs to nothing refuses the whole name**, and that is a
    rule rather than a shortcut: the pattern admits a trailing underscore, so
    joining an empty segment would quietly answer ``aws_`` for
    ``istota/aws/!!!`` — a name the user never wrote, which a sibling
    ``istota/aws/???`` produces identically. Collisions are caught a level up,
    so the pair would at least not be applied; a single such entry would be.

    **Pure, and the caller warns.** The composed name is what a warning has to
    report and only the caller knows which entry and which field produced it,
    so this returns ``None`` and every call site names its own subject through
    :func:`_label`. Nothing here logs, which also makes it safe to call from a
    test in a loop.
    """
    parts: list[str] = []
    for segment in segments:
        slug = _SLUG_DROP_RE.sub("_", str(segment).casefold()).strip("_")
        if not slug:
            return None
        parts.append(slug)
    if not parts:
        return None
    name = "_".join(parts)
    return name if VAULT_NAME_RE.fullmatch(name) else None


@dataclass
class _Walk:
    """What one walk has found so far. Mutable, single-threaded, per parse."""

    recyclebin: object = None
    #: name -> every value produced under it, so a second producer is a
    #: collision rather than an overwrite.
    candidates: dict[str, list[str]] = field(default_factory=dict)
    bindings: dict[str, dict] = field(default_factory=dict)
    #: Entry names the sync must not grant on its own (ISSUE-590).
    no_auto_grant: set[str] = field(default_factory=set)
    otp_names: set[str] = field(default_factory=set)
    #: Mirror copies under `generated/`; a name produced twice is dropped.
    generated: dict[str, dict[str, str]] = field(default_factory=dict)
    generated_duplicates: set[str] = field(default_factory=set)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    entries_visited: int = 0
    fields_examined: int = 0
    untitled: int = 0
    depth_dropped: int = 0
    stopped: str = ""


def _root_groups(kp, recyclebin) -> tuple[list, list]:
    """The top-level groups that narrow the read, and the ones to walk.

    Two lists, because they answer different questions. The first is "does
    anything narrow this read at all", which decides ``VaultRead.scoped`` and
    therefore whether the file root is walked instead. The second is what to
    walk, which excludes a group nominated as the recycle bin. They differ by
    exactly that group, and collapsing them is the widening the second
    paragraph below is about.

    The match strips surrounding whitespace and folds case, so ``Istota``,
    ``ISTOTA`` and ``"istota "`` all scope — see :data:`VAULT_ROOT_GROUP` for
    why that is load-bearing rather than a courtesy. **Top level only**: a
    group named ``istota`` nested inside another is an ordinary group and
    contributes a path segment like any other.

    **A group nominated as the recycle bin still counts as one, and that is a
    narrowing rather than an oversight.** It contributes no entries — the
    caller drops it — but its *presence* keeps the read scoped, so the answer
    is "this file narrows to a group that happens to hold only deleted things",
    which reads nothing. Skipping it outright would leave no root at all, and
    the caller would then read the **whole file**: a widening arranged by the
    Meta element of a file a task in that user's own sandbox can write, which
    is the one direction this module must never move by accident.

    Several matching groups are all read, which is what the old exact match did
    for duplicates; nothing chooses between them, because choosing would decide
    by XML order. The casefold makes an `istota` / `Istota` pair newly
    reachable, and a name they share is then an ordinary collision — held where
    fewer than two of them supply a value, skipped where more do.
    """
    found = []
    live = []
    for group in kp.root_group.subgroups:
        if str(group.name or "").strip().casefold() != VAULT_ROOT_GROUP:
            continue
        found.append(group)
        if recyclebin is None or group.uuid != recyclebin:
            live.append(group)
    return found, live


def _map_groups(kp, digest: str) -> VaultRead:
    """Every name the file shares, out of an open database.

    **The file is the consent boundary and the ``istota`` group is an optional
    narrowing inside it** (§1). A top-level group matching
    :data:`VAULT_ROOT_GROUP` scopes the read to it and everything outside is
    parsed and discarded; with no such group the **whole file** is read, the
    root standing in for ``istota/``. Either way the shape below the starting
    point is the user's own — entries directly under it or in subgroups nested
    to :data:`VAULT_MAX_DEPTH`.

    An unscoped read is reported rather than refused: ``VaultRead.scoped`` is
    False, the settings card and ``vault-status`` say so, and the first
    unscoped sync for a user raises a notification. It is not a refusal because
    istota does not second-guess a file the user deliberately placed in their
    own vault folder.

    Each entry contributes one name per field: the password under the entry's
    own path, the username, the URL and each custom string field under that
    path plus one more segment. A field that is empty after stripping produces
    a name with no value, which is *held* rather than dropped — see
    :class:`VaultRead`. Notes are not read (they are free text and frequently
    hold something other than a credential), and neither are attachments.

    **Custom string fields are read where the previous design ignored them.**
    The reason they were ignored still holds — several mobile clients cannot
    create them — but it argues against *requiring* them rather than against
    reading one that is there, and an entry with four values in it is exactly
    the case that did not fit before.

    **A name produced twice anywhere in the tree is skipped in every place it
    was produced**, with one warning. The derivation flattens, so
    ``istota/aws/key``, ``istota/AWS Key`` and an entry ``aws`` with a custom
    field ``key`` all yield ``aws_key``; choosing one silently would make which
    credential is live depend on XML order. That is why the collision is
    resolved over the whole namespace after the walk rather than per group
    during it.

    **The caps bound the work after decryption**, not the read — the file is
    already refused unread above :data:`VAULT_READ_CAP_BYTES`. Each of them
    warns and applies what it read, because half a namespace applied is a user
    with some credentials working and a refusal is a user with none.

    **Depth comes from ``Group.entries`` and ``Group.subgroups`` being direct
    children**, which is a property of pykeepass rather than of anything written
    here — so an edited entry's ``History`` copies are not entries of the group,
    and are not duplicates of it. That is the expensive one if it ever changes,
    since it would turn every credential the user has ever edited into a
    collision.
    """
    walk = _Walk(recyclebin=_recyclebin_uuid(kp))
    named, roots = _root_groups(kp, walk.recyclebin)
    # `named` rather than `roots`: a file whose only `istota` group is the
    # recycle bin is a *scoped* read of nothing, not an unscoped read of
    # everything. See `_root_groups`.
    scoped = bool(named)
    # The database's own root group is the starting point when nothing narrows
    # the read, and it contributes **no** path segment — it stands in for
    # `istota/`, so `<root>/aws/key` produces `aws_key` exactly as
    # `istota/aws/key` does. Its own entries are read too: a KDBX exported out
    # of a password manager commonly has entries sitting at the top level.
    starting_roots = roots if scoped else [kp.root_group]
    generated_count = sum(
        len(group.entries)
        for root in starting_roots
        for group in root.subgroups
        if str(group.name or "").strip().casefold() == VAULT_WRITE_GROUP
        and group.uuid != walk.recyclebin
    )
    for root in starting_roots:
        _visit_group(walk, root, (), 0)

    if not scoped:
        # A notice, never a refusal. One line per parse rather than per name,
        # and it carries a count rather than any name — the durable half of
        # this is the notification `_publish` raises on the first unscoped
        # sync, which is what catches "I pointed at my real password database"
        # within one interval instead of never.
        logger.warning(
            "vault: no top-level %r group, so the whole file is shared "
            "(%d name(s))",
            VAULT_ROOT_GROUP,
            len(walk.candidates),
        )
    if walk.untitled:
        # Counted rather than one line each: an entry with no title has no name
        # to report, so N lines say exactly what one line says.
        logger.warning(
            "vault: %d entr%s had no title, skipped",
            walk.untitled,
            "y" if walk.untitled == 1 else "ies",
        )
    if walk.depth_dropped:
        logger.warning(
            "vault: %d group(s) deeper than %d level(s) below the read's root "
            "were not read",
            walk.depth_dropped,
            VAULT_MAX_DEPTH,
        )
    if walk.stopped:
        logger.warning(
            "vault: stopped reading at the %s cap; what was read is applied",
            walk.stopped,
        )

    services: dict[str, str] = {}
    held: set[str] = set()
    for name, produced in walk.candidates.items():
        if len(produced) > 1:
            values = sum(1 for value in produced if value)
            if values < 2:
                # **Held rather than deleted, and the count is the reason.**
                # §2 licenses the deletion of a collided name on one premise:
                # "istota cannot say which of two values it is." With fewer
                # than two values that premise is simply false — there is
                # nothing to choose between — so the licence does not apply and
                # destroying the stored row is a loss with no argument behind
                # it. The name is still absent from `services`, so nothing is
                # *written* on ambiguous evidence; only the destruction is
                # withheld.
                #
                # The one-value case is the one that costs a credential, and
                # the tree had already measured it: `_near_miss_title`, removed
                # with the service mapping this change replaces, existed for
                # exactly it and its docstring recorded the observation —
                # `API_KEY` with a blank password beside a stored `api_key`
                # deleted the credential. Under the flat namespace both titles
                # slug to one name, so the hazard survived the mechanism that
                # used to cover it. Silent at zero values (an entry with no URL
                # is the ordinary case and two colliding entries collide on
                # their empty fields too, so warning there names fields the
                # user never filled in) and warned at one, because at one the
                # user has a real credential that is not being applied.
                held.add(name)
                if values:
                    walk.skipped.append((name, SKIP_DUPLICATE_NAME))
                    logger.warning(
                        "vault: %s is produced by %d entries and only one of "
                        "them has a value, so none is used and the stored "
                        "credential is left alone; rename one",
                        _label(name),
                        len(produced),
                    )
                continue
            # Two or more real values. Absent, and therefore deleted if it was
            # there before — which is correct, since istota cannot say which of
            # them it is, and keeping whichever is already stored would make
            # that answer depend on history rather than on the file. Not held
            # for exactly that reason.
            walk.skipped.append((name, SKIP_DUPLICATE_NAME))
            logger.warning(
                "vault: %s is produced by %d entries with %d different values, "
                "so none of them is used; rename one",
                _label(name),
                len(produced),
                values,
            )
            continue
        value = produced[0]
        if not value:
            # Silent: an entry with no URL is the ordinary case, not a mistake.
            # It is *held* rather than dropped, which is the whole point —
            # blanking a password field must not delete the credential.
            held.add(name)
            continue
        # `surrogatepass` is belt-and-braces rather than a live case: lxml
        # refuses to serialize a lone surrogate, measured against pykeepass
        # 4.2.0, so no KDBX can hold one. It is kept because the alternative is
        # this measurement raising, and `secrets_store`'s own encode is strict
        # — so a value that somehow carried one would abort a pass part-way
        # rather than being skipped here.
        size = len(value.encode("utf-8", "surrogatepass"))
        if size > VAULT_MAX_VALUE_BYTES:
            walk.skipped.append((name, SKIP_OVERSIZE_VALUE))
            held.add(name)
            logger.warning(
                "vault: %s is %d bytes, over the %d-byte limit, so it is not "
                "used; the stored value is left alone",
                _label(name),
                size,
                VAULT_MAX_VALUE_BYTES,
            )
            continue
        services[name] = value

    return VaultRead(
        digest=digest,
        services=services,
        held=frozenset(held),
        # The depth cap is deliberately not truncation: see `VaultRead`.
        truncated=walk.stopped if walk.stopped in _TRUNCATING_CAPS else "",
        scoped=scoped,
        skipped=tuple(walk.skipped),
        generated_count=generated_count,
        bindings={name: (walk.bindings[name] if len(walk.candidates[name]) == 1
                         else {"hosts": [], "headers": [], "revealable": False, "source": "vault",
                               "kind": "totp" if name in walk.otp_names else "value"})
                  for name in services.keys() | held},
        no_auto_grant=frozenset(walk.no_auto_grant),
        generated={name: copy for name, copy in walk.generated.items()
                   if name not in walk.generated_duplicates},
    )


def _visit_group(walk: _Walk, group, path: tuple[str, ...], depth: int) -> None:
    """One group's entries, then its subgroups, into ``walk``.

    ``depth`` is how many subgroup levels below the root group this one sits,
    so the root itself is ``0``. The recycle bin is tested at **every** level
    rather than at the top two: any group can be nominated as the bin,
    including one nested several deep, at which point everything the user
    deleted sits exactly where the walk looks.
    """
    for entry in group.entries:
        if walk.stopped:
            return
        if walk.entries_visited >= VAULT_MAX_ENTRIES:
            walk.stopped = "entry"
            return
        walk.entries_visited += 1
        _take_entry(walk, entry, path)

    for subgroup in group.subgroups:
        if walk.stopped:
            return
        if walk.recyclebin is not None and subgroup.uuid == walk.recyclebin:
            continue
        if depth + 1 > VAULT_MAX_DEPTH:
            walk.depth_dropped += 1
            continue
        name = subgroup.name or ""
        _visit_group(walk, subgroup, (*path, name), depth + 1)


def _take_entry(walk: _Walk, entry, group_path: tuple[str, ...]) -> None:
    """One entry's fields into ``walk.candidates``, or a warning saying why not.

    Every field is offered whatever its value, including an empty one, because
    an empty value is a *hold* rather than an absence — a name the file
    produced and cannot supply a value for is a name the applying half must not
    delete. Only the walk's own refusals (an unusable name) drop a name
    entirely.
    """
    title = entry.title or ""
    if not title:
        walk.untitled += 1
        return

    path = (*group_path, title)
    if slug_name(path) is None:
        walk.skipped.append((_original(path), SKIP_UNUSABLE_NAME))
        logger.warning(
            "vault: %s does not produce a usable name (letter first, at most "
            "%d characters of a-z, 0-9 and _), skipped",
            _original(path),
            VAULT_NAME_MAX_CHARS,
        )
        return

    from istota.credentials.broker.bindings import parse_binding
    attributes = entry.custom_properties
    binding = parse_binding(entry.url, attributes, entry.tags)
    # A task's `vault_create` writes under `generated/`, and its later tasks
    # are meant to need a grant the user gave, so the sync never gives one.
    if (VAULT_NO_GRANT_TAG in (entry.tags or [])
            or (group_path and str(group_path[0]).strip().casefold() == VAULT_WRITE_GROUP)):
        walk.no_auto_grant.add(slug_name(path))
    fields: list[tuple[tuple[str, ...] | None, object]] = [
        (path, entry.password),
        ((*path, _USERNAME_SEGMENT), entry.username),
        ((*path, _URL_SEGMENT), entry.url),
    ]
    otp_fields = {}
    for field_name, raw in attributes.items():
        folded = str(field_name).casefold()
        if folded in {"totp seed", "totp settings"} or folded.startswith(("timeotp-", "hmacotp-")):
            otp_fields[folded] = str(raw or "")
            # Consumed properties still spend the walk's field budget.
            fields.append((None, None))

    otp_value = None
    otp_uri = entry.otp
    try:
        if otp_uri:
            params = totp.parse_otpauth(otp_uri)
        elif any(name.startswith("timeotp-") for name in otp_fields):
            params = totp.parse_keepass_timeotp(otp_fields)
        elif "totp seed" in otp_fields:
            params = totp.parse_keepassxc_legacy(otp_fields["totp seed"], otp_fields.get("totp settings"))
        elif any(name.startswith("hmacotp-") for name in otp_fields):
            raise totp.TotpError("hotp_unsupported")
        else:
            params = None
        if params is not None:
            otp_value = totp.to_uri(params)
    except totp.TotpError as exc:
        if otp_uri:
            fields.append((None, None))
        label = slug_name((*path, "totp")) or _original(path)
        walk.skipped.append((label, f"{SKIP_UNUSABLE_OTP}: {exc.code}"))
        logger.warning("vault: %s OTP source skipped (%s)", _label(label), exc.code)

    if len(group_path) == 1 and str(group_path[0]).strip().casefold() == VAULT_WRITE_GROUP:
        # A mirror copy of a credential Istota generated (ISSUE-686): kept
        # apart so the apply can neither write it over the table nor sweep
        # the table's row when the copy is missing.
        _take_generated_copy(walk, slug_name(path), entry, otp_value)
        return

    otp_index = len(fields)
    if otp_value is not None:
        fields.append(((*path, "totp"), otp_value))
    for field_name, raw in sorted(attributes.items(), key=lambda kv: str(kv[0])):
        folded = str(field_name).casefold()
        if folded.startswith("istota_") or folded in otp_fields:
            continue
        fields.append(((*path, field_name), raw))

    produced = 0
    for index, (segments, raw) in enumerate(fields):
        # Counted **before** the name is derived rather than after: the branch
        # below that produces no name is not free, and a cap only the
        # successful branch pays is not a cap.
        if walk.fields_examined >= VAULT_MAX_NAMES:
            walk.stopped = "name"
            return
        walk.fields_examined += 1
        if segments is None:
            continue
        value = str(raw or "").strip()
        name = slug_name(segments)
        if name is None:
            # **Silent where the field is empty**, which is the ordinary case
            # rather than a mistake: every entry has a URL field and most have
            # no username, so a title at the length cap would otherwise warn
            # twice about names nobody asked for. An empty field with no usable
            # name is also nothing to hold — there is no name to hold it under.
            if value:
                walk.skipped.append((_original(segments), SKIP_UNUSABLE_NAME))
                logger.warning(
                    "vault: the field %s does not produce a usable name, skipped",
                    _original(segments),
                )
            continue
        if otp_value is not None and index > otp_index and name == slug_name((*path, "totp")):
            walk.skipped.append((name, SKIP_DUPLICATE_NAME))
            logger.warning("vault: %s custom field duplicates the OTP name, skipped", _label(name))
            continue
        walk.candidates.setdefault(name, []).append(value)
        walk.bindings[name] = {**binding, "credential": slug_name(path)}
        if otp_value is not None and index == otp_index:
            walk.bindings[name]["kind"] = "totp"
            walk.otp_names.add(name)
        if value:
            produced += 1

    if not produced:
        # Every field empty. One warning, no skip record: the hold is what
        # matters and it is already recorded, and the user's remedy is the same
        # sentence the old empty-password warning carried.
        logger.warning(
            "vault: %s has no value in any field, so nothing is set from it; "
            "delete the entry to remove the credential",
            _original(path),
        )


def _take_generated_copy(walk: _Walk, name: str, entry, otp_value: str | None) -> None:
    if walk.fields_examined >= VAULT_MAX_NAMES:
        walk.stopped = "name"
        return
    walk.fields_examined += 1
    if name in walk.generated:
        walk.generated_duplicates.add(name)
        walk.skipped.append((name, SKIP_DUPLICATE_NAME))
        return
    from istota.credentials.generated import RECOVERY_FIELD
    recovery = next((str(raw or "").strip() for field_name, raw in entry.custom_properties.items()
                     if str(field_name).casefold() == RECOVERY_FIELD.casefold()), "")
    walk.generated[name] = {
        "password": str(entry.password or "").strip(),
        "username": str(entry.username or "").strip(),
        "url": str(entry.url or "").strip(),
        "otp": otp_value or "",
        "recovery": recovery,
    }


def _original(segments: Sequence[str]) -> str:
    """The path as the file spells it, bounded and flattened.

    Bounded **where it is produced** rather than where it is rendered, because
    this is the one string in a :class:`VaultRead` that is file text rather
    than a derived name: it is what a skip record carries when the refusal is
    that no usable name could be derived, and a skip record outlives the log
    line — it reaches ``vault-status`` and the read's own ``__repr__``. An
    entry title is unbounded and free to carry a newline, and the file is
    writable by a task in that user's own sandbox.
    """
    return _label("/".join(str(segment) for segment in segments))


def _label(name: str, limit: int = _LABEL_MAX_CHARS) -> str:
    """A name out of the vault file, bounded and flattened, for a log line.

    Group and entry names are arbitrary strings out of the file: unbounded in
    length and free to contain newlines, so an unflattened one can forge a log
    record in the daemon's own log. §12 records that a task in the user's own
    sandbox can overwrite that file, so they are attacker-reachable rather than
    merely self-inflicted — which is why this flattens rather than trusting the
    source. The rule is ``transport``'s ``_slug``: bound every axis that came
    from outside.

    ``limit`` is a parameter rather than a second copy of the function, because
    the two callers want different bounds for the same rule: a key name at 64,
    and a resolved path at ``_PATH_LABEL_MAX_CHARS``, where the shorter bound
    would truncate exactly the thing the log line exists to say.

    **Sliced before it is flattened**, which is the order rather than a detail:
    a flatten-then-slice costs a per-character loop and a full copy of an
    unbounded input to print a bounded one. Stage 3's review made the same
    correction to ``storage._bounded_for_log``, which states the same rule for a
    path out of ``config.toml``.
    """
    head = "".join(ch if ch.isprintable() else " " for ch in str(name)[:limit])
    return head + ("…" if len(str(name)) > limit else "")


# ---------------------------------------------------------------------------
# The passphrase
# ---------------------------------------------------------------------------

#: The key the vault passphrase is stored under, in the ``vault`` service.
VAULT_PASSPHRASE_KEY = "passphrase"

#: Bytes of entropy ``--generate`` mints. 32 random bytes rendered urlsafe-base64
#: is 43 characters, which is the ``openssl rand -base64 32`` form the spec
#: documented as the fallback, at the same strength. The precedent in the tree is
#: the Overland ingest token (``secrets.token_urlsafe(32)``), whose docstring
#: settles the same question for the same reason: the value has to reach a device
#: the server cannot write to, and the alternative is a human choosing one.
VAULT_PASSPHRASE_BYTES = 32

#: The floor a *supplied* passphrase must clear. **A proxy that does not measure
#: the property, used anyway with that stated**: a 24-character memorable
#: passphrase clears any reasonable character count at perhaps 40 bits, so this
#: catches the careless case and not the confident one. Measuring entropy
#: properly means a wordlist and a policy this module has no business owning;
#: refusing short values and making ``--generate`` the path everybody takes gets
#: the same outcome without one.
#:
#: Deliberately **not** ``secrets_store._MIN_KEY_LEN``, which happens to be the
#: same number today. That is the floor on the deployment's master Fernet key, a
#: different credential with a different lifecycle, and sharing the constant
#: would mean a change to either silently moving the other. ``doctor`` reads the
#: store's constant rather than copying it because there it is the *same* rule;
#: here it is not.
VAULT_PASSPHRASE_MIN_CHARS = 32


def generate_passphrase() -> str:
    """A fresh vault passphrase, for ``istota secret ensure --generate``.

    **Minted rather than documented, and that is the property the whole design
    rests on.** The vault file sits in a tree bound read-write into the user's
    own sandbox, so a prompt-injected task can read the ciphertext of every
    credential that user owns and carry it out. Argon2id makes that useless
    against 256 random bits and does not make it useless against a memorable
    phrase. Everything else in this design is a boundary against a mistake; this
    is the boundary against an adversary, and it is the only one.
    """
    import secrets as _secrets  # noqa: PLC0415 - stdlib, shadowed by the package

    return _secrets.token_urlsafe(VAULT_PASSPHRASE_BYTES)


def passphrase_refusal(value: str) -> str | None:
    """Why this *supplied* passphrase is refused, or None.

    **Refused rather than warned about.** A warning in ``vault-status`` is read
    after provisioning by whoever goes looking, which is not the person who has
    just typed a weak passphrase — and by then the file is encrypted under it.

    Applied to a supplied value and never to a generated one. That is not a
    detail: the floor exists to make ``--generate`` the path everybody takes, so
    a floor that could refuse ``--generate`` itself would break the remedy it was
    built to serve. It reads the module global at call time rather than binding
    it, so a caller cannot import the number and drift from the rule.

    **Two arms, and the whitespace one is the one that has actually bitten.**
    The length is measured on the value as it will be stored rather than on its
    stripped form, which is what the arm above guarantees: every caller stores
    what it passed here, so measuring a *different* string from the one that
    gets stored is how a passphrase can satisfy the floor and still not be the
    one the file was keyed with. ``generate_passphrase`` produces neither
    shape, so nothing here can refuse the generated path.
    """
    if value != value.strip():
        # Refused rather than stripped, because this is a credential and the
        # two answers are not equally safe. The floor below has always measured
        # `value.strip()` while every caller stored `value` unstripped, so a
        # passphrase pasted with a trailing newline — which is what copying out
        # of a password manager, a terminal or a file gives you — passed the
        # check, went into the row with the newline on it, and then opened
        # nothing. What the user saw was `VaultLocked`: "the stored passphrase
        # does not match the file", about a password that was correct.
        #
        # Stripping silently would fix that case and quietly alter a
        # credential in the one place a user cannot read it back to check, and
        # would be wrong for a passphrase that really does end in a space. So
        # the ambiguity is handed back rather than resolved.
        return (
            "a vault passphrase must not start or end with a space or a line "
            "break — check for a stray newline if you pasted it; if the "
            "padding really is part of your password, re-key the file without "
            "it"
        )
    if len(value) < VAULT_PASSPHRASE_MIN_CHARS:
        return (
            f"a vault passphrase must be at least {VAULT_PASSPHRASE_MIN_CHARS} "
            "characters, and should be generated rather than chosen"
        )
    return None


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

#: A cycle that read, parsed and applied.
OUTCOME_OK = "ok"
#: The digest matched a cached one, so nothing was parsed and nothing written.
OUTCOME_UNCHANGED = "unchanged"
#: The host-private lock stayed held, so this cycle deferred without a read.
OUTCOME_BUSY = "busy"
#: No ``vault_path`` for this user — the feature is off, which is the default.
OUTCOME_NOT_CONFIGURED = "not_configured"
#: ``sync_all``'s containment: something outside the closed set of vault errors
#: escaped ``sync_user``. A bug, reported rather than swallowed.
OUTCOME_ERROR = "error"


VAULT_ISOLATION_REASON = (
    "vaults are disabled on a multi-user deployment without effective sandboxing; "
    "same-uid tasks can access another user's credentials. Enable a working "
    "sandbox, or have the operator accept this exposure with [security] "
    "allow_unsandboxed_multi_user_vaults = true and restart the services"
)


class VaultIsolationRequired(VaultError):
    """The operator has not accepted unsandboxed multi-user vault access."""


def vault_has_other_users(config, user_id: str = "") -> bool:
    """Count task users, including a not-yet-configured CLI recipient."""
    users = set(config.users)
    if user_id:
        users.add(user_id)
    return len(users) > 1


def vault_isolation_refusal(config, user_id: str) -> str | None:
    """Gate the whole credential store on deployment isolation.

    The name is historical: it gates every source in the ``vault_entries``
    namespace (the KeePassXC sync, credentials added in Istota, and their use
    by tasks) before any credential is read.
    """
    if not vault_has_other_users(config, user_id):
        return None
    if config.security.allow_unsandboxed_multi_user_vaults is True:
        return None
    from istota.executor import effective_sandboxing

    if effective_sandboxing(config):
        return None
    return VAULT_ISOLATION_REASON


class VaultPathRefused(VaultError):
    """The configured ``vault_path`` is one the resolver may not open.

    ``str(exc)`` is a stable ``storage.VAULT_PATH_*`` id. Its own class because
    the remedy is a **config edit** rather than anything to do with the file,
    which puts it in the uncached set twice over: an unchanged digest would be no
    evidence a config error had been fixed, and there are no bytes to hash in the
    first place.

    Before this the resolver answered a bare ``None`` and the reason reached the
    daemon log and nothing else — so the cross-user typo §1 exists to close, the
    one case in this design about an attack rather than a mistake, reached no
    notification and no status row.
    """


class VaultPassphraseMissing(VaultError):
    """No ``vault/passphrase`` row for this user, so nothing can be opened.

    Distinct from ``VaultLocked`` although the remedy is the same command: §7
    lists the two separately, ``vault-status`` has to tell "never provisioned"
    from "does not match", and this one never reaches the unlock at all — so a
    cycle in this state costs no key derivation.
    """


class VaultKeyUnusable(VaultError):
    """``ISTOTA_SECRET_KEY`` cannot read this deployment's stored passphrase.

    Absent, below the store's own floor, or simply the wrong key — which
    ``get_secret`` reports as ``None``, indistinguishable from an absent row
    unless ``secret_exists`` is asked as well. One class for the three because
    they share a remedy, and it is the deployment's rather than the user's.

    Checked **before** the parse, so a broken deployment costs no key derivation,
    and reported here rather than left to ``apply_vault``'s own refusal — which
    is a backstop that only fires once the vault has already been opened.
    """


#: The outcomes whose digest a later cycle may skip on. **Success and
#: ``VaultCorrupt`` only**, and the rule is whether the remedy changes the file:
#: both of these are resolved by the bytes moving, so an unchanged digest is
#: genuine evidence that nothing has been fixed — which is what keeps a
#: permanently broken vault from logging every five minutes forever.
#:
#: Everything else is resolved somewhere other than the file — ``VaultLocked``
#: and ``VaultPassphraseMissing`` by ``istota secret ensure``,
#: ``VaultLibraryMissing`` by installing the extra, ``VaultKeyUnusable`` by
#: fixing the deployment's key, ``VaultPathRefused`` by a config edit — so
#: caching any of them makes the remedy inert: the operator follows a correct
#: instruction, the digest is unchanged, the next cycle skips, and nothing
#: happens until a restart.
#:
#: ``VaultMissing`` and ``VaultUnreadable`` are in the uncached set by
#: construction rather than by decision, since every refusal path in
#: ``read_overlay_bytes`` answers before a read and so has no bytes to hash. A
#: **zero-byte** file is the one ``VaultCorrupt`` that is uncached for the same
#: reason, and cheaply so: its retry is one bounded ``open(2)``, and a write
#: caught mid-flight is exactly the shape that should be looked at again.
_CACHEABLE_OUTCOMES = frozenset({OUTCOME_OK, VaultCorrupt.__name__})


# ---------------------------------------------------------------------------
# What a failure says, per class
# ---------------------------------------------------------------------------

#: The service name this module raises its notification under, in
#: ``connected_service``'s ``SERVICES`` allowlist. The same word as
#: ``VAULT_PASSPHRASE_SERVICE``, and that is not a coincidence worth collapsing:
#: one names a row in the secrets table and the other names a notification
#: object id, and they are equal because both are "the vault" rather than
#: because either is derived from the other.
VAULT_NOTIFICATION_SERVICE = "vault"

#: One sentence per outcome class, **code-owned and never ``str(exc)``**.
#:
#: §8 puts the class-specific text in ``raise_for_service``'s ``reason``
#: argument rather than in ``connected_service``'s ``_REMEDY``, because that
#: table is keyed by service and holds one static string each — so the classes
#: cannot have a row apiece there. This is that text.
#:
#: **Why a table rather than the exception's own message.** ``reason`` is
#: rendered into a notification body a browser displays, is copied into the
#: row's ``params``, and is written into the per-user sync record below, which
#: ``db_backup`` snapshots onto the mount. An exception message is safe only
#: until somebody interpolates a path, a length or a value into one — and there
#: is already a live example: ``secrets_store``'s too-weak-key message names the
#: master key's *length*, which is why ``_resolve_passphrase`` logs that message
#: and raises a fixed sentence instead. A table makes the property structural
#: rather than a rule each raise site has to keep.
#:
#: **The read's eight classes and the write's two classes.**
#: ``VaultUnreadable`` is in neither enumeration and reaches ``_settle`` by
#: exactly the route ``VaultMissing`` does, so it gets a sentence here and
#: ``test_every_vault_error_class_has_a_sentence`` walks the subclasses rather
#: than trusting either list.
NOTIFICATION_REASONS: dict[str, str] = {
    VaultIsolationRequired.__name__: VAULT_ISOLATION_REASON,
    VaultLocked.__name__: (
        "the stored credentials do not open the file — either the passphrase is "
        "wrong, or the file also needs a key file or a hardware key, which "
        "istota cannot supply. Check KeePassXC's Database Credentials: if a key "
        "file is set, remove it or use a different file here. If it is password "
        "only, re-provision with `istota secret ensure` and run "
        "`istota secret vault-sync`"
    ),
    VaultCorrupt.__name__: (
        "the file is not a readable KeePass database, which is also what a sync "
        "caught mid-write looks like, so check the mount before suspecting the "
        "file"
    ),
    VaultMissing.__name__: (
        "there is no vault file to read — put one in your vault folder, or "
        "check the path an operator configured"
    ),
    VaultUnreadable.__name__: (
        "the file at the configured path was refused unread — it is not a "
        "regular file, or it is over the size cap"
    ),
    VaultLibraryMissing.__name__: (
        "the `vault` extra is not installed on this host, which is an operator "
        "remedy rather than one you can act on"
    ),
    VaultPassphraseMissing.__name__: (
        "no vault passphrase has been provisioned yet — an operator provisions "
        "it with `istota secret ensure --service vault --key passphrase "
        "--generate`"
    ),
    VaultKeyUnusable.__name__: (
        "this deployment's ISTOTA_SECRET_KEY cannot read stored credentials, "
        "which is an operator remedy; the daemon log has the detail"
    ),
    VaultPathRefused.__name__: (
        "the configured vault_path is not one the daemon may open — an operator "
        "corrects it in config.toml"
    ),
    VaultChanged.__name__: (
        "the vault changed during a credential write — read it again before retrying"
    ),
    VaultWriteRefused.__name__: (
        "the requested credential could not be created without changing the "
        "vault's existing namespace"
    ),
}

#: For an outcome with no row above. Reachable only through a defect, since the
#: subclass walk in the tests requires every class to have one — but a raise
#: that said nothing would be worse than a raise that said this.
_DEFAULT_NOTIFICATION_REASON = "the vault could not be read; see the daemon log"


def notification_reason(outcome: str) -> str:
    """The sentence a given outcome class publishes, for every read surface."""
    return NOTIFICATION_REASONS.get(outcome, _DEFAULT_NOTIFICATION_REASON)


def resolution_reason(refusal: str | None) -> str:
    """The sentence a surface renders for a resolution that found no file.

    Beside :func:`_resolution_outcome` rather than inside it, because the two
    answer different questions and only one of them is about a failure: an
    unchosen folder is *not* an outcome — nothing failed and nothing is
    notified — and it still has something to say on the settings card, which is
    the surface whose dropdown answers it.
    """
    exc = _resolution_outcome(refusal)
    if exc is not None:
        return notification_reason(type(exc).__name__)

    from istota import storage  # noqa: PLC0415 - see `sync_user`

    if refusal == storage.VAULT_DIR_UNCHOSEN:
        return (
            "there are several vault files in your vault folder — choose which "
            "one Istota should read"
        )
    return ""


# ---------------------------------------------------------------------------
# The durable record
# ---------------------------------------------------------------------------

#: A reserved ``istota_kv`` namespace, so the ``kv`` skill refuses it on every
#: verb and the deferred-op replay refuses it again for a sandboxed task. The
#: precedent is ``_session_log_sweep`` and ``_avatar_import``: framework state
#: that happens to live in the KV store, written by the daemon and read by a
#: process that never saw the work.
#:
#: **Per-user ``istota_kv`` rather than ``shared_kv``**, unlike those two: a
#: vault belongs to one user and the table's key already carries a user id, so
#: a shared row would need the id folded into the key by hand.
VAULT_SYNC_STATE_NAMESPACE = "_vault_sync"
VAULT_SYNC_STATE_KEY = "last_sync"


def encode_sync_state(
    outcome: str,
    reason: str,
    *,
    now: str,
    previous: dict | None,
    unscoped: bool | None = None,
    generated_count: int | None = None,
    name_conflicts: int | None = None,
) -> str:
    """The row body for the cycle that just settled.

    ``ok_at`` is carried forward from ``previous`` on a failure, because §9's
    heading asks for the *last successful* sync and a failure must not erase the
    answer. It is the field a reader should present as "last synced"; ``at``
    moves on every settled cycle, failures included, and presenting *that* as
    the sync time would make a vault that has been broken for a week read as
    having synced a moment ago.

    ``unscoped`` is §1's answer for the surfaces that never open the file — the
    settings card runs ``vault_status(parse=False)`` and so cannot know it any
    other way. ``None`` means "this cycle did not read the file", and carries
    the previous answer forward for the same reason ``ok_at`` is carried: a
    failed cycle is not evidence the file grew a group. That carry is also what
    keeps the first-unscoped notification a *latch* rather than something a
    transient failure re-arms.

    An absent key on an older record reads as False, which is the right
    default: nothing was ever notified for it, so the first cycle after an
    upgrade raises the notice exactly once.

    ``reason`` is the ``NOTIFICATION_REASONS`` sentence rather than the
    exception's, for the reason that table gives: this row is read by the web
    tier and snapshotted by ``db_backup`` onto the mount.

    ``generated_count`` comes from the parsed group, not flattened names. A
    failed read carries the last proven count forward. ``name_conflicts`` is
    the apply's :data:`SKIP_NAME_TAKEN` count and is carried the same way; a
    record written before it existed reads as 0.
    """
    carried = (previous or {}).get("ok_at")
    if unscoped is None:
        unscoped = bool((previous or {}).get("unscoped"))
    if generated_count is None:
        prior_count = (previous or {}).get("generated_count")
        generated_count = prior_count if isinstance(prior_count, int) and prior_count >= 0 else 0
    if name_conflicts is None:
        name_conflicts = _count_field(previous, "name_conflicts")
    return json.dumps(
        {
            "at": now,
            "outcome": outcome,
            "reason": reason,
            "ok_at": now if outcome == OUTCOME_OK else (carried or None),
            "unscoped": bool(unscoped),
            "generated_count": generated_count,
            "name_conflicts": name_conflicts,
        },
        sort_keys=True,
    )


def _count_field(record: dict | None, key: str) -> int:
    """A non-negative count off a sync record, 0 when absent or malformed."""
    value = (record or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def decode_sync_state(raw: object) -> dict | None:
    """The last settled cycle's record, or ``None`` when there is not a usable one.

    ``None`` rather than a raise on anything unparseable. Every reader is a
    report — a settings endpoint, a CLI line, a notification resolver deciding
    whether to keep a row open — and a row nobody can read is, for all three,
    indistinguishable from no row at all.
    """
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def read_sync_state(conn, user_id: str) -> dict | None:
    """What the syncing process last settled for this user, on the caller's connection.

    **Takes a connection rather than a path**, which is the opposite of every
    other helper here, and deliberately: both readers already hold one. The
    notification resolver is handed the panel's connection, and opening a second
    one underneath it is the thirty-second busy-timeout hazard
    ``.claude/rules/notifications.md`` opens with.
    """
    from istota import db  # noqa: PLC0415 - see `sync_user` for the import rule

    try:
        return decode_sync_state(
            db.kv_get(conn, user_id, VAULT_SYNC_STATE_NAMESPACE, VAULT_SYNC_STATE_KEY)
        )
    except Exception:  # noqa: BLE001 - a report, never the work
        logger.warning("vault: could not read the sync record", exc_info=True)
        return None


#: A short lock budget for the record, rather than ``get_db``'s 30-second
#: default. The precedent is ``scheduler._record_session_log_sweep`` and
#: ``connected_service.close_for_service``, and the argument is theirs: losing a
#: record of the work is strictly cheaper than holding a writer for half a
#: minute. It matters here because the startup ``sync_all`` runs on the daemon's
#: boot path, once per configured user.
_RECORD_BUSY_TIMEOUT_MS = 2000


def _record_sync_state(
    db_path, user_id: str, outcome: str, reason: str, *,
    unscoped: bool | None = None, generated_count: int | None = None,
    name_conflicts: int | None = None,
) -> bool:
    """Write down what this cycle settled, for the processes that never see it.

    ``unscoped`` rides along for the surfaces that never open the file — the
    settings card runs ``vault_status(parse=False)`` and can learn it no other
    way. **It is state, not a latch**, and the distinction cost a review
    finding: an earlier shape returned "this is the transition into an unscoped
    read" from here and had :func:`_publish` raise the notice on it, which
    commits the latch *before* the notice is written. A notice write that then
    failed — and that path swallows every exception by contract — left the
    record saying the user had been told, for ever, about the one condition
    this feature exists to announce. The latch is the notification row itself
    now; see :func:`_report_unscoped`.

    ``_SYNC_STATE`` is per-process and stays that way — §7's cache is about
    whether the *next cycle in this process* does work, and persisting it would
    change the restart semantics that section argues for. This is the other
    question: what a process that never runs a sync can say about one. The web
    tier renders the settings heading and the notification panel, and under the
    Ansible shape it is a different unit from the scheduler entirely.

    **The read and the write are one transaction, and they have to be.**
    ``db.get_db`` opens in autocommit, so the ``SELECT`` in ``kv_get`` takes no
    lock and only ``kv_set``'s upsert opens a deferred write one — which leaves
    a window between them. Two settlers can interleave there (``istota secret
    vault-sync`` against a running daemon is the reachable pair, and two daemons
    the pathological one): A reads ``ok_at``, B records a success, A writes its
    failure record carrying the ``ok_at`` it read before B's. ``last_success_at``
    then goes *backwards* on the settings heading, which is the one field on it
    a user would act on. ``BEGIN IMMEDIATE`` takes the write lock before the
    read, which is what ``handle_whatsapp_batch`` does for the same shape.
    """
    from istota import db  # noqa: PLC0415

    try:
        with db.get_db(db_path, busy_timeout_ms=_RECORD_BUSY_TIMEOUT_MS) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = decode_sync_state(
                db.kv_get(
                    conn, user_id, VAULT_SYNC_STATE_NAMESPACE, VAULT_SYNC_STATE_KEY
                )
            )
            body = encode_sync_state(
                outcome,
                reason,
                now=db.iso_utc_now(),
                previous=previous,
                unscoped=unscoped,
                generated_count=generated_count,
                name_conflicts=name_conflicts,
            )
            db.kv_set(
                conn,
                user_id,
                VAULT_SYNC_STATE_NAMESPACE,
                VAULT_SYNC_STATE_KEY,
                body,
            )
        return True
    except Exception:  # noqa: BLE001 - a record of the work, not the work
        logger.warning(
            "vault: %s: the sync record was not written", _label(user_id),
            exc_info=True,
        )
        return False


#: ``user_id -> (digest, outcome)``. In memory, never persisted: a restart
#: re-applies, every write is an idempotent upsert, and the alternative is a
#: persistence question with no payoff.
#:
#: **Two fields, because retrying is not re-reporting.** The digest decides
#: whether the next cycle does any *work*, and is ``None`` for every outcome
#: outside ``_CACHEABLE_OUTCOMES`` — so a wrongful skip is structurally
#: impossible for those, rather than prevented by a condition somebody has to
#: keep getting right. The outcome decides whether the next cycle *says*
#: anything, which is what lets a vault retried every cycle for a week produce
#: one log line and one panel row rather than two thousand.
#:
#: A restart re-reports once, which is correct rather than merely tolerable: a
#: daemon that has just started has told nobody anything.
#:
#: **Unsynchronised, and that rests on there being one in-process caller at a
#: time rather than on anything here.** Today that holds: the startup
#: ``sync_all`` is synchronous and completes before the daemon loop starts, the
#: interval gate is spawned through ``_spawn_background_check``, whose in-flight
#: registry refuses a second ``vault-sync`` thread, and the CLI is a separate
#: process with a state of its own. It stops holding the moment a second
#: in-process caller appears — a web request thread calling ``sync_user``, say —
#: because ``_settle`` is a read-modify-write and, worse, two concurrent
#: ``apply_vault`` passes interleave writes with a delete plan computed before
#: them. A caller adding one takes a lock around ``sync_user`` rather than
#: relying on this. ``vault_status`` is safe to call concurrently: it only reads
#: this dict and settles nothing.
_SYNC_STATE: dict[str, tuple[str | None, str]] = {}


def reset_sync_state(user_id: str | None = None) -> None:
    """Forget what this process settled on, for one user or for all of them.

    ``istota secret vault-sync`` is the operator's escape hatch from every
    cache-shaped surprise §7 can still produce, so it must not be subject to the
    cache it exists to defeat — and §8's remedy strings name that command for
    exactly this reason. It lives here rather than in the CLI branch because a
    fresh CLI process has an empty cache anyway: the caller that needs the clear
    is an in-process one.

    Also the test seam. The state is module-level, so a leftover digest would
    make a later test's first cycle a skip.
    """
    if user_id is None:
        _SYNC_STATE.clear()
    else:
        _SYNC_STATE.pop(user_id, None)


@dataclass(frozen=True)
class VaultSyncResult:
    """What one user's cycle did, in the vocabulary every surface reports.

    ``outcome`` is one of the ``OUTCOME_*`` constants or a ``VaultError``
    subclass's name — a stable word rather than a sentence, because the CLI
    groups by it and §8's notification keys on it.

    ``transition`` is whether this outcome differs from the last one this process
    settled on for this user, which is what the raise-once rule reads. A skip is
    never a transition and never overwrites the state.

    ``last_outcome`` is what this process had already settled on, and it exists
    because **``OUTCOME_UNCHANGED`` is not evidence of health**. A skip says only
    that the bytes have not moved, and the digest of a file that failed to
    *parse* is cached — so a cycle over a still-corrupt vault answers
    ``unchanged`` exactly as a cycle over a working one does. A consumer reading
    that as success would close a notification about a vault that is still
    broken, which is the one thing §8 exists to prevent. On a skip this field
    carries the cached class; everywhere else it is what the outcome replaced.

    **It never carries a ``VaultRead``.** That type holds every plaintext the
    vault carries between parse and apply, and this object is returned to a CLI
    that prints it and, later, to a web tier that serialises it.
    """

    user_id: str
    outcome: str
    digest: str | None = None
    transition: bool = False
    reason: str = ""
    path: str = ""
    apply: VaultApplyResult | None = None
    last_outcome: str = ""
    #: §1: whether a top-level `istota` group narrowed the read. A bool off the
    #: `VaultRead` rather than the read itself, so the "never carries a
    #: VaultRead" rule above is untouched. False on a cycle that never parsed,
    #: which is why the surfaces that must distinguish the two read the durable
    #: record instead.
    unscoped: bool = False
    #: How many names the cycle read, for the notice's count. Names never.
    names: int = 0
    generated_count: int = 0
    name_conflicts: int = 0


@dataclass(frozen=True)
class VaultStatusReport:
    """What ``vault-status`` knows, as data rather than as printed lines.

    One answer, two renderers — the rule ``usage_render`` already states for a
    fact with a CLI and a web surface. The CLI formats this; §9's
    ``GET /settings/vault`` will serialise the same fields.

    **Names and counts only, never a value.** ``names`` is the namespace the
    parse produced, sorted, which is the feedback this feature has never had:
    after dropping a file in and generating a passphrase, the user can see
    which names arrived. ``skipped`` is why one they expected is not there.
    ``scoped`` is §1's answer — False means the file has no ``istota`` group
    and every credential in it is shared.

    Showing a user their own names is deliberately **not** the rule ``doctor``
    follows: ``security.vault_contents`` reports counts and no names, because a
    ``CheckResult`` is rendered into the boot log and the admin Health pane
    where one user's labels are read by every admin. This is the user's own
    terminal.
    """

    user_id: str
    configured: bool
    path: str = ""
    refusal: str = ""
    passphrase_present: bool = False
    outcome: str = ""
    reason: str = ""
    names: tuple[str, ...] = ()
    otp_count: int = 0
    generated_count: int = 0
    #: How many file entries the last applied cycle skipped because a
    #: credential added in Istota holds the name. From the durable record.
    name_conflicts: int = 0
    skipped: tuple[tuple[str, str], ...] = ()
    scoped: bool = True
    truncated: str = ""
    last_outcome: str = ""

    #: What the durable record says about the last cycle's scope, for the
    #: surface that never opens the file. `scoped` above is this call's own
    #: answer and is meaningless on the `parse=False` arm; this one survives
    #: the process that wrote it.
    recorded_unscoped: bool = False

    #: The durable record of what the *syncing* process last settled, which is
    #: a different question from the four fields above and is the only one a
    #: process that never runs a sync can answer. ``last_success_at`` is what
    #: §9's heading calls the last sync: ``last_sync_at`` moves on a failure
    #: too, so presenting that one would make a vault broken for a week read as
    #: having synced a moment ago.
    last_sync_at: str = ""
    last_success_at: str = ""
    recorded_outcome: str = ""
    recorded_reason: str = ""

    #: False when the report was built without opening the file. The four
    #: parse-only fields are then empty because nothing looked, which a renderer
    #: has to be able to tell from "looked and found nothing".
    parsed: bool = False


def label_for_display(name: str) -> str:
    """One name out of the vault file, bounded and flattened for a human.

    The public spelling of ``_label`` for a renderer outside this module. Every
    name a report prints came out of the vault file, and §12 records that a task
    in the user's own sandbox can overwrite it — so a name reaching an
    operator's terminal is attacker-reachable text and takes the same bound the
    log lines here take.
    """
    return _label(name)


def sync_is_scheduled(config) -> bool:
    """Whether anything in this deployment will run a vault cycle again.

    The interval half of ``scheduler.vault_sync_enabled``, lifted here so there
    is one spelling of the rule and one place its reasoning lives. It is here
    rather than there because the second reader is the notification resolver,
    which runs in the **web** process and may not pull ``scheduler`` in at module
    scope — while it already imports this module.

    ``> 0`` rather than truthiness: a negative interval is truthy, and
    ``_tick_interval_gates`` bypasses the clock for any non-positive one.

    What it is *for* on the resolver's side: ``vault_sync_interval = 0`` is a
    documented off-switch for the feature, and switching a feature off must not
    leave one of its warnings standing for ever with nothing able to close it.
    """
    return getattr(config.scheduler, "vault_sync_interval", 0) > 0


def _vault_is_enabled(config, user_id: str) -> bool:
    """Has this user turned a vault on at all — the cycle's first question.

    A vault is a file **and** a passphrase, and the passphrase is the half that
    cannot become true by accident: nothing can be read without it, so a
    ``.kdbx`` sitting in the folder of a user who has not provisioned one is
    not a configured vault and must not be reported as a broken one.

    **A configured ``vault_path`` counts too, and that is not the same
    question.** It is an operator's line rather than the user's own act, so a
    path with no passphrase behind it is a misconfiguration somebody has to be
    told about — ``VaultPassphraseMissing`` names the command that fixes it.
    Gating on the passphrase alone would make that state silent on every
    surface a user sees. It costs no directory read either: a configured path
    resolves without the folder being listed.

    Never raises. A database that cannot be opened answers False on the
    passphrase half, which is the safe direction for a background gate: the
    next cycle asks again, where the other way round is a resolve and a read on
    every user of a deployment whose database is gone.

    **Present-but-blank counts as configured**, deliberately, which is why the
    test is ``raw != ""`` rather than ``raw.strip()``:
    ``resolve_user_vault_path``'s own contract is that ``vault_path = "  "`` is
    "a configured value that resolves to nothing, which must not be silence",
    and it refuses it by name. A gate that stripped would sit upstream of that
    refusal and make it unreachable.
    """
    raw = config.vault_path_for(user_id)
    if isinstance(raw, str) and raw != "":
        return True
    return _passphrase_present(config, user_id)


def _passphrase_present(config, user_id: str) -> bool:
    """Does this user have a vault passphrase row. Presence, never a value.

    Presence rather than a successful decrypt: a row that will not decrypt is
    still a vault somebody configured, and the class that says so is
    ``VaultKeyUnusable`` from further down the cycle rather than silence here.
    """
    if config.db_path is None:
        return False
    try:
        return secrets_store.secret_exists(
            config.db_path, user_id, VAULT_PASSPHRASE_SERVICE, VAULT_PASSPHRASE_KEY
        )
    except Exception:  # noqa: BLE001 - a gate, never the work
        logger.warning(
            "vault: %s: could not check for a stored passphrase", _label(user_id)
        )
        return False


def _resolution_outcome(refusal: str | None) -> VaultError | None:
    """Which failure, if any, a resolution with no location is.

    Three answers from two new refusal ids, and collapsing any pair of them
    loses something:

    - ``VAULT_DIR_EMPTY`` is :class:`VaultMissing`. This user has a passphrase,
      so they have configured a vault and the file is not there — the existing
      class, its existing retry rule and its existing notification, because
      "the file went away" is worth saying.
    - ``VAULT_DIR_UNCHOSEN`` is **nothing at all**. Several files and none
      chosen is a question for the settings card, not a fault: reporting it as
      a failure would push a notification at a user whose answer is one
      dropdown away, every cycle until they answer it.
    - Any ``VAULT_PATH_*`` id is :class:`VaultPathRefused`, unchanged — a
      configured path the daemon may not open is an operator's to fix.
    """
    from istota import storage  # noqa: PLC0415 - see `sync_user`

    if refusal is None or refusal == storage.VAULT_DIR_UNCHOSEN:
        return None
    if refusal == storage.VAULT_DIR_EMPTY:
        return VaultMissing("no vault file in the vault folder")
    return VaultPathRefused(refusal)


def sync_user(
    config, user_id: str, *, force: bool = False, deliver: bool = True
) -> VaultSyncResult:
    """One user's vault, read and applied if the bytes have moved.

    The order is the design and each step earns its place ahead of the next:

    0. **Is a vault switched on at all?** :func:`_vault_is_enabled`, and it is
       ahead of the resolve because the resolve now lists a directory, which on
       the deployment shape this runs on is a FUSE mount. A user with neither a
       passphrase nor a configured path is skipped before any file is touched.
    1. **Resolve.** No file is not a failure; which of its two shapes *is* one
       is :func:`_resolution_outcome`.
    2. **Read the bytes and hash them.** Bounded and cheap, and it needs no
       library at all — ``pykeepass`` is not imported on a cycle that stops here.
    3. **Compare the digest.** An unchanged file stops the cycle: no key
       derivation, no database touch, no log line. This is ahead of the
       passphrase lookup because that lookup is a database read.
    4. **The master key, then the passphrase.** Both before the parse, so a
       deployment that cannot decrypt its own store spends no Argon2id.
    5. **Parse, then apply.**

    **Which failures cache their digest is the whole of §7**, and getting it
    wrong makes §8's remedies a lie rather than merely slow — see
    ``_CACHEABLE_OUTCOMES``.

    ``force`` drops this user's cached state **before reading anything**, which
    is what ``istota secret vault-sync`` passes. ``deliver`` is the row-versus-
    push fork for the same caller; see :func:`_report`.

    **It closes ``VaultLocation.dir_fd`` on every path**, including every
    failure path and the skip. A gate running per user per 300 seconds leaks one
    descriptor a cycle otherwise, which ends as a daemon that cannot open a
    socket, days later, with nothing pointing here.

    Raises nothing from the closed set of ``VaultError`` classes — each becomes
    an outcome. Anything else propagates to ``sync_all``, which contains it and
    reports it as a bug rather than as a vault condition.
    """
    # Function-scoped: `storage` imports `config`, and `config`'s own load-time
    # validator already function-scopes its import of *this* module to keep
    # `secret_schema` and `secrets_store` out of every `load_config`. A module
    # scope import here would hand that cost straight back.
    from istota import storage  # noqa: PLC0415

    if force:
        reset_sync_state(user_id)

    if not _vault_is_enabled(config, user_id):
        # The feature is off for this user, which is every user by default.
        # Not a state worth remembering, and not one to report a transition
        # out of.
        return VaultSyncResult(user_id=user_id, outcome=OUTCOME_NOT_CONFIGURED)

    refusal = vault_isolation_refusal(config, user_id)
    if refusal:
        return _publish(
            config,
            _settle(user_id, VaultIsolationRequired(refusal), digest=None, path=""),
            deliver=deliver,
        )

    resolution = storage.vault_location_for(config, user_id)
    if resolution.location is None:
        exc = _resolution_outcome(resolution.refusal)
        if exc is None:
            return VaultSyncResult(user_id=user_id, outcome=OUTCOME_NOT_CONFIGURED)
        return _publish(
            config,
            _settle(user_id, exc, digest=None, path=""),
            deliver=deliver,
        )

    location = resolution.location
    path = str(location.path)
    try:
        try:
            with _vault_file_lock(location, lock_root=config.db_path.parent, blocking=True):
                result = _sync_resolved(config, user_id, location, path)
        except TimeoutError:
            return VaultSyncResult(user_id=user_id, outcome=OUTCOME_BUSY, reason="vault lock busy")
        except VaultError as exc:
            result = _settle(user_id, exc, digest=None, path=path)
    finally:
        if location.dir_fd is not None:
            os.close(location.dir_fd)
    # Outside the lock: a mirror write takes it itself (ISSUE-686), and runs
    # whether or not the file moved, since a write that did not land is retried.
    mirror_pending(config, user_id)
    if deliver and result.apply is not None and result.apply.generated_notices:
        from istota.notifications.store import deliver_pending  # noqa: PLC0415
        try:
            deliver_pending(config, result.apply.generated_notices)
        except Exception:  # noqa: BLE001 - the row is written; only the push is lost
            logger.warning("vault: %s: divergence notice not pushed", _label(user_id))
    # Outside the `finally`, deliberately: `_publish` takes a write lock and may
    # push over the network, and the descriptor pins a directory on a FUSE mount.
    return _publish(config, result, deliver=deliver)


def _sync_resolved(config, user_id, location, path) -> VaultSyncResult:
    """Everything after the path resolved, with the descriptor still open."""
    try:
        # `dir_fd` beside `path`, never one without the other: the descriptor is
        # what covers every component above the leaf, which for the relative form
        # all live in the tree bound read-write into this user's own sandbox.
        # `tests/test_overlay_dir_containment.py` fails a call that drops it.
        data, digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
    except VaultError as exc:
        return _settle(user_id, exc, digest=None, path=path)

    cached_digest, settled = _SYNC_STATE.get(user_id, (None, ""))
    if cached_digest is not None and cached_digest == digest:
        # The whole point of `read_vault_bytes` and `parse_vault` being two
        # functions. A single `read_vault(path, passphrase)` would spend an
        # Argon2id unlock every cycle to learn that nothing had changed.
        return VaultSyncResult(
            user_id=user_id,
            outcome=OUTCOME_UNCHANGED,
            digest=digest,
            path=path,
            # The settled class travels with the skip. Without it `unchanged`
            # is indistinguishable from `unchanged and healthy`, and the only
            # cacheable failure is `VaultCorrupt` — so the shape that would be
            # misread is exactly a vault that is still broken.
            last_outcome=settled,
        )

    try:
        passphrase = _resolve_passphrase(config.db_path, user_id)
        read = parse_vault(data, passphrase)
    except VaultError as exc:
        return _settle(user_id, exc, digest=digest, path=path)

    applied = apply_vault(config.db_path, user_id, read)
    return _settle(
        user_id,
        None,
        digest=digest,
        path=path,
        applied=applied,
        unscoped=not read.scoped,
        names=len(read.services),
        generated_count=read.generated_count,
        name_conflicts=applied.name_conflicts,
    )


def _resolve_passphrase(db_path, user_id: str) -> str:
    """The stored passphrase, or the class that says why there is not one.

    ``get_secret`` answers ``None`` for three different conditions — no master
    key, a master key that will not decrypt this row, and no row — and they have
    two different remedies, so the ambiguity is resolved here rather than
    reported as one. ``secret_exists`` is what separates the last from the other
    two, exactly as ``import_from_user_configs`` uses it.
    """
    try:
        # The store's own validator, so the message matches the condition — the
        # precedent `apply_vault` and `doctor.security.secret_key` both follow.
        # The key itself is not bound.
        secrets_store._validated_key()
    except (
        secrets_store.SecretKeyMissingError,
        secrets_store.SecretKeyTooWeakError,
    ) as exc:
        # The store's own message is logged and does not become the `reason`.
        # It names the key's *length* on the too-weak arm, and `reason` is
        # rendered per user — into `vault-status` and, from Stage 5, into a
        # notification row — so a deployment-level fact about the master key
        # would be published on a per-user surface. §3's rule is about values
        # and this is adjacent to it rather than a breach of it; the daemon log
        # is the right place for the detail.
        logger.warning("vault: the master key is unusable: %s", exc)
        raise VaultKeyUnusable(
            "this deployment's ISTOTA_SECRET_KEY cannot read stored "
            "credentials; see the daemon log"
        ) from exc

    value = secrets_store.get_secret(
        db_path, user_id, VAULT_PASSPHRASE_SERVICE, VAULT_PASSPHRASE_KEY
    )
    if value is not None:
        # `is not None` rather than truthiness. An empty string cannot be
        # written through `upsert_secret` — `set_secret` reads it as a deletion —
        # but a hand-edited row can hold one, and passing it through gets
        # `VaultLocked` from the parse, which is the right remedy. Falling
        # through instead would report a wrong master key for a row the master
        # key had just decrypted perfectly.
        return value
    if secrets_store.secret_exists(
        db_path, user_id, VAULT_PASSPHRASE_SERVICE, VAULT_PASSPHRASE_KEY
    ):
        raise VaultKeyUnusable(
            "a vault passphrase is stored but will not decrypt; "
            "check ISTOTA_SECRET_KEY"
        )
    raise VaultPassphraseMissing(
        "no vault passphrase is provisioned for this user"
    )


def _report(
    config, user_id: str, outcome: str, *, transition: bool, deliver: bool
) -> None:
    """The notification half of a settled cycle: raise once, close on success.

    Three rules, and each is a decision rather than a consequence.

    **Raise on a transition only.** The dedup bump does not redeliver, so a
    second raise of an open row costs one UPDATE and no push — but it also
    refreshes the body, and re-raising every cycle would make the panel row's
    ``occurrences`` a count of cycles rather than of failures. A vault retried
    every 300 seconds for a week is one row and one push.

    **Close on every ``OUTCOME_OK``, not on a transition to it.** The
    in-memory state is empty after a restart, so a daemon that raised a row and
    then restarted has ``previous == ""`` on its next successful cycle — gated
    on a transition *out of a failure*, that row would stand open for ever. The
    close is an idempotent UPDATE matching nothing in the ordinary case, and
    ``OUTCOME_OK`` only happens on a cycle where the digest moved.

    **A skip reaches none of this**, because it never reaches ``_settle``. That
    is the whole of the ``OUTCOME_UNCHANGED`` hazard: ``VaultCorrupt`` caches
    its digest, so a cycle over a still-broken vault answers ``unchanged``
    exactly as a healthy one does, and a close keyed on anything a skip returns
    would close a warning about a vault that is still broken.

    ``deliver`` is the row-versus-push fork ``connected_service`` already draws
    for its own two callers. The daemon pushes; ``istota secret vault-sync``
    writes the row and pushes nothing, because the operator running it is
    looking at the failure on their own terminal, and because a push out of a
    short-lived CLI process means standing an ``AsyncRuntime`` up to deliver it.

    **Never raises, including out of a cancelled delivery.** The guards inside
    ``connected_service`` catch ``Exception``, and a push goes through
    ``run_coro``, whose ``future.result()`` raises ``CancelledError`` — a
    ``BaseException`` — when the loop is torn down mid-send. In a plain thread
    that means the delivery failed, not that this thread was cancelled, so it is
    caught here beside ``Exception``; ``KeyboardInterrupt`` and ``SystemExit``
    still propagate. Without this a daemon shutdown landing inside a vault
    cycle's notification would escape ``sync_all``'s own ``except Exception``.
    """
    import asyncio  # noqa: PLC0415 - for the exception type alone

    from istota.notifications.resolvers import connected_service  # noqa: PLC0415

    try:
        if outcome == OUTCOME_OK:
            connected_service.close_for_service(
                config.db_path, user_id, VAULT_NOTIFICATION_SERVICE, by="vault_sync",
            )
            return
        if not transition:
            return
        reason = notification_reason(outcome)
        if deliver:
            connected_service.raise_for_service(
                config, user_id, VAULT_NOTIFICATION_SERVICE, reason=reason,
            )
        else:
            connected_service.write_for_service(
                config.db_path, user_id, VAULT_NOTIFICATION_SERVICE, reason=reason,
            )
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - see above
        logger.warning(
            "vault: %s: the notification for %s was not written",
            _label(user_id), _label(outcome), exc_info=True,
        )


def _publish(config, result: VaultSyncResult, *, deliver: bool) -> VaultSyncResult:
    """The two surfaces outside this process, written once the file is let go.

    Split from :func:`_settle` so it runs **after** ``sync_user``'s ``finally``
    has closed ``VaultLocation.dir_fd``. Both legs can be slow — the record
    takes a write lock, and the notification fans out to Talk and ntfy over
    ``run_coro`` — and holding a descriptor pinned to a directory on a
    ``fuse.rclone`` mount across an outbound HTTP call is a hold measured in
    seconds where it used to be microseconds.

    Each leg is guarded on its own, so losing either costs that surface and not
    the cycle: by the time this runs the outcome is settled and the result is
    already built.

    Returns ``result`` so a caller can tail-call it.
    """
    if result.outcome in (OUTCOME_NOT_CONFIGURED, OUTCOME_UNCHANGED):
        # A skip settles nothing and says nothing — §7's cycle costs no database
        # touch, and the feature being off is not a state to record.
        return result
    # The published sentence, never `result.reason` — see `NOTIFICATION_REASONS`.
    #
    # `unscoped=None` on a cycle that never opened the file, which carries the
    # previous answer forward: a failed read is not evidence the file grew an
    # `istota` group, and clearing it here would re-arm the one-shot notice for
    # the next successful cycle.
    _record_sync_state(
        config.db_path,
        result.user_id,
        result.outcome,
        "" if result.outcome == OUTCOME_OK else notification_reason(result.outcome),
        unscoped=result.unscoped if result.outcome == OUTCOME_OK else None,
        generated_count=result.generated_count if result.outcome == OUTCOME_OK else None,
        name_conflicts=result.name_conflicts if result.outcome == OUTCOME_OK else None,
    )
    _report(
        config,
        result.user_id,
        result.outcome,
        transition=result.transition,
        deliver=deliver,
    )
    if result.unscoped and result.outcome == OUTCOME_OK:
        # Unconditional on every unscoped cycle, and the once-ness is
        # `_report_unscoped`'s own: it is the thing that has to observe its
        # write succeed before anything records that the user was told.
        _report_unscoped(config, result.user_id, result.names, deliver=deliver)
    return result


#: The one-shot notice §1 asks for, on `task_alert` rather than on
#: `connected_service`. Three reasons, and the first is the one that decides it:
#: `connected_service`'s dedup key is the service name, so a vault notice
#: written there would collide with the sync-failure row the same feature
#: already raises and each would bump the other. Second, §10 says that arm is
#: unchanged. Third, this is exactly `task_alert`'s shape — a notice with no
#: object, nothing that will ever close it, and `auto_resolve_on_seen` doing
#: the closing when the user reads it.
_UNSCOPED_DEDUP_KEY = "vault-unscoped"
_UNSCOPED_TITLE = "Your credential vault shares its whole file"


def _unscoped_body(names: int) -> str:
    """What the notice says. A count, never a name and never a value.

    **No backticks**, and not as a style choice: ``task_alert.write`` puts every
    body through ``flatten_body``, whose ``_BODY_MARKUP_CHARS`` maps ``` ` ``` to
    a space — so a quoted name arrives as ``named  istota .`` The group name is
    the one word this sentence needs a reader to copy exactly, so it is quoted
    with characters that survive.
    """
    return (
        f'The KDBX file in your vault folder has no top-level group named '
        f'"istota", so every credential in it is shared with your own tasks — '
        f"{names} so far. That is how it is meant to work if you put a file "
        f"there for istota. If you copied in your everyday password database, "
        f"move it out, or put the credentials you meant to share under a "
        f'top-level group named "istota".'
    )


def _report_unscoped(config, user_id: str, names: int, *, deliver: bool) -> None:
    """Raise the unscoped-read notice for this user, once ever.

    **The notification row is the latch, and that is the correction rather than
    the obvious shape.** The obvious shape reads the durable sync record for a
    False-to-True transition and raises on it — which commits "this user has
    been told" *before* anything writes the telling. This function swallows
    every exception by contract, so a write that failed there left the record
    latched for good and the user never learned that their whole password
    database was shared. Asking the notifications table instead makes the two
    the same fact: a row exists only because a write succeeded, so a failure
    leaves no row and the next cycle tries again.

    It asks for a row in **any** state rather than an open one.
    ``task_alert`` is ``auto_resolve_on_seen``, so the row closes the moment
    the user opens the panel with it visible — an open-row test would re-raise
    on the next cycle after they read it, which is a notice per sync interval
    for as long as the file stays that way.

    The check and the write share one connection, so nothing can interleave
    between them that this function would then act on.

    ``deliver`` is the row-versus-push fork :func:`_report` already draws, for
    the same reason: ``istota secret vault-sync`` writes the row and pushes
    nothing, because the operator running it is reading the warning off their
    own terminal as it prints. That does consume the latch, so a vault first
    synced by hand reaches the bell and not a push; accepted, and the same
    property ``connected_service``'s fork has.

    Never raises, and never carries a name or a value.
    """
    import asyncio  # noqa: PLC0415 - for the exception type alone

    from istota import db  # noqa: PLC0415
    from istota.notifications.resolvers import task_alert  # noqa: PLC0415
    from istota.notifications.store import deliver_pending  # noqa: PLC0415

    try:
        with db.get_db(config.db_path) as conn:
            told = conn.execute(
                "SELECT 1 FROM notifications "
                "WHERE user_id = ? AND source = ? AND dedup_key = ? LIMIT 1",
                (user_id, task_alert.SOURCE, _UNSCOPED_DEDUP_KEY),
            ).fetchone()
            if told is not None:
                return
            raised = task_alert.write(
                conn,
                user_id,
                dedup_key=_UNSCOPED_DEDUP_KEY,
                title=_UNSCOPED_TITLE,
                body=_unscoped_body(names),
                params={"task_id": None, "status": "vault_unscoped"},
            )
        if deliver and raised is not None:
            deliver_pending(config, [raised])
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - see `_report`
        logger.warning(
            "vault: %s: the unscoped-read notice was not written",
            _label(user_id),
            exc_info=True,
        )


def _settle(
    user_id: str,
    exc: VaultError | None,
    *,
    digest: str | None,
    path: str,
    applied: VaultApplyResult | None = None,
    unscoped: bool = False,
    names: int = 0,
    generated_count: int = 0,
    name_conflicts: int = 0,
) -> VaultSyncResult:
    """Record the outcome, decide whether it is a transition, and say so once.

    The digest is stored **only** for a cacheable outcome, so the skip test one
    call up is a plain equality rather than a second condition that has to agree
    with ``_CACHEABLE_OUTCOMES``.

    **It settles and says; it does not publish.** The durable record and the
    notification are :func:`_publish`'s, and they are deliberately not here: both
    are called with the vault's directory descriptor still open if they are, and
    one of them can spend as long as a Talk post takes. What stays is the
    in-memory state (free, and the transition rule depends on it) and the log.
    """
    outcome = OUTCOME_OK if exc is None else type(exc).__name__
    reason = "" if exc is None else str(exc)
    _, previous = _SYNC_STATE.get(user_id, (None, ""))
    transition = outcome != previous
    _SYNC_STATE[user_id] = (
        digest if outcome in _CACHEABLE_OUTCOMES else None,
        outcome,
    )

    if transition and exc is not None:
        # One WARNING per transition *into* a failing state, carrying the user,
        # the resolved path and the mapped class — never the passphrase and
        # never a value. Both interpolated strings are bounded, since the path
        # comes out of `config.toml` and can carry a newline.
        logger.warning(
            "vault: %s: %s (path=%s): %s",
            _label(user_id),
            outcome,
            _label(path, _PATH_LABEL_MAX_CHARS) if path else "unresolved",
            _label(reason, _PATH_LABEL_MAX_CHARS),
        )
    elif transition and previous:
        logger.info("vault: %s: recovered from %s", _label(user_id), previous)

    if applied is not None and (applied.created or applied.updated or applied.deleted):
        logger.info(
            "vault: %s: %d written, %d unchanged, %d deleted",
            _label(user_id),
            applied.created + applied.updated,
            applied.unchanged,
            applied.deleted,
        )

    return VaultSyncResult(
        user_id=user_id,
        outcome=outcome,
        digest=digest,
        transition=transition,
        reason=reason,
        path=path,
        apply=applied,
        last_outcome=previous,
        unscoped=unscoped,
        names=names,
        generated_count=generated_count,
        name_conflicts=name_conflicts,
    )


def sync_all(
    config,
    *,
    users: list[str] | None = None,
    force: bool = False,
    deliver: bool = True,
) -> list[VaultSyncResult]:
    """Every configured user's vault, one at a time, containing each failure.

    One user's vault is not another's, and a raise out of one must not cost the
    rest — the gate that calls this runs unattended every 300 seconds, so the
    alternative is a single malformed config silently stopping the feature for
    the whole deployment.

    Anything outside the ``VaultError`` closed set is a bug rather than a vault
    condition, so it is logged with a stack and reported as ``OUTCOME_ERROR``
    rather than folded into an error class a notification would then explain
    wrongly. **That arm deliberately reaches neither the sync record nor a
    notification**: it never enters ``_settle``, so nothing is settled, nothing
    is published, and the next cycle transitions from whatever the last real
    outcome was. A raise here is a defect in this module, and a defect must not
    be published to a user as a statement about their vault file — the stack in
    the daemon log is the right and only surface for it.
    """
    wanted = list(config.users) if users is None else users
    results: list[VaultSyncResult] = []
    for user_id in wanted:
        try:
            results.append(
                sync_user(config, user_id, force=force, deliver=deliver)
            )
        except Exception:  # noqa: BLE001 - a background gate, one user of many
            logger.exception("vault: %s: sync failed unexpectedly", _label(user_id))
            results.append(
                VaultSyncResult(
                    user_id=user_id,
                    outcome=OUTCOME_ERROR,
                    reason="an unexpected error; see the daemon log",
                )
            )
    return results


def vault_status(
    config, user_id: str, *, parse: bool = True
) -> VaultStatusReport:
    """What this user's vault looks like right now, without applying anything.

    **It applies nothing** — no credential row is written, updated or deleted —
    which is what makes it the verb an operator can run while wondering whether
    to trust the file. It is not quite "writes nothing", and the exception is
    worth stating rather than glossing: resolving the passphrase goes through
    ``secrets_store.get_secret``, which stamps ``last_accessed_at`` on the
    ``vault/passphrase`` row on every successful decrypt. That is one row, it is
    the vault's own, and it is honest — the passphrase really was used — but it
    is the same column ``apply_vault``'s docstring already warns is not evidence
    a shared credential is read by anything.

    It also touches no sync state in either direction: it is not subject to the
    digest cache — the operator is asking *now* — and it does not settle an
    outcome, since an outcome nobody applied would suppress the next real
    cycle's report.

    Parsing is what earns the command its keep: it is the only way to see the
    names the file produces, the skips that say why one is missing, and whether
    the read was scoped (§1). A name in the list is a credential istota holds;
    one the user expected and cannot see is an entry with a skip beside it.

    **``parse=False`` is §9's endpoint, and it is not an optimisation.** An
    Argon2id unlock is tuned to about a second, and a settings page that spent
    one per load would spend it inside a FastAPI handler — where, on the event
    loop, it stalls every other request in the web process, and where a wedged
    ``fuse.rclone`` mount stalls them indefinitely. The endpoint does not need
    the parse either: §8's card asks for the path, the last successful sync and
    the failing class, and every one of those is either config, a resolve, or
    the durable record below — including the unscoped verdict, which is why it
    is written into that record in the first place. So this stays one function
    returning one shape — ``usage_render``'s rule for a fact with a CLI and a web
    surface — with the parse-only fields empty and ``parsed`` False to say
    so. What ``parse=False`` still does is *resolve*, because a refused path is
    the one failing state that is true of the configuration rather than of a
    past cycle, and reporting a stale ``VaultPathRefused`` — or missing a fresh
    one — would be wrong in both directions.

    The **durable record** is read on both arms. It is what makes this verb
    answer at all in a process that has never run a sync: the web process under
    the Ansible shape is a different unit from the scheduler, and a CLI
    invocation is a different process again, so ``last_outcome`` is empty for
    both and always has been.
    """
    from istota import db  # noqa: PLC0415 - see `sync_user`
    from istota import storage  # noqa: PLC0415

    last = _SYNC_STATE.get(user_id, (None, ""))[1]
    # `configured` is the enable itself — `_vault_is_enabled`, the predicate
    # the cycle gates on — rather than a second opinion beside it. A file in
    # the folder with no passphrase is deliberately *not* configured: nothing
    # can open it, the cycle skips that user, and a report saying otherwise
    # would have the card claim a read that never happens. Read off
    # `vault_path_for` alone it would answer False for every folder user, and
    # the card would have no sync record to render.
    present = _passphrase_present(config, user_id)
    refusal = vault_isolation_refusal(config, user_id)
    if refusal:
        return VaultStatusReport(
            user_id=user_id, configured=_vault_is_enabled(config, user_id),
            passphrase_present=present, outcome=VaultIsolationRequired.__name__,
            reason=refusal,
        )
    if not _vault_is_enabled(config, user_id):
        return VaultStatusReport(
            user_id=user_id, configured=False, passphrase_present=present,
        )
    resolution = storage.vault_location_for(config, user_id)

    recorded: dict | None = None
    try:
        with db.get_db(config.db_path) as conn:
            recorded = read_sync_state(conn, user_id)
    except Exception:  # noqa: BLE001 - a report, never the work
        logger.warning("vault: could not open the database for the sync record")
    recorded = recorded or {}
    count_value = recorded.get("generated_count")
    stored_generated_count = (
        count_value if isinstance(count_value, int) and count_value >= 0 else 0
    )

    def _with_record(report: VaultStatusReport) -> VaultStatusReport:
        return dataclasses.replace(
            report,
            last_sync_at=str(recorded.get("at") or ""),
            last_success_at=str(recorded.get("ok_at") or ""),
            recorded_outcome=str(recorded.get("outcome") or ""),
            recorded_reason=str(recorded.get("reason") or ""),
            recorded_unscoped=bool(recorded.get("unscoped")),
            generated_count=stored_generated_count,
            name_conflicts=_count_field(recorded, "name_conflicts"),
        )

    if resolution.location is None:
        # The two folder ids reach here beside the `VAULT_PATH_*` ones, and
        # they do not all name a failure: an unchosen folder has a sentence for
        # the card and no outcome at all, because nothing about it is broken.
        exc = _resolution_outcome(resolution.refusal)
        return _with_record(
            VaultStatusReport(
                user_id=user_id,
                configured=True,
                refusal=resolution.refusal or "",
                passphrase_present=present,
                outcome=type(exc).__name__ if exc is not None else "",
                reason=resolution_reason(resolution.refusal),
                last_outcome=last,
            )
        )

    location = resolution.location
    try:
        report = _with_record(
            VaultStatusReport(
                user_id=user_id,
                configured=True,
                path=str(location.path),
                passphrase_present=present,
                last_outcome=last,
            )
        )
        if not parse:
            # The resolve was the point; nothing below it is answerable without
            # opening the file. `outcome` stays empty rather than being filled
            # in from the record, because the two say different things — one is
            # what this call found, the other is what a past cycle settled — and
            # a renderer that could not tell them apart would report a week-old
            # failure as the current state of a file it never looked at.
            return report
        try:
            data, _digest = read_vault_bytes(
                location.path, dir_fd=location.dir_fd
            )
            passphrase = _resolve_passphrase(config.db_path, user_id)
            read = parse_vault(data, passphrase)
        except VaultError as exc:
            return dataclasses.replace(
                report, outcome=type(exc).__name__, reason=str(exc), parsed=True
            )
    finally:
        if location.dir_fd is not None:
            os.close(location.dir_fd)

    # Names and the reasons a name is missing — never a value. `read.services`
    # holds every plaintext this function ever sees and only its keys leave
    # here; `VaultRead.__repr__` states the same rule one object over.
    #
    # `held` names are deliberately absent from `names`: the file produced them
    # and could not supply a value, so istota does not hold a credential under
    # them from this read. They are what the apply declines to delete rather
    # than something to report as present.
    out = dataclasses.replace(
        report,
        outcome=OUTCOME_OK,
        parsed=True,
        names=tuple(sorted(read.services)),
        otp_count=sum(read.bindings[name].get("kind") == "totp" for name in read.services),
        generated_count=read.generated_count,
        skipped=read.skipped,
        scoped=read.scoped,
        truncated=read.truncated,
    )
    # Lifetime, and kept from the shape this replaced: `read` is the only
    # object in this process holding the user's whole decrypted namespace, and
    # a local outlives its last use — into a traceback a caller renders, and
    # into whatever a debugger or a profiler is holding. Dropping it on the
    # line the names are taken is the narrowest window this function can give
    # it.
    #
    # It briefly had a second justification — `doctor`'s leak check went red
    # without it — and that was a measurement of the check rather than of this
    # function. The parse allocates tens of thousands of objects, so where the
    # interpreter's generational threshold fell decided whether a few
    # already-finished sqlite and lxml handles were still listed in `/dev/fd`
    # when the check sampled. The check collects before sampling now, so this
    # `del` stands on the reason above alone.
    del read
    return out
