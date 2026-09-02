"""The OCR namespace, executed rather than asserted about (ISSUE-397).

`tests/test_brain_request_confinement.py` patches `_bwrap_available` and reads
the argv `build_daemon_sandbox`'s closure produces. That is the strongest thing
a darwin host can say and it is a claim about a command line: the plan can be
right and the namespace still wrong. ISSUE-395's resolution note held this
change back for exactly that reason — "the document lives under `uploads_dir`,
a wrap that fails to bind it stops extraction working at all" — so the question
this file answers is the one that was open: does an OCR call inside the wrap
still reach its document, and is everything the wrap is for actually gone.

**Two properties, and the second is what makes the first mean anything.** A
test that only asserts the document is readable passes on a sandbox that
mounted nothing at all, which is the shape this repo has been caught by four
times (`.claude/rules/testbed.md`). So each probe pairs the document read with
a database read that must fail and a write that must be refused — answers only
a namespace can produce.

Run with `scripts/test-linux.sh`. Carries the `linux` marker.
"""

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import _bwrap_available, build_daemon_sandbox

from .test_sandbox_real import _unavailable

pytestmark = pytest.mark.linux

#: Distinctive enough that finding it in stdout cannot be a coincidence.
DOCUMENT_SENTINEL = "sentinel-lab-report-body"


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    """Same gate as `test_sandbox_real.py`, restated because a fixture is not
    inherited across modules."""
    if sys.platform != "linux":
        _unavailable("needs a real Linux kernel")
    if not _bwrap_available():
        _unavailable("needs a bubblewrap that can create namespaces")


def _q(path):
    return shlex.quote(str(path))


@pytest.fixture
def layout(tmp_path, make_config):
    """A deployment shaped like the real one: databases beside a bound root.

    `sandbox_ro_paths` carries the directory holding the databases, which is
    what makes the "the DB is unreadable" half non-vacuous — without the mask
    the bind would put the files in the namespace.
    """
    app = tmp_path / "app"
    db_dir = app / "data"
    module_dir = app / "moduledbs"
    db_dir.mkdir(parents=True)
    module_dir.mkdir(parents=True)
    (db_dir / "istota.db").write_text("framework-db-contents")
    (module_dir / "alice").mkdir()
    (module_dir / "alice" / "health.db").write_text("module-db-contents")

    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Users" / "bob").mkdir(parents=True)
    (mount / "Users" / "bob" / "secret.txt").write_text("bob's private file")

    return make_config(
        db_path=db_dir / "istota.db",
        module_data_dir=module_dir,
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=True, sandbox_ro_paths=[str(app)]),
    )


@pytest.fixture
def panel_upload(layout):
    """A bloodwork panel's upload, where `resolve_for_user` really puts one."""
    doc = (
        Path(layout.nextcloud_mount_path)
        / "Users" / "alice" / "Istota" / "health" / "uploads" / "7" / "original.txt"
    )
    doc.parent.mkdir(parents=True)
    doc.write_text(DOCUMENT_SENTINEL)
    return doc.resolve()


def run_in_wrap(script, sandbox):
    """Run `sh -c script` inside the wrap the OCR request carries.

    Fails rather than returning when the closure declined to build a bwrap
    command: `build_bwrap_cmd` returns its argument unchanged where the sandbox
    is unavailable, so a probe that quietly ran on the host would satisfy every
    positive assertion below and none of the negative ones would mean anything.
    """
    assert sandbox.wrap is not None, "no wrap was built"
    cmd = sandbox.wrap(["/bin/sh", "-c", script])
    assert cmd[0] == "bwrap", "sandbox unavailable — probe would have run unsandboxed"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


