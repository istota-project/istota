"""A user's KeePass credential vault: the bytes, what they mean, and what they own.

A user who keeps their credentials in a password manager otherwise maintains two
copies of every key, and the copy istota reads is the one they cannot see,
search or back up. This module is the answer's provisioning half: a KDBX file in
the user's own workspace that istota decrypts and never writes, and the pass
that copies what it holds into the encrypted ``secrets`` table.

**It is provisioning input, not a storage backend.** ``resolve_secret``'s order
is unchanged, the table stays the live store, and a vault that is missing,
half-synced or locked leaves every credential working. What the pass adds is the
one direction the table cannot express on its own: a service the vault owns is
the file's to say, deletions included — which is why the applying half is where
the destructive rules live and why each of them is stated below rather than
inferred.

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

**And it never logs a value, at any level.** Counts, service names and key names
are loggable and are what the warnings below carry. The rule is easy to defeat
by accident, which is why ``VaultRead`` carries a ``__repr__`` of its own: that
dataclass holds every plaintext the vault carries between parse and apply, and
pytest's assertion rewriting prints the repr of whatever a failing comparison
touched, so nobody has to write a logging call to leak it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import secret_schema, secrets_store

logger = logging.getLogger(__name__)

#: The size above which the file is refused unread. A vault is a few kilobytes
#: of credentials; 8 MiB is slack for attachments the user kept in the same
#: database and a bound on what a planted file can make the daemon read.
VAULT_READ_CAP_BYTES = 8 * 1024 * 1024

#: The one top-level group the reader looks at, matched **exactly**. Only the
#: service segment below it folds (see ``_map_groups``).
VAULT_ROOT_GROUP = "istota"

#: How much of a group or key name a log line may carry (see ``_label``).
_LABEL_MAX_CHARS = 64

#: The service the vault's own passphrase is stored under. Subtracted from
#: eligibility: the passphrase cannot live in the file it unlocks.
VAULT_PASSPHRASE_SERVICE = "vault"

#: Services with declared writable keys that the daemon rewrites on its own —
#: Monarch's cookie pair from ``/api/money/monarch/login``, the Overland ingest
#: token from its generate endpoint (which also reloads a cache an importer
#: would have to reproduce), and the two machine-managed OAuth blobs. Vault
#: ownership of any of them means a window in which a freshly minted credential
#: is reverted, so they are subtracted whatever the schema says. Two of the four
#: already fall out for having no writable keys; they are named anyway, because
#: the reason they must never be vault-owned is this one and not that one.
DAEMON_WRITTEN_SERVICES = frozenset(
    {"monarch", "overland", "garmin", "google_workspace"}
)

#: Reserved service namespace: ``connector:<id>`` rows have exactly one writer,
#: the connector route that validated the key against the resolved provider
#: manifest. Nothing in the tree declares one yet — the prefix is checked here so
#: the refusal is this module's own rather than a side effect of the schema walk
#: finding nothing. ``Specs/Drafts/generic-connectors.md`` owns the namespace and
#: will bring ``secret_schema.is_reserved_service()`` with it; read it from there
#: when it lands rather than keeping this spelling.
_RESERVED_SERVICE_PREFIX = "connector:"

#: The fixed vocabulary ``VaultApplyResult.skipped`` reports with. The service
#: and key ride in their own slots of the triple, so a reason never interpolates
#: either: these reach ``vault-status`` output, where a caller wants to group by
#: reason rather than parse a sentence.
SKIP_RESERVED_SERVICE = "reserved service namespace"
SKIP_INELIGIBLE_SERVICE = "not a vault-eligible service"
SKIP_UNKNOWN_KEY = "not a key this service declares"
SKIP_UNREADABLE_ROW = "stored value will not decrypt, so it is not deleted"
SKIP_DELETE_HELD = "a near-miss entry title held this deletion back"


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
    """The stored passphrase does not open the file.

    Distinct from ``VaultCorrupt`` because the remedy is a re-provision and
    touches no byte of the vault — so a cycle that cached this digest would make
    that remedy inert.
    """


class VaultLibraryMissing(VaultError):
    """The ``vault`` extra is not installed on this host. An operator remedy."""


@dataclass(frozen=True)
class VaultRead:
    """One parsed vault.

    ``services`` is the plaintext the file holds, folded service name to key to
    value, for **every** group found rather than only the owned ones — which
    service a vault may own is the applying half's question, and ``vault-status``
    needs the rest to tell a user their group name matches nothing.

    ``group_present`` is the same set of names mapped to the spelling as it
    stands in the file. The folded key is what ``vault_services`` is compared
    against; the value is what gets printed back, so a user whose group is
    ``Karakeep`` is shown the name they typed and told it matched, instead of
    being shown one they never wrote. It is also what the apply step's
    "the vault has no opinion about a service it does not mention" rule reads,
    so a group holding no entries is present here with an empty bucket beside
    it rather than dropped.

    ``entry_titles`` is every title found in a group **whether or not a value
    was taken from it**, and it exists because the applying half's deletion rule
    is about the group's contents rather than about the values the mapping
    kept. §6 deletes a schema key "the group does not contain", and a group
    contains an entry whose password field is empty or whose title is
    duplicated — both of which are skips that leave nothing in ``services``.
    Computing the deletion set from ``services`` would therefore make §3's "an
    empty password is skipped, not treated as a deletion" false in exactly the
    fat-finger case it names, and would turn the duplicate-title hard skip into
    a credential deletion. Required rather than defaulted for the same reason:
    a hand-built ``VaultRead`` that omitted it would get the destructive
    reading silently.
    """

    digest: str
    services: dict[str, dict[str, str]]
    group_present: dict[str, str]
    entry_titles: dict[str, frozenset[str]]

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

        Key names are kept, because they are the thing a reader needs and are
        loggable by the same rule the warnings below follow.

        **It closes the route through this object and not the field itself.**
        ``services`` is public, so ``assert read.services == {...}`` compares two
        plain dicts and prints both, and a caller is free to log the attribute
        directly. What this covers is the case nobody writes on purpose: the
        object reaching a repr by way of a container, a failing comparison or a
        debug line about the read as a whole.
        """
        keys = {service: sorted(values) for service, values in self.services.items()}
        titles = {
            service: sorted(names) for service, names in self.entry_titles.items()
        }
        return (
            f"VaultRead(digest={self.digest!r}, "
            f"group_present={self.group_present!r}, keys={keys!r}, "
            f"entry_titles={titles!r})"
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
    from .skills._loader import OVERLAY_UNREADABLE, read_overlay_bytes

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
        raise VaultLocked("the stored passphrase does not open this vault") from exc
    except (HeaderChecksumError, PayloadChecksumError) as exc:
        raise VaultCorrupt("the file is not a readable KeePass database") from exc
    except Exception as exc:
        logger.warning(
            "vault: parse failed (%s.%s)",
            type(exc).__module__,
            type(exc).__name__,
        )
        raise VaultCorrupt("the file is not a readable KeePass database") from None


def eligible_services() -> frozenset[str]:
    """Services a vault may own: declared writable keys, minus the exclusions.

    Computed from ``secret_schema`` rather than hand-listed, so a service added
    there is classified by these rules instead of by somebody remembering this
    module exists. Three exclusions, each for its own reason:

    - **No writable keys.** ``"fields": []`` means the credential is a
      machine-managed blob (``google_workspace``, ``garmin``); there is nothing
      a file could express.
    - **``DAEMON_WRITTEN_SERVICES``.** The daemon rewrites these rows itself, so
      vault ownership means a window in which a freshly minted credential is
      reverted — the one class of writer §6's "no other writer is left" argument
      could never close.
    - **The reserved ``connector:`` namespace and ``vault`` itself.**

    **Eligibility is not enablement, and this deliberately does not consult the
    modules.** Two of the names it yields belong to modules (``feeds`` and
    ``carto``), and a deployment with those modules off is still offered them.
    Writing the row anyway is right: the secrets table is not module-scoped,
    ``istota secret ensure`` does not consult enablement either, and a
    credential that is present and unread costs nothing while one silently
    refused because a module was off is a failure with no surface.
    """
    return frozenset(
        service
        for service, keys in secret_schema.known_service_keys().items()
        if keys
        and service not in DAEMON_WRITTEN_SERVICES
        and service != VAULT_PASSPHRASE_SERVICE
        and not service.startswith(_RESERVED_SERVICE_PREFIX)
    )


def service_refusal(
    service: str, eligible: frozenset[str] | None = None
) -> str | None:
    """Why this service may not be vault-owned, or None.

    The same predicate ``apply_vault`` applies to every entry of ``owned``, for
    the caller outside this module — ``config``'s load-time filter, which drops
    an ineligible ``vault_services`` entry with a warning naming the service and
    the reason.

    **Delegation rather than a second copy**, and the distinction matters: the
    load-time filter is what tells the operator a line is inert, and if the two
    could disagree the operator would be told a line was live and then have it
    refused hours later inside a background gate. Same reason constants, so
    ``vault-status`` and the boot log say one word about one condition.

    ``eligible`` is the set to test against, computed when it is not given. A
    caller asking about several names in a loop passes it, because
    ``eligible_services`` is a schema walk and building it per name is the cost
    ``apply_vault`` hoists it out of a loop to avoid. Deliberately not memoized:
    the schema is monkeypatched in tests, and a cache would make an exclusion
    pinned there vacuously true on the second call.
    """
    return _service_refusal(
        service, eligible_services() if eligible is None else eligible
    )


@dataclass
class VaultApplyResult:
    """What one apply did, in the vocabulary the CLI and the panel report.

    ``deleted_keys`` names what went rather than counting it, because the
    deletion rule's documented surprise (§6) is a first sync removing keys the
    operator did not realise the vault would own — a bare count tells them four
    credentials are gone and leaves them to work out which four.

    ``skipped`` is ``(service, key, reason)``. A **service-level** refusal
    carries an empty key: the whole service was refused and no key of it was
    looked at. Reasons come from the fixed vocabulary above, never a sentence
    built around a name.
    """

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    deleted_keys: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)


