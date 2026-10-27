"""User-controlled file access goes through `sandbox.confined`, by descriptor.

ISSUE-779. Four of the web security findings (#775, #776, #782 and the review
of the #775 fix) were one defect: a path checked, then used again by name. The
rule in `.claude/rules/sandbox.md` did not stop either of the first two, so
this file fails when new code reaches for the unsafe idiom:

- `FileResponse` anywhere in `src/`. It takes a path and reopens it when the
  response is sent; `webui.fd_response.fd_file_response` streams an open fd.
- A function that resolves a path (`realpath`, `.resolve()`) and tests
  containment (`is_relative_to`, `commonpath`, `relative_to`, `.parents`,
  `path_under_roots`, `is_within`, `startswith`, or an equality with the
  resolved value). The ones that existed when this guard
  landed are on `RESOLVE_THEN_COMPARE_ALLOWED`, and the list only shrinks: an
  entry that no longer matches fails too, so whoever moves a function onto a
  walk takes it off.

The scan is per function and reads the AST, so it misses a resolve in one
function and a compare in its caller. It is a tripwire for the common shape,
not a proof. It reads files off disk, which testmon cannot attribute, so it
runs on a full or named run rather than under `scripts/qt`.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from istota.sandbox import confined

SRC = Path(__file__).resolve().parent.parent / "src"

_RESOLVE = frozenset({"realpath", "resolve"})
_CONTAIN = frozenset({
    "is_relative_to", "commonpath", "relative_to", "path_under_roots", "is_within",
})

# "<path below src/>::<function>". Most of these resolve a daemon-composed
# path, or check containment for a message rather than to open anything; they
# have not each been reviewed. Move one onto `confined` when its module is
# touched, and take it off here.
RESOLVE_THEN_COMPARE_ALLOWED = frozenset({
    "istota/brain/tmux_claude.py::transcript",
    "istota/build_info.py::_git_dir",
    "istota/build_info.py::checkout_revision",
    "istota/cli.py::cmd_workspace_localize",
    "istota/cli.py::cmd_workspace_moved_note",
    "istota/config.py::_validate_sandbox_ro_paths",
    "istota/config.py::module_db_root",
    "istota/doctor.py::_control_mask_finding",
    "istota/doctor.py::_session_log_mask_shape_reason",
    "istota/doctor.py::check_forge_wrapper_shadowing",
    "istota/doctor.py::check_secret_key_separation",
    "istota/executor.py::_task_dir_under",
    "istota/executor.py::_validate_workspace_dir",
    "istota/executor.py::build_clean_env",
    "istota/executor.py::python_base_prefix_binds",
    "istota/executor.py::resolve_sandbox_cache_dir",
    "istota/health/documents.py::resolve_document_path",
    "istota/health/ocr.py::_debug_main",
    "istota/mail/drafts.py::_confined_attachment",
    "istota/maintenance/worktree_reaper.py::_classify",
    "istota/maintenance/worktree_reaper.py::_is_within",
    "istota/money/core/invoicing.py::_resolve_logo",
    "istota/sandbox/attachment_source.py::_parts_under_resolved",
    "istota/sandbox/attachment_source.py::_spellings",
    # Resolves the daemon-composed root only, to match a second spelling of it.
    "istota/sandbox/confined.py::parts_below",
    "istota/sandbox/git_remote_scrub.py::_writable",
    "istota/sandbox/git_remote_scrub.py::find_git_dirs",
    "istota/sandbox/host_paths.py::developer_repos_root",
    "istota/sandbox/host_paths.py::resolve_in_roots",
    "istota/sandbox/host_paths.py::resolve_under_repos",
    "istota/sandbox/plan.py::_mask_dir",
    "istota/sandbox/plan.py::plan_masks",
    "istota/sandbox/plan.py::project_fs_roots",
    "istota/sandbox/user_scope.py::scoped_user_dir",
    "istota/scheduler.py::_at_or_above",
    "istota/scheduler.py::_localize_owner_file",
    "istota/scheduler.py::localize_workspace_attachments",
    "istota/scheduler_deferred.py::_source_path_allowed",
    "istota/session/tools/files.py::make_grep_tool",
    "istota/skills/_loader.py::contained_group_dir",
    "istota/skills/_loader.py::contained_overlay_dir",
    "istota/skills/browse/__init__.py::_workspace_relative",
    "istota/skills/developer/istota_dev.py::_same_path",
    "istota/skills/email/__init__.py::_unholdable_attachments",
    "istota/skills/memory/__init__.py::_channel_md_path",
    "istota/skills/skills/__init__.py::_mount_relative",
    "istota/skills/whatsapp/__init__.py::with_file",
    "istota/storage.py::_absolute_vault_location",
    "istota/storage.py::_contained_channel_dir",
    "istota/storage.py::resolve_user_scripts_dir",
    "istota/transport/whatsapp/baileys_bridge.py::_same_directory",
    "istota/webui/admin_logs.py::_contained",
    "istota/webui/admin_logs.py::resolve_app_log_chain",
    "istota/webui/router_stubs.py::get_user_context",
    "istota/webui/router_stubs.py::make_get_user_context",
    "istota/workspace/config_files.py::list_files",
    "istota/workspace/reach.py::_mount_reach",
})

# The allowlist's size when the guard landed. Lower it as entries come off;
# never raise it.
ALLOWLIST_CEILING = 57


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def file_response_lines(source: str) -> list[int]:
    """Lines that call or import `FileResponse`."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _call_name(node) == "FileResponse":
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(a.name == "FileResponse" for a in node.names):
            lines.append(node.lineno)
    return sorted(lines)


