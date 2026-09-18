"""The KDBX vault reader: what it accepts, what it refuses, and what it logs.

Every fixture here is a **real** KDBX file built by `pykeepass` in `tmp_path`,
not a recorded blob and not a mock. The module's whole job is to be right about
a third-party file format and about the failure classes that format produces, so
a stand-in for the library would assert this suite's idea of pykeepass rather
than pykeepass.

Three of the assertions are about a boundary rather than about the mapping — the
symlink, the FIFO and the size cap — and all three are inherited from
`skills._loader.read_overlay_bytes` rather than implemented in `secrets_vault`.
That is exactly the shape `.claude/rules/testbed.md` warns about: they would
pass just as happily against a `path.read_bytes()` that had none of the
hardening, because the *happy* path is identical. They were driven with the
reader swapped for `path.read_bytes()`: the symlink and cap cases went red, and
the FIFO case did not fail at all — it hung, which is the production failure
exactly. The commit adding this file records that in its body. Read the three as
pinning which reader is used, not as proof the reader works.

The values in the fixtures are strings invented here. Nothing in this file is a
credential, and `test_no_log_record_carries_a_value` is what keeps it that way
in the other direction — it sweeps every record the parse emitted for any of
them.

`TestApply` and `TestEligibility` are the writing half, and they run against a
real temp database with a real `ISTOTA_SECRET_KEY`. Two of them go through a
real KDBX as well, because what they assert is a property of the seam between
the parse and the apply — an entry the parse skipped is still an entry the group
contains, which is what stops a fat-fingered password field deleting a
credential.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import sqlite3
import sys
import textwrap

from pathlib import Path
from unittest import mock

import pytest

from istota import secret_schema
from istota import secrets_vault as secrets_vault_module
from istota.config import UserConfig, load_config
from istota.secrets_vault import (
    DAEMON_WRITTEN_SERVICES,
    SKIP_DUPLICATE_NAME,
    SKIP_INELIGIBLE_SERVICE,
    SKIP_OVERSIZE_VALUE,
    SKIP_RESERVED_SERVICE,
    SKIP_UNUSABLE_NAME,
    VAULT_READ_CAP_BYTES,
    VaultCorrupt,
    VaultLibraryMissing,
    VaultLocked,
    VaultMissing,
    VaultRead,
    VaultUnreadable,
    eligible_services,
    parse_vault,
    read_vault_bytes,
    service_refusal,
)
from istota.skills._loader import (
    OVERLAY_IS_A_SYMLINK,
    OVERLAY_NOT_A_REGULAR_FILE,
    OVERLAY_UNREADABLY_LARGE,
)
from tests.support.drift import source_of

REPO = Path(__file__).resolve().parent.parent

PASSPHRASE = "fixture-passphrase-not-a-real-one"

# Distinctive enough that `test_no_log_record_carries_a_value` finding one in a
# log record is unambiguous, and shaped like the real thing so the mapping
# assertions are about plausible input.
API_KEY_VALUE = "ak-vault-fixture-alpha"
BASE_URL_VALUE = "https://karakeep.example.com"
TOPIC_VALUE = "ntfy-fixture-topic"
USERNAME_VALUE = "un-vault-fixture-delta"
CUSTOM_VALUE = "cf-vault-fixture-epsilon"
NOTES_VALUE = "nt-vault-fixture-zeta"


def _ours(caplog):
    """Every message this module logged, and nothing anyone else did.

    `caplog.records` non-empty proves nothing on its own: pykeepass emits five
    DEBUG records during an ordinary parse, so a sweep over all of them was
    satisfied by a third party's output and stayed green with all three
    warnings deleted.
    """
    return [r.getMessage() for r in caplog.records if r.name == "istota.secrets_vault"]


def _new_db(tmp_path, *, password=PASSPHRASE, name="vault.kdbx"):
    """An empty KDBX at `tmp_path/name`, and the open handle on it.

    Imported inside the helper rather than at module scope: `pykeepass` pulls
    `lxml`, `argon2-cffi` and `pycryptodomex`, and nothing in this repository
    should pay that at collection — which is the same rule
    `secrets_vault.parse_vault` follows and `test_the_library_import_is_function_scoped`
    holds it to.
    """
    from pykeepass import create_database

    path = tmp_path / name
    return create_database(str(path), password=password), path


def _standard_vault(tmp_path, *, password=PASSPHRASE):
    """`istota/karakeep/{base_url,api_key}` and `istota/ntfy/topic`."""
    kp, path = _new_db(tmp_path, password=password)
    root = kp.add_group(kp.root_group, "istota")
    karakeep = kp.add_group(root, "karakeep")
    kp.add_entry(karakeep, "base_url", "", BASE_URL_VALUE)
    kp.add_entry(karakeep, "api_key", "", API_KEY_VALUE)
    ntfy = kp.add_group(root, "ntfy")
    kp.add_entry(ntfy, "topic", "", TOPIC_VALUE)
    kp.save()
    return kp, path


def _read(path, *, password=PASSPHRASE):
    data, digest = read_vault_bytes(path)
    return parse_vault(data, password), digest


def _load_config_text(tmp_path, body: str):
    """`load_config` over a config file written from `body`.

    A real load rather than a hand-built `Config`, because what the config-side
    tests below assert is that the loader *reads* these keys — a dataclass with
    a default and no line in the loader is the defect class they exist for.
    """
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    return load_config(path)


#: The names an "extra not installed" simulation has to make unimportable.
#: `pykeepass.pykeepass` is in the list because it is the module that actually
#: *raises*, and leaving it cached is what makes the naive teardown poison the
#: worker — see `_library_absent`.
_LIBRARY_MODULES = ("pykeepass", "pykeepass.exceptions", "pykeepass.pykeepass")


@contextlib.contextmanager
def _library_absent():
    """`import pykeepass` raises ImportError for the body, and only the body.

    A `None` in `sys.modules` is what the import machinery reads as "this module
    is known to be absent". **Saving and restoring is the whole of this helper**,
    and the obvious `del sys.modules[name]` teardown is a live defect rather than
    an untidiness: it drops the *class objects* along with the modules, so the
    next `from pykeepass.exceptions import CredentialsError` binds a freshly
    constructed class while whichever submodule stayed cached goes on raising the
    original. `except CredentialsError` then misses, and a wrong passphrase
    arrives as `VaultCorrupt` — the module's own catch-all silently reclassifying
    it, with no import error anywhere to say why. Measured: the file stays green
    only because collection order happens to put the locked tests first, and
    reversing two node ids on one `-n0` run turns `test_a_wrong_passphrase_reads_as_locked`
    red. That is the shape the project memory records as "a new file green in
    isolation turns a *different* file red under xdist".
    """
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in _LIBRARY_MODULES}
    for name in _LIBRARY_MODULES:
        sys.modules[name] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class TestRead:
    """`read_vault_bytes` and `parse_vault` against real KDBX files."""

    # ---- the names -----------------------------------------------------

    def test_the_names_a_flat_entry_produces(self, tmp_path):
        """An entry sitting directly under `istota/` is a name of its own.

        The path below the root group is the whole of the derivation, so the
        shortest path is one segment — and that shape is the one the previous
        design threw away, because it had no service to file it under."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "GitHub PAT", "", API_KEY_VALUE)
        kp.save()

        read, digest = _read(path)

        assert read.services == {"github_pat": API_KEY_VALUE}
        assert read.digest == digest

    def test_the_names_a_nested_entry_produces(self, tmp_path):
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "Home Assistant"), "Token", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"home_assistant_token": API_KEY_VALUE}

    def test_the_names_a_three_level_nested_entry_produces(self, tmp_path):
        """Every group between the root and the entry is a segment, in order."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        work = kp.add_group(root, "Work")
        aws = kp.add_group(work, "AWS")
        kp.add_entry(kp.add_group(aws, "prod"), "Access Key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"work_aws_prod_access_key": API_KEY_VALUE}

    def test_the_standard_vault_reads_as_four_names(self, tmp_path):
        """The fixture the rest of the file is built on, in the new vocabulary.

        Its entries are laid out the way the old service mapping wanted them,
        which is now simply a two-segment path and nothing special."""
        _, path = _standard_vault(tmp_path)

        read, digest = _read(path)

        assert read.services == {
            "karakeep_base_url": BASE_URL_VALUE,
            "karakeep_api_key": API_KEY_VALUE,
            "ntfy_topic": TOPIC_VALUE,
        }
        assert read.digest == digest

    def test_the_digest_is_the_sha256_of_the_file_bytes(self, tmp_path):
        """Both halves compute it, because §7 hashes to decide whether to parse
        at all and `VaultRead` carries it onward. They must agree."""
        _, path = _standard_vault(tmp_path)

        data, digest = read_vault_bytes(path)

        assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
        assert data == path.read_bytes()
        assert parse_vault(data, PASSPHRASE).digest == digest

    def test_username_url_and_a_custom_field_each_take_a_suffix(self, tmp_path):
        """One entry, four credentials — the case the previous design could not
        express at all, and the reason custom string fields are read now."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        entry = kp.add_entry(
            root, "Acme", USERNAME_VALUE, API_KEY_VALUE, url=BASE_URL_VALUE
        )
        entry.set_custom_property("API Key", CUSTOM_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {
            "acme": API_KEY_VALUE,
            "acme_username": USERNAME_VALUE,
            "acme_url": BASE_URL_VALUE,
            "acme_api_key": CUSTOM_VALUE,
        }

    def test_a_note_is_not_read(self, tmp_path):
        """Notes are free text and frequently hold something other than a
        credential. A user who wants one read puts it in a custom field."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", "", API_KEY_VALUE, notes=NOTES_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"acme": API_KEY_VALUE}
        assert NOTES_VALUE not in read.services.values()

    def test_values_are_stripped_of_surrounding_whitespace(self, tmp_path):
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", "", f"  {API_KEY_VALUE}\n")
        kp.save()

        read, _ = _read(path)

        assert read.services == {"acme": API_KEY_VALUE}

    def test_an_entry_with_only_a_username_is_legitimate(self, tmp_path, caplog):
        """One name, no warning. Every entry has an empty URL field and most
        have an empty username, so an empty field is the ordinary case rather
        than a mistake — which is why it is silent."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", USERNAME_VALUE, "")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {"acme_username": USERNAME_VALUE}
        assert _ours(caplog) == []

    # ---- what an empty field holds back --------------------------------

    def test_an_empty_field_produces_no_name_and_holds_it(self, tmp_path):
        """The one piece of state the applying half cannot derive.

        The namespace sweep deletes every stored name the read does not hold,
        so a blanked password field — the fat-finger case — would delete the
        credential it was meant to change. `held` is what stops that, and it
        covers every field of the entry rather than the password alone."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", "", "")
        kp.add_entry(root, "Other", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"other": API_KEY_VALUE}
        assert {"acme", "acme_username", "acme_url"} <= read.held
        # Deleting a credential means deleting the entry, so a name no entry
        # produced at all is neither written nor held.
        assert "gone" not in read.held

    def test_a_whitespace_only_value_is_held_too(self, tmp_path):
        """Values are stripped before the emptiness test, or a value that is a
        stray newline lands in the table as an empty string — which
        `set_secret` reads as a deletion."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", "", "   \n ")
        kp.save()

        read, _ = _read(path)

        assert read.services == {}
        assert "acme" in read.held

    def test_an_entry_with_nothing_in_it_says_so_once(self, tmp_path, caplog):
        """The old empty-password warning, kept for the case that is still a
        mistake: an entry that sets nothing at all."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", "", "")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {}
        said = _ours(caplog)
        assert len(said) == 1, said
        assert "acme" in said[0].lower() and "delete the entry" in said[0]

    def test_a_value_over_the_byte_cap_is_skipped_and_held(self, tmp_path, caplog):
        """Refusing the new value *and* destroying the old one is a combination
        no rule here intends, so an oversize value takes the same hold an empty
        one does. The warning names the size and never the value."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        oversize = "z" * (secrets_vault_module.VAULT_MAX_VALUE_BYTES + 1)
        kp.add_entry(root, "Acme", "", oversize)
        kp.add_entry(root, "Other", "", API_KEY_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {"other": API_KEY_VALUE}
        assert "acme" in read.held
        assert ("acme", SKIP_OVERSIZE_VALUE) in read.skipped
        said = _ours(caplog)
        assert len(said) == 1, said
        assert "acme" in said[0] and str(len(oversize)) in said[0]
        assert oversize not in said[0]

    def test_a_value_at_the_byte_cap_is_read(self, tmp_path):
        """The boundary, in bytes rather than characters: a multi-byte value is
        measured as UTF-8, which is what the column stores."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        cap = secrets_vault_module.VAULT_MAX_VALUE_BYTES
        kp.add_entry(root, "Acme", "", "z" * cap)
        kp.add_entry(root, "Wide", "", "é" * (cap // 2))
        kp.add_entry(root, "Over", "", "é" * (cap // 2 + 1))
        kp.save()

        read, _ = _read(path)

        assert set(read.services) == {"acme", "wide"}
        assert "over" in read.held

    # ---- one name, one producer ----------------------------------------

    def test_two_entries_that_produce_one_name_are_both_absent(
        self, tmp_path, caplog
    ):
        """The derivation flattens, so `istota/aws/key` and `istota/AWS Key`
        are the same name. Choosing either silently would make which credential
        is live depend on the XML order.

        Not *held*, deliberately: a collided name is absent, and therefore
        deleted if it was stored before, because istota cannot say which of the
        two values it is."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "aws"), "key", "", "ak-from-the-group")
        kp.add_entry(root, "AWS Key", "", "ak-from-the-flat-entry")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert "aws_key" not in read.services
        assert "aws_key" not in read.held
        assert ("aws_key", SKIP_DUPLICATE_NAME) in read.skipped
        said = [m for m in _ours(caplog) if "aws_key" in m]
        assert len(said) == 1, said

    def test_the_group_form_alone_is_present(self, tmp_path):
        """The first half of the collision's control: with only one producer,
        the name is there — so the test above is about the collision rather
        than about either entry being unreadable."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "aws"), "key", "", "ak-from-the-group")
        kp.save()

        read, _ = _read(path)

        assert read.services["aws_key"] == "ak-from-the-group"

    def test_the_flat_form_alone_is_present(self, tmp_path):
        """The other half."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "AWS Key", "", "ak-from-the-flat-entry")
        kp.save()

        read, _ = _read(path)

        assert read.services["aws_key"] == "ak-from-the-flat-entry"

    def test_a_custom_field_can_collide_with_a_standard_one(self, tmp_path):
        """`custom_properties` excludes the *exactly* reserved field names, so a
        custom `Url` is a custom field that slugs onto the standard URL field's
        name — which is the collision rule working rather than a case to
        special-case. (`URL` itself is refused by pykeepass, so a file cannot
        hold both spellings of the reserved name.)"""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        entry = kp.add_entry(root, "Acme", "", API_KEY_VALUE, url=BASE_URL_VALUE)
        entry.set_custom_property("Url", "https://collides.invalid")
        kp.save()

        read, _ = _read(path)

        assert read.services == {"acme": API_KEY_VALUE}
        assert ("acme_url", SKIP_DUPLICATE_NAME) in read.skipped

    def test_an_empty_field_still_collides(self, tmp_path):
        """A name is produced by the *field*, not by its value, so an empty one
        collides exactly as a filled one does. Otherwise emptying one of two
        colliding entries would silently promote the other, which is the same
        XML-order dependence one step removed."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "aws"), "key", "", "")
        kp.add_entry(root, "AWS Key", "", "ak-from-the-flat-entry")
        kp.save()

        read, _ = _read(path)

        assert "aws_key" not in read.services
        assert ("aws_key", SKIP_DUPLICATE_NAME) in read.skipped

    def test_two_spellings_of_one_group_collide(self, tmp_path):
        """Case folding happens inside the name, so `Karakeep` and `karakeep`
        are one namespace rather than two."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "Karakeep"), "api_key", "", "ak-upper")
        kp.add_entry(kp.add_group(root, "karakeep"), "api_key", "", "ak-lower")
        kp.add_entry(kp.add_group(root, "ntfy"), "topic", "", TOPIC_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"ntfy_topic": TOPIC_VALUE}

    def test_an_edited_entrys_history_is_not_a_collision(self, tmp_path):
        """The expensive one if `Group.entries` ever stops meaning current
        entries: a KDBX keeps previous versions of an edited entry as `History`
        children, so every credential the user has ever changed would collide
        with itself — silently, and for the names they touch most."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        entry = kp.add_entry(root, "Acme", "", "ak-the-old-value")
        entry.save_history()
        entry.password = API_KEY_VALUE
        kp.save()

        read, _ = _read(path)

        assert read.services == {"acme": API_KEY_VALUE}

    # ---- names that cannot be used --------------------------------------

    def test_a_title_that_slugs_to_nothing_is_skipped(self, tmp_path, caplog):
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "!!!", "", API_KEY_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {}
        assert read.skipped == (("!!!", SKIP_UNUSABLE_NAME),)
        assert any("!!!" in m for m in _ours(caplog)), _ours(caplog)

    def test_a_name_starting_with_a_digit_is_skipped(self, tmp_path):
        """The name is what a person types in a shell and what a script may
        export as a variable."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "9lives", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}
        assert read.skipped == (("9lives", SKIP_UNUSABLE_NAME),)

    def test_a_name_over_the_length_cap_is_skipped(self, tmp_path):
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        cap = secrets_vault_module.VAULT_NAME_MAX_CHARS
        kp.add_entry(root, "a" * cap, "", API_KEY_VALUE)
        kp.add_entry(root, "b" * (cap + 1), "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert set(read.services) == {"a" * cap}
        assert read.skipped == (("b" * (cap + 1), SKIP_UNUSABLE_NAME),)

    def test_a_group_segment_that_slugs_to_nothing_refuses_the_whole_name(
        self, tmp_path
    ):
        """The trailing-underscore trap, and why an empty segment is refused
        rather than dropped.

        `^[a-z][a-z0-9_]{0,63}$` admits a trailing underscore, so joining an
        empty segment would answer `aws_` for `istota/aws/!!!` — a name the user
        never wrote, which every other junk-titled entry in that group produces
        identically."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "   "), "key", "", API_KEY_VALUE)
        kp.add_entry(kp.add_group(root, "aws"), "!!!", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}
        assert not any(name.endswith("_") for name in read.services)
        assert {reason for _name, reason in read.skipped} == {SKIP_UNUSABLE_NAME}

    def test_a_custom_field_whose_name_is_unusable_is_skipped_alone(self, tmp_path):
        """One field refused does not cost the entry its other names."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        entry = kp.add_entry(root, "Acme", "", API_KEY_VALUE)
        entry.set_custom_property("???", CUSTOM_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"acme": API_KEY_VALUE}
        assert read.skipped == (("Acme/???", SKIP_UNUSABLE_NAME),)

    def test_an_untitled_entry_is_skipped(self, tmp_path):
        """An entry with no title names nothing. It must not raise on the way
        past, which a bare `title.strip()` would."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "", "", API_KEY_VALUE)
        kp.add_entry(root, "Other", "", BASE_URL_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"other": BASE_URL_VALUE}

    def test_slug_name_is_pure(self):
        """The rules on their own, so the walk's tests are about the walk.

        Every call site warns for itself, because only the caller knows which
        entry and which field produced the name."""
        from istota.secrets_vault import slug_name

        assert slug_name(["GitHub PAT"]) == "github_pat"
        assert slug_name(["Home Assistant", "Token"]) == "home_assistant_token"
        assert slug_name(["  spaced  out  "]) == "spaced_out"
        assert slug_name(["a-b_c.d"]) == "a_b_c_d"
        # No transliteration is attempted: what survives is the ASCII, which
        # is the rule being literal rather than clever. Two names that collapse
        # to the same thing collide, which is where that is answered.
        assert slug_name(["ÉTÉ"]) == "t"
        assert slug_name([]) is None
        assert slug_name([""]) is None
        assert slug_name(["aws", ""]) is None
        assert slug_name(["9lives"]) is None
        assert slug_name(["x" * 65]) is None
        assert slug_name(["x" * 64]) == "x" * 64

    # ---- the caps --------------------------------------------------------

    def test_the_shipped_caps(self):
        """Module constants rather than settings: an operator who needs a
        different *parse* cap is a signal to revisit the design."""
        assert secrets_vault_module.VAULT_MAX_DEPTH == 8
        assert secrets_vault_module.VAULT_MAX_ENTRIES == 512
        assert secrets_vault_module.VAULT_MAX_NAMES == 1024
        assert secrets_vault_module.VAULT_MAX_VALUE_BYTES == 8192
        assert secrets_vault_module.VAULT_NAME_MAX_CHARS == 64

    def test_groups_deeper_than_the_cap_are_not_walked(
        self, tmp_path, monkeypatch, caplog
    ):
        """The cap is counted in subgroup levels below the root group, so a
        cap of 1 admits `istota/<group>/<entry>` and nothing below it."""
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_DEPTH", 1)
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        shallow = kp.add_group(root, "aws")
        kp.add_entry(shallow, "key", "", API_KEY_VALUE)
        kp.add_entry(kp.add_group(shallow, "deeper"), "key", "", BASE_URL_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {"aws_key": API_KEY_VALUE}
        assert any("deeper than 1" in m for m in _ours(caplog)), _ours(caplog)

    def test_the_entry_cap_stops_the_walk_and_applies_what_it_read(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_ENTRIES", 2)
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        for index in range(4):
            kp.add_entry(root, f"entry{index}", "", f"value-{index}")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert set(read.services) == {"entry0", "entry1"}
        assert any("entry cap" in m for m in _ours(caplog)), _ours(caplog)

    def test_the_name_cap_stops_the_walk_and_applies_what_it_read(
        self, tmp_path, monkeypatch, caplog
    ):
        """Names are counted as they are produced, so the cap can fall inside
        an entry — the password, the username and the URL are three."""
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_NAMES", 2)
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "Acme", USERNAME_VALUE, API_KEY_VALUE, url=BASE_URL_VALUE)
        kp.add_entry(root, "Other", "", TOPIC_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {"acme": API_KEY_VALUE, "acme_username": USERNAME_VALUE}
        assert any("name cap" in m for m in _ours(caplog)), _ours(caplog)

    # ---- what the walk excludes -----------------------------------------

    def test_a_vault_with_no_istota_group_reads_as_empty_and_says_so(
        self, tmp_path, caplog
    ):
        """"I emptied my vault" and "I mistyped the group name" reach the same
        state, and only one of them was intended — so the absence is said out
        loud even though it is not an error."""
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.add_group(kp.root_group, "Personal"), "key", "", API_KEY_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {}
        assert any("istota" in m for m in _ours(caplog)), _ours(caplog)

    def test_an_istota_group_nested_in_another_group_is_not_read(self, tmp_path):
        """Only the root-level group is the consent boundary. A nested one is
        somebody else's folder that happens to share a name."""
        kp, path = _new_db(tmp_path)
        outer = kp.add_group(kp.root_group, "Personal")
        kp.add_entry(kp.add_group(outer, "istota"), "key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}

    def test_the_top_level_group_name_is_not_folded(self, tmp_path):
        """`Istota/` is not the vault's group. Matched exactly, so the file
        either is shared or is not, with nothing in between."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "Istota")
        kp.add_entry(kp.add_group(root, "karakeep"), "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}

    def test_an_empty_istota_group_reads_as_empty_without_a_warning(
        self, tmp_path, caplog
    ):
        """The group is there, so the absent-group warning must not fire: the
        two states have different remedies and only one of them is a typo."""
        kp, path = _new_db(tmp_path)
        kp.add_group(kp.root_group, "istota")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert read.services == {} and read.held == frozenset()
        assert _ours(caplog) == []

    # ---- the recycle bin -----------------------------------------------

    def test_a_trashed_entry_is_excluded(self, tmp_path):
        """KeePassXC moves a deleted entry into the recycle bin rather than
        removing it, so a credential the user deleted is still in the file."""
        kp, path = _standard_vault(tmp_path)
        entry = kp.find_entries(title="api_key", first=True)
        kp.trash_entry(entry)
        kp.save()

        read, _ = _read(path)

        assert "karakeep_api_key" not in read.services
        assert "karakeep_api_key" not in read.held
        assert read.services["karakeep_base_url"] == BASE_URL_VALUE

    def test_a_trashed_istota_group_is_excluded(self, tmp_path):
        """Trashing the whole group moves it *under* the recycle bin, where a
        search by name would still find it. The walk starts at the root group's
        own children, so it does not."""
        kp, path = _standard_vault(tmp_path)
        root = next(g for g in kp.root_group.subgroups if g.name == "istota")
        kp.trash_group(root)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}

    def test_a_nested_group_nominated_as_the_recycle_bin_is_excluded(
        self, tmp_path
    ):
        """The defensive half, for the layout the walk does not exclude by
        construction.

        KeePassXC lets any group be the recycle bin, so a group inside
        `istota/` can be one — at which point every entry in it is something
        the user deleted, sitting exactly where the walk looks. Nominated here
        by writing the Meta element, because there is no public setter and the
        point is the file this reader will meet rather than the API that
        produced it."""
        kp, path = _standard_vault(tmp_path)
        karakeep = kp.find_groups(name="karakeep", first=True)
        elem = kp._xpath("/KeePassFile/Meta/RecycleBinUUID", first=True)
        elem.text = base64.b64encode(karakeep.uuid.bytes).decode()
        kp.save()

        read, _ = _read(path)

        assert read.services == {"ntfy_topic": TOPIC_VALUE}

    def test_a_deeply_nested_group_nominated_as_the_recycle_bin_is_excluded(
        self, tmp_path
    ):
        """The bin is tested at every level of the recursion rather than at the
        top two, which is what the walk being recursive changed."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        work = kp.add_group(root, "work")
        binned = kp.add_group(work, "old")
        kp.add_entry(binned, "key", "", API_KEY_VALUE)
        kp.add_entry(work, "key", "", BASE_URL_VALUE)
        kp.save()
        elem = kp._xpath("/KeePassFile/Meta/RecycleBinUUID", first=True)
        elem.text = base64.b64encode(
            kp.find_groups(name="old", first=True).uuid.bytes
        ).decode()
        kp.save()

        read, _ = _read(path)

        assert read.services == {"work_key": BASE_URL_VALUE}

    def test_the_istota_group_nominated_as_the_recycle_bin_is_excluded(
        self, tmp_path
    ):
        """The sibling of the case above, and the one that keeps the outer check
        from being free to delete.

        The two checks are at different levels and neither covers the other:
        deleting the outer one left the whole file green, because the three other
        recycle-bin tests reach only the inner check or the walk's own starting
        point."""
        kp, path = _standard_vault(tmp_path)
        root = next(g for g in kp.root_group.subgroups if g.name == "istota")
        elem = kp._xpath("/KeePassFile/Meta/RecycleBinUUID", first=True)
        elem.text = base64.b64encode(root.uuid.bytes).decode()
        kp.save()

        read, _ = _read(path)

        assert read.services == {}

    # ---- the read boundary ---------------------------------------------

    def test_an_absent_file_reads_as_missing(self, tmp_path):
        with pytest.raises(VaultMissing):
            read_vault_bytes(tmp_path / "nothing-here.kdbx")

    def test_a_zero_byte_file_reads_as_corrupt_rather_than_missing(self, tmp_path):
        """The two come back from `read_overlay_bytes` as the *same* bytes and
        differ only in the size element, so a suite holding one and not the
        other cannot see the distinction at all.

        It matters because the retry rules differ: `VaultCorrupt` caches its
        digest and waits for the file to change, which is right for a file an
        rclone write was caught mid-flight, while `VaultMissing` says the path
        is wrong."""
        path = tmp_path / "vault.kdbx"
        path.write_bytes(b"")

        with pytest.raises(VaultCorrupt):
            read_vault_bytes(path)

    def test_a_symlink_is_refused(self, tmp_path):
        """`O_NOFOLLOW`. Every component of the relative form lives under a
        directory bound read-write into that user's own sandbox, so a link
        planted at the filename otherwise hands the daemon another file — and
        this one is then decrypted with a key the daemon holds."""
        real = tmp_path / "elsewhere.kdbx"
        real.write_bytes(b"not a kdbx but a real file")
        link = tmp_path / "vault.kdbx"
        link.symlink_to(real)

        with pytest.raises(VaultUnreadable) as caught:
            read_vault_bytes(link)

        assert str(caught.value) == OVERLAY_IS_A_SYMLINK

    def test_a_fifo_is_refused(self, tmp_path):
        """`S_ISREG` behind `O_NONBLOCK`. A FIFO at that name blocks `open(2)`
        until somebody writes to it, and the sync runs on a background gate with
        no timeout behind it."""
        path = tmp_path / "vault.kdbx"
        os.mkfifo(path)

        with pytest.raises(VaultUnreadable) as caught:
            read_vault_bytes(path)

        assert str(caught.value) == OVERLAY_NOT_A_REGULAR_FILE

    def test_a_file_over_the_cap_is_refused(self, tmp_path):
        """The size is checked on the fd before the read, so the refusal costs
        one `fstat` rather than 8 MiB of `os.read`. Sparse, because the
        assertion is about `st_size`."""
        path = tmp_path / "vault.kdbx"
        with open(path, "wb") as fh:
            fh.truncate(VAULT_READ_CAP_BYTES + 1)

        with pytest.raises(VaultUnreadable) as caught:
            read_vault_bytes(path)

        assert str(caught.value) == OVERLAY_UNREADABLY_LARGE

    def test_a_file_at_the_cap_is_read(self, tmp_path):
        """The control for the one above: the refusal is `>` and not `>=`, so a
        vault that has grown to exactly the cap is still read. Not a KDBX, so it
        gets as far as the bytes and no further."""
        path = tmp_path / "vault.kdbx"
        with open(path, "wb") as fh:
            fh.truncate(VAULT_READ_CAP_BYTES)

        data, digest = read_vault_bytes(path)

        assert len(data) == VAULT_READ_CAP_BYTES
        assert digest == hashlib.sha256(data).hexdigest()

    # ---- the parse failures --------------------------------------------

    def test_a_wrong_passphrase_reads_as_locked(self, tmp_path):
        """Separate from corrupt because the remedies differ and only one of
        them touches the file — §7 caches the digest of the other."""
        _, path = _standard_vault(tmp_path)
        data, _ = read_vault_bytes(path)

        with pytest.raises(VaultLocked):
            parse_vault(data, "the-wrong-passphrase")

    def test_a_rotated_master_passphrase_reads_as_locked(self, tmp_path):
        """The same owned set re-encrypted under a new password, which is what
        changing the master password in KeePassXC produces. It is a whole new
        file and still a `VaultLocked`, not a `VaultCorrupt`."""
        kp, path = _standard_vault(tmp_path)
        kp.password = "a-rotated-passphrase"
        kp.save()
        data, _ = read_vault_bytes(path)

        with pytest.raises(VaultLocked):
            parse_vault(data, PASSPHRASE)

        assert (
            parse_vault(data, "a-rotated-passphrase").services["ntfy_topic"]
            == TOPIC_VALUE
        )

    def test_a_truncated_file_reads_as_corrupt(self, tmp_path):
        """A half-synced file. KDBX4 HMACs the whole payload, so it fails to
        parse rather than yielding half a vault — which is what makes §6's
        "keep the existing rows on failure" rule safe."""
        _, path = _standard_vault(tmp_path)
        whole = path.read_bytes()

        with pytest.raises(VaultCorrupt):
            parse_vault(whole[: len(whole) // 2], PASSPHRASE)

    def test_a_file_that_is_not_a_kdbx_reads_as_corrupt(self, tmp_path):
        with pytest.raises(VaultCorrupt):
            parse_vault(b"# just some text\n" * 20, PASSPHRASE)

    def test_a_file_that_decrypts_but_has_no_root_group_reads_as_corrupt(
        self, tmp_path
    ):
        """The mapping walk is inside the guard, so the closed set of error
        classes is a contract rather than an observation.

        `kp.root_group` is None for a database whose `/KeePassFile/Root/Group` is
        gone, and an `AttributeError` out of the walk is not a class §7's retry
        rule or §8's notification can act on — it would arrive at a background
        gate as an unhandled exception instead. It needs the passphrase to
        produce, so a task cannot arrange it; a client that wrote a malformed
        file can."""
        kp, path = _standard_vault(tmp_path)
        root = kp._xpath("/KeePassFile/Root/Group", first=True)
        root.getparent().remove(root)
        kp.save()
        data, _ = read_vault_bytes(path)

        with pytest.raises(VaultCorrupt):
            parse_vault(data, PASSPHRASE)

    def test_a_path_with_a_nul_is_refused_rather_than_raising(self, tmp_path):
        """`read_overlay_bytes` catches `FileNotFoundError` and `OSError`, and a
        NUL makes `os.open` raise `ValueError`, which is neither — so it would
        leave `read_vault_bytes` as something outside the closed set. `vault_path`
        is a TOML string, where a NUL is expressible."""
        with pytest.raises(VaultUnreadable):
            read_vault_bytes(tmp_path / "vault\0.kdbx")

    def test_an_absent_library_reads_as_library_missing(self, tmp_path):
        """The extra is optional, so a deployment without it gets a mapped error
        with a remedy rather than an ImportError out of a background gate."""
        _, path = _standard_vault(tmp_path)
        data, _ = read_vault_bytes(path)

        with _library_absent():
            with pytest.raises(VaultLibraryMissing):
                parse_vault(data, PASSPHRASE)

    def test_simulating_an_absent_library_leaves_the_real_one_working(
        self, tmp_path
    ):
        """The control on `_library_absent`'s teardown, and on nothing else.

        It asserts the property the naive `del sys.modules[...]` version breaks:
        after the simulation, a wrong passphrase is still `VaultLocked` rather
        than the catch-all's `VaultCorrupt`. Without the save-and-restore this
        fails here rather than in whichever unrelated test the worker reaches
        next, which is the point of having it."""
        _, path = _standard_vault(tmp_path)
        data, _ = read_vault_bytes(path)

        with _library_absent():
            with pytest.raises(VaultLibraryMissing):
                parse_vault(data, PASSPHRASE)

        with pytest.raises(VaultLocked):
            parse_vault(data, "the-wrong-passphrase")
        assert set(parse_vault(data, PASSPHRASE).services) == {
            "karakeep_base_url",
            "karakeep_api_key",
            "ntfy_topic",
        }

    # ---- the properties the rest of the design rests on ----------------

    def test_saving_twice_with_no_change_produces_different_bytes(self, tmp_path):
        """§7's mitigation, pinned so a format change that made saves
        byte-stable is noticed rather than quietly removing it.

        KDBX draws a fresh master seed and encryption IV on every save, so the
        digest moves when the user saves out of habit — which costs a parse and
        an idempotent apply, and is the one thing that eventually corrects
        table-side drift, since any save re-asserts the whole owned set."""
        kp, path = _standard_vault(tmp_path)
        first = path.read_bytes()

        kp.save()
        second = path.read_bytes()

        assert first != second
        assert parse_vault(first, PASSPHRASE).services == (
            parse_vault(second, PASSPHRASE).services
        )
        assert (
            parse_vault(first, PASSPHRASE).digest
            != parse_vault(second, PASSPHRASE).digest
        )

    def test_the_repr_omits_the_values(self):
        """`services` holds every plaintext the vault carries between parse and
        apply, and the no-value-logging rule is defeated by one
        `logger.debug("read=%r", read)`. pytest's assertion rewriting prints the
        repr of whatever a failing comparison touched, so a developer never has
        to write that call themselves to leak it."""
        read = VaultRead(
            digest="0" * 64,
            services={"karakeep_api_key": API_KEY_VALUE},
            held=frozenset({"karakeep_base_url"}),
            skipped=(("karakeep_topic", SKIP_DUPLICATE_NAME),),
        )

        rendered = repr(read)

        assert API_KEY_VALUE not in rendered
        assert "0" * 64 in rendered
        assert "karakeep_api_key" in rendered
        # The other two fields carry names as well, and a reader needs them.
        assert "karakeep_base_url" in rendered and "karakeep_topic" in rendered
        # A list's repr calls repr on each element, so a `__str__`-only override
        # would be bypassed by every real failure this protects.
        assert API_KEY_VALUE not in repr([read])

    def test_no_log_record_carries_a_value(self, tmp_path, caplog, monkeypatch):
        """Names and counts are loggable. Values are not, at any level — so the
        fixture below walks **every** branch that logs.

        Seven of them now: the collision, the untitled counter, the entry that
        sets nothing, the unusable name, the oversize value and the two caps.
        The names in the file are attacker-reachable strings, since a task in
        the user's own sandbox can write it, and the values are the credentials
        the whole module exists to keep out of a log line."""
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_VALUE_BYTES", 16)
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_DEPTH", 1)
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_ENTRIES", 7)
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        aws = kp.add_group(root, "aws")
        # A collision: `aws/key` and the flat `AWS Key` are one name.
        kp.add_entry(aws, "key", "", API_KEY_VALUE)
        kp.add_entry(root, "AWS Key", "", "ak-the-duplicate")
        # No title, nothing set, an unusable name, and a value over the cap.
        kp.add_entry(root, "", "", "untitled-value")
        kp.add_entry(root, "Blank", "", "")
        kp.add_entry(root, "!!!", "", "unusable-name-value")
        kp.add_entry(root, "Oversize", "", "oversize-fixture-value-past-the-cap")
        kp.add_entry(root, "Last", "", "ok")
        # One group past the depth cap, and one entry past the entry cap.
        kp.add_entry(kp.add_group(aws, "deeper"), "key", "", "too-deep-value")
        kp.add_entry(kp.add_group(root, "zzz"), "key", "", "past-the-entry-cap-value")
        kp.save()
        secrets = {
            API_KEY_VALUE,
            "ak-the-duplicate",
            "untitled-value",
            "unusable-name-value",
            "oversize-fixture-value-past-the-cap",
            "too-deep-value",
            "past-the-entry-cap-value",
            PASSPHRASE,
        }

        with caplog.at_level(logging.DEBUG):
            read, _ = _read(path)

        # Every branch below ran: the assertion is the log, but a sweep over a
        # parse that logged nothing would pass just as happily.
        said = _ours(caplog)
        assert len(said) == 7, said
        assert set(read.services) == {"last"}
        for record in caplog.records:
            rendered = f"{record.getMessage()} {record.args!r} {record.exc_text!r}"
            for value in secrets:
                assert value not in rendered, (
                    f"{record.name} logged a fixture value: {record.getMessage()!r}"
                )

    def test_each_skip_says_which_name_it_skipped(self, tmp_path, caplog):
        """The behavioural assertions above cannot see whether a warning was
        emitted — they prove the branch ran, not that it said anything."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "aws"), "key", "", API_KEY_VALUE)
        kp.add_entry(root, "AWS Key", "", "ak-the-duplicate")
        kp.add_entry(root, "Blank", "", "")
        kp.add_entry(root, "", "", "untitled-value")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            _read(path)

        said = _ours(caplog)
        assert any("aws_key" in m and "produced by" in m for m in said), said
        assert any("Blank" in m and "no value in any field" in m for m in said), said
        assert any("no title" in m for m in said), said

    def test_untitled_entries_are_counted_rather_than_named_one_by_one(
        self, tmp_path, caplog
    ):
        """An entry with no title has no name to report, so N lines say exactly
        what one line says — and §7 parses on every save, so a vault carrying a
        few of them would otherwise write N lines for the life of the
        deployment."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        for _ in range(4):
            kp.add_entry(karakeep, "", "", API_KEY_VALUE, force_creation=True)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            _read(path)

        assert _ours(caplog) == ["vault: 4 entries had no title, skipped"]

    def test_a_name_cannot_forge_a_log_line(self, tmp_path, caplog):
        """Group and entry names are arbitrary strings out of the file:
        unbounded, and free to carry a newline. Unflattened, one of them can put
        a whole fabricated record into the daemon's log. §12 records that a task
        in the user's own sandbox can overwrite that file, so they are
        attacker-reachable rather than merely self-inflicted."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "9lives\nERROR forged line", "", API_KEY_VALUE)
        kp.add_entry(kp.add_group(root, "x" * 400), "api_key", "", API_KEY_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            _read(path)

        assert _ours(caplog), "the fixture reached no warning at all"
        for message in _ours(caplog):
            assert "\n" not in message
            assert len(message) < 300

    def test_the_catch_all_does_not_render_the_exception(self, tmp_path, caplog):
        """The branch the `exc_info` departure was made for, driven.

        Nothing else in the file reaches it with a value in play, so re-adding
        `exc_info=True` — or dropping `raise ... from None` — passes every other
        test here. A stand-in raiser rather than a crafted KDBX3, because what is
        being asserted is what this module does with an exception whose text
        carries credential material, not which library produces one."""
        _, path = _standard_vault(tmp_path)
        data, _ = read_vault_bytes(path)

        class _Boom(Exception):
            pass

        def _explode(*args, **kwargs):
            raise _Boom(f"choked on <Value>{API_KEY_VALUE}</Value>")

        import pykeepass

        with caplog.at_level(logging.DEBUG):
            with mock.patch.object(pykeepass, "PyKeePass", _explode):
                with pytest.raises(VaultCorrupt) as caught:
                    parse_vault(data, PASSPHRASE)

        assert API_KEY_VALUE not in str(caught.value)
        # `raise ... from None`: a caller's own `logger.exception` renders the
        # chain, so suppressing it is part of the same rule.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None or caught.value.__suppress_context__
        assert caplog.records
        for record in caplog.records:
            rendered = f"{record.getMessage()} {record.args!r} {record.exc_text!r}"
            assert API_KEY_VALUE not in rendered
        said = [r.getMessage() for r in caplog.records
                if r.name == "istota.secrets_vault"]
        assert said == ["vault: parse failed (tests.test_secrets_vault._Boom)"]

    def test_a_truncated_file_takes_the_catch_all_and_says_so(
        self, tmp_path, caplog
    ):
        """Which arm a corruption shape takes, pinned.

        Every arm produces `VaultCorrupt`, so the class assertions above cannot
        tell them apart. Measured against pykeepass 4.2.0: only an HMAC mismatch
        raises `PayloadChecksumError`, and a truncated file — §2's own named
        mid-write case — dies inside `construct` with a `StreamError` that
        pykeepass re-raises unmapped. That is why the catch-all's line does not
        call the failure unexpected."""
        _, path = _standard_vault(tmp_path)
        whole = path.read_bytes()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            with pytest.raises(VaultCorrupt):
                parse_vault(whole[: len(whole) // 2], PASSPHRASE)

        said = [r.getMessage() for r in caplog.records
                if r.name == "istota.secrets_vault"]
        assert said == ["vault: parse failed (construct.core.StreamError)"]

    def test_bytes_that_are_not_a_kdbx_take_the_mapped_arm(self, tmp_path, caplog):
        """The control for the one above: a bad *header* is mapped, so it logs
        nothing. Without this the assertion there is about a string rather than
        about which branch ran."""
        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            with pytest.raises(VaultCorrupt):
                parse_vault(b"# just some text\n" * 20, PASSPHRASE)

        assert [r for r in caplog.records if r.name == "istota.secrets_vault"] == []


class TestTheLibraryStaysOutOfTheImportGraph:
    """`pykeepass` is an optional extra pulling two compiled extensions.

    Nothing may load it at import or at collection: a deployment without the
    extra has to reach `VaultLibraryMissing` rather than an ImportError at
    startup, `doctor` tests for it with `find_spec` rather than an import, and a
    sync cycle that stops at the file digest must cost no import at all.
    """

    def test_the_library_import_is_function_scoped(self):
        """A source guard, read through `source_of` so `scripts/qt` selects it —
        a test asserting against source text executes none of the lines it
        reads, so testmon would otherwise never run this one."""
        from tests.support.drift import source_of

        from istota import secrets_vault

        source = source_of(secrets_vault)
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                assert "pykeepass" not in line, (
                    f"module-scope import of pykeepass: {line!r}"
                )
        assert "pykeepass" in source, "the guard is looking at the wrong module"

    def test_reading_the_bytes_needs_no_library(self, tmp_path):
        """`read_vault_bytes` is the half that runs every cycle, so it must work
        with the extra absent — which is what makes the digest short-circuit
        free."""
        _, path = _standard_vault(tmp_path)

        with _library_absent():
            data, digest = read_vault_bytes(path)

        assert data == path.read_bytes()
        assert digest == hashlib.sha256(data).hexdigest()


class TestEligibility:
    """Which services a vault may own — computed from the schema, not listed.

    §4 derives it so that a service added to `secret_schema` is classified by
    the rules rather than by somebody remembering this module exists.
    """

    def test_the_eligible_set_is_the_five_services_the_schema_yields_today(self):
        """The exact set, so a new schema service has to be classified here on
        purpose rather than becoming vault-ownable by arriving. One the daemon
        rewrites on its own belongs in `DAEMON_WRITTEN_SERVICES`; one the
        operator writes belongs in this list."""
        assert eligible_services() == frozenset(
            {"karakeep", "ntfy", "native_brain", "feeds", "carto"}
        )

    def test_a_service_with_no_writable_keys_is_not_eligible(self):
        """`google_workspace` and `garmin` declare `"fields": []` because their
        credentials are machine-managed blobs. The assertion on the key sets is
        what keeps this test about the rule rather than about two names."""
        keys = secret_schema.known_service_keys()
        assert keys["google_workspace"] == frozenset()
        assert keys["garmin"] == frozenset()
        assert not ({"google_workspace", "garmin"} & eligible_services())

    def test_every_daemon_written_service_is_excluded(self):
        """And two of them have writable keys, so the subtraction is doing the
        work rather than the empty-key rule doing it for free."""
        assert not (DAEMON_WRITTEN_SERVICES & eligible_services())
        keys = secret_schema.known_service_keys()
        assert keys["monarch"] and keys["overland"]

    def test_the_two_exclusion_rules_hold_over_the_whole_schema(self):
        """The quantified form of the two tests above, and the one that survives
        a new schema service.

        Both of those name the services the schema holds today, so a sixth
        machine-managed blob added tomorrow is covered by neither: the empty-key
        rule would go on being asserted about `google_workspace` and `garmin`
        while the new one walked straight into `eligible_services()`. This walks
        the schema instead, so the assertion is about the rule.

        `source_of` is what makes `scripts/qt` select it. The two subjects are
        module-level literals — `DAEMON_WRITTEN_SERVICES` and the schema dicts —
        which execute at *import* and are therefore attributed to whichever test
        in the worker imported the module first, so editing either records no
        dependency on this test. Reading both modules' text registers one.
        """
        source_of(secrets_vault_module)
        source_of(secret_schema)

        keys = secret_schema.known_service_keys()
        eligible = eligible_services()

        keyless = {service for service, declared in keys.items() if not declared}
        assert keyless, "no service declares an empty key set; the rule is untested"
        assert not (keyless & eligible), (
            "a service with no operator-writable keys is vault-eligible: "
            f"{sorted(keyless & eligible)}"
        )

        assert DAEMON_WRITTEN_SERVICES, "the daemon-written set is empty"
        assert not (DAEMON_WRITTEN_SERVICES & eligible), (
            "a service the daemon rewrites on its own is vault-eligible: "
            f"{sorted(DAEMON_WRITTEN_SERVICES & eligible)}"
        )

    def test_the_empty_key_rule_refuses_a_service_nothing_else_would(
        self, monkeypatch
    ):
        """The half above that cannot fail against today's schema, made to.

        Found by control: deleting `if keys` from `eligible_services` leaves the
        quantified test green, and Stage 2's named version green too. Both
        keyless services — `google_workspace` and `garmin` — are also in
        `DAEMON_WRITTEN_SERVICES`, so the subtraction refuses them a second time
        and the empty-key rule is masked by a coincidence of today's schema
        rather than exercised by it.

        A keyless service outside that set is what separates the two rules, and
        the schema has none to point at, so one is monkeypatched in — the
        technique `test_a_reserved_connector_service_is_never_eligible` uses for
        the same reason. The same mutation then turns this red and leaves its
        neighbours alone.
        """
        schema = dict(secret_schema.CONNECTED_SERVICE_SCHEMA)
        schema["blob_only"] = {"label": "Blob only", "used_by": (), "fields": []}
        monkeypatch.setattr(secret_schema, "CONNECTED_SERVICE_SCHEMA", schema)

        assert secret_schema.known_service_keys()["blob_only"] == frozenset()
        assert "blob_only" not in DAEMON_WRITTEN_SERVICES
        assert "blob_only" not in eligible_services()

    def test_the_vault_service_is_excluded_once_the_schema_declares_it(
        self, monkeypatch
    ):
        """§4 subtracts `vault` itself: the passphrase cannot live in the file it
        unlocks.

        The schema entry is real now — Stage 4 added it — so the monkeypatch is
        no longer what makes the exclusion visible. It stays because it is what
        keeps the test honest if the entry is ever removed again: the exclusion
        is vacuously true against a schema with no `vault` in it, which is the
        state this was written in."""
        schema = dict(secret_schema.CONNECTED_SERVICE_SCHEMA)
        schema["vault"] = {
            "label": "Credential vault",
            "used_by": (),
            "cli_only": True,
            "fields": [{"key": "passphrase", "label": "Vault passphrase",
                        "type": "password"}],
        }
        monkeypatch.setattr(secret_schema, "CONNECTED_SERVICE_SCHEMA", schema)

        assert secret_schema.known_service_keys()["vault"] == frozenset({"passphrase"})
        assert "vault" not in eligible_services()

    def test_a_reserved_connector_service_is_never_eligible(self, monkeypatch):
        """`connector:<id>` is a reserved namespace with exactly one writer —
        the connector route that validated the key against the resolved provider
        manifest (`Specs/Drafts/generic-connectors.md`). It is outside the schema
        today, so the exclusion is invisible unless something declares one; this
        pins that the prefix is what refuses it rather than the absence being
        what saves us. `secret_schema.is_reserved_service()` is that spec's and
        will own the prefix when it lands."""
        schema = dict(secret_schema.CONNECTED_SERVICE_SCHEMA)
        schema["connector:acme"] = {
            "label": "Acme", "used_by": (),
            "fields": [{"key": "api_key", "label": "API key", "type": "password"}],
        }
        monkeypatch.setattr(secret_schema, "CONNECTED_SERVICE_SCHEMA", schema)

        assert "connector:acme" in secret_schema.known_service_keys()
        assert "connector:acme" not in eligible_services()


class TestReadingThroughADescriptor:
    """`read_vault_bytes(path, dir_fd=...)` — the relative form's containment.

    The bytes are read through `read_overlay_bytes`, which given a descriptor
    opens `path.name` relative to it and consults no component above the leaf.
    That is what lets `storage.resolve_user_vault_path` hand the read a
    directory it walked with `O_NOFOLLOW` rather than a name to walk again:
    every component under `{mount}/Users/{user_id}` is model-writable, and the
    leaf-only `O_NOFOLLOW` the reader already had does not see a swapped
    `config/`.
    """

    def test_the_bytes_come_from_the_descriptor_not_the_path(self, tmp_path):
        """The discriminating layout: two files of the same name, one directory
        open. A read that walked the path would find the other one."""
        held = tmp_path / "held"
        held.mkdir()
        (held / "vault.kdbx").write_bytes(b"from-the-descriptor")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "vault.kdbx").write_bytes(b"from-the-path")

        fd = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
        try:
            data, digest = read_vault_bytes(decoy / "vault.kdbx", dir_fd=fd)
        finally:
            os.close(fd)
        assert data == b"from-the-descriptor"
        assert digest == hashlib.sha256(b"from-the-descriptor").hexdigest()

    def test_a_symlink_at_the_leaf_is_still_refused_through_a_descriptor(
        self, tmp_path
    ):
        held = tmp_path / "held"
        held.mkdir()
        (held / "real.kdbx").write_bytes(b"real")
        (held / "vault.kdbx").symlink_to(held / "real.kdbx")

        fd = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(VaultUnreadable) as exc:
                read_vault_bytes(held / "vault.kdbx", dir_fd=fd)
        finally:
            os.close(fd)
        assert str(exc.value) == OVERLAY_IS_A_SYMLINK

    def test_an_absent_file_is_still_missing_through_a_descriptor(self, tmp_path):
        held = tmp_path / "held"
        held.mkdir()
        fd = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(VaultMissing):
                read_vault_bytes(held / "vault.kdbx", dir_fd=fd)
        finally:
            os.close(fd)

    def test_no_descriptor_is_the_absolute_forms_behaviour_unchanged(self, tmp_path):
        path = tmp_path / "vault.kdbx"
        path.write_bytes(b"absolute")
        data, _digest = read_vault_bytes(path)
        assert data == b"absolute"


class TestTheConfigFields:
    """`vault_path`, `vault_services` and `scheduler.vault_sync_interval`.

    Asserted as a round trip through `load_config` rather than against the
    dataclass alone, because "declared, documented, and read by nothing" is a
    defect class `config_mapper.py` records eleven instances of — and for these
    three the symptom is a feature the operator configured and the daemon never
    ran.
    """

    def test_the_defaults_leave_the_feature_off(self, tmp_path):
        config = _load_config_text(tmp_path, 'bot_name = "Istota"\n')
        assert config.scheduler.vault_sync_interval == 300
        user = UserConfig()
        assert user.vault_path == ""
        assert user.vault_services == []

    def test_both_user_fields_round_trip(self, tmp_path):
        config = _load_config_text(tmp_path, """
            [users.alice]
            vault_path = "istota/config/vault.kdbx"
            vault_services = ["karakeep", "ntfy"]
        """)
        assert config.users["alice"].vault_path == "istota/config/vault.kdbx"
        assert config.users["alice"].vault_services == ["karakeep", "ntfy"]

    def test_the_interval_round_trips(self, tmp_path):
        config = _load_config_text(
            tmp_path, "[scheduler]\nvault_sync_interval = 60\n"
        )
        assert config.scheduler.vault_sync_interval == 60

    def test_a_non_list_vault_services_is_dropped_rather_than_iterated(
        self, tmp_path, caplog
    ):
        """A bare string iterates as characters, so `"karakeep"` would become
        eight one-letter services.

        **The result is `[]` either way**, which is why this asserts on what was
        said rather than only on what was kept: the eligibility filter refuses
        every one of those eight on its own, so a version that iterated the
        string would leave the list empty and be indistinguishable here — while
        telling the operator eight times that a service they never wrote is not
        vault-eligible. Measured: with the string guard removed, the kept list
        is still `[]` and the warning count goes from one to eight.
        """
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load_config_text(tmp_path, """
                [users.alice]
                vault_services = "karakeep"
            """)
        assert config.users["alice"].vault_services == []
        said = [
            m for m in (r.getMessage() for r in caplog.records)
            if "vault_services" in m
        ]
        assert len(said) == 1, said
        assert "not a list of service names" in said[0]

    def test_a_non_string_vault_path_is_dropped(self, tmp_path):
        config = _load_config_text(tmp_path, """
            [users.alice]
            vault_path = 7
        """)
        assert config.users["alice"].vault_path == ""

    def test_the_interval_is_documented(self):
        """`tests/test_config_field_coverage.py` holds every leaf field of the
        tree to the example or the Ansible template, with an exemption list that
        is deliberately empty. Named here so a reader of this file finds out
        where the line has to live."""
        text = (REPO / "config" / "config.example.toml").read_text()
        assert "vault_sync_interval" in text


class TestTheEligibilityFilterAtLoad:
    """A `vault_services` name a vault may not own is dropped, with a warning.

    Dropped rather than raised: §4's rule is that a config refusing to boot
    because a module was disabled is worse than one telling the operator which
    line is inert. The filter is not the boundary — `apply_vault` refuses the
    same names again on its own terms — it is what makes the refusal visible at
    the moment somebody could act on it.
    """

    def _load_with(self, tmp_path, caplog, services):
        body = "[users.alice]\nvault_services = %s\n" % json.dumps(services)
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            config = _load_config_text(tmp_path, body)
        return config.users["alice"].vault_services, [
            r.getMessage() for r in caplog.records if r.name == "istota.config"
        ]

    def test_an_eligible_service_survives(self, tmp_path, caplog):
        kept, said = self._load_with(tmp_path, caplog, ["karakeep"])
        assert kept == ["karakeep"]
        assert not [m for m in said if "vault_service" in m]

    def test_a_daemon_written_service_is_dropped_and_named(self, tmp_path, caplog):
        kept, said = self._load_with(tmp_path, caplog, ["karakeep", "monarch"])
        assert kept == ["karakeep"]
        warned = [m for m in said if "monarch" in m]
        assert warned, said
        assert SKIP_INELIGIBLE_SERVICE in warned[0]
        assert "alice" in warned[0]

    def test_a_reserved_connector_service_is_dropped_under_its_own_reason(
        self, tmp_path, caplog
    ):
        """The reserved namespace has exactly one writer, and it is not this.

        Reported as the namespace rather than as an ordinary unknown service, so
        the refusal survives a connector service one day appearing in the
        schema — which is the arrangement `apply_vault` already has.
        """
        kept, said = self._load_with(tmp_path, caplog, ["connector:acme"])
        assert kept == []
        warned = [m for m in said if "connector:acme" in m]
        assert warned, said
        assert SKIP_RESERVED_SERVICE in warned[0]

    def test_the_vaults_own_service_is_dropped(self, tmp_path, caplog):
        """The passphrase cannot live in the file it unlocks."""
        kept, _said = self._load_with(tmp_path, caplog, ["vault"])
        assert kept == []

    def test_a_case_variant_is_dropped_rather_than_folded(self, tmp_path, caplog):
        """§3 folds the *file's* service group and nothing else.

        The fold exists because a phone keyboard autocapitalizes a group name.
        `vault_services` is operator config in a file nobody types on a phone,
        so a name that does not match is a typo — and folding it here would put
        the normalization on both sides of one comparison.
        """
        kept, said = self._load_with(tmp_path, caplog, ["Karakeep"])
        assert kept == []
        assert [m for m in said if "Karakeep" in m], said

    def test_surrounding_whitespace_is_stripped_rather_than_refused(
        self, tmp_path, caplog
    ):
        kept, _said = self._load_with(tmp_path, caplog, [" karakeep "])
        assert kept == ["karakeep"]

    def test_an_empty_entry_is_dropped(self, tmp_path, caplog):
        kept, _said = self._load_with(tmp_path, caplog, ["", "   "])
        assert kept == []

    def test_a_non_string_entry_is_dropped_rather_than_raising(
        self, tmp_path, caplog
    ):
        kept, _said = self._load_with(tmp_path, caplog, [7, ["karakeep"]])
        assert kept == []

    def test_a_hand_built_config_carrying_junk_does_not_fail_the_load(self):
        """Driven against the filter directly, because nothing can reach it.

        `_vault_services_value` guarantees `list[str]` on the TOML path and
        nothing else writes the field, so a `load_config` round trip cannot
        exercise this — the shape guard upstream would have to be removed first,
        and a test whose subject is reachable only through another bug is a test
        that passes for the wrong reason. What it defends is worth a line
        anyway: `_service_refusal` calls `.startswith`, and an `AttributeError`
        escaping `load_config` stops the scheduler, the web app, the webhook
        receiver and every host-side skill CLI the proxy spawns per call.
        """
        from istota.config import Config, _validate_vault_services

        config = Config(users={"alice": UserConfig()})
        config.users["alice"].vault_services = [7, None, ["karakeep"], "karakeep"]

        _validate_vault_services(config)

        assert config.users["alice"].vault_services == ["karakeep"]

    def test_the_filter_agrees_with_what_apply_vault_would_refuse(self):
        """One predicate, two callers. A second copy of the rule here is the
        drift the rule exists to catch — a name the loader keeps and the apply
        refuses is a line the operator was told was live."""
        for service in sorted(eligible_services()):
            assert service_refusal(service) is None
        for service in sorted(DAEMON_WRITTEN_SERVICES):
            assert service_refusal(service) is not None
        assert service_refusal("connector:acme") == SKIP_RESERVED_SERVICE


class TestTheProfileTableGuard:
    """Neither field may be settable by anything downstream of a task.

    `user_profiles` is writable from the settings UI, and every other per-user
    scalar is overlaid from it by `_apply_user_profiles`. These two are a
    security control rather than a preference: `vault_path` selects which file
    the daemon decrypts with a key it holds, and `vault_services` selects which
    credentials that file may overwrite and delete.

    Two halves, and neither covers the other. The column set is read off a real
    initialised database rather than grepped out of `schema.sql`, because
    `_run_migrations` adds columns with `ALTER TABLE` and a grep would not see
    one. The behavioural half drives the overlay itself, because a column is not
    the only way a value could arrive — `merge_into_user_config` sets attributes
    by name and could set these from anywhere.
    """

    def test_the_table_carries_neither_column(self, db_path):
        with sqlite3.connect(db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(user_profiles)")
            }
        assert columns, "the table must exist for this assertion to mean anything"
        assert "vault_path" not in columns
        assert "vault_services" not in columns

    def test_the_overlay_leaves_both_fields_alone(self, db_path):
        from istota import user_profiles as up

        up.ensure_profile(db_path, "alice", display_name="Alice")
        rows = up.list_profiles(db_path)
        assert "alice" in rows

        user = UserConfig(
            vault_path="istota/config/vault.kdbx",
            vault_services=["karakeep"],
            display_name="from-toml",
        )
        up.merge_into_user_config(rows["alice"], user)

        # The control: the overlay demonstrably ran on this object, so the two
        # assertions below are about what it declined to touch rather than
        # about a call that did nothing.
        assert user.display_name == "Alice"
        assert user.vault_path == "istota/config/vault.kdbx"
        assert user.vault_services == ["karakeep"]

    def test_the_profile_dataclass_declares_neither_field(self):
        from istota.user_profiles import UserProfile

        names = {f.name for f in dataclasses.fields(UserProfile)}
        assert "vault_path" not in names
        assert "vault_services" not in names


class TestNoSkillManifestDeclaresThePassphrase:
    """The drift guard on §5's single named route into a task.

    Credential injection into a task is driven entirely by
    `executor.derive_skill_credential_map` reading manifest `env:` blocks, and
    the proxy serves its `credential` request type out of a dict built from
    those same manifests. So a passphrase no manifest names can reach a task by
    no route at all, and the whole of §5's claim rests on no manifest naming it.

    **Matched on the parsed `(service, key)` pair, never on the variable's
    name.** A manifest is free to call the variable anything it likes — the
    resolver reads `service` and `key` off the spec and hands them to
    `secrets_store.get_secret` — so a name-shaped grep for `VAULT_PASSPHRASE`
    passes a manifest declaring `service: vault, key: passphrase` as `FOO`,
    which is the live route wearing a name nobody would search for.

    **Dated, because it covers manifests and not the appearance of a second
    reader.** As of `76c12104` the manifest env spec resolved in
    `skills/_env.py` is the only code path in the tree that reads the secrets
    table on a task's behalf, there is no verb anywhere returning an arbitrary
    `(service, key)` row, and `_PROXY_LOOKUP_BLOCKED` is checked against a
    manifest-derived allowlist. One second reader is already scheduled:
    `Specs/Drafts/generic-connectors.md`'s broker reads `connector:*` rows the
    day it lands. This guard will still be green then and the claim above will
    not be, so widen it rather than trusting it.
    """

    def _specs(self):
        """Every `EnvSpec` of every manifest this deployment ships.

        The operator directory is included beside the bundled one because an
        override is a manifest too, and `build_skill_env` reads whichever wins.
        """
        from istota.skills._loader import load_skill_index

        index = load_skill_index(REPO / "config" / "skills", bundled_dir=None)
        assert index, "no skill manifests loaded — did the loader change shape?"
        return [(name, spec) for name, meta in index.items() for spec in meta.env_specs]

    def test_no_manifest_declares_the_vault_passphrase(self):
        """Folded on both halves, which is wider than the route it guards.

        `get_secret` matches the service and key as written, so a manifest
        spelling `Vault` would resolve nothing today. The guard refuses it
        anyway: the thing being protected is that nobody writes the pair down in
        a manifest at all, and a comparison that let one spelling through would
        be relitigated the first time the store's matching changed.
        """
        from istota.secrets_vault import VAULT_PASSPHRASE_KEY, VAULT_PASSPHRASE_SERVICE

        declared = [
            f"{skill}: {spec.var or '<unnamed>'}"
            for skill, spec in self._specs()
            if spec.source == "secret"
            and spec.service.strip().casefold() == VAULT_PASSPHRASE_SERVICE
            and spec.key.strip().casefold() == VAULT_PASSPHRASE_KEY
        ]
        assert not declared, (
            "a skill manifest declares the vault passphrase as a task credential, "
            "which is the one route §5 says does not exist: " + ", ".join(declared)
        )

    def test_the_guard_is_reading_real_secret_specs(self):
        """Non-vacuity, and it is not optional here.

        Every assertion above is a `not in`, so a loader change that left
        `service` and `key` empty — or a parse that dropped `from: secret`
        entirely — would make the guard pass about nothing. This requires the
        shipped manifests to carry secret-sourced specs with both halves
        populated, so the comparison is over real data.
        """
        secret_specs = [
            (skill, spec) for skill, spec in self._specs() if spec.source == "secret"
        ]
        assert len(secret_specs) >= 5, (
            f"only {len(secret_specs)} secret-sourced env specs parsed; the guard "
            "above would be comparing against nothing"
        )
        assert all(spec.service and spec.key for _skill, spec in secret_specs), (
            "a secret-sourced spec parsed with an empty service or key, so the "
            "pair the guard matches on is not what the manifests carry"
        )
