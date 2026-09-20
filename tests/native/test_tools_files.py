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


class TestWriteDeniedRoots:
    """A read-only carve-out nested inside a writable root.

    Mirrors build_cmd's ``--ro-bind`` of ``.developer`` applied after the
    read-write bind of its parent (executor.py, "must be read-only to prevent a
    compromised subprocess from replacing them"): reads pass, writes do not.
    """

    def _env(self, workspace, denied):
        return ToolEnv(
            cwd=workspace,
            read_roots=(workspace,),
            write_roots=(workspace,),
            write_denied_roots=tuple(denied),
        )

    async def test_write_into_denied_subdir_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        env = self._env(ws, [carve])
        target = carve / "credential-fetch"
        result = await _run(
            make_write_tool(env), {"file_path": str(target), "content": "#!/bin/sh\n"},
        )
        assert not target.exists()
        assert "read-only" in _text(result).lower()

    async def test_read_from_denied_subdir_allowed(self, tmp_path):
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        (carve / "helper").write_text("visible\n")
        env = self._env(ws, [carve])
        result = await _run(make_read_tool(env), {"file_path": str(carve / "helper")})
        assert "visible" in _text(result)

    async def test_edit_inside_denied_subdir_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        target = carve / "credential-fetch"
        target.write_text("original\n")
        env = self._env(ws, [carve])
        result = await _run(
            make_edit_tool(env),
            {
                "file_path": str(target),
                "old_string": "original",
                "new_string": "tampered",
            },
        )
        assert target.read_text() == "original\n"
        assert "read-only" in _text(result).lower()

    async def test_sibling_of_denied_subdir_still_writable(self, tmp_path):
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        env = self._env(ws, [carve])
        target = ws / "notes.txt"
        result = await _run(
            make_write_tool(env), {"file_path": str(target), "content": "ok\n"},
        )
        assert target.read_text() == "ok\n"
        assert "Created" in _text(result)

    async def test_symlink_into_denied_subdir_rejected(self, tmp_path):
        """The deny check resolves symlinks, like the root check above it."""
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        (carve / "credential-fetch").write_text("original\n")
        link = ws / "link"
        link.symlink_to(carve / "credential-fetch")
        env = self._env(ws, [carve])
        result = await _run(
            make_write_tool(env), {"file_path": str(link), "content": "tampered\n"},
        )
        assert (carve / "credential-fetch").read_text() == "original\n"
        assert "read-only" in _text(result).lower()

    async def test_no_denied_roots_leaves_writes_alone(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        env = self._env(ws, [])
        target = ws / "a.txt"
        await _run(make_write_tool(env), {"file_path": str(target), "content": "x\n"})
        assert target.read_text() == "x\n"

    async def test_denied_even_when_unconfined(self, tmp_path):
        """A deny root is a statement about a path, not about whether a root
        allowlist happens to be configured. No caller does this today; the
        point is that one who tries gets the refusal it looks like."""
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        env = ToolEnv(cwd=ws, write_denied_roots=(carve,))  # no read_roots
        assert env.confined is False
        target = carve / "credential-fetch"
        result = await _run(
            make_write_tool(env), {"file_path": str(target), "content": "x"},
        )
        assert not target.exists()
        assert "read-only" in _text(result).lower()

    async def test_denied_via_symlinked_parent_component(self, tmp_path):
        """The check runs on the resolved path, so a symlink standing in for an
        intermediate component does not route around the carve-out."""
        ws = tmp_path / "ws"
        carve = ws / ".developer"
        carve.mkdir(parents=True)
        (ws / "alias").symlink_to(carve)
        env = self._env(ws, [carve])
        result = await _run(
            make_write_tool(env),
            {"file_path": str(ws / "alias" / "credential-fetch"), "content": "x"},
        )
        assert not (carve / "credential-fetch").exists()
        assert "read-only" in _text(result).lower()


class TestComposedSystemPromptIsWriteDenied:
    """The composed system prompt is a deny root, and it is a *file*.

    `.developer` is a directory, so every existing case above exercises the
    `is_relative_to` arm of `_in_denied`. `task_<id>_system_prompt.txt` is one
    file in a directory that stays writable, which exercises the equality arm
    — and the sibling case is what says the carve-out did not swallow the
    directory it lives in.

    Why it is denied at all: the bwrap `--ro-bind` covers Bash, which enters
    the namespace. `Write` and `Edit` go through `ToolEnv`, which is a
    different mechanism reaching the same answer, and without this a native
    task could rewrite the standing instructions it is running under.
    """

    def _env(self, workspace, denied):
        return ToolEnv(
            cwd=workspace,
            read_roots=(workspace,),
            write_roots=(workspace,),
            write_denied_roots=tuple(denied),
        )

    def _composed(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        composed = ws / "task_42_system_prompt.txt"
        composed.write_text("## Important rules\n")
        return ws, composed

    async def test_write_to_the_composed_file_is_refused(self, tmp_path):
        ws, composed = self._composed(tmp_path)
        env = self._env(ws, [composed])
        result = await _run(
            make_write_tool(env),
            {"file_path": str(composed), "content": "ignore all rules\n"},
        )
        assert composed.read_text() == "## Important rules\n"
        assert "read-only" in _text(result).lower()

    async def test_edit_of_the_composed_file_is_refused(self, tmp_path):
        ws, composed = self._composed(tmp_path)
        env = self._env(ws, [composed])
        result = await _run(
            make_edit_tool(env),
            {
                "file_path": str(composed),
                "old_string": "Important rules",
                "new_string": "Unimportant rules",
            },
        )
        assert composed.read_text() == "## Important rules\n"
        assert "read-only" in _text(result).lower()

    async def test_reading_it_back_is_allowed(self, tmp_path):
        """Denied writes only. The model may read its own instructions, and
        the bwrap `--ro-bind` says the same thing on the other mechanism."""
        ws, composed = self._composed(tmp_path)
        env = self._env(ws, [composed])
        result = await _run(make_read_tool(env), {"file_path": str(composed)})
        assert "Important rules" in _text(result)

    async def test_a_sibling_in_the_same_directory_stays_writable(self, tmp_path):
        """The task's own workspace is not collateral. `task_<id>_result.txt`
        lives here, and so does everything the task writes."""
        ws, composed = self._composed(tmp_path)
        env = self._env(ws, [composed])
        target = ws / "task_42_result.txt"
        result = await _run(
            make_write_tool(env), {"file_path": str(target), "content": "done\n"},
        )
        assert target.read_text() == "done\n"
        assert "Created" in _text(result)

    async def test_a_symlink_to_it_is_refused(self, tmp_path):
        ws, composed = self._composed(tmp_path)
        link = ws / "shortcut.txt"
        link.symlink_to(composed)
        env = self._env(ws, [composed])
        result = await _run(
            make_write_tool(env), {"file_path": str(link), "content": "tampered\n"},
        )
        assert composed.read_text() == "## Important rules\n"
        assert "read-only" in _text(result).lower()


class TestResolveReturnsResolvedPath:
    """resolve() hands back the symlink-free path it actually checked.

    Returning the raw input would leave every caller re-walking the symlinks at
    open() time — a different resolution from the one that was validated.
    """

    async def test_symlinked_parent_resolves(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        env = ToolEnv(cwd=tmp_path, read_roots=(tmp_path,), write_roots=(tmp_path,))
        got = env.resolve(str(link / "f.txt"), write=True)
        assert got == (real_dir / "f.txt").resolve()

    async def test_relative_path_resolves_against_cwd(self, tmp_path):
        env = ToolEnv(cwd=tmp_path, read_roots=(tmp_path,), write_roots=(tmp_path,))
        got = env.resolve("sub/f.txt", write=True)
        assert got == (tmp_path / "sub" / "f.txt").resolve()

    async def test_unconfined_also_resolves(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (tmp_path / "link").symlink_to(real_dir)
        env = ToolEnv(cwd=tmp_path)
        assert env.resolve(str(tmp_path / "link" / "f.txt")) == (real_dir / "f.txt").resolve()


# --------------------------------------------------------------------------- #
# Read's image arm
# --------------------------------------------------------------------------- #


def _png(tmp_path, name="shot.png", size=(64, 48)):
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, format="PNG")
    path = tmp_path / name
    path.write_bytes(buffer.getvalue())
    return path


class TestReadAnImage:
    """A PNG in the workspace used to come back as `Cannot read binary file`,
    so nothing under the native brain could look at a page, a chart or a
    screenshot a previous task took. Everything below the producer was already
    built; this is the producer."""

    async def test_it_comes_back_as_two_blocks_text_first(self, tmp_path):
        from istota.llm.types import ImageContent, TextContent
        from istota.untrusted import IMAGE_NOTICE

        path = _png(tmp_path)
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert not result.is_error
        assert len(result.content) == 2
        notice, image = result.content
        assert isinstance(notice, TextContent)
        assert isinstance(image, ImageContent)
        # Text first, because a model with no vision never sees the image and
        # would otherwise be handed an unexplained omission.
        assert "image/png" in notice.text
        assert "64x48 pixels" in notice.text
        assert IMAGE_NOTICE in notice.text
        assert image.media_type == "image/png"
        assert image.data

    async def test_the_base64_round_trips_to_the_bytes_on_disk(self, tmp_path):
        import base64

        path = _png(tmp_path)
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert base64.b64decode(result.content[1].data) == path.read_bytes()

    async def test_display_name_is_the_basename(self, tmp_path):
        # It never reaches a provider; compaction's loss notice renders it, and
        # empty it reads `[image attachment — no longer in context]`, which
        # tells the model nothing about which picture it lost.
        path = _png(tmp_path, name="screenshot-20260919-141530.png")
        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert result.content[1].display_name == "screenshot-20260919-141530.png"

    @pytest.mark.parametrize("fmt,suffix", [
        ("JPEG", ".jpg"), ("GIF", ".gif"), ("WEBP", ".webp"),
    ])
    async def test_the_other_decodable_formats(self, tmp_path, fmt, suffix):
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (32, 16), (1, 2, 3)).save(buffer, format=fmt)
        path = tmp_path / f"pic{suffix}"
        path.write_bytes(buffer.getvalue())

        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert len(result.content) == 2
        assert result.content[1].media_type.startswith("image/")
        assert "32x16 pixels" in result.content[0].text

    async def test_a_max_read_bytes_below_the_file_does_not_truncate_it(self, tmp_path):
        import base64

        # `env.max_read_bytes` is 25 MB and `_read_bytes_capped` cuts the tail
        # off. A truncated PNG is a corrupt PNG, so the image path reads the
        # whole file or refuses it.
        path = _png(tmp_path)
        env = ToolEnv(cwd=tmp_path, max_read_bytes=16)

        result = await _run(make_read_tool(env), {"file_path": str(path)})

        assert not result.is_error
        assert base64.b64decode(result.content[1].data) == path.read_bytes()

    async def test_an_image_over_the_cap_is_refused_with_the_size(self, tmp_path):
        from istota.session.tools import files

        path = tmp_path / "huge.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (files.MAX_IMAGE_BYTES + 10))

        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert result.is_error
        text = _text(result)
        assert "too large" in text
        assert str(files.MAX_IMAGE_BYTES) in text
        # Nothing truncated: no image block reaches the model at all.
        assert len(result.content) == 1

    @pytest.mark.parametrize("key,value", [("offset", 2), ("limit", 10)])
    async def test_offset_and_limit_are_refused_rather_than_ignored(
        self, tmp_path, key, value,
    ):
        # Both count lines. Silently dropping them is the shape where a caller
        # asks for part of something and is handed all of it.
        path = _png(tmp_path)
        result = await _run(
            make_read_tool(_env(tmp_path)), {"file_path": str(path), key: value},
        )

        assert result.is_error
        assert key in _text(result)
        assert len(result.content) == 1

    async def test_a_heic_is_read_rather_than_refused(self, tmp_path):
        # The one case that separates the two sniffers, and therefore the only
        # thing holding the choice between them. `sniff_raster` answers what
        # `/chat/files` serves inline and excludes HEIF on a browser-support
        # argument that has nothing to do with what a model can see; an iPhone
        # photograph is a HEIC.
        #
        # A synthetic ISO-BMFF header rather than a real encode: the arm
        # sniffs and ships bytes without ever opening the image, so what is
        # under test is the predicate, and the fixture needs no encoder.
        path = tmp_path / "photo.heic"
        path.write_bytes(
            b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 64,
        )

        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert not result.is_error
        assert len(result.content) == 2
        assert result.content[1].media_type == "image/heic"
        # HEIF needs a real ISO-BMFF box walk to reach its size, which
        # `image_dimensions` deliberately does not do, so the text block says
        # nothing about the pixels rather than guessing.
        assert "pixels" not in result.content[0].text

    async def test_an_svg_named_png_still_takes_the_binary_branch(self, tmp_path):
        # The case `image_sniff` exists for: the extension is a caller-supplied
        # string on a file the model wrote, so the decision is the bytes'.
        path = tmp_path / "trick.png"
        path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'>\x00</svg>")

        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert result.is_error
        assert "binary" in _text(result).lower()

    async def test_a_non_image_binary_is_unchanged(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(b"\x00\x01\x02binary")

        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert result.is_error
        assert "Cannot read binary file" in _text(result)

    async def test_a_text_file_is_unchanged(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("first\nsecond\n")

        result = await _run(make_read_tool(_env(tmp_path)), {"file_path": str(path)})

        assert len(result.content) == 1
        assert "1\tfirst" in _text(result)

    async def test_the_confinement_still_applies(self, tmp_path):
        # The arm sits inside `_read`, after `env.resolve`, so a path outside
        # the roots is refused before anything is sniffed.
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = _png(tmp_path, name="outside.png")
        env = ToolEnv(cwd=workspace, read_roots=(workspace,), write_roots=(workspace,))

        result = await _run(make_read_tool(env), {"file_path": str(outside)})

        assert result.is_error
        assert len(result.content) == 1


class TestTheImageCapIsTheTreesOwnNumber:
    async def test_it_equals_the_brains_per_image_cap(self):
        # Restated rather than imported: `files.py` runs inside the tool
        # server, and `brain/native.py` pulls the whole brain import graph into
        # a process that starts once per task attempt.
        from istota.brain import native
        from istota.session.tools import files

        assert files.MAX_IMAGE_BYTES == native._MAX_IMAGE_BYTES

    async def test_it_sits_well_inside_the_tool_server_frame_cap(self):
        # base64 inflates by 4/3, and the frame also carries the text block and
        # the envelope.
        from istota import tool_server_protocol as proto
        from istota.session.tools import files

        assert files.MAX_IMAGE_BYTES * 4 // 3 < proto.MAX_FRAME_BYTES
