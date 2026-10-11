"""Every security parity row is witnessed, pending, or excluded by design.

`tests/support/parity.py` holds the matrix's rows and the decorator a witness
carries. This file walks `tests/` for that decorator with `ast` rather than
importing the tier modules, so a witness under `tests/smoke/` or `tests/image/`
counts without its tier's fixtures loading. Reading files off disk is invisible
to testmon (`AGENTS.md`), so `scripts/qt` will not select this file when a
witness is added; name it in the closing run of any change that moves one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.support import parity

TESTS = Path(__file__).resolve().parent


def _witness_rows(decorator: ast.expr) -> list[int] | None:
    """The rows a `parity.witness(...)` or `witness(...)` decorator names."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    named = (
        isinstance(func, ast.Attribute) and func.attr == "witness"
        and isinstance(func.value, ast.Name) and func.value.id == "parity"
    ) or (isinstance(func, ast.Name) and func.id == "witness")
    if not named:
        return None
    rows = []
    for arg in decorator.args:
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, int)):
            raise AssertionError(f"parity.witness takes literal row numbers, got {ast.dump(arg)}")
        rows.append(arg.value)
    return rows


def scan_witnesses(root: Path) -> dict[int, list[str]]:
    """Row number to the `file::name` of every class or function witnessing it."""
    found: dict[int, list[str]] = {}
    for path in sorted(root.rglob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                rows = _witness_rows(decorator)
                for row in rows or ():
                    found.setdefault(row, []).append(f"{path.relative_to(root.parent)}::{node.name}")
    return found


@pytest.fixture(scope="module")
def witnesses() -> dict[int, list[str]]:
    return scan_witnesses(TESTS)


class TestTheMatrix:
    def test_rows_are_numbered_one_to_twenty_without_gaps(self):
        assert sorted(parity.ROWS) == list(range(1, 21))

    def test_row_14_alone_is_not_witnessed_by_design(self):
        assert set(parity.NOT_WITNESSED) == {14}


class TestEveryRowHasAWitness:
    def test_every_row_is_witnessed_pending_or_excluded(self, witnesses):
        unaccounted = [
            row for row in parity.ROWS
            if row not in witnesses and row not in parity.PENDING and row not in parity.NOT_WITNESSED
        ]
        assert unaccounted == [], f"rows with no witness and no pending stage: {unaccounted}"

    def test_a_witnessed_row_is_no_longer_pending(self, witnesses):
        both = sorted(set(witnesses) & set(parity.PENDING))
        assert both == [], f"rows witnessed but still listed in PENDING: {both}"

    def test_the_excluded_row_has_no_witness_and_is_not_pending(self, witnesses):
        for row in parity.NOT_WITNESSED:
            assert row not in witnesses
            assert row not in parity.PENDING

    def test_every_witness_names_a_real_row(self, witnesses):
        assert sorted(set(witnesses) - set(parity.ROWS)) == []

    def test_row_1_is_witnessed_by_the_existing_smoke_sandbox_tests(self, witnesses):
        files = {name.split("::")[0] for name in witnesses.get(1, [])}
        assert files == {
            "tests/smoke/test_sandbox_in_stack.py",
            "tests/smoke/test_sandbox_repos_isolation.py",
            "tests/smoke/test_sandbox_shared_room.py",
        }


class TestThePendingListOnlyShrinks:
    def test_pending_is_a_subset_of_what_stage_1_left(self):
        grown = sorted(set(parity.PENDING) - parity.PENDING_AT_STAGE_1)
        assert grown == [], f"rows added to PENDING after Stage 1: {grown}"

    def test_every_pending_row_names_the_stage_that_closes_it(self):
        for row, stage in parity.PENDING.items():
            assert stage.startswith("Stage "), (row, stage)

    def test_an_outstanding_half_belongs_to_a_witnessed_row(self, witnesses):
        for row, half in parity.OUTSTANDING_HALVES.items():
            assert row in witnesses, f"row {row} has an outstanding half but no witness"
            assert row not in parity.PENDING
            assert half.startswith("Stage "), (row, half)


class TestTheScanner:
    def test_it_finds_both_spellings_and_ignores_other_decorators(self, tmp_path):
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_example.py").write_text(
            "from tests.support import parity\n"
            "from tests.support.parity import witness\n"
            "import pytest\n"
            "@parity.witness(4, 5)\n"
            "class TestA:\n"
            "    @witness(16)\n"
            "    def test_b(self): pass\n"
            "@pytest.mark.smoke\n"
            "def test_c(): pass\n"
        )
        (tests / "helper.py").write_text("@parity.witness(9)\ndef test_not_a_test_file(): pass\n")
        assert scan_witnesses(tests) == {
            4: ["tests/test_example.py::TestA"],
            5: ["tests/test_example.py::TestA"],
            16: ["tests/test_example.py::test_b"],
        }

    def test_a_computed_row_number_is_refused(self, tmp_path):
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_example.py").write_text("ROW = 4\n@parity.witness(ROW)\ndef test_a(): pass\n")
        with pytest.raises(AssertionError, match="literal row numbers"):
            scan_witnesses(tests)


class TestTheDecorator:
    def test_it_records_rows_and_returns_the_target_unchanged(self):
        class Target:
            pass

        marked = parity.witness(2)(parity.witness(3)(Target))
        assert marked is Target
        assert Target.__parity_rows__ == (3, 2)

    @pytest.mark.parametrize("rows", [(), (0,), (21,)])
    def test_it_refuses_no_row_or_an_unknown_one(self, rows):
        with pytest.raises(ValueError):
            parity.witness(*rows)
