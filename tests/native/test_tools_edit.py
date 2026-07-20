"""Stage 1 — make_edit_tool through the argument shim + fuzzy/multi engine."""

import json

import pytest

from istota.session.tools import ToolEnv, make_edit_tool

pytestmark = pytest.mark.asyncio


def _env(tmp_path):
    return ToolEnv(cwd=tmp_path)


async def _run(tool, args):
    prepared = tool.prepare_arguments(args) if tool.prepare_arguments else args
    return await tool.execute("c1", prepared, None, None)


def _text(result):
    return result.content[0].text


class TestLegacySingleEdit:
    async def test_replaces_unique(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("alpha beta gamma")
        r = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "old_string": "beta", "new_string": "BETA"})
        assert f.read_text() == "alpha BETA gamma"
        assert "Edited" in _text(r)
        assert "1 block" in _text(r)

    async def test_replace_all_exact(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x x x")
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "x", "new_string": "y", "replace_all": True},
        )
        assert f.read_text() == "y y y"
        assert "3 occurrences" in _text(r)

    async def test_replace_all_coerced_from_string(self, tmp_path):
        # The shim coerces "true" → True (bypassing the generic coercion layer).
        f = tmp_path / "a.py"
        f.write_text("x x x")
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "x", "new_string": "y", "replace_all": "true"},
        )
        assert f.read_text() == "y y y"

    async def test_fuzzy_single_edit(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("def f():   \n    return 1\n")  # trailing whitespace
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "def f():\n    return 1", "new_string": "def f():\n    return 2"},
        )
        assert "return 2" in f.read_text()
        assert "Edited" in _text(r)


class TestEditsArray:
    async def test_multi_disjoint(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("a = 1\nb = 2\nc = 3\n")
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "a = 1", "new_string": "a = 10"},
                    {"old_string": "c = 3", "new_string": "c = 30"},
                ],
            },
        )
        assert f.read_text() == "a = 10\nb = 2\nc = 30\n"
        assert "2 block" in _text(r)

    async def test_edits_as_json_string(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("a = 1\nb = 2\n")
        edits = json.dumps([{"old_string": "a = 1", "new_string": "a = 9"}])
        r = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "edits": edits})
        assert f.read_text() == "a = 9\nb = 2\n"
        assert "Edited" in _text(r)

    async def test_edits_supersedes_old_string(self, tmp_path):
        # When edits is present, old_string/new_string are ignored.
        f = tmp_path / "a.py"
        f.write_text("keep me\ntarget\n")
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {
                "file_path": str(f),
                "old_string": "keep me",
                "new_string": "SHOULD NOT APPLY",
                "edits": [{"old_string": "target", "new_string": "TARGET"}],
            },
        )
        assert "keep me" in f.read_text()
        assert "TARGET" in f.read_text()
        assert "SHOULD NOT APPLY" not in f.read_text()

    async def test_overlap_error(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("abcdef\n")
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "abcd", "new_string": "X"},
                    {"old_string": "cdef", "new_string": "Y"},
                ],
            },
        )
        assert r.is_error
        assert "overlap" in _text(r)
        assert f.read_text() == "abcdef\n"  # unchanged

    async def test_duplicate_error(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x x\n")
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "edits": [{"old_string": "x", "new_string": "z"}]},
        )
        assert r.is_error
        assert "unique" in _text(r)


class TestLineEndingFidelity:
    async def test_crlf_preserved(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_bytes(b"a = 1\r\nb = 2\r\n")
        await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "a = 1", "new_string": "a = 99"},
        )
        assert f.read_bytes() == b"a = 99\r\nb = 2\r\n"

    async def test_bom_preserved(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_bytes("﻿a = 1\nb = 2\n".encode("utf-8"))
        await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "a = 1", "new_string": "a = 99"},
        )
        assert f.read_bytes() == "﻿a = 99\nb = 2\n".encode("utf-8")


class TestConfinement:
    async def test_write_outside_root_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = tmp_path / "outside.py"
        target.write_text("alpha beta\n")
        env = ToolEnv(cwd=ws, read_roots=(ws,), write_roots=(ws,))
        r = await _run(
            make_edit_tool(env),
            {"file_path": str(target), "old_string": "beta", "new_string": "BETA"},
        )
        assert target.read_text() == "alpha beta\n"  # unchanged
        assert "outside" in _text(r).lower() or "workspace" in _text(r).lower()


class TestErrorPaths:
    async def test_missing_file(self, tmp_path):
        r = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(tmp_path / "nope.py"), "old_string": "a", "new_string": "b"},
        )
        assert r.is_error
        assert "not found" in _text(r).lower()

    async def test_no_edits_and_no_old_string(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x\n")
        r = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f)})
        assert r.is_error
