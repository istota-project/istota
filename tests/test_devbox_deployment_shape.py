"""The devbox as a deployed thing: one shape, and the guards on the other one.

The devbox shipped in two shapes from one Dockerfile, and this file used to hold
them against each other — `docker/docker-compose.yml` defined a single-user
service, `deploy/ansible/templates/docker-compose.devbox.yml.j2` renders one per
user, a comment in the first said "keep this entry in sync with" the second, and
until ISSUE-282 nothing enforced it. That is how the compose shape ended up with
no credential socket while `skill.md` promised the capability unconditionally.

The compose service is gone now, and the parity comparison went with it. The bot
could never reach that container: the skill CLI shells in with `docker exec` from
inside the `istota` container, which installs no docker client and mounts no
docker socket, and `render-config.sh` writes no `[devbox]` section so
`devbox.enabled` is always false there. What the service did do was oblige every
Ansible-side change to be mirrored into something nobody could use, which is the
mechanism that produced ISSUE-282 rather than a defence against it.

So this file is now two halves. The Ansible service must carry the properties its
users depend on, none of which a comparison was ever checking. And the compose
shape must keep shipping no devbox, with `docs/deployment/docker.md` saying so —
because a devbox reappearing there, or a docker socket appearing in the `istota`
service, would make that page a lie in the same way ISSUE-282 did.

What this does not check: that the Ansible template is *correct*. It checks the
handful of properties whose violation is silent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker" / "docker-compose.yml"
ANSIBLE = REPO / "deploy" / "ansible"
TEMPLATE = ANSIBLE / "templates" / "docker-compose.devbox.yml.j2"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"

DEVBOX_USER = "alice"


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS_FILE.read_text())


def _resolve(variables: dict, env: Environment) -> dict:
    """Expand `{{ other_var }}` inside the defaults, the way Ansible would.

    Iterates to a fixed point: `istota_repo_dir` is `{{ istota_home }}/istota`
    and `istota_home` is itself `/srv/app/{{ istota_namespace }}`, so one pass
    leaves a template in the output. Bounded, so a genuine cycle fails loudly
    rather than hanging the suite.
    """

    def expand(value):
        if isinstance(value, str) and "{{" in value:
            return env.from_string(value).render(**variables)
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    for _ in range(10):
        expanded = {k: expand(v) for k, v in variables.items()}
        if expanded == variables:
            return variables
        variables = expanded
    raise AssertionError("defaults/main.yml did not reach a fixed point in 10 passes")


def _render_service(**overrides) -> dict:
    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    variables = _resolve(
        {
            **_defaults(),
            # The one host fact the defaults read (istota_browser_cpu_limit).
            # Supplied rather than stubbed away, so a *second* fact appearing in
            # the defaults fails here loudly under StrictUndefined instead of
            # rendering as an empty string.
            "ansible_facts": {"processor_vcpus": 4},
            "istota_devbox_users": [DEVBOX_USER],
            "istota_devbox_proxy_group_gid": 1001,
            # Derived by the role with `getent`, never configured: the value
            # that makes the shared repos mount work is whatever uid the daemon
            # actually runs as. Supplied here for the same reason the gid above
            # is — StrictUndefined would otherwise stop the render.
            "istota_devbox_uid": 999,
            "istota_devbox_gid": 999,
            **overrides,
        },
        env,
    )
    rendered = yaml.safe_load(env.from_string(TEMPLATE.read_text()).render(**variables))
    services = rendered["services"]
    assert len(services) == 1, f"expected one rendered service, got {list(services)}"
    return next(iter(services.values()))


@pytest.fixture(scope="module")
def ansible_service() -> dict:
    """The service as the defaults render it: `backend = none`."""
    return _render_service()


#: What a deployment routing builds into the devbox sets. Values chosen so the
#: three container mounts are distinguishable from each other and from anything
#: the defaults already produce.
CONTAINER_VARS = {
    "istota_developer_enabled": True,
    "istota_developer_container_backend": "devbox",
    "istota_developer_repos_dir": "/srv/repos",
    "istota_security_sandbox_cache_dir": "/srv/repos/.cache",
}


@pytest.fixture(scope="module")
def container_service() -> dict:
    """The service with `backend = devbox` — the shape this whole design is for."""
    return _render_service(**CONTAINER_VARS)


class TestTheAnsibleDevboxCarriesItsCredentialSocket:
    """The socket is what makes `gh`, `glab` and `git push` work inside the
    container, and it is mounted in one place now that the compose shape ships
    no devbox at all. Half-implementing it — the mount without the group, or
    either without the path the image bakes — fails here."""

    CRED_MOUNT = "/run/istota-cred"

    def test_ansible_mounts_the_credential_socket_directory(self, ansible_service):
        assert any(
            str(v).endswith(f":{self.CRED_MOUNT}") for v in ansible_service["volumes"]
        ), "the Ansible devbox no longer mounts the credential proxy socket dir"

    def test_ansible_joins_the_group_that_can_open_it(self, ansible_service):
        """The socket is mode 0660 owned by the istota group. Without the
        supplementary group the mount is present and unusable, which is a
        harder failure to read than no mount at all."""
        assert ansible_service.get("group_add"), (
            "the credential socket is mounted with no group_add — uid 1000 "
            "cannot open a 0660 socket it does not share a group with"
        )

    def test_the_documented_socket_path_is_the_one_the_image_expects(self):
        """The mount point is not arbitrary: the image bakes
        `ISTOTA_CRED_SOCK=/run/istota-cred/sock`, and the wrapper and the git
        credential helper both read it from there."""
        dockerfile = (REPO / "docker" / "devbox" / "Dockerfile").read_text()
        assert f"ISTOTA_CRED_SOCK={self.CRED_MOUNT}/sock" in dockerfile


class TestTheSkillBodyMatchesTheDeployedShapes:
    """`skill.md` is the prompt. It promised the forge capability without
    qualification, which is the half of ISSUE-282 the model actually saw, and
    the same class of error is now available one step further out: telling a
    model about a container that exists on one deployment shape only."""

    @pytest.fixture(scope="class")
    def body(self) -> str:
        return (REPO / "src" / "istota" / "skills" / "devbox" / "skill.md").read_text()

    def test_it_says_the_compose_shape_has_no_devbox(self, body):
        """One sentence has to carry both halves. Two whole-document searches
        pass on a page that says "docker compose" in one paragraph and "no
        devbox" about something else in another, which is the weakness the
        class this replaced also had."""
        sentences = [s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", body)]
        assert any("docker compose" in s and "no devbox" in s for s in sentences), (
            "no single sentence in skill.md tells the model that the docker "
            "compose deployment ships no devbox. It used to say the box was "
            "there and only the forge commands failed, which is the wrong shape "
            "of wrong: a model reading that will try to use it"
        )

    def test_it_still_explains_the_credential_free_case(self, body):
        """Not made moot by the removal: `istota_devbox_proxy_enabled` is an
        Ansible variable and the template gates the socket mount on it, so a
        devbox with no credentials is still reachable — and skill.md is the only
        place the model is told what the wrapper's exit 4 means."""
        assert "credential" in body.lower()

    def test_it_tells_the_model_what_exit_4_means(self, body):
        assert "exit 4" in body.lower()


