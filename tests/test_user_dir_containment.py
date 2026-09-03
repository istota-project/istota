"""A user id that does not name a child of ``Users/`` binds nothing (ISSUE-402).

``mount / "Users" / task.user_id`` is a plain join, and ``pathlib`` discards an
empty component: ``Path("/mnt") / "Users" / ""`` is ``Path("/mnt/Users")``, the
*parent of every user's directory*, which the plan then emitted as a read-write
bind. ``.`` collapses the same way and an absolute component replaces the root
outright, so ``user_id = "/etc"`` bound ``/etc`` read-write into the sandbox.

The repos half of exactly this was already guarded — ``get_user_repos_dir``
applies a containment equality and its docstring names the three values
truthiness lets through — so the codebase held both the correct pattern and the
reasoning for it, applied to one of the two joins. :func:`scoped_user_dir` is
now the one statement of that rule and this file is what holds every join to
it.

**Every assertion here has to name the collapsed path rather than the absence
of the user's own.** A bind that fell back to the mount root, or a projection
that dropped the entry and kept a wider ancestor, satisfies "alice's directory
is not bound" while being the exposure itself — the same reason
``TestPerUserReposDir`` asks whether bob's tree is *reachable from* a bind
rather than whether it is named by one.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, SecurityConfig
from istota.executor import build_bwrap_cmd, image_bind_roots, native_fs_roots
from istota.sandbox_plan import SandboxProfile, build_mount_plan
from istota.user_scope import scoped_user_dir

#: Every component that does not name a child of the root. `""` and `"."` are
#: dropped by `PurePath`, `".."` is a child by name and the parent on disk, and
#: a nested one goes deeper than the layout describes. The absolute case is not
#: in the list because naming it needs a real directory: see
#: :meth:`TestTheBind.test_an_absolute_user_id_does_not_bind_the_path_it_names`.
BAD_IDS = ["", ".", "..", "a/b"]


@pytest.fixture
def config(tmp_path):
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Users" / "bob").mkdir(parents=True)
    (mount / "Channels" / "room123").mkdir(parents=True)
    (mount / "Talk").mkdir()
    db_file = tmp_path / "data" / "istota.db"
    db_file.parent.mkdir(parents=True)
    db_file.touch()
    return Config(
        db_path=db_file,
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(sandbox_enabled=True),
    )


def _task(user_id):
    return db.Task(
        id=1, prompt="t", user_id=user_id, source_type="talk",
        status="running", conversation_token="room123",
    )


def _user_temp_for(config, user_id):
    """The workspace bind's real shape: `{temp_dir}/{user_id}`."""
    path = config.temp_dir / user_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _user_temp(config, task):
    """What the caller would pass for this task.

    A plain join, exactly as `executor.get_user_temp_dir` does it — including
    for an id that collapses, which is the input the refusal exists for. The
    `mkdir` is `config.temp_dir` itself in that case, which is the point.
    """
    path = config.temp_dir / str(task.user_id).lstrip("/")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _argv(config, task, *, user_temp=None, resources=None, **kwargs):
    with patch("istota.executor._bwrap_available", return_value=True):
        return build_bwrap_cmd(
            ["cmd"], config, task, False, resources or [],
            user_temp if user_temp is not None else _user_temp(config, task),
            profile=SandboxProfile.NATIVE, **kwargs,
        )


def _bind_sources(argv):
    """Every source path bwrap is told to mount, **resolved**, whatever the verb.

    Resolved, and that is the difference between an assertion that can fail and
    one that cannot. `render_bwrap_argv` emits the source as written, and the
    pre-fix plan bound `{mount}/Users/..` verbatim — which is a real read-write
    bind of the whole mount root at `mount(2)`, but compares unequal to
    `{mount}/Users` as a string and is not an ancestor of `{mount}/Users/bob`
    under `is_relative_to`, which is lexical. So the `".."` case of every
    assertion below was green against the unfixed code. The kernel resolves at
    use time; the test has to ask the question the kernel answers.
    """
    verbs = ("--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind")
    out = []
    for i, a in enumerate(argv):
        if a in verbs and i + 2 < len(argv):
            try:
                out.append(Path(argv[i + 1]).resolve())
            except OSError:
                continue
    return out


