"""Tests for istota.skill_host_paths — the shared host-path allowlist.

A skill CLI runs host-side (the proxy spawns it outside the sandbox), so any
verb taking a host path is an arbitrary-file read or write unless it is scoped.
Two skills need the same scoping — devbox's ``cp-in`` / ``cp-out`` and kv's
``set --value-file`` — so the rule lives in one leaf module rather than being
restated per skill, where the two copies would drift.

The roots mirror what ``build_bwrap_cmd`` binds *for this user*.
``NEXTCLOUD_MOUNT_PATH`` is the shared mount root for everyone, so taking it
whole would hand one user another's workspace.
"""

from pathlib import Path

import pytest

from istota.skill_host_paths import (
    allowed_host_roots,
    resolve_host_path,
    validate_host_path,
)


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """A mount laid out like the real one, with alice as the caller."""
    root = tmp_path / "mount"
    (root / "Users" / "alice").mkdir(parents=True)
    (root / "Users" / "bob").mkdir(parents=True)
    (root / "Channels" / "tok1").mkdir(parents=True)
    (root / "Channels" / "tok2").mkdir(parents=True)
    (root / "Talk").mkdir(parents=True)
    deferred = tmp_path / "deferred"
    deferred.mkdir()
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(root))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(deferred))
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    return root


