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

And five rules about the role's own tasks: root writes nothing below
`config/` or `secrets/`; every task whose arguments carry a secret has
`no_log`, and no secret value is on an argv; an exit 2 counts as a change
only when the command ran; the provision and image decisions come after
the inputs they read; and the application settings reach istota as one
`istota apply --input-format role-vars` envelope in two parts, the config
before the image check and the user settings after the stack is healthy,
each checked through `settings_plan.role_input.convert_role_input` here.

The role itself runs only in the vm tier (`tests/vm/test_management_role.py`).
"""

from __future__ import annotations

import json
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
    """A command task's argv: a YAML list, or the literal list a templated
    argv starts with (`"{{ [istota_stack_command, 'apply', path] + flags }}"`),
    with each variable in it written back as `{{ name }}`."""
    for module in ("ansible.builtin.command", "command"):
        body = task.get(module)
        if not isinstance(body, dict):
            continue
        argv = body.get("argv")
        if isinstance(argv, list):
            return argv
        match = re.match(r"^\{\{\s*\[([^\]]*)\]", str(argv or ""))
        if match:
            items = [item.strip() for item in match.group(1).split(",")]
            return [item[1:-1] if item[:1] in "'\"" else f"{{{{ {item} }}}}" for item in items]
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
    "proxied-self-signed": dict(ingress="proxied", hostname="istota.example.com", upstream_proxy="192.0.2.10",
                                listen_addr="10.0.0.5", listen_port=8443, tls_cert_source="self-signed"),
    "nextcloud-browser": dict(ingress="local", hostname="localhost:8080",
                              nextcloud_url="https://cloud.example.com", profiles=("browser", "signaling")),
    # Talk with the workspace on the state volume: the role sets
    # `istota_storage: local` and leaves `workspace_mount_path` out of the plan.
    "nextcloud-local-workspace": dict(ingress="local", hostname="localhost:8080",
                                      nextcloud_url="https://cloud.example.com", workspace="local"),
}


def _answers(mode: dict) -> setup_wizard.ContainerAnswers:
    return setup_wizard.ContainerAnswers(**mode)


def _role_vars(mode: dict) -> dict:
    variables = dict(_defaults())
    variables.update(
        istota_ingress=mode["ingress"],
        istota_domain=mode["hostname"],
        istota_storage=(
            "nextcloud" if mode.get("nextcloud_url") and mode.get("workspace", "mount") == "mount"
            else "local"
        ),
        istota_rclone_remote=(
            setup_wizard.RCLONE_REMOTE
            if mode.get("nextcloud_url") and mode.get("workspace", "mount") == "mount"
            else ""
        ),
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

    def test_the_settings_go_through_istota_stack_apply_not_ensure_loops(self):
        """The ensure loops are gone: users, resources, briefings and secrets
        reach istota through `istota-stack apply`, whose one-shot container
        drops to uid 10001, and never by a `docker compose exec` of the
        role's own, which would run as root."""
        for task in _tasks():
            argv = _command_argv(task) or []
            assert "ensure" not in [str(arg) for arg in argv], task.get("name")
            assert "loop" not in task or "ensure" not in task.get("name", ""), task.get("name")
            assert "istota_cli_command" not in yaml.safe_dump(task), task.get("name")
            assert "docker compose exec" not in yaml.safe_dump(task), task.get("name")
        applies = [t for t in _tasks() if (argv := _command_argv(t)) and argv[:2] == [STACK_COMMAND, "apply"]]
        assert [t["name"] for t in applies] == ["istota-stack apply --check (user settings)", "istota-stack apply",
                                                "istota-stack apply (user settings)"]
        for task in applies:
            assert _command_argv(task)[3:5] == ["--input-format", "role-vars"], task["name"]


# --- what root writes ---------------------------------------------------------

#: Modules that write to a path on the VM, and the argument naming it.
_WRITING_MODULES = {
    "copy": "dest", "template": "dest", "lineinfile": "path", "blockinfile": "path",
    "replace": "path", "file": "path", "fetch": "dest", "get_url": "dest", "unarchive": "dest",
}


def _module(task: dict) -> tuple[str, object] | tuple[None, None]:
    for key, body in task.items():
        short = key.removeprefix("ansible.builtin.")
        if short in _WRITING_MODULES or short in ("command", "shell", "set_fact", "assert", "debug", "stat"):
            return short, body
    return None, None


