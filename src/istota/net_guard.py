"""Whether an address is a routable public one: the SSRF rule, in one place.

Two daemon-network callers fetch a URL somebody else chose and must not be
turned against the host's own network: the native WebFetch tool
(`session/tools/web_fetch.py`) and the `wordpress` skill CLI, which runs
host-side with no egress confinement at all. Both resolve the host, refuse the
request if *any* resolved address is non-public, and connect to the address
they checked.

Lifted out of `web_fetch` rather than imported from it, because importing that
module from a skill pulls in the native tool package (about fifty modules), and
it is a leaf for the reason `untrusted.py` is one: `web_fetch` runs inside the
tool server, which may not import `istota.skills`.

stdlib-only leaf, never raises.
"""

from __future__ import annotations

import ipaddress
import logging

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
