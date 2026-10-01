"""`Groups/<id>/GROUP.md`: the seed, the hardened read, the atomic write.

`GROUP.md` goes into every member's prompt and its directory is bound
read-write into every member's sandbox, so the read is held to the terms
`read_channel_memory` is held to (ISSUE-339): containment by equality under
`{mount}/Groups`, no symlink, no FIFO, no oversized file.
"""

import logging
import os
import threading

import pytest

from istota import cli, db, storage
from istota.config import Config


@pytest.fixture
def config(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    return Config(db_path=tmp_path / "istota.db", workspace_path=mount)


def _group_dir(config, group_id="fam"):
    return config.workspace_path / "Groups" / group_id


class TestTheSeed:
    def test_ensure_creates_the_layout_and_seeds_the_charter(self, config):
        assert storage.ensure_group_directories(config, "fam", display_name="The Fam")
        assert (_group_dir(config) / "memories").is_dir()
        text = (_group_dir(config) / "GROUP.md").read_text()
        assert "<!-- Group memory for \"The Fam\"." in text
        assert "every current and future member of this group" in text
        assert "When in doubt it goes\n     in USER.md." in text
        assert "no automatic\n     extraction writes here." in text
        for heading in ("# The Fam", "## Members", "## Conventions", "## Reference"):
            assert f"\n{heading}\n" in text

    def test_ensure_never_overwrites(self, config):
        storage.ensure_group_directories(config, "fam", display_name="Fam")
        (_group_dir(config) / "GROUP.md").write_text("ours\n")
        assert storage.ensure_group_directories(config, "fam", display_name="Fam")
        assert (_group_dir(config) / "GROUP.md").read_text() == "ours\n"

    def test_the_display_name_defaults_to_the_id(self, config):
        storage.ensure_group_directories(config, "fam")
        assert "\n# fam\n" in (_group_dir(config) / "GROUP.md").read_text()

    def test_a_display_name_cannot_close_the_charter_comment(self, config):
        storage.ensure_group_directories(config, "fam", display_name="x -->\n# y")
        text = (_group_dir(config) / "GROUP.md").read_text()
        assert text.count("-->") == 1
        assert "\n# y" not in text

    @pytest.mark.parametrize("group_id", ["", ".", "..", "a/b", "/abs", "UPPER"])
    def test_an_unusable_id_creates_nothing(self, config, group_id):
        assert storage.ensure_group_directories(config, group_id) is False
        assert storage.read_group_memory(config, group_id) is None
        assert not (config.workspace_path / "Groups").exists() or not any(
            (config.workspace_path / "Groups").iterdir()
        )

    def test_no_workspace_is_a_refusal(self, tmp_path):
        config = Config(db_path=tmp_path / "istota.db")
        assert storage.ensure_group_directories(config, "fam") is False
        assert storage.read_group_memory(config, "fam") is None

    def test_the_operator_cli_seeds_on_create(self, config, capsys, monkeypatch):
        db.init_db(config.db_path)
        monkeypatch.setattr(cli, "load_config", lambda path=None: config)

        class Args:
            group_id = "fam"
            kind = "family"
            name = "The Fam"
            config = None

        cli.cmd_group_create(Args())
        assert "\n# The Fam\n" in (_group_dir(config) / "GROUP.md").read_text()


class TestTheRead:
    def test_round_trip(self, config):
        storage.ensure_group_directories(config, "fam")
        assert storage.write_group_memory(config, "fam", "plumber: Ana\n")
        assert storage.read_group_memory(config, "fam") == "plumber: Ana\n"

    def test_absent_or_blank_is_none(self, config):
        assert storage.read_group_memory(config, "fam") is None
        _group_dir(config).mkdir(parents=True)
        (_group_dir(config) / "GROUP.md").write_text("  \n")
        assert storage.read_group_memory(config, "fam") is None

    def test_a_symlinked_group_dir_is_refused(self, config, tmp_path, caplog):
        storage.ensure_group_directories(config, "other")
        (_group_dir(config, "other") / "GROUP.md").write_text("OTHER GROUP\n")
        os.symlink(_group_dir(config, "other"), _group_dir(config, "fam"))
        with caplog.at_level(logging.WARNING, logger="istota.storage"):
            assert storage.read_group_memory(config, "fam") is None
        assert any("group_dir_outside_group_root" in r.getMessage()
                   for r in caplog.records)

    def test_a_symlinked_file_is_refused(self, config, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET\n")
        _group_dir(config).mkdir(parents=True)
        os.symlink(secret, _group_dir(config) / "GROUP.md")
        assert storage.read_group_memory(config, "fam") is None

    def test_a_fifo_is_refused(self, config):
        _group_dir(config).mkdir(parents=True)
        os.mkfifo(_group_dir(config) / "GROUP.md")
        assert storage.read_group_memory(config, "fam") is None

    def test_an_oversized_file_is_refused(self, config, monkeypatch):
        monkeypatch.setattr(storage, "USER_CONFIG_READ_CAP_BYTES", 64)
        _group_dir(config).mkdir(parents=True)
        (_group_dir(config) / "GROUP.md").write_text("x" * 65)
        assert storage.read_group_memory(config, "fam") is None

    def test_past_the_soft_limit_it_loads_and_warns(self, config, caplog):
        _group_dir(config).mkdir(parents=True)
        body = "x" * (storage.GROUP_MEMORY_SOFT_LIMIT_BYTES + 1)
        (_group_dir(config) / "GROUP.md").write_text(body)
        with caplog.at_level(logging.WARNING, logger="istota.storage"):
            assert storage.read_group_memory(config, "fam") == body
        assert any("group_memory_large group=fam" in r.getMessage()
                   for r in caplog.records)

    def test_under_the_soft_limit_it_is_quiet(self, config, caplog):
        storage.ensure_group_directories(config, "fam")
        with caplog.at_level(logging.WARNING, logger="istota.storage"):
            storage.read_group_memory(config, "fam")
        assert not any("group_memory_large" in r.getMessage() for r in caplog.records)


class TestTheWrite:
    def test_a_symlinked_group_dir_is_refused(self, config):
        storage.ensure_group_directories(config, "other")
        os.symlink(_group_dir(config, "other"), _group_dir(config, "fam"))
        assert storage.write_group_memory(config, "fam", "x\n") is False
        assert "x\n" not in (_group_dir(config, "other") / "GROUP.md").read_text()

    def test_a_reader_never_sees_a_partial_file(self, config):
        storage.ensure_group_directories(config, "fam")
        a, b = "A" * 200_000 + "\n", "B" * 200_000 + "\n"
        storage.write_group_memory(config, "fam", a)
        seen: set[str] = set()
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                text = storage.read_group_memory(config, "fam")
                if text is not None:
                    seen.add(text)

        t = threading.Thread(target=reader)
        t.start()
        for i in range(30):
            storage.write_group_memory(config, "fam", a if i % 2 else b)
        stop.set()
        t.join()
        assert seen <= {a, b}
