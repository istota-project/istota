"""Stateless, selected imports from a KeePass file into the credential store."""

from contextlib import nullcontext
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field as dataclass_field
import hashlib

import io
import logging
from istota.lib import totp
from istota.credentials.names import (VaultError, VAULT_MAX_VALUE_BYTES, VAULT_NAME_MAX_CHARS, slug_name, _label)

from istota import db
from istota.credentials import audit, generated, store
from istota.credentials.broker import bindings, grants

logger = logging.getLogger(__name__)

_SERVICE = "vault_entries"


@dataclass(frozen=True)
class ImportItem:
    name: str
    origin: str
    fields: tuple[str, ...]
    hosts: tuple[str, ...]
    status: str
    changed_fields: tuple[str, ...]
    reason: str | None
    default_selected: bool


@dataclass(frozen=True)
class ImportPreview:
    digest: str
    scoped: bool
    truncated: str
    items: tuple[ImportItem, ...]
    skipped: dict[str, int]


@dataclass(frozen=True)
class ImportResult:
    imported: list[str]
    not_imported: dict[str, str]


def _field(owner, name):
    if name == owner:
        return "value"
    suffix = name.removeprefix(owner + "_")
    return "otp" if suffix == "totp" else suffix


def _entries(read):
    entries = {}
    for name, value in read.services.items():
        binding = read.bindings[name]
        owner = binding.get("credential", name)
        entries.setdefault(owner, {})[name] = (value, {**binding, "source": "local"})
    for owner, copy in read.generated.items():
        members = {}
        for field, name in generated.entry_names(owner).items():
            if value := copy.get(field):
                kind = {"otp": "totp", "recovery": "recovery"}.get(field, "value")
                binding = read.generated_bindings.get(owner) or bindings.parse_binding(copy.get("url", ""), {}, [], source="generated")
                members[name] = (value, {**binding, "credential": owner, "kind": kind})
        entries[owner] = members
    return entries


def _preview(conn, user_id, read, entries):
    groups = bindings.credential_groups(conn, user_id)
    stored_names = {name for names in groups.values() for name in names}
    produced = {}
    for name in read.services:
        produced[name] = {(read.bindings[name].get("credential", name), "entry")}
    for owner, copy in read.generated.items():
        for field, name in generated.entry_names(owner).items():
            if copy.get(field):
                produced.setdefault(name, set()).add((owner, "generated"))
    collided = {owner for sources in produced.values() if len(sources) > 1 for owner, _ in sources}
    items = []
    for owner, members in sorted(entries.items()):
        origin = "generated" if owner in read.generated else "entry"
        expected_source = "generated" if origin == "generated" else "local"
        current = set(groups.get(owner, []))
        reason = None
        # Generated creation reserves every member, even fields absent in this copy.
        destinations = set(generated.entry_names(owner).values()) if origin == "generated" else set(members)
        for name in current | destinations | {owner}:
            binding = bindings.get_binding(conn, user_id, name)
            if binding and bindings.effective_source(binding["source"]) != expected_source:
                reason = "name_taken_" + bindings.effective_source(binding["source"])
                break
            if (name in destinations and (binding or name in stored_names)
                    and bindings.credential_name(conn, user_id, name) != owner):
                reason = "name_taken_local"
                break
            if origin == "generated" and name in current and binding is None:
                reason = "name_taken_local"
                break
        changed = set()
        for name in current | set(members):
            incoming = members.get(name)
            if incoming is None or name not in current:
                changed.add(_field(owner, name))
                continue
            value, binding = incoming
            stored = store.get_secret(None, user_id, _SERVICE, name, connection=conn)
            old_binding = bindings.get_binding(conn, user_id, name) or {}
            metadata = ("hosts", "headers", "revealable", "kind")
            if stored != value or any(old_binding.get(k, "value" if k == "kind" else None)
                                      != binding.get(k, "value" if k == "kind" else None) for k in metadata):
                changed.add(_field(owner, name))
        if owner in collided:
            status, reason = "skipped", SKIP_DUPLICATE_NAME
        elif reason:
            status = "conflict"
        elif not members or (origin == "generated" and not read.generated[owner].get("password")):
            status, reason = "skipped", SKIP_EMPTY_VALUE
        else:
            status = "new" if not current else "changed" if changed else "unchanged"
        hosts = tuple(sorted({h for _, binding in members.values() for h in binding["hosts"]}))
        items.append(ImportItem(owner, origin, tuple(_field(owner, n) for n in members), hosts,
                                status, tuple(sorted(changed)) if status == "changed" else (),
                                reason, status == "new"))
    named = {item.name for item in items}
    skipped = list(read.skipped)
    for name in sorted(read.held):
        owner = read.bindings.get(name, {}).get("credential", name)
        if owner not in named:
            skipped.append((owner, SKIP_EMPTY_VALUE))
    for name, reason in skipped:
        if name not in named:
            items.append(ImportItem(name, "entry", (), (), "skipped", (), reason, False))
            named.add(name)
    return ImportPreview(read.digest, read.scoped, read.truncated, tuple(items),
                         dict(Counter(reason for _, reason in read.skipped)))


