"""A skill plugin package on disk, for the loader and dispatch tests.

Two builders over one minimal package (spec: external-skill-packages):

- ``injected``: the package in a directory prepended to ``sys.path``, and an
  ``importlib.metadata.EntryPoint`` to hand ``load_skill_index`` through
  ``plugin_entry_points``. Such an entry point has no ``dist``.
- ``installed``: the same package plus a real ``.dist-info`` beside it, so
  ``importlib.metadata`` finds it with no injection. A subprocess sees it
  through ``PYTHONPATH=<site>``.
- ``wheel``: the installed tree zipped into
  ``example_plugin-0.1.0-py3-none-any.whl``, for the image, smoke and vm tiers.
  The one builder with fixed names, since it never touches this process.

**Every package name is unique** (``example_plugin_<8 hex>``, with the skill
name and variable prefix derived from it). ``find_spec`` answers from
``sys.modules`` once a module has been imported, so two tests sharing one
package name in different directories would see each other's spec, in an order
xdist decides. For the same reason the distribution name is unique too:
``importlib.metadata`` keeps only the first distribution of a normalized name.

Teardown removes each ``sys.path`` entry, pops each package from
``sys.modules``, invalidates the import caches and resets the loader's
discovery cache, so the next test in the worker sees no plugin.

The package's ``__init__`` appends a line to the file named by
``SENTINEL_ENV`` when it is imported, so a test can assert it was not. With
``setup_env`` given, it also defines a ``setup_env(ctx)`` hook returning that
dict.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.metadata
import json
import secrets
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SENTINEL_ENV = "EXAMPLE_PLUGIN_SENTINEL"

#: The names :meth:`PluginBuilder.wheel` builds under.
WHEEL_PACKAGE = "example_plugin"
WHEEL_SKILL = "example"
WHEEL_DIST = "example-plugin"
WHEEL_VERSION = "0.1.0"
WHEEL_NAME = f"{WHEEL_PACKAGE}-{WHEEL_VERSION}-py3-none-any.whl"

# zipfile refuses a timestamp before 1980; a fixed one makes the bytes repeatable.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: The bundled `__main__.py` template, code lines byte for byte.
MAIN_TEMPLATE = (
    '"""Allow running as `python -m <package>`."""\n'
    "\n"
    "from istota.skills._cli import run_skill_main\n"
    "\n"
    "from . import main\n"
    "\n"
    "run_skill_main(main)\n"
)

_INIT_TEMPLATE = '''"""Example skill plugin, written by tests/support/plugin_fixture.py."""

import argparse
import json
import os

_sentinel = os.environ.get({sentinel_env!r})
if _sentinel:
    with open(_sentinel, "a") as _f:
        _f.write("imported\\n")
{raise_line}
{setup_env_block}

def build_parser():
    parser = argparse.ArgumentParser(prog="istota-skill {skill}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "ping":
        print(json.dumps({{"status": "ok", "cred_fd": "ISTOTA_CRED_FD" in os.environ}}))
'''


@dataclass
class PluginPackage:
    """One built plugin: where it is and the names derived from it."""

    package: str
    skill: str
    site: Path
    token_var: str
    service: str = "plugin_example"
    dist_name: str = ""
    version: str = ""
    entry_point: importlib.metadata.EntryPoint | None = None

    @property
    def package_dir(self) -> Path:
        return self.site / self.package


def default_env(skill: str) -> list[dict]:
    """The one `from: secret` spec the fixture manifest declares."""
    return [{
        "var": f"PLUGIN_{skill.upper()}_TOKEN", "from": "secret",
        "service": "plugin_example", "key": "token", "sensitive": True,
    }]


def render_skill_md(frontmatter: dict, body: str = "# Example plugin\n\nAn example skill.\n") -> str:
    """Frontmatter in the loader's minimal YAML subset, then the body."""
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                lines.append(f"{key}: {json.dumps(value, separators=(',', ':'))}")
            else:
                lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


