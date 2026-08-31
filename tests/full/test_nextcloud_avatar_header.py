"""What a real Nextcloud 30 says when asked for an avatar.

`src/istota/nextcloud/avatars.py` refuses to import a picture unless it can tell
a user-set avatar from the coloured letter Nextcloud generates for a user who
set none. It cannot tell them apart from the body — both are PNGs of the same
shape at the same size — so it reads one response header, and everything else
about the feature rests on that header's name being right.

**This is the reason a full-tier file exists for two HTTP calls.** Every case in
`tests/test_nextcloud_avatar_import.py` scripts the header it then checks for,
using the module's own constant on both sides, so a wrong spelling passes the
whole file. The failure that spelling produces is not a crash: `fetch_avatar`
degrades to "no custom avatar" for every user on the deployment, the import goes
quietly dead, and the only visible symptom is that nobody's picture ever
appears. That is exactly the shape of defect this repository's negative-control
discipline exists for, and the only place it is answerable is against the real
server.

Observed on `nextcloud:30-apache` — see `TestTheCustomAvatarHeader`, which
prints the whole header set on failure so the next spelling change is one read
away rather than a re-derivation.

The two cases use two different users on purpose. The stack is session-scoped
and `reset()` does not un-set an avatar, so a file where one test sets a picture
and another asserts its absence is a file that depends on its own declaration
order. `test_user` is never given one here; the bot is.
"""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.full

FULL = pytest.mark.profile("full")

CONTAINER_CONFIG = "/data/config/config.toml"

_PREAMBLE = (
    "import pathlib;"
    "from istota.config import load_config;"
    f"c = load_config(pathlib.Path('{CONTAINER_CONFIG}'));"
)


def _run(stack, snippet: str) -> str:
    """One Python snippet inside the istota container, or a readable failure."""
    result = stack.exec(
        ["uv", "run", "python", "-c", _PREAMBLE + snippet], timeout=180
    )
    assert result.returncode == 0, (
        f"the snippet exited {result.returncode}\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _tagged(output: str, tag: str) -> str:
    for line in output.splitlines():
        if line.startswith(tag + " "):
            return line[len(tag) + 1:]
    raise AssertionError(f"no {tag!r} line in:\n{output}")


def _picture() -> bytes:
    """A small, unmistakably user-set picture. Small because it travels in argv."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (17, 119, 204)).save(buf, "PNG")
    return buf.getvalue()


def _header(headers: dict, name: str) -> tuple[str, str] | None:
    """The (name, value) pair whose name matches `name`, case-insensitively.

    Returns the name *as the server spelled it*, which is half of what this file
    is here to record. HTTP header names are case-insensitive and `httpx` treats
    them so, but a constant written down wrong is still worth knowing about.
    """
    for key, value in headers.items():
        if key.lower() == name.lower():
            return key, value
    return None


@FULL
class TestTheCustomAvatarHeader:
    """The open question Stage 4 of the profile-icons spec was blocked on."""

    def test_a_generated_avatar_is_marked_as_not_custom(self, stack):
        from istota.nextcloud.avatars import CUSTOM_AVATAR_HEADER

        nextcloud = stack.service("nextcloud")

        status, headers, length = nextcloud.avatar_response(nextcloud.test_user)

        assert status == 200, f"the avatar endpoint answered {status}"
        assert length > 0, "Nextcloud generates a picture even with none set"
        found = _header(headers, CUSTOM_AVATAR_HEADER)
        assert found is not None, (
            f"no header matching {CUSTOM_AVATAR_HEADER!r} came back, so nothing "
            "can tell a user-set picture from the coloured letter Nextcloud "
            f"generates. What did come back:\n{sorted(headers)}"
        )
        name, value = found
        assert name == CUSTOM_AVATAR_HEADER, (
            f"the server spells it {name!r}; the module says "
            f"{CUSTOM_AVATAR_HEADER!r}"
        )
        assert value.strip().lower() not in {"1", "true", "yes", "on"}, (
            f"{name} was {value!r} for a user who has set no avatar"
        )

    def test_a_user_set_avatar_is_marked_as_custom(self, stack):
        from istota.nextcloud.avatars import CUSTOM_AVATAR_HEADER

        nextcloud = stack.service("nextcloud")
        nextcloud.set_avatar(nextcloud.bot_user, _picture())

        status, headers, length = nextcloud.avatar_response(nextcloud.bot_user)

        assert status == 200
        assert length > 0
        found = _header(headers, CUSTOM_AVATAR_HEADER)
        assert found is not None, (
            f"no header matching {CUSTOM_AVATAR_HEADER!r}. What did come "
            f"back:\n{sorted(headers)}"
        )
        name, value = found
        assert name == CUSTOM_AVATAR_HEADER
        assert value.strip().lower() in {"1", "true", "yes", "on"}, (
            f"{name} was {value!r} for a user whose avatar was just set"
        )

    def test_an_etag_comes_back_so_the_next_tick_can_revalidate(self, stack):
        """The negative probe row stores this, and without one every tick
        re-downloads a placeholder for every user, forever."""
        nextcloud = stack.service("nextcloud")

        _, headers, _ = nextcloud.avatar_response(nextcloud.test_user)

        assert _header(headers, "ETag") is not None, (
            f"no ETag on the avatar response. Headers:\n{sorted(headers)}"
        )


@FULL
class TestTheDaemonsOwnReading:
    """`fetch_avatar` itself, in the shipped image, against the same server.

    The class above pins the header. This pins that the module *acts* on it —
    same code, same credentials, same internal URL as the import job, so a
    passing pair here is the feature working end to end rather than a constant
    matching a string.
    """

    def test_it_classifies_a_generated_avatar_as_no_custom_avatar(self, stack):
        nextcloud = stack.service("nextcloud")

        out = _run(
            stack,
            "from istota.nextcloud import avatars as a;"
            f"r = a.fetch_avatar(c, {nextcloud.test_user!r});"
            "print('KIND', type(r).__name__);"
            "print('SEEN', getattr(r, 'header_seen', None));"
            "print('ETAG', bool(getattr(r, 'etag', '')));",
        )

        assert _tagged(out, "KIND") == "NoCustomAvatar"
        assert _tagged(out, "SEEN") == "True", (
            "the header was not read, so this deployment would import nothing"
        )
        assert _tagged(out, "ETAG") == "True"

    def test_it_brings_back_a_user_set_avatar_to_import(self, stack):
        nextcloud = stack.service("nextcloud")
        nextcloud.set_avatar(nextcloud.bot_user, _picture())

        out = _run(
            stack,
            "from istota.nextcloud import avatars as a;"
            f"r = a.fetch_avatar(c, {nextcloud.bot_user!r});"
            "print('KIND', type(r).__name__);"
            "print('BYTES', len(getattr(r, 'image', b'')));",
        )

        assert _tagged(out, "KIND") == "RemoteAvatar"
        assert int(_tagged(out, "BYTES")) > 0

    def test_a_stored_etag_gets_a_304_and_learns_nothing(self, stack):
        """The whole point of recording the ETag beside a negative result."""
        nextcloud = stack.service("nextcloud")

        out = _run(
            stack,
            "from istota.nextcloud import avatars as a;"
            f"first = a.fetch_avatar(c, {nextcloud.test_user!r});"
            f"again = a.fetch_avatar(c, {nextcloud.test_user!r}, etag=first.etag);"
            "print('AGAIN', again is None);",
        )

        assert _tagged(out, "AGAIN") == "True"
