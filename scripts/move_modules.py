#!/usr/bin/env python3
"""Move root modules of `src/istota/` into subsystem packages, and rewrite every reference.

The package reorganisation (spec: src-package-layout) moves about a hundred
root modules into packages named after the subsystem they belong to. This
script holds the whole move table as data and does the mechanical half of each
stage: the `git mv`, and the rewrite of every import, patch target, logger
name, unit-template module path and file path that names an old module. It is
stdlib-only, re-runnable and idempotent, so a branch cut before a stage landed
can catch up by merging `main` and running it once.

    scripts/move_modules.py --only lib            # perform one stage
    scripts/move_modules.py --check --only lib    # exit 1 while work remains
    scripts/move_modules.py                       # catch a branch up

Stage names, and the spec stage each one is. Stage 0 is this script, Stage 1
deletes `stream_parser.py` (a deletion, not a move, so it has no rows), and
Stage 12 is the docs pass:

    lib            Stage 2   dependency-free leaves
    rooms_relay    Stage 3   rooms/ and relay/
    sandbox        Stage 4   sandbox/
    credentials    Stage 5   credentials/, credential_broker/ included
    devbox         Stage 6   devbox/
    notifications  Stage 7   notifications/ (an old module becomes a package)
    usage          Stage 8   usage/ (likewise)
    webui          Stage 9   webui/
    misc           Stage 10  maintenance/, nextcloud/, mail/, browser/
    adjacent       Stage 11  location_logic into location/; geo, user_briefings
                             and shared_blocks_store failed the import gate
                             and stay at the root

**Which stages a run touches.** `--only` names them, and a stage is moved only
when it is named. With no flags the run takes every stage that has *already
landed* (its moves are done) and rewrites references for those alone: that is
the catch-up a branch needs after merging `main`, and it must never move a
stage `main` has not landed yet. A stage that is partly moved is an error
without `--only`; naming it resumes the moves.

What a run does, per file, in this order:

1. Moves. `git mv` for every pending pair of the named stages, creating each new
   package directory with an empty `__init__.py`.
2. Relative imports become absolute, under `src/istota/`, wherever a move would
   change what they resolve to.
3. `from istota import a, b as c` keeps unmoved names and splits each moved one
   onto its own line under its old local name:
   `from istota.rooms import policy as room_policy`.
4. Dotted references `istota.x.y` map by the longest old prefix, and so do the
   templated `{{ istota_package }}.x` forms.
5. `src/istota/<old>.py` file paths, and the directory form for a package move.
6. Markdown: a backticked exact filename `` `room_policy.py` `` is rewritten
   when that basename is unique in the tree. Everything else that names an old
   file (bare `"du.py"` strings in Python, unbackticked prose) is ambiguous and
   is listed as a hand fix rather than rewritten.

Two collision modules become packages of the same name (`notifications`,
`usage`). A reference whose next component is one of the new package's children
is already converted and is left alone, which is also what makes a second run a
no-op. A line carrying `move-modules: keep` is never rewritten: the stub tests
name the old entry-point paths on purpose.

Exit codes: 0 done (or nothing to do), 1 `--check` found work, 2 refused with
nothing changed (dirty tree under a path to move, an inconsistent pair, an
unknown stage, a partly moved stage without `--only`).
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

#: Stage name -> spec stage number. The order is the order the stages land.
STAGES: dict[str, int] = {
    "lib": 2,
    "rooms_relay": 3,
    "sandbox": 4,
    "credentials": 5,
    "devbox": 6,
    "notifications": 7,
    "usage": 8,
    "webui": 9,
    "misc": 10,
    "adjacent": 11,
}

MOVES: list[tuple[str, str, str]] = [
    # (stage, old dotted path, new dotted path)
    ("lib", "istota.atomic_write", "istota.lib.atomic_write"),
    ("lib", "istota.file_lock", "istota.lib.file_lock"),
    ("lib", "istota.filenames", "istota.lib.filenames"),
    ("lib", "istota.date_parse", "istota.lib.date_parse"),
    ("lib", "istota.du", "istota.lib.du"),
    ("lib", "istota.sqlite_util", "istota.lib.sqlite_util"),
    ("lib", "istota.toml_fence", "istota.lib.toml_fence"),
    ("lib", "istota.llm_json", "istota.lib.llm_json"),
    ("lib", "istota.untrusted", "istota.lib.untrusted"),
    ("lib", "istota.image_sniff", "istota.lib.image_sniff"),
    ("lib", "istota.http_headers", "istota.lib.http_headers"),
    ("lib", "istota.retry_after", "istota.lib.retry_after"),
    ("lib", "istota.timestamps", "istota.lib.timestamps"),
    ("lib", "istota.ocr_leaf", "istota.lib.ocr_leaf"),
    ("lib", "istota.rclone_client", "istota.lib.rclone_client"),
    ("rooms_relay", "istota.room_policy", "istota.rooms.policy"),
    ("rooms_relay", "istota.room_scopes", "istota.rooms.scopes"),
    ("rooms_relay", "istota.room_veto", "istota.rooms.veto"),
    ("rooms_relay", "istota.room_colors", "istota.rooms.colors"),
    ("rooms_relay", "istota.side_rooms", "istota.rooms.side_rooms"),
    ("rooms_relay", "istota.speech_gate", "istota.rooms.speech_gate"),
    ("rooms_relay", "istota.surfaces", "istota.rooms.surfaces"),
    ("rooms_relay", "istota.provision_rooms", "istota.rooms.provision"),
    ("rooms_relay", "istota.message_relays", "istota.relay.relays"),
    ("rooms_relay", "istota.relay_destinations", "istota.relay.destinations"),
    ("rooms_relay", "istota.whatsapp_requests", "istota.relay.requests"),
    ("sandbox", "istota.sandbox_plan", "istota.sandbox.plan"),
    ("sandbox", "istota.task_env", "istota.sandbox.task_env"),
    ("sandbox", "istota.task_cgroup", "istota.sandbox.cgroup"),
    ("sandbox", "istota.skill_host_paths", "istota.sandbox.host_paths"),
    ("sandbox", "istota.user_scope", "istota.sandbox.user_scope"),
    ("sandbox", "istota.peer_process", "istota.sandbox.peer_process"),
    ("sandbox", "istota.process_group", "istota.sandbox.process_group"),
    ("sandbox", "istota.shell_exec", "istota.sandbox.shell_exec"),
    ("sandbox", "istota.network_proxy", "istota.sandbox.network_proxy"),
    ("sandbox", "istota.skill_proxy", "istota.sandbox.skill_proxy"),
    ("sandbox", "istota.claude_runtime_env", "istota.sandbox.claude_runtime_env"),
    ("sandbox", "istota.credential_shim", "istota.sandbox.credential_shim"),
    ("sandbox", "istota.git_hardening", "istota.sandbox.git_hardening"),
    ("sandbox", "istota.git_remote_scrub", "istota.sandbox.git_remote_scrub"),
    ("sandbox", "istota.kv_namespaces", "istota.sandbox.kv_namespaces"),
    ("sandbox", "istota.net_guard", "istota.sandbox.net_guard"),
    ("sandbox", "istota.unix_server", "istota.sandbox.unix_server"),
    ("sandbox", "istota.tool_server", "istota.sandbox.tool_server"),
    ("sandbox", "istota.tool_server_protocol", "istota.sandbox.tool_server_protocol"),
    ("sandbox", "istota.forge_cli", "istota.sandbox.forge_cli"),
    ("sandbox", "istota.forge_bin", "istota.sandbox.forge_bin"),
    ("credentials", "istota.secrets_store", "istota.credentials.store"),
    ("credentials", "istota.secrets_vault", "istota.credentials.vault"),
    ("credentials", "istota.secret_schema", "istota.credentials.schema"),
    ("credentials", "istota.local_credentials", "istota.credentials.local"),
    ("credentials", "istota.google_scopes", "istota.credentials.google_scopes"),
    ("credentials", "istota.credential_broker", "istota.credentials.broker"),  # a package
    ("devbox", "istota.devbox_exec_client", "istota.devbox.exec_client"),
    ("devbox", "istota.devbox_exec_protocol", "istota.devbox.exec_protocol"),
    ("devbox", "istota.devbox_peer", "istota.devbox.peer"),
    ("devbox", "istota.devbox_proxy", "istota.devbox.proxy"),
    ("devbox", "istota.devbox_proxy_protocol", "istota.devbox.proxy_protocol"),
    ("notifications", "istota.notifications", "istota.notifications.delivery"),
    ("notifications", "istota.notification_store", "istota.notifications.store"),
    ("notifications", "istota.notification_sources", "istota.notifications.sources"),
    ("notifications", "istota.notification_resolvers", "istota.notifications.resolvers"),  # a package
    ("notifications", "istota.ntfy_headers", "istota.notifications.ntfy_headers"),
    ("usage", "istota.usage", "istota.usage.telemetry"),
    ("usage", "istota.usage_render", "istota.usage.render"),
    ("usage", "istota.subscription_usage", "istota.usage.subscription"),
    ("webui", "istota.web_app", "istota.webui.app"),
    ("webui", "istota.web_auth", "istota.webui.auth"),
    ("webui", "istota.web_auth_mail", "istota.webui.auth_mail"),
    ("webui", "istota.web_origin", "istota.webui.origin"),
    ("webui", "istota.web_router_stubs", "istota.webui.router_stubs"),
    ("webui", "istota.web_session_secret", "istota.webui.session_secret"),
    ("webui", "istota.web_shutdown", "istota.webui.shutdown"),
    ("webui", "istota.web_tokens", "istota.webui.tokens"),
    ("webui", "istota.chat_files", "istota.webui.chat_files"),
    ("webui", "istota.static_dir", "istota.webui.static_dir"),
    ("webui", "istota.admin_logs", "istota.webui.admin_logs"),
    ("webui", "istota.admin_config_view", "istota.webui.admin_config_view"),
    ("webui", "istota.admin_browsers", "istota.webui.admin_browsers"),
    ("webui", "istota.garmin_routes", "istota.webui.garmin_routes"),
    ("webui", "istota.map_basemap", "istota.webui.map_basemap"),
    ("webui", "istota.avatars", "istota.webui.avatars"),
    ("webui", "istota.webhook_receiver", "istota.webui.webhook_receiver"),
    ("misc", "istota.db_backup", "istota.maintenance.db_backup"),
    ("misc", "istota.db_backup_relocate", "istota.maintenance.db_backup_relocate"),
    ("misc", "istota.db_restore", "istota.maintenance.db_restore"),
    ("misc", "istota.db_relocate", "istota.maintenance.db_relocate"),
    ("misc", "istota.db_health", "istota.maintenance.db_health"),
    ("misc", "istota.repos_relocate", "istota.maintenance.repos_relocate"),
    ("misc", "istota.room_relocate", "istota.maintenance.room_relocate"),
    ("misc", "istota.room_mount_reconcile", "istota.maintenance.room_mount_reconcile"),
    ("misc", "istota.worktree_reaper", "istota.maintenance.worktree_reaper"),
    ("misc", "istota.sandbox_cache_sweeper", "istota.maintenance.sandbox_cache_sweeper"),
    ("misc", "istota.host_pressure", "istota.maintenance.host_pressure"),
    ("misc", "istota.talk", "istota.nextcloud.talk"),
    ("misc", "istota.ocs", "istota.nextcloud.ocs"),
    ("misc", "istota.nextcloud_api", "istota.nextcloud.user_metadata"),
    ("misc", "istota.nextcloud_client", "istota.nextcloud.compat"),
    ("misc", "istota.email_support", "istota.mail.support"),
    ("misc", "istota.email_ownership", "istota.mail.ownership"),
    ("misc", "istota.outbound_policy", "istota.mail.outbound_policy"),
    ("misc", "istota.outbound_drafts", "istota.mail.drafts"),
    ("misc", "istota.browser_admission", "istota.browser.admission"),
    ("misc", "istota.browser_owner", "istota.browser.owner"),
    ("adjacent", "istota.location_logic", "istota.location.logic"),
]

#: Old paths that are packages rather than modules; their whole tree moves.
PACKAGES: frozenset[str] = frozenset({
    "istota.credential_broker",
    "istota.notification_resolvers",
})

#: Old modules that become packages of the same name.
COLLISIONS: frozenset[str] = frozenset({"istota.notifications", "istota.usage"})

#: Old paths that keep an entry-point stub beside the moved module, because
#: units rendered by an earlier play name them. A stub at the old path beside
#: the new module counts as done, and the stub file itself is never rewritten.
ENTRY_POINT_STUBS: frozenset[str] = frozenset({
    "istota.web_app",
    "istota.webhook_receiver",
    "istota.devbox_proxy",
})

KEEP_MARKER = "move-modules: keep"

SRC = "src/istota"

SCAN_ROOTS = (
    "src/", "tests/", "testbed/", "scripts/", "deploy/", "docker/", "docs/", "docs-site/", ".claude/",
    # Beyond the spec's list: config.example.toml names `src/istota/forge_cli.py`,
    # and the workflows could name a module path.
    "config/", ".github/",
)
# pyproject.toml carries per-file ruff ignores keyed on `src/istota/web_app.py`.
SCAN_FILES = ("AGENTS.md", "README.md", "pyproject.toml", "install.sh", "schema.sql")
SUFFIXES = frozenset({".py", ".sh", ".md", ".toml", ".yml", ".yaml", ".j2", ".service", ".txt", ".cfg"})
EXCLUDED_NAMES = frozenset({"CHANGELOG.md", "DEVLOG.md"})
EXCLUDED_PREFIXES = (
    "web/",
    "tests/golden/",
    "docker/devbox/lib/",
    "docker/browser/lib/",
    ".claude/worktrees/",
    "scripts/move_modules.py",
    # The mover's own tests build their fixtures from old names on purpose.
    "tests/test_move_modules.py",
)
EXCLUDED_PARTS = frozenset({"__pycache__", "node_modules", ".git"})

TOKEN_RE = re.compile(r"(?<![\w.])istota((?:\.[A-Za-z_]\w*)+)")
TEMPLATED_RE = re.compile(r"(istota_package\s*\}\})((?:\.[A-Za-z_]\w*)+)")
PATH_RE = re.compile(r"(?<![\w.])src/istota/([A-Za-z_]\w*(?:/[A-Za-z_]\w*)*)(\.py(?!\w))?")
IMPORT_CHILD_RE = re.compile(r"\s+import\s+\(?\s*([A-Za-z_]\w*)")
RELATIVE_HEAD_RE = re.compile(r"from[ \t]+(\.+)[ \t]*([A-Za-z_][\w.]*)?([ \t]+import\b)")

EXIT_OK = 0
EXIT_WORK = 1
EXIT_REFUSED = 2


class Refused(Exception):
    """Nothing was changed; the message says why."""


@dataclass(frozen=True)
class Table:
    moves: list[tuple[str, str, str]]
    packages: frozenset[str]
    collisions: frozenset[str]
    stubs: frozenset[str]

    @property
    def stages(self) -> list[str]:
        seen: list[str] = []
        for stage, _, _ in self.moves:
            if stage not in seen:
                seen.append(stage)
        return seen

    def children(self, old: str) -> frozenset[str]:
        """The new package's child names for a collision module."""
        prefix = old + "."
        return frozenset(new[len(prefix):].split(".")[0] for _, _, new in self.moves if new.startswith(prefix))


