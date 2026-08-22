"""Plan utilization for a Claude Code subscription.

On a subscription deployment the dashboard's cost column is deliberately blank —
a plan-equivalent list price is not spend — so the real budget is the rate-limit
windows Anthropic reports at ``GET /api/oauth/usage``. This module is the one
place that knows that endpoint's shape: a pure parser, a thin blocking fetch, and
a disk cache with a TTL and a stale-fallback read. Three surfaces (the doctor
check, the admin card, ``!usage``) read it; none of them grows its own fetch or
its own parse.

**Nothing here raises.** Every entry point returns a ``UsageSnapshot``, and a
failure is a snapshot carrying a non-empty ``error``. Both callers reach it from
a diagnostic path — one of them is the daemon's boot sequence — where an
exception is worse than a missing number.

**Stdlib only** (``urllib.request``, not httpx), following the leaf convention of
``host_pressure.py``, ``forge_cli.py`` and ``ntfy_headers.py``: this is imported
from ``doctor.py``, which sits near the config-load path, and the request is one
GET with three headers.

Nothing here knows about doctor, the web app or the sandbox. Every path, the
environment, the clock and the transport are parameters. ``get_snapshot`` takes a
``Config`` only to read ``db_path`` and the ``brain.claude_code.*`` settings, and
reads them defensively so a config predating the block behaves as the shipping
default.

The credential is **read, never written and never refreshed**. See
``resolve_token``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from . import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

logger = logging.getLogger("istota.subscription_usage")

BASE_URL = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = f"istota/{__version__}"
_CACHE_FILENAME = "subscription_usage.json"

# Bound on the macOS Keychain probe. Short: it is a local lookup, and a hung
# `security` call would stall a doctor run or a dashboard refresh.
PROBE_TIMEOUT = 5.0

# Response bodies are small (a few kB). The cap is a guard against a proxy or a
# captive portal handing us something enormous, not a real size expectation.
_MAX_BODY_BYTES = 1 << 20
# HTTP error bodies are read for the DEBUG log only; they are never echoed into
# an error string a user or an operator sees.
_ERROR_BODY_CHARS = 200

# Settings defaults, kept in step with ``ClaudeCodeBrainConfig`` in config.py.
# They are duplicated here rather than imported because this module must stay a
# leaf and must behave correctly against a Config that predates the block.
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 10.0

NO_CREDENTIAL_ERROR = "no Claude Code OAuth credential found"
NO_WINDOWS_ERROR = "the endpoint returned no recognizable rate-limit windows"
DISABLED_ERROR = "disabled by config"

# Allowlist for the fallback parse path. Anything not named here is dropped,
# including the unreleased codenames the endpoint returns in the same top-level
# namespace. This is the one place the module deliberately renders less than the
# payload offers: an unshipped feature name must not reach a public project's
# dashboard.
TOP_LEVEL_WINDOWS: dict[str, str] = {
    "five_hour": "5-hour",
    "seven_day": "Weekly (all models)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_oauth_apps": "Weekly (OAuth apps)",
}

# Labels for the ``limits[]`` path. ``weekly_scoped`` takes its label from
# ``scope.model.display_name``, so it is not in this table. An unknown kind is
# *kept* here (labelled from the kind itself) rather than dropped: ``limits[]``
# entries are structured records, not a namespace shared with codenames.
LIMIT_KINDS: dict[str, str] = {
    "session": "5-hour",
    "weekly_all": "Weekly (all models)",
}

_SCOPED_KIND = "weekly_scoped"

# ``(url, headers, timeout) -> (status, body)``. Injectable so no test touches
# the network; the default is a small urllib wrapper.
Transport = Callable[[str, dict[str, str], float], "tuple[int, bytes]"]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageWindow:
    """One rate-limit window, from either parse path.

    ``key`` is a stable id (``"session"``, ``"weekly_all"``,
    ``"weekly_scoped:fable"``); ``label`` is display text. ``severity`` and
    ``is_active`` are the server's own — carried on the wire for a future use,
    never acted on and never filtered on here.
    """

    key: str
    label: str
    percent: float
    resets_at: str | None = None
    resets_in_seconds: int | None = None
    severity: str = ""
    is_active: bool | None = None


@dataclass(frozen=True)
class Spend:
    """Pay-as-you-go credits beyond the plan. Real money, unlike a token cost.

    Amounts are minor units (cents for USD); ``exponent`` is the number of minor
    units per major as a power of ten. Taken from the payload rather than
    hardcoded to 100, which is wrong for any currency that is not two-decimal.
    """

    enabled: bool = False
    used_minor: int = 0
    limit_minor: int = 0
    currency: str = "USD"
    exponent: int = 2
    percent: float = 0.0


@dataclass(frozen=True)
class UsageSnapshot:
    """A reading, or the reason there isn't one.

    ``fetched_at`` is when the data was *obtained*, not when it was read, so a
    cache hit carries the original fetch time. ``source`` names where the data
    came from: ``"fetch"``, ``"cache"``, ``"stale-cache"``, or ``"none"`` when
    there is no data at all (disabled, no credential, or a failed fetch with no
    cache to fall back on). A non-empty ``error`` means there is nothing usable
    here, and such a snapshot is never written to the cache.
    """

    fetched_at: float
    windows: tuple[UsageWindow, ...] = ()
    spend: Spend | None = None
    source: str = "fetch"
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when this snapshot carries usable data.

        A successful fetch that yielded no recognizable window sets
        ``NO_WINDOWS_ERROR``, so ``ok`` also implies at least one window.
        """
        return not self.error

    def age_seconds(self, now_ts: float) -> float:
        return now_ts - self.fetched_at


