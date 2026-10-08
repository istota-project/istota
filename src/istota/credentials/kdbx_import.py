"""Stateless, selected imports from a KeePass file into the credential store."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib

from istota import db
from istota.credentials import audit, generated, store, vault
from istota.credentials.broker import bindings, grants

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
            status, reason = "skipped", vault.SKIP_DUPLICATE_NAME
        elif reason:
            status = "conflict"
        elif not members or (origin == "generated" and not read.generated[owner].get("password")):
            status, reason = "skipped", vault.SKIP_EMPTY_VALUE
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
            skipped.append((owner, vault.SKIP_EMPTY_VALUE))
    for name, reason in skipped:
        if name not in named:
            items.append(ImportItem(name, "entry", (), (), "skipped", (), reason, False))
            named.add(name)
    return ImportPreview(read.digest, read.scoped, read.truncated, tuple(items),
                         dict(Counter(reason for _, reason in read.skipped)))


def preview(db_path, user_id, data: bytes, passphrase: str, *, keyfile: bytes | None = None) -> ImportPreview:
    if not store.secret_key_available():
        raise store.SecretKeyMissingError("The credential store is not configured")
    read = vault.parse_vault(data, passphrase, keyfile=keyfile)
    with db.get_db(db_path) as conn:
        return _preview(conn, user_id, read, _entries(read))


def apply(db_path, user_id, data: bytes, passphrase: str, *, selected: Sequence[str],
          expected_digest: str, actor: str, keyfile: bytes | None = None) -> ImportResult:
    if not store.secret_key_available():
        raise store.SecretKeyMissingError("The credential store is not configured")
    if not selected:
        raise ValueError("import_nothing_selected")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("import_file_changed")
    read = vault.parse_vault(data, passphrase, keyfile=keyfile)
    entries = _entries(read)
    imported, not_imported = [], {}
    with db.get_db(db_path) as conn:
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
            if item.origin == "generated":
                copy = read.generated[owner]
                if item.status == "new":
                    generated.create(conn, user_id, name=owner, username=copy.get("username", ""),
                                     password=copy["password"], url=copy.get("url", ""),
                                     mirror=False, actor=actor)
                else:
                    generated._write_rows(conn, user_id, owner, {
                        k: v for k, v in copy.items() if k not in ("otp", "recovery")}, actor=actor)
                if copy.get("otp"):
                    generated.set_otp(conn, user_id, owner, copy["otp"], replace=True, actor=actor)
                if copy.get("recovery"):
                    generated.set_recovery(conn, user_id, owner, copy["recovery"], actor=actor)
                for name, (_, binding) in members.items():
                    bindings.put_binding(conn, user_id, name, binding)
            else:
                for name, (value, binding) in members.items():
                    store.set_secret(None, user_id, _SERVICE, name, value,
                                     binding=binding, connection=conn, actor=actor)
                owners.append(owner)
            for name in set(groups.get(owner, [])) - set(members):
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