class TestRootWritesNothingUid10001Owns:
    """`config/` and `secrets/` are uid 10001's directories, and Ansible's copy
    follows a link to a directory at its destination. A link 10001 planted
    there would have root write a 10001-owned file anywhere, so the role
    writes nothing below either directory as root: `istota-stack put-files`
    drops to 10001 before it opens anything."""

    def test_no_task_writes_below_config_or_secrets(self):
        for task in _tasks():
            module, body = _module(task)
            if module not in _WRITING_MODULES or not isinstance(body, dict):
                continue
            target = str(body.get(_WRITING_MODULES[module], ""))
            if module == "file" and body.get("state") == "directory":
                # The two directories themselves sit in root's stack directory.
                assert target == "{{ item.path }}", task.get("name")
                continue
            for owned in ("/secrets", "/config"):
                assert f"{{{{ istota_stack_dir }}}}{owned}" not in target, task.get("name")

    def test_the_secrets_and_admins_go_through_put_files_on_stdin(self):
        put = [t for t in _tasks() if (argv := _command_argv(t)) and argv[:2] == [STACK_COMMAND, "put-files"]]
        assert put, "no task calls istota-stack put-files"
        for task in put:
            body = task["ansible.builtin.command"]
            assert "stdin" in body, task.get("name")
            assert task.get("no_log") is True, task.get("name")
            assert not any("istota_secrets" in str(arg) for arg in body["argv"]), task.get("name")


class TestTheRoleClaimsWhatItWrites:
    """The role marks what it manages, so the web's config editor shows those
    resources read-only rather than taking an edit the next play reverts. The
    plan's `[meta]` claims the config; the `put-files` request's `managed`
    claims the secrets and the admins, each only while the role sets them."""

    def _env(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["to_json"] = json.dumps
        env.filters["string"] = str
        return env

    def _put_files(self, secrets: dict, admins: list) -> dict:
        task = next(t for t in _tasks() if t.get("name") == "Secrets and admins")
        env = self._env()
        context = {"istota_secret_names": list(setup_wizard.SECRET_NAMES),
                   "istota_secrets": secrets, "istota_admins": admins}
        for name, template in task["vars"].items():
            context[name] = env.from_string(template).render(context)
        return json.loads(context["_istota_put_files"])

    def test_the_plan_claims_the_config(self):
        """The envelope goes through `convert_role_input`, which marks the
        config the role submits as the role's."""
        from istota.settings_plan.role_input import convert_role_input

        config, _ = _settings_parts({"istota_plan": {"meta": {"source": "snapshot"}, "config": {"bot_name": "Zed"}}})
        plan, _ = convert_role_input(config)
        assert plan["meta"]["managed"] is True and plan["meta"]["source"] == "ansible"
        assert plan["config"] == {"bot_name": "Zed"}

    def test_put_files_claims_what_the_role_sets(self):
        request = self._put_files({"anthropic_api_key": "sk"}, ["alice"])
        assert request["managed"] == {"source": "ansible", "secrets": True, "admins": True}

    def test_put_files_releases_what_the_role_does_not_set(self):
        request = self._put_files({}, [])
        assert request["managed"] == {"source": "ansible", "secrets": False, "admins": False}


# --- secrets stay out of output -----------------------------------------------

#: Variables whose value is a credential, or may hold one (a config can).
_SECRET_VARS = {"istota_secrets", "istota_rclone_conf", "istota_plan", "istota_plan_secrets"}
#: Task keys that are conditions or bookkeeping: a variable named there is
#: evaluated, never printed.
_CONDITION_KEYS = {"name", "when", "failed_when", "changed_when", "until", "register",
                   "notify", "no_log", "check_mode", "retries", "delay", "listen"}


def _data(task: dict) -> str:
    """The Jinja in what of a task reaches its arguments or result: a
    variable's name in plain text (a fail_msg) prints no value."""
    module, body = _module(task)
    data = {key: value for key, value in task.items() if key not in _CONDITION_KEYS}
    if module == "assert" and isinstance(body, dict):
        data[next(k for k in task if k.removeprefix("ansible.builtin.") == "assert")] = {
            key: value for key, value in body.items() if key != "that"}
    text = json.dumps(data)
    return "\n".join(re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text, re.S))


def _secret_vars() -> set[str]:
    """The secret variables, plus every fact set from one."""
    names = set(_SECRET_VARS)
    while True:
        grown = set(names)
        for task in _tasks():
            module, body = _module(task)
            if module == "set_fact" and any(re.search(rf"\b{n}\b", _data(task)) for n in names):
                grown |= set(body)
        if grown == names:
            return names
        names = grown


class TestSecretsStayOutOfOutput:
    """`-v` prints a task's arguments and result unless the task has
    `no_log: true`, so any task whose arguments, vars or loop name a secret
    variable carries it. A condition names a variable without printing it."""

    def test_the_derived_secret_facts_are_found(self):
        assert _secret_vars() > _SECRET_VARS

    def test_every_task_carrying_a_secret_has_no_log(self):
        secret = _secret_vars()
        for task in _tasks():
            if "block" in task:
                continue
            text = _data(task)
            named = sorted(n for n in secret if re.search(rf"\b{n}\b", text))
            if named:
                assert task.get("no_log") is True, f"{task.get('name')} carries {named} without no_log"


def _bool(value):
    return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")


def _envs():
    """A text and a native Jinja environment with the filters the role's
    settings tasks use, all of which Ansible's own reduce to here."""
    from jinja2.nativetypes import NativeEnvironment

    text_env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    native_env = NativeEnvironment(undefined=jinja2.StrictUndefined)
    for env in (text_env, native_env):
        env.filters["to_json"] = json.dumps
        env.filters["from_json"] = json.loads
        env.filters["bool"] = _bool
    return text_env, native_env


