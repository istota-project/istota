"""Self-signed TLS material for the mail service.

istota's email transport picks its TLS mode by port number and always verifies
the server certificate — `skills/email/__init__.py` builds its context with
`ssl.create_default_context()` and there is no plaintext or no-verify path. That
is a property worth keeping, so the testbed gives the mail server a real
certificate and arranges for both readers to trust it, rather than adding a
TLS-weakening knob to production code so a test can run.

Two readers, one certificate, and that is why the SAN list has three entries by
default. A process inside the compose network reaches the server as `mail`, the
service name; the pytest process reaches the same container as `localhost` or
`127.0.0.1` on a published port. The certificate is self-signed, so the leaf is
its own trust anchor: the container side appends it to
`/etc/ssl/certs/ca-certificates.crt`, and the host side points `SSL_CERT_FILE`
at it.

**Never write this into the checkout.** A private key in the tree trips
`gitleaks` in `.githooks/pre-commit`, correctly. It would also enter the Docker
build context — both compose files build with `context: ..`, so anything under
the repo root is copied into every image the tier builds, including the one the
`image` tier asserts against. Callers pass a `tmp_path`-derived directory.

This is the one module in the package with a third-party import. The rest is
stdlib on purpose (see `pyproject.toml`), and the exception is argued there: the
alternative is shelling out to `openssl`, which is what both external rigs do
today and which is one of the things they are copying.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import stat
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

#: What the mail server is reached by, from the two sides that reach it.
#:
#: `mail` is the compose service name, which is how the istota container and any
#: other container in the project resolve it. `localhost` and `127.0.0.1` are
#: the published port on the host, which is how the wire suite reaches it.
DEFAULT_SANS: tuple[str, ...] = ("mail", "localhost", "127.0.0.1")

CERT_NAME = "mail.crt"
KEY_NAME = "mail.key"

#: Long enough that a developer's checkout never fails for a reason that is not
#: about istota. Nothing outside this rig ever trusts it.
VALIDITY_DAYS = 3650

KEY_MODE = 0o600


def generate_self_signed(
    out_dir: Path, sans: tuple[str, ...] | list[str] = DEFAULT_SANS
) -> tuple[Path, Path]:
    """Write `mail.crt` / `mail.key` into `out_dir` and return `(crt, key)`.

    RSA 2048, SHA-256, ten-year validity, `CN=mail`, and a `subjectAltName`
    covering every entry in `sans` — as an `IPAddress` for anything that parses
    as one and a `DNSName` otherwise, because a verifying client matching
    `127.0.0.1` against a `DNSName` entry fails and says nothing useful about
    why.

    Idempotent: if both files are already present it returns them untouched.
    The caller is a session fixture and a compose bind mount, both of which may
    run twice against one directory, and regenerating underneath a running
    container would leave the server holding a key the client no longer trusts.

    The key is *created* 0600 rather than chmod-ed to it afterwards. A window
    between the two is a private key readable by every account on the machine,
    and it is exactly the window a test that checks the final mode cannot see.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    crt_path = out_dir / CERT_NAME
    key_path = out_dir / KEY_NAME
    if crt_path.exists() and key_path.exists():
        return crt_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "mail"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "istota testbed"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        # Self-signed: the leaf is its own issuer and its own trust anchor.
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # A minute of backdating, so a container whose clock is a few seconds
        # behind the host does not reject a certificate issued "in the future".
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(sans)), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    _write_private(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    crt_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return crt_path, key_path


def _san_entries(sans: tuple[str, ...] | list[str]) -> list[x509.GeneralName]:
    """One `GeneralName` per entry, an IP where the entry parses as one.

    Split here rather than at the call site so the rule has one home and a test
    can drive it directly.
    """
    entries: list[x509.GeneralName] = []
    for name in sans:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return entries


def _write_private(path: Path, payload: bytes) -> None:
    """Create `path` with mode 0600 and write `payload` into it.

    `os.open` with the mode rather than `write_bytes` then `chmod`, so the file
    never exists at the process umask. `stack.py` learned the same lesson about
    the compose env-file; the reasoning is identical and the fix is deliberately
    the same shape.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KEY_MODE)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    # An existing file keeps its old mode through `O_CREAT`, so say it again for
    # the idempotent-overwrite path. Cheap, and the alternative is a key that is
    # 0600 on a fresh directory and 0644 on a reused one.
    os.chmod(path, KEY_MODE)


def key_is_private(path: Path) -> bool:
    """True when `path` is readable by its owner alone.

    Used by the package's own tests. A helper rather than an inline `st_mode`
    mask in three places, because getting the mask wrong makes the assertion
    pass on a world-readable key.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
