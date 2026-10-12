"""One-time import of a standalone install into the local VM's stack.

    istota migrate import-standalone --source DIR --hostname HOST
                                     [--vm-dir DIR] [--dry-run]

The standalone install (`install.sh --standalone`) keeps its state and its
files in one folder, `~/.istota`: the framework database `istota.db`, the
module databases under `modules/`, scratch in `tmp/`, the session transcripts
in `logs/`, the snapshots in `Backups/db/`, and the workspace itself (`Users/`,
`Channels/`, ...). Its `config.toml`, `istota.env` and `admins` are in
`~/.config/istota/`. That layout is restated below rather than read from
`setup_wizard`, whose standalone half is deleted once this has shipped.

The Mac's `istota-stack import-standalone` copies the workspace content into
the local VM's folder itself, and stages the rest for this module:

    MANIFEST          format, source_root (the standalone folder on the Mac),
                      mac_home, exported_at
    config.toml       the standalone config; admins beside it when there is one
    db/<date>/        online backups (`sqlite3 .backup`) in db_backup's
                      snapshot layout: framework/istota.db, <user>/<module>.db
    logs/             the session transcripts, when there are any
    workspace-users   the names under Users/, one per line

`istota.env` comes on stdin: it holds the master key and every credential, so
it is on no argv and in no staged file. It runs as uid 10001 in a one-shot
container with the stack down (`istota-stack import-standalone`), onto a VM
made with `create --no-setup`. In order, stopping at the first failure:

1. Checks everything before writing anything: the manifest, a config in the
   standalone layout, a home for every name `istota.env` holds, user ids that
   stay distinct on a case-insensitive folder (the rule `lib.user_ids` holds
   the stack to), no different master key and no work already there. An older
   standalone install has no `ISTOTA_SECRET_KEY`: when its database holds
   nothing encrypted under one (`credentials.store.count_master_key_rows`) the
   stack gets a new key, as `istota setup` makes one, and otherwise it is
   refused with the tables and their row counts.
2. Writes `config.toml` through `container_config_document` for the local VM
   (host folder, local ingress, sandbox on, the native login in place of
   `auth = "none"`), keeping the brain, email, CalDAV and briefing settings and
   the `[users.*]` blocks from the standalone file. A value under the
   standalone root is rebased; one outside it is dropped, since the VM cannot
   see it, and every setting that pointed outside is reported.
3. The master key into `/data/.secret_key`, the credentials into the stack's
   secrets files, the admins file, and `.env` and `host.env` into `--vm-dir`
   for `istota-stack` to install.
4. Restores the databases (`bare_metal_import.restore_snapshot`) and copies the
   transcripts.
5. In one transaction: rebases the stored paths by `bare_metal_import`'s rule
   and writes the `_migration` record under its own key.

Exit codes are `bare_metal_import`'s: 0 done (or already imported), 1 refused
with nothing written, 2 partial. A partial leaves
`/data/.standalone-import.in-progress`, and a re-run of the same import
resumes.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import sqlite3
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from istota import apply as apply_module
from istota import setup_wizard as wizard
from istota.lib import sqlite_util
from istota.maintenance import bare_metal_import as bmi

EXIT_OK = bmi.EXIT_OK
EXIT_REFUSED = bmi.EXIT_REFUSED
EXIT_PARTIAL = bmi.EXIT_PARTIAL

MANIFEST_FORMAT = "standalone-1"
RECORD_KEY = "standalone_import"
PROGRESS_NAME = ".standalone-import.in-progress"

#: The standalone layout under its root, as its `istota setup` wrote it.
STANDALONE_DB = "istota.db"
STANDALONE_MODULES = "modules"
STANDALONE_TEMP = "tmp"
STANDALONE_LOGS = "logs"

#: Where the local VM's stack keeps the same things.
WORKSPACE = wizard.CONTAINER_NEXTCLOUD_WORKSPACE
DB_DIR = str(Path(wizard.CONTAINER_DB_PATH).parent)

#: Carried from the standalone config, whole: the settings its user chose.
#: Everything else takes the stack's value or its default.
CARRIED = ("brain", "email", "caldav", "briefings", "default_briefings", "briefing_shared_blocks")

#: Keys the standalone layout fixes, which the stack's own values replace.
LAYOUT_KEYS = ("db_path", "workspace_path", "temp_dir", "module_data_dir", "users", "bot_name")

#: Credentials a carried section may hold inline, moved into the secrets file
#: the stack reads them from. `istota.env` outranks them, as at load.
INLINE_CREDENTIALS = {
    ("brain", "native", "api_key"): "istota_brain_native_api_key",
    ("email", "imap_password"): "istota_email_imap_password",
    ("email", "smtp_password"): "istota_email_smtp_password",
    ("caldav", "password"): "istota_caldav_password",
}

#: `istota.env` names with no place in the stack, and why.
ENV_LEFT = {
    "ISTOTA_WEB_INSECURE_COOKIES": "the stack sets its own",
    "ISTOTA_ADMINS_FILE": "the admins file is copied to /data/config/admins",
}

_LOOPBACK_URL = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::(\d+))?(?:/|$)")


class Refusal(bmi.Refusal):
    """Nothing was written."""


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


@dataclass
class Source:
    root: Path
    manifest: dict[str, str]
    config: dict
    admins: str
    snapshot: Path
    snapshot_dbs: list[str]
    workspace_users: list[str]

    @property
    def standalone_root(self) -> str:
        return self.manifest["source_root"].rstrip("/")

    @property
    def mac_home(self) -> str:
        return self.manifest.get("mac_home", "").rstrip("/")


def read_source(root: Path) -> Source:
    manifest: dict[str, str] = {}
    for line in bmi._read_text(root / "MANIFEST", "MANIFEST").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            manifest[key.strip()] = value.strip()
    if manifest.get("format") != MANIFEST_FORMAT:
        raise Refusal(f"MANIFEST format {manifest.get('format')!r}; this release reads {MANIFEST_FORMAT}")
    if not manifest.get("source_root", "").startswith("/"):
        raise Refusal("MANIFEST names no absolute source_root")
    try:
        config = tomllib.loads(bmi._read_text(root / "config.toml", "config.toml"))
    except tomllib.TOMLDecodeError as exc:
        raise Refusal(f"config.toml does not parse: {exc}") from None
    from istota.maintenance.db_backup import _dated_dirs

    dated = _dated_dirs(root / "db")
    if not dated:
        raise Refusal("the staging directory holds no database under db/<YYYY-MM-DD>/")
    snapshot = dated[0]
    dbs = sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*.db"))
    if "framework/istota.db" not in dbs:
        raise Refusal(f"{snapshot} has no framework/istota.db")
    admins_path = root / "admins"
    admins = admins_path.read_text(encoding="utf-8") if admins_path.exists() else ""
    users_path = root / "workspace-users"
    users = users_path.read_text(encoding="utf-8").split("\n") if users_path.exists() else []
    return Source(root, manifest, config, admins, snapshot, dbs, [u for u in users if u])


def _expand(value: str, home: str) -> str:
    if value == "~" or value.startswith("~/"):
        return home + value[1:]
    return value


def check_layout(source: Source) -> None:
    """The config describes the standalone layout under the root that was staged."""
    old, root, home = source.config, source.standalone_root, source.mac_home

    def path_of(key: str) -> str:
        value = old.get(key)
        return _expand(value, home).rstrip("/") if isinstance(value, str) else ""

    if path_of("workspace_path") != root:
        raise Refusal(
            f"config.toml's workspace_path is {old.get('workspace_path')!r}, not the standalone "
            f"folder {root}: this is not the layout `istota setup` writes for a standalone install"
        )
    if path_of("db_path") != f"{root}/{STANDALONE_DB}":
        raise Refusal(f"config.toml's db_path is {old.get('db_path')!r}, not {root}/{STANDALONE_DB}")
    if old.get("module_data_dir") and path_of("module_data_dir") != f"{root}/{STANDALONE_MODULES}":
        raise Refusal(f"config.toml's module_data_dir is {old.get('module_data_dir')!r}, outside {root}")


def profile_user_ids(db_path: Path) -> list[str]:
    conn = sqlite_util.connect_read_only(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT user_id FROM user_profiles")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def check_user_ids(config_users, profiles, workspace_users) -> None:
    """Refuse two names that are one directory on the Mac's folder.

    The local VM's workspace is a case-insensitive folder, so `Users/Alice`
    and `Users/alice` would be one user's files; the stack refuses such a pair
    at every load (`lib.user_ids`), so it is refused here before anything is
    written rather than at the first start.
    """
    from istota.lib.user_ids import user_id_index

    try:
        user_id_index([*config_users, *profiles, *workspace_users])
    except ValueError as exc:
        raise Refusal(
            f"{exc}: on the local VM's folder the two are one directory under Users/. "
            "Rename one in the standalone install first."
        ) from None


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------


@dataclass
class ConfigRewrite:
    document: dict
    user_id: str
    answers: wizard.ContainerAnswers
    changes: list[str] = field(default_factory=list)
    prefixes: list[tuple[str, str]] = field(default_factory=list)
    credentials: dict[str, str] = field(default_factory=dict)


def path_prefixes(root: str) -> list[tuple[str, str]]:
    """The standalone root's parts and where each is on the stack, longest first."""
    pairs = [
        (f"{root}/{STANDALONE_DB}", wizard.CONTAINER_DB_PATH),
        (f"{root}/{STANDALONE_MODULES}", f"{DB_DIR}/modules"),
        (f"{root}/{STANDALONE_LOGS}", f"{DB_DIR}/logs"),
        (f"{root}/{STANDALONE_TEMP}", wizard.CONTAINER_TEMP_DIR),
        (root, WORKSPACE),
    ]
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def _is_path(value) -> bool:
    return isinstance(value, str) and (value.startswith("/") or value == "~" or value.startswith("~/"))