class TestAllowedHostRoots:
    def test_scopes_the_mount_to_the_calling_user(self, mount, monkeypatch):
        roots = allowed_host_roots()
        assert mount / "Users" / "alice" in roots
        assert mount / "Users" / "bob" not in roots
        assert mount not in roots

    def test_includes_the_tasks_own_channel_only(self, mount, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        roots = allowed_host_roots()
        assert mount / "Channels" / "tok1" in roots
        assert mount / "Channels" / "tok2" not in roots

    def test_talk_is_readable_but_not_writable(self, mount):
        assert mount / "Talk" in allowed_host_roots(writable=False)
        assert mount / "Talk" not in allowed_host_roots(writable=True)

    def test_traversal_token_is_not_turned_into_a_path(self, mount, monkeypatch):
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "../..")
        assert all("Channels" not in str(r) for r in allowed_host_roots())

    def test_mount_contributes_nothing_without_a_user_id(self, mount, monkeypatch):
        """Fail closed: without an identity there is no per-user subtree to
        scope to, so the mount contributes no root at all."""
        monkeypatch.delenv("ISTOTA_USER_ID")
        roots = allowed_host_roots()
        assert all(not r.is_relative_to(mount) for r in roots)

    def test_blank_and_unset_are_skipped(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", "   ")
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        assert allowed_host_roots() == []


class TestResolveHostPath:
    def test_accepts_the_users_own_workspace(self, mount):
        p = mount / "Users" / "alice" / "ok.json"
        p.write_text("{}")
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert err is None
        assert resolved == p.resolve()

    def test_refuses_another_users_workspace(self, mount):
        """The core cross-tenant case: the mount is shared, the bind is not."""
        p = mount / "Users" / "bob" / "private.json"
        p.write_text('{"bobs": "notes"}')
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert resolved is None
        assert "outside allowed roots" in err

    def test_refuses_the_mount_root_itself(self, mount):
        p = mount / "loose.json"
        p.write_text("{}")
        _, err = resolve_host_path(p, writable=False, operation="op")
        assert err is not None

    def test_accepts_the_deferred_dir(self, mount, tmp_path):
        p = tmp_path / "deferred" / "v.json"
        p.write_text("{}")
        resolved, err = resolve_host_path(p, writable=False, operation="op")
        assert err is None
        assert resolved == p.resolve()

    def test_returns_the_resolved_path_for_the_caller_to_use(self, mount):
        """Handing back the approved path is what lets a caller avoid
        re-walking symlinks on the original."""
        real = mount / "Users" / "alice" / "real.json"
        real.write_text("{}")
        sub = mount / "Users" / "alice" / "sub"
        sub.mkdir()
        resolved, err = resolve_host_path(
            mount / "Users" / "alice" / "sub" / ".." / "real.json",
            writable=False, operation="op",
        )
        assert err is None
        assert resolved == real.resolve()

    def test_refuses_leaf_symlink(self, mount):
        target = mount / "Users" / "bob" / "secret.json"
        target.write_text("{}")
        link = mount / "Users" / "alice" / "link.json"
        link.symlink_to(target)
        _, err = resolve_host_path(link, writable=False, operation="op")
        assert "symlink" in err

    def test_intermediate_symlink_out_of_bounds_is_caught_by_resolution(self, mount):
        """A symlinked *directory* is not the leaf, so the leaf check misses it;
        comparing the fully resolved path is what refuses it."""
        (mount / "Users" / "bob" / "deep").mkdir()
        secret = mount / "Users" / "bob" / "deep" / "s.json"
        secret.write_text("{}")
        hop = mount / "Users" / "alice" / "hop"
        hop.symlink_to(mount / "Users" / "bob" / "deep")
        _, err = resolve_host_path(hop / "s.json", writable=False, operation="op")
        assert err is not None
        assert "outside allowed roots" in err

    def test_missing_source_reported(self, mount):
        _, err = resolve_host_path(
            mount / "Users" / "alice" / "nope.json", writable=False, operation="op",
        )
        assert "not found" in err

    def test_no_roots_refuses_and_names_the_operation(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)
        monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
        _, err = resolve_host_path(tmp_path / "x", writable=False, operation="cp-in/cp-out")
        assert "cp-in/cp-out" in err


class TestResolveHostPathWritable:
    def test_accepts_a_new_file_under_a_root(self, mount):
        dest = mount / "Users" / "alice" / "sub" / "new.txt"
        resolved, err = resolve_host_path(dest, writable=True, operation="cp-out")
        assert err is None
        assert resolved == (mount / "Users" / "alice" / "sub").resolve() / "new.txt"
        assert dest.parent.is_dir()

    def test_does_not_create_directories_outside_the_roots(self, mount, tmp_path):
        """The check must precede the mkdir — creating an out-of-bounds tree as
        the daemon user and only then refusing is still a write."""
        dest = tmp_path / "attacker" / "deep" / "tree" / "x.txt"
        _, err = resolve_host_path(dest, writable=True, operation="cp-out")
        assert err is not None
        assert not (tmp_path / "attacker").exists()

    def test_talk_is_refused_as_a_destination(self, mount):
        _, err = resolve_host_path(
            mount / "Talk" / "x.txt", writable=True, operation="cp-out",
        )
        assert err is not None


class TestValidateHostPathWrapper:
    def test_error_only_wrapper_matches(self, mount):
        p = mount / "Users" / "alice" / "ok.json"
        p.write_text("{}")
        assert validate_host_path(p, must_exist=True, operation="op") is None
        assert validate_host_path(
            mount / "Users" / "bob" / "x", must_exist=True, operation="op",
        ) is not None


class TestDevboxStillDelegates:
    """The devbox skill keeps its private wrapper names, but must not keep a
    second copy of the rule."""

    def test_wrapper_delegates_to_the_shared_validator(self, mount):
        from istota.skills import devbox
        assert devbox._validate_host_path(Path("/etc/passwd"), must_exist=True) is not None
        p = mount / "Users" / "alice" / "ok.txt"
        p.write_text("x")
        assert devbox._validate_host_path(p, must_exist=True) is None

    def test_resolving_wrapper_returns_the_approved_path(self, mount):
        from istota.skills import devbox
        p = mount / "Users" / "alice" / "ok.txt"
        p.write_text("x")
        resolved, err = devbox._resolve_host_path(p, must_exist=True)
        assert err is None
        assert resolved == p.resolve()

    def test_cross_user_cp_in_is_refused(self, mount):
        from istota.skills import devbox
        p = mount / "Users" / "bob" / "secret.txt"
        p.write_text("x")
        _, err = devbox._resolve_host_path(p, must_exist=True)
        assert err is not None
