"""``config.toml.j2`` renders to something ``load_config`` accepts.

The cheap piece of bare-metal coverage, and the one that addresses where
``30bb7c83``'s bug actually lived. Production is the Ansible shape, not the
Docker one: the role installs the forge CLIs to ``/usr/bin`` and renders those
paths into ``config.toml``, and nothing in the suite had ever rendered that
template. A key the code renamed, or a path the role stopped creating, showed up
first on a host.

Three properties, and the third is the one with teeth:

  * the rendered file parses as TOML and ``load_config`` accepts it;
  * every key the template emits exists on the corresponding dataclass — the
    loader ignores unknown keys, so a typo reaches every host and does nothing
    at all, silently;
  * ``developer.gh_bin_path`` names a path the role's own install tasks create.

**What this cannot see.** Ansible is not in the dependency set, so the template
is rendered with plain jinja2 plus shims for the two Ansible-provided filters it
uses. ``to_json`` and ``ternary`` are shimmed to their documented behaviour, and
for the values involved here — lists of strings, booleans — that is the same
output. Variable references *inside* ``defaults/main.yml`` are resolved to a
fixed point below, because Ansible resolves them recursively and jinja2 does
not; without that, ``db_path`` renders with a literal ``{{ istota_namespace }}``
in it. What none of this reproduces is inventory, host facts, or the vault, so
this asserts the template against its own defaults and nothing more.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from istota import config as config_module
from istota.config import Config, load_config

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "deploy" / "ansible"
TEMPLATE = ANSIBLE / "templates" / "config.toml.j2"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"
TASKS_FILE = ANSIBLE / "tasks" / "main.yml"


def _custom_filters() -> dict:
    """The role's own filter plugin, loaded the way test_ansible_briefing_blocks_toml does."""
    spec = importlib.util.spec_from_file_location(
        "istota_toml", ANSIBLE / "filter_plugins" / "istota_toml.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FilterModule().filters()


def _ternary(value, true_val, false_val, none_val=None):
    if value is None and none_val is not None:
        return none_val
    return true_val if value else false_val


def _environment() -> Environment:
    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    env.filters.update(_custom_filters())
    # Ansible-provided, not jinja2-provided.
    env.filters["to_json"] = lambda v, **kw: json.dumps(v, **kw)
    env.filters["ternary"] = _ternary
    return env


def _resolve(variables: dict, env: Environment) -> dict:
    """Expand `{{ other_var }}` inside the defaults, the way Ansible would.

    Iterates to a fixed point rather than once: `istota_repo_dir` is
    `{{ istota_home }}/istota` and `istota_home` is itself
    `/srv/app/{{ istota_namespace }}`, so a single pass leaves a template in the
    output. Bounded, because a genuine cycle in the defaults should fail here
    loudly rather than hang the suite.
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


# The one host fact the defaults read: `istota_browser_cpu_limit` is
# `{{ ansible_facts['processor_vcpus'] }}`. Supplied rather than stubbed away,
# because a *second* fact appearing in the defaults should fail this file
# loudly — StrictUndefined does that — rather than render as an empty string.
FACTS = {"processor_vcpus": 4}


def render(**overrides) -> str:
    env = _environment()
    variables = _resolve(
        {
            **yaml.safe_load(DEFAULTS_FILE.read_text()),
            "ansible_facts": FACTS,
            **overrides,
        },
        env,
    )
    return env.from_string(TEMPLATE.read_text()).render(**variables)


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


@pytest.fixture(scope="module")
def parsed(rendered: str) -> dict:
    return tomllib.loads(rendered)


class TestItRendersSomethingTheLoaderAccepts:
    def test_no_jinja_survives_into_the_output(self, rendered):
        # A `{{ istota_namespace }}` left in a path is the failure mode this
        # file's own harness had before the fixed-point pass, and it is also
        # what an Ansible variable named in the template but missing from the
        # defaults would look like on a host.
        assert "{{" not in rendered
        assert "{%" not in rendered

    def test_the_output_is_valid_toml(self, rendered):
        tomllib.loads(rendered)

    def test_load_config_parses_it(self, tmp_path, rendered):
        path = tmp_path / "config.toml"
        path.write_text(rendered)

        config = load_config(path)

        assert isinstance(config, Config)
        assert config.bot_name


class TestEveryRenderedKeyIsARealField:
    """The loader ignores unknown keys, so a rename is silent on a host.

    Walked section by section against the dataclass tree rather than flattened:
    a key that is valid under `[scheduler]` and meaningless under `[web]` should
    fail, and a flat name set cannot tell those apart.
    """

    @pytest.mark.parametrize(
        "section", ["scheduler", "security", "web", "logging", "nextcloud", "brain"]
    )
    def test_the_walk_descends_into_the_sections_that_matter(self, parsed, section):
        """Guard on the guard, asserting the descent rather than the presence.

        The first version of this only checked ``section in parsed``, which is
        not the same claim. ``_nested_dataclass`` resolves a field's annotation
        to a dataclass by name; if that resolution broke — a field annotated
        ``SchedulerConfig | None``, a qualified name, ``config.py`` moving off
        ``from __future__ import annotations`` — the walk would silently check
        top-level keys only, and a presence check would stay green over a
        coverage claim that had collapsed to nothing.

        So: inject a key that cannot be a real field, and require the walk to
        report it at the right depth.
        """
        assert section in parsed, f"[{section}] is not in the rendered template"

        poisoned = {**parsed, section: {**parsed[section], "zzz_not_a_field": 1}}

        assert f"{section}.zzz_not_a_field" in _unknown_keys(poisoned, Config, prefix="")

    def test_no_section_names_a_field_the_dataclass_does_not_have(self, parsed):
        unknown = sorted(_unknown_keys(parsed, Config, prefix=""))

        assert not unknown, (
            "config.toml.j2 renders keys no dataclass has. The loader drops "
            f"these silently on every host: {unknown}"
        )


def _unknown_keys(section: dict, target, prefix: str) -> list[str]:
    """Every dotted key in `section` with no matching field on `target`.

    Descends only where the dataclass field is itself a dataclass. A dict-valued
    field like `users` is user data, not config schema — its keys are user ids
    and cannot be checked against a field list.
    """
    if not is_dataclass(target):
        return []

    by_name = {f.name: f for f in fields(target)}
    problems: list[str] = []

    for key, value in section.items():
        dotted = f"{prefix}{key}"
        field = by_name.get(key)
        if field is None:
            problems.append(dotted)
            continue
        if isinstance(value, dict):
            nested = _nested_dataclass(field.type)
            if nested is not None:
                problems.extend(_unknown_keys(value, nested, prefix=f"{dotted}."))

    return problems


def _nested_dataclass(annotation):
    """The dataclass a field's annotation names, or None.

    Handles both forms, because `config.py` today gives type objects and a
    future `from __future__ import annotations` there would give strings —
    either way the name is what gets resolved against the config module.

    Returns None for anything that is not a plain dataclass annotation, which
    includes unions like `SchedulerConfig | None`. That is a silent loss of
    coverage rather than an error, so
    `TestEveryRenderedKeyIsARealField::test_the_walk_descends_into_the_sections_that_matter`
    asserts the descent for each section that matters instead of trusting this.
    """
    name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    candidate = getattr(config_module, name, None)
    return candidate if is_dataclass(candidate) else None


class TestTheForgePathsMatchWhatTheRoleInstalls:
    """Where 30bb7c83 lived.

    The role installs `gh` and `glab` from the Debian archive with `apt`, which
    puts them in `/usr/bin`. The rendered config has to name that. A change to
    either side alone — switching the install to a vendor tarball under
    `/usr/local`, or editing the default path — leaves a config naming a binary
    that is not there, and `os.execve` exits 6 mid-task with `ENOENT`.
    """

    def test_the_role_installs_the_forge_clis_with_apt(self):
        # The premise of the assertion below. If the role stops using apt, the
        # /usr/bin inference stops holding and this test should be rewritten
        # rather than quietly kept.
        tasks = TASKS_FILE.read_text()

        assert "Install forge CLIs for the developer skill" in tasks
        assert "apt:" in tasks

    @pytest.mark.parametrize(
        "key,binary",
        [("gh_bin_path", "gh"), ("glab_bin_path", "glab")],
    )
    def test_the_rendered_path_is_where_apt_puts_the_binary(self, key, binary):
        config = load_config_from(render(istota_developer_enabled=True))

        assert getattr(config.developer, key) == f"/usr/bin/{binary}"

    def test_the_developer_block_is_absent_when_the_skill_is_off(self, parsed):
        # The role default is off, and an off skill should render no paths at
        # all rather than paths to binaries apt was never asked to install.
        assert "developer" not in parsed


def load_config_from(rendered: str) -> Config:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(rendered)
        return load_config(path)
