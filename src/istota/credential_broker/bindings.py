"""Non-secret credential metadata, stored beside vault values."""

import ipaddress
import json
import re
from urllib.parse import urlsplit

DEFAULT_HEADERS = ["authorization", "private-token", "x-api-key", "x-auth-token"]
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9a-z-]+")


def https_host(url):
    """Canonical exact HTTPS authority, or a refusal for an unsafe URL."""
    if (not isinstance(url, str) or not url or not url.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or "\\" in url):
        raise ValueError("invalid credential host")
    parsed = urlsplit(url)
    host = parsed.hostname
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.netloc.endswith(":")):
        raise ValueError("credential hosts require HTTPS without user information")
    if ":" in host:
        host = f"[{ipaddress.IPv6Address(host)}]"
    elif (len(host) > 253 or not all(re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label,
    ) for label in host.split("."))):
        raise ValueError("credential hosts must be exact names")
    port = parsed.port
    if port == 0:
        raise ValueError("invalid credential port")
    return host if port in (None, 443) else f"{host}:{port}"


def parse_binding(url, attributes, tags, *, source="vault"):
    """Invalid host metadata unbinds the entry; it never retains an old host."""
    hosts = set()
    try:
        if url:
            hosts.add(https_host(url))
        for value in attributes.get("istota_hosts", "").split(","):
            value = value.strip()
            if not value:
                continue
            if any(c in value for c in "/?#@"):
                raise ValueError("expected host[:port]")
            hosts.add(https_host("https://" + value))
    except (TypeError, ValueError):
        hosts.clear()
    raw_headers = attributes.get("istota_headers")
    headers = DEFAULT_HEADERS if raw_headers is None else [
        h.strip().lower() for h in raw_headers.split(",")
    ]
    headers = sorted({h for h in headers if _HEADER.fullmatch(h)
                      and h != "proxy-authorization"})
    return {"hosts": sorted(hosts), "headers": headers,
            "revealable": "istota:reveal" in (tags or []), "source": source}


def put_binding(conn, user_id, name, binding):
    """Caller owns the transaction, including the credential value write."""
    conn.execute("""
        INSERT INTO credential_bindings (user_id, name, hosts, headers, revealable, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, name) DO UPDATE SET
            hosts=excluded.hosts, headers=excluded.headers,
            revealable=excluded.revealable, source=excluded.source,
            updated_at=datetime('now')
    """, (user_id, name, json.dumps(binding["hosts"]), json.dumps(binding["headers"]),
          int(binding["revealable"]), binding["source"]))


def get_binding(conn, user_id, name):
    row = conn.execute("""
        SELECT hosts, headers, revealable, source FROM credential_bindings
        WHERE user_id=? AND name=?
    """, (user_id, name)).fetchone()
    if row is None:
        return None
    return {"hosts": json.loads(row[0]), "headers": json.loads(row[1]),
            "revealable": bool(row[2]), "source": row[3]}


def forge_bindings(developer):
    """Deployment tokens retain their config values and have a separate namespace."""
    result = {}
    for forge in ("gitlab", "github"):
        if not getattr(developer, forge + "_token"):
            continue
        binding = parse_binding(getattr(developer, forge + "_url"), {}, [], source="config")
        if forge == "github" and binding["hosts"] == ["github.com"]:
            binding["hosts"].insert(0, "api.github.com")
        result["forge." + forge] = binding
    return result


def sync_forge_bindings(conn, user_id, developer, *, available_names=None):
    bindings = forge_bindings(developer)
    if available_names is not None:
        bindings = {name: binding for name, binding in bindings.items() if name in available_names}
    conn.execute("DELETE FROM credential_bindings WHERE user_id=? AND source='config'",
                 (user_id,))
    for name, binding in bindings.items():
        put_binding(conn, user_id, name, binding)
    return bindings