DEFAULT_TABLE = Table(MOVES, PACKAGES, COLLISIONS, ENTRY_POINT_STUBS)


@dataclass
class Mapper:
    """Old-to-new mapping for the selected stages, with the collision rule."""

    table: Table
    pairs: dict[str, str]
    root: Path | None = None
    reverse_pairs: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.reverse_pairs = {new: old for old, new in self.pairs.items()}
        self._children: dict[str, frozenset[str]] = {}
        self._landed: set[str] = set()
        for old in self.table.collisions:
            children = set(self.table.children(old))
            if self.root is not None and not module_file(self.root, old).is_file():
                # Landed: the package is the authority on its children, so a
                # module added to it later is not read as an old-module attribute.
                self._landed.add(old)
                children |= _package_entries(module_dir(self.root, old))
            self._children[old] = frozenset(children)
        self.ambiguous = False

    def forward_parts(self, parts: list[str], following: str = "", token: bool = False) -> list[str] | None:
        """Map `istota.<parts>`; None when nothing changes.

        `following` is the text after a bare collision token, so
        `from istota.notifications import delivery` reads as converted. With
        `token`, a bare collision token on a landed stage that is not followed
        by `import <name>` could mean the package itself: it is left alone and
        `self.ambiguous` is set for the caller to report.
        """
        self.ambiguous = False
        for k in range(len(parts), 0, -1):
            old = "istota." + ".".join(parts[:k])
            new = self.pairs.get(old)
            if new is None:
                continue
            if old in self._children:
                children = self._children[old]
                if len(parts) > k and parts[k] in children:
                    return None
                if len(parts) == k:
                    m = IMPORT_CHILD_RE.match(following) if following else None
                    if m and m.group(1) in children:
                        return None
                    if token and m is None and old in self._landed:
                        self.ambiguous = True
                        return None
            return new.split(".")[1:] + parts[k:]
        return None

    def forward(self, dotted: str) -> str:
        mapped = self.forward_parts(dotted.split(".")[1:])
        return dotted if mapped is None else "istota." + ".".join(mapped)

    def reverse(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        for k in range(len(parts), 1, -1):
            new = ".".join(parts[:k])
            old = self.reverse_pairs.get(new)
            if old is not None:
                return ".".join([old] + parts[k:])
        return None


@dataclass
class Report:
    moved: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    hand_fix: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Tree state


def _package_entries(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    entries = set()
    for entry in directory.iterdir():
        if entry.suffix == ".py" and entry.stem != "__init__":
            entries.add(entry.stem)
        elif (entry / "__init__.py").is_file():
            entries.add(entry.name)
    return entries


def module_file(root: Path, dotted: str) -> Path:
    return root / SRC / Path(*dotted.split(".")[1:]).with_suffix(".py")


def module_dir(root: Path, dotted: str) -> Path:
    return root / SRC / Path(*dotted.split(".")[1:])


def old_exists(root: Path, table: Table, old: str) -> bool:
    if old in table.packages:
        return (module_dir(root, old) / "__init__.py").is_file()
    return module_file(root, old).is_file()


def new_exists(root: Path, table: Table, old: str, new: str) -> bool:
    if old in table.packages:
        return (module_dir(root, new) / "__init__.py").is_file()
    return module_file(root, new).is_file()


def pair_state(root: Path, table: Table, old: str, new: str) -> str:
    """`pending`, `done`, or `inconsistent`."""
    has_old = old_exists(root, table, old)
    has_new = new_exists(root, table, old, new)
    if has_old and not has_new:
        # A leftover destination directory (a stale __pycache__/ after a reset)
        # would make `git mv` nest the package inside it.
        if old in table.packages and module_dir(root, new).exists():
            return "inconsistent"
        return "pending"
    if has_new and not has_old:
        return "done"
    if has_new and has_old and old in table.stubs:
        return "done"
    return "inconsistent"


def stage_state(root: Path, table: Table, stage: str) -> str:
    states = {pair_state(root, table, old, new) for s, old, new in table.moves if s == stage}
    if "inconsistent" in states:
        return "inconsistent"
    if states == {"done"}:
        return "done"
    if states == {"pending"}:
        return "pending"
    return "partial"


def module_exists(root: Path, dotted: str) -> bool:
    return module_file(root, dotted).is_file() or (module_dir(root, dotted) / "__init__.py").is_file()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def scanned_files(root: Path, skip: set[str]) -> list[str]:
    listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    out = []
    for rel in sorted(set(filter(None, listing.split("\0")))):
        if rel in skip:
            continue
        if not (rel.startswith(SCAN_ROOTS) or rel in SCAN_FILES):
            continue
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        parts = rel.split("/")
        if parts[-1] in EXCLUDED_NAMES or EXCLUDED_PARTS.intersection(parts):
            continue
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        if rel not in SCAN_FILES and not wanted_type(path):
            continue
        out.append(rel)
    return out


def wanted_type(path: Path) -> bool:
    if path.suffix in SUFFIXES or path.name.startswith("Dockerfile"):
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and (b"sh" in first or b"python" in first)


# --------------------------------------------------------------------------
# Rewrite rules


def _keep_line(text: str, pos: int) -> bool:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return KEEP_MARKER in text[start:end if end != -1 else len(text)]


def _module_name(rel: str) -> tuple[str, str]:
    """(module, package) for a file under src/istota/."""
    parts = Path(rel).with_suffix("").parts[1:]  # drop "src"
    if parts[-1] == "__init__":
        mod = ".".join(parts[:-1])
        return mod, mod
    mod = ".".join(parts)
    return mod, ".".join(parts[:-1])


def _resolve(package: str, level: int) -> str | None:
    parts = package.split(".")
    if level - 1 >= len(parts):
        return None
    return ".".join(parts[: len(parts) - (level - 1)])


def rule_relative(text: str, rel: str, root: Path, mapper: Mapper, hand_fix: list[str]) -> tuple[str, int]:
    """Rule 2: relative imports a move would break become absolute (old names)."""
    if not re.search(r"^[ \t]*from[ \t]+\.", text, re.M):
        return text, 0
    try:
        tree = ast.parse(text)
    except SyntaxError:
        hand_fix.append(f"{rel}: does not parse; relative imports not checked")
        return text, 0
    mod, pkg = _module_name(rel)
    is_init = rel.endswith("__init__.py")
    old_identity = mapper.reverse(mod)
    if old_identity is not None:
        # The file already sits at a new path: resolve against where it was,
        # and leave any relative import that resolves where it now is.
        resolve_pkg = old_identity if is_init else old_identity.rsplit(".", 1)[0]
        final_pkg = pkg
        moved_already = True
    else:
        resolve_pkg = pkg
        final_mod = mapper.forward(mod)
        final_pkg = final_mod if is_init else final_mod.rsplit(".", 1)[0]
        moved_already = False

    lines = io.StringIO(text).readlines()
    edits: list[tuple[int, int, int, str]] = []  # (line index, start col, end col, replacement)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base_pre = _resolve(resolve_pkg, node.level)
        base_post = _resolve(final_pkg, node.level)
        if base_pre is None or base_post is None:
            hand_fix.append(f"{rel}:{node.lineno}: relative import climbs above istota")
            continue
        if node.module:
            suffixes = [node.module]
        else:
            suffixes = [alias.name for alias in node.names]
        convert = False
        for suffix in suffixes:
            pre = f"{base_pre}.{suffix}"
            post = f"{base_post}.{suffix}"
            if mapper.forward(pre) == post:
                continue
            if moved_already and _resolves_now(root, post, base_post, suffix, bool(node.module)):
                # Written against the file's new home, not carried over a merge,
                # unless the old home had a module of that name too (`db` beside
                # `briefings/db.py`): then either reading is plausible.
                if module_exists(root, mapper.forward(pre)):
                    hand_fix.append(f"{rel}:{node.lineno}: `{suffix}` resolves at both the old and the new home")
                continue
            convert = True
        if not convert:
            continue
        line = lines[node.lineno - 1]
        if KEEP_MARKER in "".join(lines[node.lineno - 1: node.end_lineno]):
            continue
        col = node.col_offset
        m = RELATIVE_HEAD_RE.match(line, col)
        if line[:col].strip() or m is None:
            hand_fix.append(f"{rel}:{node.lineno}: relative import not rewritten (unusual layout)")
            continue
        absolute = base_pre + (f".{node.module}" if node.module else "")
        edits.append((node.lineno - 1, m.start(), m.end(), f"from {absolute}{m.group(3)}"))
    for index, start, end, replacement in sorted(edits, reverse=True):
        lines[index] = lines[index][:start] + replacement + lines[index][end:]
    return "".join(lines), len(edits)


def _resolves_now(root: Path, post: str, base_post: str, suffix: str, has_module: bool) -> bool:
    """Whether a relative import already resolves from the file's current home."""
    if module_exists(root, post):
        return True
    if has_module or suffix == "*":
        return False
    return _defines(root, base_post, suffix)


def _defines(root: Path, package: str, name: str) -> bool:
    """Whether `package/__init__.py` binds `name` (for `from . import name`)."""
    init = module_dir(root, package) / "__init__.py"
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    return name in _top_level_names(tree)


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
    return names


def _comments(source: str) -> list[str]:
    try:
        return [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
                if tok.type == tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []


def _alias_text(name: str, asname: str | None) -> str:
    return name if asname is None or asname == name else f"{name} as {asname}"


def rule_from_istota(text: str, rel: str, mapper: Mapper, hand_fix: list[str]) -> tuple[str, int]:
    """Rule 3: split moved names out of `from istota import ...`."""
    if not re.search(r"from\s+istota\s+import\b", text):
        return text, 0
    try:
        tree = ast.parse(text)
    except SyntaxError:
        hand_fix.append(f"{rel}: does not parse; `from istota import` not checked")
        return text, 0
    lines = io.StringIO(text).readlines()
    edits: list[tuple[int, int, list[str]]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "istota"):
            continue
        moved = [a for a in node.names if mapper.forward(f"istota.{a.name}") != f"istota.{a.name}"]
        if not moved:
            continue
        first = lines[node.lineno - 1]
        last = lines[node.end_lineno - 1]
        indent = first[: node.col_offset]
        tail = last[node.end_col_offset:].strip()
        if indent.strip() or (tail and not tail.startswith("#")):
            hand_fix.append(f"{rel}:{node.lineno}: `from istota import` not rewritten (shares its line)")
            continue
        span = "".join(lines[node.lineno - 1: node.end_lineno])
        if KEEP_MARKER in span:
            continue
        comments = _comments(span)
        trailing = tail if tail.startswith("#") else ""
        inner = [c for c in comments if c != trailing]
        suffix = f"  {trailing}" if trailing else ""
        new_lines = [f"{indent}{c}\n" for c in inner]
        kept = [a for a in node.names if a not in moved]
        if kept:
            if node.end_lineno > node.lineno:
                new_lines.append(f"{indent}from istota import (\n")
                new_lines += [f"{indent}    {_alias_text(a.name, a.asname)},\n" for a in kept]
                new_lines.append(f"{indent}){suffix}\n")
            else:
                names = ", ".join(_alias_text(a.name, a.asname) for a in kept)
                new_lines.append(f"{indent}from istota import {names}{suffix}\n")
        for alias in moved:
            new = mapper.forward(f"istota.{alias.name}")
            parent, leaf = new.rsplit(".", 1)
            local = alias.asname or alias.name
            as_part = "" if leaf == local else f" as {local}"
            new_lines.append(f"{indent}from {parent} import {leaf}{as_part}{suffix}\n")
        if not last.endswith("\n"):
            new_lines[-1] = new_lines[-1].rstrip("\n")
        edits.append((node.lineno - 1, node.end_lineno, new_lines))
    for start, end, new_lines in sorted(edits, reverse=True):
        lines[start:end] = new_lines
    return "".join(lines), len(edits)


def rule_dotted(text: str, rel: str, mapper: Mapper, hand_fix: list[str]) -> tuple[str, int]:
    """Rule 4: `istota.x.y` tokens and `{{ istota_package }}.x` forms."""
    count = 0

    def substitute(pattern: re.Pattern[str], prefix_group: int | None, dotted_group: int, source: str) -> str:
        def one(match: re.Match[str]) -> str:
            nonlocal count
            if _keep_line(source, match.start()):
                return match.group(0)
            parts = match.group(dotted_group).split(".")[1:]
            mapped = mapper.forward_parts(parts, source[match.end(): match.end() + 200], token=True)
            if mapper.ambiguous:
                line = source.count("\n", 0, match.start()) + 1
                hand_fix.append(f"{rel}:{line}: {match.group(0)}: the package, or the old module it replaced?")
            if mapped is None:
                return match.group(0)
            count += 1
            head = "istota" if prefix_group is None else match.group(prefix_group)
            return head + "." + ".".join(mapped)

        return pattern.sub(one, source)

    text = substitute(TOKEN_RE, None, 1, text)
    text = substitute(TEMPLATED_RE, 1, 2, text)
    return text, count


def rule_paths(text: str, mapper: Mapper, table: Table) -> tuple[str, int]:
    """Rule 5: `src/istota/<old>.py`, and `src/istota/<old pkg>/...`."""
    count = 0

    def path(match: re.Match[str]) -> str:
        nonlocal count
        if _keep_line(text, match.start()):
            return match.group(0)
        parts = match.group(1).split("/")
        is_py = match.group(2) is not None
        for k in range(len(parts), 0, -1):
            old = "istota." + ".".join(parts[:k])
            if old not in mapper.pairs:
                continue
            # A module's path names its file; a bare directory form of a module
            # name is a collision package that already exists, so leave it.
            if old not in table.packages and not (is_py and k == len(parts)):
                return match.group(0)
            new_parts = mapper.pairs[old].split(".")[1:] + parts[k:]
            count += 1
            return "src/istota/" + "/".join(new_parts) + (match.group(2) or "")
        return match.group(0)

    return PATH_RE.sub(path, text), count


def _new_relpath(mapper: Mapper, old: str, table: Table) -> str:
    new = mapper.pairs[old].split(".")[1:]
    return "/".join(new) + ("/" if old in table.packages else ".py")


def _old_basename(old: str, table: Table) -> str:
    leaf = old.split(".")[-1]
    return f"{leaf}/" if old in table.packages else f"{leaf}.py"


def rule_markdown(text: str, mapper: Mapper, table: Table, unique: set[str]) -> tuple[str, int]:
    """Rule 6: backticked exact filenames in markdown, when unambiguous."""
    replacements = {
        _old_basename(old, table): _new_relpath(mapper, old, table)
        for old in mapper.pairs
        if _old_basename(old, table) in unique
    }
    if not replacements:
        return text, 0
    pattern = re.compile("`(" + "|".join(re.escape(b) for b in sorted(replacements, key=len, reverse=True)) + ")`")
    count = 0

    def sub(match: re.Match[str]) -> str:
        nonlocal count
        if _keep_line(text, match.start()):
            return match.group(0)
        count += 1
        return "`" + replacements[match.group(1)] + "`"

    return pattern.sub(sub, text), count


def _leftover_patterns(mapper: Mapper) -> tuple[re.Pattern[str], re.Pattern[str]] | None:
    leaves = sorted({old.split(".")[-1] for old in mapper.pairs}, key=len, reverse=True)
    if not leaves:
        return None
    names = "|".join(re.escape(leaf) for leaf in leaves)
    quoted = re.compile(r"""(['"])(""" + names + r""")\.py\1""")
    bare = re.compile(r"(?<![\w/.-])(" + names + r")\.py(?!\w)")
    return quoted, bare


def report_leftovers(text: str, rel: str, patterns: tuple[re.Pattern[str], re.Pattern[str]] | None,
                     hand_fix: list[str]) -> None:
    """List every remaining mention of an old filename; never rewritten.

    In Python only an exact quoted `"<old>.py"` string counts (the guard-test
    allowlists); comments and docstrings are prose. Elsewhere any mention not
    already under a directory does.
    """
    if patterns is None:
        return
    pattern = patterns[0] if rel.endswith(".py") else patterns[1]
    for number, line in enumerate(text.splitlines(), 1):
        if KEEP_MARKER in line:
            continue
        for match in pattern.finditer(line):
            name = match.group(match.lastindex or 0)
            hand_fix.append(f"{rel}:{number}: {name}.py: {line.strip()[:120]}")


def rewrite_text(text: str, rel: str, root: Path, mapper: Mapper, table: Table, unique: set[str],
                 hand_fix: list[str], leftovers: tuple[re.Pattern[str], re.Pattern[str]] | None = None,
                 ) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    if rel.endswith(".py") and rel.startswith(SRC + "/"):
        text, counts["relative"] = rule_relative(text, rel, root, mapper, hand_fix)
    if rel.endswith(".py"):
        text, counts["from_istota"] = rule_from_istota(text, rel, mapper, hand_fix)
    text, counts["dotted"] = rule_dotted(text, rel, mapper, hand_fix)
    text, counts["paths"] = rule_paths(text, mapper, table)
    if rel.endswith(".md"):
        text, counts["markdown"] = rule_markdown(text, mapper, table, unique)
    report_leftovers(text, rel, leftovers, hand_fix)
    return text, {k: v for k, v in counts.items() if v}


def unique_basenames(root: Path, table: Table) -> set[str]:
    """Old basenames no other file under src/istota/ shares."""
    moved_files: set[Path] = set()
    for _, old, new in table.moves:
        for dotted in (old, new):
            if old in table.packages:
                d = module_dir(root, dotted)
                if d.is_dir():
                    moved_files.update(p.resolve() for p in d.rglob("*.py"))
            else:
                moved_files.add(module_file(root, dotted).resolve())
    others: set[str] = set()
    for path in (root / SRC).rglob("*.py"):
        if "__pycache__" in path.parts or path.resolve() in moved_files:
            continue
        others.add(path.name)
    result = set()
    for _, old, _new in table.moves:
        base = _old_basename(old, table)
        if old in table.packages or base not in others:
            result.add(base)
    return result


# --------------------------------------------------------------------------
# The run


def select(root: Path, table: Table, only: list[str] | None) -> tuple[list[str], list[str]]:
    """(stages to process, informational notes)."""
    notes = []
    if only:
        unknown = [s for s in only if s not in table.stages]
        if unknown:
            raise Refused(f"unknown stage {', '.join(unknown)}; known: {', '.join(table.stages)}")
        for stage in only:
            if stage_state(root, table, stage) == "inconsistent":
                raise Refused(_inconsistent(root, table, stage))
        return list(only), notes
    selected = []
    for stage in table.stages:
        state = stage_state(root, table, stage)
        if state == "done":
            selected.append(stage)
        elif state == "inconsistent":
            raise Refused(_inconsistent(root, table, stage))
        elif state == "partial":
            raise Refused(f"stage {stage} is partly moved; finish it with --only {stage}")
        else:
            notes.append(f"stage {stage} has not landed; skipped (name it with --only to perform it)")
    return selected, notes


def _inconsistent(root: Path, table: Table, stage: str) -> str:
    bad = [f"{old} -> {new}" for s, old, new in table.moves
           if s == stage and pair_state(root, table, old, new) == "inconsistent"]
    return f"inconsistent move pair (both or neither path exists): {'; '.join(bad)}"


def run(root: Path, only: list[str] | None, check: bool, table: Table = DEFAULT_TABLE) -> tuple[int, Report]:
    root = root.resolve()
    report = Report()
    stages, notes = select(root, table, only)
    selected = [(s, old, new) for s, old, new in table.moves if s in stages]
    mapper = Mapper(table, {old: new for _, old, new in selected}, root)
    pending = [(old, new) for _, old, new in selected if pair_state(root, table, old, new) == "pending"]

    if pending and not check:
        paths = []
        for old, _new in pending:
            target = module_dir(root, old) if old in table.packages else module_file(root, old)
            paths.append(str(target.relative_to(root)))
        dirty = git(root, "status", "--porcelain", "--untracked-files=no", "--", *paths)
        if dirty.strip():
            raise Refused("uncommitted changes under a path to move:\n" + dirty.rstrip())
        ignored = _ignored(root, [_created_path(root, table, old, new) for old, new in pending])
        if ignored:
            # `lib/` is in the stock Python .gitignore: a package there would be
            # moved on disk, never committed, and left out of the wheel.
            raise Refused("a destination is gitignored; add an exception first:\n" + "\n".join(ignored))

    skip: set[str] = set()
    for old in table.stubs:
        new = mapper.pairs.get(old)
        if new and module_file(root, old).is_file() and module_file(root, new).is_file():
            skip.add(str(module_file(root, old).relative_to(root)))

    unique = unique_basenames(root, table)
    leftovers = _leftover_patterns(mapper)
    changes: dict[str, str] = {}
    for rel in scanned_files(root, skip):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text, counts = rewrite_text(text, rel, root, mapper, table, unique, report.hand_fix, leftovers)
        if new_text != text:
            changes[rel] = new_text
            summary = " ".join(f"{k}={v}" for k, v in counts.items())
            report.rewritten.append(f"{rel}: {summary}")

    for old, new in pending:
        src_path = module_dir(root, old) if old in table.packages else module_file(root, old)
        dst_path = module_dir(root, new) if old in table.packages else module_file(root, new)
        report.moved.append(f"{src_path.relative_to(root)} -> {dst_path.relative_to(root)}")

    for note in notes:
        print(note)
    if check:
        for line in report.moved:
            print(f"would move {line}")
        for line in report.rewritten:
            print(f"would rewrite {line}")
        _print_hand_fix(report)
        return (EXIT_WORK if report.moved or report.rewritten else EXIT_OK), report

    for rel, new_text in changes.items():
        (root / rel).write_text(new_text, encoding="utf-8")
    for old, new in pending:
        _move(root, table, old, new)
    for line in report.moved:
        print(f"moved {line}")
    for line in report.rewritten:
        print(f"rewrote {line}")
    _print_hand_fix(report)
    return EXIT_OK, report


def _created_path(root: Path, table: Table, old: str, new: str) -> str:
    target = module_dir(root, new) / "__init__.py" if old in table.packages else module_file(root, new)
    return str(target.relative_to(root))


def _ignored(root: Path, paths: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--no-index", "--", *paths],
        capture_output=True, text=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise Refused(f"git check-ignore failed: {result.stderr.strip()}")
    return result.stdout.split()


def _move(root: Path, table: Table, old: str, new: str) -> None:
    parts = new.split(".")
    for k in range(2, len(parts)):
        package = module_dir(root, ".".join(parts[:k]))
        init = package / "__init__.py"
        if not init.exists():
            package.mkdir(parents=True, exist_ok=True)
            init.write_text("", encoding="utf-8")
            git(root, "add", "--", str(init.relative_to(root)))
            print(f"created {init.relative_to(root)}")
    if old in table.packages:
        src_path, dst_path = module_dir(root, old), module_dir(root, new)
    else:
        src_path, dst_path = module_file(root, old), module_file(root, new)
    git(root, "mv", "--", str(src_path.relative_to(root)), str(dst_path.relative_to(root)))


def _print_hand_fix(report: Report) -> None:
    if report.hand_fix:
        print(f"hand-fix ({len(report.hand_fix)}): ambiguous references, not rewritten")
        for line in report.hand_fix:
            print(f"hand-fix {line}")


def main(argv: list[str] | None = None, table: Table = DEFAULT_TABLE) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", metavar="STAGE", help="the stages to process")
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 while work remains")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="the repository (default: this script's checkout)")
    args = parser.parse_args(argv)
    try:
        code, _ = run(args.root, args.only, args.check, table)
    except Refused as exc:
        print(f"move_modules: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return code


if __name__ == "__main__":
    sys.exit(main())
