"""The money package names its encoding on every text file it touches.

The ledger is beancount text the user owns, and payees arrive from Monarch with
whatever characters the merchant registered — "Café Nordwind", "Grüner Markt".
Consolidating the atomic writer pinned two readers in ``core/edit.py`` and the
writer they round-trip with; the staging writers, the append path and the
remaining readers were left taking ``locale.getencoding()``, so a daemon
started without a UTF-8 locale would read the ledger as ASCII and raise, or
write it as ASCII and refuse the transaction (ISSUE-467).

Nothing shipped hits that today — PEP 538 coerces the C locale to C.UTF-8, so
the default resolves to UTF-8 in practice. What is closed is the asymmetry: one
half of a round trip naming an encoding and the other half inheriting one.

Two kinds of test. The behavioural ones run the real functions in a child
interpreter under a genuinely ASCII locale, which is the only condition that
tells a pinned call site from an unpinned one. The guards read the source, so
the next call site is caught without needing a subprocess of its own — one over
the file I/O, one over the two beancount subprocesses, whose output is decoded
with the same locale and is the other half of the same round trip.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.support.ascii_locale import run_ascii_locale

MONEY_SRC = Path(__file__).resolve().parents[2] / "src" / "istota" / "money"

LEDGER = (
    '2026-01-15 * "Café Nordwind" "Coffee"\n'
    "  Expenses:Food:Coffee  8.50 USD\n"
    "  Assets:Bank:Checking\n"
)


def _text_io_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Every ``open`` / ``read_text`` / ``write_text`` call in text mode.

    Keyed on the call shape rather than on a list of known files, because the
    thing being prevented is a *new* call site, which will be in a file no
    list written today names. Those three spellings are the whole of what this
    sees: a `codecs.open` or an `os.fdopen` would pass it, and neither exists
    in this package today.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        else:
            continue
        if name not in {"open", "read_text", "write_text"}:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        if name == "open" and _binary_mode(node):
            continue
        hits.append((node.lineno, ast.unparse(node)[:70]))
    return hits


MODE_CHARS = set("rwxab+t")


def _binary_mode(node: ast.Call) -> bool:
    """``open(p, "rb")`` and friends — no encoding applies, and none is legal.

    The mode is the second positional for the builtin and the first for
    ``Path.open``, and the AST cannot tell ``p.open(...)`` from ``zf.open(...)``
    — so a candidate counts only if it reads as a mode. Substring-testing an
    arbitrary first argument for a "b" exempts ``zf.open("body.json")``, which
    is the false negative that lets a real unpinned call site through.
    """
    candidates = []
    positions = (0, 1) if isinstance(node.func, ast.Attribute) else (1,)
    for position in positions:
        if len(node.args) > position and isinstance(node.args[position], ast.Constant):
            candidates.append(node.args[position].value)
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            candidates.append(kw.value.value)
    return any(
        isinstance(mode, str) and mode and set(mode) <= MODE_CHARS and "b" in mode
        for mode in candidates
    )


def test_every_money_text_io_names_its_encoding():
    unpinned = []
    for path in sorted(MONEY_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, source in _text_io_calls(tree):
            unpinned.append(f"{path.relative_to(MONEY_SRC.parents[2])}:{lineno}: {source}")

    assert unpinned == [], (
        "text I/O without an explicit encoding takes the process locale, which "
        "the daemon does not control:\n" + "\n".join(unpinned)
    )


def _locale_decoded_subprocess(tree: ast.AST) -> list[tuple[int, str]]:
    """``subprocess.run(..., text=True)`` with no ``encoding``.

    ``text=True`` decodes the child's output with ``locale.getencoding()``,
    which is the same defect as an unpinned ``read_text`` on the other half of
    the round trip: bean-check echoes the offending ledger line back, so its
    stderr carries whatever the payee holds.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name not in {"run", "check_output", "Popen"}:
            continue
        keywords = {kw.arg for kw in node.keywords}
        if not keywords & {"text", "universal_newlines"}:
            continue
        if "encoding" in keywords:
            continue
        hits.append((node.lineno, ast.unparse(node.func)))
    return hits