class TestTheRule:
    """:func:`scoped_user_dir` itself, before any consumer."""

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_a_component_that_is_not_a_child_is_refused(self, tmp_path, user_id):
        assert scoped_user_dir(tmp_path, user_id) is None

    def test_an_ordinary_id_is_the_join(self, tmp_path):
        assert scoped_user_dir(tmp_path, "alice") == tmp_path / "alice"

    def test_a_symlink_out_of_the_root_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "elsewhere").mkdir()
        (root / "alice").symlink_to(tmp_path / "elsewhere")
        assert scoped_user_dir(root, "alice") is None

    def test_the_path_comes_back_as_written(self, tmp_path):
        """Not resolved: `_bind` uses the string it is handed as the
        in-namespace destination, so a resolved root would land a symlinked
        deployment at a different name from everything bound under it."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        (real / "alice").mkdir()
        assert scoped_user_dir(link, "alice") == link / "alice"

    def test_a_user_id_of_the_wrong_type_is_refused_rather_than_raising(self, tmp_path):
        assert scoped_user_dir(tmp_path, None) is None
        assert scoped_user_dir(tmp_path, 7) is None

    @pytest.mark.parametrize("user_id", [" alice", "alice ", "\talice"])
    def test_surrounding_whitespace_is_refused(self, tmp_path, user_id):
        """Not about containment — `" alice"` names a real child. It is about
        two consumers disagreeing: `skill_host_paths` reads `ISTOTA_USER_ID`
        through `.strip()`, so the sandbox would bind `{mount}/Users/ alice`
        while the host-side allowlist admitted `{mount}/Users/alice`."""
        assert scoped_user_dir(tmp_path, user_id) is None

    def test_a_backslash_is_an_ordinary_character(self, tmp_path):
        """`DOMAIN\\user` is what an LDAP-backed Nextcloud hands out, and on
        POSIX it is a genuinely contained child — refusing it would narrow
        nothing and raise out of `create_task` for that user's every task."""
        (tmp_path / "DOMAIN\\user").mkdir()
        assert scoped_user_dir(tmp_path, "DOMAIN\\user") == tmp_path / "DOMAIN\\user"

    def test_a_symlink_cycle_never_raises_and_never_escapes(self, tmp_path):
        """A cycle at the user's own name, whose handling is version-dependent.

        `Path.resolve()` raises `RuntimeError` on a loop through 3.12 and hands
        the path back as written from 3.13, so the answer is either `None` or
        the contained child depending on the interpreter. Both are acceptable
        and neither is an exposure; what must hold on both is that nothing
        raises out of a function three sandbox builders call, and that nothing
        outside the root comes back. `RuntimeError` is in the caught tuple for
        this: it is not in `OSError`'s hierarchy, and without it 3.12 raises
        straight through `build_mount_plan`.
        """
        (tmp_path / "a").symlink_to(tmp_path / "b")
        (tmp_path / "b").symlink_to(tmp_path / "a")
        result = scoped_user_dir(tmp_path, "a")
        assert result is None or result == tmp_path / "a"


