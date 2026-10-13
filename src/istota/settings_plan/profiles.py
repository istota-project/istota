"""Read-only profile previews and transactional shared provisioning."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from istota import db, user_profiles
from istota.config import UserConfig, load_config
from istota.lib import toml_write
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan.engine import ExecutionError, Operation, Unit, state_digest
from istota.settings_plan.schema import PROFILE_FIELDS
from istota.transport import make_registry, parse_output_target


def snapshot_profile(profile):
    """Complete restorable fields; ownership, consent and enrollment stay local."""
    return {"profile_mode": "update", "profile": {
        name: deepcopy(getattr(profile, name)) for name in sorted(PROFILE_FIELDS)
    }}


def _load_config(plan, context):
    try:
        if plan.config is None:
            return load_config(context.config_path, apply_db_overlays=False)
        with tempfile.TemporaryDirectory(prefix="istota-profile-plan-") as scratch:
            path = Path(scratch) / "config.toml"
            path.write_text(toml_write.dumps(plan.config), encoding="utf-8")
            return load_config(path, apply_db_overlays=False)
    except Exception:
        raise ExecutionError("current_config_invalid", ["config"]) from None


def _binding_identity(binding):
    if binding is None:
        return None
    # Learned timestamps never make an unchanged declaration drift.
    return (binding.bootstrap_phone_number, binding.bsuid, binding.jid, binding.provider)


def _profile_state(conn, user):
    profile = user_profiles.get_profile(Path(), user, conn=conn)
    binding = db.get_whatsapp_binding(conn, user)
    return state_digest({
        "profile": snapshot_profile(profile) if profile else None,
        "managed": sorted(user_profiles.managed_fields(conn, user)),
        "binding": _binding_identity(binding),
    })


def validate_destination(conn, config, user, descriptor, *, channels=()):
    """Check destination grammar and room reach without network or store writes."""
    destinations = parse_output_target(descriptor)
    if not destinations:
        raise ValueError("invalid destination")
    known = {"talk", "email", "ntfy", "istota_file", "stream", "room"} | set(make_registry(config).names())
    talk_refs = db.talk_refs_for_member(conn, user)
    for destination in destinations:
        if destination.surface not in known:
            raise ValueError("unknown surface")
        token = destination.channel
        if not token or destination.surface not in {"talk", "web", "room"}:
            continue
        room_token = token
        if destination.surface == "talk":
            room_token = db.resolve_room_token(conn, "talk", token)
            if token not in talk_refs.values() and token not in channels:
                raise ValueError("unavailable conversation")
            if room_token is None:
                room_token = token if db.get_room(conn, token) else None
        if room_token:
            room = db.get_room(conn, room_token)
            if room is None or room.archived or not db.is_room_member(conn, room_token, user):
                raise ValueError("unavailable room")
            if db.room_is_shared(conn, room_token):
                raise ValueError("shared destination")


def _validate_profile(conn, config, user, fields, existing):
    from istota.skills._loader import load_skill_index

    for name, value in fields.items():
        try:
            if name == "timezone" and value:
                ZoneInfo(value)
            elif name in {"max_foreground_workers", "max_background_workers"} and value < 0:
                raise ValueError("negative worker limit")
            elif name == "disabled_skills":
                if set(value) - set(load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)):
                    raise ValueError("unknown skill")
            elif name == "relay_delivery" and value not in user_profiles.RELAY_DELIVERY_VALUES:
                raise ValueError("invalid relay preference")
            elif name in {"log_channel", "alerts_channel"} and value:
                if existing is None or value != getattr(existing, name):
                    validate_destination(conn, config, user, "talk:" + value)
            elif name == "default_room" and value:
                validate_destination(conn, config, user, "web:" + value.strip())
            elif name in {"default_destination", "routing"}:
                channels = (existing.log_channel, existing.alerts_channel) if existing else ()
                descriptors = value.values() if name == "routing" else [value or "talk"]
                for descriptor in descriptors:
                    if descriptor:
                        validate_destination(conn, config, user, descriptor, channels=channels)
        except (ValueError, ZoneInfoNotFoundError):
            raise ExecutionError("validation_error", ["users", user, "profile", name]) from None


def _enrollment_args(sections):
    enrollment = sections.get("enrollment", {})
    operation = None
    if enrollment.get("clear"):
        operation = "clear"
    elif "number" in enrollment or "bsuid" in enrollment:
        operation = "set"
    return {"mode": sections.get("profile_mode", "update"), "whatsapp_op": operation,
            "whatsapp_number": enrollment.get("number"), "whatsapp_bsuid": enrollment.get("bsuid")}


def _claim_identities(context, user, provision, before, binding):
    from istota.webui.auth import normalize_email

    claims = [("email", normalize_email(value)) for value in provision.profile.email_addresses
              if before is None or normalize_email(value) not in {
                  normalize_email(item) for item in before.email_addresses}]
    if provision.profile.sms_phone_number and (
            before is None or provision.profile.sms_phone_number != before.sms_phone_number):
        claims.append(("sms", provision.profile.sms_phone_number))
    if provision.whatsapp_op == "set":
        number, bsuid = db.prospective_whatsapp_identity(
            binding, bootstrap_phone_number=provision.whatsapp_number, bsuid=provision.whatsapp_bsuid)
        if number:
            claims.append(("whatsapp", number))
        if bsuid:
            claims.append(("bsuid", bsuid))
    for claim in claims:
        holder = context.profile_claims.get(claim)
        if holder is not None and holder != user:
            raise ExecutionError("identity_conflict", ["users", user, "profile"])
        context.profile_claims[claim] = user


def plan_profile(plan, user, context, options):
    if context.config is None:
        context.config = _load_config(plan, context)
    config = context.config
    sections = plan.users[user].sections
    fields = deepcopy(sections.get("profile", {}))
    if fields.get("timezone") == "":
        fields["timezone"] = "UTC"
    arguments = _enrollment_args(sections)
    path = ["users", user, "profile"]
    try:
        with closing(connect_read_only(config.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            # These tables are required even when no values have been set yet.
            for table in ("user_profiles", "user_profile_managed_fields", "whatsapp_user_bindings", "web_auth_identities"):
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
            expected = _profile_state(conn, user)
            before = user_profiles.get_profile(config.db_path, user, conn=conn)
            binding = db.get_whatsapp_binding(conn, user)
            _validate_profile(conn, config, user, fields, before)
            provision = user_profiles.plan_user_provisioning(conn, user, fields, **arguments)
            _claim_identities(context, user, provision, before, binding)
            managed_before = user_profiles.managed_fields(conn, user)
    except user_profiles.ProvisioningRefused:
        raise ExecutionError("profile_refused", path) from None
    except (sqlite3.Error, OSError):
        raise ExecutionError("profile_store_unavailable_run_istota_init", path) from None

    context.profiles[user] = provision.profile
    uc = config.users.setdefault(user, UserConfig())
    user_profiles.merge_into_user_config(provision.profile, uc)
    managed_after = managed_before
    if provision.mode == "managed":
        managed_after = provision.asserted
    elif provision.mode == "seed":
        managed_after = set()
    changes = []
    for name, value in provision.updates.items():
        if before is None or not user_profiles._same_value(name, getattr(before, name), value):
            changes.append({"path": [name], "before": getattr(before, name) if before else None, "after": value})
    enrollment_changed = provision.whatsapp_op == "clear" and binding is not None
    if provision.whatsapp_op == "set":
        identity = db.prospective_whatsapp_identity(
            binding, bootstrap_phone_number=provision.whatsapp_number, bsuid=provision.whatsapp_bsuid)
        enrollment_changed = binding is None or identity != (binding.bootstrap_phone_number, binding.bsuid)
    if enrollment_changed:
        changes.append({"path": ["enrollment"], "after": "clear" if provision.whatsapp_op == "clear" else "<redacted>"})
    operations = []
    if before is None or changes:
        operations.append(Operation("profile", user, "create" if before is None else "update", user=user,
                                    changes=changes, expected_state=expected, restart_required=True))
    if not operations and managed_before == managed_after:
        return

    @contextmanager
    def transaction():
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def write(conn, operations):
        try:
            current = user_profiles.get_profile(config.db_path, user, conn=conn)
            _validate_profile(conn, config, user, fields, current)
            # Revalidate ownership under the same transaction as the writer.
            user_profiles.provision_user(config.db_path, user, fields, conn=conn, **arguments)
        except (user_profiles.ProvisioningRefused, ExecutionError):
            raise ExecutionError("state_changed", path) from None

    context.units.append(Unit("profile", user, operations, transaction,
                              lambda conn: _profile_state(conn, user), write, expected_state=expected))