def _role_context(variables: dict) -> dict:
    from istota.settings_plan.role_input import ROLE_VARIABLES

    defaults = _defaults()
    names = (*ROLE_VARIABLES, "istota_plan_prune", "istota_plan_prune_secrets", "istota_plan_actor")
    return {name: defaults[name] for name in names} | variables


def _settings_parts(variables: dict) -> tuple[dict, dict]:
    """The two `--input-format role-vars` envelopes the role writes for
    `variables`: the config part and the user-settings part."""
    text_env, native_env = _envs()
    task = _tasks()[_index("Split the application settings")]
    context = _role_context(variables)
    for name, template in (task.get("vars") or {}).items():
        context[name] = text_env.from_string(template).render(context)
    facts = {name: native_env.from_string(template).render(context)
             for name, template in task["ansible.builtin.set_fact"].items()}
    return facts["_istota_settings_config"], facts["_istota_settings_users"]


def _apply_argv(name: str, **context) -> list:
    _, native_env = _envs()
    task = _tasks()[_index(name)]
    full = _role_context({}) | {"istota_stack_command": STACK_COMMAND, "ansible_check_mode": False,
                                "_istota_config_input": {"path": "/tmp/config.json"},
                                "_istota_users_input": {"path": "/tmp/users.json"},
                                "_istota_users_check_input": {"path": "/tmp/users-check.json"},
                                "_istota_users_checked": {"skipped": True}} | context
    return native_env.from_string(task["ansible.builtin.command"]["argv"]).render(full)


class TestTheSettingsInput:
    """The role's application variables reach istota as `istota apply
    --input-format role-vars` envelopes, converted by the same Python adapter
    the CLI uses. The config goes first, alone, because the stack cannot
    start without it; everything else waits for an initialized database."""

    VALUE = "kk-secret-value"
    HEADER = ["brain", "native", "extra_headers", "x.tenant"]

    def _canonical(self) -> dict:
        return {
            "istota_plan": {
                "meta": {"version": 1, "generated_by": "snapshot", "source": "snapshot"},
                "config": {"bot_name": "Zed", "brain": {"native": {"extra_headers": {"x.tenant": "<unchanged>"}}}},
                "users": {"alice": {"profile_mode": "update", "profile": {"timezone": "UTC"},
                                    "secrets": [{"service": "karakeep", "keys": ["api_key"]}]}},
                "shared_briefing_blocks": [],
            },
            "istota_plan_secrets": {
                "config": [{"path": self.HEADER, "value": "inline-value"}],
                "users": {"alice": {"karakeep": {"api_key": self.VALUE}}},
            },
        }

    def test_a_canonical_plan_splits_and_each_secret_follows_its_section(self):
        from istota.settings_plan.role_input import convert_role_input

        variables = self._canonical()
        plan, secrets = variables["istota_plan"], variables["istota_plan_secrets"]
        config, users = _settings_parts(variables)
        assert config == {"istota_plan": {"meta": plan["meta"], "config": plan["config"]},
                          "istota_plan_secrets": {"config": secrets["config"]}}
        assert users == {"istota_plan": {k: v for k, v in plan.items() if k != "config"},
                         "istota_plan_secrets": {"users": secrets["users"]}}
        converted, sidecar = convert_role_input(config)
        assert converted["config"] == plan["config"] and converted["meta"]["managed"] is True
        assert sidecar == {"config": secrets["config"]}
        converted, sidecar = convert_role_input(users)
        assert "config" not in converted and converted["users"] == plan["users"]
        assert converted["shared_briefing_blocks"] == [] and sidecar == {"users": secrets["users"]}

    def test_a_part_with_nothing_to_apply_is_empty(self):
        assert _settings_parts({}) == ({}, {})
        only_config = {"istota_plan": {"config": {"bot_name": "Zed"}}}
        assert _settings_parts(only_config)[1] == {}
        only_users = {"istota_plan": {"users": {"alice": {"profile": {"timezone": "UTC"}}}}}
        assert _settings_parts(only_users)[0] == {}

    def test_a_secret_with_no_section_to_land_in_is_refused_not_dropped(self):
        """A user value with no user settings, or a config value with no
        config, still reaches apply, which refuses it before writing."""
        from istota.settings_plan.role_input import convert_role_input
        from istota.settings_plan.schema import SchemaError

        users_only_secret = {"istota_plan": {"config": {"bot_name": "Zed"}},
                             "istota_plan_secrets": {"users": {"alice": {"karakeep": {"api_key": self.VALUE}}}}}
        _, users = _settings_parts(users_only_secret)
        assert users
        with pytest.raises(SchemaError):
            convert_role_input(users)
        config_only_secret = {"istota_plan": {"users": {"alice": {"profile": {"timezone": "UTC"}}}},
                              "istota_plan_secrets": {"config": [{"path": self.HEADER, "value": "v"}]}}
        config, _ = _settings_parts(config_only_secret)
        assert config
        with pytest.raises(SchemaError):
            convert_role_input(config)

    def test_no_secret_value_is_on_an_argv(self):
        """argv is in root's docker CLI command line and dockerd's exec records."""
        for name in ("istota-stack apply", "istota-stack apply (user settings)",
                     "istota-stack apply --check (user settings)"):
            for check in (False, True):
                argv = _apply_argv(name, ansible_check_mode=check, **self._canonical())
                assert all(isinstance(arg, str) for arg in argv), argv
                assert not any(self.VALUE in arg or "inline-value" in arg for arg in argv), argv


