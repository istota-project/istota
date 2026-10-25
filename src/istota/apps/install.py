"""The install store: pending installs, approval, versions, uninstall, grants and starter apps.

A draft becomes a *pending install* when the host reads it, validates its
manifest and builds its document; the pending install is the host's own copy,
so later edits to the folder change nothing. *Approve* turns it into the
app's next installed version and makes that current. Grants are stored per
version, so a rollback brings the old version's grants back with it.

Grants are keyed by the capability's full text as the manifest requests it
(``files:read:notes``, not the family ``files:read``), so an app may hold two
scopes of one family. ``scope_json`` holds the scope the bridge checks against:
the manifest's qualifier, or a folder the user chose instead for a
``files:read`` scope (see :func:`grant`). An unscoped capability's scope is
``null``.

Transactions: these functions do not commit ``conn``; the caller does.
:func:`ensure_starter_apps` and :func:`reinstall_starter` are the exception and
commit after each app, so one starter app that fails is rolled back alone;
call them with no transaction of your own open. :func:`uninstall` with
``delete_data`` commits its KV deletion on the framework database itself.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat

from istota import db as framework_db
from istota.apps import db as apps_db
from istota.apps.capabilities import (
    ALWAYS_GRANTED,
    CapabilityError,
    parse_capability,
    split_capability,
)
from istota.apps.document import BuiltDocument, build_document
from istota.apps.drafts import read_draft
from istota.apps.kit import Kit, KitMissing, load_kit, load_starter_files
from istota.apps.manifest import Manifest, ManifestError, parse_manifest
from istota.sandbox.confined import open_dir

logger = logging.getLogger(__name__)

#: The starter apps every user with Apps enabled is given, by id. Each is a
#: folder under ``starter/`` in the apps build. Empty until Board lands.
STARTER_APPS: tuple[str, ...] = ()

ORIGINS = ("draft", "starter")
MANIFEST = "manifest.toml"
DATA = "data"


class NotFound(LookupError):
    """No such app, version or pending install."""


class StaleApproval(Exception):
    """The approval named a different set of new capabilities than the pending install has.

    ``expected`` is the set the screen should have shown.
    """

    def __init__(self, expected: list[str]):
        super().__init__("The capabilities this install asks for have changed. Reopen it to review them.")
        self.expected = expected


class CurrentVersionError(ValueError):
    """The current version cannot be deleted; roll back or uninstall instead."""


class GrantError(ValueError):
    """A grant the first-open screen asked for that the host will not make."""


class UninstallError(Exception):
    """The app's data could not be removed; nothing about the app was changed after that point."""


def kv_namespace(app_id: str) -> str:
    return f"app.{app_id}"


def manifest_record(manifest: Manifest) -> dict:
    """What ``manifest_json`` stores: the parsed manifest, capabilities as their full text."""
    return {
        "id": manifest.id,
        "name": manifest.name,
        "icon": manifest.icon,
        "entry": manifest.entry,
        "description": manifest.description,
        "capabilities": [cap.text for cap in manifest.capabilities],
        "narrowest": manifest.narrowest,
        "reason": manifest.reason,
    }


def _build(config, files: dict[str, bytes], app_id: str, kit: Kit) -> tuple[Manifest, BuiltDocument]:
    raw = files.get(MANIFEST)
    if raw is None:
        raise ManifestError([f"The app folder has no {MANIFEST}."])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError([f"{MANIFEST} is not UTF-8 text."]) from exc
    manifest = parse_manifest(text, folder_name=app_id, bot_dir=config.bot_dir_name)
    built = build_document(files, manifest, kit, max_bytes=config.apps.bundle_max_bytes)
    return manifest, built


# -- pending installs and approval --------------------------------------------


