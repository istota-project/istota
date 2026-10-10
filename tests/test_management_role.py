"""The optional Ansible management role, held to the names it shares with istota.

`ansible/` is a thin wrapper: it writes `host.env`, the stack's `.env` keys,
the secrets files and an `istota apply` plan from variables, and calls
`provision.sh`, `istota-stack` and the VM's `istota` wrapper. Its whole
contract with this repository is a handful of name lists, and a role kept
anywhere else drifts the first time one of them grows (five secrets were added
in the one-deployment-shape spec's Stage 8). So each list is read from the
role's YAML on one side and from the code on the other:

- the secret names against `setup_wizard.SECRET_NAMES`;
- the `host.env` keys against what `provision.sh` documents and reads, what
  `istota-stack` reads, and what the wizard writes;
- the `.env` keys against `setup_wizard.stack_env_values`, with the values
  the role derives for each ingress mode rendered here and compared with the
  wizard's for the same answers;
- the `istota-stack` verbs the role calls against the script's own dispatch.

The role itself runs only in the vm tier (`tests/vm/test_management_role.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest
import yaml

from istota import setup_wizard

REPO = Path(__file__).resolve().parent.parent
ROLE = REPO / "ansible"
PROVISION = REPO / "host" / "provision.sh"
ISTOTA_STACK = REPO / "host" / "istota-stack"

STACK_COMMAND = "{{ istota_stack_command }}"
CLI_COMMAND = "{{ istota_cli_command }}"


def _load(relative: str):
    return yaml.safe_load((ROLE / relative).read_text())


def _defaults() -> dict:
    return _load("defaults/main.yml")


def _tasks() -> list[dict]:
    """Every task in the role, blocks flattened, handlers included."""
    found: list[dict] = []

    def walk(items):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            found.append(item)
            for key in ("block", "rescue", "always"):
                walk(item.get(key))

    for name in ("tasks/main.yml", "handlers/main.yml"):
        walk(_load(name))
    return found


def _command_argv(task: dict) -> list | None:
    for module in ("ansible.builtin.command", "command"):
        body = task.get(module)
        if isinstance(body, dict) and isinstance(body.get("argv"), list):
            return body["argv"]
    return None


# --- the code's side --------------------------------------------------------


def _documented_host_env_keys() -> set[str]:
    """The keys provision.sh's header lists as host.env's contract."""
    text = PROVISION.read_text()
    header = text.split("# The machine's choices come from", 1)[1].split("set -euo pipefail", 1)[0]
    keys: set[str] = set()
    for line in header.splitlines():
        match = re.match(r"^#\s{2,}([A-Z][A-Z0-9_]*(?:, [A-Z][A-Z0-9_]*)*)(?:\s|$)", line)
        if match:
            keys.update(match.group(1).split(", "))
    return keys


def _read_host_env_keys() -> set[str]:
    """The keys provision.sh and istota-stack actually look up in host.env."""
    keys = set(re.findall(r"\bhost_env ([A-Z][A-Z0-9_]*)\b", PROVISION.read_text()))
    keys |= set(re.findall(r'env_get "\$HOST_ENV" ([A-Z][A-Z0-9_]*)\b', ISTOTA_STACK.read_text()))
    return keys


def _stack_verbs() -> set[str]:
    text = ISTOTA_STACK.read_text()
    main = text.split("\nmain() {", 1)[1].split("\n}\n", 1)[0]
    verbs: set[str] = set()
    for arm in re.findall(r"^\s+([a-z|-]+)\)", main, re.M):
        verbs.update(arm.split("|"))
    return verbs


# --- rendering the role's derived values ------------------------------------


def _render(variables: dict) -> dict:
    """Resolve the role's `{{ }}` values the way Ansible would, for plain Jinja.

    Only the core filters plus `bool`: the derived values in defaults are kept
    to those, so this renderer and Ansible's agree.
    """
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda value: value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")

    class Vars(dict):
        """Each variable resolved when a template looks it up, so defaults may
        refer to one another without resolving the rest."""

        def __getitem__(self, name):
            return resolve(dict.__getitem__(self, name))

    lookup = Vars(variables)

    def resolve(value, depth=0):
        if depth > 20:
            raise AssertionError(f"unresolvable template: {value!r}")
        if isinstance(value, str) and "{{" in value:
            template = env.from_string(value)
            text = env.concat(template.root_render_func(template.new_context(lookup, shared=True)))
            return resolve(text, depth + 1)
        if isinstance(value, dict):
            return {k: resolve(v, depth) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, depth) for v in value]
        return value

    return {name: resolve(value) for name, value in variables.items()}


#: Answers the wizard can be given, and the role variables that say the same.
MODES = {
    "local": dict(ingress="local", hostname="localhost:8080"),
    "local-other-port": dict(ingress="local", hostname="localhost:9090"),
    "local-no-port": dict(ingress="local", hostname="localhost"),
    "direct-acme": dict(ingress="direct", hostname="istota.example.com", tls_cert_source="acme"),
    "direct-files": dict(ingress="direct", hostname="istota.example.com", tls_cert_source="files"),
    "proxied": dict(ingress="proxied", hostname="istota.example.com", upstream_proxy="192.0.2.10",
                    listen_addr="10.0.0.5", listen_port=8080, tls_cert_source=""),
    "proxied-port": dict(ingress="proxied", hostname="istota.example.com", upstream_proxy="192.0.2.0/28",
                         listen_addr="10.0.0.5", listen_port=8443, tls_cert_source=""),
    "proxied-files": dict(ingress="proxied", hostname="istota.example.com", upstream_proxy="192.0.2.10",
                          listen_addr="10.0.0.5", listen_port=8443, tls_cert_source="files"),
    "nextcloud-browser": dict(ingress="local", hostname="localhost:8080",
                              nextcloud_url="https://cloud.example.com", profiles=("browser", "signaling")),
}


def _answers(mode: dict) -> setup_wizard.ContainerAnswers:
    return setup_wizard.ContainerAnswers(**mode)


def _role_vars(mode: dict) -> dict:
    variables = dict(_defaults())
    variables.update(
        istota_ingress=mode["ingress"],
        istota_domain=mode["hostname"],
        istota_storage="nextcloud" if mode.get("nextcloud_url") else "local",
        istota_compose_profiles=list(mode.get("profiles", ())),
    )
    for answer, var in (("tls_cert_source", "istota_tls_cert_source"),
                        ("upstream_proxy", "istota_upstream_proxy"),
                        ("listen_addr", "istota_listen_addr"),
                        ("listen_port", "istota_listen_port")):
        if answer in mode:
            variables[var] = mode[answer]
    return _render(variables)


# --- the tests --------------------------------------------------------------


class TestTheRoleIsThere:
    def test_every_yaml_file_parses(self):
        files = sorted(ROLE.rglob("*.yml"))
        assert files, "no role found under ansible/"
        for path in files:
            yaml.safe_load(path.read_text())

    def test_it_renders_no_unit_template_or_cron(self):
        """provision.sh and istota-stack own the units, nginx and the cron-free
        update path; the role only writes inputs and calls them."""
        assert not (ROLE / "templates").exists()
        for task in _tasks():
            for module in ("template", "ansible.builtin.template", "cron", "ansible.builtin.cron"):
                assert module not in task, task.get("name")

    def test_the_paths_are_the_ones_provision_installs(self):
        defaults = _defaults()
        text = PROVISION.read_text()
        assert f'STACK="${{ISTOTA_STACK_DIR:-{defaults["istota_stack_dir"]}}}"' in text
        assert f'/istota-stack" {defaults["istota_stack_command"]}\n' in text
        assert f'/istota" {defaults["istota_cli_command"]}\n' in text


class TestTheSecretNames:
    def test_the_role_names_every_secret_istota_reads(self):
        names = _defaults()["istota_secret_names"]
        assert len(names) == len(set(names)), "a secret named twice"
        assert sorted(names) == sorted(setup_wizard.SECRET_NAMES)

    def test_istota_secrets_starts_empty(self):
        """Values come from the estate's vault, never from this repository."""
        assert _defaults()["istota_secrets"] == {}


