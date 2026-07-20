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

    async def test_truncation_states_concrete_offset(self, tmp_path):
        # Stage 5: the tail note names the exact offset to continue from.
        f = tmp_path / "a.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(f), "offset": 3, "limit": 2})
        # read lines 3-4 → next line is 5.
        assert "offset=5" in _text(result)

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
        assert "could not find" in _text(result).lower()

    async def test_ambiguous_without_replace_all(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x x x")
        result = await _run(make_edit_tool(_env(tmp_path)), {"file_path": str(f), "old_string": "x", "new_string": "y"})
        assert "unique" in _text(result)
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
        assert "more; raise head_limit" in _text(result)


class TestGrepContextAndLiteral:
    """Stage 6 — `-C` context lines and `literal` matching (pure-Python)."""

    async def test_context_lines(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\nNEEDLE\nfour\nfive\n")
        result = await _run(
            make_grep_tool(_env(tmp_path)),
            {"pattern": "NEEDLE", "output_mode": "content", "-C": 1},
        )
        text = _text(result)
        # Match line uses `:lineno:`, context lines use `-lineno-`.
        assert ":3:NEEDLE" in text
        assert "-2-two" in text
        assert "-4-four" in text
        # Lines outside the context window are absent.
        assert "one" not in text
        assert "five" not in text

    async def test_context_group_separator(self, tmp_path):
        # Two matches far apart → a `--` separator between the two context groups.
        lines = ["x"] * 20
        lines[2] = "MATCH"
        lines[15] = "MATCH"
        (tmp_path / "a.txt").write_text("\n".join(lines) + "\n")
        result = await _run(
            make_grep_tool(_env(tmp_path)),
            {"pattern": "MATCH", "output_mode": "content", "-C": 1},
        )
        text = _text(result)
        assert "--" in text
        assert ":3:MATCH" in text
        assert ":16:MATCH" in text

    async def test_context_alias_key(self, tmp_path):
        (tmp_path / "a.txt").write_text("a\nHIT\nb\n")
        result = await _run(
            make_grep_tool(_env(tmp_path)),
            {"pattern": "HIT", "output_mode": "content", "context": 1},
        )
        text = _text(result)
        assert "-1-a" in text
        assert ":2:HIT" in text

    async def test_literal_treats_metacharacters_literally(self, tmp_path):
        (tmp_path / "a.txt").write_text("value = foo.bar\nvalue = fooXbar\n")
        # As a literal, `foo.bar` matches only the dotted line (not fooXbar).
        result = await _run(
            make_grep_tool(_env(tmp_path)),
            {"pattern": "foo.bar", "output_mode": "content", "literal": True},
        )
        text = _text(result)
        assert "foo.bar" in text
        assert "fooXbar" not in text

    async def test_literal_matches_regex_special_string(self, tmp_path):
        (tmp_path / "a.txt").write_text("cost is $5 (approx)\n")
        # `$5 (approx)` is not a valid regex, but literal matches it fine.
        result = await _run(
            make_grep_tool(_env(tmp_path)),
            {"pattern": "$5 (approx)", "output_mode": "content", "literal": True},
        )
        assert "cost is" in _text(result)


# --------------------------------------------------------------------------- #
# Confinement (NB-1) — file tools must not escape the allowed roots
# --------------------------------------------------------------------------- #


def _confined_env(workspace, *, read_extra=(), write_roots=None):
    """A ToolEnv confined to ``workspace`` (writable) plus optional read roots."""
    write = (workspace,) if write_roots is None else tuple(write_roots)
    reads = (workspace, *read_extra)
    return ToolEnv(cwd=workspace, read_roots=reads, write_roots=write)


class TestConfinement:
    async def test_unconfined_env_is_not_confined(self, tmp_path):
        env = ToolEnv(cwd=tmp_path)
        assert env.confined is False

    async def test_confined_env_reports_confined(self, tmp_path):
        env = _confined_env(tmp_path)
        assert env.confined is True

    async def test_read_inside_root_allowed(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("hello\n")
        result = await _run(make_read_tool(_confined_env(ws)), {"file_path": str(ws / "a.txt")})
        assert "hello" in _text(result)

    async def test_read_outside_root_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("classified\n")
        result = await _run(make_read_tool(_confined_env(ws)), {"file_path": str(secret)})
        text = _text(result).lower()
        assert "classified" not in text
        assert "outside" in text or "not allowed" in text or "workspace" in text

    async def test_read_traversal_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "secret.txt").write_text("classified\n")
        result = await _run(make_read_tool(_confined_env(ws)), {"file_path": str(ws / ".." / "secret.txt")})
        assert "classified" not in _text(result)

    async def test_symlink_escape_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("classified\n")
        link = ws / "link.txt"
        link.symlink_to(outside)
        result = await _run(make_read_tool(_confined_env(ws)), {"file_path": str(link)})
        assert "classified" not in _text(result)

    async def test_write_inside_root_allowed(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "out.txt"
        result = await _run(make_write_tool(_confined_env(ws)), {"file_path": str(target), "content": "hi\n"})
        assert target.read_text() == "hi\n"
        assert "Created" in _text(result)

    async def test_write_outside_root_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = tmp_path / "escape.txt"
        result = await _run(make_write_tool(_confined_env(ws)), {"file_path": str(target), "content": "x"})
        assert not target.exists()
        assert "outside" in _text(result).lower() or "workspace" in _text(result).lower()

    async def test_write_denied_in_read_only_root(self, tmp_path):
        # A root that is readable but not writable rejects writes but allows reads.
        readonly = tmp_path / "ro"
        readonly.mkdir()
        (readonly / "doc.txt").write_text("readable\n")
        ws = tmp_path / "ws"
        ws.mkdir()
        env = _confined_env(ws, read_extra=(readonly,))
        # read from the read-only root works
        read_result = await _run(make_read_tool(env), {"file_path": str(readonly / "doc.txt")})
        assert "readable" in _text(read_result)
        # write into the read-only root is rejected
        write_result = await _run(make_write_tool(env), {"file_path": str(readonly / "new.txt"), "content": "x"})
        assert not (readonly / "new.txt").exists()
        assert "outside" in _text(write_result).lower() or "workspace" in _text(write_result).lower()

    async def test_edit_outside_root_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = tmp_path / "outside.txt"
        target.write_text("alpha beta\n")
        result = await _run(
            make_edit_tool(_confined_env(ws)),
            {"file_path": str(target), "old_string": "beta", "new_string": "BETA"},
        )
        assert target.read_text() == "alpha beta\n"  # unchanged
        assert "outside" in _text(result).lower() or "workspace" in _text(result).lower()

    async def test_grep_path_outside_root_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        (secret_dir / "s.txt").write_text("classified needle\n")
        result = await _run(make_grep_tool(_confined_env(ws)), {"pattern": "needle", "path": str(secret_dir)})
        assert "classified" not in _text(result)
        assert "outside" in _text(result).lower() or "workspace" in _text(result).lower()

    async def test_glob_path_outside_root_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        (secret_dir / "s.py").write_text("")
        result = await _run(make_glob_tool(_confined_env(ws)), {"pattern": "*.py", "path": str(secret_dir)})
        assert "s.py" not in _text(result)
        assert "outside" in _text(result).lower() or "workspace" in _text(result).lower()


class TestFileToolQuality:
    """NB-19: is_error propagation, bounded reads, path-globs, safe glob sort."""

    async def test_missing_file_is_error(self, tmp_path):
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(tmp_path / "nope")})
        assert result.is_error is True

    async def test_successful_read_not_error(self, tmp_path):
        (tmp_path / "a.txt").write_text("hi\n")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(tmp_path / "a.txt")})
        assert result.is_error is False

    async def test_edit_missing_old_string_is_error(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("alpha")
        result = await _run(
            make_edit_tool(_env(tmp_path)),
            {"file_path": str(f), "old_string": "zzz", "new_string": "x"},
        )
        assert result.is_error is True

    async def test_read_bounded_by_max_bytes(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("\n".join("line" + str(i) for i in range(10000)))
        env = _env(tmp_path)
        env.max_read_bytes = 200
        result = await _run(make_read_tool(env), {"file_path": str(f)})
        assert "truncated" in _text(result).lower()

    async def test_grep_path_glob_matches_nested(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "pkg").mkdir()
        (tmp_path / "src" / "pkg" / "mod.py").write_text("needle here\n")
        (tmp_path / "other.py").write_text("needle here\n")
        result = await _run(
            make_grep_tool(_env(tmp_path)),
            {"pattern": "needle", "glob": "src/**/*.py"},
        )
        text = _text(result)
        assert "mod.py" in text
        assert "other.py" not in text

    async def test_grep_bare_glob_still_matches_basename(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "a.txt").write_text("needle\n")
        result = await _run(make_grep_tool(_env(tmp_path)), {"pattern": "needle", "glob": "*.py"})
        text = _text(result)
        assert "a.py" in text
        assert "a.txt" not in text

    async def test_glob_sort_survives_broken_symlink(self, tmp_path):
        (tmp_path / "real.py").write_text("")
        # A broken symlink that resolves to nothing must not crash the mtime sort.
        (tmp_path / "dangling.py").symlink_to(tmp_path / "gone")
        result = await _run(make_glob_tool(_env(tmp_path)), {"pattern": "*.py"})
        assert "real.py" in _text(result)