class TestTheWrapperRefusalNamesTheShape:
    """The other half: a refusal that prints a socket path tells the reader
    nothing about why it is missing."""

    def test_the_no_proxy_message_names_both_deployments(self):
        from istota.forge_cli import NoProxyError, fetch_forge_credentials

        with pytest.raises(NoProxyError) as excinfo:
            fetch_forge_credentials("github", {}, {})
        message = str(excinfo.value).lower()
        assert "ansible" in message
        assert "docker-compose" in message or "docker compose" in message

    def test_it_does_not_lead_with_a_bare_socket_path(self):
        from istota.forge_cli import NoProxyError, fetch_forge_credentials

        with pytest.raises(NoProxyError) as excinfo:
            fetch_forge_credentials("gitlab", {}, {})
        assert not str(excinfo.value).startswith("/")


class TestTheAnsibleVolumeMountsAreHeldInLine:
    """The destinations decide what the container can touch, and on a container
    the model drives the one that matters is `/var/run/docker.sock` —
    root-equivalent, and one line to add.

    An explicit allowlist rather than a comparison: the compose service that
    used to be the other side of it is gone, and "the same as last time" was
    never the property worth holding anyway.
    """

    #: Every mount point the Ansible devbox may have, and what it is for.
    PERMITTED = {
        "/home/dev": "the per-user home volume, which is where its state lives",
        "/run/istota-cred": (
            "the per-user credential proxy socket directory, mounted as the "
            "directory rather than the socket so a daemon restart can recreate "
            "the inode without stranding the container"
        ),
    }

    @staticmethod
    def _mount_points(service: dict) -> set[str]:
        points = set()
        for entry in service.get("volumes", []):
            if isinstance(entry, dict):          # long form
                points.add(entry["target"])
            else:                                 # "source:target[:opts]"
                parts = str(entry).split(":")
                assert len(parts) >= 2, f"unparsable volume entry: {entry!r}"
                points.add(parts[1])
        return points

    def test_the_extraction_finds_something(self, ansible_service):
        assert self._mount_points(ansible_service)

    def test_it_mounts_nothing_unexplained(self, ansible_service):
        extra = self._mount_points(ansible_service) - set(self.PERMITTED)
        assert not extra, (
            f"the Ansible devbox mounts {sorted(extra)} with nothing saying why. "
            f"Add it to PERMITTED with a reason, or take it out of the template"
        )

    def test_every_permitted_mount_is_actually_present(self, ansible_service):
        """A permission for a mount that no longer exists is a standing licence
        for whatever reintroduces the path."""
        points = self._mount_points(ansible_service)
        missing = sorted(set(self.PERMITTED) - points)
        assert not missing, (
            f"PERMITTED excuses {missing} and the template no longer mounts "
            f"them — drop the entries"
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["/var/run/docker.sock", "/run/docker.sock", "/", "/etc", "/proc", "/sys"],
    )
    def test_it_does_not_mount_the_host_in(self, forbidden, ansible_service):
        """Named rather than inferred. The docker socket is root-equivalent, and
        binding it into a container the model drives would hand it the daemon.
        There used to be an allowlist proxy standing in front of it for the
        sandbox; that is retired, and nothing on either side of the boundary
        reaches Docker now."""
        for entry in ansible_service.get("volumes", []):
            source = str(entry).split(":")[0] if not isinstance(entry, dict) else \
                entry.get("source", "")
            assert source != forbidden, (
                f"the Ansible devbox binds {forbidden} into the container"
            )