class TestTheApplyCalls:
    NAMES = ("istota-stack apply", "istota-stack apply (user settings)")

    def test_check_mode_is_a_dry_run(self):
        for name in self.NAMES:
            assert "--dry-run" not in _apply_argv(name, ansible_check_mode=False)
            assert "--dry-run" in _apply_argv(name, ansible_check_mode=True)
            # The command itself runs under --check; --dry-run keeps it from writing.
            assert _tasks()[_index(name)].get("check_mode") is False, name

    def test_the_input_is_the_role_vars_envelope(self):
        assert _apply_argv("istota-stack apply") == [
            STACK_COMMAND, "apply", "/tmp/config.json", "--input-format", "role-vars"]
        assert _apply_argv("istota-stack apply (user settings)") == [
            STACK_COMMAND, "apply", "/tmp/users.json", "--input-format", "role-vars"]

    def test_group_changes_forward_only_an_explicit_actor(self):
        assert "--actor" not in _apply_argv("istota-stack apply (user settings)")
        assert _apply_argv("istota-stack apply (user settings)", istota_plan_actor="alice")[-2:] == ["--actor", "alice"]

    def test_prune_reaches_only_the_user_settings(self):
        """The config is a whole document, so prune means nothing to it."""
        prune = {"istota_plan_prune": True, "istota_plan_prune_secrets": True}
        assert "--prune" not in _apply_argv("istota-stack apply", **prune)
        users = _apply_argv("istota-stack apply (user settings)", **prune)
        assert users[-2:] == ["--prune", "--prune-secrets"]
        only_prune = _apply_argv("istota-stack apply (user settings)", istota_plan_prune=True)
        assert only_prune[-1] == "--prune" and "--prune-secrets" not in only_prune

    def test_istota_stack_restarts_and_the_role_does_not_again(self):
        """`istota-stack apply` restarts the stack once when the result says a
        committed change needs it; the ensure loops' notify restarted it a
        second time."""
        for name in self.NAMES:
            assert "notify" not in _tasks()[_index(name)], name

    def test_the_config_apply_leaves_its_restart_to_the_user_settings(self):
        """A play that changes both halves restarts the stack once (ISSUE-741):
        the config apply defers its restart whenever the user settings apply
        will run after it, and that apply takes it with its own. Whether it
        will is read from the user settings check, which runs on the same
        conditions, rather than from the settings themselves, which carry
        credentials an argv template may not name."""
        checked = {"rc": 0, "stdout": "{}"}
        assert _apply_argv("istota-stack apply", _istota_users_checked=checked)[-1] == "--no-restart"
        assert "--no-restart" not in _apply_argv("istota-stack apply")
        for check in (False, True):
            argv = _apply_argv("istota-stack apply (user settings)", _istota_users_checked=checked,
                               ansible_check_mode=check)
            assert "--no-restart" not in argv
        check_block = next(t for t in _tasks() if t.get("name") == "Check the user settings")
        apply_block = next(t for t in _tasks() if t.get("name") == "Apply the user settings")
        assert set(check_block["when"]) == set(apply_block["when"]) - {
            "not ansible_check_mode or _istota_config_present.stat.exists"}

    def test_a_restart_the_user_settings_took_is_waited_for(self):
        """The health wait before the user settings no longer follows the
        config's restart, so a second one follows the apply that took it."""
        wait = _tasks()[_index("Wait for the daemon service to be healthy after the user settings")]
        first = _tasks()[_index("Wait for the daemon service to be healthy")]
        assert wait["ansible.builtin.shell"] == first["ansible.builtin.shell"]
        assert wait["retries"] == first["retries"] and wait["until"] == f"{wait['register']}.rc == 0"
        assert _index("Wait for the daemon service to be healthy after the user settings") > _index(
            "Report a refused user settings apply")
        assert "not ansible_check_mode" in _when(wait)
        assert "_istota_users_applied" in _when(wait)
        assert "tags" not in wait

    def test_each_input_file_is_private_and_removed_afterwards(self):
        for block_name, write, remove in (
                ("Apply the config", "Write the config input", "Remove the config input file"),
                ("Apply the user settings", "Write the user settings input",
                 "Remove the user settings input file")):
            block = next(t for t in _tasks() if t.get("name") == block_name)
            assert remove in [t.get("name") for t in block.get("always", [])], block_name
            task = _tasks()[_index(write)]
            assert task.get("no_log") is True, write
            assert task["ansible.builtin.copy"]["mode"] == "0600", write


