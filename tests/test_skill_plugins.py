"""Skill plugins: discovery, the manifest restrictions and the four-layer index.

Spec: external-skill-packages, Stage 1; the secret registry and snapshot
classes at the end are Stage 3's. The dispatch and credential-channel cases
live in `tests/test_skill_plugin_dispatch.py`.
"""

import importlib
import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from istota.lib import skill_module
from istota.lib.skill_module import (
    RESERVED_SKILL_NAMES,
    plugin_name_problem,
    valid_plugin_value,
)
from istota.skills import _loader
from istota.skills._loader import (
    compute_skills_fingerprint,
    discover_plugin_skills,
    effective_disabled_skills,
    expand_companions,
    load_skill_index,
    plugin_problems,
    resolve_skill_module,
)
from istota.skills._types import PluginProblem, SkillMeta, is_plugin
from tests.support.plugin_fixture import SENTINEL_ENV, build_plugins, render_skill_md

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def plugin_builder(tmp_path):
    yield from build_plugins(tmp_path)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    bundled = tmp_path / "bundled"
    bundled.mkdir(exist_ok=True)
    return ops, bundled


def _write_skill(base: Path, name: str, frontmatter: dict) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.md").write_text(render_skill_md(frontmatter))
    return skill_dir


def _index(tmp_path, eps, bundled=None):
    ops, empty = _dirs(tmp_path)
    return load_skill_index(ops, bundled if bundled is not None else empty, plugin_entry_points=eps)


def _reasons(tmp_path, eps, bundled=None) -> dict[str, list[str]]:
    _, empty = _dirs(tmp_path)
    out: dict[str, list[str]] = {}
    for problem in plugin_problems(bundled if bundled is not None else empty, plugin_entry_points=eps):
        out.setdefault(problem.name, []).append(problem.reason)
    return out


class TestTheLeaf:
    def test_names(self):
        assert plugin_name_problem("example") is None
        assert plugin_name_problem("example_2") is None
        for bad in ("Logs", "_x", "a-b", "", "1x", "a.b", "example\n"):
            assert plugin_name_problem(bad) == "bad_name", bad
        for reserved in sorted(RESERVED_SKILL_NAMES):
            assert plugin_name_problem(reserved) == "reserved_name", reserved

    def test_values(self):
        assert valid_plugin_value("example_plugin")
        assert valid_plugin_value("ExamplePlugin")
        for bad in ("pkg:attr", "pkg.sub", "pkg-x", "", " pkg", "pkg\n", "pkg [extra]"):
            assert not valid_plugin_value(bad), bad

    def test_the_reserved_set_covers_every_scope_token_it_copies(self):
        from istota.rooms.scopes import SYNTHETIC_SCOPES
        from istota.sandbox.user_scope import ROOM_NOTES_WITHHELD
        from istota.user_profiles import CORRESPONDENT_REFUSED_SCOPES

        expected = set(SYNTHETIC_SCOPES) | {ROOM_NOTES_WITHHELD} | set(CORRESPONDENT_REFUSED_SCOPES) | {"web"}
        assert expected <= RESERVED_SKILL_NAMES

    def test_the_loader_uses_the_leafs_rules(self):
        assert _loader.plugin_name_problem is skill_module.plugin_name_problem
        assert _loader.valid_plugin_value is skill_module.valid_plugin_value


class TestTheSkillMetaFields:
    def test_defaults(self):
        meta = SkillMeta(name="x", description="")
        assert (meta.module, meta.origin, meta.plugin_dist) == ("", "", "")
        assert not is_plugin(meta)

    def test_is_plugin_keys_on_module(self):
        assert is_plugin(SkillMeta(name="x", description="", module="pkg", origin="operator"))
        assert not is_plugin(SkillMeta(name="x", description="", origin="plugin"))

    def test_plugin_problem_is_frozen(self):
        problem = PluginProblem(name="x", dist="", reason="bad_name")
        with pytest.raises(Exception):
            problem.reason = "other"


class TestTomlFallback:
    def test_a_plugin_directory_reads_no_skill_toml(self, tmp_path):
        d = tmp_path / "pkg"
        d.mkdir()
        (d / "skill.toml").write_text('description = "from toml"\ncli = true\n')
        assert _loader._load_skill_meta(d, toml_fallback=False) is None
        assert _loader._load_skill_meta(d).description == "from toml"

    def test_frontmatter_wins_and_toml_fills_nothing(self, tmp_path):
        d = tmp_path / "pkg"
        d.mkdir()
        (d / "skill.md").write_text(render_skill_md({"description": "from md"}))
        (d / "skill.toml").write_text("cli = true\n")
        meta = _loader._load_skill_meta(d, toml_fallback=False)
        assert meta.description == "from md" and meta.cli is False
        assert _loader._load_skill_meta(d).cli is True


