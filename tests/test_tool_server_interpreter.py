"""Which interpreter the tool server's argv names, and where it has to live.

The tool server is the one thing istota runs *inside* the namespace with its
own interpreter, so its argv[0] has to be a path that exists in there. The
namespace has exactly one Python in it besides ``/usr``: the venv
``build_mount_plan`` ro-binds, at the path ``executor._source_and_venv_paths``
returns.

So the invariant is a relationship rather than a value — **the interpreter in
the argv is inside the venv that gets bound** — and asserting it against a
literal would have missed the bug it was written for. ISSUE-389 put an
interpreter in the namespace for the first time; 88c36d54 then moved the bind
onto ``sys.prefix`` while the argv kept the raw ``sys.executable``, and those
two are the same string only where no symlink stands between them. On a
deployment where the venv is reached through one (``.venv -> istota/.venv``,
which is how the Ansible role lays it out) the bind landed on the resolved
path, the argv named the unresolved one, and every native task died at the
handshake with ``exit 127: env: '…/python': No such file or directory``.

``sys.executable`` is re-rooted rather than resolved, which is the half that is
easy to get wrong in the other direction: ``.venv/bin/python`` is itself a
symlink to the system interpreter, so ``resolve()`` hands back
``/usr/bin/python3`` — a real path, present in the namespace, and no longer in
a venv, so the server starts and cannot import istota.
"""

import sys
import tempfile
from pathlib import Path

import pytest

from istota import db, executor
from istota.config import Config
from istota.sandbox_plan import SandboxProfile, build_mount_plan
from istota.session.tools.remote import server_command

#: The interpreter this process really started under, read at import time. The
#: tests below monkeypatch `sys.executable`, so comparing against it *inside* a
#: patched test is a fixed-point check that holds whatever the function did.
REAL_EXECUTABLE = sys.executable


@pytest.fixture
def venv_tree(tmp_path):
    """A venv at a real path, plus a symlink standing in front of it."""
    real = tmp_path / "istota" / ".venv"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python").write_text("#!/bin/sh\n")
    link = tmp_path / ".venv"
    link.symlink_to(real)
    return real, link


def _as_venv(monkeypatch, prefix, executable):
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", str(executable))


class TestTheInterpreterIsInsideTheBoundVenv:
    def test_a_symlinked_venv_root_does_not_split_the_argv_from_the_bind(
        self, monkeypatch, venv_tree
    ):
        """The production shape: `/srv/app/{ns}/.venv -> …/istota/.venv`."""
        real, link = venv_tree
        _as_venv(monkeypatch, link, link / "bin" / "python")

        _, bound = executor._source_and_venv_paths()
        interpreter = server_command()[0]

        assert interpreter.startswith(f"{bound}/"), (
            f"{interpreter} is not inside the venv bound at {bound}"
        )
        assert interpreter == str(real / "bin" / "python")

    def test_an_unsymlinked_venv_is_left_exactly_as_it_was(
        self, monkeypatch, venv_tree
    ):
        """The developer and CI shape, and the regression guard for it."""
        real, _ = venv_tree
        _as_venv(monkeypatch, real, real / "bin" / "python")

        assert server_command()[0] == str(real / "bin" / "python")

    def test_the_interpreter_is_re_rooted_and_never_resolved(
        self, monkeypatch, tmp_path
    ):
        """`bin/python` is a symlink to the system interpreter in every venv
        uv or `python -m venv` builds. Following it leaves the venv."""
        real = tmp_path / "istota" / ".venv"
        (real / "bin").mkdir(parents=True)
        system = tmp_path / "usr" / "bin" / "python3"
        system.parent.mkdir(parents=True)
        system.write_text("#!/bin/sh\n")
        (real / "bin" / "python").symlink_to(system)
        link = tmp_path / ".venv"
        link.symlink_to(real)
        _as_venv(monkeypatch, link, link / "bin" / "python")

        assert server_command()[0] == str(real / "bin" / "python")

    def test_a_distro_python_keeps_the_interpreter_it_was_started_with(
        self, monkeypatch
    ):
        """A non-venv interpreter under /usr, which is bound unconditionally
        and as written, so there is nothing to rewrite."""
        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

        assert server_command()[0] == "/usr/bin/python3"

    def test_a_symlinked_standalone_python_is_re_rooted_too(
        self, monkeypatch, tmp_path
    ):
        """The non-venv branch has the same split, and it acquired it from the
        fix rather than having it all along.

        A pyenv- or uv-managed interpreter run outside a venv has
        `sys.prefix == sys.base_prefix`, so the venv branch never sees it — and
        once `python_base_prefix_binds` started binding it *resolved*, returning
        an unresolved `sys.executable` here put the argv and the bind back on
        two different paths. `/usr` cannot catch this: `resolve()` is a no-op
        there, so the only non-venv case that existed passed either way.
        """
        real = tmp_path / "opt" / "pyenv" / "versions" / "3.12"
        (real / "bin").mkdir(parents=True)
        (real / "bin" / "python").write_text("#!/bin/sh\n")
        link = tmp_path / "home" / ".pyenv-link"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        monkeypatch.setattr(sys, "prefix", str(link))
        monkeypatch.setattr(sys, "base_prefix", str(link))
        monkeypatch.setattr(sys, "executable", str(link / "bin" / "python"))

        bound = executor.python_base_prefix_binds()
        interpreter = server_command()[0]

        assert bound[0] == real.resolve()
        assert interpreter == str(real.resolve() / "bin" / "python"), (
            f"{interpreter} is not under the path the sandbox binds ({bound[0]})"
        )

    def test_both_spellings_are_bound_when_a_symlink_separates_them(
        self, monkeypatch, tmp_path
    ):
        """A symlink stores the string it was written with, and the kernel
        walks that string inside the namespace — so the unresolved spelling has
        to be a path in there too, or a `bin/python` naming it dangles."""
        real = tmp_path / "opt" / "py"
        (real / "bin").mkdir(parents=True)
        link = tmp_path / "linked-py"
        link.symlink_to(real)
        monkeypatch.setattr(sys, "prefix", str(link))
        monkeypatch.setattr(sys, "base_prefix", str(link))

        assert executor.python_base_prefix_binds() == [real.resolve(), link]

    def test_an_unsymlinked_standalone_python_is_bound_once(
        self, monkeypatch, tmp_path
    ):
        real = tmp_path / "opt" / "py"
        (real / "bin").mkdir(parents=True)
        monkeypatch.setattr(sys, "prefix", str(real))
        monkeypatch.setattr(sys, "base_prefix", str(real))

        assert executor.python_base_prefix_binds() == [real.resolve()]


