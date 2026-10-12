"""The devbox as a deployed thing: the image, the skill body, the shipped compose file.

The one deployment shape renders its devboxes with `istota devbox compose-file`
(`src/istota/devbox/compose_file.py`), and `tests/test_devbox_compose_file.py`
holds that render's mount table, containment and transport gate. This file
holds what sits around it: the image's own paths, what the model is told, and
the shipped compose file, which declares no devbox service of its own (they come
from the generated second file) and no route to the host's Docker in the
`daemon` service or the image, which the credential proxy's identity-by-mount
design depends on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from istota.config import Config, ContainerConfig, DeveloperConfig, DevboxConfig
from istota.devbox import compose_file

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker" / "docker-compose.yml"
DEVBOX_DOCKERFILE = REPO / "docker" / "devbox" / "Dockerfile"


def _copy_lines() -> list[list[str]]:
    """Every `COPY` in the devbox Dockerfile, tokenized."""
    return [
        line.split()
        for line in DEVBOX_DOCKERFILE.read_text().splitlines()
        if line.startswith("COPY ")
    ]


def _context_copy_sources() -> list[str]:
    """The COPY sources that come from the *build context*.

    A `COPY --from=<image>` reads a path inside another image, so its sources
    exist nowhere under `docker/devbox` and asserting they do would fail the
    build recipe for being correct.
    """
    sources = []
    for tokens in _copy_lines():
        if any(t.startswith("--from=") for t in tokens):
            continue
        sources.append(tokens[1])
    return sources


def _mount_points(service: dict) -> set[str]:
    return {entry["target"] for entry in service.get("volumes", [])}


@pytest.fixture(scope="module")
def rendered_service() -> dict:
    """alice's devbox as `istota devbox compose-file` renders it, transport on."""
    config = Config()
    config.developer = DeveloperConfig(
        enabled=True,
        repos_dir="/data/repos",
        devbox_proxy_socket_dir="/data/devbox/cred",
        container=ContainerConfig(exec_socket_dir="/data/devbox/exec"),
    )
    config.devbox = DevboxConfig(enabled=True, users=["alice"])
    document = yaml.safe_load(compose_file.render_devbox_compose(config))
    return document["services"]["devbox-alice"]