def _failure_message(name: str, result: dict) -> str:
    text_env, native_env = _envs()
    task = _tasks()[_index(name)]
    context = dict(result)
    for key, template in (task.get("vars") or {}).items():
        context[key] = native_env.from_string(template).render(context)
    return " ".join(text_env.from_string(task["ansible.builtin.fail"]["msg"]).render(context).split())


class TestAFailedApplyIsReportedFromItsResult:
    """A refused or partial apply is reported from the result document's
    error codes and operation identities, never by matching words in stdout."""

    NAMES = (("Report a refused config apply", "_istota_applied"),
             ("Report a refused user settings apply", "_istota_users_applied"))

    def _doc(self, **overrides) -> str:
        document = {"version": 1, "dry_run": False, "changed": False, "partial": False, "restart_required": False,
                    "counts": {}, "operations": [], "warnings": [],
                    "errors": [{"code": "validation_error", "path": ["users", "alice", "profile", 0]}]}
        return json.dumps(document | overrides)

    def test_a_refusal_names_its_codes_and_paths(self):
        for name, register in self.NAMES:
            message = _failure_message(name, {register: {"rc": 1, "stdout": self._doc(), "stderr": ""}})
            assert "validation_error at users.alice.profile.0" in message, message
            assert "nothing was written" in message, message
            assert f"{register}.rc == 1" in _when(_tasks()[_index(name)]), name

    def test_a_refusal_carries_its_message_when_the_document_has_one(self):
        """The role's refusals name the variable and the fix; without the
        message a JSON error says only where."""
        errors = [{"code": "validation_error", "path": ["istota_users"],
                   "message": "not a settings variable; the role takes istota_plan and istota_plan_secrets"}]
        for name, register in self.NAMES:
            message = _failure_message(name, {register: {"rc": 1, "stdout": self._doc(errors=errors), "stderr": ""}})
            assert ("validation_error at istota_users: not a settings variable; "
                    "the role takes istota_plan and istota_plan_secrets") in message, message

    def test_a_partial_apply_names_what_was_applied(self):
        operations = [
            {"section": "profile", "user": "alice", "identity": "alice", "action": "update",
             "status": "applied", "changes": []},
            {"section": "resources", "user": "bob", "identity": "folder:/data/x", "action": "create",
             "status": "failed", "changes": []},
        ]
        name, register = self.NAMES[1]
        stdout = self._doc(partial=True, changed=True, operations=operations,
                           errors=[{"code": "execution_failed", "path": ["users", "bob", "resources"]}])
        message = _failure_message(name, {register: {"rc": 1, "stdout": stdout, "stderr": ""}})
        assert "partially applied" in message and "execution_failed at users.bob.resources" in message, message
        assert "profile alice alice" in message and "folder:/data/x" not in message, message

    def test_no_result_document_reports_the_exit_and_stderr(self):
        name, register = self.NAMES[0]
        message = _failure_message(name, {register: {"rc": 1, "stdout": "", "stderr": "update the stack first."}})
        assert "without a result document" in message and "update the stack first." in message, message


