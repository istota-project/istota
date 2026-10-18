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
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan


def configure(config_path, settings, key_data):
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
    key = serialization.load_pem_private_key(key_data, password=None)
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
        document["push"] = {**document.get("push", {}), **settings, "apns_key_path": str(key_path)}
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
            result = engine.execute_plan(operations, context)
        except Exception:
            # A committed key replacement still needs a restart after recovery.
            return 3
        if result.errors:
            return 3 if key_changed or result.changed else 1
        return 2 if key_changed or result.changed else 0


def main():
    try:
        inputs = Path(sys.argv[1])
        return configure(Path('/data/config/config.toml'), json.loads((inputs / 'settings.json').read_text()),
                         (inputs / 'AuthKey.p8').read_bytes())
    except Exception:
        print('Push configuration refused; check the settings, signing key and deployed push support.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