def _names_bound_to_a_resolve(nodes: list[ast.AST]) -> set[str]:
    names = set()
    for n in nodes:
        if isinstance(n, ast.Assign) and any(
            isinstance(c, ast.Call) and _call_name(c) in _RESOLVE for c in ast.walk(n.value)
        ):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
    return names


def resolve_then_compare(source: str) -> list[str]:
    """Names of functions that both resolve a path and test containment."""
    found = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(ast.walk(fn))
        calls = {_call_name(n) for n in nodes if isinstance(n, ast.Call)}
        if not calls & _RESOLVE:
            continue
        parents = any(isinstance(n, ast.Attribute) and n.attr == "parents" for n in nodes)
        # `resolved == root / name` and `str(resolved).startswith(...)` are the
        # same check spelled without a containment helper.
        resolved_names = _names_bound_to_a_resolve(nodes)
        compared = False
        for n in nodes:
            if not isinstance(n, ast.Compare):
                continue
            if not any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in n.ops):
                continue
            for operand in [n.left, *n.comparators]:
                for sub in ast.walk(operand):
                    if isinstance(sub, ast.Call) and _call_name(sub) in _RESOLVE:
                        compared = True
                    elif isinstance(sub, ast.Name) and sub.id in resolved_names:
                        compared = True
        if calls & _CONTAIN or "startswith" in calls or parents or compared:
            found.append(fn.name)
    return found


def _sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
    ]


class TestTheGuard:
    def test_no_file_response_in_src(self):
        hits = [
            f"{rel}:{line}"
            for rel, text in _sources()
            for line in file_response_lines(text)
        ]
        assert hits == [], (
            "FileResponse reopens its path when the response is sent; open the "
            "file through sandbox.confined and serve it with "
            f"webui.fd_response.fd_file_response: {hits}"
        )

    def test_no_new_resolve_then_compare(self):
        found = {
            f"{rel}::{name}"
            for rel, text in _sources()
            for name in resolve_then_compare(text)
        }
        new = sorted(found - RESOLVE_THEN_COMPARE_ALLOWED)
        assert new == [], (
            "A resolved path compared to a root is true only until a component "
            "is swapped for a link. Walk it with sandbox.confined instead: "
            f"{new}"
        )

    def test_the_allowlist_only_shrinks(self):
        found = {
            f"{rel}::{name}"
            for rel, text in _sources()
            for name in resolve_then_compare(text)
        }
        stale = sorted(RESOLVE_THEN_COMPARE_ALLOWED - found)
        assert stale == [], f"No longer resolve-then-compare; remove from the allowlist: {stale}"
        assert len(RESOLVE_THEN_COMPARE_ALLOWED) <= ALLOWLIST_CEILING