def apply_vault(
    db_path: Path,
    user_id: str,
    read: VaultRead,
    owned: frozenset[str],
) -> VaultApplyResult:
    """Write ``read``'s owned services into the secrets table, and delete out of it.

    Precedence is **vault-wins** for a service the operator has named, which
    inverts ``secrets_store.import_from_user_configs`` — that one never
    overwrites, because TOML extras are a legacy source being drained, and this
    one is the live authority for its declared services or editing the file
    would change nothing.

    Three rules, and the first is the guard against the catastrophic case:

    1. A service **not** in ``read.group_present`` is left alone entirely. The
       vault has no opinion about a service it does not mention, so a file that
       parses and has lost its contents — an older copy put back by a resync, a
       group deleted by mistake — is silence rather than a wipe.
    2. Each key in the group that the schema declares is upserted, counted by
       the state ``upsert_secret`` returns.
    3. A schema key the group does not contain, and the table has, is deleted.
       "Contain" is read against ``VaultRead.entry_titles`` rather than against
       the values, so an entry whose password is empty or whose title is
       duplicated holds its row — see that field's own note.

    **Two things hold a deletion back, and both are about a delete being the
    one action that cannot be undone by fixing the cause.** A row that is
    present and will not decrypt is never deleted: that is a stale master key
    rather than a credential the user retired, and the credential comes back
    when the right key does but not from a delete. And a deletion whose key
    differs from a title refused in the same group only by case or surrounding
    whitespace is held, because the alternative is refusing the new value and
    destroying the old one over one trailing space — a combination §6 licenses
    only by not having considered it. Both are reported in ``skipped``.

    **Every write happens before any delete**, across all services rather than
    within each, so a failure part-way through leaves credentials present rather
    than absent. That ordering is the reason the deletions are planned into a
    list and executed at the end instead of inline.

    **A missing master key refuses the whole pass**, in the store's own
    vocabulary. Every other path raises on its first ``set_secret`` anyway; the
    one that does not is a present group holding nothing, where the pass would
    delete every row of a service on a deployment that can neither read what it
    is removing nor write a replacement. That guard tests presence and a length
    floor, which is all ``secret_key_available`` can see, so a key that is
    *wrong* passes it — the per-row readability test above is what covers that,
    and neither substitutes for the other: an absent key makes every row
    unreadable and is a deployment fault rather than a per-row one.

    **It bumps ``last_accessed_at`` on every owned credential it reaches**, and
    that is a cost to the column rather than to this pass: ``upsert_secret``
    compares against ``get_secret``, the readability test above is another read,
    and §7 parses on every *save* of the file rather than every change — so a
    user who opens the vault and saves out of habit marks their whole owned set
    as used. Nothing here can avoid it, since vault-wins needs the comparison
    the existing importer avoids by never overwriting; what it means is that
    ``last_accessed_at`` is not evidence a vault-owned credential is read by
    anything.

    ``owned`` is matched exactly. §3 folds the *service segment of the file*
    and nothing else, and ``vault_services`` is operator config rather than
    something a phone keyboard touched — a name that does not match is refused
    here and warned about at config load.
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

    result = VaultApplyResult()
    eligible = eligible_services()
    schema = secret_schema.known_service_keys()
    pending_deletes: list[tuple[str, str]] = []

    for service in sorted(owned):
        reason = _service_refusal(service, eligible)
        if reason is not None:
            result.skipped.append((service, "", reason))
            logger.warning(
                "vault: %s is not a service a vault may own (%s), skipped",
                _label(service),
                reason,
            )
            continue
        if service not in read.group_present:
            continue

        declared = schema[service]
        values = read.services.get(service, {})
        written: set[str] = set()
        for key in sorted(values):
            if key not in declared:
                result.skipped.append((service, key, SKIP_UNKNOWN_KEY))
                logger.warning(
                    "vault: %s/%s is not a key this service declares, skipped",
                    _label(service),
                    _label(key),
                )
                continue
            state = secrets_store.upsert_secret(
                db_path, user_id, service, key, values[key]
            )
            written.add(key)
            if state == "created":
                result.created += 1
            elif state == "updated":
                result.updated += 1
            elif state == "noop":
                result.unchanged += 1
            else:  # pragma: no cover - the store's contract is three literals
                raise ValueError(f"unexpected upsert state {state!r}")

        # Indexed, not `.get(..., frozenset())`: an absent entry reads as "the
        # group contains nothing" and plans a delete for every declared key, so
        # the convenient default is the destructive one — the same reason the
        # field itself carries no default. `_map_groups` fills it for every
        # group it reports present, so a KeyError here is a caller that built a
        # `VaultRead` by hand and got it wrong.
        titles = read.entry_titles[service]
        # `written` is subtracted so the pass can never write a key and delete
        # it in the same call. It follows from `values <= titles` on every
        # parser path, and this keeps it a property of `apply_vault` rather than
        # one inherited from a collaborator.
        for key in sorted(declared - titles - written):
            if not secrets_store.secret_exists(db_path, user_id, service, key):
                continue
            if secrets_store.get_secret(db_path, user_id, service, key) is None:
                # Present and undecryptable: a stale master key, a half-loaded
                # `secrets.env`, a rotation applied to the wrong host. The
                # process-level guard above cannot see this — `_MIN_KEY_LEN` and
                # presence are all it tests — so without this arm a wrong key
                # turns a transient misconfiguration into permanent loss: the
                # credential comes back when the right key does, and does not
                # come back from a delete. Never destroy what cannot be read.
                result.skipped.append((service, key, SKIP_UNREADABLE_ROW))
                logger.warning(
                    "vault: %s/%s is stored but will not decrypt, so it is not "
                    "deleted; check ISTOTA_SECRET_KEY",
                    _label(service),
                    _label(key),
                )
                continue
            if _near_miss_title(key, titles):
                # An entry titled `api_key ` or `API_KEY` is refused above as a
                # key this service does not declare, and would then be followed
                # by a delete of the very credential it was meant to set —
                # refuse the new value and destroy the old one, from one
                # trailing space on a phone keyboard. Neither branch knows about
                # the other, and §6 rule 3 licenses the delete in the abstract,
                # but the combination is a state no rule intends. The fuzzy
                # match only ever *holds* a deletion; nothing is written on it.
                result.skipped.append((service, key, SKIP_DELETE_HELD))
                logger.warning(
                    "vault: %s/%s was not deleted: an entry title in that group "
                    "differs from it only in case or surrounding whitespace, so "
                    "the deletion is more likely a typo than an instruction",
                    _label(service),
                    _label(key),
                )
                continue
            pending_deletes.append((service, key))

    if pending_deletes:
        # Named **before** the loop rather than after it. Each `delete_secret`
        # commits its own transaction, so a raise part-way through — a locked
        # database on a background gate contending with the scheduler's own
        # writers — leaves earlier rows gone with nothing on any surface saying
        # which. Logging the plan over-reports in that case, which is the
        # direction to be wrong in: the operator gets a superset of what went.
        logger.warning(
            "vault: %s: deleting %d credential(s) absent from the vault: %s",
            _label(user_id),
            len(pending_deletes),
            ", ".join(f"{_label(s)}/{_label(k)}" for s, k in pending_deletes),
        )
    for service, key in pending_deletes:
        if secrets_store.delete_secret(db_path, user_id, service, key):
            result.deleted += 1
            result.deleted_keys.append((service, key))
    return result


def _near_miss_title(key: str, titles: frozenset[str]) -> bool:
    """Whether some title in this group was probably meant to be ``key``.

    Compares on the two axes a phone keyboard moves: surrounding whitespace and
    case. §3 matches entry titles exactly and reports a mismatch as a typo,
    which is the right rule for deciding what to *write*; this is the narrower
    question of whether a deletion licensed by that same mismatch should go
    ahead, and there the answer that costs nothing is to hold.

    **Asked of the group's titles rather than of the skips this pass recorded**,
    which is wider by two shapes and was the first version's gap. An unknown key
    only reaches ``skipped`` if it carried a value, so a near-miss title with an
    *empty* password — or a duplicated one — is dropped at parse, lands in no
    skip list, and its spelling is exactly what keeps the real key out of
    ``entry_titles``: measured, `API_KEY` with a blank password beside a stored
    `api_key` deleted the credential and put nothing in ``skipped``, so the user
    got neither the new value nor a word about losing the old one. The evidence
    the hold rests on is "the group holds a title that was probably meant to be
    this key", which is a property of the titles, so that is what it reads.

    A title equal to the key never reaches here: it would be in ``titles``, and
    a key in ``titles`` is not a deletion candidate at all.
    """
    return any(title.strip().casefold() == key.casefold() for title in titles)


def _service_refusal(service: str, eligible: frozenset[str]) -> str | None:
    """Why this service may not be vault-owned, or None.

    The reserved-namespace arm is checked before eligibility although the
    eligible set already excludes it, so that the refusal names the namespace
    rather than reading as an ordinary unknown service — and so that it survives
    a connector service one day appearing in the schema.
    """
    if service.startswith(_RESERVED_SERVICE_PREFIX):
        return SKIP_RESERVED_SERVICE
    if service not in eligible:
        return SKIP_INELIGIBLE_SERVICE
    return None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recyclebin_uuid(kp):
    """The recycle bin's UUID, or None where there is not one.

    Defence in depth rather than the boundary. The walk below starts at the root
    group's own children, and KeePassXC's recycle bin is a root-level group, so
    both a trashed entry and a trashed ``istota`` group are already outside what
    is read. What this covers is the layout that is not excluded by
    construction: any group can be nominated as the recycle bin, including a
    service group, at which point every entry in it is something the user
    deleted sitting exactly where the walk looks.

    ``recyclebin_group`` reads the Meta element and decodes it, so a database
    whose Meta is absent or malformed raises rather than answering — and a
    missing recycle bin must not fail the whole read.
    """
    try:
        group = kp.recyclebin_group
    except Exception:  # pragma: no cover - a Meta element no editor writes
        return None
    return getattr(group, "uuid", None) if group is not None else None


def _map_groups(kp, digest: str) -> VaultRead:
    """``istota/<service>/<key>`` out of an open database.

    The entry **title** is the key and the **password** field is the value.
    Username, URL, notes, attachments, custom string fields, anything nested
    deeper than one subgroup, and entries sitting directly under ``istota/`` are
    all ignored — a title rather than custom attributes because several mobile
    clients cannot create those at all, and editing from a phone is the point.

    **The service segment folds and nothing else does.** A phone keyboard
    autocapitalizes a group name it takes for the start of a sentence, and
    ``Karakeep`` silently owning nothing is the hardest failure in this design to
    diagnose from the user's end, because the file looks right. Every service
    name is lower-case ASCII with no pair that collides when folded. Entry
    titles are matched exactly, since a key that does not match the schema is
    reported as a typo at apply time rather than silently discarded, and so is
    the ``istota`` group itself.

    **Two spellings of one service are one service.** The spec does not name
    that case; it is the duplicate-title rule's own premise a level up, since
    ``Karakeep/api_key`` beside ``karakeep/api_key`` is the same choice made by
    XML order. So entries pool across every group folding to one name, a title
    seen twice anywhere in the pool is a duplicate skip, and ``group_present``
    keeps the first spelling found.

    Neither skip rule is a deletion. A key skipped here is simply absent from
    the group, and what the apply step does about that is its own rule.

    **Depth comes from ``Group.entries`` and ``Group.subgroups`` being direct
    children**, which is a property of pykeepass rather than of anything written
    here — so the two exclusions that follow from it are pinned by a fixture
    rather than by a branch: an entry one subgroup deeper is not a key, and an
    edited entry's ``History`` copies are not duplicates of it. The second is the
    expensive one if it ever changes, since it would turn every credential the
    user has ever edited into a duplicate hard-skip.

    A group whose name is only whitespace is dropped rather than becoming a
    service, because §6's deletion rule fires on presence in ``group_present``
    and a junk row there is a row that can delete. It is *not* stripped for the
    purpose of matching, since §3 strips values and normalizes nothing else.
    """
    services: dict[str, dict[str, str]] = {}
    group_present: dict[str, str] = {}
    seen: dict[str, set[str]] = {}
    duplicated: dict[str, set[str]] = {}
    recyclebin = _recyclebin_uuid(kp)

    for root_group in kp.root_group.subgroups:
        if root_group.name != VAULT_ROOT_GROUP:
            continue
        if recyclebin is not None and root_group.uuid == recyclebin:
            continue
        for group in root_group.subgroups:
            if recyclebin is not None and group.uuid == recyclebin:
                continue
            name = group.name or ""
            if not name.strip():
                continue
            folded = name.casefold()
            group_present.setdefault(folded, name)
            values = services.setdefault(folded, {})
            titles = seen.setdefault(folded, set())
            dupes = duplicated.setdefault(folded, set())
            untitled = 0
            for entry in group.entries:
                if not entry.title:
                    untitled += 1
                    continue
                _take_entry(folded, entry, values, titles, dupes)
            if untitled:
                # Counted rather than one line each: an entry with no title has
                # no name to report, so N lines say exactly what one line says.
                logger.warning(
                    "vault: %s has %d entr%s with no title, skipped",
                    _label(name),
                    untitled,
                    "y" if untitled == 1 else "ies",
                )

    return VaultRead(
        digest=digest,
        services=services,
        group_present=group_present,
        # `seen` is already "every title in this group", skipped ones included,
        # which is what the deletion rule needs and what `services` is not.
        entry_titles={service: frozenset(titles) for service, titles in seen.items()},
    )


def _label(name: str) -> str:
    """A group or key name, bounded and flattened, for a log line.

    Both are arbitrary strings out of the file: unbounded in length and free to
    contain newlines, so an unflattened one can forge a log record in the
    daemon's own log. Self-inflicted rather than attacker-reachable — the file is
    the user's — which is why this flattens rather than refusing. The rule is
    ``transport``'s ``_slug``: bound every axis that came from outside.
    """
    flat = "".join(ch if ch.isprintable() else " " for ch in name)
    return flat[:_LABEL_MAX_CHARS] + ("…" if len(flat) > _LABEL_MAX_CHARS else "")


def _take_entry(
    service: str,
    entry,
    values: dict[str, str],
    titles: set[str],
    duplicated: set[str],
) -> None:
    """One entry into ``values``, or a warning saying why not.

    **The title is recorded as seen before the value is looked at**, which is
    the ordering the duplicate rule depends on: an empty-password ``api_key``
    followed by an ``api_key`` holding a value would otherwise resolve to the
    second, which is the silent XML-order dependence the rule exists to refuse,
    arrived at from the other direction. A duplicate also removes whatever was
    already accepted under that title, since the first copy is no more
    authoritative than the second.

    The caller has already dropped an entry with no title, since that one has no
    name to report and is counted per group instead. Every name that does reach a
    log line goes through ``_label``.
    """
    title = entry.title
    if title in titles:
        values.pop(title, None)
        if title not in duplicated:
            duplicated.add(title)
            logger.warning(
                "vault: %s/%s appears more than once, so neither copy is used; "
                "delete one",
                _label(service),
                _label(title),
            )
        return
    titles.add(title)

    value = (entry.password or "").strip()
    if not value:
        logger.warning(
            "vault: %s/%s has an empty password field, skipped; delete the entry "
            "to remove the credential",
            _label(service),
            _label(title),
        )
        return
    values[title] = value


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
    """
    if len(value.strip()) < VAULT_PASSPHRASE_MIN_CHARS:
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
#: No ``vault_path`` for this user — the feature is off, which is the default.
OUTCOME_NOT_CONFIGURED = "not_configured"
#: ``sync_all``'s containment: something outside the closed set of vault errors
#: escaped ``sync_user``. A bug, reported rather than swallowed.
OUTCOME_ERROR = "error"


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
    owned: frozenset[str] = frozenset()
    apply: VaultApplyResult | None = None


