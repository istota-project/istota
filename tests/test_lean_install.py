"""The suite runs on an install without the heavy ML extras, and stays that way.

`memory-search` (torch, sentence-transformers) and `whisper` (faster-whisper,
av, onnxruntime) are together about 750 MB of wheels. Nothing needs them to
collect: every heavy import in `src/` is inside a function, deliberately, so the
whole suite bar one test runs on an install that omits them. That matters
because the venv is per-worktree and per-container, so the difference is paid
again on every checkout.

Two failure modes keep that property from holding on its own, and both are
silent on a developer host where the extras happen to be installed:

  * a test importing torch or faster-whisper at module scope. Collection fails
    before any marker applies, so the `ml` marker cannot rescue it, and the
    result is an error in an unrelated file rather than a missing package;
  * a test dependency reaching the suite only as somebody else's transitive.
    `jinja2` used to arrive via mkdocs and torch, `psutil` via the `whisper`
    extra — so a lean install reported eight collection errors and two failures
    that read as a code regression. Both now sit in the `dev` group.

The same shape as `tests/test_image_tier.py` and `tests/test_linux_runner.py`,
which guard the tiers either side of this one.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"

# Top-level module names the two heavy extras bring in. A test importing any of
# these at module scope breaks collection on a lean install.
HEAVY_MODULES = frozenset(
    {
        "torch",
        "sentence_transformers",
        "transformers",
        "sqlite_vec",
        "faster_whisper",
        "onnxruntime",
        "av",
    }
)

# Every test module, enumerated at import so the checks below cover files added
# after they were written. Guarded by `test_the_enumeration_found_the_suite`,
# because an empty list would make the sweep collapse into a green no-op.
_TEST_MODULES = sorted(
    p for p in TESTS_ROOT.rglob("*.py") if "__pycache__" not in p.parts
)


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def _ini() -> dict:
    return _pyproject()["tool"]["pytest"]["ini_options"]


def _dev_group() -> list[str]:
    return _pyproject()["dependency-groups"]["dev"]


def _requirement_names(requirements: list[str]) -> set[str]:
    """Distribution names from a requirements list, normalised to import form."""
    names = set()
    for req in requirements:
        name = re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0].strip()
        if name:
            names.add(name.replace("-", "_").lower())
    return names


def _module_scope_imports(path: Path) -> set[str]:
    """Top-level package names imported at module scope by `path`.

    Module scope only: an import inside a function or a `try` body that the
    module tolerates failing is not what breaks collection. `ast` rather than a
    regex so a name in a docstring or a comment cannot register as an import.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    return imported


class TestTheEnumerationIsNotEmpty:
    def test_the_enumeration_found_the_suite(self):
        # Every sweep below iterates this list. A bad glob would make them all
        # pass while checking nothing.
        assert len(_TEST_MODULES) > 100, (
            f"only found {len(_TEST_MODULES)} test modules under {TESTS_ROOT}"
        )


class TestTheMarkerIsRegisteredAndOffByDefault:
    def test_ml_is_a_registered_marker(self):
        # An unregistered marker is a warning, not an error, so a typo would
        # deselect nothing and make the heavy extras required again.
        assert any(m.startswith("ml:") for m in _ini()["markers"])

    def test_addopts_deselects_ml(self):
        match = re.search(r"-m '([^']+)'", _ini()["addopts"])
        assert match, f"could not find a -m expression in addopts: {_ini()['addopts']!r}"
        assert re.search(r"\bnot ml\b", match.group(1))

    def test_the_marker_names_what_it_needs(self):
        # The point of the description is that a red `-m ml` run tells you which
        # extra to install, rather than leaving you to read the traceback.
        description = next(m for m in _ini()["markers"] if m.startswith("ml:"))
        assert "memory-search" in description
        assert "whisper" in description


class TestNoTestImportsAHeavyPackageAtModuleScope:
    """The property the `ml` marker cannot enforce.

    A marker is applied during collection; a module-scope import fails *at*
    collection. So the marker deselects the test and the import error is
    reported anyway.
    """

    def test_no_module_scope_heavy_imports(self):
        offenders = {}
        for path in _TEST_MODULES:
            heavy = _module_scope_imports(path) & HEAVY_MODULES
            if heavy:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = sorted(heavy)

        assert not offenders, (
            "these test modules import a heavy-extra package at module scope, "
            "which breaks collection on an install without it. Move the import "
            "into the test body and mark the test `ml`:\n"
            + "\n".join(f"  {p}: {', '.join(mods)}" for p, mods in sorted(offenders.items()))
        )