def create_pending(
    config,
    user_id: str,
    conn,
    app_id: str,
    *,
    origin: str,
    created_by_task: int | None = None,
    task_read_untrusted: bool | None = None,
) -> str:
    """Read the draft ``app_id``, build it and store it as the app's pending install. Returns its id.

    Replaces any pending install the app already had. Raises the reader's and
    builder's own errors unchanged: ``DraftNotFound`` / ``DraftError``,
    ``ManifestError``, ``DocumentError`` (each with the sentences to show) and
    ``KitMissing``.
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}")
    files = read_draft(config, user_id, app_id)
    manifest, built = _build(config, files, app_id, load_kit())
    return apps_db.replace_pending(
        conn,
        app_id=app_id,
        document=built.html,
        manifest=manifest_record(manifest),
        source=built.source,
        script_hashes=list(built.script_hashes),
        csp=built.csp,
        warnings=list(manifest.warnings) + list(built.warnings),
        origin=origin,
        created_by_task=created_by_task,
        task_read_untrusted=task_read_untrusted,
    )


def _current_grants(conn, app_id: str) -> dict[str, object]:
    app = apps_db.get_app(conn, app_id)
    if app is None or app["current_version"] is None:
        return {}
    return apps_db.get_grants(conn, app_id, app["current_version"])


def new_capabilities(conn, app_id: str, manifest: dict) -> list[str]:
    """The capabilities ``manifest`` requests that the app's current version is not granted.

    In the manifest's order. The two every app is granted are never new.
    """
    granted = _current_grants(conn, app_id)
    return [
        cap for cap in manifest["capabilities"]
        if cap not in ALWAYS_GRANTED and cap not in granted
    ]


def _grants_for(requested: list[str], carried: dict[str, object]) -> dict[str, object]:
    """Grants for a new version: what ``carried`` holds for a capability it requests, else its own scope."""
    grants = {}
    for cap in requested:
        if cap in carried:
            grants[cap] = carried[cap]
        else:
            grants[cap] = split_capability(cap).scope
    return grants


def approve(conn, pending_id: str, *, new_capabilities_ack: list[str]) -> int:
    """Install the pending install as the app's next version and make it current. Returns the version.

    ``new_capabilities_ack`` is the list of new capabilities the approval
    screen showed; it must name exactly the ones :func:`new_capabilities`
    finds now (in any order), else :class:`StaleApproval` and nothing changes.
    Every capability the version requests is granted, keeping the scope the
    current version holds for one it already had.
    """
    pending = apps_db.get_pending(conn, pending_id)
    if pending is None:
        raise NotFound("That install is no longer waiting. Reopen it from the app's folder.")
    app_id = pending["app_id"]
    manifest = pending["manifest"]
    new = new_capabilities(conn, app_id, manifest)
    ack = new_capabilities_ack if isinstance(new_capabilities_ack, list) else None
    if ack is None or not all(isinstance(cap, str) for cap in ack) or sorted(set(ack)) != sorted(new):
        raise StaleApproval(new)

    carried = _current_grants(conn, app_id)
    apps_db.upsert_app(
        conn, app_id,
        name=manifest["name"], icon=manifest["icon"], description=manifest["description"],
        origin=pending["origin"],
    )
    version = apps_db.insert_version(
        conn, app_id,
        document=pending["document"],
        manifest=manifest,
        source=pending["source"],
        narrowest=manifest["narrowest"],
        script_hashes=pending["script_hashes"],
        csp=pending["csp"],
        warnings=pending["warnings"],
        created_by_task=pending["created_by_task"],
        task_read_untrusted=pending["task_read_untrusted"],
        approved_at=apps_db.sqlite_timestamp(),
    )
    apps_db.set_grants(conn, app_id, version, _grants_for(manifest["capabilities"], carried))
    apps_db.set_current_version(conn, app_id, version)
    apps_db.delete_pending(conn, pending_id)
    apps_db.gc_documents(conn)
    return version


# -- versions -----------------------------------------------------------------


def _require_version(conn, app_id: str, version: int) -> dict:
    app = apps_db.get_app(conn, app_id)
    if app is None:
        raise NotFound(f"There is no app {app_id}.")
    if apps_db.get_version(conn, app_id, version) is None:
        raise NotFound(f"{app_id} has no version {version}.")
    return app


def rollback(conn, app_id: str, version: int) -> None:
    """Make an installed ``version`` current again; its own grants come with it."""
    _require_version(conn, app_id, version)
    apps_db.set_current_version(conn, app_id, version)


def delete_version(conn, app_id: str, version: int) -> None:
    """Delete a version that is not current, its grants, and its document if nothing else uses it."""
    app = _require_version(conn, app_id, version)
    if app["current_version"] == version:
        raise CurrentVersionError(f"Version {version} is the current version of {app_id}.")
    apps_db.delete_version(conn, app_id, version)
    apps_db.gc_documents(conn)


# -- grants -------------------------------------------------------------------


def ungranted(conn, app_id: str) -> list[str]:
    """What the current version's manifest requests and is not granted, in the manifest's order."""
    app = apps_db.get_app(conn, app_id)
    if app is None or app["current_version"] is None:
        return []
    version = apps_db.get_version(conn, app_id, app["current_version"])
    granted = apps_db.get_grants(conn, app_id, app["current_version"])
    return [cap for cap in version["manifest"]["capabilities"] if cap not in granted]


def grant(conn, app_id: str, version: int, capability: str, scope: str | None, *, bot_dir: str) -> None:
    """Grant one capability the current version's manifest requests (the first-open screen).

    ``capability`` is the requested text. ``scope`` ``None`` keeps the
    manifest's own qualifier. Only a ``files:read`` scope may be changed, to
    another folder that passes the same rule (``bot_dir`` is what that rule
    refuses); any other scope must equal the manifest's.
    """
    app = apps_db.get_app(conn, app_id)
    if app is None:
        raise NotFound(f"There is no app {app_id}.")
    if app["current_version"] != version:
        raise GrantError("Only the app's current version can be granted anything. Reload the app.")
    row = apps_db.get_version(conn, app_id, version)
    if capability not in row["manifest"]["capabilities"]:
        raise GrantError(f"{app_id} does not ask for {capability}.")
    requested = split_capability(capability)
    if scope is None or scope == requested.scope:
        scope = requested.scope
    elif requested.name == "files:read" and isinstance(scope, str):
        try:
            scope = parse_capability(f"files:read:{scope}", bot_dir=bot_dir).scope
        except CapabilityError as exc:
            raise GrantError(str(exc)) from exc
    else:
        raise GrantError(f"The scope of {capability} cannot be changed.")
    apps_db.put_grant(conn, app_id, version, capability, scope)


# -- uninstall ----------------------------------------------------------------


def _delete_data_folder(config, user_id: str, app_id: str) -> None:
    """Remove ``{bot_dir}/apps/<app_id>/data/``, walked by descriptor with no link followed.

    A link anywhere on the way, or a ``data`` that is itself a link, removes
    nothing outside the folder: the walk refuses the first, and ``rmtree`` with
    ``dir_fd`` unlinks links below the top rather than following them. A
    ``data`` that is a link or a file is left where it is.
    """
    root = config.workspace_root(user_id)
    if root is None:
        return
    fd = open_dir(root, config.bot_dir_name, "apps", app_id)
    if fd is None:
        return
    try:
        try:
            mode = os.stat(DATA, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(mode):
            logger.warning("apps_uninstall_data_not_a_folder app=%s", app_id)
            return
        try:
            shutil.rmtree(DATA, dir_fd=fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            logger.warning(
                "apps_uninstall_data_failed app=%s error=%s",
                app_id, getattr(exc, "errno", None) or type(exc).__name__,
            )
            raise UninstallError(f"The data folder of {app_id} could not be removed.") from exc
    finally:
        os.close(fd)


def uninstall(config, user_id: str, conn, app_id: str, *, delete_data: bool) -> None:
    """Remove the app's rows from ``apps.db``; with ``delete_data``, its KV namespace and ``data/`` too.

    The draft folder is never touched. A starter app is marked dismissed so it
    is not seeded again. Data goes first, so a removal that fails
    (:class:`UninstallError`) leaves the app installed for another try.
    """
    app = apps_db.get_app(conn, app_id)
    if app is None:
        raise NotFound(f"There is no app {app_id}.")
    if delete_data:
        with framework_db.get_db(config.db_path) as fw:
            framework_db.kv_delete_namespace(fw, user_id, kv_namespace(app_id))
        _delete_data_folder(config, user_id, app_id)
    if app["origin"] == "starter":
        state = apps_db.get_starter_state(conn, app_id) or {}
        apps_db.set_starter_state(
            conn, app_id,
            installed_hash=state.get("installed_hash"),
            dismissed_at=apps_db.sqlite_timestamp(),
        )
    apps_db.delete_app(conn, app_id)


# -- starter apps -------------------------------------------------------------


def _seed_starter(config, conn, app_id: str, kit: Kit) -> str:
    state = apps_db.get_starter_state(conn, app_id)
    if state is not None and state["dismissed_at"] is not None:
        return "dismissed"
    app = apps_db.get_app(conn, app_id)
    if app is not None and app["origin"] != "starter":
        return "conflict"
    manifest, built = _build(config, load_starter_files(app_id), app_id, kit)
    record = manifest_record(manifest)

    current = None
    if app is not None and app["current_version"] is not None:
        current = apps_db.get_version(conn, app_id, app["current_version"])
    if current is not None and current["content_hash"] == built.content_hash:
        status = "current"
    else:
        if current is None:
            carried = {cap: None for cap in ALWAYS_GRANTED}
            status = "installed"
        else:
            carried = apps_db.get_grants(conn, app_id, current["version"])
            status = "upgraded"
        apps_db.upsert_app(
            conn, app_id,
            name=manifest.name, icon=manifest.icon, description=manifest.description,
            origin="starter",
        )
        version = apps_db.insert_version(
            conn, app_id,
            document=built.html,
            manifest=record,
            source=built.source,
            narrowest=manifest.narrowest,
            script_hashes=list(built.script_hashes),
            csp=built.csp,
            warnings=list(manifest.warnings) + list(built.warnings),
        )
        # Only what both versions request carries over; a capability new in
        # this build is asked for on next open.
        grants = {cap: carried[cap] for cap in record["capabilities"] if cap in carried}
        apps_db.set_grants(conn, app_id, version, grants)
        apps_db.set_current_version(conn, app_id, version)
        apps_db.gc_documents(conn)
    apps_db.set_starter_state(conn, app_id, installed_hash=built.content_hash, dismissed_at=None)
    return status


def ensure_starter_apps(config, user_id: str, conn, *, only: str | None = None) -> dict[str, str]:
    """Install or upgrade each starter app the user has not dismissed. Returns a status per app.

    ``installed`` (version 1, only ``kv:own`` and ``files:own`` granted),
    ``upgraded`` (the packaged build changed), ``current``, ``dismissed``
    (the user uninstalled it), ``conflict`` (an app the user installed holds
    the id), ``unavailable`` (the apps build is missing or does not match its
    manifest) or ``failed``. Commits after each app; a failure is logged and
    rolled back for that app alone.
    """
    ids = [app_id for app_id in STARTER_APPS if only is None or app_id == only]
    statuses: dict[str, str] = {}
    if not ids:
        return statuses
    try:
        kit = load_kit()
    except KitMissing as exc:
        logger.warning("apps_starter_unavailable user=%s reason=%s", user_id, exc.reason)
        kit = None
    for app_id in ids:
        try:
            if kit is None:
                state = apps_db.get_starter_state(conn, app_id)
                dismissed = state is not None and state["dismissed_at"] is not None
                statuses[app_id] = "dismissed" if dismissed else "unavailable"
                continue
            statuses[app_id] = _seed_starter(config, conn, app_id, kit)
            conn.commit()
        except KitMissing as exc:
            conn.rollback()
            logger.warning("apps_starter_unavailable user=%s app=%s reason=%s", user_id, app_id, exc.reason)
            statuses[app_id] = "unavailable"
        except Exception as exc:
            conn.rollback()
            logger.warning("apps_starter_failed user=%s app=%s error=%s", user_id, app_id, type(exc).__name__)
            statuses[app_id] = "failed"
    return statuses


def reinstall_starter(config, user_id: str, conn, app_id: str) -> str:
    """Clear a starter app's dismissal and seed it again. Returns its status, as :func:`ensure_starter_apps`."""
    if app_id not in STARTER_APPS:
        raise NotFound(f"{app_id} is not an app that ships with Istota.")
    apps_db.set_starter_state(conn, app_id, installed_hash=None, dismissed_at=None)
    conn.commit()
    return ensure_starter_apps(config, user_id, conn, only=app_id)[app_id]
