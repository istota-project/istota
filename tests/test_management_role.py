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

And four rules about the role's own tasks: root writes nothing below
`config/` or `secrets/`; every task whose arguments carry a secret has
`no_log`, and no secret value is on an argv; an exit 2 counts as a change
only when the command ran; and the provision and image decisions come after
the inputs they read.

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
    "proxied-self-signed": dict(ingress="proxied", hostname="istota.example.com", upstream_proxy="192.0.2.10",
                                listen_addr="10.0.0.5", listen_port=8443, tls_cert_source="self-signed"),
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


# --- secrets stay out of output -----------------------------------------------

#: Variables whose value is a credential, or may hold one (a config can).
_SECRET_VARS = {"istota_secrets", "istota_rclone_conf", "istota_user_secrets", "istota_config"}
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


def _ensure_calls(variables: dict) -> list[dict]:
    """Every ensure call the role's planning facts produce for `variables`,
    rendered with the core Jinja filters Ansible's own reduce to here."""
    from jinja2.nativetypes import NativeEnvironment

    text_env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    native_env = NativeEnvironment(undefined=jinja2.StrictUndefined)
    for env in (text_env, native_env):
        env.filters["to_json"] = json.dumps
        env.filters["from_json"] = json.loads
    calls: list[dict] = []
    for task in _tasks():
        module, body = _module(task)
        if module != "set_fact":
            continue
        context = dict(variables)
        for name, template in (task.get("vars") or {}).items():
            context[name] = text_env.from_string(template).render(context)
        for template in body.values():
            fact = native_env.from_string(template).render(context)
            for group in (fact.values() if isinstance(fact, dict) else [fact]):
                calls += [call for call in group if isinstance(call, dict) and "argv" in call]
    return calls


class TestTheEnsurePlan:
    VALUE = "kk-secret-value"

    def _vars(self) -> dict:
        return {
            "istota_users": {"alice": {"display-name": "Alice", "email": ["a@example.com"]}},
            "istota_user_resources": {}, "istota_user_briefings": {},
            "istota_user_secrets": {"alice": [
                {"service": "karakeep", "key": "api_key", "value": self.VALUE},
                {"service": "karakeep", "key": "base_url", "value": ""},
            ]},
        }

    def _secret_calls(self) -> list[dict]:
        return [c for c in _ensure_calls(self._vars()) if c["argv"][:2] == ["secret", "ensure"]]

    def test_a_secret_value_is_never_on_argv(self):
        """argv is in root's docker CLI cmdline and dockerd's exec records."""
        calls = self._secret_calls()
        assert calls
        for call in calls:
            assert self.VALUE not in call["argv"] and "--value" not in call["argv"], call["argv"]

    def test_the_value_goes_on_stdin(self):
        (call,) = self._secret_calls()
        assert "--value-stdin" in call["argv"]
        assert call["stdin"] == self.VALUE
        task = next(t for t in _tasks() if t.get("name") == "istota secret ensure")
        assert task["ansible.builtin.command"]["stdin"] == "{{ item.stdin }}"

    def test_an_empty_value_is_skipped(self):
        """As the old role did: ensure refuses an empty value, and `secret
        remove` is how one is cleared."""
        assert [c["argv"][c["argv"].index("--key") + 1] for c in self._secret_calls()] == ["api_key"]

    def test_the_user_calls_are_unchanged(self):
        users = [c for c in _ensure_calls(self._vars()) if c["argv"][:2] == ["user", "ensure"]]
        assert users == [{"label": "user alice", "argv": [
            "user", "ensure", "--name", "alice", "--display-name", "Alice", "--email", "a@example.com"]}]


# --- exit codes ---------------------------------------------------------------


class TestAnExitTwoIsOnlyAChangeWhenTheCommandRan:
    """The command module answers rc 2 when it cannot exec the program at all
    (ENOENT), with no start, end or delta. A task treating rc 2 as "changed"
    requires the command to have run."""

    def test_every_rc_two_task_requires_a_delta(self):
        found = 0
        for task in _tasks():
            failed_when = str(task.get("failed_when", ""))
            if "[0, 2]" not in failed_when:
                continue
            found += 1
            register = task["register"]
            assert f"{register}.delta is not defined" in failed_when, task.get("name")
        assert found >= 3


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
