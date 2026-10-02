"""Deployment signing material and the public trust files for one task.

The private record contains both key and certificate so a crash cannot publish
half of a CA. Deleting that record and restarting rotates the deployment CA.
Only public certificates are written to task control directories.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import ipaddress
import os
import re
from pathlib import Path
import ssl
import stat
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from istota.lib.atomic_write import write_bytes_atomic


@dataclass
class Leaf:
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    certificate: x509.Certificate
    context: ssl.SSLContext | None = field(default=None, repr=False)


@dataclass
class Authority:
    state_dir: Path
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    certificate: x509.Certificate
    leaves: OrderedDict = field(default_factory=OrderedDict, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def state_directory(config) -> Path:
    """Refuse a state location covered by any configured sandbox bind."""
    from istota.sandbox.plan import sandbox_bound_reason

    if not config.db_path:
        raise ValueError("credential broker requires a daemon database directory")
    path = Path(config.db_path).absolute().parent / "credential-broker"
    # The temp-root predicate allows direct child *files*. This path is a
    # directory, so also check its record: a user called credential-broker
    # would otherwise receive the directory as their writable task temp.
    if sandbox_bound_reason(config, path) or sandbox_bound_reason(config, path / "ca-key.pem"):
        raise ValueError("credential broker CA state must be outside sandbox binds")
    return path


def _private_record(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("credential broker CA record must be a daemon-owned file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("credential broker CA record requires permissions 0600")
        return handle.read()


def load_or_create_ca(state_dir: Path) -> Authority:
    """Load or atomically create one P-256 CA, serialized across processes."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory_fd).st_uid != os.geteuid():
            raise ValueError("credential broker CA directory must be daemon-owned")
        os.fchmod(directory_fd, 0o700)
    finally:
        os.close(directory_fd)
    lock_fd = os.open(state_dir / "ca.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "rb") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        path = state_dir / "ca-key.pem"
        try:
            record = _private_record(path)
        except FileNotFoundError:
            key = ec.generate_private_key(ec.SECP256R1())
            now = datetime.now(timezone.utc)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Istota credential broker CA")])
            cert = (
                x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                    crl_sign=True, encipher_only=None, decipher_only=None,
                ), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256())
            )
            record = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ) + cert.public_bytes(serialization.Encoding.PEM)
            write_bytes_atomic(path, record, mode=0o600, fsync=True)
        return _authority_from_record(state_dir, record)


def read_ca(state_dir: Path) -> Authority:
    """Read and validate existing state without creating or repairing anything."""
    info = state_dir.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("credential broker CA directory requires private daemon ownership")
    return _authority_from_record(state_dir, _private_record(state_dir / "ca-key.pem"))


def _authority_from_record(state_dir: Path, record: bytes) -> Authority:
    key = serialization.load_pem_private_key(record, password=None)
    cert = x509.load_pem_x509_certificate(record)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("credential broker CA requires a P-256 key")
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise ValueError("credential broker CA key and certificate differ")
    cert.verify_directly_issued_by(cert)
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError("credential broker CA has expired or is not yet valid; rotate it")
    if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("credential broker certificate is not a CA")
    return Authority(state_dir, key, cert)


def mint_leaf(authority: Authority, host: str, *, validity_hours: int = 24,
              now: datetime | None = None) -> Leaf:
    """Mint a server leaf with an exact DNS/IP SAN; keep at most 256 in memory."""
    if not isinstance(validity_hours, int) or isinstance(validity_hours, bool) or validity_hours <= 0:
        raise ValueError("leaf validity must be a positive number of hours")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or len(host) > 253 or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            or any(not (c.isascii() and (c.isalnum() or c == "-")) for c in label)
            for label in host.split(".")
        ):
            raise ValueError("invalid certificate hostname")
        san = x509.DNSName(host)
    else:
        host = str(address)
        san = x509.IPAddress(address)
    now = now or datetime.now(timezone.utc)
    with authority.lock:
        cache_key = (host, validity_hours)
        cached = authority.leaves.get(cache_key)
        if cached and cached.certificate.not_valid_before_utc <= now < cached.certificate.not_valid_after_utc:
            authority.leaves.move_to_end(cache_key)
            return cached
        if not authority.certificate.not_valid_before_utc <= now < authority.certificate.not_valid_after_utc:
            raise ValueError("credential broker CA has expired or is not yet valid; rotate it")
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
            .issuer_name(authority.certificate.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(min(now + timedelta(hours=validity_hours), authority.certificate.not_valid_after_utc))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                authority.private_key.public_key(),
            ), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(authority.private_key, hashes.SHA256())
        )
        leaf = Leaf(key, cert)
        authority.leaves[cache_key] = leaf
        authority.leaves.move_to_end(cache_key)
        while len(authority.leaves) > 256:
            authority.leaves.popitem(last=False)
        return leaf


def server_context(authority: Authority, host: str, *, validity_hours: int = 24) -> ssl.SSLContext:
    """HTTP/1.1 server context. The intercept caller must enforce SNI and Host."""
    with authority.lock:
        leaf = mint_leaf(authority, host, validity_hours=validity_hours)
        if leaf.context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(["http/1.1"])
            # SSLContext accepts files only. Keep the leaf key in daemon state
            # just long enough to load it, never in a model-readable temp dir.
            with tempfile.NamedTemporaryFile(dir=authority.state_dir) as pem:
                pem.write(leaf.private_key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ))
                pem.write(leaf.certificate.public_bytes(serialization.Encoding.PEM))
                pem.flush()
                context.load_cert_chain(pem.name)
            leaf.context = context
        return leaf.context


def upstream_context() -> ssl.SSLContext:
    """Verify upstream TLS with daemon trust, without the broker CA."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    return context


def write_trust_bundle(authority: Authority, task_dir: Path) -> dict[str, str]:
    """Write only public roots and return the model-only exec environment."""
    task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    system = upstream_context()
    # OpenSSL loads capath certificates lazily, so get_ca_certs alone omits
    # roots available only through a hashed certificate directory.
    capath = ssl.get_default_verify_paths().capath
    for directory in capath.split(os.pathsep) if capath else ():
        for entry in Path(directory).iterdir():
            if not re.fullmatch(r"[0-9a-fA-F]{8}\.[0-9]+", entry.name):
                continue
            try:
                system.load_verify_locations(cafile=str(entry))
            except (OSError, ssl.SSLError):
                continue
    roots = system.get_ca_certs(binary_form=True)
    if not roots:
        raise ValueError("no system CA certificates available for the broker trust bundle")
    public_ca = authority.certificate.public_bytes(serialization.Encoding.PEM)
    combined = b"".join(ssl.DER_cert_to_PEM_cert(cert).encode("ascii") for cert in roots)
    bundle = task_dir / "ca-bundle.pem"
    ca_file = task_dir / "broker-ca.pem"
    write_bytes_atomic(bundle, combined + public_ca, mode=0o600)
    write_bytes_atomic(ca_file, public_ca, mode=0o600)
    return {
        "SSL_CERT_FILE": str(bundle), "REQUESTS_CA_BUNDLE": str(bundle),
        "CURL_CA_BUNDLE": str(bundle), "GIT_SSL_CAINFO": str(bundle),
        "NODE_EXTRA_CA_CERTS": str(ca_file),
    }