# ---------------------------------------------------------------------------
# Coercion helpers — same discipline as usage.py's _int / _float
# ---------------------------------------------------------------------------


def _percent(value: Any) -> float | None:
    """A utilization figure clamped to ``[0, 100]``, or ``None`` if unusable.

    ``bool`` is excluded because it is an ``int`` subclass and ``True`` would
    otherwise read as 1%. Non-finite floats are rejected: ``json.loads`` accepts
    the bare tokens ``NaN`` and ``Infinity``, so they really do arrive. A string
    is rejected too — a window with an unusable percent is dropped rather than
    rendered as zero, because a fabricated 0% on a full quota is the worst
    possible error here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    as_float = float(value)
    if not math.isfinite(as_float):
        return None
    return max(0.0, min(100.0, as_float))


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    return default


def _str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) and value else default


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _slug(value: str) -> str:
    """A display name reduced to a stable key fragment. May be empty."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _redact(text: str, token: str) -> str:
    """Remove a credential from a message built out of an exception string.

    Nothing in this module puts a token into an error deliberately, but an
    injected transport's exception text is not ours to trust, and this string
    ends up in a doctor ``detail`` and an admin payload.
    """
    if token and token in text:
        return text.replace(token, "***")
    return text


def _normalize_resets_at(value: Any) -> tuple[str | None, int | None]:
    """``(canonical ISO-8601 UTC, seconds until reset)``.

    An unparseable *string* is carried through verbatim (it is what the endpoint
    said) with ``None`` seconds; anything that is not a non-empty string yields
    ``(None, None)``. ``resets_in_seconds`` floors at 0 — clock skew between this
    host and Anthropic is normal and must not produce a negative duration.
    """
    if not isinstance(value, str) or not value:
        return None, None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return value, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    return canonical, parsed.timestamp()


def _window_times(value: Any, now_ts: float) -> tuple[str | None, int | None]:
    canonical, ts = _normalize_resets_at(value)
    if ts is None:
        return canonical, None
    return canonical, max(0, int(ts - now_ts))


# ---------------------------------------------------------------------------
# resolve_token
# ---------------------------------------------------------------------------


def resolve_token(env: Mapping[str, str], home: Path) -> tuple[str, str] | None:
    """``(token, source)`` from the first source that has one, else ``None``.

    Ordered: ``CLAUDE_CODE_OAUTH_TOKEN`` (``"env"``, what both server shapes
    actually set) → ``~/.claude/.credentials.json`` (``"file"``) → the macOS
    Keychain (``"keychain"``).

    **No expiry check, no refresh, no write, on any branch.** Three reasons, each
    sufficient on its own. The server credential's ``expiresAt`` is the sentinel
    ``"9999-12-31T23:59:59.999Z"`` — a *string* where the keychain blob holds
    epoch milliseconds as an *int*, so arithmetic on it raises on exactly the
    deployment shape this has to work on. The server credential has no
    ``refreshToken`` to refresh with. And a daemon rewriting
    ``~/.claude/.credentials.json`` would be racing the ``claude`` subprocesses
    it spawns for that same file. An expired token is reported as an error, not
    repaired.

    ``env`` and ``home`` are parameters so no test reads the real ones.
    """
    token = _str(env.get("CLAUDE_CODE_OAUTH_TOKEN")).strip()
    if token:
        return token, "env"

    token = _token_from_file(Path(home) / ".claude" / ".credentials.json")
    if token:
        return token, "file"

    token = _token_from_keychain(env)
    if token:
        return token, "keychain"

    return None