class TestTheSandboxRefusal:
    """An id that cannot name a directory gets no namespace at all.

    `user_temp_dir` is `config.temp_dir / user_id` — the same plain join one
    directory over — and `config.temp_dir` holds `.control/{every_user_id}/`,
    every other task's assembled system prompt. It is bound **read-write**, and
    the `extra_ro_binds` re-bind covers only this task's own control directory,
    so a collapsed id put every user's standing instructions in the namespace,
    writable. `.claude/rules/executor.md` already recorded the containment for
    that join as living in the sandbox plan; it did not.

    A raise rather than a dropped bind: `user_temp_dir` is also the `--chdir`
    target and the deferred-op directory, so a namespace without it is broken
    rather than narrower, and bwrap would fail on the chdir naming nothing.
    """

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_the_plan_refuses(self, config, user_id):
        task = _task(user_id)
        with pytest.raises(ValueError, match="does not name a directory under"):
            build_mount_plan(
                config, task, False, [], _user_temp(config, task),
                profile=SandboxProfile.NATIVE,
            )

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_the_argv_builder_refuses(self, config, user_id):
        with pytest.raises(ValueError):
            _argv(config, _task(user_id))

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_the_projection_refuses(self, config, user_id):
        """`native_fs_roots` catches `ValueError` around its own workspace
        validation and not around the plan, so the refusal propagates rather
        than degrading to an unconfined root list."""
        task = _task(user_id)
        with patch("istota.executor._bwrap_available", return_value=True):
            with pytest.raises(ValueError):
                native_fs_roots(config, task, False, [], _user_temp(config, task))

    def test_the_shared_temp_root_is_never_bound(self, config):
        """The exposure named directly, with its control.

        Without the refusal this argv carried `config.temp_dir` itself as a
        read-write bind. The control matters more than usual here: every
        assertion above is satisfied by a builder that refuses everything.
        """
        with pytest.raises(ValueError):
            _argv(config, _task(""))

        own = _user_temp_for(config, "alice")
        sources = _bind_sources(_argv(config, _task("alice"), user_temp=own))
        assert own.resolve() in sources
        assert config.temp_dir.resolve() not in sources


class TestTheMountJoinIsGuardedSeparately:
    """The two joins are not one guard wearing two names.

    An id can be scopable under `config.temp_dir` and not under
    `{mount}/Users` — a symlink at `{mount}/Users/alice` leading out of the
    tree is exactly that — so the plan is built, the workspace is bound, and
    the mount bind is the one that has to be refused on its own. Without a case
    that separates them the mount guard would be unreachable through this
    entry point and untested.
    """

    def _relocated(self, config, tmp_path):
        users = config.nextcloud_mount_path / "Users"
        (users / "alice").rmdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (users / "alice").symlink_to(elsewhere)
        return elsewhere

    def test_the_relocated_user_dir_is_not_bound(self, config, tmp_path):
        elsewhere = self._relocated(config, tmp_path)
        sources = _bind_sources(
            _argv(config, _task("alice"), user_temp=_user_temp_for(config, "alice"))
        )
        assert elsewhere.resolve() not in sources
        assert (config.nextcloud_mount_path / "Users").resolve() not in sources

    def test_the_workspace_is_still_bound(self, config, tmp_path):
        """The control: the plan was built, so the assertion above is the mount
        guard acting alone rather than the workspace refusal firing again."""
        self._relocated(config, tmp_path)
        own = _user_temp_for(config, "alice")
        assert own.resolve() in _bind_sources(
            _argv(config, _task("alice"), user_temp=own)
        )

    def test_the_projection_agrees(self, config, tmp_path):
        """`native_fs_roots` reads the same plan entry, so the mount guard
        reaches the native brain's write roots without a second derivation."""
        elsewhere = self._relocated(config, tmp_path)
        task = _task("alice")
        with patch("istota.executor._bwrap_available", return_value=True):
            read, write, _denied = native_fs_roots(
                config, task, False, [], _user_temp_for(config, "alice"),
            )
        users_root = (config.nextcloud_mount_path / "Users").resolve()
        assert elsewhere.resolve() not in write
        assert users_root not in write and users_root not in read

    def test_a_resource_under_another_user_is_not_skipped_as_covered(
        self, config, tmp_path,
    ):
        """The second join is a *comparison*, and it collapsed the same way.

        `build_mount_plan` skips a per-resource mount already covered by the
        user-directory bind. With a collapsed path as the comparison, every
        resource anywhere under `Users/` read as covered — by a bind that is
        not emitted. Fail-closed in the same direction: no scoped user
        directory means nothing is covered by one.
        """
        self._relocated(config, tmp_path)
        (config.nextcloud_mount_path / "Users" / "bob" / "docs").mkdir()
        resource = db.UserResource(
            id=1, user_id="alice", resource_type="folder",
            resource_path="Users/bob/docs", display_name=None,
            permissions="read",
        )
        plan = build_mount_plan(
            config, _task("alice"), False, [resource],
            _user_temp_for(config, "alice"), profile=SandboxProfile.NATIVE,
        )
        assert "user_resource" in [m.reason for m in plan.mounts], (
            "the resource was skipped as covered by a user-directory bind that "
            "was never emitted"
        )