class TestAValidPlugin:
    def test_it_is_in_the_index(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(extra={
            "admin_only": True, "companion_skills": ["untrusted_input"], "egress": "external",
        })
        index = _index(tmp_path, [p.entry_point])
        meta = index[p.skill]
        assert meta.name == p.skill
        assert meta.module == p.package
        assert meta.origin == "plugin"
        assert meta.plugin_dist == ""
        assert Path(meta.skill_dir) == p.package_dir
        assert meta.admin_only is True
        assert meta.cli is True
        assert meta.companion_skills == ["untrusted_input"]
        assert meta.egress == "external"
        assert meta.shared_room == "private"
        assert [(s.var, s.source, s.service, s.key, s.sensitive) for s in meta.env_specs] == [
            (p.token_var, "secret", "plugin_example", "token", True),
        ]
        assert is_plugin(meta)
        assert plugin_problems(_dirs(tmp_path)[1], plugin_entry_points=[p.entry_point]) == []

    def test_an_installed_plugin_is_found_with_no_injection(self, tmp_path, plugin_builder):
        p = plugin_builder.installed()
        ops, bundled = _dirs(tmp_path)
        meta = load_skill_index(ops, bundled)[p.skill]
        assert meta.module == p.package
        assert meta.plugin_dist == f"{p.dist_name} {p.version}"
        assert Path(meta.skill_dir).resolve() == p.package_dir.resolve()

    def test_a_name_in_frontmatter_is_ignored(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(extra={"name": "bookmarks"})
        index = _index(tmp_path, [p.entry_point])
        assert p.skill in index and "bookmarks" not in index

    def test_discovery_does_not_import_the_plugin(self, tmp_path, plugin_builder, monkeypatch):
        sentinel = tmp_path / "imported.txt"
        monkeypatch.setenv(SENTINEL_ENV, str(sentinel))
        p = plugin_builder.installed()
        ops, bundled = _dirs(tmp_path)
        assert p.skill in load_skill_index(ops, bundled)
        assert plugin_problems(bundled) == []
        assert not sentinel.exists()
        assert p.package not in sys.modules
        # Control: importing it does write the sentinel.
        importlib.import_module(p.package)
        assert sentinel.read_text() == "imported\n"


class TestTheBundledClash:
    def test_bundled_wins_and_the_clash_is_a_problem(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(skill="bookmarks")
        ops = tmp_path / "ops"
        ops.mkdir()
        index = load_skill_index(ops, plugin_entry_points=[p.entry_point])
        assert index["bookmarks"].module == ""
        assert index["bookmarks"].origin == "bundled"
        problems = plugin_problems(plugin_entry_points=[p.entry_point])
        assert [(x.name, x.reason) for x in problems] == [("bookmarks", "clashes_with_bundled")]

    def test_a_plugin_replaces_a_legacy_entry(self, tmp_path, plugin_builder):
        p = plugin_builder.injected()
        ops, bundled = _dirs(tmp_path)
        (ops / "_index.toml").write_text(f'[{p.skill}]\ndescription = "legacy"\n')
        meta = load_skill_index(ops, bundled, plugin_entry_points=[p.entry_point])[p.skill]
        assert meta.origin == "plugin" and meta.module == p.package
        assert plugin_problems(bundled, plugin_entry_points=[p.entry_point]) == []


class TestTheLoaderIsQuiet:
    """A problem never reaches stderr, where a `skills` subprocess would hand it to the model."""

    def _run(self, tmp_path, plugin_builder, *, loud: bool) -> subprocess.CompletedProcess:
        clash = plugin_builder.installed(skill="bookmarks", activate=False)
        reserved = plugin_builder.installed(skill="room_notes", activate=False)
        ops = tmp_path / "ops"
        ops.mkdir(exist_ok=True)
        patch = "L.logger.debug = L.logger.warning\n" if loud else ""
        code = textwrap.dedent(
            "import sys\n"
            "from pathlib import Path\n"
            "import istota.skills._loader as L\n"
        ) + patch + f"index = L.load_skill_index(Path({str(ops)!r}))\n"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(clash.site), str(reserved.site), str(REPO / "src")]
        )
        return subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120,
        )

    def test_nothing_on_stderr(self, tmp_path, plugin_builder):
        result = self._run(tmp_path, plugin_builder, loud=False)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_control_the_same_run_at_warning_does_print(self, tmp_path, plugin_builder):
        result = self._run(tmp_path, plugin_builder, loud=True)
        assert result.returncode == 0, result.stderr
        assert "clashes_with_bundled" in result.stderr
        assert "reserved_name" in result.stderr


