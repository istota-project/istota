"""One statement of each path-containment idiom, enforced by shape (F18).

``user_scope.py`` owns three rules — ``scoped_user_dir``'s two-term equality,
``is_within``'s at-or-under test and ``paths_overlap``'s both-directions test —
and every one of them had been written out again somewhere else. Prose did not
stop that: ``repos_relocate._contained``, the developer skill's inline copy and
both of ``sandbox_cache_sweeper``'s each carried a comment naming the function
they were a copy of, and stayed copies.

So this guard reads **shape, not names**. It parses every module under
``src/istota`` and looks for the syntax each idiom takes, which is what a new
copy would be written in whatever it were called and whichever file it landed
in. A name-keyed list is the failure round 1 of this spec measured twice: two
drift guards enumerated their modules by hand, so the sixth loader would have
been unguarded while they reported green.

**What is deliberately not flagged.** A bare ``x.is_relative_to(y)`` or a
single ``relative_to`` inside a larger expression is ordinary pathlib and is
used all over the tree for things that are not a containment decision — taking
a relative part, pruning a walk, building a display path. The three shapes
below are narrower than that on purpose: each is the *whole* of a predicate,
which is what a re-implementation of one of these functions looks like and
what an incidental use does not.

The one permitted file is ``user_scope.py`` itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "istota"

#: The module allowed to state each rule. Exactly one, which is the point.
OWNER = "user_scope.py"


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _attr_call(node, name: str) -> bool:
    """``<anything>.name(...)`` with at least one argument."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        and len(node.args) == 1
    )


def _relative_to_pair(node) -> tuple[str, str] | None:
    """``a.is_relative_to(b)`` as the pair ``(a, b)``, unparsed."""
    if not _attr_call(node, "is_relative_to"):
        return None
    return ast.unparse(node.func.value), ast.unparse(node.args[0])


def _find_overlap_copies(tree) -> list[int]:
    """Both directions of the same at-or-under test in one boolean expression.

    ``a.is_relative_to(b) or b.is_relative_to(a)`` — with or without the
    redundant ``a == b`` term the four converted sites carried, since
    ``is_relative_to`` already answers True for two equal paths.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        pairs = [p for p in (_relative_to_pair(v) for v in node.values) if p]
        if any((y, x) in pairs for (x, y) in pairs if x != y):
            hits.append(node.lineno)
    return hits


def _find_single_direction_copies(tree) -> list[int]:
    """``a == b or a.is_relative_to(b)`` -- the at-or-under test spelled inline.

    A separate matcher from the overlap one, which requires *both* directions
    and so was blind to every site this stage actually converted. The redundant
    equality term is the tell and is what keeps this narrow: a bare
    ``x.is_relative_to(y)`` inside a larger expression is ordinary pathlib and
    is not matched, because writing the equality out beside it is what someone
    does when they mean "at or under" and have not noticed that
    ``is_relative_to`` already says so.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        pairs = [p for p in (_relative_to_pair(v) for v in node.values) if p]
        equalities = [
            (ast.unparse(v.left), ast.unparse(v.comparators[0]))
            for v in node.values
            if isinstance(v, ast.Compare)
            and len(v.ops) == 1
            and isinstance(v.ops[0], ast.Eq)
        ]
        if any((x, y) in equalities or (y, x) in equalities for (x, y) in pairs):
            hits.append(node.lineno)
    return hits


def _find_is_within_copies(tree) -> list[int]:
    """A function whose entire body is ``try: p.relative_to(q) / except: False``.

    The five-times-repeated predicate. Written as a ``try`` because
    ``relative_to`` raises rather than answering, which is the tell: a caller
    that wanted the *relative part* uses the result, and one that wanted the
    boolean throws it away.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [s for s in node.body if not _is_docstring(s)]
        if len(body) != 1 or not isinstance(body[0], ast.Try):
            continue
        block = body[0]
        used = [
            s for s in block.body
            if isinstance(s, ast.Expr) and _attr_call(s.value, "relative_to")
        ]
        returns_bool = any(
            isinstance(s, ast.Return) and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, bool)
            for h in block.handlers for s in h.body
        )
        if used and returns_bool:
            hits.append(node.lineno)
    return hits


def _find_scoped_user_dir_copies(tree) -> list[str]:
    """``candidate.parent == root and candidate.resolve() == root.resolve() / x``.

    The two-term equality, matched on the two terms rather than on their
    spelling: one comparison against a ``.parent`` and one against a
    ``.resolve()``, joined into a single value. Every hand-rolled copy the
    audit found is this, whether written as one ``and``, as a tuple of
    conditions or split over two ``if``\\ s in one function body.

    ``!=`` counts as well as ``==``. Both of the cache sweeper's copies were
    written negated -- ``candidate.parent != subtree``, ``candidate.resolve()
    != resolved_root / user_id / CACHE_ROOT_NAME`` -- so a matcher keyed on
    ``ast.Eq`` alone missed the two sites this guard's own docstring names as
    its motivating examples, and a re-copy written the same way would report
    green. Found by review, not by reading.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent_cmp = resolve_cmp = False
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Compare):
                continue
            if not isinstance(inner.ops[0], (ast.Eq, ast.NotEq)):
                continue
            left = ast.unparse(inner.left)
            if left.endswith(".parent"):
                parent_cmp = True
            if ".resolve()" in left:
                resolve_cmp = True
        if parent_cmp and resolve_cmp:
            hits.append(node.name)
    return hits


