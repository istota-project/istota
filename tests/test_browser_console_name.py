"""The console socket name shared by the browser pool and the web relay."""

from __future__ import annotations

import hashlib
import re

from istota.lib.browser_console import console_socket_name

_SHAPE = re.compile(r"[0-9a-f]{32}\.sock")


def test_name_is_the_sha256_prefix_of_the_utf8_id():
    expected = hashlib.sha256(b"alice").hexdigest()[:32] + ".sock"
    assert console_socket_name("alice") == expected


def test_name_is_stable_and_shaped():
    first = console_socket_name("alice")
    assert first == console_socket_name("alice")
    assert _SHAPE.fullmatch(first)


def test_two_ids_get_two_names():
    assert console_socket_name("alice") != console_socket_name("bob")


def test_briefings_instance_has_a_name():
    assert _SHAPE.fullmatch(console_socket_name("__briefings__"))


def test_non_ascii_id_is_hashed_as_utf8():
    name = console_socket_name("zoë")
    assert _SHAPE.fullmatch(name)
    assert name == hashlib.sha256("zoë".encode("utf-8")).hexdigest()[:32] + ".sock"


def test_long_id_still_gives_a_short_name():
    user_id = "a" * 255
    name = console_socket_name(user_id)
    assert _SHAPE.fullmatch(name)
    assert len(name.encode("utf-8")) < 60
