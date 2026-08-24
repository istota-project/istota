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

    The rendered config has to name the path the role actually installs to. A
    change to either side alone leaves a config naming a binary that is not
    there, and `os.execve` exits 6 mid-task with `ENOENT`.

    Rewritten when the role stopped using apt: it now extracts both binaries
    from the vendors' release .debs into `/usr/local/bin`, so the old `/usr/bin`
    inference no longer holds. The previous premise test asserted `"apt:" in
    tasks` over the *whole* task file, which several other sections also
    satisfy — so it would have gone on passing against exactly this change.
    That is why the premise below names the install task and its method rather
    than a substring the file is never without.
    """

    def test_the_role_installs_the_forge_clis_from_the_vendor_releases(self):
        # The premise of the assertion below. `tests/test_ansible_forge_cli_
        # install.py` is where the install itself is held to its contract; this
        # only establishes that the inference about *where* it lands is sound.
        tasks = TASKS_FILE.read_text()

        assert "Install the forge CLIs from the vendors' releases" in tasks
        assert "dpkg-deb -x" in tasks

    @pytest.mark.parametrize(
        "key,binary",
        [("gh_bin_path", "gh"), ("glab_bin_path", "glab")],
    )
    def test_the_rendered_path_is_where_the_role_puts_the_binary(self, key, binary):
        config = load_config_from(render(istota_developer_enabled=True))

        assert getattr(config.developer, key) == f"/usr/local/bin/{binary}"

    def test_the_developer_block_is_absent_when_the_skill_is_off(self, parsed):
        # The role default is off, and an off skill should render no paths at
        # all rather than paths to binaries the role was never asked to install.
        assert "developer" not in parsed


def load_config_from(rendered: str) -> Config:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(rendered)
        return load_config(path)


class TestThePackageCacheRoot:
    """ISSUE-305, ISSUE-317, ISSUE-319 — the root, the sweep keys, the bind order.

    **This key has moved twice and the reasons are different, which is why the
    third reader gets a paragraph.** It shipped blank because there was nowhere
    good to put a package cache. `cc691d6f` derived it from
    `istota_developer_repos_dir` — `{repos_dir}/.package-caches` — because uv
    hardlinks out of its cache and `link(2)` compares mounts, so the cache has
    to be inside the bind that also holds the venv, and the repos bind was the
    only such bind. That root was shared by every user, which is ISSUE-319, and
    it cost about 200 lines of sibling masks to make safe.

    It is blank again now, and *not* for the original reason. The daemon derives
    the cache itself, per user, at `{repos_dir}/{user_id}/.package-caches`, and
    `resolve_sandbox_cache_dir` does not read this key at all while `repos_dir`
    is set. A value here would name the fallback path — what a deployment
    running the sandbox *without* the developer skill uses — while reading like
    the intended one. So the assertion below is not "the key is unused"; it is
    "the developer deployment must not set it".
    """

    def test_the_root_stays_blank_whatever_the_repos_dir_says(self):
        """Both ways. A blank `repos_dir` has no tree to derive from, and a set
        one derives inside the per-user subtree without consulting this key."""
        for repos_dir in ("", "/srv/example/repos"):
            rendered = tomllib.loads(render(istota_developer_repos_dir=repos_dir))
            assert "sandbox_cache_dir" not in rendered["security"], (
                f"the role set a cache root for repos_dir={repos_dir!r}; the "
                "daemon derives it and would ignore this value"
            )

    def test_the_default_render_puts_the_cache_inside_the_bind_that_covers_it(
        self, tmp_path,
    ):
        """The rendered default tied to the argv it produces.

        The repos bind has to come *after* the cache bind: that is the single
        mount uv hardlinks across, and the whole reason the cache is derived
        rather than configured. Move either bind and this goes red. What is
        *not* asserted any more is a mask after both — there is no other user's
        cache in the namespace to mask, which is the property that replaced it.
        """
        from unittest.mock import patch

        from istota.db import Task
        from istota.executor import build_bwrap_cmd

        repos = tmp_path / "repos"
        (repos / "alice").mkdir(parents=True)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        config = load_config_from(render(
            istota_developer_enabled=True, istota_developer_repos_dir=str(repos),
        ))
        config.temp_dir = tmp_path / "temp"
        assert config.security.sandbox_cache_dir == ""
        task = Task(id=1, prompt="x", user_id="alice", source_type="cli", status="running")

        with patch("istota.executor._bwrap_available", return_value=True):
            argv = build_bwrap_cmd(["claude"], config, task, True, [], user_temp)

        binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        cache = str(repos / "alice" / ".package-caches")
        assert cache in binds
        assert str(repos / "alice") in binds
        assert binds.index(str(repos / "alice")) > binds.index(cache), (
            "the repos bind no longer covers the cache bind — uv stops "
            "hardlinking and every worktree pays a full copy"
        )
        assert str(repos) not in binds, "the shared root was bound"

    def test_the_sweep_keys_render_when_an_operator_sets_the_root(self):
        rendered = tomllib.loads(
            render(istota_security_sandbox_cache_dir="/srv/example/repos/.caches")
        )

        assert rendered["security"]["sandbox_cache_dir"] == "/srv/example/repos/.caches"
        assert rendered["security"]["sandbox_cache_sweep_enabled"] is True
        assert rendered["security"]["sandbox_cache_max_gb"] > 0
        assert rendered["scheduler"]["sandbox_cache_sweep_interval"] > 0

    def test_the_role_creates_the_root_the_resolver_requires(self):
        """`resolve_sandbox_cache_dir` refuses a root that does not already exist.

        It falls open on every refusal — a warning in the log and the caches back
        on bubblewrap's root tmpfs — so a `sandbox_cache_dir` whose directory
        nothing creates is the same no-op the key was before, with the appearance
        of a fix. Nothing else in the tree creates it.
        """
        tasks = yaml.safe_load(TASKS_FILE.read_text())
        creators = [
            t for t in tasks
            if isinstance(t.get("file"), dict)
            and "istota_security_sandbox_cache_dir" in str(
                [t["file"].get("path"), t.get("loop")]
            )
        ]

        assert creators, "tasks/main.yml creates no package-cache root"
        task = creators[0]
        assert task["file"]["owner"] == "{{ istota_user }}"

        # 0700, because each subdirectory holds package archives uv trusts on
        # read and re-verifies against no hash.
        modes = [entry["mode"] for entry in task["loop"]
                 if "sandbox_cache_dir" in entry["path"]]
        assert modes == ["0700"]

        # Skipped entirely when the key is blank, which is the shipped default,
        # so no deployment gets a stray directory out of this.
        assert any(
            "istota_security_sandbox_cache_dir" in str(cond)
            for cond in task["when"]
        )