class TestTheTestOnlyDependenciesAreDeclared:
    """`jinja2` and `psutil` are used by tests and by nothing that installs them.

    Checked in both directions on purpose. A one-way check that they appear in
    the dev group would keep passing after the last user was deleted; a one-way
    check that something imports them would keep passing while they arrived as
    somebody else's transitive, which is the state this file exists to end.
    """

    def _importers(self, module: str) -> list[str]:
        return sorted(
            p.relative_to(REPO_ROOT).as_posix()
            for p in _TEST_MODULES
            if module in _module_scope_imports(p)
        )

    def test_jinja2_is_in_the_dev_group(self):
        assert "jinja2" in _requirement_names(_dev_group())

    def test_jinja2_is_actually_used(self):
        assert self._importers("jinja2"), (
            "nothing imports jinja2 any more — drop it from the dev group"
        )

    def test_psutil_is_in_the_dev_group(self):
        assert "psutil" in _requirement_names(_dev_group())

    def test_psutil_is_actually_used(self):
        assert self._importers("psutil"), (
            "nothing imports psutil any more — drop it from the dev group"
        )


class TestTheTestExtraIsAllMinusTheHeavyOnes:
    """`test` is a hand-written copy of `all`, so it drifts.

    There is no way to subtract an extra, so the two lists are maintained
    separately and a new module extra added to `all` will not appear in `test`.
    The symptom is a test importing a package the lean install does not have,
    which surfaces on whichever machine syncs `test` rather than on the one that
    made the change.
    """

    HEAVY_EXTRAS = frozenset({"memory-search", "whisper"})

    def _extras(self) -> dict[str, list[str]]:
        return _pyproject()["project"]["optional-dependencies"]

    def _composed(self, extra: str) -> set[str]:
        """The `istota[x]` self-references in a composite extra."""
        return set(re.findall(r"istota\[([\w-]+)\]", " ".join(self._extras()[extra])))

    def test_test_is_exactly_all_minus_the_heavy_extras(self):
        assert self._composed("test") == self._composed("all") - self.HEAVY_EXTRAS

    def test_all_still_contains_the_heavy_extras(self):
        # The subtraction above is vacuous if `all` stops carrying them, and a
        # deployment install would silently lose memory search and transcription.
        assert self.HEAVY_EXTRAS <= self._composed("all")

    def test_test_composes_rather_than_listing_packages(self):
        # A raw package in here is a package that has to be kept in step with
        # the extra it was copied from.
        assert all(req.startswith("istota[") for req in self._extras()["test"])


class TestTheLinuxRunnerInstallsTheDevGroupAndTheTestExtra:
    """The runner image is where a lean install is actually exercised.

    It used to carry `--extra docs` purely for jinja2 and `--extra whisper`
    purely for psutil; both are now in the dev group, and re-adding either extra
    to fix an import error would hide the declaration bug again.
    """

    def _dockerfile_sync(self) -> str:
        body = (REPO_ROOT / "docker" / "test" / "Dockerfile").read_text()
        match = re.search(r"^RUN uv sync .*?(?=\n\n)", body, re.MULTILINE | re.DOTALL)
        assert match, "could not find the `RUN uv sync` block in docker/test/Dockerfile"
        return match.group(0)

    def test_it_installs_the_dev_group(self):
        # Without this the runner has no jinja2 and no psutil, and reports the
        # collection errors this change exists to remove.
        assert "--group dev" in self._dockerfile_sync()

    def test_it_installs_the_test_extra(self):
        assert "--extra test" in self._dockerfile_sync()

    def test_it_reaches_for_no_extra_beyond_test(self):
        # `--extra docs` (jinja2), `--extra whisper` (psutil) and
        # `--extra memory-search` (a heavy import someone moved to module scope)
        # are the three that would plausibly get added back.
        extras = set(re.findall(r"--extra ([\w-]+)", self._dockerfile_sync()))
        assert extras == {"test"}

    def test_the_setup_script_installs_the_same_thing(self):
        # A fresh clone gets its venv here, and a bare `uv sync` is what
        # produced the several-hundred-error state in the first place.
        body = (REPO_ROOT / "scripts" / "setup.sh").read_text()
        assert re.search(r"^uv sync --extra test\b", body, re.MULTILINE), (
            "scripts/setup.sh no longer installs the test extra"
        )
