"""Which file the daemon decrypts: `storage.resolve_user_vault_path`.

The vault reader holds a passphrase the daemon has and the model does not, so
the path it opens decides what gets decrypted and written into the secrets
table. Two forms of `[users.<id>] vault_path`, and they are contained by two
different mechanisms because their exposure is different:

**Relative** resolves under `{workspace}/Users/{user_id}`, which
`build_bwrap_cmd` binds **read-write** into that user's own sandbox — so every
directory component above the leaf is model-writable and `mv config
config.real && ln -s /anywhere config` is two commands from inside it.
`read_overlay_bytes`' `O_NOFOLLOW` covers the *last* component only, so the
containment here is `open_overlay_dir`'s `openat` walk: each component opened
`O_NOFOLLOW | O_DIRECTORY` relative to the one above, and the caller ends up
holding a descriptor pinned to an inode rather than a name to walk again.

**Absolute** is refused unless it resolves *outside* `{workspace_path}`
entirely, which is the one cross-user route in the design: `[users.alice]`
naming `{mount}/Users/bob/config/vault.kdbx` reads bob's vault, and since an
operator provisioning two users is liable to paste one generated passphrase
twice, it then writes bob's credentials onto alice's rows with no error on any
surface. Everything under the workspace is sandbox-reachable by construction,
and the stated purpose of the absolute form is a file the sandbox cannot reach,
so refusing costs nothing.

**The two mechanisms answer a symlink differently and both answers are here.**
A symlink at a *directory* component fails the walk, so the resolver returns
None. A symlink at the *leaf* is invisible to the walk — it is not a directory
component — and is refused one layer down by `read_vault_bytes`, which opens
`O_NOFOLLOW`. Testing only the first would leave the second resting on a
`read_overlay_bytes` property nothing here names.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from istota.config import Config, UserConfig
from istota.secrets_vault import VaultUnreadable, read_vault_bytes
from istota.skills._loader import OVERLAY_IS_A_SYMLINK, OVERLAY_NOT_A_REGULAR_FILE
from istota.storage import (
    VAULT_PATH_BAD_COMPONENT,
    VAULT_PATH_NO_SUCH_DIRECTORY,
    VAULT_PATH_NOT_A_FILENAME,
    VAULT_PATH_OUTSIDE_USER_TREE,
    resolve_user_vault_path,
)


VAULT_BYTES = b"not-a-real-kdbx-just-bytes-to-identify-the-file"
DECOY_BYTES = b"decoy-bytes-from-outside-the-users-tree"


def _config(tmp_path, *, workspace=True, **users) -> Config:
    """A config with a workspace at `{tmp_path}/mount` and the given users."""
    kwargs: dict = {
        "db_path": tmp_path / "istota.db",
        "temp_dir": tmp_path / "tmp",
        "users": {name: uc for name, uc in users.items()},
    }
    if workspace:
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        kwargs["workspace_path"] = mount
    return Config(**kwargs)


def _user_root(config: Config, user_id: str) -> Path:
    root = Path(config.workspace_path) / "Users" / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed(path: Path, data: bytes = VAULT_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _resolve(config: Config, user_id: str = "alice"):
    """Resolve, and close the descriptor for the caller.

    Only for the tests that assert on the *path* or on None. Anything asserting
    on a read holds the location itself, since the descriptor is the whole
    mechanism.
    """
    location = resolve_user_vault_path(config, user_id).location
    if location is not None and location.dir_fd is not None:
        os.close(location.dir_fd)
    return location


class TestTheRelativeForm:
    """Resolved under the user's own root, and held open rather than named."""

    def test_a_relative_path_resolves_under_the_user_root(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig(vault_path="istota/config/vault.kdbx"))
        root = _user_root(config, "alice")
        _seed(root / "istota" / "config" / "vault.kdbx")

        location = resolve_user_vault_path(config, "alice").location
        assert location is not None
        try:
            assert location.path == root.resolve() / "istota" / "config" / "vault.kdbx"
            # The descriptor is what makes the claim structural rather than a
            # comparison, so the absence of one is a failure even though every
            # other assertion here would pass without it.
            assert location.dir_fd is not None
            data, _digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
            assert data == VAULT_BYTES
        finally:
            os.close(location.dir_fd)

    def test_a_bare_filename_resolves_against_the_user_root_itself(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig(vault_path="vault.kdbx"))
        root = _user_root(config, "alice")
        _seed(root / "vault.kdbx")

        location = resolve_user_vault_path(config, "alice").location
        assert location is not None
        try:
            assert location.path == root.resolve() / "vault.kdbx"
            data, _digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
            assert data == VAULT_BYTES
        finally:
            os.close(location.dir_fd)

    def test_the_descriptor_survives_the_directory_being_swapped(self, tmp_path):
        """The by-construction claim, driven rather than asserted about.

        This is the window a resolved *path* leaves open: the check and the
        `open(2)` are separated in time, and the components between them are
        writable from inside the sandbox. With a descriptor the swap lands on a
        name nothing reads again.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path="config/vault.kdbx"))
        root = _user_root(config, "alice")
        _seed(root / "config" / "vault.kdbx")
        outside = _seed(tmp_path / "outside" / "vault.kdbx", DECOY_BYTES)

        location = resolve_user_vault_path(config, "alice").location
        assert location is not None
        try:
            (root / "config").rename(root / "config.real")
            (root / "config").symlink_to(outside.parent)

            data, _digest = read_vault_bytes(location.path, dir_fd=location.dir_fd)
            assert data == VAULT_BYTES
        finally:
            os.close(location.dir_fd)

    def test_a_symlinked_directory_component_out_of_the_tree_is_refused(
        self, tmp_path, caplog
    ):
        config = _config(tmp_path, alice=UserConfig(vault_path="config/vault.kdbx"))
        root = _user_root(config, "alice")
        outside = _seed(tmp_path / "outside" / "vault.kdbx", DECOY_BYTES)
        (root / "config").symlink_to(outside.parent)

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        said = [
            r.getMessage() for r in caplog.records if r.name == "istota.storage"
        ]
        assert said, "a configured path that is refused must say so somewhere"
        # The reason id, not just the line: a refused path that reported a
        # missing directory would send the operator to create one.
        assert VAULT_PATH_OUTSIDE_USER_TREE in said[0], said

    def test_a_symlinked_directory_component_inside_the_tree_is_refused_too(
        self, tmp_path
    ):
        """The one place this is stricter than §1's stated rule, deliberately.

        §1 named `_contained_under_user_root`, which resolves symlinks and
        accepts one landing back inside the user's own tree. `open_overlay_dir`
        refuses a symlink at any component whatever it points at, because
        refusing is the only answer that survives the path being rewritten
        underneath it — the same reversal ISSUE-344 made for skill overlays. The
        cost is a user who deliberately linked a directory inside their own
        workspace, and the answer is a refusal rather than silence.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path="config/vault.kdbx"))
        root = _user_root(config, "alice")
        _seed(root / "real" / "vault.kdbx")
        (root / "config").symlink_to(root / "real")

        assert _resolve(config) is None

    def test_a_symlinked_leaf_resolves_and_is_refused_by_the_read(self, tmp_path):
        """The walk cannot see the leaf, so the refusal is one layer down.

        Both halves matter. The resolver returning a location here is what makes
        the `O_NOFOLLOW` on the leaf the thing that stops it, and a test that
        only checked the resolver would leave that resting on nothing.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path="config/vault.kdbx"))
        root = _user_root(config, "alice")
        _seed(root / "config" / "real.kdbx")
        (root / "config" / "vault.kdbx").symlink_to(root / "config" / "real.kdbx")

        location = resolve_user_vault_path(config, "alice").location
        assert location is not None
        try:
            with pytest.raises(VaultUnreadable) as exc:
                read_vault_bytes(location.path, dir_fd=location.dir_fd)
            assert str(exc.value) == OVERLAY_IS_A_SYMLINK
        finally:
            os.close(location.dir_fd)

    def test_the_leaf_is_returned_as_written_and_not_resolved(self, tmp_path):
        """The negative control on the display path's construction.

        `read_overlay_bytes` opens `path.name` relative to the descriptor, so the
        two have to agree: realpath'ing the whole join would return the link's
        *target* name, and the read would then open a different file from the
        one the walk contained — silently, and only when the leaf happens to be
        a link. The root is realpath'd (a mount is reached through a symlink on
        some hosts); the components the operator wrote are not.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path="config/vault.kdbx"))
        root = _user_root(config, "alice")
        _seed(root / "config" / "real.kdbx")
        (root / "config" / "vault.kdbx").symlink_to(root / "config" / "real.kdbx")

        location = _resolve(config)
        assert location is not None
        assert location.path.name == "vault.kdbx"

    def test_no_workspace_returns_none_for_a_relative_path(self, tmp_path):
        config = _config(
            tmp_path, workspace=False, alice=UserConfig(vault_path="config/vault.kdbx")
        )
        assert _resolve(config) is None

    def test_a_missing_directory_component_returns_none(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig(vault_path="config/vault.kdbx"))
        _user_root(config, "alice")
        assert _resolve(config) is None