class TestTheHostEnvKeys:
    def test_the_role_writes_exactly_the_keys_provision_documents(self):
        written = set(_defaults()["istota_host_env"])
        assert written == _documented_host_env_keys()

    def test_every_key_provision_and_istota_stack_read_is_written(self):
        read = _read_host_env_keys()
        assert read, "found no host.env lookups; the parser is broken"
        assert read <= set(_defaults()["istota_host_env"])

    def test_every_key_the_wizard_writes_is_written(self):
        wizard = set(setup_wizard.host_env_values(setup_wizard.ContainerAnswers()))
        assert wizard <= set(_defaults()["istota_host_env"])

    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_the_values_are_the_wizards_for_the_same_answers(self, mode):
        wizard = setup_wizard.host_env_values(_answers(MODES[mode]))
        rendered = _role_vars(MODES[mode])["istota_host_env"]
        assert {key: str(rendered[key]) for key in wizard} == wizard


class TestTheStackEnvKeys:
    def test_the_role_writes_exactly_the_keys_the_wizard_owns(self):
        wizard = set(setup_wizard.stack_env_values(setup_wizard.ContainerAnswers()))
        assert set(_defaults()["istota_stack_env"]) == wizard

    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_the_values_are_the_wizards_for_the_same_answers(self, mode):
        wizard = setup_wizard.stack_env_values(_answers(MODES[mode]))
        rendered = _role_vars(MODES[mode])["istota_stack_env"]
        assert {key: str(value) for key, value in rendered.items()} == wizard


class TestTheVerbs:
    def test_the_role_calls_istota_stack_only_by_argv(self):
        """So the verb list below is complete: a call inside a shell string
        would be invisible to it."""
        modules = ("command", "shell", "raw", "script")
        for task in _tasks():
            argv = _command_argv(task)
            if argv is not None and argv[0] == STACK_COMMAND:
                continue
            for module in modules:
                body = task.get(module, task.get(f"ansible.builtin.{module}"))
                if body is None:
                    continue
                text = yaml.safe_dump(body)
                # The unit is istota-stack.service; the command is the bare name.
                assert "istota_stack_command" not in text, task.get("name")
                assert not re.search(r"istota-stack(\s|$)", text), task.get("name")

    def test_every_verb_the_role_calls_is_one_istota_stack_has(self):
        called = {argv[1] for task in _tasks() if (argv := _command_argv(task)) and argv[0] == STACK_COMMAND}
        assert called >= {"update", "apply"}, called
        assert called <= _stack_verbs(), called - _stack_verbs()

    def test_the_ensure_loops_go_through_the_wrapper(self):
        """The CLI is reached as uid 10001 through the VM's wrapper, never by a
        `docker compose exec` of the role's own, which would run as root."""
        ensure = [t for t in _tasks() if "ensure" in t.get("name", "") and "loop" in t]
        assert len(ensure) == 2, [t.get("name") for t in ensure]
        for task in ensure:
            assert task["ansible.builtin.command"]["argv"].startswith("{{ [istota_cli_command] + "), task["name"]
        for task in _tasks():
            assert "docker compose exec" not in yaml.safe_dump(task), task.get("name")
