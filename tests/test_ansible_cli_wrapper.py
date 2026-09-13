"""The operator CLI has to be reachable, and pointed at the right config.

Two separate ways the deployment made `istota` unusable from a shell, and the
second is the one that looks like it works:

  * The console script lives in the venv and nothing put it on PATH, so every
    command had to be typed as an absolute path — on the host where an
    operator is most likely to be reading a runbook rather than a source tree.
  * The config search order starts at the working directory, so a command run
    from anywhere but the repository resolved *no* config and answered about a
    default `Config` — a relative `data/istota.db`, a `[security]` block
    nobody wrote. `doctor` gates itself on that now and refuses; the other
    verbs have no such gate and would act on it.

So the wrapper is not a convenience shim, and both halves are asserted here.
A symlink would have fixed only the first.

The rendering tests use Jinja rather than reading the template as text,
because what matters is the command a shell actually runs: `{{ istota_package
}}` and `{{ istota_repo_dir }}` are themselves templated from other variables,
and a substring check against the source would pass on a line that renders to
nothing.
"""

from pathlib import Path

import pytest
import yaml
from jinja2 import Template

REPO_ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = REPO_ROOT / "deploy" / "ansible"
TEMPLATE = ANSIBLE / "templates" / "istota-cli.sh.j2"


@pytest.fixture(scope="module")
def tasks() -> list:
    return yaml.safe_load((ANSIBLE / "tasks" / "main.yml").read_text())


@pytest.fixture(scope="module")
def defaults() -> dict:
    return yaml.safe_load((ANSIBLE / "defaults" / "main.yml").read_text())


@pytest.fixture(scope="module")
def install_task(tasks) -> dict:
    found = [
        t for t in tasks
        if isinstance(t, dict) and t.get("template", {}).get("src") == "istota-cli.sh.j2"
    ]
    assert len(found) == 1, "exactly one task installs the CLI wrapper"
    return found[0]


def _command(rendered: str) -> str:
    """The script with its commentary removed.

    Every assertion below is about what a shell runs. The header explains the
    same things it does, so a substring test against the whole file passes on
    the prose describing a line that was deleted — which is how the first
    version of the argument-order test read `"$@"` out of a comment.
    """
    lines = [
        line for line in rendered.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


def _render(**overrides) -> str:
    variables = {
        "istota_namespace": "istota",
        "istota_package": "istota",
        "istota_home": "/srv/app/istota",
        "istota_repo_dir": "/srv/app/istota/istota",
    }
    variables.update(overrides)
    return Template(TEMPLATE.read_text()).render(**variables)


class TestTheWrapperIsInstalled:
    def test_it_lands_on_the_default_path(self, install_task):
        """`/usr/local/bin` is ahead of `/usr/bin` on the default PATH and is
        the half of the filesystem the distro leaves alone — the same place
        the role puts its other entry points."""
        assert install_task["template"]["dest"] == "/usr/local/bin/{{ istota_namespace }}"

    def test_it_is_executable_and_root_owned(self, install_task):
        """It is on every user's PATH, so a writable one is a way to run code
        as whoever types the command."""
        template = install_task["template"]
        assert template["mode"] == "0755"
        assert template["owner"] == "root"
        assert template["group"] == "root"

    def test_a_web_only_converge_installs_nothing(self, install_task):
        """`istota_web_only` skips the venv this wrapper execs into, so
        installing it there would leave a command that cannot run."""
        assert install_task["when"] == "not istota_web_only"


class TestWhatTheWrapperRuns:
    def test_it_execs_the_console_script_in_the_venv(self):
        command = _command(_render())

        assert "exec /srv/app/istota/.venv/bin/istota" in command

    def test_it_names_the_deployment_config(self):
        """The half a symlink could not have fixed."""
        command = _command(_render())

        assert "-c /srv/app/istota/istota/config/config.toml" in command

    def test_an_explicit_config_still_wins(self):
        """argparse takes the last occurrence, so the operator's own -c has to
        come after the wrapper's. If "$@" were expanded first, a deployment
        could not be pointed at another config from the shell at all."""
        command = _command(_render())

        assert command.index("-c /srv/app") < command.index('"$@"')

    def test_it_follows_the_namespace_rather_than_the_package(self, install_task):
        """Two deployments on one host each get their own command. A wrapper
        named for the package would have the second converge silently repoint
        the first one's binary at the second one's config."""
        command = _command(_render(
            istota_namespace="second",
            istota_home="/srv/app/second",
            istota_repo_dir="/srv/app/second/istota",
        ))

        assert install_task["template"]["dest"].endswith("{{ istota_namespace }}")
        assert "exec /srv/app/second/.venv/bin/istota" in command
        assert "-c /srv/app/second/istota/config/config.toml" in command

    def test_the_venv_path_is_the_one_the_role_symlinks(self, tasks, defaults):
        """`uv sync` builds the venv inside the repository and a later task
        links `{{ istota_home }}/.venv` at it. The wrapper uses the link,
        which is what the units and every `command:` in the role use — so a
        change to either path breaks all of them together rather than leaving
        this one pointing at a venv that moved."""
        links = [
            t for t in tasks
            if isinstance(t, dict)
            and t.get("file", {}).get("dest") == "{{ istota_home }}/.venv"
            and t.get("file", {}).get("state") == "link"
        ]
        assert len(links) == 1, "the home-directory venv link is what this rests on"
        assert links[0]["file"]["src"] == "{{ istota_repo_dir }}/.venv"

        command = _command(_render(istota_home="/elsewhere"))
        assert "exec /elsewhere/.venv/bin/istota" in command
