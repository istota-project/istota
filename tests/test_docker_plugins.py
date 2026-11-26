"""Skill plugins beside the image: the mount point, the `.pth` and the overlay.

Spec: external-skill-packages, Stage 5. A plugin never goes into the image.
The image's venv reads one extra `sys.path` entry, `/app/plugins/installed/site`,
through `istota-plugins.pth`, and `docker/docker-compose.plugins.yml` binds the
stack's `plugins/` directory at `/app/plugins`, read-only, into the two
services that load the skill index. These are text pins: no default-suite tier
builds the image, and the image tier (Stage 7) is what runs it.

The paths are named in four places that must agree: the Dockerfile's `.pth`
line, the overlay's bind target, `lib/skill_module.PLUGIN_SITE_DIR` (what
`check_tree` leaves out of the venv) and `doctor.PLUGIN_WHEEL_DIR` (the wheels
doctor compares with what loaded). `istota-stack plugins sync` names the wheel
directory as the one-shot sees it, held here too.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml

from istota import doctor
from istota.lib import skill_module

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"
OVERLAY = REPO / "docker" / "docker-compose.plugins.yml"
MOUNT = PurePosixPath("/app/plugins")


def _dockerfile_instructions() -> list[str]:
    """Each instruction as one line: comments dropped, continuations joined."""
    lines = [line for line in DOCKERFILE.read_text().splitlines() if not line.lstrip().startswith("#")]
    return [" ".join(part.split()) for part in "\n".join(lines).replace("\\\n", " ").split("\n") if part.strip()]


def _plugins_run() -> str:
    runs = [i for i in _dockerfile_instructions() if i.startswith("RUN ") and "istota-plugins.pth" in i]
    assert len(runs) == 1, runs
    return runs[0]


class TestTheImage:
    def test_it_makes_the_mount_point_on_the_read_only_root(self):
        assert re.search(r"\bmkdir -p /app/plugins\b", _plugins_run())

    def test_the_pth_names_the_synced_tree_and_nothing_else(self):
        run = _plugins_run()
        assert f"echo {skill_module.PLUGIN_SITE_DIR} >" in run
        assert run.count("/app/plugins/installed") == 1, run

    def test_the_pth_goes_into_the_venvs_purelib(self):
        run = _plugins_run()
        assert "/app/.venv/bin/python" in run
        assert 'sysconfig.get_path("purelib")' in run
        assert run.rstrip('"').endswith("/istota-plugins.pth")

    def test_it_comes_after_the_last_project_install(self):
        """A later `uv sync` owns site-packages and could drop a file it did not install."""
        instructions = _dockerfile_instructions()
        last_sync = max(i for i, line in enumerate(instructions) if line.startswith("RUN uv sync"))
        assert instructions.index(_plugins_run()) > last_sync

    def test_the_venv_reads_no_plugin_path_from_the_environment(self):
        assert not [i for i in _dockerfile_instructions() if "PYTHONPATH" in i]


class TestTheOverlay:
    def _doc(self) -> dict:
        return yaml.safe_load(OVERLAY.read_text())

    def test_it_binds_the_plugins_directory_read_only_into_daemon_and_web_alone(self):
        services = self._doc()["services"]
        assert set(services) == {"daemon", "web"}
        for name, service in services.items():
            assert set(service) == {"volumes"}, name
            assert service["volumes"] == [
                "${ISTOTA_PLUGINS_DIR:?istota-stack sets ISTOTA_PLUGINS_DIR}:/app/plugins:ro"], name

    def test_the_paths_agree(self):
        target = PurePosixPath(self._doc()["services"]["daemon"]["volumes"][0].split(":")[-2])
        assert target == MOUNT
        assert PurePosixPath(skill_module.PLUGIN_SITE_DIR) == MOUNT / "installed" / "site"
        assert PurePosixPath(doctor.PLUGIN_WHEEL_DIR) == MOUNT / "src"
        stack = (REPO / "host" / "istota-stack").read_text()
        assert f'wheels+=("{MOUNT}/src/${{name}}")' in stack

    def test_no_other_compose_file_names_the_mount(self):
        for path in sorted((REPO / "docker").glob("docker-compose*.yml")):
            if path != OVERLAY:
                assert "/app/plugins" not in path.read_text(), path.name
                assert "ISTOTA_PLUGINS_DIR" not in path.read_text(), path.name