def _under(value: str, root: str) -> bool:
    return value == root or value.startswith(root + "/")


def _primary_user(old: dict, admins: str) -> str:
    users = old.get("users") if isinstance(old.get("users"), dict) else {}
    for line in admins.splitlines():
        name = line.split("#", 1)[0].strip()
        if name in users:
            return name
    if users:
        return next(iter(users))
    raise Refusal("config.toml has no [users.<id>] block: there is no user to import")


def _answers(old: dict, user_id: str, hostname: str) -> wizard.ContainerAnswers:
    def section(name: str) -> dict:
        value = old.get(name)
        return value if isinstance(value, dict) else {}

    user = section("users").get(user_id) or {}
    brain, native = section("brain"), section("brain").get("native") or {}
    email, caldav = section("email"), section("caldav")
    disabled = set(user.get("disabled_modules") or [])
    kind = brain.get("kind") if brain.get("kind") in ("claude_code", "native") else "claude_code"
    return wizard.ContainerAnswers(
        bot_name=str(old.get("bot_name") or "Istota"),
        user_id=user_id,
        display_name=str(user.get("display_name") or ""),
        timezone=str(user.get("timezone") or "UTC"),
        hostname=hostname,
        ingress="local",
        host_folder=True,
        brain_kind=kind,
        native_base_url=str(native.get("base_url") or wizard.DEFAULT_ANTHROPIC_BASE_URL),
        native_model=str(native.get("model") or ""),
        email_enabled=bool(email.get("enabled")),
        imap_host=str(email.get("imap_host") or ""),
        imap_user=str(email.get("imap_user") or ""),
        smtp_host=str(email.get("smtp_host") or ""),
        caldav_url=str(caldav.get("url") or ""),
        caldav_username=str(caldav.get("username") or ""),
        location_enabled=bool(section("location").get("enabled")),
        money_enabled="money" not in disabled,
        health_enabled="health" not in disabled,
        feeds_enabled="feeds" not in disabled,
        briefings_enabled="briefings" not in disabled,
    )


