"""Every installation credential `Config` reads has a stack secrets file.

`admin_config_view.is_secret_path` is the redaction rule and is broad on
purpose. This guard needs an exact answer instead: every leaf it flags must be
in `reference.INSTALLATION_CREDENTIALS` (it has a secrets file) or in
`reference.NOT_CREDENTIALS` (it is not a credential, with the reason), and in
only one of them. A new credential field with neither fails here, which is
what stops the web UI from having to write the next one into `config.toml`.
"""

from __future__ import annotations

import dataclasses
import typing

import pytest

from istota import config as config_module
from istota import setup_wizard
from istota.config import Config
from istota.config_editor import reference
from istota.config_editor.reference import _collection_of_dataclasses
from istota.webui.admin_config_view import is_secret_path

#: The keys a dict-of-dataclasses field is walked under. A dict field's keys
#: are path segments (`servarr.apps.radarr.api_key`), so the walk has to know
#: them; the arr apps are a closed set.
COLLECTION_KEYS: dict[str, tuple[str, ...]] = {
    "servarr.apps": config_module.SERVARR_APPS,
}

#: Collections of dataclasses the walk does not enter, each with the reason.
NOT_INSTALLATION_SCOPED: dict[str, str] = {
    "users": "per-user config; a user's credentials live in their own vault",
    "default_briefings": "briefing definitions: schedules, sources and room tokens",
    "briefing_shared_blocks": "briefing text blocks shared between users",
}


class UnclassifiedCollection(Exception):
    pass


def leaf_paths(
    owner: type,
    collection_keys: dict[str, tuple[str, ...]],
    skipped: typing.Collection[str],
    prefix: str = "",
) -> list[str]:
    """Every leaf of a dataclass tree, as a dotted path, read off the types.

    A nested dataclass is entered. A dict of dataclasses is entered once per
    key in `collection_keys`; one named in neither that nor `skipped` raises,
    so a new collection field cannot pass the guard by being invisible to it.
    A dict of plain values (`extra_headers`) is a leaf.
    """
    hints = typing.get_type_hints(owner)
    found: list[str] = []
    for field in dataclasses.fields(owner):
        path = f"{prefix}.{field.name}" if prefix else field.name
        declared = hints[field.name]
        if dataclasses.is_dataclass(declared):
            found.extend(leaf_paths(declared, collection_keys, skipped, path))
            continue
        if _collection_of_dataclasses(owner, field):
            if path in skipped:
                continue
            if path not in collection_keys:
                raise UnclassifiedCollection(path)
            item_type = next(arg for arg in typing.get_args(declared) if dataclasses.is_dataclass(arg))
            for key in collection_keys[path]:
                found.extend(leaf_paths(item_type, collection_keys, skipped, f"{path}.{key}"))
            continue
        found.append(path)
    return found


def unclassified(
    paths: typing.Iterable[str],
    is_secret: typing.Callable[[tuple[str, ...]], bool],
    credentials: typing.Collection[str],
    not_credentials: typing.Collection[str],
) -> list[str]:
    """The flagged paths that are in neither list, or in both."""
    return [
        path for path in paths
        if is_secret(tuple(path.split(".")))
        and (path in credentials) == (path in not_credentials)
    ]


CONFIG_LEAVES = leaf_paths(Config, COLLECTION_KEYS, NOT_INSTALLATION_SCOPED)
FLAGGED = [path for path in CONFIG_LEAVES if is_secret_path(tuple(path.split(".")))]


class TestTheGuard:
    @pytest.mark.parametrize("path", FLAGGED)
    def test_every_flagged_path_is_a_credential_with_a_file_or_not_a_credential(self, path):
        found = unclassified(
            [path], is_secret_path, reference.INSTALLATION_CREDENTIALS, reference.NOT_CREDENTIALS,
        )
        assert not found, (
            f"{path} looks like a credential. Give it a stack secrets file (a "
            f"`setup_wizard.SECRET_NAMES` entry named after the variable "
            f"`load_config` reads it from), or add it to "
            f"`reference.NOT_CREDENTIALS` with the reason it is not one."
        )

    def test_the_walk_enters_the_arr_apps(self):
        assert "servarr.apps.radarr.api_key" in FLAGGED

    def test_the_two_lists_do_not_overlap(self):
        assert not set(reference.INSTALLATION_CREDENTIALS) & set(reference.NOT_CREDENTIALS)


class TestTheLists:
    def test_every_credential_file_is_a_secret_name(self):
        assert set(reference.INSTALLATION_CREDENTIALS.values()) <= set(setup_wizard.SECRET_NAMES)

    def test_every_credential_is_a_field(self):
        assert set(reference.INSTALLATION_CREDENTIALS) <= set(CONFIG_LEAVES)

    @pytest.mark.parametrize("path", sorted(reference.NOT_CREDENTIALS))
    def test_every_not_credential_is_a_flagged_field_with_a_reason(self, path):
        assert path in FLAGGED, f"{path} is not a field `is_secret_path` flags; drop it"
        assert reference.NOT_CREDENTIALS[path].strip()

    def test_credential_files_is_the_list_plus_the_credentials_with_no_file(self):
        files = reference.credential_files()
        assert {path: name for path, name in files.items() if name} == reference.INSTALLATION_CREDENTIALS


@dataclasses.dataclass
class _App:
    url: str = ""
    api_key: str = ""


@dataclasses.dataclass
class _Mail:
    host: str = ""
    password: str = ""


@dataclasses.dataclass
class _Root:
    mail: _Mail = dataclasses.field(default_factory=_Mail)
    apps: dict[str, _App] = dataclasses.field(default_factory=dict)
    retries: int = 3


def _fake_secret(path: tuple[str, ...]) -> bool:
    return path[-1] in ("password", "api_key")


class TestTheWalker:
    def test_a_planted_credential_without_a_file_is_reported(self):
        paths = leaf_paths(_Root, {"apps": ("one",)}, ())
        assert unclassified(paths, _fake_secret, {"apps.one.api_key"}, set()) == ["mail.password"]

    def test_a_credential_in_both_lists_is_reported(self):
        paths = leaf_paths(_Root, {"apps": ("one",)}, ())
        found = unclassified(paths, _fake_secret, {"mail.password", "apps.one.api_key"}, {"mail.password"})
        assert found == ["mail.password"]

    def test_a_dict_of_dataclasses_is_walked_per_key(self):
        paths = leaf_paths(_Root, {"apps": ("one", "two")}, ())
        assert paths == [
            "mail.host", "mail.password",
            "apps.one.url", "apps.one.api_key", "apps.two.url", "apps.two.api_key",
            "retries",
        ]

    def test_an_unclassified_collection_is_refused(self):
        with pytest.raises(UnclassifiedCollection, match="apps"):
            leaf_paths(_Root, {}, ())

    def test_a_skipped_collection_is_not_entered(self):
        assert leaf_paths(_Root, {}, {"apps"}) == ["mail.host", "mail.password", "retries"]
