"""``[wordpress]``: the loader's check on ``private_hosts`` (ISSUE-591).

The skill compares a site's host against this list by exact name, so an entry
written as a URL, with a port or as a wildcard matches nothing and is refused by
nothing. The operator sees the same "resolves to a private address" refusal they
were trying to lift and no reason why, which is the typo-that-did-nothing shape
``config_mapper`` warns about for keys. The loader warns; it does not drop the
entry or refuse to boot, since a malformed entry already grants nothing.
"""

from __future__ import annotations

import logging
import re
import textwrap

import pytest

from istota.config import WORDPRESS_PRIVATE_HOST_PATTERN, load_config


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    return load_config(path)


GOOD = ["wp.internal.example.com", "Wp.Example.Com.", "localhost", "10.0.0.5", "::1", "fd00::5"]
BAD = [
    "https://wp.internal.example.com",
    "wp.internal.example.com/",
    "wp.internal.example.com:8443",
    "*.example.com",
    "user@wp.internal.example.com",
    "two words",
]


class TestTheLoader:
    def test_the_values_round_trip(self, tmp_path):
        config = _load(tmp_path, """
            [wordpress]
            private_hosts = ["wp.internal.example.com"]
            max_upload_mb = 40
        """)
        assert config.wordpress.private_hosts == ["wp.internal.example.com"]
        assert config.wordpress.max_upload_mb == 40

    def test_well_formed_entries_load_without_a_warning(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        hosts = ", ".join(f'"{h}"' for h in GOOD)
        _load(tmp_path, f"[wordpress]\nprivate_hosts = [{hosts}]\n")
        assert not [r for r in caplog.records if "private_hosts" in r.getMessage()]

    @pytest.mark.parametrize("entry", BAD)
    def test_a_malformed_entry_is_named_in_a_warning(self, tmp_path, caplog, entry):
        caplog.set_level(logging.WARNING, logger="istota.config")
        config = _load(tmp_path, f'[wordpress]\nprivate_hosts = ["ok.example.com", "{entry}"]\n')
        messages = [r.getMessage() for r in caplog.records if "private_hosts" in r.getMessage()]
        assert len(messages) == 1
        assert repr(entry) in messages[0]
        assert "ok.example.com" not in messages[0]
        # Left on the field: it grants nothing, and the admin view shows what was written.
        assert entry in config.wordpress.private_hosts


class TestThePattern:
    @pytest.mark.parametrize("entry", GOOD)
    def test_the_pattern_accepts(self, entry):
        assert re.match(WORDPRESS_PRIVATE_HOST_PATTERN, entry)

    @pytest.mark.parametrize("entry", BAD)
    def test_the_pattern_refuses(self, entry):
        assert not re.match(WORDPRESS_PRIVATE_HOST_PATTERN, entry)