class TestTheAbsoluteForm:
    """Outside the workspace it passes; anywhere under it, it is refused."""

    def test_an_absolute_path_outside_the_workspace_passes(self, tmp_path):
        vault = _seed(tmp_path / "etc" / "vault.kdbx")
        config = _config(tmp_path, alice=UserConfig(vault_path=str(vault)))

        location = resolve_user_vault_path(config, "alice").location
        assert location is not None
        assert location.path == vault.resolve()
        # No descriptor, and that is the form's own property rather than an
        # omission: the file is outside the workspace by the refusal below, so
        # it has no sandbox-writable ancestor to hold open.
        assert location.dir_fd is None
        data, _digest = read_vault_bytes(location.path)
        assert data == VAULT_BYTES

    def test_an_absolute_path_under_the_workspace_is_refused(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig())
        vault = _seed(Path(config.workspace_path) / "shared" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        assert _resolve(config) is None

    def test_another_users_vault_is_refused(self, tmp_path, caplog):
        """The typo §1 closes, and the only cross-user route in the design.

        `vault_path = "{mount}/Users/bob/config/vault.kdbx"` under
        `[users.alice]` is a plausible copy-paste, not a wild path. Unrefused it
        parses bob's vault with a passphrase an operator very likely generated
        once and pasted twice, and writes bob's credentials onto alice's rows.
        """
        config = _config(tmp_path, alice=UserConfig(), bob=UserConfig())
        bob_vault = _seed(
            Path(config.workspace_path) / "Users" / "bob" / "config" / "vault.kdbx"
        )
        config.users["alice"].vault_path = str(bob_vault)

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        assert any("vault_path_refused" in r.getMessage() for r in caplog.records)

    def test_an_absolute_path_under_the_task_temp_dir_is_refused(self, tmp_path):
        """`workspace_path` is not the only tree bound read-write into a sandbox.

        `sandbox_plan` binds `{temp_dir}/{user_id}` read-write into **every**
        task's namespace, and `{developer.repos_dir}/{user_id}` into an admin
        developer task's. Both are outside `workspace_path`, so an operator
        putting the vault there to "keep it off the mount" would land it in a
        tree the model can write — and the absolute branch hands back no
        descriptor, so the leaf-only `O_NOFOLLOW` is all that stands behind it.
        §1 scoped the refusal to the workspace without weighing these two.
        """
        config = _config(tmp_path, alice=UserConfig())
        vault = _seed(Path(config.temp_dir) / "alice" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        assert _resolve(config) is None

    def test_an_absolute_path_under_the_developer_repos_root_is_refused(
        self, tmp_path
    ):
        config = _config(tmp_path, alice=UserConfig())
        repos = tmp_path / "repos"
        config.developer.repos_dir = str(repos)
        vault = _seed(repos / "alice" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        assert _resolve(config) is None

    def test_an_absolute_path_under_the_package_cache_is_refused(self, tmp_path):
        """The fourth read-write bind, and the one this list was missing.

        `sandbox_plan` emits `_rw(cache_dir, "package_cache")` for
        `resolve_sandbox_cache_dir`, whose configured branch is
        `{security.sandbox_cache_dir}/{user_id}` — the shape for every
        non-admin and for any deployment without the developer skill, so the
        common case rather than the exotic one. It was admitted here until
        this function was read beside `sandbox_plan.config_sandbox_bound_roots`,
        which lists it.
        """
        config = _config(tmp_path, alice=UserConfig())
        cache = tmp_path / "caches"
        config.security.sandbox_cache_dir = str(cache)
        vault = _seed(cache / "alice" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        assert _resolve(config) is None

    def test_an_unconfigured_package_cache_refuses_nothing(self, tmp_path):
        """The control for the entry above, matching the repos-root pair: an
        empty `sandbox_cache_dir` must not read as "every path is inside it"."""
        config = _config(tmp_path, alice=UserConfig())
        config.security.sandbox_cache_dir = ""
        vault = _seed(tmp_path / "etc" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        location = _resolve(config)
        assert location is not None
        assert location.path == vault.resolve()

    def test_an_unconfigured_repos_root_refuses_nothing(self, tmp_path):
        """The control: `developer.repos_dir` is empty by default, and an empty
        root must not read as "every path is inside it"."""
        config = _config(tmp_path, alice=UserConfig())
        assert config.developer.repos_dir == ""
        vault = _seed(tmp_path / "etc" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        location = _resolve(config)
        assert location is not None
        assert location.path == vault.resolve()

    def test_a_symlink_from_outside_the_mount_into_it_is_refused(self, tmp_path):
        """The refusal is by *resolved* path, so the link is followed first."""
        config = _config(tmp_path, alice=UserConfig(), bob=UserConfig())
        bob_vault = _seed(
            Path(config.workspace_path) / "Users" / "bob" / "config" / "vault.kdbx"
        )
        link = tmp_path / "etc" / "vault.kdbx"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(bob_vault)
        config.users["alice"].vault_path = str(link)

        assert _resolve(config) is None

    def test_the_workspace_root_itself_is_refused(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig())
        config.users["alice"].vault_path = str(config.workspace_path)
        assert _resolve(config) is None

    def test_a_sibling_sharing_the_workspaces_name_prefix_passes(self, tmp_path):
        """The control on the containment test being a path comparison.

        `{tmp}/mount-other` is not under `{tmp}/mount`, and a refusal written as
        a string prefix would say it is.
        """
        config = _config(tmp_path, alice=UserConfig())
        vault = _seed(tmp_path / "mount-other" / "vault.kdbx")
        config.users["alice"].vault_path = str(vault)

        location = _resolve(config)
        assert location is not None
        assert location.path == vault.resolve()

    def test_a_symlinked_workspace_root_still_refuses_what_is_under_it(self, tmp_path):
        """Both sides are resolved, so a symlinked mount is not a way past it.

        The mount is reached through a symlink on some hosts, which is why
        `contained_overlay_dir` resolves both sides before comparing. Comparing
        a resolved vault path against an unresolved workspace root reads every
        path as outside — here, admitting exactly the cross-user case above.
        """
        real_mount = tmp_path / "real-mount"
        real_mount.mkdir()
        link = tmp_path / "linked-mount"
        link.symlink_to(real_mount)
        vault = _seed(real_mount / "Users" / "bob" / "vault.kdbx")

        config = Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "tmp",
            workspace_path=link,
            users={"alice": UserConfig(vault_path=str(vault))},
        )
        assert _resolve(config) is None

    def test_an_absolute_path_passes_when_there_is_no_workspace_at_all(self, tmp_path):
        vault = _seed(tmp_path / "etc" / "vault.kdbx")
        config = _config(
            tmp_path, workspace=False, alice=UserConfig(vault_path=str(vault))
        )
        location = _resolve(config)
        assert location is not None
        assert location.path == vault.resolve()


class TestWhatIsRefusedBeforeEitherBranch:
    """A path that names nothing openable, and a user that names nobody."""

    def test_an_unconfigured_user_returns_none(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig())
        assert _resolve(config) is None

    def test_an_unknown_user_returns_none(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig(vault_path="vault.kdbx"))
        assert _resolve(config, "nobody") is None

    @pytest.mark.parametrize("vault_path", [".", "..", "./", "config/..", "   "])
    def test_a_path_naming_no_plain_file_is_refused(
        self, tmp_path, vault_path, caplog
    ):
        """`Path(".").parts` is empty, so the leaf extraction has nothing to
        take — and `Path("a/..").name` is `..`, which climbs rather than
        descending. Refused here rather than left to fail three layers down,
        where the reason would name the wrong thing.

        The reason id is asserted, not just the None. Every case in this class
        returns None, so the value alone cannot say which branch produced it —
        and `"   "` in particular used to reach None by the *unconfigured*
        early return, which says nothing at all about a value the operator did
        configure.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path=vault_path))
        _user_root(config, "alice")

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        said = [
            r.getMessage() for r in caplog.records if r.name == "istota.storage"
        ]
        assert said, "a configured path refused in silence is the thing to avoid"
        assert VAULT_PATH_NOT_A_FILENAME in said[0], said

    def test_an_empty_vault_path_is_off_rather_than_refused(self, tmp_path, caplog):
        """The one silent answer, and the discriminating pair for the case above.

        Empty means the feature is off for this user, which is every user by
        default — so a WARNING here would be one line per sync cycle per user on
        a deployment nobody has configured a vault on. A blank-but-present value
        is the opposite: something was written and it resolves to nothing.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path=""))
        _user_root(config, "alice")

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        assert not [r for r in caplog.records if r.name == "istota.storage"]

    def test_a_missing_directory_says_so_rather_than_naming_containment(
        self, tmp_path, caplog
    ):
        """A typo'd directory is the commonest refusal and the least alarming.

        `open_overlay_dir` answers None for five causes at once, and reported as
        a containment refusal the ordinary one sends an operator hunting a
        boundary that is working.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path="confg/vault.kdbx"))
        _user_root(config, "alice")

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        said = [
            r.getMessage() for r in caplog.records if r.name == "istota.storage"
        ]
        assert said
        assert VAULT_PATH_NO_SUCH_DIRECTORY in said[0], said
        assert VAULT_PATH_OUTSIDE_USER_TREE not in said[0]

    @pytest.mark.parametrize("vault_path", ["config", "config/", "config/."])
    def test_a_trailing_slash_or_dot_is_not_distinguishable_from_a_filename(
        self, tmp_path, vault_path
    ):
        """All three are `Path("config")`, so all three resolve.

        `pathlib` drops a `.` component and a trailing separator, which means a
        refusal written for `"config/"` would refuse `"config"` as well — an
        ordinary, if wrong, leaf name. The refusal that fits is the read's:
        `read_overlay_bytes` checks `S_ISREG` on the descriptor and answers
        `not a regular file`, which is what the operator actually needs to hear.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path=vault_path))
        root = _user_root(config, "alice")
        (root / "config").mkdir()

        location = resolve_user_vault_path(config, "alice").location
        assert location is not None
        try:
            assert location.path == root.resolve() / "config"
            with pytest.raises(VaultUnreadable) as exc:
                read_vault_bytes(location.path, dir_fd=location.dir_fd)
            assert str(exc.value) == OVERLAY_NOT_A_REGULAR_FILE
        finally:
            os.close(location.dir_fd)

    def test_a_null_byte_in_the_path_is_refused_rather_than_raising(
        self, tmp_path, caplog
    ):
        """`\\u0000` is expressible in a TOML string and must not raise.

        On the relative branch the NUL is caught by the component check, which
        mirrors `open_overlay_dir`'s own pre-loop rule — so this does *not*
        exercise the resolver's `except ValueError`, and the reason id says so
        rather than blaming the tree. The absolute sibling below is the one
        where `os.path.realpath` genuinely raises `ValueError`.
        """
        config = _config(tmp_path, alice=UserConfig(vault_path="con\0fig/vault.kdbx"))
        _user_root(config, "alice")

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        said = [
            r.getMessage() for r in caplog.records if r.name == "istota.storage"
        ]
        assert said
        assert VAULT_PATH_BAD_COMPONENT in said[0], said

    def test_an_absolute_path_with_a_null_byte_is_refused(self, tmp_path):
        config = _config(tmp_path, alice=UserConfig(vault_path="/etc/va\0ult.kdbx"))
        assert _resolve(config) is None

    def test_the_refusal_line_cannot_be_forged_by_the_path(self, tmp_path, caplog):
        """A TOML string carries a newline and has no length limit.

        Self-inflicted rather than attacker-reachable — a `config.toml` is the
        operator's — but an unflattened value forges a whole record in the
        daemon's own log, which is the rule `secrets_vault._label` already
        applies to the names inside the file.
        """
        config = _config(tmp_path, alice=UserConfig())
        forged = "%s/%s\nWARNING vault_path_refused user=root reason=none" % (
            config.workspace_path, "a" * 500,
        )
        config.users["alice"].vault_path = forged

        with caplog.at_level("WARNING", logger="istota.storage"):
            assert _resolve(config) is None
        said = [
            r.getMessage() for r in caplog.records if r.name == "istota.storage"
        ]
        assert said
        assert "\n" not in said[0]
        assert len(said[0]) < 400

    @pytest.mark.parametrize("user_id", ["", ".", "..", "a/b"])
    def test_a_user_id_that_names_no_child_is_refused(self, tmp_path, user_id):
        """`{root}/{user_id}` is a join, and a join is not the check it reads as.

        The scoping goes through `user_scope.scoped_user_dir` rather than being
        written here, so `""` cannot collapse to `{mount}/Users` — every user's
        directory at once, read with a passphrase and written into one user's
        rows.
        """
        config = _config(tmp_path, workspace=True)
        config.users[user_id] = UserConfig(vault_path="vault.kdbx")
        assert _resolve(config, user_id) is None