class TestDuplicates:
    def test_two_entry_points_with_one_name_load_neither(self, tmp_path, plugin_builder):
        a = plugin_builder.injected()
        b = plugin_builder.injected(skill=a.skill)
        index = _index(tmp_path, [a.entry_point, b.entry_point])
        assert a.skill not in index
        assert _reasons(tmp_path, [a.entry_point, b.entry_point]) == {
            a.skill: ["duplicate_plugin", "duplicate_plugin"],
        }

    def test_two_plugins_sharing_an_env_var_load_neither(self, tmp_path, plugin_builder):
        spec = {"var": "PLUGIN_A_B_X", "from": "secret", "service": "plugin_ab", "key": "x"}
        a = plugin_builder.injected(skill="a", env=[spec])
        ab = plugin_builder.injected(skill="a_b", env=[spec])
        index = _index(tmp_path, [a.entry_point, ab.entry_point])
        assert "a" not in index and "a_b" not in index
        assert _reasons(tmp_path, [a.entry_point, ab.entry_point]) == {
            "a": ["duplicate_env_var"], "a_b": ["duplicate_env_var"],
        }

    def test_the_prefix_rule_accepts_each_alone(self, tmp_path, plugin_builder):
        spec = {"var": "PLUGIN_A_B_X", "from": "secret", "service": "plugin_ab", "key": "x"}
        a = plugin_builder.injected(skill="a", env=[spec])
        assert "a" in _index(tmp_path, [a.entry_point])


class TestEachRejection:
    """Every rejection is skipped with its reason; the rest of the index is intact."""

    def _assert_only(self, tmp_path, plugin_builder, bad_ep, reason):
        good = plugin_builder.injected()
        bundled = _dirs(tmp_path)[1]
        _write_skill(bundled, "bundled_one", {"description": "bundled"})
        eps = [bad_ep, good.entry_point]
        index = _index(tmp_path, eps, bundled=bundled)
        assert bad_ep.name not in index
        assert good.skill in index and "bundled_one" in index
        assert _reasons(tmp_path, eps, bundled=bundled) == {bad_ep.name: [reason]}

    @pytest.mark.parametrize("name", ["Logs", "_x", "a-b"])
    def test_bad_name(self, tmp_path, plugin_builder, name):
        p = plugin_builder.injected(skill=name)
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "bad_name")

    @pytest.mark.parametrize("name", sorted(RESERVED_SKILL_NAMES))
    def test_reserved_name(self, tmp_path, plugin_builder, name):
        p = plugin_builder.injected(skill=name)
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "reserved_name")

    def test_an_attribute_value(self, tmp_path, plugin_builder):
        p = plugin_builder.injected()
        ep = importlib.metadata.EntryPoint(p.skill, f"{p.package}:main", "istota.skills")
        self._assert_only(tmp_path, plugin_builder, ep, "bad_value")

    def test_a_dotted_value(self, tmp_path, plugin_builder, monkeypatch):
        sentinel = tmp_path / "imported.txt"
        monkeypatch.setenv(SENTINEL_ENV, str(sentinel))
        p = plugin_builder.injected()
        ep = importlib.metadata.EntryPoint(p.skill, f"{p.package}.sub", "istota.skills")
        self._assert_only(tmp_path, plugin_builder, ep, "bad_value")
        assert not sentinel.exists()

    def test_a_missing_module(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(write=False)
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "module_not_found")

    def test_a_plain_module(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(layout="module")
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "not_a_package")

    def test_a_namespace_package(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(layout="namespace")
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "namespace_package")

    def test_no_skill_md(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(write_skill_md=False)
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "no_skill_md")

    def test_a_skill_toml_without_skill_md(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(write_skill_md=False, skill_toml='description = "x"\ncli = true\n')
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "no_skill_md")

    def test_no_frontmatter(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(skill_md="# Example\n\nNo frontmatter here.\n")
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "unparseable_skill_md")

    def test_frontmatter_that_fails_to_parse(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(skill_md='---\ndescription: x\nenv: [{"var": oops}]\n---\nbody\n')
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "unparseable_skill_md")

    def test_find_spec_raising(self, tmp_path, plugin_builder, monkeypatch):
        p = plugin_builder.injected()
        real = importlib.util.find_spec

        def find_spec(name, *args, **kwargs):
            if name == p.package:
                raise ValueError("broken finder")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", find_spec)
        self._assert_only(tmp_path, plugin_builder, p.entry_point, "module_not_found")


