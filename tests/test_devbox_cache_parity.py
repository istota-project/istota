"""The package cache has one location, and three files have to spell it the same.

The host writes uv's and npm's caches under `{repos_dir}/{user_id}/.package-caches`
(`executor.SANDBOX_CACHE_UV` / `SANDBOX_CACHE_NPM` name the subdirectories), the
sweeper reclaims by walking those same two names, and the devbox compose template
points the *container's* tools at them. Nothing connected the three, and two of
them were wrong.

Measured on a live deployment: the template set `UV_CACHE_DIR` to the cache root
rather than to `{root}/uv`, so the container wrote `archive-v0/` as a sibling of
the host's `uv/` — 401 MB against 404 MB of the same wheels. A `uv sync` in the
container re-downloaded what was already on disk one directory over, and the
sweeper's `uv cache prune` never saw it, because the sweeper only ever points uv
at `{root}/uv`. It could not even report the overage properly: its "outside uv/
and npm/, which neither reclaim verb can touch" line is precisely this shape, and
it was the only symptom visible from the host. `npm_config_cache` was worse —
`/home/dev/.npm`, not on the shared mount at all, so a second copy accumulated
inside the container's home volume and nothing on the host could reach it.

Neither is the kind of defect a test of one side finds: each file was
self-consistent. What was missing is the assertion that they agree, which is what
this file is. It reads the rendered template rather than the Jinja source, so a
change to how the path is built is still caught as long as the result differs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from istota.executor import SANDBOX_CACHE_NPM, SANDBOX_CACHE_UV
from istota.sandbox_cache_sweeper import CACHE_NPM, CACHE_UV, CACHE_ROOT_NAME

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "deploy" / "ansible" / "templates"

REPOS_DIR = "/srv/repos"
USER = "alice"


@pytest.fixture(scope="module")
def container_env() -> dict:
    """The `environment:` block the template renders for a devbox user."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    rendered = env.get_template("docker-compose.devbox.yml.j2").render(
        istota_namespace="istota",
        istota_devbox_users=[USER],
        istota_devbox_image="istota-devbox:latest",
        istota_devbox_container_prefix="devbox-",
        istota_devbox_uid="999",
        istota_devbox_gid="989",
        istota_devbox_proxy_enabled=True,
        istota_devbox_proxy_group_gid="989",
        istota_devbox_proxy_socket_dir="/var/run/istota",
        istota_devbox_mem_limit="4g",
        istota_devbox_cpus=2,
        istota_devbox_pids_limit=512,
        istota_devbox_log_max_size="10m",
        istota_devbox_log_max_file=3,
        istota_devbox_network_name="istota-devbox-net",
        istota_devbox_network_subnet="172.30.0.0/24",
        istota_repo_dir="/srv/app/istota/istota",
        istota_home="/srv/app/istota",
        istota_developer_enabled=True,
        istota_developer_repos_dir=REPOS_DIR,
        istota_developer_container_exec_socket_dir="/run/istota-exec",
        istota_developer_container_idle_timeout_seconds=3600,
    )
    service = yaml.safe_load(rendered)["services"][f"devbox-{USER}"]
    return service["environment"]


def _host_cache_root() -> str:
    """What `resolve_sandbox_cache_dir` derives for this user, as a string."""
    return f"{REPOS_DIR}/{USER}/{CACHE_ROOT_NAME}"


class TestTheThreeSpellingsAgree:
    def test_the_two_modules_name_the_same_subdirectories(self):
        """The host writer and the sweeper, first — they are one import apart
        and still worth pinning, since the sweeper restates them rather than
        importing them (its own comment says so)."""
        assert SANDBOX_CACHE_UV == CACHE_UV
        assert SANDBOX_CACHE_NPM == CACHE_NPM

    def test_the_container_uv_cache_is_the_hosts_uv_cache(self, container_env):
        assert container_env["UV_CACHE_DIR"] == f"{_host_cache_root()}/{CACHE_UV}"

    def test_the_container_npm_cache_is_the_hosts_npm_cache(self, container_env):
        assert container_env["npm_config_cache"] == f"{_host_cache_root()}/{CACHE_NPM}"

    def test_neither_is_the_bare_cache_root(self, container_env):
        """The exact live defect for uv: a path that looks right, is inside the
        right mount, and is one level too shallow — so uv puts `archive-v0/`
        where the sweeper expects a `uv/` directory to be."""
        root = _host_cache_root()

        assert container_env["UV_CACHE_DIR"] != root
        assert container_env["npm_config_cache"] != root

    def test_neither_escapes_the_shared_mount(self, container_env):
        """The exact live defect for npm: `/home/dev/.npm` is in the container's
        own volume, which no host process can reach and no sweep can bound."""
        for name in ("UV_CACHE_DIR", "npm_config_cache"):
            assert container_env[name].startswith(f"{REPOS_DIR}/{USER}/"), name

    def test_the_caches_the_host_does_not_share_stay_in_the_home_volume(
        self, container_env
    ):
        """The control. Cargo, Go and uv's managed interpreters are the
        container's alone — the host writes none of them — so they belong in
        `/home/dev` and must not be dragged onto the shared mount by a
        well-meaning sweep of this file.
        """
        for name in ("CARGO_HOME", "GOMODCACHE", "UV_PYTHON_INSTALL_DIR"):
            assert container_env[name].startswith("/home/dev/"), name
