"""The rclone remote for a Nextcloud account, and rclone's password obscuring.

Two writers produce the VM's ``rclone.conf``: the container wizard
(``setup_wizard.write_container_setup``, which stages it for ``istota-stack``)
and the management role's variable converter (``maintenance.role_vars``). Both
render through ``render_webdav_remote``, so the two cannot disagree on the
path the mount uses. The wizard goes through ``webdav_remote_for``, which keeps
the staged copy of the installed file when it already says the same thing.

``obscure`` is rclone's own scheme (``fs/config/obscure``): AES-256-CTR under a
key published in rclone's source, a random 16-byte IV prepended, base64url
without padding. It is reimplemented here because the image has no rclone. It
is not encryption: anyone with rclone can reveal the value. The file's 0600
mode and uid 10001 ownership are what protect it.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: rclone's fixed obscuring key, from `fs/config/obscure/obscure.go`.
_CRYPT_KEY = bytes([
    0x9C, 0x93, 0x5B, 0x48, 0x73, 0x0A, 0x55, 0x4D,
    0x6B, 0xFD, 0x7C, 0x63, 0xC8, 0x86, 0xA9, 0x2B,
    0xD3, 0x90, 0x19, 0x8E, 0xB8, 0x12, 0x8A, 0xFB,
    0xF4, 0xDE, 0x16, 0x2B, 0x8B, 0x95, 0xF6, 0x38,
])
_IV_LEN = 16


def _new_iv() -> bytes:
    return os.urandom(_IV_LEN)


def _ctr(iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(_CRYPT_KEY), modes.CTR(iv))
    transform = cipher.encryptor()
    return transform.update(data) + transform.finalize()


def obscure(value: str, *, iv: bytes | None = None) -> str:
    """``rclone obscure value``. ``iv`` is for a fixed test vector only."""
    iv = _new_iv() if iv is None else iv
    if len(iv) != _IV_LEN:
        raise ValueError("the IV must be 16 bytes")
    raw = iv + _ctr(iv, value.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def reveal(obscured: str) -> str:
    """``rclone reveal obscured``: the inverse of ``obscure``."""
    padded = obscured + "=" * (-len(obscured) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    if len(raw) < _IV_LEN:
        raise ValueError("input too short to be an obscured value")
    return _ctr(raw[:_IV_LEN], raw[_IV_LEN:]).decode("utf-8")


def nextcloud_files_url(url: str, username: str) -> str:
    """The account's WebDAV files root, the path the rclone mount serves."""
    return f"{url.rstrip('/')}/remote.php/dav/files/{username}/"


def render_webdav_remote(remote: str, url: str, username: str, obscured_password: str) -> str:
    """One ``[remote]`` section for a Nextcloud account at ``url``.

    ``url`` is the Nextcloud base address, as the config's ``[nextcloud] url``
    holds it. A value with a line break would add lines to the file, so it is
    refused rather than written.
    """
    for value in (remote, url, username, obscured_password):
        if "\n" in value or "\r" in value:
            raise ValueError("an rclone.conf value contains a line break")
    return (
        f"[{remote}]\n"
        "type = webdav\n"
        f"url = {nextcloud_files_url(url, username)}\n"
        "vendor = nextcloud\n"
        f"user = {username}\n"
        f"pass = {obscured_password}\n"
    )


def webdav_remote_for(existing: str | None, remote: str, url: str, username: str, password: str) -> str:
    """The remote for these answers, keeping ``existing`` when it already says it.

    ``obscure`` is salted, so rendering the same answers twice gives two files,
    and ``provision.sh --digest`` hashes the file: every re-run would read as a
    host change (#720). ``existing`` is kept only when it is byte for byte the
    rendering of these answers under its own obscured password.
    """
    for line in (existing or "").splitlines():
        if not line.startswith("pass = "):
            continue
        kept = line.removeprefix("pass = ")
        try:
            same_password = reveal(kept) == password
        except (ValueError, UnicodeDecodeError):
            same_password = False
        if same_password and render_webdav_remote(remote, url, username, kept) == existing:
            return existing
        break
    return render_webdav_remote(remote, url, username, obscure(password))
