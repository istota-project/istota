"""Apply push settings from the live config under the settings writer's lock."""
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from istota import apply
from istota.config import PushConfig
from istota.lib.atomic_write import write_bytes_atomic
from istota.push import envelope
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan


def configure(config_path, settings, key_data, *, replace_signing_key=False):
    allowed = {field.name for field in dataclasses.fields(PushConfig)} - {"apns_key_path"}
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError("Unknown push setting")
    if type(settings.get("enabled")) is not bool:
        raise ValueError("Set enabled explicitly")
    for name in ("apns_key_id", "apns_team_id"):
        if not re.fullmatch(r"[A-Z0-9]{10}", str(settings.get(name, ""))):
            raise ValueError("Invalid APNs identifier")
    if not isinstance(settings.get("apns_topic"), str) or not settings["apns_topic"].strip():
        raise ValueError("Missing APNs topic")
    if importlib.util.find_spec("h2") is None:
        raise ValueError("The deployed image lacks HTTP/2 support")
    try:
        key = serialization.load_pem_private_key(key_data, password=None)
    except (ValueError, TypeError):
        raise ValueError("The APNs key is not an unencrypted PEM key") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("APNs requires a P-256 signing key")
    key_dir = config_path.parent / "apns"
    key_path = key_dir / "AuthKey.p8"
    override = os.environ.get("ISTOTA_APNS_KEY_PATH", "").strip()
    if override and override != str(key_path):
        raise ValueError("ISTOTA_APNS_KEY_PATH points elsewhere")
    if key_dir.is_symlink() or key_path.is_symlink():
        raise ValueError("The APNs destination must not be a symlink")
    with engine.apply_lock(config_path):
        document = tomllib.loads(config_path.read_text())
        live = dict(document.get("push", {}))
        document["push"] = {**live, **settings, "apns_key_path": str(key_path)}
        signing_key = _signing_key_path(config_path, document["push"])
        repairs = set()
        if signing_key is not None:
            repairs = _signing_key_repairs(signing_key)
            was_private = (live.get("content") == "private"
                           and str(live.get("signing_key_path", "")).strip() == str(signing_key))
            if "create" in repairs and was_private and not replace_signing_key:
                raise ValueError(_LOST_KEY)
        # snapshot preserves the current config-management marker.
        plan = parse_plan({"meta": {"version": 1, "source": "snapshot"}, "config": document})
        context = engine.Context(config_path, backup=10)
        operations = engine.build_plan(plan, context, context.options)
        key_changed = not key_path.exists()
        if not key_changed:
            info = key_path.stat()
            key_changed = (key_path.read_bytes() != key_data or stat.S_IMODE(info.st_mode) != 0o600
                           or info.st_uid != os.geteuid())
        try:
            if key_changed:
                key_dir.mkdir(mode=0o700, exist_ok=True)
                apply.write_private(key_path, key_data)
            _repair_signing_key(signing_key, repairs)
            result = engine.execute_plan(operations, context)
        except Exception:
            # A committed key replacement still needs a restart after recovery.
            return 3
        written = key_changed or bool(repairs)
        if result.errors:
            return 3 if written or result.changed else 1
        return 2 if written or result.changed else 0


_LOST_KEY = (
    "The push signing key and its .pub are missing although the config already uses them. "
    "A new key makes every enrolled device enroll again. Restore the pair from a backup, "
    "or set istota_push_replace_signing_key: true for one run to create a new one."
)


def _signing_key_path(config_path, push):
    """Where the private push signing key belongs, or None when push stays legacy.

    `content = "private"` with no path gets one in the config directory, the
    only place this container may write, as the APNs key does.
    """
    if push.get("content", "legacy") != "private":
        return None
    path = str(push.get("signing_key_path", "")).strip()
    if not path:
        path = str(config_path.parent / "push" / "signing.pem")
        push["signing_key_path"] = path
    override = os.environ.get("ISTOTA_PUSH_SIGNING_KEY_PATH", "").strip()
    if override and override != path:
        raise ValueError("ISTOTA_PUSH_SIGNING_KEY_PATH points elsewhere")
    path = Path(path)
    if path.parent.is_symlink() or path.is_symlink() or envelope.public_key_path(path).is_symlink():
        raise ValueError("The push signing key destination must not be a symlink")
    return path


def _signing_key_repairs(path):
    """What the key pair at `path` needs: a subset of create, public and chmod.

    A pair that is broken in any other way is refused, never replaced:
    replacing a key makes every enrolled device enroll again, so that stays
    `istota push keygen --force`, an operator's deliberate step.
    """
    public = envelope.public_key_path(path)
    if not path.exists() and not public.exists():
        return {"create"}
    repairs = set()
    try:
        raw = envelope.load_signing_key(path).public_key().public_bytes_raw()
        if not public.exists():
            repairs.add("public")
        elif envelope.load_public_key(public) != raw:
            raise ValueError("The push public key file does not match the signing key")
    except envelope.KeyFileError as exc:
        raise ValueError(str(exc)) from None
    info = path.stat()
    if info.st_uid != os.geteuid():
        raise ValueError("The push signing key is owned by another account")
    if stat.S_IMODE(info.st_mode) != 0o600:
        repairs.add("chmod")
    return repairs


def _repair_signing_key(path, repairs):
    if "create" in repairs:
        envelope.write_signing_key(path)
        return
    if "chmod" in repairs:
        os.chmod(path, 0o600)
    if "public" in repairs:
        line = envelope.public_key_line(envelope.load_signing_key(path))
        write_bytes_atomic(envelope.public_key_path(path), line.encode("ascii"), mode=0o644, fsync=True)


def main():
    try:
        inputs = Path(sys.argv[1])
        return configure(Path('/data/config/config.toml'), json.loads((inputs / 'settings.json').read_text()),
                         (inputs / 'AuthKey.p8').read_bytes(),
                         replace_signing_key=(inputs / 'replace-signing-key').exists())
    except ValueError as exc:
        # Every refusal above names a condition, never a value from a key or the config.
        print(f'Push configuration refused: {exc}', file=sys.stderr)
        return 1
    except Exception:
        print('Push configuration refused; check the settings, signing key and deployed push support.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