def _token_from_blob(text: str) -> str:
    """``claudeAiOauth.accessToken`` out of a credentials JSON blob, or ``""``."""
    try:
        blob = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed credential file is "no token"
        return ""
    return _str(_dict(_dict(blob).get("claudeAiOauth")).get("accessToken")).strip()


def _token_from_file(path: Path) -> str:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001 — unreadable / a directory / permissions
        logger.debug("credential file read failed: %s", path, exc_info=True)
        return ""
    return _token_from_blob(text)


def _token_from_keychain(env: Mapping[str, str]) -> str:
    """The macOS Keychain branch. Spawns nothing anywhere but Darwin."""
    try:
        if platform.system() != "Darwin":
            return ""
        account = _str(env.get("USER"))
        if not account:
            return ""
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        if proc.returncode != 0:
            return ""
        return _token_from_blob(proc.stdout or "")
    except Exception:  # noqa: BLE001 — a timeout, a missing binary, anything
        logger.debug("keychain credential lookup failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# parse_usage
# ---------------------------------------------------------------------------


def parse_usage(raw: object, *, now_ts: float) -> tuple[tuple[UsageWindow, ...], Spend | None]:
    """Parse a ``/api/oauth/usage`` payload into windows plus optional spend.

    Two overlapping views of the same fact exist in the payload. ``limits[]`` is
    the richer one — it carries the server's ``severity``, an ``is_active`` flag
    and a ``scope`` naming the model in a form fit for display — so it is the
    primary path; the allowlisted top-level keys are the fallback, taken when
    ``limits`` is absent, empty, or not a list. Both produce the same
    ``UsageWindow`` list, so nothing downstream learns which one ran.

    Pure, and never raises: a bad field drops its window, a bad payload yields
    ``((), None)``.
    """
    payload = _dict(raw)
    if not payload:
        return (), None

    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        windows = _parse_limits(limits, now_ts)
    else:
        windows = _parse_top_level(payload, now_ts)

    return tuple(windows), _parse_spend(payload)


def _parse_limits(entries: list, now_ts: float) -> list[UsageWindow]:
    out: list[UsageWindow] = []
    for entry in entries:
        try:
            window = _parse_one_limit(entry, now_ts)
        except Exception:  # noqa: BLE001 — one bad entry must not kill the rest
            logger.debug("rate-limit entry parse raised; skipping", exc_info=True)
            continue
        if window is not None:
            out.append(window)
    return out


def _parse_one_limit(entry: Any, now_ts: float) -> UsageWindow | None:
    if not isinstance(entry, dict):
        return None
    kind = _str(entry.get("kind"))
    if not kind:
        return None
    percent = _percent(entry.get("percent"))
    if percent is None:
        return None

    if kind == _SCOPED_KIND:
        named = _scoped_key_and_label(entry)
        if named is None:
            return None
        key, label = named
    else:
        key = kind
        label = LIMIT_KINDS.get(kind) or kind.replace("_", " ").capitalize()

    resets_at, resets_in = _window_times(entry.get("resets_at"), now_ts)
    is_active = entry.get("is_active")
    return UsageWindow(
        key=key,
        label=label,
        percent=percent,
        resets_at=resets_at,
        resets_in_seconds=resets_in,
        severity=_str(entry.get("severity")),
        is_active=is_active if isinstance(is_active, bool) else None,
    )


def _scoped_key_and_label(entry: dict) -> tuple[str, str] | None:
    """``weekly_scoped`` names itself from ``scope.model``.

    ``display_name`` first, then ``id``. When neither is usable the entry is
    dropped rather than sharing a key with another scoped window: two tiles with
    identical labels and different numbers is worse than one missing tile.
    (That drop is why the spec's third label fallback, ``"Weekly (scoped)"``, is
    unreachable and so not implemented.)
    """
    model = _dict(_dict(entry.get("scope")).get("model"))
    name = _str(model.get("display_name")) or _str(model.get("id"))
    slug = _slug(name) if name else ""
    if not slug:
        return None
    return f"{_SCOPED_KIND}:{slug}", f"Weekly ({name})"


def _parse_top_level(payload: dict, now_ts: float) -> list[UsageWindow]:
    out: list[UsageWindow] = []
    for key, label in TOP_LEVEL_WINDOWS.items():
        entry = payload.get(key)
        if not isinstance(entry, dict):
            continue
        percent = _percent(entry.get("utilization"))
        if percent is None:
            continue
        resets_at, resets_in = _window_times(entry.get("resets_at"), now_ts)
        out.append(
            UsageWindow(
                key=key,
                label=label,
                percent=percent,
                resets_at=resets_at,
                resets_in_seconds=resets_in,
            )
        )
    return out


def _parse_spend(payload: dict) -> Spend | None:
    """``spend`` if present, else the legacy ``extra_usage``, else ``None``."""
    spend = _dict(payload.get("spend"))
    if spend:
        used = _dict(spend.get("used"))
        limit = _dict(spend.get("limit"))
        currency = (
            _str(used.get("currency"))
            or _str(limit.get("currency"))
            or _str(spend.get("currency"), "USD")
        )
        exponent = _first_exponent(used, limit, spend)
        return Spend(
            enabled=spend.get("enabled") is True,
            used_minor=_int(used.get("amount_minor")),
            limit_minor=_int(limit.get("amount_minor")),
            currency=currency,
            exponent=exponent,
            percent=_percent(spend.get("percent")) or 0.0,
        )

    extra = _dict(payload.get("extra_usage"))
    if extra:
        return Spend(
            enabled=extra.get("is_enabled") is True,
            used_minor=_int(extra.get("used_credits")),
            limit_minor=_int(extra.get("monthly_limit")),
            currency=_str(extra.get("currency"), "USD"),
            exponent=_exponent(extra.get("decimal_places")),
            percent=_percent(extra.get("utilization")) or 0.0,
        )

    return None


def _exponent(value: Any) -> int:
    """Minor units per major, as a power of ten. Defaults to 2 when absent.

    Bounded: a nonsense exponent would become a division by an absurd power of
    ten in a renderer. The removed ``!usage`` divided by a hardcoded 100, which
    is wrong for any currency that is not two-decimal.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 2
    return value if 0 <= value <= 6 else 2


def _first_exponent(*blocks: dict) -> int:
    for block in blocks:
        value = block.get("exponent")
        if isinstance(value, int) and not isinstance(value, bool):
            return _exponent(value)
    return 2


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def _urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """The default ``Transport``: one GET, stdlib only.

    An ``HTTPError`` is a response, not a transport failure, so it is turned back
    into ``(status, body)`` — that is what routes a 401 or a 403 into the status
    branch instead of the exception branch.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001 — the status is what we needed
            body = b""
        return int(exc.code), body


def fetch_snapshot(
    token: str,
    *,
    timeout: float,
    now_ts: float,
    transport: Transport | None = None,
) -> UsageSnapshot:
    """One GET against the usage endpoint. Returns; never raises.

    The token goes into the ``Authorization`` header and nowhere else — not into
    the URL, not into a log line, and not into the returned snapshot. An HTTP
    error body is read for the DEBUG log only and never echoed into ``error``,
    which is built from a fixed set of literals plus a status code.
    """
    url = f"{BASE_URL}{USAGE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    send = transport or _urllib_transport

    try:
        status, body = send(url, headers, timeout)
    except Exception as exc:  # noqa: BLE001 — a diagnostic fetch never propagates
        logger.debug("subscription usage fetch failed", exc_info=True)
        detail = _redact(f"{type(exc).__name__}: {exc}", token)[:_ERROR_BODY_CHARS]
        return _failed(now_ts, f"could not reach api.anthropic.com ({detail})")

    if status != 200:
        logger.debug(
            "subscription usage endpoint returned HTTP %s: %s",
            status,
            _snippet(body),
        )
        return _failed(now_ts, f"the usage endpoint returned HTTP {status}")

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a proxy or a captive portal, most likely
        logger.debug("subscription usage response was not JSON", exc_info=True)
        return _failed(now_ts, "the usage endpoint returned a non-JSON response")

    windows, spend = parse_usage(payload, now_ts=now_ts)
    if not windows:
        return UsageSnapshot(
            fetched_at=now_ts, windows=(), spend=spend, source="fetch", error=NO_WINDOWS_ERROR
        )
    return UsageSnapshot(fetched_at=now_ts, windows=windows, spend=spend, source="fetch")


def _failed(now_ts: float, message: str) -> UsageSnapshot:
    return UsageSnapshot(fetched_at=now_ts, source="none", error=message)


def _snippet(body: bytes) -> str:
    try:
        return body.decode("utf-8", "replace")[:_ERROR_BODY_CHARS]
    except Exception:  # noqa: BLE001 — a debug log line is never worth an exception
        return ""


# ---------------------------------------------------------------------------
# disk cache
# ---------------------------------------------------------------------------


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / _CACHE_FILENAME


def _snapshot_to_json(snapshot: UsageSnapshot) -> dict:
    return {
        "version": 1,
        "fetched_at": snapshot.fetched_at,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "percent": w.percent,
                "resets_at": w.resets_at,
                "resets_in_seconds": w.resets_in_seconds,
                "severity": w.severity,
                "is_active": w.is_active,
            }
            for w in snapshot.windows
        ],
        "spend": None
        if snapshot.spend is None
        else {
            "enabled": snapshot.spend.enabled,
            "used_minor": snapshot.spend.used_minor,
            "limit_minor": snapshot.spend.limit_minor,
            "currency": snapshot.spend.currency,
            "exponent": snapshot.spend.exponent,
            "percent": snapshot.spend.percent,
        },
    }