class TestTheImageBakesTheCredentialSocketPath:
    CRED_MOUNT = "/run/istota-cred"

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

    def test_it_no_longer_says_the_compose_shape_has_no_devbox(self, body):
        """The stack runs devboxes now. A body still telling the model the
        compose deployment has none would be ISSUE-282's shape in reverse."""
        sentences = [s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", body)]
        assert not any("docker compose" in s and "no devbox" in s for s in sentences)

    def test_it_still_explains_the_credential_free_case(self, body):
        """Not made moot by the removal: `[developer] devbox_proxy_enabled`
        gates the credential volume in the compose render, so a devbox with no
        credentials is still reachable — and skill.md is the only
        place the model is told what the wrapper's exit 4 means."""
        assert "credential" in body.lower()

    def test_it_tells_the_model_what_exit_4_means(self, body):
        assert "exit 4" in body.lower()


class TestTheWrapperRefusalNamesTheShape:
    """The other half: a refusal that prints a socket path tells the reader
    nothing about why it is missing."""

    def test_the_no_proxy_message_names_what_provides_the_socket(self):
        from istota.sandbox.forge_cli import NoProxyError, fetch_forge_credentials

        with pytest.raises(NoProxyError) as excinfo:
            fetch_forge_credentials("github", {}, {})
        message = str(excinfo.value).lower()
        assert "[devbox] users" in message
        assert "ansible" in message
        assert "ships no devbox" not in message

    def test_it_does_not_lead_with_a_bare_socket_path(self):
        from istota.sandbox.forge_cli import NoProxyError, fetch_forge_credentials

        with pytest.raises(NoProxyError) as excinfo:
            fetch_forge_credentials("gitlab", {}, {})
        assert not str(excinfo.value).startswith("/")

class TestTheBuildContextHoldsEveryCopy:
    """Every COPY in the devbox Dockerfile is relative to `docker/devbox`, so a
    context rooted anywhere else fails on the first one. The compose service had
    exactly that wrong once — context at the repo root — and nothing caught it,
    because the smoke tier never builds the devbox image.
    `tests/test_devbox_compose_file.py` holds the rendered context itself.
    """

    DEVBOX_DIR = REPO / "docker" / "devbox"

    def test_every_copied_path_exists_under_that_context(self):
        """And the reason the above matters, checked directly: each COPY source
        is a real path inside docker/devbox. This is what a build would fail
        on."""
        sources = _context_copy_sources()
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
        sources = _context_copy_sources()
        resolvable_from_root = [s for s in sources if (REPO / s).exists()]
        assert not resolvable_from_root, (
            f"{resolvable_from_root} resolve from the repo root as well as from "
            f"docker/devbox, so this class no longer distinguishes the two"
        )


class TestTheImageToolchainOutlivesTheHomeVolume:
    """ISSUE-336 — what the image installs must not land in the mount.

    Docker seeds a named volume from the image only when the volume is empty at
    container-*create* time. `/home/dev` is that volume, and `devbox reset`
    empties it and then `docker restart`s the container, which copies nothing —
    so anything the recipe installed under it is gone for good, from a verb
    whose name promises a clean box rather than a smaller one.

    That is what happened: uv and rustup were installed as `dev` into
    `~/.local/bin` and `~/.cargo`, survived a first boot, and disappeared on the
    first reset. uv is the one that matters, because it is in
    `config.DEFAULT_SHIM_COMMANDS` — a missing uv on a devbox-backed deployment
    is `uv sync` failing inside a shim rather than a scratch box missing a tool.

    The mount points come from the rendered service rather than from a literal,
    so a render that moved the volume moves this assertion with it.
    """

    @staticmethod
    def _copy_destination(needle: str) -> tuple[str, list[str]]:
        for tokens in _copy_lines():
            if needle in tokens[1:-1]:
                return tokens[-1], tokens
        raise AssertionError(
            f"the devbox Dockerfile copies no {needle}; if uv now arrives some "
            f"other way, this class has to learn how"
        )

    def test_uv_is_installed_outside_every_mount_point(self, rendered_service):
        destination, _ = self._copy_destination("/uv")
        mounts = _mount_points(rendered_service)
        assert mounts, "the service rendered no mounts, so this proves nothing"
        under = [m for m in mounts if destination.startswith(m.rstrip("/") + "/")]
        assert not under, (
            f"the devbox Dockerfile installs uv at {destination}, which is "
            f"inside {under} — a runtime mount masks it and `devbox reset` "
            f"deletes it (ISSUE-336)"
        )

    def test_uvx_arrives_with_it(self):
        """Both are shimmed, so both have to be in the image."""
        _, tokens = self._copy_destination("/uv")
        assert "/uvx" in tokens, (
            "the image ships uv without uvx, which is shimmed too and would "
            "fail with a missing-command refusal"
        )

    def test_the_uv_image_is_pinned(self):
        _, tokens = self._copy_destination("/uv")
        source = next(t for t in tokens if t.startswith("--from="))
        assert re.fullmatch(r"--from=\S+/uv:\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?", source), (
            f"{source} is not a pinned uv release, so a rebuild is not "
            f"reproducible"
        )

    def test_the_recipe_installs_no_rust_toolchain(self):
        """The other half of the decision, and a real cost: rustup with a
        minimal profile is ~600 MB of image for a toolchain nothing in istota
        uses. `cargo`, `rustc` and `rustup` stay shimmed — the container is
        still where they belong — and a task that wants them installs them at
        the time, into /home/dev, where the next reset correctly takes them.
        """
        body = DEVBOX_DOCKERFILE.read_text()
        offenders = [
            line
            for line in body.splitlines()
            if not line.lstrip().startswith("#")
            and ("rustup.rs" in line or "rustup-init" in line)
        ]
        assert not offenders, (
            f"the devbox image installs a Rust toolchain again: {offenders}. "
            f"It would live in /home/dev and die with the next reset, and it is "
            f"~600 MB nothing in this project needs (ISSUE-336)"
        )


class TestTheShippedComposeFile:
    """The devboxes come from the generated second file, not this one, and
    nothing here is a route to the host's Docker."""

    ISTOTA_DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"

    @pytest.fixture(scope="class")
    def compose(self) -> dict:
        return yaml.safe_load(COMPOSE.read_text())

    def test_there_is_no_devbox_service_of_its_own(self, compose):
        assert compose["services"], "the compose file parsed to no services at all"
        offending = sorted(n for n in compose["services"] if "devbox" in n)
        assert not offending, (
            f"docker/docker-compose.yml declares {offending}. Devboxes are per "
            f"user and rendered by `istota devbox compose-file`; a static one "
            f"here would carry no per-user socket volumes"
        )

    #: Naming the socket alone is not enough: bind its *directory* and the
    #: container has the socket.
    FORBIDDEN_PATHS = frozenset(
        {"/var/run/docker.sock", "/run/docker.sock", "/var/run", "/run", "/"}
    )

    def test_the_daemon_service_mounts_no_route_to_the_host_daemon(self, compose):
        mounts = compose["services"]["daemon"].get("volumes", [])
        assert mounts, "the daemon service lost its volumes; this test is asserting nothing"
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
            f"the daemon service now mounts {offending}. That is root of the VM "
            f"for the daemon and every task; the credential proxy identifies a "
            f"devbox by its socket volume precisely so it needs none"
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
        """Nothing in istota speaks Docker any more (`devbox reset` is an exec
        request, the proxy has no peer inspection), so a client in the image
        could only be a way in for a task."""
        dockerfile = self.ISTOTA_DOCKERFILE.read_text()
        assert "FROM " in dockerfile, (
            "docker/istota/Dockerfile does not look like a Dockerfile, so this "
            "test is reading the wrong thing and asserting nothing"
        )
        lowered = dockerfile.lower()
        found = [marker for marker in self.DOCKER_CLIENT_MARKERS if marker in lowered]
        assert not found, f"docker/istota/Dockerfile now carries {found}"

    def test_the_written_config_leaves_the_devbox_to_the_operator(self):
        """`istota setup` places the socket directories where the render
        accepts them, and enables nothing: which users get a box is the
        operator's call."""
        import tomllib

        from istota.setup_wizard import ContainerAnswers, render_container_config

        answers = ContainerAnswers(user_id="alice", developer_enabled=True, session_secret="s" * 64)
        written = tomllib.loads(render_container_config(answers, inline_credentials=False))
        assert "devbox" not in written
        assert written["developer"]["devbox_proxy_socket_dir"].startswith("/data/")
        assert written["developer"]["container"]["exec_socket_dir"].startswith("/data/")

    def test_the_docs_describe_the_rendered_devboxes(self):
        doc = (REPO / "docs" / "deployment" / "docker.md").read_text()
        assert "### Devboxes" in doc
        assert "#devboxes" in doc
        assert "istota devbox compose-file" in doc
