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
import logging
import os
import sqlite3
import sys
import textwrap

from pathlib import Path
from unittest import mock

import pytest

from istota import secrets_store
from istota import secrets_vault as secrets_vault_module
from istota.config import UserConfig, load_config
from istota.secrets_vault import (
    SKIP_DUPLICATE_NAME,
    SKIP_OVERSIZE_VALUE,
    SKIP_UNREADABLE_ROW,
    SKIP_UNUSABLE_NAME,
    VAULT_ENTRY_SERVICE,
    VAULT_READ_CAP_BYTES,
    VaultCorrupt,
    VaultLibraryMissing,
    VaultLocked,
    VaultMissing,
    VaultRead,
    VaultUnreadable,
    apply_vault,
    parse_vault,
    read_vault_bytes,
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

    def test_a_collision_where_only_one_entry_has_a_value_is_held(
        self, tmp_path, caplog
    ):
        """The fat-finger case, and the one the removed `_near_miss_title`
        existed for.

        A user adds a second entry whose title slugs to the same name and
        leaves its password blank — `API_KEY` beside `api_key`, `AWS Key`
        beside `aws/key`. §2's deletion is licensed by "istota cannot say which
        of two values it is", and with one value that premise is false. The
        name is still not applied, because which *entry* owns it is genuinely
        ambiguous; what must not happen is the stored credential being
        destroyed over a duplicated title.

        The measurement behind it is in the deleted `_near_miss_title`
        docstring: `API_KEY` with a blank password beside a stored `api_key`
        deleted the credential and put nothing in `skipped`.
        """
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "aws"), "key", "", API_KEY_VALUE)
        kp.add_entry(root, "AWS Key", "", "")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        assert "aws_key" not in read.services
        assert "aws_key" in read.held, "the stored credential would be deleted"
        # Warned, unlike the zero-value collision: the user has a real
        # credential that is not being applied and wants to know why.
        assert ("aws_key", SKIP_DUPLICATE_NAME) in read.skipped
        said = [m for m in _ours(caplog) if "aws_key" in m]
        assert len(said) == 1, said

    def test_a_held_collision_does_not_delete_the_stored_row(
        self, tmp_path, db_path, secret_key_env
    ):
        """The hold driven through to the rows, which is where it matters.

        `held` is a field; a test asserting only on the field passes against an
        apply that ignores it.
        """
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "aws"), "key", "", API_KEY_VALUE)
        kp.save()
        apply_vault(db_path, "alice", _read(path)[0])
        assert _entry(db_path, "alice", "aws_key") == API_KEY_VALUE

        kp.add_entry(root, "AWS Key", "", "")
        kp.save()
        result = apply_vault(db_path, "alice", _read(path)[0])

        assert result.deleted == 0
        assert _entry(db_path, "alice", "aws_key") == API_KEY_VALUE

    def test_two_case_variant_roots_that_collide_hold_rather_than_delete(
        self, tmp_path
    ):
        """The case-insensitive match makes a pair of top-level `istota` groups
        reachable that the exact match could not produce — a KDBX merge, or a
        user who made the folder twice. Every name they share collides.

        Both roots are read rather than one being picked, because picking would
        decide by XML order, which is what §2 refuses. The collision rule then
        applies as it does anywhere else, and the one-value arm is what keeps a
        duplicate folder from emptying the namespace.
        """
        kp, path = _new_db(tmp_path)
        lower = kp.add_group(kp.root_group, "istota")
        upper = kp.add_group(kp.root_group, "Istota")
        kp.add_entry(lower, "shared", "", API_KEY_VALUE)
        kp.add_entry(upper, "shared", "", "")
        kp.save()

        read, _ = _read(path)

        assert read.scoped is True
        assert "shared" not in read.services
        assert "shared" in read.held

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
        # The skip record carries the original bounded and flattened, because
        # it outlives the log line: it reaches `vault-status` and the read's
        # own repr, and an entry title is unbounded file text.
        assert read.skipped == (("b" * cap + "…", SKIP_UNUSABLE_NAME),)

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
        # Not truncation: a group below the depth cap is excluded by a rule
        # rather than interrupted by a budget, so nothing under it has ever
        # been readable and there is no stored name it can strand.
        assert read.truncated == ""

    def test_an_untruncated_read_says_so(self, tmp_path):
        """The control for the two assertions above: an ordinary read must not
        claim to be a prefix, or the applying half would stop deleting
        anything at all."""
        _, path = _standard_vault(tmp_path)

        read, _ = _read(path)

        assert read.truncated == ""

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
        # The applying half may not read the names it did not reach as names
        # the user removed, so the read says it is a prefix of the file, and
        # which bound cut it.
        assert read.truncated == "entry"

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
        assert read.truncated == "name"

    def test_a_field_that_produces_no_name_still_spends_the_budget(
        self, tmp_path, monkeypatch, caplog
    ):
        """The cap counts fields considered, not names produced.

        A field whose name cannot be derived is not free: it writes a skip
        record and a daemon WARNING, and `Entry.custom_properties` is unbounded
        in cardinality. Counting only the successful branch left one entry
        whose custom fields all slug to nothing emitting one of each per field,
        past every cap, from a file a task in that user's own sandbox can
        write."""
        monkeypatch.setattr(secrets_vault_module, "VAULT_MAX_NAMES", 4)
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        entry = kp.add_entry(root, "Acme", "", API_KEY_VALUE)
        for index in range(20):
            entry.set_custom_property("!" * (index + 1), f"junk-{index}")
        kp.add_entry(root, "Other", "", BASE_URL_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        # Four fields: the password, the username, the URL, and one custom
        # field that produced nothing. The walk stops there.
        assert read.services == {"acme": API_KEY_VALUE}
        assert read.truncated == "name"
        assert len(read.skipped) == 1
        assert len(_ours(caplog)) == 2, _ours(caplog)

    # ---- what the walk excludes -----------------------------------------

    def test_an_istota_group_nested_in_another_group_does_not_scope(self, tmp_path):
        """Only a **top-level** group narrows the read. A nested one is an
        ordinary group that happens to share a name, so the read is unscoped
        and it contributes a path segment like any other."""
        kp, path = _new_db(tmp_path)
        outer = kp.add_group(kp.root_group, "Personal")
        kp.add_entry(kp.add_group(outer, "istota"), "key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.scoped is False
        assert read.services == {"personal_istota_key": API_KEY_VALUE}

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
            truncated="",
            scoped=True,
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
            read, _ = _read(path)

        assert _ours(caplog), "the fixture reached no warning at all"
        for message in _ours(caplog):
            assert "\n" not in message
            assert len(message) < 300
        # The same two strings land in the skip records, which outlive the log
        # line: they reach `vault-status` and the read's own repr, so they are
        # bounded and flattened where they are produced rather than where they
        # are rendered.
        assert read.skipped, "the fixture produced no skip record"
        for name, _reason in read.skipped:
            assert "\n" not in name
            assert len(name) <= secrets_vault_module._LABEL_MAX_CHARS + 1

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


class TestScope:
    """§1: the file is the consent boundary, the `istota` group narrows it.

    The rule this replaced made the group the boundary and read a file without
    one as empty — which the §3 sweep turns into the deletion of every stored
    credential, and a mistyped group is indistinguishable from a deliberately
    emptied vault. Under this one an empty read happens only when the file is
    genuinely empty.

    The case-insensitive match is the half with teeth in the other direction.
    `Istota` is what a person types, so under an exact lowercase match the most
    likely spelling of the narrowing would fall through to the widest read —
    which is a disclosure rather than a wipe, and the negative control below is
    written to catch that direction specifically.
    """

    def test_a_top_level_istota_group_scopes_the_read(self, tmp_path):
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "shared", "", API_KEY_VALUE)
        kp.add_entry(kp.add_group(kp.root_group, "Personal"), "private", "", TOPIC_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.scoped is True
        assert read.services == {"shared": API_KEY_VALUE}
        assert "personal_private" not in read.services

    def test_no_istota_group_reads_the_whole_file(self, tmp_path):
        """The root stands in for `istota/` and contributes no path segment, so
        `<root>/aws/key` produces exactly what `istota/aws/key` would."""
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.add_group(kp.root_group, "aws"), "key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.scoped is False
        assert read.services == {"aws_key": API_KEY_VALUE}

    def test_an_entry_at_the_root_of_an_unscoped_file_is_read(self, tmp_path):
        """A KDBX exported out of a password manager commonly has entries
        sitting at the top level rather than in a group. Walking only the root's
        *subgroups* would drop every one of them."""
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.root_group, "github pat", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.scoped is False
        assert read.services == {"github_pat": API_KEY_VALUE}

    def test_an_unscoped_read_says_so_once_with_a_count_and_no_name(
        self, tmp_path, caplog
    ):
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.add_group(kp.root_group, "aws"), "key", "", API_KEY_VALUE)
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            read, _ = _read(path)

        messages = _ours(caplog)
        assert len(messages) == 1, messages
        assert "istota" in messages[0]
        assert API_KEY_VALUE not in messages[0]
        assert "aws_key" not in messages[0]
        assert read.scoped is False

    @pytest.mark.parametrize("spelling", ["Istota", "ISTOTA", "istota ", " IsToTa\t"])
    def test_every_case_and_whitespace_variant_scopes(self, tmp_path, spelling):
        """The whole of the fix, driven by each spelling a person actually
        types. Under the old exact match every one of these fell through to the
        unscoped read."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, spelling)
        kp.add_entry(root, "shared", "", API_KEY_VALUE)
        kp.add_entry(
            kp.add_group(kp.root_group, "Personal"), "outside", "", TOPIC_VALUE
        )
        kp.save()

        read, _ = _read(path)

        # **The presence of the entry outside the group is the assertion, and
        # it goes first**, because that is the failure this control has to
        # discriminate. A test asserting that `shared` is *missing* goes red on
        # a wipe and on a disclosure alike; this one goes red only when a
        # credential the user kept outside the narrowing has been read. Under
        # the reverted `group.name == VAULT_ROOT_GROUP` match this read is
        # unscoped and `personal_outside` is in `services`, which is exactly
        # the disclosure the case-insensitive match exists to prevent.
        assert "personal_outside" not in read.services, read.services
        assert read.scoped is True
        assert read.services == {"shared": API_KEY_VALUE}

    def test_several_matching_top_level_groups_are_all_read(self, tmp_path):
        """Nothing chooses between them, which is what the old exact match did
        for several groups all spelled `istota`."""
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.add_group(kp.root_group, "istota"), "one", "", API_KEY_VALUE)
        kp.add_entry(kp.add_group(kp.root_group, "Istota"), "two", "", TOPIC_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.scoped is True
        assert read.services == {"one": API_KEY_VALUE, "two": TOPIC_VALUE}

    def test_an_empty_scoped_read_is_distinguishable_from_an_empty_unscoped_one(
        self, tmp_path
    ):
        """Both produce no names and they mean different things: one is a user
        revoking their namespace on purpose, the other is a genuinely empty
        file. `scoped` is what separates them."""
        kp, scoped_path = _new_db(tmp_path, name="scoped.kdbx")
        kp.add_group(kp.root_group, "istota")
        kp.save()
        kp2, bare_path = _new_db(tmp_path, name="bare.kdbx")
        kp2.save()

        scoped, _ = _read(scoped_path)
        bare, _ = _read(bare_path)

        assert scoped.services == {} and scoped.scoped is True
        assert bare.services == {} and bare.scoped is False

    def test_an_istota_group_nominated_as_the_recycle_bin_still_scopes(
        self, tmp_path
    ):
        """It contributes no entries and it still narrows the read.

        The widening this refuses is reachable from a sandbox: the recycle bin
        is a Meta element in a file a task in that user's own sandbox can
        write, so treating "the `istota` group is the bin" as "there is no
        `istota` group" would let a task turn a scoped vault into a read of the
        user's whole file by editing one UUID.

        **The discriminating content is the entry outside the group**, not the
        empty result: a file whose only other content is the bin reads as `{}`
        under either rule, which is how the earlier version of this test passed
        while asserting the wider behaviour.
        """
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "shared", "", API_KEY_VALUE)
        kp.add_entry(
            kp.add_group(kp.root_group, "Personal"), "outside", "", TOPIC_VALUE
        )
        elem = kp._xpath("/KeePassFile/Meta/RecycleBinUUID", first=True)
        elem.text = base64.b64encode(root.uuid.bytes).decode()
        kp.save()

        read, _ = _read(path)

        assert "personal_outside" not in read.services, read.services
        assert read.scoped is True
        assert read.services == {}

    def test_a_recycle_bin_is_excluded_from_an_unscoped_read(self, tmp_path):
        """The one case where the bin check is the boundary rather than a
        backstop.

        A scoped read starts at the *children* of the `istota` group, and
        KeePassXC's bin is a root-level group, so a trashed credential is
        outside the read by construction. An unscoped read starts **at** the
        file root, where the bin is an ordinary subgroup — and
        `_visit_group`'s UUID test is then the only thing keeping a credential
        the user deleted out of `vault_entries`, where their own tasks could
        fetch it by name.
        """
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.add_group(kp.root_group, "live"), "key", "", API_KEY_VALUE)
        binned = kp.add_group(kp.root_group, "Recycle Bin")
        kp.add_entry(binned, "old", "", TOPIC_VALUE)
        kp.save()
        elem = kp._xpath("/KeePassFile/Meta/RecycleBinUUID", first=True)
        elem.text = base64.b64encode(binned.uuid.bytes).decode()
        kp.save()

        read, _ = _read(path)

        assert read.scoped is False
        assert "recycle_bin_old" not in read.services, read.services
        assert read.services == {"live_key": API_KEY_VALUE}

    def test_the_repr_carries_the_scope_and_still_no_value(self, tmp_path):
        kp, path = _new_db(tmp_path)
        kp.add_entry(kp.add_group(kp.root_group, "aws"), "key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert "scoped=False" in repr(read)
        assert API_KEY_VALUE not in repr(read)


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


@pytest.fixture
def secret_key_env():
    """A real `ISTOTA_SECRET_KEY` for the duration of a test.

    Same shape as `tests/test_secrets_store.py`'s fixture: the apply tests run
    against a real temp database through the real Fernet layer, because what
    they assert is the state of rows after a write and a delete, and a stand-in
    store would assert this suite's idea of `upsert_secret`'s three return
    values rather than `upsert_secret`'s.
    """
    with mock.patch.dict(os.environ, {"ISTOTA_SECRET_KEY": "deadbeef" * 8}):
        yield


def _vault_read(services, *, held=(), truncated="", scoped=True, skipped=()):
    """A `VaultRead` shaped as `parse_vault` would have produced it.

    `held` is the one field the applying half cannot derive from `services`: a
    name the file produced and could not supply a value for. It defaults to
    empty, which is the ordinary case, and the tests that turn on it pass it.

    The seam cases — an empty password, a duplicate title — deliberately do
    **not** use this helper. They go through a real KDBX and `parse_vault`,
    because the whole question there is what the parse puts in `held`, and
    building one here would assert this file's idea of the parse.
    """
    return VaultRead(
        digest="0" * 64,
        services=dict(services),
        held=frozenset(held),
        truncated=truncated,
        scoped=scoped,
        skipped=tuple(skipped),
    )


def _entry(db_path, user, name):
    return secrets_store.get_secret(db_path, user, VAULT_ENTRY_SERVICE, name)


class TestApply:
    """Writing a read into `vault_entries`, and sweeping the namespace.

    Every test here runs against a real temp database with a real master key:
    the subject is the state of rows, and two of the three counting states
    (`updated` against `unchanged`) are a property of what is already stored.
    """

    def test_a_first_apply_creates_every_name(self, db_path, secret_key_env):
        read = _vault_read({"karakeep_api_key": API_KEY_VALUE,
                            "github_pat": TOPIC_VALUE})

        result = apply_vault(db_path, "alice", read)

        assert (result.created, result.updated, result.unchanged) == (2, 0, 0)
        assert result.deleted == 0 and result.deleted_keys == []
        assert result.skipped == []
        assert result.swept is True
        assert _entry(db_path, "alice", "karakeep_api_key") == API_KEY_VALUE
        assert _entry(db_path, "alice", "github_pat") == TOPIC_VALUE

    def test_an_unchanged_value_is_counted_apart_from_an_updated_one(
        self, db_path, secret_key_env
    ):
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE,
                                                   "b": TOPIC_VALUE}))

        result = apply_vault(
            db_path, "alice", _vault_read({"a": API_KEY_VALUE, "b": "moved"})
        )

        assert (result.created, result.updated, result.unchanged) == (0, 1, 1)

    # ---- the namespace sweep --------------------------------------------

    def test_a_name_removed_from_the_file_is_deleted(self, db_path, secret_key_env):
        """The whole of what makes deleting an entry revoke it."""
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE,
                                                   "b": TOPIC_VALUE}))

        result = apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))

        assert result.deleted == 1 and result.deleted_keys == ["b"]
        assert _entry(db_path, "alice", "b") is None
        assert _entry(db_path, "alice", "a") == API_KEY_VALUE

    def test_an_empty_read_deletes_every_row(self, db_path, secret_key_env):
        """How a user revokes the whole namespace: empty the `istota` group, or
        empty the file. Both are unambiguous under §1."""
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE,
                                                   "b": TOPIC_VALUE}))

        result = apply_vault(db_path, "alice", _vault_read({}))

        assert result.deleted == 2
        assert sorted(result.deleted_keys) == ["a", "b"]

    def test_deletion_is_scoped_to_the_user(self, db_path, secret_key_env):
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))
        apply_vault(db_path, "bob", _vault_read({"a": TOPIC_VALUE}))

        apply_vault(db_path, "alice", _vault_read({}))

        assert _entry(db_path, "alice", "a") is None
        assert _entry(db_path, "bob", "a") == TOPIC_VALUE

    def test_another_service_is_never_touched(self, db_path, secret_key_env):
        """**The passphrase-isolation control**, and the one test in this file
        whose failure mode is a deployment that cannot open its own vault.

        The sweep enumerates `vault_entries` alone, and `vault/passphrase` is a
        different service — so the isolation is structural rather than a filter
        somebody has to keep right. A vault holding an entry the user titled
        `passphrase` is what makes that concrete: applied against a database
        already holding `vault/passphrase`, the stored passphrase is byte
        -identical afterwards and a *separate* row carries the file's value.
        """
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", "the-real-one")
        secrets_store.set_secret(db_path, "alice", "karakeep", "api_key", "typed")

        apply_vault(
            db_path, "alice", _vault_read({"passphrase": API_KEY_VALUE})
        )

        assert secrets_store.get_secret(
            db_path, "alice", "vault", "passphrase"
        ) == "the-real-one"
        assert secrets_store.get_secret(
            db_path, "alice", "karakeep", "api_key"
        ) == "typed"
        assert _entry(db_path, "alice", "passphrase") == API_KEY_VALUE

    def test_an_empty_read_leaves_the_passphrase_alone(self, db_path, secret_key_env):
        """The other half of the control: a sweep that deleted every row of
        every service would take the passphrase with it, and the vault could
        never be opened again."""
        secrets_store.set_secret(db_path, "alice", "vault", "passphrase", "the-real-one")
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))

        apply_vault(db_path, "alice", _vault_read({}))

        assert secrets_store.get_secret(
            db_path, "alice", "vault", "passphrase"
        ) == "the-real-one"

    def test_every_write_happens_before_any_delete(
        self, db_path, secret_key_env, monkeypatch
    ):
        """A failure part-way through must leave credentials present rather than
        absent, which is why the deletions are planned and executed at the end
        rather than inline."""
        apply_vault(db_path, "alice", _vault_read({"old": TOPIC_VALUE}))

        order: list[str] = []
        real_upsert = secrets_store.upsert_secret
        real_delete = secrets_store.delete_secret

        def _upsert(*args, **kwargs):
            order.append("write")
            return real_upsert(*args, **kwargs)

        def _delete(*args, **kwargs):
            order.append("delete")
            return real_delete(*args, **kwargs)

        monkeypatch.setattr(secrets_vault_module.secrets_store, "upsert_secret", _upsert)
        monkeypatch.setattr(secrets_vault_module.secrets_store, "delete_secret", _delete)

        apply_vault(db_path, "alice", _vault_read({"new": API_KEY_VALUE}))

        assert order == ["write", "delete"]

    # ---- what holds a deletion back --------------------------------------

    def test_a_held_name_is_not_deleted(self, db_path, secret_key_env):
        """The fat-finger case: a password field blanked in the file must not
        delete the credential it was meant to change."""
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))

        result = apply_vault(db_path, "alice", _vault_read({}, held=["a"]))

        assert result.deleted == 0 and result.deleted_keys == []
        assert _entry(db_path, "alice", "a") == API_KEY_VALUE

    def test_a_blanked_password_holds_its_row_through_a_real_parse(
        self, tmp_path, db_path, secret_key_env
    ):
        """The seam, driven end to end rather than through the helper: what the
        parse puts in `held` is the whole of what the apply declines to delete.
        """
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "github pat", "", API_KEY_VALUE)
        kp.save()
        apply_vault(db_path, "alice", _read(path)[0])
        assert _entry(db_path, "alice", "github_pat") == API_KEY_VALUE

        entry = kp.find_entries(title="github pat", first=True)
        entry.password = ""
        kp.save()
        result = apply_vault(db_path, "alice", _read(path)[0])

        assert result.deleted == 0
        assert _entry(db_path, "alice", "github_pat") == API_KEY_VALUE

    def test_a_truncated_read_withholds_every_deletion(self, db_path, secret_key_env):
        """A prefix of the file says nothing about a stored name's absence, so
        the sweep is withheld whole rather than filtered — there is no way to
        tell a name past the cap from one the user removed. Writes still land.
        """
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE,
                                                   "b": TOPIC_VALUE}))

        result = apply_vault(
            db_path, "alice", _vault_read({"a": "moved"}, truncated="entry")
        )

        assert result.swept is False
        assert result.deleted == 0 and result.deleted_keys == []
        assert _entry(db_path, "alice", "b") == TOPIC_VALUE
        assert _entry(db_path, "alice", "a") == "moved"

    def test_a_row_that_will_not_decrypt_is_never_deleted(
        self, db_path, secret_key_env
    ):
        """A stale master key is a transient misconfiguration; a delete makes it
        permanent. The credential comes back when the right key does and never
        from a delete."""
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE secrets SET encrypted_value = ? "
                "WHERE user_id = ? AND service = ? AND key = ?",
                (b"not-a-fernet-token", "alice", VAULT_ENTRY_SERVICE, "a"),
            )

        result = apply_vault(db_path, "alice", _vault_read({}))

        assert result.deleted == 0
        assert result.skipped == [("a", SKIP_UNREADABLE_ROW)]
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM secrets WHERE user_id = ? AND service = ?",
                ("alice", VAULT_ENTRY_SERVICE),
            ).fetchone()
        assert rows[0] == 1

    def test_an_unreadable_row_overwritten_is_counted_rather_than_created(
        self, db_path, secret_key_env
    ):
        """`upsert_secret` derives its own answer from `get_secret`, which
        reports an undecryptable row as absent — so on a deployment with a stale
        master key every write would otherwise read as `created` and the counts
        an operator reads would be exactly backwards."""
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE secrets SET encrypted_value = ? "
                "WHERE user_id = ? AND service = ? AND key = ?",
                (b"not-a-fernet-token", "alice", VAULT_ENTRY_SERVICE, "a"),
            )

        result = apply_vault(db_path, "alice", _vault_read({"a": TOPIC_VALUE}))

        assert (result.created, result.updated) == (0, 1)
        assert result.unreadable_overwrites == 1
        assert _entry(db_path, "alice", "a") == TOPIC_VALUE

    def test_a_missing_master_key_refuses_the_whole_pass(self, db_path):
        """Without it an empty read on a deployment that can neither read what
        it is removing nor write a replacement would sweep the namespace flat.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ISTOTA_SECRET_KEY", None)
            with pytest.raises(secrets_store.SecretKeyMissingError):
                apply_vault(db_path, "alice", _vault_read({}))

    # ---- reporting -------------------------------------------------------

    def test_the_reads_own_skips_travel_on_the_result(self, db_path, secret_key_env):
        read = _vault_read({}, skipped=[("aws_key", SKIP_DUPLICATE_NAME)])

        result = apply_vault(db_path, "alice", read)

        assert result.skipped == [("aws_key", SKIP_DUPLICATE_NAME)]

    def test_no_log_record_carries_a_value(self, db_path, secret_key_env, caplog):
        apply_vault(db_path, "alice", _vault_read({"a": API_KEY_VALUE}))
        with caplog.at_level(logging.DEBUG, logger="istota.secrets_vault"):
            apply_vault(
                db_path, "alice", _vault_read({"b": TOPIC_VALUE}, truncated="entry")
            )
            apply_vault(db_path, "alice", _vault_read({}))

        for message in _ours(caplog):
            assert API_KEY_VALUE not in message
            assert TOPIC_VALUE not in message

    def test_the_skip_vocabulary_is_exactly_five(self):
        """The four service-mapping reasons went with the machinery that
        produced them. A reason with no producer reads as a condition the apply
        can still reach."""
        assert secrets_vault_module.SKIP_REASONS == frozenset({
            SKIP_UNUSABLE_NAME,
            SKIP_DUPLICATE_NAME,
            secrets_vault_module.SKIP_EMPTY_VALUE,
            SKIP_OVERSIZE_VALUE,
            SKIP_UNREADABLE_ROW,
        })

    @pytest.mark.parametrize(
        "name",
        [
            "SKIP_RESERVED_SERVICE",
            "SKIP_INELIGIBLE_SERVICE",
            "SKIP_UNKNOWN_KEY",
            "SKIP_DELETE_HELD",
            "eligible_services",
            "service_refusal",
            "_service_refusal",
            "DAEMON_WRITTEN_SERVICES",
            "_near_miss_title",
            "vault_owned_services",
            "format_skip",
        ],
    )
    def test_the_removed_machinery_is_gone(self, name):
        """A drift guard rather than a behaviour: each of these is on the
        stage's own removal list, and one coming back would bring the service
        mapping's reasoning with it."""
        assert not hasattr(secrets_vault_module, name), name


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
    """`vault_path` and `scheduler.vault_sync_interval`.

    Asserted as a round trip through `load_config` rather than against the
    dataclass alone, because "declared, documented, and read by nothing" is a
    defect class `config_mapper.py` records eleven instances of — and for both
    of these the symptom is a feature the operator configured and the daemon
    never ran.
    """

    def test_the_defaults_leave_the_feature_off(self, tmp_path):
        config = _load_config_text(tmp_path, 'bot_name = "Istota"\n')
        assert config.scheduler.vault_sync_interval == 300
        assert UserConfig().vault_path == ""

    def test_the_user_field_round_trips(self, tmp_path):
        config = _load_config_text(tmp_path, """
            [users.alice]
            vault_path = "istota/vault/credentials.kdbx"
        """)
        assert (
            config.users["alice"].vault_path == "istota/vault/credentials.kdbx"
        )

    def test_the_interval_round_trips(self, tmp_path):
        config = _load_config_text(
            tmp_path, "[scheduler]\nvault_sync_interval = 60\n"
        )
        assert config.scheduler.vault_sync_interval == 60

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


class TestTheProfileTableGuard:
    """`vault_path` may not be settable by anything downstream of a task.

    `user_profiles` is writable from the settings UI, and every other per-user
    scalar is overlaid from it by `_apply_user_profiles`. This one is a security
    control rather than a preference: it selects which file the daemon decrypts
    with a key it holds, and it outranks the folder. What a *user* may set is a
    filename out of their own folder, which lives in the reserved `_vault_file`
    KV namespace and is guarded there.

    Two halves, and neither covers the other. The column set is read off a real
    initialised database rather than grepped out of `schema.sql`, because
    `_run_migrations` adds columns with `ALTER TABLE` and a grep would not see
    one. The behavioural half drives the overlay itself, because a column is not
    the only way a value could arrive — `merge_into_user_config` sets attributes
    by name and could set this one from anywhere.
    """

    def test_the_table_carries_no_vault_column(self, db_path):
        with sqlite3.connect(db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(user_profiles)")
            }
        assert columns, "the table must exist for this assertion to mean anything"
        assert "vault_path" not in columns

    def test_the_overlay_leaves_the_field_alone(self, db_path):
        from istota import user_profiles as up

        up.ensure_profile(db_path, "alice", display_name="Alice")
        rows = up.list_profiles(db_path)
        assert "alice" in rows

        user = UserConfig(
            vault_path="istota/vault/credentials.kdbx",
            display_name="from-toml",
        )
        up.merge_into_user_config(rows["alice"], user)

        # The control: the overlay demonstrably ran on this object, so the
        # assertion below is about what it declined to touch rather than about a
        # call that did nothing.
        assert user.display_name == "Alice"
        assert user.vault_path == "istota/vault/credentials.kdbx"

    def test_the_profile_dataclass_declares_no_vault_field(self):
        from istota.user_profiles import UserProfile

        names = {f.name for f in dataclasses.fields(UserProfile)}
        assert "vault_path" not in names


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


class TestThePassphraseFloor:
    """What `passphrase_refusal` accepts, and the asymmetry that broke a vault.

    The floor used to measure `value.strip()` while every caller stored `value`
    itself. A passphrase pasted with a trailing newline — what copying out of a
    password manager, a terminal or a file gives you — therefore satisfied a
    check about one string and was stored as a different one, after which the
    unlock failed for the life of the deployment and reported `VaultLocked`:
    "the stored passphrase does not match the file", about a password that was
    correct. Found in production, not by a test.
    """

    def test_a_long_enough_passphrase_is_accepted(self):
        assert secrets_vault_module.passphrase_refusal("x" * 32) is None

    def test_a_short_passphrase_is_refused_by_length(self):
        refusal = secrets_vault_module.passphrase_refusal("x" * 31)
        assert refusal is not None
        assert "at least" in refusal

    def test_a_trailing_newline_is_refused_rather_than_stripped(self):
        refusal = secrets_vault_module.passphrase_refusal("x" * 32 + "\n")
        assert refusal is not None
        assert "line break" in refusal

    def test_a_trailing_space_is_refused(self):
        assert secrets_vault_module.passphrase_refusal("x" * 32 + " ") is not None

    def test_a_leading_space_is_refused(self):
        assert secrets_vault_module.passphrase_refusal(" " + "x" * 32) is not None

    def test_the_length_is_measured_on_what_will_be_stored(self):
        """The discriminating case, and the whole point of the change.

        Thirty-two characters of padding and one of password: the old floor
        measured the stripped form and so refused this, which was right — but
        it measured the stripped form of an *acceptable* value too, which is
        what let a padded 32-character passphrase through. Both arms have to
        agree that the value stored is the value judged.
        """
        padded = " " * 32 + "x"
        assert secrets_vault_module.passphrase_refusal(padded) is not None

    def test_a_generated_passphrase_is_never_refused(self):
        """The remedy the floor exists to steer people towards must survive it."""
        for _ in range(20):
            assert secrets_vault_module.passphrase_refusal(
                secrets_vault_module.generate_passphrase()
            ) is None

    def test_whitespace_inside_a_passphrase_is_untouched(self):
        """Only the ends are ambiguous. A passphrase of words is ordinary."""
        assert secrets_vault_module.passphrase_refusal("correct horse battery staple xyz") is None