def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        merged = dict(base)
        for key, value in over.items():
            merged[key] = _merge(base.get(key), value)
        return merged
    return copy.deepcopy(over)


def rewrite_config(old: dict, *, source: Source, hostname: str) -> ConfigRewrite:
    """The standalone config as the local VM's. Pure."""
    root, home = source.standalone_root, source.mac_home
    prefixes = path_prefixes(root)
    user_id = _primary_user(old, source.admins)
    answers = _answers(old, user_id, hostname)
    doc = wizard.container_config_document(answers, inline_credentials=False)
    changes: list[str] = []

    if _is_path(old.get("workspace_path")):
        changes.append(f"workspace: {old['workspace_path']} -> {WORKSPACE} (the folder on the Mac, shared into the VM)")
    web_auth = (old.get("web") or {}).get("auth") if isinstance(old.get("web"), dict) else None
    if web_auth in ("none", ["none"]):
        changes.append("web.auth: none -> email (the stack's own login; `istota-stack import-standalone` prints the link)")

    # Every setting that names a path outside the root, carried or not.
    for where, value in bmi._strings(old, ""):
        if _is_path(value) and not _under(_expand(value, home), root):
            changes.append(f"{where}: {value}: outside the standalone folder, which the VM cannot see; not carried")
        url = _LOOPBACK_URL.match(value) if isinstance(value, str) else None
        if url and where.split(".")[0] in CARRIED:
            port = url.group(1) or "80"
            changes.append(
                f"{where}: {value} is on the Mac's loopback; in the VM it is http://host.lima.internal:{port}, "
                f"which containers reach once you run `istota-stack allow-host-port {port}`"
            )

    credentials: dict[str, str] = {}
    carried = {key: copy.deepcopy(old[key]) for key in CARRIED if key in old}
    users = old.get("users") if isinstance(old.get("users"), dict) else {}
    carried_users = {uid: copy.deepcopy(block) for uid, block in users.items() if isinstance(block, dict)}
    for path, name in INLINE_CREDENTIALS.items():
        value = bmi._pop(carried, path)
        if value:
            credentials[name] = str(value)

    def outside(node) -> bool:
        return any(
            _is_path(value) and not _under(_expand(value, home), root)
            for _, value in bmi._strings(node, "")
        )

    def scrub(node):
        """`node` with paths under the root rebased, a key naming any other
        path dropped, and a list entry naming one dropped whole (a `[[...]]`
        table without its path is not the same setting); a list left empty
        goes too."""
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if _is_path(value):
                    rebased = bmi.rebase_path(_expand(value, home), prefixes)
                    if rebased is not None:
                        out[key] = rebased
                    continue
                if isinstance(value, list):
                    kept = [scrub(item) for item in value if not outside(item)]
                    if value and not kept:
                        continue
                    out[key] = kept
                    continue
                out[key] = scrub(value)
            return out
        if _is_path(node):
            return bmi.rebase_path(_expand(node, home), prefixes) or node
        return node

    for key, value in scrub(carried).items():
        doc[key] = _merge(doc.get(key), value) if isinstance(value, dict) else value
    for uid, block in scrub(carried_users).items():
        doc["users"][uid] = _merge(doc["users"].get(uid, {}), block)
    left = sorted(key for key in old if key not in CARRIED and key not in LAYOUT_KEYS)
    for key in left:
        changes.append(f"{key}: the stack's own value, not the standalone install's")
    return ConfigRewrite(doc, user_id, answers, changes, prefixes, credentials)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@dataclass