def _is_docstring(stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _offenders(finder) -> list[str]:
    """Every hit outside ``user_scope.py``, as ``path:where``.

    ``where`` is a line number for the two expression-shaped idioms and the
    *function name* for the scoping equality, because that one carries a
    permitted site and a line number for it would drift into a lie the first
    time anything above it changed — which is how this spec's earlier stages
    kept finding their premises stale.
    """
    out = []
    for path, tree in _modules():
        for where in finder(tree):
            if path.name == OWNER:
                continue
            out.append(f"{path.relative_to(SRC)}:{where}")
    return out


class TestOneStatementOfEachIdiom:
    def test_the_overlap_test_is_not_written_out_anywhere(self):
        assert _offenders(_find_overlap_copies) == [], (
            "both directions of the at-or-under test written inline; "
            "call user_scope.paths_overlap"
        )

    def test_the_at_or_under_test_is_not_spelled_inline_anywhere(self):
        assert _offenders(_find_single_direction_copies) == [], (
            "`x == y or x.is_relative_to(y)` written inline; the equality term "
            "is redundant and the whole expression is user_scope.is_within"
        )

    def test_the_at_or_under_predicate_is_not_written_out_anywhere(self):
        assert _offenders(_find_is_within_copies) == [], (
            "a local try/relative_to/except predicate; call user_scope.is_within"
        )

    def test_the_scoping_equality_is_not_written_out_anywhere(self):
        assert _offenders(_find_scoped_user_dir_copies) == [
            "executor.py:get_task_control_dir",
        ], (
            "the two-term scoping equality written out again; call "
            "user_scope.scoped_user_dir. The one permitted site is "
            "executor.get_task_control_dir, whose root is deliberately "
            "unresolved — see its docstring and user_scope's."
        )


class TestTheGuardCanFail:
    """The controls. A shape matcher that matches nothing reports green.

    Each of these is the idiom as it was written in the tree before this
    stage, parsed from a string, and each must be found.
    """

    def test_the_overlap_shape_is_recognised(self):
        tree = ast.parse("def f(a, b):\n    return a == b or a.is_relative_to(b) or b.is_relative_to(a)\n")
        assert _find_overlap_copies(tree)

    def test_the_overlap_shape_without_the_equality_term_is_recognised(self):
        tree = ast.parse("def f(a, b):\n    return a.is_relative_to(b) or b.is_relative_to(a)\n")
        assert _find_overlap_copies(tree)

    def test_one_direction_alone_is_not_an_overlap(self):
        tree = ast.parse("def f(a, b):\n    return a.is_relative_to(b)\n")
        assert not _find_overlap_copies(tree)

    def test_the_single_direction_shape_is_recognised(self):
        tree = ast.parse("def f(a, b):\n    return a == b or a.is_relative_to(b)\n")
        assert _find_single_direction_copies(tree)

    def test_the_single_direction_shape_is_recognised_reversed(self):
        tree = ast.parse("def f(a, b):\n    return b.is_relative_to(a) or a == b\n")
        assert _find_single_direction_copies(tree)

    def test_a_bare_is_relative_to_is_not_matched(self):
        """Ordinary pathlib, used all over the tree for things that are not a
        containment decision."""
        tree = ast.parse("def f(a, b, c):\n    return a.is_relative_to(b) or c\n")
        assert not _find_single_direction_copies(tree)

    def test_the_negated_scoping_equality_is_recognised(self):
        """The cache sweeper's own pre-change spelling, which the first version
        of this matcher missed entirely."""
        tree = ast.parse(
            "def f(root, user_id, cache):\n"
            "    c = root / user_id / cache\n"
            "    if c.parent != root / user_id:\n"
            "        return None\n"
            "    if c.resolve() != root.resolve() / user_id / cache:\n"
            "        return None\n"
            "    return c\n"
        )
        assert _find_scoped_user_dir_copies(tree) == ["f"]

    def test_an_async_predicate_copy_is_recognised(self):
        tree = ast.parse(
            "async def f(p, q):\n"
            "    try:\n"
            "        p.relative_to(q)\n"
            "        return True\n"
            "    except ValueError:\n"
            "        return False\n"
        )
        assert _find_is_within_copies(tree)

    def test_the_predicate_shape_is_recognised(self):
        tree = ast.parse(
            "def f(p, q):\n"
            "    try:\n"
            "        p.relative_to(q)\n"
            "        return True\n"
            "    except ValueError:\n"
            "        return False\n"
        )
        assert _find_is_within_copies(tree)

    def test_taking_the_relative_part_is_not_the_predicate(self):
        tree = ast.parse(
            "def f(p, q):\n"
            "    try:\n"
            "        rel = p.relative_to(q)\n"
            "    except ValueError:\n"
            "        return None\n"
            "    return rel\n"
        )
        assert not _find_is_within_copies(tree)

    def test_the_scoping_equality_is_recognised(self):
        tree = ast.parse(
            "def f(root, name):\n"
            "    c = root / name\n"
            "    return c.parent == root and c.resolve() == root.resolve() / name\n"
        )
        assert _find_scoped_user_dir_copies(tree) == ["f"]

    def test_the_scoping_equality_split_over_two_ifs_is_recognised(self):
        tree = ast.parse(
            "def f(root, name):\n"
            "    c = root / name\n"
            "    if c.parent == root:\n"
            "        if c.resolve() == root.resolve() / name:\n"
            "            return c\n"
            "    return None\n"
        )
        assert _find_scoped_user_dir_copies(tree)

    def test_one_term_alone_is_not_the_equality(self):
        tree = ast.parse("def f(root, name):\n    return (root / name).parent == root\n")
        assert not _find_scoped_user_dir_copies(tree)