class TestTheBaseInterpreterBindRefusesRatherThanWidening:
    """The containment test is an ancestor check, and `/` passes neither arm.

    Without a refusal a `sys.base_prefix` of `/` would ro-bind the entire host
    filesystem into every namespace — the shape `user_scope` and
    `sandbox_cache_sweeper` both answer with `None` rather than a fallback,
    because the fallback *is* the exposure. Barely reachable; two lines.
    """

    def test_the_filesystem_root_is_never_bound(self, monkeypatch):
        monkeypatch.setattr(sys, "prefix", "/")
        monkeypatch.setattr(sys, "base_prefix", "/")

        assert executor.python_base_prefix_binds() == []

    def test_something_that_is_not_a_python_installation_is_not_bound(
        self, monkeypatch, tmp_path
    ):
        """No `bin/` under it, so binding it would carry in whatever it names
        rather than an interpreter."""
        odd = tmp_path / "not-python"
        odd.mkdir()
        monkeypatch.setattr(sys, "prefix", str(odd))
        monkeypatch.setattr(sys, "base_prefix", str(odd))

        assert executor.python_base_prefix_binds() == []

    def test_a_distro_python_needs_no_bind_of_its_own(self, monkeypatch):
        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")

        assert executor.python_base_prefix_binds() == []

    def test_an_interpreter_outside_the_prefix_is_left_alone(self, monkeypatch):
        """Nothing to re-root against, so the answer is the input rather than
        a path assembled out of two unrelated trees."""
        monkeypatch.setattr(sys, "prefix", "/opt/venv")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

        assert server_command()[0] == "/usr/bin/python3"


class TestTheRestOfTheArgvIsUnchanged:
    def test_it_still_runs_the_tool_server_module(self):
        assert server_command()[1:] == ["-m", "istota.tool_server"]


class TestTheInterpreterIsSomethingTheMountPlanActuallyBinds:
    """The oracle is the mount plan, not the helper the product computes from.

    Every assertion above compares ``server_command()`` against
    ``_source_and_venv_paths()`` — which is what ``sandbox_interpreter`` builds
    its answer *from*, so the two agree by construction for any value that
    function returns, including a wrong one. That is the shape 88c36d54 already
    shipped once: it moved the bind and left the argv behind, and a test written
    this way would have passed either side of the bug.

    These two ask ``build_mount_plan`` what it emits and compare the argv to
    that, unpatched, against whatever venv this host really has.
    """

    def _ro_sources(self) -> list[Path]:
        task = db.Task(
            id=1,
            prompt="t",
            user_id="alice",
            source_type="talk",
            status="running",
            conversation_token="c1",
        )
        with tempfile.TemporaryDirectory() as td:
            plan = build_mount_plan(
                Config(), task, True, [], Path(td), profile=SandboxProfile.NATIVE
            )
        return [
            m.source
            for m in plan.mounts
            if m.mode == "ro" and m.source is not None
        ]

    @staticmethod
    def _covered_by(path: Path, roots: list[Path]) -> list[Path]:
        return [r for r in roots if r == path or r in path.parents]

    def test_the_argv_names_a_path_inside_a_bind_the_plan_emits(self):
        roots = self._ro_sources()
        exe = Path(server_command()[0])
        assert self._covered_by(exe, roots), (
            f"{exe} is inside none of the read-only binds the NATIVE plan "
            f"emits, so it does not exist in the namespace: {roots}"
        )

    def test_the_interpreter_the_venv_links_to_is_bound_as_well(self):
        """A bind carries a symlink, not its target.

        ``{venv}/bin/python`` points at the interpreter the venv was built from.
        Where that is the distro's it is under ``/usr``; where it is a uv- or
        pyenv-managed standalone build it is under ``$HOME`` and needs
        ``python_base_prefix_bind``. Without it the argv resolves to a dangling
        link inside the namespace and the exec fails with the same 127 — the
        same bug one level down, and the shape of the machine this was written
        on, whose venv links into ``~/.local/share/uv``.
        """
        roots = self._ro_sources()
        target = Path(server_command()[0]).resolve()
        assert self._covered_by(target, roots), (
            f"{target} — what the venv's interpreter symlink points at — is "
            f"inside none of the read-only binds the NATIVE plan emits: {roots}"
        )