class SecretsPlan:
    keys: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    left: list[str] = field(default_factory=list)
    #: What became of the master key when istota.env had none.
    key_note: str = ""


def map_secrets(env: dict[str, str], inline: dict[str, str]) -> SecretsPlan:
    """Each `istota.env` name to where the stack reads it. Refuses a name with
    no home, by name: a credential is never dropped silently."""
    from istota.credentials.store import _MIN_KEY_LEN

    by_variable = {name.upper(): name for name in wizard.SECRET_NAMES}
    plan = SecretsPlan()
    unmapped = []
    for variable, value in env.items():
        if variable in bmi.KEY_FILES:
            plan.keys[bmi.KEY_FILES[variable]] = value
        elif variable in by_variable:
            plan.files[by_variable[variable]] = value
        elif variable in ENV_LEFT:
            plan.left.append(f"{variable}: {ENV_LEFT[variable]}")
        else:
            unmapped.append(variable)
    if unmapped:
        raise Refusal(
            "istota.env names " + ", ".join(sorted(unmapped)) + ", which has no place in the "
            "stack (its credentials are the files setup_wizard.SECRET_NAMES lists). Remove the "
            "line from istota.env, or move the value where the stack reads it. Nothing was written."
        )
    key = plan.keys.pop(".secret_key", "")
    if key and len(key) < _MIN_KEY_LEN:
        raise Refusal(
            f"istota.env's ISTOTA_SECRET_KEY is shorter than a usable key ({_MIN_KEY_LEN} characters); "
            "it cannot be the key the install used"
        )
    if key:
        plan.keys[".secret_key"] = key
    for name, value in inline.items():
        plan.files.setdefault(name, value)
    if not plan.files.get("istota_web_session_secret_key"):
        plan.files["istota_web_session_secret_key"] = secrets.token_hex(32)
    return plan


