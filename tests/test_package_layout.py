"""The shape of `src/istota/`: what may sit at the root, and what a new package may hold.

The root grew one file per concern because nothing stopped it. These guards
are what make adding to it a visible decision (spec: src-package-layout):

- **The root allowlist.** Every module directly under `src/istota/` is listed
  here, and every listed name exists. Each stage of the reorganisation shrinks
  the list in the same commit as its move; a new root module is a one-line
  addition somebody has to write and a reviewer can see.
- **New packages hold nothing in `__init__.py`** but a docstring. Several moved
  modules sit at the root today only because of import cost (the tool server
  must not import `istota.skills`), and an `__init__` with imports would add to
  every importer of every module in the package.
- **`lib/` imports nothing from `istota`**, at any depth, function-scope
  imports included. That is what the package is: dependency-free leaves.
- **Every module imports.** One subprocess imports each module of the package
  in turn, dropping the `istota` modules it loaded before the next one, so a
  module that only imports because something else loaded first, or a cycle a
  move introduced, fails here rather than in whichever test happened to import
  it first. A module whose optional extra is not installed is skipped, the
  way the suite skips it.

These read files off disk, so testmon cannot see what they depend on; they are
in the residual `AGENTS.md` names and run in every full pass.
"""

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
PACKAGE = SRC / "istota"


def _mover():
    spec = importlib.util.spec_from_file_location("move_modules_layout", REPO / "scripts" / "move_modules.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["move_modules_layout"] = module
    spec.loader.exec_module(module)
    return module


mm = _mover()

#: Every module allowed directly under src/istota/. Shrinks stage by stage; the
#: modules that stay are the spine and the process entry points.
ROOT_ALLOWLIST = frozenset({
    "__init__",
    "admin_browsers",
    "admin_config_view",
    "admin_logs",
    "async_runtime",
    "atomic_write",
    "avatars",
    "brain_availability",
    "browser_admission",
    "browser_owner",
    "build_info",
    "chat_files",
    "claude_runtime_env",
    "cli",
    "cli_briefings",
    "cli_money",
    "commands",
    "config",
    "config_mapper",
    "confirmations",
    "context",
    "credential_shim",
    "cron_loader",
    "date_parse",
    "db",
    "db_backup",
    "db_backup_relocate",
    "db_health",
    "db_relocate",
    "db_restore",
    "devbox_exec_client",
    "devbox_exec_protocol",
    "devbox_peer",
    "devbox_proxy",
    "devbox_proxy_protocol",
    "doctor",
    "du",
    "email_ownership",
    "email_support",
    "events",
    "executor",
    "executor_stream",
    "experimental",
    "file_lock",
    "filenames",
    "forge_bin",
    "forge_cli",
    "garmin_routes",
    "geo",
    "git_hardening",
    "git_remote_scrub",
    "google_scopes",
    "heartbeat",
    "host_pressure",
    "http_headers",
    "image_attachments",
    "image_sniff",
    "kv_namespaces",
    "llm_json",
    "local_credentials",
    "location_logic",
    "logging_setup",
    "map_basemap",
    "message_relays",
    "module_loader",
    "modules",
    "net_guard",
    "network_proxy",
    "nextcloud_api",
    "nextcloud_client",
    "notification_sources",
    "notification_store",
    "notifications",
    "ntfy_headers",
    "ocr_leaf",
    "ocs",
    "outbound_drafts",
    "outbound_policy",
    "peer_process",
    "process_group",
    "provision_rooms",
    "rclone_client",
    "relay_destinations",
    "repos_relocate",
    "retry_after",
    "room_colors",
    "room_mount_reconcile",
    "room_policy",
    "room_relocate",
    "room_scopes",
    "room_veto",
    "sandbox_cache_sweeper",
    "sandbox_plan",
    "scheduler",
    "scheduler_deferred",
    "secret_schema",
    "secrets_store",
    "secrets_vault",
    "serve",
    "setup_wizard",
    "shared_blocks_store",
    "shared_file_organizer",
    "shell_exec",
    "side_rooms",
    "skill_client",
    "skill_host_paths",
    "skill_proxy",
    "speech_gate",
    "sqlite_util",
    "static_dir",
    "status_writer",
    "storage",
    "subscription_usage",
    "surfaces",
    "talk",
    "task_cgroup",
    "task_env",
    "tasks_file_poller",
    "timestamps",
    "toml_fence",
    "tool_server",
    "tool_server_protocol",
    "unix_server",
    "untrusted",
    "updater",
    "usage",
    "usage_render",
    "user_briefings",
    "user_profiles",
    "user_scope",
    "web_app",
    "web_auth",
    "web_auth_mail",
    "web_origin",
    "web_router_stubs",
    "web_session_secret",
    "web_shutdown",
    "web_tokens",
    "webhook_receiver",
    "whatsapp_requests",
    "worktree_reaper",
})

#: Packages that existed before the reorganisation; the `__init__` rule is not theirs.
EXISTING_PACKAGES = frozenset({"istota", "istota.nextcloud", "istota.location", "istota.briefings"})


def _root_modules() -> set[str]:
    return {path.stem for path in PACKAGE.glob("*.py")}


class TestTheRootAllowlist:
    def test_every_root_module_is_allowlisted(self):
        unlisted = sorted(_root_modules() - ROOT_ALLOWLIST)
        assert unlisted == [], (
            "new module(s) at the package root; put them in the subsystem package "
            f"they belong to, or add them to ROOT_ALLOWLIST deliberately: {unlisted}"
        )

    def test_every_allowlisted_module_exists(self):
        """A stale entry would let the next module of that name back in unseen."""
        missing = sorted(ROOT_ALLOWLIST - _root_modules())
        assert missing == [], f"remove these from ROOT_ALLOWLIST: {missing}"


def _created_packages() -> set[str]:
    news = {new for _, _, new in mm.MOVES}
    return {new.rsplit(".", 1)[0] for new in news} - EXISTING_PACKAGES - news


def _docstring_only(path: Path) -> bool:
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if not body:
        return True
    return len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
        and isinstance(body[0].value.value, str)


class TestNewPackages:
    def test_the_set_is_the_specs(self):
        assert _created_packages() == {
            f"istota.{name}" for name in (
                "lib", "rooms", "relay", "sandbox", "credentials", "devbox",
                "notifications", "usage", "webui", "maintenance", "mail", "browser",
            )
        }

    def test_each_init_is_empty_or_a_docstring(self):
        offenders = []
        for package in sorted(_created_packages()):
            init = mm.module_dir(REPO, package) / "__init__.py"
            if init.is_file() and not _docstring_only(init):
                offenders.append(str(init.relative_to(REPO)))
        assert offenders == []

    def test_the_docstring_check_tells_the_two_apart(self, tmp_path):
        empty, doc, code = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"
        empty.write_text("")
        doc.write_text('"""Rooms."""\n')
        code.write_text('"""Rooms."""\nfrom . import policy\n')
        assert _docstring_only(empty) and _docstring_only(doc)
        assert not _docstring_only(code)


def _istota_imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name == "istota" or a.name.startswith("istota.")]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or module == "istota" or module.startswith("istota."):
                found.append("." * node.level + module)
    return found


