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
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import sys

from unittest import mock

import pytest

from istota.secrets_vault import (
    VAULT_READ_CAP_BYTES,
    VaultCorrupt,
    VaultLibraryMissing,
    VaultLocked,
    VaultMissing,
    VaultRead,
    VaultUnreadable,
    parse_vault,
    read_vault_bytes,
)
from istota.skills._loader import (
    OVERLAY_IS_A_SYMLINK,
    OVERLAY_NOT_A_REGULAR_FILE,
    OVERLAY_UNREADABLY_LARGE,
)

PASSPHRASE = "fixture-passphrase-not-a-real-one"

# Distinctive enough that `test_no_log_record_carries_a_value` finding one in a
# log record is unambiguous, and shaped like the real thing so the mapping
# assertions are about plausible input.
API_KEY_VALUE = "ak-vault-fixture-alpha"
BASE_URL_VALUE = "https://karakeep.example.com"
TOPIC_VALUE = "ntfy-fixture-topic"


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

    # ---- the mapping ---------------------------------------------------

    def test_the_mapping(self, tmp_path):
        """Group path `istota/<service>`, entry title is the key, password the
        value."""
        _, path = _standard_vault(tmp_path)

        read, digest = _read(path)

        assert read.services == {
            "karakeep": {"base_url": BASE_URL_VALUE, "api_key": API_KEY_VALUE},
            "ntfy": {"topic": TOPIC_VALUE},
        }
        assert read.group_present == {"karakeep": "karakeep", "ntfy": "ntfy"}
        assert read.digest == digest

    def test_the_digest_is_the_sha256_of_the_file_bytes(self, tmp_path):
        """Both halves compute it, because §7 hashes to decide whether to parse
        at all and `VaultRead` carries it onward. They must agree."""
        _, path = _standard_vault(tmp_path)

        data, digest = read_vault_bytes(path)

        assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
        assert data == path.read_bytes()
        assert parse_vault(data, PASSPHRASE).digest == digest

    def test_everything_but_the_title_and_the_password_is_ignored(self, tmp_path):
        """Username, URL and notes are not keys and not values."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(
            karakeep,
            "api_key",
            "a-username",
            API_KEY_VALUE,
            url="https://example.invalid",
            notes="a note",
        )
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"api_key": API_KEY_VALUE}}

    def test_values_are_stripped_of_surrounding_whitespace(self, tmp_path):
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", f"  {API_KEY_VALUE}\n")
        kp.save()

        read, _ = _read(path)

        assert read.services["karakeep"]["api_key"] == API_KEY_VALUE

    def test_an_entry_directly_under_the_root_group_is_ignored(self, tmp_path):
        """`istota/<key>` with no service subgroup belongs to no service."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(root, "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}
        assert read.group_present == {}

    def test_a_vault_with_no_istota_group_reads_as_empty(self, tmp_path):
        """Not an error: a KDBX that parses and mentions no service is a vault
        with no opinion, which §6 rule 1 turns into silence rather than
        deletion."""
        kp, path = _new_db(tmp_path)
        other = kp.add_group(kp.root_group, "Personal")
        kp.add_entry(other, "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}
        assert read.group_present == {}

    def test_an_empty_service_group_is_still_present(self, tmp_path):
        """Presence, not content, is what §6's deletion rule fires on, so a
        group holding no entries has to reach `group_present` with an empty
        bucket beside it rather than being dropped as uninteresting."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_group(root, "ntfy")
        kp.save()

        read, _ = _read(path)

        assert read.group_present == {"ntfy": "ntfy"}
        assert read.services == {"ntfy": {}}

    # ---- the skip rules ------------------------------------------------

    def test_an_empty_password_is_skipped_rather_than_deleting(self, tmp_path):
        """`set_secret` reads an empty value as a deletion, and the vault
        deliberately does not expose that: an empty password field is a far
        likelier fat-finger than a deliberate one, so the key is dropped from
        the read entirely. Deleting a credential means deleting the entry."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", "")
        kp.add_entry(karakeep, "base_url", "", BASE_URL_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"base_url": BASE_URL_VALUE}}
        assert "karakeep" in read.group_present

    def test_a_whitespace_only_password_is_skipped_too(self, tmp_path):
        """Values are stripped before the emptiness test, or a key whose value
        is a stray newline lands in the table as an empty string — which
        `set_secret` reads as a deletion."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "karakeep"), "api_key", "", "   \n ")
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {}}

    def test_a_duplicate_title_skips_that_key_and_only_that_key(self, tmp_path):
        """Picking either copy silently would make which credential is live
        depend on the XML order."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", "ak-first")
        kp.add_entry(karakeep, "api_key", "", "ak-second", force_creation=True)
        kp.add_entry(karakeep, "base_url", "", BASE_URL_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"base_url": BASE_URL_VALUE}}

    def test_a_duplicate_whose_first_copy_is_empty_is_still_a_duplicate(
        self, tmp_path
    ):
        """The ordering case, and the branch a refactor reorders.

        An empty-password entry has to be recorded as *seen* before it is
        skipped, or `api_key` (empty) followed by `api_key` (a value) resolves
        to the second one — which is the silent XML-order dependence the rule
        above exists to refuse, arrived at from the other direction."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", "")
        kp.add_entry(karakeep, "api_key", "", API_KEY_VALUE, force_creation=True)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {}}

    def test_an_untitled_entry_is_skipped(self, tmp_path):
        """An entry with no title names no key. It must not raise on the way
        past, which a bare `title.strip()` would."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "", "", API_KEY_VALUE)
        kp.add_entry(karakeep, "base_url", "", BASE_URL_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"base_url": BASE_URL_VALUE}}

    # ---- the recycle bin -----------------------------------------------

    def test_a_trashed_entry_is_excluded(self, tmp_path):
        """KeePassXC moves a deleted entry into the recycle bin rather than
        removing it, so a credential the user deleted is still in the file."""
        kp, path = _standard_vault(tmp_path)
        entry = kp.find_entries(title="api_key", first=True)
        kp.trash_entry(entry)
        kp.save()

        read, _ = _read(path)

        assert read.services["karakeep"] == {"base_url": BASE_URL_VALUE}

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
        assert read.group_present == {}

    def test_a_service_group_nominated_as_the_recycle_bin_is_excluded(
        self, tmp_path
    ):
        """The defensive half, for the layout the walk does not exclude by
        construction.

        KeePassXC lets any group be the recycle bin, so `istota/karakeep` can be
        one — at which point every entry in it is something the user deleted,
        sitting exactly where the walk looks. Nominated here by writing the Meta
        element, because there is no public setter and the point is the file
        this reader will meet rather than the API that produced it."""
        kp, path = _standard_vault(tmp_path)
        karakeep = kp.find_groups(name="karakeep", first=True)
        elem = kp._xpath("/KeePassFile/Meta/RecycleBinUUID", first=True)
        elem.text = base64.b64encode(karakeep.uuid.bytes).decode()
        kp.save()

        read, _ = _read(path)

        assert read.services == {"ntfy": {"topic": TOPIC_VALUE}}
        assert read.group_present == {"ntfy": "ntfy"}

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
        assert read.group_present == {}

    # ---- what the walk's shape excludes --------------------------------

    def test_an_entry_nested_deeper_than_one_subgroup_is_not_a_key(self, tmp_path):
        """`istota/<service>/<key>` and no deeper.

        Implemented by *not* recursing — `Group.entries` is direct children —
        so nothing in the module states this and only a fixture holds it. A
        pykeepass change to recursive `entries`, or a refactor to
        `find_entries(..., recursive=True)`, would start importing credentials
        from an arbitrary depth with the rest of this file green."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", API_KEY_VALUE)
        deeper = kp.add_group(karakeep, "deeper")
        kp.add_entry(deeper, "base_url", "", BASE_URL_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"api_key": API_KEY_VALUE}}

    def test_an_edited_entrys_history_is_not_a_duplicate_of_it(self, tmp_path):
        """The expensive one if `Group.entries` ever stops meaning current
        entries: a KDBX keeps previous versions of an edited entry as `History`
        children, so every credential the user has ever changed would become a
        duplicate hard-skip — silently, and for the keys they touch most."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        entry = kp.add_entry(karakeep, "api_key", "", "ak-the-old-value")
        entry.save_history()
        entry.password = API_KEY_VALUE
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"api_key": API_KEY_VALUE}}

    def test_a_whitespace_only_group_name_is_not_a_service(self, tmp_path):
        """§6's deletion rule fires on presence in `group_present`, so a junk row
        there is a row that can delete. The name is not stripped for *matching* —
        §3 strips values and normalizes nothing else — only tested for being
        entirely whitespace."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "   "), "api_key", "", API_KEY_VALUE)
        kp.add_entry(kp.add_group(root, "karakeep"), "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.group_present == {"karakeep": "karakeep"}
        assert read.services == {"karakeep": {"api_key": API_KEY_VALUE}}

    # ---- ownership and folding -----------------------------------------

    def test_an_unowned_group_is_parsed_and_reported(self, tmp_path):
        """`parse_vault` knows nothing about `vault_services`: it reports every
        group it found, so `vault-status` can tell a user their group name
        matches no owned service."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "not_a_service"), "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.group_present == {"not_a_service": "not_a_service"}
        assert read.services == {"not_a_service": {"api_key": API_KEY_VALUE}}

    def test_the_service_segment_is_matched_case_folded(self, tmp_path):
        """`Karakeep` owns `karakeep`. A phone keyboard autocapitalizes a group
        name it takes for the start of a sentence, and a file that looks right
        owning nothing is the hardest failure here to diagnose."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "Karakeep"), "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"api_key": API_KEY_VALUE}}
        # The folded name is what `vault_services` is compared against; the
        # value is the spelling to print back, so a user is shown the name they
        # typed rather than one they never wrote.
        assert read.group_present == {"karakeep": "Karakeep"}

    def test_an_entry_title_is_not_folded(self, tmp_path):
        """Only the service segment folds. A key that does not match the schema
        is reported as a typo at apply time rather than silently accepted."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        kp.add_entry(kp.add_group(root, "karakeep"), "API_KEY", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"API_KEY": API_KEY_VALUE}}

    def test_the_top_level_group_name_is_not_folded(self, tmp_path):
        """The other half of "nothing else does". `Istota/` is not the vault's
        group, and reporting nothing is what sends the user to `vault-status`
        rather than leaving them with a half-read file."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "Istota")
        kp.add_entry(kp.add_group(root, "karakeep"), "api_key", "", API_KEY_VALUE)
        kp.save()

        read, _ = _read(path)

        assert read.services == {}
        assert read.group_present == {}

    def test_groups_that_fold_together_are_pooled(self, tmp_path):
        """Two spellings of one service are one service.

        The spec does not name this case; it is the duplicate-title rule's own
        premise one level up, since `Karakeep/api_key` beside `karakeep/api_key`
        is the same order-dependent choice. Distinct keys merge, a key in both
        is a duplicate skip, and `group_present` keeps the first spelling
        found."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        upper = kp.add_group(root, "Karakeep")
        lower = kp.add_group(root, "karakeep")
        kp.add_entry(upper, "base_url", "", BASE_URL_VALUE)
        kp.add_entry(upper, "api_key", "", "ak-from-upper")
        kp.add_entry(lower, "api_key", "", "ak-from-lower")
        kp.save()

        read, _ = _read(path)

        assert read.services == {"karakeep": {"base_url": BASE_URL_VALUE}}
        assert read.group_present == {"karakeep": "Karakeep"}

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

        assert parse_vault(data, "a-rotated-passphrase").services["ntfy"] == {
            "topic": TOPIC_VALUE
        }

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
        assert parse_vault(data, PASSPHRASE).group_present == {
            "karakeep": "karakeep",
            "ntfy": "ntfy",
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
            services={"karakeep": {"api_key": API_KEY_VALUE}},
            group_present={"karakeep": "karakeep"},
        )

        rendered = repr(read)

        assert API_KEY_VALUE not in rendered
        assert "0" * 64 in rendered
        assert "karakeep" in rendered
        # A list's repr calls repr on each element, so a `__str__`-only override
        # would be bypassed by every real failure this protects.
        assert API_KEY_VALUE not in repr([read])

    def test_no_log_record_carries_a_value(self, tmp_path, caplog):
        """Counts, service names and key names are loggable. Values are not, at
        any level — so the fixture below walks every branch that logs."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", API_KEY_VALUE)
        kp.add_entry(karakeep, "api_key", "", "ak-the-duplicate", force_creation=True)
        kp.add_entry(karakeep, "base_url", "", "")
        kp.add_entry(karakeep, "", "", "untitled-value")
        ntfy = kp.add_group(root, "ntfy")
        kp.add_entry(ntfy, "topic", "", TOPIC_VALUE)
        kp.save()
        secrets = {
            API_KEY_VALUE,
            "ak-the-duplicate",
            "untitled-value",
            TOPIC_VALUE,
            PASSPHRASE,
        }

        with caplog.at_level(logging.DEBUG):
            read, _ = _read(path)

        assert read.services == {"karakeep": {}, "ntfy": {"topic": TOPIC_VALUE}}
        # Scoped to this module's own logger, and counted. `caplog.records`
        # non-empty proves nothing on its own: pykeepass emits five DEBUG
        # records during an ordinary parse, so the sweep was satisfied by a
        # third party's output and stayed green with all three warnings deleted.
        ours = [r for r in caplog.records if r.name == "istota.secrets_vault"]
        assert len(ours) == 3, [r.getMessage() for r in ours]
        for record in caplog.records:
            rendered = f"{record.getMessage()} {record.args!r} {record.exc_text!r}"
            for value in secrets:
                assert value not in rendered, (
                    f"{record.name} logged a fixture value: {record.getMessage()!r}"
                )

    def test_each_skip_says_which_key_it_skipped(self, tmp_path, caplog):
        """§3 asks for a warning naming the service and the key, and the
        behavioural assertions above cannot see whether one was emitted — they
        prove the branch ran, not that it said anything."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api_key", "", API_KEY_VALUE)
        kp.add_entry(karakeep, "api_key", "", "ak-the-duplicate", force_creation=True)
        kp.add_entry(karakeep, "base_url", "", "")
        kp.add_entry(karakeep, "", "", "untitled-value")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            _read(path)

        said = [r.getMessage() for r in caplog.records
                if r.name == "istota.secrets_vault"]
        assert any("karakeep" in m and "api_key" in m and "more than once" in m
                   for m in said), said
        assert any("karakeep" in m and "base_url" in m and "empty password" in m
                   for m in said), said
        assert any("karakeep" in m and "no title" in m for m in said), said

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

        said = [r.getMessage() for r in caplog.records
                if r.name == "istota.secrets_vault"]
        assert said == ["vault: karakeep has 4 entries with no title, skipped"]

    def test_a_name_cannot_forge_a_log_line(self, tmp_path, caplog):
        """Group and entry names are arbitrary strings out of the file:
        unbounded, and free to carry a newline. Unflattened, one of them can put
        a whole fabricated record into the daemon's log. Self-inflicted rather
        than attacker-reachable — the file is the user's — so this flattens and
        bounds rather than refusing."""
        kp, path = _new_db(tmp_path)
        root = kp.add_group(kp.root_group, "istota")
        karakeep = kp.add_group(root, "karakeep")
        kp.add_entry(karakeep, "api\nkey ERROR forged line", "", "")
        kp.add_entry(kp.add_group(root, "x" * 400), "api_key", "", "")
        kp.save()

        with caplog.at_level(logging.WARNING, logger="istota.secrets_vault"):
            _read(path)

        for record in caplog.records:
            if record.name != "istota.secrets_vault":
                continue
            assert "\n" not in record.getMessage()
            assert len(record.getMessage()) < 300

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