def preview(db_path, user_id, data: bytes, passphrase: str, *, keyfile: bytes | None = None) -> ImportPreview:
    if not store.secret_key_available():
        raise store.SecretKeyMissingError("The credential store is not configured")
    read = parse_vault(data, passphrase, keyfile=keyfile)
    with db.get_db(db_path) as conn:
        return _preview(conn, user_id, read, _entries(read))


def apply(db_path, user_id, data: bytes, passphrase: str, *, selected: Sequence[str],
          expected_digest: str, actor: str, keyfile: bytes | None = None,
          migration: bool = False, connection=None) -> ImportResult:
    if not store.secret_key_available():
        raise store.SecretKeyMissingError("The credential store is not configured")
    if not selected:
        raise ValueError("import_nothing_selected")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("import_file_changed")
    read = parse_vault(data, passphrase, keyfile=keyfile)
    entries = _entries(read)
    imported, not_imported = [], {}
    with (nullcontext(connection) if connection is not None else db.get_db(db_path)) as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        items = {item.name: item for item in _preview(conn, user_id, read, entries).items}
        groups = bindings.credential_groups(conn, user_id)
        grants.baseline_auto_grants(conn, user_id, groups)
        owners = []
        changed = 0
        for owner in dict.fromkeys(selected):
            item = items.get(owner)
            if item is None or item.status not in ("new", "changed"):
                not_imported[owner] = item.status if item else "not_found"
                continue
            members = entries[owner]
            if migration:
                # The final automatic import cannot replace a user-owned value.
                occupied = set(groups.get(owner, [])) | set(members) | {owner}
                if item.origin == "generated" and item.status != "new":
                    continue
                if item.origin == "entry" and any(
                    (bindings.get_binding(conn, user_id, name) or {}).get("source")
                    in ("local", "generated", "config") for name in occupied
                ):
                    continue
            if item.origin == "generated":
                copy = read.generated[owner]
                if item.status == "new":
                    generated.create(conn, user_id, name=owner, username=copy.get("username", ""),
                                     password=copy["password"], url=copy.get("url", ""),
                                     actor=actor)
                else:
                    generated._write_rows(conn, user_id, owner, {
                        k: v for k, v in copy.items() if k not in ("otp", "recovery")}, actor=actor)
                if copy.get("otp"):
                    generated.set_otp(conn, user_id, owner, copy["otp"], replace=True, actor=actor)
                if copy.get("recovery"):
                    lines = copy["recovery"].splitlines()
                    spent = [index for index, line in enumerate(lines) if line.startswith("(used) ")]
                    text = "\n".join(line.removeprefix("(used) ") for line in lines)
                    generated.set_recovery(conn, user_id, owner, text,
                                           fmt="codes" if spent else "block", actor=actor)
                    if spent:
                        import json
                        conn.execute("UPDATE recovery_code_state SET spent=? WHERE user_id=? AND name=?",
                                     (json.dumps(spent), user_id, owner))
                for name, (_, binding) in members.items():
                    bindings.put_binding(conn, user_id, name, binding)
            else:
                for name, (value, binding) in members.items():
                    store.set_secret(None, user_id, _SERVICE, name, value,
                                     binding=binding, connection=conn, actor=actor)
                owners.append(owner)
            for name in (() if migration else set(groups.get(owner, [])) - set(members)):
                store.delete_secret(None, user_id, _SERVICE, name, connection=conn, actor=actor)
            imported.append(owner)
            changed += item.status == "changed"
            audit.record(conn, user_id, action="import", actor=actor, name=owner)
        grants.auto_grant_vault_entries(conn, user_id, owners,
                                       declined=read.no_auto_grant, scoped=read.scoped)
        audit.record(conn, user_id, action="import", actor=actor,
                     detail={"imported": len(imported), "changed": changed,
                             "new": len(imported) - changed, "digest": read.digest[:12]})
    return ImportResult(imported, not_imported)


VAULT_WRITE_GROUP = "generated"


VAULT_NO_GRANT_TAG = "istota:nogrant"


VAULT_READ_CAP_BYTES = 8 * 1024 * 1024


VAULT_ROOT_GROUP = "istota"


VAULT_MAX_DEPTH = 8


VAULT_MAX_ENTRIES = 512


VAULT_MAX_NAMES = 1024


_TRUNCATING_CAPS = frozenset({"entry", "name"})


_USERNAME_SEGMENT = "username"


_URL_SEGMENT = "url"


SKIP_UNUSABLE_NAME = "the entry name cannot be used"


SKIP_DUPLICATE_NAME = "two entries produce the same name"


SKIP_EMPTY_VALUE = "the field is empty"


