"""``docker/istota/config-diff.py`` — the boot's drift report, by key.

The entrypoint calls it twice: after a re-render, to say what this boot changed,
and under ``ISTOTA_CONFIG_RENDER=preserve``, to say what this boot is ignoring.
ISSUE-368's own conclusion was that the class of failure is the silence, so this
is the half of the fix that survives whichever mode a deployment runs in.

Two properties carry real weight. **It never prints a value that must not be
logged**, because the destination is the container log and ``config.toml`` holds
the bot's app password, the OAuth2 client secret, the forge tokens and the Talk
room tokens. And **it never fails the boot**, because it runs on the start-up
path of a deployment that is otherwise fine.

The first property is where this file earned its keep, in the wrong direction:
its first draft asserted that ``users.a.log_channel`` was *not* a credential,
which is where the room token lives — so the leak was covered by a passing test
saying it was correct, and the only redaction case with coverage was
``imap_password``, whose leaf the substring rule cannot get wrong. That is the
"probe whose success is indistinguishable from a no-op" failure
``.claude/rules/testbed.md`` describes, and the cases below are picked so that
the rule has to do real work to satisfy them.

Loaded by path rather than imported: the file has no ``.py``-importable name in
a package, and it deliberately ships as a standalone script beside
``entrypoint.sh`` so a container can run it with the system ``python3``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "docker" / "istota" / "config-diff.py"


def _load():
    spec = importlib.util.spec_from_file_location("istota_config_diff", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config_diff = _load()


def describe(old: dict, new: dict) -> list[str]:
    return config_diff.describe(old, new, label_old="before", label_new="after")


class TestFlattening:
    def test_nested_tables_become_dotted_keys(self):
        flat = config_diff.flatten({"brain": {"native": {"model": "glm-5.2"}}})
        assert flat == {"brain.native.model": "glm-5.2"}

    def test_an_array_of_tables_is_indexed(self):
        flat = config_diff.flatten(
            {"users": {"a": {"resources": [{"type": "feeds"}, {"type": "money"}]}}}
        )
        assert flat == {
            "users.a.resources[0].type": "feeds",
            "users.a.resources[1].type": "money",
        }

    def test_a_scalar_array_stays_whole(self):
        """`email_addresses = ["a@x", "b@x"]` reads better as one value."""
        flat = config_diff.flatten({"users": {"a": {"email_addresses": ["a@x", "b@x"]}}})
        assert flat == {"users.a.email_addresses": ["a@x", "b@x"]}


class TestWhatMustNotBePrinted:
    @pytest.mark.parametrize(
        "key",
        [
            "nextcloud.app_password",
            "web.oauth2_client_secret",
            "web.session_secret_key",
            "users.a.resources[0].ingest_token",
            "developer.gitlab_token",
            "web.map.api_key",
            "users.a.monarch_password",
        ],
    )
    def test_credential_shaped_keys_are_recognised(self, key):
        assert config_diff.is_sensitive(key)

    @pytest.mark.parametrize("key", ["users.a.log_channel", "users.a.alerts_channel"])
    def test_the_talk_room_tokens_are_recognised(self, key):
        """These hold a bearer token and match no credential word at all.

        `render-config.sh` writes the token `create_group_room` returned
        straight into `log_channel` / `alerts_channel`, and whoever holds a Talk
        room token can read and post in that room. The first draft of this
        module printed both in full while its docstring said it did not, and
        this file asserted that was correct — so the leak had a test holding it
        in place.
        """
        assert config_diff.is_sensitive(key)

    def test_the_email_address_is_withheld(self):
        assert config_diff.is_sensitive("users.a.email_addresses")

    def test_a_boolean_that_merely_contains_channel_is_still_printed(self):
        """`scheduler.log_channel_show_skills` is why the rule is exact-leaf.

        Adding `channel` to the substring markers would have withheld this too,
        and it is an ordinary setting whose flips are worth seeing.
        """
        assert not config_diff.is_sensitive("scheduler.log_channel_show_skills")

    @pytest.mark.parametrize(
        "key",
        [
            "brain.native.model",
            "web.oauth2_client_id",
            "nextcloud.url",
            "logging.rotate",
        ],
    )
    def test_ordinary_keys_are_not(self, key):
        assert not config_diff.is_sensitive(key)


class TestTheReport:
    def test_agreement_reports_nothing(self):
        assert describe({"a.b": 1}, {"a.b": 1}) == []

    def test_a_changed_value_is_named_with_both_sides(self):
        (line,) = describe({"brain.native.model": "old"}, {"brain.native.model": "new"})
        assert "brain.native.model" in line
        assert "old" in line and "new" in line

    def test_a_changed_credential_is_named_without_either_side(self):
        (line,) = describe(
            {"nextcloud.app_password": "hunter2"},
            {"nextcloud.app_password": "correct-horse"},
        )
        assert "nextcloud.app_password" in line
        assert "hunter2" not in line and "correct-horse" not in line
        assert config_diff.REDACTED in line

    def test_an_added_credential_is_named_without_its_value(self):
        (line,) = describe({}, {"developer.gitlab_token": "glpat-secret"})
        assert "developer.gitlab_token" in line
        assert "glpat-secret" not in line

    def test_a_removed_credential_is_named_without_its_value(self):
        (line,) = describe({"developer.gitlab_token": "glpat-secret"}, {})
        assert "developer.gitlab_token" in line
        assert "glpat-secret" not in line

    def test_a_long_value_is_truncated(self):
        (line,) = describe({"a.b": "x"}, {"a.b": "y" * 500})
        assert len(line) < 300
        assert "..." in line

    def test_a_truncated_string_is_still_a_balanced_literal(self):
        """Truncating the `repr` cut inside the quotes and printed `'aaa...`.

        Which reads as a malformed value rather than an elided one, on a log
        line whose whole job is to be read by a person.
        """
        (line,) = describe({"a.b": "x"}, {"a.b": "y" * 500})
        rendered = line.split("-> ")[1].split(" (after)")[0]
        assert rendered.startswith("'") and rendered.endswith("'"), rendered

    def test_keys_are_reported_in_a_stable_order(self):
        lines = describe({}, {"z.a": 1, "a.z": 2, "m.m": 3})
        assert [line.split()[1] for line in lines] == ["a.z", "m.m", "z.a"]


class TestItNeverFailsTheBoot:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_missing_file_exits_zero_and_says_why(self, tmp_path):
        present = tmp_path / "a.toml"
        present.write_text('x = 1\n')

        result = self._run(str(present), str(tmp_path / "absent.toml"))

        assert result.returncode == 0
        assert "does not exist" in result.stderr

    def test_unparseable_toml_exits_zero_and_says_why(self, tmp_path):
        good = tmp_path / "a.toml"
        good.write_text('x = 1\n')
        bad = tmp_path / "b.toml"
        bad.write_text('x = = =\n')

        result = self._run(str(good), str(bad))

        assert result.returncode == 0
        assert "not valid TOML" in result.stderr

    def test_a_real_difference_is_printed_and_still_exits_zero(self, tmp_path):
        old = tmp_path / "a.toml"
        old.write_text('[brain.native]\nmodel = "old"\n')
        new = tmp_path / "b.toml"
        new.write_text('[brain.native]\nmodel = "new"\n')

        result = self._run(str(old), str(new), "--heading", "config.toml changed")

        assert result.returncode == 0
        assert "config.toml changed (1 key(s))" in result.stdout
        assert "brain.native.model" in result.stdout

    def test_identical_files_print_nothing(self, tmp_path):
        for name in ("a.toml", "b.toml"):
            (tmp_path / name).write_text('[brain.native]\nmodel = "same"\n')

        result = self._run(str(tmp_path / "a.toml"), str(tmp_path / "b.toml"))

        assert result.returncode == 0
        assert result.stdout == ""


class TestItShipsInTheImage:
    def test_the_dockerfile_copies_it_beside_the_entrypoint(self):
        """The entrypoint resolves it relative to its own directory.

        A guard there makes a missing reporter cost the report rather than the
        boot, which is right — and is exactly why nothing else would notice the
        Dockerfile forgetting the file.
        """
        dockerfile = (REPO / "docker" / "istota" / "Dockerfile").read_text()
        assert "COPY docker/istota/config-diff.py /config-diff.py" in dockerfile