class TestShadowing:
    def test_a_same_named_package_earlier_on_the_path_is_shadowed(self, tmp_path, plugin_builder):
        p = plugin_builder.installed()
        ops, bundled = _dirs(tmp_path)
        assert p.skill in load_skill_index(ops, bundled)  # before the shadow
        plugin_builder.shadow(p.package)
        assert p.skill not in load_skill_index(ops, bundled)
        assert [(x.name, x.dist, x.reason) for x in plugin_problems(bundled)] == [
            (p.skill, f"{p.dist_name} {p.version}", "shadowed"),
        ]


class TestTheManifestFields:
    @pytest.mark.parametrize("extra", [
        {"always_include": True},
        {"egress": "local"},
        {"shared_room": "safe"},
        {"exclude_skills": ["sensitive_actions"]},
        {"dependencies": ["requests"]},
    ], ids=["always_include", "egress_local", "shared_room_safe", "exclude_skills", "dependencies"])
    def test_a_forbidden_field(self, tmp_path, plugin_builder, extra):
        p = plugin_builder.injected(extra=extra)
        assert p.skill not in _index(tmp_path, [p.entry_point])
        assert _reasons(tmp_path, [p.entry_point]) == {p.skill: ["forbidden_manifest_field"]}

    def test_what_a_plugin_may_state(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(extra={
            "always_include": False, "egress": "external", "shared_room": "private",
        })
        meta = _index(tmp_path, [p.entry_point])[p.skill]
        assert (meta.egress, meta.shared_room, meta.always_include) == ("external", "private", False)


class TestTheEnvSpecs:
    def _spec(self, p, **changes):
        spec = {"var": p.token_var, "from": "secret", "service": "plugin_example", "key": "token"}
        spec.update(changes)
        return {k: v for k, v in spec.items() if v is not None}

    @pytest.mark.parametrize("changes", [
        {"from": "config", "service": None, "key": None, "config_path": "nextcloud.url"},
        {"from": "template_file", "service": None, "key": None, "template": "x"},
        {"fallback_var": "SOME_VAR"},
        {"proxy_only": True},
        {"var": "KARAKEEP_API_KEY"},
        {"var": "PLUGIN_OTHER_TOKEN"},
        {"service": "karakeep"},
        {"service": "vault"},
        {"service": "mail_account"},
        {"service": "example"},
    ], ids=[
        "from_config", "from_template_file", "fallback_var", "proxy_only", "bundled_var",
        "another_prefix", "service_karakeep", "service_vault", "service_mail_account",
        "service_unprefixed",
    ])
    def test_a_forbidden_spec(self, tmp_path, plugin_builder, changes):
        p = plugin_builder.injected()
        (p.package_dir / "skill.md").write_text(render_skill_md({
            "description": "Example", "cli": True, "env": [self._spec(p, **changes)],
        }))
        assert p.skill not in _index(tmp_path, [p.entry_point])
        assert _reasons(tmp_path, [p.entry_point]) == {p.skill: ["forbidden_env_spec"]}

    def test_the_fixture_spec_is_accepted(self, tmp_path, plugin_builder):
        p = plugin_builder.injected()
        assert p.token_var.startswith("PLUGIN_EXAMPLE_")
        meta = _index(tmp_path, [p.entry_point])[p.skill]
        assert [s.var for s in meta.env_specs] == [p.token_var]

    @pytest.mark.parametrize("source", ["setup_env", "user_id"])
    def test_the_other_allowed_sources(self, tmp_path, plugin_builder, source):
        p = plugin_builder.injected()
        spec = {"var": f"PLUGIN_{p.skill.upper()}_VALUE", "from": source, "when": "nextcloud.url"}
        (p.package_dir / "skill.md").write_text(render_skill_md({
            "description": "Example", "cli": True, "env": [spec],
        }))
        meta = _index(tmp_path, [p.entry_point])[p.skill]
        assert [(s.var, s.source) for s in meta.env_specs] == [(spec["var"], source)]

    def test_no_bundled_manifest_declares_a_plugin_variable(self, tmp_path):
        ops = tmp_path / "ops"
        ops.mkdir()
        index = load_skill_index(ops, plugin_entry_points=[])
        assert len(index) > 10
        offenders = sorted(
            f"{name}:{spec.var}" for name, meta in index.items()
            for spec in meta.env_specs if spec.var.startswith("PLUGIN_")
        )
        assert offenders == []


class TestTheOperatorLayer:
    def test_an_override_of_a_plugin_inherits_module_and_dist(self, tmp_path, plugin_builder):
        p = plugin_builder.installed()
        ops, bundled = _dirs(tmp_path)
        override = _write_skill(ops, p.skill, {"description": "operator doc", "cli": True})
        meta = load_skill_index(ops, bundled)[p.skill]
        assert meta.description == "operator doc"
        assert meta.skill_dir == str(override)
        assert meta.origin == "operator"
        assert meta.module == p.package
        assert meta.plugin_dist == f"{p.dist_name} {p.version}"
        assert is_plugin(meta)

    def test_an_operator_only_skill_is_not_a_plugin(self, tmp_path):
        ops, bundled = _dirs(tmp_path)
        _write_skill(ops, "local_only", {"description": "operator", "cli": True})
        meta = load_skill_index(ops, bundled, plugin_entry_points=[])["local_only"]
        assert meta.module == "" and meta.origin == "operator"
        assert not is_plugin(meta)

    def test_an_override_of_a_bundled_skill_stays_bundled_dispatch(self, tmp_path):
        ops, bundled = _dirs(tmp_path)
        _write_skill(bundled, "thing", {"description": "bundled"})
        _write_skill(ops, "thing", {"description": "operator"})
        meta = load_skill_index(ops, bundled, plugin_entry_points=[])["thing"]
        assert (meta.origin, meta.module, meta.description) == ("operator", "", "operator")


class TestTheModuleResolver:
    """`resolve_skill_module`: which package runs a skill (Stage 2)."""

    def test_a_bundled_skill(self, tmp_path):
        ops, bundled = _dirs(tmp_path)
        _write_skill(bundled, "thing", {"description": "bundled"})
        index = load_skill_index(ops, bundled, plugin_entry_points=[])
        assert resolve_skill_module("thing", index) == "istota.skills.thing"

    def test_a_plugin(self, tmp_path, plugin_builder):
        p = plugin_builder.injected()
        index = _index(tmp_path, [p.entry_point])
        assert resolve_skill_module(p.skill, index) == p.package

    def test_an_operator_entry_over_a_plugin(self, tmp_path, plugin_builder):
        p = plugin_builder.injected()
        ops, bundled = _dirs(tmp_path)
        _write_skill(ops, p.skill, {"description": "operator doc", "cli": True})
        index = load_skill_index(ops, bundled, plugin_entry_points=[p.entry_point])
        assert resolve_skill_module(p.skill, index) == p.package

    def test_an_unknown_name_raises(self):
        with pytest.raises(KeyError):
            resolve_skill_module("nothing_here", {})


class TestOrigins:
    def test_bundled_and_legacy(self, tmp_path):
        ops, bundled = _dirs(tmp_path)
        _write_skill(bundled, "thing", {"description": "bundled"})
        (ops / "_index.toml").write_text('[old]\ndescription = "legacy"\n')
        index = load_skill_index(ops, bundled, plugin_entry_points=[])
        assert index["thing"].origin == "bundled"
        assert index["old"].origin == "legacy"


class TestTheCache:
    def test_injected_entry_points_bypass_it(self, tmp_path, plugin_builder):
        a = plugin_builder.injected()
        b = plugin_builder.injected()
        assert set(_index(tmp_path, [a.entry_point])) == {a.skill}
        assert set(_index(tmp_path, [b.entry_point])) == {b.skill}

    def test_default_discovery_is_cached_per_process(self, tmp_path, plugin_builder, monkeypatch):
        calls = []
        real = importlib.metadata.entry_points

        def counting(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(importlib.metadata, "entry_points", counting)
        _loader._reset_plugin_cache()
        ops, bundled = _dirs(tmp_path)
        load_skill_index(ops, bundled)
        load_skill_index(ops, bundled)
        plugin_problems(bundled)
        assert calls == [{"group": "istota.skills"}]

    def test_a_caller_mutating_its_index_does_not_reach_the_cache(self, tmp_path, plugin_builder):
        p = plugin_builder.installed()
        ops, bundled = _dirs(tmp_path)
        first = load_skill_index(ops, bundled)
        first[p.skill].description = "changed"
        first[p.skill].env_specs.clear()
        second = load_skill_index(ops, bundled)
        assert second[p.skill].description == "Example plugin skill"
        assert len(second[p.skill].env_specs) == 1

    def test_discovery_returns_plugins_and_problems(self, tmp_path, plugin_builder):
        good = plugin_builder.injected()
        bad = plugin_builder.injected(skill="Bad")
        plugins, problems = discover_plugin_skills([good.entry_point, bad.entry_point])
        assert set(plugins) == {good.skill}
        assert problems == [PluginProblem(name="Bad", dist="", reason="bad_name")]


class TestAPluginIsAnOrdinarySkill:
    def test_effective_disabled_skills(self, tmp_path, plugin_builder, make_config):
        p = plugin_builder.injected()
        q = plugin_builder.injected(extra={"requires_capability": ["browser"]})
        index = _index(tmp_path, [p.entry_point, q.entry_point])
        config = make_config(disabled_skills=[p.skill])
        disabled = effective_disabled_skills(config, "alice", index)
        assert p.skill in disabled and q.skill in disabled

    def test_expand_companions(self, tmp_path, plugin_builder):
        p = plugin_builder.injected(extra={"companion_skills": ["helper"]})
        ops, bundled = _dirs(tmp_path)
        _write_skill(bundled, "helper", {"description": "helper"})
        index = load_skill_index(ops, bundled, plugin_entry_points=[p.entry_point])
        assert expand_companions([p.skill], index) == ["helper"]
        assert expand_companions([p.skill], index, disabled_skills={"helper"}) == []

    def test_the_fingerprint_ignores_plugins(self, tmp_path, plugin_builder):
        ops, bundled = _dirs(tmp_path)
        _write_skill(bundled, "thing", {"description": "bundled"})
        before = compute_skills_fingerprint(ops, bundled)
        p = plugin_builder.installed()
        assert p.skill in load_skill_index(ops, bundled)
        assert compute_skills_fingerprint(ops, bundled) == before


def _install(tmp_path, monkeypatch):
    """A config and framework DB for the `istota secret` and plan cases.

    ``skills_dir`` is an operator layer of its own, so a test can put an
    operator entry over an installed plugin; the bundled layer is the real one.
    """
    from istota import db, user_profiles
    from istota.lib import toml_write

    monkeypatch.delenv("ISTOTA_ADMINS_FILE", raising=False)
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "plugin-registry-key-" * 3)
    database = tmp_path / "framework.db"
    db.init_db(database)
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({
        "db_path": str(database), "temp_dir": str(tmp_path / "temp"),
        "workspace_path": str(tmp_path / "workspace"), "skills_dir": str(ops),
    }))
    user_profiles.provision_user(database, "alice", {})
    return config, database, ops


