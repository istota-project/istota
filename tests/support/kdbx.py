"""``pykeepass.create_database`` with a key derivation tests can afford.

pykeepass's blank database carries Argon2 at 64 MiB and 14 iterations, so every
save and every open of a test vault cost about 0.3s on an idle machine and
several seconds under a full xdist run; the vault suites spent most of their
time deriving keys. A test asserts nothing about the cost parameters, and the
product reads them from the file, so a vault built here opens and saves in
about a millisecond through exactly the same code.

The full-cost derivation is paid once per worker, building the template.
"""

from __future__ import annotations

import io
from functools import lru_cache

_TEMPLATE_PASSWORD = "template"


@lru_cache(maxsize=1)
def _template_bytes() -> bytes:
    from pykeepass import create_database as _create

    kp = _create(io.BytesIO(), password=_TEMPLATE_PASSWORD)
    params = kp.kdbx.header.value.dynamic_header.kdf_parameters.data.dict
    params["I"].value = 1
    params["M"].value = 64 * 1024
    out = io.BytesIO()
    kp.save(out)
    return out.getvalue()


def create_database(filename, password=None, keyfile=None, transformed_key=None):
    """Same signature and return value as ``pykeepass.create_database``."""
    from pykeepass import PyKeePass

    kp = PyKeePass(io.BytesIO(_template_bytes()), password=_TEMPLATE_PASSWORD)
    kp.filename = filename
    kp.password = password
    kp.keyfile = keyfile
    kp.save(transformed_key=transformed_key)
    return kp