class TestTheOcrNamespace:
    def test_the_document_is_readable_and_the_databases_are_not(
        self, layout, panel_upload
    ):
        """The two halves together, because neither is worth much alone.

        Readable-document alone passes on an unsandboxed run. Unreadable-DB
        alone passes on a namespace so empty that extraction could never work,
        which is the outage ISSUE-395 held this change back over.
        """
        sandbox = build_daemon_sandbox(
            layout, "alice", extra_ro_binds=[panel_upload]
        )
        result = run_in_wrap(
            f'cat {_q(panel_upload)} 2>/dev/null || echo DOC_UNREADABLE; '
            f'cat {_q(layout.db_path)} 2>/dev/null && echo DB_READ_OK || echo DB_READ_FAIL',
            sandbox,
        )

        assert DOCUMENT_SENTINEL in result.stdout, result.stderr
        assert "DOC_UNREADABLE" not in result.stdout, result.stdout
        assert "DB_READ_FAIL" in result.stdout, result.stdout
        assert "framework-db-contents" not in result.stdout, result.stdout

    def test_the_document_is_read_only(self, layout, panel_upload):
        """`extra_ro_binds` is emitted after the read-write mount that holds it.

        The uploads directory arrives read-write through the
        `{mount}/Users/{user_id}` bind, so read-only here is a fact about argv
        order rather than about the flag being present — which is why
        `build_bwrap_cmd` emits this block last of the binds. The sibling write
        is the positive control: it must succeed, or "cannot write the
        document" would pass on a namespace where nothing is writable.
        """
        sandbox = build_daemon_sandbox(
            layout, "alice", extra_ro_binds=[panel_upload]
        )
        sibling = panel_upload.parent / "scratch.txt"
        result = run_in_wrap(
            f'echo tampered > {_q(panel_upload)} 2>/dev/null '
            f'&& echo DOC_WRITE_OK || echo DOC_WRITE_FAIL; '
            f'echo ok > {_q(sibling)} 2>/dev/null '
            f'&& echo SIBLING_WRITE_OK || echo SIBLING_WRITE_FAIL',
            sandbox,
        )

        assert "DOC_WRITE_FAIL" in result.stdout, result.stdout
        assert "SIBLING_WRITE_OK" in result.stdout, result.stdout
        assert panel_upload.read_text() == DOCUMENT_SENTINEL

    def test_another_users_files_are_not_in_the_namespace(
        self, layout, panel_upload
    ):
        """The per-user scoping, on the path that builds its own `db.Task`.

        `build_daemon_sandbox` synthesises a task to key the per-user binds on,
        so a wrong `user_id` there would widen the namespace silently.
        """
        sandbox = build_daemon_sandbox(
            layout, "alice", extra_ro_binds=[panel_upload]
        )
        other = Path(layout.nextcloud_mount_path) / "Users" / "bob" / "secret.txt"
        result = run_in_wrap(
            f'cat {_q(other)} 2>/dev/null && echo OTHER_READ_OK || echo OTHER_READ_FAIL',
            sandbox,
        )

        assert "OTHER_READ_FAIL" in result.stdout, result.stdout
        assert "bob's private file" not in result.stdout, result.stdout

    def test_the_work_dir_is_the_cwd_and_is_writable(self, layout, panel_upload):
        """`work_dir` and the wrap name one directory.

        The request puts `work_dir` on `cwd` and bwrap chdirs into it, so a
        disagreement between the two is a call that starts in a directory that
        is not there.
        """
        sandbox = build_daemon_sandbox(
            layout, "alice", extra_ro_binds=[panel_upload]
        )
        result = run_in_wrap("pwd; touch ./probe && echo CWD_WRITE_OK", sandbox)

        assert str(sandbox.work_dir) in result.stdout, result.stdout
        assert "CWD_WRITE_OK" in result.stdout, result.stdout

    def test_a_document_outside_every_standard_bind_still_arrives(
        self, layout, tmp_path
    ):
        """The encounter and immunization routes' case, and `_debug_main`'s.

        Neither hands over a file under `{mount}/Users/{user_id}`, so if the
        document were not bound by name those two extractors would return
        nothing at all on a sandboxed deployment — silently, since a model that
        cannot open the file answers with an empty extraction rather than an
        error.
        """
        elsewhere = tmp_path / "outside" / "visit-summary.txt"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(DOCUMENT_SENTINEL)
        elsewhere = elsewhere.resolve()

        sandbox = build_daemon_sandbox(layout, "alice", extra_ro_binds=[elsewhere])
        result = run_in_wrap(
            f'cat {_q(elsewhere)} 2>/dev/null || echo DOC_UNREADABLE', sandbox
        )

        assert DOCUMENT_SENTINEL in result.stdout, result.stderr
        assert "DOC_UNREADABLE" not in result.stdout, result.stdout

    def test_without_the_bind_the_document_is_gone(self, layout, tmp_path):
        """The in-session control for the case above.

        `extra_ro_binds` is what carries a document outside the standard binds,
        so the same probe with an empty list must fail. Without this, the test
        above would pass on a namespace that bound the whole filesystem.
        """
        elsewhere = tmp_path / "outside" / "visit-summary.txt"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(DOCUMENT_SENTINEL)
        elsewhere = elsewhere.resolve()

        sandbox = build_daemon_sandbox(layout, "alice")
        result = run_in_wrap(
            f'cat {_q(elsewhere)} 2>/dev/null || echo DOC_UNREADABLE', sandbox
        )

        assert "DOC_UNREADABLE" in result.stdout, result.stdout
        assert DOCUMENT_SENTINEL not in result.stdout, result.stdout


class TestTheRefusal:
    def test_an_unscoped_user_builds_no_wrap(self, layout):
        """And says it refused, rather than handing back a usable-looking one."""
        sandbox = build_daemon_sandbox(layout, "")

        assert sandbox.wrap is None
        assert sandbox.refused

    def test_a_task_row_is_never_written(self, layout):
        """The synthetic `db.Task(id=0, …)` is a value, not a row.

        `build_bwrap_cmd` wants a task to key the per-user binds on and there
        is none here. Nothing should reach the tasks table because of that.
        """
        Path(layout.db_path).unlink()
        db.init_db(layout.db_path)

        build_daemon_sandbox(layout, "alice")

        with db.get_db(layout.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 0