class TestTheThreeSharedPathsAndNoFourth:
    """`backend = devbox` adds exactly three mounts, and each is load-bearing.

    Every one of them is a directory the container can write, so a fourth
    arriving unannounced is the thing to catch. The `PERMITTED` allowlist next
    door covers the default shape; this covers the one this design is for.
    """

    #: Mount point -> why it is there. Keyed on the *destination*, like
    #: `TestTheAnsibleVolumeMountsAreHeldInLine`.
    PERMITTED = {
        "/home/dev": "the per-user home volume, which is where its state lives",
        "/run/istota-cred": "the credential proxy socket directory",
        "/run/istota-exec/alice": (
            "the exec transport's per-user socket directory — mounted as the "
            "directory rather than the socket file, so a server restart can "
            "recreate the inode without stranding the daemon, and only the "
            "per-user subdirectory so no user's container holds another's"
        ),
        "/srv/repos/alice": (
            "this user's worktrees, at the identical absolute path on both "
            "sides so a working directory needs no translation"
        ),
        "/srv/repos/.cache/alice": (
            "uv's cache, on the same mount as the venv it populates so link(2) "
            "does not return EXDEV"
        ),
    }

    @staticmethod
    def _mounts(service: dict) -> dict[str, str]:
        """`{destination: source}` for every volume entry."""
        found: dict[str, str] = {}
        for entry in service.get("volumes", []):
            if isinstance(entry, dict):
                found[entry["target"]] = str(entry.get("source", ""))
            else:
                parts = str(entry).split(":")
                assert len(parts) >= 2, f"unparsable volume entry: {entry!r}"
                found[parts[1]] = parts[0]
        return found

    def test_the_three_paths_are_mounted(self, container_service):
        mounts = self._mounts(container_service)
        assert "/srv/repos/alice" in mounts
        assert "/srv/repos/.cache/alice" in mounts
        assert "/run/istota-exec/alice" in mounts, (
            "the exec socket directory is not mounted, so nothing routes"
        )

    def test_each_of_the_three_is_at_the_same_spelling_on_both_sides(
        self, container_service
    ):
        """The whole reason a working directory needs no translation. A shim
        sends `os.getcwd()` and the server checks it with `realpath` against its
        own root; if the two sides spelled the tree differently that check would
        refuse every real path."""
        mounts = self._mounts(container_service)
        for dest, source in mounts.items():
            if dest in ("/home/dev", "/run/istota-cred"):
                continue
            assert source == dest, (
                f"{dest} is mounted from {source}; the shared paths must carry "
                f"one spelling"
            )

    def test_only_the_per_user_socket_subdirectory_is_mounted(self, container_service):
        """Mounting the parent would put every user's socket in every user's
        container, which is arbitrary command execution against another user's
        repositories."""
        mounts = self._mounts(container_service)
        for source in mounts.values():
            assert not source.rstrip("/").endswith("-exec"), (
                f"{source} is the socket *parent*, not a per-user subdirectory"
            )

    def test_nothing_else_arrived(self, container_service):
        extra = set(self._mounts(container_service)) - set(self.PERMITTED)
        assert not extra, (
            f"the container-backed devbox mounts {sorted(extra)} with nothing "
            f"saying why. Add it to PERMITTED with a reason, or take it out"
        )

    def test_the_default_shape_mounts_none_of_them(self, ansible_service):
        """The negative control. `backend = none` is every deployment that has
        not opted in, and it must be byte-identical to what it was: no repos
        tree in the container, no cache, no socket."""
        mounts = TestTheThreeSharedPathsAndNoFourth._mounts(ansible_service)
        assert set(mounts) == {"/home/dev", "/run/istota-cred"}

    def test_the_supervisor_is_told_where_the_socket_and_the_repos_are(
        self, container_service
    ):
        """Both are required by the supervisor and neither has a defensible
        default — guessing either would be guessing which user's container this
        is. Without them it holds with the transport down."""
        env = container_service.get("environment") or {}
        assert env.get("ISTOTA_EXEC_SOCKET", "").endswith("/alice/exec.sock")
        assert env.get("ISTOTA_EXEC_REPOS_ROOT") == "/srv/repos/alice"

    def test_the_caches_are_set_on_the_container_not_on_the_wire(
        self, container_service
    ):
        """One spelling per deployment, in the container's own environment,
        which the model cannot reach — which is what lets the protocol carry no
        `env` field at all."""
        env = container_service.get("environment") or {}
        assert env["npm_config_cache"] == "/home/dev/.npm"
        assert env["CARGO_HOME"] == "/home/dev/.cargo"
        assert env["GOMODCACHE"] == "/home/dev/go/pkg/mod"
        assert env["UV_PYTHON_INSTALL_DIR"] == "/home/dev/.uv-python"
        assert env["UV_CACHE_DIR"] == "/srv/repos/.cache/alice"

    def test_the_uv_cache_mount_and_variable_arrive_together(self):
        """A `UV_CACHE_DIR` naming a directory the container does not have is
        worse than no variable: uv would create it on the container's own
        filesystem and every task would silently lose its cache at recreate."""
        service = _render_service(
            **{**CONTAINER_VARS, "istota_security_sandbox_cache_dir": ""}
        )
        env = service.get("environment") or {}
        assert "UV_CACHE_DIR" not in env
        mounts = TestTheThreeSharedPathsAndNoFourth._mounts(service)
        assert not any("cache" in dest for dest in mounts)

    def test_the_image_is_built_at_the_daemons_own_uid(self, container_service):
        """The central invariant of the shared mount. A mismatch fails in both
        directions — the container cannot write into a worktree the daemon
        made, and once that is worked around the daemon cannot unlink a tree the
        container made, so every worktree that ever ran a build becomes
        permanently unreapable."""
        args = container_service["build"]["args"]
        assert str(args["DEV_UID"]) == "999"
        assert str(args["DEV_GID"]) == "999"

    def test_the_uid_args_are_passed_whatever_the_backend(self, ansible_service):
        """Not gated on the backend: the volume is shared with the daemon's own
        `docker cp` path too, and a build arg that appears only on one shape is
        an image that differs between them."""
        args = ansible_service["build"]["args"]
        assert str(args["DEV_UID"]) == "999"


