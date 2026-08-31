"""The extensionless-file crash in pytest-testmon, and the shim that closes it.

Reproduces the failure directly rather than through a pytest session: the
IndexError comes out of `SourceTree.get_file`, and a unit test over that one
call is both faster and honest about which line is at fault.
"""

import pytest

testmon_core = pytest.importorskip("testmon.testmon_core")

from .support import testmon_compat


PROGRAM = """\
#!/usr/bin/env python3
def main():
    return 1
"""


def _tree(tmp_path):
    return testmon_core.SourceTree(rootdir=str(tmp_path))


class TestTheExtensionlessFile:
    """`docker/devbox/scripts/istota-exec-serve` and its two neighbours carry no
    `.py` suffix on purpose — they are on the container's PATH and invoked by
    name — and `tests/test_devbox_exec_server.py` imports one in-process, so
    coverage traces it and testmon is handed a filename with no dot in it.
    """

    def test_get_file_returns_a_module(self, tmp_path):
        (tmp_path / "istota-exec-serve").write_text(PROGRAM)

        module = _tree(tmp_path).get_file("istota-exec-serve")

        assert module is not None
        assert module.source_code == PROGRAM

    def test_it_is_fingerprinted_exactly_as_the_same_file_with_a_suffix(self, tmp_path):
        """The shim reimplements four lines of upstream's `get_file` for the
        dotless branch. This is what says the copy still agrees with the
        original: same bytes, same file, one name with a suffix and one without.
        """
        (tmp_path / "istota-exec-serve").write_text(PROGRAM)
        (tmp_path / "istota_exec_serve.py").write_text(PROGRAM)
        tree = _tree(tmp_path)

        bare = tree.get_file("istota-exec-serve")
        suffixed = tree.get_file("istota_exec_serve.py")

        assert bare.fs_fsha == suffixed.fs_fsha
        assert bare.checksums == suffixed.checksums

    def test_a_missing_file_is_cached_as_none(self, tmp_path):
        assert _tree(tmp_path).get_file("istota-exec-serve") is None


class TestTheShimItself:
    def test_installing_twice_leaves_one_layer(self, tmp_path):
        """conftest installs it at import; a second call must not wrap the wrapper."""
        first = testmon_core.SourceTree.get_file

        testmon_compat.install()

        assert testmon_core.SourceTree.get_file is first

    def test_a_dotted_filename_still_goes_to_the_original(self, tmp_path):
        (tmp_path / "mod.py").write_text(PROGRAM)

        module = _tree(tmp_path).get_file("mod.py")

        assert module.ext == "py"
