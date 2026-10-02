"""Non-secret credential metadata, stored beside vault values."""

import ipaddress
import json
import re
from urllib.parse import urlsplit

DEFAULT_HEADERS = ["authorization", "private-token", "x-api-key", "x-auth-token"]
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9a-z-]+")


def credential_host(url, *, allow_http=False):
    """Exact authority; HTTP retains its scheme so it cannot share HTTPS grants."""
    if (not isinstance(url, str) or not url or not url.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or "\\" in url):
        raise ValueError("invalid credential host")
    parsed = urlsplit(url)
    host = parsed.hostname
    if (parsed.scheme not in (("https", "http") if allow_http else ("https",)) or not host or parsed.username is not None
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
    default_port = 80 if parsed.scheme == "http" else 443
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return "http://" + authority if parsed.scheme == "http" else authority


def https_host(url):
    return credential_host(url)


def parse_binding(url, attributes, tags, *, source="vault"):
    """Invalid host metadata unbinds the entry; it never retains an old host."""
    hosts = set()
    # Both are a value a person typed into a URL field: a KeePass entry, or a
    # credential added in Istota. Deployment config gets neither allowance.
    typed = source in ("vault", "local")
    try:
        if url:
            # A bare authority gets the https shorthand; actual request URLs
            # still require HTTPS.
            if typed and isinstance(url, str) and not any(c in url for c in "/?#@"):
                url = "https://" + url
            hosts.add(credential_host(url, allow_http=typed))
        for value in (attributes.get("istota_hosts") or "").split(","):
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
    from istota import db
    owner = binding.get("credential", name)
    previous = credential_name(conn, user_id, name)
    if previous != owner:
        # Never carry a field-only grant into another entry's shared policy.
        from .grants import delete_grant
        delete_grant(conn, user_id, name)
    db.kv_set(conn, user_id, "_credential_fields", name, owner)
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


def credential_name(conn, user_id, name):
    row = conn.execute("SELECT value FROM istota_kv WHERE user_id=? "
                       "AND namespace='_credential_fields' AND key=?", (user_id, name)).fetchone()
    return row[0] if row else name


def credential_groups(conn, user_id):
    """Entry membership comes from the vault parser, never name suffixes."""
    groups = {}
    for row in conn.execute("SELECT key FROM secrets WHERE user_id=? AND service='vault_entries'",
                            (user_id,)):
        name = row[0]
        groups.setdefault(credential_name(conn, user_id, name), []).append(name)
    return groups


def get_entry_binding(conn, user_id, name, groups=None):
    """``groups`` is ``credential_groups``' answer, for a caller looping over entries."""
    binding = get_binding(conn, user_id, name)
    if binding is not None:
        return binding
    if groups is None:
        groups = credential_groups(conn, user_id)
    # Entries without passwords still have username, URL or custom fields.
    for member in groups.get(name, []):
        binding = get_binding(conn, user_id, member)
        if binding is not None:
            return binding
    return None