class TestTheAnsibleServiceKeepsItsContainment:
    """The presence half of what the parity comparison used to check.

    `MUST_MATCH` held nine keys equal across the two shapes, and its first act
    was `assert key in ansible_service` — a presence check on the rendered
    template that never needed a second shape to mean anything. Deleting the
    comparison took that with it, and only `tmpfs` is caught elsewhere
    (`tests/test_skills_devbox.py::TestTmpfsMountList`). Without this class the
    template can lose its memory cap, its pid cap or its capability grant with
    the whole suite green.

    `networks` is the sharp one. Drop that key and compose puts the container on
    the project's default network, outside `istota_devbox_network_subnet` — at
    which point every DOCKER-USER rule the role installs is scoped to a subnet
    no container uses and drops nothing, while `doctor.check_devbox_netfilter`
    reads the chain, finds the rules well formed and reports ok.
    `tests/test_ansible_devbox_iptables.py` writes that failure mode down and
    holds the network's *definition*; this holds the service's membership of it.
    """

    #: Key -> the value the deployment expects, or None where the value is a
    #: configurable and only its presence is the property.
    REQUIRED = {
        "image": None,
        "restart": "unless-stopped",
        "networks": ["devbox-net"],
        "mem_limit": None,
        "cpus": None,
        "pids_limit": None,
        # tini at PID 1. `command` below is a supervisor that never calls
        # `wait()` on a process it did not start, so without this anything that
        # outlives the exec server's own reap stays a zombie against
        # `pids_limit` for the life of the container. `dockerd` used to reap
        # `docker exec` children and nobody had to think about it.
        "init": True,
        # The json-file driver is unbounded by default and the supervisor
        # writes a line per server exit, so a server that cannot start at all
        # fills the disk one line at a time.
        "logging": None,
    }

    #: Keys this class used to require and deliberately does not any more. Kept
    #: as a written record rather than deleted, because the class's whole
    #: purpose is that a key leaving it is a decision somebody made:
    #:
    #: `cap_add: [NET_RAW]` — it existed for `ping`, `traceroute`, `mtr` and
    #: `tcpdump`. A build needs none of them, the diagnostics case the original
    #: spec was written for is not what the box is used for, and holding the
    #: capability puts the container in the ISSUE-299 class, where it picks its
    #: own source address and walks past every `-s`-scoped DROP rule.
    #:
    #: `command: ["sleep", "infinity"]` — the container is a service host now,
    #: not an exec target. Its command is the exec server's supervisor, which is
    #: also the image's own `CMD`, so the compose value is belt-and-braces
    #: rather than the property. What matters is that it is *not* `sleep`:
    #: nothing would restart the server after a dockerd restart or a container
    #: OOM, and `sleep` as PID 1 never reaps.
    RETIRED = ("cap_add", "command")

    @pytest.mark.parametrize("key", sorted(RETIRED))
    def test_a_retired_key_is_not_quietly_back(self, key, ansible_service):
        """Both were dropped for a reason, and re-adding one should be as
        deliberate as removing it was."""
        if key == "command":
            # The supervisor is a legitimate value here; `sleep infinity` is the
            # one this design cannot have.
            assert ansible_service.get("command") != ["sleep", "infinity"]
            return
        assert key not in ansible_service, (
            f"the Ansible devbox declares {key!r} again. It was dropped "
            f"deliberately — see RETIRED for why"
        )

    @pytest.mark.parametrize("key", sorted(REQUIRED))
    def test_the_key_is_present(self, key, ansible_service):
        assert key in ansible_service, (
            f"the Ansible devbox template no longer declares {key!r}. Every key "
            f"here changes what the container can do or reach — if dropping it "
            f"is deliberate, take it out of REQUIRED and say why in the commit"
        )

    @pytest.mark.parametrize(
        "key", sorted(k for k, v in REQUIRED.items() if v is not None)
    )
    def test_the_value_is_the_one_the_deployment_needs(self, key, ansible_service):
        assert ansible_service[key] == self.REQUIRED[key], (
            f"the Ansible devbox declares {key}={ansible_service[key]!r}, not "
            f"{self.REQUIRED[key]!r}"
        )

    def test_the_limits_are_set_to_something(self, ansible_service):
        """The three that are role variables rather than literals. Their values
        belong to the operator; that they are set at all does not."""
        for key in ("mem_limit", "cpus", "pids_limit"):
            value = ansible_service.get(key)
            assert value not in (None, "", 0), (
                f"the Ansible devbox renders {key}={value!r}, so the container "
                f"runs with no {key} at all"
            )


