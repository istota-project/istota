"""Whether an address is a routable public one: the SSRF rule, in one place.

Daemon-network callers reach a host somebody else chose and must not be
turned against the host's own network: the native WebFetch tool
(`session/tools/web_fetch.py`), and three host-side callers with no egress
confinement at all: `mail/connect.py` (a user's connected mail account), and
the `wordpress` and `servarr` skill CLIs. Each resolves the host, refuses the
request if *any* resolved address is non-public unless the operator listed
the exact name, and connects to the address it checked. The resolver
(`resolve_host`, `host_addresses`), the listed-name rule (`normal_host`,
`host_set`), the check (`first_non_public`) and the three composed
(`checked_addresses`) are here so the host-side callers share one copy; each
keeps its own error vocabulary. The HTTP half, a request pinned to a checked
address and a guarded send, is `sandbox/guarded_http.py`, which imports httpx
and so cannot live in this leaf.

Lifted out of `web_fetch` rather than imported from it, because importing that
module from a skill pulls in the native tool package (about fifty modules), and
it is a leaf for the reason `untrusted.py` is one: `web_fetch` runs inside the
tool server, which may not import `istota.skills`.

stdlib-only leaf, never raises.
"""

from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)

# Explicit private/reserved networks refused by `ip_is_public`. Kept explicit
# (rather than relying only on ipaddress' is_private/is_reserved flags) so the
# blocklist is auditable and testable, and so CGNAT + benchmarking ranges that
# some Python versions don't fold into is_private are always covered.
BLOCKED_V4 = (
    "0.0.0.0/8",  # "this host"
    "10.0.0.0/8",  # RFC1918
    "100.64.0.0/10",  # CGNAT (RFC6598)
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local (blocks 169.254.169.254 metadata)
    "172.16.0.0/12",  # RFC1918
    "192.168.0.0/16",  # RFC1918
    "198.18.0.0/15",  # benchmarking
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved
)
BLOCKED_V6 = (
    "::1/128",  # loopback
    "::/128",  # unspecified
    "::/96",  # deprecated IPv4-compatible (::a.b.c.d)
    "64:ff9b::/96",  # NAT64 (embeds an IPv4 — could translate to a private v4)
    "64:ff9b:1::/48",  # local-use NAT64
    "2002::/16",  # 6to4 (embeds an IPv4)
    "fc00::/7",  # ULA (covers fd00:ec2::254 AWS metadata)
    "fe80::/10",  # link-local
    "fec0::/10",  # deprecated site-local (RFC3879 — NOT flagged by stdlib is_private)
    "ff00::/8",  # multicast
)
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(c) for c in (BLOCKED_V4 + BLOCKED_V6)
)


def parse_cidrs(cidrs) -> tuple:
    """Operator-supplied CIDRs as networks; an invalid one is logged and skipped."""
    out = []
    for c in cidrs or ():
        try:
            out.append(ipaddress.ip_network(str(c), strict=False))
        except ValueError:
            logger.warning("ignoring invalid blocked CIDR %r", c)
    return tuple(out)


def ip_is_public(ip, extra_blocked=()) -> bool:
    """True iff ``ip`` is a routable public address (not private/reserved).

    Pure over an ``ipaddress`` address. IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is
    unwrapped to the embedded IPv4 and re-checked — a common bypass of
    validators that only canonicalize IPv4 strings.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    for net in BLOCKED_NETWORKS + parse_cidrs(extra_blocked):
        if ip.version == net.version and ip in net:
            return False

    # Backstop: anything the stdlib flags private/reserved even if a CIDR above
    # missed it (e.g. IETF protocol assignments).
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return False
    return True


def normal_host(host) -> str:
    """A host name as the listed-name rule compares it: trimmed, lowercase, no root dot."""
    return str(host).strip().lower().rstrip(".")


def host_set(hosts) -> frozenset:
    """An operator's ``private_hosts`` list as the exact names it allows."""
    return frozenset(normal_host(h) for h in (hosts or ()) if str(h).strip())


def resolve_host(host: str, port: int) -> list:
    """Every address ``host`` resolves to, in resolver order; empty on failure."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    seen = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def host_addresses(host: str, port: int, *, resolve=None) -> list:
    """``host``'s addresses as ``ipaddress`` objects, a literal unresolved.

    A scoped IPv6 answer's ``%zone`` is dropped before parsing; anything that
    does not parse is skipped. Empty when nothing resolved. ``resolve``
    defaults to `resolve_host`, and takes ``(host, port)``.
    """
    try:
        return [ipaddress.ip_address(normal_host(host))]
    except ValueError:
        pass
    addresses = []
    for raw in (resolve or resolve_host)(host, port):
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses


def first_non_public(addresses, extra_blocked=()):
    """The first address `ip_is_public` refuses, or None when all are public."""
    for address in addresses:
        if not ip_is_public(address, extra_blocked):
            return address
    return None


def checked_addresses(host, port: int, *, private_hosts, resolve=None) -> tuple[list, object]:
    """The rule each host-side caller applies before connecting, composed once.

    ``(addresses, None)`` when every address ``host`` resolves to is public, or
    the exact name is in ``private_hosts``: the addresses as strings, in
    resolver order, which is what the caller connects to. ``([], address)``
    when ``address`` is the first non-public one and the name is not listed.
    ``([], None)`` when nothing resolved. The two empty answers stay apart
    because the callers word them differently: an unreachable host, and a
    refused one.
    """
    addresses = host_addresses(host, port, resolve=resolve)
    if not addresses:
        return [], None
    if normal_host(host) not in host_set(private_hosts):
        refused = first_non_public(addresses)
        if refused is not None:
            return [], refused
    return [str(address) for address in addresses], None