@dataclass
class PluginBuilder:
    """Builds plugin packages under ``root`` and undoes what it did to the process."""

    root: Path
    _paths: list[str] = field(default_factory=list)
    _packages: list[str] = field(default_factory=list)

    def _names(self, skill: str | None, package: str | None) -> tuple[str, str, str]:
        tag = secrets.token_hex(4)
        return (
            package or f"example_plugin_{tag}",
            skill or f"example_{tag}",
            tag,
        )

    def _write(
        self,
        site: Path,
        package: str,
        skill: str,
        *,
        layout: str,
        frontmatter: dict | None,
        extra: dict | None,
        env: list[dict] | None,
        skill_md: str | None,
        write_skill_md: bool,
        skill_toml: str | None,
        init_raises: bool,
        with_main: bool,
        setup_env: dict | None = None,
    ) -> None:
        site.mkdir(parents=True, exist_ok=True)
        if layout == "module":
            (site / f"{package}.py").write_text("VALUE = 1\n")
            return
        pkg_dir = site / package
        pkg_dir.mkdir()
        if layout == "package":
            raise_line = 'raise RuntimeError("example plugin refused to import")' if init_raises else ""
            setup_env_block = (
                f"\n\ndef setup_env(ctx):\n    return {setup_env!r}\n"
                if setup_env is not None else ""
            )
            (pkg_dir / "__init__.py").write_text(_INIT_TEMPLATE.format(
                sentinel_env=SENTINEL_ENV, raise_line=raise_line, skill=skill,
                setup_env_block=setup_env_block,
            ))
            if with_main:
                (pkg_dir / "__main__.py").write_text(MAIN_TEMPLATE)
        elif layout != "namespace":
            raise ValueError(f"unknown layout {layout!r}")
        if skill_toml is not None:
            (pkg_dir / "skill.toml").write_text(skill_toml)
        if not write_skill_md:
            return
        if skill_md is None:
            fm = dict(frontmatter) if frontmatter is not None else {
                "description": "Example plugin skill",
                "cli": True,
                "env": default_env(skill) if env is None else env,
            }
            if extra:
                fm.update(extra)
            skill_md = render_skill_md(fm)
        (pkg_dir / "skill.md").write_text(skill_md)

    def _activate(self, site: Path, package: str) -> None:
        sys.path.insert(0, str(site))
        self._paths.append(str(site))
        self._packages.append(package)
        importlib.invalidate_caches()
        _reset_loader_cache()

    def injected(
        self,
        *,
        skill: str | None = None,
        package: str | None = None,
        value: str | None = None,
        layout: str = "package",
        frontmatter: dict | None = None,
        extra: dict | None = None,
        env: list[dict] | None = None,
        skill_md: str | None = None,
        write_skill_md: bool = True,
        skill_toml: str | None = None,
        init_raises: bool = False,
        with_main: bool = True,
        setup_env: dict | None = None,
        write: bool = True,
    ) -> PluginPackage:
        """A package on ``sys.path`` and an entry point naming it, with no ``dist``.

        ``value`` overrides the entry point's value (a ``pkg:attr``, a dotted
        name); ``write=False`` writes nothing, for a module that is not there.
        """
        package, skill, tag = self._names(skill, package)
        site = self.root / f"injected_{tag}"
        if write:
            self._write(
                site, package, skill, layout=layout, frontmatter=frontmatter,
                extra=extra, env=env, skill_md=skill_md, write_skill_md=write_skill_md,
                skill_toml=skill_toml, init_raises=init_raises, with_main=with_main,
                setup_env=setup_env,
            )
        else:
            site.mkdir(parents=True, exist_ok=True)
        self._activate(site, package)
        ep = importlib.metadata.EntryPoint(skill, value if value is not None else package, "istota.skills")
        return PluginPackage(
            package=package, skill=skill, site=site,
            token_var=f"PLUGIN_{skill.upper()}_TOKEN", entry_point=ep,
        )

    def installed(
        self,
        *,
        skill: str | None = None,
        package: str | None = None,
        version: str = "0.1.0",
        frontmatter: dict | None = None,
        extra: dict | None = None,
        env: list[dict] | None = None,
        init_raises: bool = False,
        with_main: bool = True,
        setup_env: dict | None = None,
        activate: bool = True,
        dist_name: str | None = None,
    ) -> PluginPackage:
        """The package plus a ``.dist-info`` that ``importlib.metadata`` finds.

        ``activate=False`` leaves ``sys.path`` alone, for a subprocess that
        reaches the tree through ``PYTHONPATH``.
        """
        package, skill, tag = self._names(skill, package)
        dist_name = dist_name or f"example-plugin-{tag}"
        site = self.root / f"site_{tag}"
        self._write(
            site, package, skill, layout="package", frontmatter=frontmatter,
            extra=extra, env=env, skill_md=None, write_skill_md=True,
            skill_toml=None, init_raises=init_raises, with_main=with_main,
            setup_env=setup_env,
        )
        dist_info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n"
        )
        (dist_info / "entry_points.txt").write_text(f"[istota.skills]\n{skill} = {package}\n")
        (dist_info / "WHEEL").write_text(
            "Wheel-Version: 1.0\nGenerator: istota-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        files = sorted(
            p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()
        )
        record = [f"{name},," for name in files] + [f"{dist_info.name}/RECORD,,"]
        (dist_info / "RECORD").write_text("\n".join(record) + "\n")
        plugin = PluginPackage(
            package=package, skill=skill, site=site,
            token_var=f"PLUGIN_{skill.upper()}_TOKEN",
            dist_name=dist_name, version=version,
        )
        if activate:
            self._activate(site, package)
        return plugin

    def wheel(self, out_dir: Path) -> Path:
        """The installed tree as ``example_plugin-0.1.0-py3-none-any.whl``, for the artifact tiers.

        Fixed names rather than unique ones: a wheel's filename must match its
        distribution, and the tiers install it into a tree of its own, never
        into this process. Nothing is put on ``sys.path``.
        """
        plugin = self.installed(
            skill=WHEEL_SKILL, package=WHEEL_PACKAGE, dist_name=WHEEL_DIST,
            version=WHEEL_VERSION, activate=False,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / WHEEL_NAME
        dist_info = f"{WHEEL_PACKAGE}-{WHEEL_VERSION}.dist-info"
        record_name = f"{dist_info}/RECORD"
        files = sorted(
            p.relative_to(plugin.site).as_posix()
            for p in plugin.site.rglob("*")
            if p.is_file() and p.relative_to(plugin.site).as_posix() != record_name
        )
        record = []
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                data = (plugin.site / name).read_bytes()
                archive.writestr(zipfile.ZipInfo(name, _ZIP_EPOCH), data)
                record.append(f"{name},sha256={_record_digest(data)},{len(data)}")
            record.append(f"{record_name},,")
            archive.writestr(zipfile.ZipInfo(record_name, _ZIP_EPOCH), "\n".join(record) + "\n")
        return path

    def editable(self, *, src_layout: bool = False, direct_url: bool = True, **kwargs) -> PluginPackage:
        """An editable install: the ``.dist-info`` in one directory and the package
        in a project directory the distribution's ``direct_url.json`` names.

        ``src_layout`` puts the package under ``<project>/src/``;
        ``direct_url=False`` leaves the file out, so nothing says where the
        package belongs.
        """
        plugin = self.installed(**kwargs)
        project = self.root / f"project_{secrets.token_hex(4)}"
        source = project / "src" if src_layout else project
        source.mkdir(parents=True)
        (plugin.site / plugin.package).rename(source / plugin.package)
        if direct_url:
            dist_info = next(plugin.site.glob("*.dist-info"))
            (dist_info / "direct_url.json").write_text(
                json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})
            )
        self._activate(source, plugin.package)
        return plugin

    def shadow(self, package: str) -> Path:
        """Another package of the same name, earlier on ``sys.path``."""
        site = self.root / f"shadow_{secrets.token_hex(4)}"
        pkg_dir = site / package
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "skill.md").write_text(render_skill_md({"description": "shadow"}))
        self._activate(site, package)
        return site

    def cleanup(self) -> None:
        for path in self._paths:
            while path in sys.path:
                sys.path.remove(path)
        for package in self._packages:
            for name in list(sys.modules):
                if name == package or name.startswith(package + "."):
                    del sys.modules[name]
        self._paths.clear()
        self._packages.clear()
        importlib.invalidate_caches()
        _reset_loader_cache()


def _record_digest(data: bytes) -> str:
    """A RECORD hash: urlsafe base64 of the SHA-256, unpadded (PEP 376)."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _reset_loader_cache() -> None:
    from istota.skills._loader import _reset_plugin_cache

    _reset_plugin_cache()


def build_plugins(tmp_path: Path):
    """A :class:`PluginBuilder` whose packages are gone from the process afterwards.

    A generator for a test module's own fixture, ``yield from
    build_plugins(tmp_path)``, so no module imports a fixture by name.
    """
    builder = PluginBuilder(tmp_path / "plugins")
    try:
        yield builder
    finally:
        builder.cleanup()
