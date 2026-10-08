"""Fresh KeePass exports built entirely in memory from the credential store."""

from dataclasses import dataclass
import hashlib
import io
import threading

from istota import db
from istota.credentials import generated, store, vault
from istota.credentials.broker import bindings, grants
from istota.lib import totp


@dataclass(frozen=True)
class ExportOptions:
    argon2_memory_kib: int
    argon2_iterations: int
    argon2_parallelism: int


INTERACTIVE = ExportOptions(256 * 1024, 8, 2)
BACKUP = ExportOptions(64 * 1024, 2, 2)
EXPORT_SLOT = threading.Semaphore(1)


@dataclass(frozen=True)
class ExportSummary:
    credentials: int
    generated: int
    otp: int
    recovery: int


def generate_export_password() -> str:
    raw = vault.generate_password(vault.PasswordPolicy(length=24, allow_symbols=False, require_symbols=False))
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def keyfile_refusal(keyfile: bytes) -> str | None:
    if not isinstance(keyfile, bytes) or not keyfile or len(keyfile) > 4096:
        return "keyfile_invalid"
    try:
        from lxml import etree
    except ImportError:
        raise vault.VaultLibraryMissing("Install the vault extra") from None
    try:
        root = etree.fromstring(keyfile, etree.XMLParser(resolve_entities=False, no_network=True))
        if root.getroottree().docinfo.doctype or root.tag != "KeyFile" or root.findtext("Meta/Version") != "2.0":
            return "keyfile_invalid"
        data = root.find("Key/Data")
        if data is None:
            return "keyfile_invalid"
        raw = bytes.fromhex(data.text or "")
        if len(raw) != 32 or data.get("Hash", "").upper() != hashlib.sha256(raw).hexdigest()[:8].upper():
            return "keyfile_invalid"
    except (ValueError, etree.LxmlError):
        return "keyfile_invalid"
    return None


def _write_generated_entry(entry, values: dict[str, str]) -> None:
    """The fields shared by exports and the temporary legacy mirror writer."""
    entry.password = values.get("password") or ""
    entry.username = values.get("username") or ""
    entry.url = values.get("url") or ""
    for field_name in list(entry.custom_properties):
        folded = str(field_name).casefold()
        if folded in vault._LEGACY_OTP_FIELDS or folded.startswith(("timeotp-", "hmacotp-")):
            entry.delete_custom_property(field_name)
    if values.get("otp"):
        entry.otp = values["otp"]
    elif entry.otp:
        entry.otp = ""
    for field_name in list(entry.custom_properties):
        if str(field_name).casefold() == generated.RECOVERY_FIELD.casefold():
            entry.delete_custom_property(field_name)
    if values.get("recovery"):
        entry.set_custom_property(generated.RECOVERY_FIELD, values["recovery"], protect=True)


def build_kdbx(db_path, user_id, *, password: str, keyfile: bytes | None,
               options: ExportOptions) -> tuple[bytes, ExportSummary]:
    try:
        import pykeepass
    except ImportError:
        raise vault.VaultLibraryMissing("Install the vault extra") from None
    if keyfile is not None and keyfile_refusal(keyfile):
        raise ValueError("keyfile_invalid")
    if not store.secret_key_available():
        raise store.SecretKeyMissingError("The credential store is not configured")
    # One snapshot for values, ownership and bindings, without reading any vault file.
    entries = []
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN")
        groups = bindings.credential_groups(conn, user_id)
        for owner, names in sorted(groups.items()):
            binding = bindings.get_entry_binding(conn, user_id, owner, groups)
            if binding and binding["source"] == "config":
                continue
            binding = binding or bindings.parse_binding("", {}, [], source="local")
            members = {}
            for name in names:
                member_binding = bindings.get_binding(conn, user_id, name)
                if member_binding and member_binding["source"] == "config":
                    continue
                value = store.get_secret(None, user_id, "vault_entries", name, connection=conn)
                if value is None:
                    raise ValueError("export_value_unavailable")
                members[name] = value
            if members:
                declined = grants.auto_grant_marker(conn, user_id, owner) == grants.AUTO_GRANT_DECLINED
                entries.append((owner, members, binding, declined))
    if not entries:
        raise ValueError("export_empty")
    kp = pykeepass.create_database(io.BytesIO(), password=password,
                                   keyfile=io.BytesIO(keyfile) if keyfile is not None else None)
    params = kp.kdbx.header.value.dynamic_header.kdf_parameters.data.dict
    params["$UUID"].value = bytes.fromhex("9e298b1956db4773b23dfc3ec6f0a1e6")
    params["M"].value = options.argon2_memory_kib * 1024
    params["I"].value = options.argon2_iterations
    params["P"].value = options.argon2_parallelism
    root = kp.add_group(kp.root_group, vault.VAULT_ROOT_GROUP)
    generated_group = None
    generated_count = otp_count = recovery_count = 0
    for owner, members, binding, declined in entries:
        values = {field: members.get(name, "") for field, name in generated.entry_names(owner).items()}
        url, attributes, tags = bindings.binding_entry_fields(binding)
        # Preserve the user's complete URL, including its path, when present.
        values["url"] = values["url"] or url
        if binding["source"] == "generated":
            if generated_group is None:
                generated_group = kp.add_group(root, vault.VAULT_WRITE_GROUP)
            entry = kp.add_entry(generated_group, owner.removeprefix(vault.VAULT_WRITE_GROUP + "_"), "", "")
            generated_count += 1
        else:
            entry = kp.add_entry(root, owner, "", "")
        if values["otp"]:
            values["otp"] = totp.to_uri(totp.parse_otpauth(values["otp"]))
            otp_count += 1
        recovery_count += bool(values["recovery"])
        _write_generated_entry(entry, values)
        for key, value in attributes.items():
            entry.set_custom_property(key, value)
        if declined:
            tags.append(vault.VAULT_NO_GRANT_TAG)
        entry.tags = tags
        known = set(generated.entry_names(owner).values())
        for name, value in members.items():
            if name not in known:
                entry.set_custom_property(name.removeprefix(owner + "_"), value, protect=True)
    out = io.BytesIO()
    kp.save(out)
    return out.getvalue(), ExportSummary(len(entries), generated_count, otp_count, recovery_count)
