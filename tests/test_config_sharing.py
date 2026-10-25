"""``[sharing]`` and ``[sharing.ocm]``: defaults, the poll floor and ``private_hosts``."""

from __future__ import annotations

import logging
import textwrap

from istota.config import ICS_POLL_INTERVAL_MIN, Config, load_config
from istota.config_editor import schema


def _load(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    return load_config(path)


class TestDefaults:
    def test_ocm_is_off_and_nothing_is_listed(self):
        sharing = Config().sharing
        assert sharing.ocm.enabled is False
        assert sharing.private_hosts == []
        assert (sharing.ics_poll_interval, sharing.ics_max_bytes,
                sharing.ics_max_feeds_per_user, sharing.events_per_task) == (900, 2097152, 10, 10)
        assert (sharing.ocm.max_pending_per_user, sharing.ocm.pending_days,
                sharing.ocm.max_discovery_fetches_per_minute) == (20, 14, 30)


class TestTheLoader:
    def test_the_values_round_trip(self, tmp_path):
        config = _load(tmp_path, """
            [sharing]
            private_hosts = ["cloud.internal.example"]
            ics_poll_interval = 1800
            events_per_task = 0

            [sharing.ocm]
            enabled = true
            pending_days = 3
        """)
        assert config.sharing.private_hosts == ["cloud.internal.example"]
        assert config.sharing.ics_poll_interval == 1800
        assert config.sharing.events_per_task == 0
        assert config.sharing.ocm.enabled is True
        assert config.sharing.ocm.pending_days == 3

    def test_a_poll_interval_under_the_floor_is_raised_to_it(self, tmp_path):
        config = _load(tmp_path, "[sharing]\nics_poll_interval = 30\n")
        assert config.sharing.ics_poll_interval == ICS_POLL_INTERVAL_MIN

    def test_a_malformed_private_host_is_warned_about_and_kept(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="istota.config")
        config = _load(tmp_path, '[sharing]\nprivate_hosts = ["https://cloud.example"]\n')
        messages = [r.getMessage() for r in caplog.records if "private_hosts" in r.getMessage()]
        assert len(messages) == 1 and "[sharing]" in messages[0]
        assert config.sharing.private_hosts == ["https://cloud.example"]


def test_both_security_switches_need_a_typed_confirmation():
    assert ("sharing", "ocm", "enabled") in schema.RISKY_KEYS
    assert ("sharing", "private_hosts") in schema.RISKY_KEYS