def settle_master_key(source: Source, target: bmi.Target, plan: SecretsPlan) -> None:
    """A master key for an install whose istota.env has none, or a refusal.

    Without a key the standalone could encrypt nothing, but a key once set and
    later removed from the file leaves rows only it opens, so the staged
    database is asked. A key already in `/data` (an earlier partial run's) is
    kept, since nothing here is encrypted under any key.
    """
    from istota.credentials import store as secrets_store

    if plan.keys.get(".secret_key"):
        return
    conn = sqlite_util.connect_read_only(source.snapshot / "framework" / "istota.db")
    try:
        encrypted = secrets_store.count_master_key_rows(conn)
    finally:
        conn.close()
    if encrypted:
        listing = ", ".join(
            f"{table} ({count} row{'' if count == 1 else 's'})" for table, count in sorted(encrypted.items())
        )
        raise Refusal(
            f"istota.env has no ISTOTA_SECRET_KEY, and the standalone database holds rows encrypted "
            f"under one: {listing}. Put the key the install used back in istota.env, or remove those "
            "credentials in the standalone install first"
        )
    existing = wizard._read_secret_file(target.data_dir / ".secret_key")
    if len(existing) >= secrets_store._MIN_KEY_LEN:
        plan.keys[".secret_key"] = existing
        plan.key_note = (
            "master key: none in istota.env and nothing encrypted under one; "
            "keeping the new master key an earlier run made"
        )
        return
    plan.keys[".secret_key"] = secrets.token_hex(32)
    plan.key_note = (
        "master key: none in istota.env and nothing encrypted under one; "
        "a new master key is made, as `istota setup` makes one"
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _record(source: Source, prefixes) -> str:
    return json.dumps({
        "prefixes": [list(pair) for pair in prefixes],
        "source_root": source.standalone_root,
        "mac_home": source.mac_home,
        "exported_at": source.manifest.get("exported_at", ""),
        "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def read_record(db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    conn = sqlite_util.connect_read_only(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM shared_kv WHERE namespace = ? AND key = ?",
            (bmi.MIGRATION_NAMESPACE, RECORD_KEY),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _scan(source: Source) -> list[str]:
    return [p for p in (source.standalone_root, source.mac_home) if p]


def _preflight(source: Source, target: bmi.Target, plan: SecretsPlan) -> None:
    progress = target.data_dir / PROGRESS_NAME
    resuming = progress.exists() and progress.read_text(encoding="utf-8").strip() == source.standalone_root
    if progress.exists() and not resuming:
        raise Refusal(f"{progress} names another import; finish or remove it first")
    if not resuming and target.db_path.exists() and bmi._task_count(target.db_path):
        raise Refusal(
            f"{target.db_path} already holds tasks: this stack has done work, and an import "
            "replaces its database and master key"
        )
    key_path = target.data_dir / ".secret_key"
    if key_path.exists() and key_path.read_text(encoding="utf-8").strip() != plan.keys[".secret_key"]:
        raise Refusal(f"{key_path} holds a different master key; an import goes onto a VM never set up")
    if not target.secrets_dir.is_dir() or not os.access(target.secrets_dir, os.W_OK | os.X_OK):
        raise Refusal(f"{target.secrets_dir} is not a writable directory; mount the stack's secrets there")
    from istota.maintenance.db_restore import _daemon_running

    if _daemon_running():
        raise Refusal("the scheduler daemon is running; stop the stack first")
    problems = bmi._quick_check(source.snapshot / "framework" / "istota.db")
    if problems:
        raise Refusal("the staged framework database fails quick_check: " + "; ".join(problems[:5]))


def _print_plan(rewrite: ConfigRewrite, section, plan: SecretsPlan, out) -> None:
    out("config:")
    for line in rewrite.changes:
        out(f"  {line}")
    for warning in section.warnings:
        out(f"  warning: {warning}")
    for line in section.diff:
        out(line)
    out("secrets:")
    for name in sorted(plan.files):
        out(f"  secrets/{name}")
    for name in sorted(plan.keys):
        if name == ".secret_key" and plan.key_note:
            out(f"  /data/{name} ({plan.key_note})")
        else:
            out(f"  /data/{name} (carried byte for byte)")
    for line in plan.left:
        out(f"  not carried: {line}")


def _write_stack_files(answers: wizard.ContainerAnswers, vm_dir: Path) -> None:
    for name, render in ((".env", wizard.render_stack_env), ("host.env", wizard.render_host_env)):
        path = vm_dir / name
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(render(answers, existing), encoding="utf-8")


def _finish_database(source: Source, target: bmi.Target, prefixes) -> bmi.DbReport:
    from istota import db

    conn = sqlite3.connect(target.db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        report = bmi.rebase_database(conn, prefixes, write=True, rehearsal=False, scan=_scan(source))
        db.shared_kv_set(conn, bmi.MIGRATION_NAMESPACE, RECORD_KEY, _record(source, prefixes), "import-standalone")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return report


def run(args, *, env_text: str, out, err) -> int:
    from istota.serve import parse_env_text

    target = bmi.Target(Path(args.config), Path(args.secrets_dir), Path(args.data_dir))
    vm_dir = Path(args.vm_dir) if args.vm_dir else None
    try:
        source = read_source(Path(args.source))
        record = read_record(target.db_path)
        if record is not None:
            out(f"already imported from {record.get('source_root')} at {record.get('imported_at')}; nothing to do")
            return EXIT_OK
        check_layout(source)
        rewrite = rewrite_config(source.config, source=source, hostname=args.hostname)
        plan = map_secrets(parse_env_text(env_text), rewrite.credentials)
        check_user_ids(
            list((source.config.get("users") or {}).keys()),
            profile_user_ids(source.snapshot / "framework" / "istota.db"),
            source.workspace_users,
        )
        settle_master_key(source, target, plan)
        try:
            section = apply_module.plan_config(rewrite.document, target.config_path)
        except apply_module.PlanError as exc:
            raise Refusal(f"the rewritten config does not validate: {exc}") from None
        if args.dry_run:
            _print_plan(rewrite, section, plan, out)
            conn = sqlite_util.connect_read_only(source.snapshot / "framework" / "istota.db")
            try:
                report = bmi.rebase_database(conn, rewrite.prefixes, write=False, rehearsal=False, scan=_scan(source))
            finally:
                conn.close()
            bmi._print_db_report(report, out)
            out("--dry-run: nothing was written.")
            return EXIT_OK
        _preflight(source, target, plan)
    except Refusal as exc:
        err(f"import-standalone: refused: {exc}")
        return EXIT_REFUSED

    progress = target.data_dir / PROGRESS_NAME
    try:
        target.data_dir.mkdir(parents=True, exist_ok=True)
        progress.write_text(source.standalone_root + "\n", encoding="utf-8")
        _print_plan(rewrite, section, plan, out)
        if section.changed:
            section.write()
        admins = source.admins if source.admins.strip() else f"{rewrite.user_id}\n"
        apply_module.write_private(target.config_path.parent / "admins", admins)
        for name, value in plan.keys.items():
            apply_module.write_private(target.data_dir / name, value)
        for name, value in plan.files.items():
            wizard._write_secret_file(target.secrets_dir / name, value)
        if vm_dir is not None:
            _write_stack_files(rewrite.answers, vm_dir)
        out("databases:")
        bmi.restore_snapshot(source.snapshot, source.snapshot_dbs, target, out)
        logs = source.root / STANDALONE_LOGS
        dst = target.db_path.parent / STANDALONE_LOGS
        if logs.is_dir() and not dst.exists():
            bmi._copy_tree(logs, dst)
            out(f"  copied logs/ -> {dst}")
        report = _finish_database(source, target, rewrite.prefixes)
        bmi._print_db_report(report, out)
        progress.unlink()
    except Exception as exc:  # noqa: BLE001 - a partial is reported, never raised
        err(f"import-standalone: PARTIAL: {exc}")
        err(f"import-standalone: {progress} is kept; run the import again to resume.")
        return EXIT_PARTIAL
    out(f"imported the standalone install at {source.standalone_root}.")
    out(
        "Files of your own that name the old folder (CRON.md, scripts) are not rewritten; "
        f"once the stack runs: istota migrate workspace-paths --map {source.standalone_root}={WORKSPACE}"
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="istota migrate import-standalone",
        description="Import a standalone install, staged by the Mac's istota-stack, "
                    "with its istota.env on stdin.",
    )
    parser.add_argument("--source", required=True, help="The staged directory.")
    parser.add_argument("--hostname", required=True, help="The stack's [site] hostname, localhost:<port>.")
    parser.add_argument(
        "--config", default=os.environ.get("ISTOTA_CONFIG_PATH") or "/data/config/config.toml",
        help="The config.toml to write.",
    )
    parser.add_argument("--vm-dir", help="Where to stage .env and host.env for istota-stack.")
    parser.add_argument("--secrets-dir", help="The stack's secrets directory (default: <vm-dir>/secrets).")
    parser.add_argument("--data-dir", default=str(wizard.CONTAINER_DATA_DIR), help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and write nothing.")
    return parser


def main(argv: list[str] | None = None, *, stdin=None, out=None, err=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not args.secrets_dir:
        if not args.vm_dir:
            parser.print_usage(sys.stderr)
            return 64
        args.secrets_dir = str(Path(args.vm_dir) / "secrets")
    out_stream = out if out is not None else sys.stdout
    err_stream = err if err is not None else sys.stderr
    env_text = (stdin if stdin is not None else sys.stdin).read()
    return run(
        args, env_text=env_text,
        out=lambda line: print(line, file=out_stream),
        err=lambda line: print(line, file=err_stream),
    )


if __name__ == "__main__":
    raise SystemExit(main())