@dataclass(frozen=True)
class VaultStatusReport:
    """What ``vault-status`` knows, as data rather than as printed lines.

    One answer, two renderers — the rule ``usage_render`` already states for a
    fact with a CLI and a web surface. The CLI formats this; §9's
    ``GET /settings/vault`` will serialise the same fields.

    **Names and counts only, never a value.** ``groups`` maps the folded service
    name to the spelling as it stands in the file, which is what tells a user
    whose group is ``Karakeep`` that it matched; ``key_counts`` is how many
    usable entries each group holds; ``unowned`` and ``absent`` are the two ways
    a group and ``vault_services`` fail to line up, and between them they answer
    the hardest failure in this design to diagnose from the user's end.
    """

    user_id: str
    configured: bool
    path: str = ""
    refusal: str = ""
    owned: tuple[str, ...] = ()
    passphrase_present: bool = False
    outcome: str = ""
    reason: str = ""
    groups: dict[str, str] = field(default_factory=dict)
    key_counts: dict[str, int] = field(default_factory=dict)
    unowned: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    last_outcome: str = ""


def format_skip(service: str, key: str) -> str:
    """One ``skipped`` triple's subject, for a human-readable report.

    ``VaultApplyResult.skipped`` carries an **empty key** for a service-level
    refusal — the whole service was refused and no key of it was looked at — so
    rendering ``f"{service}/{key}"`` blindly prints ``monarch/`` with nothing
    after the slash, which reads as a key named ``""`` rather than as a service
    nobody may own.
    """
    return f"{_label(service)}/{_label(key)}" if key else _label(service)


