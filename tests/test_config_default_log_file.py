"""ISSUE-703: the image's default app log, applied where the config names no file."""

from pathlib import Path

import pytest

from istota.config import load_config

DEFAULT = "/data/logs/istota.log"


def _load(tmp_path: Path, monkeypatch, text: str, *, default: str | None = DEFAULT):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
    if default is None:
        monkeypatch.delenv("ISTOTA_DEFAULT_LOG_FILE", raising=False)
    else:
        monkeypatch.setenv("ISTOTA_DEFAULT_LOG_FILE", default)
    return load_config(path).logging


def test_no_logging_section_takes_the_default_file_and_keeps_the_console(tmp_path, monkeypatch):
    logging = _load(tmp_path, monkeypatch, 'bot_name = "Zed"\n')
    assert (logging.output, logging.file) == ("both", DEFAULT)


def test_an_explicit_console_with_no_file_still_gains_the_file(tmp_path, monkeypatch):
    """An imported config spells out the dataclass default; it named no file."""
    logging = _load(tmp_path, monkeypatch, '[logging]\nlevel = "INFO"\noutput = "console"\n')
    assert (logging.output, logging.file) == ("both", DEFAULT)


@pytest.mark.parametrize("text, expected", [
    ('[logging]\noutput = "console"\nfile = ""\n', ("console", "")),
    ('[logging]\noutput = "file"\nfile = "/data/elsewhere.log"\n', ("file", "/data/elsewhere.log")),
])
def test_a_config_naming_a_file_keeps_its_own_choice(tmp_path, monkeypatch, text, expected):
    logging = _load(tmp_path, monkeypatch, text)
    assert (logging.output, logging.file) == expected


def test_output_file_alone_keeps_file_only(tmp_path, monkeypatch):
    logging = _load(tmp_path, monkeypatch, '[logging]\noutput = "file"\n')
    assert (logging.output, logging.file) == ("file", DEFAULT)


def test_outside_the_image_nothing_changes(tmp_path, monkeypatch):
    logging = _load(tmp_path, monkeypatch, 'bot_name = "Zed"\n', default=None)
    assert (logging.output, logging.file) == ("console", "")