def _operator_mail_account_override(ops: Path, skill: str) -> None:
    """An operator entry over a plugin whose `env:` names a built-in service.

    The operator layer's `env:` is not put through the plugin allowlist, so
    only `plugin_service_keys`' own prefix filter keeps `mail_account` out.
    """
    _write_skill(ops, skill, {"description": "operator doc", "cli": True, "env": [
        {"var": "OPS_MAIL", "from": "secret", "service": "mail_account",
         "key": "1.imap_password", "sensitive": True},
        {"var": "OPS_OTHER", "from": "secret", "service": "plugin_other", "key": "password"},
    ]})


def _secret_cli(monkeypatch, capsys, config, *argv, stdin="value-placeholder\n"):
    import io

    from istota import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), "secret", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    output = capsys.readouterr()
    return code, output.out, output.err


def _plan_secrets(config, declarations, values):
    from istota.settings_plan import engine
    from istota.settings_plan.schema import parse_plan, parse_sidecar

    context = engine.Context(config, sidecar=parse_sidecar({"users": {"alice": values}}))
    return engine.build_plan(parse_plan({"users": {"alice": {"secrets": declarations}}}),
                             context, engine.Options())


class TestTheSecretRegistry:
    """A plugin's `from: secret` services join the writable registry (Stage 3).

    Only when a caller hands the three schema functions an index: with none,
    which is how the web settings routes call them, the registry is today's.
    """

    def test_plugin_service_keys_returns_the_plugins_pair(self, tmp_path, plugin_builder):
        from istota.credentials.schema import plugin_service_keys

        p = plugin_builder.injected()
        assert plugin_service_keys(_index(tmp_path, [p.entry_point])) == {"plugin_example": ("token",)}

    def test_the_bundled_index_holds_no_plugin_pair(self, tmp_path):
        from istota.credentials.schema import plugin_service_keys

        ops, _ = _dirs(tmp_path)
        assert plugin_service_keys(load_skill_index(ops, plugin_entry_points=[])) == {}

    def test_the_schema_functions_take_the_pair_only_with_an_index(self, tmp_path, plugin_builder):
        from istota.credentials.schema import (
            all_known_services,
            known_service_keys,
            service_accepts_key,
        )

        p = plugin_builder.installed()
        index = _index(tmp_path, None)
        assert p.skill in index
        widened = all_known_services(index)["plugin_example"]
        assert [field["key"] for field in widened["fields"]] == ["token"]
        assert "key_rule" not in widened
        assert known_service_keys(index)["plugin_example"] == frozenset({"token"})
        assert service_accepts_key("plugin_example", "token", skill_index=index)
        assert not service_accepts_key("plugin_example", "other", skill_index=index)
        # No index: the static registry, whatever is installed.
        assert "plugin_example" not in all_known_services()
        assert "plugin_example" not in known_service_keys()
        assert not service_accepts_key("plugin_example", "token")
        assert set(all_known_services(index)) - set(all_known_services()) == {"plugin_example"}

    def test_an_operator_entry_over_a_plugin_cannot_add_a_built_in_service(
        self, tmp_path, plugin_builder,
    ):
        from istota.credentials.schema import all_known_services, plugin_service_keys

        p = plugin_builder.injected()
        ops, bundled = _dirs(tmp_path)
        _operator_mail_account_override(ops, p.skill)
        index = load_skill_index(ops, bundled, plugin_entry_points=[p.entry_point])
        assert is_plugin(index[p.skill])
        assert plugin_service_keys(index) == {"plugin_other": ("password",)}
        assert "mail_account" not in all_known_services(index)

    def test_a_non_plugin_entry_adds_nothing(self, tmp_path):
        from istota.credentials.schema import plugin_service_keys

        ops, bundled = _dirs(tmp_path)
        _write_skill(ops, "local_only", {"description": "operator", "env": [
            {"var": "LOCAL_TOKEN", "from": "secret", "service": "plugin_local", "key": "token"},
        ]})
        index = load_skill_index(ops, bundled, plugin_entry_points=[])
        assert plugin_service_keys(index) == {}

    def test_istota_secret_ensure_writes_a_plugin_service(
        self, tmp_path, plugin_builder, monkeypatch, capsys,
    ):
        from istota.credentials import store

        config, database, _ = _install(tmp_path, monkeypatch)
        plugin_builder.installed()
        code, out, err = _secret_cli(
            monkeypatch, capsys, config, "ensure", "--user", "alice",
            "--service", "plugin_example", "--key", "token", "--value-stdin",
            stdin="plugin-token-placeholder\n",
        )
        assert code == 0, err
        assert "STATE: created" in out
        assert store.get_secret(database, "alice", "plugin_example", "token") == "plugin-token-placeholder"

        code, out, err = _secret_cli(
            monkeypatch, capsys, config, "ensure", "--user", "alice",
            "--service", "plugin_example", "--key", "other", "--value-stdin",
        )
        assert code == 1
        assert "unknown key 'other'" in err
        assert store.get_secret(database, "alice", "plugin_example", "other") is None

    def test_istota_secret_ensure_refuses_a_plugin_service_nothing_declares(
        self, tmp_path, monkeypatch, capsys,
    ):
        config, _, _ = _install(tmp_path, monkeypatch)
        code, _, err = _secret_cli(
            monkeypatch, capsys, config, "ensure", "--user", "alice",
            "--service", "plugin_example", "--key", "token", "--value-stdin",
        )
        assert code == 1
        assert "unknown service 'plugin_example'" in err

    def test_istota_secret_ensure_still_refuses_mail_account_under_an_override(
        self, tmp_path, plugin_builder, monkeypatch, capsys,
    ):
        from istota.credentials import store

        config, database, ops = _install(tmp_path, monkeypatch)
        p = plugin_builder.installed()
        _operator_mail_account_override(ops, p.skill)
        code, _, err = _secret_cli(
            monkeypatch, capsys, config, "ensure", "--user", "alice",
            "--service", "mail_account", "--key", "1.imap_password", "--value-stdin",
        )
        assert code == 1
        assert "unknown service 'mail_account'" in err
        assert store.get_secret(database, "alice", "mail_account", "1.imap_password") is None

    def test_a_plan_may_provision_a_plugin_service(self, tmp_path, plugin_builder, monkeypatch):
        from istota.settings_plan.secrets import PROVISIONABLE_SERVICES, provisionable_keys

        config, _, _ = _install(tmp_path, monkeypatch)
        plugin_builder.installed()
        index = _index(tmp_path, None)
        assert provisionable_keys(index)["plugin_example"] == frozenset({"token"})
        assert set(provisionable_keys()) == PROVISIONABLE_SERVICES
        operations = _plan_secrets(config, [{"service": "plugin_example", "keys": ["token"]}],
                                   {"plugin_example": {"token": "plugin-token-placeholder"}})
        assert [(op.identity, op.action) for op in operations] == [("plugin_example.token", "create")]

    def test_a_plan_naming_mail_account_is_still_refused_under_an_override(
        self, tmp_path, plugin_builder, monkeypatch,
    ):
        from istota.settings_plan import engine

        config, _, ops = _install(tmp_path, monkeypatch)
        p = plugin_builder.installed()
        _operator_mail_account_override(ops, p.skill)
        with pytest.raises(engine.ExecutionError, match="secret_service_not_provisionable"):
            _plan_secrets(config, [{"service": "mail_account", "keys": ["1.imap_password"]}],
                          {"mail_account": {"1.imap_password": "placeholder"}})

    def test_a_plan_naming_a_plugin_service_before_the_plugin_is_refused(
        self, tmp_path, monkeypatch,
    ):
        from istota.settings_plan import engine

        config, _, _ = _install(tmp_path, monkeypatch)
        with pytest.raises(engine.ExecutionError, match="secret_service_not_provisionable"):
            _plan_secrets(config, [{"service": "plugin_example", "keys": ["token"]}],
                          {"plugin_example": {"token": "placeholder"}})