def vault_owned_services(config, user_id: str) -> frozenset[str]:
    """Which of this user's services the vault owns, right now.

    The predicate §9's ``vault_managed`` and open question 3's CLI refusal both
    read: the service is in ``vault_services`` **and** the user's ``vault_path``
    resolves. A configured-but-refused path owns nothing in practice, and
    refusing an operator's write on the strength of a line that does not work is
    a refusal with no remedy behind it.

    **The cheap half is asked first**, deliberately. Resolving walks directories
    and opens a descriptor, and it logs a refusal — so testing the service set
    afterwards would make every ``istota secret ensure`` for every user pay a
    directory walk, and would emit ``vault_path_refused`` as a side effect of
    commands that have nothing to do with the vault.

    Closes the descriptor it opens: this asks a question and opens nothing.
    """
    user = config.users.get(user_id)
    declared = frozenset(getattr(user, "vault_services", None) or ())
    if not declared:
        return frozenset()

    from . import storage  # noqa: PLC0415 - see `sync_user` for why

    location = storage.resolve_user_vault_path(config, user_id).location
    if location is None:
        return frozenset()
    if location.dir_fd is not None:
        os.close(location.dir_fd)
    return declared


def sync_user(config, user_id: str, *, force: bool = False) -> VaultSyncResult:
    """One user's vault, read and applied if the bytes have moved.

    The order is the design and each step earns its place ahead of the next:

    1. **Resolve.** No ``vault_path`` is not a failure and is reported as
       ``OUTCOME_NOT_CONFIGURED``; a *refused* path is ``VaultPathRefused``.
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
    is what ``istota secret vault-sync`` passes.

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
    from . import storage  # noqa: PLC0415

    if force:
        reset_sync_state(user_id)

    resolution = storage.resolve_user_vault_path(config, user_id)
    if resolution.location is None:
        if resolution.refusal is None:
            # The feature is off for this user. Not a state worth remembering,
            # and not one to report a transition out of.
            return VaultSyncResult(user_id=user_id, outcome=OUTCOME_NOT_CONFIGURED)
        return _settle(
            user_id, VaultPathRefused(resolution.refusal), digest=None, path=""
        )

    location = resolution.location
    path = str(location.path)
    owned = frozenset(getattr(config.users.get(user_id), "vault_services", None) or ())
    try:
        return _sync_resolved(config, user_id, location, path, owned)
    finally:
        if location.dir_fd is not None:
            os.close(location.dir_fd)


def _sync_resolved(config, user_id, location, path, owned) -> VaultSyncResult:
    """Everything after the path resolved, with the descriptor still open."""
    try:
        # `dir_fd` beside `path`, never one without the other: the descriptor is
        # what covers every component above the leaf, which for the relative form
        # all live in the tree bound read-write into this user's own sandbox.
        # `tests/test_overlay_dir_containment.py` fails a call that drops it.
        data, digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
    except VaultError as exc:
        return _settle(user_id, exc, digest=None, path=path)

    cached_digest, _last = _SYNC_STATE.get(user_id, (None, ""))
    if cached_digest is not None and cached_digest == digest:
        # The whole point of `read_vault_bytes` and `parse_vault` being two
        # functions. A single `read_vault(path, passphrase)` would spend an
        # Argon2id unlock every cycle to learn that nothing had changed.
        return VaultSyncResult(
            user_id=user_id,
            outcome=OUTCOME_UNCHANGED,
            digest=digest,
            path=path,
            owned=owned,
        )

    try:
        passphrase = _resolve_passphrase(config.db_path, user_id)
        read = parse_vault(data, passphrase)
    except VaultError as exc:
        return _settle(user_id, exc, digest=digest, path=path)

    applied = apply_vault(config.db_path, user_id, read, owned)
    return _settle(
        user_id, None, digest=digest, path=path, owned=owned, applied=applied
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
        raise VaultKeyUnusable(str(exc)) from exc

    value = secrets_store.get_secret(
        db_path, user_id, VAULT_PASSPHRASE_SERVICE, VAULT_PASSPHRASE_KEY
    )
    if value:
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


def _settle(
    user_id: str,
    exc: VaultError | None,
    *,
    digest: str | None,
    path: str,
    owned: frozenset[str] = frozenset(),
    applied: VaultApplyResult | None = None,
) -> VaultSyncResult:
    """Record the outcome, decide whether it is a transition, and say so once.

    The digest is stored **only** for a cacheable outcome, so the skip test one
    call up is a plain equality rather than a second condition that has to agree
    with ``_CACHEABLE_OUTCOMES``.
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
            _label(path) if path else "unresolved",
            _label(reason),
        )
    elif transition and previous:
        logger.info("vault: %s: reading again after %s", _label(user_id), previous)

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
        owned=owned,
        apply=applied,
    )