def _assert_fails(task_name: str, variables: dict) -> list[str]:
    """The `that` expressions of an assert task that fail for `variables`."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    env.filters["bool"] = _bool
    task = _tasks()[_index(task_name)]
    context = _render(_defaults() | variables)
    return [that for that in task["ansible.builtin.assert"]["that"] if not env.compile_expression(that)(**context)]


class TestTheSettingsVariablesAreCheckedFirst:
    """Contradictory application inputs are refused before the play changes
    anything on the VM; deployment variables may sit beside the plan."""

    CHECK = "Check the application-settings variables"
    PLAN = {"istota_plan": {"users": {"alice": {"profile": {"timezone": "UTC"}}}}}

    def _fails(self, **variables) -> list[str]:
        return _assert_fails(self.CHECK, variables)

    def test_the_check_runs_before_the_first_change(self):
        assert _index(self.CHECK) < _index("Stack directories")
        assert _index("Split the application settings") < _index("Stack directories")

    def test_no_input_or_the_plan_passes(self):
        assert self._fails() == []
        assert self._fails(**self.PLAN, istota_plan_secrets={"users": {}}, istota_plan_prune=True,
                           istota_plan_prune_secrets=True) == []

    def test_deployment_variables_may_sit_beside_the_plan(self):
        assert self._fails(**self.PLAN, istota_secrets={"anthropic_api_key": "k"}, istota_admins=["alice"],
                           istota_rclone_conf="[x]") == []

    def test_a_retired_settings_variable_is_refused_even_empty(self):
        """The role no longer reads them; without the refusal a play would
        apply no settings and report success."""
        retired = "Refuse the retired settings variables"
        assert _index(retired) < _index("Stack directories")
        assert _assert_fails(retired, {}) == []
        assert _assert_fails(retired, self.PLAN) == []
        for name in ("istota_config", "istota_users", "istota_user_resources",
                     "istota_user_briefings", "istota_user_secrets", "istota_user_profile_mode"):
            assert _assert_fails(retired, {name: {}}) == [f"{name} is not defined"], name

    def test_plan_secrets_need_the_plan(self):
        assert self._fails(istota_plan_secrets={"users": {}}) != []

    def test_prune_secrets_needs_prune_and_prune_needs_the_plan(self):
        assert self._fails(**self.PLAN, istota_plan_prune_secrets=True) != []
        assert self._fails(istota_plan_prune=True) != []

    def test_the_inputs_must_be_mappings(self):
        assert self._fails(istota_plan="settings.json") != []
        assert self._fails(**self.PLAN, istota_plan_secrets=["x"]) != []


class TestTheUserSettingsAreCheckedBeforeTheConfig:
    """The user settings are applied after the health wait, but a schema
    error or an unknown field in them must fail the play before the
    config is written. `apply --check` reads no config and no database."""

    CHECK = "istota-stack apply --check (user settings)"
    REPORT = "Report refused user settings"

    def test_the_check_runs_before_the_config_is_written(self):
        assert _index("Split the application settings") < _index(self.CHECK) < _index("istota-stack apply")
        assert _index(self.CHECK) < _index("Write the config input")

    def test_the_check_is_schema_only_and_never_a_change(self):
        for check in (False, True):
            assert _apply_argv(self.CHECK, ansible_check_mode=check) == [
                STACK_COMMAND, "apply", "/tmp/users-check.json", "--input-format", "role-vars", "--check", "--json"]
        task = _tasks()[_index(self.CHECK)]
        assert task.get("check_mode") is False and task.get("changed_when") is False
        failed_when = str(task["failed_when"])
        assert "_istota_users_checked.delta is not defined" in failed_when and "[0, 1]" in failed_when

    def test_it_runs_wherever_the_user_settings_would(self):
        block = next(t for t in _tasks() if t.get("name") == "Check the user settings")
        assert "_istota_settings_users | length > 0" in _when(block)
        assert self.CHECK in [t.get("name") for t in block["block"]]

    def test_a_refusal_fails_the_play_with_its_messages(self):
        stdout = json.dumps({"problems": [{"kind": "discarded", "path": ["istota_users"],
                                           "message": "not a settings variable; the role takes istota_plan"}],
                             "error": None})
        message = _failure_message(self.REPORT, {"_istota_users_checked": {"rc": 1, "stdout": stdout, "stderr": ""}})
        assert "istota_users: not a settings variable; the role takes istota_plan" in message, message
        assert "nothing was written" in message, message
        assert "_istota_users_checked.rc == 1" in _when(_tasks()[_index(self.REPORT)])
        stdout = json.dumps({"problems": [], "error": "the plan in role-vars input does not parse: invalid JSON"})
        message = _failure_message(self.REPORT, {"_istota_users_checked": {"rc": 1, "stdout": stdout, "stderr": ""}})
        assert "does not parse: invalid JSON" in message, message
        message = _failure_message(self.REPORT, {"_istota_users_checked": {"rc": 1, "stdout": "", "stderr": "boom"}})
        assert "boom" in message, message

    def test_the_check_input_is_private_and_removed_afterwards(self):
        block = next(t for t in _tasks() if t.get("name") == "Check the user settings")
        assert "Remove the user settings check file" in [t.get("name") for t in block.get("always", [])]
        task = _tasks()[_index("Write the user settings check input")]
        assert task.get("no_log") is True
        assert task["ansible.builtin.copy"]["mode"] == "0600"


class TestTheUserSettingsWaitForTheStack:
    """The user settings need an initialized database, which the daemon's
    entrypoint makes; the config has to be written before the stack starts."""

    def test_the_config_is_applied_before_the_image_check_and_the_rest_after_health(self):
        assert _index("istota-stack apply") < _index("Does the image have what the config needs")
        assert _index("istota-stack apply (user settings)") > _index("Wait for the daemon service to be healthy")

    def test_check_mode_reports_what_it_cannot_dry_run(self):
        """Under --check on a VM with no deployed release or no config yet,
        there is nothing to dry-run the user settings against."""
        report = _tasks()[_index("Report the user settings --check would apply")]
        assert report.get("changed_when") is True
        assert "ansible_check_mode" in _when(report)
        assert _index("Does the VM have a config") < _index("istota-stack apply (user settings)")


# --- exit codes ---------------------------------------------------------------


class TestAnExitTwoIsOnlyAChangeWhenTheCommandRan:
    """The command module answers rc 2 when it cannot exec the program at all
    (ENOENT), with no start, end or delta. A task treating rc 2 as "changed"
    requires the command to have run."""

    def test_every_rc_two_task_requires_a_delta(self):
        found = 0
        for task in _tasks():
            failed_when = str(task.get("failed_when", ""))
            if "[0, 2]" not in failed_when and "[0, 1, 2]" not in failed_when:
                continue
            found += 1
            register = task["register"]
            assert f"{register}.delta is not defined" in failed_when, task.get("name")
        assert found >= 5


# --- order --------------------------------------------------------------------


def _index(name: str) -> int:
    names = [task.get("name") for task in _tasks()]
    assert name in names, name
    return names.index(name)


class TestTheImageFollowsTheConfig:
    """`istota-stack update` decides from config.toml whether to build Claude
    Code in, and on a first converge the update runs before the apply writes
    the config. So the image is checked against the config after the apply,
    and the same tag rebuilt when it lacks what the config needs."""

    def test_the_check_runs_after_the_apply(self):
        check = [t for t in _tasks() if (argv := _command_argv(t)) and argv[:2] == [STACK_COMMAND, "check-image"]]
        assert check, "no task calls istota-stack check-image"
        assert _index(check[0]["name"]) > _index("istota-stack apply")

    def test_a_mismatch_rebuilds_the_deployed_tag(self):
        rebuild = _index("Rebuild the image for the config")
        task = _tasks()[rebuild]
        assert _command_argv(task)[:2] == [STACK_COMMAND, "update"]
        assert rebuild > _index("istota-stack apply")
        assert rebuild < _index("Restart the stack now if an input changed")


def _assert_holds(variables: dict) -> list[str]:
    """The `that` expressions of the role's variable check that fail for `variables`."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    task = _tasks()[_index("Check the variables")]
    context = _render(_defaults() | variables)
    return [that for that in task["ansible.builtin.assert"]["that"] if not env.compile_expression(that)(**context)]