def test_every_money_subprocess_names_its_encoding():
    unpinned = []
    for path in sorted(MONEY_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, source in _locale_decoded_subprocess(tree):
            unpinned.append(f"{path.relative_to(MONEY_SRC.parents[2])}:{lineno}: {source}")

    assert unpinned == [], (
        "text-mode subprocess output without an explicit encoding is decoded "
        "with the process locale:\n" + "\n".join(unpinned)
    )


def test_ledger_reads_back_under_an_ascii_locale(tmp_path):
    """``_ledger_has_posting`` scans the ledger for a payee it was given.

    The second call is what makes the first one mean something: the function
    also answers True when the ledger is missing, so a fixture path that never
    resolved would print FOUND without a byte having been decoded. Only a
    ledger that was actually read can answer MISSING for an account not in it.
    """
    ledger = tmp_path / "main.beancount"
    ledger.write_bytes(LEDGER.encode("utf-8"))

    proc = run_ascii_locale(f"""
        from pathlib import Path
        from types import SimpleNamespace
        from istota.money.core.transactions import _ledger_has_posting

        txn = SimpleNamespace(txn_date="2026-01-15", merchant="Café Nordwind")
        found = _ledger_has_posting(
            Path({str(ledger)!r}), txn, "Expenses:Food:Coffee",
        )
        elsewhere = _ledger_has_posting(
            Path({str(ledger)!r}), txn, "Expenses:Nonexistent",
        )
        print("FOUND" if found else "MISSING", "FOUND" if elsewhere else "MISSING")
    """)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["FOUND", "MISSING"]


def test_an_undecodable_ledger_keeps_the_conservative_fallback(tmp_path):
    """``_ledger_has_posting`` promises True on a ledger it cannot read.

    Pinning the encoding is what makes that reachable: a latin-1 byte now
    raises ``UnicodeDecodeError``, which is a ``ValueError`` and so was not
    caught by the ``except OSError`` the docstring's promise rested on. The
    only caller is the reconciliation loop inside ``sync_monarch``, which has
    no handler of its own — the escape aborts the whole sync.
    """
    from types import SimpleNamespace

    from istota.money.core.transactions import _ledger_has_posting

    ledger = tmp_path / "main.beancount"
    ledger.write_bytes(b'2026-01-15 * "Caf\xe9 Nordwind" "Coffee"\n  Expenses:Food  8.50 USD\n')

    txn = SimpleNamespace(txn_date="2026-01-15", merchant="Café Nordwind")
    assert _ledger_has_posting(ledger, txn, "Expenses:Food") is True


def test_dedup_hashes_a_non_ascii_ledger_under_an_ascii_locale(tmp_path):
    """``parse_ledger_transactions`` reads the ledger *and* every staging file
    in ``imports/``; both were unpinned, so both are exercised here."""
    ledger = tmp_path / "main.beancount"
    ledger.write_bytes(LEDGER.encode("utf-8"))
    imports = tmp_path / "imports"
    imports.mkdir()
    (imports / "monarch_sync_1.beancount").write_bytes(
        '2026-02-01 * "Grüner Markt" "Groceries"\n'
        "  Expenses:Food:Groceries  42.00 EUR\n"
        "  Assets:Bank:Checking\n".encode("utf-8")
    )

    proc = run_ascii_locale(f"""
        from pathlib import Path
        from istota.money.core.dedup import (
            compute_transaction_hash, parse_ledger_transactions,
        )

        hashes = parse_ledger_transactions(Path({str(ledger)!r}))
        wanted = {{
            compute_transaction_hash("2026-01-15", 8.50, "Café Nordwind"),
            compute_transaction_hash("2026-02-01", 42.00, "Grüner Markt"),
        }}
        print("BOTH" if wanted <= hashes else f"MISSING {{len(hashes)}}")
    """)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "BOTH"


def test_append_writes_utf8_under_an_ascii_locale(tmp_path):
    """The append path is the writer half: an ASCII locale turns a non-ASCII
    payee into a UnicodeEncodeError out of ``append_to_ledger``, which is the
    Monarch sync losing the transaction rather than mangling it."""
    ledger = tmp_path / "main.beancount"
    ledger.write_bytes(LEDGER.encode("utf-8"))

    proc = run_ascii_locale(f"""
        from pathlib import Path
        from istota.money.core.transactions import append_to_ledger

        entry = (
            '2026-03-02 * "Bäckerei Süd" "Bread"\\n'
            '  Expenses:Food:Groceries  6.00 EUR\\n'
            '  Assets:Bank:Checking'
        )
        append_to_ledger(Path({str(ledger)!r}), [entry])
    """)

    assert proc.returncode == 0, proc.stderr
    assert "Bäckerei Süd" in ledger.read_text(encoding="utf-8")