class TestTheBuildRecipeIsRootedWhereTheCopiesExpect:
    """Every COPY in the devbox Dockerfile is relative to `docker/devbox`, so a
    context rooted anywhere else fails on the first one. The compose service had
    exactly that wrong — context at the repo root — and nothing caught it,
    because the smoke tier never builds the devbox image.
    """

    DEVBOX_DIR = REPO / "docker" / "devbox"

    @staticmethod
    def _context_and_file(service: dict) -> tuple[str, str]:
        build = service["build"]
        assert isinstance(build, dict), "the short-form build string is not handled"
        return str(build["context"]), str(build.get("dockerfile", "Dockerfile"))

    def test_it_resolves_to_the_devbox_dockerfile(self, ansible_service):
        context, dockerfile = self._context_and_file(ansible_service)
        combined = f"{context.rstrip('/')}/{dockerfile}"
        assert combined.endswith("devbox/Dockerfile"), (
            f"the Ansible devbox builds {combined}, not devbox/Dockerfile"
        )

    def test_the_context_is_rooted_at_docker_devbox(self, ansible_service):
        context, _ = self._context_and_file(ansible_service)
        assert context.rstrip("/").endswith("devbox"), (
            f"the Ansible devbox roots its build context at {context!r}; the "
            f"Dockerfile's COPY paths are relative to docker/devbox and "
            f"resolve against nothing else"
        )

    def test_every_copied_path_exists_under_that_context(self):
        """And the reason the above matters, checked directly: each COPY source
        is a real path inside docker/devbox. This is what a build would fail
        on."""
        sources = [
            line.split()[1]
            for line in (self.DEVBOX_DIR / "Dockerfile").read_text().splitlines()
            if line.startswith("COPY ")
        ]
        assert sources, "no COPY lines found in the devbox Dockerfile"
        missing = [s for s in sources if not (self.DEVBOX_DIR / s).exists()]
        assert not missing, (
            f"the devbox Dockerfile copies {missing}, which do not exist under "
            f"docker/devbox — the build would fail on the first one"
        )

    def test_none_of_them_resolve_from_the_repo_root(self):
        """The negative control for the two above: these paths must *not* exist
        at the repo root, or rooting the context there would have worked and
        the bug this class pins would not have been a bug."""
        sources = [
            line.split()[1]
            for line in (self.DEVBOX_DIR / "Dockerfile").read_text().splitlines()
            if line.startswith("COPY ")
        ]
        resolvable_from_root = [s for s in sources if (REPO / s).exists()]
        assert not resolvable_from_root, (
            f"{resolvable_from_root} resolve from the repo root as well as from "
            f"docker/devbox, so this class no longer distinguishes the two"
        )