def _when(task: dict) -> str:
    when = task.get("when", "")
    return " and ".join(when) if isinstance(when, list) else str(when)


class TestTheTwoUpdateModes:
    """`release` installs a signed tag named by istota_release; `latest`
    follows main through `update --unsigned latest` and needs no key."""

    PROXIED = {"istota_ingress": "local"}

    def test_release_is_the_default(self):
        assert _defaults()["istota_update_mode"] == "release"

    def test_release_mode_requires_the_signing_key(self):
        assert _assert_holds({"istota_release_signing_key": ""}) != []
        assert _assert_holds({"istota_release_signing_key": "ssh-ed25519 AAAA releases"}) == []

    def test_latest_mode_needs_no_key(self):
        assert _assert_holds({"istota_update_mode": "latest", "istota_release_signing_key": ""}) == []

    def test_an_unknown_mode_is_refused(self):
        assert _assert_holds({"istota_update_mode": "nightly", "istota_release_signing_key": "k"}) != []

    def _update_tasks(self) -> dict[tuple, dict]:
        return {
            tuple(argv[2:]): task for task in _tasks()
            if (argv := _command_argv(task)) and argv[:2] == [STACK_COMMAND, "update"]
            and "Rebuild" not in task["name"]
        }

    def test_each_mode_has_its_own_update_call(self):
        tasks = self._update_tasks()
        assert set(tasks) == {("{{ istota_release }}",), ("--unsigned", "latest")}
        assert "istota_update_mode == 'release'" in _when(tasks[("{{ istota_release }}",)])
        assert "istota_update_mode == 'latest'" in _when(tasks[("--unsigned", "latest")])

    def test_latest_updates_only_when_main_moved(self):
        """An update always rebuilds, so following main compares the name
        `latest-tag` gives with the deployed one first."""
        latest = _index("What main's tip would install")
        task = _tasks()[latest]
        assert _command_argv(task) == [STACK_COMMAND, "latest-tag"]
        assert task.get("check_mode") is False and task.get("changed_when") is False
        follow = self._update_tasks()[("--unsigned", "latest")]
        assert latest < _index(follow["name"])
        assert "_istota_current_tag.stdout" in _when(follow)

    def test_a_new_release_or_a_moved_main_both_reprovision(self):
        when = _when(_tasks()[_index("Provision from the new release")])
        assert "_istota_updated is changed" in when and "_istota_followed is changed" in when

    def test_the_rebuild_keeps_an_unsigned_tag_unsigned(self):
        """Rebuilding the deployed main-<hex> or unsigned tag through a signed
        update would be refused."""
        signed = _tasks()[_index("Rebuild the image for the config")]
        unsigned = _tasks()[_index("Rebuild the image for the config (unsigned tag)")]
        assert _command_argv(signed)[:2] == [STACK_COMMAND, "update"] and "--unsigned" not in _command_argv(signed)
        assert _command_argv(unsigned)[:3] == [STACK_COMMAND, "update", "--unsigned"]
        reread = _index("Read the tag now deployed")
        assert reread > max(_index(t["name"]) for t in self._update_tasks().values())
        assert reread < _index("Rebuild the image for the config")
        for task in (signed, unsigned):
            assert "_istota_deployed" in str(_command_argv(task)) and "_istota_deployed" in _when(task)


def _when_holds(task: dict, context: dict) -> bool:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["bool"] = lambda value: value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    when = task.get("when", [])
    for condition in (when if isinstance(when, list) else [when]):
        if not env.compile_expression(str(condition))(**context):
            return False
    return True


