"""Make pytest-testmon tolerate a source file whose name carries no extension.

testmon derives a traced file's extension with `filename.rsplit(".", 1)[1]`
(`testmon_core.SourceTree.get_file`), which raises `IndexError` on a name with
no dot in it. That call sits under the `pytest_runtest_logreport` hook, so the
IndexError does not fail a test — it aborts the whole session with an
INTERNALERROR, *after* every test has already passed, and pytest exits 3.

This repository ships Python programs with no `.py` suffix on purpose: the
three under `docker/devbox/scripts/` are on the container's PATH and invoked by
name. `tests/test_devbox_exec_server.py` imports one of them in-process, so
coverage traces it under its real, dotless name and testmon takes down any
`--testmon` run that collects that file. `scripts/qt` used to work around it by
excluding the file, which left it untraceable by testmon and did nothing for
the run that builds `.testmondata` in a fresh worktree — that run collects the
whole suite and died every time.

There is no configuration for this: testmon builds its `Coverage` with
`config_file=False`, and the line that would honour a coverage `omit` is
commented out upstream. So the fix is a shim, installed from `tests/conftest.py`
at import.

Upstream: pytest-testmon 2.2.0, `testmon/testmon_core.py`.
"""

import os

#: Set once `install()` has replaced the method, so a second call is a no-op.
_installed = False


def install() -> bool:
    """Patch `SourceTree.get_file`. Returns whether testmon is present at all."""
    global _installed

    try:
        from testmon import testmon_core
        from testmon.process_code import Module, get_source_sha
    except ImportError:
        return False

    if _installed:
        return True

    original = testmon_core.SourceTree.get_file

    def get_file(self, filename):
        # Everything with a dot in it is upstream's business, unchanged.
        if "." in os.path.basename(filename):
            return original(self, filename)

        if filename in self.cache:
            return self.cache[filename]

        code, fsha = get_source_sha(directory=self.rootdir, filename=filename)
        if not fsha:
            self.cache[filename] = None
            return None

        try:
            mtime = os.path.getmtime(os.path.join(self.rootdir, filename))
        except FileNotFoundError:
            self.cache[filename] = None
            return None

        # `ext` decides whether testmon fingerprints the file block by block or
        # as one lump. Anything coverage traced was executed by CPython, so "py"
        # is right — and where it is not, upstream already swallows the
        # SyntaxError and carries on with no blocks.
        self.cache[filename] = Module(
            source_code=code,
            mtime=mtime,
            ext="py",
            fs_fsha=fsha,
            filename=filename,
            rootdir=self.rootdir,
        )
        return self.cache[filename]

    testmon_core.SourceTree.get_file = get_file
    _installed = True
    return True