def _lib_files() -> list[Path]:
    """Every module of lib/: the stage's rows wherever they sit now, plus anything added."""
    files = set()
    for stage, old, new in mm.MOVES:
        if stage != "lib":
            continue
        for dotted in (new, old):
            path = mm.module_file(REPO, dotted)
            if path.is_file():
                files.add(path)
                break
    lib = PACKAGE / "lib"
    if lib.is_dir():
        files.update(lib.rglob("*.py"))
    return sorted(files)


class TestLib:
    def test_every_lib_row_is_found(self):
        assert len(_lib_files()) >= sum(1 for stage, _, _ in mm.MOVES if stage == "lib")

    def test_lib_imports_nothing_from_istota(self):
        offenders = {
            str(path.relative_to(REPO)): imports
            for path in _lib_files() if (imports := _istota_imports(path))
        }
        assert offenders == {}

    def test_the_import_walk_sees_a_function_scope_import(self, tmp_path):
        path = tmp_path / "leaf.py"
        path.write_text("import os\n\ndef f():\n    from istota import db\n    from . import x\n")
        assert _istota_imports(path) == ["istota", "."]


SWEEP = r"""
import importlib, json, sys
names = json.load(sys.stdin)
failures, skipped = [], []
for name in names:
    before = set(sys.modules)
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and missing != "istota" and not missing.startswith("istota."):
            skipped.append([name, missing])
        else:
            failures.append([name, repr(exc)])
    except BaseException as exc:
        failures.append([name, repr(exc)])
    # The next module starts from no istota state, as a fresh process would.
    for key in set(sys.modules) - before:
        if key == "istota" or key.startswith("istota."):
            del sys.modules[key]
print(json.dumps({"failures": failures, "skipped": skipped, "count": len(names)}))
"""


def _all_modules() -> list[str]:
    names = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(SRC)
        if "__pycache__" in rel.parts or path.stem == "__main__":
            continue
        # Only importable packages: a directory without __init__.py holds data or templates.
        if not all((SRC / Path(*rel.parts[:i]) / "__init__.py").is_file() for i in range(1, len(rel.parts))):
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


class TestImportSweep:
    def test_every_module_imports_on_its_own(self, tmp_path):
        names = _all_modules()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH", "")]))
        result = subprocess.run(
            [sys.executable, "-c", SWEEP],
            input=json.dumps(names), capture_output=True, text=True,
            cwd=tmp_path, env=env, timeout=600, check=False,
        )
        assert result.returncode == 0, result.stderr[-4000:]
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["count"] == len(names)
        assert report["failures"] == [], report["failures"]