class TestAnUpdateOnItsOwn:
    """`--tags istota_update` runs the version update and nothing else, the
    play's form of ssh-ing in and running `istota-stack update`; and
    `istota_update_force` runs it when nothing moved, as the command would."""

    UPDATE_ONLY = [
        "Check the variables",
        "Check the VM was bootstrapped from a verified release",
        "Refuse a VM with no checkout",
        "Is istota-stack installed",
        "Read the deployed tag",
        "What main's tip would install",
        "Report the update --check would make",
        "Update to the release",
        "Follow main",
        "Provision from the new release",
        "Read the tag now deployed",
        "The stack is enabled and running",
        "Wait for the daemon service to be healthy",
    ]

    def test_the_tag_selects_exactly_the_update(self):
        tagged = [t["name"] for t in _tasks() if "istota_update" in (t.get("tags") or [])]
        assert tagged == self.UPDATE_ONLY

    def test_nothing_that_writes_config_secrets_or_users_is_tagged(self):
        for task in _tasks():
            if "istota_update" not in (task.get("tags") or []):
                continue
            argv = _command_argv(task) or []
            assert argv[1:2] not in (["apply"], ["put-files"]), task["name"]
            assert "ensure" not in task["name"], task["name"]

    def test_force_is_off_by_default(self):
        assert _defaults()["istota_update_force"] is False

    def _context(self, **overrides) -> dict:
        context = {
            "ansible_check_mode": False, "istota_update_force": False,
            "istota_update_mode": "latest", "istota_release": "",
            "_istota_current_tag": {"stdout": "main-0123456789ab"},
            "_istota_latest": {"stdout": "main-0123456789ab"},
        }
        context.update(overrides)
        return context

    def test_latest_with_main_unmoved_updates_only_when_forced(self):
        follow = _tasks()[_index("Follow main")]
        assert not _when_holds(follow, self._context())
        assert _when_holds(follow, self._context(istota_update_force=True))

    def test_release_unchanged_updates_only_when_forced(self):
        update = _tasks()[_index("Update to the release")]
        same = dict(istota_update_mode="release", istota_release="v1", _istota_current_tag={"stdout": "v1"})
        assert not _when_holds(update, self._context(**same))
        assert _when_holds(update, self._context(istota_update_force=True, **same))

    def test_force_never_updates_in_check_mode(self):
        for name in ("Update to the release", "Follow main"):
            task = _tasks()[_index(name)]
            assert not _when_holds(task, self._context(ansible_check_mode=True, istota_update_force=True,
                                                       istota_update_mode=task["name"] == "Follow main" and "latest" or "release",
                                                       istota_release="v1"))

    OLD_USAGE = "istota-stack: the VM's command ...\n  istota-stack update --unsigned latest\n"

    def test_an_istota_stack_without_latest_tag_does_not_fail_the_play(self):
        """A VM provisioned before `latest-tag` existed answers it with usage
        and 64; the update that follows installs the istota-stack that has it."""
        task = _tasks()[_index("What main's tip would install")]
        env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
        failed = env.compile_expression(str(task["failed_when"]))
        assert not failed(_istota_latest={"rc": 64, "stdout": "", "stderr": self.OLD_USAGE})
        assert failed(_istota_latest={"rc": 64, "stdout": "", "stderr": "usage ... latest-tag ..."})
        assert failed(_istota_latest={"rc": 1, "stdout": "", "stderr": "could not read main"})
        assert not failed(_istota_latest={"rc": 0, "stdout": "main-0123456789ab", "stderr": ""})

    def test_an_unknown_tip_follows_main(self):
        follow = _tasks()[_index("Follow main")]
        old = {"rc": 64, "stdout": "", "stderr": self.OLD_USAGE}
        assert _when_holds(follow, self._context(_istota_latest=old))
        report = _tasks()[_index("Report the update --check would make")]
        assert _when_holds(report, self._context(ansible_check_mode=True, _istota_latest=old,
                                                 _istota_target=""))

    def test_check_mode_reports_a_forced_update(self):
        report = _tasks()[_index("Report the update --check would make")]
        assert "istota_update_force" in _when(report)


class TestTheProvisionDecisionIsDurable:
    """A play that dies after host.env, .env or rclone.conf changed and before
    provision.sh finished must still provision on the next play, so the
    decision is a digest of those inputs compared with the one provision.sh
    records on success, not whether this play's writes changed something."""

    def test_the_digest_is_read_after_every_input_is_written(self):
        digest = _index("What provision.sh would run with")
        for name in ("host.env", "The stack's .env keys", "rclone.conf"):
            assert _index(name) < digest, name
        assert digest < _index("Provision the VM")

    def test_provision_runs_on_a_digest_mismatch(self):
        task = _tasks()[_index("Provision the VM")]
        when = str(task["when"])
        assert "_istota_provision_inputs.stdout" in when and "_istota_provisioned_with" in when
        assert "_istota_host_env is changed" not in when