class TestTheComposeShapeShipsNoDevbox:
    """The other half. `docs/deployment/docker.md` tells an operator that this
    stack has no devbox and that the skill cannot be enabled on it. Four
    mechanisms make that true, and each is one line away from not being.

    Reintroducing any of them without rewriting that page is the ISSUE-282
    failure again: a shape promising a capability it does not have, or in the
    socket's case a page denying a route that exists.
    """

    DOC = REPO / "docs" / "deployment" / "docker.md"
    RENDER = REPO / "docker" / "istota" / "render-config.sh"
    ISTOTA_DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"
    ANCHOR = "#the-devbox-is-ansible-only"
    HEADING = "### The devbox is Ansible-only"

    @pytest.fixture(scope="class")
    def compose(self) -> dict:
        # `${USER_NAME}` and friends are compose-time interpolation, not YAML,
        # so the document parses as-is.
        return yaml.safe_load(COMPOSE.read_text())

    def test_there_is_no_devbox_service(self, compose):
        assert compose["services"], "the compose file parsed to no services at all"
        offending = sorted(n for n in compose["services"] if "devbox" in n)
        assert not offending, (
            f"docker/docker-compose.yml declares {offending}. Nothing in this "
            f"shape can reach a devbox — if that changed, rewrite the devbox "
            f"section of docs/deployment/docker.md, which says the stack has none"
        )

    def test_no_devbox_volume_or_network_survives(self, compose):
        leftovers = sorted(
            name for group in ("volumes", "networks")
            for name in (compose.get(group) or {})
            if "devbox" in name
        )
        assert not leftovers, (
            f"the devbox service is gone but {leftovers} remain, so compose "
            f"still creates them for nothing"
        )

    #: Naming the socket alone is not enough: bind its *directory* and the
    #: container has the socket. Same list the Ansible side is held to.
    FORBIDDEN_PATHS = frozenset(
        {"/var/run/docker.sock", "/run/docker.sock", "/var/run", "/run", "/"}
    )

    def test_the_istota_service_mounts_no_route_to_the_host_daemon(self, compose):
        mounts = compose["services"]["istota"].get("volumes", [])
        assert mounts, "the istota service lost its volumes; this test is asserting nothing"
        offending = []
        for entry in mounts:
            if isinstance(entry, dict):
                source, target = str(entry.get("source", "")), str(entry.get("target", ""))
            else:
                parts = str(entry).split(":")
                source, target = parts[0], (parts[1] if len(parts) > 1 else "")
            if (
                "docker.sock" in str(entry)
                or source in self.FORBIDDEN_PATHS
                or target in self.FORBIDDEN_PATHS
            ):
                offending.append(entry)
        assert not offending, (
            f"the istota service now mounts {offending}. That is a route to the "
            f"host's Docker, in a shape whose tasks run unsandboxed — if it is "
            f"deliberate, rewrite the devbox section of "
            f"docs/deployment/docker.md, which tells operators there is none"
        )

    #: How a docker client could arrive. The package names are the obvious
    #: route and the only one the first cut of this test checked, which a
    #: control showed was not enough: `docker/istota/Dockerfile` installs `gh`
    #: and `glab` from pinned release tarballs into /usr/local/bin, and
    #: `.claude/rules/deployment.md` records that as the house style over apt.
    #: So a tarball or a `COPY --from` was the likely reintroduction and the
    #: one the test could not see.
    DOCKER_CLIENT_MARKERS = (
        "docker-ce-cli",
        "docker.io",
        "docker-cli",
        "docker-compose-plugin",
        "copy --from=docker",
        "download.docker.com",
        "/usr/local/bin/docker",
        "/usr/bin/docker",
    )

    def test_the_istota_image_installs_no_docker_client(self):
        """The other half of "the daemon has no way in": the skill CLI resolves
        its binary with `shutil.which("docker")`, so a client in the image is
        half the route even with no socket mounted."""
        dockerfile = self.ISTOTA_DOCKERFILE.read_text()
        assert "FROM " in dockerfile, (
            "docker/istota/Dockerfile does not look like a Dockerfile, so this "
            "test is reading the wrong thing and asserting nothing"
        )
        lowered = dockerfile.lower()
        found = [marker for marker in self.DOCKER_CLIENT_MARKERS if marker in lowered]
        assert not found, (
            f"docker/istota/Dockerfile now carries {found}; "
            f"docs/deployment/docker.md says the image has no docker client"
        )

    def test_the_generated_config_has_no_devbox_section(self):
        rendered = self.RENDER.read_text()
        assert "[developer]" in rendered, "render-config.sh no longer looks like itself"
        assert "[devbox]" not in rendered, (
            "render-config.sh now emits a [devbox] section, so the skill can be "
            "switched on in the compose shape. docs/deployment/docker.md says it "
            "cannot — update the page with whatever is now true"
        )

    def test_the_docs_carry_the_heading_the_links_point_at(self):
        """Pinned on the heading rather than the sentence: a link further down
        that page targets the slug this heading generates, and demoting it to
        body text would leave the prose intact and the link broken."""
        doc = self.DOC.read_text()
        assert self.HEADING in doc, (
            f"docs/deployment/docker.md no longer carries {self.HEADING!r}, the "
            f"section explaining that the compose shape ships no devbox"
        )
        assert doc.count(self.ANCHOR) >= 1, (
            f"nothing links to {self.ANCHOR} any more; either the anchor moved "
            f"or the pointers into it were dropped"
        )
