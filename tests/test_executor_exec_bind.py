"""The exec socket bind: the security half of the two-predicate gate.

Writing the shims is gated on configuration alone, because `developer` is a menu
skill and a selection gate would leave the feature not routing on the first turn
of a conversation (`tests/test_developer_shims.py`). Binding the socket is a
different question with a different answer: it is gated on `"developer" in
authorized_skills`, byte for byte the predicate at `_build_network_allowlist`
that already decides whether this task's CONNECT allowlist gets the package
registries and the forge.

**Why the docker-proxy socket is in this file at all, having been deleted.** It
used to be bound with no gate whatever, and that was correct on its own terms:
the proxy was an allowlist, refusing create, run, build, privileged and
host-mount, so even an untrusted-content task reaching it with
`curl --unix-socket` could not escalate. The exec socket is a different
mechanism — an unauthenticated arbitrary-command channel into a container with
permissive egress — so ungated it would hand an email, feed or browse task a
route straight around `_build_network_allowlist`, which is per task and
skill-scoped. That asymmetry is why one bind could be ungated and this one
cannot, and `TestTheDockerProxySocket` stays as the standing check that the
retired one did not come back.
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
from istota.executor import SandboxProfile, build_bwrap_cmd


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
        container=ContainerConfig(exec_socket_dir=str(exec_root)),
    )
    # The backend is derived, so this is what turns the transport on.
    config.devbox = DevboxConfig(enabled=True)
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
            profile=SandboxProfile.CLAUDE,
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
        opted in has no transport to reach.

        `[devbox] enabled` is what says so now — the `backend` key it replaced
        could be off while the devbox was on, which is the pairing that made a
        skill whose every verb refused.
        """
        config, task, exec_root = sandbox
        config.devbox.enabled = False

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
    """Gone, in both directions, and that is the whole of this class.

    The bind used to be unconditional: `cp`, `restart`, `inspect` and the raw
    HTTP surface were reachable from every task of every user on a devbox
    deployment. It went in the same change that stopped the devbox skill needing
    it, so the two never coexisted in a release.

    The capability being *on* is the case worth naming, because that is the one
    that used to bind. A deployment with `devbox.enabled` and a live devbox
    still gets no Docker socket and no `docker` binary in any sandbox.
    """

    def test_it_is_absent_while_the_devbox_capability_is_off(self, sandbox):
        config, task, _ = sandbox

        for authorized in ({"developer"}, {"email"}):
            argv = _argv(config, task, authorized)
            assert "/var/run/docker.sock" not in argv

    def test_it_is_absent_where_the_capability_is_on(self, sandbox, tmp_path):
        config, task, _ = sandbox
        cli = tmp_path / "docker"
        cli.write_text("#!/bin/sh\n")
        config.devbox = DevboxConfig(enabled=True, docker_cli=str(cli))

        for authorized in ({"developer"}, {"email"}):
            argv = _argv(config, task, authorized)
            assert "/var/run/docker.sock" not in argv
            # The explicit read-only bind of the client binary went too. It is
            # not what makes Docker unreachable — `/usr` is bound whole, so
            # `/usr/bin/docker` is in the namespace on any host that installs
            # the client — but it was there only to serve the socket bind above,
            # and a bind with nothing behind it is a claim about the boundary
            # that is not the boundary.
            assert str(cli) not in _bind_sources(argv)
            assert str(cli) not in argv


class TestTheRepoBindIsPerUser:
    def test_only_this_users_worktrees_are_bound(self, sandbox, tmp_path):
        config, task, _ = sandbox
        (Path(config.developer.repos_dir) / "bob").mkdir(parents=True, exist_ok=True)

        sources = _bind_sources(_argv(config, task, {"developer"}))

        assert str((Path(config.developer.repos_dir) / "alice").resolve()) in sources
        assert str(Path(config.developer.repos_dir).resolve()) not in sources
        assert str((Path(config.developer.repos_dir) / "bob").resolve()) not in sources
