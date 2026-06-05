"""Phase 2 — Read / Write / Edit / Grep / Glob tool implementations."""


import pytest

from istota.session.tools import (
    ToolEnv,
    make_edit_tool,
    make_glob_tool,
    make_grep_tool,
    make_read_tool,
    make_write_tool,
)

pytestmark = pytest.mark.asyncio


def _env(tmp_path):
    return ToolEnv(cwd=tmp_path)


async def _run(tool, args):
    return await tool.execute("c1", args, None, None)


def _text(result):
    return result.content[0].text


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


class TestRead:
    async def test_reads_with_line_numbers(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("first\nsecond\nthird\n")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(f)})
        text = _text(result)
        assert "1\tfirst" in text
        assert "2\tsecond" in text
        assert "3\tthird" in text

    async def test_missing_file(self, tmp_path):
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(tmp_path / "nope")})
        assert "not found" in _text(result).lower()

    async def test_offset_and_limit(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(f), "offset": 3, "limit": 2})
        text = _text(result)
        assert "3\tline3" in text
        assert "4\tline4" in text
        assert "line5" not in text
        assert "more lines" in text

    async def test_binary_rejected(self, tmp_path):
        f = tmp_path / "b.bin"
        f.write_bytes(b"\x00\x01\x02binary")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(f)})
        assert "binary" in _text(result).lower()

    async def test_relative_path_resolves_against_cwd(self, tmp_path):
        (tmp_path / "rel.txt").write_text("hi\n")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": "rel.txt"})
        assert "hi" in _text(result)


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


class TestWrite:
    async def test_creates_file_and_parents(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "out.txt"
        result = await _run(make_write_tool(_env(tmp_path)), {"file_path": str(target), "content": "hello\n"})
        assert target.read_text() == "hello\n"
        assert "Created" in _text(result)

    async def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old")
        result = await _run(make_write_tool(_env(tmp_path)), {"file_path": str(target), "content": "new"})
        assert target.read_text() == "new"
        assert "Updated" in _text(result)


# --------------------------------------------------------------------------- #
# Edit
# --------------------------------------------------------------------------- #


class TestEdit:
    async def test_replaces_unique_string(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("alpha beta gamma")
        result = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "old_string": "beta", "new_string": "BETA"})
        assert f.read_text() == "alpha BETA gamma"
        assert "Edited" in _text(result)

    async def test_missing_old_string(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("alpha")
        result = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "old_string": "zzz", "new_string": "x"})
        assert "not found" in _text(result).lower()

    async def test_ambiguous_without_replace_all(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x x x")
        result = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "old_string": "x", "new_string": "y"})
        assert "replace_all" in _text(result)
        assert f.read_text() == "x x x"  # unchanged

    async def test_replace_all(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x x x")
        result = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "x", "new_string": "y", "replace_all": True},
        )
        assert f.read_text() == "y y y"
        assert "3 occurrences" in _text(result)

    async def test_identical_strings_rejected(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello")
        result = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "old_string": "hello", "new_string": "hello"})
        assert "identical" in _text(result).lower()


# --------------------------------------------------------------------------- #
# Glob
# --------------------------------------------------------------------------- #


class TestGlob:
    async def test_finds_by_pattern(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        result = await _run(make_glob_tool(_env(tmp_path)), {"pattern": "*.py"})
        text = _text(result)
        assert "a.py" in text
        assert "b.py" in text
        assert "c.txt" not in text

    async def test_recursive(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text("")
        result = await _run(make_glob_tool(_env(tmp_path)), {"pattern": "**/*.py"})
        assert "mod.py" in _text(result)

    async def test_no_matches(self, tmp_path):
        result = await _run(make_glob_tool(_env(tmp_path)), {"pattern": "*.rs"})
        assert "No files match" in _text(result)


# --------------------------------------------------------------------------- #
# Grep
# --------------------------------------------------------------------------- #


class TestGrep:
    async def test_files_with_matches_default(self, tmp_path):
        (tmp_path / "a.txt").write_text("needle here\n")
        (tmp_path / "b.txt").write_text("nothing\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "needle"})
        text = _text(result)
        assert "a.txt" in text
        assert "b.txt" not in text

    async def test_content_mode(self, tmp_path):
        (tmp_path / "a.txt").write_text("line one\nhas needle\nline three\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "needle", "output_mode": "content"})
        text = _text(result)
        assert ":2:" in text
        assert "has needle" in text

    async def test_count_mode(self, tmp_path):
        (tmp_path / "a.txt").write_text("x\nx\ny\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "x", "output_mode": "count"})
        assert ":2" in _text(result)

    async def test_case_insensitive(self, tmp_path):
        (tmp_path / "a.txt").write_text("HELLO\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "hello", "-i": True})
        assert "a.txt" in _text(result)

    async def test_glob_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("match\n")
        (tmp_path / "a.txt").write_text("match\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "match", "glob": "*.py"})
        text = _text(result)
        assert "a.py" in text
        assert "a.txt" not in text

    async def test_invalid_regex(self, tmp_path):
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "("})
        assert "invalid regex" in _text(result).lower()

    async def test_skips_binary(self, tmp_path):
        (tmp_path / "bin").write_bytes(b"\x00needle\x00")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "needle"})
        assert "No matches" in _text(result)

    async def test_head_limit(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("hit\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "hit", "head_limit": 2})
        assert "more)" in _text(result)
