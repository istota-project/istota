"""The two properties of `sandbox_plan` that the argv goldens cannot see.

`tests/test_sandbox_argv_golden.py` pins what the module *emits*, across a
thirty-case matrix, and that is the whole safety net for the mount plan's
contents. Two things it structurally cannot cover live here instead: which way
the imports point, and a log line that is the fail-closed signal for a bind
nothing else in the namespace provides.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from istota.sandbox_plan import (
    EXTRA_RO_BIND,
    Mount,
    MountPlan,
    SandboxProfile,
    render_bwrap_argv,
)


def _fresh_interpreter(body: str) -> set[str]:
    """The modules loaded after running ``body`` in a clean interpreter.

    A subprocess rather than `monkeypatch.delitem` on `sys.modules`, for the
    reason `tests/test_doctor.py::_run_in_fresh_interpreter` already spawns
    one: deleting a module while its importer stays cached makes the deletion
    inert, and under `-n auto` that is a flake rather than a curiosity.
    """
    code = (
        "import json, sys\n"
        f"{body}\n"
        "print(json.dumps(sorted(sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    # Not `check=True`: `CalledProcessError` does not render `stderr`, so an
    # ImportError — which is the exact failure this file exists to catch —
    # would surface as a bare non-zero exit with the cause discarded.
    assert out.returncode == 0, out.stderr
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


class TestTheImportDirection:
    """One way only, and getting it backwards is an import-time cycle.

    ``executor`` imports ``sandbox_plan`` at module scope, because it has to in
    order to re-export ``SandboxProfile`` for the five sites that import it
    from there. ``sandbox_plan`` therefore imports ``executor`` inside
    functions. The default suite cannot see a regression here: dozens of test
    modules import ``istota.executor`` before any sandbox test runs, so a
    module-scope ``from .executor import ...`` added to ``sandbox_plan`` would
    resolve from cache all suite long and fail only at a real entry point.
    """

    def test_the_plan_module_pulls_in_no_executor(self):
        loaded = _fresh_interpreter("import istota.sandbox_plan")
        assert "istota.executor" not in loaded, (
            "sandbox_plan reached executor at module scope. executor imports "
            "sandbox_plan at module scope to re-export SandboxProfile, so this "
            "is a cycle: move the import inside the function that needs it."
        )

    def test_the_probe_can_see_an_import(self):
        """The control, so the assertion above cannot pass on an empty set."""
        loaded = _fresh_interpreter("import istota.executor")
        assert "istota.executor" in loaded
        assert "istota.sandbox_plan" in loaded

    def test_the_re_export_is_the_same_object(self):
        """The five import sites name ``executor.SandboxProfile``.

        Identity rather than equality: ``SandboxProfile`` is a ``str`` Enum, so
        a second class with the same members would compare equal member for
        member while ``is`` comparisons — which is how every gate in the plan
        reads it — silently answered False.
        """
        from istota import executor

        assert executor.SandboxProfile is SandboxProfile


class TestTheAbsentExtraBindIsAnnounced:
    """A skipped ``extra_ro_binds`` entry has to say so.

    bwrap fails the whole namespace on a bind whose source is absent, so the
    render skips one rather than raising — one cleanup race would otherwise
    fail every task instead of one. That makes the log line the only signal,
    and its two callers lose different things by it: the OCR document sits
    inside a read-write bind, so a skipped entry leaves the *writable* copy in
    the namespace, while the task's control directory is bound by nothing else,
    so a skipped entry leaves the path absent and the CLI exits at
    ``--append-system-prompt-file``. The golden case
    ``extra_ro_binds_present_and_absent`` pins the argv; nothing pinned the
    message until now.
    """

    def _render(self, source: Path, caplog) -> list[str]:
        plan = MountPlan(
            mounts=(Mount(mode="ro", source=source, dest=None, reason=EXTRA_RO_BIND),),
            chdir=source.parent,
        )
        with caplog.at_level(logging.WARNING):
            return render_bwrap_argv(
                plan, ["true"], user_temp_dir=source.parent,
            )

    def test_an_absent_entry_is_skipped_and_logged(self, tmp_path, caplog):
        argv = self._render(tmp_path / "gone.pdf", caplog)

        assert "--ro-bind" not in argv
        assert "extra read-only bind" in caplog.text
        assert "gone.pdf" in caplog.text
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_a_present_entry_is_bound_and_silent(self, tmp_path, caplog):
        """The control. Without it the assertions above hold for a render that
        logged the warning unconditionally, or for one that never bound
        anything at all."""
        doc = tmp_path / "doc.pdf"
        doc.write_text("doc\n")

        argv = self._render(doc, caplog)

        assert argv[:4] == ["bwrap", "--ro-bind", str(doc), str(doc)]
        assert "extra read-only bind" not in caplog.text