class TestTheProjectionForAnOrdinaryId:
    """The control for every refusal above."""

    def test_an_ordinary_user_id_is_still_a_write_root(self, config):
        task = _task("alice")
        with patch("istota.executor._bwrap_available", return_value=True):
            _read, write, _denied = native_fs_roots(
                config, task, False, [], _user_temp(config, task),
            )
        assert (config.nextcloud_mount_path / "Users" / "alice").resolve() in write


class TestTheImageAttachmentRoots:
    """`image_bind_roots` names the roots an attachment may be copied from, and
    derives the same join independently of the plan."""

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_the_users_root_is_not_a_root(self, config, user_id):
        task = _task(user_id)
        roots = image_bind_roots(config, task, _user_temp(config, task))
        users_root = (config.nextcloud_mount_path / "Users").resolve()
        assert users_root not in roots

    @pytest.mark.parametrize("user_id", BAD_IDS)
    def test_no_other_users_directory_is_a_root(self, config, user_id):
        """Reachability, not naming — `image_bind_roots` resolves its own
        entries, so `..` reaches the mount root under a name that equals
        neither of the two paths the test above compares against."""
        task = _task(user_id)
        roots = image_bind_roots(config, task, _user_temp(config, task))
        bob = (config.nextcloud_mount_path / "Users" / "bob").resolve()
        exposed = [r for r in roots if bob == r or bob.is_relative_to(r)]
        assert exposed == [], f"user_id={user_id!r} exposes bob's tree: {exposed}"

    def test_an_ordinary_user_id_still_gets_its_own_root(self, config):
        task = _task("alice")
        roots = image_bind_roots(config, task, _user_temp(config, task))
        assert (config.nextcloud_mount_path / "Users" / "alice").resolve() in roots


class TestTheHostPathAllowlist:
    """`skill_host_paths` guards the conversation token against `.`, `..` and a
    separator and left the user id unguarded beside it — the same asymmetry, in
    the allowlist a host-side skill CLI is scoped by."""

    @pytest.mark.parametrize("user_id", [i for i in BAD_IDS if i])
    def test_a_user_id_that_does_not_scope_yields_no_user_root(
        self, config, monkeypatch, user_id,
    ):
        from istota import skill_host_paths

        mount = config.nextcloud_mount_path
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        roots = skill_host_paths.allowed_host_roots(writable=True)
        users_root = (mount / "Users").resolve()
        assert users_root not in roots
        assert not any(
            (mount / "Users" / "bob").resolve().is_relative_to(r) for r in roots
        ), f"user_id={user_id!r} reaches bob's directory: {roots}"

    def test_an_ordinary_user_id_still_gets_its_own_root(self, config, monkeypatch):
        from istota import skill_host_paths

        mount = config.nextcloud_mount_path
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        roots = skill_host_paths.allowed_host_roots(writable=True)
        assert (mount / "Users" / "alice").resolve() in roots


