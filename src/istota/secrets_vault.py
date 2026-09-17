"""Reading a user's KeePass credential vault: the bytes, and what they mean.

A user who keeps their credentials in a password manager otherwise maintains two
copies of every key, and the copy istota reads is the one they cannot see,
search or back up. This module is the reading half of the answer: a KDBX file in
the user's own workspace that istota decrypts and never writes.

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
here is the last component alone, by way of ``read_overlay_bytes``.

**And it never logs a value, at any level.** Counts, service names and key names
are loggable and are what the warnings below carry. The rule is easy to defeat
by accident, which is why ``VaultRead`` carries a ``__repr__`` of its own: that
dataclass holds every plaintext the vault carries between parse and apply, and
pytest's assertion rewriting prints the repr of whatever a failing comparison
touched, so nobody has to write a logging call to leak it.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

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
    """

    digest: str
    services: dict[str, dict[str, str]]
    group_present: dict[str, str]

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
        return (
            f"VaultRead(digest={self.digest!r}, "
            f"group_present={self.group_present!r}, keys={keys!r})"
        )


def read_vault_bytes(path: Path) -> tuple[bytes, str]:
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
        data, refusal, size = read_overlay_bytes(path, max_bytes=VAULT_READ_CAP_BYTES)
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

    return VaultRead(digest=digest, services=services, group_present=group_present)


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