class TestTheSnapshotKeepsAPluginSecret:
    """Without the index a snapshot drops a plugin's declarations, and the apply
    that follows it would lose them."""

    def _stored(self, tmp_path, monkeypatch):
        from istota.credentials import store

        config, database, _ = _install(tmp_path, monkeypatch)
        store.set_secret(database, "alice", "plugin_example", "token", "stored-placeholder")
        store.set_secret(database, "alice", "karakeep", "api_key", "stored-placeholder")
        return config, database

    def _snapshot(self, database, **kwargs):
        from contextlib import closing

        from istota.lib.sqlite_util import connect_read_only
        from istota.settings_plan.secrets import snapshot_secrets

        warnings = []
        with closing(connect_read_only(database)) as conn:
            conn.execute("BEGIN")
            declarations = snapshot_secrets(conn, "alice", warnings, **kwargs)
        return declarations, warnings

    def test_with_the_index_the_declaration_is_kept_and_unwarned(
        self, tmp_path, plugin_builder, monkeypatch,
    ):
        _, database = self._stored(tmp_path, monkeypatch)
        plugin_builder.installed()
        declarations, warnings = self._snapshot(database, skill_index=_index(tmp_path, None))
        assert {"service": "plugin_example", "keys": ["token"]} in declarations
        assert warnings == []

    def test_without_the_index_it_is_excluded_as_before(self, tmp_path, plugin_builder, monkeypatch):
        _, database = self._stored(tmp_path, monkeypatch)
        plugin_builder.installed()
        declarations, warnings = self._snapshot(database)
        assert declarations == [{"service": "karakeep", "keys": ["api_key"]}]
        assert warnings and "non-provisionable" in warnings[0]

    def test_istota_snapshot_passes_the_index(self, tmp_path, plugin_builder, monkeypatch):
        from istota.settings_plan import snapshot

        config, _ = self._stored(tmp_path, monkeypatch)
        plugin_builder.installed()
        document, warnings = snapshot.gather(config, users=["alice"])
        assert {"service": "plugin_example", "keys": ["token"]} in document["users"]["alice"]["secrets"]
        assert not any("non-provisionable" in warning for warning in warnings)