class TestTheScanCanFail:
    """Negative controls: each detector flags the shape it exists for."""

    def test_file_response_is_flagged(self):
        source = (
            "from starlette.responses import FileResponse\n"
            "def download(path):\n"
            "    return FileResponse(path)\n"
        )
        assert file_response_lines(source) == [1, 3]

    def test_the_attribute_spelling_is_flagged(self):
        source = "import fastapi\ndef d(p):\n    return fastapi.responses.FileResponse(p)\n"
        assert file_response_lines(source) == [3]

    @pytest.mark.parametrize("compare", [
        "candidate.relative_to(root)",
        "candidate.is_relative_to(root)",
        "os.path.commonpath([candidate, root]) == root",
        "root in candidate.parents",
        "candidate == root / name",
        "str(candidate).startswith(str(root))",
    ])
    def test_resolve_then_compare_is_flagged(self, compare):
        source = (
            "import os\n"
            "def serve(root, name):\n"
            "    candidate = (root / name).resolve()\n"
            f"    ok = {compare}\n"
            "    return open(candidate)\n"
        )
        assert resolve_then_compare(source) == ["serve"]

    def test_a_walk_is_not_flagged(self):
        source = (
            "from istota.sandbox.confined import open_regular\n"
            "def serve(root, parts):\n"
            "    return open_regular(root, parts)\n"
        )
        assert resolve_then_compare(source) == []


class TestConfined:
    def test_opens_a_regular_file_below_the_root(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "f.txt").write_text("inside")
        fd = confined.open_regular(tmp_path, ["a", "f.txt"])
        assert fd is not None
        try:
            assert os.read(fd, 100) == b"inside"
        finally:
            os.close(fd)

    def test_refuses_a_leaf_link_back_inside_the_root(self, tmp_path):
        (tmp_path / "real.txt").write_text("inside")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        assert confined.open_regular(tmp_path, ["link.txt"]) is None

    def test_refuses_a_directory_link_above_the_leaf(self, tmp_path):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("outside")
        (root / "a").symlink_to(outside)
        assert confined.open_regular(root, ["a", "secret.txt"]) is None
        assert confined.open_dir(root, "a") is None

    def test_follows_a_link_at_the_anchor(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "f.txt").write_text("ok")
        (tmp_path / "mount").symlink_to(real)
        fd = confined.open_regular(tmp_path / "mount", ["f.txt"])
        assert fd is not None
        os.close(fd)

    def test_refuses_a_fifo_without_blocking(self, tmp_path):
        os.mkfifo(tmp_path / "pipe")
        assert confined.open_regular(tmp_path, ["pipe"]) is None

    @pytest.mark.parametrize("parts", [[], [""], ["."], [".."], ["a/b"], ["a\0b"]])
    def test_refuses_a_component_that_is_not_one_name(self, tmp_path, parts):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").write_text("x")
        assert confined.open_regular(tmp_path, parts) is None

    def test_the_descriptor_survives_a_swap_after_the_open(self, tmp_path):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        (root / "a").mkdir(parents=True)
        outside.mkdir()
        (root / "a" / "f.txt").write_text("authorized")
        (outside / "f.txt").write_text("outside")
        fd = confined.open_regular(root, ["a", "f.txt"])
        assert fd is not None
        try:
            os.rename(root / "a", root / "a.real")
            (root / "a").symlink_to(outside)
            assert os.pread(fd, 100, 0) == b"authorized"
        finally:
            os.close(fd)

    def test_a_mount_error_on_the_leaf_raises_only_when_asked(self, tmp_path, monkeypatch):
        import errno

        (tmp_path / "f.txt").write_text("x")
        real_open = os.open

        def eio(path, flags, *args, **kwargs):
            if path == "f.txt":
                raise OSError(errno.EIO, "I/O error")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(confined.os, "open", eio)
        assert confined.open_regular(tmp_path, ["f.txt"]) is None
        with pytest.raises(OSError):
            confined.open_regular(tmp_path, ["f.txt"], raise_mount_errors=True)

    def test_create_walk_refuses_a_link(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "root"
        root.mkdir()
        (root / "a").symlink_to(outside)
        with pytest.raises(OSError):
            confined.open_or_create_dirs(root, ["a", "b"])
        assert not (outside / "b").exists()

    def test_parts_below_is_lexical(self, tmp_path):
        assert confined.parts_below(tmp_path / "a" / "b", tmp_path) == ("a", "b")
        assert confined.parts_below(tmp_path, tmp_path) == ()
        assert confined.parts_below(tmp_path.parent / "x", tmp_path) is None
        assert confined.parts_below(tmp_path / ".." / "x", tmp_path) is None

    def test_parts_below_matches_the_root_through_a_link(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "mount").symlink_to(real)
        assert confined.parts_below(real / "a", tmp_path / "mount") == ("a",)

    def test_old_names_still_reach_the_same_functions(self):
        from istota.sandbox import attachment_source
        from istota.skills import _loader
        from istota.workspace import writes

        assert _loader.open_overlay_dir is confined.open_dir
        assert attachment_source.open_regular_at is confined.open_regular_at
        assert writes.open_or_create_dirs is confined.open_or_create_dirs
