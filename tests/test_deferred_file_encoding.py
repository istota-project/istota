"""Deferred-op files are UTF-8 on both sides of the sandbox boundary.

The producers run inside a task subprocess whose environment is rebuilt from
scratch (``build_stripped_env``); the consumer runs in the daemon, which
inherits systemd's. Nothing guarantees the two agree on a locale, so both ends
name the encoding explicitly instead of taking ``locale.getencoding()``.

The subprocess tests pin that by running the producer under a genuinely
ASCII locale (``LC_ALL=C`` with PEP 538 coercion and UTF-8 mode both disabled),
which is the shape the mismatch takes on a real deploy.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from istota.scheduler_deferred import _load_deferred_json

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_ascii_locale(script: str) -> subprocess.CompletedProcess:
    """Run ``script`` in a child interpreter whose default encoding is ASCII."""
    env = dict(os.environ)
    env.update({
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
        "PYTHONPATH": str(REPO_ROOT / "src"),
    })
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True, text=True, env=env, timeout=60,
    )


class TestLoaderDecodeFailures:
    """A file the loader cannot decode must not escape into the drain loop."""

    def test_undecodable_file_is_warned_and_dropped(self, tmp_path, caplog):
        path = tmp_path / "task_1_subtasks.json"
        # Latin-1 bytes for `[{"prompt": "café"}]` — a valid JSON document in
        # some encoding, but not decodable as UTF-8.
        path.write_bytes(b'[{"prompt": "caf\xe9"}]')

        result = _load_deferred_json(tmp_path, 1, "subtasks")

        assert result is None
        assert not path.exists(), "an undecodable file is unlinked like a malformed one"
        assert "subtasks" in caplog.text

    def test_undecodable_file_does_not_strand_later_handlers(self, tmp_path):
        """The drain loop calls nine handlers in sequence with no try/except
        between them, so an exception out of the first one silently skips the
        rest. Assert the loader absorbs it rather than raising.
        """
        (tmp_path / "task_2_subtasks.json").write_bytes(b'[{"prompt": "\xff\xfe"}]')
        (tmp_path / "task_2_kv_ops.json").write_text(
            json.dumps([{"op": "set", "namespace": "n", "key": "k", "value": 1}]),
            encoding="utf-8",
        )

        assert _load_deferred_json(tmp_path, 2, "subtasks") is None
        loaded = _load_deferred_json(tmp_path, 2, "kv_ops")
        assert loaded is not None
        assert loaded[1][0]["key"] == "k"

    def test_utf8_payload_reads_back_under_an_ascii_locale(self, tmp_path):
        """The consumer side: the daemon must decode a non-ASCII deferred file
        regardless of the locale it happens to be running under.

        Written as raw UTF-8 rather than through ``json.dumps`` because the
        producer here is the *model's own shell heredoc* (the documented
        subtask idiom in ``skills/tasks/skill.md``), which emits the bytes it
        was given with no ``\\uXXXX`` escaping in between.
        """
        path = tmp_path / "task_3_subtasks.json"
        path.write_bytes('[{"prompt": "résumé the café thread"}]'.encode("utf-8"))

        proc = _run_ascii_locale(f"""
            import json
            from pathlib import Path
            from istota.scheduler_deferred import _load_deferred_json
            loaded = _load_deferred_json(Path({str(tmp_path)!r}), 3, "subtasks")
            print(json.dumps(loaded[1], ensure_ascii=True))
        """)

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)[0]["prompt"] == "résumé the café thread"


@pytest.mark.parametrize("skill_module,defer_call", [
    (
        "istota.skills.kv",
        '_defer_op({"op": "set", "namespace": "n", "key": "k", "value": "café"})',
    ),
    (
        "istota.skills.health",
        '_defer_op({"op": "insert_panel", "lab_name": "Genève"})',
    ),
])
def test_skill_defer_op_writes_utf8_under_an_ascii_locale(
    tmp_path, skill_module, defer_call,
):
    """A skill CLI writing a non-ASCII deferred op must not blow up (or write
    mojibake) because the sandbox handed it a stripped, locale-less env.
    """
    proc = _run_ascii_locale(f"""
        import os
        os.environ["ISTOTA_DEFERRED_DIR"] = {str(tmp_path)!r}
        os.environ["ISTOTA_TASK_ID"] = "9"
        from {skill_module} import _defer_op
        assert {defer_call} is True
    """)

    assert proc.returncode == 0, proc.stderr
    written = list(tmp_path.glob("task_9_*.json"))
    assert len(written) == 1
    # Decodes as UTF-8 and round-trips through the daemon-side loader.
    payload = json.loads(written[0].read_bytes().decode("utf-8"))
    assert payload[0] == json.loads(json.dumps(payload[0]))
    assert any("é" in str(v) or "è" in str(v) for v in payload[0].values())


def test_sent_email_record_writes_utf8_under_an_ascii_locale(tmp_path):
    """``email``'s deferred writer is the one that dumps with
    ``ensure_ascii=False``, so it is the one that actually puts non-ASCII bytes
    on disk rather than ``\\uXXXX`` escapes.
    """
    proc = _run_ascii_locale(f"""
        import os
        os.environ["ISTOTA_DEFERRED_DIR"] = {str(tmp_path)!r}
        os.environ["ISTOTA_TASK_ID"] = "11"
        from istota.skills.email import _write_deferred_sent_email
        _write_deferred_sent_email(
            message_id="<a@b>", to_addr="x@example.com", subject="Rechnung über café",
        )
    """)

    assert proc.returncode == 0, proc.stderr
    path = tmp_path / "task_11_sent_emails.json"
    assert path.exists()
    records = json.loads(path.read_bytes().decode("utf-8"))
    assert records[0]["subject"] == "Rechnung über café"

    loaded = _load_deferred_json(tmp_path, 11, "sent_emails")
    assert loaded is not None
    assert loaded[1][0]["subject"] == "Rechnung über café"
