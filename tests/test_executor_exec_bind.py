"""The exec socket bind: the security half of the two-predicate gate.

Writing the shims is gated on configuration alone, because `developer` is a menu
skill and a selection gate would leave the feature not routing on the first turn
of a conversation (`tests/test_developer_shims.py`). Binding the socket is a
different question with a different answer: it is gated on `"developer" in
authorized_skills`, byte for byte the predicate at `_build_network_allowlist`
that already decides whether this task's CONNECT allowlist gets the package
registries and the forge.

**Why the two binds in this file are not symmetric.** The docker-proxy socket is
bound with no gate at all, and that is correct on its own terms: the proxy is an
allowlist, refusing create, run, build, privileged and host-mount, so even an
untrusted-content task reaching it with `curl --unix-socket` cannot escalate.
The exec socket is an unauthenticated arbitrary-command channel into a container
with permissive egress. Ungated, it would hand an email, feed or browse task a
route straight around `_build_network_allowlist`, which is per task and
skill-scoped. Same file, opposite answers, for a reason about the mechanism
rather than about caution.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import (
    Config,
    ContainerConfig,
    DeveloperConfig,
    DevboxConfig,
    SecurityConfig,
)
from istota.executor import build_bwrap_cmd


@pytest.fixture
def sandbox(tmp_path):
    """A config whose devbox exec socket directory exists, and a matching task."""
    repos = tmp_path / "repos" / "alice"
    repos.mkdir(parents=True)
    exec_root = tmp_path / "run" / "istota-exec"
    (exec_root / "alice").mkdir(parents=True)
    (exec_root / "bob").mkdir(parents=True)

    config = Config()
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.db_path = tmp_path / "data" / "istota.db"
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.security = SecurityConfig(sandbox_enabled=True)
    config.developer = DeveloperConfig(
        enabled=True,
        repos_dir=str(tmp_path / "repos"),
        container=ContainerConfig(backend="devbox", exec_socket_dir=str(exec_root)),
    )
    task = db.Task(
        id=1, prompt="p", user_id="alice", source_type="talk", status="running",
    )
    return config, task, exec_root


def _argv(config, task, authorized, *, is_admin=True):
    user_temp = Path(config.temp_dir) / task.user_id
    user_temp.mkdir(parents=True, exist_ok=True)
    with patch("istota.executor._bwrap_available", return_value=True), \
         patch("istota.executor._bwrap_supports", return_value=False), \
         patch("istota.executor._bwrap_supports_remount_ro", return_value=False), \
         patch("platform.system", return_value="Linux"):
        return build_bwrap_cmd(
            ["echo", "hi"], config, task, is_admin, [], user_temp,
            authorized_skills=frozenset(authorized),
        )


def _bind_sources(argv: list[str]) -> set[str]:
    found = set()
    for index, token in enumerate(argv):
        if token in ("--bind", "--ro-bind") and index + 2 < len(argv):
            found.add(argv[index + 1])
    return found


class TestTheGate:
    def test_the_socket_directory_is_bound_when_developer_is_authorized(self, sandbox):
        config, task, exec_root = sandbox

        argv = _argv(config, task, {"developer"})

        assert str((exec_root / "alice").resolve()) in _bind_sources(argv)

    def test_it_is_absent_when_developer_is_not_authorized(self, sandbox):
        """The case the whole gate exists for: an email, feed or browse task on
        a deployment that routes builds into containers. A shim invoked anyway
        exits 120 naming the socket it could not reach — the same class of loud,
        immediate refusal a host-side `npm ci` gets from the CONNECT proxy."""
        config, task, exec_root = sandbox

        argv = _argv(config, task, {"email", "browse"})

        assert str((exec_root / "alice").resolve()) not in _bind_sources(argv)

    def test_an_empty_authorization_set_binds_nothing(self, sandbox):
        config, task, exec_root = sandbox

        argv = _argv(config, task, set())

        assert str((exec_root / "alice").resolve()) not in _bind_sources(argv)

    def test_the_backend_being_off_binds_nothing(self, sandbox):
        """Authorization is necessary, not sufficient: a deployment that has not
        opted in has no transport to reach."""
        config, task, exec_root = sandbox
        config.developer.container.backend = "none"

        argv = _argv(config, task, {"developer"})

        assert str((exec_root / "alice").resolve()) not in _bind_sources(argv)

    def test_a_disabled_developer_skill_binds_nothing(self, sandbox):
        config, task, exec_root = sandbox
        config.developer.enabled = False

        argv = _argv(config, task, {"developer"})

        assert str((exec_root / "alice").resolve()) not in _bind_sources(argv)

    def test_only_this_users_subdirectory_is_bound(self, sandbox):
        """The parent holds every user's socket. Binding it would be arbitrary
        command execution against another user's repositories."""
        config, task, exec_root = sandbox

        sources = _bind_sources(_argv(config, task, {"developer"}))

        assert str(exec_root.resolve()) not in sources
        assert str((exec_root / "bob").resolve()) not in sources

    def test_a_non_admin_authorized_for_developer_still_gets_the_socket(self, sandbox):
        """Deliberate, and it is the per-user repos root that makes it safe. The
        transport reaches `{repos_dir}/{user_id}` and nothing else, so it adds
        no reach past the admin-only repos bind — a non-admin's container sees
        only their own tree, which the sandbox does not bind for them either."""
        config, task, exec_root = sandbox

        argv = _argv(config, task, {"developer"}, is_admin=False)

        assert str((exec_root / "alice").resolve()) in _bind_sources(argv)
        # …and the repos tree is still admin-only on the host side.
        assert str((Path(config.developer.repos_dir) / "alice").resolve()) \
            not in _bind_sources(argv)


class TestTheDockerProxySocket:
    """Its removal is the *next* stage's — the bind and its replacement must not
    coexist in a release, so it goes in the same change that stops the devbox
    skill needing it. What holds today is only that it is not reached by
    accident: `devbox.enabled` defaults False, so a deployment that switched the
    exec transport on without switching the skill's capability on has no docker
    socket in its sandboxes at all.
    """

    def test_it_is_absent_while_the_devbox_capability_is_off(self, sandbox):
        config, task, _ = sandbox

        for authorized in ({"developer"}, {"email"}):
            argv = _argv(config, task, authorized)
            assert "/var/run/docker.sock" not in argv

    def test_it_is_still_bound_where_the_capability_is_on(self, sandbox, tmp_path):
        """Stated as the current behaviour rather than asserted as desirable, so
        that the next stage's deletion turns this red and is noticed rather than
        landing beside a test that never mentioned it."""
        config, task, _ = sandbox
        proxy_dir = tmp_path / "docker-proxy"
        proxy_dir.mkdir()
        (proxy_dir / "alice.sock").write_text("")
        config.devbox = DevboxConfig(
            enabled=True, api_proxy_enabled=True, api_proxy_socket_dir=str(proxy_dir),
        )

        argv = _argv(config, task, {"email"})

        assert "/var/run/docker.sock" in argv


class TestTheRepoBindIsPerUser:
    def test_only_this_users_worktrees_are_bound(self, sandbox, tmp_path):
        config, task, _ = sandbox
        (Path(config.developer.repos_dir) / "bob").mkdir(parents=True, exist_ok=True)

        sources = _bind_sources(_argv(config, task, {"developer"}))

        assert str((Path(config.developer.repos_dir) / "alice").resolve()) in sources
        assert str(Path(config.developer.repos_dir).resolve()) not in sources
        assert str((Path(config.developer.repos_dir) / "bob").resolve()) not in sources