def _windows_from_json(raw: Any) -> tuple[UsageWindow, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[UsageWindow] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = _str(entry.get("key"))
        label = _str(entry.get("label"))
        percent = _percent(entry.get("percent"))
        if not key or not label or percent is None:
            continue
        resets_in = entry.get("resets_in_seconds")
        is_active = entry.get("is_active")
        resets_at = entry.get("resets_at")
        out.append(
            UsageWindow(
                key=key,
                label=label,
                percent=percent,
                resets_at=resets_at if isinstance(resets_at, str) else None,
                resets_in_seconds=_int(resets_in) if isinstance(resets_in, (int, float)) else None,
                severity=_str(entry.get("severity")),
                is_active=is_active if isinstance(is_active, bool) else None,
            )
        )
    return tuple(out)


def _spend_from_json(raw: Any) -> Spend | None:
    if not isinstance(raw, dict):
        return None
    return Spend(
        enabled=raw.get("enabled") is True,
        used_minor=_int(raw.get("used_minor")),
        limit_minor=_int(raw.get("limit_minor")),
        currency=_str(raw.get("currency"), "USD"),
        exponent=_exponent(raw.get("exponent")),
        percent=_percent(raw.get("percent")) or 0.0,
    )


def _read_raw(path: Path) -> dict | None:
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — corrupt / truncated / permissions
        logger.debug("subscription usage cache read failed: %s", path, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def _snapshot_from_raw(raw: dict) -> UsageSnapshot | None:
    fetched_at = raw.get("fetched_at")
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return None
    if not math.isfinite(float(fetched_at)):
        return None
    windows = _windows_from_json(raw.get("windows"))
    if not windows:
        return None
    return UsageSnapshot(
        fetched_at=float(fetched_at),
        windows=windows,
        spend=_spend_from_json(raw.get("spend")),
        source="cache",
    )


def read_cache(path: Path, ttl_seconds: float, *, now_ts: float) -> UsageSnapshot | None:
    """The cached reading if it exists and is within TTL, else ``None``.

    A negative age — the clock moved backwards, or the file claims to be from
    next week — counts as stale rather than as fresh forever. Use
    ``read_cache_any_age`` for the stale-fallback path.
    """
    raw = _read_raw(path)
    if raw is None:
        return None
    snapshot = _snapshot_from_raw(raw)
    if snapshot is None:
        return None
    age = now_ts - snapshot.fetched_at
    if age < 0:
        return None
    if ttl_seconds > 0 and age > ttl_seconds:
        return None
    return snapshot


def read_cache_any_age(path: Path) -> UsageSnapshot | None:
    """The cached reading regardless of TTL. ``None`` if absent or unusable."""
    raw = _read_raw(path)
    if raw is None:
        return None
    return _snapshot_from_raw(raw)


def write_cache(path: Path, snapshot: UsageSnapshot) -> None:
    """Persist a successful reading. Best-effort; never raises.

    Two processes read and write this file — ``istota-scheduler`` and
    ``istota-web`` are separate units — so the write goes to a sibling ``.tmp``
    and is ``os.replace``d into place: a reader sees an old file or a new one,
    never a truncated one. Two processes racing a fetch is possible and harmless
    (one redundant request, last writer wins, both readings are equally true), so
    there is no lock to deadlock.

    ``0600``: the payload is not a credential, but it is account data and the
    data dir is shared. A snapshot carrying an ``error`` is never written — the
    cache holds successful readings only.
    """
    if snapshot.error or not snapshot.windows:
        return
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_snapshot_to_json(snapshot))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
        finally:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — a cache write is best-effort
        logger.debug("subscription usage cache write failed: %s", path, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — nothing left to do about it
            pass


# ---------------------------------------------------------------------------
# get_snapshot — the only function the callers use
# ---------------------------------------------------------------------------


def _settings(config: object) -> tuple[bool, float, float]:
    """``(enabled, ttl_seconds, timeout_seconds)`` from ``brain.claude_code``.

    Read defensively: this module must not import ``Config`` (leaf), and a
    deployment whose config predates ``[brain.claude_code]`` gets the shipping
    defaults rather than an ``AttributeError`` on the daemon's boot path. The
    loader clamps these fields; the floors here are a second guard so a value
    that reached the dataclass some other way cannot turn the TTL into a fetch
    on every dashboard poll.
    """
    block = getattr(getattr(config, "brain", None), "claude_code", None)
    enabled = getattr(block, "subscription_usage", True)
    ttl = _positive(
        getattr(block, "subscription_usage_cache_ttl_seconds", None), DEFAULT_CACHE_TTL_SECONDS
    )
    timeout = _positive(
        getattr(block, "subscription_usage_timeout_seconds", None), DEFAULT_TIMEOUT_SECONDS
    )
    return bool(enabled), ttl, timeout


def _positive(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    as_float = float(value)
    if not math.isfinite(as_float) or as_float < 1:
        return default
    return as_float


def _data_dir(config: object) -> Path | None:
    db_path = getattr(config, "db_path", None)
    if not db_path:
        return None
    try:
        return Path(db_path).parent
    except Exception:  # noqa: BLE001 — a nonsense db_path only costs the cache
        return None


def get_snapshot(
    config: "Config",
    *,
    now_ts: float,
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> UsageSnapshot:
    """The whole policy, in the one function the callers use.

    1. Disabled by config → ``source="none"``, ``error="disabled by config"``.
    2. Fresh cache within TTL → return it, ``source="cache"``. No request.
    3. No credential → an error snapshot, **not cached**: absence of a credential
       is cheap to re-check and caching it would outlive the fix.
    4. Fetch. On success, write the cache and return ``source="fetch"``.
    5. On failure, fall back to a cache of any age: ``source="stale-cache"`` with
       the fetch error preserved, so the caller can decide what an old-but-real
       reading is worth. No cache to fall back on → the fetch failure.

    ``env`` and ``home`` default to this process's own and are parameters only so
    a test never reads the real ones. Never raises.
    """
    enabled, ttl, timeout = _settings(config)
    if not enabled:
        return UsageSnapshot(fetched_at=0.0, source="none", error=DISABLED_ERROR)

    data_dir = _data_dir(config)
    path = cache_path(data_dir) if data_dir is not None else None

    if path is not None:
        cached = read_cache(path, ttl, now_ts=now_ts)
        if cached is not None:
            return cached

    resolved = resolve_token(
        os.environ if env is None else env,
        Path.home() if home is None else home,
    )
    if resolved is None:
        return UsageSnapshot(fetched_at=0.0, source="none", error=NO_CREDENTIAL_ERROR)

    snapshot = fetch_snapshot(resolved[0], timeout=timeout, now_ts=now_ts, transport=transport)
    if not snapshot.error:
        if path is not None:
            write_cache(path, snapshot)
        return snapshot

    stale = read_cache_any_age(path) if path is not None else None
    if stale is not None:
        return replace(stale, source="stale-cache", error=snapshot.error)
    return snapshot