class TestTheProducer:
    """The second layer, at the source. The joins fail closed on their own; this
    is what stops the row existing at all.

    `db.create_task` defaulted `user_id` to `""` and validated nothing, while
    `tasks.user_id` is `TEXT NOT NULL` — which SQLite satisfies with `''`. Every
    network-facing producer gates on membership in `config.users`, and the
    subtask path pins the id to the parent row, so nothing an inbound message
    reaches could write one; what could is `istota task -u`, `istota repl -u`,
    `execute_task_interactive` and the omitted argument itself.
    """

    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "framework.db"
        db.init_db(path)
        return path

    @pytest.mark.parametrize("user_id", ["", "   ", ".", "..", "a/b", "/etc", None, 7])
    def test_a_user_id_that_cannot_scope_is_refused(self, db_path, user_id):
        with db.get_db(db_path) as conn:
            with pytest.raises(ValueError, match="cannot name a per-user directory"):
                db.create_task(conn, prompt="p", user_id=user_id, source_type="cli")

    def test_the_omitted_argument_is_refused(self, db_path):
        """The default is still `""` so the parameter order is unchanged; what
        changed is that the default no longer produces a row."""
        with db.get_db(db_path) as conn:
            with pytest.raises(ValueError):
                db.create_task(conn, "prompt with no owner")

    def test_no_row_is_written(self, db_path):
        with db.get_db(db_path) as conn:
            with pytest.raises(ValueError):
                db.create_task(conn, prompt="p", user_id="", source_type="cli")
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    @pytest.mark.parametrize("user_id", ["alice", "first.last", "user-1", "a_b", "Ünïcode"])
    def test_an_ordinary_user_id_is_accepted(self, db_path, user_id):
        """The control, and the dotted name specifically: `.` and `..` are
        refused as whole values, never as substrings."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="p", user_id=user_id, source_type="cli")
            assert task_id > 0
            assert db.get_task(conn, task_id).user_id == user_id


class TestTheMemorySkillRoots:
    """The same allowlist shape in the two memory skills.

    `memory_search`'s `_indexable_roots` says in its own docstring that an
    unbounded read "would let a task index another user's workspace and then
    retrieve the contents through `search`" — and built its one root by the
    plain join. `memory`'s `_user_id` checked emptiness only, while two joins
    below it build a *containment base* out of the result.
    """

    @pytest.mark.parametrize("user_id", [i for i in BAD_IDS if i.strip()])
    def test_indexable_roots_drops_the_users_root(self, config, monkeypatch, user_id):
        from istota.skills.memory_search import _indexable_roots

        mount = config.nextcloud_mount_path
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        roots = _indexable_roots(user_id)
        bob = (mount / "Users" / "bob").resolve()
        assert (mount / "Users").resolve() not in roots
        assert not any(bob == r or bob.is_relative_to(r) for r in roots), (
            f"user_id={user_id!r} makes bob's workspace indexable: {roots}"
        )

    def test_indexable_roots_keeps_an_ordinary_users_own(self, config, monkeypatch):
        from istota.skills.memory_search import _indexable_roots

        mount = config.nextcloud_mount_path
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        assert (mount / "Users" / "alice").resolve() in _indexable_roots("alice")

    @pytest.mark.parametrize("user_id", [i for i in BAD_IDS if i.strip()])
    def test_the_memory_skill_refuses_the_id(self, monkeypatch, user_id):
        from istota.skills import memory as memory_skill

        monkeypatch.setenv("ISTOTA_USER_ID", user_id)
        with pytest.raises(SystemExit):
            memory_skill._user_id()

    def test_the_memory_skill_accepts_an_ordinary_id(self, monkeypatch):
        from istota.skills import memory as memory_skill

        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        assert memory_skill._user_id() == "alice"


class TestOneBadRowDoesNotCostTheBatch:
    """`create_task` raising must fail its own row, not the tick.

    The briefing and scheduled-job loops share one transaction, so an escaping
    `ValueError` discarded every earlier user's task *and* their `last_run`
    stamp — then repeated the whole thing next tick, forever. The guard is
    meant to be fail-closed, not fail-batch.
    """

    @pytest.fixture
    def db_path(self, tmp_path):
        path = tmp_path / "framework.db"
        db.init_db(path)
        return path

    def test_the_rows_around_a_refused_one_still_land(self, db_path):
        rows = [("alice", "ok"), ("", "refused"), ("bob", "also ok")]
        created = []
        with db.get_db(db_path) as conn:
            for user_id, prompt in rows:
                try:
                    created.append(db.create_task(
                        conn, prompt=prompt, user_id=user_id, source_type="briefing",
                    ))
                except ValueError:
                    continue
        assert len(created) == 2
        with db.get_db(db_path) as conn:
            prompts = {
                r[0] for r in conn.execute("SELECT prompt FROM tasks").fetchall()
            }
        assert prompts == {"ok", "also ok"}, (
            "the refusal rolled back the rows that had already been written"
        )

    def test_the_refusal_writes_nothing_of_its_own(self, db_path):
        """Why catching and continuing is safe at all: `create_task` validates
        before it executes any statement, so a caught refusal leaves no partial
        row for the surrounding transaction to commit."""
        with db.get_db(db_path) as conn:
            with pytest.raises(ValueError):
                db.create_task(conn, prompt="x", user_id=".", source_type="briefing")
            db.create_task(conn, prompt="after", user_id="alice", source_type="briefing")
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