def sync_all(
    config, *, users: list[str] | None = None, force: bool = False
) -> list[VaultSyncResult]:
    """Every configured user's vault, one at a time, containing each failure.

    One user's vault is not another's, and a raise out of one must not cost the
    rest — the gate that calls this runs unattended every 300 seconds, so the
    alternative is a single malformed config silently stopping the feature for
    the whole deployment.

    Anything outside the ``VaultError`` closed set is a bug rather than a vault
    condition, so it is logged with a stack and reported as ``OUTCOME_ERROR``
    rather than folded into an error class a notification would then explain
    wrongly.
    """
    wanted = list(config.users) if users is None else users
    results: list[VaultSyncResult] = []
    for user_id in wanted:
        try:
            results.append(sync_user(config, user_id, force=force))
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


def vault_status(config, user_id: str) -> VaultStatusReport:
    """What this user's vault looks like right now, without applying anything.

    **It reads and parses and writes nothing**, which is what makes it the verb
    an operator can run while wondering whether to trust the file. It also
    touches no sync state in either direction: it is not subject to the digest
    cache — the operator is asking *now* — and it does not settle an outcome,
    since an outcome nobody applied would suppress the next real cycle's report.

    Parsing is what earns the command its keep. §3 records every
    ``istota/<service>`` group found, owned or not, precisely so this can tell a
    user their group name matches nothing — the hardest failure in this design to
    diagnose from their end, because the file looks right.
    """
    from . import storage  # noqa: PLC0415 - see `sync_user`

    user = config.users.get(user_id)
    raw = getattr(user, "vault_path", "") if user is not None else ""
    owned = tuple(sorted(getattr(user, "vault_services", None) or ()))
    last = _SYNC_STATE.get(user_id, (None, ""))[1]
    if not raw:
        return VaultStatusReport(user_id=user_id, configured=False, owned=owned)

    resolution = storage.resolve_user_vault_path(config, user_id)
    if resolution.location is None:
        return VaultStatusReport(
            user_id=user_id,
            configured=True,
            refusal=resolution.refusal or "",
            owned=owned,
            outcome=VaultPathRefused.__name__,
            reason=resolution.refusal or "",
            last_outcome=last,
        )

    location = resolution.location
    present = secrets_store.secret_exists(
        config.db_path, user_id, VAULT_PASSPHRASE_SERVICE, VAULT_PASSPHRASE_KEY
    )
    report = VaultStatusReport(
        user_id=user_id,
        configured=True,
        path=str(location.path),
        owned=owned,
        passphrase_present=present,
        last_outcome=last,
    )
    try:
        try:
            data, _digest = read_vault_bytes(
                location.path, dir_fd=location.dir_fd
            )
            passphrase = _resolve_passphrase(config.db_path, user_id)
            read = parse_vault(data, passphrase)
        except VaultError as exc:
            return dataclasses.replace(
                report, outcome=type(exc).__name__, reason=str(exc)
            )
    finally:
        if location.dir_fd is not None:
            os.close(location.dir_fd)

    owned_set = set(owned)
    return dataclasses.replace(
        report,
        outcome=OUTCOME_OK,
        groups=dict(read.group_present),
        key_counts={
            service: len(values) for service, values in read.services.items()
        },
        unowned=tuple(sorted(set(read.group_present) - owned_set)),
        absent=tuple(sorted(owned_set - set(read.group_present))),
    )