SKIP_UNUSABLE_OTP = "the OTP source cannot be used"


SKIP_OVERSIZE_VALUE = "the value is larger than the limit"


class VaultCorrupt(VaultError):
    """Bytes that are not a readable KeePass database."""


class VaultLocked(VaultError):
    """The stored credentials do not open the file."""


class VaultLibraryMissing(VaultError):
    """The ``vault`` extra is not installed on this host. An operator remedy."""


@dataclass(frozen=True)
class VaultRead:
    """One parsed vault: the names it holds, and what it will not say about."""

    digest: str
    services: dict[str, str]
    held: frozenset[str]
    truncated: str
    scoped: bool
    skipped: tuple[tuple[str, str], ...] = ()
    generated_count: int = 0
    bindings: dict[str, dict] = dataclass_field(default_factory=dict)
    no_auto_grant: frozenset[str] = frozenset()
    #: Entries directly under ``generated/``: owner name -> ``password``,
    #: ``username``, ``url``, ``otp``. Not names in the namespace: since
    #: ISSUE-686 they are the mirror of credentials the table owns, compared by
    generated: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)
    generated_bindings: dict[str, dict] = dataclass_field(default_factory=dict)

    def __repr__(self) -> str:
        """Everything but the values."""
        return (
            f"VaultRead(digest={self.digest!r}, "
            f"names={sorted(self.services)!r}, "
            f"held={sorted(self.held)!r}, truncated={self.truncated!r}, "
            f"scoped={self.scoped!r}, skipped={self.skipped!r})"
        )


def parse_vault(data: bytes, passphrase: str, *, keyfile: bytes | None = None) -> VaultRead:
    """``data`` opened with ``passphrase``, mapped, or a mapped ``VaultError``."""
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
        kp = PyKeePass(io.BytesIO(data), password=passphrase,
                       keyfile=io.BytesIO(keyfile) if keyfile is not None else None)
        return _map_groups(kp, _digest(data))
    except CredentialsError as exc:
        raise VaultLocked(
            "the supplied credentials do not open this vault — check the passphrase "
            "and key file; hardware keys are not supported"
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


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recyclebin_uuid(kp):
    """The recycle bin's UUID, or None where there is not one."""
    try:
        group = kp.recyclebin_group
    except Exception:  # pragma: no cover - a Meta element no editor writes
        return None
    return getattr(group, "uuid", None) if group is not None else None


@dataclass
class _Walk:
    """What one walk has found so far. Mutable, single-threaded, per parse."""

    recyclebin: object = None
    #: name -> every value produced under it, so a second producer is a
    #: collision rather than an overwrite.
    candidates: dict[str, list[str]] = dataclass_field(default_factory=dict)
    bindings: dict[str, dict] = dataclass_field(default_factory=dict)
    #: Entry names the sync must not grant on its own (ISSUE-590).
    no_auto_grant: set[str] = dataclass_field(default_factory=set)
    otp_names: set[str] = dataclass_field(default_factory=set)
    #: Mirror copies under `generated/`; a name produced twice is dropped.
    generated: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)
    generated_bindings: dict[str, dict] = dataclass_field(default_factory=dict)
    generated_duplicates: set[str] = dataclass_field(default_factory=set)
    skipped: list[tuple[str, str]] = dataclass_field(default_factory=list)
    entries_visited: int = 0
    fields_examined: int = 0
    untitled: int = 0
    depth_dropped: int = 0
    stopped: str = ""


def _root_groups(kp, recyclebin) -> tuple[list, list]:
    """The top-level groups that narrow the read, and the ones to walk."""
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
    """Every name the file shares, out of an open database."""
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
        generated_bindings={name: binding for name, binding in walk.generated_bindings.items()
                            if name not in walk.generated_duplicates},
        generated={name: copy for name, copy in walk.generated.items()
                   if name not in walk.generated_duplicates},
    )


def _visit_group(walk: _Walk, group, path: tuple[str, ...], depth: int) -> None:
    """One group's entries, then its subgroups, into ``walk``."""
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
    """One entry's fields into ``walk.candidates``, or a warning saying why not."""
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
    from istota.credentials.broker.bindings import parse_binding
    walk.generated_bindings[name] = parse_binding(entry.url, entry.custom_properties, entry.tags, source="generated")
    walk.generated[name] = {
        "password": str(entry.password or "").strip(),
        "username": str(entry.username or "").strip(),
        "url": str(entry.url or "").strip(),
        "otp": otp_value or "",
        "recovery": recovery,
    }


def _original(segments: Sequence[str]) -> str:
    """The path as the file spells it, bounded and flattened."""
    return _label("/".join(str(segment) for segment in segments))


SKIP_REASONS = frozenset({SKIP_UNUSABLE_NAME, SKIP_DUPLICATE_NAME, SKIP_EMPTY_VALUE, SKIP_UNUSABLE_OTP, SKIP_OVERSIZE_VALUE})
